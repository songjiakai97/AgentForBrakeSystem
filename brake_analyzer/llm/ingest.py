"""docx 手册摄取：转成 KB 可解析的 Markdown（### id — 标题 切节格式）。

- 段落与表格按文档顺序转换；表格转 Markdown 表——标定手册的信息大头在
  限值表/参数表里，丢了表格语义就丢了一半。
- Heading 1 → 文档标题；Heading 2 → 一节（### <slug>.s<N> — 标题）；
  Heading 3 → 节内子标题（#### 父节标题 · 子标题：各工况下子标题大量同名，
  拼接父级让 md 里的标题行自解释、可区分归属）；Heading 4+ → 更深子标题。
  整篇没有 Heading 2 时退化用 Heading 1 切节。
- 节 id 由「文件 slug + 序号」生成：真实手册标题五花八门，不假设能从标题
  造出合法 id；返回 id↔标题对照，规则引擎的 kb_ref 将来要引用这些 id，
  用户在导入回执里能看到、能改。
- python-docx 是可选依赖：未安装时上传接口给出明确提示（与 BLF 同一口径）。
"""

import os
import re
from dataclasses import dataclass, field
from typing import List


class IngestError(Exception):
    """摄取失败（空文档、结构不可解析等）：消息要让人知道下一步该干什么。"""


@dataclass
class IngestResult:
    markdown: str
    doc_title: str
    sections: List = field(default_factory=list)   # [{"id": str, "title": str}]


def _slug(name: str) -> str:
    base = os.path.splitext(os.path.basename(name))[0]
    s = re.sub(r"[^\w-]+", "_", base, flags=re.UNICODE).strip("_").lower()
    return s or "manual"


def _heading_level(p) -> int:
    """返回标题级别（1~9），非标题返回 0。

    style.name 在中文 Word 文档里通常仍是 'Heading 1'（内部名不本地化），
    但不赌它：style_id 一并检查，任一命中即可。
    """
    try:
        sid = (p.style.style_id or "")
        name = (p.style.name or "")
    except Exception:
        return 0
    for s in (sid, name):
        m = re.match(r"^(?:Heading|标题)\s*(\d)$", s.strip(), re.I)
        if m:
            return int(m.group(1))
    if name.strip() == "Title":
        return 1
    return 0


def _para_md(p) -> str:
    text = p.text.strip()
    return text


def _table_md(tbl) -> str:
    rows = []
    for row in tbl.rows:
        cells = [c.text.strip().replace("\n", " ").replace("|", "\\|") for c in row.cells]
        rows.append(cells)
    if not rows or all(not any(r) for r in rows):
        return ""
    out = ["| " + " | ".join(rows[0]) + " |",
           "| " + " | ".join(["---"] * len(rows[0])) + " |"]
    for r in rows[1:]:
        r = r + [""] * (len(rows[0]) - len(r))
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def docx_to_markdown(path: str, source_name: str = "") -> IngestResult:
    try:
        import docx as _docx
    except ImportError:
        raise IngestError("服务器未安装 python-docx，无法解析 .docx（pip install python-docx）")
    try:
        document = _docx.Document(path)
    except Exception as e:
        raise IngestError(f"docx 打开失败（文件可能损坏或不是 Word 格式）：{type(e).__name__}")

    slug = _slug(source_name or path)
    body = list(document.iter_inner_content())

    doc_title = ""
    items = []          # (level, text)  level: 0=段落, 1..9=标题, -1=表格
    for blk in body:
        if hasattr(blk, "rows"):
            items.append((-1, _table_md(blk)))
            continue
        lvl = _heading_level(blk)
        text = _para_md(blk)
        if not text and lvl == 0:
            continue
        items.append((lvl, text))
    if not items:
        raise IngestError("文档是空的：没有可摄取的段落或表格")

    # 文档标题 = 第一个 Heading 1 / Title；同时决定切节级别（有 H2 用 H2，否则 H1）
    sec_level = 2
    if not any(lvl == 2 for lvl, _ in items):
        sec_level = 1
    first_h1 = next(((i, t) for i, (lvl, t) in enumerate(items) if lvl == 1), None)
    if first_h1:
        doc_title = first_h1[1]
        items = items[:first_h1[0]] + items[first_h1[0] + 1:]

    sections = []       # {"id", "title", "lines"}
    cur = None
    for lvl, text in items:
        if lvl == sec_level:
            cur = {"id": "", "title": text, "lines": []}
            sections.append(cur)
            continue
        if cur is None:
            # 第一个节之前的内容归入「前言」，别让概述性段落从检索里消失
            if not text:
                continue
            cur = {"id": "", "title": "前言", "lines": []}
            sections.append(cur)
        if lvl == -1:
            cur["lines"].append(text)
        elif lvl == sec_level + 1:
            # 子标题拼接父级节名：标定手册每个工况下都有同名「标定参数说明」
            # 「评价标准说明」，光看 `####` 行无法区分归属——检索片段、Web 浏览、
            # 表格定位都靠这行认上下文，必须在转 md 时就做成唯一、自解释的标题
            cur["lines"].append("#### " + f"{cur['title']} · {text}")
        elif lvl > sec_level:
            cur["lines"].append("#" * (lvl + 1) + " " + text)
        else:
            cur["lines"].append(text)

    # 全是空节的兜底（比如只有标题没有正文的壳文档）
    sections = [s for s in sections
                if any(ln.strip() for ln in s["lines"])]
    if not sections:
        raise IngestError("没有切出任何带正文的节：文档只有标题？"
                          "确认手册正文用的是 Word 标题样式（Heading）而不是手动加粗")

    out = ["# " + (doc_title or slug)] if doc_title else []
    meta = []
    for i, s in enumerate(sections, 1):
        sid = f"{slug}.s{i}"
        s["id"] = sid
        out.append(f"\n### {sid} — {s['title']}\n")
        out.append("\n\n".join(ln for ln in s["lines"] if ln.strip()))
        meta.append({"id": sid, "title": s["title"]})
    return IngestResult(markdown="\n".join(out).strip() + "\n",
                        doc_title=doc_title or slug, sections=meta)
