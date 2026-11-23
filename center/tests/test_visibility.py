"""A1–A8：'谁读了什么'只有 owner 和审计能看，访问者之间互相不可见。"""

from __future__ import annotations

from .conftest import admin_headers, ask, bearer

MARKER = "SYNTHETIC-QUERY-MARKER-7f3a"


def test_a1_owner_sees_who_read_my_library(client, world):
    ask(client, world["fin"], query=MARKER)
    body = client.get("/v1/datasets/hr/audit", headers=bearer(world["hr"])).json()
    hits = [r for r in body["audit"] if r["reader_dept"] == "fin"]
    assert hits, "owner 必须能看到谁读过自己的库"
    assert any(r["matched_docs"] > 0 for r in hits)
    assert body["counts"]["readers"] >= 1


def test_a2_visitor_cannot_see_grant_list(client, world):
    resp = client.get("/v1/datasets/hr/grants", headers=bearer(world["fin"]))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "NOT_OWNER"

    # 探测本身留痕，且只进 admin 视图
    admin = client.get("/admin/audit", headers=admin_headers()).json()["audit"]
    assert any(
        r["action"] == "probe" and r["reader_dept"] == "fin" and r["dataset"] == "hr"
        for r in admin
    )


def test_a3_visitors_are_invisible_to_each_other(client, world):
    ask(client, world["fin"], query="报销")
    ask(client, world["rd"], query="保密", knowledge_id="hr")  # rd 被拒

    mine = client.get("/v1/me/audit", headers=bearer(world["fin"])).json()["audit"]
    assert mine, "fin 自己应该有记录"
    assert all(r["reader_dept"] == "fin" for r in mine)
    assert not any(r["reader_dept"] == "rd" for r in mine), "FIN 不能看到 RD 的任何行为"


def test_a4_visible_list_is_scoped(client, world):
    names = {d["name"] for d in client.get("/v1/datasets", headers=bearer(world["fin"])).json()["datasets"]}
    assert names == {"fin", "hr"}
    assert "rd" not in names


def test_a5_unreadable_dataset_is_indistinguishable(client, world):
    data = ask(client, world["fin"], query="保密", knowledge_id="rd")
    assert data["records"] == []
    assert data["federation"]["searched_count"] == 0

    # 但主人的审计里能看到这次越权探测
    rd_audit = client.get("/v1/datasets/rd/audit", headers=bearer(world["rd"])).json()["audit"]
    assert any(
        r["reader_dept"] == "fin" and r["decision"] == "denied" for r in rd_audit
    )


def test_a5b_unknown_dataset_looks_the_same(client, world):
    ghost = ask(client, world["fin"], query="x", knowledge_id="does-not-exist")
    real = ask(client, world["fin"], query="x", knowledge_id="rd")
    assert ghost["records"] == real["records"] == []
    assert ghost["federation"]["searched_count"] == real["federation"]["searched_count"] == 0


def test_a6_revoke_takes_effect_immediately(client, world):
    assert ask(client, world["fin"], query="劳动合同", knowledge_id="hr")["records"]

    client.delete("/v1/datasets/hr/grants/fin", headers=bearer(world["hr"]))

    after = ask(client, world["fin"], query="劳动合同", knowledge_id="hr")
    assert after["records"] == []

    hr_audit = client.get("/v1/datasets/hr/audit", headers=bearer(world["hr"])).json()["audit"]
    assert any(r["reader_dept"] == "fin" and r["decision"] == "denied" for r in hr_audit)


def test_a7_audit_is_append_only(client, world):
    assert client.put("/v1/datasets/hr/audit", headers=bearer(world["hr"])).status_code == 405
    assert client.delete("/v1/me/audit", headers=bearer(world["hr"])).status_code == 405
    assert client.post("/v1/audit", json={}, headers=bearer(world["hr"])).status_code == 404


def test_a8_no_query_plaintext_leaks(client, world):
    resp = client.post(
        "/v1/dify/retrieval",
        json={"knowledge_id": "global", "query": MARKER, "retrieval_setting": {"top_k": 5}},
        headers=bearer(world["fin"]),
    )
    assert MARKER not in resp.text

    mine = client.get("/v1/me/audit", headers=bearer(world["fin"]))
    assert MARKER not in mine.text

    admin = client.get("/admin/audit", headers=admin_headers())
    assert MARKER not in admin.text


def test_audit_hash_chain_is_intact(client, world):
    ask(client, world["fin"], query="劳动合同")
    ask(client, world["rd"], query="保密")
    verify = client.get("/admin/audit/verify", headers=admin_headers()).json()
    assert verify["ok"] is True
    assert verify["checked"] >= 3


def test_audit_chain_detects_tampering(client, world):
    ask(client, world["fin"], query="劳动合同")
    client.app.state.db.run("UPDATE audit SET decision='allowed' WHERE decision='denied'")
    client.app.state.db.run("UPDATE audit SET matched_docs=999 WHERE id=1")
    verify = client.get("/admin/audit/verify", headers=admin_headers()).json()
    assert verify["ok"] is False
