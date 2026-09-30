"""对外数据契约（Pydantic v2）。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Role = Literal["admin", "executive", "dept_director", "sub_manager", "sub_employee",
               "knowledge_reviewer", "compliance_reviewer"]
Intent = Literal["chitchat", "public_kb", "dept_kb", "cross_dept", "data_analysis", "operation"]


# ---------------- 认证 ----------------
class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_expires_in: int
    user: "UserInfo"


class UserInfo(BaseModel):
    user_id: str
    username: str
    display_name: str
    role: Role
    dept_id: str                       # 所属子部门
    dept_name: str
    parent_dept_id: str                # 所属事业部
    parent_dept_name: str
    accessible_depts: list[str] = Field(default_factory=list)
    must_change_password: bool = False  # 首次登录强制改密（FR-AUTH-01）


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8, max_length=64)


# ---------------- 问答 ----------------
class ChatStreamRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    session_id: str | None = None
    top_k: int = 5
    use_cache: bool = True


class Citation(BaseModel):
    doc_name: str
    page_num: int | None = None
    department_id: str
    department_name: str = ""
    score: float = 0.0                 # 融合后得分
    confidence: Literal["high", "medium", "low"] = "medium"
    snippet: str = ""


class FeedbackRequest(BaseModel):
    session_id: str
    message_id: str | None = None
    question: str
    answer: str
    feedback_type: Literal["like", "dislike"]
    reason: str | None = None


# ---------------- 知识库 ----------------
class DocUploadResponse(BaseModel):
    doc_name: str
    department_id: str
    chunk_count: int
    version: int
    task_id: str


class DocInfo(BaseModel):
    doc_name: str
    department_id: str
    version: int
    chunk_count: int
    uploaded_by: str
    uploaded_at: str


class SearchRequest(BaseModel):
    query: str
    top_k: int = 5


# ---------------- 管理员 ----------------
class HealthComponent(BaseModel):
    name: str
    status: Literal["ok", "degraded", "down"]
    detail: str = ""
    latency_ms: float = 0.0


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "down"]
    version: str
    components: list[HealthComponent]
    notes: list[str] = []        # 降级/异常的具体原因，面向运维可直接读
    models: dict = {}            # 各模型名称与版本信息（LLM / Embedding / 意图识别 / OCR）


LoginResponse.model_rebuild()
