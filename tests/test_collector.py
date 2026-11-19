"""收集器端到端测试：基于本地合成来源与临时证据库，验证追溯与解释规则。"""
import hashlib
import http.server
import json
import os
import shutil
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from tests.support import SourceCase
from source.protocol import BINARY, IDS, SHA
from collector.client import LrsClient
from collector.collect import collect_once
from collector.store import EvidenceStore
from collector.views import build_statement_views, dashboard, effective_participation


class CollectorCase(SourceCase):
    def setUp(self):
        super().setUp()
        self.repo = tempfile.mkdtemp(prefix="evidence-repo-")
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

    def collect(self):
        with EvidenceStore(self.repo) as store:
            report = collect_once(LrsClient(self.base), store)
        return report

    def open_store(self):
        return EvidenceStore(self.repo)


class FullScenario(CollectorCase):
    def test_stage0_collects_with_open_void_and_pending_material(self):
        report = self.collect()
        self.assertEqual(report.status, "done")
        # 阶段0稳定边界为 10:00:03Z，本轮确认收齐范围精确等于该 stored 边界。
        self.assertEqual(report.consistent_through, "2026-11-16T10:00:03Z")
        with self.open_store() as store:
            self.assertEqual(store.get_cursor(), "2026-11-16T10:00:03Z")
            # 三条语句：void-1、s-2、s-3；s-late 尚未入库。
            self.assertEqual(report.seen_statements, 3)
            # 作废先到、被引用原文缺失：未闭合引用必须显式可见。
            self.assertEqual(store.open_void_targets(), [IDS["s-late"]])
            gaps = store.material_gaps()
            # s-2/s-3 共用的字节随 multipart 已核验，不应有缺口。
            self.assertEqual(gaps, [])
            data = dashboard(store)
            self.assertEqual(len(data["open_void_references"]), 1)

    def test_voided_original_arrives_later_but_never_becomes_valid(self):
        self.collect()
        self.control(1)  # s-late 入库（事件时间很早、stored 很晚），其附件仅给地址
        report = self.collect()
        self.assertEqual(report.status, "done")
        self.assertEqual(report.consistent_through, "2026-11-16T10:00:04Z")
        with self.open_store() as store:
            # 原文已补到且可查，但作废状态不允许恢复。
            self.assertTrue(store.has_statement(IDS["s-late"]))
            self.assertEqual(store.open_void_targets(), [])
            views = {v.statement_id: v for v in build_statement_views(store)}
            self.assertTrue(views[IDS["s-late"]].voided)
            self.assertFalse(views[IDS["s-late"]].valid)
            # 事件时间早于入库时间很多，收集依据 stored 而非 timestamp。
            self.assertLess(views[IDS["s-late"]].event_time,
                            views[IDS["s-late"]].stored)
            evidence = effective_participation(store)
            ids_in_evidence = {row["statement_id"] for row in evidence}
            self.assertNotIn(IDS["s-late"], ids_in_evidence)
            # s-2、s-3 仍是有效证据。
            self.assertEqual(ids_in_evidence, {IDS["s-2"], IDS["s-3"]})

    def test_delayed_material_blocks_evidence_until_complete(self):
        # 用只给授权地址的来源：材料发布前 fileUrl 返回 503。
        source = CustomSource()
        self.addCleanup(source.close)
        data = BINARY
        CustomHandler.files = {SHA: (data, SHA)}
        CustomHandler.file_status = 503
        CustomHandler.payload = {"statements": [
            _statement(IDS["s-late"], "2026-11-16T10:00:04Z",
                       "urn:activity:build", "urn:project:practice",
                       attachment={"usageType": "urn:campus:evidence",
                                   "display": {"zh-CN": "操作记录"},
                                   "contentType": "application/octet-stream",
                                   "length": len(data), "sha2": SHA,
                                   "fileUrl": source.base + "/files/" + SHA})],
            "more": ""}
        with EvidenceStore(self.repo) as store:
            report = collect_once(LrsClient(source.base), store)
            self.assertEqual(report.status, "done")
            # 503 被记为可恢复缺口，引用保持 pending。
            self.assertEqual({g["sha2"] for g in store.material_gaps()}, {SHA})
            self.assertTrue(all(r["status"] == "pending"
                                for r in store.attachment_rows(IDS["s-late"])))
            self.assertIn(IDS["s-late"] + ":" + SHA, report.blobs_unavailable)
            self.assertEqual(effective_participation(store), [])
        # 材料开放后重跑：字节补齐，但该语句此前已被作废，仍不恢复有效。
        CustomHandler.file_status = 200
        with EvidenceStore(self.repo) as store:
            # 预置作废关系，模拟 void-1 先于 s-late 到达的真实顺序。
            run = store.start_run(None)
            store.add_void_link(IDS["void-1"], IDS["s-late"], run)
            store.mark_voided_if_present(IDS["s-late"])
            collect_once(LrsClient(source.base), store)
        with self.open_store() as store:
            self.assertEqual(store.material_gaps(), [])
            views = {v.statement_id: v for v in build_statement_views(store)}
            self.assertTrue(views[IDS["s-late"]].material_complete)
            self.assertTrue(views[IDS["s-late"]].voided)
            self.assertFalse(views[IDS["s-late"]].valid)
            self.assertNotIn(IDS["s-late"],
                             {r["statement_id"] for r in effective_participation(store)})

    def test_corrupt_bytes_quarantined_and_not_delivered(self):
        source = CustomSource()
        self.addCleanup(source.close)
        good = BINARY
        bad = good + b"x"  # 声明 sha2 仍是好字节的，来源实际给出损坏内容
        CustomHandler.files = {SHA: (bad, SHA)}
        CustomHandler.file_status = 200
        CustomHandler.payload = {"statements": [
            _statement(IDS["s-2"], "2026-11-16T10:00:02Z",
                       "urn:activity:build", "urn:project:practice",
                       attachment={"usageType": "urn:campus:evidence",
                                   "display": {"zh-CN": "操作记录"},
                                   "contentType": "application/octet-stream",
                                   "length": len(good), "sha2": SHA,
                                   "fileUrl": source.base + "/files/" + SHA})],
            "more": ""}
        with EvidenceStore(self.repo) as store:
            report = collect_once(LrsClient(source.base), store)
            self.assertTrue(report.blobs_corrupt)
            self.assertFalse(store.has_verified_blob(SHA))
            self.assertEqual(len(os.listdir(store.quarantine_dir)), 1)
            self.assertEqual(effective_participation(store), [])
        # 来源恢复正常后重跑：同一引用补齐为合格字节，证据随之完整。
        CustomHandler.files = {SHA: (good, SHA)}
        with EvidenceStore(self.repo) as store:
            report = collect_once(LrsClient(source.base), store)
            self.assertEqual(report.status, "done")
            self.assertTrue(store.has_verified_blob(SHA))
            self.assertEqual(
                {r["statement_id"] for r in effective_participation(store)},
                {IDS["s-2"]})

    def test_interrupted_collection_resumes_without_advancing_cursor(self):
        # 让来源在读取中途失败：用一个只成功返回首屏、随后报错的客户端包装。
        calls = {"n": 0}
        client = LrsClient(self.base)
        original_get = client._get

        def flaky_get(url):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise ConnectionError("模拟网络中断")
            return original_get(url)

        client._get = flaky_get
        with EvidenceStore(self.repo) as store:
            report = collect_once(client, store)
            self.assertEqual(report.status, "interrupted")
            # 中断时水位不得推进，首屏语句已留存。
            self.assertIsNone(store.get_cursor())
            self.assertTrue(store.has_statement(IDS["void-1"]))
        # 恢复后重新收集，应完整成功且不产生重复语句。
        report2 = self.collect()
        self.assertEqual(report2.status, "done")
        # 首屏两条（void-1、s-2）中断前已入库，本页去重；只有第二页为新增。
        self.assertEqual(report2.new_statements, 1)
        with self.open_store() as store:
            self.assertEqual(store.get_cursor(), "2026-11-16T10:00:03Z")
            total = store.conn.execute("SELECT COUNT(*) c FROM statements").fetchone()["c"]
            self.assertEqual(total, 3)
            # 同一语句没有重复行。
            distinct = store.conn.execute(
                "SELECT COUNT(DISTINCT statement_id) c FROM statements").fetchone()["c"]
            self.assertEqual(distinct, 3)

    def test_idempotent_reruns_and_raw_traceability(self):
        self.collect()
        second = self.collect()
        self.assertEqual(second.status, "done")
        self.assertEqual(second.new_statements, 0)
        with self.open_store() as store:
            row = store.conn.execute(
                "SELECT raw_json FROM statements WHERE statement_id=?",
                (IDS["s-2"],)).fetchone()
            raw = json.loads(row["raw_json"])
            # 原文逐字段保留，附件声明与授权地址可追溯。
            self.assertEqual(raw["attachments"][0]["sha2"], SHA)
            self.assertTrue(raw["attachments"][0]["fileUrl"].endswith("/files/" + SHA))

    def test_shared_sha2_stores_bytes_once_but_keeps_per_statement_refs(self):
        self.collect()
        with self.open_store() as store:
            blob_files = []
            for root, _, files in os.walk(store.blobs_dir):
                blob_files.extend(os.path.join(root, f) for f in files)
            self.assertEqual(len(blob_files), 1)  # 共用 sha2，字节只落盘一次
            self.assertEqual(blob_files[0].endswith(SHA), True)
            refs_s2 = store.attachment_rows(IDS["s-2"])
            refs_s3 = store.attachment_rows(IDS["s-3"])
            # 两条语句各持自己的引用行与授权地址，互不借用凭据。
            self.assertEqual(len(refs_s2), 1)
            self.assertEqual(len(refs_s3), 1)
            self.assertEqual(refs_s2[0]["file_url"], refs_s3[0]["file_url"])
            self.assertNotEqual(refs_s2[0]["statement_id"], refs_s3[0]["statement_id"])
            # 原始字节可还原且与来源一致。
            with open(blob_files[0], "rb") as fh:
                self.assertEqual(fh.read(), BINARY)


