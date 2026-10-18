"""知识库语义检索（RAG）测试：假 embedder 验证排序/缓存/维度失效/关键词兜底。"""

import numpy as np
import pytest

from brake_analyzer.llm.kb import KnowledgeBase


@pytest.fixture
def kb(tmp_path):
    mdir = tmp_path / "manual"
    mdir.mkdir()
    (mdir / "abs.md").write_text(
        "# ABS 手册\n\n"
        "### abs.s1 — 横摆稳定性\n\n"
        "判定：窗口内横摆角速度峰值不超过限值，超限与左右轮压差有关。\n\n"
        "### abs.s2 — 制动距离\n\n"
        "判定：全力制动至停车的距离不超过 40 米，与增压响应速度有关。\n",
        encoding="utf-8")
    return KnowledgeBase(str(tmp_path))


def _fake_embed_dim2(texts):
    """判定型假 embedder：含「横摆」→[1,0]，否则 [0,1]。调用计数器外置。"""
    return [[1.0, 0.0] if "横摆" in t else [0.0, 1.0] for t in texts]


def test_semantic_search_ranks_by_vector(kb):
    calls = []
    def embed(texts):
        calls.append(texts)
        return _fake_embed_dim2(texts)

    kb.set_embedder(embed, "fake-model")
    hits = kb.search("横摆稳定 怎么判", top_k=1)
    assert kb.last_search_mode == "semantic"
    assert hits and hits[0]["id"] == "abs.s1"
    # 两次调用：建索引（2 节一批）+ 查询向量化（1 条）
    assert len(calls) == 2 and len(calls[0]) == 2 and len(calls[1]) == 1


def test_vector_cache_reused_across_reload(kb):
    n_calls = []
    def embed(texts):
        n_calls.append(len(texts))
        return _fake_embed_dim2(texts)

    kb.set_embedder(embed, "fake-model")
    kb.search("横摆")                 # call1 建索引 + call2 查询
    kb.reload()                      # 重载后同样的内容不应重新调 embedding
    hits = kb.search("制动距离", top_k=1)   # call3 只有查询
    assert kb.last_search_mode == "semantic"
    assert hits[0]["id"] == "abs.s2"
    assert len(n_calls) == 3         # 索引部分全命中缓存，只多了一次查询向量化
    # 内容变化（模拟改手册）→ hash 变 → 重算
    import os

    with open(os.path.join(kb.kb_dir, "manual", "abs.md"), "w", encoding="utf-8") as f:
        f.write("### abs.s1 — 横摆稳定性\n\n改过的正文。\n")
    kb.reload()
    kb.search("横摆")
    assert len(n_calls) == 5 and n_calls[3] == 1   # call4 只有变了的那节重算，call5 查询


def test_model_change_invalidates_cache(kb):
    def embed(texts):
        return _fake_embed_dim2(texts)

    kb.set_embedder(embed, "model-a")
    kb.search("横摆")
    kb.set_embedder(embed, "model-b")   # 换模型：缓存文件按模型名分开，天然失效
    hits = kb.search("横摆")
    assert hits and kb.last_search_mode == "semantic"


def test_keyword_fallback_without_embedder(kb):
    hits = kb.search("制动距离", top_k=1)
    assert kb.last_search_mode == "keyword"
    assert hits and hits[0]["id"] == "abs.s2"


def test_embed_failure_falls_back(kb):
    def broken_embed(texts):
        return None                    # 端点挂了：不抛异常，退关键词

    kb.set_embedder(broken_embed, "fake-model")
    hits = kb.search("制动距离", top_k=1)
    assert kb.last_search_mode == "keyword"
    assert hits and hits[0]["id"] == "abs.s2"
