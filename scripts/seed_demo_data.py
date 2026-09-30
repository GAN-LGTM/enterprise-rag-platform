# -*- coding: utf-8 -*-
"""预置演示数据：让交付界面"开箱即有真实感"。

生成内容（均为幂等：已存在足够数据时自动跳过，--force 可强制重建）：
  - audit.json        近 30 天的真实感审计日志（登录/问答/上传/删除/权限变更/越权拦截），
                      覆盖低/中/高三档风险：含 1 起连续登录失败、1 起短时间跨部门扫描、
                      若干非工作时间登录与敏感部门访问，保证"异常检测"页签开箱有信号。
  - search_gaps.json  23 个知识缺口工单（待处理/已补充/已关闭混合），累计被问 60+ 次。
  - feedback.json     问答满意度反馈（约 87% 满意，符合健康系统水位）。
  - hot.json          热门问题排行。

用法（必须先停止服务）：
    stop.bat
    python scripts/seed_demo_data.py            # 幂等：数据足够则跳过
    python scripts/seed_demo_data.py --force    # 强制重建审计与缺口数据
    start.bat
"""
from __future__ import annotations

import hashlib
import json
import random
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "backend" / "data"
FORCE = "--force" in sys.argv

rng = random.Random(20260925)

# ---------------- 账号与部门（与 backend/app/db/models.py 内置口径一致） ----------------
USERS = [
    ("u_admin",   "admin",      "系统管理员"),
    ("u_mkt_d",   "mkt_director", "营销总监-李娜"),
    ("u_sales_m", "sales_mgr",  "销售主管-王强"),
    ("u_sales_e", "sales_emp",  "销售专员-张三"),
    ("u_mkt_e",   "market_emp", "市场专员-赵敏"),
    ("u_tech_d",  "tech_director", "技术总监-陈晨"),
    ("u_rd_e",    "rd_emp",     "研发工程师-孙悟空"),
    ("u_fin_m",   "fin_mgr",    "财务主管-周敏"),
    ("u_hr_e",    "hr_emp",     "人力专员-嫦娥"),
    ("u_legal_e", "legal_emp",  "法务专员-包拯"),
    ("u_ceo",     "ceo",        "总经理-黛玉"),
    ("u_kb_r",    "kb_reviewer", "知识审核员-王芳"),
    ("u_comp_r",  "compliance_reviewer", "合规审核员-李明"),
]
# 用户 -> 常用部门（生成 chat 日志时用，保证大部分行为"看起来合理"）
USER_DEPTS = {
    "sales_emp": ["mkt.sales", "public"], "market_emp": ["mkt.market", "public"],
    "sales_mgr": ["mkt.sales", "public"], "mkt_director": ["mkt", "mkt.sales", "mkt.market"],
    "rd_emp": ["tech.rd", "public"], "tech_director": ["tech", "tech.rd", "tech.qa"],
    "fin_mgr": ["fin.general", "fin", "public"], "hr_emp": ["hr", "public"],
    "legal_emp": ["legal", "public"], "ceo": ["mkt", "tech", "fin", "hr", "legal"],
    "admin": ["public"], "kb_reviewer": ["hr", "public"],
    "compliance_reviewer": ["legal", "fin", "hr"],
}
SENSITIVE = ("fin", "hr", "legal")     # 与 SENSITIVE_DEPTS 默认口径一致

QUESTIONS = {
    "mkt.sales": ["销售提成怎么计算", "大客户折扣审批流程", "出差差旅费报销标准", "季度销售目标分解", "客户回款逾期怎么处理"],
    "mkt.market": ["品牌物料申领流程", "市场活动预算申请", "竞品分析报告模板", "展会参展审批流程"],
    "tech.rd": ["代码评审规范", "上线发布流程", "测试环境申请", "技术方案评审模板", "Git 分支管理规范"],
    "tech.qa": ["缺陷定级标准", "回归测试范围确认", "自动化测试覆盖率要求"],
    "fin.general": ["发票开具流程", "费用报销审批时限", "月度结账时间表", "预算调整申请流程"],
    "hr": ["入职流程清单", "年假计算规则", "转正答辩流程", "社保公积金缴纳比例", "离职交接清单"],
    "legal": ["合同审批流程", "保密协议模板", "用章申请流程", "合同违约处理流程"],
    "public": ["公司通讯录在哪里查", "办公用品申领", "会议室预订方式", "企业邮箱使用指南", "VPN 连接方法"],
}

