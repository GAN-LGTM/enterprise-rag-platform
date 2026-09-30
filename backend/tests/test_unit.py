"""纯逻辑单元测试（不启动服务，进程内执行，纳入覆盖率）。

覆盖任务书要求的核心能力中可脱离外部依赖验证的部分：
  - 多格式解析（FR-KB-02）：txt / md / csv 解析 + 不支持类型报错
  - 生产安全基线自检（preflight）：生产必须拦截、dev 只告警
  - 合规审计：时间解析 _to_ts、报送周期 _period_range、CSV 导出、敏感部门判定
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ingest.parser import parse_file
from app.core import preflight
from app.config import Settings
from app.api.compliance import _to_ts, _period_range
from app.core import audit


# ---------------- 多格式解析 ----------------
def test_parser_txt():
    p = tempfile.mktemp(suffix=".txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("hello 世界\n第二行")
    try:
        blocks = parse_file(p, "demo.txt")
        assert blocks and "hello 世界" in blocks[0].text
    finally:
        os.unlink(p)


def test_parser_md():
    p = tempfile.mktemp(suffix=".md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("# 标题\n\n正文内容")
    try:
        blocks = parse_file(p, "demo.md")
        assert blocks and "标题" in blocks[0].text and "正文内容" in blocks[0].text
    finally:
        os.unlink(p)


def test_parser_csv():
    p = tempfile.mktemp(suffix=".csv")
    with open(p, "w", encoding="utf-8") as f:
        f.write("name,age\nalice,30\nbob,25")
    try:
        blocks = parse_file(p, "demo.csv")
        assert blocks and "name" in blocks[0].text and "alice" in blocks[0].text
    finally:
        os.unlink(p)


def test_parser_unsupported_type_raises():
    with pytest.raises(ValueError):
        parse_file("x.xyz", "x.xyz")


# ---------------- 生产安全基线自检 ----------------
def _swap(settings_obj):
    old = preflight.settings
    preflight.settings = settings_obj
    return old


def test_preflight_production_rejects_weak_jwt():
    s = Settings(ENVIRONMENT="production", JWT_SECRET_KEY="short",
                 EMBEDDING_BACKEND="local", DB_FORCE_MEMORY=False,
                 LLM_BACKEND="vllm", ALLOWED_ORIGINS="https://rag.corp.example.com",
                 DEMO_MODE="false")
    old = _swap(s)
    try:
        with pytest.raises(preflight.PreflightError):
            preflight.run(verbose=False)
    finally:
        preflight.settings = old


def test_preflight_production_rejects_demo_mode_and_memory_db():
    s = Settings(ENVIRONMENT="production", JWT_SECRET_KEY="x" * 48,
                 EMBEDDING_BACKEND="hash", DB_FORCE_MEMORY=True,
                 LLM_BACKEND="mock", ALLOWED_ORIGINS="*",
                 DEMO_MODE="true")
    old = _swap(s)
    try:
        with pytest.raises(preflight.PreflightError):
            preflight.run(verbose=False)
    finally:
        preflight.settings = old


def test_preflight_dev_allows_insecure_config():
    # dev 环境：同样的不安全配置只告警、不阻断启动
    s = Settings(ENVIRONMENT="dev", JWT_SECRET_KEY="short",
                 EMBEDDING_BACKEND="hash", DB_FORCE_MEMORY=True,
                 LLM_BACKEND="mock", ALLOWED_ORIGINS="*",
                 DEMO_MODE="true")
    old = _swap(s)
    try:
        preflight.run(verbose=False)  # dev 不应抛 PreflightError
    finally:
        preflight.settings = old


# ---------------- 合规审计 ----------------
def test_compliance_to_ts():
    assert isinstance(_to_ts("2024-01-01 00:00:00"), int)
    assert _to_ts("2024-01-01") is not None
    assert _to_ts("") is None
    assert _to_ts(None) is None
    assert _to_ts("not-a-date") is None
    assert _to_ts(1700000000) == 1700000000


def test_compliance_period_range():
    s, u, label = _period_range("month")
    assert s and u and "月" in label
    s, u, label = _period_range("quarter")
    assert "季度" in label
    with pytest.raises(Exception):
        _period_range("year")  # 只支持 month / quarter


def test_audit_export_csv_empty():
    csv_text = audit.export_csv([])
    assert csv_text.strip() != ""  # 至少包含表头


def test_audit_is_sensitive_dept_returns_bool():
    assert isinstance(audit._is_sensitive_dept("fin.general"), bool)
    # 财务 / 法务 / 人力等通常视为敏感部门
    assert audit._is_sensitive_dept("fin.general") is True
    assert audit._is_sensitive_dept("mkt.sales") is False
