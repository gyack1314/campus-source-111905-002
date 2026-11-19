"""学习记录库：不可变语句、前向作废与材料发布。"""
import hashlib
import json
from urllib.parse import urlencode

BINARY="合成操作截图：项目实践".encode()
SHA=hashlib.sha256(BINARY).hexdigest()
def dump(v):return json.dumps(v,ensure_ascii=False).encode()
class Source:
    name="学习活动记录库"
    def statements(self):
        def statement(id,stored,verb,obj,attachment=False):
            s=dict(id=id,stored=stored,timestamp="2026-10-01T08:00:00Z",
                   actor={"account":{"homePage":"https://example.invalid","name":"learner-1"}},
                   verb={"id":"http://adlnet.gov/expapi/verbs/"+verb},object=obj,
                   context={"contextActivities":{"grouping":[{"id":"urn:project:practice"}]}})
            if attachment:
                s["attachments"]=[dict(usageType="urn:campus:evidence",display={"zh-CN":"操作记录"},contentType="application/octet-stream",length=len(BINARY),sha2=SHA,fileUrl="/files/"+SHA)]
            return s
        rows=[
            statement("void-1","2026-11-16T10:00:01Z","voided",{"objectType":"StatementRef","id":"s-late"}),
            statement("s-2","2026-11-16T10:00:02Z","experienced",{"id":"urn:activity:build"},True),
            statement("s-3","2026-11-16T10:00:03Z","experienced",{"id":"urn:activity:build"},True)]
        if self.stage>=1:rows.append(statement("s-late","2026-11-16T10:00:04Z","completed",{"id":"urn:activity:build"},True))
        return rows
    def route(self,method,path,q,headers,body):
        h={"X-Experience-API-Version":"1.0.3","X-Experience-API-Consistent-Through":"2026-11-16T10:00:03Z" if self.stage==0 else "2026-11-16T10:00:04Z"}
        if path.startswith("/files/"):
            if path.split("/")[-1]!=SHA:return 404,{},b""
            if self.stage<2:return 503,{"Retry-After":"1"},b""
            data=BINARY if not self.options.get("corrupt") else BINARY+b"x"
            return 200,{"Content-Type":"application/octet-stream","X-Experience-API-Hash":SHA},data
        if path not in ["/xapi/statements","/xapi/more"]:return 404,{},b""
        rows=self.statements()
        if "voidedStatementId" in q:rows=[s for s in rows if s["id"]==q["voidedStatementId"]]
        elif "statementId" in q:rows=[s for s in rows if s["id"]==q["statementId"] and s["id"]!="s-late"]
        else:rows=[s for s in rows if s["stored"]>q.get("since","") and s["stored"]<=q.get("until","9999")]
        offset=int(q.get("offset","0"));page=rows[offset:offset+2]
        more="/xapi/more?"+urlencode(dict(q,offset=offset+2)) if offset+2<len(rows) else ""
        payload=dict(statements=page,more=more)
        if q.get("attachments")!="true":
            return 200,dict(h,**{"Content-Type":"application/json"}),dump(payload)
        boundary="source-evidence-boundary"
        chunks=[b"--"+boundary.encode()+b"\r\nContent-Type: application/json\r\n\r\n"+dump(payload)+b"\r\n"]
        # 同内容的材料在一份响应中只随附一次，晚到语句仅提供地址。
        if any(s["id"] in ["s-2","s-3"] for s in page):
            chunks.append(b"--"+boundary.encode()+b"\r\nContent-Type: application/octet-stream\r\nX-Experience-API-Hash: "+SHA.encode()+b"\r\nContent-Transfer-Encoding: binary\r\n\r\n"+BINARY+b"\r\n")
        chunks.append(b"--"+boundary.encode()+b"--\r\n")
        return 200,dict(h,**{"Content-Type":'multipart/mixed; boundary="'+boundary+'"'}),b"".join(chunks)
