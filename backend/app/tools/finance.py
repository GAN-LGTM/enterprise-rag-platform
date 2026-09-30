"""
财经实时数据工具：个股行情 / 大盘指数 / 板块涨跌榜 / 汇率。

为什么必须有这一层 ——

    "今天哪个板块涨"这类问题如果走通用网页搜索，搜索引擎只会给回导航站、
    词典页、没有日期的营销文（实测就是这样）。模型拿到这种语料，要么编数字，
    要么答不上来。结构化行情接口给的是**带字段含义的数字**（涨跌幅、领涨股、
    涨跌家数），模型只需要"读"，不需要"猜"。

    从幻觉治理的角度说：让模型在**数据里**作答，而不是在**记忆里**作答，
    是所有治理手段里收益最高的一种。数据来源可标注、可核对，答错甩不了锅。

数据源（均免密钥，本机实测可达）：
    东方财富 push2     主源：板块排行 / 指数 / 个股行情 / 代码模糊搜索
    腾讯行情 gtimg     备源：个股与指数兜底（GBK 文本协议，字段十余年未变）
    open.er-api.com    汇率

挂载方式：模块只负责"识别 → 取数 → 排版"，由 tools/realtime.py 的注册表收录，
工作流经统一入口调用，新增工具不需要改图（graph/nodes.py 零改动）。

失败契约：任何异常都返回 None，绝不阻塞主流程。
"""
from __future__ import annotations

import datetime
import re

import httpx

from ..config import settings
from ..logging_setup import get_logger
from .base import RealtimeResult

log = get_logger("tools.market")

# ---------------- 数据源 ----------------
_EM_LIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"    # 板块排行
_EM_ULIST_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"  # 批量：指数
_EM_STOCK_URL = "https://push2.eastmoney.com/api/qt/stock/get"   # 单股
_EM_SUGGEST_URL = "https://searchapi.eastmoney.com/api/suggest/get"  # 名称 → 代码
_EM_SUGGEST_TOKEN = "D43BF722C8E33BDC906FB84D85E326E8"          # 东方财富公开的搜索token
_TX_URL = "https://qt.gtimg.cn/q"                               # 备源（GBK）
_TX_RANK_URL = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank"  # 备源：板块排行
_FX_URL = "https://open.er-api.com/v6/latest/CNY"               # 汇率（基准 CNY）

_TIMEOUT = httpx.Timeout(6.0)
# 关键：httpx 默认读 HTTP_PROXY 环境变量，本机代理会把请求劫持到 127.0.0.1 代理端口。
# 天气工具里踩过同样的坑，这里必须一致地关掉。
_CLIENT = httpx.AsyncClient(trust_env=False, timeout=_TIMEOUT, follow_redirects=True)
_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}

# ---------------- 触发词 ----------------
# 分三组，命中任意一组即认为"该走行情工具"。分组的意义在于后续按优先级路由子意图，
# 而不是一股脑把四种数据都拉一遍（省接口、省上下文）。
_MARKET_WORDS = (r"股票|股价|个股|A股|沪指|深指|创业板|科创板|科创50|北交所|两地|两市"
                 r"|大盘|上证|深证|沪深300|中证500|证券市场|股市|证券")
_SECTOR_WORDS = (r"板块|行业板块|概念板块|地域板块|题材|涨得最|跌得最|涨得最好|跌得最惨"
                 r"|涨幅榜|跌幅榜|涨幅前|跌幅前|涨停|跌停|领涨|领跌|资金流向|主力净流入")
_QUOTE_WORDS = (r"多少点|多少钱一股|现价|最新价|收盘价|开盘价|最高价|最低价|盘中|盘口"
                r"|换手率|市盈率|市净率|成交额|成交量|市值|走势|涨跌幅|涨了|跌了")
_FX_WORDS = r"汇率|外汇|离岸人民币|美元兑|欧元兑|日元兑|英镑兑|港币兑|人民币汇率"

_TRIGGER = re.compile(f"({_MARKET_WORDS})|({_SECTOR_WORDS})|({_QUOTE_WORDS})|({_FX_WORDS})")
_FX_TRIGGER = re.compile(_FX_WORDS)
_SECTOR_TRIGGER = re.compile(_SECTOR_WORDS)
_QUOTE_TRIGGER = re.compile(_QUOTE_WORDS)

