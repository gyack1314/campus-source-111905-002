"""来源 HTTP 读取层：分页语句、multipart/mixed 解析与授权材料下载。

关键契约（见 docs/来源契约.md）：
- 连续读取以 stored 为准（since 为 stored 严格大于），more 只代表当前查询的
  后续页，读取 more 时不得改变查询参数；
- 每页响应头 X-Experience-API-Consistent-Through 是来源确认完整可见的 stored
  边界，more 翻完不代表更早发生（timestamp）的活动已收齐；
- attachments=true 时首部分为 JSON，其后二进制部分用 X-Experience-API-Hash
  关联 sha2，同一摘要在一份响应中只随附一次；
- 附件也可能只给 fileUrl 授权下载地址，材料发布前 GET /files/<sha2> 返回 503。
"""
from __future__ import annotations

import email.parser
import email.policy
from dataclasses import dataclass, field
from typing import Iterator, Optional

import requests

XAPI_VERSION = "1.0.3"
VOIDED_VERB = "http://adlnet.gov/expapi/verbs/voided"
CONSISTENT_HEADER = "X-Experience-API-Consistent-Through"
HASH_HEADER = "X-Experience-API-Hash"


class SourceError(RuntimeError):
    """来源响应不符合契约或传输失败。"""


@dataclass(frozen=True)
class StatementPage:
    """一次语句读取的结果。

    statements 为本页语句原文（解析后的 JSON 对象，保持来源字段不改名）；
    more 为来源给出的相对/绝对后续地址，空串表示本查询已无后续页；
    consistent_through 为本页响应头声明的稳定可见 stored 边界。
    """

    statements: list[dict]
    more: str
    consistent_through: str
    attachment_parts: list[AttachmentPart] = field(default_factory=list)


@dataclass(frozen=True)
class AttachmentPart:
    """multipart 响应中的一个二进制部分。"""

    sha2: str
    content_type: str
    data: bytes


class LrsClient:
    """只读的来源客户端，不持久化任何状态，便于中断后由上层接续。"""

    def __init__(self, base_url: str, token: Optional[str] = None, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers["X-Experience-API-Version"] = XAPI_VERSION
        if token:
            # 凭据只在请求时随头发送，绝不写入本地材料库；不同语句各用各的来源地址。
            self._session.headers["Authorization"] = f"Bearer {token}"

    def _get(self, url: str) -> requests.Response:
        try:
            response = self._session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise SourceError(f"读取来源失败：{url} ({exc})") from exc
        if response.status_code != 200:
            raise SourceError(f"来源返回 {response.status_code}：{url}")
        return response

    @staticmethod
    def _consistent_through(response: requests.Response) -> str:
        value = response.headers.get(CONSISTENT_HEADER)
        if not value:
            raise SourceError(f"响应缺少 {CONSISTENT_HEADER} 头，无法确认稳定可见边界")
        return value

    def get_statements(
        self, since: Optional[str] = None, attachments: bool = True
    ) -> Iterator[StatementPage]:
        """从首屏开始逐页产出语句页，自动跟随 more 且不改变原查询。

        since 为本地已确认收齐的 stored 边界（严格大于语义）。每页都带上
        attachments=true，以便一次性收下来源愿意随附的二进制部分。
        """
        params = {"attachments": "true" if attachments else "false"}
        if since:
            params["since"] = since
        url = requests.Request("GET", self.base_url + "/xapi/statements", params=params)
        prepared = self._session.prepare_request(url)
        yield from self._follow(prepared.url)

    def get_voided_statement(self, statement_id: str) -> Optional[dict]:
        """按 voidedStatementId 精确取回已作废语句原文。

        普通 statementId 查询不返回已被作废的原文，必须走此通道。
        """
        url = f"{self.base_url}/xapi/statements?voidedStatementId={statement_id}"
        response = self._get(url)
        payload = response.json()
        rows = payload.get("statements", [])
        if not rows:
            return None
        return rows[0]

    def _follow(self, url: str) -> Iterator[StatementPage]:
        while url:
            response = self._get(url)
            content_type = response.headers.get("Content-Type", "")
            consistent_through = self._consistent_through(response)
            if content_type.startswith("multipart/mixed"):
                payload, parts = parse_multipart(response.content, content_type)
                page = StatementPage(payload["statements"], payload.get("more", ""),
                                     consistent_through, parts)
            else:
                payload = response.json()
                page = StatementPage(payload["statements"], payload.get("more", ""),
                                     consistent_through)
            yield page
            more = page.more
            if not more:
                return
            # more 是来源相对地址且保留原查询；原样拼接，不重新组装参数。
            url = requests.compat.urljoin(self.base_url + "/xapi/statements", more)

    def fetch_file(self, file_url: str) -> bytes:
        """按语句自己的授权地址下载原始字节，503 等未发布状态向上抛出。"""
        response = self._session.get(file_url, timeout=self.timeout)
        if response.status_code == 503:
            raise MaterialUnavailable(file_url, retry_after=response.headers.get("Retry-After"))
        if response.status_code != 200:
            raise SourceError(f"材料下载返回 {response.status_code}：{file_url}")
        return response.content


class MaterialUnavailable(SourceError):
    """材料尚未发布（来源返回 503），属于可恢复的缺口而非永久失败。"""

    def __init__(self, url: str, retry_after: Optional[str] = None):
        super().__init__(f"材料尚未发布：{url}")
        self.url = url
        self.retry_after = retry_after


def parse_multipart(body: bytes, content_type: str) -> tuple[dict, list[AttachmentPart]]:
    """解析 multipart/mixed：首部分 JSON，其余为按 sha2 关联的二进制。"""
    message = email.parser.BytesParser(policy=email.policy.default).parsebytes(
        ("Content-Type: " + content_type + "\r\n\r\n").encode() + body
    )
    parts = list(message.iter_parts())
    if not parts:
        raise SourceError("multipart 响应没有任何部分")
    payload = parts[0].get_payload(decode=True)
    try:
        statements_payload = requests.compat.json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SourceError(f"multipart 首部分不是合法 JSON：{exc}") from exc
    attachments: list[AttachmentPart] = []
    for part in parts[1:]:
        sha2 = part.get(HASH_HEADER)
        if not sha2:
            raise SourceError("二进制部分缺少 X-Experience-API-Hash，无法关联语句")
        data = part.get_payload(decode=True) or b""
        attachments.append(AttachmentPart(sha2=sha2.lower(),
                                          content_type=part.get_content_type(), data=data))
    return statements_payload, attachments


def is_voiding(statement: dict) -> bool:
    """判断语句是否为 voided 动词的作废语句。"""
    return statement.get("verb", {}).get("id") == VOIDED_VERB


def void_target(statement: dict) -> Optional[str]:
    """取出作废语句通过 StatementRef 指向的目标语句 id。"""
    obj = statement.get("object", {})
    if obj.get("objectType") == "StatementRef":
        return obj.get("id")
    return None
