"""
检索质量评测（企业级交付佐证）：黄金问答集回归。

验证两类质量指标：
  1) 召回正确性 —— 命中题的引用/答案必须包含期望关键词（证明检索找对了文档）
  2) 权限正确性 —— 越权题的引用绝不能来自禁入部门（公共库对全员可见是正确行为；
     权限不变量 = 引用来源 ⊆ 用户可访问部门）

用法：先启动服务（默认 http://127.0.0.1:8000，可用 BASE_URL 覆盖），再运行本脚本：
  python backend/scripts/eval_quality.py
退出码：全部通过=0；有失败=1（可接入 CI 做质量门禁）。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

BASE = os.getenv("BASE_URL", "http://127.0.0.1:8000")

# 管理员口令：演示模式默认 admin123；交付环境用 ADMIN_PASSWORD 传入（初始口令由交付方持有）
ADMIN_PWD = os.getenv("ADMIN_PASSWORD", "admin123")


def _post(path, token, body):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def login(user, pwd="123456"):
    if user == "admin":
        pwd = ADMIN_PWD
    return _post("/api/auth/login", None, {"username": user, "password": pwd})["access_token"]


def chat(token, q, sid):
    """返回 (answer, citations)。citations 供召回关键词/权限断言。"""
    req = urllib.request.Request(
        BASE + "/api/chat/stream",
        data=json.dumps({"question": q, "session_id": sid}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        raw = r.read().decode("utf-8")
    answer, cites = "", []
    for frame in raw.split("\n\n"):
        if "event: done" in frame:
            for line in frame.split("\n"):
                if line.startswith("data: "):
                    d = json.loads(line[6:])
                    answer = d.get("answer", "")
                    cites = d.get("citations", [])
    return answer, cites


def me(token):
    req = urllib.request.Request(BASE + "/api/auth/me",
                                 headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _cite_text(c):
    return (c.get("chunk_text") or c.get("text") or "") + (c.get("doc_name") or "")


# ============================================================
#  黄金问答集
#  召回题: (user, question, 期望关键词, None)      —— 引用/答案须含关键词
#  越权题: (user, question, [], 禁入部门id)        —— 引用绝不能来自禁入部门
# ============================================================
CASES = [
    # ---- 召回正确性 ----
    ("sales_emp", "销售提成怎么算？", ["提成"], None),
    ("sales_emp", "差旅住宿的报销标准是多少？", ["报销", "住宿"], None),
    ("hr_emp", "人力资源部有多少人？", ["38"], None),
    ("hr_emp", "公司的考勤制度是什么？", ["考勤"], None),
    ("rd_emp", "研发部的编码规范是什么？", ["编码"], None),
    ("fin_mgr", "报销的审批流程是怎样的？", ["报销"], None),
    ("admin", "2024年微短剧市场规模是多少？", ["微短剧"], None),
    # ---- 权限正确性（引用不越权）----
    ("sales_emp", "研发部的编码规范是什么？", [], "tech.rd"),
    ("rd_emp", "销售提成怎么算？", [], "mkt.sales"),
    ("rd_emp", "人力资源部有多少人？", [], "hr"),
    ("legal_emp", "研发部编码规范是什么？", [], "tech.rd"),
]


def main():
    tokens, profiles = {}, {}
    passed, failed = 0, 0
    hits_ok = hits_total = leak_ok = leak_total = 0
    print("=" * 72)
    print("检索质量评测（黄金问答集回归）")
    print("=" * 72)

    for user, q, keywords, forbidden_dept in CASES:
        if user not in tokens:
            tokens[user] = login(user)
            profiles[user] = set(me(tokens[user]).get("accessible_depts", []))
        sid = f"eval-{user}"
        try:
            answer, cites = chat(tokens[user], q, sid)
        except Exception as e:  # noqa: BLE001
            print(f"[FAIL] {user} | {q} | 请求异常: {e}")
            failed += 1
            continue

        if forbidden_dept:
            leak_total += 1
            bad = [c.get("department_id") for c in cites
                   if c.get("department_id") not in profiles[user]]
            ok = not bad
            if bad:
                print(f"   !!! 权限泄露：引用来自无权部门 {bad}")
            tag = "无越权" if ok else "越权泄露!"
            leak_ok += ok
        else:
            hits_total += 1
            blob = answer + " ".join(_cite_text(c) for c in cites)
            ok = any(k in blob for k in keywords) if keywords else True
            tag = "命中" if ok else "未命中!"
            hits_ok += ok

        passed += ok
        failed += (not ok)
        preview = (answer or "").replace("\n", " ")[:42]
        print(f"[{'PASS' if ok else 'FAIL'}] {user:<10} | {q:<24} | {tag} | {preview}")

    print("-" * 72)
    print(f"总计: {passed}/{passed + failed} 通过 | 召回: {hits_ok}/{hits_total} | 越权防护: {leak_ok}/{leak_total}")
    if failed:
        print("❌ 存在未通过用例")
        sys.exit(1)
    print("✅ 检索质量评测全部通过")


if __name__ == "__main__":
    main()
