"""
答案质量三道防线 —— 解决"LLM 基于无关文档编造答案"的问题。

来自同类项目的真实事故：
    搜"员工福利标准"，语义向量把《员工手册（含考勤、着装、行为规范）》排到 top-1，
    LLM 拿到这份考勤制度后编了一段"福利包含全勤奖"——这句话在任何企业文档里都不存在。

**检索到文档不等于拿到可用证据。**本模块提供三道防线：

    防线一（检索后）are_sources_relevant  —— 检索结果跟问题不沾边 → 标记依据不足
    防线二（生成前）insufficient_answer   —— 依据不足时下发确定性话术，不硬生成
    防线三（生成后）normalize_used_citations —— 只保留答案里实际引用的 [N]，并按出现顺序重排编号

防线三的意义常被低估：模型说"根据[1][3]回答"却给你 8 条引用卡片，
用户点开发现一半跟答案无关，信任感直接崩掉。而未被引用的检索结果如果留在下一轮上下文里，
还会白白吃掉 40%~60% 的上下文窗口。
"""
from __future__ import annotations

import re

from ..logging_setup import get_logger
from .rerank import extract_terms

log = get_logger("retrieval.quality")

_CITE_RE = re.compile(r"\[(\d+)\]")


# ============================================================
#  防线一：检索后的来源相关性校验
# ============================================================
def are_sources_relevant(question: str, sources: list) -> bool:
    """
    判断检索到的内容是否真的跟问题有关。

    做法：提取问题的区分性关键词，对每条引用做命中计数。
    命中要求随问题复杂度递增 —— 问题越具体，可接受的最低证据就越高，
    避免"一个常见二元词碰巧命中标题"就放行。

    入参 sources 接受两种形态（duck typing，不强依赖具体类型）：
      · Citation(pydantic)  → 取 doc_name / snippet
      · Chunk(dataclass)    → 取 doc_name / chunk_text
    """
    if not sources:
        return False

    terms = extract_terms(question)
    if not terms:
        return True   # 问题太短无法判断 → 放行，宁可不拦也不能误伤

    best_hits = 0
    best_title_hits = 0
    for s in sources:
        title = str(getattr(s, "doc_name", "") or "").lower()
        body = str(getattr(s, "snippet", None) or getattr(s, "chunk_text", "") or "").lower()
        haystack = f"{title} {body}"
        hits = sum(1 for t in terms if t in haystack)
        best_hits = max(best_hits, hits)
        title_hits = sum(1 for t in terms if t in title)
        best_title_hits = max(best_title_hits, title_hits)

    required = max(2, int(len(terms) * 0.25 + 0.999))   # ceil(len*0.25)，下界 2
    ok = max(best_hits, best_title_hits * 2) >= required
    if not ok:
        log.info("quality.irrelevant_sources", terms=len(terms), required=required,
                 best_hits=best_hits, best_title_hits=best_title_hits)
    return ok


# ============================================================
#  防线二：依据不足时的确定性话术
# ============================================================
def insufficient_answer(question: str, sources: list, *, keep: int = 3) -> str:
    """
    检索到了片段但都不相关时，给一段"有据可查"的兜底回答。

    为什么不让 LLM 自由发挥：此时模型手里的资料跟问题无关，
    它越"努力"回答，编造概率越高。确定性话术至少保证三件事——
      1. 明确说"没有检索到直接依据"，不编；
      2. 把最相关的几篇文档名和摘要列出来，用户可自行核对或换问法；
      3. 提示下一步动作（换关键词 / 联系资料负责人），而不是冷冰冰地"无结果"。
    """
    head = (
        f"我检索了您有权访问的知识库，没有找到能直接回答「{question.strip()}」的依据。"
        "为避免给出不准确的信息，这里不做推测。"
    )
    if not sources:
        return head + "\n\n建议换个更具体的说法再问一次，或联系对应部门的资料负责人补充文档。"

    lines = []
    for i, s in enumerate(sources[:keep], 1):
        title = str(getattr(s, "doc_name", "") or "未知文档")
        dept = str(getattr(s, "department_name", "") or "")
        raw = str(getattr(s, "snippet", None) or getattr(s, "chunk_text", "") or "")
        # 只给"这段大概在讲什么"的线索，不把制度原文整段贴进答案：
        # 去掉 markdown 记号（# 标题、**加粗、`代码`、> 引用）再截短，
        # 否则兜底回答会变成一屏原始文档碎片，比不答还难读。
        snippet = re.sub(r"[#*`>]+", " ", raw)
        snippet = re.sub(r"\s+", " ", snippet).strip()[:60]
        dept_part = f"（{dept}）" if dept else ""
        lines.append(f"[{i}] 《{title}》{dept_part}：{snippet}…")

    tail = (
        "\n\n以上是最接近的几篇资料（不含能直接回答该问题的条款）。"
        "资料未明确写出的范围、金额或业务对象不能据此推定，"
        "建议补充相应制度文档，或联系资料负责人确认。"
    )
    return head + "\n\n" + "\n\n".join(lines) + tail


# ============================================================
#  防线三：生成后的引用归一化
# ============================================================
def normalize_used_citations(answer: str, citations: list) -> tuple[str, list]:
    """
    只保留答案中**实际引用**的 [N]，并按首次出现的顺序连续重排编号。

    返回 (改写后的答案, 精简后的引用列表)。
    若答案里一个引用编号都没有（模型忘了标），保守起见**原样返回**，
    不做"帮模型挑几个"这种危险动作。
    """
    if not citations or not answer:
        return answer, citations

    used: list[int] = []
    for m in _CITE_RE.finditer(answer):
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        if 1 <= n <= len(citations) and n not in used:
            used.append(n)
    if not used:
        return answer, citations

    number_map = {old: new for new, old in enumerate(used, start=1)}

    def _sub(m: re.Match) -> str:
        try:
            n = int(m.group(1))
        except ValueError:
            return m.group(0)
        return f"[{number_map[n]}]" if n in number_map else m.group(0)

    new_answer = _CITE_RE.sub(_sub, answer)
    new_citations = [citations[old - 1] for old in used]

    if len(new_citations) < len(citations):
        log.info("quality.citations_pruned", before=len(citations), after=len(new_citations))
    return new_answer, new_citations


# ============================================================
#  辅助：显式纠偏识别（"不是 X，是 Y"）
# ============================================================
def explicit_user_correction(question: str) -> str:
    """
    识别用户明确的自我纠偏，返回纠偏后的真实问句；未识别到返回空串。

    用于两件事：
      · 禁止把"我说的是……"当成新一轮检索，避免重复召回同一批无用文档
      · 让纠偏后的问句直接替换原问句进入检索
    """
    value = (question or "").strip().strip('\u201c\u201d"\u2018\u2019')
    m = re.match(r"^不是.{0,30}?[，,；;]\s*(?:是|应该是)\s*(.+)$", value)
    if m and m.group(1):
        return m.group(1).strip()
    m = re.match(r"^(?:不是[，,：:\s]*(?:是)?|我说的是|我的意思是|应该是)\s*(.+)$", value)
    return m.group(1).strip() if (m and m.group(1)) else ""
