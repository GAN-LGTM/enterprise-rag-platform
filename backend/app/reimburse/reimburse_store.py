"""
报销数据持久化（JSON 文件，零依赖；生产替换为 PostgreSQL 两张表：
reimburse_drafts 与 reimburse_requests 即可，函数签名不变）。

两类数据：
  · 草稿 drafts：按 user_id 隔离，引导式采集过程中临时保存当前填写进度。
  · 申请 requests：正式提交后的报销单，含审批链 chain（每一步的审批人/状态/意见）。

并发安全：单进程 RLock + 原子写（tmp + os.replace）。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

from ..logging_setup import get_logger

log = get_logger("reimburse.store")

_STORE_PATH = Path(os.environ.get("APP_DATA_DIR") or (Path(__file__).resolve().parents[2] / "data")) / "reimburse.json"
_LOCK = threading.RLock()
_DATA: dict = {"drafts": {}, "requests": []}
_LOADED = False


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        if _STORE_PATH.exists():
            raw = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _DATA["drafts"] = raw.get("drafts", {}) or {}
                _DATA["requests"] = raw.get("requests", []) or []
            log.info("reimburse.loaded", path=str(_STORE_PATH),
                     requests=len(_DATA["requests"]))
    except Exception as e:  # noqa: BLE001
        log.warning("reimburse.load_failed", error=str(e))


def _save() -> None:
    try:
        _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STORE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_DATA, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _STORE_PATH)
    except Exception as e:  # noqa: BLE001
        log.warning("reimburse.save_failed", error=str(e))


def _now() -> float:
    return round(time.time(), 3)


# ============================================================
#  草稿（按 user_id 隔离）
# ============================================================
def get_draft(user_id: str) -> dict | None:
    with _LOCK:
        _load()
        return _DATA["drafts"].get(user_id)


def save_draft(user_id: str, draft: dict) -> None:
    with _LOCK:
        _load()
        draft["updated_at"] = _now()
        _DATA["drafts"][user_id] = draft
        _save()


def delete_draft(user_id: str) -> None:
    with _LOCK:
        _load()
        if user_id in _DATA["drafts"]:
            del _DATA["drafts"][user_id]
            _save()


# ============================================================
#  报销申请
# ============================================================
def create_request(req: dict) -> dict:
    with _LOCK:
        _load()
        _DATA["requests"].insert(0, req)
        _save()
        return req


def get_request(rid: str) -> dict | None:
    with _LOCK:
        _load()
        return next((r for r in _DATA["requests"] if r["id"] == rid), None)


def list_requests_by_applicant(user_id: str) -> list[dict]:
    with _LOCK:
        _load()
        return [r for r in _DATA["requests"] if r["applicant_id"] == user_id]


def list_pending_for_approver(user_id: str) -> list[dict]:
    """返回当前用户作为「当前待审步骤」审批人的申请。"""
    with _LOCK:
        _load()
        out = []
        for r in _DATA["requests"]:
            if r["status"] != "pending":
                continue
            step = _current_step(r)
            if step and step["approver_id"] == user_id and step["status"] == "pending":
                out.append(r)
        return out


def _current_step(req: dict) -> dict | None:
    for s in req["chain"]:
        if s["status"] == "pending":
            return s
    return None


def _apply_decision(req: dict, user_id: str, decision: str, comment: str | None) -> dict | None:
    """
    对当前待审步骤落审批意见；返回 req（调用方负责 _save）。

    返回值：更新后的 req；若 user_id 不是当前待审人返回 None（防越权）。
    """
    step = _current_step(req)
    if step is None or step["approver_id"] != user_id:
        return None
    step["status"] = decision            # approved / rejected
    step["acted_at"] = _now()
    step["comment"] = comment or ""
    return req


def approve_step(rid: str, user_id: str, comment: str | None = None) -> dict | None:
    with _LOCK:
        _load()
        req = get_request(rid)
        if req is None:
            return None
        if _apply_decision(req, user_id, "approved", comment) is None:
            return None
        _advance(req)
        _save()
        return req


def reject_step(rid: str, user_id: str, comment: str | None = None) -> dict | None:
    with _LOCK:
        _load()
        req = get_request(rid)
        if req is None:
            return None
        if _apply_decision(req, user_id, "rejected", comment) is None:
            return None
        req["status"] = "rejected"
        req["completed_at"] = _now()
        _save()
        return req


def _advance(req: dict) -> None:
    """根据审批链推进：全部通过→approved；否则保持 pending 等待下一步。"""
    if all(s["status"] == "approved" for s in req["chain"]):
        req["status"] = "approved"
        req["completed_at"] = _now()
        req["notified"] = False          # 待向申请人推送"审批通过"提示
        log.info("reimburse.approved", rid=req["id"], applicant=req["applicant_id"])
    else:
        req["status"] = "pending"


def pop_notifications(user_id: str) -> list[dict]:
    """
    取出并清除该申请人的未读结果通知（审批通过/驳回）。
    每次聊天首帧或轮询调用，保证「审批完成后会有提示」。
    """
    with _LOCK:
        _load()
        events: list[dict] = []
        for r in _DATA["requests"]:
            if r["applicant_id"] != user_id:
                continue
            if r.get("notified") is False and r["status"] in ("approved", "rejected"):
                events.append({
                    "rid": r["id"],
                    "status": r["status"],
                    "rb_type": r["rb_type"],
                    "amount": r["amount"],
                    "completed_at": r["completed_at"],
                })
                r["notified"] = True
        if events:
            _save()
        return events


def gen_request_id() -> str:
    day = time.strftime("%y%m%d")
    return f"RB{day}{uuid.uuid4().hex[:4].upper()}"
