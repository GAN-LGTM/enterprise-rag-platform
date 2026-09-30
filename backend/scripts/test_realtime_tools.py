"""
实时外部工具回归测试：注册表路由 / 子意图判定 / retrieve 节点挂载 / 结果质量治理。

默认全离线跑（不触网、不依赖外部接口可用性）：
    python scripts/test_realtime_tools.py

想顺带校验真实行情接口的连通性（需要公网）：
    RUN_NET=1 python scripts/test_realtime_tools.py

覆盖：
  · TOOL_ROUTER 注册表的放行语义：天气"语义唯一"恒定放行，行情受部门范围约束
  · 强时效命中放行、内部事项护栏（"我们公司的股价制度"不去查公网行情）
  · finance.plan 子意图路由：汇率 / 代码 / 指数 / 板块 / 个股 / 大盘兜底
  · retrieve 节点：公网开关关闭时零出网；开启时经脱敏调用行情工具
  · websearch 结果质量治理：导航站 / 词典 / 跳转页被剔除
"""
from __future__ import annotations

import asyncio
import os
import sys

os.environ.setdefault("DB_FORCE_MEMORY", "true")
os.environ.setdefault("REDIS_ENABLED", "false")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app.tools import finance, realtime, websearch  # noqa: E402
from app.graph.nodes import build_nodes  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = ""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {extra}")
        FAILS.append(name)


# ============================================================
# 1. 注册表：谁该被放行去公网
# ============================================================
print("\n[1] TOOL_ROUTER 放行语义")
names = [s.name for s in realtime.TOOL_ROUTER]
check("注册表含天气与行情两类工具", "weather" in names and "market" in names, str(names))
check("注册表含百科与节假日工具",
      "encyclopedia" in names and "holiday" in names, str(names))
check("每个工具都声明了前端进度文案", all(s.label for s in realtime.TOOL_ROUTER), str(names))

check("天气问题放行（未圈定部门）", realtime.should_dispatch("北京今天天气怎么样"))
check("天气语义唯一 → 即便圈定了部门也放行",
      realtime.should_dispatch("上海下雨吗", dept_scoped=True))

check("行情问题放行（未圈定部门）", realtime.should_dispatch("今天哪个板块涨"))
check("强时效行情 → 圈定部门也放行（内网必然没有答案）",
      realtime.should_dispatch("今天哪个板块涨", dept_scoped=True))
check("内部股价制度 → 圈定部门时不放行外部工具",
      not realtime.should_dispatch("股价管理制度是怎么规定的", dept_scoped=True))
check("公司内部股价问法被护栏拦下",
      not finance.is_market_query("我们公司的股价是多少"))
check("无关问题不放行", not realtime.should_dispatch("帮我提交请假申请"))

# ============================================================
# 2. 财经子意图路由（纯规则，零 IO）
# ============================================================
print("\n[2] finance.plan 子意图路由")
CASES = [
    ("今天哪个板块涨", "sector"),
    ("现在哪个概念板块在领涨", "sector"),
    ("今天有涨停股吗", "sector"),
    ("贵州茅台现在多少钱一股", "stock"),
    ("600519最新价是多少", "stock"),
    ("上证指数今天多少点", "overview"),
    ("创业板指盘中走势如何", "overview"),
    ("今天美元兑人民币汇率是多少", "fx"),
    ("日元汇率怎么样", "fx"),
    ("今天股市怎么样", "overview"),
]
for q, expect in CASES:
    got = finance.plan(q)
    check(f"「{q}」→ {expect}", got == expect, f"实际={got}")

# ============================================================
# 3. 抽取与格式化
# ============================================================
print("\n[3] 抽取与格式化")
check("抽取六位股票代码", finance._extract_code("帮我看看 600519") == "600519")
check("抽取指数名", finance._extract_index("今天上证指数涨了多少") == ["1.000001"])
check("剔除噪声后的个股候选", finance._name_candidates("请问贵州茅台现在多少钱一股") == ["贵州茅台"],
      str(finance._name_candidates("请问贵州茅台现在多少钱一股")))
check("纯板块问句不产出个股候选", finance._name_candidates("今天哪个板块涨") == [],
      str(finance._name_candidates("今天哪个板块涨")))
check("百分比格式化", finance._pct(7.73) == "+7.73%", finance._pct(7.73))
check("成交额格式化（元→亿元）", finance._yuan(3_260_057_865) == "32.60亿元", finance._yuan(3_260_057_865))
check("异常值不炸", finance._f("-") is None and finance._pct(None) == "-")

print("\n[3b] 双开关语义（合规优先）")


class _SBoth:
    PUBLIC_SEARCH_ENABLED = True
    REALTIME_MARKET_ENABLED = True


class _SPublicOff:
    PUBLIC_SEARCH_ENABLED = False
    REALTIME_MARKET_ENABLED = True


class _SMarketOff:
    PUBLIC_SEARCH_ENABLED = True
    REALTIME_MARKET_ENABLED = False


