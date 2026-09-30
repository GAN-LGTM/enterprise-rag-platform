"""
意图判定留痕（FR-CHAT-02 的"持续迭代"基础设施）。

每一次意图判定都落一行 JSONL，字段含 trace_id / 判定层 / 意图 / 置信度 / 命中原因。
用途：

  1. 规则层补词：定期看哪些 query 走了 L3/L4 或置信度偏低，把高频词补进 L1 规则，
     让最便宜的第一层覆盖率越来越高（scripts/intent_report.py）。
  2. 小模型微调：把线上分错/低置信样本导出为待标注集，人工标注后微调 BERT-tiny
     （`python scripts/intent_report.py --export-finetune samples.jsonl`）。
  3. 事故复盘：用 trace_id 把"判定 → 权限 → 检索 → 生成"整条链路串起来。

存储为 JSONL（追加写，不做全量重写），默认 backend/data/intent_log.jsonl，
可用 APP_DATA_DIR 环境变量覆盖；超过 _MAX_BYTES 自动滚动保留一份 .1 备份。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

_lock = threading.Lock()
_MAX_BYTES = 20 * 1024 * 1024  # 20MB 后滚动，避免长期运行把磁盘写满


def _dir() -> Path:
    return Path(os.environ.get("APP_DATA_DIR") or (Path(__file__).resolve().parents[2] / "data"))


def _path() -> Path:
    return _dir() / "intent_log.jsonl"


def _rotate_if_needed(p: Path) -> None:
    try:
        if p.exists() and p.stat().st_size > _MAX_BYTES:
            backup = p.with_suffix(".jsonl.1")
            if backup.exists():
                backup.unlink()
            p.replace(backup)
    except OSError:
        pass


def record(
    trace_id: str,
    query: str,
    intent: str,
    layer: str,
    confidence: float,
    need_confirm: bool = False,
    reason: str = "",
    user_id: str = "",
    dept_id: str = "",
    session_id: str = "",
    extra: dict | None = None,
) -> None:
    """追加一条判定记录；任何异常都不允许影响主流程。"""
    entry = {
        "ts": round(time.time(), 3),
        "trace_id": trace_id or "-",
        "user_id": user_id or "",
        "dept_id": dept_id or "",
        "session_id": session_id or "",
        "query": (query or "")[:500],
        "intent": intent,
        "layer": layer,
        "confidence": round(float(confidence or 0.0), 3),
        "need_confirm": bool(need_confirm),
        "reason": reason or "",
    }
    if extra:
        entry.update(extra)
    try:
        with _lock:
            d = _dir()
            d.mkdir(parents=True, exist_ok=True)
            p = _path()
            _rotate_if_needed(p)
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _iter_reverse(p: Path):
    """从文件末尾往前读，避免大文件全量载入。"""
    try:
        with p.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def recent(limit: int = 200, layer: str | None = None,
           max_confidence: float | None = None) -> list[dict]:
    """取最近的判定记录（默认按时间倒序）。"""
    out: list[dict] = []
    for e in _iter_reverse(_path()):
        if layer and e.get("layer") != layer:
            continue
        if max_confidence is not None and float(e.get("confidence", 1.0)) > max_confidence:
            continue
        out.append(e)
        if len(out) >= limit:
            break
    return out


def low_confidence(threshold: float = 0.8, limit: int = 200) -> list[dict]:
    """低置信度样本：规则层补词 / 小模型微调的优先候选。"""
    return recent(limit=limit, max_confidence=threshold)


def stats(limit: int = 5000) -> dict:
    """按层与意图统计分布，用于观察 L1 覆盖率是否达标（目标 ≥80%）。"""
    by_layer: dict[str, int] = {}
    by_intent: dict[str, int] = {}
    total = 0
    for e in _iter_reverse(_path()):
        total += 1
        by_layer[e.get("layer", "?")] = by_layer.get(e.get("layer", "?"), 0) + 1
        by_intent[e.get("intent", "?")] = by_intent.get(e.get("intent", "?"), 0) + 1
        if total >= limit:
            break
    return {
        "total": total,
        "by_layer": by_layer,
        "by_intent": by_intent,
        "rule_coverage": round(by_layer.get("rule", 0) / total, 4) if total else 0.0,
    }
