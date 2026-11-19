"""命令行入口：收集、状态查看、有效参与视图、语句原文追溯。

示例：
    python -m collector.cli collect --base http://127.0.0.1:8765 --repo ./evidencerepo
    python -m collector.cli status  --repo ./evidencerepo
    python -m collector.cli evidence --repo ./evidencerepo
    python -m collector.cli show <statement-id> --repo ./evidencerepo

凭据通过 --token 或环境变量 LRS_TOKEN 提供，只用于请求头，不落盘。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from .client import LrsClient
from .collect import collect_once
from .store import EvidenceStore
from .views import build_statement_views, dashboard, effective_participation, open_references


def _print_json(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False))


def _cmd_collect(args: argparse.Namespace) -> int:
    token = args.token or os.environ.get("LRS_TOKEN")
    with EvidenceStore(args.repo) as store:
        client = LrsClient(args.base, token=token)
        report = collect_once(client, store)
        if args.json:
            _print_json(report.as_dict())
            return 0 if report.status == "done" else 2
        _print_collect_text(store, report)
        return 0 if report.status == "done" else 2


def _print_collect_text(store: EvidenceStore, report) -> None:
    print(f"运行 #{report.run_id} 状态：{report.status}")
    print(f"本轮 since（stored 严格大于）：{report.since or '（首次收集，无下界）'}")
    if report.consistent_through:
        print(f"本次确认收齐的入库范围：stored <= {report.consistent_through}")
        print("（该边界来自来源 X-Experience-API-Consistent-Through；"
              "晚到的旧活动入库后下一轮自动接续，不按事件时间推断收齐）")
    print(f"读取语句页：{report.pages}，本轮流经语句：{report.seen_statements}，"
          f"新入库：{report.new_statements}")
    if report.beyond_boundary:
        print("stored 晚于确认边界、已留存但下轮仍会复核的语句："
              + ", ".join(report.beyond_boundary))
    print(f"附件字节：新核验 {report.blobs_verified}，复用已有 {report.blobs_reused}")
    if report.blobs_unavailable:
        print("材料尚未发布（可恢复缺口）：" + ", ".join(report.blobs_unavailable))
    if report.blobs_corrupt:
        print("校验不符已隔离（相关语句不视为证据齐全）：")
        for item in report.blobs_corrupt:
            print("  - " + item)
    if report.void_links_open:
        print("未闭合作废引用（原文未到）：" + ", ".join(report.void_links_open))
    if report.void_links_closed:
        print("本轮闭合作废引用：" + ", ".join(report.void_links_closed))
    if report.note:
        print("中断原因：" + report.note)
        print("水位未推进，下次运行从原 since 接续。")


def _cmd_status(args: argparse.Namespace) -> int:
    with EvidenceStore(args.repo) as store:
        if args.json:
            _print_json(dashboard(store))
            return 0
        data = dashboard(store)
        print(f"已确认收齐入库边界：{data['confirmed_cursor'] or '（尚未完成过一轮）'}")
        counts = data["counts"]
        print(f"语句总数 {counts['statements_total']}（活动 {counts['activity_statements']}"
              f"，作废指令 {counts['voiding_statements']}，其中已作废 "
              f"{counts['voided_statements']}）")
        print(f"当前可交付的有效且材料齐全证据：{counts['valid_complete_evidence']} 条")
        print("\n每条语句当前状态：")
        for view in build_statement_views(store):
            validity = {
                "activity": ("有效" if view.valid else "已作废·无效"),
                "voiding": "作废指令（不作为参与证据）",
            }[view.kind]
            material = "材料完整" if view.material_complete else \
                f"材料不完整（缺 {len(view.missing_materials)} 项）"
            print(f"  {view.statement_id} [{view.kind}] {validity}；{material}；"
                  f"事件 {view.event_time}；入库 {view.stored}；项目 {view.project_id}")
        if data["open_void_references"]:
            print("\n未闭合引用：")
            for ref in data["open_void_references"]:
                print(f"  作废语句 {ref['voiding_statement_id']} -> 缺失原文 "
                      f"{ref['target_statement_id']}")
        if data["material_gaps"]:
            print("\n材料缺口：")
            for gap in data["material_gaps"]:
                print(f"  语句 {gap['statement_id']} 的 {gap['sha2']} "
                      f"状态={gap['status']} 地址={gap['file_url']}")
        if not data["open_void_references"] and not data["material_gaps"]:
            print("\n无未闭合引用、无材料缺口。")
        return 0


def _cmd_evidence(args: argparse.Namespace) -> int:
    with EvidenceStore(args.repo) as store:
        rows = effective_participation(store)
        if args.json:
            _print_json(rows)
            return 0
        if not rows:
            print("当前没有可交付的有效参与证据（可能尚未收集、均被作废或材料未齐）。")
            return 0
        print(f"有效参与视图（{len(rows)} 条，按语句逐条保留，不按活动/时间合并）：")
        for row in rows:
            files = ", ".join(a["file_name"] or a["sha2"] for a in row["attachments"])
            print(f"  {row['statement_id']} 学生={row['actor']} 项目={row['project_id']} "
                  f"活动={row['activity_id']} 动词={row['verb_id']} "
                  f"事件={row['event_time']} 附件=[{files}]")
        return 0


def _cmd_show(args: argparse.Namespace) -> int:
    with EvidenceStore(args.repo) as store:
        row = store.conn.execute(
            "SELECT * FROM statements WHERE statement_id=?", (args.statement_id,)
        ).fetchone()
        if row is None:
            print(f"本地未收录语句：{args.statement_id}", file=sys.stderr)
            return 1
        refs = [dict(r) for r in store.attachment_rows(args.statement_id)]
        result = {
            "statement_id": row["statement_id"],
            "is_voiding": bool(row["is_voiding"]),
            "voided": bool(row["voided"]),
            "event_time": row["event_time"],
            "stored": row["stored"],
            "raw_statement": json.loads(row["raw_json"]),
            "attachment_refs": refs,
            "open_void_references": [r for r in open_references(store)
                                     if r["target_statement_id"] == args.statement_id],
        }
        _print_json(result)
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="collector",
                                     description="xAPI 学习活动原始证据收集器")
    sub = parser.add_subparsers(dest="command", required=True)

    p_collect = sub.add_parser("collect", help="执行一轮可接续的收集")
    p_collect.add_argument("--base", required=True, help="来源基址，如 http://127.0.0.1:8765")
    p_collect.add_argument("--repo", required=True, help="本地证据库目录")
    p_collect.add_argument("--token", default=None, help="授权令牌，也可用环境变量 LRS_TOKEN")
    p_collect.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    p_collect.set_defaults(func=_cmd_collect)

    p_status = sub.add_parser("status", help="查看有效性、材料完整性与未闭合引用")
    p_status.add_argument("--repo", required=True)
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=_cmd_status)

    p_ev = sub.add_parser("evidence", help="输出有效参与证据视图（不含已作废/材料不齐）")
    p_ev.add_argument("--repo", required=True)
    p_ev.add_argument("--json", action="store_true")
    p_ev.set_defaults(func=_cmd_evidence)

    p_show = sub.add_parser("show", help="追溯单条语句原文及其附件引用")
    p_show.add_argument("statement_id")
    p_show.add_argument("--repo", required=True)
    p_show.set_defaults(func=_cmd_show)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
