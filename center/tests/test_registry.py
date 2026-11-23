"""C6–C8 + 登记与授权语义。"""

from __future__ import annotations

from .conftest import admin_headers, bearer, make_dept, make_token


def test_department_and_token_flow(client):
    make_dept(client, "hr")
    token = make_token(client, "hr")
    assert token.startswith("soli_hr_")

    # 明文只出现一次：后续任何接口都不再返回
    listed = client.get("/v1/datasets", headers=bearer(token))
    assert "token" not in listed.text

    # 重复建部门是幂等的（部署脚本可重跑）
    assert client.post("/admin/departments", json={"name": "hr"}, headers=admin_headers()).status_code == 200


def test_unknown_department_token(client):
    resp = client.post(
        "/admin/tokens", json={"dept": "ghost", "scopes": ["read"]}, headers=admin_headers()
    )
    assert resp.status_code == 404


def test_invalid_scope(client):
    make_dept(client, "hr")
    resp = client.post(
        "/admin/tokens", json={"dept": "hr", "scopes": ["write"]}, headers=admin_headers()
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_SCOPE"


def test_unknown_node_rejected(client):
    make_dept(client, "hr")
    token = make_token(client, "hr")
    resp = client.put(
        "/v1/datasets/hr", json={"node": "node-ghost"}, headers=bearer(token)
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "UNKNOWN_NODE"


def test_owner_is_immutable(client):
    make_dept(client, "hr")
    make_dept(client, "fin")
    hr = make_token(client, "hr")
    fin = make_token(client, "fin")

    assert client.put("/v1/datasets/hr", json={"node": "node-hr"}, headers=bearer(hr)).status_code == 200

    stolen = client.put("/v1/datasets/hr", json={"node": "node-fin"}, headers=bearer(fin))
    assert stolen.status_code == 403
    assert stolen.json()["error"]["code"] == "NOT_OWNER"


def test_register_requires_grant_scope(client):
    make_dept(client, "hr")
    ro = make_token(client, "hr", scopes=("read",))
    resp = client.put("/v1/datasets/hr", json={"node": "node-hr"}, headers=bearer(ro))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "SCOPE_REQUIRED"


def test_grant_and_revoke_flow(client, world):
    hr, fin = world["hr"], world["fin"]

    # fin 能看见 hr 的库
    visible = client.get("/v1/datasets", headers=bearer(fin)).json()["datasets"]
    assert {d["name"] for d in visible} == {"fin", "hr"}

    # owner 能看到自己的白名单
    grants = client.get("/v1/datasets/hr/grants", headers=bearer(hr)).json()["grants"]
    assert [g["dept"] for g in grants] == ["fin"]

    # 收回后立刻不可见
    resp = client.delete("/v1/datasets/hr/grants/fin", headers=bearer(hr))
    assert resp.status_code == 200
    visible_after = client.get("/v1/datasets", headers=bearer(fin)).json()["datasets"]
    assert {d["name"] for d in visible_after} == {"fin"}


def test_grant_is_not_recursive(client, world):
    """FIN 拿到 HR 的库之后，不能再转授给 RD。"""
    fin, rd = world["fin"], world["rd"]
    resp = client.post(
        "/v1/datasets/hr/grants", json={"dept": "rd"}, headers=bearer(fin)
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "NOT_OWNER"

    # RD 仍然看不到 hr
    visible = client.get("/v1/datasets", headers=bearer(rd)).json()["datasets"]
    assert {d["name"] for d in visible} == {"rd"}


def test_grant_to_unknown_department(client, world):
    hr = world["hr"]
    resp = client.post(
        "/v1/datasets/hr/grants", json={"dept": "ghost"}, headers=bearer(hr)
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "UNKNOWN_DEPARTMENT"


def test_private_dataset_ignores_grants(client):
    make_dept(client, "hr")
    make_dept(client, "fin")
    hr = make_token(client, "hr")
    fin = make_token(client, "fin")

    client.put(
        "/v1/datasets/hr",
        json={"node": "node-hr", "visibility": "private", "grants": [{"dept": "fin"}]},
        headers=bearer(hr),
    )
    visible = client.get("/v1/datasets", headers=bearer(fin)).json()["datasets"]
    assert visible == []
