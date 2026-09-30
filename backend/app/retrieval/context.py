"""
会话上下文治理 —— 把"上下文窗口"当成一等公民来管理。

同类项目踩过的坑：
    5 轮对话后，每轮检索 5 篇文档（每篇 ~800 字），加上 system prompt 和历史消息，
    第 6 轮模型直接返回 context_length_exceeded。

这不是靠扩大窗口能解决的——窗口再大，不控制输入量迟早还是会爆。四层处理：

    Layer 1  追问检测       "那需要什么材料？" → 沿用上一轮引用文档范围，不全量重检
    Layer 2  引用溯源过滤   只把上一轮**被实际引用**的文档带进下一轮（由 quality 模块完成）
    Layer 3  滑动窗口裁剪   历史按字符预算裁剪，而不是固定取最近 N 条（长短消息差 10 倍）
    Layer 4  会话边界识别   "好的""不需要""再见" → 识别为收尾，不重新检索

Layer 3 是本模块最实用的一条：原来写死 `history[-6:]`，
一条 800 字的长答案和一条"好"的消息占用完全不同，按字符预算才稳。
"""
from __future__ import annotations

import re

from ..logging_setup import get_logger

log = get_logger("retrieval.context")

# ------------------------------------------------------------
# Layer 1：追问信号
# ------------------------------------------------------------
# 句首承接词：出现即说明这句大概率是接着上一轮在问
FOLLOWUP_LEADS: tuple[str, ...] = (
    "那", "这", "它", "其", "上面", "上一条", "刚才", "还", "再", "另外", "此外",
    "前者", "后者", "这些", "那些", "这个", "那个", "其中", "具体来说", "那如果",
    "这种情况下", "这么", "那么", "还有就是", "如果是", "要是不",
)

# 追问题式：短句 + 疑问词/语气词，且没有新的实体信息
FOLLOWUP_PATTERNS = re.compile(
    r"^(.{0,14})?(呢|吗|怎么样|如何|是多少|有没有|要不要|能不能|可以吗|是吗|对吗)[？?]?$"
)

# 明确的转向信号：一旦出现，禁止沿用上一轮范围（用户已经不想聊刚才那个了）
REDIRECT_CUES: tuple[str, ...] = (
    "换个问题", "换一个问题", "不问这个", "换个话题", "另外问", "重新问",
    "不是这样", "我说的是", "我的意思是",
)

# ------------------------------------------------------------
# Layer 4：会话边界（收尾/应答），这些不是新问题
# ------------------------------------------------------------
BOUNDARY_RESPONSES: frozenset[str] = frozenset({
    "好的", "好", "好的谢谢", "谢谢", "thanks", "thank you", "thx", "收到", "明白",
    "知道了", "了解了", "清楚", "清楚了", "ok", "okay", "行", "可以", "不用了",
    "不需要", "没事了", "没了", "再见", "拜拜", "bye", "先这样", "就这样", "辛苦了",
})


def is_explicit_redirect(question: str) -> bool:
    """用户明确换了话题/纠偏了上一轮理解。"""
    q = (question or "").strip()
    return any(cue in q for cue in REDIRECT_CUES)


# 语气词后缀：用户很少只回一个词，常常带"好的谢谢！""不需要了～"
_TRAILING_MOOD = "了啦的呀哦哈吧咯呐呢啊嘛"


def _normalize_reply(q: str) -> str:
    """去掉标点与语气词后缀，让"不需要了""好的谢谢！"都能对上边界词表。"""
    s = (q or "").strip().strip("。！!～~…、,，. ")
    return s.rstrip(_TRAILING_MOOD) or s


def is_session_boundary(question: str) -> bool:
    """用户在对 AI 的追问做应答 / 结束会话，这类输入不该触发新一轮检索。"""
    q = (question or "").strip().strip("。！!～~…、,，. ")
    if not q:
        return True
    low = q.lower()
    if low in BOUNDARY_RESPONSES:
        return True
    return _normalize_reply(low) in BOUNDARY_RESPONSES


