"""
引导式请假流程引擎（不依赖 LLM，纯规则驱动，保证稳定可复现）。

交互模型（与需求一致）：
  员工在聊天里输入「我想请假」→ 系统按【钉钉请假模板字段】逐个追问
  （请假类型 / 开始时间 / 结束时间 / 请假事由 / 工作交接人）→ 汇总请申请人确认
  → 确认后调用钉钉适配器创建审批实例，并按部门路由推送给审批负责人。

状态机：
  ask_type → ask_start → ask_end → ask_reason → ask_handover → confirm
  confirm 下：确认→提交 / 修改→回到 ask_type / 取消→删除草稿

所有进度存于 leave_store 的草稿（按 user_id 隔离）；提交后落 request。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from ..db.models import USERS, dept_name
from ..intent.chitchat import chitchat_reply, is_chitchat
from ..logging_setup import get_logger
from . import dingtalk_client, leave_store, routing

log = get_logger("leave.flow")

STEPS = ["ask_type", "ask_start", "ask_end", "ask_reason", "ask_handover"]

LEAVE_TYPES = {
    "事假": "事假", "私事": "事假", "家里有事": "事假", "个人事务": "事假",
    "病假": "病假", "生病": "病假", "就医": "病假", "看病": "病假",
    "年假": "年假", "年休": "年假", "年休假": "年假",
    "调休": "调休", "调班": "调休",
    "婚假": "婚假", "结婚": "婚假",
    "产假": "产假",
    "丧假": "丧假", "奔丧": "丧假", "白事": "丧假",
    "陪产假": "陪产假", "陪护": "陪产假",
    "工伤假": "工伤假", "工伤": "工伤假",
    "探亲假": "探亲假", "探亲": "探亲假",
}

TRIGGER_RE = re.compile(
    r"(请假|休假|请个假|我想请|帮我请|申请假期|请假申请|leave|leave of absence)", re.I
)
# 「申请年假」算发起请求；但「年假怎么算」是知识问答，不能误触发 → 带疑问词则不触发
QUESTION_WORDS = ("怎么", "如何", "什么", "流程", "规定", "制度", "标准", "多少",
                  "能否", "可以吗", "吗？", "？", "?", "政策", "几天的")

CONFIRM_WORDS = ("确认", "提交", "确定", "好的", "OK", "ok", "yes", "是的", "通过", "没问题")
EDIT_WORDS = ("修改", "改一下", "编辑", "重填", "重新填", "调整")
CANCEL_WORDS = ("取消", "退出", "算了", "不请了", "放弃")

# 进度/状态查询：优先于请假触发，避免"请假进度怎么样"误开新流程
PROGRESS_QUERY_RE = re.compile(
    r"(请假.*?(进度|状态|情况|到哪儿?了|到哪了|怎么样了|如何了|查一下|看看)|"
    r"^(进度|状态|情况|到哪儿?了|到哪了|怎么样了|如何了|查一下|看看).*?请假|"
    r"我的请假|请假申请|请假单).*?", re.I
)


# ============================================================
#  触发检测
# ============================================================
def is_progress_query(text: str) -> bool:
    """判断用户是否在问已有请假进度，优先级最高。"""
    return bool(PROGRESS_QUERY_RE.search(text or ""))


def is_trigger(text: str) -> bool:
    t = text or ""
    # 先排除进度查询，避免"请假进度怎么样"误开新流程
    if is_progress_query(t):
        return False
    if TRIGGER_RE.search(t):
        return True
    # 「申请X假」形态：仅在无疑问词时视为发起请求，避免与知识问答冲突
    if re.search(r"申请.{0,4}(年假|病假|事假|调休|婚假|产假|丧假|陪产假|工伤假|探亲假)", t):
        return not any(q in t for q in QUESTION_WORDS)
    return False


# ============================================================
#  日期时间解析（支持「9月24日 9点」「明天 14:00」等中文表达）
# ============================================================
_WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def _parse_time(text: str, default_hour: int) -> int:
    """从文本里抠出小时分钟，找不到用默认值。"""
    m = re.search(r"(\d{1,2})\s*[:：]\s*(\d{1,2})", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d{1,2})\s*点", text)
    if m:
        h = int(m.group(1))
        pm = "下午" in text or "晚上" in text or "pm" in text.lower()
        if pm and h < 12:
            h += 12
        return h, 0
    pm = "下午" in text or "晚上" in text
    return (default_hour + 12 if pm and default_hour < 12 else default_hour), 0


def _base_date(text: str) -> date | None:
    """解析日期部分；返回 date 或 None（让调用方决定默认）。"""
    today = date.today()
    if "后天" in text:
        return today + timedelta(days=2)
    if "大后天" in text:
        return today + timedelta(days=3)
    if "明天" in text or "明日" in text:
        return today + timedelta(days=1)
    if "今天" in text or "今日" in text:
        return today
    m = re.search(r"下周(一|二|三|四|五|六|日|天)", text)
    if m:
        target = _WEEKDAYS[m.group(1)]
        days_ahead = (target - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        return today + timedelta(days=days_ahead)
    m = re.search(r"(?:(\d{4})[-/年.\s])?(\d{1,2})[-/月.\s](\d{1,2})", text)
    if m:
        y = int(m.group(1)) if m.group(1) else today.year
        mo = int(m.group(2))
        d = int(m.group(3))
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    return None


def parse_datetime(text: str, default_hour: int = 9) -> datetime | None:
    """解析「日期 + 时间」表达式，失败返回 None。"""
    text = (text or "").strip()
    if not text:
        return None
    d = _base_date(text)
    if d is None:
        # 没有可识别的日期词，放弃
        return None
    h, mi = _parse_time(text, default_hour)
    try:
        return datetime(d.year, d.month, d.day, h, mi)
    except ValueError:
        return None


# ============================================================
#  进度查询
# ============================================================
def query_progress(user) -> str | None:
    """返回当前用户最新请假申请的状态文本；无记录返回 None。"""
    reqs = leave_store.list_requests_by_applicant(user.user_id)
    if not reqs:
        return "您最近还没有提交过请假申请哦。想请假的话，直接说「我想请假」就行～"
    # 取最新一条
    latest = max(reqs, key=lambda r: r.get("submitted_at", 0) or r.get("created_at", 0))
    status = latest.get("status", "pending")
    lt = latest.get("leave_type", "—")
    days = latest.get("duration_days", 0)
    st = latest.get("start_time", "")[:16]
    et = latest.get("end_time", "")[:16]

    if status == "approved":
        return (
            f"🎉 您的 **{lt}** 申请已**全部审批通过**，请假生效！\n\n"
            f"· 时间：{st} ~ {et}，共 {days} 天\n"
            f"· 审批人：{' → '.join(c['approver_name'] for c in latest.get('chain', []))}"
        )
    if status == "rejected":
        # 找最后一个被驳回的步骤
        rejecter = next((c for c in reversed(latest.get("chain", [])) if c.get("status") == "rejected"), {})
        return (
            f"您的 **{lt}** 申请已被 **{rejecter.get('approver_name','审批人')}** 驳回。\n\n"
            f"· 时间：{st} ~ {et}，共 {days} 天\n"
            f"· 驳回意见：{rejecter.get('comment') or '无'}\n\n"
            "如需重新请假，可以说「我想请假」；如有疑问也可以问我的工作问题哦。"
        )

    # pending：找到当前待审批人
    chain = latest.get("chain", [])
    cur = next((c for c in chain if c.get("status") == "pending"), None)
    done = [c for c in chain if c.get("status") == "approved"]
    progress = f"{len(done)}/{len(chain)}"
    if cur:
        return (
            f"您的 **{lt}** 申请正在审批中，当前进度 **{progress}**。\n\n"
            f"· 时间：{st} ~ {et}，共 {days} 天\n"
            f"· 当前等待：**{cur['approver_name']}** 审批\n"
            f"· 完整流程：{' → '.join(c['approver_name'] for c in chain)}\n\n"
            "您可以在右上角「请假 / 审批」里查看详情；也可以照常问我工作问题。"
        )
    return (
        f"您的 **{lt}** 申请状态：审批中。\n"
        f"· 时间：{st} ~ {et}，共 {days} 天\n"
        "稍等片刻，审批人处理后会第一时间通知您。"
    )


# ============================================================
#  流程驱动
# ============================================================
def _new_draft(user) -> dict:
    return {
        "state": "ask_type",
        "step": "ask_type",
        "answers": {},
        "applicant_id": user.user_id,
        "applicant_name": user.display_name,
        "dept_id": user.dept_id,
        "dept_name": dept_name(user.dept_id),
        "created_at": leave_store._now(),
        "updated_at": leave_store._now(),
    }


def start(user) -> tuple[dict, str]:
    draft = _new_draft(user)
    leave_store.save_draft(user.user_id, draft)
    text = (
        "好的，我来帮您发起请假申请 📝\n\n"
        "我会依次询问请假类型、开始时间、结束时间、请假事由和工作交接人，"
        "确认后会自动提交给您的部门负责人审批。\n"
        "我们一步一步来，请直接回复即可。\n\n"
        "**① 请假类型**——请回复类型名称：\n"
        "事假 / 病假 / 年假 / 调休 / 婚假 / 产假 / 丧假 / 陪产假 / 工伤假 / 探亲假"
    )
    return draft, text


def _ask_text(step: str) -> str:
    if step == "ask_type":
        return ("**① 请假类型**——请回复类型名称：\n"
                "事假 / 病假 / 年假 / 调休 / 婚假 / 产假 / 丧假 / 陪产假 / 工伤假 / 探亲假")
    if step == "ask_start":
        return ("**② 开始时间**——例如：\n"
                "`2026-09-24 09:00` 或 `9月24日上午9点` 或 `明天 9:00`")
    if step == "ask_end":
        return ("**③ 结束时间**——例如：\n"
                "`2026-09-25 18:00` 或 `9月25日下午6点`")
    if step == "ask_reason":
        return "**④ 请假事由**——例如：家中有事需处理 / 感冒就医。"
    if step == "ask_handover":
        return ("**⑤ 工作交接人**（选填）——填写交接同事姓名；"
                "若无交接请回复「无」。")
    return ""


def _parse_type(text: str) -> str | None:
    for key, val in LEAVE_TYPES.items():
        if key in text:
            return val
    return None


def _advance(draft: dict) -> str:
    """推进到下一个待问步骤；全部问完进入 confirm，返回下一步要说的文本。"""
    idx = STEPS.index(draft["step"]) if draft["step"] in STEPS else -1
    if idx + 1 < len(STEPS):
        nxt = STEPS[idx + 1]
        draft["step"] = nxt
        draft["state"] = nxt
        return _ask_text(nxt)
    # 全部收集完成 → 汇总确认
    draft["step"] = "confirm"
    draft["state"] = "confirm"
    return _summary(draft)


def _summary(draft: dict) -> str:
    a = draft["answers"]
    st = a.get("start_time")
    et = a.get("end_time")
    st_dt = datetime.fromisoformat(st) if isinstance(st, str) else st
    et_dt = datetime.fromisoformat(et) if isinstance(et, str) else et
    s_str = st_dt.strftime("%Y-%m-%d %H:%M") if isinstance(st_dt, datetime) else "—"
    e_str = et_dt.strftime("%Y-%m-%d %H:%M") if isinstance(et_dt, datetime) else "—"
    dur = a.get("duration_days", 0)
    return (
        "✅ 信息已采集齐全，请核对以下**请假申请**：\n\n"
        f"- 申请人：{draft['applicant_name']}（{draft['dept_name']}）\n"
        f"- 请假类型：{a.get('leave_type','—')}\n"
        f"- 开始时间：{s_str}\n"
        f"- 结束时间：{e_str}\n"
        f"- 请假时长：{dur} 天\n"
        f"- 请假事由：{a.get('reason','—')}\n"
        f"- 工作交接人：{a.get('handover') or '无'}\n\n"
        "请回复 **确认** 提交审批，或 **修改** 重新填写，或 **取消** 放弃。"
    )


def handle(user, message: str, draft: dict) -> tuple[str, dict | None]:
    """
    处理一条用户消息（在草稿进行中）。返回 (回复文本, 提交后的 request 或 None)。
    """
    text = (message or "").strip()

    # 即使在填表过程中，用户也可能问"进度怎么样"——优先查进度，不打断当前草稿
    if is_progress_query(text):
        return query_progress(user), None

    # 纯客套话（"好哒，谢谢""嗯嗯"）≠ 字段答案：温和回应后重复当前问题，草稿原样保留。
    # 注意只在采集阶段拦截——confirm 阶段的"好的"是确认语，走原有确认逻辑。
    if draft["state"] in STEPS and is_chitchat(text):
        return f"{chitchat_reply(text)}\n\n我们继续：{_ask_text(draft['state'])}", None

    # 取消：任何阶段都可放弃（在解析答案之前，避免被当成字段值）
    if any(w in text for w in CANCEL_WORDS):
        leave_store.delete_draft(user.user_id)
        return "已取消本次请假申请，草稿已清除。如需请假随时告诉我～", None

    # 用户在采集阶段又敲了触发词 → 重复当前问题，不当作答案
    if draft["state"] in STEPS and is_trigger(text) and len(text) <= 8:
        return _ask_text(draft["state"]), None

    if draft["state"] == "confirm":
        return _handle_confirm(user, text, draft)

    # ---- 采集阶段：解析当前步骤的答案 ----
    step = draft["step"]
    if step == "ask_type":
        lt = _parse_type(text)
        if not lt:
            return ("没太理解请假类型，请直接回复以下之一：\n"
                    "事假 / 病假 / 年假 / 调休 / 婚假 / 产假 / 丧假 / 陪产假 / 工伤假 / 探亲假", None)
        draft["answers"]["leave_type"] = lt
        leave_store.save_draft(user.user_id, draft)
        return _advance(draft), None

    if step == "ask_start":
        dt = parse_datetime(text, default_hour=9)
        if dt is None:
            return ("开始时间没解析成功，请按格式填写，例如：`2026-09-24 09:00` 或 `明天 9:00`", None)
        # 以 ISO 字符串落库，保证 JSON 可序列化
        draft["answers"]["start_time"] = dt.isoformat()
        leave_store.save_draft(user.user_id, draft)
        return _advance(draft), None

    if step == "ask_end":
        dt = parse_datetime(text, default_hour=18)
        if dt is None:
            return ("结束时间没解析成功，请按格式填写，例如：`2026-09-25 18:00` 或 `后天 18:00`", None)
        st = draft["answers"].get("start_time")
        st_dt = datetime.fromisoformat(st) if isinstance(st, str) else st
        if isinstance(st_dt, datetime) and dt <= st_dt:
            return ("结束时间需要晚于开始时间哦，请重新填写结束时间。", None)
        draft["answers"]["end_time"] = dt.isoformat()
        # 计算时长
        if isinstance(st_dt, datetime):
            days = round((dt - st_dt).total_seconds() / 86400, 2)
            draft["answers"]["duration_days"] = days
        leave_store.save_draft(user.user_id, draft)
        return _advance(draft), None

    if step == "ask_reason":
        if not text:
            return ("请假事由不能为空，请简要说明，例如：家中有事。", None)
        draft["answers"]["reason"] = text
        leave_store.save_draft(user.user_id, draft)
        return _advance(draft), None

    if step == "ask_handover":
        if text in ("无", "没有", "没", "空", "否", ""):
            draft["answers"]["handover"] = None
        else:
            draft["answers"]["handover"] = text
        leave_store.save_draft(user.user_id, draft)
        return _advance(draft), None

    return "好的，我们继续。", None


def _handle_confirm(user, text: str, draft: dict) -> tuple[str, dict | None]:
    if any(w in text for w in CANCEL_WORDS):
        leave_store.delete_draft(user.user_id)
        return "已取消本次请假申请，草稿已清除。如需请假随时告诉我～", None
    if any(w in text for w in EDIT_WORDS):
        draft["answers"] = {}
        draft["step"] = "ask_type"
        draft["state"] = "ask_type"
        leave_store.save_draft(user.user_id, draft)
        return ("好的，我们重新填写。\n\n**① 请假类型**——请回复类型名称：\n"
                "事假 / 病假 / 年假 / 调休 / 婚假 / 产假 / 丧假 / 陪产假 / 工伤假 / 探亲假", None)
    if any(w in text for w in CONFIRM_WORDS):
        return _submit(user, draft)
    return ("请回复 **确认** 提交审批，或 **修改** 重新填写，或 **取消** 放弃。", None)


def _approver_login_hint(chain: list[dict]) -> str:
    """生成"请用 xxx 账号登录审批"的友好提示，避免用户看不懂 user_id。"""
    hints = []
    for c in chain:
        uid = c["approver_id"]
        name = c["approver_name"]
        login_name = ""
        for uname, u in USERS.items():
            if u.user_id == uid:
                login_name = uname
                break
        if login_name:
            hints.append(f"· 第 {c['step']} 步审批人 **{name}**，请切换登录账号 `{login_name}` 后点击右上角「请假 / 审批」处理")
        else:
            hints.append(f"· 第 {c['step']} 步审批人 {name}")
    return "**如何审批**\n" + "\n".join(hints)


def _submit(user, draft: dict) -> tuple[str, dict]:
    a = draft["answers"]
    st = datetime.fromisoformat(a["start_time"])
    et = datetime.fromisoformat(a["end_time"])
    days = a.get("duration_days", round((et - st).total_seconds() / 86400, 2))
    dept_id = draft["dept_id"]

    chain = routing.build_chain(dept_id, a["leave_type"], days)
    approver_ids = [c["approver_id"] for c in chain]

    payload = {
        "leave_type": a["leave_type"],
        "start_time": st,
        "end_time": et,
        "duration_days": days,
        "reason": a.get("reason", ""),
        "handover": a.get("handover"),
        "dept_id": dept_id,
        "approver_ids": approver_ids,
        "originator_ding_id": user.user_id,
    }
    # 调用钉钉请假接口（mock 模式返回 DINGMOCK-xxxx）
    instance_id = dingtalk_client.create_leave_instance(payload)

    req = {
        "id": leave_store.gen_request_id(),
        "applicant_id": user.user_id,
        "applicant_name": user.display_name,
        "dept_id": dept_id,
        "dept_name": draft["dept_name"],
        "leave_type": a["leave_type"],
        "start_time": st.isoformat(),
        "end_time": et.isoformat(),
        "duration_days": days,
        "reason": a.get("reason", ""),
        "handover": a.get("handover"),
        "status": "pending",
        "dingtalk_instance_id": instance_id,
        "chain": chain,
        "created_at": leave_store._now(),
        "submitted_at": leave_store._now(),
        "completed_at": None,
        "notified": False,
    }
    leave_store.create_request(req)
    leave_store.delete_draft(user.user_id)

    first = chain[0]
    steps_desc = " → ".join(f"{c['step']}.{c['approver_name']}" for c in chain)
    # 提示用户用哪个登录账号去审批（把 user_id 映射回登录名，方便切换）
    approver_login_hint = _approver_login_hint(chain)
    text = (
        f"🎉 请假申请已生成并提交至钉钉（实例号 `{instance_id}`）。\n\n"
        f"**审批流程**：{steps_desc}\n"
        f"当前进度：**第 1 步 · {first['approver_name']} 待审批**\n\n"
        f"{approver_login_hint}\n\n"
        "审批期间您可以照常向我提问工作问题，不受影响。\n"
        "审批进度可在右上角「请假 / 审批」中查看；全部通过后会有提示，请假即生效。"
    )
    log.info("leave.submitted", rid=req["id"], applicant=user.user_id,
             instance=instance_id, days=days)
    from ..core import audit
    audit.record("leave_submit", user_id=user.user_id, user_name=user.display_name,
                 detail=f"{a['leave_type']} {days} 天", rid=req["id"], instance_id=instance_id)
    return text, req