_saved_settings = finance.settings
try:
    finance.settings = _SPublicOff()
    check("公网总开关关闭 → 行情一并关闭（合规优先于功能）", finance.market_enabled() is False)
    finance.settings = _SMarketOff()
    check("行情独立开关可单独关闭", finance.market_enabled() is False)
    finance.settings = _SBoth()
    check("双开关都开 → 行情可用", finance.market_enabled() is True)
finally:
    finance.settings = _saved_settings

# ============================================================
# 4. 网页搜索结果质量治理
# ============================================================
print("\n[4] websearch 垃圾结果过滤")
junk = [
    {"url": "https://www.hao123.com/nav", "title": "hao123导航", "snippet": ""},
    {"url": "https://www.360.cn/nav.html", "title": "360导航", "snippet": ""},
    {"url": "https://dict.iciba.com/stock", "title": "stock是什么意思", "snippet": ""},
]
ok = [{"url": "https://finance.eastmoney.com/a/x.html", "title": "板块异动", "snippet": ""}]
check("导航站/词典被判为垃圾", all(websearch._is_junk_result(r) for r in junk))
check("财经站点不被误杀", not any(websearch._is_junk_result(r) for r in ok))


# ============================================================
# 5. retrieve 节点挂载（开关关闭零出网 / 开启经脱敏调用）
# ============================================================
print("\n[5] retrieve 节点：开关关闭零出网 / 开启经脱敏调用行情")
class _FakeContainer:
    cache = store = embedder = llm = classifier = None


nodes = build_nodes(_FakeContainer())


async def _run(state: dict, enabled: bool) -> dict:
    class _S:
        PUBLIC_SEARCH_ENABLED = enabled
        REALTIME_MARKET_ENABLED = True
        INTERNAL_TERMS: list[str] = []
        CTX_GOVERNANCE_ENABLED = False
        CTX_HISTORY_MAX_CHARS = 3000
        RERANK_ENABLED = True
        RELEVANCE_GATE_ENABLED = True

    import app.graph.nodes as N
    import app.tools.finance as F
    import app.tools.websearch as W

    saved = (N.settings, F.settings, W.settings)
    N.settings = F.settings = W.settings = _S()

    # 本套件的关注点是"外部工具有没有被调用"，检索后端在这里用桩：
    # 走到真实 hybrid_search 会因为 FakeContainer 没有 embedder 而炸，白白掩盖被测逻辑。
    async def _fake_search(*_a, **_k):
        return [], {"source": "stub", "cost_ms": 0.0}

    saved_search = N.hybrid_search
    N.hybrid_search = _fake_search
    try:
        return await nodes["retrieve"](state)
    finally:
        N.settings, F.settings, W.settings = saved
        N.hybrid_search = saved_search


QUEUE = "今天哪个板块涨"


async def _main():
    out_off = await _run({"question": QUEUE, "dept_ids": [], "user": None,
                          "cache_enabled": False}, False)
    check("公网开关关闭 → 行情工具不出网",
          out_off.get("answer_source") == "chat"
          and not str(out_off.get("retrieval_diag", {}).get("source")).startswith("realtime"),
          str(out_off.get("retrieval_diag")))

    captured: dict = {}

    async def _fake_dispatch(question, redact=None):
        captured["question"] = question
        captured["redact"] = redact
        from app.tools.base import RealtimeResult

        return RealtimeResult(tool="market_sector", text="板块榜数据",
                              summary="某板块 +7.73%", answer_source="market")

    saved_fn = finance.dispatch
    finance.dispatch = _fake_dispatch
    try:
        out_on = await _run({"question": QUEUE, "dept_ids": [], "user": None,
                             "cache_enabled": False}, True)
    finally:
        finance.dispatch = saved_fn

    check("开关开启 → 走行情工具，answer_source=market",
          out_on.get("answer_source") == "market"
          and out_on.get("retrieval_diag", {}).get("source") == "realtime_market",
          str(out_on.get("retrieval_diag")))
    check("出网前准备了脱敏词集", isinstance(captured.get("redact"), set),
          str(captured.get("redact")))

    # 圈定部门范围 + 非强时效问题 → 不应出网（内部业务问题优先）
    finance.dispatch = _fake_dispatch
    try:
        out_dept = await _run({"question": "股价管理制度是怎么规定的", "dept_ids": ["fin"],
                               "user": None, "cache_enabled": False}, True)
    finally:
        finance.dispatch = saved_fn
    check("圈定部门范围的非强时效问题不调用行情工具",
          out_dept.get("answer_source") != "market", str(out_dept.get("retrieval_diag")))


asyncio.run(_main())


# ============================================================
# 5b. 参考类工具：百科 / 节假日（命中判断与护栏，纯规则零 IO）
# ============================================================
print("\n[5b] 参考类工具命中与内部事项护栏")
from app.tools import reference  # noqa: E402

check("百科型问法命中", reference.is_encyclopedia_query("什么是人工智能"))
check("人物型问法命中", reference.is_encyclopedia_query("李白是谁"))
check("介绍型问法命中", reference.is_encyclopedia_query("介绍一下量子计算"))
# 内部制度类问法必须弃权，交回内网 RAG —— 用公网百科答内部制度是错的
check("公司内部制度 → 百科弃权",
      not reference.is_encyclopedia_query("我们公司的报销标准是什么"))
