"""
融合后的轻量重排 —— 解决"高频官僚词污染排序"的问题。

背景（来自同类项目的实测教训）：
    "材料""制度""管理""申请"这些词出现在 80% 的企业文档里。若每个命中都加同样的分，
    它们会淹没真正有区分度的关键词（"差旅""报销标准""试用期"），
    表现为：搜"差旅住宿报销"却排在通用《费用管理制度》上。

本模块**不改 RRF 核心**（RRF 仍是主排序），只在融合之后做一层可解释的加权：

    final = base × (1 + 0.15 × evidence × noise_factor) × (1 + phrase_bonus)

    evidence      = 标题命中数×2 + 正文命中数（区分性关键词）
    noise_factor  = 只命中噪声词时降到 0.15，正常为 1.0
    phrase_bonus  = 查询中的长短语在片段里完整出现 → +0.12

设计上的三条克制：
    1. 加权而非替换 —— RRF 分数仍是主序，重排最多把强命中片段提到前面，不会颠覆语义召回。
    2. 先验 sinks 先验收敛 —— `has_strong_match` 为假时（区分性词一个都没命中），
       即使向量撞上了也不加权，杜绝"跟问题无关但向量相似"的假阳性被放大。
    3. 全程纯规则、零依赖、零网络 —— 私有化离线环境可用，单测可复现。
"""
from __future__ import annotations

import re

from ..logging_setup import get_logger
from .store import Chunk

log = get_logger("retrieval.rerank")

# ------------------------------------------------------------
# 噪声词表：企业语料里出现频率极高、但对区分文档几乎没有贡献的词。
# 命中这类词不计入 evidence（若一个片段只命中噪声词，则整体降权到 0.15）。
# ------------------------------------------------------------
NOISE_TERMS: frozenset[str] = frozenset({
    "材料", "资料", "制度", "管理", "规定", "要求", "内容", "信息", "相关", "说明",
    "情况", "工作", "公司", "部门", "员工", "流程", "规范", "标准", "办法", "细则",
    "申请", "审批", "审核", "提供", "进行", "处理", "需要", "可以", "应当", "必须",
    "以及", "或者", "其他", "有关", "规定", "如下", "以下", "事项", "单位", "人员",
    "数据", "业务", "系统", "平台", "服务", "使用", "支持", "确认", "完成", "提交",
})

# 停用词：提取查询关键词时先剔除（疑问代词、助词等，跟文档内容无关）
STOP_WORDS: frozenset[str] = frozenset({
    "哪些", "什么", "怎么", "怎样", "如何", "是否", "可以", "相关", "信息", "内容",
    "要求", "哪位", "找谁", "谁", "吗", "啊", "呢", "吧", "请", "帮我", "告诉我",
    "请问", "我想", "我要", "的", "了", "和", "与", "及", "在", "是", "有", "对",
    "关于", "多少", "几个", "哪里", "为什么", "什么时候",
})

_CJK_RE = re.compile(r"[\u3400-\u9fff]")
# 连续中文必须整段取出再做二元切分：若按单字切，二元词根本组不起来。
_TOKEN_RE = re.compile(r"[\u3400-\u9fff]+|[a-zA-Z0-9]+")

TITLE_WEIGHT = 2.0        # 标题命中的权重（相对正文）
NOISE_FACTOR = 0.15       # 只命中噪声词时的降权系数
EVIDENCE_CAP = 6.0        # evidence 上限，防止长问句把分数拉飞
EVIDENCE_GAIN = 0.15      # 每个 evidence 单位带来的分数增益
PHRASE_BONUS = 0.12       # 完整短语命中加成


def extract_terms(query: str) -> list[str]:
    """
    中文二元分词 + 英文/数字整词，剔除停用词与噪声词。

    为什么是二元：中文没有空格，而"差旅报销"这类核心术语多为 2~4 字，
    二元切分在无需分词库的前提下召回最稳（`内存向量库的 _tokenize_for_fts` 同此思路）。
    噪声词在此处**保留**返回与否要区分——这里返回一个 (terms, noise_terms) 语义：
    为简化调用，本函数只返回"区分性词"，噪声词由 is_noise_only 另行判断。
    """
    q = (query or "").lower()
    units = _TOKEN_RE.findall(q)
    terms: list[str] = []
    for u in units:
        if len(u) <= 2:
            terms.append(u)
            continue
        for i in range(len(u) - 1):
            terms.append(u[i:i + 2])
    out: list[str] = []
    for t in terms:
        if not t or t in STOP_WORDS or t in NOISE_TERMS:
            continue
        if len(t) < 2 and not re.match(r"[a-z0-9]", t):
            continue
        out.append(t)
    # 去重保序
    return list(dict.fromkeys(out))


