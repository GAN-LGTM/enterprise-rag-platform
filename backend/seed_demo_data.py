"""
交付前演示数据整理脚本（一次性）。

目标：把"测试痕迹"重的反馈 / 知识缺口工单 / 审计日志 / 会话 / 请假 / 报销
重置为一套干净、可信的演示数据，并保留热门问题（业务提问）作为产品能力展示。

设计要点：
  · 所有文件路径都用 `Path(__file__).resolve()` 推导，避免沙箱 /d 字面量路径解析怪癖。
  · feedback.json   重播为满意度 88%（25 条：22 赞 + 3 踩）
  · search_gaps.json 重播 4 条干净工单（2 待处理 / 1 已补充闭环 / 1 已关闭），去掉"还是查不到"等测试残渣
  · audit.json     重播 12 条干净操作日志（登录 / 问答 / 缺口治理 / 审批流）
  · sessions.json  重置为 8 条干净演示会话（各角色各几条，含 1-2 轮真实业务问答）
  · leave.json / reimburse.json 重置为空（全新系统，可现场演示"意图识别→提交→审批"）
  · hot.json       保留（业务提问排行榜，本身是好的产品演示素材）
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent          # backend/
BACKEND_DATA = HERE / "data"                     # backend/data/
ROOT_DATA = HERE.parent / "data"                 # <project>/data/  (sessions 在此)

NOW = time.time()
DAY = 86400.0


def _iso(ts: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# 1) 满意度反馈：22 赞 + 3 踩 = 88%
# ============================================================
def build_feedback():
    likes_q = [
        ("差旅报销的标准流程是什么", "差旅报销需先在报销系统提交申请，附行程单与发票，部门负责人审批后由财务复核打款。"),
        ("销售合同审批需要几级", "标准销售合同由销售主管初审、法务复核、财务确认后生效；金额超阈值需总监加签。"),
        ("研发环境怎么申请权限", "研发环境权限通过 ITSM 工单申请，由研发部负责人审批，运维在 1 个工作日内开通。"),
        ("年假怎么计算", "入职满 1 年享 5 天年假，每满 1 年递增 1 天，上限 15 天，按自然年清零。"),
        ("客服工单的 SLA 是多久", "P1 故障 15 分钟响应、2 小时解决；P2 1 小时响应、8 小时解决。"),
        ("市场活动预算怎么报批", "单次 5 万以内由市场部负责人批；超过需营销总监与财务双签。"),
        ("离职流程需要哪些步骤", "离职需直属领导审批、IT 回收设备、财务清算借款、HR 办理社保转出。"),
        ("新员工入职需要准备什么", "身份证、银行卡、学历证明，并签署保密与廉洁协议，首日由 HR 引导完成。"),
        ("产品上线前要做哪些合规检查", "隐私影响评估、等保备案、数据分类分级、第三方组件安全扫描四项必过。"),
        ("发票抬头怎么修改", "在财务系统「发票管理」提交抬头变更，附营业执照，财务 1 个工作日处理。"),
        ("试用期是几个月", "标准试用期为 3 个月，技术骨干可约定 6 个月，以劳动合同为准。"),
        ("采购申请审批流程", "5 万内部门负责人批，5-20 万加财务与分管总审，20 万以上上会决议。"),
        ("数据安全分级怎么划分", "分为公开、内部、机密、绝密四级，机密以上须加密存储并限制访问范围。"),
        ("加班费怎么计算", "工作日加班 1.5 倍、休息日 2 倍、法定假日 3 倍，需事前在系统提报审批。"),
        ("保密协议有效期", "在职期间持续有效，离职后仍具约束力，核心岗位约定竞业限制最长 2 年。"),
        ("服务器巡检频率", "生产环境每日自动巡检、每周人工复查、每月出具健康报告。"),
        ("客户投诉处理时限", "首次响应不超过 4 小时，实质性处理方案不超过 3 个工作日。"),
        ("预算调整怎么发起", "年中预算调整由部门提报、财务汇总、经管会审批后生效。"),
        ("第三方组件安全怎么管控", "引入前做漏洞扫描与许可证审查，上线后纳入持续监控与季度复扫。"),
        ("会议室怎么预订", "通过办公系统预订，超 20 人需提前 3 天，跨部门会议由行政统一协调。"),
        ("报销单据保存几年", "会计凭证与报销单据法定保存 30 年，电子档案同步留痕可追溯。"),
        ("股权/期权激励怎么归属", "期权分 4 年归属、每年 25%，满 1 年首期解锁，离职未归属部分收回。"),
    ]
    dislikes_q = [
        ("海外出差汇率怎么折算", ""),
        ("服务器宕机应急联系人", ""),
        ("季度绩效考核标准", ""),
    ]
    items = []
    t = NOW - 3 * DAY
    names = ["销售专员-张三", "市场专员-赵敏", "研发工程师-孙悟空", "财务主管-周敏",
             "人力专员-嫦娥", "法务专员-包拯", "销售主管-王强", "营销总监-李娜", "技术总监-陈晨"]
    for i, (q, a) in enumerate(likes_q):
        items.append({
            "feedback_type": "like", "ts": round(t + i * 137, 3),
            "question": q, "answer": a,
            "user_name": names[i % len(names)], "user_id": f"u_{i % 9}",
            "session_id": f"seed_{i}",
        })
    for i, q in enumerate(dislikes_q):
        items.append({
            "feedback_type": "dislike", "ts": round(t + 3000 + i * 911, 3),
            "question": q, "answer": "",
            "user_name": names[i % len(names)], "user_id": f"u_{i % 9}",
            "session_id": f"seed_d{i}",
        })
    return items


# ============================================================
# 2) 知识缺口工单：4 条干净（2 待处理 / 1 已补充 / 1 已关闭）
# ============================================================
def _gap_key(dept: str, q: str) -> str:
    return f"{dept}:{hashlib.md5(q.strip().encode('utf-8')).hexdigest()[:12]}"


def _gap(dept, q, reason, status, count, first_off, last_off, last_user, feedbacks, resolution, notifications):
    rec = {
        "question": q, "department_id": dept, "reason": reason,
        "count": count,
        "first_asked_at": round(NOW - first_off * DAY, 3),
        "last_asked_at": round(NOW - last_off * DAY, 3),
        "last_user": last_user, "last_trace_id": "",
        "feedbacks": feedbacks, "status": status,
        "assignee": "", "resolution": resolution, "notifications": notifications,
        "updated_at": round(NOW - last_off * DAY, 3),
    }
    if status == "pending" and feedbacks:
        rec["assignee"] = feedbacks[0].get("assignee", "")
    return rec


def build_gaps():
    gaps = {}
    # (1) 销售部 待处理：差旅补贴
    gaps[_gap_key("mkt.sales", "海外子公司差旅补贴标准是多少")] = _gap(
        "mkt.sales", "海外子公司差旅补贴标准是多少", "no_hit", "pending", 3, 12, 1, "销售专员-张三",
        [{"id": "fb1", "user_id": "u_sales_e", "user_name": "销售专员-张三",
          "reasons": ["not_found"], "note": "东南亚出差伙食补贴按什么标准算？",
          "created_at": round(NOW - 1 * DAY, 3), "assignee": "sales_mgr"},
         {"id": "fb2", "user_id": "u_sales_m", "user_name": "销售主管-王强",
          "reasons": ["not_found"], "note": "希望补充海外驻点补贴细则",
          "created_at": round(NOW - 0.5 * DAY, 3), "assignee": "sales_mgr"}],
        None, [])
    # (2) 研发部 待处理：API 网关限流
    gaps[_gap_key("tech.rd", "新版本 API 网关的限流阈值怎么配置")] = _gap(
        "tech.rd", "新版本 API 网关的限流阈值怎么配置", "low_evidence", "pending", 2, 6, 2, "研发工程师-孙悟空",
        [{"id": "fb3", "user_id": "u_rd_e", "user_name": "研发工程师-孙悟空",
          "reasons": ["inaccurate", "irrelevant_cite"], "note": "检索到的文档是旧版网关，阈值单位不一致",
          "created_at": round(NOW - 2 * DAY, 3), "assignee": "tech_director"}],
        None, [])
    # (3) 市场部 已补充（闭环演示）：展会预算审批
    g3_key = _gap_key("mkt.market", "年度品牌展会预算审批流程")
    gaps[g3_key] = _gap(
        "mkt.market", "年度品牌展会预算审批流程", "no_hit", "supplemented", 4, 20, 5, "市场专员-赵敏",
        [{"id": "fb4", "user_id": "u_mkt_e", "user_name": "市场专员-赵敏",
          "reasons": ["not_found"], "note": "大型展会预算由谁批、需要哪些附件？",
          "created_at": round(NOW - 5 * DAY, 3), "assignee": "mkt_director"}],
        {"docs": ["市场部品牌展会管理办法 v2.docx"],
         "note": "已补充《市场部品牌展会管理办法 v2》：单次 5 万内市场部负责人批，超阈值营销总监+财务双签，需附比价单。",
         "operator": "营销总监-李娜", "resolved_at": round(NOW - 4 * DAY, 3)},
        [{"id": "nt1", "user_name": "市场专员-赵敏", "gap_id": g3_key,
          "question": "年度品牌展会预算审批流程", "status": "supplemented",
          "message": "你反馈的知识缺口「年度品牌展会预算审批流程」已补充（已补充文档：市场部品牌展会管理办法 v2.docx），再问一次试试～",
          "created_at": round(NOW - 4 * DAY, 3), "read": False}])
    # (4) 公共 已关闭：离职账号注销
    gaps[_gap_key("public", "离职员工账号注销与数据交接 SOP")] = _gap(
        "public", "离职员工账号注销与数据交接 SOP", "no_hit", "closed", 1, 30, 28, "人力专员-嫦娥",
        [{"id": "fb5", "user_id": "u_hr_e", "user_name": "人力专员-嫦娥",
          "reasons": ["not_found"], "note": "离职后各系统账号如何统一注销？",
          "created_at": round(NOW - 28 * DAY, 3), "assignee": "admin"}],
        {"docs": [], "note": "该流程归属 ITSM/SSO 统一账号生命周期管理，已在线下知识库归档，关闭本工单。",
         "operator": "系统管理员", "resolved_at": round(NOW - 27 * DAY, 3)},
        [])
    return gaps


# ============================================================
# 3) 审计日志：12 条干净操作记录
# ============================================================
def build_audit():
    items = [
        {"ts": round(NOW - 30 * DAY, 3), "type": "login_ok", "user_id": "u_admin", "user_name": "系统管理员", "detail": "登录成功"},
        {"ts": round(NOW - 20 * DAY, 3), "type": "login_ok", "user_id": "u_ceo", "user_name": "总经理-黛玉", "detail": "登录成功"},
        {"ts": round(NOW - 9 * DAY, 3), "type": "chat", "user_id": "u_sales_e", "user_name": "销售专员-张三", "detail": "问答：差旅报销的标准流程是什么"},
        {"ts": round(NOW - 8 * DAY, 3), "type": "chat", "user_id": "u_rd_e", "user_name": "研发工程师-孙悟空", "detail": "问答：研发环境怎么申请权限"},
        {"ts": round(NOW - 5 * DAY, 3), "type": "gap_feedback", "user_id": "u_mkt_e", "user_name": "市场专员-赵敏", "detail": "知识缺口反馈：年度品牌展会预算审批流程"},
        {"ts": round(NOW - 4 * DAY, 3), "type": "gap_resolve", "user_id": "u_mkt_d", "user_name": "营销总监-李娜", "detail": "缺口已补充：年度品牌展会预算审批流程"},
        {"ts": round(NOW - 3 * DAY, 3), "type": "leave_submit", "user_id": "u_sales_e", "user_name": "销售专员-张三", "detail": "提交请假：年假 3 天"},
        {"ts": round(NOW - 3 * DAY + 3600, 3), "type": "leave_approve", "user_id": "u_sales_m", "user_name": "销售主管-王强", "detail": "批准请假：销售专员-张三 年假 3 天"},
        {"ts": round(NOW - 2 * DAY, 3), "type": "reimburse_submit", "user_id": "u_rd_e", "user_name": "研发工程师-孙悟空", "detail": "提交报销：云服务器费用 ¥2,380"},
        {"ts": round(NOW - 2 * DAY + 7200, 3), "type": "reimburse_approve", "user_id": "u_fin_m", "user_name": "财务主管-周敏", "detail": "批准报销：研发工程师-孙悟空 ¥2,380"},
        {"ts": round(NOW - 1 * DAY, 3), "type": "gap_feedback", "user_id": "u_sales_e", "user_name": "销售专员-张三", "detail": "知识缺口反馈：海外子公司差旅补贴标准是多少"},
        {"ts": round(NOW - 0.5 * DAY, 3), "type": "login_ok", "user_id": "u_hr_e", "user_name": "人力专员-嫦娥", "detail": "登录成功"},
    ]
    return items


# ============================================================
# 4) 会话：8 条干净演示会话（各角色）
# ============================================================
def _sess(uid, sid, title, q, a):
    t = round(NOW - 2 * DAY, 3)
    return {
        "id": sid, "title": title,
        "messages": [
            {"role": "user", "content": q, "ts": t},
            {"role": "assistant", "content": a, "citations": [], "ts": t + 1.2},
        ],
        "created_at": t, "updated_at": t + 1.2,
    }


def build_sessions():
    data = {
        "u_sales_e": [
            _sess("u_sales_e", "s_se1", "差旅报销的标准流程是什么",
                  "差旅报销的标准流程是什么？",
                  "差旅报销需先在报销系统提交申请，附行程单与发票，由部门负责人审批、财务复核后打款；出差前建议先发起差旅申请单。"),
            _sess("u_sales_e", "s_se2", "销售合同审批需要几级",
                  "销售合同审批需要几级？",
                  "标准销售合同由销售主管初审、法务复核、财务确认后生效；金额超过 thresholds 需营销总监加签。"),
        ],
        "u_rd_e": [
            _sess("u_rd_e", "s_rd1", "研发环境怎么申请权限",
                  "研发环境怎么申请权限？",
                  "研发环境权限通过 ITSM 工单申请，由研发部负责人审批，运维在 1 个工作日内开通，并启用最小权限原则。"),
        ],
        "u_fin_m": [
            _sess("u_fin_m", "s_fin1", "发票抬头怎么修改",
                  "发票抬头怎么修改？",
                  "在财务系统「发票管理」提交抬头变更并附营业执照，财务 1 个工作日内处理完成。"),
        ],
        "u_hr_e": [
            _sess("u_hr_e", "s_hr1", "年假怎么计算",
                  "年假怎么计算？",
                  "入职满 1 年享 5 天年假，每满 1 年递增 1 天，上限 15 天，按自然年清零，离职时按已工作月份折算。"),
        ],
        "u_mkt_e": [
            _sess("u_mkt_e", "s_mk1", "市场活动预算怎么报批",
                  "市场活动预算怎么报批？",
                  "单次 5 万以内由市场部负责人审批；超过需营销总监与财务双签，并附活动方案与比价单。"),
        ],
        "u_ceo": [
            _sess("u_ceo", "s_ceo1", "公司差旅政策总览",
                  "帮我汇总一下各事业部的差旅政策。",
                  "各事业部差旅标准已在公共知识库统一归档，包含交通、住宿、伙食与补贴上限，可按部门维度检索查看。"),
        ],
    }
    return data


def _write(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    print(f"  wrote {path.name}  ({len(obj) if hasattr(obj, '__len__') else '?'} records)")


def main():
    print("== 交付前演示数据整理 ==")
    # feedback
    fb = build_feedback()
    like = sum(1 for f in fb if f["feedback_type"] == "like")
    print(f"feedback.json: {len(fb)} 条，赞 {like} 踩 {len(fb)-like} → 满意度 {like/len(fb)*100:.0f}%")
    _write(BACKEND_DATA / "feedback.json", fb)
    # gaps
    gaps = build_gaps()
    _write(BACKEND_DATA / "search_gaps.json", gaps)
    # audit
    aud = build_audit()
    _write(BACKEND_DATA / "audit.json", aud)
    # sessions
    sess = build_sessions()
    tot = sum(len(v) for v in sess.values())
    print(f"sessions.json: {tot} 条演示会话")
    _write(ROOT_DATA / "sessions.json", sess)
    # leave / reimburse → 空（全新系统，可现场演示意图识别→审批）
    _write(BACKEND_DATA / "leave.json", {"drafts": {}, "requests": []})
    _write(BACKEND_DATA / "reimburse.json", {"drafts": {}, "requests": []})
    # hot.json 保留（不覆盖）
    hot_path = BACKEND_DATA / "hot.json"
    if hot_path.exists():
        hot = json.loads(hot_path.read_text(encoding="utf-8"))
        print(f"hot.json: 保留（{len(hot)} 条业务提问，用于热门问题展示）")
    # intent_log 清空（内部识别日志，交付前不留测试痕）
    ilp = BACKEND_DATA / "intent_log.jsonl"
    if ilp.exists():
        ilp.write_text("", encoding="utf-8")
        print("intent_log.jsonl: 已清空")
    print("== 完成 ==")


if __name__ == "__main__":
    main()
