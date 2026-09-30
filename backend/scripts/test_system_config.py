"""系统运行信息接口验证：前端据此决定登录形态（演示下拉 / 账号密码输入）。"""
from __future__ import annotations

import os
import sys
import tempfile

# 自包含：把 backend 加入导入路径，使脚本可独立运行（无需外部设置 PYTHONPATH）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

os.environ.setdefault("DB_FORCE_MEMORY", "true")
os.environ.setdefault("REDIS_ENABLED", "false")
os.environ.setdefault("APP_DATA_DIR", tempfile.mkdtemp())

from fastapi.testclient import TestClient     # noqa: E402
from app.main import app                      # noqa: E402

FAILS = []
OUT = os.path.join(tempfile.gettempdir(), "rag_system_config_result.txt")
lines: list[str] = []


def check(name, ok):
    lines.append(("ok   " if ok else "FAIL ") + name)
    if not ok:
        FAILS.append(name)


c = TestClient(app)
c.__enter__()   # 触发 lifespan（含启动自检）

try:
    r = c.get("/api/system/config")
    d = r.json()
    check("系统信息接口 200", r.status_code == 200)
    check("返回环境为 dev", d.get("environment") == "dev")
    check("dev 下演示模式开启", d.get("demo_mode") is True)
    check("返回版本号", bool(d.get("version")))

    # 演示模式下内置账号可登录（供前端下拉使用）
    r = c.post("/api/auth/login", json={"username": "sales_mgr", "password": "123456"})
    check("演示账号可登录", r.status_code == 200)
    check("登录返回角色", r.json().get("user", {}).get("role") == "sub_manager")
finally:
    c.__exit__(None, None, None)

lines.append("")
lines.append("FAILS=" + str(FAILS))
lines.append("RESULT: " + ("ALL_PASS" if not FAILS else "HAS_FAIL"))
with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
