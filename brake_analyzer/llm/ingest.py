"""docx 手册摄取：转成 KB 可解析的 Markdown（### id — 标题 切节格式）。

- 段落与表格按文档顺序转换；表格转 Markdown 表——标定手册的信息大头在
  限值表/参数表里，丢了表格语义就丢了一半。
- 切节不止认单一层级：Heading 1 与 Heading 2 都参与切节（KB 的存储契约是
  `### <id> — 标题`，所以更浅的层级必须升格成节，不能降成段落）。真实手册
  是「章（H1）→ 工况（H2）→ 说明（H3）」，只切 H2 会把章级标题连它下面的
  导语、限值表一起粘进相邻工况节——层级归属在知识库里就没了。
- 节标题带上级路径（`第1章 对开路面 · 干沥青100km/h`）：扁平化成一串 `###`
  后，顺序是唯一体现层级关系的东西，光看节标题无法判断工况属于哪一章；
  面包屑让 id、检索片段、导入回执都自解释，同名工况分属不同章时也不会撞车。
- 文档标题兜底链：只出现一次的最浅层级标题（封面式）→ Word 文档属性 → 文件名。
  节内首个更深层级（手册通常是 Heading 3，也有直接跳到 4 的）→ `#### 父节标题 · 子标题`：
  各工况下子标题大量同名，拼接父级让 md 里的标题行自解释、可区分归属，同时也是
  kb 的切块边界；再深的层级 → 更深一级标题。整篇没有 Heading 2 时退化用 Heading 1 切节。
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
        # 封面标题比 Heading 1 更浅（0.5）：手册常是 Title + 多个 Heading 1 章，
        # 若都算 level 1 就会被当成「出现多次的最浅层级」而当成章切节、抢掉文档标题
        return 0.5
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

    # Word 文档属性里的标题：章级手册常把书名放属性、正文全是 Heading 1 章，
    # 没有它就只能拿文件名当 doc_title（kb 的工况过滤/面包屑顶层要用）
    try:
        cp_title = (document.core_properties.title or "").strip()
    except Exception:
        cp_title = ""

    doc_title = ""
    items = []          # (level, text)  level: 0=段落, 0.5=Title, 1..9=标题, -1=表格
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

    # 文档标题：只出现一次的最浅层级标题视为封面（Title / 单个 Heading 1），取走不切节；
    # 出现多次（章级）则全部留作节，标题退回用 Word 文档属性 / 文件名，别让它们从面包屑里消失
    levels = sorted({lvl for lvl, _ in items if lvl > 0})
    if levels:
        shallowest = levels[0]
        if sum(1 for lvl, _ in items if lvl == shallowest) == 1:
            k = next(i for i, (lvl, _) in enumerate(items) if lvl == shallowest)
            doc_title = items[k][1]
            items = items[:k] + items[k + 1:]
            levels = sorted({lvl for lvl, _ in items if lvl > 0})

    # 切节基准层级：有 H2 用 H2（工况节），否则取实际出现的最浅层级。
    # 比基准更浅的层级同样切节（章 → 独立一节）：KB 只认 `### id — 标题`，
    # 把章降级成段落就等于把章的导语和限值表粘进相邻工况节、层级归属全丢。
    # 比基准更深的层级留在节内做子标题，切得越碎反而丢了工况整体性。
    sec_level = (2 if 2 in levels else levels[0]) if levels else 2
    sub_level = next((lvl for lvl in levels if lvl > sec_level), 0)

    sections = []       # {"id", "title", "lines"}
    cur = None
    path = {}           # 层级 → 最近一个该层级标题（给更深层的节做面包屑）
    for lvl, text in items:
        if 0 < lvl <= sec_level:
            # 上级路径拼进节标题：KB 把每层级都摊平成 `###`，摊平后节与节的
            # 从属关系只能靠标题文字体现——「干沥青100km/h」看不出属于哪一章，
            # 跨章同名工况还会撞车
            crumbs = [path[k] for k in sorted(path) if k < lvl and path[k] != text]
            title = " · ".join(crumbs + [text])
            path[lvl] = text
            for k in [k for k in path if k > lvl]:
                del path[k]
            cur = {"id": "", "title": title, "lines": []}
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
        elif sub_level and lvl == sub_level:
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

    # doc_title 兜底链：封面标题 → Word 文档属性 → 文件名。总写一行 `# `，
    # KB 靠它取 doc_title（工况过滤、面包屑顶层），留空会让这两处退化
    top = doc_title or cp_title or slug
    out = ["# " + top]
    meta = []
    for i, s in enumerate(sections, 1):
        sid = f"{slug}.s{i}"
        s["id"] = sid
        out.append(f"\n### {sid} — {s['title']}\n")
        out.append("\n\n".join(ln for ln in s["lines"] if ln.strip()))
        meta.append({"id": sid, "title": s["title"]})
    return IngestResult(markdown="\n".join(out).strip() + "\n",
                        doc_title=top, sections=meta)