# 强时效：时间词 + 市场名词，或市场名词 + 走势问法。
# 命中时**无视是否命中了内部知识库**——这类问题内网文档里必然没有答案。
_HARD_TRIGGER = re.compile(
    rf"(今天|今日|现在|最新|实时|当下|此时|盘中|收盘|开盘|本周|本月|刚刚)"
    rf"[^。？?]{{0,12}}({_MARKET_WORDS}|{_SECTOR_WORDS}|{_FX_WORDS})"
    rf"|({_MARKET_WORDS}|{_SECTOR_WORDS}|{_FX_WORDS})[^。？?]{{0,10}}(多少|怎么样|如何|哪些|哪个|什么|情况|走势|排行|排名|涨|跌)"
)

# 指数名 → secid（东方财富 1.=沪市 0.=深市）
_INDEX_NAMES = {
    "上证指数": "1.000001", "沪指": "1.000001", "上证": "1.000001", "大盘": "1.000001",
    "深证成指": "0.399001", "深指": "0.399001", "深证": "0.399001", "深成指": "0.399001",
    "创业板指": "0.399006", "创业板": "0.399006",
    "沪深300": "1.000300", "沪深三百": "1.000300",
    "中证500": "1.000905", "科创50": "1.000688", "上证50": "1.000016",
    "北证50": "0.899050",
}
_INDEX_DEFAULT = ["1.000001", "0.399001", "0.399006", "1.000688", "1.000300"]

# 从问句里抽疑似股票名时，先把这些"噪声"剔除
_NOISE = re.compile(
    r"今天|今日|现在|最新|实时|目前|当下|请问|帮我|麻烦|查一下|查询一下|查询|查查|查下|看看|想知道|知道|告诉|一下"
    r"|怎么样|怎么看待|如何|什么样|吗|呢|？|\?"
    r"|哪个|哪些|为什么|是不是|有没有"
    r"|股价|股票|个股|A股|大盘|市场|股市|证券|板块|概念|题材|行业|涨停|跌停|领涨|领跌"
    r"|多少|块钱|一股|现价|最新价|收盘|开盘|盘中|走势|行情|涨了|跌了|涨|跌|涨跌|数据|情况"
    r"|指数|沪指|深指|点位|成分股"
    r"|汇率|外汇|人民币|美元|欧元|日元|港币|英镑|兑换|兑"
)

# 内部事项护栏：这些问法虽然带"股价/汇率"字样，但问的是公司自己的制度，
# 拿去查公网行情只会答非所问（还多一次外网请求）。命中即不算行情问题。
_INTERNAL_GUARD = re.compile(r"我们公司|本公司|咱们公司|我司|员工持股|内部制度|内部.")
_CJK = re.compile(r"[\u4e00-\u9fa5]{2,6}")
# 注意：不能用 \b —— Python re 里中文字符也算 word char，
# "看600519" 中 \b(数字) 永远匹配不上（已踩过）。改成"前后都不是数字"的断言。
_CODE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_STOP_CAND = {"哪里", "哪些", "什么", "问问", "有没有", "最近"}


# ============================================================
#  工具函数
# ============================================================
def market_enabled() -> bool:
    """行情工具开关：独立开关，但仍受公网总开关约束。

    PUBLIC_SEARCH_ENABLED=false（OFFLINE_MODE 私有化默认）→ 一律不出网，
    即便 REALTIME_MARKET_ENABLED=true。合规语义优先于功能语义。
    """
    if not bool(getattr(settings, "PUBLIC_SEARCH_ENABLED", False)):
        return False
    return bool(getattr(settings, "REALTIME_MARKET_ENABLED", True))


def is_market_query(question: str) -> bool:
    return not _INTERNAL_GUARD.search(question) and bool(_TRIGGER.search(question))


def is_hard_market_query(question: str) -> bool:
    return not _INTERNAL_GUARD.search(question) and bool(_HARD_TRIGGER.search(question))


def _f(v) -> float | None:
    """接口字段安全转 float：'-' / None / 空串一律视为无数据。"""
    try:
        if v is None or v == "-" or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(v) -> str:
    n = _f(v)
    return f"{n:+.2f}%" if n is not None else "-"


def _lots(v) -> str:
    """成交量（手）：接口给的是浮点，展示成整数。"""
    n = _f(v)
    return str(int(n)) if n is not None else "-"


