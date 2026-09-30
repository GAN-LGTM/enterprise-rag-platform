"""JWT 签发/校验 + bcrypt 密码哈希 + 登录失败锁定。"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import bcrypt
import jwt

from ..config import settings
from ..errors import AuthError
from .token_store import (
    add_refresh_jti,
    blacklist_access_jti,
    is_access_blacklisted,
    is_refresh_valid,
    revoke_refresh_jti,
)

# 失败计数：{username: (fail_count, lock_until_ts)}
#
# 为什么要落盘而不是只放内存：生产镜像以 `uvicorn --workers 2` 运行，两个 worker
# 是同一容器内的两个进程。计数只存内存时，5 次锁定会被拆成"每进程各 5 次"，
# 暴力破解的尝试次数直接翻倍；且锁定状态在进程重启后归零。
# 这里与 token_store 用同一套办法：状态落 data/login_fail.json，按文件指纹
# （mtime+size）增量重载，两个 worker 在秒级内收敛到同一份锁定状态。
_LOGIN_FAIL: dict[str, tuple[int, float]] = {}
_FAIL_PATH = Path(
    os.environ.get("APP_DATA_DIR") or (Path(__file__).resolve().parents[2] / "data")
) / "login_fail.json"
_FAIL_SIG: tuple[float, int] = (-1.0, -1)
_FAIL_LOADED = False


def _load_login_fail() -> None:
    global _FAIL_SIG, _FAIL_LOADED
    try:
        st = _FAIL_PATH.stat()
        sig = (st.st_mtime, st.st_size)
    except OSError:
        sig = (-1.0, -1)
    if _FAIL_LOADED and sig == _FAIL_SIG:
        return
    _FAIL_SIG = sig
    try:
        raw = json.loads(_FAIL_PATH.read_text(encoding="utf-8")) if _FAIL_PATH.exists() else {}
        if isinstance(raw, dict):
            now = time.time()
            _LOGIN_FAIL.clear()
            for k, v in raw.items():
                if isinstance(v, (list, tuple)) and len(v) == 2:
                    cnt, until = int(v[0]), float(v[1])
                    # 已过锁定窗口且未达阈值的计数没有意义，顺手清掉，避免文件无限增长
                    if until > now or cnt < settings.LOGIN_FAIL_LIMIT:
                        _LOGIN_FAIL[k] = (cnt, until)
    except Exception:  # noqa: BLE001
        pass
    _FAIL_LOADED = True


def _save_login_fail() -> None:
    global _FAIL_SIG
    try:
        _FAIL_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _FAIL_PATH.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({k: [v[0], v[1]] for k, v in _LOGIN_FAIL.items()}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, _FAIL_PATH)
        st = _FAIL_PATH.stat()
        _FAIL_SIG = (st.st_mtime, st.st_size)
    except Exception:  # noqa: BLE001
        pass


def hash_password(raw: str) -> str:
    return bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(raw.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def create_token(payload: dict[str, Any], expire_minutes: int | None = None,
                 typ: str = "access") -> str:
    """签发 JWT。每个令牌都带唯一 jti（用于登出黑名单 / 刷新令牌轮转）。"""
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=expire_minutes or settings.JWT_EXPIRE_MINUTES
    )
    body = {**payload, "exp": expire, "iat": datetime.now(timezone.utc),
            "jti": uuid.uuid4().hex, "typ": typ}
    return jwt.encode(body, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(payload: dict[str, Any]) -> str:
    """刷新令牌：有效期更长（天），仅用于换取新的访问令牌，不参与业务鉴权。"""
    return create_token(payload, expire_minutes=settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60,
                        typ="refresh")


def decode_token(token: str, expect_typ: str = "access") -> dict[str, Any]:
    """校验签名/有效期并解出载荷，同时**强制令牌类型**。

    为什么必须校验 typ：访问令牌只有 30 分钟，刷新令牌长达 7 天，两者用同一个
    签名密钥签发。若这里不校验类型，任何人都可以拿 refresh_token 直接当访问令牌
    调业务接口——短期访问令牌的时效设计被绕过，且登出（只拉黑访问令牌 jti）
    对它无效，等于给系统留了一把 7 天有效的万能钥匙。
    """
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise AuthError("Token 已过期")
    except jwt.InvalidTokenError:
        raise AuthError("Token 无效")
    if payload.get("typ") != expect_typ:
        raise AuthError("Token 类型不合法，请重新登录")
    # 仅访问令牌参与登出黑名单；刷新令牌走白名单校验（见 token_store）
    if expect_typ == "access" and is_access_blacklisted(payload.get("jti"), time.time()):
        raise AuthError("登录状态已失效，请重新登录")
    return payload


def decode_refresh_token(token: str) -> dict[str, Any]:
    """仅用于校验刷新令牌：必须 typ=refresh，过期即失效（需重新登录）。"""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise AuthError("刷新令牌已过期，请重新登录")
    except jwt.InvalidTokenError:
        raise AuthError("刷新令牌无效")
    if payload.get("typ") != "refresh":
        raise AuthError("非法的刷新令牌")
    return payload


def _jti_of(token: str) -> str | None:
    try:
        return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM],
                          options={"verify_exp": False}).get("jti")
    except Exception:  # noqa: BLE001
        return None


def issue_tokens(user) -> dict:
    """登录 / 刷新成功后下发双令牌：短期访问令牌 + 长期刷新令牌（并已登记白名单）。"""
    access = create_token({"sub": user.username, "uid": user.user_id, "role": user.role})
    refresh = create_refresh_token({"sub": user.username, "uid": user.user_id, "role": user.role})
    jti = _jti_of(refresh)
    if jti:
        add_refresh_jti(jti)
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": settings.JWT_EXPIRE_MINUTES * 60,
        "refresh_expires_in": settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60,
    }


def rotate_refresh(refresh_token: str, users) -> dict:
    """用刷新令牌换取新的一对令牌，并立即作废旧刷新令牌（防重放 / 防共享）。"""
    payload = decode_refresh_token(refresh_token)
    jti = payload.get("jti")
    if not is_refresh_valid(jti):
        raise AuthError("刷新令牌已失效，请重新登录")
    revoke_refresh_jti(jti)
    user = users.get(payload.get("sub"))
    if not user:
        raise AuthError("用户不存在或已停用")
    return issue_tokens(user)


def logout(access_token: str | None, refresh_token: str | None) -> None:
    """登出：拉黑当前访问令牌（至其到期）+ 撤销刷新令牌，双令牌同时失效。"""
    if access_token:
        blacklist_access_jti(_jti_of(access_token), _exp_of(access_token))
    if refresh_token:
        revoke_refresh_jti(_jti_of(refresh_token))


def _exp_of(token: str) -> float | None:
    try:
        return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM],
                         options={"verify_exp": False}).get("exp")
    except Exception:  # noqa: BLE001
        return None


def check_login_lock(username: str) -> None:
    """登录失败 5 次锁定 15 分钟，防暴力破解（状态跨 worker 共享）。"""
    _load_login_fail()
    fail, until = _LOGIN_FAIL.get(username, (0, 0.0))
    if fail >= settings.LOGIN_FAIL_LIMIT and time.time() < until:
        left = int(until - time.time())
        raise AuthError(f"连续登录失败次数过多，账号已锁定，请 {left} 秒后重试")


def record_login_fail(username: str) -> None:
    _load_login_fail()
    fail, until = _LOGIN_FAIL.get(username, (0, 0.0))
    fail += 1
    if fail >= settings.LOGIN_FAIL_LIMIT:
        until = time.time() + settings.LOGIN_LOCK_MINUTES * 60
    _LOGIN_FAIL[username] = (fail, until)
    _save_login_fail()


def clear_login_fail(username: str) -> None:
    _load_login_fail()
    if username in _LOGIN_FAIL:
        _LOGIN_FAIL.pop(username, None)
        _save_login_fail()


def validate_password(raw: str) -> tuple[bool, str]:
    """密码复杂度校验（FR-AUTH-02）。企业交付基线：8+ 位且含大小写+数字+特殊字符。

    返回 (是否合规, 不合规时的中文说明)。仅对明文做规则校验，绝不记录明文。
    """
    if not raw:
        return False, "密码不能为空"
    if len(raw) < settings.PASSWORD_MIN_LEN:
        return False, f"密码至少 {settings.PASSWORD_MIN_LEN} 位"
    if settings.PASSWORD_REQUIRE_UPPER and not any(c.isupper() for c in raw):
        return False, "密码需包含至少一个大写字母"
    if settings.PASSWORD_REQUIRE_LOWER and not any(c.islower() for c in raw):
        return False, "密码需包含至少一个小写字母"
    if settings.PASSWORD_REQUIRE_DIGIT and not any(c.isdigit() for c in raw):
        return False, "密码需包含至少一个数字"
    if settings.PASSWORD_REQUIRE_SPECIAL and not any(not c.isalnum() for c in raw):
        return False, "密码需包含至少一个特殊字符（如 !@#$%^&*）"
    return True, ""
