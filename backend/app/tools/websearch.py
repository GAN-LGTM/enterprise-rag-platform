"""
通用联网搜索工具。

大模型不知道"今天"发生的事（金价、新闻、赛事等），闲聊类问题中带时效性的
先走这里：必应搜索拿结果列表 → 抓取前 2 个结果页正文 → 拼成实时上下文给模型。

引擎：必应（cn.bing.com）主用，搜狗兜底（本机实测：百度 302 反爬、DuckDuckGo 不可达）。
任何失败都返回 None 静默降级，绝不阻塞主流程。

结果质量治理：搜索引擎对时效性问题首屏常常给出导航站、网址大全、词典这类
"有标题有正文但没有答案"的页面。抓回来喂给模型，等于逼它在一堆垃圾里编答案。
所以 results 会先过一遍黑名单与垃圾正文判定再送入上下文（见 _is_junk_result）。
纯行情类问题应当走 tools/finance.py 的结构化接口，不在这里硬扒。
"""
from __future__ import annotations

import asyncio
import datetime
import html as _html
import re
from urllib.parse import urlparse

import httpx

from ..config import settings
from ..logging_setup import get_logger

log = get_logger("tools.websearch")

_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
_CLIENT = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(8.0), follow_redirects=True)

# 时效性触发词：时间词 / 行情新闻名词 / 生活娱乐名词。只对闲聊类意图生效（dept_ids 为空时才进来）
_SEARCH_TRIGGER = re.compile(
    r"今天|今日|现在|最新|最近|实时|目前|今晚|刚刚|本周|今年|近期"
    r"|金价|银价|油价|股价|股票|基金|汇率|利率"
    r"|多少钱|行情|新闻|热搜|比分|赛事|票房"
    r"|演唱会|音乐会|音乐节|演出|话剧|漫展|画展"
    r"|电影|上映|影院|电视剧|综艺|追剧"
    r"|球赛|赛程|欧冠|世界杯|奥运会|亚运会"
    r"|旅游景点|去哪玩|哪里好玩|旅游攻略|美食推荐|明星|歌手|专辑|新歌"
)

# 搜索词里的问句尾巴，去掉后搜索质量更高
_TAIL_WORDS = re.compile(r"怎么样|怎么看待|如何|什么样|吗|呢|？|\?")

_MAX_RESULTS = 6
_PAGE_FETCH = 2          # 抓前 2 个结果页正文
_PAGE_CHARS = 1500       # 每页最多带入正文字符


# ---------------- 结果质量治理 ----------------
# 导航站 / 网址大全 / 词典词条 / 下载站 / 跳转中间页：这类页面有标题有正文却没有答案，
# 混进上下文只会稀释有效信息，甚至诱导模型"照着导航页编"。直接剔除。
_JUNK_HOST = re.compile(
    r"hao123|hao\.360|360\.(cn|com)|123\.com|2345\.com|baidu\.com/link|baidu\.com/s\?"
    r"|iciba\.com|cidian\.|dict\.(cn|com)|dict\.youdao|dict\.bing|fanyi\."
    r"|downxia|cr173|pcsoft|xiazaiba|rjbgx|softonic"
    r"|zhidao\.baidu|tieba\.baidu|wenda\.|localhost|example\.com",
    re.I,
)
# 正文侧特征词：即便域名没命中，也要防"跳转提示页""纯导航页"
_JUNK_TEXT = re.compile(
    r"网址大全|热门网址|常用网址|导航地带|友情链接|页面正在跳转|正在为您跳转"
    r"|您的浏览器版本过低|请使用IE浏览器|站点地图|copyright.*all rights reserved",
    re.I,
)


def _is_junk_result(r: dict) -> bool:
    url = r.get("url") or ""
    host = urlparse(url).netloc
    return bool(_JUNK_HOST.search(host) or _JUNK_HOST.search(url))


def is_search_query(question: str) -> bool:
    return bool(_SEARCH_TRIGGER.search(question))


# 强时效触发：时间词 + 行情/新闻/娱乐名词组合（或行情名词+走势问法）。
# 命中时无论意图分类结果是什么都直接联网——这类问题知识库里必然没有答案。
_HARD_TRIGGER = re.compile(
    r"(今天|今日|现在|最新|最近|近期|实时|今晚|本周|今年|周末|国庆|春节|五一|元旦)"
    r"[^。？?]{0,12}(金价|银价|油价|股价|汇率|新闻|比分|票房|热搜|行情"
    r"|演唱会|音乐会|音乐节|演出|话剧|漫展|画展|电影|上映|综艺|球赛|赛程|赛事|比赛)"
    r"|(金价|银价|油价|股价|汇率|大盘)[^。？?]{0,8}(多少钱|走势|行情|涨了|跌了|涨|跌)"
    r"|(演唱会|音乐会|音乐节|话剧|漫展|电影|综艺|球赛|演出|电视剧)"
    r"[^。？?]{0,6}(门票|票价|排期|行程|有哪些|有什么|推荐|安排|资讯|信息)"
)


