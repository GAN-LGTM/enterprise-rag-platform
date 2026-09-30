"""依赖锁定一致性检查 —— 防止 `requirements.lock.txt` 变成没人维护的死文件。

背景：容器构建与 CI 安装用的是 `requirements.txt`（声明式），而离线/内网交付
安装用的是 `requirements.lock.txt`（全量 `pip freeze` 结果）。两者一旦漂移，
最典型的后果是"内网装出来的版本和验收时跑过的版本不是同一套"，
出问题极难定位。本脚本把这条契约变成可自动验证的门禁。

规则：
  · 只校验**直接依赖**：`requirements.txt` 里声明的每个包，都必须在 lock 里出现；
  · 且 lock 中的该包必须是精确 pin（`==`），否则"可复现"无从谈起；
  · lock 里多出来的条目是传递依赖，允许存在。

用法（项目根目录或任意目录均可）：
  python backend/scripts/check_dep_lock.py
退出码：0 = 一致；1 = 存在缺失或未 pin。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REQ = ROOT / "requirements.txt"
LOCK = ROOT / "requirements.lock.txt"


def _norm(name: str) -> str:
    """按 PEP 503 归一化包名（大小写、`-`/`_`/`.` 等价）。"""
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _parse(path: Path) -> dict[str, tuple[str, str]]:
    """解析 requirements 文件 → {归一化名: (原名, 版本运算符)}。"""
    out: dict[str, tuple[str, str]] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.split("#", 1)[0].strip()
        # 跳过空行、pip 选项（-r / --index-url / -e）、环境标记行
        if not line or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(==|>=|<=|~=|!=|>|<)?\s*([^\s;]*)", line)
        if not m:
            continue
        out[_norm(m.group(1))] = (m.group(1), m.group(2) or "")
    return out


def main() -> int:
    for p in (REQ, LOCK):
        if not p.is_file():
            print(f"[FAIL] 缺少依赖文件：{p}")
            return 1

    req, lock = _parse(REQ), _parse(LOCK)
    missing = [orig for key, (orig, _) in req.items() if key not in lock]
    loose = [lock[key][0] for key in req if key in lock and lock[key][1] != "=="]

    print(f"requirements.txt 直接依赖 {len(req)} 个 | requirements.lock.txt 锁定 {len(lock)} 个")
    if missing:
        print(f"[FAIL] lock 未覆盖以下直接依赖（离线安装会漏装）：{', '.join(missing)}")
    if loose:
        print(f"[FAIL] 以下依赖在 lock 中未精确 pin（必须用 ==，否则无法复现）：{', '.join(loose)}")
    if missing or loose:
        return 1
    print("RESULT: ALL_PASS 依赖锁定与声明一致（离线安装可复现）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
