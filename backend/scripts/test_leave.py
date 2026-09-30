"""
请假工作流冒烟测试（无需启动服务，直接驱动领域层）。

覆盖：
  · 触发词识别
  · 钉钉字段逐一解析（类型/起止时间/时长/事由/交接）
  · 提交后生成申请 + 部门审批路由
  · 审批链推进 + 全部通过 → 通知
  · 长假期/年假追加高管复核（二级审批）
  · 多种部门的一级审批人映射
  · 越权审批防护 + 取消流程
"""
from __future__ import annotations

import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

from app.db.models import USERS  # noqa: E402
from app.leave import flow, leave_store, routing  # noqa: E402

# 干净环境：清空历史数据（与 leave_store.py 的 _STORE_PATH 保持一致）
_DATA = os.path.join(BACKEND, "data", "leave.json")
if os.path.exists(_DATA):
    os.remove(_DATA)
leave_store._LOADED = False
leave_store._DATA = {"drafts": {}, "requests": []}


def check(name: str, cond: bool):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"测试失败：{name}")


def ans(u, msg):
    """驱动一步引导对话，返回回复文本。"""
    return flow.handle(u, msg, leave_store.get_draft(u.user_id))[0]


# 1) 触发词
check("trigger 我想请假", flow.is_trigger("我想请假"))
check("trigger 申请年假", flow.is_trigger("申请年假"))
check("non-trigger 你好", not flow.is_trigger("你好，今天天气不错"))
check("non-trigger 年假怎么算(知识问答不误触发)",
      not flow.is_trigger("年假怎么算？需要走什么流程"))

# 2) 日期解析
dt = flow.parse_datetime("2026-09-24 09:00", 9)
check("parse_datetime 完整", dt is not None and dt.year == 2026 and dt.hour == 9)
dt2 = flow.parse_datetime("明天 14:00", 9)
check("parse_datetime 明天", dt2 is not None and dt2 > dt)
check("parse_datetime 失败返回None", flow.parse_datetime("随便说点", 9) is None)

# 3) 完整流程：sales_emp 请 2 天病假（单级审批）
u = USERS["sales_emp"]  # 张三 / mkt.sales
_, q1 = flow.start(u)
check("start 首问含请假类型", "请假类型" in q1)

q2 = ans(u, "病假")
check("ask_start 追问开始时间", "开始时间" in q2)
q3 = ans(u, "2026-09-24 09:00")
check("ask_end 追问结束时间", "结束时间" in q3)
q4 = ans(u, "2026-09-26 09:00")
check("ask_reason 追问事由", "请假事由" in q4)
q5 = ans(u, "感冒就医需要休息")
check("ask_handover 追问交接", "工作交接人" in q5)
q6 = ans(u, "无")
check("confirm 汇总核对", "请核对" in q6)

text, req = flow.handle(u, "确认", leave_store.get_draft(u.user_id))
check("submit 生成实例号", "DINGMOCK-" in text)
check("submit 申请已落库", req is not None and req["id"].startswith("LV"))
check("submit 时长=2天", abs(req["duration_days"] - 2.0) < 1e-6)
check("submit 草稿已清除", leave_store.get_draft(u.user_id) is None)
check("submit 一级审批人=营销总监", req["chain"][0]["approver_id"] == "u_mkt_d")
check("submit 单级审批链长度=1", len(req["chain"]) == 1)
check("submit 状态=pending", req["status"] == "pending")

# 4) 审批推进：营销总监通过 → 全通过
approver = USERS["mkt_director"]
r = leave_store.approve_step(req["id"], approver.user_id, "同意，注意休息")
check("approve 全部通过→approved", r["status"] == "approved")
check("approve 通知未读(notified=False)", r["notified"] is False)

events = leave_store.pop_notifications(u.user_id)
check("notify 取到审批通过事件", any(e["status"] == "approved" for e in events))
check("notify 已标记已读", leave_store.get_request(req["id"])["notified"] is True)
check("notify 二次读取为空", leave_store.pop_notifications(u.user_id) == [])

# 5) 长假期 / 年假 → 二级审批（高管复核）
u2 = USERS["sales_emp"]
flow.start(u2)
ans(u2, "年假")
ans(u2, "2026-10-01 09:00")
ans(u2, "2026-10-08 18:00")  # 7天
ans(u2, "陪家人出游")
ans(u2, "无")
_, req2 = flow.handle(u2, "确认", leave_store.get_draft(u2.user_id))
check("年假+7天 二级审批链长度=2", len(req2["chain"]) == 2)
check("二级审批人=总经理", req2["chain"][1]["approver_id"] == "u_ceo")
leave_store.approve_step(req2["id"], "u_mkt_d", "准")
check("一级通过后仍 pending", leave_store.get_request(req2["id"])["status"] == "pending")
leave_store.approve_step(req2["id"], "u_ceo", "准")
check("二级通过后 approved", leave_store.get_request(req2["id"])["status"] == "approved")

# 6) 多部门一级审批人映射
check("tech.rd→技术总监", routing.DEPT_PRIMARY_APPROVER["tech.rd"] == "u_tech_d")
check("fin.general→财务主管", routing.DEPT_PRIMARY_APPROVER["fin.general"] == "u_fin_m")
check("hr→总经理", routing.DEPT_PRIMARY_APPROVER["hr"] == "u_ceo")
check("legal→总经理", routing.DEPT_PRIMARY_APPROVER["legal"] == "u_ceo")

