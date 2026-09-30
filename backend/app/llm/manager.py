"""
LLM 管理器 —— 三级降级 + 熔断。

面试话术落地：
  ① 主模型首字超时（默认 3s）→ 自动切备用模型，用户无感
  ② 连续失败 N 次 → 熔断 30s，不再浪费请求打在挂掉的节点上
  ③ 全部不可用 → 返回友好提示（LLMUnavailable），绝不抛裸异常给前端
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

from ..config import settings
from ..errors import LLMUnavailable
from ..logging_setup import get_logger, node_timer
from ..metrics import LLM_CALLS, LLM_TOKENS
from .base import LLMMessage, LLMBackend

log = get_logger("llm")


class CircuitBreaker:
    """极简熔断器：连续失败阈值 + 恢复窗口。"""

    def __init__(self, threshold: int, recover_seconds: int):
        self.threshold = threshold
        self.recover = recover_seconds
        self._fails: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}

    def is_open(self, key: str) -> bool:
        opened = self._opened_at.get(key)
        if opened is None:
            return False
        if time.time() - opened >= self.recover:
            # 半开：放行一次探测请求
            self._opened_at.pop(key, None)
            self._fails[key] = 0
            return False
        return True

    def record_fail(self, key: str):
        self._fails[key] = self._fails.get(key, 0) + 1
        if self._fails[key] >= self.threshold:
            self._opened_at[key] = time.time()
            log.warning("llm.circuit_open", backend=key, fails=self._fails[key])

    def record_success(self, key: str):
        self._fails[key] = 0
        self._opened_at.pop(key, None)


class LLMManager:
    def __init__(self, primary: LLMBackend, fallback: LLMBackend | None = None):
        self.primary = primary
        self.fallback = fallback
        self.breaker = CircuitBreaker(
            settings.LLM_CIRCUIT_FAIL_THRESHOLD, settings.LLM_CIRCUIT_RECOVER_SECONDS
        )

    @property
    def chain(self) -> list[LLMBackend]:
        return [b for b in (self.primary, self.fallback) if b is not None]

    @property
    def primary_key(self) -> str:
        return f"{self.primary.name}:{self.primary.model}" if self.primary else ""

    async def astream(
        self,
        messages: list[LLMMessage],
        temperature: float | None = None,
        max_tokens: int | None = None,
        meta: dict | None = None,
    ) -> AsyncIterator[str]:
        last_err: Exception | None = None

        for backend in self.chain:
            key = f"{backend.name}:{backend.model}"
            if self.breaker.is_open(key):
                log.warning("llm.skip_by_circuit", backend=key)
                continue

            try:
                gen = backend.astream(
                    messages,
                    temperature=temperature if temperature is not None else settings.LLM_TEMPERATURE,
                    max_tokens=max_tokens or settings.LLM_MAX_TOKENS,
                )
                it = gen.__aiter__()
                # ① 首字超时保护：等不到第一个 token 就切下一个后端
                first = await asyncio.wait_for(it.__anext__(), timeout=settings.LLM_TIMEOUT_SECONDS)
                self.breaker.record_success(key)
                LLM_CALLS.labels(model=backend.model, status="ok").inc()
                # 上层通过 meta 感知"实际由哪个后端应答"（降级答案不写缓存等决策用）。
                # 在拿到第一个 token 后才写入：只有真正服务成功的后端才算数。
                if meta is not None:
                    meta["backend"] = key
                yield first
                LLM_TOKENS.labels(model=backend.model).inc()

                async for tok in it:
                    yield tok
                    LLM_TOKENS.labels(model=backend.model).inc()
                return

            except asyncio.TimeoutError:
                last_err = TimeoutError(f"{key} 首字超时 {settings.LLM_TIMEOUT_SECONDS}s")
                self.breaker.record_fail(key)
                LLM_CALLS.labels(model=backend.model, status="timeout").inc()
                log.warning("llm.first_token_timeout", backend=key)
                continue

            except StopAsyncIteration:
                # 后端返回空流，视为异常并降级
                last_err = RuntimeError(f"{key} 返回空响应")
                self.breaker.record_fail(key)
                LLM_CALLS.labels(model=backend.model, status="empty").inc()
                continue

            except Exception as e:  # noqa: BLE001
                last_err = e
                self.breaker.record_fail(key)
                LLM_CALLS.labels(model=backend.model, status="error").inc()
                log.error("llm.call_failed", backend=key, error=str(e))
                continue

        # ③ 全部不可用
        LLM_CALLS.labels(model="none", status="unavailable").inc()
        log.error("llm.all_unavailable", last_error=str(last_err))
        raise LLMUnavailable(str(last_err) if last_err else None)

    async def generate(self, messages: list[LLMMessage], **kw) -> str:
        """非流式一次性生成（用于意图兜底、摘要压缩等短调用）。"""
        buf: list[str] = []
        async for tok in self.astream(messages, **kw):
            buf.append(tok)
        return "".join(buf)

    async def health(self) -> list[tuple[str, bool, str]]:
        out = []
        for b in self.chain:
            ok, msg = await b.health()
            out.append((f"{b.name}:{b.model}", ok, msg))
        return out