def _yuan(v) -> str:
    n = _f(v)
    if n is None:
        return "-"
    if abs(n) >= 1e8:
        return f"{n / 1e8:.2f}亿元"
    if abs(n) >= 1e4:
        return f"{n / 1e4:.2f}万元"
    return f"{n:.2f}"


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def _extract_code(question: str) -> str | None:
    m = _CODE.search(question)
    return m.group(1) if m else None


def _extract_index(question: str) -> list[str]:
    hit = []
    for name, secid in _INDEX_NAMES.items():
        if name in question and secid not in hit:
            hit.append(secid)
    return hit


def _name_candidates(question: str) -> list[str]:
    """抽疑似股票名的中文候选：先剔噪声词，再取连续的 2~6 个汉字。

    兜不住所有口语说法，所以候选会丢给东方财富的模糊搜索接口验证——
    搜不到 A 股代码的候选直接丢弃，不会拿错东西去查行情。
    """
    q = _NOISE.sub(" ", question)
    # 先试整段残句里的连续汉字，再退化为所有候选片段
    cands: list[str] = []
    for seg in re.findall(_CJK, q):
        seg = seg.strip()
        if 2 <= len(seg) <= 6 and seg not in _STOP_CAND:
            cands.append(seg)
    return cands[:6]


# ============================================================
#  取数：东方财富（主源）
# ============================================================
async def _em_get(url: str, params: dict) -> dict | None:
    try:
        d = (await _CLIENT.get(url, params=params, headers=_UA)).json()
    except Exception as e:  # noqa: BLE001
        log.warning("tool.market.request_fail", url=url, error=str(e))
        return None
    if isinstance(d, dict) and d.get("rc") not in (0, None):
        return None
    return (d or {}).get("data")


async def _em_resolve(keyword: str) -> tuple[str, str] | None:
    """名称/代码 → (secid, 标准名称)。走东方财富模糊搜索。"""
    try:
        d = (await _CLIENT.get(_EM_SUGGEST_URL, params={
            "input": keyword, "type": "14", "token": _EM_SUGGEST_TOKEN, "count": "8",
        }, headers=_UA)).json()
        rows = ((d or {}).get("QuotationCodeTable") or {}).get("Data") or []
    except Exception as e:  # noqa: BLE001
        log.warning("tool.market.suggest_fail", keyword=keyword, error=str(e))
        return None
    for r in rows:
        quote_id = str(r.get("QuoteID") or "")
        name = str(r.get("Name") or "")
        # 只要 A 股个股（INDEX/ETF/期货都会出现在结果里，取错会拿到行情 who cares 的字段）
        if str(r.get("Classify", "")).upper() == "ASTOCK" and quote_id and name:
            return quote_id, name
    return None


async def _em_quote(secid: str) -> dict | None:
    return await _em_get(_EM_STOCK_URL, {
        "secid": secid, "fltt": "2", "invt": "2",
        "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f168,f169,f170,f116,f117",
    })


async def _em_indices(secids: list[str]) -> list[dict]:
    """主源：东方财富批量指数 → 统一结构。"""
    data = await _em_get(_EM_ULIST_URL, {
        "secids": ",".join(secids), "fltt": "2", "invt": "2",
        "fields": "f2,f3,f4,f12,f14,f15,f16,f17,f18",
    })
    rows = (data or {}).get("diff") or []
    return [{
        "name": r.get("f14"), "price": r.get("f2"), "pct": _f(r.get("f3")),
        "change": _f(r.get("f4")), "open": r.get("f17"),
        "high": r.get("f15"), "low": r.get("f16"), "amount": _f(r.get("f18")),
    } for r in rows]


async def _tx_indices(secids: list[str]) -> list[dict]:
    """备源：腾讯指数。字段位与个股一致（见 _tx_quotes），指数没有换手率。"""
    codes = [c for c in (_secid_to_tx(s) for s in secids) if c]
    quotes = await _tx_quotes(codes)
    return [{
        "name": q["name"], "price": q["price"], "pct": q["change_pct"],
        "change": q["change"], "open": q["open"],
        "high": q["high"], "low": q["low"], "amount": None,
    } for q in quotes.values()]


