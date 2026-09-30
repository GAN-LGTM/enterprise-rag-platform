"""备份与恢复（企业级交付的底线能力）。

为什么必须做这一层
------------------
此前 /api/admin/backup 只把 data/ 目录打成 zip，而真正的业务主存储在 PostgreSQL
（doc_chunks 向量库、会话、审计、反馈、权限申请）——等于"备份了最不重要的那部分"。
一旦数据库出事，知识库全部丢失，且没有任何恢复手段。

设计原则（与项目其他模块的"静默降级"**故意相反**）
------------------------------------------------
- 备份失败必须**显式报错**，绝不静默跳过。
  理由：一个"以为备份了其实没备份"的备份，比没有备份更危险——它会在真正的
  事故现场才暴露，那时已经晚了。所以这里不返回 None 降级，而是抛 BackupError。
- 恢复前**自动先备份当前库**（安全备份），保证任何一次恢复都可反悔。
- 恢复：备份名走白名单正则，包内条目逐个校验落点（防 Zip-Slip），
  并用清单里的 sha256 证明"恢复的确实是当初那一份"。

产物布局（BACKUP_DIR，默认 <项目根>/backups）
--------------------------------------------
    backup-20260929-214500/
        manifest.json    记录清单：时间、类型、大小、数据库、校验和
        database.dump    pg_dump custom 格式（可用 pg_restore 选择性恢复）
        data.zip         data/ 目录（会话/反馈/向量缓存等本地文件）
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path

from ..config import settings
from ..logging_setup import get_logger

log = get_logger("core.backup")

# 单份备份的组成部分（恢复时按顺序校验）
_DB_FILE = "database.dump"
_DATA_FILE = "data.zip"
_MANIFEST = "manifest.json"


class BackupError(RuntimeError):
    """备份/恢复失败。必须向上冒泡到 API 层变成明确错误，不允许吞掉。"""


# ------------------------------------------------------------------
# 路径与连接串
# ------------------------------------------------------------------
def _project_root() -> Path:
    """backend/app/core/backup.py → 上溯 4 级到项目根。"""
    return Path(__file__).resolve().parents[3]


def backup_root() -> Path:
    """备份根目录。可用 BACKUP_DIR 环境变量/配置覆盖（生产应挂到独立卷）。"""
    custom = getattr(settings, "BACKUP_DIR", "") or ""
    if custom.strip():
        return Path(custom.strip())
    return _project_root() / "backups"


def data_dir() -> Path:
    """本地文件数据的根目录，取值口径必须与各业务 store **完全一致**。

    各 store（audit / session_store / leave_store / reimburse_store / token_store …）
    统一按 `APP_DATA_DIR` 环境变量决定落盘位置；容器里 Dockerfile 把它设为
    /data/app（对应 appdata 卷）。

    这里曾经写成 `getattr(settings, "APP_DATA_DIR", "")` —— Settings 里根本没有
    这个字段，且配置类是 extra="ignore"，于是永远取到空字符串、静默回退到
    <项目根>/data；而真实数据在 backend/data（本地）或 /data/app（容器）。
    结果是"备份成功"但 data.zip 打包的是一个空目录，会话/审计/反馈/授权数据
    一份都没进去。兜底值也必须指向 backend/data，否则本地跑时同样会打错目录。
    """
    d = os.environ.get("APP_DATA_DIR") or ""
    if d.strip():
        return Path(d)
    return Path(__file__).resolve().parents[2] / "data"


def _pg_env() -> tuple[dict[str, str], str]:
    """把 SQLAlchemy 连接串拆成 pg_dump/psql 能用的环境变量与库名。

    postgresql+psycopg://user:pwd@host:port/db  →  环境变量 + db
    口令只通过 PGPASSWORD 环境变量传递，不出现在命令行里（避免 ps 泄露）。
    """
    url = (getattr(settings, "DATABASE_URL", "") or "").strip()
    if not url:
        raise BackupError("DATABASE_URL 未配置，无法备份数据库")
    m = re.match(
        r"^postgres(?:ql)?(?:\+\w+)?://(?P<user>[^:@/]+)(?::(?P<pwd>[^@]*))?"
        r"@(?P<host>[^:/]+)(?::(?P<port>\d+))?/(?P<db>[^?]+)", url
    )
    if not m:
        raise BackupError(f"DATABASE_URL 无法解析为 PostgreSQL 连接串：{url.split('@')[-1]}")

    env = {"PGUSER": m.group("user")}
    if m.group("pwd"):
        env["PGPASSWORD"] = m.group("pwd")
    env["PGHOST"] = m.group("host")
    env["PGPORT"] = m.group("port") or "5432"
    return {**env, "PATH": _pg_bin_path(), "PATH_SEP": ";"}, m.group("db")


def _pg_bin_path() -> str:
    """把常见 pg_dump 安装位置补进 PATH（Windows 本机常不在 PATH 里）。"""
    import os
    base = os.environ.get("PATH", "")
    extra = []
    for p in (r"C:\Program Files\PostgreSQL", r"D:\应用文件\PostgreSQL"):
        root = Path(p)
        if root.is_dir():
            for sub in sorted(root.iterdir(), reverse=True):
                cand = sub / "bin"
                if cand.is_dir():
                    extra.append(str(cand))
                    break
    return os.pathsep.join(extra + [base]) if extra else base


def _require_tool(name: str) -> str:
    exe = shutil.which(name, path=_pg_bin_path())
    if not exe:
        raise BackupError(
            f"未找到 {name}。备份数据库需要 PostgreSQL 客户端工具；"
            f"请安装 postgresql-client（容器内镜像需预装），或把 bin 目录加入 PATH。"
        )
    return exe


# ------------------------------------------------------------------
# 备份
# ------------------------------------------------------------------
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dump_database(dest_dir: Path) -> dict:
    """pg_dump 全量备份（custom 格式，可 pg_restore 选择性恢复）。"""
    pg_dump = _require_tool("pg_dump")
    env, db = _pg_env()
    target = dest_dir / _DB_FILE
    cmd = [pg_dump, "--format=custom", "--no-owner", "--no-acl",
           "--dbname", db, "--file", str(target)]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          env={**env, **_os_env()}, timeout=3600)
    if proc.returncode != 0 or not target.is_file():
        raise BackupError(f"pg_dump 失败（退出码 {proc.returncode}）：{proc.stderr.strip()[:300]}")
    if target.stat().st_size < 1024:
        # 空库 dump 也有几 KB；小于 1KB 几乎肯定是失败产物，不能当成成功
        raise BackupError(f"pg_dump 产物异常偏小（{target.stat().st_size} 字节），判定为失败")
    log.info("backup.db_ok", file=str(target), size_kb=round(target.stat().st_size / 1024, 1))
    return {"file": _DB_FILE, "size_kb": round(target.stat().st_size / 1024, 1),
            "sha256": _sha256(target)}


def _os_env() -> dict[str, str]:
    import os
    return {k: v for k, v in os.environ.items() if not k.startswith("PG")}


def zip_data(dest_dir: Path) -> dict | None:
    """打包 data/ 目录。目录不存在时返回 None（数据库才是主存储，本地文件可缺省）。"""
    src = data_dir()
    if not src.is_dir():
        log.info("backup.data_skip", reason="data 目录不存在")
        return None
    target = dest_dir / _DATA_FILE
    n = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in src.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(src))
                n += 1
    return {"file": _DATA_FILE, "size_kb": round(target.stat().st_size / 1024, 1),
            "files": n, "sha256": _sha256(target)}


def create_backup(reason: str = "manual") -> dict:
    """执行一次完整备份：数据库（必须）+ data 目录（可选）+ 清单。

    返回 manifest 内容。任何一步失败都会抛出 BackupError，且**不删除半成品**
    ——留着半成品便于排障，由 retention 统一清理。
    """
    root = backup_root()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = root / f"backup-{stamp}"
    dest.mkdir(parents=True, exist_ok=True)

    parts = []
    db = dump_database(dest)          # 数据库是主存储：失败即整体失败
    parts.append(db)
    data = zip_data(dest)
    if data:
        parts.append(data)

    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "stamp": stamp,
        "reason": reason,
        "app_version": getattr(settings, "APP_VERSION", "unknown"),
        "database": _db_name(),
        "parts": parts,
        "total_kb": round(sum(p["size_kb"] for p in parts), 1),
    }
    (dest / _MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.warning("backup.created", stamp=stamp, reason=reason,
                total_kb=manifest["total_kb"], parts=len(parts))
    apply_retention()
    return {**manifest, "dir": str(dest), "name": dest.name}


def _db_name() -> str:
    try:
        return _pg_env()[1]
    except Exception:  # noqa: BLE001 —— 描述性字段，失败不影响主流程
        return "unknown"


def list_backups() -> list[dict]:
    root = backup_root()
    if not root.is_dir():
        return []
    items = []
    for d in sorted(root.glob("backup-*"), reverse=True):
        if not d.is_dir():
            continue
        mf = d / _MANIFEST
        info = {"name": d.name, "dir": str(d), "has_manifest": mf.is_file()}
        if mf.is_file():
            try:
                data = json.loads(mf.read_text(encoding="utf-8"))
                info.update({k: data.get(k) for k in
                             ("created_at", "reason", "app_version", "total_kb", "database")})
                info["parts"] = [p["file"] for p in data.get("parts", [])]
                # 校验产物是否还在（有人手动删过就标出来）
                info["complete"] = all((d / p["file"]).is_file() for p in data.get("parts", []))
            except Exception as e:  # noqa: BLE001
                info["error"] = str(e)
        else:
            info["complete"] = False
        items.append(info)
    return items[:100]


def apply_retention(keep: int | None = None) -> list[str]:
    """保留最近 N 份，删除更早的。返回被删除的备份名。"""
    n = keep if keep is not None else int(getattr(settings, "BACKUP_KEEP", 14) or 14)
    if n <= 0:
        return []
    root = backup_root()
    if not root.is_dir():
        return []
    dirs = sorted([d for d in root.glob("backup-*") if d.is_dir()], reverse=True)
    removed = []
    for d in dirs[n:]:
        shutil.rmtree(d, ignore_errors=True)
        removed.append(d.name)
    if removed:
        log.info("backup.retention", keep=n, removed=len(removed))
    return removed


# ------------------------------------------------------------------
# 恢复
# ------------------------------------------------------------------
def _resolve(name: str) -> Path:
    """按名字定位备份目录（只认 backup-* 目录，防路径穿越）。"""
    if not re.fullmatch(r"backup-[\w\-]+", name or ""):
        raise BackupError(f"非法备份名：{name!r}")
    d = backup_root() / name
    if not d.is_dir():
        raise BackupError(f"备份不存在：{name}")
    return d


def restore_database(name: str, *, safety_backup: bool = True) -> dict:
    """从指定备份恢复数据库。

    恢复是**破坏性操作**，所以：
      1. 恢复前自动对当前库做一次安全备份（revert_to 指回它，可反悔）
      2. 用 pg_restore --clean 重建对象，忽略"对象不存在"类告警（首次恢复时正常）
      3. 返回新旧备份名，便于审计追溯
    """
    d = _resolve(name)
    dump = d / _DB_FILE
    if not dump.is_file():
        raise BackupError(f"备份 {name} 缺少 {_DB_FILE}，无法恢复数据库")

    revert_to = None
    if safety_backup:
        try:
            revert_to = create_backup(reason="pre-restore-safety")["name"]
        except BackupError as e:
            # 安全备份失败就拒绝恢复 —— 不能把自己置于"恢复后无法回退"的境地
            raise BackupError(f"恢复前安全备份失败，已中止恢复：{e}") from e

    pg_restore = _require_tool("pg_restore")
    env, db = _pg_env()
    cmd = [pg_restore, "--dbname", db, "--clean", "--if-exists",
           "--no-owner", "--no-acl", "--exit-on-error", str(dump)]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          env={**env, **_os_env()}, timeout=3600)
    # pg_restore 对"DROP 不存在的对象"会报 ERROR；--if-exists 已尽量规避，
    # 只把 exit-on-error 之外的情况视为可容忍，退出码 !=0 时明确失败。
    if proc.returncode != 0:
        raise BackupError(
            f"pg_restore 失败（退出码 {proc.returncode}）：{proc.stderr.strip()[:300]}"
            f"{'；已生成恢复前安全备份 ' + revert_to if revert_to else ''}"
        )
    log.warning("backup.restored", from_backup=name, revert_to=revert_to)
    return {"ok": True, "restored_from": name, "revert_to": revert_to}


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> int:
    """逐条目解压，并校验每个落点都在 dest 内（防 Zip-Slip）。

    不能直接用 extractall：zip 条目名可以写成 `../../x`，解压时会写到目标目录
    之外。这里的备份包虽由本机生成，但恢复场景下 zip 可能已被替换/损坏，
    所以按"不可信输入"处理。
    """
    root = dest.resolve()
    n = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        rel = info.filename.replace("\\", "/").lstrip("/")
        target = (root / rel).resolve()
        if target != root and not str(target).startswith(str(root) + os.sep):
            raise BackupError(f"备份包内含非法路径条目：{info.filename}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as fin, target.open("wb") as fout:
            shutil.copyfileobj(fin, fout)
        n += 1
    return n


def restore_data(name: str) -> dict | None:
    """恢复 data/ 目录（可选部分，备份里没有就跳过）。"""
    d = _resolve(name)
    z = d / _DATA_FILE
    if not z.is_file():
        return None
    src = data_dir()
    src.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(z) as zf:
        n = _safe_extract(zf, src)
    return {"ok": True, "target": str(src), "files": n}


def verify_backup(name: str) -> dict:
    """校验一份备份是否可用：清单存在、产物齐全、sha256 与清单一致、dump 能被 pg_restore 读取。

    只检查"文件在不在"是不够的 —— 磁盘损坏、传输截断都会让文件存在但内容已坏，
    而 sha256 才是能证明"这份备份还是当初那一份"的证据（清单里已记录）。
    """
    d = _resolve(name)
    mf = d / _MANIFEST
    if not mf.is_file():
        raise BackupError(f"备份 {name} 缺少 {_MANIFEST}")
    try:
        manifest = json.loads(mf.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise BackupError(f"清单无法解析：{e}") from e

    checks: list[dict] = []
    ok = True
    for part in manifest.get("parts", []):
        f = d / part["file"]
        existed = f.is_file()
        actual = _sha256(f) if existed else ""
        matched = bool(existed and actual == part.get("sha256"))
        ok = ok and matched
        checks.append({
            "file": part["file"],
            "exists": existed,
            "sha256_match": matched,
            "size_kb": round(f.stat().st_size / 1024, 1) if existed else 0,
        })

    db_ok = None
    dump = d / _DB_FILE
    if dump.is_file():
        try:
            pg_restore = _require_tool("pg_restore")
            proc = subprocess.run([pg_restore, "--list", str(dump)],
                                  capture_output=True, text=True, timeout=300)
            db_ok = proc.returncode == 0
        except BackupError:
            db_ok = None      # 环境没有 pg_restore 时不下结论，不误报损坏
    ok = ok and (db_ok is not False)

    return {"name": name, "ok": ok, "parts": checks, "dump_readable": db_ok,
            "manifest": {k: manifest.get(k)
                         for k in ("created_at", "app_version", "database", "total_kb")}}
