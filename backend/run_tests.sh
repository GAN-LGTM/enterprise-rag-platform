#!/usr/bin/env bash
# 企业级测试与覆盖率报告入口（Linux / macOS / Git Bash）
# 用法：bash backend/run_tests.sh
#
# 覆盖：纯逻辑单元测试 + 10 个默认回归套件（scripts/run_all_tests.py 的 SUITES，
# 不依赖外部服务）。需要「已启动 uvicorn」的端到端套件（test_sessions 等）不在默认范围内。
set -e
cd "$(dirname "$0")"          # 进入 backend/
ROOT="$(cd .. && pwd)"        # 项目根（venv 在根目录）

# 把 backend 以「本机原生路径」暴露给所有被测子进程（Windows 下必须是 D:\... 而非 /d/...，
# 否则子进程 import app 失败）。
if command -v cygpath >/dev/null 2>&1; then
  export PYTHONPATH="$(cygpath -w "$PWD")"
else
  export PYTHONPATH="$PWD"
fi

if [ -f "$ROOT/.venv/Scripts/coverage.exe" ]; then
  COV="$ROOT/.venv/Scripts/coverage.exe"
elif [ -f "$ROOT/.venv/bin/coverage" ]; then
  COV="$ROOT/.venv/bin/coverage"
else
  COV=coverage
fi

# 默认回归套件（与 scripts/run_all_tests.py 的 SUITES 保持一致，排除 E2E）
SUITES="test_preflight test_system_config test_doc_perm test_optimize test_quality_opt \
test_intent test_websearch test_interrupt test_leave test_reimburse"

"$COV" erase
rm -f .coverage.*

echo "===== 1) 纯逻辑单元测试（进程内覆盖率）====="
"$COV" run -p -m pytest tests/test_unit.py -q

echo
echo "===== 2) 默认回归套件（各自进程覆盖率，并行模式落盘后合并）====="
for s in $SUITES; do
  echo "---- $s ----"
  "$COV" run -p "scripts/$s.py" > "logs/_testruns/$s.cov.out" 2>&1 || echo "  >>> 退出码非0，详见 logs/_testruns"
done

echo
echo "===== 3) 合并覆盖率并汇总 ====="
"$COV" combine
"$COV" report --show-missing
"$COV" json -o coverage.json
echo "DONE -> backend/coverage.json"
