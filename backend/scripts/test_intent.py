"""
意图识别准确性回归测试（FR-CHAT-02 四层递进 + 三项准确性加固）。

覆盖：
  · L1 规则层覆盖率与优先级（含"劳动合同 vs 合同"这类易错排序）
  · L2 会话上下文补偿（强指代优先继承 / 纠偏禁止继承 / 点名换部门不继承）
  · L3 模糊地带置信度分级（need_confirm + 确认引导文案）
  · L4 兜底安全降级（判 cross_dept / operation 缺硬证据 → 落 dept_kb）
  · 判定留痕（trace_id 可追溯）
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

# 测试隔离：数据目录指向临时目录，避免污染真实 data/
_TMP = tempfile.mkdtemp(prefix="rag_intent_test_")
os.environ["APP_DATA_DIR"] = _TMP
os.environ.setdefault("DB_FORCE_MEMORY", "true")
os.environ.setdefault("REDIS_ENABLED", "false")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # noqa: E402

from app.intent.classifier import IntentClassifier  # noqa: E402
from app.core import intent_log  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = ""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {extra}")
        FAILS.append(name)


class _MockLLM:
    """只返回固定标签的假 LLM，用于验证 L4 兜底与安全降级。"""

    def __init__(self, out: str):
        self.out = out
        self.calls = 0

    async def generate(self, messages, max_tokens: int = 16) -> str:
        self.calls += 1
        return self.out


def run():
    clf = IntentClassifier()          # 无 LLM → L4 直接兜底
    print("\n[1] 规则层（L1）：优先级与易错排序")

    r = asyncio.run(clf.classify("差旅报销的标准是什么", trace_id="t1"))
    check("报销 → public_kb", r.intent == "public_kb" and r.layer == "rule", r.intent)

    r = asyncio.run(clf.classify("劳动合同到期怎么续签", trace_id="t2"))
    check("劳动合同 → public_kb（不被 dept_kb 的'合同'吃掉）",
          r.intent == "public_kb", r.intent)

    r = asyncio.run(clf.classify("客户合同模板在哪下载", trace_id="t3"))
    check("客户合同 → dept_kb", r.intent == "dept_kb", r.intent)

    r = asyncio.run(clf.classify("市场部今年的推广方案", trace_id="t4"))
    check("点名市场部 → cross_dept", r.intent == "cross_dept", r.intent)
    check("cross_dept 抽取目标部门", r.target_depts == ["mkt.market"], str(r.target_depts))

    r = asyncio.run(clf.classify("人力资源部的招聘流程", trace_id="t5"))
    check("点名人力部 → cross_dept(hr)", r.intent == "cross_dept" and r.target_depts == ["hr"],
          f"{r.intent}/{r.target_depts}")

    r = asyncio.run(clf.classify("各区域销售额同比对比", trace_id="t6"))
    check("同比对比 → data_analysis（不被'销售额'判成业务问答）",
          r.intent == "data_analysis", r.intent)

    r = asyncio.run(clf.classify("帮我提交一个请假申请", trace_id="t7"))
    check("显式动作 → operation", r.intent == "operation", r.intent)

    r = asyncio.run(clf.classify("销售话术怎么培训新人", trace_id="t8"))
    check("话术 → dept_kb", r.intent == "dept_kb", r.intent)

    print("\n[2] 会话上下文补偿（L2）")
    # 强指代优先于规则层：上轮在聊跨部门，本轮"那费用呢"应继承 cross_dept，
    # 而不是被 public_kb 的"费用"规则吃掉
    r = asyncio.run(clf.classify("那费用呢", last_intent="cross_dept", trace_id="t9"))
    check("强指代 → 继承上一轮（优先于规则层）", r.intent == "cross_dept" and r.layer == "session",
          f"{r.intent}/{r.layer}")

    r = asyncio.run(clf.classify("不是，我是问销量下滑原因", last_intent="public_kb", trace_id="t10"))
    check("纠偏语句禁止继承", r.intent != "public_kb" and r.layer != "session",
          f"{r.intent}/{r.layer}")

    r = asyncio.run(clf.classify("研发部的部署规范是什么", last_intent="dept_kb", trace_id="t11"))
    check("点名换部门 → 不继承，重新判定", r.intent == "cross_dept", r.intent)

    print("\n[3] 模糊地带置信度分级（L3）")
    r = asyncio.run(clf.classify("我们部门流程指标", trace_id="t12"))
    check("L3 命中", r.layer == "small_model", r.layer)
    check("中低置信 → need_confirm", r.need_confirm is True, str(r.confidence))
    check("生成确认引导文案", bool(r.clarify) and "理解" in r.clarify, r.clarify)
    check("置信度落在 0.5~0.8 区间", 0.5 <= r.confidence < 0.8, str(r.confidence))

    print("\n[4] L4 兜底与安全降级（问句需穿透 L1/L3 才会走到 LLM）")
    clf_op = IntentClassifier(llm_manager=_MockLLM("operation"))
    r = asyncio.run(clf_op.classify("年终述职报告交给谁", trace_id="t13"))
    check("LLM 判 operation 但无动作词 → 降级 dept_kb",
          r.intent == "dept_kb" and r.layer == "default", f"{r.intent}/{r.layer}")
    check("降级同时给出确认引导", r.need_confirm and bool(r.clarify), str(r.need_confirm))

    clf_cross = IntentClassifier(llm_manager=_MockLLM("cross_dept"))
    r = asyncio.run(clf_cross.classify("打印机坏了找谁修", trace_id="t14"))
    check("LLM 判 cross_dept 但未点名部门 → 降级 dept_kb",
          r.intent == "dept_kb" and r.layer == "default", f"{r.intent}/{r.layer}")

    clf_ok = IntentClassifier(llm_manager=_MockLLM("public_kb"))
    r = asyncio.run(clf_ok.classify("打印机卡纸怎么办", trace_id="t15"))
    check("LLM 判 public_kb 正常采纳", r.intent == "public_kb" and r.layer == "llm",
          f"{r.intent}/{r.layer}")

    clf_none = IntentClassifier()      # 无 LLM：兜底必须落在最安全的 dept_kb
    r = asyncio.run(clf_none.classify("打印机卡纸怎么办", trace_id="t16"))
    check("无 LLM 不确定 → 兜底 dept_kb（不误触跨部门/操作型）",
          r.intent == "dept_kb" and r.layer == "default", f"{r.intent}/{r.layer}")

    print("\n[5] 生活娱乐/时效话题：必须闲聊 + 联网搜索，绝不进知识库")
    from app.tools import websearch

    for q in ("最近有什么演唱会吗", "最近有什么电影推荐", "本周末有什么音乐节",
              "周杰伦今年有演唱会吗", "世界杯赛程出来了没", "国庆去哪玩比较好"):
        r = asyncio.run(clf.classify(q, trace_id=f"fun-{q[:6]}"))
        check(f"「{q}」→ chitchat", r.intent == "chitchat" and r.layer == "rule",
              f"{r.intent}/{r.layer}/{r.matched}")

    # 强时效兜底：即使意图误判成 dept_kb，也必须触发联网搜索（硬触发无视意图）
    for q in ("最近有什么演唱会吗", "最近有什么电影上映", "今天有什么比赛"):
        check(f"「{q}」命中强时效联网触发", websearch.is_hard_realtime(q))
        check(f"「{q}」命中搜索触发词", websearch.is_search_query(q))
    # 反例：纯业务问题不该触发联网
    check("「各区域销售额同比对比」不触发联网", not websearch.is_search_query("各区域销售额同比对比"))
    check("「帮我提交请假申请」不触发强时效", not websearch.is_hard_realtime("帮我提交请假申请"))

    print("\n[6] 判定留痕（供规则补词 / 小模型微调）")
    recs = intent_log.recent(limit=40)
    check("判定写入 intent_log", len(recs) >= 10, str(len(recs)))
    check("留痕带 trace_id", any(x.get("trace_id") == "t16" for x in recs))
    st = intent_log.stats()
    check("可按层统计", st.get("total", 0) >= 5 and "by_layer" in st, str(st.get("total")))
    check("低置信可筛出", isinstance(intent_log.low_confidence(0.8, 10), list))


if __name__ == "__main__":
    run()
    print()
    print("FAILS=" + str(FAILS))
    print("RESULT:", "ALL_PASS" if not FAILS else "HAS_FAIL")
    sys.exit(0 if not FAILS else 1)
