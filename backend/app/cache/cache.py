"""
高频问答缓存 —— Redis 为主，进程内 LRU 为降级（需求书 FR-CHAT-04 / NFR-USA-03）。

要点：
  1. 缓存 Key = MD5(问题 + 排序后的 dept_ids)：不同部门不同 Key，权限不串味
  2. 文档更新时按部门前缀批量失效，避免读到旧知识
  3. Redis 挂了自动降级为本地 LRU，问答链路不受影响
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict

from ..config import settings
from ..logging_setup import get_logger
from ..metrics import CACHE_HITS

log = get_logger("cache")


def cache_key(question: str, dept_ids: list[str]) -> str:
    norm = " ".join(question.strip().lower().split())
    depts = ",".join(sorted(dept_ids or []))
    raw = f"{norm}|{depts}"
    return "qa:" + hashlib.md5(raw.encode("utf-8")).hexdigest()


class CacheBackend:
    def __init__(self):
        self._redis = None
        self._local: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
        self.mode = "memory"
        self._init_redis()

    def _init_redis(self):
        if not settings.REDIS_ENABLED:
            log.info("cache.disabled")
            return
        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            self.mode = "redis"
            log.info("cache.redis_ready", url=settings.REDIS_URL)
        except Exception as e:  # noqa: BLE001
            log.warning("cache.redis_unavailable", error=str(e))
            self._redis = None
            self.mode = "memory"

    # ---------------- 读 ----------------
    async def get(self, key: str) -> dict | None:
        raw = None
        if self._redis is not None:
            try:
                raw = await self._redis.get(key)
            except Exception as e:  # noqa: BLE001
                log.warning("cache.redis_get_failed", error=str(e))
                if settings.CACHE_AUTO_FALLBACK:
                    self._redis = None
                    self.mode = "memory"
                raw = None

        if raw is None:
            hit = self._local.get(key)
            if hit:
                value, expire = hit
                if time.time() < expire:
                    self._local.move_to_end(key)
                    raw = value
                else:
                    self._local.pop(key, None)

        if raw:
            try:
                CACHE_HITS.labels(result="hit").inc()
                return json.loads(raw)
            except json.JSONDecodeError:
                return None
        CACHE_HITS.labels(result="miss").inc()
        return None

    # ---------------- 写 ----------------
    async def set(self, key: str, value: dict, ttl: int | None = None):
        ttl = ttl or settings.CACHE_TTL_SECONDS
        payload = json.dumps(value, ensure_ascii=False)
        if self._redis is not None:
            try:
                await self._redis.setex(key, ttl, payload)
                return
            except Exception as e:  # noqa: BLE001
                log.warning("cache.redis_set_failed", error=str(e))
                if settings.CACHE_AUTO_FALLBACK:
                    self._redis = None
                    self.mode = "memory"
        if len(self._local) >= settings.CACHE_MAX_ITEMS:
            self._local.popitem(last=False)
        self._local[key] = (payload, time.time() + ttl)

    # ---------------- 失效 ----------------
    async def invalidate_department(self, department_id: str, question_hashes: list[str] | None = None):
        """
        文档更新后清除该部门相关缓存。
        Redis 侧：维护 dept -> keys 的集合，按集合批量删；本地侧按前缀扫描。
        """
        if question_hashes:
            for h in question_hashes:
                await self.delete(h)
        # 部门级集合失效
        idx_key = f"qa:dept:{department_id}"
        if self._redis is not None:
            try:
                keys = await self._redis.smembers(idx_key)
                if keys:
                    await self._redis.delete(*list(keys))
                await self._redis.delete(idx_key)
            except Exception as e:  # noqa: BLE001
                log.warning("cache.invalidate_failed", error=str(e))
        for k in list(self._local.keys()):
            self._local.pop(k, None)
        log.info("cache.invalidated", department_id=department_id)

    async def index_key(self, department_id: str, key: str):
        """把缓存 key 登记到部门索引里，便于后续批量失效。"""
        if self._redis is not None:
            try:
                await self._redis.sadd(f"qa:dept:{department_id}", key)
                await self._redis.expire(f"qa:dept:{department_id}", settings.CACHE_TTL_SECONDS * 2)
            except Exception:  # noqa: BLE001
                pass

    async def delete(self, key: str):
        if self._redis is not None:
            try:
                await self._redis.delete(key)
            except Exception:  # noqa: BLE001
                pass
        self._local.pop(key, None)

    async def health(self) -> tuple[bool, str]:
        if self.mode == "redis" and self._redis is not None:
            try:
                await self._redis.ping()
                return True, "Redis 正常"
            except Exception as e:  # noqa: BLE001
                return False, f"Redis 不可达: {type(e).__name__}"
        return True, f"本地 LRU 缓存（降级模式），{len(self._local)} 条"


_cache: CacheBackend | None = None


def get_cache() -> CacheBackend:
    global _cache
    if _cache is None:
        _cache = CacheBackend()
    return _cache
