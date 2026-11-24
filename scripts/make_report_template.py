"""程序化生成报告模板 brake_analyzer/report/report_template.docx（design.md §14）。

模板 = 版式 + docxtpl 占位标签，改样式跑本脚本重出文件即可，运行期代码不动。
占位契约与 brake_analyzer/report/generator.py 的 build_context 一一对应，
两边改任何一边都要同步（本脚本的 docstring 末尾附契约表）。

标签约定（docxtpl）：
- {%p ... %} 独占一个段落，渲染后该段落被移除，用于包住跨段落/跨表格的块；
- {%tr ... %} 独占一行（写在首格），渲染后该行被移除，用于循环表格数据行；
- 标记行与循环体行必须分行：往循环体行填占位表达式若与标记同行，
  会把 endfor 标签覆盖掉，jinja 渲染直接报错。

版式（用户定版 2026-11-21）：
- 1 分析总览；2 分析结果明细（编号直接用 conditions.yaml 的 file_ids，如
  4.1.1，一个工况一个小节）；3 规则结论与标定建议汇总（有内容才有该章）；附录。
- 工况小节内 = 两列表「客观指标（名称+限值内联）| 结果」+ 居中时序图；
  多文件/多样本时前导行（run · 窗口 · 文件）区分块，单文件单样本自动消失。

用法：python scripts/make_report_template.py [--out 路径]

自定义模板：运行期用环境变量 REPORT_TEMPLATE 指向自己的 docx 即可（版式自定，
占位契约见本文件末尾与 docs/design.md §18.7）。

模板上下文契约（generator.build_context 提供）：
  title / generated_at / chat_title / analyses_total / bad_total / files[]
  analyses[]（总览行）: index section_no condition_name profile profile_suffix
                        file_name sample_count bad_count
  condition_sections[]: title（"4.1.1 ABS 干沥青 100kph 全力制动"）+ blocks[]
  blocks[]: {lead, rows[{metric, result}], image(InlineImage)|None, caption}
  has_findings / verdict_rows[]: {cond, rule, metric, message, severity}
  suggestion_rows[]: {cond, param, direction, metric, effect, risk, feedback}
  legend_lines[]: str
"""

import argparse
import os

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

DEFAULT_OUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "brake_analyzer", "report", "report_template.docx")

_GRAY = RGBColor(0x66, 0x66, 0x66)
_TABLE_FONT = "微软雅黑"


def _east_asian(style, name: str = "微软雅黑") -> None:
    """中文字体：python-docx 只设 ascii 字体，东亚字体要写 rFonts@eastAsia。"""
    style.font.name = name
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), name)


def _run_font(run, size: float = 9.0) -> None:
    """表格 run 字体：ascii + eastAsia 都钉住，否则表里中文回落到宋体。"""
    run.font.name = _TABLE_FONT
    run.font.size = Pt(size)
    rpr = run._r.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), _TABLE_FONT)


def _page_number_footer(doc: Document) -> None:
    """页脚「第 N 页」：PAGE 域是 Word 字段，python-docx 没有封装，手拼 XML。"""
    footer = doc.sections[0].footer
    p = footer.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    def _run():
        r = p.add_run()
        r.font.size = Pt(9)
        return r

    _run().add_text("第 ")
    r = _run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = "PAGE"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for el in (begin, instr, end):
        r._r.append(el)
    _run().add_text(" 页")


def _cell_text(row, col: int, text: str, bold: bool = False):
    cell = row.cells[col]
    cell.text = text
    cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
    run = cell.paragraphs[0].runs[0] if cell.paragraphs[0].runs \
        else cell.paragraphs[0].add_run(text)
    _run_font(run, 9.0)
    run.font.bold = bold
    return cell


def _shade(cell, fill: str) -> None:
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), fill)
    cell._tc.get_or_add_tcPr().append(shd)


