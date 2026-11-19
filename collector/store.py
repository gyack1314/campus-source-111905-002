"""本地证据库：语句原文、作废关系、附件引用与原始字节的持久化。

身份与复用规则：
- 语句 id 是不可变身份，按 id 幂等 upsert，重复收集不产生重复行；
- 字节按 sha2 内容寻址存储，多条语句共用同一 sha2 时字节只落盘一次，但每条
  语句各自保留独立的附件引用行（自己的 fileUrl、length、contentType、文件名），
  因此复用字节不会把一条语句的权限/来源凭据混给另一条；
- 文件名不是身份：磁盘上不按来源文件名存放，避免同名图片互相覆盖；来源文件
  名只作为引用元数据保留；
- 校验（sha2 与 length）不过的字节绝不进入正式字节区，而是隔离到 quarantine，
  依赖它的语句在补齐合格字节前保持材料不完整，不能交付为证据齐全。

库目录布局：
    <repo>/evidence.sqlite3        关系数据
    <repo>/blobs/<sha2前2位>/<sha2> 已校验原始字节
    <repo>/quarantine/<sha2>...     校验失败字节（仅留存排查，不作为证据）
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    since_cursor TEXT,
    consistent_through TEXT,
    status TEXT NOT NULL,
    collected_statements INTEGER NOT NULL DEFAULT 0,
    new_statements INTEGER NOT NULL DEFAULT 0,
    fetched_blobs INTEGER NOT NULL DEFAULT 0,
    note TEXT
);
CREATE TABLE IF NOT EXISTS statements(
    statement_id TEXT PRIMARY KEY,
    verb_id TEXT NOT NULL,
    activity_id TEXT,
    project_id TEXT,
    actor_name TEXT,
    event_time TEXT,
    stored TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    is_voiding INTEGER NOT NULL DEFAULT 0,
    voided INTEGER NOT NULL DEFAULT 0,
    first_seen_run INTEGER NOT NULL,
    last_seen_run INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS void_links(
    voiding_statement_id TEXT NOT NULL,
    target_statement_id TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0,
    first_seen_run INTEGER NOT NULL,
    PRIMARY KEY(voiding_statement_id, target_statement_id)
);
CREATE TABLE IF NOT EXISTS blobs(
    sha2 TEXT PRIMARY KEY,
    content_type TEXT,
    length INTEGER NOT NULL,
    stored_path TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    first_run INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS attachment_refs(
    statement_id TEXT NOT NULL,
    sha2 TEXT NOT NULL,
    declared_length INTEGER NOT NULL,
    content_type TEXT,
    file_name TEXT,
    file_url TEXT NOT NULL,
    status TEXT NOT NULL,
    blob_sha2 TEXT,
    updated_run INTEGER NOT NULL,
    PRIMARY KEY(statement_id, sha2)
);
CREATE TABLE IF NOT EXISTS kv(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class BlobVerdict:
    """一次字节写入尝试的结论。"""

    sha2: str
    ok: bool
    reason: Optional[str]
    reused: bool = False


class EvidenceStore:
    def __init__(self, repo_dir: str):
        self.repo_dir = os.path.abspath(repo_dir)
        self.blobs_dir = os.path.join(self.repo_dir, "blobs")
        self.quarantine_dir = os.path.join(self.repo_dir, "quarantine")
        os.makedirs(self.blobs_dir, exist_ok=True)
        os.makedirs(self.quarantine_dir, exist_ok=True)
        self.db_path = os.path.join(self.repo_dir, "evidence.sqlite3")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "EvidenceStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 运行与水位 -----------------------------------------------------

    def start_run(self, since: Optional[str]) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs(started_at, since_cursor, status) VALUES(?,?,?)",
            (_utcnow(), since, "running"),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, consistent_through: Optional[str],
                   note: Optional[str] = None) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, status=?, consistent_through=?, note=? "
            "WHERE run_id=?",
            (_utcnow(), status, consistent_through, note, run_id),
        )
        self.conn.commit()

    def get_cursor(self) -> Optional[str]:
        """已确认收齐的 stored 边界；下次读取以此作为 since（严格大于）。"""
        row = self.conn.execute("SELECT value FROM kv WHERE key='consistent_through'").fetchone()
        return row["value"] if row else None

    def advance_cursor(self, consistent_through: str, run_id: int) -> None:
        self.conn.execute(
            "INSERT INTO kv(key,value) VALUES('consistent_through',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (consistent_through,),
        )
        self.conn.commit()

    # ---- 语句与作废关系 -------------------------------------------------

    def upsert_statement(self, statement: dict, run_id: int) -> bool:
        """幂等写入语句原文，返回是否为首次见到的新语句。"""
        sid = statement["id"]
        existing = self.conn.execute(
            "SELECT 1 FROM statements WHERE statement_id=?", (sid,)
        ).fetchone()
        verb_id = statement.get("verb", {}).get("id", "")
        obj = statement.get("object", {})
        activity_id = None if obj.get("objectType") == "StatementRef" else obj.get("id")
        grouping = (statement.get("context", {})
                    .get("contextActivities", {}).get("grouping", []))
        project_id = grouping[0].get("id") if grouping else None
        actor = statement.get("actor", {})
        actor_name = actor.get("name") or actor.get("account", {}).get("name")
        from .client import is_voiding
        is_void = 1 if is_voiding(statement) else 0
        payload = (
            sid, verb_id, activity_id, project_id, actor_name,
            statement.get("timestamp"), statement["stored"],
            json.dumps(statement, ensure_ascii=False),
            is_void, run_id, run_id,
        )
        if existing:
            # 原文不可变：只刷新“最后一次见到”的运行标记，raw_json 维持原值。
            self.conn.execute(
                "UPDATE statements SET last_seen_run=? WHERE statement_id=?",
                (run_id, sid),
            )
            self.conn.commit()
            return False
        self.conn.execute(
            "INSERT INTO statements(statement_id, verb_id, activity_id, project_id, "
            "actor_name, event_time, stored, raw_json, is_voiding, first_seen_run, "
            "last_seen_run) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            payload,
        )
        self.conn.commit()
        return True

    def add_void_link(self, voiding_id: str, target_id: str, run_id: int) -> None:
        """登记作废关系；目标语句可能尚未到达（resolved=0 即未闭合引用）。"""
        self.conn.execute(
            "INSERT INTO void_links(voiding_statement_id, target_statement_id, "
            "resolved, first_seen_run) VALUES(?,?,0,?) "
            "ON CONFLICT(voiding_statement_id, target_statement_id) DO NOTHING",
            (voiding_id, target_id, run_id),
        )
        # 若目标原文已在库中，立即将其标记为已作废——作废不允许以后被撤销。
        target = self.conn.execute(
            "SELECT 1 FROM statements WHERE statement_id=?", (target_id,)
        ).fetchone()
        if target:
            self._mark_voided(target_id)
        self.conn.commit()

    def _mark_voided(self, target_id: str) -> None:
        self.conn.execute(
            "UPDATE statements SET voided=1 WHERE statement_id=?", (target_id,)
        )
        self.conn.execute(
            "UPDATE void_links SET resolved=1 WHERE target_statement_id=?", (target_id,)
        )

    def mark_voided_if_present(self, target_id: str) -> bool:
        """补到目标原文后调用：标记作废并闭合引用。返回目标是否存在。"""
        exists = self.conn.execute(
            "SELECT 1 FROM statements WHERE statement_id=?", (target_id,)
        ).fetchone()
        if exists:
            self._mark_voided(target_id)
            self.conn.commit()
        return bool(exists)

    def open_void_targets(self) -> list[str]:
        """尚未闭合的作废目标 id（作废语句已到、原文未到）。"""
        rows = self.conn.execute(
            "SELECT DISTINCT target_statement_id FROM void_links WHERE resolved=0"
        ).fetchall()
        return [r["target_statement_id"] for r in rows]

    def has_statement(self, statement_id: str) -> bool:
        return bool(self.conn.execute(
            "SELECT 1 FROM statements WHERE statement_id=?", (statement_id,)
        ).fetchone())

    def insert_raw_statement(self, statement: dict, run_id: int) -> bool:
        """通过 voidedStatementId 通道补到的原文，同样走幂等登记。"""
        is_new = self.upsert_statement(statement, run_id)
        self.mark_voided_if_present(statement["id"])
        return is_new

    # ---- 附件引用与字节 -------------------------------------------------

    def upsert_attachment_ref(self, statement_id: str, att: dict, run_id: int) -> None:
        """每条语句独立登记附件声明，保留自己的授权地址与文件名。"""
        sha2 = att["sha2"].lower()
        verified = self.conn.execute(
            "SELECT 1 FROM blobs WHERE sha2=?", (sha2,)
        ).fetchone()
        status = "verified" if verified else "pending"
        self.conn.execute(
            "INSERT INTO attachment_refs(statement_id, sha2, declared_length, "
            "content_type, file_name, file_url, status, blob_sha2, updated_run) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(statement_id, sha2) DO UPDATE SET declared_length=excluded.declared_length,"
            " content_type=excluded.content_type, file_name=excluded.file_name,"
            " file_url=excluded.file_url, updated_run=excluded.updated_run",
            (statement_id, sha2, att.get("length"), att.get("contentType"),
             att.get("display", {}).get("zh-CN") or att.get("fileName"),
             att["fileUrl"], status, sha2 if verified else None, run_id),
        )
        self.conn.commit()

    def _blob_path(self, sha2: str) -> str:
        return os.path.join(self.blobs_dir, sha2[:2], sha2)

    def _quarantine_path(self, sha2: str) -> str:
        # 隔离名带时间戳，避免同一 sha2 的多次坏响应互相覆盖。
        return os.path.join(self.quarantine_dir, f"{sha2}.{_utcnow_for_path()}.bin")

    def has_verified_blob(self, sha2: str) -> bool:
        return bool(self.conn.execute(
            "SELECT 1 FROM blobs WHERE sha2=?", (sha2.lower(),)
        ).fetchone())

    def verify_and_store(self, sha2: str, data: bytes, content_type: Optional[str],
                         declared_length: Optional[int], run_id: int) -> BlobVerdict:
        """校验 sha2/length 后内容寻址落盘；不合格字节进隔离区。"""
        sha2 = sha2.lower()
        if self.has_verified_blob(sha2):
            return BlobVerdict(sha2=sha2, ok=True, reason=None, reused=True)
        actual_sha = hashlib.sha256(data).hexdigest()
        if actual_sha != sha2:
            self._quarantine(data)
            return BlobVerdict(sha2, ok=False,
                               reason=f"sha2 不符：声明 {sha2}，实得 {actual_sha}")
        if declared_length is not None and len(data) != declared_length:
            self._quarantine(data)
            return BlobVerdict(sha2, ok=False,
                               reason=f"length 不符：声明 {declared_length}，实得 {len(data)}")
        path = self._blob_path(sha2)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _atomic_write(path, data)
        self.conn.execute(
            "INSERT INTO blobs(sha2, content_type, length, stored_path, verified_at, first_run) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(sha2) DO NOTHING",
            (sha2, content_type, len(data), path, _utcnow(), run_id),
        )
        # 字节补齐后，所有引用该 sha2 的附件行一起转为 verified——但不改变语句作废状态。
        self.conn.execute(
            "UPDATE attachment_refs SET status='verified', blob_sha2=? WHERE sha2=?",
            (sha2, sha2),
        )
        self.conn.commit()
        return BlobVerdict(sha2=sha2, ok=True, reason=None)

    def _quarantine(self, data: bytes) -> None:
        os.makedirs(self.quarantine_dir, exist_ok=True)
        _atomic_write(self._quarantine_path(hashlib.sha256(data).hexdigest()), data)

    def mark_ref_unavailable(self, statement_id: str, sha2: str, run_id: int,
                             reason: str) -> None:
        """材料仍未发布（503）等可恢复缺口：保持 pending 并记录原因。"""
        self.conn.execute(
            "UPDATE attachment_refs SET status='pending', updated_run=? "
            "WHERE statement_id=? AND sha2=?",
            (run_id, statement_id, sha2.lower()),
        )
        self.conn.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"gap:{statement_id}:{sha2.lower()}", reason),
        )
        self.conn.commit()

    def mark_ref_corrupt(self, statement_id: str, sha2: str, run_id: int,
                         reason: str) -> None:
        self.conn.execute(
            "UPDATE attachment_refs SET status='corrupt', updated_run=? "
            "WHERE statement_id=? AND sha2=?",
            (run_id, statement_id, sha2.lower()),
        )
        self.conn.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"gap:{statement_id}:{sha2.lower()}", reason),
        )
        self.conn.commit()

    # ---- 视图查询 -------------------------------------------------------

    def statement_rows(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM statements ORDER BY stored, statement_id"))

    def attachment_rows(self, statement_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM attachment_refs WHERE statement_id=? ORDER BY sha2",
            (statement_id,)))

    def pending_refs(self) -> list[sqlite3.Row]:
        """材料尚未齐全的附件引用（pending/corrupt 都不算证据齐全）。"""
        return list(self.conn.execute(
            "SELECT * FROM attachment_refs WHERE status!='verified' "
            "ORDER BY statement_id, sha2"))

    def material_gaps(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT DISTINCT ar.statement_id, ar.sha2, ar.status, ar.file_url, ar.declared_length "
            "FROM attachment_refs ar WHERE ar.status!='verified' ORDER BY ar.statement_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def run_summary(self, run_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else {}

    def last_finished_cursor(self) -> Optional[str]:
        row = self.conn.execute(
            "SELECT consistent_through FROM runs WHERE status='done' "
            "AND consistent_through IS NOT NULL ORDER BY run_id DESC LIMIT 1").fetchone()
        return row["consistent_through"] if row else None


def _atomic_write(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utcnow_for_path() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
