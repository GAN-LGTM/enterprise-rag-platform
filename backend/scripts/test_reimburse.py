"""
报销工作流单元测试：覆盖触发识别、进度查询识别、字段解析、引导提交、
审批链路由、二级/三级审批推进、越权防护、取消、通知生成。
运行：PYTHONPATH=backend python backend/scripts/test_reimburse.py
"""
import os
import sys

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.abspath(BACKEND))

from app.db.models import get_user_by_name  # noqa: E402
from app.reimburse import flow as rb_flow, reimburse_store as rbstore, routing  # noqa: E402

# 干净环境：清空历史数据
_DATA = os.path.join(BACKEND, "..", "data", "reimburse.json")
if os.path.exists(_DATA):
    os.remove(_DATA)

USERS = {"sales_emp": get_user_by_name("sales_emp")}  # 张三 / mkt.sales

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}")


print("== 报销流程触发 / 进度查询 ==")
check("is_trigger('我想报销')", rb_flow.is_trigger("我想报销"))
check("is_trigger('帮我报销差旅费')", rb_flow.is_trigger("帮我报销差旅费"))
check("is_trigger('报销标准是多少') 不算触发", not rb_flow.is_trigger("报销标准是多少"))
check("is_trigger('我的报销怎么写') 不算触发", not rb_flow.is_trigger("我的报销怎么写"))
check("is_progress_query('报销进度怎么样')", rb_flow.is_progress_query("报销进度怎么样"))
check("is_progress_query('我的报销')", rb_flow.is_progress_query("我的报销"))
check("is_progress_query('我想报销') 不算进度查询", not rb_flow.is_progress_query("我想报销"))

print("== 金额 / 日期解析 ==")
check("parse_amount('800')=800", rb_flow.parse_amount("800") == 800)
check("parse_amount('1.2万')=12000", rb_flow.parse_amount("1.2万") == 12000.0)
check("parse_amount('1280.5')=1280.5", rb_flow.parse_amount("1280.5") == 1280.5)
check("parse_amount('abc') 失败=None", rb_flow.parse_amount("abc") is None)
check("parse_expense_date('2026-09-24')", str(rb_flow.parse_expense_date("2026-09-24")) == "2026-09-24")
check("parse_expense_date('9月24日')", rb_flow.parse_expense_date("9月24日") is not None)
check("parse_expense_date('昨天')", rb_flow.parse_expense_date("昨天") is not None)

print("== 审批链路由 ==")
# 销售部张三 差旅费 普通金额 → 营销总监(部门) + 财务主管(必经) = 2 级
chain1 = routing.build_chain("mkt.sales", "差旅费", 800)
check("销售部差旅费审批链=2人", len(chain1) == 2)
check("链首=营销总监", chain1[0]["approver_name"] == "营销总监-李娜")
check("链次=财务主管", chain1[1]["approver_name"] == "财务主管-周敏")
# 大额 → 追加总经理
chain2 = routing.build_chain("mkt.sales", "差旅费", 8000)
check("大额追加总经理=3人", len(chain2) == 3 and chain2[2]["approver_name"] == "总经理-黛玉")
# 财务本部门 → 部门负责人=财务主管，必经财务去重，本人报销追加总经理
chain3 = routing.build_chain("fin.general", "办公用品", 300)
check("财务本部门链=2人(财务主管+总经理)", len(chain3) == 2 and chain3[-1]["approver_name"] == "总经理-黛玉")

print("== 完整引导流程 ==")
u = USERS["sales_emp"]
rbstore.delete_draft(u.user_id)
_, q1 = rb_flow.start(u)
check("start 首问含报销类型", "报销类型" in q1)

q2 = rb_flow.handle(u, "差旅费", rbstore.get_draft(u.user_id))[0]
check("ask_amount 追问金额", "报销金额" in q2)
q3 = rb_flow.handle(u, "800", rbstore.get_draft(u.user_id))[0]
check("ask_date 追问日期", "费用发生日期" in q3)
q4 = rb_flow.handle(u, "2026-09-24", rbstore.get_draft(u.user_id))[0]
check("ask_reason 追问说明", "费用说明" in q4)
q5 = rb_flow.handle(u, "上海出差高铁票", rbstore.get_draft(u.user_id))[0]
check("ask_payee 追问收款人", "收款人" in q5)
q6 = rb_flow.handle(u, "无", rbstore.get_draft(u.user_id))[0]
check("confirm 汇总核对", "请核对" in q6)