def extract_phrases(query: str, min_len: int = 3) -> list[str]:
    q = (query or "").strip()
    phrases: list[str] = []
    # 中文无明显分隔符，用滑动窗口切出 3~4 字候选短语。
    # 取多个长度是为了让"差旅费""差旅费报""差旅费报销"都有机会命中，
    # 而不是要求整句一字不差地出现在文档里（那样加权几乎永远不会触发）。
    for seg in re.split(r"[，。！？、,.!?：:；;/\s]+", q):
        for run in re.findall(r"[\u3400-\u9fff]{2,}|[a-zA-Z]{3,}", seg):
            if run and run not in STOP_WORDS:
                phrases.append(run)
            for size in (min_len, min_len + 1):
                if len(run) > size:
                    phrases.extend(run[i:i + size] for i in range(len(run) - size + 1))
    out = [p for p in phrases if len(p) >= min_len and p not in STOP_WORDS]
    return list(dict.fromkeys(out))


def score_evidence(query: str, chunk: Chunk) -> tuple[float, bool]:
    """
    计算单个片段的证据强度。

    返回 (evidence, has_strong_match)。
    evidence 越高说明该片段越"对得上"用户的提问；
    has_strong_match = 至少命中一个区分性关键词（标题或正文）。
    """
    terms = extract_terms(query)
    if not terms:
        return 0.0, False

    title = (chunk.doc_name or "").lower()
    body = (chunk.chunk_text or "").lower()

    title_hits = sum(1 for t in terms if t in title)
    body_hits = sum(1 for t in terms if t in body)
    strong = (title_hits + body_hits) > 0

    if strong:
        evidence = title_hits * TITLE_WEIGHT + body_hits
    else:
        # 一个区分性词都没命中 —— 检查一下是不是只撞上了噪声词
        noise_hits = sum(1 for n in NOISE_TERMS if n in body or n in title)
        evidence = noise_hits * NOISE_FACTOR
    return float(evidence), strong


def _phrase_hit(query: str, chunk: Chunk) -> bool:
    phrases = extract_phrases(query)
    if not phrases:
        return False
    haystack = f"{chunk.doc_name or ''} {chunk.chunk_text or ''}".lower()
    return any(p.lower() in haystack for p in phrases)


def rerank(query: str, chunks: list[Chunk]) -> list[Chunk]:
    """
    对 RRF 融合结果做一轮可解释的重排（原地返回新列表，不修改入参）。

    只在 query 有区分性关键词时生效；否则原样返回（避免空查询被打乱顺序）。
    """
    if not chunks:
        return chunks
    terms = extract_terms(query)
    if not terms:
        return chunks

    scored: list[tuple[float, Chunk]] = []
    for c in chunks:
        evidence, strong = score_evidence(query, c)
        if not strong:
            # 没有区分性命中 → 不加权（相当于上限锁死），杜绝纯向量假阳性被放大
            new_score = float(c.score)
        else:
            ev = min(evidence, EVIDENCE_CAP)
            bonus = EVIDENCE_GAIN * ev
            if _phrase_hit(query, c):
                bonus += PHRASE_BONUS
            new_score = float(c.score) * (1.0 + bonus)
        scored.append((round(new_score, 6), Chunk(
            c.id, c.department_id, c.chunk_text, c.doc_name, c.page_num,
            c.chunk_index, c.version, new_score, c.metadata,
        )))

    scored.sort(key=lambda x: -x[0])
    out = [c for _, c in scored]
    top, old_top = out[0].doc_name, chunks[0].doc_name
    if top != old_top:
        log.info("rerank.reordered", top_before=old_top, top_after=top,
                 before=round(chunks[0].score, 5), after=round(out[0].score, 5))
    return out


def boost_in_docs(chunks: list[Chunk], focus_docs: set[str], factor: float = 1.3) -> list[Chunk]:
    """
    把"上一轮引用过的文档"里的片段提到前面（追问场景的检索范围收敛）。

    不是硬过滤——如果追问的答案其实在别的文档里，这些文档仍会出现（只是分值没被放大），
    避免"收敛过头导致答非所问"。
    """
    if not chunks or not focus_docs:
        return chunks
    out = []
    for c in chunks:
        s = float(c.score) * factor if (c.doc_name or "") in focus_docs else float(c.score)
        out.append(Chunk(c.id, c.department_id, c.chunk_text, c.doc_name, c.page_num,
                         c.chunk_index, c.version, s, c.metadata))
    out.sort(key=lambda x: -x.score)
    return out
