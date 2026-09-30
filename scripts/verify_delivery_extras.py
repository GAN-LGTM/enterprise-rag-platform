"""交付级打磨 —— 第二批验收（数据看板 / 模型管理 / PDF 合规报告 / 帮助中心 / 版本按钮）。

覆盖评审清单里第二批补做的功能，幂等可重复执行：
  1. 数据看板接口（DAU/MAU、趋势、知识增长）
  2. 模型管理（清单 + 主备热切换 + 切换后还原）
  3. 合规报告 PDF 导出（全量 / 本月 / 本季度）
  4. 原有 CSV / JSON 导出未被破坏
  5. 越权拦截：非管理员不得访问 analytics / models / 切换模型 / PDF 导出

用法：先启动服务（python -m uvicorn app.main:app --port 8000），再 python scripts/verify_delivery_extras.py
"""
from __future__ import annotations

import os

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"

# 管理员口令：演示模式默认 admin123；交付环境用 ADMIN_PASSWORD 传入（初始口令由交付方持有）
ADMIN_PWD = os.getenv("ADMIN_PASSWORD", "admin123")
PASS = FAIL = 0


def call(path: str, token: str | None = None, method: str = "GET", body: dict | None = None,
         raw: bool = False):
    req = urllib.request.Request(BASE + path, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, data=data, timeout=60) as r:
            if raw:
                return r.status, r.read(), r.headers.get("Content-Type", "")
            return r.status, json.loads(r.read() or b"{}"), ""
    except urllib.error.HTTPError as e:
        if raw:
            return e.code, e.read(), ""
        try:
            return e.code, json.loads(e.read() or b"{}"), ""
        except Exception:  # noqa: BLE001
            return e.code, {}, ""


