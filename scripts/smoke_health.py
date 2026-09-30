"""轻量冒烟探针：仅验证服务连通性与关键接口，不写任何业务数据。"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"

# 管理员口令：演示模式默认 admin123；交付环境用 ADMIN_PASSWORD 传入（初始口令由交付方持有）
ADMIN_PWD = os.getenv("ADMIN_PASSWORD", "admin123")
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 绕过系统代理


def get(path, token=None):
    req = urllib.request.Request(BASE + path)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")


def main():
    lines = []
    code, body = get("/api/health")
    lines.append(f"[health] HTTP {code} {body[:120]}")

    # 用演示账号登录，验证鉴权链路正常
    payload = json.dumps(
        {"username": "admin", "password": ADMIN_PWD},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        BASE + "/api/auth/login",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener.open(req, timeout=10) as r:
            tok = json.loads(r.read().decode("utf-8"))
        tk = tok.get("access_token", "") if isinstance(tok, dict) else ""
        lines.append(f"[login] HTTP {r.status} token_len={len(tk)}")
    except urllib.error.HTTPError as e:
        tk = ""
        lines.append(f"[login] HTTP {e.code} {e.read().decode('utf-8', 'ignore')[:200]}")

    for path in ("/api/compliance/event-types", "/api/admin/grants-matrix"):
        code, body = get(path, tk)
        try:
            n = len(json.loads(body).get("users", json.loads(body).get("types", [])))
        except Exception:
            n = body[:60]
        lines.append(f"[get {path}] HTTP {code} items={n}")

    code, body = get("/api/compliance/audit-logs?limit=3", tk)
    try:
        d = json.loads(body)
        total = d.get("total", len(d.get("items", [])))
    except Exception:
        total = body[:60]
    lines.append(f"[audit-logs] HTTP {code} total={total}")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
