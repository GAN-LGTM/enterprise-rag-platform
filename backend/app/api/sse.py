"""
SSE（Server-Sent Events）协议工具 + 事件类型定义。

协议设计（前端按 event 类型分派）：
  event: meta       首帧，携带 trace_id / 意图 / 权限范围，前端可立即渲染状态条
  event: stage      节点级进度（classify → permission → retrieve → generate）
  event: citation   溯源卡片（在正文开始前推，前端可以先渲染引用区）
  event: token      字级增量，前端逐字拼接
  event: done       结束帧，带完整答案、耗时、各节点 timings
  event: error      异常帧

为什么不用浏览器原生 EventSource：
  EventSource 只支持 GET，无法携带 Authorization 头和 JSON body。
  所以后端走标准 SSE 帧格式，前端用 fetch + ReadableStream 自己解析 ——
  兼容性更好，也方便统一走网关鉴权。
"""
from __future__ import annotations

import json
from typing import Any

# 事件类型
META = "meta"
STAGE = "stage"
CITATION = "citation"
TOKEN = "token"
DONE = "done"
ERROR = "error"

HEARTBEAT = ": ping\n\n"


def sse_frame(event: str, data: Any) -> str:
    """组装一个标准 SSE 帧。data 用 JSON 单行序列化，天然不含换行。"""
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


STREAM_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",      # 关键：关掉 Nginx 缓冲，否则流式会变成一次性返回
    "Content-Type": "text/event-stream; charset=utf-8",
}
