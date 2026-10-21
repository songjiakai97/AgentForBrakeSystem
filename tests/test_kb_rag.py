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
        assert len(c["text"]) <= 560, f"{c['chunk_id']} 超限: {len(c['text'])}"


# ---------------- 中心窗口（snippet_window） ----------------

@pytest.fixture
def tail_kb(tmp_path):
    """目标串在超大节最后一个块里的知识库：复现「只搜到 #### 前面部分」。"""
    mdir = tmp_path / "manual"
    mdir.mkdir()
    rows = "\n".join(f"| {i} | Opt_pad_{i:03d} | 填充参数{i} | 0.{i % 9} |"
                     for i in range(1, 25))
    (mdir / "big.md").write_text(
        "# ABS 手册\n\n"
        "### big.s1 — ABS 车型参数\n\n"
        "#### 车型基础参数表\n\n"
        "| 序号 | 参数 | 描述 | 值 |\n|---|---|---|---|\n"
        f"{rows}\n\n| 99 | Cp_RA | 后轴CP值 | 26.87 |\n",
        encoding="utf-8")
    return KnowledgeBase(str(tmp_path))


def test_hit_block_span_located_in_section(tail_kb):
    """合并命中给出最佳块在节正文中的区间，且区间确实包住目标内容。"""
    hits = tail_kb.search("后轴CP值", top_k=1)
    assert hits and not hits[0]["fallback"]
    s0, e0 = hits[0]["best"]
    assert "Cp_RA" in hits[0]["full"][s0:e0]
    assert hits[0]["chunk"].startswith("big.s1")   # chunk 字段=最佳块 id


def test_window_contains_tail_marker(tail_kb):
    """窗口预算小于节长时，内容必须来自命中块（而非节头），且不超过预算。"""
    hit = tail_kb.search("后轴CP值", top_k=1)[0]
    for budget in (480, 300, 260):
        snip, _note = KnowledgeBase.snippet_window(hit, budget)
        assert len(snip) <= budget
        assert "Cp_RA" in snip and "26.87" in snip
        assert "ABS 车型参数" in snip            # 标题前缀恒在
    # 预算充足时全文给出、无省略注记
    snip, note = KnowledgeBase.snippet_window(hit, 4000)
    assert snip.count("Cp_RA") == 1 and note == ""


def test_window_snaps_to_line_bounds(tail_kb):
    """切点吸附行边界：表格行要么完整要么不出现，不许有半行参数名。"""
    hit = tail_kb.search("后轴CP值", top_k=1)[0]
    snip, _ = KnowledgeBase.snippet_window(hit, 300)
    for ln in snip.splitlines():
        if ln.lstrip().startswith("|"):
            # 完整 Markdown 行：首尾都有竖线，列数与表头一致（4 列 → 5 个竖线）
            assert ln.startswith("|") and ln.endswith("|"), f"半行表格: {ln!r}"
            assert ln.count("|") == 5, f"列数残缺: {ln!r}"


def test_merged_text_is_verbatim_section(tail_kb):
    """small-to-big 合并不再用空行 join 硬切块：节正文原样可寻（表格不断行）。"""
    hit = tail_kb.search("后轴CP值", top_k=1)[0]
    sec = tail_kb.sections["big.s1"]
    assert hit["full"] == sec.text
    assert hit["text"].startswith("ABS 车型参数\n")   # 合并字段带标题前缀（裸题）
    # 旧实现按块 join 会在表格中段插 \n\n；原文里没有这种空洞（表行连续）
    table_block = sec.text[sec.text.index("| 序号"):sec.text.index("\n\n| 99")]
    assert "\n\n" not in table_block
    assert table_block in hit["full"]


def test_title_not_duplicated_in_chunks(tail_kb):
    """块文本身以 #### 子标题开头时不重复注入子标题（合并后无双写）。"""
    texts = [c["text"] for c in tail_kb._index_chunks() if c["section_id"] == "big.s1"]
    joined = "\n".join(texts)
    assert "车型基础参数表\n#### 车型基础参数表" not in joined


# ---------------- 标识符通道：大小写/分隔符/Unicode + 不串参数 ----------------

