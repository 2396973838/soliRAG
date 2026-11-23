"""请求/响应模型与统一错误。

接口优先：JSON 形状先在这里定死，handler 只做搬运。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------- #
# 统一错误
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    """所有"禁止分支"都用它，禁止静默成功。"""

    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code


def error_body(code: str, message: str, request_id: str = "") -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "request_id": request_id}}


# --------------------------------------------------------------------------- #
# 管理面
# --------------------------------------------------------------------------- #
class DepartmentIn(BaseModel):
    name: str


class TokenIn(BaseModel):
    dept: str
    scopes: list[str] = Field(default_factory=lambda: ["read"])
    expires_at: str | None = None
    created_by: str = "admin"


# --------------------------------------------------------------------------- #
# 登记与授权
# --------------------------------------------------------------------------- #
class GrantIn(BaseModel):
    dept: str
    permission: str = "read"
    expires_at: str | None = None


class DatasetIn(BaseModel):
    node: str
    visibility: str = "shared"
    grants: list[GrantIn] = Field(default_factory=list)


class GrantsIn(BaseModel):
    grants: list[GrantIn] = Field(default_factory=list)


class AddGrantIn(BaseModel):
    dept: str
    expires_at: str | None = None


# --------------------------------------------------------------------------- #
# 检索（Dify 外部知识库协议，字段名由 Dify 决定，不可改）
# --------------------------------------------------------------------------- #
class RetrievalSetting(BaseModel):
    """Dify 实际发来的 top_k / score_threshold 可能是字符串或 null，
    这里做宽松转换（真实联调发现：Dify 的命中测试会发 "top_k": "4"）。"""

    top_k: int = 5
    score_threshold: float = 0.0

    @field_validator("top_k", mode="before")
    @classmethod
    def _coerce_top_k(cls, value: Any) -> int:
        if value is None or value == "":
            return 5
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 5

    @field_validator("score_threshold", mode="before")
    @classmethod
    def _coerce_threshold(cls, value: Any) -> float:
        if value is None or value == "":
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0


class RetrievalIn(BaseModel):
    knowledge_id: str | None = None
    query: str = ""
    retrieval_setting: RetrievalSetting | None = None


class RecordOut(BaseModel):
    content: str
    score: float
    title: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalOut(BaseModel):
    records: list[RecordOut] = Field(default_factory=list)
    federation: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 中心 ↔ 节点 线协议（见 docs/wire-contract.md）
# --------------------------------------------------------------------------- #
class NodeRetrieveIn(BaseModel):
    dataset: str
    query: str
    top_k: int = 10
    reader_dept: str = ""
    request_id: str = ""


class NodeHit(BaseModel):
    content: str
    score: float
    document_id: str = ""
    segment_id: str = ""
    title: str = ""


class NodeRetrieveOut(BaseModel):
    node: str
    dataset: str
    took_ms: int = 0
    hits: list[NodeHit] = Field(default_factory=list)
