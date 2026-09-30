"""通用通知存储：权限申请结果、文档删除审批结果等需要"送达用户"的事件。

为什么单独建一个 store，而不是复用 gap_store 的通知：
  - gap 通知只覆盖"知识缺口已补充"一种场景，且挂在工单记录里；
  - 权限审批通过/驳回、文档删除审批结果没有工单可挂，需要独立的通用通道。
  - 请假/报销审批结果维持原有 chat 内 push 通道（pop 语义），不进本 store，避免两处消费竞争。

落盘 data/notifications.json（原子写），生产换成 PostgreSQL notifications 表即可。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

from ..logging_setup import get_logger

log = get_logger("notify")

_DATA_DIR = Path(os.environ.get("APP_DATA_DIR") or (Path(__file__).resolve().parents[2] / "data"))
_PATH = _DATA_DIR / "notifications.json"
_LOCK = threading.RLock()
_ITEMS: list[dict] = []
_LOADED = False
_MAX_ITEMS = 2000


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        if _PATH.exists():
            raw = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                _ITEMS.extend(raw[-_MAX_ITEMS:])
    except Exception as e:  # noqa: BLE001
        log.warning("notify.load_failed", error=str(e))


def _save() -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_ITEMS[-_MAX_ITEMS:], ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _PATH)
    except Exception as e:  # noqa: BLE001
        log.warning("notify.save_failed", error=str(e))


def push(username: str, ntype: str, title: str, message: str) -> dict:
    """给某个用户发一条通知。ntype: permission / doc_audit / system。"""
    with _LOCK:
        _load()
        item = {
            "id": "n" + uuid.uuid4().hex[:12],
            "username": username,
            "type": ntype,
            "title": title[:80],
            "message": message[:500],
            "created_at": round(time.time(), 3),
            "read": False,
        }
        _ITEMS.append(item)
        _save()
        log.info("notify.push", user=username, type=ntype, title=title[:30])
        return item


def list_for(username: str, limit: int = 50) -> dict:
    with _LOCK:
        _load()
        mine = [i for i in _ITEMS if i.get("username") == username]
        mine.sort(key=lambda x: -x.get("created_at", 0.0))
        return {"unread": sum(1 for i in mine if not i.get("read")),
                "items": mine[:limit]}


def mark_read(username: str, ids: list[str] | None = None) -> int:
    """标记已读。ids 为空 = 全部已读。返回实际标记条数。"""
    with _LOCK:
        _load()
        want = set(ids or [])
        n = 0
        for i in _ITEMS:
            if i.get("username") != username or i.get("read"):
                continue
            if want and i.get("id") not in want:
                continue
            i["read"] = True
            n += 1
        if n:
            _save()
        return n
