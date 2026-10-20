"""docx 手册摄取 + KB 上传/删除链路测试。python-docx 缺失时整文件跳过（与 BLF 同口径）。"""

import os

import pytest

docx_mod = pytest.importorskip("docx")

from docx import Document

from brake_analyzer.llm.ingest import IngestError, docx_to_markdown
from brake_analyzer.llm.kb import KnowledgeBase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KB_DIR = os.path.join(ROOT, "knowledge")


def _make_docx(path):
    d = Document()
    d.add_heading("测试标定手册", level=1)
    d.add_paragraph("这是概述段落。")
    d.add_heading("横摆稳定性", level=2)
    d.add_paragraph("判定：横摆角速度峰值不超过限值。")
    t = d.add_table(rows=2, cols=2)
    t.rows[0].cells[0].text = "指标"
    t.rows[0].cells[1].text = "限值"
    t.rows[1].cells[0].text = "yaw"
    t.rows[1].cells[1].text = "5"
    d.add_heading("详细说明", level=3)
    d.add_paragraph("节内子标题下的细节。")
    d.add_heading("制动距离", level=2)
    d.add_paragraph("判定：不超过 40m。")
    d.save(str(path))


def test_docx_sections_tables_and_ids(tmp_path):
    p = tmp_path / "m.docx"
    _make_docx(p)
    res = docx_to_markdown(str(p), source_name="m.docx")
    assert res.doc_title == "测试标定手册"
    titles = [s["title"] for s in res.sections]
    assert "横摆稳定性" in titles and "制动距离" in titles
    # id 是 slug.s<N> 形式，全部唯一
    ids = [s["id"] for s in res.sections]
    assert ids[0].startswith("m.s") and len(set(ids)) == len(ids)
    # 表格转 md 表（标定手册的信息大头）
    assert "| 指标 | 限值 |" in res.markdown
    assert "| yaw | 5 |" in res.markdown
    # H3 保留为节内子标题，不切断节；且拼接父级节名保证唯一（见下）
    assert "#### 横摆稳定性 · 详细说明" in res.markdown
    assert "节内子标题下的细节" in res.markdown


def test_ingested_markdown_is_kb_parseable(tmp_path):
    """摄取产物必须能被 KnowledgeBase 按 ### id — 标题 切节——这是存储格式契约。"""
    p = tmp_path / "m.docx"
    _make_docx(p)
    md = docx_to_markdown(str(p), source_name="m.docx").markdown
    kb_file = tmp_path / "manual"
    kb_file.mkdir()
    (kb_file / "m.md").write_text(md, encoding="utf-8")
    kb = KnowledgeBase(str(tmp_path))
    # 前言 + 两个 H2 节：第一个标题前的概述段落归入自动生成的「前言」节，
    # 不让它从检索里消失
    assert len(kb.sections) == 3
    sec = kb.sections["m.s2"]
    assert sec.title == "横摆稳定性"
    assert "| yaw | 5 |" in sec.text


def test_empty_docx_raises_readable(tmp_path):
    p = tmp_path / "empty.docx"
    Document().save(str(p))
    with pytest.raises(IngestError):
        docx_to_markdown(str(p), source_name="empty.docx")


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from web.main import app

    return TestClient(app)


def test_upload_delete_roundtrip(client, tmp_path):
    """上传 docx → 节出现在 /api/kb → 删除 → /api/kb 恢复为空（不污染真实 knowledge/）。"""
    from web import main as m

    p = tmp_path / "up.docx"
    _make_docx(p)
    before = len(m.KB.sections)
    with open(str(p), "rb") as f:
        r = client.post("/api/kb/manual",
                        files={"files": ("up.docx", f, "application/octet-stream")})
    assert r.status_code == 200, r.text
    body = r.json()["files"][0]
    assert body["source"] == "docx" and body["section_count"] == 3
    assert body["sections"][0]["id"].startswith("up.s")
    kb = client.get("/api/kb").json()
    assert any(g["function"] == "up" for g in kb["manual"])
    # 原件留底
    assert os.path.isfile(os.path.join(m.KB_ARCHIVE_DIR, "up.docx"))

    r = client.delete("/api/kb/manual/up")
    assert r.status_code == 200, r.text
    assert len(m.KB.sections) == before
    kb = client.get("/api/kb").json()
    assert not any(g["function"] == "up" for g in kb["manual"])


def test_upload_rejects_bad_ext(client):
    r = client.post("/api/kb/manual",
                    files={"files": ("x.pdf", b"fake", "application/pdf")})
    assert r.status_code == 400


def test_subheadings_unique_across_sections(tmp_path):
    """同名子标题（各工况都有「标定参数说明」）拼接父级后必须唯一可辨。"""
    import docx
    d = docx.Document()
    d.add_heading("ABS 标定手册", level=1)
    for cond in ("干沥青100km/h", "洒水玄武岩60km/h"):
        d.add_heading(cond, level=2)
        d.add_heading("标定参数说明", level=3)
        d.add_paragraph(f"{cond} 的参数表")
    p = tmp_path / "m2.docx"
    d.save(str(p))
    res = docx_to_markdown(str(p), source_name="m2.docx")
    subs = [ln for ln in res.markdown.splitlines() if ln.startswith("#### ")]
    assert len(subs) == 2
    assert len(set(subs)) == 2, f"子标题撞名: {subs}"
    assert any("干沥青100km/h · 标定参数说明" in s for s in subs)


