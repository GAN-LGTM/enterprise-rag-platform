"""
请假工作流端到端验证（仅用标准库，连真实 HTTP 服务）。

流程：sales_emp 在聊天里走完请假引导 → 提交 → mkt_director 审批通过
      → sales_emp 读取未读通知（审批通过提示）。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

BASE = os.getenv("BASE_URL", "http://127.0.0.1:8000")


def _req(path, token, body, method="POST"):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def login(user):
    return _req("/api/auth/login", None, {"username": user, "password": "123456"})["access_token"]


def drop_draft(token):
    """清空演示账号的遗留草稿，保证脚本可重复执行。

    背景：只要存在进行中的草稿，`/api/chat/stream` 就会把每条消息都当成对该草稿的填空，
    首问「我想请假」不会重新发起流程。以前脚本跑第二遍必挂，必须先重置。
    """
    req = urllib.request.Request(
        BASE + "/api/leave/draft", method="DELETE",
        headers={"Authorization": "Bearer " + token},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def chat(token, q, sid):
    req = urllib.request.Request(
        BASE + "/api/chat/stream",
        data=json.dumps({"question": q, "session_id": sid}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode("utf-8")
    answer = ""
    for frame in raw.split("\n\n"):
        if "event: done" in frame:
            for line in frame.split("\n"):
                if line.startswith("data: "):
                    answer = json.loads(line[6:]).get("answer", "")
    return answer


def check(name, cond):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        print("   >>> 失败内容不足，终止")
        sys.exit(1)


sid = "e2e-leave-1"
tok = login("sales_emp")
drop_draft(tok)

a1 = chat(tok, "我想请假", sid)
check("E2E 首问含请假类型", "请假类型" in a1)

a2 = chat(tok, "病假", sid)
check("E2E 追问开始时间", "开始时间" in a2)

a3 = chat(tok, "2026-09-24 09:00", sid)
check("E2E 追问结束时间", "结束时间" in a3)

a4 = chat(tok, "2026-09-25 18:00", sid)
check("E2E 追问请假事由", "请假事由" in a4)

a5 = chat(tok, "感冒就医需要休息", sid)
check("E2E 追问工作交接人", "工作交接人" in a5)

a6 = chat(tok, "无", sid)
check("E2E 汇总请核对", "请核对" in a6)

a7 = chat(tok, "确认", sid)
check("E2E 提交生成钉钉实例", "DINGMOCK-" in a7)
check("E2E 提交提示审批流转", "审批" in a7)
check("E2E 提交提示含当前审批人", "营销总监-李娜" in a7)

# 进度查询：问"请假进度怎么样"应直接返回当前进度，不重启流程
a8 = chat(tok, "请假进度怎么样？", sid)
check("E2E 进度查询不误触发新流程", "请假类型" not in a8)
check("E2E 进度查询显示审批中", "正在审批中" in a8)
check("E2E 进度查询显示当前审批人", "营销总监-李娜" in a8)

# 审批人视角
atok = login("mkt_director")
pending = _req("/api/leave/pending/me", atok, None, "GET")
check("E2E 审批人看到待办", len(pending) >= 1)
rid = pending[0]["id"]
appr = _req(f"/api/leave/{rid}/approve", atok, {"comment": "同意"})
check("E2E 审批通过→approved", appr["status"] == "approved")

# 申请人读取未读通知
note = _req("/api/leave/notify", tok, None, "GET")
check("E2E 申请人收到审批通过通知", any(e["status"] == "approved" for e in note["events"]))

# 我的请假列表可见进度
my = _req("/api/leave/my", tok, None, "GET")
check("E2E 我的请假含已通过单", any(r["status"] == "approved" for r in my))

print("\n✅ 请假工作流端到端验证通过")
