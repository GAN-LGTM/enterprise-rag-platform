# -*- coding: utf-8 -*-
"""交付前清理：清除调试/测试残留，把演示数据恢复到干净出厂状态。

清理范围（仅演示模式适用，生产环境请勿随意执行）：
  - grants.json              测试期间授予的跨部门权限（会让"员工看到别的部门"，交付前必须清）
  - grant_meta.json          授权明细（授予人 / 有效期 / 权限类型），与 grants.json 成对清理
  - permission_requests.json 测试提交的权限申请单
  - users_admin.json         测试期间新增/改密/调角色的账号覆盖（恢复出厂账号体系）
  - notifications.json       测试产生的通知
  - refresh_tokens.json      测试签发的刷新令牌
  - token_blacklist.json     测试登出产生的令牌黑名单
  - audit.json               测试产生的权限申请审计条目（其余审计保留）

用法（**必须先停止服务**，否则服务内存中的状态会重新写回文件）：
    stop.bat
    python scripts/reset_demo_data.py
    start.bat
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "backend" / "data"

EMPTY_LIST: list = []
EMPTY_DICT: dict = {}


def _write(name: str, payload) -> None:
    p = DATA / name
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  清空 {name}")


def main() -> int:
    if not DATA.is_dir():
        print(f"未找到数据目录：{DATA}")
        return 1

    print("=" * 58)
    print("  演示数据清理（交付前执行）")
    print(f"  数据目录：{DATA}")
    print("=" * 58)

    for f, payload in (("grants.json", EMPTY_DICT),
                       ("grant_meta.json", EMPTY_DICT),
                       ("permission_requests.json", EMPTY_LIST),
                       ("users_admin.json", EMPTY_DICT),
                       ("notifications.json", EMPTY_LIST),
                       ("refresh_tokens.json", EMPTY_LIST),
                       ("token_blacklist.json", EMPTY_DICT)):
        if (DATA / f).exists():
            _write(f, payload)

    audit_path = DATA / "audit.json"
    if audit_path.exists():
        try:
            items = json.loads(audit_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            items = []
        if isinstance(items, list):
            kept = [i for i in items if not str(i.get("type", "")).startswith("perm_request")]
            removed = len(items) - len(kept)
            audit_path.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  审计日志：移除测试条目 {removed} 条，保留 {len(kept)} 条")

    print("\n清理完成。请重新启动服务（start.bat）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
