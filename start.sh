#!/usr/bin/env bash
# 企业级知识检索中台 - 一键启动 (Linux / macOS / Git Bash)
set -e
cd "$(dirname "$0")"

echo "============================================"
echo "  企业级知识检索中台 - 一键启动"
echo "============================================"

if [ ! -d ".venv" ]; then
    echo "[1/3] 创建虚拟环境..."
    python3 -m venv .venv
fi

echo "[2/3] 安装依赖..."
./.venv/bin/python -m pip install -q -r requirements.txt \
  || ./.venv/bin/python -m pip install -q -r requirements.txt -i https://pypi.org/simple

# 监听地址：默认只监听本机回环；容器/局域网访问时 HOST=0.0.0.0 ./start.sh
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

echo "[3/3] 启动服务..."
echo
echo "  接口文档 : http://127.0.0.1:${PORT}/docs"
echo "  健康检查 : http://127.0.0.1:${PORT}/api/health"
echo "  前端页面 : http://127.0.0.1:${PORT}/ui  （浏览器打开）"
if [ "${DEMO_MODE}" != "false" ]; then
  echo "  演示模式 : 内置账号密码 123456，系统管理员 admin123"
else
  echo "  交付模式 : 管理员初始口令见 data/initial_admin.txt"
fi
echo "  监听地址 : ${HOST}:${PORT}"
echo
export PYTHONPATH="$(pwd)/backend"
exec ./.venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT"
