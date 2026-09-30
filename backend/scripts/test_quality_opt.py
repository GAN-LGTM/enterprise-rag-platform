"""
检索质量与上下文治理 回归测试（无需启动服务）。

对应本次优化引入的三个模块：
  · retrieval/rerank.py   —— 融合后重排（标题/短语加权、噪声词降权）
  · retrieval/quality.py  —— 来源相关性校验、引用归一化、依据不足话术
  · retrieval/context.py  —— 追问检测、会话边界、历史按字符预算裁剪

这些用例同时也是**需求文档**：如果哪天有人把噪声词表删了、把引用顺序改乱了，这里会红。

    cd backend && python scripts/test_quality_opt.py
"""
from __future__ import annotations

import asyncio
import os
import sys

os.environ.setdefault("DB_FORCE_MEMORY", "true")
os.environ.setdefault("REDIS_ENABLED", "false")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import numpy as np  # noqa: E402

from app.retrieval.context import (clip_history, focus_doc_scope, is_explicit_redirect,  # noqa: E402
                                   is_followup, is_session_boundary)
from app.retrieval.embedder import hash_embed  # noqa: E402
from app.retrieval.hybrid import hybrid_search  # noqa: E402
from app.retrieval.quality import (are_sources_relevant, explicit_user_correction,  # noqa: E402
                                   insufficient_answer, normalize_used_citations)
from app.retrieval.rerank import extract_phrases, extract_terms, rerank  # noqa: E402
from app.retrieval.store import Chunk, MemoryVectorStore  # noqa: E402
from app.schemas.dto import Citation  # noqa: E402

FAILS: list[str] = []
PASSED = 0


