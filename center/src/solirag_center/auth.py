"""认证：部门 token（sha256 存储）与 admin token。

纪律：认证与授权只在这里做，不得散落到各 handler。
"""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from typing import FrozenSet

from fastapi import Request

from .db import Database
from .models import ApiError
from .util import is_past, token_hash


@dataclass(frozen=True)
class Principal:
    dept: str
    scopes: FrozenSet[str]

    def has(self, scope: str) -> bool:
        return scope in self.scopes


def extract_bearer(header: str | None) -> str | None:
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    raw = parts[1].strip()
    return raw or None


def authenticate(db: Database, raw: str | None) -> Principal:
    if not raw:
        raise ApiError(401, "UNAUTHORIZED", "缺少 Bearer token")

    row = db.one("SELECT * FROM tokens WHERE token_hash = ?", (token_hash(raw),))
    if row is None:
        raise ApiError(401, "UNAUTHORIZED", "token 无效")
    if row["revoked_at"]:
        raise ApiError(401, "TOKEN_REVOKED", "token 已撤销")
    if is_past(row["expires_at"]):
        raise ApiError(401, "TOKEN_EXPIRED", "token 已过期")

    dept = db.one("SELECT * FROM departments WHERE id = ?", (row["dept"],))
    if dept is None or dept["status"] != "active":
        raise ApiError(401, "DEPT_DISABLED", "部门不存在或已停用")

    try:
        scopes = json.loads(row["scopes_json"] or "[]")
    except json.JSONDecodeError:
        scopes = []
    return Principal(dept=row["dept"], scopes=frozenset(str(s) for s in scopes))


async def get_principal(request: Request) -> Principal:
    return authenticate(
        request.app.state.db,
        extract_bearer(request.headers.get("authorization")),
    )


async def get_admin(request: Request) -> str:
    settings = request.app.state.settings
    raw = extract_bearer(request.headers.get("authorization"))
    if not settings.admin_token or not raw:
        raise ApiError(401, "ADMIN_UNAUTHORIZED", "admin token 未配置或缺失")
    if not hmac.compare_digest(token_hash(raw), token_hash(settings.admin_token)):
        raise ApiError(401, "ADMIN_UNAUTHORIZED", "admin token 无效")
    return "admin"


def require(principal: Principal, scope: str) -> None:
    if not principal.has(scope):
        raise ApiError(403, "SCOPE_REQUIRED", f"缺少能力位 {scope}")
