"""
文档版本管理存储（需求书 FR-KB-05）。

每一次文档入库 / 回滚都会登记一条版本记录，保留最近 10 个历史版本：
  · 支持查看历史版本列表
  · 支持回滚至任意历史版本（重新入库该版本对应的文件，生成新的递增版本号）
  · 回滚操作记入审计日志

与项目既有约定一致：内存镜像 + 原子写 JSON（data/versions.json），重启不丢。
生产环境替换为 doc_versions 表（doc_name, version, department_id, file_path,
chunk_count, uploaded_by, uploaded_at, change_note, UNIQUE(doc_name, version)）即可，
函数签名不变。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from ..logging_setup import get_logger

log = get_logger("version")

_DATA_DIR = Path(os.environ.get("APP_DATA_DIR") or (Path(__file__).resolve().parents[2] / "data"))
_STORE_PATH = _DATA_DIR / "versions.json"
_LOCK = threading.RLock()
_MAX_VERSIONS = 10

# (doc_name, department_id) -> {"version": int, "history": [record, ...]}
_STATE: dict[tuple[str, str], dict] = {}
_LOADED = False


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        if _STORE_PATH.exists():
            raw = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    doc, dept = k.split("\u0001", 1)
                    _STATE[(doc, dept)] = v
        log.info("version.loaded", docs=len(_STATE))
    except Exception as e:  # noqa: BLE001
        log.warning("version.load_failed", error=str(e))


def _save() -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _STORE_PATH.with_suffix(".tmp")
        serial = {f"{d}\u0001{dep}": v for (d, dep), v in _STATE.items()}
        tmp.write_text(json.dumps(serial, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _STORE_PATH)
    except Exception as e:  # noqa: BLE001
        log.warning("version.save_failed", error=str(e))


def current_version(doc_name: str, department_id: str) -> int:
    with _LOCK:
        _load()
        return _STATE.get((doc_name, department_id), {}).get("version", 0)


def next_version(doc_name: str, department_id: str) -> int:
    with _LOCK:
        _load()
        key = (doc_name, department_id)
        entry = _STATE.setdefault(key, {"version": 0, "history": []})
        entry["version"] = entry["version"] + 1
        _save()
        return entry["version"]


def record_version(doc_name: str, department_id: str, version: int, chunk_count: int,
                   uploaded_by: str, file_path: str | None, content_hash: str,
                   change_note: str = "") -> None:
    """登记一条版本记录（追加到历史，自动裁剪到最近 10 版）。"""
    with _LOCK:
        _load()
        key = (doc_name, department_id)
        entry = _STATE.setdefault(key, {"version": version, "history": []})
        entry["version"] = max(entry["version"], version)
        entry["history"].append({
            "version": version, "chunk_count": chunk_count,
            "uploaded_by": uploaded_by, "uploaded_at": round(time.time(), 3),
            "file_path": file_path, "content_hash": content_hash,
            "change_note": change_note,
        })
        # 保留最近 _MAX_VERSIONS 个版本
        if len(entry["history"]) > _MAX_VERSIONS:
            entry["history"] = entry["history"][-_MAX_VERSIONS:]
        _save()


def register_seed_version(doc_name: str, department_id: str, chunk_count: int,
                          content_hash: str) -> None:
    """演示种子文档注册 v1（无真实文件路径，回滚时提示历史文件不可用）。"""
    with _LOCK:
        _load()
        key = (doc_name, department_id)
        if key in _STATE and _STATE[key]["version"] >= 1:
            return
        record_version(doc_name, department_id, 1, chunk_count, "system", None,
                      content_hash, change_note="演示种子")


def list_versions(doc_name: str, department_id: str) -> dict:
    with _LOCK:
        _load()
        entry = _STATE.get((doc_name, department_id))
        if not entry:
            return {"doc_name": doc_name, "department_id": department_id,
                    "current_version": 0, "history": []}
        history = list(reversed(entry["history"]))  # 最新在前
        return {"doc_name": doc_name, "department_id": department_id,
                "current_version": entry["version"], "history": history}


def get_version(doc_name: str, department_id: str, version: int) -> dict | None:
    with _LOCK:
        _load()
        entry = _STATE.get((doc_name, department_id))
        if not entry:
            return None
        for rec in entry["history"]:
            if rec["version"] == version:
                return rec
        return None


def delete_versions(doc_name: str, department_id: str) -> None:
    with _LOCK:
        _load()
        _STATE.pop((doc_name, department_id), None)
        _save()


def doc_status(updated_at: float | None) -> str:
    """文档状态判定（供知识目录四色标识使用）。

    - expired   已过期：最后更新距今超过 settings.DOC_EXPIRE_DAYS（红色，提示复核）
    - published 已发布：正常在库（绿色）
    注：「审核中」由上传审核流程产生、「无权访问」由前端按部门权限派生，
    均不在此函数判定范围内。
    """
    from ..config import settings

    if not updated_at or not settings.DOC_EXPIRE_DAYS:
        return "published"
    age_days = (time.time() - updated_at) / 86400
    return "expired" if age_days > settings.DOC_EXPIRE_DAYS else "published"


def list_doc_versions() -> list[dict]:
    """列出所有文档的当前版本（用于知识库文档列表）。

    在既有 doc_name / department_id / version 三个字段基础上**追加**（不修改）：
      updated_at    最后更新时间戳（秒）
      uploaded_by   最后更新人
      chunk_count   分块数
      status        见 doc_status()
    """
    with _LOCK:
        _load()
        out = []
        for (d, dep), v in _STATE.items():
            history = v.get("history") or []
            latest = None
            for rec in history:
                if latest is None or rec.get("version", 0) >= latest.get("version", 0):
                    latest = rec
            updated_at = (latest or {}).get("uploaded_at")
            out.append({
                "doc_name": d,
                "department_id": dep,
                "version": v["version"],
                "updated_at": updated_at,
                "uploaded_by": (latest or {}).get("uploaded_by", ""),
                "chunk_count": (latest or {}).get("chunk_count", 0),
                "status": doc_status(updated_at),
            })
        return out