PERM_TYPES = {"grant_dept", "revoke_dept", "perm_request_approve", "perm_request_reject", "perm_request_cancel"}
DELETE_TYPES = {"doc_delete", "doc_delete_request", "doc_delete_approve", "delete_ok", "delete_request"}


def _off_hours(dt: datetime) -> bool:
    return dt.weekday() >= 5 or dt.hour >= 22 or dt.hour < 6


def _sensitive(dept: str) -> bool:
    return any(dept == s or dept.startswith(s + ".") for s in SENSITIVE)


def _risk(t: str, dt: datetime, dept: str = "", dept_ids=None) -> str:
    if t in ("login_fail",) or t in PERM_TYPES or t in DELETE_TYPES or t == "chat_deny":
        return "medium"
    if t == "login_ok" and _off_hours(dt):
        return "medium"
    if dept and _sensitive(dept):
        return "medium"
    if any(_sensitive(d) for d in (dept_ids or [])):
        return "medium"
    return "low"


def _ev(ts: float, t: str, uid: str, uname: str, detail: str, **extra) -> dict:
    dt = datetime.fromtimestamp(ts)
    item = {
        "ts": round(ts, 3),
        "type": t,
        "user_id": uid,
        "user_name": uname,
        "detail": detail[:500],
        "trace_id": uuid.uuid4().hex[:16],
        "risk_level": _risk(t, dt, extra.get("dept", ""), extra.get("dept_ids")),
    }
    item.update({k: v for k, v in extra.items() if v is not None})
    return item


def _rand_work_dt(base: datetime) -> datetime:
    """工作日 08:30 - 19:30 之间的随机时间。"""
    d = base
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.replace(hour=rng.randint(8, 19), minute=rng.randint(0, 59), second=rng.randint(0, 59))


