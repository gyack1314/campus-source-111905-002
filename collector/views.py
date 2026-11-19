"""面向使用者的解释视图：有效性、材料完整性、未闭合引用与有效参与证据。

解释规则（只依据本地已留存的原文与引用，不反推过程）：
- 有效性：被作废（voided=1）的语句一律无效，即使原文是稍后才补到的，也不会因
  补到原文或补齐材料而恢复；voided 动词的作废语句本身归类为 voiding，不作为学
  生参与活动；
- 材料完整性：语句声明的每个附件都须有核验通过的字节（sha2/length 一致），
  pending 或 corrupt 都算不完整，不得呈现为证据齐全；
- 有效参与视图只列出“未作废且材料完整”的活动语句；跨项目的同一活动对象是不同
  参与，按语句 id 各自成行，绝不按时间或活动名称合并。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from .store import EvidenceStore


@dataclass
class StatementView:
    statement_id: str
    verb_id: str
    activity_id: Optional[str]
    project_id: Optional[str]
    actor_name: Optional[str]
    event_time: Optional[str]
    stored: str
    kind: str               # activity | voiding
    valid: bool             # 当前有效性
    voided: bool
    material_complete: bool
    attachments: list[dict] = field(default_factory=list)
    missing_materials: list[dict] = field(default_factory=list)


def build_statement_views(store: EvidenceStore) -> list[StatementView]:
    views: list[StatementView] = []
    for row in store.statement_rows():
        refs = [dict(r) for r in store.attachment_rows(row["statement_id"])]
        missing = [r for r in refs if r["status"] != "verified"]
        if row["is_voiding"]:
            kind = "voiding"
            # 作废语句的“有效性”含义是它作为作废指令成立；不作为参与证据。
            valid = False
        else:
            kind = "activity"
            valid = not bool(row["voided"])
        views.append(StatementView(
            statement_id=row["statement_id"],
            verb_id=row["verb_id"],
            activity_id=row["activity_id"],
            project_id=row["project_id"],
            actor_name=row["actor_name"],
            event_time=row["event_time"],
            stored=row["stored"],
            kind=kind,
            valid=valid,
            voided=bool(row["voided"]),
            material_complete=not missing,
            attachments=refs,
            missing_materials=missing,
        ))
    return views


def open_references(store: EvidenceStore) -> list[dict]:
    """未闭合引用：作废语句指向的原文尚未入库。"""
    rows = store.conn.execute(
        "SELECT vl.voiding_statement_id, vl.target_statement_id, vl.first_seen_run "
        "FROM void_links vl WHERE vl.resolved=0 ORDER BY vl.voiding_statement_id"
    ).fetchall()
    return [dict(r) for r in rows]


def effective_participation(store: EvidenceStore) -> list[dict]:
    """有效参与视图：仅未作废且材料完整的活动语句，逐条成行不合并。"""
    evidence: list[dict] = []
    for view in build_statement_views(store):
        if view.kind != "activity" or not view.valid or not view.material_complete:
            continue
        evidence.append({
            "statement_id": view.statement_id,
            "actor": view.actor_name,
            "project_id": view.project_id,
            "activity_id": view.activity_id,
            "verb_id": view.verb_id,
            "event_time": view.event_time,
            "stored": view.stored,
            "attachments": [
                {"sha2": a["sha2"], "content_type": a["content_type"],
                 "length": a["declared_length"], "file_name": a["file_name"]}
                for a in view.attachments
            ],
        })
    return evidence


def dashboard(store: EvidenceStore) -> dict:
    """汇总当前库的可解释状态，缺口与未闭合引用必须显式可见。"""
    views = build_statement_views(store)
    activity_views = [v for v in views if v.kind == "activity"]
    return {
        "confirmed_cursor": store.get_cursor(),
        "counts": {
            "statements_total": len(views),
            "activity_statements": len(activity_views),
            "voiding_statements": sum(1 for v in views if v.kind == "voiding"),
            "voided_statements": sum(1 for v in views if v.voided),
            "valid_complete_evidence": len(effective_participation(store)),
        },
        "statements": [asdict(v) for v in views],
        "open_void_references": open_references(store),
        "material_gaps": store.material_gaps(),
    }
