"""C1–C5：认证是最外层关口。"""

from __future__ import annotations

from .conftest import admin_headers, bearer, make_dept, make_token


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_no_token(client):
    resp = client.get("/v1/datasets")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


def test_bad_token(client):
    resp = client.get("/v1/datasets", headers=bearer("soli_nope_0000"))
    assert resp.status_code == 401


def test_bearer_prefix_required(client):
    make_dept(client, "hr")
    token = make_token(client, "hr")
    resp = client.get("/v1/datasets", headers={"Authorization": token})
    assert resp.status_code == 401


def test_revoked_token(client):
    make_dept(client, "hr")
    token = make_token(client, "hr")
    assert client.get("/v1/datasets", headers=bearer(token)).status_code == 200

    client.app.state.db.run(
        "UPDATE tokens SET revoked_at = '2026-01-01T00:00:00Z' WHERE 1=1"
    )
    resp = client.get("/v1/datasets", headers=bearer(token))
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "TOKEN_REVOKED"


def test_expired_token(client):
    make_dept(client, "hr")
    resp = client.post(
        "/admin/tokens",
        json={"dept": "hr", "scopes": ["read"], "expires_at": "2000-01-01T00:00:00Z"},
        headers=admin_headers(),
    )
    token = resp.json()["token"]
    got = client.get("/v1/datasets", headers=bearer(token))
    assert got.status_code == 401
    assert got.json()["error"]["code"] == "TOKEN_EXPIRED"


def test_disabled_department(client):
    make_dept(client, "hr")
    token = make_token(client, "hr")
    client.app.state.db.run("UPDATE departments SET status='disabled' WHERE id='hr'")
    resp = client.get("/v1/datasets", headers=bearer(token))
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "DEPT_DISABLED"


def test_admin_requires_admin_token(client):
    resp = client.post("/admin/departments", json={"name": "x"})
    assert resp.status_code == 401
    resp = client.post("/admin/departments", json={"name": "x"}, headers=bearer("wrong"))
    assert resp.status_code == 401


def test_emergency_revoke_is_off_by_default(client):
    resp = client.post(
        "/admin/departments/hr/status", json={"status": "disabled"}, headers=admin_headers()
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "EMERGENCY_REVOKE_DISABLED"
