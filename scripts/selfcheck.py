# -*- coding: utf-8 -*-
"""启动自检：服务起来后跑一遍核心链路，输出可读的体检报告。

用法：
    python scripts/selfcheck.py            # 检查本机 8000 端口
    python scripts/selfcheck.py 8080       # 指定端口

退出码：0 = 全部通过；1 = 存在失败项。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

PORT = sys.argv[1] if len(sys.argv) > 1 else "8000"
BASE = f"http://127.0.0.1:{PORT}"

# 关键：本机回环请求必须绕过系统代理。
# urllib 默认会读取 Windows 系统代理设置，把 127.0.0.1 也发给代理，
# 表现为服务明明正常却返回 HTTP 502（排查时会误判成"服务没起来"）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
urllib.request.install_opener(_OPENER)

_results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((ok, name, detail))
    print(f"  [{'通过' if ok else '失败'}] {name}" + (f"  — {detail}" if detail and not ok else ""))
    return ok


def http(method: str, path: str, token: str | None = None, body: dict | None = None,
         raw: bool = False):
    """raw=True 时不按 JSON 解析（用于前端页面等 HTML 响应）。"""
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    data = json.dumps(body).encode() if body is not None else None
    try:
        with _OPENER.open(req, data=data, timeout=10) as r:
            text = r.read().decode("utf-8", "replace")
            return r.status, (text if raw else json.loads(text))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def main() -> int:
    print("=" * 58)
    print("  系统自检报告")
    print(f"  目标地址：{BASE}   时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 58)

    print("\n[1] 服务与页面")
    st, d = http("GET", "/api/health")
    check("后端健康检查 /api/health", st == 200, f"HTTP {st}")
    st, page = http("GET", "/ui/", raw=True)
    check("前端页面 /ui/ 可访问", st == 200 and "<html" in str(page).lower(), f"HTTP {st}")
    st, cfg = http("GET", "/api/system/config")
    check("系统配置可读取", st == 200 and isinstance(cfg, dict), f"HTTP {st}")
    demo = bool(cfg.get("demo_mode")) if isinstance(cfg, dict) else False
    print(f"       运行模式：{'演示模式（含内置示例账号）' if demo else '生产模式（需企业统一账号）'}")

    if not (st == 200 and demo):
        print("\n生产模式跳过账号相关检查（账号由企业统一分配）。")
        return _summary()

    print("\n[2] 认证与令牌")
    st, emp = http("POST", "/api/auth/login", body={"username": "sales_emp", "password": "123456"})
    check("员工账号登录", st == 200, f"HTTP {st}")
    if st != 200:
        return _summary()
    emp_tok = emp.get("access_token", "")
    check("登录返回刷新令牌", bool(emp.get("refresh_token")))
    st, r2 = http("POST", "/api/auth/refresh", body={"refresh_token": emp.get("refresh_token")})
    check("刷新令牌可续期", st == 200, f"HTTP {st}")

    print("\n[3] 权限隔离（最关键）")
    st, me = http("GET", "/api/auth/me", token=emp_tok)
    depts = me.get("accessible_depts", []) if isinstance(me, dict) else []
    extra = [x for x in depts if x not in ("public", "mkt.sales")]
    check("员工仅可访问本部门 + 公共库", st == 200 and not extra,
          f"多出 {extra}（历史授权残留？可执行 scripts/reset_demo_data.py 重置）")
    st, docs = http("GET", "/api/knowledge/docs", token=emp_tok)
    leaked = [x for x in (docs or []) if x.get("department_id") not in depts]
    check("文档列表无越权部门数据", st == 200 and not leaked, f"越权 {len(leaked)} 条")
    check("文档列表返回更新时间与状态字段",
          st == 200 and bool(docs) and ("updated_at" in docs[0]) and ("status" in docs[0]))

    print("\n[4] 知识与治理")
    st, hp = http("GET", "/api/knowledge/health", token=emp_tok)
    check("知识健康度接口可用", st == 200 and isinstance(hp, dict) and "score" in hp,
          f"score={hp.get('score') if isinstance(hp, dict) else '-'}")
    st, _ = http("GET", "/api/knowledge/departments", token=emp_tok)
    check("部门列表接口可用", st == 200)

    print("\n[5] 角色隔离")
    st, d = http("POST", "/api/auth/login", body={"username": "ceo", "password": "123456"})
    ceo_tok = d.get("access_token", "") if st == 200 else ""
    check("高管账号登录", st == 200, f"HTTP {st}")
    st, _ = http("GET", "/api/compliance/audit-logs", token=ceo_tok)
    check("非合规角色访问审计日志被拒（403）", st == 403, f"HTTP {st}")
    st, d = http("POST", "/api/auth/login", body={"username": "compliance_reviewer", "password": "123456"})
    check("合规审核员账号登录", st == 200, f"HTTP {st}")
    if st == 200:
        st, _ = http("GET", "/api/compliance/audit-logs", token=d.get("access_token", ""))
        check("合规审核员可读取审计日志", st == 200, f"HTTP {st}")

    return _summary()


def _summary() -> int:
    ok = sum(1 for r in _results if r[0])
    bad = [r for r in _results if not r[0]]
    print("\n" + "=" * 58)
    print(f"  自检结果：{ok} / {len(_results)} 项通过")
    if bad:
        print("  未通过项：")
        for _, name, detail in bad:
            print(f"    - {name}  {detail}")
        print("\n  处理建议：查看 logs/server.log 末尾报错；")
        print("  若为端口占用导致，运行 stop.bat 后重新 start.bat。")
    else:
        print("  系统各核心链路正常，可正常使用。")
    print("=" * 58)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