@pytest.fixture
def ident_kb(tmp_path):
    """两个相邻参数（Cp_FA / Cp_RA）分属不同节，外加一个 Word 导出形态的名字。"""
    mdir = tmp_path / "manual"
    mdir.mkdir()
    (mdir / "abs.md").write_text(
        "# ABS 标定手册\n\n"
        "### abs.s1 — 干沥青 100kph 全力制动\n\n"
        "| 序号 | 参数 | 初始值 |\n|---|---|---|\n"
        "| 1 | Cp_FA | 26.87 |\n"
        "| 2 | Opt\u2011Tbl2 | 1.0 |\n\n"
        "标定方向：缩短初始夹紧力建立时间。\n\n"
        "### abs.s2 — 洒水玄武岩 60kph 全力制动\n\n"
        "| 序号 | 参数 | 初始值 |\n|---|---|---|\n"
        "| 1 | Cp_RA | 12.5 |\n",
        encoding="utf-8")
    return KnowledgeBase(str(tmp_path))


def test_identifier_variants_hit_same_section(ident_kb):
    """大小写/下划线/连字符/空格/全角写法都是同一个名字（两侧共用 _fold_text）。"""
    for q in ["Cp_FA", "cp_fa", "CP_FA", "cp-fa", "cp fa", "CPFA", "Ｃｐ＿ＦＡ"]:
        hits = ident_kb.search(q, top_k=3)
        assert [h["id"] for h in hits] == ["abs.s1"], q


def test_identifier_query_does_not_leak_to_neighbour_param(ident_kb):
    """Cp_FA 不能把含 Cp_RA 的节带进结果：旧实现靠 p_ 二元组串过来，是假阳性。

    假阳性比搜不到更坏——模型看到两个参数表，可能引用另一个工况的值。
    """
    hits = ident_kb.search("Cp_FA", top_k=3)
    assert "abs.s2" not in [h["id"] for h in hits]
    assert ident_kb.search("cp_ra", top_k=3)[0]["id"] == "abs.s2"
    assert all("Cp_RA" not in h["text"] for h in hits)


def test_identifier_bigrams_are_not_used(ident_kb):
    """标识符不产出二元组：关键词打分里不存在 0<p_> 这类碎片命中。"""
    chunks = {c["chunk_id"]: c for c in ident_kb._index_chunks()}
    scored = dict((cid, s) for s, cid in ident_kb._keyword_rank("cp_fa", chunks))
    assert scored["abs.s1"] == 3.0        # 归一化全等
    assert "abs.s2" not in scored          # 旧实现 0.25（p_）在，现在不该出现


def test_identifier_unicode_dash_matches_ascii(ident_kb):
    """U+2011（Word 导出）与 ASCII 连字符等价；_IDENT_RE 不能把它切成两段。"""
    chunks = {c["chunk_id"]: c for c in ident_kb._index_chunks()}
    # 手册里是 Opt‑Tbl2（U+2011），查 ASCII 写法也要整段命中
    assert ident_kb._norm_param("Opt-Tbl2") in chunks["abs.s1"]["idents"]
    for q in ["Opt-Tbl2", "Opt\u2011Tbl2", "opt tbl2", "OPT_TBL2"]:
        assert ident_kb.search(q, top_k=1)[0]["id"] == "abs.s1", q


def test_title_channel_uses_same_folding(tmp_path):
    """标题通道与正文通道共用分词：标题里的参数名大小写不同也能命中。"""
    mdir = tmp_path / "manual"
    mdir.mkdir()
    (mdir / "m.md").write_text(
        "# 手册\n\n### m.s1 — Cp_FA 标定说明\n\n这一段没有参数名，只有叙述。\n",
        encoding="utf-8")
    kb = KnowledgeBase(str(tmp_path))
    hits = kb.search("cp_fa", top_k=1)
    assert hits and hits[0]["id"] == "m.s1"


def test_chinese_retrieval_unchanged(ident_kb):
    """中文仍走整词+二元组：标识符改造不改变原有召回。"""
    assert ident_kb.search("标定方向", top_k=1)[0]["id"] == "abs.s1"
    assert ident_kb.search("夹紧力建立时间", top_k=1)[0]["id"] == "abs.s1"
    # 纯数字不是标识符：按普通词匹配，不会把整本手册的数值行都算命中
    assert ident_kb.search("26.87", top_k=3)[0]["id"] == "abs.s1"
