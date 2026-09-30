"""
知识缺口台账 v2 —— 从"只记录发生过什么"升级为"可闭环的治理工单"。

传统检索系统的浪费在于：用户搜了 A 没结果，换成 B 找到了，这个行为数据就被丢弃了。
台账解决"该补哪份文档"的问题；v2 进一步把**人和流程**接进来，形成闭环：

    用户提问答不上来 / 答不准
        → 前端给出"没解决？告诉我们缺什么"反馈入口
        → 用户勾选原因 + 补充说明，提交生成**知识缺口工单**（状态：待处理）
        → 管理员/部门主管在治理台看到工单，指派给对应部门负责人
        → 负责人补充文档后，工单流转**已补充**，系统给原提问人生成通知
        → 用户下次再问同类问题能查到答案；若还是不行，可再次反馈 → 工单自动重开

工单字段：
    id              稳定主键 = f"{department_id}:{question_hash}"（同部门同一问题永不重复建单）
    question        原始问题
    department_id   归属部门（决定了谁能处理）
    reason          自动判定原因：no_hit（零命中）/ low_evidence（命中但不相关）/ ""（纯用户反馈）
    feedbacks       用户主动提交的反馈明细（原因标签 + 补充说明 + 提交人 + 时间）
    status          pending 待处理 / supplemented 已补充 / closed 已关闭
    assignee        指派给谁（部门负责人用户名）
    resolution      处理结果：{ docs, note, operator, resolved_at }
    notifications   给提问人的通知队列（补充完成后生成，用户读取后标记 read）

落盘 data/search_gaps.json（原子写），生产换成 PostgreSQL knowledge_gaps 表即可，
对外暴露的函数签名不变 —— 这是本模块刻意保持的替换边界。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

from ..logging_setup import get_logger

log = get_logger("gap")

_DATA_DIR = Path(os.environ.get("APP_DATA_DIR") or (Path(__file__).resolve().parents[2] / "data"))
_GAP_PATH = _DATA_DIR / "search_gaps.json"
_LOCK = threading.RLock()

_GAPS: dict[str, dict] = {}      # key = f"{dept_id}:{question_hash}"
_LOADED = False
_MAX_ITEMS = 2000


# ------------------------------------------------------------
#  枚举：状态 / 原因标签
# ------------------------------------------------------------
STATUS_PENDING = "pending"              # 待处理
STATUS_SUPPLEMENTED = "supplemented"    # 已补充（资料已上架）
STATUS_CLOSED = "closed"                # 已关闭（不补充 / 不在范围内）

STATUS_ORDER = [STATUS_PENDING, STATUS_SUPPLEMENTED, STATUS_CLOSED]
STATUS_LABELS = {STATUS_PENDING: "待处理", STATUS_SUPPLEMENTED: "已补充",
                 STATUS_CLOSED: "已关闭"}

# 自动判定原因（系统侧）与用户反馈原因（人工侧）分开存，避免语义混淆
# 前者回答"缺口怎么来的"，后者回答"用户觉得问题在哪"。
FEEDBACK_REASONS = {
    "inaccurate": "答案不准确",
    "not_found": "没有找到资料",
    "irrelevant_cite": "引用不相关",
    "outdated": "内容已过期",
}
AUTO_REASON_LABELS = {"no_hit": "完全没命中", "low_evidence": "命中但不相关"}


# ------------------------------------------------------------
#  持久化
# ------------------------------------------------------------
def _new_ticket(question: str, department_id: str, *, reason: str = "",
                user_name: str = "", trace_id: str = "") -> dict:
    now = round(time.time(), 3)
    return {
        "question": question,
        "department_id": department_id,
        "reason": reason,
        "count": 0,
        "first_asked_at": now,
        "last_asked_at": 0.0,
        "last_user": user_name,
        "last_trace_id": trace_id,
        # ---- v2 工单字段（缺省值保证兼容旧数据）----
        "feedbacks": [],       # [{id, user_name, reasons, note, created_at}]
        "status": STATUS_PENDING,
        "assignee": "",
        "resolution": None,    # {docs, note, operator, resolved_at}
        "notifications": [],   # [{id, user_name, status, message, created_at, read}]
        "updated_at": now,
    }


def _normalize(rec: dict) -> dict:
    """补齐旧持久化数据缺失的 v2 字段，避免 dict.get 散落各处。"""
    if not isinstance(rec, dict):
        return {}
    rec.setdefault("feedbacks", [])
    rec.setdefault("status", STATUS_PENDING)
    rec.setdefault("assignee", "")
    rec.setdefault("resolution", None)
    rec.setdefault("notifications", [])
    rec.setdefault("count", 0)
    rec.setdefault("updated_at", rec.get("last_asked_at") or rec.get("first_asked_at") or 0.0)
    if rec.get("status") not in STATUS_LABELS:
        rec["status"] = STATUS_PENDING
    return rec


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        if _GAP_PATH.exists():
            raw = json.loads(_GAP_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                # v1 主键里带了 reason，同一问题在两种原因下会分成两条；
                # v2 主键与原因无关，加载时按新主键归并，历史数据不丢。
                for key, val in raw.items():
                    val = _normalize(val)
                    new_key = _key(val.get("department_id", ""), val.get("question", ""))
                    exist = _GAPS.get(new_key)
                    if exist is None:
                        _GAPS[new_key] = val
                    else:
                        exist["count"] += int(val.get("count", 0))
                        exist["feedbacks"].extend(val.get("feedbacks") or [])
                        exist["notifications"].extend(val.get("notifications") or [])
                        # 保留更晚的提问时间与更早的首次提问时间
                        exist["last_asked_at"] = max(exist.get("last_asked_at", 0.0),
                                                     val.get("last_asked_at", 0.0))
                        exist["first_asked_at"] = min(exist.get("first_asked_at", time.time()),
                                                      val.get("first_asked_at", time.time()))
                        if not exist.get("reason") and val.get("reason"):
                            exist["reason"] = val["reason"]
                log.info("gap.loaded", items=len(_GAPS), migrated=len(raw) - len(_GAPS))
    except Exception as e:  # noqa: BLE001
        log.warning("gap.load_failed", error=str(e))


def _save() -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _GAP_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_GAPS, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _GAP_PATH)
    except Exception as e:  # noqa: BLE001
        log.warning("gap.save_failed", error=str(e))


def _q_hash(question: str) -> str:
    return hashlib.md5(question.strip().encode("utf-8")).hexdigest()[:12]


def _key(department_id: str, question: str) -> str:
    return f"{department_id}:{_q_hash(question)}"


def _trim() -> None:
    """容量治理：超出上限时淘汰最久没被问到、也没人提过反馈的冷数据。"""
    if len(_GAPS) <= _MAX_ITEMS:
        return
    def cold_score(r: dict) -> tuple:
        # 优先淘汰：0 反馈 + 最早被问的时间
        return (len(r.get("feedbacks") or []), r.get("last_asked_at", 0.0))
    cold = sorted(_GAPS.items(), key=lambda kv: cold_score(kv[1]))[:len(_GAPS) - _MAX_ITEMS]
    for k, _ in cold:
        _GAPS.pop(k, None)


# ------------------------------------------------------------
#  系统自动沉淀（保持 v1 签名）
# ------------------------------------------------------------
def record_gap(question: str, department_id: str, *, reason: str = "no_hit",
               user_name: str = "", trace_id: str = "") -> dict:
    """
    记录一条未被回答的问题。同一部门 + 同一问题归并计数，不重复堆积工单。
    reason ∈ {"no_hit", "low_evidence"}
    已经是"已补充"状态的工单若再次未被回答，说明补充的资料仍然不够 → 自动重开为待处理。
    """
    q = (question or "").strip()
    if not q:
        return {}
    with _LOCK:
        _load()
        key = _key(department_id, q)
        rec = _GAPS.get(key)
        if rec is None:
            rec = _new_ticket(q, department_id, reason=reason, user_name=user_name,
                              trace_id=trace_id)
            _GAPS[key] = rec
        rec["count"] += 1
        rec["last_asked_at"] = round(time.time(), 3)
        rec["last_user"] = user_name
        rec["last_trace_id"] = trace_id
        rec["updated_at"] = rec["last_asked_at"]
        if rec.get("status") == STATUS_SUPPLEMENTED:
            # 闭环兜底：资料标了"已补充"但用户又问到同样的问题且没答上来 → 自动重开。
            rec["status"] = STATUS_PENDING
            rec["resolution"] = None
            log.info("gap.reopened", key=key, question=q[:40])
        _trim()
        _save()
        return _export(key, rec)


# ------------------------------------------------------------
#  用户主动反馈（闭环入口）
# ------------------------------------------------------------
def submit_feedback(question: str, department_id: str, reasons: list[str], note: str = "",
                    *, user_name: str = "", user_id: str = "", trace_id: str = "") -> dict:
    """
    用户对本次回答提交"没解决"反馈 → 生成/归并到工单，状态置为待处理。

    返回工单 dict；调用方负责写审计。同一工单允许多人反馈（feedbacks 是列表），
    每次反馈都会把工单重新激活为待处理——哪怕它已经被关闭或标记补充过。
    """
    q = (question or "").strip()
    if not q:
        return {}
    reasons = [r for r in (reasons or []) if r in FEEDBACK_REASONS]
    note = (note or "").strip()[:1000]

    with _LOCK:
        _load()
        key = _key(department_id, q)
        rec = _GAPS.get(key)
        if rec is None:
            rec = _new_ticket(q, department_id, user_name=user_name, trace_id=trace_id)
            rec["count"] = 1
            rec["first_asked_at"] = round(time.time(), 3)
            _GAPS[key] = rec
        rec["feedbacks"].append({
            "id": f"fb{int(time.time() * 1000)}",
            "user_id": user_id,
            "user_name": user_name,
            "reasons": reasons,
            "note": note,
            "created_at": round(time.time(), 3),
        })
        rec["status"] = STATUS_PENDING
        rec["resolution"] = None
        rec["updated_at"] = round(time.time(), 3)
        rec["last_user"] = user_name or rec.get("last_user", "")
        _trim()
        _save()
        log.info("gap.feedback", key=key, reasons=reasons, user=user_name)
        return _export(key, rec)


# ------------------------------------------------------------
#  管理员处置
# ------------------------------------------------------------
def _export(gap_id: str, rec: dict) -> dict:
    """对外导出的工单快照：补上稳定的 id，避免调用方自己拼主键。"""
    return dict(rec, id=gap_id)


def get_gap(gap_id: str) -> dict | None:
    with _LOCK:
        _load()
        rec = _GAPS.get(gap_id)
        return _export(gap_id, rec) if rec else None


def assign(gap_id: str, assignee: str, operator: str = "") -> dict:
    """指派给部门负责人（或知识管理员）。"""
    with _LOCK:
        _load()
        rec = _GAPS.get(gap_id)
        if rec is None:
            return {}
        rec["assignee"] = assignee
        rec["updated_at"] = round(time.time(), 3)
        _save()
        log.info("gap.assigned", key=gap_id, assignee=assignee, by=operator)
        return _export(gap_id, rec)


def resolve(gap_id: str, docs: list[str] | None = None, note: str = "",
            operator: str = "") -> dict:
    """
    标记"已补充"：资料已上架。此时给所有反馈过的人 + 最近提问人生成通知，
    让闭环回到用户这一端——他才知道自己报的缺口已经被补上了。
    """
    docs = [str(d).strip() for d in (docs or []) if str(d).strip()]
    note = (note or "").strip()[:1000]
    with _LOCK:
        _load()
        rec = _GAPS.get(gap_id)
        if rec is None:
            return {}
        now = round(time.time(), 3)
        rec["status"] = STATUS_SUPPLEMENTED
        rec["resolution"] = {"docs": docs, "note": note, "operator": operator,
                             "resolved_at": now}
        rec["updated_at"] = now

        # 通知对象：所有提过反馈的人 + 最近一次提问的人（去重）
        targets: list[str] = []
        for fb in rec.get("feedbacks") or []:
            name = fb.get("user_name") or ""
            if name and name not in targets:
                targets.append(name)
        last = rec.get("last_user") or ""
        if last and last not in targets:
            targets.append(last)

        doc_hint = f"（已补充文档：{('、'.join(docs))[:80]}）" if docs else ""
        for name in targets:
            rec["notifications"].append({
                "id": f"nt{int(now * 1000)}-{len(rec['notifications'])}",
                "user_name": name,
                "gap_id": gap_id,
                "question": rec.get("question", ""),
                "status": STATUS_SUPPLEMENTED,
                "message": f"你反馈的知识缺口「{rec.get('question', '')[:30]}」已补充{doc_hint}，再问一次试试～",
                "created_at": now,
                "read": False,
            })
        _save()
        log.info("gap.resolved", key=gap_id, operator=operator, notified=len(targets))
        return _export(gap_id, rec)


def reopen(gap_id: str, operator: str = "") -> dict:
    """管理员手动把已处理的工单拉回待处理（补充不到位 / 结论有争议）。"""
    with _LOCK:
        _load()
        rec = _GAPS.get(gap_id)
        if rec is None:
            return {}
        rec["status"] = STATUS_PENDING
        rec["resolution"] = None
        rec["updated_at"] = round(time.time(), 3)
        _save()
        log.info("gap.reopen_manual", key=gap_id, operator=operator)
        return _export(gap_id, rec)


def close(gap_id: str, note: str = "", operator: str = "") -> dict:
    """关闭工单（不补充：不在知识库范围内 / 重复问题 / 已线下解答）。"""
    with _LOCK:
        _load()
        rec = _GAPS.get(gap_id)
        if rec is None:
            return {}
        now = round(time.time(), 3)
        rec["status"] = STATUS_CLOSED
        rec["resolution"] = {"docs": [], "note": (note or "").strip()[:1000],
                             "operator": operator, "resolved_at": now}
        rec["updated_at"] = now
        _save()
        log.info("gap.closed", key=gap_id, operator=operator)
        return _export(gap_id, rec)


# ------------------------------------------------------------
#  查询
# ------------------------------------------------------------
def list_gaps(top: int = 20, department_id: str | None = None,
              reason: str | None = None, status: str | None = None) -> list[dict]:
    """台账列表。status 为空表示全部；排序：待处理优先 → 提问次数 → 最近提问时间。"""
    with _LOCK:
        _load()
        items = list(_GAPS.values())
        if department_id:
            items = [i for i in items if i.get("department_id") == department_id]
        if reason:
            items = [i for i in items if i.get("reason") == reason]
        if status:
            items = [i for i in items if i.get("status") == status]
        items.sort(key=lambda x: (
            STATUS_ORDER.index(x.get("status", STATUS_PENDING)),
            -len(x.get("feedbacks") or []),
            -x.get("count", 0),
            -x.get("last_asked_at", 0.0),
        ))
        out = []
        for key, val in [(_key(i["department_id"], i["question"]), i) for i in items]:
            item = dict(val)
            item["id"] = key
            out.append(item)
        return out[:top]


def my_feedbacks(user_name: str, limit: int = 20) -> list[dict]:
    """我提交过的缺口反馈及其处理进度（用户侧"我的反馈"）。"""
    with _LOCK:
        _load()
        out = []
        for key, val in _GAPS.items():
            mine = [f for f in (val.get("feedbacks") or []) if f.get("user_name") == user_name]
            if not mine:
                continue
            item = dict(val)
            item["id"] = key
            item["my_feedbacks"] = mine
            out.append(item)
        out.sort(key=lambda x: -max((f.get("created_at") or 0) for f in x["my_feedbacks"]))
        return out[:limit]


def pull_notifications(user_name: str, *, unread_only: bool = True) -> list[dict]:
    """用户拉取自己的通知（补全完成后由 resolve 生成）。"""
    with _LOCK:
        _load()
        items = []
        for val in _GAPS.values():
            for n in (val.get("notifications") or []):
                if n.get("user_name") != user_name:
                    continue
                if unread_only and n.get("read"):
                    continue
                items.append(dict(n))
        items.sort(key=lambda x: -x.get("created_at", 0.0))
        return items


def mark_notification_read(notification_id: str, user_name: str) -> bool:
    with _LOCK:
        _load()
        for val in _GAPS.values():
            for n in (val.get("notifications") or []):
                if n.get("id") == notification_id and n.get("user_name") == user_name:
                    n["read"] = True
                    _save()
                    return True
        return False


def gap_summary() -> dict:
    with _LOCK:
        _load()
        total_unique = len(_GAPS)
        fb_total = sum(len(i.get("feedbacks") or []) for i in _GAPS.values())
        return {
            "total_unique": total_unique,
            "total_asks": sum(i.get("count", 0) for i in _GAPS.values()),
            "no_hit": sum(1 for i in _GAPS.values() if i.get("reason") == "no_hit"),
            "low_evidence": sum(1 for i in _GAPS.values() if i.get("reason") == "low_evidence"),
            # ---- v2 工单维度 ----
            "feedback_total": fb_total,
            "pending": sum(1 for i in _GAPS.values() if i.get("status") == STATUS_PENDING),
            "supplemented": sum(1 for i in _GAPS.values() if i.get("status") == STATUS_SUPPLEMENTED),
            "closed": sum(1 for i in _GAPS.values() if i.get("status") == STATUS_CLOSED),
        }
