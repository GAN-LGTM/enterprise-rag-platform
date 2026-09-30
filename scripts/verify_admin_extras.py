# -*- coding: utf-8 -*-
"""交付打磨项专项验收：健康检查 / 用户管理 / 部门管理 / 系统配置 / 备份 / 通知中心。

覆盖本轮新增的 P0/P1 能力（对应交付清单 1-12）：
  1. 健康检查：状态不矛盾（degraded 必有 notes）、含模型清单
  2. 用户管理：新增账号 → 新账号登录（强制改密）→ 重置密码 → 调角色 → 停用（登录被拒）→ 启用
  3. 越权拦截：非管理员访问用户管理接口 403
  4. 部门管理：组织树含成员分布
  5. 系统配置：只读配置 + 手动备份 + 备份历史
  6. 通知中心：权限审批通过/驳回自动生成通知、未读数、标记已读

幂等：可重复执行；新建的测试账号 test_emp01 每次先停用清理再重建。
执行前确保服务已启动（start.bat）。
"""
from __future__ import annotations

import os

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8000"

# 管理员口令：演示模式默认 admin123；交付环境用 ADMIN_PASSWORD 传入（初始口令由交付方持有）
ADMIN_PWD = os.getenv("ADMIN_PASSWORD", "admin123")
FAILS: list[str] = []
_passed = 0


def log(msg: str) -> None:
    print(msg, flush=True)


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        log(f"  [PASS] {name}")
    else:
        FAILS.append(f"{name} {detail}")
        log(f"  [FAIL] {name} {detail}")


def req(method: str, path: str, token: str | None = None, body: dict | None = None,
        expect: int = 200):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        resp = opener.open(r, timeout=30)
        payload = json.loads(resp.read().decode("utf-8"))
        return resp.status, payload
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            payload = {}
        return e.code, payload


def login(username: str, password: str) -> str:
    st, d = req("POST", "/api/auth/login", body={"username": username, "password": password})
    if st != 200:
        raise RuntimeError(f"登录失败 {username}: {st} {d}")
    return d["access_token"]


