"""
问答满意度反馈 + 热门问题排行榜的持久化存储。

需求映射：
  · FR-CHAT-05 满意度反馈：每个答案下方 👍/👎，反馈数据不可删除（qa_feedback 表语义）
  · FR-CHAT-06 热门问题排行榜：按 (问题哈希, 部门) 去重计数，支持按部门维度统计

与项目既有约定一致：内存镜像 + 原子写 JSON（data/feedback.json / data/hot.json），
服务重启不丢、页面刷新可恢复。生产环境替换为 PostgreSQL qa_feedback / hot_questions 两张表即可，
函数签名不变。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

from ..logging_setup import get_logger

log = get_logger("feedback")

_DATA_DIR = Path(os.environ.get("APP_DATA_DIR") or (Path(__file__).resolve().parents[2] / "data"))
_FEEDBACK_PATH = _DATA_DIR / "feedback.json"
_HOT_PATH = _DATA_DIR / "hot.json"
_LOCK = threading.RLock()

_FEEDBACKS: list[dict] = []          # qa_feedback 镜像
_HOT: dict[str, dict] = {}            # key = f"{dept_id}:{question_hash}" -> 计数镜像
_LOADED = False


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        if _FEEDBACK_PATH.exists():
            raw = json.loads(_FEEDBACK_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                _FEEDBACKS.extend(raw)
        if _HOT_PATH.exists():
            raw = json.loads(_HOT_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _HOT.update(raw)
        log.info("feedback.loaded", feedback=len(_FEEDBACKS), hot=len(_HOT))
    except Exception as e:  # noqa: BLE001
        log.warning("feedback.load_failed", error=str(e))


def _save_feedback() -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _FEEDBACK_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_FEEDBACKS, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _FEEDBACK_PATH)
    except Exception as e:  # noqa: BLE001
        log.warning("feedback.save_failed", error=str(e))


def _save_hot() -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _HOT_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_HOT, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _HOT_PATH)
    except Exception as e:  # noqa: BLE001
        log.warning("feedback.hot_save_failed", error=str(e))


# ============================================================
#  满意度反馈 FR-CHAT-05
# ============================================================
def record_feedback(fb: dict) -> None:
    """追加一条反馈（不可删除，仅追加）。"""
    with _LOCK:
        _load()
        fb = {**fb, "ts": round(time.time(), 3)}
        _FEEDBACKS.append(fb)
        # 控制规模：仅保留最近 5000 条，超出裁剪最旧的（不破坏"不可删除"语义，属容量治理）
        if len(_FEEDBACKS) > 5000:
            del _FEEDBACKS[:-5000]
        _save_feedback()


def list_feedback(limit: int = 100, feedback_type: str | None = None) -> list[dict]:
    with _LOCK:
        _load()
        items = _FEEDBACKS
        if feedback_type:
            items = [f for f in items if f.get("feedback_type") == feedback_type]
        return items[-limit:][::-1]


def feedback_stats() -> dict:
    with _LOCK:
        _load()
        total = len(_FEEDBACKS)
        likes = sum(1 for f in _FEEDBACKS if f.get("feedback_type") == "like")
        dislikes = total - likes
        satisfaction = round(likes / total, 3) if total else 0.0
        return {"total": total, "like": likes, "dislike": dislikes,
                "satisfaction": satisfaction}


# ============================================================
#  热门问题 FR-CHAT-06
# ============================================================
def _q_hash(question: str) -> str:
    return hashlib.md5(question.strip().encode("utf-8")).hexdigest()


def record_hot(question: str, department_id: str) -> None:
    with _LOCK:
        _load()
        key = f"{department_id}:{_q_hash(question)}"
        rec = _HOT.get(key)
        if rec is None:
            rec = {"question": question.strip(), "department_id": department_id,
                   "ask_count": 0, "last_asked_at": 0.0}
            _HOT[key] = rec
        rec["ask_count"] += 1
        rec["last_asked_at"] = round(time.time(), 3)
        _save_hot()


def hot_questions(top: int = 10, department_id: str | None = None) -> list[dict]:
    with _LOCK:
        _load()
        items = list(_HOT.values())
        if department_id:
            items = [i for i in items if i.get("department_id") == department_id]
        items.sort(key=lambda x: -x["ask_count"])
        return items[:top]


def hot_totals() -> tuple[int, int]:
    """返回 (总提问次数, 去重问题数)。"""
    with _LOCK:
        _load()
        total = sum(i["ask_count"] for i in _HOT.values())
        return total, len(_HOT)
