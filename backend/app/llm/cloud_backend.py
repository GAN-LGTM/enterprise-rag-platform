"""
WorkBuddy 云端 LLM 后端 —— 免密钥大模型通道（OpenAI 兼容流式）。

用途：本地/演示环境没有 vLLM 与 GPU 时，接入云端模型获得真实对话能力。
认证：请求头 x-wb-webapp-access-key 携带应用的 publishableKey（服务端配置，不进前端）。
说明：接口仅支持 stream=true；思维链(reasoning_content)单独下发，这里只取正文 content。
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from ..config import settings
from .base import LLMBackend, LLMMessage


class CloudLLMBackend(LLMBackend):
    name = "cloud"

    def __init__(
        self,
        endpoint: str | None = None,
        publishable_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ):
        self.endpoint = (endpoint or settings.CLOUD_LLM_ENDPOINT).rstrip("/")
        self.publishable_key = publishable_key or settings.CLOUD_LLM_PUBLISHABLE_KEY
        self.model = model or settings.CLOUD_LLM_MODEL
        self.timeout = timeout or 60.0

    def _headers(self) -> dict[str, str]:
        return {
            "x-wb-webapp-access-key": self.publishable_key,
            "Origin": self.endpoint,
            "Content-Type": "application/json",
        }

    async def astream(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> AsyncIterator[str]:
        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=15.0), trust_env=False
        ) as client:
            async with client.stream(
                "POST", f"{self.endpoint}/.cloud/llm/chat/completions",
                json=payload, headers=self._headers(),
            ) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "ignore")[:300]
                    raise RuntimeError(f"cloud llm http {resp.status_code}: {body}")
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or [{}]
                    delta = choices[0].get("delta") or {}
                    # 只透出正文；思维链不入答案流
                    content = delta.get("content")
                    if content:
                        yield content

    async def health(self) -> tuple[bool, str]:
        try:
            async with httpx.AsyncClient(timeout=8.0, trust_env=False) as client:
                r = await client.get(
                    f"{self.endpoint}/.cloud/llm/models", headers=self._headers()
                )
                r.raise_for_status()
                models = r.json()
                ids = [m.get("id") for m in models][:4]
                return True, f"cloud ok, models={ids}"
        except Exception as e:  # noqa: BLE001
            return False, f"云端模型不可达: {type(e).__name__}: {e}"