def _fixed_widths(table, widths_cm: list) -> None:
    """固定列宽：Word 默认 auto-fit 会按内容重排，长英文键（brake_distance）
    被拆成两行甚至断词，表格看起来「很乱」。tblLayout=fixed + 每格宽度显式写死。"""
    table.autofit = False
    tblPr = table._tbl.tblPr
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tblPr.append(layout)
    for i, w in enumerate(widths_cm):
        table.columns[i].width = Cm(w)
        for row in table.rows:
            row.cells[i].width = Cm(w)


def _data_table(doc: Document, headers: list, widths_cm: list):
    """带表头与循环骨架的数据表，4 行结构：

    0 表头 / 1 {%tr for %} 标记行（渲染时整行移除）/ 2 循环体模板行（调用方填）
    / 3 {%tr endfor %} 标记行（渲染时整行移除）。

    表头行设为跨页重复（tblHeader），多页报告每页都能对上列名；表头加浅灰底。
    """
    table = doc.add_table(rows=4, cols=len(headers))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    _fixed_widths(table, widths_cm)
    for i, h in enumerate(headers):
        _cell_text(table.rows[0], i, h, bold=True)
        _shade(table.rows[0].cells[i], "F2F2F2")
    trPr = table.rows[0]._tr.get_or_add_trPr()
    repeat = OxmlElement("w:tblHeader")
    repeat.set(qn("w:val"), "true")
    trPr.append(repeat)
    _cell_text(table.rows[1], 0, "{%tr for x in ITEMS %}")
    _cell_text(table.rows[3], 0, "{%tr endfor %}")
    return table


def _gray_par(doc: Document, text: str, size: float = 9.0) -> None:
    p = doc.add_paragraph()
    r = p.add_run(text)
    r.font.size = Pt(size)
    r.font.color.rgb = _GRAY


def _bold_par(doc: Document, text: str, size: float = 10.5) -> None:
    p = doc.add_paragraph()
    r = p.add_run(text)
    r.font.bold = True
    r.font.size = Pt(size)


