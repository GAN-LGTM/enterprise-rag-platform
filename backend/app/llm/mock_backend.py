"""
无 GPU 环境下的模拟后端：逐字 yield，节奏贴近真实推理（20-30 token/s）。

用途：
  1. 开发/演示阶段打通 SSE 全链路，不依赖显卡
  2. 压测时作为稳定的下游桩
  3. 客户 POC 环境还没采购 GPU 时的临时方案

它会读取检索到的上下文片段，拼出一段「看起来像模像样」的答案，
保证前端演示时能看到"引用了哪些文档"的效果。
"""
from __future__ import annotations

import asyncio
import random
import re
from collections.abc import AsyncIterator

from .base import LLMMessage, LLMBackend

_CHITCHAT_TEMPLATES = [
    "我是企业知识检索中台的智能助手，可以帮你查公司制度、部门业务知识和跨部门资料。",
    "这个问题我可以直接回答～不过如果你要查公司制度或部门文档，直接问我就好，我会带上来源。",
]


def _build_answer(question: str, context: str, has_citations: bool) -> str:
    if not has_citations:
        return random.choice(_CHITCHAT_TEMPLATES)

    return (
        f"根据知识库检索结果，针对「{question}」为您整理如下：\n\n"
        f"{context}\n\n"
        "**小结**\n"
        "1. 以上内容来自您有权限访问的知识库，已在下方标注来源与置信度；\n"
        "2. 如需查看原文，可点击引用卡片跳转对应文档与页码；\n"
        "3. 若答案与实际情况有出入，请点👎反馈，我们会持续优化检索质量。\n"
    )


class MockBackend(LLMBackend):
    name = "mock"
    model = "mock-stream"

    def __init__(self, tokens_per_second: float = 26.0):
        self.tps = tokens_per_second

    async def astream(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> AsyncIterator[str]:
        system = next((m.content for m in messages if m.role == "system"), "")
        # 必须取最后一条 user 消息：多轮对话时消息列表形如
        # [system, 历史 user, 历史 assistant, ..., user(【参考资料】+【用户问题】)]。
        # 若取第一条 user 消息（往往是上一轮的普通提问，不含【参考资料】），
        # mock 会误判为闲聊，把检索成功的问题答成欢迎语——且该错误答案
        # 会被写入问答缓存，在 TTL 内反复污染后续相同提问。
        user_msgs = [m.content for m in messages if m.role == "user"]
        user = user_msgs[-1] if user_msgs else ""

        question = user.split("【用户问题】")[-1].split("\n")[0].strip() if "【用户问题】" in user else user[:80]
        has_citations = "【参考资料】" in user
        context = ""
        if has_citations:
            context = user.split("【参考资料】")[-1].split("【用户问题】")[0].strip()
            # 抽取片段正文，去掉编号前缀
            lines = []
            for ln in context.split("\n"):
                s = ln.strip()
                if not s:
                    continue
                if s.startswith("[") and "]" in s:
                    s = s.split("]", 1)[1].strip()
                if s.startswith("来源") or s.startswith("置信度"):
                    continue
                lines.append(s)
            # 摘要式呈现：去掉 markdown 记号、控制篇幅，避免降级作答时
            # 把制度原文整段倾倒到回答区（此前用户反馈过"满屏 # 乱码"）。
            cleaned = []
            for s in lines[:6]:
                s = re.sub(r"[#*`>|]+", "", s).strip()
                if len(s) > 80:
                    s = s[:80] + "…"
                if s:
                    cleaned.append(s)
            context = "\n".join(cleaned)

        text = _build_answer(question, context, has_citations)
        delay = 1.0 / self.tps
        for ch in text:
            if ch == "\n":
                await asyncio.sleep(delay * 2)
            else:
                await asyncio.sleep(delay)
            yield ch
