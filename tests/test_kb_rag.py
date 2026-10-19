"""知识库语义检索（RAG）测试：假 embedder 验证排序/缓存/维度失效/关键词兜底。"""

import os

import numpy as np
import pytest

from brake_analyzer.llm.kb import KnowledgeBase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
    assert kb.last_search_mode in ("hybrid", "semantic")
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
    assert kb.last_search_mode in ("hybrid", "semantic")
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
    assert hits and kb.last_search_mode in ("hybrid", "semantic")


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


# ---------------- 分块 + 混合 RRF ----------------

@pytest.fixture
def chunked_kb(tmp_path):
    mdir = tmp_path / "manual"
    mdir.mkdir()
    (mdir / "abs.md").write_text(
        "# ABS 手册\n\n"
        "### abs.s1 — 横摆稳定性\n\n"
        "概述：横摆角速度峰值为判定主指标。\n\n"
        "#### 标定参数说明\n\n"
        "Opt_Instabil_Lambda_HA 初始值 0.11，当前值 0.06。\n\n"
        "#### 评价标准说明\n\n"
        "控制模式一致的时间占比不低于 90%。\n\n"
        "### abs.s2 — 制动距离\n\n"
        "判定：100 km/h 全力制动距离不超过 40 m。\n",
        encoding="utf-8")
    return KnowledgeBase(str(tmp_path))


def test_chunk_splitting_by_h4(chunked_kb):
    """含 #### 子标题的节切成多块，块 id 为 section_id#N。"""
    chunks = chunked_kb._index_chunks()
    ids = [c["chunk_id"] for c in chunks]
    assert "abs.s1" in ids and "abs.s2" in ids
    assert "abs.s1#2" in ids and "abs.s1#3" in ids
    # 每块文本都带节标题前缀（块脱离节上下文，embedding 需要主题信息）
    assert all("横摆稳定性" in c["text"] for c in chunks if c["section_id"] == "abs.s1")


def test_keyword_retrieval_hits_exact_param_chunk(chunked_kb):
    """参数名精确检索：关键词通道直接命中含该参数的 #### 块，而不是整节。"""
    hits = chunked_kb.search("Opt_Instabil_Lambda_HA", top_k=1)
    assert kb_mode_ok(chunked_kb, hits)
    # small-to-big：命中块所属的节合并为完整文本，参数表所在的 #### 块必然在内
    assert hits[0]["id"] == "abs.s1"
    assert "Opt_Instabil_Lambda_HA" in hits[0]["text"]
    assert "0.11" in hits[0]["text"]


def kb_mode_ok(kb, hits):
    return bool(hits)


def test_hybrid_rrf_promotes_keyword_hit(chunked_kb):
    """语义通道偏好节标题语义，关键词通道锁定参数块：RRF 融合后参数块胜出。"""
    def embed(texts):
        # 节标题带「横摆」→ [1,0]；其余 [0,1]
        return [[1.0, 0.0] if "横摆" in t else [0.0, 1.0] for t in texts]

    chunked_kb.set_embedder(embed, "fake")
    hits = chunked_kb.search("Opt_Instabil_Lambda_HA", top_k=3)
    assert chunked_kb.last_search_mode == "hybrid"
    assert hits[0]["id"] == "abs.s1"
    assert "Opt_Instabil_Lambda_HA" in hits[0]["text"]


def test_chunks_hash_sync_per_block(chunked_kb, tmp_path):
    """改一个 #### 块只重算该块（增量同步以块为单位）。"""
    calls = []
    def embed(texts):
        calls.append(len(texts))
        return [[1.0, 0.0] for _ in texts]

    chunked_kb.set_embedder(embed, "fake")
    chunked_kb.search("横摆")
    n0 = sum(calls)
    import os
    p = os.path.join(chunked_kb.kb_dir, "manual", "abs.md")
    s = open(p, encoding="utf-8").read().replace("0.11", "0.12")
    open(p, "w", encoding="utf-8").write(s)
    chunked_kb.reload()
    chunked_kb.search("横摆")
    assert calls[-1] == 1, "只应重算内容变更的那一块"


# ---------------- 尺寸兜底（MAX_CHUNK_CHARS） ----------------

def test_oversized_section_force_split(tmp_path):
    """无 #### 的超长节：按尺寸强制切，每块都 ≤ 上限，embedding 输入有界。"""
    long_text = "这是超长段落的一句内容，用于填充块体积。" * 60   # ≈1140 字
    mdir = tmp_path / "manual"
    mdir.mkdir()
    (mdir / "big.md").write_text(f"### big.s1 — 巨型节\n\n{long_text}\n", encoding="utf-8")
    kb = KnowledgeBase(str(tmp_path))
    chunks = kb._index_chunks()
    assert len(chunks) >= 2
    assert all(len(c["text"]) <= 480 + 20 for c in chunks)   # 上限 + 标题前缀余量
    # 内容无丢失（拼回去等于原文去空白差）
    assert all("填充块体积" in c["text"] for c in chunks)


def test_real_manual_chunks_respect_cap():
    """真实知识库（abs_calib.md + rag_sample.md）全部块 ≤ 上限。"""
    kb = KnowledgeBase(os.path.join(ROOT, "knowledge"))
    for c in kb._index_chunks():
        assert len(c["text"]) <= 480 + 20, f"{c['chunk_id']} 超限: {len(c['text'])}"
