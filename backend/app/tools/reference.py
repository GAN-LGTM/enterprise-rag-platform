"""
参考类实时工具：百科词条 + 中国节假日。

这一层要解决的问题很具体：过去"XX 是什么""10 月 1 号放假吗"这类问题只能靠
通用网页搜索硬扒，抓回来的常常是导航站、词典页、转载站，答非所问。
现在改成直接调**结构化接口**——返回的就是字段清晰的 JSON，和豆包/DeepSeek
接专用数据源是同一个思路，而不是"自己爬网页再猜哪段是正文"。

两个数据源都是国内可达、免密钥：
  · 百度百科 OpenAPI  → 概念 / 人物 / 事件的权威摘要
  · timor.tech 节假日 → 精确的放假、调休、工资倍数

为什么不用维基百科 / DuckDuckGo：本机实测境外域名一律 ConnectTimeout，
私有化部署环境更是大概率不通，所以只选国内可达源。任何失败都返回 None。
"""
from __future__ import annotations

import datetime as dt
import re

import httpx

from ..logging_setup import get_logger
from .base import RealtimeResult

log = get_logger("tools.reference")

_CLIENT = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(6.0), follow_redirects=True)
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120"}

_BAIKE_URL = "https://baike.baidu.com/api/openapi/BaikeLemmaCardApi"
_HOLIDAY_URL = "https://timor.tech/api/holiday/info/{date}"


def enabled() -> bool:
    """参考类工具的独立开关。"""
    from ..config import settings

    return bool(getattr(settings, "reference_tools_enabled", True))


# ============================================================
#  内部事项护栏
# ============================================================
# 企业里"报销/请假/考勤/绩效"这些词既是通用概念也是内部制度，
# 一旦放行去查百科，就会用公科普回答替代公司内部规定 —— 这是不能接受的。
# 所以命中内部语境时，本工具直接弃权，交回内网 RAG。
_INTERNAL_GUARD = re.compile(
    r"我们(公司|单位)|本公司|公司(的|内部)?|我司|集团|部门|团队"
    r"|制度|规定|规章|流程|办法|政策|标准|规范|手册|条例|守则"
    r"|报销|请假|考勤|绩效|考核|薪酬|社保|公积金|入职|离职|转正|出差|差旅"
    r"|员工手册|内部|保密|合规要求"
)


def _looks_internal(question: str) -> bool:
    return bool(_INTERNAL_GUARD.search(question))


# ============================================================
#  子工具一：百科词条
# ============================================================
# 只认明确的"百科型问法"。问"报销标准是多少"不带这些标志词，
# 就不会被百科抢走 —— 内部业务问题必须留给内网知识库。
_ENCY_TRIGGER = re.compile(
    r"什么是|什么叫|是谁|谁是|介绍一下|介绍一下|简介|释义|的定义|是什么意思|啥是|何为"
    r"|百科|词条|介绍一下"
)

# 从问句里把被询问的实体抠出来：去掉疑问壳与修饰词，剩下的核心词
_STRIP = re.compile(
    r"请问|麻烦|帮我|我想|知道|了解一下|了解|查询|查一下|查查|查|说说|讲讲|告诉我|告诉"
    r"|什么是|什么叫|是什么意思|啥是|何为|的定义|是谁|谁是|介绍一下|介绍|简介|释义|百科|词条"
    r"|一下|呢|吗|啊|呀|？|\?|。|，|,|、|的|了|是"
)


def is_encyclopedia_query(question: str) -> bool:
    if not _ENCY_TRIGGER.search(question):
        return False
    if _looks_internal(question):
        return False
    return bool(_extract_term(question))


_TAG = re.compile(r"<[^>]*>")
_ENTITY = {"&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
           "&quot;": '"', "&#39;": "'", "&mdash;": "—"}


def _as_text(v: object) -> str:
    """接口字段既不稳又带 HTML。

    ① 同一字段有时是 str 有时是 list（card.value 实测为 list）；
    ② 属性值里常夹着 <sup>124</sup>、<a target=_blank> 这类标记，
       不清理就会把标签原样喂进 Prompt，白占 token 还干扰模型。
    """
    if v is None:
        return ""
    s = " ".join(str(x) for x in v if x) if isinstance(v, list) else str(v)
    # 先整段删掉上标脚注（<sup>124</sup>），再清其余标签 —— 顺序反了会留下
    # "李白124" 这种被剥了壳的脚注编号，看起来像乱码。
    s = re.sub(r"<sup\b[^>]*>.*?</sup>", "", s, flags=re.S | re.I)
    s = _TAG.sub("", s)
    for a, b in _ENTITY.items():
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


def _extract_term(question: str) -> str:
    """抠出被问的实体。纯规则、零 IO，可单测。"""
    t = _STRIP.sub(" ", question).strip()
    t = re.sub(r"\s+", "", t)          # 中文之间不留空格
    # 去掉句尾残留的语气/疑问碎片
    t = re.sub(r"(呢|吗|啊|呀|吧)$", "", t).strip()
    return t


