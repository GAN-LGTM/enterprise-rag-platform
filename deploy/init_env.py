"""交付初始化：生成生产环境配置与密钥。

用法（部署机执行一次）：
    python deploy/init_env.py                 # 生成 .env.production（已存在则只校验不覆盖）
    python deploy/init_env.py --force         # 强制重新生成密钥
    python deploy/init_env.py --check         # 只校验不写入

生成内容：
  * JWT_SECRET_KEY      随机 64 位密钥（务必保密，泄露等于可伪造任意身份）
  * ADMIN_INITIAL_PASSWORD  初始管理员口令（首次登录强制改密）
  * PG_PASSWORD         PostgreSQL 口令
  * 其余为生产推荐值（ENVIRONMENT=production、关闭演示模式等）

脚本不会把密钥打印到标准输出的明文以外位置；.env.production 权限设为 600（Windows 尽力而为）。
"""
from __future__ import annotations

import argparse
import os
import secrets
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".env.production"

TEMPLATE = """# ============================================================
# 生产环境配置（由 deploy/init_env.py 生成，请勿提交代码仓库）
# 生成时间：{ts}
# ============================================================
ENVIRONMENT=production
DEMO_MODE=false
APP_NAME={app_name}
APP_VERSION={app_version}
OFFLINE_MODE=true

# ---------------- 安全 ----------------
JWT_SECRET_KEY={jwt}
JWT_EXPIRE_MINUTES={jwt_expire}
FORCE_PASSWORD_CHANGE_ON_FIRST_LOGIN=true
ALLOWED_ORIGINS={origins}

# 初始管理员（首次登录强制改密；改密后请删除 data/initial_admin.txt）
ADMIN_INITIAL_USERNAME=admin
ADMIN_INITIAL_PASSWORD={admin_pwd}
ADMIN_FORCE_PASSWORD_CHANGE=true

# ---------------- 数据库 / 缓存 ----------------
PG_USER=rag
PG_PASSWORD={pg_pwd}
PG_DB=ragdb
DB_FORCE_MEMORY=false
DB_AUTO_FALLBACK=false
REDIS_ENABLED=true
REDIS_MAXMEMORY=2gb
# Redis 口令（内部网络同样要求认证；compose 用它做 requirepass）
REDIS_PASSWORD={redis_pwd}

# ---------------- 模型（私有化本地推理）----------------
# 本地向量模型：local=内置 BGE（需权重）/ http=自建服务
EMBEDDING_BACKEND={emb}
EMBEDDING_MODEL_PATH=/data/models/bge-large-zh-v1.5
EMBEDDING_DIM=1024

# 生成模型：vllm=本地 vLLM（离线）；如确需云端，另设 ALLOW_CLOUD_LLM=true
LLM_BACKEND={llm}
LLM_MODEL={llm_model}
LLM_BASE_URL={llm_base}
LLM_FALLBACK_MODEL={llm_fallback}
ALLOW_CLOUD_LLM=false

# 意图识别小模型（放入权重后开启）
BERT_ENABLED=false
BERT_MODEL_PATH=/data/models/bert-tiny/intent.onnx

# ---------------- 运行 ----------------
LOG_LEVEL=INFO
LOG_DIR=/data/logs
RATE_LIMIT_PER_MIN=20
RATE_LIMIT_WINDOW_SECONDS=60
# 必须与 deploy/nginx.conf 的 client_max_body_size 一致，否则文件过网关后被后端拒绝
MAX_UPLOAD_MB={max_upload_mb}
WEB_CONCURRENCY=2
HTTP_PORT=80
HTTPS_PORT=443
MODEL_DIR=/data/models

# ---------------- 数据目录（必须与 docker-compose.prod.yml 的卷挂载点逐一对应）----------------
# 不一致的后果：数据写进容器可写层，容器重建/升级后上传文档、日志、会话全部丢失。
APP_DATA_DIR=/data/app
DOC_UPLOAD_DIR=/data/docs
BACKUP_DIR=/data/backups
BACKUP_KEEP=14
"""


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)   # 600
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="生成生产环境配置")
    ap.add_argument("--force", action="store_true", help="已存在也重新生成")
    ap.add_argument("--check", action="store_true", help="只校验不写入")
    ap.add_argument("--origins", default="https://rag.corp.example.com",
                    help="允许的前端来源，逗号分隔（禁止 *）")
    ap.add_argument("--llm", default="vllm", choices=["vllm", "cloud"])
    ap.add_argument("--llm-model", default="qwen-14b-chat")
    ap.add_argument("--llm-base", default="http://vllm:8000/v1")
    ap.add_argument("--llm-fallback", default="qwen-7b-chat")
    ap.add_argument("--emb", default="local", choices=["local", "http", "hash"])
    args = ap.parse_args()

    if args.check:
        if not OUT.exists():
            print(f"[缺失] {OUT} 不存在，请先执行 python deploy/init_env.py")
            return 1
        txt = OUT.read_text(encoding="utf-8")
        bad = [k for k in ("JWT_SECRET_KEY=", "ADMIN_INITIAL_PASSWORD=", "PG_PASSWORD=")
               if f"{k}\n" in txt or txt.rstrip().endswith(k[:-1])]
        print("[异常] 存在空值配置项：" + ", ".join(bad) if bad else "[正常] 关键配置项均已填写")
        return 1 if bad else 0

    if OUT.exists() and not args.force:
        print(f"[跳过] {OUT} 已存在；如需重新生成密钥请加 --force")
        return 0

    import datetime
    cfg = dict(
        ts=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        app_name=_app_settings.APP_NAME,
        app_version=_app_settings.APP_VERSION,
        max_upload_mb=_app_settings.MAX_UPLOAD_MB,
        jwt=secrets.token_urlsafe(48),
        jwt_expire=os.getenv("JWT_EXPIRE_MINUTES", "60"),
        origins=args.origins,
        admin_pwd=secrets.token_urlsafe(16),
        pg_pwd=secrets.token_urlsafe(20),
        redis_pwd=secrets.token_urlsafe(20),
        emb=args.emb,
        llm=args.llm,
        llm_model=args.llm_model,
        llm_base=args.llm_base,
        llm_fallback=args.llm_fallback,
    )
    _write(OUT, TEMPLATE.format(**cfg))
    print(f"[完成] 已生成 {OUT}")
    print("  · 初始管理员：admin / " + cfg["admin_pwd"])
    print("  · 请立即备份该文件并限制访问；首次登录后请修改管理员密码")
    print("  · 若启用 GPU 推理：docker compose -f deploy/docker-compose.prod.yml --profile gpu up -d")
    return 0


if __name__ == "__main__":
    sys.exit(main())
