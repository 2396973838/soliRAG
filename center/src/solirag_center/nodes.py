"""centor -> node 客户端：唯一跨机调用方向。

节点是"很薄的策略代理"：不存文档、不做切片嵌入，只按库名转发到本部门 Dify。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

TIMEOUT_ERROR = "UPSTREAM_TIMEOUT"
CONNECT_ERROR = "NODE_UNREACHABLE"


@dataclass
class NodeResult:
    dataset: str
    ok: bool
    hits: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    code: str = ""
    took_ms: int = 0


class NodeClient:
    def __init__(self, nodes: dict[str, str], key: str, timeout_ms: int) -> None:
        self._nodes = dict(nodes)
        self._key = key
        self._timeout = max(50, timeout_ms) / 1000.0

    def url_of(self, node_id: str) -> str | None:
        return self._nodes.get(node_id)

    def known(self, node_id: str) -> bool:
        return node_id in self._nodes

    async def retrieve(
        self,
        http: httpx.AsyncClient,
        *,
        node_id: str,
        dataset: str,
        query: str,
        top_k: int,
        request_id: str,
        reader_dept: str,
    ) -> NodeResult:
        base = self._nodes.get(node_id)
        if not base:
            return NodeResult(dataset, False, error=f"未知节点 {node_id}", code="UNKNOWN_NODE")

        payload = {
            "dataset": dataset,
            "query": query,
            "top_k": top_k,
            "reader_dept": reader_dept,
            "request_id": request_id,
        }
        headers = {
            "X-Federation-Key": self._key,
            "X-Reader-Dept": reader_dept,
            "X-Request-Id": request_id,
            # 二期：中心在此附 X-Grant-Signature，节点本地校验后硬拦（见 wire-contract.md §6）
        }
        try:
            resp = await http.post(
                f"{base}/v1/node/retrieve",
                json=payload,
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.TimeoutException:
            return NodeResult(dataset, False, error="节点超时", code=TIMEOUT_ERROR)
        except httpx.HTTPError as exc:
            return NodeResult(dataset, False, error=f"节点不可达: {exc}", code=CONNECT_ERROR)

        if resp.status_code != 200:
            code = "NODE_ERROR"
            try:
                code = str(resp.json().get("error", {}).get("code") or code)
            except Exception:  # noqa: BLE001 - 错误体不是 JSON 时保持默认
                pass
            return NodeResult(
                dataset, False, error=f"节点返回 {resp.status_code}", code=code
            )

        try:
            body = resp.json()
        except ValueError:
            return NodeResult(dataset, False, error="节点响应不是 JSON", code="NODE_BAD_RESPONSE")

        hits = body.get("hits") or []
        if not isinstance(hits, list):
            return NodeResult(dataset, False, error="节点响应 hits 非法", code="NODE_BAD_RESPONSE")

        return NodeResult(
            dataset,
            True,
            hits=[h for h in hits if isinstance(h, dict)],
            took_ms=int(body.get("took_ms") or 0),
        )