# ============================================================
#  1. 审计日志
# ============================================================
def seed_audit() -> None:
    path = DATA / "audit.json"
    existing = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            existing = []
    if not FORCE and len(existing) >= 300:
        print(f"  审计日志已有 {len(existing)} 条，跳过（--force 可重建）")
        return

    items: list[dict] = []
    now = datetime.now()

    for day in range(30, 0, -1):
        base = now - timedelta(days=day)
        # 每天 6-10 人登录
        daily_users = rng.sample(USERS, k=rng.randint(6, 10))
        for uid, uname, disp in daily_users:
            lt = _rand_work_dt(base.replace(hour=8))
            items.append(_ev(lt.timestamp(), "login_ok", uid, disp, f"{disp} 登录成功"))
            # 每人 1-6 次问答
            for _ in range(rng.randint(1, 6)):
                dept = rng.choice(USER_DEPTS.get(uname, ["public"]))
                q = rng.choice(QUESTIONS.get(dept, QUESTIONS["public"]))
                qt = lt + timedelta(minutes=rng.randint(5, 300))
                items.append(_ev(qt.timestamp(), "chat", uid, disp, q,
                                 dept=dept, dept_ids=[dept], intent="knowledge"))
        # 偶发事件
        if rng.random() < 0.35:
            uid, uname, disp = rng.choice(USERS)
            lt = _rand_work_dt(base)
            items.append(_ev(lt.timestamp(), "login_fail", uid, disp, "密码错误"))
        if rng.random() < 0.30:
            uid, uname, disp = rng.choice([u for u in USERS if u[1] in ("fin_mgr", "sales_mgr", "mkt_director", "kb_reviewer")])
            dept = USER_DEPTS[uname][0]
            items.append(_ev(_rand_work_dt(base).timestamp(), "doc_upload", uid, disp,
                             f"上传文档到 {dept}", dept=dept))
        if rng.random() < 0.25:
            uid, uname, disp = rng.choice(USERS)
            items.append(_ev(_rand_work_dt(base).timestamp(), "leave_submit", uid, disp,
                             f"{disp} 提交请假申请"))
        if rng.random() < 0.20:
            uid, uname, disp = rng.choice(USERS)
            items.append(_ev(_rand_work_dt(base).timestamp(), "reimburse_submit", uid, disp,
                             f"{disp} 提交报销单"))

    # ---- 有信号的风险事件（异常检测页签开箱可见）----
    # R1 非工作时间登录（3 起）
    for day, hour, who in ((2, 23, "rd_emp"), (5, 5, "fin_mgr"), (9, 22, "mkt_director")):
        base = now - timedelta(days=day)
        lt = base.replace(hour=hour, minute=rng.randint(0, 59))
        uid, uname, disp = next(u for u in USERS if u[1] == who)
        items.append(_ev(lt.timestamp(), "login_ok", uid, disp, f"{disp} 登录成功（非工作时间）"))

    # R2 连续登录失败（昨天，sales_emp 账号被撞 4 次）
    base = now - timedelta(days=1)
    t0 = base.replace(hour=14, minute=20)
    for i in range(4):
        items.append(_ev((t0 + timedelta(minutes=i * 2)).timestamp(), "login_fail",
                         "u_sales_e", "销售专员-张三", "密码错误（异地 IP）"))

    # R3 短时间大量跨部门查询（3 天前，某账号 60 秒内扫了 11 个部门）
    base = now - timedelta(days=3)
    uid, uname, disp = next(u for u in USERS if u[1] == "market_emp")
    t0 = base.replace(hour=10, minute=15)
    all_depts = ["mkt.sales", "mkt.market", "mkt.service", "tech.rd", "tech.qa",
                 "tech.ops", "fin", "fin.general", "hr", "legal", "public"]
    for i, dept in enumerate(all_depts):
        q = rng.choice(QUESTIONS.get(dept, QUESTIONS["public"]))
        items.append(_ev((t0 + timedelta(seconds=i * 5)).timestamp(), "chat", uid, disp, q,
                         dept=dept, dept_ids=[dept], intent="knowledge"))

    # R4 频繁删除文档（5 天前，1 小时内删 5 份）
    base = now - timedelta(days=5)
    uid, uname, disp = next(u for u in USERS if u[1] == "fin_mgr")
    t0 = base.replace(hour=16, minute=0)
    for i in range(5):
        items.append(_ev((t0 + timedelta(minutes=i * 9)).timestamp(), "doc_delete_approve",
                         uid, disp, f"删除过期文档 fin-old-{i+1}.xlsx", dept="fin.general"))

    # R5 敏感部门访问（合规视角正常业务，但应留痕为中风险）
    for day in (1, 2, 4, 6, 7):
        base = _rand_work_dt(now - timedelta(days=day))
        uid, uname, disp = next(u for u in USERS if u[1] == "fin_mgr")
        items.append(_ev(base.timestamp(), "chat", uid, disp,
                         rng.choice(QUESTIONS["fin.general"]), dept="fin.general",
                         dept_ids=["fin.general"], intent="knowledge"))

    # R6 权限变更（审批通过 / 驳回 / 手动授权 / 回收）
    perm_seq = [
        (6, "perm_request_create", "u_rd_e", "研发工程师-孙悟空", "申请访问 fin.general | 需要核对项目成本", "fin.general"),
        (6, "perm_request_approve", "u_fin_m", "财务主管-周敏", "rd_emp -> fin.general（自动开通）", "fin.general"),
        (6, "grant_dept", "u_fin_m", "财务主管-周敏", "rd_emp -> fin.general", "fin.general"),
        (4, "perm_request_create", "u_mkt_e", "市场专员-赵敏", "申请访问 tech.rd | 需要查阅接口文档", "tech.rd"),
        (4, "perm_request_reject", "u_tech_d", "技术总监-陈晨", "驳回 market_emp -> tech.rd：建议先走公共文档", "tech.rd"),
        (2, "grant_dept", "u_admin", "系统管理员", "hr_emp -> legal（手动授权）", "legal"),
        (1, "revoke_dept", "u_admin", "系统管理员", "hr_emp -> legal（到期回收）", "legal"),
    ]
    for day, t, uid, disp, detail, dept in perm_seq:
        ts = _rand_work_dt(now - timedelta(days=day)).timestamp()
        items.append(_ev(ts, t, uid, disp, detail, dept=dept, changed=True))

    # 越权拦截（chat_deny）
    for day in (3, 8, 12):
        base = _rand_work_dt(now - timedelta(days=day))
        uid, uname, disp = next(u for u in USERS if u[1] == "sales_emp")
        items.append(_ev(base.timestamp(), "chat_deny", uid, disp,
                         "试图访问未授权部门 fin 的知识库，已拦截", dept="fin"))

    items.sort(key=lambda x: x["ts"])
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    high_hint = sum(1 for i in items if i["risk_level"] != "low")
    print(f"  审计日志：生成 {len(items)} 条（含中高风险 {high_hint} 条）")