async def _load_indices(secids: list[str]) -> tuple[list[dict], str]:
    """主源 → 备源。返回 (指数列表, 数据源名)。"""
    rows = await _em_indices(secids)
    if rows:
        return rows, "东方财富"
    rows = await _tx_indices(secids)
    if rows:
        log.info("tool.market.index_fallback", source="tencent")
        return rows, "腾讯财经"
    return [], "东方财富"


async def _em_sectors(fs: str, top_n: int, desc: bool) -> list[dict]:
    """主源：东方财富板块排行 → 统一结构。"""
    data = await _em_get(_EM_LIST_URL, {
        "pn": 1, "pz": top_n, "po": 1 if desc else 0, "np": 1,
        "fltt": 2, "invt": 2, "fid": "f3", "fs": fs,
        "fields": "f2,f3,f12,f14,f104,f105,f128,f136,f140",
    })
    rows = (data or {}).get("diff") or []
    return [{
        "name": r.get("f14"), "pct": _f(r.get("f3")), "point": r.get("f2"),
        "leader": r.get("f128"), "leader_code": r.get("f140"),
        "leader_pct": _f(r.get("f136")),
        "up": r.get("f104"), "down": r.get("f105"),
    } for r in rows]


async def _tx_sectors(board: str, top_n: int, desc: bool) -> list[dict]:
    """备源：腾讯板块排行。主源被限流/断连时接上，字段归一化到同一结构。

    board: hy=行业 gn=概念 dy=地域。
    实测东财 push2 在短时高频访问后会直接断连（RemoteProtocolError），
    所以板块排行必须有备源，不能只依赖一家 —— 与本项目"任何单点故障都要有降级"一致。

    坑：该接口的 sort_type=price 是按**板块指数点位**排，不是涨跌幅，
    取回来的第一屏压根不是"涨幅居前"。所以这里一次多取若干板块，
    在本地按涨跌幅排序再截断，保证两个数据源的口径完全一致。
    """
    try:
        d = (await _CLIENT.get(_TX_RANK_URL, params={
            "board_type": board, "sort_type": "price",
            "direct": "down", "offset": 0, "count": 100,
        }, headers=_UA)).json()
        rows = ((d or {}).get("data") or {}).get("rank_list") or []
    except Exception as e:  # noqa: BLE001
        log.warning("tool.market.tx_rank_fail", board=board, error=str(e))
        return []
    out = []
    for r in rows:
        up, down = None, None
        zgb = str(r.get("zgb") or "")       # 形如 "50/122"：上涨家数/下跌家数
        if "/" in zgb:
            up, down = zgb.split("/", 1)
        lead = r.get("lzg") or {}
        out.append({
            "name": r.get("name"), "pct": _f(r.get("zdf")), "point": r.get("zxj"),
            "leader": lead.get("name"), "leader_code": lead.get("code"),
            "leader_pct": _f(lead.get("zdf")),
            "up": up, "down": down,
        })
    out.sort(key=lambda s: s["pct"] if s["pct"] is not None else -1e9, reverse=desc)
    return out[:top_n]


# ============================================================
#  取数：腾讯行情（备源，GBK 文本协议）
# ============================================================
def _secid_to_tx(secid: str) -> str | None:
    try:
        mk, code = secid.split(".")
    except ValueError:
        return None
    prefix = "sh" if mk == "1" else "sz" if mk == "0" else "bj"
    return f"{prefix}{code}"


async def _tx_quotes(tx_codes: list[str]) -> dict[str, dict]:
    """腾讯行情兜底。返回 {股票代码: 行情dict}。

    协议形如 v_sh600519="1~贵州茅台~600519~1235.58~1243.88~..."，字段位固定：
    3=现价 4=昨收 5=今开 6=成交量(手) 30=时间 31=涨跌额 32=涨跌幅 33=最高 34=最低
    37=成交额(万元) 38=换手率
    """
    if not tx_codes:
        return {}
    try:
        r = await _CLIENT.get(_TX_URL, params={"q": ",".join(tx_codes)}, headers=_UA)
        raw = r.content.decode("gbk", errors="ignore")
    except Exception as e:  # noqa: BLE001
        log.warning("tool.market.tx_fail", error=str(e))
        return {}
    out: dict[str, dict] = {}
    for m in re.finditer(r'v_[a-z0-9]+="([^"]*)"', raw):
        p = m.group(1).split("~")
        if len(p) < 35:
            continue
        out[p[2]] = {
            "name": p[1], "code": p[2], "price": _f(p[3]), "prev_close": _f(p[4]),
            "open": _f(p[5]), "high": _f(p[33]), "low": _f(p[34]),
            "change": _f(p[31]), "change_pct": _f(p[32]),
            "volume": _f(p[6]), "amount": _f(p[37]), "turnover": _f(p[38]),
            "time": p[30],
        }
    return out


