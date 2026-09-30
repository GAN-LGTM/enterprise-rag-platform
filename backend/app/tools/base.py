"""
外部实时工具的公共契约。

所有实时工具（天气 / 行情 / 后续可接的节假日、物流、汇率…）对外只有一个约定：

    输入：用户问题（已脱敏）
    输出：RealtimeResult 或 None

返回 None 的含义是「这个工具答不了」，工作流会静默降级走原链路（检索 / 闲聊），
绝不会因为某个外部接口抖动就把整轮问答拖挂 —— 这是系统级降级容错的一环。

把契约单独放一个文件，是为了让工具可以互相独立：
新增工具不依赖其他工具模块，避免 import 成环，也避免"为了复用一个 dataclass
而被迫引入整个天气实现"。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RealtimeResult:
    """外部工具的统一返回。"""

    tool: str                 # 工具名：写入 retrieve 诊断与日志（如 weather / market）
    text: str                 # 给 LLM 看的结构化数据块（已经排版好，可直接进 Prompt）
    summary: str              # 给日志看的一句话摘要
    answer_source: str = ""   # 来源标识（驱动前端徽章与缓存策略）；留空表示与 tool 同名