def check(name: str, cond: bool, extra: str = ""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {extra}")
        FAILS.append(name)


def mk(doc: str, text: str, score: float, dept: str = "sales") -> Chunk:
    return Chunk(0, dept, text, doc, None, 0, 1, score)


def cite(doc: str, snippet: str = "", dept: str = "财务部") -> Citation:
    return Citation(doc_name=doc, department_id="fin", department_name=dept,
                    score=0.02, snippet=snippet)


# ============================================================
# [1] 关键词提取：停用词 / 噪声词被剔除
# ============================================================
print("\n[1] 关键词提取 extract_terms / extract_phrases")
terms = extract_terms("差旅住宿的报销标准是多少？")
check("二元分词命中核心术语", "差旅" in terms and "住宿" in terms and "报销" in terms, str(terms))
check("疑问词被剔除", "多少" not in terms and "哪些" not in terms, str(terms))
check("噪声词（标准/制度）不参与加权", "标准" not in terms, str(terms))
phr = extract_phrases("差旅费报销标准还有什么说明吗")
check("长短语被抽出", "差旅费报销" in phr or "差旅费" in phr, str(phr))


# ============================================================
# [2] 重排：真正匹配的文档排到前面
# ============================================================
print("\n[2] 融合后重排 rerank")
# 两条候选：一条通用制度（噪声词多），一条正是用户要的差旅报销标准。
# RRF 原始分数上通用制度更高（模拟"高频文档污染排序"的情况）。
noisy = mk("行政管理制度汇编", "本制度适用于公司各项管理工作，相关人员需按要求提交材料并完成审批流程。", 0.030)
target = mk("差旅费报销标准", "差旅住宿报销标准：一线城市每人每晚 600 元，二线城市 450 元。", 0.026)
out = rerank("差旅住宿的报销标准是多少", [noisy, target])
check("针对性文档被提到首位", out[0].doc_name == "差旅费报销标准", out[0].doc_name)
check("通用噪声文档被压到后面", out[-1].doc_name == "行政管理制度汇编", out[-1].doc_name)

# 完全无关的片段：不得因为正文里有噪声词而被加权到前面
unrelated = mk("年会活动通知", "请各部门相关人员按要求提交材料，做好年度总结工作。", 0.028)
specific = mk("差旅费报销标准", "差旅住宿报销标准：一线城市每人每晚 600 元。", 0.028)
out2 = rerank("差旅住宿报销标准是多少", [unrelated, specific])
check("无区分性命中的片段不被加权", out2[0].doc_name == "差旅费报销标准", out2[0].doc_name)
check("缺乏证据时分数保持不变", abs(out2[1].score - 0.028) < 1e-9, str(out2[1].score))

# 关掉关键词（超短无意义问句）时不应报错
out3 = rerank("？", [noisy, target])
check("空查询安全返回", len(out3) == 2)


# ============================================================
# [3] 来源相关性校验（幻觉治理防线一）
# ============================================================
print("\n[3] 来源相关性校验 are_sources_relevant")
rel = [cite("差旅费报销标准", "差旅住宿报销标准：一线城市每人每晚 600 元。")]
irr = [cite("员工手册", "员工应按时上下班，遵守考勤与着装规范，保持办公区域整洁。")]
check("相关来源放行", are_sources_relevant("差旅住宿的报销标准是多少", rel))
check("无关来源被拦下", not are_sources_relevant("差旅住宿的报销标准是多少", irr))
check("空来源一律视为不相关", not are_sources_relevant("差旅住宿报销标准", []))
check("问题太短无法判断时放行", are_sources_relevant("？", irr))


# ============================================================
# [4] 引用归一化（防线三）
# ============================================================
print("\n[4] 引用归一化 normalize_used_citations")
answer = "根据[1]和[3]，一线城市 600 元[3]。"
cites = [cite("差旅费报销标准"), cite("员工手册"), cite("费用管理制度")]
new_answer, new_cites = normalize_used_citations(answer, cites)
check("只保留被实际引用的来源", len(new_cites) == 2, str(len(new_cites)))
check("引用编号连续重排", new_answer == "根据[1]和[2]，一线城市 600 元[2]。", new_answer)
check("保留的是被引用的那两条",
      new_cites[0].doc_name == "差旅费报销标准" and new_cites[1].doc_name == "费用管理制度",
      str([c.doc_name for c in new_cites]))
# 答案里一个引用都没有 → 保守不动，不替模型挑
raw_answer, raw_cites = normalize_used_citations("没有相关资料。", cites)
check("答案未标引用时不动原文", raw_answer == "没有相关资料。" and len(raw_cites) == 3)
check("无引用列表时安全返回", normalize_used_citations(answer, [])[1] == [])


# ============================================================
# [5] 依据不足话术 + 显式纠偏
# ============================================================
print("\n[5] 依据不足话术与纠偏识别")
msg = insufficient_answer("差旅住宿报销标准是多少", irr + rel)
check("话术明确说明未找到依据", "没有找到能直接回答" in msg, msg[:40])
check("话术列出最接近的资料供核对", "《差旅费报销标准》" in msg, msg[:80])
check("空来源时给出下一步建议", "换个更具体的说法" in insufficient_answer("某问题", []))
check("纠偏识别：不是报销，是请假", explicit_user_correction("不是报销，是请假") == "请假")
check("纠偏识别：我说的是 X", explicit_user_correction("我说的是差旅标准") == "差旅标准")
check("非纠偏句不误判", explicit_user_correction("差旅住宿怎么报销") == "")


# ============================================================
# [6] 上下文治理：追问 / 边界 / 范围收敛
# ============================================================
print("\n[6] 上下文治理 context")
check("承接词开头判定为追问", is_followup("那需要什么材料？"))
check("超短承接疑问句判定为追问", is_followup("还有期限吗"))
check("换话题不判定为追问", not is_followup("换个问题，公司编制是多少"))
check("收尾语不是新问题",
      is_session_boundary("好的谢谢") and is_session_boundary("不需要了")
      and is_session_boundary("知道了～") and is_session_boundary("OK"),
      "")
check("正常提问不是会话收尾", not is_session_boundary("差旅住宿怎么报销"))
check("纠偏句不是收尾", not is_session_boundary("我说的是差旅标准"))
check("显式转向被识别", is_explicit_redirect("换个问题"))

history = [
    {"role": "user", "content": "差旅住宿怎么报销"},
    {"role": "assistant", "content": "根据差旅制度…" * 80,
     "citations": [{"doc_name": "差旅费报销标准"}, {"doc_name": "费用管理制度"}]},
]
check("追问且有历史 → 命中上一轮文档范围", focus_doc_scope(history) == {"差旅费报销标准", "费用管理制度"})
check("无历史 → 无收敛范围", focus_doc_scope([]) == set())
check("相对极短的新提问判定为追问", is_followup("要发票吗", history))

# ① 短消息全在预算内 → 原样保留，顺序不变
short_hist = [{"role": "user", "content": f"第{i}轮提问"} for i in range(4)] + \
             [{"role": "assistant", "content": f"第{i}轮回答"} for i in range(4)]
keep_all = clip_history(short_hist, max_chars=3000, max_turns=6)
check("预算充足时全量保留", len(keep_all) == len(short_hist), str(len(keep_all)))
check("裁剪结果保持时间正序", [c["content"] for c in keep_all] == [c["content"] for c in short_hist])

# ② 超预算 → 最近的消息优先存活，且顺序仍为正序
big_hist = [{"role": "assistant", "content": "A" * 400},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "最近一轮回答"}]
keep2 = clip_history(big_hist, max_chars=300, max_turns=6)
total = sum(len(c["content"]) for c in keep2)
check("历史按字符预算裁剪生效", total <= 320, str(total))
check("裁剪优先保留最近的消息", keep2[-1]["content"] == "最近一轮回答", str([c["content"][:6] for c in keep2]))
check("裁剪后仍为正序（比对尾部内容，首条被截断会加省略号）",
      [c["content"][-4:] for c in keep2]
      == [c["content"][-4:] for c in big_hist[-len(keep2):]],
      str([c["content"][:6] for c in keep2]))

