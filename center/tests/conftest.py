"""测试脚手架：假节点（httpx.MockTransport）+ 三方世界（hr / fin / rd）。

全部测试零网络、零外部服务、零容器。
"""

from __future__ import annotations

import json
from typing import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from solirag_center.config import Settings
from solirag_center.main import create_app

ADMIN = "admin-secret"
NODE_KEY = "node-secret"
NODES = {
    "node-hr": "http://node-hr:8001",
    "node-fin": "http://node-fin:8001",
    "node-rd": "http://node-rd:8001",
}


class FakeNodes:
    """用 dataset 名做键的假节点集合。"""

    def __init__(self) -> None:
        self.docs: dict[str, list[str]] = {}
        self.down: set[str] = set()
        self.payloads: list[dict] = []

    def add(self, dataset: str, *contents: str) -> None:
        self.docs.setdefault(dataset, []).extend(contents)

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        dataset = str(body["dataset"])
        self.payloads.append(body)
        if dataset in self.down:
            raise httpx.ConnectError("node down", request=request)
        hits = [
            {
                "content": content,
                "score": round(1.0 - i * 0.1, 4),
                "document_id": f"{dataset}-d{i}",
                "segment_id": f"{dataset}-s{i}",
                "title": f"{dataset}-title{i}",
            }
            for i, content in enumerate(self.docs.get(dataset, []))
        ]
        return httpx.Response(
            200,
            json={
                "node": request.url.host,
                "dataset": dataset,
                "took_ms": 1,
                "hits": hits,
            },
        )


@pytest.fixture
def fake_nodes() -> FakeNodes:
    return FakeNodes()


@pytest.fixture
def settings() -> Settings:
    # 用内存库：测试完全不落盘，也就不依赖临时目录（本机沙箱对 Temp 有策略）
    return Settings(
        db_path=":memory:",
        admin_token=ADMIN,
        node_key=NODE_KEY,
        nodes=dict(NODES),
    )


@pytest.fixture
def client(settings: Settings, fake_nodes: FakeNodes) -> Iterator[TestClient]:
    app = create_app(settings, transport=httpx.MockTransport(fake_nodes.handler))
    with TestClient(app) as test_client:
        yield test_client


def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN}"}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def make_dept(client: TestClient, name: str) -> None:
    resp = client.post("/admin/departments", json={"name": name}, headers=admin_headers())
    assert resp.status_code == 200, resp.text


def make_token(
    client: TestClient, dept: str, scopes: tuple[str, ...] = ("read", "grant")
) -> str:
    resp = client.post(
        "/admin/tokens",
        json={"dept": dept, "scopes": list(scopes)},
        headers=admin_headers(),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


@pytest.fixture
def world(client: TestClient, fake_nodes: FakeNodes) -> dict[str, str]:
    """hr / fin / rd 三部门；hr 授权 fin 读，rd 谁也不授权。"""
    tokens: dict[str, str] = {}
    for dept in ("hr", "fin", "rd"):
        make_dept(client, dept)
        tokens[dept] = make_token(client, dept)

    fake_nodes.add("hr", "劳动合同到期不续签需支付经济补偿", "年假折算办法")
    fake_nodes.add("fin", "差旅报销标准与审批流程")
    fake_nodes.add("rd", "内部研发保密协议条款")

    assert (
        client.put(
            "/v1/datasets/hr",
            json={"node": "node-hr", "grants": [{"dept": "fin"}]},
            headers=bearer(tokens["hr"]),
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/v1/datasets/fin",
            json={"node": "node-fin", "grants": []},
            headers=bearer(tokens["fin"]),
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/v1/datasets/rd",
            json={"node": "node-rd", "grants": []},
            headers=bearer(tokens["rd"]),
        ).status_code
        == 200
    )
    return tokens


def ask(
    client: TestClient, token: str, query: str = "劳动合同", knowledge_id: str | None = "global",
    top_k: int = 5, threshold: float = 0.0,
) -> dict:
    body: dict = {"query": query, "retrieval_setting": {"top_k": top_k, "score_threshold": threshold}}
    if knowledge_id is not None:
        body["knowledge_id"] = knowledge_id
    return client.post("/v1/dify/retrieval", json=body, headers=bearer(token)).json()