async def fetch_encyclopedia(question: str) -> RealtimeResult | None:
    term = _extract_term(question)
    if not term or len(term) < 2:
        return None
    try:
        r = await _CLIENT.get(_BAIKE_URL, params={
            "scope": "103", "format": "json", "appid": "379020",
            "bk_key": term, "bk_length": "800",
        }, headers=_UA)
        if r.status_code != 200:
            log.info("tool.ency.http_fail", status=r.status_code, term=term)
            return None
        d = r.json() or {}
        title = _as_text(d.get("title"))
        # 未命中时接口返回空对象（无任何 key），这是明确的"查无此条"信号
        if not title:
            log.info("tool.ency.miss", term=term)
            return None

        # abstract 是完整摘要，desc 只是一句话定义，两者都拿、互为补充
        abstract = _as_text(d.get("abstract"))
        desc = _as_text(d.get("desc"))
        url = _as_text(d.get("url"))

        # card 是结构化属性表（中文名/外文名/定义/出处…），注意 value 可能是 list
        attrs: list[str] = []
        card = d.get("card")
        if isinstance(card, list):
            for c in card[:10]:
                if isinstance(c, dict):
                    val = _as_text(c.get("value"))
                    if not val:
                        continue
                    name = _as_text(c.get("name"))
                    attrs.append(f"{name}：{val}" if name else val)

        lines = [f"【百科词条 · {title}】"]
        if desc:
            lines.append(f"定义：{desc}")
        if abstract:
            lines.append(f"摘要：{abstract}")
        if attrs:
            lines.append("属性：" + "；".join(attrs))
        if url:
            lines.append(f"来源：{url}")
        lines.append("说明：以上为公开百科信息，仅供参考，不代表公司内部规定。")

        log.info("tool.ency.ok", term=term, title=title)
        return RealtimeResult(
            tool="encyclopedia", text="\n".join(lines),
            summary=f"百科命中 {title}", answer_source="encyclopedia",
        )
    except Exception as e:  # noqa: BLE001 —— 工具失败绝不阻塞主流程
        log.warning("tool.ency.fail", term=term, error=str(e))
        return None


# ============================================================
#  子工具二：中国节假日
# ============================================================
_HOLIDAY_TRIGGER = re.compile(
    r"放假|休息|上班|调休|补班|节假日|法定假日|假期|周末|双休"
    r"|国庆|春节|元旦|清明|劳动节|端午|中秋|过年|除夕"
)

_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
# 0=工作日 1=周末 2=法定节日 3=调休补班
_TYPE_NAME = {0: "工作日", 1: "周末", 2: "法定节假日", 3: "调休补班"}


def is_holiday_query(question: str) -> bool:
    """命中放假类问法，但**排除内部语境**。

    「我们公司什么时候放假」「公司的考勤制度」问的是内部安排，
    拿法定节假日去答是错的 —— 这类必须交回内网 RAG，所以这里直接弃权。
    """
    if not _HOLIDAY_TRIGGER.search(question):
        return False
    return not _looks_internal(question)


def _extract_date(question: str) -> dt.date | None:
    """从问句里解析日期。支持：今天/明天/后天/昨天、X月X号(日)、YYYY-MM-DD。

    纯规则、零 IO，可单测。解析不出来就返回 None（调用方按"今天"处理）。
    """
    q = question.strip()
    today = dt.date.today()
    if re.search(r"前天", q):
        return today - dt.timedelta(days=2)
    if re.search(r"昨天", q):
        return today - dt.timedelta(days=1)
    if re.search(r"今天|今日", q):
        return today
    if re.search(r"明天|明日", q):
        return today + dt.timedelta(days=1)
    if re.search(r"后天", q):
        return today + dt.timedelta(days=2)

    m = re.search(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", q)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})月(\d{1,2})[号日]", q)
    if m:
        y = today.year
        try:
            d = dt.date(y, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
        # 说"1月1号"但今天已过该日期，多半在说明年
        if d < today:
            try:
                d = dt.date(y + 1, int(m.group(1)), int(m.group(2)))
            except ValueError:
                pass
        return d
    return None


async def _query_day(day: dt.date) -> dict | None:
    r = await _CLIENT.get(_HOLIDAY_URL.format(date=day.isoformat()), headers=_UA)
    if r.status_code != 200:
        return None
    d = r.json() or {}
    return d if d.get("code") == 0 else None


async def fetch_holiday(question: str) -> RealtimeResult | None:
    day = _extract_date(question) or dt.date.today()
    try:
        d = await _query_day(day)
        if not d:
            log.info("tool.holiday.no_data", date=day.isoformat())
            return None
        t = d.get("type") or {}
        h = d.get("holiday") or {}
        ttype = t.get("type")
        kind = _TYPE_NAME.get(ttype, t.get("name") or "未知")
        is_rest = bool(h.get("holiday"))
        name = h.get("name") or t.get("name") or ""

        lines = [f"【中国节假日查询 · {day.isoformat()}（{_WEEKDAYS[day.weekday()]}）】"]
        lines.append(f"日期类型：{kind}")
        if is_rest:
            lines.append(f"是否放假：放假（{name}）")
            wage = h.get("wage")
            if wage:
                wage_desc = {1: "1 倍工资", 2: "2 倍工资", 3: "3 倍工资"}.get(wage)
                if wage_desc:
                    lines.append(f"加班工资：{wage_desc}")
        else:
            lines.append("是否放假：不放假（正常工作日/休息日之外的普通日期）")
        lines.append("数据来源：timor.tech 节假日接口（按国务院放假安排）")
        lines.append("说明：以国务院办公厅公布的年度放假安排为准，仅供参考。")

        summary = f"{day.isoformat()} {kind}" + (f" {name}" if name else "")
        log.info("tool.holiday.ok", date=day.isoformat(), kind=kind)
        return RealtimeResult(tool="holiday", text="\n".join(lines),
                              summary=summary, answer_source="holiday")
    except Exception as e:  # noqa: BLE001
        log.warning("tool.holiday.fail", date=day.isoformat(), error=str(e))
        return None


# ============================================================
#  注册表入口：节假日优先于百科（语义更窄、更确定）
# ============================================================
async def dispatch(question: str, redact: set[str] | None = None) -> RealtimeResult | None:
    if not enabled():
        return None
    if is_holiday_query(question):
        r = await fetch_holiday(question)
        if r:
            return r
    if is_encyclopedia_query(question):
        return await fetch_encyclopedia(question)
    return None
