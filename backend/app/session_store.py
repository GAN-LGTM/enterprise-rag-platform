"""
按账号隔离的会话存储（含 JSON 文件持久化）。

需求映射：
  · 每个账号登录后只看到自己的会话 —— 按 user_id 隔离，接口层强制归属校验（防横向越权）
  · 会话标题默认"新会话"，首轮提问后自动改为问题摘要（前端侧边栏不再一排"新会话"）
  · 会话与消息落盘 data/sessions.json，服务重启不丢、页面刷新可恢复历史

生产环境替换为 PostgreSQL conversations / messages 两张表即可，函数签名不变。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

from .logging_setup import get_logger

log = get_logger("session")

_STORE_PATH = Path(os.environ.get("APP_DATA_DIR") or (Path(__file__).resolve().parents[2] / "data")) / "sessions.json"
_LOCK = threading.RLock()
_DATA: dict[str, list[dict]] = {}      # user_id -> [session, ...]，新的在前
_LOADED = False

MAX_SESSIONS_PER_USER = 50
MAX_MESSAGES_PER_SESSION = 100


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        if _STORE_PATH.exists():
            raw = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _DATA.update(raw)
            log.info("session.loaded", path=str(_STORE_PATH), users=len(_DATA))
    except Exception as e:  # noqa: BLE001
        log.warning("session.load_failed", error=str(e))


def _save() -> None:
    """原子写入（tmp + os.replace），避免写一半被读坏。"""
    try:
        _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STORE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_DATA, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _STORE_PATH)
    except Exception as e:  # noqa: BLE001
        log.warning("session.save_failed", error=str(e))


def _now() -> float:
    return round(time.time(), 3)


def list_sessions(user_id: str) -> list[dict]:
    with _LOCK:
        _load()
        # 置顶会话按 pin 时间倒序排最前（拖拽排序时会重写 pin 时间），其余按存储顺序（新的在前）
        bucket = sorted(_DATA.get(user_id, []),
                        key=lambda s: s.get("pinned") or 0, reverse=True)
        return [
            {"id": s["id"], "title": s["title"], "turns": len(s["messages"]) // 2,
             "created_at": s["created_at"], "updated_at": s["updated_at"],
             "pinned": bool(s.get("pinned"))}
            for s in bucket
        ]


def create_session(user_id: str, session_id: str | None = None,
                   title: str = "新会话") -> dict:
    """创建会话；若 session_id 已属于该用户则幂等返回。"""
    with _LOCK:
        _load()
        sid = session_id or uuid.uuid4().hex[:12]
        bucket = _DATA.setdefault(user_id, [])
        existed = next((s for s in bucket if s["id"] == sid), None)
        if existed:
            return existed
        s = {"id": sid, "title": title, "messages": [],
             "created_at": _now(), "updated_at": _now()}
        bucket.insert(0, s)
        del bucket[MAX_SESSIONS_PER_USER:]          # 上限裁剪
        _save()
        return s


def get_session(user_id: str, session_id: str) -> dict | None:
    """严格归属校验：不存在或不属于该用户一律 None（防横向越权读他人会话）。"""
    with _LOCK:
        _load()
        return next((s for s in _DATA.get(user_id, []) if s["id"] == session_id), None)


def append_exchange(user_id: str, session_id: str, question: str, answer: str,
                    citations: list | None = None) -> dict | None:
    """写入一轮问答；标题还是默认值时用首个问题自动命名。"""
    with _LOCK:
        _load()
        s = get_session(user_id, session_id)
        if s is None:
            s = create_session(user_id, session_id)
        s["messages"].append({"role": "user", "content": question, "ts": _now()})
        s["messages"].append({"role": "assistant", "content": answer,
                              "citations": citations or [], "ts": _now()})
        del s["messages"][MAX_MESSAGES_PER_SESSION:]
        if s["title"] == "新会话" and question.strip():
            q = " ".join(question.split())
            s["title"] = (q[:18] + "…") if len(q) > 18 else q
        s["updated_at"] = _now()
        _save()
        return s


def delete_session(user_id: str, session_id: str) -> bool:
    with _LOCK:
        _load()
        bucket = _DATA.get(user_id, [])
        kept = [s for s in bucket if s["id"] != session_id]
        changed = len(kept) != len(bucket)
        if changed:
            _DATA[user_id] = kept
            _save()
        return changed


def rename_session(user_id: str, session_id: str, title: str) -> bool:
    with _LOCK:
        _load()
        s = get_session(user_id, session_id)
        if s is None:
            return False
        s["title"] = title.strip()[:40] or s["title"]
        s["updated_at"] = _now()
        _save()
        return True


def pin_session(user_id: str, session_id: str, pinned: bool) -> bool:
    """置顶 / 取消置顶；置顶时间作为排序键，新置顶的排最上。"""
    with _LOCK:
        _load()
        s = get_session(user_id, session_id)
        if s is None:
            return False
        if pinned:
            s["pinned"] = _now()
        else:
            s.pop("pinned", None)
        _save()
        return True


def reorder_pinned(user_id: str, ordered_ids: list[str]) -> int:
    """持久化置顶会话的拖拽顺序：按传入顺序重写 pinned 时间戳（前面的更大）。"""
    with _LOCK:
        _load()
        base = _now()
        n = len(ordered_ids)
        hit = 0
        for i, sid in enumerate(ordered_ids):
            s = get_session(user_id, sid)
            if s is not None and s.get("pinned"):
                s["pinned"] = round(base + (n - i), 3)
                hit += 1
        if hit:
            _save()
        return hit
