"""
结构化日志：structlog 输出 JSON，每个请求自动分配 trace_id 并贯穿 LangGraph 全节点。

交付要点：
  - trace_id 存在 contextvars 里，异步并发下不会串号
  - **双通道落盘**：文件写 JSON（便于日志平台采集/检索），控制台写人类可读文本（便于现场排障）
  - 日志 100MB 轮转、保留 10 份，容器化时用 volume 挂到宿主机
  - 私有化场景：所有日志落本地文件，不上云

实现说明（重要）：
  早期版本用 structlog.PrintLoggerFactory()，日志只打到 stdout，绕过了 stdlib logging，
  导致 RotatingFileHandler **收不到任何应用日志**（磁盘上只剩第三方库的 print 输出），
  排障与审计都无从下手。这里改为 structlog.stdlib.LoggerFactory + ProcessorFormatter，
  让应用日志真正经过 stdlib handler 落盘。
"""
from __future__ import annotations

import contextvars
import logging
import os
import sys
import time
import uuid
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler

import structlog

from .config import settings

# trace_id 用 contextvars，保证 asyncio 并发请求之间互不污染
trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")
user_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("user_id", default="-")
dept_ids_var: contextvars.ContextVar[str] = contextvars.ContextVar("dept_ids", default="-")


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def bind_trace(trace_id: str | None = None, user_id: str = "-", dept_ids: list[str] | None = None):
    tid = trace_id or new_trace_id()
    trace_id_var.set(tid)
    user_id_var.set(user_id or "-")
    dept_ids_var.set(",".join(dept_ids) if dept_ids else "-")
    return tid


def _inject_context(_logger, _method_name, event_dict):
    event_dict.setdefault("trace_id", trace_id_var.get())
    event_dict.setdefault("user_id", user_id_var.get())
    event_dict.setdefault("dept_ids", dept_ids_var.get())
    return event_dict


def _level() -> int:
    return getattr(logging, str(settings.LOG_LEVEL).upper(), logging.INFO)


def setup_logging() -> None:
    os.makedirs(settings.LOG_DIR, exist_ok=True)
    level = _level()

    # ---- 预处理器：与输出格式无关的部分（落盘/控制台共用）----
    pre_chain = [
        _inject_context,
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=pre_chain + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # ---- 文件：JSON（机器可读，供 filebeat/ELK 采集）----
    # 用 processor= 单数形参：ProcessorFormatter 会自动前置 remove_processors_meta，
    # 把 _record / _from_structlog 两个中间字段剔除，保证落盘 JSON 干净。
    file_handler = RotatingFileHandler(
        os.path.join(settings.LOG_DIR, "app.log"),
        maxBytes=100 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(ensure_ascii=False),
            foreign_pre_chain=pre_chain,
        )
    )

    # ---- 控制台：人类可读（现场排障无需 jq）----
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(colors=False),
            foreign_pre_chain=pre_chain,
        )
    )

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:  # noqa: BLE001
            pass
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    root.setLevel(level)

    # 第三方库降噪：httpx/openai 每请求一条 INFO 会淹没应用日志
    for noisy in ("httpx", "httpcore", "openai", "asyncio", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str = "rag"):
    return structlog.get_logger(name)


log = get_logger("rag")


@contextmanager
def node_timer(node: str, logger=None):
    """
    节点级耗时监控。用法：
        with node_timer("retrieve"):
            ...
    """
    logger = logger or log
    start = time.perf_counter()
    logger.info("node.enter", node=node)
    try:
        yield
    except Exception as e:
        cost = (time.perf_counter() - start) * 1000
        logger.error("node.error", node=node, cost_ms=round(cost, 2), error=str(e))
        raise
    else:
        cost = (time.perf_counter() - start) * 1000
        logger.info("node.exit", node=node, cost_ms=round(cost, 2))
