"""
闲聊 / 客套话识别 —— 供三处共用：

  1. 意图分类器 L0（比硬规则更早）：纯寒暄直接判 chitchat，不进检索
  2. 请假 / 报销引导流程：填表中途的"好哒，谢谢"不能当成字段答案吃掉

判定原则（宁可漏判、不可误判）：
  · 只识别**纯**客套话 —— 消息里一旦出现业务词（假/报销/制度/怎么…）就不是闲聊
  · 长度超过 16 字直接放弃（长句几乎不可能是纯寒暄）
  · 按标点切分后，每一段都必须命中客套词表，才算纯闲聊
"""
from __future__ import annotations

import random
import re

# 单个客套词段（fullmatch 才算命中）；末尾允许带语气词（收到啦 / 谢谢哈 / 明白了）
_BODY = (
    # 应答 / 附和
    r"好的?|好哒|好嘞|好呀|好嘞嘞|嗯+|哦+|噢+|额|对|对的|行|行吧|中|是滴|是的呀|"
    # 感谢
    r"谢谢|多谢|感谢|太感谢了?|辛苦了?|麻烦你了?|不客气|不用谢|"
    # 告别
    r"再见|拜拜|晚安|回见|下次见|"
    # 问候
    r"你好|您好|哈喽|嗨|早上好|上午好|中午好|下午好|晚上好|"
    # 领会
    r"明白|了解|知道|收到|懂了|记住了|get到了?|"
    # 夸赞 / 情绪
    r"哈哈+|嘿嘿|呵呵|嘻嘻|厉害|真棒|太棒了|太好了|不错不错|完美|"
    # 英文
    r"ok|okay|thx|thanks|thank\s*you|bye|hi|hello|hey"
)
_TOKEN_RE = re.compile(r"(?:" + _BODY + r")[啦了哦呀哈嘛呗呢]?", re.IGNORECASE)

# 出现这些业务词 / 疑问词就绝不是闲聊（保护："好的，那报销怎么走流程"）
_BUSINESS_HINT_RE = re.compile(
    r"(假|报销|薪|工资|制度|流程|考勤|打卡|部门|公司|系统|审批|单据|发票|"
    r"怎么|为什么|如何|什么|多少|哪|吗|？|\?)",
    re.IGNORECASE,
)

# 纯表情 / 符号
_EMOJI_ONLY_RE = re.compile(r"^[\W_]+$", re.UNICODE)


def is_chitchat(text: str) -> bool:
    """是否为纯客套话 / 寒暄（不含任何业务内容或疑问）。"""
    t = (text or "").strip()
    if not t or len(t) > 16:
        return False
    if _BUSINESS_HINT_RE.search(t):
        return False
    # 纯表情 / 标点（如 👍🎉）视为客套
    if _EMOJI_ONLY_RE.fullmatch(t) and not t.isdigit():
        return True
    segs = [s for s in re.split(r"[\s，,。.！!？?~～、;；的]+", t) if s]
    if not segs:
        return False
    return all(_TOKEN_RE.fullmatch(s) for s in segs)


def chitchat_reply(text: str) -> str:
    """给纯客套话一个自然、简短的回应。

    每类场景维护一个变体池随机抽取——同一句话连问两次不该听到一字不差的回答，
    否则用户立刻能感觉到"对面是个模板"。
    """
    t = (text or "").strip()
    if re.search(r"谢谢|感谢|多谢|thx|thanks", t, re.I):
        return random.choice([
            "不客气～有需要随时找我 😊",
            "不客气，有需要随时问我～",
            "应该的～还有别的问题随时说。",
            "不用谢～能帮上忙就好。",
        ])
    if re.search(r"再见|拜拜|晚安|回见|下次见|bye", t, re.I):
        return random.choice([
            "再见～有需要随时找我。",
            "拜拜～祝工作顺利！",
            "回见～我一直在。",
        ])
    if re.search(r"你好|您好|哈喽|嗨|hello|hi|早上好|上午好|中午好|下午好|晚上好", t, re.I):
        return random.choice([
            "你好呀～有什么可以帮您？可以问制度流程，也可以说「我想请假」「我想报销」。",
            "你好～公司制度、部门业务都可以问我，说「我想请假」也能帮您发起申请。",
        ])
    if re.search(r"厉害|真棒|太棒|不错|完美|哈哈|嘿嘿|嘻嘻", t, re.I):
        return random.choice([
            "谢谢夸奖～能帮到您就好 😊",
            "嘿嘿，随时为您服务～",
        ])
    return random.choice(["好的～", "收到～", "嗯嗯，我在。"])
