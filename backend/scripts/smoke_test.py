"""
端到端冒烟测试 —— 验证 SSE 流式输出、权限隔离、缓存命中三条主链路。

用法（服务已启动）：
    python backend/scripts/smoke_test.py
"""
from __future__ import annotations

import json
import sys

import os

import httpx

BASE = os.getenv("BASE_URL", "http://127.0.0.1:8000")

# 管理员口令：演示模式默认 admin123；交付环境用 ADMIN_PASSWORD 传入（初始口令由交付方持有）
ADMIN_PWD = os.getenv("ADMIN_PASSWORD", "admin123")


def login(username: str, password: str = "123456") -> str:
    r = httpx.post(f"{BASE}/api/auth/login",
                   json={"username": username, "password": password}, timeout=10, trust_env=False)
    r.raise_for_status()
    return r.json()["access_token"]


def stream(token: str, question: str, session_id: str | None = None) -> dict:
    """消费 SSE 流，统计各类事件。"""
    stats = {"meta": 0, "stage": [], "token": 0, "citation": 0, "done": None, "error": None}
    body = {"question": question}
    if session_id:
        body["session_id"] = session_id

    with httpx.stream("POST", f"{BASE}/api/chat/stream", json=body,
                      headers={"Authorization": f"Bearer {token}"},
                      timeout=60, trust_env=False) as resp:
        resp.raise_for_status()
        print(f"   Content-Type: {resp.headers.get('content-type')}")
        buf = ""
        for chunk in resp.iter_text():
            buf += chunk
            while "\n\n" in buf:
                frame, buf = buf.split("\n\n", 1)
                event, data = None, None
                for line in frame.splitlines():
                    if line.startswith("event: "):
                        event = line[7:]
                    elif line.startswith("data: "):
                        data = line[6:]
                if not event:
                    continue
                if event == "token":
                    stats["token"] += 1
                elif event == "stage":
                    d = json.loads(data)
                    stats["stage"].append(d.get("node"))
                elif event == "meta":
                    stats["meta"] = json.loads(data)
                elif event == "citation":
                    stats["citation"] = len(json.loads(data).get("citations", []))
                elif event == "done":
                    stats["done"] = json.loads(data)
                elif event == "error":
                    stats["error"] = json.loads(data)
    return stats


def check(name: str, cond: bool, extra: str = ""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name} {extra}")
    return cond


def main() -> int:
    ok = True
    print("=" * 70)
    print("1) 登录与健康检查")
    print("=" * 70)
    h = httpx.get(f"{BASE}/api/health", timeout=10, trust_env=False).json()
    print("   health:", h["status"], [(c["name"], c["status"]) for c in h["components"]])

    t_sales = login("sales_emp")
    t_admin = login("admin", ADMIN_PWD)
    print("   登录 OK: sales_emp / admin")

    print()
    print("=" * 70)
    print("2) 公共知识库问答（SSE 流式）")
    print("=" * 70)
    r1 = stream(t_sales, "差旅住宿的报销标准是多少？")
    print("   stage:", r1["stage"])
    print("   token 帧数:", r1["token"], " 引用数:", r1["citation"])
    d = r1["done"] or {}
    print("   意图:", d.get("intent"), " 缓存:", d.get("cache_hit"), " 耗时:", d.get("timings"))
    print("   答案前 120 字:", (d.get("answer") or "")[:120].replace("\n", " "))
    ok &= check("收到 meta 帧", bool(r1["meta"]))
    ok &= check("收到 token 流（>50 帧）", r1["token"] > 50, f"实际 {r1['token']}")
    ok &= check("走完 4 个节点", set(["classify_intent", "permission_gate", "retrieve", "generate"]).issubset(set(r1["stage"])))
    ok &= check("意图识别为 public_kb", d.get("intent") == "public_kb", str(d.get("intent")))

    print()
    print("=" * 70)
    print("3) 缓存命中（同一问题再问一次）")
    print("=" * 70)
    r2 = stream(t_sales, "差旅住宿的报销标准是多少？", session_id="s1")
    d2 = r2["done"] or {}
    print("   缓存命中:", d2.get("cache_hit"), " 耗时:", d2.get("timings", {}).get("__total__"))
    ok &= check("第二次命中缓存", bool(d2.get("cache_hit")))

    print()
    print("=" * 70)
    print("4) 跨部门越权拦截（销售专员查研发部文档）")
    print("=" * 70)
    r3 = stream(t_sales, "研发部的编码规范是什么？")
    d3 = r3["done"] or {}
    print("   意图:", d3.get("intent"), " 节点:", r3["stage"])
    print("   答案:", (d3.get("answer") or "")[:80])
    ok &= check("意图识别为 cross_dept", d3.get("intent") == "cross_dept", str(d3.get("intent")))
    ok &= check("被权限门拦下（走 deny 节点）", "deny" in r3["stage"])
    ok &= check("无引用返回（不触达目标向量表）", r3["citation"] == 0, str(r3["citation"]))
    ok &= check("给出友好拒答", "没有访问该知识的权限" in (d3.get("answer") or ""))

    print()
    print("=" * 70)
    print("5) 管理员跨部门查询（应当放行）")
    print("=" * 70)
    r4 = stream(t_admin, "研发部的编码规范是什么？")
    d4 = r4["done"] or {}
    print("   节点:", r4["stage"], " 引用:", r4["citation"])
    print("   答案前 100 字:", (d4.get("answer") or "")[:100].replace("\n", " "))
    ok &= check("管理员放行检索", "retrieve" in r4["stage"] and "deny" not in r4["stage"])
    ok &= check("召回到研发部文档", r4["citation"] > 0, str(r4["citation"]))

    print()
    print("=" * 70)
    print("6) 本部门隔离（销售专员查自己部门）")
    print("=" * 70)
    r5 = stream(t_sales, "销售提成怎么算？")
    d5 = r5["done"] or {}
    print("   意图:", d5.get("intent"), " dept_ids:", d5.get("dept_ids"))
    ok &= check("限定在本部门+公共库", set(d5.get("dept_ids", [])) == {"mkt.sales", "public"},
                str(d5.get("dept_ids")))

    print()
    print("=" * 70)
    print(f"结果: {'全部通过' if ok else '存在失败项'}")
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