# ============================================================
#  子工具一：个股行情
# ============================================================
async def fetch_stock(question: str) -> RealtimeResult | None:
    """查单只A股的实时报价。先猜代码/名称，再取数，主源失败走腾讯兜底。"""
    code = _extract_code(question)
    secid = name = None
    if code:
        # 6 开头 / 5 开头 = 沪市，其余（0/3/1）= 深市 / 北交所
        secid = f"1.{code}" if code[0] in "56" else f"0.{code}"
    if not secid:
        for cand in _name_candidates(question):
            hit = await _em_resolve(cand)
            if hit:
                secid, name = hit
                break
    if not secid:
        return None

    lines: list[str] = []
    d = await _em_quote(secid)
    if isinstance(d, dict) and d.get("f58"):
        price, prev, chg, pctv = _f(d.get("f43")), _f(d.get("f60")), _f(d.get("f169")), _f(d.get("f170"))
        name = str(d.get("f58"))
        lines = [
            f"股票：{name}（{d.get('f57')}）",
            f"最新价：{price if price is not None else '-'} 元"
            + (f"，涨跌 {chg:+.2f} 元（{pctv:+.2f}%）" if chg is not None and pctv is not None else ""),
            f"今开 {_f(d.get('f46')) or '-'} / 昨收 {prev or '-'} / "
            f"最高 {_f(d.get('f44')) or '-'} / 最低 {_f(d.get('f45')) or '-'}",
            f"成交量 {_lots(d.get('f47'))} 手，成交额 {_yuan(d.get('f48'))}"
            + (f"，换手率 {_f(d.get('f168')):.2f}%" if _f(d.get("f168")) is not None else ""),
            f"数据来源：东方财富行情接口（查询时间 {_now()}）",
        ]
        summary = f"{name} {price} 元 {_pct(d.get('f170'))}"
    else:
        tx = _secid_to_tx(secid)
        quote = (await _tx_quotes([tx])) if tx else {}
        q = next(iter(quote.values()), None) if quote else None
        if not q:
            log.info("tool.market.stock_no_data", secid=secid)
            return None
        name = q["name"]
        lines = [
            f"股票：{q['name']}（{q['code']}）",
            f"最新价：{q['price']} 元，涨跌 {q['change']:+.2f} 元（{q['change_pct']:+.2f}%）"
            if q["change"] is not None else f"最新价：{q['price']} 元",
            f"今开 {q['open']} / 昨收 {q['prev_close']} / 最高 {q['high']} / 最低 {q['low']}",
            f"成交量 {_lots(q['volume'])} 手，成交额 {_yuan((q['amount'] or 0) * 1e4)}"
            + (f"，换手率 {q['turnover']:.2f}%" if q["turnover"] is not None else ""),
            f"数据来源：腾讯财经行情接口（交易所时间戳 {q['time']}，查询时间 {_now()}）",
        ]
        summary = f"{name} {q['price']} 元 {_pct(q['change_pct'])}"

    text = "【A股实时行情（结构化数据，非网页抓取）】\n" + "\n".join(lines)
    log.info("tool.market.stock_ok", name=name, summary=summary)
    return RealtimeResult(tool="market_stock", text=text, summary=summary,
                          answer_source="market")


# ============================================================
#  子工具二：板块涨跌榜
# ============================================================
# 板块类型：东财过滤条件 → 腾讯 board_type → 中文名
_SECTOR_KINDS = {
    "industry": ("m:90+t:2", "hy", "行业板块"),
    "concept": ("m:90+t:3", "gn", "概念板块"),
    "region": ("m:90+t:1", "dy", "地域板块"),
}


def _pick_sector_kind(question: str) -> tuple[str, str, str]:
    if "概念" in question or "题材" in question:
        return _SECTOR_KINDS["concept"]
    if "地域" in question or "地区" in question or "省市" in question:
        return _SECTOR_KINDS["region"]
    return _SECTOR_KINDS["industry"]


