"""中断后继续问答：验证点击停止后，半成品答案不会被写入缓存和会话历史。

对应用户反馈：前端点击“停止”后，再提同样/类似问题不应接着上次的内容，
而应照常重新回答。根因是后端 producer 在 finally 中把半成品写进了缓存/历史；
本测试直接驱动 chat._producer 验证取消后这两处都不写入。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

os.environ.setdefault("DB_FORCE_MEMORY", "true")
os.environ.setdefault("REDIS_ENABLED", "false")
os.environ["APP_DATA_DIR"] = tempfile.mkdtemp()

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "backend"))

PASS, FAIL = 0, 0
FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok {name}")
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"  FAIL {name}  {extra}")


from fastapi.testclient import TestClient
from app.main import app
from app.api.chat import _producer
from app.cache.cache import cache_key
from app.schemas.dto import ChatStreamRequest


def make_state(question: str, trace_id: str = "t-int"):
    return {
        "trace_id": trace_id,
        "user": None,
        "question": question,
        "session_id": "int-test",
        "history": [],
        "last_intent": None,
        "cache_enabled": True,
        "timings": {},
    }


async def slow_run_stream(state):
    """慢速生成器，让取消有机会发生在完成之前。"""
    yield "stage", "classify_intent", {"intent": "chitchat", "intent_confidence": 0.95}
    yield "stage", "permission_gate", {"allowed": True, "dept_ids": []}
    yield "stage", "retrieve", {"answer_source": "chat", "cache_hit": False,
                                "retrieval_diag": {"source": "skip"}}
    for i in range(50):
        await asyncio.sleep(0.02)
        yield "token", f"字{i}-", None
    yield "stage", "generate", {}


async def read_frames(queue: asyncio.Queue, count: int):
    """从队列读取 count 条非 None 帧。"""
    items = []
    for _ in range(count):
        item = await queue.get()
        if item is None:
            break
        items.append(item)
    return items


async def drain_all(queue: asyncio.Queue):
    """读到 None 为止。"""
    items = []
    while True:
        item = await queue.get()
        if item is None:
            break
        items.append(item)
    return items


async def run_test():
    with TestClient(app) as client:
        container = app.state.container
        orig_run_stream = container.workflow.run_stream
        container.workflow.run_stream = slow_run_stream

        from app.db.models import USERS
        user = USERS["admin"]
        session = {"title": ""}
        question = "测试中断后是否重复"
        key = cache_key(question, [])
        body = ChatStreamRequest(question=question, session_id="int-test-1")

        # --- 第一次：运行 producer，读几个 token 后 cancel ---
        queue1 = asyncio.Queue()
        task1 = asyncio.create_task(
            _producer(queue1, make_state(question), body, user, session, "int-test-1",
                      container, [], [])
        )
        await read_frames(queue1, 5)  # 3 stage + 2 token
        task1.cancel()
        try:
            await task1
        except asyncio.CancelledError:
            pass

        cache_after_interrupt = await container.cache.get(key)
        check("中断后缓存未写入", cache_after_interrupt is None,
              str(cache_after_interrupt)[:80] if cache_after_interrupt else "")

        # --- 第二次：同一问题完整跑完，应不命中缓存且答案完整 ---
        queue2 = asyncio.Queue()
        task2 = asyncio.create_task(
            _producer(queue2, make_state(question, trace_id="t-int-2"),
                      body, user, session, "int-test-2", container, [], [])
        )
        frames = await drain_all(queue2)
        try:
            await task2
        except asyncio.CancelledError:
            pass

        done_frame = None
        for f in frames:
            text = f.decode("utf-8") if isinstance(f, bytes) else f
            if text.startswith("event: done"):
                for line in text.splitlines():
                    if line.startswith("data: "):
                        done_frame = json.loads(line[6:])
                        break
                break

        check("第二次未命中缓存", done_frame is not None and done_frame.get("cache_hit") is False,
              f"done={done_frame}")
        check("第二次拿到完整答案", done_frame is not None and len(done_frame.get("answer", "")) >= 50,
              f"len={len(done_frame.get('answer','')) if done_frame else 'N/A'}")

        # 恢复
        container.workflow.run_stream = orig_run_stream


asyncio.run(run_test())

print(f"\n==== 结果：通过 {PASS} / 失败 {FAIL} ====")
sys.exit(1 if FAIL else 0)