# ============================================================
#  2. 知识缺口工单
# ============================================================
GAP_SEED = [
    # (question, dept, reason, count, status, feedbacks)
    ("销售提成阶梯比例 2026 版", "mkt.sales", "no_hit", 6, "pending", 2),
    ("海外客户签单的汇率结算规则", "mkt.sales", "low_evidence", 4, "pending", 1),
    ("大客户年度框架合同模板", "mkt.sales", "no_hit", 3, "pending", 0),
    ("渠道代理商返点政策", "mkt.sales", "no_hit", 5, "supplemented", 1),
    ("市场部线下活动供应商名录", "mkt.market", "no_hit", 3, "pending", 1),
    ("品牌 VI 规范最新版本", "mkt.market", "outdated", 4, "supplemented", 2),
    ("客服部投诉升级处理时限", "mkt.service", "no_hit", 2, "pending", 0),
    ("微服务灰度发布操作手册", "tech.rd", "no_hit", 5, "pending", 2),
    ("数据库变更审批流程", "tech.rd", "low_evidence", 4, "pending", 1),
    ("测试环境数据脱敏规范", "tech.qa", "no_hit", 3, "pending", 1),
    ("生产事故定级与复盘模板", "tech.ops", "no_hit", 4, "supplemented", 1),
    ("跨境支付手续费核算口径", "fin.general", "no_hit", 3, "pending", 1),
    ("固定资产折旧年限表", "fin.general", "no_hit", 2, "pending", 0),
    ("季度预算滚动调整模板", "fin", "low_evidence", 3, "pending", 1),
    ("实习生转正考核标准", "hr", "no_hit", 4, "pending", 2),
    ("异地办公申请政策", "hr", "no_hit", 3, "pending", 1),
    ("竞业限制协议适用范围", "hr", "low_evidence", 2, "closed", 0),
    ("股权期权激励计划说明", "hr", "no_hit", 5, "pending", 2),
    ("供应商合同违约条款审查要点", "legal", "no_hit", 3, "pending", 1),
    ("数据出境合规评估流程", "legal", "no_hit", 4, "pending", 2),
    ("商标续展申请时间表", "legal", "no_hit", 2, "supplemented", 0),
    ("员工发明创造归属规定", "legal", "low_evidence", 2, "closed", 1),
    ("采购招标评分细则", "public", "no_hit", 3, "pending", 1),
]
FB_USERS = ["销售专员-张三", "市场专员-赵敏", "研发工程师-孙悟空", "人力专员-嫦娥", "法务专员-包拯"]
FB_REASONS = {"inaccurate": "答案不准确", "not_found": "没有找到资料",
              "irrelevant_cite": "引用不相关", "outdated": "内容已过期"}


def _qhash(q: str) -> str:
    return hashlib.md5(q.strip().encode("utf-8")).hexdigest()[:12]