def check(name: str, cond: bool, extra: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {extra}")
    return cond


def login(username: str, password: str) -> str | None:
    st, d, _ = call("/api/auth/login", method="POST",
                    body={"username": username, "password": password})
    return d.get("access_token") if st == 200 else None


def main() -> int:
    print("=" * 60)
    print("交付级打磨 · 第二批验收（数据看板 / 模型管理 / PDF 报告）")
    print("=" * 60)

    admin = login("admin", ADMIN_PWD)
    if not check("管理员登录", bool(admin)):
        return 1
    emp = login("sales_emp", "123456")

    # ---------- 1. 数据看板 ----------
    print("\n[1] 数据看板 /api/admin/analytics")
    st, d, _ = call("/api/admin/analytics?days=14", admin)
    check("接口可用（200）", st == 200, f"status={st}")
    check("返回 DAU 字段且为数字", isinstance(d.get("dau"), int))
    check("返回 MAU 字段且为数字", isinstance(d.get("mau"), int))
    check("MAU >= DAU（口径自洽）", d.get("mau", 0) >= d.get("dau", 0),
          f"dau={d.get('dau')} mau={d.get('mau')}")
    trend = d.get("trend") or []
    check("趋势点数 = 请求天数（空档补齐）", len(trend) == 14, f"len={len(trend)}")
    check("趋势点含 questions/active_users/date",
          all({"questions", "active_users", "date"} <= set(t) for t in trend))
    check("文档总数 > 0", (d.get("docs_total") or 0) > 0, f"docs_total={d.get('docs_total')}")
    check("文档趋势点数 = 请求天数", len(d.get("doc_trend") or []) == 14)
    st7, d7, _ = call("/api/admin/analytics?days=7", admin)
    check("days=7 生效", st7 == 200 and len(d7.get("trend") or []) == 7)

    # ---------- 2. 模型管理 ----------
    print("\n[2] 模型管理 /api/admin/models")
    st, m, _ = call("/api/admin/models", admin)
    check("接口可用（200）", st == 200, f"status={st}")
    check("含 LLM 主模型标识", bool((m.get("llm") or {}).get("primary_key")))
    chain = (m.get("llm") or {}).get("chain") or []
    check("模型链非空", len(chain) > 0, f"chain={chain}")
    check("恰有一个主模型", sum(1 for c in chain if c.get("is_primary")) == 1)
    check("含向量模型信息", bool((m.get("embedding") or {}).get("model")))
    check("GPU 字段有明确说明（不编造）",
          (m.get("gpu") or {}).get("available") is False and bool((m.get("gpu") or {}).get("reason"))
          or (m.get("gpu") or {}).get("available") is True)

    primary_before = (m.get("llm") or {}).get("primary_key")
    other = next((c["key"] for c in chain if not c.get("is_primary")), None)
    if other:
        st, r, _ = call("/api/admin/models/switch", admin, method="POST", body={"key": other})
        check("切换到备用模型成功", st == 200 and r.get("ok") is True, f"status={st} {r}")
        _, m2, _ = call("/api/admin/models", admin)
        check("主模型已变更", (m2.get("llm") or {}).get("primary_key") == other,
              f"now={(m2.get('llm') or {}).get('primary_key')}")
        call("/api/admin/models/switch", admin, method="POST", body={"key": primary_before})
        _, m3, _ = call("/api/admin/models", admin)
        check("已还原原主模型（验收不留副作用）",
              (m3.get("llm") or {}).get("primary_key") == primary_before)
    else:
        print("  SKIP  只有一个模型，跳过切换测试")
    st, _, _ = call("/api/admin/models/switch", admin, method="POST", body={"key": "not-exist:xx"})
    check("切换不存在的模型被拒（400）", st == 400, f"status={st}")

    # ---------- 3. PDF 合规报告 ----------
    print("\n[3] PDF 合规报告 /api/compliance/export?format=pdf")
    st, body, ctype = call("/api/compliance/export?format=pdf&limit=200", admin, raw=True)
    check("返回 200", st == 200, f"status={st}")
    check("Content-Type 为 application/pdf", "pdf" in ctype, f"ctype={ctype}")
    check("是合法 PDF（%PDF 头）", body[:5] == b"%PDF-", f"head={body[:8]!r}")
    check("PDF 体积合理（>2KB，含中文内容）", len(body) > 2048, f"size={len(body)}")
    st, body_m, _ = call("/api/compliance/export?format=pdf&limit=200&period=month", admin, raw=True)
    check("本月口径可导出", st == 200 and body_m[:5] == b"%PDF-", f"status={st}")
    st, body_q, _ = call("/api/compliance/export?format=pdf&limit=200&period=quarter", admin, raw=True)
    check("本季度口径可导出", st == 200 and body_q[:5] == b"%PDF-", f"status={st}")
    with open(".compliance_report_sample.pdf", "wb") as f:
        f.write(body_m)
    print("       样例已保存：.compliance_report_sample.pdf（可人工打开核对中文）")

    # ---------- 4. 原有导出未破坏 ----------
    print("\n[4] 原有 CSV / JSON 导出回归")
    st, csv_b, ctype = call("/api/compliance/export?format=csv&limit=50", admin, raw=True)
    check("CSV 导出正常", st == 200 and b"," in csv_b, f"status={st}")
    st, js_b, _ = call("/api/compliance/export?format=json&limit=50", admin, raw=True)
    check("JSON 导出正常", st == 200, f"status={st}")
    try:
        obj = json.loads(js_b)
        check("JSON 可被解析且含 items", "items" in obj)
    except Exception as e:  # noqa: BLE001
        check("JSON 可被解析且含 items", False, str(e))

    # ---------- 5. 越权拦截 ----------
    print("\n[5] 越权拦截（非管理员）")
    if not emp:
        print("  SKIP  未找到普通员工账号，跳过越权测试")
    else:
        for path, label in (("/api/admin/analytics", "数据看板"),
                            ("/api/admin/models", "模型清单")):
            st, _, _ = call(path, emp)
            check(f"员工访问{label}被拒（403）", st == 403, f"status={st}")
        st, _, _ = call("/api/admin/models/switch", emp, method="POST", body={"key": "x:y"})
        check("员工切换模型被拒（403）", st == 403, f"status={st}")
        st, _, _ = call("/api/compliance/export?format=pdf", emp, raw=True)
        check("员工导出 PDF 被拒（403）", st == 403, f"status={st}")

    print("\n" + "=" * 60)
    print(f"结果：{PASS} 通过 / {FAIL} 失败（共 {PASS + FAIL} 项）")
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
