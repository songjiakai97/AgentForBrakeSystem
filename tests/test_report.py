"""报告生成测试：图表渲染、结果收集、docx 填充、generate_report 工具挂载。

不依赖 LLM（NoLlm 同 test_agent_tools）；docx 断言用 python-docx 读回全文。
fixture 工况/指标全部用 configs 里的真实 id（abs_full_brake_dry_asphalt_100kph 等），
编号断言直接对 conditions.yaml 的 file_ids（4.1.1 / 5.1.1）。
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "configs")

import numpy as np
from docx import Document

from brake_analyzer.agent.engine import ChatEngine
from brake_analyzer.configs import load_configs
from brake_analyzer.llm.client import LlmReply
from brake_analyzer.llm.context import ContextBudget
from brake_analyzer.llm.kb import KnowledgeBase
from brake_analyzer.agent.registry import build_tool_specs
from brake_analyzer.loaders.base import SignalData
from brake_analyzer.report.charts import render_sample_chart
from brake_analyzer.report.generator import collect_analyses, generate_report
from web.store import FileEntry, Store

CFG = load_configs(CONFIG_DIR)
ABS_DRY_100 = "abs_full_brake_dry_asphalt_100kph"       # file_ids: 4.1.1
TCS_DRY_0100 = "tcs_full_throttle_dry_asphalt_0to100kph"  # file_ids: 5.1.1


class NoLlm:
    available = False


def _engine(tmp_path):
    return ChatEngine(CFG, NoLlm(),
                      KnowledgeBase(os.path.join(ROOT, "knowledge")), Store(),
                      budget=ContextBudget(max_tokens=8000, compress_tokens=6000),
                      report_dir=str(tmp_path / "reports"))


def _signals(n: int = 400) -> dict:
    """合成信号：覆盖 abs/tcs 的功能信号清单里可画的大部分通道。"""
    ts = np.linspace(0.0, 10.0, n)

    def sig(vals, unit=""):
        return SignalData(ts=ts, values=vals, unit=unit)

    return {
        "speed_vbox": sig(80 + 3 * np.sin(ts)),
        "Ax": sig(5 + 2 * np.sin(3 * ts), "m/s2"),
        "yaw_rate": sig(2 * np.sin(ts * 1.5), "deg/s"),
        "wheelSpeed_FL": sig(78 + 4 * np.sin(ts + 0.3)),
        "wheelSpeed_FR": sig(78 + 4 * np.sin(ts + 0.5)),
        "wheelSpeed_RL": sig(78 + 4 * np.sin(ts + 0.7)),
        "wheelSpeed_RR": sig(78 + 4 * np.sin(ts + 0.9)),
        "BrakePedalPos": sig(60 + 5 * np.sin(ts * 2), "%"),
        "ThrottlePedalPos": sig(30 + 5 * np.sin(ts), "%"),
        "ABS_Active": sig(1.0 * ((ts > 2) & (ts < 5))),
        "TCS_Active": sig(1.0 * ((ts > 2) & (ts < 5))),
    }


def _analysis(condition_id=ABS_DRY_100, **over) -> dict:
    """AnalysisResult.to_dict 形状的最小样本（与 schemas.AnalysisResult 同构）。

    指标键/名/单位/限值与 configs（metrics.yaml + conditions.yaml）对齐。
    """
    keys = {
        "yaw_rate_max": ("最大横摆角速度", "°/s", [None, 5]),
        "brake_distance": ("制动距离", "m", [None, 40]),
        "acc_time": ("加速时间", "s", [None, 10]),
    }
    samples = over.pop("samples", None) or [{
        "sample_id": "an_1_s1",
        "run_index": 1,
        "file_name": "demo.mf4",
        "window": {"t_start": 2.0, "t_end": 5.5, "anchor": 3.0},
        "analysis_id": "an_1",
        "status_summary": {"ok": 1, "abnormal": 1, "missing": 0, "info": 0},
        "metrics": [
            {"key": f"{condition_id}.yaw_rate_max", "name": keys["yaw_rate_max"][0],
             "category": "", "value": 2.8, "unit": keys["yaw_rate_max"][1],
             "ts_range": None, "status": "ok",
             "ok_range": keys["yaw_rate_max"][2], "profile": None},
            {"key": f"{condition_id}.brake_distance", "name": keys["brake_distance"][0],
             "category": "", "value": 45.234, "unit": keys["brake_distance"][1],
             "ts_range": None, "status": "abnormal",
             "ok_range": keys["brake_distance"][2], "profile": None},
        ],
        "verdicts": [{
            "rule_id": "R_dist_high", "metric_key": f"{condition_id}.brake_distance",
            "metric_name": "制动距离", "status": "abnormal", "severity": "high",
            "fault_domain": "brake_performance",
            "message": "制动距离 45.2m 超过限值 40m",
            "kb_ref": "", "direction": "高于上界", "scope": "function",
        }],
    }]
    d = {
        "analysis_id": "an_1",
        "condition_id": condition_id,
        "condition_name": "ABS 干沥青 100kph 全力制动",
        "function_key": "abs",
        "profile": None,
        "file_id": "fx_1",
        "file_name": "demo.mf4",
        "sample_count": len(samples),
        "degraded": False,
        "samples": samples,
        "llm_suggestion": {
            "summary": "增大目标滑移率以缩短制动距离",
            "calibration_actions": [{"param": "Cp_FA", "direction": "increase",
                                     "metric": "brake_distance",
                                     "expected_effect": "缩短制动距离",
                                     "risk": "抱死风险上升"}],
            "feedback": {"status": "accepted", "note": "已试跑", "ts": 1.0},
        },
    }
    d.update(over)
    d["samples"] = samples
    return d


class Msg:
    def __init__(self, kind, data=None, content=""):
        self.kind, self.data, self.content = kind, data, content


class FakeChat:
    def __init__(self, messages, title="测试会话"):
        self.messages, self.title = messages, title


# ---------------- 图表渲染 ----------------

def test_render_sample_chart_png(tmp_path):
    png = str(tmp_path / "c.png")
    ok = render_sample_chart(_signals(), (2.0, 5.5), CFG.chart_layout, png,
                             highlight_status="abnormal",
                             only=list(CFG.function("abs").signals))
    assert ok and os.path.isfile(png) and os.path.getsize(png) > 2000


def test_render_sample_chart_no_channels(tmp_path):
    """没有任何已配置通道 → 不出图返回 False，也不落 PNG 文件。"""
    png = str(tmp_path / "empty.png")
    assert render_sample_chart({}, (1.0, 2.0), None, png) is False
    assert not os.path.exists(png)


def test_render_sample_chart_respects_function_signals(tmp_path):
    """only 过滤（functions.yaml 功能信号清单）：ABS 不画 TCS_Active。"""
    png = str(tmp_path / "c.png")
    assert render_sample_chart(_signals(), (2.0, 5.5), CFG.chart_layout, png,
                               only=list(CFG.function("abs").signals))
    from PIL import Image
    # 通道过滤不好直接断像素，至少确认 only=None（全部组）图更高（行数更多）
    png_all = str(tmp_path / "all.png")
    assert render_sample_chart(_signals(), (2.0, 5.5), CFG.chart_layout, png_all)
    assert os.path.getsize(png_all) != os.path.getsize(png)


def test_activation_signals_occupy_separate_rows():
    """激活标志各占一行：ABS 静态一行；TCS/DTCS 共用一行、按识别结果动态拼。"""
    from brake_analyzer.charts.html import _groups_from_layout
    from brake_analyzer.drive import layout_with_drive

    rows = {g.label: g.signals for g in CFG.chart_layout}
    assert rows["ABS 激活"] == ["ABS_Active"]
    # TCS 激活位与 DMC 模式不进静态布局（同一行按分析结果动态选择）
    assert "TCS 激活" not in rows
    labels = [g[0] for g in _groups_from_layout(
        _signals(), CFG.chart_layout, only=list(CFG.function("abs").signals))]
    assert "ABS 激活" in labels and "TCS 激活" not in labels

    # TCS 数据（DMC 模式有非零）→ 激活行画 DMC 模式；显式 TCS → 画 TCS_Active
    sig = _signals()
    ts = next(iter(sig.values())).ts
    from brake_analyzer.loaders.base import SignalData

    sig["VehDriveType"] = SignalData(ts=ts, values=np.full_like(ts, 1.0))
    sig["DMCOperationModeFA"] = SignalData(ts=ts, values=np.full_like(ts, 1.0))
    sig["DMCOperationModeRA"] = SignalData(ts=ts, values=np.full_like(ts, 1.0))
    dtcs = layout_with_drive(CFG, "tcs", sig, "4WD|DTCS", CFG.chart_layout)
    row = next(g for g in dtcs if g.label.startswith("DTCS 激活"))
    assert row.signals == ["DMCOperationModeFA", "DMCOperationModeRA"]
    tcs = layout_with_drive(CFG, "tcs", sig, "4WD|TCS", CFG.chart_layout)
    row = next(g for g in tcs if g.label == "TCS 激活")
    assert row.signals == ["TCS_Active"]


def test_distance_row_has_chinese_label():
    """distance_vbox 单独一行且用中文组名（它不是车速，不能并进 km/h 那轴）。"""
    rows = {g.label: g.signals for g in CFG.chart_layout}
    assert rows["行驶距离"] == ["distance_vbox"]


def test_report_chart_global_palette_and_legend_per_axis():
    """颜色跨子图连续循环（不每轴重置成第一个颜色），单信号行也出图例。"""
    from brake_analyzer.charts.html import _groups_from_layout
    from brake_analyzer.report.charts import CHART_PALETTE, _build_figure

    sigs = _signals()
    groups = _groups_from_layout(sigs, CFG.chart_layout,
                                 only=["speed_vbox", "Ax", "yaw_rate"])
    fig, axes, plotted = _build_figure(groups, sigs, (2.0, 5.5), "ok")
    try:
        import matplotlib.pyplot as plt
        assert plotted == 3
        colors = []
        for ax in axes[:, 0]:
            lines = ax.get_lines()                 # axvspan 是 Patch，不在 lines 里
            assert len(lines) == 1
            assert ax.get_legend() is not None, "单信号子图也要有图例"
            colors.append(lines[0].get_color())
        assert colors == CHART_PALETTE[:3]
        assert len(set(colors)) == 3
        assert fig.get_size_inches()[1] < 0.95 * 3 + 0.6, "行高应压缩，一张图不占满页"
        assert fig._suptitle is None, "报告图不再画标题（信息由 docx 图注承担）"
        assert axes[-1][0].get_xlabel() == "time(s)"
    finally:
        import matplotlib.pyplot as plt
        plt.close(fig)


# ---------------- collect_analyses ----------------

def test_collect_analyses_order_dedupe_filter():
    a1 = _analysis()
    a2 = _analysis(analysis_id="an_2", condition_id=TCS_DRY_0100,
                   condition_name="TCS 干沥青 0→100kph 全油门加速",
                   function_key="tcs", profile="4WD|DTCS", file_id="fx_2")
    chat = FakeChat([
        Msg("steps", {"items": []}),                      # 非结果消息，忽略
        Msg("analysis_result", a1),
        Msg("analysis_result", dict(a1)),                 # 同 id 重复，去重
        Msg("analysis_result", a2),
        Msg("text", None, "正文"),
    ])
    all_analyses = collect_analyses(chat)
    assert [a["analysis_id"] for a in all_analyses] == ["an_1", "an_2"]
    assert [a["analysis_id"] for a in collect_analyses(chat, [ABS_DRY_100])] == ["an_1"]
    assert collect_analyses(chat, ["nope"]) == []


# ---------------- docx 生成 ----------------

def _docx_text(path) -> str:
    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs]
    for t in doc.tables:
        for row in t.rows:
            for c in row.cells:
                parts.append(c.text)
    return "\n".join(parts)


def _docx_paragraphs(path) -> list:
    """仅正文段落（不含表格单元格）：标题唯一性按段落判定——编号合法地
    出现在总览/汇总表单元格里，全文计数会把它们也算进去。"""
    return [p.text for p in Document(str(path)).paragraphs]


def test_generate_report_tables_and_suggestion(tmp_path):
    out = str(tmp_path / "r.docx")
    res = generate_report([_analysis()], out, CFG, title="专项报告", chat_title="会话A")
    assert res["ok"] is True and res["analyses"] == 1 and res["charts"] == 0
    assert res["warnings"], "无信号时应给出告警而不是静默"
    text = _docx_text(out)
    assert "专项报告" in text
    # 编号与标题来自 conditions.yaml：file_ids + name（自带 ABS 前缀）
    assert "4.1.1 ABS 干沥青 100kph 全力制动" in text
    # 客观指标：名称+限值内联，顺序=conditions.yaml 声明顺序
    assert "客观指标" in text
    assert text.index("最大横摆角速度≤5°/s") < text.index("制动距离≤40m")
    assert "2.8 °/s" in text and "45.234 m（超限）" in text
    # 只保留工况小节：总览/汇总/附录都不再出现
    assert "分析总览" not in text and "规则结论" not in text
    assert "R_dist_high" not in text and "Cp_FA" not in text


def test_config_declared_metrics_are_complete(tmp_path):
    """config 声明但结果缺失的指标补一行无值，报告不静默丢指标。"""
    d = _analysis()
    # 从结果里抽掉 brake_distance：config 仍要求它在表里
    for s in d["samples"]:
        s["metrics"] = [m for m in s["metrics"]
                        if m["key"].endswith(".yaw_rate_max")]
    out = str(tmp_path / "r.docx")
    generate_report([d], out, CFG, signals_of=lambda fid: None)
    text = _docx_text(out)
    assert "制动距离≤40m" in text and "无值（未算出）" in text


def test_config_numbering_and_order(tmp_path):
    """顺序与编号全按 config：传入顺序故意倒置，报告按 4.1.1 → 5.1.1 排。"""
    a_tcs = _analysis(analysis_id="an_tcs", condition_id=TCS_DRY_0100,
                      condition_name="TCS 干沥青 0→100kph 全油门加速",
                      function_key="tcs", profile="4WD|DTCS", file_id="fx_2",
                      file_name="tcs.mf4")
    a_abs = _analysis()
    out = str(tmp_path / "r.docx")
    generate_report([a_tcs, a_abs], out, CFG, signals_of=lambda fid: None)
    paras = _docx_paragraphs(out)
    i_abs = paras.index("4.1.1 ABS 干沥青 100kph 全力制动")
    i_tcs = paras.index("5.1.1 TCS 干沥青 0→100kph 全油门加速")
    assert i_abs < i_tcs, "config 顺序（abs 在前）应压过传入顺序"


def test_same_condition_files_share_section(tmp_path):
    """同工况多文件共用一个编号，文件进块前导行而不是新编号。"""
    a1 = _analysis(file_id="fx_1", file_name="a.mf4")
    a2 = _analysis(analysis_id="an_2", file_id="fx_2", file_name="b.mf4")
    out = str(tmp_path / "r.docx")
    res = generate_report([a1, a2], out, CFG, signals_of=lambda fid: None)
    paras = _docx_paragraphs(out)
    assert paras.count("4.1.1 ABS 干沥青 100kph 全力制动") == 1
    text = _docx_text(out)
    assert "a.mf4 · run1 · 窗口 [2.00, 5.50] s" in text
    assert "b.mf4 · run1 · 窗口 [2.00, 5.50] s" in text
    assert res["analyses"] == 2


def test_blank_paragraph_between_table_and_chart(tmp_path):
    """表格与时序图之间必须隔一个空行（用户定版）。"""
    from docx.oxml.ns import qn

    out = str(tmp_path / "r.docx")
    generate_report([_analysis()], out, CFG, signals_of=lambda fid: _signals())
    kinds = []
    for ch in Document(out).element.body.iterchildren():
        if ch.tag == qn("w:tbl"):
            kinds.append("table")
        elif ch.tag == qn("w:p"):
            if ch.findall(".//" + qn("a:blip")):
                kinds.append("image")
            else:
                kinds.append("text" if "".join(ch.itertext()).strip() else "blank")
    assert any(kinds[i:i + 3] == ["table", "blank", "image"]
               for i in range(len(kinds) - 2)), kinds


def test_generate_report_with_charts_and_cleanup(tmp_path):
    out = str(tmp_path / "r.docx")
    res = generate_report([_analysis()], out, CFG,
                          signals_of=lambda fid: _signals())
    assert res["charts"] == 1
    assert _docx_text(res["path"])
    # 临时图目录必须清干净：InlineImage 在渲染时读图，save 后不再依赖
    leftovers = [n for n in os.listdir(tmp_path) if n.startswith(".charts_")]
    assert leftovers == []


def test_chart_captions_follow_sample_run(tmp_path):
    """图标题/告警的 run 号取当前样本：残留变量会把 run1 的图标成 run2（实测踩中）。"""
    d = _analysis(samples=[
        {"sample_id": "an_1_s1", "run_index": 1, "file_name": "demo.mf4",
         "window": {"t_start": 2.0, "t_end": 5.5, "anchor": 3.0},
         "analysis_id": "an_1", "status_summary": {"ok": 1, "abnormal": 1,
                                                   "missing": 0, "info": 0},
         "metrics": _analysis()["samples"][0]["metrics"], "verdicts": []},
        {"sample_id": "an_1_s2", "run_index": 2, "file_name": "demo.mf4",
         "window": {"t_start": 14.2, "t_end": 17.8, "anchor": 16.0},
         "analysis_id": "an_1", "status_summary": {"ok": 1, "abnormal": 0,
                                                   "missing": 0, "info": 0},
         "metrics": [_analysis()["samples"][0]["metrics"][0]], "verdicts": []},
    ])
    out = str(tmp_path / "r.docx")
    res = generate_report([d], out, CFG, signals_of=lambda fid: _signals())
    assert res["charts"] == 2
    text = _docx_text(out)
    # 多样本 → 前导行与图注都按各自样本的 run/窗口成对出现，不串号
    assert "run1 · 窗口 [2.00, 5.50] s" in text
    assert "run2 · 窗口 [14.20, 17.80] s" in text
    assert text.count("窗口 [2.00, 5.50] s") == 2   # 前导行 + 图注，各一次
    assert text.count("窗口 [14.20, 17.80] s") == 2


def test_generate_report_rejects_empty_and_missing_template(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        generate_report([], str(tmp_path / "x.docx"), CFG)
    with pytest.raises(FileNotFoundError):
        generate_report([_analysis()], str(tmp_path / "x.docx"), CFG,
                        template_path=str(tmp_path / "nope.docx"))


# ---------------- 工具挂载 ----------------

def test_tool_spec_registered():
    spec = next(s for s in build_tool_specs(CFG)
                if s["function"]["name"] == "generate_report")
    props = spec["function"]["parameters"]["properties"]
    assert "title" in props and "charts" in props
    # 报告口径固定为「会话内全部分析结果」：不暴露 condition_ids，模型就不会把
    # run_analysis_on_files 的同名参数带过来，导致多份分析只写进最后一份
    assert "condition_ids" not in props


def test_report_tool_includes_all_analyses_despite_stray_condition_ids(tmp_path):
    """模型误带 condition_ids 时也必须收录全部分析（一轮多份数据的回归）。"""
    from brake_analyzer.agent.registry import TOOLS

    eng = _engine(tmp_path)
    chat = eng.store.create_chat()
    a_abs = _analysis(file_id="fx_1", file_name="a.mf4")
    a_tcs = _analysis(analysis_id="an_2", condition_id=TCS_DRY_0100,
                      condition_name="TCS 干沥青 0→100kph 全油门加速",
                      function_key="tcs", profile="4WD|DTCS",
                      file_id="fx_2", file_name="b.mf4")
    for d in (a_abs, a_tcs):
        eng.store.add_message(chat, "assistant", "analysis_result",
                              content="x", data=d)
    payload, msg = TOOLS["generate_report"].executor(
        eng, chat, {"condition_ids": [TCS_DRY_0100], "title": "测试"}, {})
    assert payload["ok"] is True and payload["analyses"] == 2
    text = _docx_text(os.path.join(str(tmp_path), "reports",
                                   chat.chat_id, payload["file"]))
    assert "4.1.1 ABS" in text and "5.1.1 TCS" in text


def test_history_injection_includes_report_kind():
    from brake_analyzer.llm.prompts import is_injected

    assert is_injected({"role": "assistant", "kind": "report", "content": "x"})
    assert not is_injected({"role": "assistant", "kind": "steps", "content": "x"})


def test_report_tool_without_config():
    eng = ChatEngine(CFG, NoLlm(),
                     KnowledgeBase(os.path.join(ROOT, "knowledge")), Store())
    chat = eng.store.create_chat()
    payload, msg = eng._report_tool(chat)
    assert payload["ok"] is False and msg is None
    assert "未配置" in payload["note"]


def test_report_tool_without_analyses(tmp_path):
    eng = _engine(tmp_path)
    chat = eng.store.create_chat()
    payload, msg = eng._report_tool(chat, condition_ids=[ABS_DRY_100])
    assert payload["ok"] is False and msg is None
    assert "分析" in payload["note"] and ABS_DRY_100 in payload["note"]


def test_report_tool_generates_docx_and_message(tmp_path):
    """端到端：落库一条 analysis_result 消息 → _report_tool 出 docx + report 消息。"""
    eng = _engine(tmp_path)
    chat = eng.store.create_chat()
    eng.store.add_message(chat, "assistant", "analysis_result",
                          content="x", data=_analysis())
    payload, msg = eng._report_tool(chat, title="测试报告")
    assert payload["ok"] is True and payload["analyses"] == 1
    assert payload["download"] == f"/api/reports/{chat.chat_id}/{payload['file']}"
    assert msg.kind == "report" and msg.data["download"] == payload["download"]
    assert os.path.isfile(os.path.join(str(tmp_path), "reports",
                                       chat.chat_id, payload["file"]))
    # 无数据文件（图跳过）但告警进了 payload 与消息 data
    assert payload["warnings"] and msg.data["warnings"] == payload["warnings"]


def test_report_tool_embeds_charts(tmp_path):
    """信号在缓存时：报告内嵌时序图，且 chart_count 随消息落库。"""
    eng = _engine(tmp_path)
    chat = eng.store.create_chat()
    eng.store.add_message(chat, "assistant", "analysis_result",
                          content="x", data=_analysis())
    chat.files.append(FileEntry(file_id="fx_1", name="demo.mf4",
                                path="nonexistent.mf4", size=0))
    eng.store.session("fx_1").signals = _signals()
    payload, msg = eng._report_tool(chat)
    assert payload["ok"] is True and payload["charts"] == 1
    assert msg.data["chart_count"] == 1
    assert not payload.get("warnings")


def test_report_tool_honors_report_template_env(tmp_path, monkeypatch):
    """REPORT_TEMPLATE 指向自定义模板时，输出用自定义版式（静态段落原样保留）。"""
    custom = str(tmp_path / "custom.docx")
    from brake_analyzer.report.generator import DEFAULT_TEMPLATE
    doc = Document(DEFAULT_TEMPLATE)
    doc.paragraphs[0].insert_paragraph_before("自定义封面")
    doc.save(custom)
    monkeypatch.setenv("REPORT_TEMPLATE", custom)

    eng = _engine(tmp_path)
    chat = eng.store.create_chat()
    eng.store.add_message(chat, "assistant", "analysis_result",
                          content="x", data=_analysis())
    payload, msg = eng._report_tool(chat)
    assert payload["ok"] is True
    text = _docx_text(os.path.join(str(tmp_path), "reports",
                                   chat.chat_id, payload["file"]))
    assert "自定义封面" in text


def test_template_tables_use_fixed_layout_and_repeat_header():
    """内置模板的表格固定列宽（避免长英文键断词）且表头跨页重复。"""
    import zipfile

    path = os.path.join(ROOT, "brake_analyzer", "report", "report_template.docx")
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    assert xml.count('w:type="fixed"') == 1          # 只剩客观指标表，固定列宽
    assert xml.count("<w:tblHeader") == 1            # 表头行跨页重复


def test_report_tool_survives_failure(tmp_path):
    """模板缺失时工具报可读错误而不是把整轮炸成 failed。"""
    import brake_analyzer.agent.engine as eng_mod

    eng = _engine(tmp_path)
    chat = eng.store.create_chat()
    eng.store.add_message(chat, "assistant", "analysis_result",
                          content="x", data=_analysis())
    saved = eng_mod.DEFAULT_TEMPLATE
    eng_mod.DEFAULT_TEMPLATE = str(tmp_path / "nope.docx")
    try:
        payload, msg = eng._report_tool(chat)
    finally:
        eng_mod.DEFAULT_TEMPLATE = saved
    assert payload["ok"] is False and msg is None
    assert "报告生成失败" in payload["note"]


# ---------------- LLM 编排流（工具回路） ----------------

def test_llm_round_emits_report_message(tmp_path):
    """tool-calling 循环：generate_report 的下载卡片要作为 message 事件流出。"""
    class ScriptLlm:
        available = True

        def __init__(self):
            self.rounds = [("generate_report", {"title": "流式报告"})]

        def chat_stream(self, messages, tools=None, **kw):
            if self.rounds:
                name, args = self.rounds.pop(0)
                yield {"type": "finish", "reply": LlmReply(tool_calls=[
                    {"id": "c1", "name": name, "arguments": args}])}
            else:
                yield {"type": "finish", "reply": LlmReply(content="报告已生成。")}

    eng = _engine(tmp_path)
    eng.llm = ScriptLlm()
    chat = eng.store.create_chat()
    eng.store.add_message(chat, "assistant", "analysis_result",
                          content="x", data=_analysis())
    kinds = []
    for ev in eng.events(chat, "生成报告"):
        if ev["event"] == "message":
            kinds.append(ev["data"]["kind"])
            if ev["data"]["kind"] == "report":
                assert ev["data"]["data"]["download"]
    assert "report" in kinds
