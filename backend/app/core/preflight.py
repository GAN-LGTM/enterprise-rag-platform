"""启动自检（preflight）：交付前的安全基线校验 + 初始管理员账号准备。

设计原则：
  * dev（本机/联调）  —— 只告警，不阻断，保证无 GPU、无 PG 也能一键跑通；
  * production（甲方私有化交付）—— 不合规**直接拒绝启动**，避免带着演示数据、
    默认密钥或伪向量上线（这类问题上线后极难发现，但后果严重）。

自检项：JWT 密钥、演示模式、向量后端、数据库模式、LLM 后端、CORS 白名单、
目录可写性、初始管理员账号。
"""
from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

from ..config import settings
from ..logging_setup import get_logger

log = get_logger("preflight")

_DEFAULT_JWT_SECRET = "please-change-this-secret-in-production-32bytes-minimum!"


class PreflightError(Exception):
    """生产环境安全基线不合规，服务必须拒绝启动。"""


def _data_dir() -> Path:
    return Path(os.environ.get("APP_DATA_DIR") or (Path(__file__).resolve().parents[2] / "data"))


def _check_writable(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    probe = path / ".write_probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()


def _ensure_admin() -> str:
    """确保存在一个可登录的管理员账号，返回提示文案。

    生产环境若未通过 ADMIN_INITIAL_PASSWORD 提供初始密码，则随机生成并**只写一次**
    到 data/initial_admin.txt（文件权限 600），避免密码出现在镜像或日志里。
    """
    from ..core.security import hash_password
    from ..db.models import User, USERS

    username = (settings.ADMIN_INITIAL_USERNAME or "admin").strip()
    if username in USERS:
        return f"管理员账号已存在：{username}"

    password = (settings.ADMIN_INITIAL_PASSWORD or "").strip()
    generated = False
    if not password:
        password = secrets.token_urlsafe(16)
        generated = True

    USERS[username] = User(
        user_id="u_admin", username=username, password_hash=hash_password(password),
        display_name="系统管理员", role="admin", dept_id="*", managed_dept_ids=[],
        can_upload=True, must_change_password=settings.ADMIN_FORCE_PASSWORD_CHANGE,
    )

    if generated:
        d = _data_dir()
        _check_writable(d)
        f = d / "initial_admin.txt"
        f.write_text(
            f"username: {username}\npassword: {password}\n"
            "注意：该文件仅在首次启动生成，请登录后立即修改密码并删除本文件。\n",
            encoding="utf-8",
        )
        try:
            os.chmod(f, stat.S_IRUSR | stat.S_IWUSR)   # 600：仅属主可读
        except Exception:  # noqa: BLE001  Windows 下 chmod 能力有限，忽略
            pass
        log.warning("admin.password_generated", path=str(f))
        return f"已生成初始管理员：{username}，密码见 {f}（首次登录强制改密，请登录后删除该文件）"

    return f"已初始化管理员账号：{username}"


def run(verbose: bool = True) -> list[dict]:
    """执行自检。返回检查项列表；生产环境不合规时抛 PreflightError。"""
    prod = settings.IS_PRODUCTION
    items: list[dict] = []

    def add(level: str, name: str, msg: str, fatal: bool = False):
        items.append({"level": level, "item": name, "message": msg})
        if fatal:
            raise PreflightError(f"[{name}] {msg}")
        getattr(log, "warning" if level == "warn" else "info")("preflight." + name, message=msg)

    # 1) 环境与演示模式
    if settings.DEMO_MODE and prod:
        add("error", "demo_mode", "生产环境禁止 DEMO_MODE=true（会加载演示账号与示例语料）", True)
    else:
        add("ok", "demo_mode", f"演示模式={'开' if settings.DEMO_MODE else '关'}")

    # 2) JWT 密钥
    weak = (settings.JWT_SECRET_KEY == _DEFAULT_JWT_SECRET) or len(settings.JWT_SECRET_KEY) < 32
    if weak:
        add("error" if prod else "warn", "jwt_secret",
            "JWT_SECRET_KEY 仍为默认值或长度不足 32 位，必须替换为随机密钥"
            + ("（生产环境已拒绝启动）" if prod else "（dev 仅告警）"), fatal=prod)

    # 3) 向量后端：hash 是伪向量，不能用于生产
    if settings.EMBEDDING_BACKEND == "hash":
        add("error" if prod else "warn", "embedding_backend",
            "EMBEDDING_BACKEND=hash 为离线伪向量，检索结果不可信；生产请配置 local(BGE) 或 http"
            + ("（生产环境已拒绝启动）" if prod else "（dev 仅告警）"), fatal=prod)

    # 4) 数据库：生产不允许跳过 PostgreSQL
    if settings.DB_FORCE_MEMORY:
        add("error" if prod else "warn", "database",
            "DB_FORCE_MEMORY=true 使用内存向量库，重启即丢；生产必须接 PostgreSQL"
            + ("（生产环境已拒绝启动）" if prod else "（dev 仅告警）"), fatal=prod)

    # 5) LLM 后端
    if settings.LLM_BACKEND == "mock":
        add("error" if prod else "warn", "llm_backend",
            "LLM_BACKEND=mock 只输出模拟文本，不能用于生产"
            + ("（生产环境已拒绝启动）" if prod else "（dev 仅告警）"), fatal=prod)
    elif settings.LLM_BACKEND == "cloud" and prod and not settings.ALLOW_CLOUD_LLM:
        add("error", "llm_backend",
            "私有化部署禁止默认外联云端 LLM（OFFLINE_MODE 要求零外网流量）；"
            "请配置本地 vLLM，或显式允许 ALLOW_CLOUD_LLM=true", fatal=True)

    # 6) CORS
    if "*" in settings.ALLOWED_ORIGINS:
        add("error" if prod else "warn", "cors",
            "ALLOWED_ORIGINS=* 允许任意来源跨域；生产请填写实际域名"
            + ("（生产环境已拒绝启动）" if prod else "（dev 仅告警）"), fatal=prod)

    # 6.5) 公网搜索 / 外部工具：OFFLINE_MODE=true 却开启公网搜索，与"零外网流量"承诺冲突
    if settings.PUBLIC_SEARCH_ENABLED and settings.OFFLINE_MODE:
        add("warn", "public_search",
            "OFFLINE_MODE=true 但 PUBLIC_SEARCH_ENABLED=true：联网问答（必应/搜狗/天气）将产生外网请求，"
            "与'零外网流量'不符；合规零外网场景请设 PUBLIC_SEARCH_ENABLED=false")
    else:
        add("ok", "public_search", f"公网搜索/外部工具={'开' if settings.PUBLIC_SEARCH_ENABLED else '关'}"
            + ("（OFFLINE_MODE=true 默认关闭）" if (not settings.PUBLIC_SEARCH_ENABLED and settings.OFFLINE_MODE) else ""))

    # 7) 首次登录强制改密
    if prod and not settings.FORCE_PASSWORD_CHANGE_ON_FIRST_LOGIN:
        add("warn", "force_password_change",
            "生产建议开启 FORCE_PASSWORD_CHANGE_ON_FIRST_LOGIN=true，避免初始口令长期有效")
    elif settings.FORCE_PASSWORD_CHANGE_ON_FIRST_LOGIN:
        add("ok", "force_password_change", "已开启首次登录强制改密")

    # 8) 目录可写性
    try:
        _check_writable(_data_dir())
        add("ok", "data_dir", f"数据目录可写：{_data_dir()}")
    except Exception as e:  # noqa: BLE001
        add("error", "data_dir", f"数据目录不可写：{e}", fatal=prod)

    try:
        _check_writable(Path(settings.DOC_UPLOAD_DIR))
        add("ok", "upload_dir", f"上传目录可写：{settings.DOC_UPLOAD_DIR}")
    except Exception as e:  # noqa: BLE001
        add("error", "upload_dir", f"上传目录不可写：{e}", fatal=prod)

    # 9) 管理员账号
    try:
        add("ok", "admin_account", _ensure_admin())
    except Exception as e:  # noqa: BLE001
        add("error", "admin_account", f"初始化管理员失败：{e}", fatal=prod)

    if verbose:
        # 走统一日志（分级 + 落盘），不再用 print：
        # 文件里是 JSON（可检索），控制台是人类可读文本，交付后由运维统一采集。
        log.info("preflight.start", environment=settings.ENVIRONMENT,
                 fatal_errors=sum(1 for it in items if it["level"] == "error"),
                 warnings=sum(1 for it in items if it["level"] == "warn"))
        for it in items:
            log_fn = {"ok": log.info, "warn": log.warning, "error": log.error}.get(it["level"], log.info)
            log_fn("preflight.item", item=it["item"], level=it["level"], message=it["message"])
        log.info("preflight.done", environment=settings.ENVIRONMENT, items=len(items))

    return items
