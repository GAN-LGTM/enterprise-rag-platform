"""
权限申请工单（FR-PERM-FLOW）：员工申请访问某部门知识库，主管 / 高管 / 管理员审批后
自动开通（grant_department）。所有变更均记审计日志，满足"权限变更可审计"的合规要求。

生产替换：平移到 permission_requests 表即可，函数签名不变。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

from ..config import settings

_STORE_PATH = Path(os.environ.get("APP_DATA_DIR") or (Path(__file__).resolve().parents[2] / "data")) / "permission_requests.json"
_LOCK = threading.RLock()
_ITEMS: list[dict] = []
_LOADED = False


def _load() -> None:
    global _LOADED, _ITEMS
    if _LOADED:
        return
    _LOADED = True
    try:
        if _STORE_PATH.exists():
            raw = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                _ITEMS = raw
    except Exception:  # noqa: BLE001
        pass


def _save() -> None:
    try:
        _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STORE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_ITEMS, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _STORE_PATH)
    except Exception:  # noqa: BLE001
        pass


def create_request(username: str, display_name: str, department_id: str, reason: str,
                   permission_type: str = "read", expires_at: float | None = None) -> dict:
    """新建申请单。

    permission_type: read（只读）/ download（可下载）——企业里"能看"和"能把文件拿走"
    是两件事，必须分开申请、分开审批。
    expires_at: 申请的权限有效期（Unix 秒），None = 长期有效。
    """
    item = {
        "id": "PR" + uuid.uuid4().hex[:10].upper(),
        "username": username,
        "display_name": display_name,
        "department_id": department_id,
        "reason": reason[:500],
        "permission_type": permission_type if permission_type in ("read", "download") else "read",
        "expires_at": expires_at,
        "status": "pending",
        "created_at": round(time.time(), 3),
        "decided_by": None,
        "decided_at": None,
    }
    with _LOCK:
        _load()
        _ITEMS.append(item)
        _save()
    return item


def get_request(rid: str) -> dict | None:
    with _LOCK:
        _load()
        return next((i for i in _ITEMS if i["id"] == rid), None)


def list_requests(status: str | None = None, username: str | None = None,
                   approver_depts: list[str] | None = None) -> list[dict]:
    with _LOCK:
        _load()
        items = _ITEMS
        if status:
            items = [i for i in items if i["status"] == status]
        if username:
            items = [i for i in items if i["username"] == username]
        if approver_depts is not None:
            items = [i for i in items if i["department_id"] in approver_depts]
        return sorted(items, key=lambda x: x["created_at"], reverse=True)


def decide(rid: str, status: str, operator: str) -> dict | None:
    with _LOCK:
        _load()
        rec = next((i for i in _ITEMS if i["id"] == rid), None)
        if rec is None or rec["status"] != "pending":
            return rec
        rec["status"] = status
        rec["decided_by"] = operator
        rec["decided_at"] = round(time.time(), 3)
        _save()
        return rec


def cancel(rid: str, username: str) -> dict | None:
    """申请人撤回自己的待审批申请（只能撤回 pending，已处理的不能再动）。"""
    with _LOCK:
        _load()
        rec = next((i for i in _ITEMS if i["id"] == rid), None)
        if rec is None or rec["status"] != "pending" or rec["username"] != username:
            return rec
        rec["status"] = "canceled"
        rec["decided_by"] = f"{rec['display_name']}（申请人撤回）"
        rec["decided_at"] = round(time.time(), 3)
        _save()
        return rec
