"""启动自检（preflight）用例：生产环境安全基线必须拦得住。"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

os.environ.setdefault("DB_FORCE_MEMORY", "true")
os.environ.setdefault("REDIS_ENABLED", "false")

# 自包含：把 backend 加入导入路径；同时让内部子进程也继承该路径
BACKEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, BACKEND_DIR)
os.environ["PYTHONPATH"] = BACKEND_DIR + os.pathsep + os.environ.get("PYTHONPATH", "")

from app.config import Settings          # noqa: E402
from app.core import preflight           # noqa: E402

FAILS = []
STRONG_JWT = "x" * 48


def check(name, ok):
    print(("  ok " if ok else "  FAIL ") + name)
    if not ok:
        FAILS.append(name)


def run_with(**kw) -> str | None:
    """用指定配置跑一次自检，返回 None 表示通过，否则返回拒绝原因。"""
    old = preflight.settings
    preflight.settings = Settings(**kw)
    try:
        preflight.run(verbose=False)
        return None
    except preflight.PreflightError as e:
        return str(e)
    finally:
        preflight.settings = old


# 生产基线：除了被测项，其余都合规
BASE = dict(
    ENVIRONMENT="production",
    JWT_SECRET_KEY=STRONG_JWT,
    EMBEDDING_BACKEND="local",
    DB_FORCE_MEMORY=False,
    LLM_BACKEND="vllm",
    ALLOWED_ORIGINS="https://rag.corp.example.com",
    DEMO_MODE="false",   # 字段用别名 DEMO_MODE（validation_alias）
)

# 1) 生产全合规 → 通过
err = run_with(**BASE)
check("生产配置合规时通过", err is None)

# 2) 默认 JWT 密钥 → 拒绝
err = run_with(**{**BASE, "JWT_SECRET_KEY": "please-change-this-secret-in-production-32bytes-minimum!"})
check("默认 JWT 密钥被拒", err is not None and "jwt_secret" in err)

# 3) 短密钥 → 拒绝
err = run_with(**{**BASE, "JWT_SECRET_KEY": "short"})
check("过短 JWT 密钥被拒", err is not None and "jwt_secret" in err)

# 4) 演示模式 → 拒绝
err = run_with(**{**BASE, "DEMO_MODE": "true"})
check("生产开启演示模式被拒", err is not None and "demo_mode" in err)

# 5) 伪向量 → 拒绝
err = run_with(**{**BASE, "EMBEDDING_BACKEND": "hash"})
check("生产使用伪向量被拒", err is not None and "embedding_backend" in err)

# 6) 内存库 → 拒绝
err = run_with(**{**BASE, "DB_FORCE_MEMORY": True})
check("生产使用内存库被拒", err is not None and "database" in err)

# 7) mock LLM → 拒绝
err = run_with(**{**BASE, "LLM_BACKEND": "mock"})
check("生产使用 mock LLM 被拒", err is not None and "llm_backend" in err)

# 8) 云端 LLM（私有化禁止外联）→ 拒绝
err = run_with(**{**BASE, "LLM_BACKEND": "cloud"})
check("生产外联云端 LLM 被拒", err is not None and "llm_backend" in err)

# 9) 显式允许云端 → 放行
err = run_with(**{**BASE, "LLM_BACKEND": "cloud", "ALLOW_CLOUD_LLM": True})
check("显式允许云端 LLM 时放行", err is None)

# 10) CORS 通配 → 拒绝
err = run_with(**{**BASE, "ALLOWED_ORIGINS": "*"})
check("生产 CORS 通配被拒", err is not None and "cors" in err)

# 11) dev 环境同类问题只告警不阻断
err = run_with(ENVIRONMENT="dev", JWT_SECRET_KEY="short", EMBEDDING_BACKEND="hash",
               DB_FORCE_MEMORY=True, DEMO_MODE="true")
check("dev 环境不阻断启动", err is None)

# 12) 生产模式：不加载任何演示账号，且自检后仅存在初始管理员
code = (
    "from app.db.models import USERS;"
    "from app.core import preflight;"
    "preflight.run(verbose=False);"
    "print('USERS=' + ','.join(sorted(USERS.keys())))"
)
env = {**os.environ, "ENVIRONMENT": "production", "DEMO_MODE": "false",
       "JWT_SECRET_KEY": STRONG_JWT, "EMBEDDING_BACKEND": "http",
       "DB_FORCE_MEMORY": "false", "LLM_BACKEND": "vllm",
       "ALLOWED_ORIGINS": "https://rag.corp.example.com",
       "ADMIN_INITIAL_PASSWORD": "Init-Pwd-2024", "APP_DATA_DIR": tempfile.mkdtemp()}
r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
out = (r.stdout or "") + (r.stderr or "")
check("生产模式无演示账号", r.returncode == 0 and "USERS=admin" in out)

# 13) 开发模式：演示账号仍然可用（保证联调体验不受影响）
# 注意：演示账号数量随业务演进会增长，这里用"地板值 11"断言，对新增账号鲁棒。
env2 = {**os.environ, "ENVIRONMENT": "dev", "DEMO_MODE": "true"}
r2 = subprocess.run([sys.executable, "-c",
                     "from app.db.models import USERS; print('N=' + str(len(USERS)))"],
                    capture_output=True, text=True, env=env2)
import re as _re
_m = _re.search(r"N=(\d+)", r2.stdout or "")
_dev_n = int(_m.group(1)) if _m else 0
check("开发模式保留演示账号", _dev_n >= 11)

print()
print("FAILS=" + str(FAILS))
print("RESULT:", "ALL_PASS" if not FAILS else "HAS_FAIL")