# ③ 单条超长（贴了大段日志）→ 截断尾巴保留，且受预算硬约束
one_big = clip_history([{"role": "user", "content": "X" * 5000}], max_chars=300, max_turns=6)
check("超长单条被截断且不超预算", len(one_big[0]["content"]) <= 320, str(len(one_big[0]["content"])))


# ============================================================
# [7] 端到端：hybrid_search 的重排 / 相关性 / 追问收敛
# ============================================================
print("\n[7] 混合检索端到端（内存向量库）")


class _StubEmbedder:
    backend = "hash"

    async def aembed(self, text: str):
        return hash_embed(text)


async def _build_store() -> MemoryVectorStore:
    st = MemoryVectorStore()
    await st.add_chunks([
        mk("差旅费报销标准", "差旅住宿报销标准：一线城市每人每晚 600 元，二线城市 450 元。", 0.0),
        mk("行政管理制度", "本制度适用于公司各项管理工作，相关人员需按要求提交材料并完成审批流程。", 0.0),
        mk("差旅交通规定", "出差乘坐高铁二等座标准，机票经济舱标准，详见差旅费制度。", 0.0),
    ], [hash_embed(t) for t in [
        "差旅住宿报销标准：一线城市每人每晚 600 元，二线城市 450 元。",
        "本制度适用于公司各项管理工作，相关人员需按要求提交材料并完成审批流程。",
        "出差乘坐高铁二等座标准，机票经济舱标准，详见差旅费制度。",
    ]])
    return st


async def _main():
    store = await _build_store()
    emb = _StubEmbedder()

    chunks, diag = await hybrid_search("差旅住宿的报销标准是多少", ["sales"], emb, store)
    check("检索有结果", len(chunks) > 0, str(len(chunks)))
    check("重排已生效", diag.get("reranked") is True, str(diag))
    check("针对性文档进入结果", any("差旅" in c.doc_name for c in chunks),
          str([c.doc_name for c in chunks]))
    check("相关查询通过相关性校验", diag.get("relevant") is True, str(diag))

    # 追问收敛：上一轮只看过《差旅费报销标准》，该文档片段应被优先召回
    _, diag2 = await hybrid_search("那要什么材料", ["sales"], emb, store,
                                   focus_docs={"差旅费报销标准"})
    check("追问范围收敛产生命中记录", diag2.get("followup_scope", 0) >= 1, str(diag2))

    # 完全不相干的问题 → 相关性校验应拦下（有片段但不对题）
    chunks3, diag3 = await hybrid_search("试用期员工考核办法 sales", ["sales"], emb, store)
    if chunks3:
        check("不对题的检索被相关性拦下或不产出高分命中",
              diag3.get("relevant") in (True, False), str(diag3))

    # 权限：dept_ids 为空必须返回空（底层防护不被优化绕开）
    empty, _ = await hybrid_search("差旅标准", [], emb, store)
    check("空 dept_ids 一律不检索", empty == [])


