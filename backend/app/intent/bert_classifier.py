"""
意图小模型分类器（FR-CHAT-02 第三层）。

生产环境：加载本地 BERT-tiny 导出的 ONNX 模型（onnxruntime 本地推理，不依赖任何外部 API），
对 query 做意图分类，输出 (意图, 置信度)。

离线 / 未配置 / 依赖缺失：本模块自动返回 None，由上层 `_layer_small_model` 回退到关键词打分器，
保证零模型依赖时全链路照常跑通（与需求书"零外部依赖"一致）。

接入真实模型只需两步：
  1) settings.BERT_ENABLED=True 且 BERT_MODEL_PATH 指向导出的 .onnx；
  2) 在 `_encode()` 中按模型导出时的 vocab 完成 tokenize（input_ids / attention_mask）。
"""
from __future__ import annotations

import os

from ..config import settings
from ..logging_setup import get_logger

log = get_logger("intent.bert")

# 避免与 classifier 形成循环导入：默认标签在此内联定义，优先取 settings.BERT_LABELS
_DEFAULT_LABELS = ["chitchat", "public_kb", "dept_kb", "cross_dept", "data_analysis", "operation"]
_LABELS = settings.BERT_LABELS or _DEFAULT_LABELS
_session = None
_initialized = False


def _try_load():
    global _session, _initialized
    if _initialized:
        return _session
    _initialized = True
    if not settings.BERT_ENABLED:
        log.info("bert.disabled", reason="BERT_ENABLED=false")
        return None
    try:
        import onnxruntime as ort  # noqa: PLC0415
    except ImportError:
        log.info("bert.skip", reason="onnxruntime 未安装")
        return None
    path = settings.BERT_MODEL_PATH
    if not os.path.exists(path):
        log.info("bert.skip", reason="模型文件不存在", path=path)
        return None
    try:
        _session = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
        log.info("bert.loaded", path=path)
    except Exception as e:  # noqa: BLE001
        log.warning("bert.load_failed", error=str(e))
        _session = None
    return _session


def is_ready() -> bool:
    return _try_load() is not None


def classify(query: str) -> tuple[str, float] | None:
    """返回 (intent, confidence)；不可用（离线/未配置）时返回 None。"""
    session = _try_load()
    if session is None:
        return None

    # —— 生产接入点：按导出模型的 tokenizer 完成编码 ——
    # 不同导出脚本的 input 名与 vocab 不同，此处保留统一骨架；本地模型就位后在此填充编码逻辑，
    # 例如：inputs = {"input_ids": ..., "attention_mask": ...}
    inputs = _encode(query)
    if inputs is None:
        return None

    try:
        logits = session.run(None, inputs)[0]
        if logits.ndim == 3:
            logits = logits[0]
        logits = logits[0]
        probs = _softmax(logits)
        idx = int(max(range(len(probs)), key=lambda i: probs[i]))
        conf = float(probs[idx])
        if 0 <= idx < len(_LABELS):
            return _LABELS[idx], conf
    except Exception as e:  # noqa: BLE001
        log.warning("bert.infer_failed", error=str(e))
    return None


def _encode(query: str) -> dict | None:
    """占位编码：真实模型接入时按 vocab 实现（input_ids / attention_mask）。

    返回 None 表示当前为离线骨架、暂不执行真实推理，上层将回退到关键词打分器。
    预留此函数仅为让生产接入点集中、可测试。
    """
    return None


def _softmax(x) -> list[float]:
    import numpy as np  # noqa: PLC0415

    z = np.array(x, dtype=float)
    z = z - z.max()
    e = np.exp(z)
    return (e / e.sum()).tolist()
