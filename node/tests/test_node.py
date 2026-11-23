"""N1–N6：节点只认中心、只认自己的库、绝不泄漏凭证。"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from solirag_node import dify as dify_mod
from solirag_node.config import DatasetConfig, NodeConfig
from solirag_node.main import create_app

NODE_KEY = "node-secret"
API_KEY = "dataset-super-secret-key"


class FakeDify:
    def __init__(self) -> None:
        self.mode = "ok"  # ok | timeout | error | unauthorized
        self.records = [
            {
                "score": 0.9,
                "segment": {
                    "id": "seg-1",
                    "document_id": "doc-1",
                    "content": "劳动合同到期不续签需支付经济补偿",
                },
            },
            {
                "score": 0.5,
                "segment": {
                    "id": "seg-2",
                    "document_id": "doc-2",
                    "content": "年假折算办法",
                },
            },
        ]
        self.calls: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/documents/" in path:
            doc_id = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"id": doc_id, "name": f"文档-{doc_id}"})

        if path.endswith("/retrieve"):
            body = json.loads(request.content.decode("utf-8"))
            self.calls.append(body)
            if self.mode == "timeout":
                raise httpx.ReadTimeout("dify slow", request=request)
            if self.mode == "error":
                return httpx.Response(500, json={"message": "boom"})
            if self.mode == "unauthorized":
                return httpx.Response(401, json={"message": "bad key"})
            return httpx.Response(200, json={"query": body["query"], "records": self.records})

        return httpx.Response(404, json={"message": "not found"})


@pytest.fixture(autouse=True)
def _clear_title_cache():
    dify_mod._TITLE_CACHE.clear()
    yield
    dify_mod._TITLE_CACHE.clear()


@pytest.fixture
def fake_dify() -> FakeDify:
    return FakeDify()


@pytest.fixture
def config() -> NodeConfig:
    return NodeConfig(
        node_id="node-hr",
        dept="hr",
        federation_key=NODE_KEY,
        datasets={
            "hr": DatasetConfig(
                name="hr",
                dify_base_url="http://dify-hr:5001",
                dify_dataset_id="ds-hr",
                api_key=API_KEY,
            )
        },
    )


@pytest.fixture
def client(config: NodeConfig, fake_dify: FakeDify):
    app = create_app(config, transport=httpx.MockTransport(fake_dify.handler))
    with TestClient(app) as test_client:
        yield test_client


def headers(key: str = NODE_KEY) -> dict[str, str]:
    return {"X-Federation-Key": key, "X-Request-Id": "req-test"}


def ask(client, dataset: str = "hr", query: str = "劳动合同", **kw):
    return client.post(
        "/v1/node/retrieve",
        json={"dataset": dataset, "query": query, "top_k": 10, **kw},
        headers=headers(),
    )


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["node"] == "node-hr"


def test_key_required(client):
    resp = client.post("/v1/node/retrieve", json={"dataset": "hr", "query": "x"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "FEDERATION_KEY_INVALID"


def test_wrong_key(client):
    resp = client.post(
        "/v1/node/retrieve",
        json={"dataset": "hr", "query": "x"},
        headers=headers("wrong"),
    )
    assert resp.status_code == 401


def test_not_my_dataset(client):
    resp = ask(client, dataset="fin")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_MY_DATASET"


def test_retrieve_ok(client):
    resp = ask(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["node"] == "node-hr"
    assert [h["content"] for h in body["hits"]][0].startswith("劳动合同")
    scores = [h["score"] for h in body["hits"]]
    assert scores == sorted(scores, reverse=True)
    # title 由文档接口补全
    assert body["hits"][0]["title"] == "文档-doc-1"


def test_dify_request_shape(client, fake_dify):
    ask(client, query="年假")
    payload = fake_dify.calls[0]
    assert payload["query"] == "年假"
    model = payload["retrieval_model"]
    assert model["score_threshold_enabled"] is False, "阈值权必须收归中心"
    assert model["reranking_enable"] is False
    assert model["top_k"] == 10
    assert model["search_method"] == "semantic_search"


def test_search_method_is_configurable(fake_dify):
    """economy 索引的库只能用 keyword_search，否则 Dify 会 400。"""
    config = NodeConfig(
        node_id="node-dify",
        dept="dify",
        federation_key=NODE_KEY,
        datasets={
            "hr-policy": DatasetConfig(
                "hr-policy", "http://dify", "ds-eco", API_KEY, "keyword_search", False
            )
        },
    )
    app = create_app(config, transport=httpx.MockTransport(fake_dify.handler))
    with TestClient(app) as test_client:
        resp = test_client.post(
            "/v1/node/retrieve",
            json={"dataset": "hr-policy", "query": "劳动合同", "top_k": 3},
            headers=headers(),
        )
        assert resp.status_code == 200, resp.text
    assert fake_dify.calls[-1]["retrieval_model"]["search_method"] == "keyword_search"


def test_empty_query(client):
    resp = ask(client, query="   ")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_bad_json(client):
    resp = client.post(
        "/v1/node/retrieve", content=b"not-json", headers=headers()
    )
    assert resp.status_code == 400


def test_upstream_timeout(client, fake_dify):
    fake_dify.mode = "timeout"
    resp = ask(client)
    assert resp.status_code == 504
    assert resp.json()["error"]["code"] == "UPSTREAM_TIMEOUT"


def test_upstream_error(client, fake_dify):
    fake_dify.mode = "error"
    resp = ask(client)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "UPSTREAM_ERROR"


def test_upstream_auth_failure(client, fake_dify):
    fake_dify.mode = "unauthorized"
    resp = ask(client)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "UPSTREAM_ERROR"


def test_no_secret_leak(client):
    for path in ("/healthz", "/v1/node/stats"):
        resp = client.get(path, headers=headers())
        assert API_KEY not in resp.text
        assert NODE_KEY not in resp.text
    resp = ask(client)
    assert API_KEY not in resp.text
    assert NODE_KEY not in resp.text


def test_stats_lists_datasets_without_secrets(client):
    body = client.get("/v1/node/stats", headers=headers()).json()
    assert body["datasets"] == [{"name": "hr", "dify_dataset_id": "ds-hr"}]


def test_allowed_readers_enforced(fake_dify):
    config = NodeConfig(
        node_id="node-hr",
        dept="hr",
        federation_key=NODE_KEY,
        allowed_readers=frozenset({"fin"}),
        datasets={
            "hr": DatasetConfig("hr", "http://dify-hr:5001", "ds-hr", API_KEY)
        },
    )
    app = create_app(config, transport=httpx.MockTransport(fake_dify.handler))
    with TestClient(app) as test_client:
        denied = test_client.post(
            "/v1/node/retrieve",
            json={"dataset": "hr", "query": "x", "reader_dept": "rd"},
            headers=headers(),
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "READER_NOT_ALLOWED"

        allowed = test_client.post(
            "/v1/node/retrieve",
            json={"dataset": "hr", "query": "x", "reader_dept": "fin"},
            headers=headers(),
        )
        assert allowed.status_code == 200


def test_top_k_is_clamped(client, fake_dify):
    test_client_ask = ask(client, top_k=999)
    assert test_client_ask.status_code == 200
    assert fake_dify.calls[-1]["retrieval_model"]["top_k"] <= 50
