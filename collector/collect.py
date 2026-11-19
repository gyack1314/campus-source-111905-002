"""收集编排：把来源读取层与本地证据库接成可中断接续的一轮轮收集。

一轮收集的边界语义：
1. 以本地水位（上一轮确认的 consistent-through）作为 since 连续翻页，more 只跟
   当前查询，不代表更早发生的活动已经收齐；
2. 只有把本查询所有页翻完，才采用本轮各页中最小的 consistent-through 作为新水
   位（保守取值，防止来源边界回退造成漏读）；stored 晚于该边界的语句照样留存，
   但下一轮仍会再次读到，由语句 id 幂等去重；
3. 作废是单向事实：voided 语句先到时先登记未闭合引用，目标原文后到也不会恢复
   为有效活动；附件后补齐只改变材料完整性，不撤销作废。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .client import LrsClient, MaterialUnavailable, SourceError, is_voiding, void_target
from .store import EvidenceStore


@dataclass
class CollectReport:
    run_id: int
    status: str
    since: Optional[str]
    consistent_through: Optional[str]
    pages: int = 0
    seen_statements: int = 0
    new_statements: int = 0
    beyond_boundary: list[str] = field(default_factory=list)
    blobs_verified: int = 0
    blobs_reused: int = 0
    blobs_unavailable: list[str] = field(default_factory=list)
    blobs_corrupt: list[str] = field(default_factory=list)
    void_links_open: list[str] = field(default_factory=list)
    void_links_closed: list[str] = field(default_factory=list)
    note: Optional[str] = None

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def collect_once(client: LrsClient, store: EvidenceStore) -> CollectReport:
    """执行一轮完整收集并返回报告；任何来源异常都会把运行标记为 interrupted。"""
    since = store.get_cursor()
    run_id = store.start_run(since)
    report = CollectReport(run_id=run_id, status="running", since=since,
                           consistent_through=None)
    try:
        page_boundaries: list[str] = []
        seen_ids: list[str] = []
        for page in client.get_statements(since=since, attachments=True):
            report.pages += 1
            page_boundaries.append(page.consistent_through)
            multipart = {part.sha2: part for part in page.attachment_parts}
            for statement in page.statements:
                _ingest_statement(statement, store, run_id, report, multipart)
                seen_ids.append(statement["id"])
            report.seen_statements += len(page.statements)

        if not page_boundaries:
            # 来源至少应返回首屏（哪怕为空）；完全无页视为传输异常。
            raise SourceError("未取得任何语句页，无法确认稳定可见边界")

        boundary = min(page_boundaries)
        report.consistent_through = boundary
        # stored 晚于确认边界的语句：留存但不视为已闭合范围内的证据。
        report.beyond_boundary = [
            sid for sid in seen_ids
            if store.conn.execute("SELECT stored FROM statements WHERE statement_id=?",
                                  (sid,)).fetchone()["stored"] > boundary
        ]

        _fetch_pending_materials(client, store, run_id, report)
        _reconcile_open_voids(client, store, run_id, report)

        store.advance_cursor(boundary, run_id)
        report.status = "done"
        report.void_links_open = store.open_void_targets()
        store.finish_run(run_id, "done", boundary)
        return report
    except (SourceError, OSError) as exc:
        report.status = "interrupted"
        report.note = str(exc)
        # 不推进水位：下次仍从旧 since 接续，漏掉的页与附件会重新进入。
        store.finish_run(run_id, "interrupted", None, note=str(exc))
        return report


def _ingest_statement(statement: dict, store: EvidenceStore, run_id: int,
                      report: CollectReport, multipart: dict) -> None:
    sid = statement["id"]
    if store.upsert_statement(statement, run_id):
        report.new_statements += 1

    if is_voiding(statement):
        target = void_target(statement)
        if target:
            store.add_void_link(sid, target, run_id)
            if store.has_statement(target):
                report.void_links_closed.append(target)
            else:
                report.void_links_open.append(target)
        return

    for att in statement.get("attachments", []):
        sha2 = att["sha2"].lower()
        # 每条语句都必须留下自己的附件引用行，即使该 sha2 的字节已被别的语句
        # 复用核验过——否则该语句的材料完整性与来源地址将无处可查。
        store.upsert_attachment_ref(sid, att, run_id)
        part = multipart.get(sha2)
        if part is not None and not store.has_verified_blob(sha2):
            verdict = store.verify_and_store(
                sha2, part.data, part.content_type, att.get("length"), run_id)
            _record_verdict(verdict, store, sid, sha2, run_id, report)


def _fetch_pending_materials(client: LrsClient, store: EvidenceStore, run_id: int,
                             report: CollectReport) -> None:
    """对未核验的附件引用按语句自己的 fileUrl 补下载；503 保留为可恢复缺口。"""
    for ref in store.pending_refs():
        sha2 = ref["sha2"]
        if store.has_verified_blob(sha2):
            continue
        try:
            data = client.fetch_file(ref["file_url"])
        except MaterialUnavailable as exc:
            store.mark_ref_unavailable(ref["statement_id"], sha2, run_id, str(exc))
            report.blobs_unavailable.append(f"{ref['statement_id']}:{sha2}")
            continue
        verdict = store.verify_and_store(
            sha2, data, ref["content_type"], ref["declared_length"], run_id)
        _record_verdict(verdict, store, ref["statement_id"], sha2, run_id, report)


def _reconcile_open_voids(client: LrsClient, store: EvidenceStore, run_id: int,
                          report: CollectReport) -> None:
    """对未闭合的作废引用，用 voidedStatementId 通道尝试补原文。

    普通 statementId 不返回已作废原文；补到后立即维持其作废状态，不恢复有效性。
    """
    for target_id in store.open_void_targets():
        statement = client.get_voided_statement(target_id)
        if statement is None:
            continue
        store.insert_raw_statement(statement, run_id)
        report.void_links_closed.append(target_id)
        if target_id in report.void_links_open:
            report.void_links_open.remove(target_id)
        # 作废语句补到原文后，其声明的附件同样进入材料完整性跟踪。
        for att in statement.get("attachments", []):
            store.upsert_attachment_ref(target_id, att, run_id)


def _record_verdict(verdict, store: EvidenceStore, statement_id: str, sha2: str,
                    run_id: int, report: CollectReport) -> None:
    if verdict.ok and verdict.reused:
        report.blobs_reused += 1
    elif verdict.ok:
        report.blobs_verified += 1
    else:
        store.mark_ref_corrupt(statement_id, sha2, run_id, verdict.reason)
        report.blobs_corrupt.append(f"{statement_id}:{sha2}（{verdict.reason}）")