def is_followup(question: str, history: list[dict] | None = None) -> bool:
    """
    判断本轮是否为"追问"（沿用上一轮话题的窄问题）。

    三个信号，命中任一且未被显式转向即为追问：
      1. 以承接词开头（"那…""这个…""还有吗"）
      2. 超短承接句（≤14 字且带疑问语气词）
      3. 上一条助手消息存在，且本句没有引入任何新的名词性内容（长度 < 上一轮答案的 1/5）
    """
    q = (question or "").strip()
    if not q or is_explicit_redirect(q) or is_session_boundary(q):
        return False

    if q.startswith(FOLLOWUP_LEADS):
        return True
    if len(q) <= 14 and FOLLOWUP_PATTERNS.match(q):
        return True

    # 相对短：历史里最近一条助手消息明显比本句长很多 → 大概率是顺着往下追
    if history:
        for turn in reversed(history):
            if turn.get("role") == "assistant":
                ans = turn.get("content") or ""
                if len(ans) > 120 and len(q) < max(12, len(ans) // 5):
                    return True
                break
    return False


def focus_doc_scope(history: list[dict] | None) -> set[str]:
    """
    Layer 2 的落地：从上一轮助手消息的引用里取出"真正被引用过的文档名"。

    只取被引用过的（而非召回过的）—— 这正是"引用溯源过滤"省上下文的地方：
    上一轮召回了 8 篇，答案只引了 2 篇，下一轮就只在这 2 篇附近找。
    """
    docs: set[str] = set()
    if not history:
        return docs
    for turn in reversed(history):
        if turn.get("role") != "assistant":
            continue
        cites = turn.get("citations") or []
        for c in cites:
            name = c.get("doc_name") if isinstance(c, dict) else getattr(c, "doc_name", None)
            if name:
                docs.add(str(name))
        break       # 只看最近一轮助手消息
    return docs


# ------------------------------------------------------------
# Layer 3：滑动窗口裁剪
# ------------------------------------------------------------
def clip_history(
    history: list[dict],
    max_chars: int = 3000,
    max_turns: int = 6,
) -> list[dict]:
    """
    按**字符预算**保留最近若干轮，而不是固定条数。

    为什么不用固定条数：一条报错堆栈可能 2000 字，一条"好的"只有 2 字，
    固定取 6 条完全无法控制实际输入量。字符预算才是上下文的真实度量。

    裁剪时保证：
      · 从最近往回累加，超预算即止（最近的消息永远优先）
      · 结果按时间正序返回（模型要求的顺序）
      · 单条超长消息（如贴了大段日志）截断保留尾部（通常结论在结尾）
    """
    if not history or max_chars <= 0:
        return []

    budget = max_chars
    kept: list[dict] = []
    for turn in reversed(history):
        content = str(turn.get("content") or "")
        if len(content) > budget:
            # 单条就超预算 → 截尾保预算，宁可少带也不能让上下文爆掉
            if budget <= 0:
                break
            kept.append({**turn, "content": "…" + content[-budget:]})
            budget = 0
            break
        budget -= len(content)
        kept.append(turn)
        if len(kept) >= max_turns * 2:      # user+assistant 算两轮
            break
        if budget <= 0:
            break

    kept.reverse()
    return kept


def build_context_plan(question: str, history: list[dict] | None, max_chars: int = 3000) -> dict:
    """
    一次性算出本轮的上下文策略，供 retrieve / generate 两个节点共用。

    返回 dict：
      followup     是否追问（→ 检索范围收敛）
      boundary     是否会话收尾（→ 可以跳过检索）
      focus_docs   要优先命中的文档名集合
      clipped      裁剪后的历史消息
    """
    history = history or []
    followup = is_followup(question, history)
    return {
        "followup": followup,
        "boundary": is_session_boundary(question),
        "redirect": is_explicit_redirect(question),
        "focus_docs": focus_doc_scope(history) if followup else set(),
        "clipped": clip_history(history, max_chars=max_chars),
    }