def main() -> int:
    log("=" * 60)
    log("  交付打磨项专项验收（健康/用户/部门/配置/备份/通知）")
    log("=" * 60)

    admin = login("admin", ADMIN_PWD)
    emp = login("sales_emp", "123456")

    # ---------- 1. 健康检查 ----------
    log("\n[1] 健康检查：状态一致 + 模型清单")
    st, h = req("GET", "/api/health")
    check("health 200", st == 200)
    check("degraded/down 必有 notes 说明原因",
          h.get("status") == "ok" or len(h.get("notes", [])) > 0,
          f"status={h.get('status')} notes={h.get('notes')}")
    check("ok 状态时不应有未解释降级", h.get("status") != "ok" or not h.get("notes"))
    models = h.get("models") or {}
    check("含 LLM 模型信息", bool(models.get("llm", {}).get("model")))
    check("含 Embedding 模型信息", bool(models.get("embedding", {}).get("model")))
    check("含 OCR 信息", "ocr" in models)

    # ---------- 2. 用户管理 ----------
    log("\n[2] 用户管理：新增 → 登录 → 重置 → 调角色 → 停用/启用")
    st, d = req("GET", "/api/admin/users", admin)
    check("用户列表 200", st == 200 and isinstance(d.get("items"), list))
    check("返回角色字典", bool(d.get("roles")))

    # 清理上次残留（若存在则先确保账号可用密码已知：直接重置密码）
    exists = any(u["username"] == "test_emp01" for u in d.get("items", []))
    if exists:
        req("POST", "/api/admin/users/test_emp01/reset-password", admin, {"password": "Test@12345"})
        req("POST", "/api/admin/users/test_emp01/status", admin, {"disabled": False})
    else:
        st, d2 = req("POST", "/api/admin/users", admin, {
            "username": "test_emp01", "display_name": "验收测试员",
            "password": "Test@12345", "role": "sub_employee", "dept_id": "mkt.sales"})
        check("新增账号", st == 200 and d2.get("ok"), f"st={st} {d2}")

    st, d = req("GET", "/api/admin/users", admin)
    t = next((u for u in d.get("items", []) if u["username"] == "test_emp01"), None)
    check("新账号在列表中且标记 custom", bool(t) and t.get("source") == "custom")

    # 新账号登录（应强制改密）
    st, d = req("POST", "/api/auth/login", body={"username": "test_emp01", "password": "Test@12345"})
    check("新账号可登录", st == 200)
    check("首次登录强制改密", d.get("user", {}).get("must_change_password") is True)
    t1 = d.get("access_token", "")

    # 管理员重置密码
    st, d = req("POST", "/api/admin/users/test_emp01/reset-password", admin, {"password": "Test@67890"})
    check("重置密码", st == 200 and d.get("ok"))
    st, d = req("POST", "/api/auth/login", body={"username": "test_emp01", "password": "Test@67890"})
    check("重置后新密码可登录", st == 200)
    t1 = d.get("access_token", t1)

    # 调角色
    st, d = req("POST", "/api/admin/users/test_emp01/role", admin, {"role": "knowledge_reviewer"})
    check("调整角色", st == 200 and d.get("role") == "knowledge_reviewer")

    # 停用 → 登录被拒；启用 → 恢复
    st, d = req("POST", "/api/admin/users/test_emp01/status", admin, {"disabled": True})
    check("停用账号", st == 200 and d.get("disabled") is True)
    st, d = req("POST", "/api/auth/login", body={"username": "test_emp01", "password": "Test@67890"})
    check("停用后登录被拒且提示停用", st in (401, 403) and "停用" in json.dumps(d, ensure_ascii=False),
          f"st={st} {d}")
    st, d = req("POST", "/api/admin/users/test_emp01/status", admin, {"disabled": False})
    check("启用账号", st == 200 and d.get("disabled") is False)

    # 保护：不能停用自己 / 不能取消自己 admin
    st, d = req("POST", "/api/admin/users/admin/status", admin, {"disabled": True})
    check("不能停用自己", st == 400)
    st, d = req("POST", "/api/admin/users/admin/role", admin, {"role": "sub_employee"})
    check("不能取消自己的管理员角色", st == 400)

    # 非法输入
    st, d = req("POST", "/api/admin/users", admin, {
        "username": "x", "display_name": "短", "password": "Test@12345",
        "role": "sub_employee", "dept_id": "mkt.sales"})
    check("过短账号被拒", st == 400)
    st, d = req("POST", "/api/admin/users", admin, {
        "username": "test_emp01", "display_name": "重复", "password": "Test@12345",
        "role": "sub_employee", "dept_id": "mkt.sales"})
    check("重复账号被拒", st == 400)

    # ---------- 3. 越权拦截 ----------
    log("\n[3] 越权拦截：非管理员访问管理接口")
    st, _ = req("GET", "/api/admin/users", emp)
    check("员工访问用户管理 403", st == 403)
    st, _ = req("POST", "/api/admin/backup", emp, {})
    check("员工触发备份 403", st == 403)
    st, _ = req("GET", "/api/admin/system-config", emp)
    check("员工看系统配置 403", st == 403)

    # ---------- 4. 部门管理 ----------
    log("\n[4] 部门管理：组织树与成员分布")
    st, d = req("GET", "/api/admin/org-tree", admin)
    check("组织树 200", st == 200 and len(d.get("items", [])) >= 10)
    fin = next((x for x in d.get("items", []) if x["id"] == "fin.general"), None)
    check("财务综合含成员", bool(fin) and fin["member_count"] >= 1)

    # ---------- 5. 系统配置 + 备份 ----------
    log("\n[5] 系统配置（只读）+ 数据备份")
    st, c = req("GET", "/api/admin/system-config", admin)
    check("配置 200 且含 LLM 段", st == 200 and bool(c.get("llm", {}).get("model")))
    check("含敏感部门配置", "sensitive_depts" in c)
    st, b = req("POST", "/api/admin/backup", admin, {})
    check("手动备份", st == 200 and b.get("ok") and b.get("files", 0) > 0, f"st={st} {b}")
    st, bl = req("GET", "/api/admin/backups", admin)
    check("备份历史含新文件", st == 200 and any(x["file"] == b.get("file") for x in bl.get("items", [])))

    # ---------- 6. 通知中心 ----------
    log("\n[6] 通知中心：权限审批自动生成通知")
    # 先清掉 rd_emp 可能的遗留：直接造一条新申请（tech.qa 对 rd_emp 无权限）
    rd = login("rd_emp", "123456")
    st, before = req("GET", "/api/notifications", rd)
    check("通知接口 200", st == 200 and "unread" in before)
    unread_before = before.get("unread", 0)

    st, d = req("POST", "/api/permission/request", rd,
                {"department_id": "tech.qa", "reason": "验收：需要查阅测试环境规范", "permission_type": "read"})
    if st == 200:
        rid = d["request_id"]
        st, d = req("POST", f"/api/permission/{rid}/approve", admin, {})
        check("管理员审批通过", st == 200)
        st, after = req("GET", "/api/notifications", rd)
        check("审批后未读数 +1", after.get("unread", 0) == unread_before + 1,
              f"before={unread_before} after={after.get('unread')}")
        items = after.get("items", [])
        latest = items[0] if items else {}
        check("通知标题为权限申请已通过", "通过" in (latest.get("title") or ""), f"latest={latest.get('title')}")
        st, d = req("POST", "/api/notifications/read", rd, {"ids": [latest.get("id")]})
        check("单条标记已读", st == 200 and d.get("marked", 0) >= 1)
        st, d = req("POST", "/api/notifications/read", rd, {})
        check("全部标记已读", st == 200)
        st, after2 = req("GET", "/api/notifications", rd)
        check("已读后未读数归零", after2.get("unread") == 0)
        # 清理：回收这次授权，避免污染环境
        req("POST", "/api/admin/grants/bulk", admin, {"username": "rd_emp", "departments": []})
    else:
        # 已有同部门 pending 申请（上次验收残留）：驳回路径同样应产生通知
        st, pend = req("GET", "/api/permission/pending", admin)
        rid = next((r["id"] for r in pend.get("items", [])
                    if r["username"] == "rd_emp" and r["department_id"] == "tech.qa"), None)
        check("找到残留申请走驳回路径", rid is not None)
        if rid:
            st, d = req("POST", f"/api/permission/{rid}/reject", admin, {"note": "验收清理"})
            check("驳回申请", st == 200)
            st, after = req("GET", "/api/notifications", rd)
            check("驳回后产生通知", after.get("unread", 0) >= unread_before + 1)
            req("POST", "/api/notifications/read", rd, {})

    log("\n" + "=" * 60)
    log(f"结论：{'全部通过' if not FAILS else '存在失败'}（{_passed} 项通过，{len(FAILS)} 项失败）")
    if FAILS:
        for f in FAILS:
            log(f"  失败项：{f}")
    log("=" * 60)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
