"""
令牌存储（企业级会话安全）：
  · 刷新令牌白名单（VALID_REFRESH）：登录/刷新时登记 jti，登出或轮转时撤销。
    仅白名单内的刷新令牌可用于换取新访问令牌，被盗用的旧刷新令牌立即失效。
  · 访问令牌黑名单（BLACKLIST）：登出时把当前访问令牌的 jti 拉黑至其到期时刻，
    即便令牌未过期也无法再用于任何接口。

生产替换：VAL_VAL_REFRESH / BLACKLIST 可平移到 Redis（SETEX + 带 TTL 的集合），
函数签名保持不变，业务层无感。当前用 JSON 落盘保证单实例重启不丢失、零外部依赖。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from ..config import settings

_REFRESH_PATH = Path(os.environ.get("APP_DATA_DIR") or (Path(__file__).resolve().parents[2] / "data")) / "refresh_tokens.json"
_BLACKLIST_PATH = Path(os.environ.get("APP_DATA_DIR") or (Path(__file__).resolve().parents[2] / "data")) / "token_blacklist.json"

_LOCK = threading.RLock()
_VALID_REFRESH: set[str] = set()
_BLACKLIST: dict[str, float] = {}   # jti -> 过期时间戳（unix）
_LOADED = False
# 文件指纹（mtime, size）：用于发现"其他 worker 已经改过这份状态"
_REFRESH_SIG: tuple[float, int] = (-1.0, -1)
_BLACKLIST_SIG: tuple[float, int] = (-1.0, -1)


def _sig(path: Path) -> tuple[float, int]:
    try:
        st = path.stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return (-1.0, -1)


def _load(force: bool = False) -> None:
    """把磁盘状态装载进内存，并按文件指纹**增量重载**。

    为什么要重载而不是只读一次：
      生产镜像里 uvicorn 以 --workers 2 启动，两个 worker 是同一容器内的两个进程、
      共享同一份卷上的 JSON 文件。旧实现在首次 _load() 后就把 _LOADED 置 True 永久
      返回，导致 worker A 登录/登出写入的黑名单，worker B 永远看不到——用户"点了退出
      却还能继续用"，或刷新令牌轮转被另一进程当成未撤销。按 mtime+size 变化重读，
      即可让两个 worker 在秒级内收敛到同一份状态。
    """
    global _LOADED, _VALID_REFRESH, _BLACKLIST, _REFRESH_SIG, _BLACKLIST_SIG
    rsig, bsig = _sig(_REFRESH_PATH), _sig(_BLACKLIST_PATH)
    refresh_changed = (not _LOADED) or force or rsig != _REFRESH_SIG
    blacklist_changed = (not _LOADED) or force or bsig != _BLACKLIST_SIG
    if not refresh_changed and not blacklist_changed:
        return

    if refresh_changed:
        _REFRESH_SIG = rsig
        try:
            if _REFRESH_PATH.exists():
                raw = json.loads(_REFRESH_PATH.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    _VALID_REFRESH = set(raw)
        except Exception:  # noqa: BLE001
            pass
    if blacklist_changed:
        _BLACKLIST_SIG = bsig
        try:
            if _BLACKLIST_PATH.exists():
                raw = json.loads(_BLACKLIST_PATH.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    now = time.time()
                    # 顺手清掉已过期的黑名单项
                    _BLACKLIST = {k: v for k, v in raw.items() if v > now}
        except Exception:  # noqa: BLE001
            pass
    _LOADED = True


def _save_refresh() -> None:
    global _REFRESH_SIG
    try:
        _REFRESH_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _REFRESH_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(_VALID_REFRESH), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _REFRESH_PATH)
        _REFRESH_SIG = _sig(_REFRESH_PATH)
    except Exception:  # noqa: BLE001
        pass


def _save_blacklist() -> None:
    global _BLACKLIST_SIG
    try:
        _BLACKLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _BLACKLIST_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_BLACKLIST, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _BLACKLIST_PATH)
        _BLACKLIST_SIG = _sig(_BLACKLIST_PATH)
    except Exception:  # noqa: BLE001
        pass


def add_refresh_jti(jti: str) -> None:
    with _LOCK:
        _load()
        _VALID_REFRESH.add(jti)
        _save_refresh()


def is_refresh_valid(jti: str | None) -> bool:
    if not jti:
        return False
    with _LOCK:
        _load()
        return jti in _VALID_REFRESH


def revoke_refresh_jti(jti: str | None) -> None:
    if not jti:
        return
    with _LOCK:
        _load()
        if jti in _VALID_REFRESH:
            _VALID_REFRESH.discard(jti)
            _save_refresh()


def blacklist_access_jti(jti: str | None, exp: float | None) -> None:
    """访问令牌拉黑至其到期时刻；无 exp 时按一个刷新周期兜底。"""
    if not jti:
        return
    exp = exp or (time.time() + settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400)
    with _LOCK:
        _load()
        _BLACKLIST[jti] = float(exp)
        _save_blacklist()


def is_access_blacklisted(jti: str | None, now: float | None = None) -> bool:
    if not jti:
        return False
    with _LOCK:
        _load()
        exp = _BLACKLIST.get(jti)
        if exp is None:
            return False
        if exp <= (now or time.time()):
            _BLACKLIST.pop(jti, None)
            _save_blacklist()
            return False
        return True