check("含报销词 → 百科弃权",
      not reference.is_encyclopedia_query("差旅报销标准是多少"))
check("部门语境 → 百科弃权",
      not reference.is_encyclopedia_query("我们部门的考勤制度是什么"))

check("放假类问法命中节假日", reference.is_holiday_query("10月1号放假吗"))
check("上班类问法命中节假日", reference.is_holiday_query("今天上班吗"))
check("调休类问法命中节假日", reference.is_holiday_query("春节怎么调休"))
check("公司放假安排 → 节假日弃权（问的是内部安排，不是法定假日）",
      not reference.is_holiday_query("我们公司什么时候放假"))
check("内部考勤 → 节假日弃权",
      not reference.is_holiday_query("公司的考勤制度是什么"))

print("\n[5b-2] 日期解析与文本清洗")
import datetime as dt  # noqa: E402

_today = dt.date.today()
check("今天", reference._extract_date("今天上班吗") == _today)
check("明天", reference._extract_date("明天休息吗") == _today + dt.timedelta(days=1))
check("后天", reference._extract_date("后天放假吗") == _today + dt.timedelta(days=2))
check("昨天", reference._extract_date("昨天上班吗") == _today - dt.timedelta(days=1))
check("X月X号", reference._extract_date("10月1号放假吗") == dt.date(_today.year, 10, 1))
check("YYYY-MM-DD", reference._extract_date("2026-01-01放假吗") == dt.date(2026, 1, 1))
check("解析不出返回 None", reference._extract_date("放假安排是怎样的") is None)

# 百科字段类型不稳（str/list 混用）+ 夹带 HTML，必须都收敛掉
check("list 字段收敛为字符串", reference._as_text(["人工智能"]) == "人工智能")
check("HTML 标签被清除",
      reference._as_text("<a target=_blank>计算机</a>") == "计算机")
check("上标脚注整段清除（不留裸数字 124）",
      reference._as_text("李白<sup>124</sup>") == "李白")
check("HTML 实体还原", reference._as_text("A&nbsp;B") == "A B")
check("None 安全", reference._as_text(None) == "")

print("\n[5b-3] 两阶段调度：内网优先，百科兜底")


async def _phase():
    # primary（检索前）不含百科 —— 内网还没翻过，不该先拿公网百科抢答
    r = await realtime.dispatch_realtime("什么是人工智能", dept_scoped=True, phase="primary")
    check("primary 阶段不跑百科（内网优先）", r is None)
    # 节假日语义确定，检索前就该生效，不必等内网
    r2 = await realtime.dispatch_realtime("10月1号放假吗", dept_scoped=True, phase="primary")
    if r2 is None and os.environ.get("RUN_NET") != "1":
        print("     （节假日需真实接口，离线阶段跳过）")
    else:
        check("primary 阶段就跑节假日（语义确定，不必等内网）",
              r2 is not None and r2.tool == "holiday")


asyncio.run(_phase())


# ============================================================
# 6. 可选：真实接口探活（RUN_NET=1）
# ============================================================
if os.environ.get("RUN_NET") == "1":
    print("\n[6] 真实行情接口探活（RUN_NET=1）")

    async def _live():
        for q in ["今天哪个板块涨", "贵州茅台现在多少钱一股",
                  "上证指数今天多少点", "今天美元兑人民币汇率是多少"]:
            try:
                res = await finance.dispatch(q)
            except Exception as e:  # noqa: BLE001
                res = None
                print(f"     异常：{e}")
            check(f"真实接口返回数据「{q}」", res is not None)
            if res:
                print(f"     → {res.tool}: {res.summary}")
                check(f"「{q}」结果标注了数据来源", "数据来源" in res.text)

        # 兜底阶段：内网答不上来时百科接手（即使圈定了部门范围也要放行）
        r = await realtime.dispatch_realtime("什么是人工智能", dept_scoped=True,
                                             phase="fallback")
        check("fallback 阶段百科兜底（即使圈定了部门范围）",
              r is not None and r.tool == "encyclopedia")

        # 参考类：百科与节假日（国内可达、免密钥）
        for q in ["什么是人工智能", "10月1号放假吗"]:
            try:
                res = await reference.dispatch(q)
            except Exception as e:  # noqa: BLE001
                res = None
                print(f"     异常：{e}")
            check(f"真实接口返回数据「{q}」", res is not None)
            if res:
                print(f"     → {res.tool}: {res.summary}")
                check(f"「{q}」结果无 HTML 残留",
                      "<sup" not in res.text and "<a " not in res.text)

    asyncio.run(_live())
else:
    print("\n[6] 真实接口探活：已跳过（设 RUN_NET=1 开启）")

print()
print("FAILS=" + str(FAILS))
print("RESULT:", "ALL_PASS" if not FAILS else "HAS_FAIL")
sys.exit(0 if not FAILS else 1)
