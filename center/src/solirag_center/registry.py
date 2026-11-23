"""登记与授权：部门、token、数据集、grants。

三条不可让步的规则（见 docs/access-contract.md）：
1. owner 由 token 决定，登记后不可过户。
2. 只有 owner 能写自己的 grants；**不可转授**。
3. 撤销与过期立即生效——没有缓存，每次检索实时查表。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Sequence

from .db import Database
from .models import ApiError, GrantIn
from .util import is_past, new_token, now_iso, token_hash

ALLOWED_SCOPES = {"read", "grant"}
ALLOWED_VISIBILITY = {"shared", "private"}
ALLOWED_PERMISSIONS = {"read"}


# --------------------------------------------------------------------------- #
# 部门与 token
# --------------------------------------------------------------------------- #
def create_department(db: Database, name: str, *, actor: str = "admin") -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise ApiError(400, "INVALID_ARGUMENT", "部门名不能为空")
    exists = db.one("SELECT * FROM departments WHERE id = ?", (name,))
    if exists:
        return {"id": exists["id"], "name": exists["name"], "status": exists["status"]}
    db.run(
        "INSERT INTO departments(id, name, status, created_at) VALUES (?,?,?,?)",
        (name, name, "active", now_iso()),
    )
    return {"id": name, "name": name, "status": "active"}


def set_department_status(db: Database, name: str, status: str) -> None:
    if status not in {"active", "disabled"}:
        raise ApiError(400, "INVALID_ARGUMENT", "status 只能是 active/disabled")
    if db.one("SELECT id FROM departments WHERE id = ?", (name,)) is None:
        raise ApiError(404, "NOT_FOUND", f"未知部门: {name}")
    db.run("UPDATE departments SET status = ? WHERE id = ?", (status, name))


def create_token(
    db: Database,
    dept: str,
    scopes: Iterable[str],
    *,
    expires_at: str | None = None,
    created_by: str = "admin",
) -> dict[str, Any]:
    if db.one("SELECT id FROM departments WHERE id = ?", (dept,)) is None:
        raise ApiError(404, "NOT_FOUND", f"未知部门: {dept}")

    scope_list = sorted({str(s) for s in scopes})
    unknown = [s for s in scope_list if s not in ALLOWED_SCOPES]
    if unknown:
        raise ApiError(400, "INVALID_SCOPE", f"未知能力位: {','.join(unknown)}")
    if not scope_list:
        raise ApiError(400, "INVALID_SCOPE", "至少需要一个能力位")

    raw = new_token(dept)
    db.run(
        "INSERT INTO tokens(token_hash, dept, scopes_json, expires_at, revoked_at,"
        " created_by, created_at) VALUES (?,?,?,?,NULL,?,?)",
        (token_hash(raw), dept, json.dumps(scope_list), expires_at, created_by, now_iso()),
    )
    # 明文只在这里出现一次，之后任何接口都不会再返回
    return {
        "token": raw,
        "dept": dept,
        "scopes": scope_list,
        "expires_at": expires_at,
    }


def revoke_token(db: Database, raw: str) -> None:
    cur = db.run(
        "UPDATE tokens SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
        (now_iso(), token_hash(raw)),
    )
    if cur.rowcount == 0:
        raise ApiError(404, "NOT_FOUND", "token 不存在或已撤销")


# --------------------------------------------------------------------------- #
# 数据集
# --------------------------------------------------------------------------- #
def _dataset_row(db: Database, name: str) -> sqlite3.Row | None:
    return db.one("SELECT * FROM datasets WHERE name = ?", (name,))


def dataset_to_dict(row: sqlite3.Row, grants: Sequence[sqlite3.Row] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": row["name"],
        "owner_dept": row["owner_dept"],
        "node": row["node_id"],
        "visibility": row["visibility"],
        "created_at": row["created_at"],
    }
    if grants is not None:
        out["grants"] = [grant_to_dict(g) for g in grants]
    return out


def grant_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "dept": row["grantee_dept"],
        "permission": "read",
        "expires_at": row["expires_at"],
        "granted_by": row["granted_by"],
        "granted_at": row["granted_at"],
        "expired": is_past(row["expires_at"]),
    }


def _validate_grants(db: Database, grants: Iterable[GrantIn]) -> list[GrantIn]:
    out: list[GrantIn] = []
    for g in grants:
        if g.permission not in ALLOWED_PERMISSIONS:
            raise ApiError(400, "INVALID_PERMISSION", f"不支持的权限: {g.permission}")
        if g.dept != g.dept.strip() or not g.dept:
            raise ApiError(400, "INVALID_ARGUMENT", "grantee 不能为空")
        if db.one("SELECT id FROM departments WHERE id = ?", (g.dept,)) is None:
            raise ApiError(400, "UNKNOWN_DEPARTMENT", f"未知部门: {g.dept}")
        out.append(g)
    return out


def _write_grants(db: Database, dataset: str, actor: str, grants: Iterable[GrantIn]) -> None:
    rows = [
        (dataset, g.dept, actor, now_iso(), g.expires_at or None)
        for g in grants
    ]
    db.run_many(
        "INSERT INTO grants(dataset, grantee_dept, granted_by, granted_at, expires_at)"
        " VALUES (?,?,?,?,?)"
        " ON CONFLICT(dataset, grantee_dept) DO UPDATE SET"
        " granted_by=excluded.granted_by, granted_at=excluded.granted_at,"
        " expires_at=excluded.expires_at",
        rows,
    )


def register_dataset(
    db: Database,
    *,
    name: str,
    owner_dept: str,
    node: str,
    visibility: str,
    grants: Iterable[GrantIn],
    known_nodes: Iterable[str],
) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise ApiError(400, "INVALID_ARGUMENT", "库名不能为空")
    if visibility not in ALLOWED_VISIBILITY:
        raise ApiError(400, "INVALID_VISIBILITY", f"visibility 只能是 {ALLOWED_VISIBILITY}")
    if node not in set(known_nodes):
        raise ApiError(400, "UNKNOWN_NODE", f"未知节点: {node}")

    row = _dataset_row(db, name)
    if row is not None and row["owner_dept"] != owner_dept:
        raise ApiError(403, "NOT_OWNER", "该库属于其他部门，不允许过户")

    grant_list = _validate_grants(db, grants)

    if row is None:
        db.run(
            "INSERT INTO datasets(name, owner_dept, node_id, visibility, created_at)"
            " VALUES (?,?,?,?,?)",
            (name, owner_dept, node, visibility, now_iso()),
        )
    else:
        if row["owner_dept"] != owner_dept:
            raise ApiError(403, "NOT_OWNER", "该库属于其他部门，不允许过户")
        db.run(
            "UPDATE datasets SET node_id = ?, visibility = ? WHERE name = ?",
            (node, visibility, name),
        )
        db.run("DELETE FROM grants WHERE dataset = ?", (name,))

    _write_grants(db, name, owner_dept, grant_list)
    fresh = _dataset_row(db, name)
    assert fresh is not None
    return dataset_to_dict(fresh, _grants_of(db, name))


def get_dataset_or_404(db: Database, name: str) -> sqlite3.Row:
    row = _dataset_row(db, name)
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"未知数据集: {name}")
    return row


def _grants_of(db: Database, dataset: str) -> list[sqlite3.Row]:
    return db.all(
        "SELECT * FROM grants WHERE dataset = ? ORDER BY grantee_dept", (dataset,)
    )


def grants_of(db: Database, dataset: str) -> list[dict[str, Any]]:
    return [grant_to_dict(r) for r in _grants_of(db, dataset)]


def replace_grants(
    db: Database, *, dataset: str, owner_dept: str, grants: Iterable[GrantIn]
) -> list[dict[str, Any]]:
    _assert_owner(db, dataset, owner_dept)
    grant_list = _validate_grants(db, grants)
    db.run("DELETE FROM grants WHERE dataset = ?", (dataset,))
    _write_grants(db, dataset, owner_dept, grant_list)
    return grants_of(db, dataset)


def add_grant(
    db: Database, *, dataset: str, owner_dept: str, grantee: str, expires_at: str | None
) -> list[dict[str, Any]]:
    _assert_owner(db, dataset, owner_dept)
    g = _validate_grants(db, [GrantIn(dept=grantee, expires_at=expires_at)])[0]
    _write_grants(db, dataset, owner_dept, [g])
    return grants_of(db, dataset)


def remove_grant(db: Database, *, dataset: str, owner_dept: str, grantee: str) -> list[dict[str, Any]]:
    _assert_owner(db, dataset, owner_dept)
    db.run("DELETE FROM grants WHERE dataset = ? AND grantee_dept = ?", (dataset, grantee))
    return grants_of(db, dataset)


def _assert_owner(db: Database, dataset: str, dept: str) -> sqlite3.Row:
    row = get_dataset_or_404(db, dataset)
    if row["owner_dept"] != dept:
        raise ApiError(403, "NOT_OWNER", "只有库的主人可以修改授权")
    return row


# --------------------------------------------------------------------------- #
# 可见性
# --------------------------------------------------------------------------- #
def _has_active_grant(db: Database, dataset: str, dept: str) -> bool:
    row = db.one(
        "SELECT * FROM grants WHERE dataset = ? AND grantee_dept = ?", (dataset, dept)
    )
    if row is None:
        return False
    return not is_past(row["expires_at"])


def can_read(db: Database, row: sqlite3.Row, dept: str) -> bool:
    if row["owner_dept"] == dept:
        return True
    if row["visibility"] != "shared":
        return False
    return _has_active_grant(db, row["name"], dept)


def visible_datasets(db: Database, dept: str) -> list[sqlite3.Row]:
    """自己的 + 被授权且未过期的。访问者看不到任何别的。"""
    rows = db.all("SELECT * FROM datasets ORDER BY name")
    return [r for r in rows if can_read(db, r, dept)]


def expand_scope(db: Database, dept: str, knowledge_id: str | None) -> list[sqlite3.Row]:
    """把 knowledge_id 展开成要扇出的数据集列表。

    global / * / 空 -> 本部门可见的全部
    具体名字        -> 单库（不可见则返回空列表，由调用方写 denied 审计）
    """
    key = (knowledge_id or "").strip()
    if key in {"", "global", "*", "all"}:
        return visible_datasets(db, dept)
    row = _dataset_row(db, key)
    if row is None or not can_read(db, row, dept):
        return []
    return [row]
