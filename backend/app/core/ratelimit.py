"""
每用户滑动窗口限流（企业级防滥用基线）。

内存实现，单进程即可用；生产多副本部署时替换为 Redis ZSET 即可（接口不变）。
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from ..config import settings

_LOCK = threading.RLock()
_HITS: dict[str, deque[float]] = defaultdict(deque)


def check(user_id: str) -> tuple[bool, int]:
    """
    尝试记录一次问答请求。
    返回 (allowed, retry_after_seconds)：超限时 allowed=False，并给出建议等待秒数。
    """
    limit = settings.RATE_LIMIT_PER_MIN
    if limit <= 0:                      # 限流关闭
        return True, 0
    now = time.time()
    window = settings.RATE_LIMIT_WINDOW_SECONDS
    with _LOCK:
        q = _HITS[user_id]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            retry = int(window - (now - q[0])) + 1
            return False, max(retry, 1)
        q.append(now)
        return True, 0


def remaining(user_id: str) -> int:
    """本窗口内剩余可用次数（供管理端观测）。"""
    limit = settings.RATE_LIMIT_PER_MIN
    if limit <= 0:
        return -1
    now = time.time()
    window = settings.RATE_LIMIT_WINDOW_SECONDS
    with _LOCK:
        q = _HITS[user_id]
        while q and now - q[0] > window:
            q.popleft()
        return max(limit - len(q), 0)