def _make_chapter_docx(path, title=None):
    """章级手册：多个 Heading 1 章 + 章下 Heading 2 工况 + Heading 3 子标题。"""
    import docx
    d = docx.Document()
    if title is not None:
        d.add_heading(title, level=1)
    d.add_paragraph("手册总则：覆盖全部工况。")
    d.add_heading("第1章 对开路面", level=1)
    d.add_paragraph("对开路面章节导语：左右附着系数不同。")
    t = d.add_table(rows=2, cols=2)
    t.rows[0].cells[0].text = "章级限值"
    t.rows[0].cells[1].text = "值"
    t.rows[1].cells[0].text = "k"
    t.rows[1].cells[1].text = "1"
    d.add_heading("干沥青100km/h", level=2)
    d.add_paragraph("工况说明。")
    d.add_heading("标定参数说明", level=3)
    d.add_paragraph("参数表如下。")
    d.add_heading("第2章 高附对接低附", level=1)
    d.add_paragraph("第2章导语。")
    d.add_heading("干沥青100km/h", level=2)      # 同名工况、不同章
    d.add_paragraph("工况说明2。")
    d.save(str(path))


def test_chapter_headings_are_not_dropped(tmp_path):
    """比节更浅的层级（章 H1）过去被降级成段落、连导语和限值表一起粘进相邻工况节。
    现在章自己成节，内容必须在库里查得到。"""
    p = tmp_path / "ch.docx"
    _make_chapter_docx(p)
    res = docx_to_markdown(str(p), source_name="ch.docx")
    titles = [s["title"] for s in res.sections]
    assert "第1章 对开路面" in titles and "第2章 高附对接低附" in titles

    kb_dir = tmp_path / "kb"
    (kb_dir / "manual").mkdir(parents=True)
    (kb_dir / "manual" / "ch.md").write_text(res.markdown, encoding="utf-8")
    kb = KnowledgeBase(str(kb_dir))
    chap = next(s for s in kb.sections.values() if s.title == "第1章 对开路面")
    assert "对开路面章节导语" in chap.text
    assert "| 章级限值 | 值 |" in chap.text, "章级表格丢失"
    # 章内容不再粘进工况节
    cond = next(s for s in kb.sections.values() if s.title.endswith("干沥青100km/h"))
    assert "章级限值" not in cond.text


def test_section_title_carries_chapter_breadcrumb(tmp_path):
    """扁平成一串 ### 后顺序是唯一体现层级的东西——工况节标题必须带上所属章，
    且同名工况分属不同章时不能撞车。"""
    p = tmp_path / "ch.docx"
    _make_chapter_docx(p)
    res = docx_to_markdown(str(p), source_name="ch.docx")
    titles = [s["title"] for s in res.sections]
    assert "第1章 对开路面 · 干沥青100km/h" in titles
    assert "第2章 高附对接低附 · 干沥青100km/h" in titles
    assert len(set(titles)) == len(titles), f"同名工况跨章撞车: {titles}"


def test_doc_title_falls_back_when_headings_are_chapters(tmp_path):
    """全是章级 H1、没有封面标题时，doc_title 退回文件名，markdown 仍带一行 `# `
    （KB 靠它取 doc_title）。"""
    p = tmp_path / "fallback.docx"
    _make_chapter_docx(p, title=None)
    res = docx_to_markdown(str(p), source_name="fallback.docx")
    assert res.doc_title == "fallback"
    assert res.markdown.startswith("# fallback\n")

    kb_dir = tmp_path / "kb2"
    (kb_dir / "manual").mkdir(parents=True)
    (kb_dir / "manual" / "fallback.md").write_text(res.markdown, encoding="utf-8")
    kb = KnowledgeBase(str(kb_dir))
    assert all(s.doc_title == "fallback" for s in kb.sections.values())


def test_deeper_subheading_level_still_chunks(tmp_path):
    """子标题层级不写死 Heading 3：手册直接跳到 H4 时也要产出 `####`（kb 的切块边界），
    过去输出 `#####` 会让整节退化成一整块。"""
    import docx
    d = docx.Document()
    d.add_heading("手册", level=1)
    d.add_heading("干沥青", level=2)
    d.add_paragraph("说明")
    d.add_heading("标定参数说明", level=4)
    d.add_paragraph("参数表内容")
    d.add_heading("评价标准说明", level=4)
    d.add_paragraph("评价内容")
    p = tmp_path / "h4.docx"
    d.save(str(p))
    res = docx_to_markdown(str(p), source_name="h4.docx")
    subs = [ln for ln in res.markdown.splitlines() if ln.startswith("#### ")]
    assert subs == ["#### 干沥青 · 标定参数说明", "#### 干沥青 · 评价标准说明"]

    kb_dir = tmp_path / "kb3"
    (kb_dir / "manual").mkdir(parents=True)
    (kb_dir / "manual" / "h4.md").write_text(res.markdown, encoding="utf-8")
    kb = KnowledgeBase(str(kb_dir))
    # 切块边界生效：一节切成三块（节头 + 两个子标题块）
    assert len(kb._index_chunks()) == 3


def test_no_h2_chapter_doc_splits_by_chapter(tmp_path):
    """退化路径：只有章级 H1、没有 H2 时按章切节，章标题保持原样（无面包屑）。"""
    import docx
    d = docx.Document()
    for name in ("第1章 对开路面", "第2章 高附对接低附"):
        d.add_heading(name, level=1)
        d.add_paragraph(f"{name} 的内容。")
        d.add_heading("标定参数说明", level=3)
        d.add_paragraph("深层内容。")
    p = tmp_path / "noh2.docx"
    d.save(str(p))
    res = docx_to_markdown(str(p), source_name="noh2.docx")
    titles = [s["title"] for s in res.sections]
    assert "第1章 对开路面" in titles and "第2章 高附对接低附" in titles
    assert any(ln.startswith("#### 第1章 对开路面 · 标定参数说明")
               for ln in res.markdown.splitlines())
