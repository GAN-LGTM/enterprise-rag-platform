"""
引导式报销流程引擎（不依赖 LLM，纯规则驱动，保证稳定可复现）。

交互模型（与需求一致）：
  员工在聊天里输入「我想报销」→ 系统按【钉钉报销模板字段】逐个追问
  （报销类型 / 报销金额 / 费用发生日期 / 费用说明 / 收款人）→ 汇总请申请人确认
  → 确认后调用钉钉适配器创建审批实例，并按部门 + 财务复核路由推送给审批负责人。

状态机：
  ask_type → ask_amount → ask_date → ask_reason → ask_payee → confirm
  confirm 下：确认→提交 / 修改→回到 ask_type / 取消→删除草稿

所有进度存于 reimburse_store 的草稿（按 user_id 隔离）；提交后落 request。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from ..db.models import USERS, dept_name
from ..intent.chitchat import chitchat_reply, is_chitchat
from ..logging_setup import get_logger
from . import dingtalk_client, reimburse_store, routing

log = get_logger("reimburse.flow")

STEPS = ["ask_type", "ask_amount", "ask_date", "ask_reason", "ask_payee"]

RB_TYPES = {
    "差旅费": "差旅费", "差旅": "差旅费", "出差": "差旅费", "差旅报销": "差旅费",
    "办公用品": "办公用品", "办公费": "办公用品", "办公": "办公用品",
    "业务招待费": "业务招待费", "招待费": "业务招待费", "招待": "业务招待费",
    "交通费": "交通费", "打车": "交通费", "车费": "交通费",
    "通讯费": "通讯费", "话费": "通讯费", "电话费": "通讯费",
    "培训费": "培训费", "培训": "培训费",
    "其他": "其他", "别的": "其他", "杂项": "其他",
}

TRIGGER_RE = re.compile(
    r"(我想报销|我要报销|帮我报销|给我报销|申请报销|报销申请|报销单|报销一下|提交报销|发起报销|报销流程申请|reimburse)", re.I
)
# 「报销标准」是知识问答，不能误触发 → 带疑问词则不触发
QUESTION_WORDS = ("怎么", "如何", "什么", "流程", "规定", "制度", "标准", "多少",
                  "能否", "可以吗", "吗？", "？", "?", "政策", "额度", "上限", "政策")

CONFIRM_WORDS = ("确认", "提交", "确定", "好的", "OK", "ok", "yes", "是的", "通过", "没问题")
EDIT_WORDS = ("修改", "改一下", "编辑", "重填", "重新填", "调整")
CANCEL_WORDS = ("取消", "退出", "算了", "不报了", "放弃")

# 进度/状态查询：优先于报销触发，避免"报销进度怎么样"误开新流程
PROGRESS_QUERY_RE = re.compile(
    r"(报销.{0,6}(进度|状态|情况|到哪儿了|到哪了|怎么样了|如何了|查一下|看看|批了|通过了|完成没|好了没))"
    r"|^(进度|状态|情况|到哪儿了|到哪了|怎么样了|如何了|查一下|看看).{0,6}报销"
    r"|我的报销", re.I
)


# ============================================================
#  触发检测
# ============================================================
def is_progress_query(text: str) -> bool:
    """判断用户是否在问已有报销进度，优先级最高。"""
    return bool(PROGRESS_QUERY_RE.search(text or ""))


def is_trigger(text: str) -> bool:
    t = text or ""
    # 先排除进度查询，避免"报销进度怎么样"误开新流程
    if is_progress_query(t):
        return False
    if TRIGGER_RE.search(t):
        return True
    # 「申请报销」形态：仅在无疑问词时视为发起请求，避免与知识问答冲突
    if re.search(r"申请.{0,4}(报销|费用报销)", t):
        return not any(q in t for q in QUESTION_WORDS)
    return False


# ============================================================
#  金额 / 日期解析（支持「800」「1.2万」「明天」等中文表达）
# ============================================================
def parse_amount(text: str) -> float | None:
    """从文本解析报销金额（元）。支持「万 / 千」单位；失败返回 None。"""
    t = (text or "").strip()
    if not t:
        return None
    m = re.search(r"([\d]+(?:\.[\d]+)?)\s*万", t)
    if m:
        return round(float(m.group(1)) * 10000, 2)
    m = re.search(r"([\d]+(?:\.[\d]+)?)\s*千", t)
    if m:
        return round(float(m.group(1)) * 1000, 2)
    m = re.search(r"([\d]+(?:\.[\d]+)?)", t)
    if m:
        return round(float(m.group(1)), 2)
    return None


_WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def parse_expense_date(text: str) -> date | None:
    """解析「费用发生日期」：支持 明天 / 9月24日 / 2026-09-24 等；失败返回 None。"""
    if not text or not text.strip():
        return None
    t = text.strip()
    today = date.today()
    if "后天" in t:
        return today + timedelta(days=2)
    if "大后天" in t:
        return today + timedelta(days=3)
    if "明天" in t or "明日" in t:
        return today + timedelta(days=1)
    if "今天" in t or "今日" in t:
        return today
    if "昨天" in t or "昨日" in t:
        return today - timedelta(days=1)
    m = re.search(r"下周(一|二|三|四|五|六|日|天)", t)
    if m:
        target = _WEEKDAYS[m.group(1)]
        days_ahead = (target - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        return today + timedelta(days=days_ahead)
    m = re.search(r"(?:(\d{4})[-/年.\s])?(\d{1,2})[-/月.\s](\d{1,2})", t)
    if m:
        y = int(m.group(1)) if m.group(1) else today.year
        mo = int(m.group(2))
        d = int(m.group(3))
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    return None


# ============================================================
#  进度查询
# ============================================================
def query_progress(user) -> str | None:
    """返回当前用户最新报销申请的状态文本；无记录返回友好提示。"""
    reqs = reimburse_store.list_requests_by_applicant(user.user_id)
    if not reqs:
        return "您最近还没有提交过报销申请哦。要报销的话，直接说「我想报销」就行～"
    # 取最新一条
    latest = max(reqs, key=lambda r: r.get("submitted_at", 0) or r.get("created_at", 0))
    status = latest.get("status", "pending")
    rb = latest.get("rb_type", "—")
    amount = latest.get("amount", 0)
    ed = (latest.get("expense_date", "") or "")[:10]

    if status == "approved":
        return (
            f"🎉 您的 **{rb}** 报销（¥{amount}）已**全部审批通过**，报销生效！\n\n"
            f"· 费用发生日期：{ed}\n"
            f"· 审批人：{' → '.join(c['approver_name'] for c in latest.get('chain', []))}"
        )
    if status == "rejected":
        rejecter = next((c for c in reversed(latest.get("chain", [])) if c.get("status") == "rejected"), {})
        return (
            f"您的 **{rb}** 报销（¥{amount}）已被 **{rejecter.get('approver_name','审批人')}** 驳回。\n\n"
            f"· 费用发生日期：{ed}\n"
            f"· 驳回意见：{rejecter.get('comment') or '无'}\n\n"
            "如需重新报销，可以说「我想报销」；如有疑问也可以问我的工作问题哦。"
        )

    # pending：找到当前待审批人
    chain = latest.get("chain", [])
    cur = next((c for c in chain if c.get("status") == "pending"), None)
    done = [c for c in chain if c.get("status") == "approved"]
    progress = f"{len(done)}/{len(chain)}"
    if cur:
        return (
            f"您的 **{rb}** 报销（¥{amount}）正在审批中，当前进度 **{progress}**。\n\n"
            f"· 费用发生日期：{ed}\n"
            f"· 当前等待：**{cur['approver_name']}** 审批\n"
            f"· 完整流程：{' → '.join(c['approver_name'] for c in chain)}\n\n"
            "您可以在右上角「报销 / 审批」里查看详情；也可以照常问我工作问题。"
        )
    return (
        f"您的 **{rb}** 报销（¥{amount}）状态：审批中。\n"
        f"· 费用发生日期：{ed}\n"
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
        "created_at": reimburse_store._now(),
        "updated_at": reimburse_store._now(),
    }


def start(user) -> tuple[dict, str]:
    draft = _new_draft(user)
    reimburse_store.save_draft(user.user_id, draft)
    text = (
        "好的，我来帮您发起报销申请 💰\n\n"
        "我会依次询问报销类型、报销金额、费用发生日期、费用说明和收款人，"
        "确认后会自动提交给部门负责人和财务复核。\n"
        "我们一步一步来，请直接回复即可。\n\n"
        "**① 报销类型**——请回复类型名称：\n"
        "差旅费 / 办公用品 / 业务招待费 / 交通费 / 通讯费 / 培训费 / 其他"
    )
    return draft, text


def _ask_text(step: str) -> str:
    if step == "ask_type":
        return ("**① 报销类型**——请回复类型名称：\n"
                "差旅费 / 办公用品 / 业务招待费 / 交通费 / 通讯费 / 培训费 / 其他")
    if step == "ask_amount":
        return ("**② 报销金额**（元）——例如：\n"
                "`800` 或 `1280.5` 或 `1.2万`")
    if step == "ask_date":
        return ("**③ 费用发生日期**——例如：\n"
                "`2026-09-24` 或 `9月24日` 或 `昨天`")
    if step == "ask_reason":
        return "**④ 费用说明**——例如：上海出差高铁票 / 团队办公用品采购。"
    if step == "ask_payee":
        return ("**⑤ 收款人**（选填）——填写收款同事或您的姓名；"
                "若无特别说明请回复「无」。")
    return ""


def _parse_type(text: str) -> str | None:
    for key, val in RB_TYPES.items():
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
    ed = a.get("expense_date", "—")
    ed_str = ed[:10] if isinstance(ed, str) else str(ed)
    return (
        "✅ 信息已采集齐全，请核对以下**报销申请**：\n\n"
        f"- 申请人：{draft['applicant_name']}（{draft['dept_name']}）\n"
        f"- 报销类型：{a.get('rb_type','—')}\n"
        f"- 报销金额：¥{a.get('amount', 0)}\n"
        f"- 费用发生日期：{ed_str}\n"
        f"- 费用说明：{a.get('reason','—')}\n"
        f"- 收款人：{a.get('payee') or '无'}\n\n"
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

    # 纯客套话 ≠ 字段答案：温和回应后重复当前问题，草稿原样保留
    if draft["state"] in STEPS and is_chitchat(text):
        return f"{chitchat_reply(text)}\n\n我们继续：{_ask_text(draft['state'])}", None

    # 取消：任何阶段都可放弃（在解析答案之前，避免被当成字段值）
    if any(w in text for w in CANCEL_WORDS):
        reimburse_store.delete_draft(user.user_id)
        return "已取消本次报销申请，草稿已清除。如需报销随时告诉我～", None

    # 用户在采集阶段又敲了触发词 → 重复当前问题，不当作答案
    if draft["state"] in STEPS and is_trigger(text) and len(text) <= 8:
        return _ask_text(draft["state"]), None

    if draft["state"] == "confirm":
        return _handle_confirm(user, text, draft)

    # ---- 采集阶段：解析当前步骤的答案 ----
    step = draft["step"]
    if step == "ask_type":
        rb = _parse_type(text)
        if not rb:
            return ("没太理解报销类型，请直接回复以下之一：\n"
                    "差旅费 / 办公用品 / 业务招待费 / 交通费 / 通讯费 / 培训费 / 其他", None)
        draft["answers"]["rb_type"] = rb
        reimburse_store.save_draft(user.user_id, draft)
        return _advance(draft), None

    if step == "ask_amount":
        amt = parse_amount(text)
        if amt is None or amt <= 0:
            return ("报销金额没解析成功，请填写数字金额，例如：`800` 或 `1.2万`", None)
        draft["answers"]["amount"] = amt
        reimburse_store.save_draft(user.user_id, draft)
        return _advance(draft), None

    if step == "ask_date":
        d = parse_expense_date(text)
        if d is None:
            return ("费用发生日期没解析成功，请按格式填写，例如：`2026-09-24` 或 `昨天`", None)
        # 以 ISO 字符串落库，保证 JSON 可序列化
        draft["answers"]["expense_date"] = d.isoformat()
        reimburse_store.save_draft(user.user_id, draft)
        return _advance(draft), None

    if step == "ask_reason":
        if not text:
            return ("费用说明不能为空，请简要说明，例如：上海出差高铁票。", None)
        draft["answers"]["reason"] = text
        reimburse_store.save_draft(user.user_id, draft)
        return _advance(draft), None

    if step == "ask_payee":
        if text in ("无", "没有", "没", "空", "否", ""):
            draft["answers"]["payee"] = None
        else:
            draft["answers"]["payee"] = text
        reimburse_store.save_draft(user.user_id, draft)
        return _advance(draft), None

    return "好的，我们继续。", None


def _handle_confirm(user, text: str, draft: dict) -> tuple[str, dict | None]:
    if any(w in text for w in CANCEL_WORDS):
        reimburse_store.delete_draft(user.user_id)
        return "已取消本次报销申请，草稿已清除。如需报销随时告诉我～", None
    if any(w in text for w in EDIT_WORDS):
        draft["answers"] = {}
        draft["step"] = "ask_type"
        draft["state"] = "ask_type"
        reimburse_store.save_draft(user.user_id, draft)
        return ("好的，我们重新填写。\n\n**① 报销类型**——请回复类型名称：\n"
                "差旅费 / 办公用品 / 业务招待费 / 交通费 / 通讯费 / 培训费 / 其他", None)
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
            hints.append(f"· 第 {c['step']} 步审批人 **{name}**，请切换登录账号 `{login_name}` 后点击右上角「报销 / 审批」处理")
        else:
            hints.append(f"· 第 {c['step']} 步审批人 {name}")
    return "**如何审批**\n" + "\n".join(hints)


def _submit(user, draft: dict) -> tuple[str, dict]:
    a = draft["answers"]
    ed = date.fromisoformat(a["expense_date"])
    amount = float(a.get("amount", 0))
    dept_id = draft["dept_id"]

    chain = routing.build_chain(dept_id, a["rb_type"], amount)
    approver_ids = [c["approver_id"] for c in chain]

    payload = {
        "rb_type": a["rb_type"],
        "amount": amount,
        "expense_date": ed,
        "reason": a.get("reason", ""),
        "payee": a.get("payee"),
        "dept_id": dept_id,
        "approver_ids": approver_ids,
        "originator_ding_id": user.user_id,
    }
    # 调用钉钉报销接口（mock 模式返回 DINGMOCK-xxxx）
    instance_id = dingtalk_client.create_expense_instance(payload)

    req = {
        "id": reimburse_store.gen_request_id(),
        "applicant_id": user.user_id,
        "applicant_name": user.display_name,
        "dept_id": dept_id,
        "dept_name": draft["dept_name"],
        "rb_type": a["rb_type"],
        "amount": amount,
        "expense_date": ed.isoformat(),
        "reason": a.get("reason", ""),
        "payee": a.get("payee"),
        "status": "pending",
        "dingtalk_instance_id": instance_id,
        "chain": chain,
        "created_at": reimburse_store._now(),
        "submitted_at": reimburse_store._now(),
        "completed_at": None,
        "notified": False,
    }
    reimburse_store.create_request(req)
    reimburse_store.delete_draft(user.user_id)

    first = chain[0]
    steps_desc = " → ".join(f"{c['step']}.{c['approver_name']}" for c in chain)
    approver_login_hint = _approver_login_hint(chain)
    text = (
        f"🎉 报销申请已生成并提交至钉钉（实例号 `{instance_id}`）。\n\n"
        f"**审批流程**：{steps_desc}\n"
        f"当前进度：**第 1 步 · {first['approver_name']} 待审批**\n\n"
        f"{approver_login_hint}\n\n"
        "审批期间您可以照常向我提问工作问题，不受影响。\n"
        "审批进度可在右上角「报销 / 审批」中查看；全部通过后会有提示，报销即生效。"
    )
    log.info("reimburse.submitted", rid=req["id"], applicant=user.user_id,
             instance=instance_id, amount=amount)
    from ..core import audit
    audit.record("reimburse_submit", user_id=user.user_id, user_name=user.display_name,
                 detail=f"{a['rb_type']} ¥{amount}", rid=req["id"], instance_id=instance_id)
    return text, req
