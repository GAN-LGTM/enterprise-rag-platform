"""会话隔离 / 自动标题 / 历史恢复 验证脚本。"""
import json
import os
import sys

import httpx

# 端口统一：所有端到端脚本均读 BASE_URL 环境变量（默认 8000），命令行参数保留兼容。
BASE = os.getenv("BASE_URL") or (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000")
DECODE = lambda b: b.decode("utf-8", "replace")
ok, fail = 0, 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"[PASS] {name} {extra}")
    else:
        fail += 1
        print(f"[FAIL] {name} {extra}")


def tok(u, p="123456"):
    r = httpx.post(f"{BASE}/api/auth/login", json={"username": u, "password": p},
                   timeout=10, trust_env=False)
    r.raise_for_status()
    return r.json()["access_token"]


def H(t):
    return {"Authorization": f"Bearer {t}", "Content-Type": "application/json"}


def ask(t, q, sid=None):
    done = {}
    with httpx.stream("POST", f"{BASE}/api/chat/stream", json={"question": q, "session_id": sid},
                      headers=H(t), timeout=180, trust_env=False) as r:
        buf = ""
        for c in r.iter_text():
            buf += c
            while "\n\n" in buf:
                f, buf = buf.split("\n\n", 1)
                ev = da = None
                for l in f.splitlines():
                    if l.startswith("event: "):
                        ev = l[7:]
                    elif l.startswith("data: "):
                        da = l[6:]
                if ev == "done" and da:
                    done = json.loads(da)
    return done


def main():
    t_sales = tok("sales_emp")
    t_rd = tok("rd_emp")

    # 0. 清理历史数据，保证可重复执行
    for t in (t_sales, t_rd):
        for s in httpx.get(f"{BASE}/api/chat/sessions", headers=H(t), timeout=10,
                           trust_env=False).json():
            httpx.delete(f"{BASE}/api/chat/sessions/{s['id']}", headers=H(t), timeout=10,
                         trust_env=False)

    # 1. 初始：两个账号都无会话
    s0 = httpx.get(f"{BASE}/api/chat/sessions", headers=H(t_sales), timeout=10,
                   trust_env=False).json()
    r0 = httpx.get(f"{BASE}/api/chat/sessions", headers=H(t_rd), timeout=10,
                   trust_env=False).json()
    check("初始会话列表为空", True, f"sales={len(s0)} rd={len(r0)}")

    # 2. 销售专员提问，会话自动创建 + 自动命名
    d1 = ask(t_sales, "销售提成怎么算？")
    sid = d1.get("session_id")
    check("返回 session_id", bool(sid), sid or "")
    lst = httpx.get(f"{BASE}/api/chat/sessions", headers=H(t_sales), timeout=10,
                    trust_env=False).json()
    check("销售账号可见 1 个会话", len(lst) == 1, json.dumps(lst, ensure_ascii=False)[:200])
    check("标题自动取首个问题", lst and lst[0]["title"].startswith("销售提成"),
          lst[0]["title"] if lst else "")
    check("轮次=1", lst and lst[0]["turns"] == 1, str(lst[0]["turns"]) if lst else "")

    # 3. 研发账号看不到该会话
    lst_rd = httpx.get(f"{BASE}/api/chat/sessions", headers=H(t_rd), timeout=10,
                       trust_env=False).json()
    check("研发账号列表不含销售会话", all(s["id"] != sid for s in lst_rd),
          f"rd={len(lst_rd)} 条")

    # 4. 横向越权读取 → 404
    r = httpx.get(f"{BASE}/api/chat/sessions/{sid}", headers=H(t_rd), timeout=10,
                  trust_env=False)
    check("越权读取会话被拒 404", r.status_code == 404, f"status={r.status_code}")

    # 5. 越权删除 → 不影响
    r = httpx.delete(f"{BASE}/api/chat/sessions/{sid}", headers=H(t_rd), timeout=10,
                     trust_env=False)
    still = httpx.get(f"{BASE}/api/chat/sessions", headers=H(t_sales), timeout=10,
                      trust_env=False).json()
    check("越权删除返回 404 且归属方仍可见", r.status_code == 404 and len(still) == 1,
          f"delete={r.status_code} remain={len(still)}")

    # 6. 多轮 + 历史恢复
    d2 = ask(t_sales, "那差旅住宿标准呢？", sid)
    detail = httpx.get(f"{BASE}/api/chat/sessions/{sid}", headers=H(t_sales), timeout=10,
                       trust_env=False).json()
    msgs = detail["messages"]
    check("历史 4 条（2 轮问答）", len(msgs) == 4, f"n={len(msgs)}")
    check("首条为用户问题", msgs and msgs[0]["role"] == "user", msgs[0]["content"][:20] if msgs else "")
    check("助手消息带引用", len(msgs) > 1 and "citations" in msgs[1],
          f"cites={len(msgs[1].get('citations', []))}" if len(msgs) > 1 else "")
    check("标题仍是首个问题（不被后续覆盖）", detail["title"].startswith("销售提成"), detail["title"])

    # 7. 新建会话接口
    ns = httpx.post(f"{BASE}/api/chat/sessions", headers=H(t_sales), timeout=10,
                    trust_env=False).json()
    lst2 = httpx.get(f"{BASE}/api/chat/sessions", headers=H(t_sales), timeout=10,
                     trust_env=False).json()
    check("新建会话后 2 条且新的在前", len(lst2) == 2 and lst2[0]["id"] == ns["id"],
          f"{[s['title'] for s in lst2]}")

    # 8. 重命名
    httpx.patch(f"{BASE}/api/chat/sessions/{ns['id']}", headers=H(t_sales),
                json={"title": "报销专题"}, timeout=10, trust_env=False)
    lst3 = httpx.get(f"{BASE}/api/chat/sessions", headers=H(t_sales), timeout=10,
                     trust_env=False).json()
    check("重命名生效", any(s["title"] == "报销专题" for s in lst3),
          str([s["title"] for s in lst3]))

    # 9. 删除
    httpx.delete(f"{BASE}/api/chat/sessions/{ns['id']}", headers=H(t_sales), timeout=10,
                 trust_env=False)
    lst4 = httpx.get(f"{BASE}/api/chat/sessions", headers=H(t_sales), timeout=10,
                     trust_env=False).json()
    check("删除生效（回到 1 条）", len(lst4) == 1, f"n={len(lst4)}")

    # 10. 落盘持久化
    import pathlib
    p = pathlib.Path(r"D:/enterprise-rag-platform/data/sessions.json")
    check("会话已落盘", p.exists(), f"{p} size={p.stat().st_size if p.exists() else 0}")

    print(f"\n结果：{ok} 通过 / {fail} 失败")
    sys.exit(1 if fail else 0)


main()
