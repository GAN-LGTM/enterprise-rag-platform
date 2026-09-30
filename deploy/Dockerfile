# syntax=dockerfile:1
# ============================================================
# 生产交付镜像（私有化部署）
#   构建：docker build -f deploy/Dockerfile -t rag-platform:<version> .
#   特点：多阶段构建（镜像不含编译工具）、非 root 运行、固定工作目录与数据卷、
#        健康检查就绪探针、uvicorn 多 worker
# ============================================================

# ---------- 阶段一：构建依赖 ----------
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# 编译依赖：psycopg / numpy 等需要（构建期）
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /build/requirements.txt
# 依赖装进独立 venv，运行时只拷贝 venv，镜像不含 pip 缓存与编译工具
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install -r /build/requirements.txt

# ---------- 阶段二：运行时 ----------
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    TZ=Asia/Shanghai \
    APP_DATA_DIR=/data/app

# 运行时依赖：
#   · curl        —— 健康检查探针
#   · postgresql-client-16 —— 备份/恢复（pg_dump、pg_restore）
#     版本必须与数据库主版本一致（compose 用 pgvector/pgvector:pg16），
#     Debian 默认源只有 client 15，版本不匹配 pg_dump 会直接拒绝执行，
#     所以这里显式从 PGDG 官方源装 16。
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates tzdata gnupg \
    && . /etc/os-release \
    && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && apt-get purge -y --auto-remove gnupg \
    && rm -rf /var/lib/apt/lists/* /etc/apt/sources.list.d/pgdg.list \
    && groupadd -g 10001 rag \
    && useradd -u 10001 -g rag -m -d /home/rag rag

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app/backend   # 应用包 app/ 位于 backend/ 下
COPY backend /app/backend
COPY sql /app/sql
COPY frontend /app/frontend

# 数据与模型目录（由 compose 挂载宿主机卷）
RUN mkdir -p /data/app /data/docs /data/logs /data/models /data/backups \
    && chown -R rag:rag /data /app

USER rag

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/ping || exit 1

# WEB_CONCURRENCY 由 compose/环境注入；单容器默认 2 worker（可按 CPU 核数调整）
ENV WEB_CONCURRENCY=2
CMD ["sh", "-c", "exec uvicorn app.main:app \
     --host 0.0.0.0 --port 8000 \
     --workers ${WEB_CONCURRENCY} \
     --proxy-headers --forwarded-allow-ips '*' \
     --timeout-keep-alive 65"]
