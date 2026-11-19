from tests.support import SourceCase
from source.protocol import SHA,BINARY,IDS
import email.parser,email.policy,json,hashlib
class Contract(SourceCase):
    def test_consistent_boundary(self):
        s,h,b=self.request("/xapi/statements")
        self.assertEqual(s,200);self.assertEqual(h["X-Experience-API-Consistent-Through"],"2026-11-16T10:00:03Z")
    def test_stored_not_event_time(self):
        self.control(1)
        s,h,b=self.request("/xapi/statements?since=2026-11-16T10:00:03Z")
        row=json.loads(b)["statements"][0]
        self.assertEqual(row["id"],IDS["s-late"]);self.assertLess(row["timestamp"],row["stored"])
    def test_forward_void(self):
        rows=json.loads(self.request("/xapi/statements")[2])["statements"]
        self.assertEqual(rows[0]["object"]["id"],IDS["s-late"])
        self.assertNotIn(IDS["s-late"],[s["id"] for s in self.source.statements()])
    def test_multipart_hash(self):
        s,h,b=self.request("/xapi/statements?attachments=true")
        msg=email.parser.BytesParser(policy=email.policy.default).parsebytes(("Content-Type: "+h["Content-Type"]+"\r\n\r\n").encode()+b)
        parts=list(msg.iter_parts());self.assertEqual(len(parts),2)
        data=parts[1].get_payload(decode=True)
        self.assertEqual(hashlib.sha256(data).hexdigest(),SHA)
    def test_more_preserves_query(self):
        self.control(1)
        more=json.loads(self.request("/xapi/statements?since=2026-11-16T10:00:01Z")[2])["more"]
        page=json.loads(self.request(more)[2]);self.assertEqual([s["id"] for s in page["statements"]],[IDS["s-late"]]);self.assertEqual(page["more"],"")
    def test_shared_attachment(self):
        rows=self.source.statements()
        self.assertEqual(rows[1]["attachments"][0]["sha2"],rows[2]["attachments"][0]["sha2"])
        self.assertNotEqual(rows[1]["id"],rows[2]["id"])
    def test_binary_release_and_corruption(self):
        self.assertEqual(self.request("/files/"+SHA)[0],503)
        self.control(2)
        self.assertEqual(self.request("/files/"+SHA)[2],BINARY)
        self.control(2,corrupt=True)
        self.assertNotEqual(hashlib.sha256(self.request("/files/"+SHA)[2]).hexdigest(),SHA)
    def test_voided_original_lookup(self):
        self.control(1)
        self.assertEqual(json.loads(self.request("/xapi/statements?statementId="+IDS["s-late"])[2])["statements"],[])
        self.assertEqual(json.loads(self.request("/xapi/statements?voidedStatementId="+IDS["s-late"])[2])["statements"][0]["id"],IDS["s-late"])
