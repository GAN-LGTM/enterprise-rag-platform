"""
意图分类器 —— 四层递进式识别（对应需求书 FR-CHAT-02）。

    L1 硬规则       0ms      覆盖 ~80%   准确率 ~95%
    L2 会话继承     0ms      覆盖 ~10%
    L3 小模型分类   <50ms    覆盖 ~8%    置信度 ≥0.8 采纳 / 0.5~0.8 追加确认
    L4 LLM 兜底     1-3s     覆盖 ~2%    仍不确定 → 落 dept_kb（最安全默认）

设计要点：越贵的判断越靠后，把 90% 的请求挡在 GPU 之外（成本与延迟双优化）。

准确性三板斧：
  1. 置信度分级：L3 处于 0.5~0.8 的"模糊地带"不硬猜，返回确认引导（need_confirm），
     由生成节点前置一句"您是想问…吗？"，让用户一句话纠偏，避免误路由。
  2. 会话上下文补偿：多轮里出现指代（"刚才那个""还有吗"）或超短问句时，
     L2 优先于 L1，不再硬分类；但用户明确纠偏（"不是""换个问题"）时禁止继承。
  3. 兜底最安全：L4 判成 cross_dept / operation 时必须有"硬证据"
     （点名部门 / 动作词），缺证一律降级为 dept_kb，避免越权与误操作。

判定留痕：每次判定写 data/intent_log.jsonl（带 trace_id），
用于规则层补词与小模型微调，详见 scripts/intent_report.py。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..core import intent_log
from ..logging_setup import get_logger
from ..metrics import INTENT_COUNT
from .bert_classifier import classify as bert_classify
from .chitchat import is_chitchat
from .rules import extract_target_depts, has_action_marker, match_rule_detail

log = get_logger("intent")

INTENTS = ["chitchat", "public_kb", "dept_kb", "cross_dept", "data_analysis", "operation"]

# 第三层：轻量语义打分器的关键词权重（生产环境替换为 BERT-tiny ONNX 推理）
_L3_KEYWORDS: dict[str, list[str]] = {
    "public_kb": ["制度", "流程", "规定", "办法", "政策", "标准", "手册", "指南", "规范"],
    "dept_kb": ["我们", "本部门", "部门", "团队", "业务", "项目", "客户", "方案", "产品"],
    "cross_dept": ["其他部门", "别的部门", "跨部门", "那边", "他们部门", "对方"],
    "data_analysis": ["数据", "统计", "报表", "指标", "分析", "对比", "多少", "趋势"],
    "operation": ["帮我", "申请", "提交", "发起", "预约"],
    "chitchat": ["你好", "您好", "谢谢", "感谢", "再见", "拜拜", "辛苦", "哈哈", "晚安",
                 "演唱会", "音乐会", "电影", "上映", "综艺", "球赛", "旅游", "明星", "天气"],
}

# 意图 → 确认引导话术（模糊地带时用，避免误路由）
_INTENT_PHRASE: dict[str, str] = {
    "chitchat": "随便聊聊",
    "public_kb": "查公司通用制度（报销、考勤、社保这类）",
    "dept_kb": "查本部门的业务知识",
    "cross_dept": "查其他部门的资料",
    "data_analysis": "做数据统计和对比分析",
    "operation": "让我代办一件事（请假、报销、订票这类）",
}

# 强指代/追问信号：命中且问句较短时，会话继承优先于规则层
_STRONG_ANAPHORA = re.compile(
    r"(刚才|剛才|上面|上面那个|上一个|之前那个|前者|后者|这个|这样|这个事|那件事|"
    r"^(那|那么|呢|还有吗|再|继续|具体点|详细点|展开|说说|讲讲))"
)
# 弱指代/追问信号：规则层没命中时才用会话继承
_WEAK_ANAPHORA = re.compile(r"^(那|那么|再|还有|另外|它|这个|继续|详细|具体|其他)")
# 纠偏信号：用户明确否定上一轮 → 禁止继承，必须重新分类
_CORRECTION = re.compile(r"(不是|不对|错了|不对你|换一个|换个问题|我问的不是|重新问|搞错了|我是说)")

_SHORT_QUERY_LEN = 25   # 不超过这个长度视为"超短问句"
_MARGIN_MIN = 0.15      # L3 top1 与 top2 的最小分差，低于此值说明判别不清


@dataclass
class IntentResult:
    intent: str = "dept_kb"
    confidence: float = 0.0
    layer: str = "default"          # rule / session / small_model / llm / default
    target_depts: list[str] = field(default_factory=list)
    need_confirm: bool = False      # L3 置信度 0.5~0.8 时追加确认引导
    reason: str = ""
    matched: str = ""               # 命中的规则片段（排障用）
    clarify: str = ""               # 给用户的确认引导文案


def _confirm_text(intent: str) -> str:
    phrase = _INTENT_PHRASE.get(intent, "查本部门知识")
    return f"我先按「{phrase}」来理解，如果理解有误，麻烦您再说具体一点～"


class IntentClassifier:
    def __init__(self, llm_manager=None):
        self.llm = llm_manager

    # ---------------- L1 ----------------
    @staticmethod
    def _layer_rule(query: str) -> IntentResult | None:
        hit = match_rule_detail(query)
        if not hit:
            return None
        intent, conf, note, matched = hit
        r = IntentResult(intent=intent, confidence=conf, layer="rule",
                         reason=note, matched=matched)
        if intent == "cross_dept":
            r.target_depts = extract_target_depts(query)
        return r

    # ---------------- L2 ----------------
    @staticmethod
    def _layer_session(query: str, last_intent: str | None,
                       strong_only: bool = False) -> IntentResult | None:
        """
        会话状态继承：用户在多轮对话里说"那报销呢""再详细说说"，
        上一轮的意图直接继承，省掉一次分类。

        strong_only=True 时只在"强指代/超短问句"下继承（用于抢在规则层之前）；
        否则弱指代也继承（用于规则层未命中的补位）。
        """
        if not last_intent:
            return None
        q = query.strip()
        if _CORRECTION.search(q):          # 用户明确纠偏 → 绝不继承
            return None
        strong = bool(_STRONG_ANAPHORA.search(q)) or len(q) <= 12
        weak = _WEAK_ANAPHORA.match(q) or len(q) <= _SHORT_QUERY_LEN
        if not (strong or (weak and not strong_only)):
            return None
        # 句里明确点名了别的部门 → 说明换了话题，别继承
        if extract_target_depts(q):
            return None
        conf = 0.9 if strong else 0.8
        r = IntentResult(
            intent=last_intent, confidence=conf, layer="session",
            reason=f"继承上一轮意图 {last_intent}（{'强指代' if strong else '短问句'}）",
        )
        if last_intent == "cross_dept":
            r.target_depts = extract_target_depts(q)
        return r

    # ---------------- L3 ----------------
    @staticmethod
    def _layer_small_model(query: str) -> IntentResult | None:
        """
        第三层：小模型分类器。
        优先尝试本地 BERT-tiny ONNX 推理（生产开启 BERT_ENABLED 后生效）；
        离线 / 未配置 / 模型缺失时回退到加权关键词打分（模拟 BERT-tiny 输出分布）。
        """
        bert = bert_classify(query)
        if bert:
            intent, conf = bert
            return IntentResult(
                intent=intent, confidence=round(conf, 3), layer="small_model",
                need_confirm=conf < 0.8,
                clarify=_confirm_text(intent) if conf < 0.8 else "",
                reason="小模型分类（BERT-tiny ONNX 本地推理）",
            )

        # —— 离线回退：关键词打分占位 ——
        scores = {i: 0.0 for i in INTENTS}
        for intent, words in _L3_KEYWORDS.items():
            for w in words:
                if w in query:
                    scores[intent] += 1.0
        total = sum(scores.values())
        if total == 0:
            return None
        for k in scores:
            scores[k] = scores[k] / total
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        best, conf = ranked[0]
        margin = conf - (ranked[1][1] if len(ranked) > 1 else 0.0)
        # Top1 与 Top2 咬得很紧 → 说明判别不清，压低置信度并追加确认
        if margin < _MARGIN_MIN:
            conf *= 0.8
        if conf < 0.5:
            return None
        conf = round(conf, 3)
        return IntentResult(
            intent=best, confidence=conf, layer="small_model",
            need_confirm=conf < 0.8,
            clarify=_confirm_text(best) if conf < 0.8 else "",
            reason="小模型分类（离线回退：关键词打分；生产为 BERT-tiny ONNX）",
        )

    # ---------------- 主入口 ----------------
    async def classify(self, query: str, last_intent: str | None = None,
                       trace_id: str = "-", user=None) -> IntentResult:
        # L0 纯客套话识别：寒暄/感谢/附和绝不进检索，也不被业务规则误吃
        # （如"好哒，谢谢"若先被 public_kb 的"报销/请假"子串规则扫过就会误判）
        if is_chitchat(query):
            r = IntentResult(intent="chitchat", confidence=0.98, layer="rule",
                             reason="纯寒暄/客套话")
            return self._finish(r, query, trace_id, user)

        # ① 会话上下文补偿：强指代 / 超短问句优先走继承，避免硬分类把追问带偏
        r = self._layer_session(query, last_intent, strong_only=True)
        if r:
            return self._finish(r, query, trace_id, user)

        # ② L1 硬规则
        r = self._layer_rule(query)
        if r:
            return self._finish(r, query, trace_id, user)

        # ③ L2 补位：弱指代 / 短问句
        r = self._layer_session(query, last_intent)
        if r:
            return self._finish(r, query, trace_id, user)

        # ④ L3 小模型分类器
        r = self._layer_small_model(query)
        if r:
            return self._finish(r, query, trace_id, user)

        # ---------------- ⑤ L4 LLM 兜底 ----------------
        r = await self._layer_llm(query)
        return self._finish(r, query, trace_id, user)

    # ---------------- 收口：埋点 + 判定留痕 ----------------
    @staticmethod
    def _finish(r: IntentResult, query: str, trace_id: str, user) -> IntentResult:
        INTENT_COUNT.labels(intent=r.intent).inc()
        log.info("intent.hit", trace_id=trace_id, layer=r.layer, intent=r.intent,
                 conf=r.confidence, need_confirm=r.need_confirm, matched=r.matched)
        intent_log.record(
            trace_id=trace_id, query=query, intent=r.intent, layer=r.layer,
            confidence=r.confidence, need_confirm=r.need_confirm, reason=r.reason,
            user_id=getattr(user, "user_id", "") or "",
            dept_id=getattr(user, "dept_id", "") or "",
            extra={"matched": r.matched, "target_depts": r.target_depts},
        )
        return r

    async def _layer_llm(self, query: str) -> IntentResult:
        if self.llm is None:
            return IntentResult(intent="dept_kb", confidence=0.4, layer="default",
                                reason="无 LLM，兜底本部门知识库")
        prompt = (
            "你是企业知识中台的意图分类器。只输出下列标签之一，不要解释：\n"
            "chitchat / public_kb / dept_kb / cross_dept / data_analysis / operation\n\n"
            "判断标准：\n"
            "- chitchat 仅限：打招呼寒暄、夸奖吐槽、问候、闲聊调侃（如\"你好\"\"讲个笑话\"），\n"
            "  以及生活娱乐/时效话题（演唱会、电影、赛事、旅游、天气、新闻热点等）——\n"
            "  这类问题公司知识库里不可能有答案。\n"
            "- 凡涉及业务知识、公司制度、流程、行业知识、具体操作方法、专业概念解释，\n"
            "  一律不是 chitchat：全公司通用制度（报销/考勤/社保等）→ public_kb；\n"
            "  其余业务问题（销售、运营、技术、售后等）→ dept_kb；点名了其他部门 → cross_dept。\n"
            "- 要统计数据/数字对比 → data_analysis；要代办事项（请假/订票等）→ operation。\n\n"
            f"用户问题：{query}\n标签："
        )
        try:
            from ..llm.base import LLMMessage

            out = await self.llm.generate([LLMMessage(role="user", content=prompt)], max_tokens=16)
            for tag in INTENTS:
                if tag in out.lower():
                    r = IntentResult(intent=tag, confidence=0.7, layer="llm",
                                     reason="LLM 兜底分类")
                    # —— 安全降级：越权/误操作的代价太高，必须有硬证据才采信 ——
                    if tag == "cross_dept":
                        r.target_depts = extract_target_depts(query)
                        if not r.target_depts:
                            return IntentResult(
                                intent="dept_kb", confidence=0.5, layer="default",
                                reason="LLM 判跨部门但问题未点名部门 → 安全降级到本部门",
                                clarify=_confirm_text("dept_kb"),
                                need_confirm=True,
                            )
                    elif tag == "operation" and not has_action_marker(query):
                        return IntentResult(
                            intent="dept_kb", confidence=0.5, layer="default",
                            reason="LLM 判操作型但句中无动作词 → 安全降级到本部门",
                            clarify=_confirm_text("dept_kb"),
                            need_confirm=True,
                        )
                    return r
        except Exception as e:  # noqa: BLE001
            log.warning("intent.llm_failed", error=str(e))
        # 仍不确定 → 默认最安全路径：本部门查询（绝不误触跨部门/操作型）
        return IntentResult(intent="dept_kb", confidence=0.4, layer="default",
                            reason="无法确定，兜底本部门知识库")
