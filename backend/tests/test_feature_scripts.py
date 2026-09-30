"""功能回归测试：把 backend/scripts/test_*.py 这组既有端到端脚本纳入 pytest 统一调度。

每个脚本都用 FastAPI TestClient 在「内存库 + 关闭 Redis」下跑真实接口，
覆盖：登录鉴权、知识库 CRUD、权限隔离、对话、请假/报销、合规审计、preflight 等。
这里只负责「统一运行 + 断言全绿」，覆盖率由 run_tests.sh 用 coverage 聚合。
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys
import tempfile

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = sorted(glob.glob(os.path.join(BACKEND, "scripts", "test_*.py")))
# 排除需要「先启动 uvicorn 服务」才能跑的端到端脚本（它们直连 localhost，离线无法执行）
_E2E_NAMES = {"test_sessions.py", "smoke_test.py"}
SCRIPTS = [
    s for s in SCRIPTS
    if os.path.basename(s) not in _E2E_NAMES
    and not os.path.basename(s).startswith("e2e_")
]


def _collect_result_text(path: str, stdout: str) -> str:
    """脚本可能把结果写到临时文件（如 test_system_config），一并纳入判定。"""
    text = stdout
    tmp = tempfile.gettempdir()
    try:
        mtime_floor = os.path.getmtime(path) - 5
    except OSError:
        mtime_floor = 0
    for fn in os.listdir(tmp):
        if "result" in fn and fn.endswith(".txt"):
            fp = os.path.join(tmp, fn)
            try:
                if os.path.getmtime(fp) >= mtime_floor:
                    text += "\n" + open(fp, encoding="utf-8", errors="ignore").read()
            except OSError:
                pass
    return text


@pytest.mark.parametrize("path", SCRIPTS, ids=[os.path.basename(p) for p in SCRIPTS])
def test_feature_script_passes(path):
    env = dict(os.environ)
    env["PYTHONPATH"] = BACKEND
    env.setdefault("DB_FORCE_MEMORY", "true")
    env.setdefault("REDIS_ENABLED", "false")
    r = subprocess.run(
        [sys.executable, path], capture_output=True, text=True, env=env, timeout=300
    )
    text = _collect_result_text(path, (r.stdout or "") + (r.stderr or ""))
    assert r.returncode == 0, f"{os.path.basename(path)} 退出码非0:\n{text[-3000:]}"
    assert "HAS_FAIL" not in text, f"{os.path.basename(path)} 存在失败用例:\n{text[-3000:]}"
    assert "  FAIL " not in text, f"{os.path.basename(path)} 存在 FAIL 标记:\n{text[-3000:]}"