def _sector_line(i: int, s: dict) -> str:
    """统一后结构的排版：两个数据源共用同一套输出，模型看到的格式与来源无关。"""
    tail = ""
    if s.get("leader"):
        tail = f"，领涨股 {s['leader']}（{s.get('leader_code') or '-'}）{_pct(s.get('leader_pct'))}"
    breadth = ""
    if s.get("up") is not None and s.get("down") is not None:
        breadth = f"，板块内 {s['up']} 涨 {s['down']} 跌"
    return f"{i}. {s.get('name')} {_pct(s.get('pct'))}（指数 {s.get('point')}）{tail}{breadth}"


async def _load_sectors(question: str, top_n: int, desc: bool) -> tuple[list[dict], str]:
    """主源（东财）失败 → 备源（腾讯）。返回 (板块列表, 数据源名)。"""
    fs, board, kind = _pick_sector_kind(question)
    rows = await _em_sectors(fs, top_n, desc)
    if rows:
        return rows, "东方财富"
    rows = await _tx_sectors(board, top_n, desc)
    if rows:
        log.info("tool.market.sector_fallback", source="tencent", kind=kind)
        return rows, "腾讯财经"
    return [], kind


async def fetch_sectors(question: str, top_n: int = 8) -> RealtimeResult | None:
    _, _, kind = _pick_sector_kind(question)
    ups, src = await _load_sectors(question, top_n, desc=True)
    if not ups:
        log.info("tool.market.sector_empty", kind=kind)
        return None

    tail_n = max(3, top_n // 2)
    downs, _ = await _load_sectors(question, tail_n, desc=False)

    lines = [f"【A股{kind}涨跌幅排行 · 实时】查询时间：{_now()}", "—— 涨幅居前 ——"]
    lines += [_sector_line(i, s) for i, s in enumerate(ups[:top_n], 1)]
    if downs:
        lines.append("—— 跌幅居前 ——")
        lines += [_sector_line(i, s) for i, s in enumerate(downs[:tail_n], 1)]
    lines.append(f"数据来源：{src}{kind}行情接口（按涨跌幅排序，实时口径）")
    lines.append("说明：以上为交易所实时行情数据，不构成任何投资建议。")

    summary = f"{kind} 领涨 {ups[0].get('name')} {_pct(ups[0].get('pct'))}"
    log.info("tool.market.sector_ok", kind=kind, source=src, n=len(ups))
    return RealtimeResult(tool="market_sector", text="\n".join(lines), summary=summary,
                          answer_source="market")


# ============================================================
#  子工具三：大盘概览（指数 + 领涨板块）
# ============================================================
async def fetch_overview(question: str) -> RealtimeResult | None:
    secids = _extract_index(question) or _INDEX_DEFAULT
    idx, src = await _load_indices(secids)
    if not idx:
        return None

    lines = [f"【A股大盘实时概览】查询时间：{_now()}"]
    for it in idx:
        lines.append(
            f"· {it['name']}：{it['price']} 点，{_pct(it.get('pct'))}"
            f"（{it.get('change') if it.get('change') is not None else '-'} 点），"
            f"今开 {it.get('open') or '-'} / 最高 {it.get('high') or '-'} / 最低 {it.get('low') or '-'}"
            + (f"，成交额 {_yuan(it.get('amount'))}" if it.get("amount") else "")
        )
    # 大盘概览顺带给出领涨板块 —— "今天股市怎么样"这种问法，用户真正想知道的就是这个
    ups, sec_src = await _load_sectors(question, 5, desc=True)
    if ups:
        lines.append("—— 领涨行业板块 ——")
        lines += [_sector_line(i, s) for i, s in enumerate(ups, 1)]
        src = src if src == sec_src else f"{src} / {sec_src}"
    lines.append(f"数据来源：{src}行情接口（实时）· 以上仅为行情数据，不构成投资建议。")

    summary = " ".join(f"{it['name']} {_pct(it.get('pct'))}" for it in idx[:3])
    log.info("tool.market.overview_ok", n=len(idx), source=src)
    return RealtimeResult(tool="market_overview", text="\n".join(lines), summary=summary,
                          answer_source="market")


# ============================================================
#  子工具四：汇率
# ============================================================
_CURRENCY = {
    "美元": ("USD", "美国"), "美金": ("USD", "美国"), "欧元": ("EUR", "欧元区"),
    "日元": ("JPY", "日本"), "港币": ("HKD", "中国香港"), "港元": ("HKD", "中国香港"),
    "英镑": ("GBP", "英国"), "韩元": ("KRW", "韩国"), "澳元": ("AUD", "澳大利亚"),
    "新加坡元": ("SGD", "新加坡"), "加元": ("CAD", "加拿大"), "新台币": ("TWD", "中国台湾"),
}
_CN_NAME = {"USD": "美元", "EUR": "欧元", "JPY": "日元", "HKD": "港币", "GBP": "英镑",
            "KRW": "韩元", "AUD": "澳元", "SGD": "新加坡元", "CAD": "加元", "TWD": "新台币",
            "CNY": "人民币"}


async def fetch_fx(question: str) -> RealtimeResult | None:
    wants = [c for cn, (c, _) in _CURRENCY.items() if cn in question]
    # 只提"汇率"没提币种 → 给最常用的四种
    targets = list(dict.fromkeys(wants)) or ["USD", "EUR", "JPY", "HKD"]
    try:
        d = (await _CLIENT.get(_FX_URL, headers=_UA)).json()
        rates = (d or {}).get("rates") or {}
        updated = str((d or {}).get("time_last_update_utc") or "未知")
    except Exception as e:  # noqa: BLE001
        log.warning("tool.market.fx_fail", error=str(e))
        return None
    base = rates.get("CNY")
    if not base:
        return None

    lines = [f"【主要货币汇率】查询时间：{_now()}"]
    for cur in targets:
        v = _f(rates.get(cur))
        if not v:
            continue
        cn = _CN_NAME.get(cur, cur)
        lines.append(f"· 1 {cn}（{cur}）≈ {base / v:.4f} 人民币")
        lines.append(f"· 1 人民币 ≈ {v:.4f} {cn}（{cur}）")
    if len(lines) == 1:
        return None
    lines.append(f"数据来源：open.er-api.com（汇率中间价，更新于 {updated}）")
    lines.append("说明：仅为参考汇率，实际成交以银行柜台/交易系统报价为准。")

    summary = f"汇率 {len(targets)} 个币种"
    log.info("tool.market.fx_ok", currencies="|".join(targets))
    return RealtimeResult(tool="market_fx", text="\n".join(lines), summary=summary,
                          answer_source="market")


# ============================================================
#  路由入口
# ============================================================
def plan(question: str) -> str:
    """决定本问要走哪个子工具。纯规则、零 IO，可单测。

    判定顺序（先具体后笼统，避免"大盘多少点"被当成"查某只股票"）：
        汇率 → 六位代码 → 指数名 → 板块词 → 疑似个股名 → 大盘概览兜底
    这样"今天哪个板块涨"不会被当成个股查询，也不会四种数据全拉一遍浪费上下文。
    """
    if _FX_TRIGGER.search(question):
        return "fx"
    if _extract_code(question):
        return "stock"
    if _extract_index(question):
        return "overview"
    if _SECTOR_TRIGGER.search(question):
        return "sector"
    if _name_candidates(question):
        return "stock"
    return "overview"


async def dispatch(question: str, redact: set[str] | None = None) -> RealtimeResult | None:
    """行情工具统一入口。返回 None = 这个工具答不了，上层静默降级。

    redact：出公网前要剔除的内网词。这里复用 websearch.sanitize_query 的语义，
    保证"问股价"这种看似无害的问题也不会把内部产品代号捎给外部接口。
    """
    if not market_enabled():
        log.info("tool.market.disabled", reason="开关关闭")
        return None

    q = question
    if redact:
        from .websearch import sanitize_query  # 局部 import：避免工具模块之间形成循环依赖

        q, removed = sanitize_query(question, redact)
        if removed:
            log.info("tool.market.redacted", removed="|".join(removed))

    kind = plan(q)
    handlers = {
        "fx": fetch_fx,
        "stock": fetch_stock,
        "sector": fetch_sectors,
        "overview": fetch_overview,
    }
    try:
        res = await handlers[kind](q)
    except Exception as e:  # noqa: BLE001  单个外部接口失败不得影响整轮问答
        log.warning("tool.market.fail", kind=kind, error=str(e))
        return None
    # 主路径无数据 → 退而求其次给大盘概览，至少不是"我不知道"
    if res is None and kind == "stock":
        try:
            return await fetch_overview(q)
        except Exception:  # noqa: BLE001
            return None
    return res
