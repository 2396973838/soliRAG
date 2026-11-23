"""小工具：时间、哈希、ID。全项目共用这一份，禁止各处自己写一套。"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    """解析 ISO8601（容忍 Z 后缀）。空值或格式错误返回 None。"""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def is_past(value: str | None) -> bool:
    """expires_at 是否已过期。空值表示永不过期。"""
    dt = parse_iso(value)
    if dt is None:
        return False
    return dt <= datetime.now(timezone.utc)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def token_hash(raw: str) -> str:
    return sha256_hex(raw)


def new_token(dept: str) -> str:
    return f"soli_{dept}_{secrets.token_hex(16)}"


def new_id(prefix: str = "id") -> str:
    return f"{prefix}-{secrets.token_hex(6)}"


_WS = re.compile(r"\s+")


def normalize_content(text: str) -> str:
    """去重用的规范化：压缩空白、去首尾。不改变可见内容，只做哈希前置。"""
    return _WS.sub(" ", (text or "").strip())