def is_hard_realtime(question: str) -> bool:
    return bool(_HARD_TRIGGER.search(question))


def _strip_tags(s: str) -> str:
    text = re.sub(r"<[^>]+>", "", s or "")
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


async def _bing_search(query: str) -> list[dict]:
    r = await _CLIENT.get("https://cn.bing.com/search",
                          params={"q": query, "setlang": "zh-CN", "count": 10}, headers=_UA)
    blocks = re.split(r'<li class="b_algo"', r.text)[1:]
    out = []
    for b in blocks:
        m = re.search(r'<h2[^>]*><a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        if not m:
            continue
        url = m.group(1)
        if not url.startswith("http"):
            continue
        p = re.search(r"<p[^>]*>(.*?)</p>", b, re.S)
        out.append({"url": url, "title": _strip_tags(m.group(2)),
                    "snippet": _strip_tags(p.group(1)) if p else ""})
        if len(out) >= _MAX_RESULTS:
            break
    return out


async def _sogou_search(query: str) -> list[dict]:
    r = await _CLIENT.get("https://www.sogou.com/web", params={"query": query}, headers=_UA)
    out = []
    for m in re.finditer(r'<h3[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S):
        url = m.group(1)
        if url.startswith("/link"):
            url = "https://www.sogou.com" + url
        if not url.startswith("http"):
            continue
        out.append({"url": url, "title": _strip_tags(m.group(2)), "snippet": ""})
        if len(out) >= _MAX_RESULTS:
            break
    return out


async def _fetch_page_text(url: str) -> str | None:
    """抓结果页正文前若干字符。页面很多是 JS 渲染，失败就跳过。"""
    try:
        r = await _CLIENT.get(url, headers=_UA)
        if r.status_code != 200 or len(r.text) < 500:
            return None
        body = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", r.text, flags=re.S | re.I)
        text = re.sub(r"\s+", " ", _strip_tags(body))
        if len(text) < 80:
            return None
        if _JUNK_TEXT.search(text):      # 跳转页 / 纯导航页：有字但没内容
            log.info("tool.search.junk_page", url=url[:80])
            return None
        return text[:_PAGE_CHARS]
    except Exception:  # noqa: BLE001
        return None


# 带时效词但句中没有年份时，搜索词补上当前年份（"最近有什么电影推荐"→ 加 2026），
# 否则搜索引擎容易把"最近"当成歌名/词条，返回过时结果。
_NEED_YEAR = re.compile(r"最近|近期|最新|今年|本周末|周末|新番|上映|排期")
_HAS_YEAR = re.compile(r"(19|20)\d{2}")


def _build_query(question: str) -> str:
    q = _TAIL_WORDS.sub("", question).strip() or question
    if _NEED_YEAR.search(q) and not _HAS_YEAR.search(q):
        q = f"{q} {datetime.date.today().year}"
    return q


def public_search_enabled() -> bool:
    """公网搜索总开关。同时控制实时天气等所有出公网工具。

    由 settings.PUBLIC_SEARCH_ENABLED 决定：OFFLINE_MODE=true（私有化默认）时为关闭，
    客户显式 PUBLIC_SEARCH_ENABLED=true 才开启。"""
    return settings.PUBLIC_SEARCH_ENABLED


def sanitize_query(question: str, redact: set[str] | None = None) -> tuple[str, list[str]]:
    """出公网前脱敏：剔除可能泄露内网上下文的词。

    入参 redact 是待剔除词集合（部门名 / 人名 / 内部术语 / 用户身份）。
    返回 (脱敏后的查询, 被剔除的词列表)。

    安全原则：
      * 长词优先替换，避免"销售"误删"销售部业绩"里的合理片段；
      * 脱敏后若啥也不剩，宁可不过滤（返回原句），绝不对外发一个空查询——
        空查询反而暴露"这里有不能说的东西"。
    """
    if not redact:
        return question, []
    removed: list[str] = []
    q = question
    for term in sorted((t for t in redact if t), key=len, reverse=True):
        if term in q:
            q = q.replace(term, "")
            removed.append(term)
    q = re.sub(r"\s{2,}", " ", q).strip()
    if len(q) < 2:
        # 脱敏后无意义：放弃脱敏，原样返回，由上层决定是否放行
        return question, []
    return q, removed


async def _tavily_search(query: str) -> tuple[list[dict], str]:
    """Tavily：面向 LLM 设计的搜索 API，返回可直接进 Prompt 的摘要。"""
    r = await _CLIENT.post(
        "https://api.tavily.com/search",
        json={"api_key": settings.SEARCH_API_KEY, "query": query,
              "max_results": _MAX_RESULTS, "search_depth": "basic",
              "include_answer": True},
        timeout=httpx.Timeout(12.0),
    )
    if r.status_code != 200:
        log.warning("tool.search.tavily_http", status=r.status_code)
        return [], ""
    d = r.json() or {}
    out = [{"title": x.get("title") or "", "url": x.get("url") or "",
            "snippet": (x.get("content") or "").strip()}
           for x in (d.get("results") or []) if x.get("url")]
    return out, (d.get("answer") or "").strip()


async def _serper_search(query: str) -> tuple[list[dict], str]:
    """Serper：Google 结果的结构化封装。"""
    r = await _CLIENT.post(
        "https://google.serper.dev/search",
        headers={"X-API-KEY": settings.SEARCH_API_KEY, "Content-Type": "application/json"},
        json={"q": query, "num": _MAX_RESULTS},
        timeout=httpx.Timeout(12.0),
    )
    if r.status_code != 200:
        log.warning("tool.search.serper_http", status=r.status_code)
        return [], ""
    d = r.json() or {}
    out = [{"title": x.get("title") or "", "url": x.get("link") or "",
            "snippet": (x.get("snippet") or "").strip()}
           for x in (d.get("organic") or []) if x.get("link")]
    kb = d.get("knowledgeGraph") or {}
    return out, (kb.get("description") or "").strip()


async def _api_search(query: str) -> tuple[list[dict], str] | None:
    """结构化搜索 API 通道。

    这才是"像豆包/DeepSeek 那样联网"的做法：向搜索 API 要 JSON，
    拿到的是干净的 title/snippet/url，而不是自己去爬搜索引擎的 HTML 再
    猜哪段是正文——后者必然混进导航站、词典页、转载站（脏结果的根源）。

    没配 provider/key 时返回 None，调用方回退到抓取兜底。
    """
    provider = (settings.SEARCH_API_PROVIDER or "").strip().lower()
    if not provider or not (settings.SEARCH_API_KEY or "").strip():
        return None
    try:
        if provider == "tavily":
            results, answer = await _tavily_search(query)
        elif provider == "serper":
            results, answer = await _serper_search(query)
        else:
            log.warning("tool.search.unknown_provider", provider=provider)
            return None
    except Exception as e:  # noqa: BLE001
        log.warning("tool.search.api_fail", provider=provider, error=str(e))
        return None
    if not results:
        return None
    log.info("tool.search.api_ok", provider=provider, n=len(results))
    return results, answer


async def fetch_search(
    question: str, redact: set[str] | None = None
) -> tuple[str, str] | None:
    """返回 (realtime_tool, context_text)；失败返回 None。

    redact 非空时，出网前先脱敏（剔除部门名/人名/内部术语/用户身份），
    确保公网搜索引擎只收到"问题本身"，绝不携带内网上下文。
    """
    if not public_search_enabled():
        log.info("tool.search.disabled", reason="PUBLIC_SEARCH_ENABLED=false")
        return None

    safe_q, removed = sanitize_query(question, redact)
    if removed:
        log.info("tool.search.redacted", removed="|".join(removed), original_len=len(question))
    query = _build_query(safe_q)
    api_answer = ""
    via_api = False
    try:
        # ① 优先走结构化搜索 API（配了才走），拿不到再回退网页抓取
        api = await _api_search(query)
        if api:
            results, api_answer = api
            engine, via_api = f"api:{settings.SEARCH_API_PROVIDER}", True
        else:
            results = await _bing_search(query)
            engine = "bing"
            if not results:
                results = await _sogou_search(query)
                engine = "sogou"
        if not results:
            log.info("tool.search.no_result", query=query)
            return None

        # 过滤导航站/词典/下载站：这些页面"有内容但没答案"，留着只会稀释上下文
        kept = [r for r in results if not _is_junk_result(r)]
        if not kept:
            log.info("tool.search.all_junk", query=query, total=len(results))
            return None
        if len(kept) < len(results):
            log.info("tool.search.junk_filtered", dropped=len(results) - len(kept))
        results = kept

        # 走搜索 API 时不再抓网页正文 —— API 给的 content/snippet 就是清洗过的摘要，
        # 再去抓一遍网页等于把脏语料（导航、广告、转载）又请回来，违背换接口的初衷。
        pages: list[str | None] = []
        if not via_api:
            page_tasks = [_fetch_page_text(r["url"]) for r in results[:_PAGE_FETCH]]
            pages = await asyncio.gather(*page_tasks)

        today = datetime.date.today().isoformat()
        src = f"{engine} 结构化接口" if via_api else f"{engine} 网页抓取"
        lines = [f"网络搜索结果（来源：{src}，查询时间：{today}，搜索词：{query}）"]
        if api_answer:
            lines.append(f"检索摘要：{api_answer[:400]}")
        for i, r in enumerate(results, 1):
            domain = urlparse(r["url"]).netloc
            line = f"{i}. {r['title']}（来源：{domain}）"
            if r["snippet"]:
                line += f"\n   摘要：{r['snippet'][:240 if via_api else 180]}"
            lines.append(line)
        for i, t in enumerate(p for p in pages if p):
            lines.append(f"\n—— 第{i + 1}条结果网页正文摘录 ——\n{t}")

        text = "\n".join(lines)
        log.info("tool.search.ok", engine=engine, results=len(results), pages=sum(1 for p in pages if p))
        return "websearch", text
    except Exception as e:  # noqa: BLE001
        log.warning("tool.search.fail", error=str(e))
        return None
