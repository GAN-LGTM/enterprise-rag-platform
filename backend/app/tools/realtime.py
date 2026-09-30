"""
实时信息工具层。

大模型本身没有实时数据（训练截止时间之后的世界一无所知），
所以"今天天气怎么样"这类问题必须在检索/生成之前先走外部工具：
识别 → 调 API 拿结构化实时数据 → 拼进 Prompt 让模型基于数据作答。

当前实现（注册表已落地）：
    weather  天气 —— Open-Meteo，免密钥、支持中文城市名
    market   财经 —— 个股 / 大盘指数 / 板块涨跌榜 / 汇率（见 finance.py）

扩展方式：新增一个 fetcher + 在 TOOL_ROUTER 里注册一行，工作流代码零改动。
失败的契约统一写在 tools/base.py：返回 None = 该工具答不了，上层静默降级。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from ..logging_setup import get_logger
from . import finance, reference
from .base import RealtimeResult  # noqa: F401 —— 对外保持原有 import 路径不变

log = get_logger("tools.realtime")

# Open-Meteo 免密钥接口
_GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT = httpx.Timeout(6.0)

# httpx 默认会读 HTTP_PROXY 环境变量，本机代理会把 127.0.0.1/外网请求都劫持，必须关掉
_CLIENT = httpx.AsyncClient(trust_env=False, timeout=_TIMEOUT)

# WMO 天气代码 → 中文
_WMO = {
    0: "晴", 1: "基本晴", 2: "局部多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "毛毛雨", 55: "浓毛毛雨",
    56: "冻毛毛雨", 57: "冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "小阵雨", 81: "阵雨", 82: "强阵雨",
    85: "小阵雪", 86: "大阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "雷阵雨伴冰雹",
}

_WEEKDAY = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 触发词：命中才考虑走天气工具（避免" Coldplay 的歌 "之类误伤）
_WEATHER_TRIGGER = re.compile(r"天气|下雨|降雨|下雪|气温|温度|热不热|冷不热|冷不冷|风力|风大|湿度")

# 填充词 / 时间词：抽取城市前先剔除，避免抽出"今天上海""我查下广州"这类脏候选
_FILLER = re.compile(r"请问|帮我|麻烦|查一下|查询一下|查询|查下|查查|看看|我想知道|我想|知道|告诉|一下")
_TIME_WORD = re.compile(r"今天|明天|后天|现在|最近|这周|周末|当前|此刻")
# 城市 = 天气词前面紧邻的连续汉字（中间允许"的/会/要/可能/将"等连接成分）
_CITY = re.compile(r"([\u4e00-\u9fa5]{2,5}?)(?:的)?(?:会|要|可能|将|是不是)*(?:天气|气温|温度|下雨|下雪|降雨|有雨|热不热|冷不冷|冷不热)")

# 从候选里剔除的常见干扰词
_STOPWORDS = {"请问", "帮我", "查一下", "查询", "看看", "一下", "我想", "知道", "告诉", "今天", "明天", "后天"}


@dataclass
class ToolSpec:
    """一个外部工具的注册信息。

    always=True 的工具语义唯一（天气），命中直接查，不受部门范围影响；
    always=False 的工具（行情）只在"没圈定知识库范围"或"命中强时效"时才调用，
    避免把内部业务问题误判成公网查询。

    fallback=True 的工具不在检索前抢答，而是等内网检索答不上来之后再兜底（百科）。
    """
    name: str
    label: str           # 前端进度文案（"查询实时行情"）
    match: object        # (question) -> bool，是否该由本工具处理
    hard: object         # (question) -> bool，强时效判定（无视部门范围）
    fetch: object        # async (question, redact) -> RealtimeResult | None
    always: bool = False
    fallback: bool = False


def is_weather_query(question: str) -> bool:
    return bool(_WEATHER_TRIGGER.search(question))


def _extract_city(question: str) -> str | None:
    # 先剔除填充词与时间词（"帮我查下广州天气"→"广州天气"，"今天上海的气温"→"上海的气温"）
    q = _FILLER.sub("", question)
    q = _TIME_WORD.sub("", q)
    m = _CITY.search(q)
    if m:
        city = m.group(1).strip()
        if city and city not in _STOPWORDS:
            return city
    return None


def _wmo_desc(code) -> str:
    return _WMO.get(int(code or -1), "未知")


def _wind_dir(deg) -> str:
    if deg is None:
        return ""
    dirs = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]
    return dirs[int(deg / 45 + 0.5) % 8] + "风"


async def fetch_weather(question: str) -> RealtimeResult | None:
    """识别城市并拉取实时天气；任何失败都返回 None（静默降级，不阻塞主流程）。"""
    city = _extract_city(question)
    if not city:
        return None
    try:
        # ① 地理编码（中文城市名 → 经纬度）
        geo = (await _CLIENT.get(_GEO_URL, params={
            "name": city, "count": 1, "language": "zh", "format": "json",
        })).json()
        results = geo.get("results") or []
        if not results:
            log.info("tool.weather.geocode_miss", city=city)
            return None
        loc = results[0]

        # ② 实况 + 未来 3 天预报
        wx = (await _CLIENT.get(_FORECAST_URL, params={
            "latitude": loc["latitude"], "longitude": loc["longitude"],
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                       "weather_code,wind_speed_10m,wind_direction_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": "auto", "forecast_days": 3,
        })).json()

        cur, daily = wx["current"], wx["daily"]
        lines = [
            f"城市：{loc['name']}（{loc.get('admin1', '')}{loc.get('country', '')}）",
            f"实况：{_wmo_desc(cur['weather_code'])}，{cur['temperature_2m']}°C"
            f"（体感 {cur['apparent_temperature']}°C），"
            f"湿度 {cur['relative_humidity_2m']}%，"
            f"{_wind_dir(cur.get('wind_direction_10m'))} {cur['wind_speed_10m']} km/h",
        ]
        for i in range(len(daily["time"])):
            d = daily["time"][i]  # 形如 2026-09-22
            wd = _WEEKDAY[__import__("datetime").date.fromisoformat(d).weekday()]
            pop = daily["precipitation_probability_max"][i]
            lines.append(
                f"{'今日' if i == 0 else '明日' if i == 1 else '后天'}（{d}，{wd}）："
                f"{_wmo_desc(daily['weather_code'][i])}，"
                f"{daily['temperature_2m_min'][i]}~{daily['temperature_2m_max'][i]}°C"
                + (f"，降水概率 {pop}%" if pop is not None else "")
            )
        lines.append("数据来源：Open-Meteo 实时气象接口")
        text = "\n".join(lines)
        log.info("tool.weather.ok", city=loc["name"])
        return RealtimeResult(tool="weather", text=text,
                              summary=f"{loc['name']} {cur['temperature_2m']}°C {_wmo_desc(cur['weather_code'])}")
    except Exception as e:  # noqa: BLE001 —— 工具失败绝不阻塞主流程
        log.warning("tool.weather.fail", error=str(e))
        return None


# ============================================================
#  工具注册表：新增工具只改这里，工作流零改动
# ============================================================
async def _weather_entry(question: str, redact: set[str] | None = None) -> RealtimeResult | None:
    """适配器：把 fetch_weather(q) 对齐注册表的 (question, redact) 签名。"""
    return await fetch_weather(question)


async def _market_entry(question: str, redact: set[str] | None = None) -> RealtimeResult | None:
    # 走属性查找而非直接绑定函数对象，保证单测里 monkeypatch finance.dispatch 生效
    return await finance.dispatch(question, redact=redact)


def _never(_question: str) -> bool:
    """永远不命中：用于"只在没圈定内网范围时才考虑"的工具。"""
    return False


async def _holiday_entry(question: str, redact: set[str] | None = None) -> RealtimeResult | None:
    if not reference.enabled():
        return None
    return await reference.fetch_holiday(question)


async def _encyclopedia_entry(question: str, redact: set[str] | None = None) -> RealtimeResult | None:
    if not reference.enabled():
        return None
    return await reference.fetch_encyclopedia(question)


TOOL_ROUTER: list[ToolSpec] = [
    # 天气：语义唯一，命中即查（不受是否圈定了内部知识库范围影响）
    ToolSpec(name="weather", label="查询实时天气", match=is_weather_query,
             hard=is_weather_query, fetch=_weather_entry, always=True),
    # 行情：只有在"没圈定内部部门范围"或"命中强时效"时才走，
    #       避免把"我们公司的股价管理制度"这类内部问题也拿去查公网行情。
    ToolSpec(name="market", label="查询实时行情", match=finance.is_market_query,
             hard=finance.is_hard_market_query, fetch=_market_entry, always=False),
    # 节假日：语义很窄（放假/上班/调休），即使圈定了内网部门范围也放行 ——
    #         "10月1号放假吗"这类问题内网知识库里本来就不会有答案。
    ToolSpec(name="holiday", label="查询节假日安排", match=reference.is_holiday_query,
             hard=reference.is_holiday_query, fetch=_holiday_entry, always=True),
    # 百科：语义最宽（"XX是什么"），**不抢答**——等内网检索确实答不上来再兜底。
    #       内网明明有制度文档却用公网百科盖过去是不负责任的；反过来内网没资料时
    #       又不该让用户对着"未检索到"干瞪眼，所以它是 fallback 而不是 primary。
    #       tools/reference 内部另有内部事项护栏，内部制度类问法会先弃权。
    ToolSpec(name="encyclopedia", label="查询百科词条", match=reference.is_encyclopedia_query,
             hard=_never, fetch=_encyclopedia_entry, always=False, fallback=True),
]


def _hit(spec: ToolSpec, question: str, dept_scoped: bool) -> bool:
    if not spec.match(question):
        return False
    if spec.always or not dept_scoped:
        return True
    return bool(spec.hard(question))


def should_dispatch(question: str, dept_scoped: bool = False) -> bool:
    """是否有某个外部工具该接这个问题。

    dept_scoped：本轮是否被限定在某个部门知识库范围内。
    为 True 时，只有强时效问题才会放行到外部工具。
    """
    return any(_hit(s, question, dept_scoped) for s in TOOL_ROUTER)


def is_external_query(question: str) -> bool:
    """给 graph/workflow.py 的路由用（此时尚未圈定部门范围）。"""
    return should_dispatch(question, dept_scoped=False)


def label_for(source: str) -> str | None:
    """来源标识 → 前端进度文案。"""
    for s in TOOL_ROUTER:
        if s.name == source:
            return s.label
    return None


async def dispatch_realtime(
    question: str, redact: set[str] | None = None, dept_scoped: bool = False,
    phase: str = "all",
) -> RealtimeResult | None:
    """统一入口：按顺序问每个工具"你答得了这个问题吗"，第一个拿回数据的胜出。

    phase 决定这一轮轮到哪些工具：
      · "primary"  —— 检索**之前**跑：天气/行情/节假日这类语义确定的工具，
                      命中就直接用结构化数据作答，不必先翻内网。
      · "fallback" —— 检索**之后**、且内网没答上来时才跑：百科这类语义宽的工具。
                      内网明明有资料却拿公网百科盖过去是不负责任的，所以它是兜底不是抢答。
      · "all"      —— 不分期，全跑（单测与向后兼容用）。

    任何工具失败都必须在本工具内部吞掉并返回 None —— 这里不再兜异常，
    免得某个工具的 bug 被掩盖成"全部工具都不可用"。
    """
    for spec in TOOL_ROUTER:
        if phase == "primary" and spec.fallback:
            continue
        if phase == "fallback" and not spec.fallback:
            continue
        # 兜底阶段说明内网已经答不上来了，此时不再受"圈定了部门范围"的限制
        hit = spec.match(question) if phase == "fallback" \
            else _hit(spec, question, dept_scoped)
        if not hit:
            continue
        res = await spec.fetch(question, redact=redact)
        if res:
            log.info("tool.dispatch.ok", tool=spec.name, phase=phase)
            return res
    return None
