# -*- coding: utf-8 -*-
"""权限申请 / 合规审计 端到端验收（会写入演示数据，跑完请执行 reset_demo_data.py 清理）。"""
import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"

# 管理员口令：演示模式默认 admin123；交付环境用 ADMIN_PASSWORD 传入（初始口令由交付方持有）
ADMIN_PWD = os.getenv("ADMIN_PASSWORD", "admin123")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
OUT = []
FAILS = []


def log(msg):
    OUT.append(msg)
    print(msg)


def call(path, method="GET", token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"}
                                 | ({"Authorization": "Bearer " + token} if token else {}))
    try:
        with OPENER.open(req, timeout=20) as r:
            raw = r.read().decode("utf-8")
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as e:
        return 0, repr(e)


def login(u, p="123456"):
    s, d = call("/api/auth/login", "POST", body={"username": u, "password": p})
    tok = ""
    if s == 200 and isinstance(d, dict):
        tok = d.get("access_token") or ""
    return tok, d


def ck(name, cond, extra=""):
    log(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' :: ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


ADMIN = login("admin", ADMIN_PWD)
EMP = login("sales_emp")
MGR = login("mkt_director")      # 营销总监：目标部门（营销事业部）的管辖者，有审批权
CROSS_MGR = login("fin_mgr")     # 财务主管：跨事业部，无权审批营销类申请
CR = login("compliance_reviewer")
KB = login("kb_reviewer")
if not ADMIN[0]:
    log("admin 登录失败：%s" % ADMIN[1])
    ADMIN = login("admin", "123456")

log("=" * 60)
log("一、权限申请：员工发起 → 主管审批 → 自动开通")
log("=" * 60)
log(f"  [diag] EMP={repr(EMP)[:160]}")
log(f"  [diag] ADMIN={repr(ADMIN)[:160]}")
depts = call("/api/knowledge/departments", token=EMP[0])
log(f"  departments -> status={depts[0]} type={type(depts[1]).__name__} "
    f"raw={json.dumps(depts[1], ensure_ascii=False)[:200]}")
dept_list = depts[1] if isinstance(depts[1], list) else (depts[1].get("items") or [])
log(f"  可申请部门数：{len(dept_list)}")
target, target_name = None, ""
for d in dept_list:
    if d["id"] != "public":
        target, target_name = d["id"], d["name"]
        break
log(f"  目标部门：{target}（{target_name}）")
if not target:
    raise SystemExit("没有可用于测试的部门，终止")

s, d = call("/api/permission/request", "POST", token=EMP[0],
            body={"department_id": target, "reason": "端到端验收：需要查阅该部门资料"})
ck("员工提交权限申请", s == 200 and d.get("request_id"), f"{s} {d}")
rid = d.get("request_id")

s, d = call("/api/permission/request", "POST", token=EMP[0],
            body={"department_id": target, "reason": "重复提交"})
ck("重复申请被拦截", s == 400, s)

s, mine = call("/api/permission/my", token=EMP[0])
ck("员工能看到自己的申请", s == 200 and any(i["id"] == rid for i in mine.get("items", [])))

s, pend = call("/api/permission/pending", token=MGR[0])
ck("主管看到待我审批", s == 200 and any(i["id"] == rid for i in pend.get("items", [])),
   [i["id"] for i in pend.get("items", [])])

s, pend_emp = call("/api/permission/pending", token=EMP[0])
ck("员工看不到待审批列表", s == 200 and pend_emp.get("items") == [])

s, d = call(f"/api/permission/{rid}/approve", "POST", token=CROSS_MGR[0])
ck("跨事业部主管无权审批", s == 403, s)

s, d = call(f"/api/permission/{rid}/approve", "POST", token=MGR[0])
ck("主管审批通过并自动开通", s == 200, f"{s} {d}")
s, d = call("/api/auth/me", token=EMP[0])
ck("员工权限立即生效", target in (d.get("accessible_depts") or []), d.get("accessible_depts"))

s, d = call("/api/permission/request", "POST", token=EMP[0],
            body={"department_id": target, "reason": "已有权限再申请"})
ck("已有权限不可重复申请", s == 400, s)

log("-" * 60)
log("二、撤回申请（新建一个 pending 后撤回）")
log("=" * 60)
target2 = None
owned = set((call("/api/auth/me", token=EMP[0])[1]).get("accessible_depts") or [])
for d_ in dept_list:
    if d_["id"] not in owned and d_["id"] != "public" and d_["id"] != target:
        target2 = d_["id"]
        break
if target2:
    s, d = call("/api/permission/request", "POST", token=EMP[0],
                body={"department_id": target2, "reason": "待撤回测试"})
    rid2 = d.get("request_id")
    s, d = call(f"/api/permission/{rid2}/cancel", "POST", token=EMP[0])
    ck("申请人可撤回待审批申请", s == 200 and d.get("status") == "canceled", f"{s} {d}")
    s, d = call(f"/api/permission/{rid}/approve", "POST", token=MGR[0])
    ck("已审批申请不能重复处理", s == 400, s)
else:
    log("  [SKIP] 没有可用于撤回测试的第二个部门")

log("=" * 60)
log("三、管理员权限配置（不走审批流，直接分配）")
log("=" * 60)
s, mx = call("/api/admin/grants-matrix", token=ADMIN[0])
ck("管理员可拉取全量授权矩阵", s == 200 and mx.get("users"), s)
u = next((x for x in mx.get("users", []) if x["username"] == "sales_emp"), None)
ck("矩阵区分角色自带与额外授权",
   bool(u) and "builtin_departments" in u and "granted_departments" in u
   and u["builtin_departments"] == ["public", "mkt.sales"],
   u.get("builtin_departments") if u else None)

s, d = call("/api/admin/grants-matrix", token=MGR[0])
ck("非管理员无权访问授权矩阵", s == 403, s)

s, d = call("/api/admin/grants/bulk", "POST", token=ADMIN[0],
            body={"username": "sales_emp", "departments": ["tech.rd", "hr"]})
ck("管理员批量授权", s == 200 and set(["tech.rd", "hr"]).issubset(set(d.get("granted_departments") or [])),
   f"{s} {d}")
s, me = call("/api/auth/me", token=EMP[0])
ck("批量授权立即对目标用户生效",
   set(["tech.rd", "hr"]).issubset(set(me.get("accessible_depts") or [])),
   me.get("accessible_depts"))

s, d = call("/api/admin/grants/bulk", "POST", token=ADMIN[0],
            body={"username": "sales_emp", "departments": ["tech.rd", "hr", "不存在的部门"]})
ck("非法部门 ID 被明确拒绝", s == 400 and "不存在" in str(d), f"{s} {d}")

s, d = call("/api/admin/grants/bulk", "POST", token=ADMIN[0],
            body={"username": "sales_emp", "departments": []})
ck("清空额外授权", s == 200, f"{s} {d}")
s, me = call("/api/auth/me", token=EMP[0])
ck("回收后仅剩角色自带权限",
   set(["tech.rd", "hr"]).isdisjoint(set(me.get("accessible_depts") or [])),
   me.get("accessible_depts"))

s, d = call("/api/admin/grants/bulk", "POST", token=MGR[0],
            body={"username": "sales_emp", "departments": ["dept_rd"]})
ck("非管理员不可改授权", s == 403, s)

log("=" * 60)
log("四、合规审计：筛选 / 导出 / 异常检测")
log("=" * 60)
s, d = call("/api/compliance/audit-logs?limit=5", token=CR[0])
ck("合规审核员可读日志", s == 200 and d.get("items"), s)

s, d = call("/api/compliance/audit-logs?limit=5", token=KB[0])
ck("知识审核员只读日志也被拒绝", s == 403, s)
s, d = call("/api/compliance/audit-logs?limit=5", token=EMP[0])
ck("普通员工访问审计日志被拒绝", s == 403, s)

s, d = call("/api/compliance/audit-logs?type=perm_request_create&limit=20", token=CR[0])
ck("按操作类型筛选", s == 200 and all(i["type"] == "perm_request_create" for i in d.get("items", [])),
   [i["type"] for i in d.get("items", [])])
ck("返回含 24h 高风险计数", "risk_high_24h" in d, d.get("risk_high_24h"))

s, d = call("/api/compliance/audit-logs?user_id=sales_emp&limit=50", token=CR[0])
ck("按用户筛选", s == 200 and all(i["user_id"] == "sales_emp" for i in d.get("items", [])),
   len(d.get("items", [])))

s, d = call("/api/compliance/audit-logs?since=2026-01-01&until=2030-01-01&limit=5", token=CR[0])
ck("按时间范围筛选", s == 200 and len(d.get("items", [])) > 0, len(d.get("items", [])))

s, d = call("/api/compliance/audit-logs?since=2020-01-01&until=2020-02-01", token=CR[0])
ck("历史时间范围筛出空集", s == 200 and d.get("total") == 0, d.get("total"))

s, d = call("/api/compliance/export?format=csv&limit=10", token=CR[0])
csv_ok = s == 200 and "﻿" in str(d) and "操作类型" in str(d)
ck("导出 CSV（带 BOM）", csv_ok, f"{s} len={len(str(d))}")
s, d = call("/api/compliance/export?format=json&limit=10", token=CR[0])
ck("导出 JSON", s == 200 and isinstance(d, dict) and "items" in d, s)

s, d = call("/api/compliance/export?format=csv", token=EMP[0])
ck("普通员工不能导出审计", s == 403, s)

s, d = call("/api/compliance/risk-alerts?window=86400", token=CR[0])
ck("异常检测接口可用", s == 200 and d.get("rules") and d.get("items") is not None,
   f"扫描{d.get('scanned')}条 检出{d.get('total_alerts')}项 高风险{d.get('high_alerts')}")
log(f"       规则：{'; '.join(r['code']+' '+r['name'] for r in d.get('rules', []))}")
for a in (d.get("items") or [])[:5]:
    log(f"       命中 → [{a['rule']}] {a['rule_name']} · {a.get('user_id')} · {a.get('detail','')[:70]}")

s, d = call("/api/compliance/risk-alerts", token=MGR[0])
ck("非合规角色不能看异常检测", s == 403, s)

s, d = call("/api/compliance/summary?days=7", token=CR[0])
ck("合规概览可用", s == 200 and "by_type" in d, f"总数{d.get('total')} 近7天{d.get('recent_total')}")

log("=" * 60)
log("五、新增：风险等级 / 6 条异常规则 / 合规报告")
log("=" * 60)
s, d = call("/api/compliance/audit-logs?limit=10", token=CR[0])
items = d.get("items", []) if isinstance(d, dict) else []
ck("审计记录带风险等级", s == 200 and bool(items) and all("risk_level" in i for i in items),
   [i.get("risk_level") for i in items[:3]])
ck("审计记录带追踪ID", bool(items) and all(i.get("trace_id") for i in items),
   (items[0].get("trace_id") if items else None))

s, d = call("/api/compliance/audit-logs?risk=medium&limit=20", token=CR[0])
med = d.get("items", []) if isinstance(d, dict) else []
ck("按风险等级筛选", s == 200 and all(i.get("risk_level") == "medium" for i in med),
   f"命中{d.get('total')}条")

s, d = call("/api/compliance/risk-alerts?window=604800", token=CR[0])
codes = {r["code"] for r in (d.get("rules") or [])}
ck("异常检测规则为 6 条（R1~R6）", codes == {"R1", "R2", "R3", "R4", "R5", "R6"}, sorted(codes))
ck("规则含敏感部门访问 R5 与权限变更 R6", {"R5", "R6"}.issubset(codes), sorted(codes))
ck("提交权限申请不会被误报成频繁删除（R4 修复）",
   not any(a["rule"] == "R4" and "perm" in str(a.get("detail", "")) for a in (d.get("items") or [])),
   [a["rule"] for a in (d.get("items") or [])][:6])

s, d = call("/api/compliance/export?format=csv&limit=5", token=CR[0])
ck("导出 CSV 含风险等级与追踪ID列", s == 200 and "风险等级" in str(d) and "追踪ID" in str(d), s)

s, d = call("/api/compliance/report", token=CR[0])
ck("合规报告可生成（默认当前月）", s == 200 and "total_operations" in d,
   {k: d.get(k) for k in ("month", "total_operations", "active_users")} if isinstance(d, dict) else d)
if s == 200:
    ck("报告含敏感部门访问 / 权限变更 / 风险分布",
       all(k in d for k in ("sensitive_access", "permission_changes", "risk_distribution")),
       {k: d.get(k) for k in ("sensitive_access", "permission_changes", "alerts_total", "alerts_high")})
s, d = call("/api/compliance/report?month=2026-08", token=CR[0])
ck("合规报告支持指定月份", s == 200 and d.get("month") == "2026-08", d.get("month"))
s, d = call("/api/compliance/report?month=abc", token=CR[0])
ck("非法月份格式被拒绝", s == 400, s)
s, d = call("/api/compliance/report", token=EMP[0])
ck("普通员工不能生成合规报告", s == 403, s)

log("=" * 60)
log("六、权限类型（只读 / 可下载）与授权有效期")
log("=" * 60)
owned2 = set((call("/api/auth/me", token=EMP[0])[1]).get("accessible_depts") or [])
target3 = next((x["id"] for x in dept_list
                if x["id"] not in owned2 and x["id"] != "public"), None)
if not target3:
    log("  [SKIP] 没有可用于类型/有效期测试的部门")
else:
    s, d = call("/api/permission/request", "POST", token=EMP[0],
                body={"department_id": target3, "reason": "类型与有效期测试",
                      "permission_type": "download", "days": 7})
    ck("可申请 download 类型并指定有效期", s == 200 and d.get("request_id"), f"{s} {d}")
    rid3 = d.get("request_id")
    s, mine = call("/api/permission/my", token=EMP[0])
    rec3 = next((i for i in (mine.get("items") or []) if i["id"] == rid3), None)
    ck("申请单记录权限类型", bool(rec3) and rec3.get("permission_type") == "download",
       rec3.get("permission_type") if rec3 else None)
    ck("申请单记录有效期", bool(rec3) and bool(rec3.get("expires_at")),
       rec3.get("expires_at") if rec3 else None)
    s, d = call(f"/api/permission/{rid3}/approve", "POST", token=MGR[0])
    ck("带权限类型的申请可正常审批", s == 200, f"{s} {d}")
    s, d = call("/api/permission/request", "POST", token=EMP[0],
                body={"department_id": target3, "reason": "非法类型", "permission_type": "root"})
    ck("非法权限类型被拒绝", s == 400, s)
    s, d = call("/api/permission/request", "POST", token=EMP[0],
                body={"department_id": target3, "reason": "非法天数", "days": -1})
    ck("非法有效期天数被拒绝", s == 400, s)

s, mx = call("/api/admin/grants-matrix", token=ADMIN[0])
u3 = next((x for x in (mx.get("users") or []) if x["username"] == "sales_emp"), None)
ck("授权矩阵返回授权明细 grant_meta", bool(u3) and "grant_meta" in u3,
   list((u3 or {}).get("grant_meta", {}).keys())[:3])

log("=" * 60)
log("七、已处理列表 /history")
log("=" * 60)
s, hist = call("/api/permission/history", token=MGR[0])
ck("主管可查看已处理列表", s == 200 and any(i["id"] == rid for i in (hist.get("items") or [])),
   len(hist.get("items") or []))
ck("已处理列表不含待审批单",
   all(i["status"] != "pending" for i in (hist.get("items") or [])),
   [i["status"] for i in (hist.get("items") or [])][:5])
s, hist_e = call("/api/permission/history", token=EMP[0])
ck("员工已处理列表只含自己的单",
   s == 200 and all(i.get("username") == "sales_emp" for i in (hist_e.get("items") or [])),
   len(hist_e.get("items") or []))
s, d = call("/api/permission/history", token=CR[0])
ck("审计员不参与权限流程（仅自身记录，不报错）", s == 200, s)

log("=" * 60)
log("八、授权到期自动失效（领域层直接验证，隔离数据目录）")
log("=" * 60)
try:
    import os
    import sys
    import tempfile
    import time as _t

    _tmp = tempfile.mkdtemp(prefix="perm_verify_")
    os.environ["APP_DATA_DIR"] = _tmp          # 必须放在 import 之前：模块加载时解析数据目录
    _backend = "D:/enterprise-rag-platform/backend"
    if _backend not in sys.path:
        sys.path.insert(0, _backend)
    from app.db.models import effective_grants, get_grant_meta, grant_department  # noqa: E402

    grant_department("tester", "hr")                                   # 长期有效
    grant_department("tester", "legal", expires_at=_t.time() - 1)      # 已过期
    grant_department("tester", "fin", expires_at=_t.time() + 3600)     # 未过期
    eff = effective_grants("tester")
    ck("未过期授权仍然有效", "hr" in eff and "fin" in eff, eff)
    ck("已过期授权自动失效", "legal" not in eff, eff)
    meta = get_grant_meta("tester")
    ck("授权明细记录有效期", bool(meta.get("legal", {}).get("expires_at")),
       list(meta.keys()))
    ck("长期有效授权无过期时间", meta.get("hr", {}).get("expires_at") is None,
       meta.get("hr", {}).get("expires_at"))
except Exception as e:  # noqa: BLE001
    ck("授权到期自动失效（领域层）", False, repr(e))

log("=" * 60)
log(f"结论：{'全部通过（%d 项失败）' % len(FAILS) if not FAILS else '存在失败：%s' % FAILS}")
log("=" * 60)

with open("D:/enterprise-rag-platform/_acceptance_out.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(OUT))