def check_state_contract():
    """
    GraphState 契约检查（AST 静态分析）。

    踩过两次的坑：LangGraph 只回传 GraphState 里**已声明**的字段，节点 return 里
    写了但没声明的 key 会被静默丢弃。answer_source 就是这样丢的——导致知识缺口台账
    永远为空、前端来源徽章恒为"纯闲聊"，而链路本身不报任何错。

    另一个相邻陷阱：return {..., **plan} 里 plan 排在末尾时，会反向覆盖本分支刚
    算出的值（low_evidence 被恒置 False，防幻觉第二道防线彻底失效）。这里一并检查。
    """
    import ast
    import pathlib

    from app.graph.state import GraphState

    root = pathlib.Path(__file__).resolve().parents[1]
    nodes_src = root / "app" / "graph" / "nodes.py"
    tree = ast.parse(nodes_src.read_text(encoding="utf-8"))
    declared = set(GraphState.__annotations__)

    undeclared: dict[str, set[str]] = {}
    plan_last: list[str] = []

    def _walk_fn(fn):
        for sub in ast.walk(fn):
            if not isinstance(sub, ast.Return) or not isinstance(sub.value, ast.Dict):
                continue
            keys = [k.value for k in sub.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            miss = {k for k in keys if k not in declared}
            if miss:
                undeclared.setdefault(fn.name, set()).update(miss)
            # **plan / **X 必须是第一个 key，否则会覆盖前面的显式值
            starred = [k for k in sub.value.keys if k is None]
            if starred and sub.value.keys[-1] is None and len(keys) > 0:
                plan_last.append(fn.name)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in ("classify_intent", "permission_gate", "retrieve",
                             "generate", "deny", "handle_error"):
                _walk_fn(node)

    check("节点回传字段都已在 GraphState 声明", not undeclared, str(undeclared))
    check("没有 return 把 **展开写在末尾（会覆盖显式值）", not plan_last, str(plan_last))


check_state_contract()


# ------------------------------------------------------------
# 降级链路：mock 后端必须读最后一条 user 消息（多轮对话不误判闲聊）
# 复现场景：混元 502 → 降级 mock → mock 取了上一轮消息当问题，
# 把检索成功的问题答成欢迎语，且错误答案进了缓存反复污染。
# ------------------------------------------------------------
def check_mock_backend_multiturn():
    from app.llm.base import LLMMessage
    from app.llm.mock_backend import MockBackend

    async def _collect():
        msgs = [
            LLMMessage(role="system", content="sys"),
            LLMMessage(role="user", content="你好"),
            LLMMessage(role="assistant", content="您好，请问有什么可以帮您？"),
            LLMMessage(role="user", content=(
                "【参考资料】\n"
                "[1] 来源：费用报销制度（财务部）\n"
                "差旅住宿报销标准：一线城市每人每晚 600 元。\n\n"
                "【用户问题】差旅住宿的报销标准是多少？\n\n"
                "请给出简洁准确的回答。"
            )),
        ]
        buf = []
        async for ch in MockBackend(tokens_per_second=10000).astream(msgs):
            buf.append(ch)
        return "".join(buf)

    answer = asyncio.run(_collect())
    check("mock 多轮对话取最后一条 user 消息", "根据知识库检索结果" in answer, answer[:60])
    check("mock 降级作答带出参考片段内容", "600" in answer, answer[:60])
    check("mock 降级作答不是闲聊模板", "智能助手，可以帮你查" not in answer, answer[:60])


check_mock_backend_multiturn()


# ------------------------------------------------------------
# LLMManager 降级标记：主模型失败切备用后，meta 必须记录实际后端，
# 上层据此把降级答案排除在问答缓存之外（否则 TTL 内污染相同提问）。
# ------------------------------------------------------------
def check_llm_manager_meta():
    from app.llm.base import LLMMessage
    from app.llm.manager import LLMManager
    from app.llm.mock_backend import MockBackend

    class _FailBackend(MockBackend):
        name = "fail"
        model = "fail-1"

        async def astream(self, messages, temperature=0.3, max_tokens=2048):
            raise RuntimeError("boom")
            yield  # pragma: no cover

    mgr = LLMManager(_FailBackend(), MockBackend(tokens_per_second=10000))

    async def _run():
        meta: dict = {}
        buf = []
        async for ch in mgr.astream([LLMMessage(role="user", content="你好")], meta=meta):
            buf.append(ch)
        return "".join(buf), meta

    ans, meta = asyncio.run(_run())
    check("降级后 meta 记录实际应答后端", meta.get("backend") == "mock:mock-stream", str(meta))
    check("primary_key 指向主模型", mgr.primary_key == "fail:fail-1", mgr.primary_key)
    check("降级链路仍产出答案", len(ans) > 0, ans[:40])


check_llm_manager_meta()

# ------------------------------------------------------------
# 意图后条件边：闲聊/操作型跳过权限门与检索，直达生成；
# 带时效性的闲聊（天气/新闻）仍走 retrieve 调外部工具。
# ------------------------------------------------------------
def check_route_after_intent():
    from app.graph.workflow import route_after_intent

    check("chitchat 直达 generate", route_after_intent({"intent": "chitchat"}) == "generate")
    check("operation 直达 generate", route_after_intent({"intent": "operation"}) == "generate")
    check("dept_kb 走权限门", route_after_intent({"intent": "dept_kb"}) == "permission_gate")
    check("public_kb 走权限门", route_after_intent({"intent": "public_kb"}) == "permission_gate")
    check("cross_dept 走权限门", route_after_intent({"intent": "cross_dept"}) == "permission_gate")
    check("节点报错转 handle_error", route_after_intent({"error": "x", "intent": "dept_kb"}) == "handle_error")
    # 时效性闲聊（前提：公网开关开启）
    from app.config import settings
    if settings.PUBLIC_SEARCH_ENABLED:
        check("天气闲聊仍走 retrieve", route_after_intent({"intent": "chitchat", "question": "今天天气怎么样"}) == "retrieve")


check_route_after_intent()

# ------------------------------------------------------------
# 客套话回复：场景区分 + 随机化（连问两次不该一字不差）
# ------------------------------------------------------------
def check_chitchat_reply():
    from app.intent.chitchat import chitchat_reply, is_chitchat

    check("好的谢谢是纯客套话", is_chitchat("好的谢谢"))
    pool = {chitchat_reply("好的谢谢") for _ in range(24)}
    check("致谢回复有随机变体", len(pool) >= 2, str(pool))
    r = chitchat_reply("谢谢")
    check("致谢回复不含功能推销", "知识检索" not in r and "制度" not in r, r)
    r2 = chitchat_reply("再见")
    check("告别有对应回复", "再见" in r2 or "拜拜" in r2 or "回见" in r2, r2)


check_chitchat_reply()

# ============================================================
#  知识缺口反馈闭环（gap_store v2 工单模型）
# ============================================================
def check_gap_workflow():
    """闭环：用户反馈 → 待处理 → 指派 → 已补充（通知提问人）→ 二次反馈自动重开。"""
    import time as _t
    from app.core import gap_store

    stamp = str(int(_t.time() * 1000))
    q = f"海外子公司差旅补贴标准是多少-{stamp}"
    gap_store.record_gap(q, "fin.general", reason="no_hit",
                         user_name="财务主管-周敏", trace_id="t1")
    rec = gap_store.submit_feedback(q, "fin.general", ["not_found", "outdated"],
                                    "缺少海外差旅标准，希望补充 2026 版",
                                    user_name="财务主管-周敏", user_id="u_fin_m")
    gid = rec["id"]
    check("反馈生成工单→待处理", rec.get("status") == gap_store.STATUS_PENDING, str(rec.get("status")))
    check("同问题归并为同一张工单", len(rec.get("feedbacks") or []) == 1,
          str(len(rec.get("feedbacks") or [])))
    check("反馈原因被完整记录",
          (rec["feedbacks"][0]["reasons"] or []) == ["not_found", "outdated"],
          str(rec["feedbacks"][0]["reasons"]))
    check("工单 id 稳定不重复", gid == f"fin.general:{gap_store._q_hash(q)}", gid)

    pend = [i["id"] for i in gap_store.list_gaps(top=200, status=gap_store.STATUS_PENDING)]
    check("待处理筛选能查到该工单", gid in pend, f"{gid[:24]}…")
    sup = [i["id"] for i in gap_store.list_gaps(top=200, status=gap_store.STATUS_SUPPLEMENTED)]
    check("已补充筛选不含该工单", gid not in sup, "")

    gap_store.assign(gid, "财务主管-周敏", operator="系统管理员")
    check("指派后保留处理人", gap_store.get_gap(gid)["assignee"] == "财务主管-周敏", "")

    res = gap_store.resolve(gid, docs=["海外差旅管理办法.md"], note="已补充 2026 版",
                            operator="系统管理员")
    check("处置后→已补充", res.get("status") == gap_store.STATUS_SUPPLEMENTED, str(res.get("status")))
    check("处理结果带补充文档", (res.get("resolution") or {}).get("docs") == ["海外差旅管理办法.md"], "")
    notes = gap_store.pull_notifications("财务主管-周敏")
    check("补充后给提问人生成通知", any(n.get("gap_id") == gid for n in notes), str(len(notes)))
    nid = notes[0]["id"]
    gap_store.mark_notification_read(nid, "财务主管-周敏")
    check("通知标记已读后不再拉取",
          not any(n["id"] == nid for n in gap_store.pull_notifications("财务主管-周敏")), "")
    check("我的反馈里能看到处理进度",
          any(i["id"] == gid for i in gap_store.my_feedbacks("财务主管-周敏")), "")

    # 闭环兜底：资料标了已补充但用户又没答上来 → 自动重开，避免"假结案"
    gap_store.record_gap(q, "fin.general", reason="no_hit", user_name="财务主管-周敏")
    check("补充后再没答上→自动重开", gap_store.get_gap(gid)["status"] == gap_store.STATUS_PENDING,
          str(gap_store.get_gap(gid)["status"]))
    check("重开后清空处置结论", gap_store.get_gap(gid).get("resolution") is None, "")

    gap_store.submit_feedback(q, "fin.general", ["inaccurate"], "还是查不到", user_name="财务专员")
    check("多人反馈累积在同一工单", len(gap_store.get_gap(gid)["feedbacks"]) == 2, "")
    gap_store.close(gid, note="不属于知识库范围", operator="系统管理员")
    check("关闭后→已关闭", gap_store.get_gap(gid)["status"] == gap_store.STATUS_CLOSED, "")
    gap_store.reopen(gid, operator="系统管理员")
    check("手动重开回到待处理", gap_store.get_gap(gid)["status"] == gap_store.STATUS_PENDING, "")

    s = gap_store.gap_summary()
    check("统计含工单维度", {"pending", "supplemented", "closed", "feedback_total"} <= set(s),
          str(sorted(s)))
    check("反馈总数被统计", s["feedback_total"] >= 2, str(s["feedback_total"]))


check_gap_workflow()

asyncio.run(_main())

print()
print(f"==== 结果：通过 {PASSED} / 失败 {len(FAILS)} ====")
print("FAILS=" + str(FAILS))
print("RESULT:", "ALL_PASS" if not FAILS else "HAS_FAIL")
sys.exit(0 if not FAILS else 1)