# 7) 越权审批防护
u3 = USERS["rd_emp"]  # 研发部员工
flow.start(u3)
ans(u3, "事假")
ans(u3, "2026-11-01 09:00")
ans(u3, "2026-11-02 18:00")
ans(u3, "私事")
ans(u3, "无")
_, req3 = flow.handle(u3, "确认", leave_store.get_draft(u3.user_id))
check("越权审批被拒(None)", leave_store.approve_step(req3["id"], "u_sales_m", "想越权") is None)
check("越权后状态仍 pending", leave_store.get_request(req3["id"])["status"] == "pending")

# 8) 取消流程
u4 = USERS["market_emp"]
flow.start(u4)
ans(u4, "事假")
text_cancel, _ = flow.handle(u4, "取消", leave_store.get_draft(u4.user_id))
check("取消→草稿清除", leave_store.get_draft(u4.user_id) is None)
check("取消→提示已取消", "取消" in text_cancel)

# 9) 进度查询优先于触发，且不启动新流程
check("进度查询不误触发", not flow.is_trigger("请假进度怎么样"))
check("进度查询意图识别", flow.is_progress_query("请假进度怎么样"))
check("多种进度问法识别", flow.is_progress_query("我的请假批了吗"))
check("正常触发仍有效", flow.is_trigger("我想请假"))

# 10) query_progress 文案（hr_emp 尚未提交过申请）
u5 = USERS["hr_emp"]
empty_progress = flow.query_progress(u5)
check("无记录时提示友好", "还没有" in empty_progress or "发起" in empty_progress)

# 11) 提交后查询进度
flow.start(u5)
ans(u5, "调休")
ans(u5, "2026-12-01 09:00")
ans(u5, "2026-12-02 18:00")
ans(u5, "处理私事")
ans(u5, "无")
_, req4 = flow.handle(u5, "确认", leave_store.get_draft(u5.user_id))
progress_text = flow.query_progress(u5)
check("进度查询显示 pending", "正在审批中" in progress_text)
check("进度查询显示当前审批人", "总经理-黛玉" in progress_text)
check("进度查询显示完整流程", "总经理-黛玉" in progress_text)

# 12) 全部通过后查询进度
leave_store.approve_step(req4["id"], "u_ceo", "同意")
progress_approved = flow.query_progress(u5)
check("通过后查询显示 approved", "全部审批通过" in progress_approved)
check("通过后查询显示审批人链", "总经理-黛玉" in progress_approved)

# 13) 进度查询不破坏已有草稿
u6 = USERS["market_emp"]
flow.start(u6)
ans(u6, "事假")
progress_in_draft = flow.handle(u6, "我的请假进度", leave_store.get_draft(u6.user_id))[0]
check("填表中途问进度仍能返回", "审批中" in progress_in_draft or "还没有提交" in progress_in_draft)
check("填表中途问进度后草稿仍在", leave_store.get_draft(u6.user_id) is not None)

# 14) 纯客套话不打断流程、绝不当成字段答案（"好哒，谢谢"场景）
from app.intent.chitchat import chitchat_reply, is_chitchat  # noqa: E402

check("chitchat 识别 好哒谢谢", is_chitchat("好哒，谢谢"))
check("chitchat 识别 嗯嗯", is_chitchat("嗯嗯"))
check("chitchat 识别 收到啦", is_chitchat("收到啦"))
check("chitchat 不误判业务句", not is_chitchat("好的，那请假流程是什么"))
check("chitchat 不误判日期答案", not is_chitchat("明天 9:00"))
check("chitchat 不误判类型答案", not is_chitchat("病假"))

u7 = USERS["market_emp"]
flow.start(u7)
# 注意：客套话回复是"变体池随机抽取"（产品要求同一句话连问两次不该一字不差），
# 所以这里不能断言单一子串，否则 40% 概率假失败 —— 与 test_quality_opt 的写法保持一致，
# 断言"回复属于致谢场景的合法变体集合"，既验证温和回应又稳定。
qc = ans(u7, "好哒，谢谢")
print(f"   [诊断] 客套话实际回复：{qc}")
check("采集阶段客套话→温和回应",
      any(v in qc for v in ("不客气", "应该的", "不用谢")))
check("采集阶段客套话→变体不呆板",
      len({chitchat_reply("好哒，谢谢") for _ in range(24)}) >= 2)
check("采集阶段客套话→重复当前问题", "请假类型" in qc)
draft_after = leave_store.get_draft(u7.user_id)
check("客套话后草稿仍在", draft_after is not None)
check("客套话未污染答案字段", not draft_after["answers"].get("leave_type"))
q_after = ans(u7, "病假")
check("客套话后流程正常推进", "开始时间" in q_after)
flow.handle(u7, "取消", leave_store.get_draft(u7.user_id))
check("清理草稿完成", leave_store.get_draft(u7.user_id) is None)

# 15) confirm 阶段"好的"仍是确认语（不受客套话拦截影响，且不含业务词也不会误判）
u8 = USERS["market_emp"]
flow.start(u8)
ans(u8, "事假")
ans(u8, "2026-10-10 09:00")
ans(u8, "2026-10-10 18:00")
ans(u8, "有事")
ans(u8, "无")
q_ok = ans(u8, "好的")
check("confirm 阶段 好的=确认提交", "DINGMOCK-" in q_ok or "实例号" in q_ok)

print("\n✅ 全部请假工作流测试通过")