text, req = rb_flow.handle(u, "确认", rbstore.get_draft(u.user_id))
check("submit 生成钉钉实例号", "DINGMOCK-" in text)
check("submit 申请已落库", req is not None and req["id"].startswith("RB"))
check("submit 金额=800", req["amount"] == 800)
check("submit 草稿已清除", rbstore.get_draft(u.user_id) is None)
check("submit 一级审批人=营销总监", req["chain"][0]["approver_id"] == "u_mkt_d")
check("submit 二级审批人=财务主管", req["chain"][1]["approver_id"] == "u_fin_m")
check("submit 审批链长度=2", len(req["chain"]) == 2)
check("submit 状态=pending", req["status"] == "pending")
check("submit 提示含如何审批", "如何审批" in text and "mkt_director" in text)

print("== 进度查询文案 ==")
prog = rb_flow.query_progress(u)
check("进度查询显示审批中", "审批中" in prog)
check("进度查询显示当前审批人", "营销总监-李娜" in prog)
check("进度查询显示完整流程", "营销总监-李娜" in prog and "财务主管-周敏" in prog)

print("== 纯客套话不打断流程 ==")
from app.intent.chitchat import is_chitchat  # noqa: E402
check("chitchat 识别 好哒谢谢", is_chitchat("好哒，谢谢"))
check("chitchat 识别 嗯嗯", is_chitchat("嗯嗯"))
check("chitchat 不误判业务句", not is_chitchat("好的，那报销流程是什么"))
check("chitchat 不误判金额答案", not is_chitchat("800"))
rbstore.delete_draft(u.user_id)
rb_flow.start(u)
qc = rb_flow.handle(u, "好哒，谢谢", rbstore.get_draft(u.user_id))[0]
check("采集阶段客套话→重复当前问题", "报销类型" in qc)
d1 = rbstore.get_draft(u.user_id)
check("客套话后草稿仍在且无答案", d1 is not None and not d1["answers"].get("rb_type"))
q_after = rb_flow.handle(u, "差旅费", rbstore.get_draft(u.user_id))[0]
check("客套话后流程正常推进", "报销金额" in q_after)
rbstore.delete_draft(u.user_id)

print("== 审批推进（二级）==")
# 营销总监通过 → 流转到财务主管
rbstore.approve_step(req["id"], "u_mkt_d", "同意")
prog2 = rb_flow.query_progress(u)
check("一级通过后显示进度 1/2", "1/2" in prog2)
# 财务主管通过 → 全部通过
rbstore.approve_step(req["id"], "u_fin_m", "准予报销")
prog3 = rb_flow.query_progress(u)
check("全部通过后提示生效", "全部审批通过" in prog3 and "报销生效" in prog3)
# 通知生成
events = rbstore.pop_notifications(u.user_id)
check("通过通知已生成", any(e["status"] == "approved" for e in events))

print("== 越权防护 ==")
rbstore.reject_step(req["id"], "u_mkt_d", "x")  # 已完成，应返回 None（无待审步骤）
# 重新造一张待审单
rbstore.delete_draft(u.user_id)
_, _ = rb_flow.start(u)
_, r2 = rb_flow.handle(u, "差旅费", rbstore.get_draft(u.user_id))
_, _ = rb_flow.handle(u, "800", rbstore.get_draft(u.user_id))
_, _ = rb_flow.handle(u, "2026-09-24", rbstore.get_draft(u.user_id))
_, _ = rb_flow.handle(u, "上海出差高铁票", rbstore.get_draft(u.user_id))
_, _ = rb_flow.handle(u, "无", rbstore.get_draft(u.user_id))
_, r2 = rb_flow.handle(u, "确认", rbstore.get_draft(u.user_id))
check("新单待审", r2["status"] == "pending")
# 用错误的人（技术总监）审批 → 应返回 None（越权）
blocked = rbstore.approve_step(r2["id"], "u_tech_d", "x")
check("越权审批被拒绝(None)", blocked is None)

print("== 取消 ==")
rbstore.delete_draft(u.user_id)
rb_flow.start(u)
_, cq = rb_flow.handle(u, "差旅费", rbstore.get_draft(u.user_id))
ctext, _ = rb_flow.handle(u, "取消", rbstore.get_draft(u.user_id))
check("任意阶段取消生效", "已取消" in ctext)
check("取消后草稿清除", rbstore.get_draft(u.user_id) is None)

print("== 无记录进度查询 ==")
# 用另一个全新用户避免被上面数据干扰
empty_prog = rb_flow.query_progress(get_user_by_name("rd_emp"))
check("无记录提示友好", "还没有提交过" in empty_prog or "我想报销" in empty_prog)

print(f"\n结果：通过 {PASS} / 失败 {FAIL}")
if FAIL:
    sys.exit(1)
print("✅ 全部报销工作流测试通过")