def seed_gaps() -> None:
    path = DATA / "search_gaps.json"
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            existing = {}
    if not FORCE and len(existing) >= 15:
        print(f"  知识缺口已有 {len(existing)} 条，跳过（--force 可重建）")
        return

    now = time.time()
    gaps: dict[str, dict] = {}
    for i, (q, dept, reason, count, status, fb_n) in enumerate(GAP_SEED):
        first = now - (25 - i) * 86400 + rng.randint(-3600, 3600)
        last = first + rng.randint(1, 8) * 86400
        feedbacks = []
        for j in range(fb_n):
            feedbacks.append({
                "id": f"fb{int(first * 1000)}-{j}",
                "user_id": "",
                "user_name": rng.choice(FB_USERS),
                "reasons": rng.sample(list(FB_REASONS), k=rng.randint(1, 2)),
                "note": rng.choice(["急用，麻烦尽快补充", "问了几次都没有", "部分内容有了但不全", ""]),
                "created_at": round(first + 3600 * (j + 1), 3),
            })
        resolution = None
        if status in ("supplemented", "closed"):
            resolution = {
                "docs": [f"{dept}-doc-{i}.pdf"] if status == "supplemented" else [],
                "note": "资料已补充上架" if status == "supplemented" else "不在知识库收录范围，已线下答复",
                "operator": "系统管理员",
                "resolved_at": round(last + 7200, 3),
            }
        rec = {
            "question": q, "department_id": dept, "reason": reason,
            "count": count, "first_asked_at": round(first, 3), "last_asked_at": round(last, 3),
            "last_user": rng.choice(FB_USERS), "last_trace_id": uuid.uuid4().hex[:16],
            "feedbacks": feedbacks, "status": status,
            "assignee": rng.choice(["", "fin_mgr", "sales_mgr", "kb_reviewer"]),
            "resolution": resolution, "notifications": [],
            "updated_at": round(last, 3),
        }
        gaps[f"{dept}:{_qhash(q)}"] = rec

    path.write_text(json.dumps(gaps, ensure_ascii=False, indent=2), encoding="utf-8")
    total_asks = sum(r["count"] for r in gaps.values())
    print(f"  知识缺口：生成 {len(gaps)} 个工单（累计被问 {total_asks} 次）")


# ============================================================
#  3. 满意度反馈 + 热门问题
# ============================================================
def seed_feedback() -> None:
    fb_path = DATA / "feedback.json"
    hot_path = DATA / "hot.json"
    existing_fb = []
    if fb_path.exists():
        try:
            existing_fb = json.loads(fb_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            existing_fb = []
    if not FORCE and len(existing_fb) >= 50:
        print(f"  满意度反馈已有 {len(existing_fb)} 条，跳过")
        return

    now = time.time()
    fbs = []
    hot: dict[str, dict] = {}
    for day in range(30, 0, -1):
        for _ in range(rng.randint(1, 4)):
            dept = rng.choice(list(QUESTIONS))
            q = rng.choice(QUESTIONS[dept])
            ts = now - day * 86400 + rng.randint(0, 80000)
            liked = rng.random() < 0.87
            fbs.append({
                "ts": round(ts, 3), "question": q, "department_id": dept,
                "feedback_type": "like" if liked else "dislike",
                "user_name": rng.choice(FB_USERS), "trace_id": uuid.uuid4().hex[:16],
            })
            key = f"{dept}:{hashlib.md5(q.strip().encode('utf-8')).hexdigest()}"
            rec = hot.setdefault(key, {"question": q.strip(), "department_id": dept,
                                       "ask_count": 0, "last_asked_at": 0.0})
            rec["ask_count"] += rng.randint(1, 3)
            rec["last_asked_at"] = round(ts, 3)

    fb_path.write_text(json.dumps(fbs, ensure_ascii=False, indent=2), encoding="utf-8")
    hot_path.write_text(json.dumps(hot, ensure_ascii=False, indent=2), encoding="utf-8")
    likes = sum(1 for f in fbs if f["feedback_type"] == "like")
    print(f"  满意度反馈：生成 {len(fbs)} 条（满意率 {likes/len(fbs)*100:.0f}%）；热门问题 {len(hot)} 条")


def main() -> int:
    if not DATA.is_dir():
        print(f"未找到数据目录：{DATA}")
        return 1
    print("=" * 58)
    print("  预置演示数据（交付开箱数据）")
    print(f"  数据目录：{DATA}")
    print("=" * 58)
    seed_audit()
    seed_gaps()
    seed_feedback()
    print("\n完成。请重新启动服务（start.bat）后查看效果。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
