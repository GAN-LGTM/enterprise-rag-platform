"""接口层依赖注入：统一从 JWT 解析身份，绝不信任前端传的部门字段。"""
from __future__ import annotations

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..core.security import decode_token
from ..db.models import USERS, User, dept_name
from ..errors import AuthError
from ..schemas.dto import UserInfo

bearer = HTTPBearer(auto_error=False)


def get_token(request: Request, cred: HTTPAuthorizationCredentials | None) -> str:
    """
    Token 支持两种携带方式：
      1) Authorization: Bearer <jwt>   （推荐）
      2) ?token=<jwt>                  （SSE/下载等场景，浏览器 EventSource 限制时用）
    """
    if cred and cred.credentials:
        return cred.credentials
    t = request.query_params.get("token")
    if t:
        return t
    raise AuthError("缺少身份凭证")


async def current_user(
    request: Request,
    cred: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> User:
    payload = decode_token(get_token(request, cred))
    username = payload.get("sub")
    user = USERS.get(username or "")
    if not user:
        raise AuthError("用户不存在或已停用")
    return user


async def active_user(user: User = Depends(current_user)) -> User:
    """受保护业务接口使用：被强制改密的账号在未改密前一律拦截（FR-AUTH-01）。

    /api/auth/change-password 仍用 current_user（不在此拦截），否则永远改不了密码。
    """
    if user.must_change_password:
        raise AuthError("请先修改初始密码后再使用系统")
    return user


def to_user_info(user: User) -> UserInfo:
    from ..db.models import get_accessible_depts
    from ..db.models import DEPARTMENTS

    dept = DEPARTMENTS.get(user.dept_id)
    parent = DEPARTMENTS.get(dept.parent_id) if dept and dept.parent_id else None
    return UserInfo(
        user_id=user.user_id, username=user.username, display_name=user.display_name,
        role=user.role,  # type: ignore[arg-type]
        dept_id=user.dept_id, dept_name=dept_name(user.dept_id),
        parent_dept_id=parent.id if parent else (dept.id if dept else ""),
        parent_dept_name=parent.name if parent else (dept.name if dept else ""),
        accessible_depts=get_accessible_depts(user),
        must_change_password=user.must_change_password,
    )
