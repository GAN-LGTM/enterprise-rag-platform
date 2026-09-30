"""LLM 后端统一抽象：所有后端都必须产出 token 级异步流，这样上层 SSE 才能逐字推送。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass
class LLMMessage:
    role: str          # system / user / assistant
    content: str


class LLMBackend(ABC):
    name: str = "base"
    model: str = "unknown"

    @abstractmethod
    async def astream(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> AsyncIterator[str]:
        """逐 token yield 文本片段。"""
        raise NotImplementedError

    async def health(self) -> tuple[bool, str]:
        return True, "ok"
