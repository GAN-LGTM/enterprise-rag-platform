"""pytest 根 conftest：把 backend 目录加入导入路径，并给测试一个安全的默认环境。

所有测试均在「内存库 + 关闭 Redis」下运行，不依赖外部 PostgreSQL / 向量库 / LLM，
保证一键可跑、可重复、可交付。
"""
from __future__ import annotations

import os
import sys

# backend/ 加入 sys.path，使 `import app` 可用
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("DB_FORCE_MEMORY", "true")
os.environ.setdefault("REDIS_ENABLED", "false")
