"""一键回归入口：串行执行全部测试脚本并汇总结果。

交付/验收场景使用：

    cd backend
    python scripts/run_all_tests.py            # 仅跑无需外部服务的套件
    RUN_E2E=1 python scripts/run_all_tests.py  # 额外跑需服务已启动的端到端套件

退出码 0 表示全部通过，非 0 表示存在失败（可用于 CI / 验收自动化）。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent

# (显示名, 脚本名) —— 默认执行，不依赖外部服务
SUITES: list[tuple[str, str]] = [
    ("启动自检 preflight", "test_preflight.py"),
    ("系统信息接口", "test_system_config.py"),
    ("权限与文档删除审批", "test_doc_perm.py"),
    ("需求符合性回归", "test_optimize.py"),
    ("检索质量与上下文治理", "test_quality_opt.py"),
    ("意图识别准确性", "test_intent.py"),
    ("公网搜索脱敏与开关", "test_websearch.py"),
    ("实时工具注册表与行情", "test_realtime_tools.py"),
    ("中断后继续问答", "test_interrupt.py"),
    ("请假流程", "test_leave.py"),
    ("报销流程", "test_reimburse.py"),
]

# 需要先启动服务（uvicorn）才可执行；设置 RUN_E2E=1 才会跑
E2E_SUITES: list[tuple[str, str]] = [
    ("端到端冒烟", "smoke_test.py"),
    ("会话隔离(端到端)", "test_sessions.py"),
    ("请假端到端", "e2e_leave.py"),
    ("报销端到端", "e2e_reimburse.py"),
]

# 判定失败：非零退出码 / "  FAIL xxx" / 非空的 FAILS=[...] / RESULT: HAS_FAIL
_FAIL_PATTERNS = [
    re.compile(r"^\s*FAIL\b"),
    re.compile(r"FAILS=\[[^\]]+\]"),
    re.compile(r"RESULT:\s*HAS_FAIL"),
]


def run(script: str) -> tuple[bool, str]:
    """执行单个脚本，返回 (是否通过, 末行摘要)。"""
    path = HERE / script
    if not path.exists():
        return True, "跳过（脚本不存在）"
    # Windows 控制台默认 GBK，子进程输出中文会 UnicodeEncodeError；强制 UTF-8 输出。
    # PYTHONPATH 必须显式带上 backend：以脚本路径方式执行时 sys.path[0] 是 scripts/，
    # 不带这行的话 test_preflight 等套件会 import app 失败。
    child_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    prev_pp = child_env.get("PYTHONPATH", "")
    child_env["PYTHONPATH"] = str(BACKEND) + (os.pathsep + prev_pp if prev_pp else "")
    proc = subprocess.run(
        [sys.executable, "-u", str(path)],
        cwd=str(BACKEND),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env,
    )
    tail = (proc.stdout or "") + (proc.stderr or "")
    lines = [ln.rstrip() for ln in tail.splitlines() if ln.strip()]
    failed = proc.returncode != 0 or any(
        p.search(ln) for ln in lines for p in _FAIL_PATTERNS
    )
    return (not failed), (lines[-1][:48] if lines else "无输出")


def main() -> int:
    suites = list(SUITES)
    if os.environ.get("RUN_E2E") == "1":
        suites += E2E_SUITES

    print("=" * 66)
    print("  企业级知识检索中台 · 回归测试")
    print("=" * 66)

    results: list[tuple[str, bool, str, float]] = []
    e2e_names = {n for n, _ in E2E_SUITES}
    # 端到端套件共用同一个演示账号（sales_emp），而限流是「每用户 20 次/分钟」。
    # 连续跑会在第四个套件上撞 429。这里插入冷却，让一键回归真正"一键通过"。
    cooldown = int(os.environ.get("E2E_COOLDOWN_SECONDS", "45"))
    for i, (name, script) in enumerate(suites):
        if name in e2e_names and i and cooldown > 0:
            prev_is_e2e = suites[i - 1][0] in e2e_names
            if prev_is_e2e:
                print(f"  ·· 冷却 {cooldown}s（避开同一演示账号的限流窗口）")
                time.sleep(cooldown)
        t0 = time.time()
        ok, summary = run(script)
        results.append((name, ok, summary, time.time() - t0))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<24} {summary:<48} {results[-1][3]:>5.1f}s")

    passed = sum(1 for _, ok, _, _ in results if ok)
    print("-" * 66)
    print(f"  合计 {passed}/{len(results)} 项套件通过")
    print("=" * 66)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
