"""备份/恢复命令行工具（运维用，可挂 crontab）。

    python scripts/backup.py backup                    # 立即备份
    python scripts/backup.py list                      # 查看备份历史
    python scripts/backup.py restore <name>            # 恢复（会先自动安全备份当前库）
    python scripts/backup.py verify                    # 校验最近一份备份是否完整可恢复
    python scripts/backup.py prune                     # 按 BACKUP_KEEP 清理旧备份

crontab 示例（每天凌晨 2 点全量备份）：
    0 2 * * * cd /app/backend && /opt/venv/bin/python scripts/backup.py backup \
        >> /data/logs/backup.log 2>&1

退出码：0=成功，1=失败（便于监控/cron 告警识别）。

⚠ 设计取舍：数据库是主存储，备份失败必须**显式报错并以非 0 退出**，
绝不"跳过继续"。早期版本在 pg_dump 不可用时只打印 [跳过] 仍返回成功，
结果是备份任务天天报成功、数据库却从未被备份过——这比没有备份更危险。

旧参数（--out / --keep-days / --list）保留兼容，但推荐用上面的子命令写法。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import backup as B  # noqa: E402


def _die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"FAILED: {msg}", file=sys.stderr)
    raise SystemExit(1)


def cmd_backup() -> None:
    try:
        mf = B.create_backup(reason="cli")
    except B.BackupError as e:
        _die(str(e))
    print(f"OK 备份完成：{mf['name']}")
    print(f"   位置：{mf['dir']}")
    print(f"   总大小：{mf['total_kb']} KB   数据库：{mf['database']}")
    for p in mf["parts"]:
        extra = f"，{p['files']} 个文件" if "files" in p else ""
        print(f"   · {p['file']}  {p['size_kb']} KB{extra}  sha256={p['sha256'][:16]}…")


def cmd_list() -> None:
    items = B.list_backups()
    if not items:
        print("（暂无备份）")
        return
    print(f"{'备份名':<26}{'时间':<21}{'大小KB':>9}  {'完整':<6}{'来源'}")
    print("-" * 82)
    for it in items:
        ok = "是" if it.get("complete") else "否"
        print(f"{it['name']:<26}{str(it.get('created_at') or '-')[:19]:<21}"
              f"{str(it.get('total_kb') or '-'):>9}  {ok:<6}{it.get('reason') or '-'}")


def cmd_restore(name: str) -> None:
    if not name:
        _die("请指定备份名，例如：python scripts/backup.py restore backup-20260929-020000")
    confirm = input(f"即将用 {name} 覆盖当前数据库，且恢复前有安全备份。输入 yes 继续：")
    if confirm.strip().lower() != "yes":
        print("已取消")
        return
    try:
        r = B.restore_database(name, safety_backup=True)
        d = B.restore_data(name)
    except B.BackupError as e:
        _die(str(e))
    print(f"OK 已从 {name} 恢复")
    print(f"   如需反悔，可执行：python scripts/backup.py restore {r['revert_to']}")
    if d:
        print(f"   data 目录已恢复到 {d['target']}（{d['files']} 个文件）")


def cmd_verify() -> None:
    """校验最近一份备份：清单完整 + sha256 与清单一致 + dump 可被 pg_restore 解析（只读）。

    只检查"文件在不在"是不够的：磁盘损坏或传输截断都会让文件存在但内容已坏，
    而 sha256 才是能证明"这份备份还是当初那一份"的证据。
    """
    items = B.list_backups()
    if not items:
        _die("没有任何备份可校验")
    it = items[0]
    if not it.get("complete"):
        _die(f"最近一份备份 {it['name']} 不完整（缺产物），请检查备份流程")
    try:
        r = B.verify_backup(it["name"])
    except B.BackupError as e:
        _die(str(e))

    for p in r["parts"]:
        if p["sha256_match"]:
            print(f"OK  {p['file']}  {p['size_kb']} KB  sha256 与清单一致")
        else:
            print(f"BAD {p['file']}  sha256 不一致（文件被改动或已损坏）")
    if r["dump_readable"] is False:
        _die(f"备份 {it['name']} 的 database.dump 无法被 pg_restore 解析")
    if r["dump_readable"] is None:
        print("WARN 本机无 pg_restore，未验证 dump 可解析性（sha256 校验不受影响）")
    if not r["ok"]:
        _die(f"备份 {it['name']} 校验未通过")
    m = r["manifest"]
    print(f"OK 备份 {it['name']} 校验通过：时间 {m.get('created_at')}，"
          f"总大小 {m.get('total_kb')} KB，数据库 {m.get('database')}")


def cmd_prune() -> None:
    removed = B.apply_retention()
    print(f"OK 清理 {len(removed)} 份旧备份" if removed else "无需清理")
    for r in removed:
        print(f"   已删除 {r}")


def main() -> None:
    argv = sys.argv[1:]
    # ---- 旧参数兼容：部署/运维手册里的 --out / --keep-days / --list 仍然可用 ----
    legacy_out = None
    legacy_keep = None
    legacy_list = False
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--out" and i + 1 < len(argv):
            legacy_out = argv[i + 1]
            i += 2
            continue
        if a.startswith("--out="):
            legacy_out = a.split("=", 1)[1]
            i += 1
            continue
        if a == "--keep-days" and i + 1 < len(argv):
            legacy_keep = int(argv[i + 1])
            i += 2
            continue
        if a.startswith("--keep-days="):
            legacy_keep = int(a.split("=", 1)[1])
            i += 1
            continue
        if a == "--list":
            legacy_list = True
            i += 1
            continue
        rest.append(a)
        i += 1

    if legacy_out:
        # settings 是进程内单例，backup_root() 每次都读它，因此这里赋值即刻生效。
        # （旧实现写 os.environ["BACKUP_DIR"] 是无效的：配置在首次导入时就已固化成
        #   settings 对象，之后再改环境变量不会被重新读取 → --out 静默失效。）
        B.settings.BACKUP_DIR = legacy_out
    if legacy_list:
        cmd_list()
        if legacy_keep is not None:
            _prune_by_days(legacy_keep)
        return

    if not rest:
        # 无参数 = 备份（与旧版默认行为一致）
        cmd_backup()
        if legacy_keep is not None:
            _prune_by_days(legacy_keep)
        return

    cmd, arg = rest[0], (rest[1] if len(rest) > 1 else "")
    table = {
        "backup": lambda: cmd_backup(),
        "list": lambda: cmd_list(),
        "restore": lambda: cmd_restore(arg),
        "verify": lambda: cmd_verify(),
        "prune": lambda: cmd_prune(),
    }
    if cmd not in table:
        print(__doc__)
        raise SystemExit(1)
    table[cmd]()
    if legacy_keep is not None and cmd == "backup":
        _prune_by_days(legacy_keep)


def _prune_by_days(days: int) -> None:
    """旧 --keep-days 行为：删除 N 天前的备份目录。"""
    import time
    from datetime import datetime
    root = B.backup_root()
    if not root.is_dir() or days <= 0:
        return
    cutoff = time.time() - days * 86400
    removed = []
    for d in sorted(root.glob("backup-*")):
        if not d.is_dir():
            continue
        try:
            stamp = datetime.strptime(d.name.replace("backup-", ""), "%Y%m%d-%H%M%S")
        except ValueError:
            continue
        if stamp.timestamp() < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d.name)
    print(f"OK 清理 {len(removed)} 份超过 {days} 天的备份" if removed else "无需清理")


if __name__ == "__main__":
    main()
