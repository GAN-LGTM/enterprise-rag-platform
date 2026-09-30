"""认证接口：登录、登出、当前身份。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..config import settings
from ..core import audit
from ..core.security import (
    check_login_lock,
    clear_login_fail,
    hash_password,
    issue_tokens,
    logout as security_logout,
    record_login_fail,
    rotate_refresh,
    validate_password,
    verify_password,
)
from ..db.models import USERS, get_user_by_name
from ..errors import AuthError, NotFound
from ..schemas.dto import ChangePasswordRequest, LoginRequest, LoginResponse
from .deps import active_user, current_user, to_user_info

router = APIRouter(prefix="/api/auth", tags=["认证"])


@router.post("/login", response_model=LoginResponse, summary="用户登录（JWT）")
async def login(body: LoginRequest):
    check_login_lock(body.username)

    # 管理员停用账号：即使密码正确也拒绝登录（区别于"密码错误"，明确告知原因）
    from ..core import user_admin_store
    if user_admin_store.is_disabled(body.username):
        audit.record("login_fail", user_id=body.username, user_name=body.username,
                     detail="账号已停用，登录被拒绝")
        raise AuthError("账号已被停用，请联系系统管理员")

    user = get_user_by_name(body.username)
    if not user or not verify_password(body.password, user.password_hash):
        record_login_fail(body.username)
        audit.record("login_fail", user_id=body.username, user_name=body.username,
                     detail="用户名或密码错误")
        raise AuthError("用户名或密码错误")

    clear_login_fail(body.username)
    audit.record("login_ok", user_id=user.user_id, user_name=user.display_name,
                 detail="登录成功", role=user.role)
    tokens = issue_tokens(user)
    return LoginResponse(
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        expires_in=tokens["expires_in"],
        refresh_expires_in=tokens["refresh_expires_in"],
        user=to_user_info(user),
    )


@router.post("/change-password", summary="修改密码（强制改密 / 主动修改，FR-AUTH-01 / FR-AUTH-02）")
async def change_password(body: ChangePasswordRequest, user=Depends(current_user)):
    # 校验旧密码
    if not verify_password(body.old_password, user.password_hash):
        audit.record("change_pwd_fail", user_id=user.user_id, user_name=user.display_name,
                     detail="旧密码错误")
        raise AuthError("原密码不正确")
    if body.old_password == body.new_password:
        raise HTTPException(status_code=400, detail="新密码不能与旧密码相同")
    # 密码复杂度校验（FR-AUTH-02）：8+ 位且含大小写 + 数字 + 特殊字符
    ok, msg = validate_password(body.new_password)
    if not ok:
        audit.record("change_pwd_fail", user_id=user.user_id, user_name=user.display_name,
                     detail="新密码不合复杂度要求")
        raise HTTPException(status_code=400, detail=msg)
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    audit.record("change_pwd_ok", user_id=user.user_id, user_name=user.display_name,
                 detail="密码已更新")
    return {"ok": True, "must_change_password": False}


@router.post("/refresh", summary="刷新访问令牌（避免 30 分钟频繁掉线）")
async def refresh(body: dict):
    """用未过期的刷新令牌换取新的一对令牌；旧刷新令牌立即作废（轮转防重放）。"""
    refresh_token = str(body.get("refresh_token") or "")
    if not refresh_token:
        raise AuthError("缺少刷新令牌")
    try:
        tokens = rotate_refresh(refresh_token, USERS)
    except AuthError as e:
        raise AuthError(str(e))
    return tokens


@router.post("/logout", summary="登出（拉黑访问令牌 + 撤销刷新令牌）")
async def logout(body: dict):
    access_token = str(body.get("access_token") or "")
    refresh_token = str(body.get("refresh_token") or "")
    security_logout(access_token or None, refresh_token or None)
    return {"ok": True}


@router.get("/me", summary="当前登录身份与可访问部门")
async def me(user=Depends(current_user)):
    return to_user_info(user)


@router.get("/demo-accounts", summary="演示账号清单（仅演示模式下可用）")
async def demo_accounts():
    """演示模式专用：让体验者无需查文档即可切换角色试权限。

    生产环境必须返回 404 —— 这些账号的口令是固定值，任何匿名访问者拿到清单
    就等于拿到管理员账号。这里用 DEMO_MODE 做门禁（production 下该开关强制关闭，
    见 config.DEMO_MODE 与 preflight），而不是靠注释约定。
    """
    if not settings.DEMO_MODE:
        raise NotFound()
    return [
        {"username": u.username, "display_name": u.display_name, "role": u.role,
         "dept": u.dept_id, "password": "123456"}
        for u in USERS.values() if u.username != "admin"
    ] + [{"username": "admin", "display_name": "系统管理员", "role": "admin",
          "dept": "*", "password": "admin123"}]