def build(out_path: str) -> str:
    doc = Document()

    # 全局字体：正文与标题都把东亚字体钉住，避免 Word 用默认宋体渲染
    for name in ("Normal", "Title", "Heading 1", "Heading 2", "Heading 3"):
        st = doc.styles[name]
        if st.type == WD_STYLE_TYPE.PARAGRAPH:
            _east_asian(st)
    doc.styles["Normal"].font.size = Pt(10.5)
    _page_number_footer(doc)
    # 页边距 2cm：版心 17cm，下面各表列宽合计 16.6cm，两侧留一点余量
    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Cm(2.0)
    sec.top_margin = sec.bottom_margin = Cm(2.0)

    # ---- 封面/元信息 ----
    h = doc.add_heading("{{ title }}", level=0)
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    meta = doc.add_paragraph()
    meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = meta.add_run("生成时间 {{ generated_at }} · 会话「{{ chat_title }}」"
                     " · {{ analyses_total }} 份分析 · {{ bad_total }} 项异常/缺失")
    r.font.size = Pt(9)
    r.font.color.rgb = _GRAY

    # ---- 1 分析总览 ----
    doc.add_heading("1 分析总览", level=1)
    tbl = _data_table(doc, ["章节", "工况", "档位", "数据文件", "样本", "异常/缺失"],
                      [1.6, 4.8, 2.8, 4.4, 1.2, 1.8])
    _cell_text(tbl.rows[1], 0, "{%tr for a in analyses %}")
    body = tbl.rows[2]
    for i, key in enumerate(["{{ a.section_no }}", "{{ a.condition_name }}",
                             "{{ a.profile_suffix or '-' }}", "{{ a.file_name }}",
                             "{{ a.sample_count }}", "{{ a.bad_count }}"]):
        _cell_text(body, i, key)

    # ---- 2 分析结果明细：编号 = conditions.yaml file_ids，一工况一小节 ----
    doc.add_heading("2 分析结果明细", level=1)
    p = doc.add_paragraph()
    p.add_run("{%p for a in condition_sections %}")
    doc.add_heading("{{ a.title }}", level=2)
    p = doc.add_paragraph()
    p.add_run("{%p for blk in a.blocks %}")
    p = doc.add_paragraph()
    p.add_run("{%p if blk.lead %}")
    _gray_par(doc, "{{ blk.lead }}")
    p = doc.add_paragraph()
    p.add_run("{%p endif %}")
    tbl = _data_table(doc, ["客观指标", "结果"], [11.0, 5.6])
    _cell_text(tbl.rows[1], 0, "{%tr for r in blk.rows %}")
    body = tbl.rows[2]
    _cell_text(body, 0, "{{ r.metric }}")
    _cell_text(body, 1, "{{ r.result }}")
    p = doc.add_paragraph()
    p.add_run("{%p if blk.image %}")
    pic = doc.add_paragraph()
    pic.alignment = WD_ALIGN_PARAGRAPH.CENTER
    pic.add_run("{{ blk.image }}")
    cap = doc.add_paragraph()
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = cap.add_run("{{ blk.caption }}")
    r.font.size = Pt(8.5)
    r.font.color.rgb = _GRAY
    p = doc.add_paragraph()
    p.add_run("{%p endif %}")
    p = doc.add_paragraph()
    p.add_run("{%p endfor %}")          # blk
    p = doc.add_paragraph()
    p.add_run("{%p endfor %}")          # condition_sections

    # ---- 3 规则结论与标定建议汇总（无任何命中/建议时整章省略）----
    p = doc.add_paragraph()
    p.add_run("{%p if has_findings %}")
    doc.add_heading("3 规则结论与标定建议汇总", level=1)
    _bold_par(doc, "规则结论（规则引擎确定性判定）")
    tbl = _data_table(doc, ["章节/工况", "规则", "指标", "结论", "严重度"],
                      [3.2, 3.0, 2.6, 6.0, 1.8])
    _cell_text(tbl.rows[1], 0, "{%tr for v in verdict_rows %}")
    body = tbl.rows[2]
    for i, key in enumerate(["{{ v.cond }}", "{{ v.rule }}", "{{ v.metric }}",
                             "{{ v.message }}", "{{ v.severity }}"]):
        _cell_text(body, i, key)
    _bold_par(doc, "标定建议（LLM 生成，区分于规则结论）")
    tbl = _data_table(doc, ["章节/工况", "可标定量", "方向", "针对指标",
                            "预期效果", "风险", "反馈"],
                      [2.8, 2.2, 1.8, 2.8, 3.2, 2.4, 1.4])
    _cell_text(tbl.rows[1], 0, "{%tr for s in suggestion_rows %}")
    body = tbl.rows[2]
    for i, key in enumerate(["{{ s.cond }}", "{{ s.param }}", "{{ s.direction }}",
                             "{{ s.metric }}", "{{ s.effect }}", "{{ s.risk }}",
                             "{{ s.feedback }}"]):
        _cell_text(body, i, key)
    p = doc.add_paragraph()
    p.add_run("{%p endif %}")

    # ---- 附录 ----
    doc.add_heading("附录", level=1)
    _bold_par(doc, "指标缺失成因图例")
    p = doc.add_paragraph()
    p.add_run("{%p for line in legend_lines %}")
    _gray_par(doc, "· {{ line }}")
    p = doc.add_paragraph()
    p.add_run("{%p endfor %}")
    _bold_par(doc, "数据文件清单")
    p = doc.add_paragraph()
    p.add_run("{%p for fl in files %}")
    _gray_par(doc, "· {{ fl }}")
    p = doc.add_paragraph()
    p.add_run("{%p endfor %}")

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    doc.save(out_path)
    return out_path


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()
    path = build(args.out)
    print(f"模板已生成: {path}（{os.path.getsize(path)} bytes）")
