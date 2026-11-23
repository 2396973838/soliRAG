"""参数校验与边界。"""

from __future__ import annotations

from .conftest import ask, bearer


def test_empty_query(client, world):
    resp = client.post(
        "/v1/dify/retrieval",
        json={"knowledge_id": "global", "query": "   "},
        headers=bearer(world["fin"]),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_missing_query_field(client, world):
    resp = client.post(
        "/v1/dify/retrieval", json={"knowledge_id": "global"}, headers=bearer(world["fin"])
    )
    assert resp.status_code == 400


def test_query_too_long(client, world):
    resp = client.post(
        "/v1/dify/retrieval",
        json={"knowledge_id": "global", "query": "x" * 5000},
        headers=bearer(world["fin"]),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "QUERY_TOO_LONG"


def test_top_k_must_be_positive(client, world):
    resp = client.post(
        "/v1/dify/retrieval",
        json={"knowledge_id": "hr", "query": "劳动合同", "retrieval_setting": {"top_k": 0}},
        headers=bearer(world["fin"]),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_top_k_is_clamped(client, world, fake_nodes):
    data = ask(client, world["fin"], query="劳动合同", top_k=999)
    assert data["records"]
    assert fake_nodes.payloads, "节点应当被调用"
    assert all(p["top_k"] <= 50 for p in fake_nodes.payloads)


def test_missing_knowledge_id_defaults_to_global(client, world):
    data = ask(client, world["fin"], query="劳动合同", knowledge_id=None)
    assert data["federation"]["mode"] == "global"


def test_missing_retrieval_setting_uses_defaults(client, world):
    resp = client.post(
        "/v1/dify/retrieval",
        json={"knowledge_id": "hr", "query": "劳动合同"},
        headers=bearer(world["fin"]),
    )
    assert resp.status_code == 200
    assert 0 < len(resp.json()["records"]) <= 5


def test_string_fields_are_tolerated(client, world):
    """真实联调发现：Dify 的命中测试会发 "top_k": "4" / "score_threshold": null。"""
    resp = client.post(
        "/v1/dify/retrieval",
        json={
            "knowledge_id": "hr",
            "query": "劳动合同",
            "retrieval_setting": {"top_k": "3", "score_threshold": None},
        },
        headers=bearer(world["fin"]),
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["records"]) <= 3


def test_non_bearer_scheme_rejected(client, world):
    resp = client.post(
        "/v1/dify/retrieval",
        json={"query": "x"},
        headers={"Authorization": f"Basic {world['fin']}"},
    )
    assert resp.status_code == 401
