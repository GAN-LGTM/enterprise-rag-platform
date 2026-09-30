"""检索调试（轻量版）：内存库 + 种子数据 + hybrid_search，不启动 PG/LLM。"""
import asyncio
import os
import sys
import types

sys.path.insert(0, r"D:/enterprise-rag-platform/backend")
os.chdir(r"D:/enterprise-rag-platform")

from app.retrieval.embedder import get_embedder  # noqa: E402
from app.retrieval.hybrid import hybrid_search  # noqa: E402
from app.retrieval.store import MemoryVectorStore  # noqa: E402
from app.scripts.seed_demo import seed_if_empty  # noqa: E402


async def main():
    store = MemoryVectorStore()
    ns = types.SimpleNamespace(store_mode="memory", store=store, embedder=get_embedder())
    n = await seed_if_empty(ns)
    print("seeded chunks:", n)
    cases = [
        ("微短剧市场规模有多大", ["mkt.market", "public"]),
        ("个人信息保护法是什么时候施行", ["legal", "public"]),
        ("2024年网民规模有多大", ["public", "mkt.market"]),
    ]
    for q, depts in cases:
        print("=" * 60)
        print("Q:", q, depts)
        chunks, diag = await hybrid_search(q, depts, ns.embedder, ns.store)
        for ch in chunks[:8]:
            print(f"  [{ch.score:.4f}] {ch.doc_name} | {ch.chunk_text[:50]}")
        print("  diag:", diag)


if __name__ == "__main__":
    asyncio.run(main())
