"""文档删除审批存储（FR-KB-05 扩展）。

规则：主管/总监/经理提交删除申请 → 管理员审批通过后才真正删除；管理员本人可直接删除。
数据持久化到 data/doc_deletions.json（支持 APP_DATA_DIR 覆盖，供测试隔离）。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

_DATA_DIR = Path(os.environ.get("APP_DATA_DIR") or (Path(__file__).resolve().parents[2] / "data"))
_PATH = _DATA_DIR / "doc_deletions.json"

_LOCK = threading.Lock()
_REQUESTS: dict[str, dict] | None = None


def _load() -> dict[str, dict]:
    global _REQUESTS
    if _REQUESTS is None:
        if _PATH.exists():
            try:
                _REQUESTS = json.loads(_PATH.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                _REQUESTS = {}
        else:
            _REQUESTS = {}
    return _REQUESTS


def _save() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(_REQUESTS, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _PATH)


def create_request(doc_name: str, department_id: str, username: str, display_name: str) -> dict:
    """创建删除申请；同一文档同一部门已有待审申请时直接复用，避免重复堆单。"""
    with _LOCK:
        data = _load()
        for r in data.values():
            if (r["status"] == "pending"
                    and r["doc_name"] == doc_name and r["department_id"] == department_id):
                return r
        rid = uuid.uuid4().hex[:12]
        req = {
            "id": rid, "doc_name": doc_name, "department_id": department_id,
            "requested_by": username, "requested_by_name": display_name,
            "requested_at": time.time(), "status": "pending",
            "reviewed_by": None, "reviewed_at": None,
        }
        data[rid] = req
        _save()
        return req


def list_requests(status: str | None = None, limit: int = 100) -> list[dict]:
    with _LOCK:
        items = sorted(_load().values(), key=lambda r: r["requested_at"], reverse=True)
    if status:
        items = [r for r in items if r["status"] == status]
    return items[:limit]


def get_request(rid: str) -> dict | None:
    with _LOCK:
        return _load().get(rid)


def set_status(rid: str, status: str, reviewer: str) -> dict | None:
    with _LOCK:
        req = _load().get(rid)
        if req is None:
            return None
        req["status"] = status
        req["reviewed_by"] = reviewer
        req["reviewed_at"] = time.time()
        _save()
        return req