class CustomHandler(http.server.BaseHTTPRequestHandler):
    """可定制的轻量来源：用于跨项目同名附件、独立授权地址等场景。

    语句只走 JSON（不随 multipart 附字节），材料一律经 fileUrl 下载，从而覆盖
    “附件可能只给授权下载地址”以及发布前 503、摘要不符等契约路径。
    """

    payload = {"statements": [], "more": ""}
    files = {}
    consistent = "2026-11-16T10:00:03Z"
    file_status = 200  # 200 正常；503 模拟未发布

    def log_message(self, *args):
        pass

    def do_GET(self):
        u = urlsplit(self.path)
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        headers = {"X-Experience-API-Version": "1.0.3",
                   "X-Experience-API-Consistent-Through": self.consistent}
        if u.path.startswith("/files/"):
            if self.file_status == 503:
                self.send_response(503)
                self.send_header("Retry-After", "1")
                self.end_headers()
                return
            key = u.path.split("/")[-1]
            if key not in self.files:
                self.send_response(404)
                self.end_headers()
                return
            data, sha2 = self.files[key]
            self.send_response(200)
            headers.update({"Content-Type": "application/octet-stream",
                            "X-Experience-API-Hash": sha2})
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        body = json.dumps(self.payload, ensure_ascii=False).encode()
        self.send_response(200)
        headers["Content-Type"] = "application/json"
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CustomSource:
    def __init__(self):
        self.server = http.server.HTTPServer(("127.0.0.1", 0), CustomHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def _statement(sid, stored, activity, project, *, attachment=None, voided=False):
    obj = ({"objectType": "StatementRef", "id": voided} if voided
           else {"id": activity})
    s = {"id": sid, "stored": stored, "timestamp": "2026-10-01T08:00:00Z",
         "actor": {"account": {"homePage": "https://example.invalid", "name": "learner-9"}},
         "verb": {"id": ("http://adlnet.gov/expapi/verbs/voided" if voided
                         else "http://adlnet.gov/expapi/verbs/completed")},
         "object": obj,
         "context": {"contextActivities": {"grouping": [{"id": project}]}}}
    if attachment:
        s["attachments"] = [attachment]
    return s


class CrossProjectAndNaming(unittest.TestCase):
    def setUp(self):
        self.source = CustomSource()
        self.repo = tempfile.mkdtemp(prefix="evidence-cross-")
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        self.addCleanup(self.source.close)

    def test_same_activity_across_projects_kept_distinct_and_same_filename_safe(self):
        data_a = b"project-A screenshot bytes"
        data_b = b"project-B different screenshot"
        sha_a = hashlib.sha256(data_a).hexdigest()
        sha_b = hashlib.sha256(data_b).hexdigest()
        CustomHandler.files = {sha_a: (data_a, sha_a), sha_b: (data_b, sha_b)}
        # 两条不同项目的参与指向同一活动对象，附件来源文件名同名但内容不同。
        att_a = {"usageType": "urn:campus:evidence",
                 "display": {"zh-CN": "截图.png"}, "contentType": "image/png",
                 "length": len(data_a), "sha2": sha_a,
                 "fileUrl": self.source.base + "/files/" + sha_a}
        att_b = dict(att_a, length=len(data_b), sha2=sha_b,
                     fileUrl=self.source.base + "/files/" + sha_b)
        CustomHandler.payload = {"statements": [
            _statement("11111111-1111-4111-8111-111111111111",
                       "2026-11-16T10:00:01Z", "urn:activity:bridge",
                       "urn:project:alpha", attachment=att_a),
            _statement("22222222-2222-4222-8222-222222222222",
                       "2026-11-16T10:00:02Z", "urn:activity:bridge",
                       "urn:project:beta", attachment=att_b),
        ], "more": ""}
        with EvidenceStore(self.repo) as store:
            report = collect_once(LrsClient(self.source.base), store)
            self.assertEqual(report.status, "done")
            evidence = effective_participation(store)
            # 同一活动对象不等于同一次参与：两条都保留，不按名称/时间合并。
            self.assertEqual(len(evidence), 2)
            projects = {row["project_id"] for row in evidence}
            self.assertEqual(projects, {"urn:project:alpha", "urn:project:beta"})
            # 磁盘按 sha2 内容寻址，同名“截图.png”不存在覆盖问题。
            stored_names = []
            for root, _, files in os.walk(store.blobs_dir):
                stored_names.extend(files)
            self.assertEqual(sorted(stored_names), sorted([sha_a, sha_b]))
            # 引用行各自保留独立授权地址，文件复用不串凭据。
            rows = store.conn.execute(
                "SELECT statement_id, file_url FROM attachment_refs").fetchall()
            urls = {r["statement_id"]: r["file_url"] for r in rows}
            self.assertIn(sha_a, urls["11111111-1111-4111-8111-111111111111"])
            self.assertIn(sha_b, urls["22222222-2222-4222-8222-222222222222"])

    def test_length_mismatch_keeps_evidence_incomplete(self):
        data = b"actual bytes here"
        sha = hashlib.sha256(data).hexdigest()
        CustomHandler.files = {sha: (data, sha)}
        att = {"usageType": "urn:campus:evidence", "display": {"zh-CN": "截图.png"},
               "contentType": "image/png", "length": len(data) + 5,  # 声明长度不符
               "sha2": sha, "fileUrl": self.source.base + "/files/" + sha}
        CustomHandler.payload = {"statements": [
            _statement("33333333-3333-4333-8333-333333333333",
                       "2026-11-16T10:00:01Z", "urn:activity:x",
                       "urn:project:gamma", attachment=att)], "more": ""}
        with EvidenceStore(self.repo) as store:
            report = collect_once(LrsClient(self.source.base), store)
            self.assertTrue(report.blobs_corrupt)
            self.assertEqual(effective_participation(store), [])
            self.assertFalse(store.has_verified_blob(sha))


if __name__ == "__main__":
    unittest.main()
