"""
意图判定复盘报告 —— 支撑"规则层持续补词 + 小模型持续微调"两个闭环。

数据源：
  · backend/data/intent_log.jsonl  每次判定的留痕（层 / 意图 / 置信度 / trace_id）
  · backend/data/feedback.json     用户点踩（负反馈 → 疑似分错样本）

用法：
    python scripts/intent_report.py                          # 总览 + 补词建议
    python scripts/intent_report.py --low-conf 0.8 --top 30
    python scripts/intent_report.py --export-finetune samples.jsonl
                                    # 导出待标注集：人工标 intent 后微调 BERT-tiny

建议流程（每月一次或点踩率升高时）：
    1) 看"规则层覆盖率"，低于 80% 说明 L1 词典不够，把"补词建议"里高频且业务成立的词
       加进 backend/app/intent/rules.py；
    2) 看"低置信样本"与"点踩样本"，人工标注正确意图；
    3) 标注结果一半补规则、一半进微调集，用 --export-finetune 导出后训练 BERT-tiny，
       替换 BERT_MODEL_PATH 并把 BERT_ENABLED 打开。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # noqa: E402

from app.core import intent_log                      # noqa: E402
from app.core.feedback_store import list_feedback     # noqa: E402
from app.intent.rules import RULES                    # noqa: E402

_CJK = r"[\u4e00-\u9fa5]"


def _fmt_pct(n: int, total: int) -> str:
    return f"{(n / total * 100):.1f}%" if total else "0%"


def _ngrams(query: str, size: int = 2) -> list[str]:
    import re

    segs = re.findall(_CJK + r"+", query)
    out: list[str] = []
    for seg in segs:
        for i in range(len(seg) - size + 1):
            out.append(seg[i:i + size])
    return out


def _already_in_rules(token: str) -> bool:
    return any(token in r.pattern.pattern for r in RULES)


def main() -> int:
    ap = argparse.ArgumentParser(description="意图判定复盘报告")
    ap.add_argument("--top", type=int, default=20, help="各列表展示条数")
    ap.add_argument("--low-conf", type=float, default=0.8, help="低置信阈值")
    ap.add_argument("--days", type=int, default=30, help="只统计最近 N 天")
    ap.add_argument("--export-finetune", metavar="FILE", help="导出待标注微调集（JSONL）")
    args = ap.parse_args()

    cutoff = time.time() - args.days * 86400
    all_items = [e for e in intent_log.recent(limit=20000) if e.get("ts", 0) >= cutoff]
    total = len(all_items)
    if total == 0:
        print("暂无判定记录（data/intent_log.jsonl 为空）。先跑一轮问答再复盘。")
        return 0

    print("=" * 68)
    print(f"  意图判定复盘报告   样本 {total} 条 / 最近 {args.days} 天")
    print("=" * 68)

    # ---------- 1. 分层覆盖 ----------
    by_layer = Counter(e.get("layer", "?") for e in all_items)
    print("\n【1】分层覆盖（目标：rule ≥ 80%，即 80% 的请求 0ms 零成本解决）")
    for layer, n in by_layer.most_common():
        print(f"  {layer:<12} {n:>6}  {_fmt_pct(n, total)}")
    rule_pct = by_layer.get("rule", 0) / total * 100
    print(f"  → 规则层覆盖率 {rule_pct:.1f}%  " +
          ("（达标）" if rule_pct >= 80 else "（偏低，优先补词）"))

    # ---------- 2. 意图分布 ----------
    by_intent = Counter(e.get("intent", "?") for e in all_items)
    print("\n【2】意图分布")
    for intent, n in by_intent.most_common():
        print(f"  {intent:<14} {n:>6}  {_fmt_pct(n, total)}")

    # ---------- 3. 低置信样本（补规则 / 微调的优先候选）----------
    low = [e for e in all_items if float(e.get("confidence", 1.0)) < args.low_conf]
    low.sort(key=lambda e: float(e.get("confidence", 1.0)))
    print(f"\n【3】低置信样本（< {args.low_conf}），共 {len(low)} 条 —— 规则补词/微调首选")
    for e in low[: args.top]:
        print(f"  [{e.get('confidence')}] {e.get('layer'):<11} {e.get('intent'):<13} {e.get('query', '')[:38]}")

    # ---------- 4. 点踩样本（疑似判错）----------
    dislikes = list_feedback(limit=500, feedback_type="dislike")
    latest_by_q = {}
    for e in all_items:
        latest_by_q.setdefault(e.get("query"), e)
    miss: list[dict] = []
    for fb in dislikes:
        q = (fb.get("question") or "").strip()
        hit = latest_by_q.get(q)
        if hit:
            miss.append({"query": q, "predicted": hit.get("intent"),
                         "layer": hit.get("layer"), "confidence": hit.get("confidence"),
                         "trace_id": hit.get("trace_id")})
    print(f"\n【4】点踩样本关联判定（疑似判错，共 {len(miss)} 条）—— 必须人工标注")
    for m in miss[: args.top]:
        print(f"  {m['predicted']:<13} conf={m['confidence']}  {m['query'][:40]}  "
              f"trace={m['trace_id']}")

    # ---------- 5. 补词建议 ----------
    pool = [e for e in all_items
            if float(e.get("confidence", 1.0)) < args.low_conf or e.get("layer") in ("llm", "default")]
    freq: Counter[str] = Counter()
    for e in pool:
        for ng in set(_ngrams(e.get("query", ""))):
            freq[ng] += 1
    candidates = [(ng, n) for ng, n in freq.most_common()
                  if n >= 2 and not _already_in_rules(ng) and len(ng) == 2]
    print(f"\n【5】规则补词建议（来自 {len(pool)} 条判不准的问句，出现 ≥2 次且当前规则未覆盖）")
    if candidates:
        for ng, n in candidates[: args.top]:
            print(f"  {ng}  ×{n}")
        print("  → 业务上确认后加进 backend/app/intent/rules.py 对应意图的 Rule 里")
    else:
        print("  （暂无，说明现有词典已覆盖主要词汇）")

    # ---------- 6. 导出微调集 ----------
    if args.export_finetune:
        seen: set[str] = set()
        rows: list[dict] = []
        for e in low:
            q = (e.get("query") or "").strip()
            if q and q not in seen:
                seen.add(q)
                rows.append({"query": q, "predicted": e.get("intent"),
                             "layer": e.get("layer"), "confidence": e.get("confidence"),
                             "label": ""})
        for m in miss:
            q = m["query"]
            if q and q not in seen:
                seen.add(q)
                rows.append({"query": q, "predicted": m["predicted"],
                             "layer": m["layer"], "confidence": m["confidence"],
                             "label": ""})
        out = Path(args.export_finetune)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"\n【6】已导出待标注微调集 {len(rows)} 条 → {out}")
        print("  人工填 label（6 类之一）后即可用于 BERT-tiny 微调。")

    print("\n" + "=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
