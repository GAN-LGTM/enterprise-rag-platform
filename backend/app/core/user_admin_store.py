"""用户管理存储：管理员在"用户管理"页做的账号变更，持久化到 data/users_admin.json。

设计边界：
  - 内置账号（models.USERS）是"出厂账号"，不直接改源文件；管理员的新增/改密/调角色/停用
    全部记录为 override，启动时按"先加载内置 → 再套用 override"的顺序合成运行态。
  - 这样 reset_demo_data.py 只需清空本文件即可恢复出厂账号，符合"交付前可还原"的运维约定。
  - 生产替换：本文件换成 PostgreSQL users 表即可，函数签名不变。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from ..db.models import DEPARTMENTS, USERS, User
from ..logging_setup import get_logger

log = get_logger("user_admin")

_DATA_DIR = Path(os.environ.get("APP_DATA_DIR") or (Path(__file__).resolve().parents[2] / "data"))
_PATH = _DATA_DIR / "users_admin.json"
_LOCK = threading.RLock()

# username -> {display_name, role, dept_id, managed_dept_ids, can_upload,
#              password_hash, disabled, source, created_by, created_at}
_OVERRIDES: dict[str, dict] = {}
_LOADED = False

VALID_ROLES = {
    "admin": "系统管理员", "executive": "事业部高管", "dept_director": "事业部总监",
    "sub_manager": "部门主管", "sub_employee": "普通员工",
    "knowledge_reviewer": "知识审核员", "compliance_reviewer": "合规审核员",
}


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        if _PATH.exists():
            raw = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _OVERRIDES.update(raw)
    except Exception as e:  # noqa: BLE001
        log.warning("user_admin.load_failed", error=str(e))


def _save() -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_OVERRIDES, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, _PATH)
    except Exception as e:  # noqa: BLE001
        log.warning("user_admin.save_failed", error=str(e))


def apply_overrides() -> None:
    """把 override 套用到运行态 USERS（启动时与用户变更后调用，幂等）。"""
    with _LOCK:
        _load()
        for uname, ov in _OVERRIDES.items():
            u = USERS.get(uname)
            if u is None:
                # 管理员新增的账号：创建运行态 User
                if not ov.get("password_hash"):
                    continue
                USERS[uname] = User(
                    user_id=ov.get("user_id") or f"u_custom_{uname}",
                    username=uname,
                    password_hash=ov["password_hash"],
                    display_name=ov.get("display_name") or uname,
                    role=ov.get("role") or "sub_employee",
                    dept_id=ov.get("dept_id") or "public",
                    managed_dept_ids=list(ov.get("managed_dept_ids") or []),
                    can_upload=bool(ov.get("can_upload")),
                    must_change_password=bool(ov.get("must_change_password", True)),
                )
            else:
                # 对内置账号的覆盖：改密 / 调角色 / 调部门
                if ov.get("password_hash"):
                    u.password_hash = ov["password_hash"]
                if ov.get("role"):
                    u.role = ov["role"]
                if ov.get("dept_id"):
                    u.dept_id = ov["dept_id"]
                if "managed_dept_ids" in ov:
                    u.managed_dept_ids = list(ov.get("managed_dept_ids") or [])
                if "can_upload" in ov:
                    u.can_upload = bool(ov["can_upload"])
                if ov.get("display_name"):
                    u.display_name = ov["display_name"]


def is_disabled(username: str) -> bool:
    with _LOCK:
        _load()
        return bool((_OVERRIDES.get(username) or {}).get("disabled"))


def _base(username: str) -> dict:
    return _OVERRIDES.setdefault(username, {})


def create_user(username: str, display_name: str, password_hash: str, role: str,
                dept_id: str, *, operator: str = "") -> None:
    with _LOCK:
        _load()
        ov = _base(username)
        ov.update({
            "user_id": f"u_custom_{username}",
            "display_name": display_name, "password_hash": password_hash,
            "role": role, "dept_id": dept_id, "managed_dept_ids": [],
            "can_upload": role in ("admin", "dept_director", "sub_manager", "knowledge_reviewer"),
            "must_change_password": True, "disabled": False,
            "created_by": operator, "created_at": round(time.time(), 3),
        })
        _save()
        apply_overrides()


def reset_password(username: str, password_hash: str, *, operator: str = "") -> None:
    with _LOCK:
        _load()
        ov = _base(username)
        ov["password_hash"] = password_hash
        ov["must_change_password"] = True       # 重置后首次登录强制改密
        ov["reset_by"] = operator
        ov["reset_at"] = round(time.time(), 3)
        _save()
        apply_overrides()


def set_role(username: str, role: str, dept_id: str | None = None, *, operator: str = "") -> None:
    with _LOCK:
        _load()
        ov = _base(username)
        ov["role"] = role
        if dept_id:
            ov["dept_id"] = dept_id
        ov["role_by"] = operator
        ov["role_at"] = round(time.time(), 3)
        _save()
        apply_overrides()


def set_disabled(username: str, disabled: bool, *, operator: str = "") -> None:
    with _LOCK:
        _load()
        ov = _base(username)
        ov["disabled"] = disabled
        ov["status_by"] = operator
        ov["status_at"] = round(time.time(), 3)
        _save()


def list_users() -> list[dict]:
    """用户管理页数据源：运行态 User + 管理元信息（来源/停用/创建人）。"""
    with _LOCK:
        _load()
        out = []
        for uname, u in USERS.items():
            ov = _OVERRIDES.get(uname) or {}
            out.append({
                "username": uname,
                "display_name": u.display_name,
                "role": u.role,
                "role_label": VALID_ROLES.get(u.role, u.role),
                "dept_id": u.dept_id,
                "dept_name": DEPARTMENTS.get(u.dept_id).name if u.dept_id in DEPARTMENTS else u.dept_id,
                "can_upload": u.can_upload,
                "must_change_password": u.must_change_password,
                "disabled": bool(ov.get("disabled")),
                "source": "custom" if ov.get("created_by") else "builtin",
                "created_by": ov.get("created_by", ""),
                "created_at": ov.get("created_at"),
            })
        out.sort(key=lambda x: (x["source"] != "builtin", x["username"]))
        return out
