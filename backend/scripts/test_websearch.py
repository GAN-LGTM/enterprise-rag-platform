"""
公网搜索：数据脱敏 + 合规开关 回归测试。

覆盖：
  · sanitize_query 出公网前脱敏（部门名 / 人名 / 内部术语 / 用户身份）
  · PUBLIC_SEARCH_ENABLED 开关语义（OFFLINE_MODE=true 默认关、可显式开启）
  · retrieve 节点：开关关闭时不触网；开启时经脱敏调用公网，answer_source 正确
"""
from __future__ import annotations

import asyncio
import os
import sys

os.environ.setdefault("DB_FORCE_MEMORY", "true")
os.environ.setdefault("REDIS_ENABLED", "false")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app.config import Settings  # noqa: E402
from app.tools import websearch  # noqa: E402
from app.graph.nodes import build_nodes  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = ""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {extra}")
        FAILS.append(name)


# ============================================================
# 1. 出公网前脱敏
# ============================================================
print("\n[1] 出公网前脱敏（sanitize_query）")
redact = {"销售部", "营销总监-李娜", "天枢系统"}
q, removed = websearch.sanitize_query(
    "我们销售部的天枢系统谁在管，找营销总监-李娜问问", redact
)
check("剔除部门名/内部术语/人名",
      "销售部" not in q and "天枢系统" not in q and "营销总监-李娜" not in q, q)
check("返回被剔除词列表",
      set(removed) == {"销售部", "天枢系统", "营销总监-李娜"}, str(removed))

# 整词优先匹配，避免子串误删
q2, _ = websearch.sanitize_query("销售部业绩如何", {"销售部"})
check("整词匹配不误删合理片段", q2 == "业绩如何", q2)

# 脱敏后若为空 → 退回原句，绝不对外发空查询（空查询反而暴露"这里不能说的东西"）
q3, removed3 = websearch.sanitize_query("销售部", {"销售部"})
check("脱敏后为空则退回原句", q3 == "销售部" and removed3 == [], q3)

# 无脱敏词 → 原样返回
q4, _ = websearch.sanitize_query("今天有什么演唱会", None)
check("无脱敏词则原样返回", q4 == "今天有什么演唱会", q4)


# ============================================================
# 2. 公网搜索总开关语义
# ============================================================
print("\n[2] 公网搜索总开关 PUBLIC_SEARCH_ENABLED")
# 用 model_construct 隔离测试（不读 .env），聚焦属性逻辑本身
check("OFFLINE_MODE=true 且未显式设置 → 默认关",
      Settings.model_construct(OFFLINE_MODE=True, PUBLIC_SEARCH_ENABLED_RAW=None).PUBLIC_SEARCH_ENABLED is False)
check("显式 true → 开",
      Settings.model_construct(OFFLINE_MODE=True, PUBLIC_SEARCH_ENABLED_RAW="true").PUBLIC_SEARCH_ENABLED is True)
check("OFFLINE_MODE=false 且未显式设置 → 默认开",
      Settings.model_construct(OFFLINE_MODE=False, PUBLIC_SEARCH_ENABLED_RAW=None).PUBLIC_SEARCH_ENABLED is True)
check("显式 false → 关",
      Settings.model_construct(OFFLINE_MODE=False, PUBLIC_SEARCH_ENABLED_RAW="false").PUBLIC_SEARCH_ENABLED is False)
check("public_search_enabled() 反映开关",
      isinstance(websearch.public_search_enabled(), bool))


# ============================================================
# 3. retrieve 节点门控（开关关闭不触网 / 开启经脱敏触网）
# ============================================================
print("\n[3] retrieve 节点：开关关闭不触网 / 开启经脱敏触网")
class _FakeContainer:
    cache = store = embedder = llm = classifier = None

nodes = build_nodes(_FakeContainer())


async def _run(state: dict, enabled: bool) -> dict:
    # 用 stub 替换两个模块里引用的 settings，避免读到真实 .env。
    # retrieve 节点用到的配置开关都要在此列出，缺一个就会 AttributeError。
    class _S:
        PUBLIC_SEARCH_ENABLED = enabled
        INTERNAL_TERMS: list[str] = []
        CTX_GOVERNANCE_ENABLED = False
        CTX_HISTORY_MAX_CHARS = 3000
        RERANK_ENABLED = True
        RELEVANCE_GATE_ENABLED = True

    import app.graph.nodes as N
    import app.tools.websearch as W

    saved_n, saved_w = N.settings, W.settings
    N.settings = _S()
    W.settings = _S()
    try:
        return await nodes["retrieve"](state)
    finally:
        N.settings = saved_n
        W.settings = saved_w


async def _main():
    # 关闭：可搜索的闲聊问题不应走公网，answer_source=chat（直接闲聊，零外网）
    out_off = await _run(
        {"question": "最近有什么演唱会吗", "dept_ids": [], "user": None, "cache_enabled": False},
        False,
    )
    check("开关关闭 → 不走公网",
          out_off.get("answer_source") == "chat"
          and out_off.get("retrieval_diag", {}).get("source") != "websearch",
          str(out_off.get("retrieval_diag")))

    # 开启 + mock 掉真实网络：应走公网，answer_source=web，且传出脱敏词集
    import app.tools.websearch as W

    captured: dict = {}

    async def _fake_fetch(question, redact=None):
        captured["question"] = question
        captured["redact"] = redact
        return ("websearch", "fake context")

    saved_fn = W.fetch_search
    W.fetch_search = _fake_fetch
    try:
        out_on = await _run(
            {"question": "最近有什么演唱会吗", "dept_ids": [], "user": None, "cache_enabled": False},
            True,
        )
    finally:
        W.fetch_search = saved_fn

    check("开关开启 → 走公网",
          out_on.get("answer_source") == "web" and out_on.get("realtime_tool") == "websearch",
          str(out_on.get("retrieval_diag")))
    check("出网前已准备脱敏词集（含部门名）",
          isinstance(captured.get("redact"), set)
          and any("部" in t for t in captured["redact"]),
          str(captured.get("redact")))


asyncio.run(_main())

print()
print("FAILS=" + str(FAILS))
print("RESULT:", "ALL_PASS" if not FAILS else "HAS_FAIL")
sys.exit(0 if not FAILS else 1)
