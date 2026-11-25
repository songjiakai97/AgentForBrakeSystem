"""驱动形式识别与按轴扭矩出图（design.md §9.5）。

三条主线：
1. 编码 → 驱动形式（前驱/后驱/四驱/其他）；
2. DMC 目标扭矩存在且有数据 → DTCS，否则 TCS；显式档位优先；
3. 图表按驱动形式出 1 或 2 根轴，每轴「实际扭矩 + 目标扭矩」两条线。
"""

import os
import shutil

import pytest
import yaml

from brake_analyzer.configs import ConfigError, load_configs
from brake_analyzer.drive import (
    drive_chart_groups,
    drive_info,
    infer_tokens,
    layout_with_drive,
    read_drive_code,
)
from brake_analyzer.pipeline import make_loader

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "configs")
MF4_DIR = os.path.join(ROOT, "tests", "data", "mf4")


@pytest.fixture(scope="module")
def cfg():
    return load_configs(CONFIG_DIR)


def _signals(fname):
    path = os.path.join(MF4_DIR, fname)
    if not os.path.exists(path):
        pytest.skip(f"demo 数据缺失：{path}")
    return make_loader(path, cfg_from_dir()).load(path)


def cfg_from_dir():
    return load_configs(CONFIG_DIR)


# ---------------------------------------------------------------- 配置

def test_drive_config_declares_all_codes(cfg):
    d = cfg.drive
    assert d is not None and d.function == "tcs" and d.signal == "VehDriveType"
    assert [d.types[c]["layout"] for c in (0x1, 0x2, 0x3)] == ["awd"] * 3
    assert d.types[0x4]["layout"] == "front"
    assert d.types[0x6]["layout"] == "front"
    assert d.types[0x7]["layout"] == "rear"
    assert d.types[0x5]["layout"] == "other"
    assert d.types[0x0]["layout"] == "other"
    assert {d.types[c]["layout"] for c in range(0x9, 0x10)} == {"other"}
    assert d.layouts == {"awd": "4WD", "front": "2WD", "rear": "2WD"}
    # 轴扭矩不被静态 chart_layout 收录（否则 ABS 也会多出空行）
    placed = {s for g in cfg.chart_layout for s in g.signals}
    assert not (set(d.dynamic_signals()) & placed)


def test_chart_layout_rejects_drive_signals(tmp_path):
    dst = tmp_path / "configs"
    shutil.copytree(CONFIG_DIR, dst)
    p = dst / "signals.yaml"
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    raw["chart_layout"][0]["signals"].append("VehDriveType")
    p.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(ConfigError, match="动态出图"):
        load_configs(str(dst))


def test_drive_torque_signals_must_be_in_function_signals(tmp_path):
    dst = tmp_path / "configs"
    shutil.copytree(CONFIG_DIR, dst)
    p = dst / "functions.yaml"
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    tcs = next(f for f in raw["functions"] if f["key"] == "tcs")
    tcs["signals"] = [s for s in tcs["signals"] if s != "FrontMotorTorque"]
    p.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(ConfigError, match="按轴出图"):
        load_configs(str(dst))


# ---------------------------------------------------------------- 识别

def test_awd_demo_identifies_dtcs(cfg):
    signals = _signals("tcs_wet60_normal.mf4")
    assert read_drive_code(cfg, signals) == 0x1
    info = drive_info(cfg, signals)
    assert info.layout == "awd" and info.known
    assert info.dtcs and info.target_kind == "DTCS"
    assert infer_tokens(cfg, signals) == {"layout": "4WD", "assist": "DTCS"}


def test_abs_demo_has_no_drive_layout(cfg):
    signals = _signals("abs_dry100_normal.mf4")
    info = drive_info(cfg, signals)
    assert info.layout == "other" and not info.known
    assert drive_chart_groups(cfg, "abs", signals) == []


def test_explicit_profile_wins_over_data(cfg):
    signals = _signals("tcs_wet60_normal.mf4")
    info = drive_info(cfg, signals, "4WD|TCS")
    assert info.layout == "awd" and info.target_kind == "TCS"
    # 2WD 没说是哪根轴，数据是四驱 → 两根都画，但目标扭矩取 TCS
    info2 = drive_info(cfg, signals, "2WD|TCS")
    assert info2.layout == "both" and info2.target_kind == "TCS"


def test_dtcs_requires_non_empty_dmc(cfg):
    """DMC 信号存在但空序列 → 不能判 DTCS。"""
    signals = {k: v for k, v in _signals("tcs_wet60_normal.mf4").items()}

    class Empty:
        ts = []
        values = []
        unit = "N.m"

    signals["DMCTarTorqueFA"] = Empty()
    signals["DMCTarTorqueRA"] = Empty()
    assert infer_tokens(cfg, signals)["assist"] == "TCS"
    assert drive_info(cfg, signals).target_kind == "TCS"


# ---------------------------------------------------------------- 出图

def test_awd_has_two_axle_rows(cfg):
    signals = _signals("tcs_wet60_normal.mf4")
    groups = drive_chart_groups(cfg, "tcs", signals)
    assert [g.label for g in groups] == ["前轴扭矩（DTCS）", "后轴扭矩（DTCS）"]
    assert groups[0].signals == ["FrontMotorTorque", "DMCTarTorqueFA"]
    assert groups[1].signals == ["RearMotorTorque", "DMCTarTorqueRA"]
    assert {g.unit for g in groups} == {"N.m"}
    # 非驱动功能不出扭矩轴
    assert drive_chart_groups(cfg, "abs", signals) == []


def test_front_and_rear_layouts_one_row(cfg):
    signals = _signals("tcs_wet60_normal.mf4")
    # 显式 2WD 换成前驱/后驱数据形状时只画一根轴：直接按 code 构造信号
    front = dict(signals)
    front["VehDriveType"] = _const_like(signals["VehDriveType"], 0x6)
    groups = drive_chart_groups(cfg, "tcs", front)
    assert [g.label for g in groups] == ["前轴扭矩（DTCS）"]
    assert groups[0].signals == ["FrontMotorTorque", "DMCTarTorqueFA"]

    rear = dict(signals)
    rear["VehDriveType"] = _const_like(signals["VehDriveType"], 0x7)
    groups = drive_chart_groups(cfg, "tcs", rear)
    assert [g.label for g in groups] == ["后轴扭矩（DTCS）"]
    assert groups[0].signals == ["RearMotorTorque", "DMCTarTorqueRA"]


def test_other_drive_type_has_no_torque_rows(cfg):
    signals = _signals("tcs_wet60_normal.mf4")
    signals = dict(signals)
    signals["VehDriveType"] = _const_like(signals["VehDriveType"], 0x5)   # 纯燃油
    assert drive_chart_groups(cfg, "tcs", signals) == []


def _const_like(sig, value):
    import numpy as np

    class S:
        ts = sig.ts
        values = np.full(len(sig.ts), value, dtype=float)
        unit = ""

    return S()


def test_layout_with_drive_keeps_static_rows_first(cfg):
    signals = _signals("tcs_wet60_normal.mf4")
    layout = layout_with_drive(cfg, "tcs", signals, "4WD|DTCS", cfg.chart_layout)
    assert layout[:len(cfg.chart_layout)] == list(cfg.chart_layout)
    assert [g.label for g in layout[len(cfg.chart_layout):]] == \
        ["前轴扭矩（DTCS）", "后轴扭矩（DTCS）"]


# ---------------------------------------------------------------- 引擎接入

def _engine(tmp_path):
    from types import SimpleNamespace

    from brake_analyzer.agent.engine import ChatEngine
    from brake_analyzer.llm.kb import KnowledgeBase
    from web.store import Store

    return ChatEngine(load_configs(CONFIG_DIR), SimpleNamespace(available=False),
                      KnowledgeBase(os.path.join(ROOT, "knowledge")),
                      Store(upload_dir=str(tmp_path / "uploads")))


def test_engine_resolves_profile_from_data(cfg, tmp_path):
    path = os.path.join(MF4_DIR, "tcs_wet60_normal.mf4")
    if not os.path.exists(path):
        pytest.skip("demo 数据缺失")
    eng = _engine(tmp_path)
    chat = eng.store.create_chat()
    eng.store.add_file(chat, "tcs.mf4", open(path, "rb").read())
    cond = "tcs_full_throttle_wet_basalt_0to60kph"
    profile, note, gap = eng._resolve_profile(chat, cond, None)
    assert gap is None and profile == "4WD|DTCS"
    assert "4WD|DTCS" in note


def test_engine_falls_back_to_panel_without_drive_info(cfg, tmp_path):
    path = os.path.join(MF4_DIR, "abs_dry100_normal.mf4")
    if not os.path.exists(path):
        pytest.skip("demo 数据缺失")
    eng = _engine(tmp_path)
    chat = eng.store.create_chat()
    eng.store.add_file(chat, "abs.mf4", open(path, "rb").read())
    profile, _note, gap = eng._resolve_profile(
        chat, "tcs_full_throttle_dry_asphalt_0to100kph", None)
    assert profile is None
    assert gap and gap["hop"] == "profile"
    # 驱动形式（0x0）识别不出 4WD/2WD；但 ABS 数据里也带 DMC 通道，
    # 按「有 DMC 即 DTCS」预选这一维，用户只需补驱动形式
    assert gap.get("preset") == ["DTCS"]


class _ScriptLlm:
    """单次工具调用 → 文本收尾的最小 LLM 桩（只用于档位自动识别链路）。"""

    def __init__(self, rounds):
        self.available = True
        self.rounds = list(rounds)

    def chat_stream(self, messages, tools=None, **kw):
        from brake_analyzer.llm.client import LlmReply

        if self.rounds:
            name, args = self.rounds.pop(0)
            yield {"type": "finish", "reply": LlmReply(tool_calls=[
                {"id": "c1", "name": name, "arguments": args}])}
        else:
            yield {"type": "finish", "reply": LlmReply(content="已按识别出的档位完成分析。")}

    def complete_json(self, system, user, **kw):
        return None


def test_engine_llm_guard_auto_resolves_profile(cfg, tmp_path):
    """模型没给 profile 时，数据能识别就自动补档，不再弹面板打断用户。"""
    from brake_analyzer.agent.engine import ChatEngine
    from brake_analyzer.llm.kb import KnowledgeBase
    from web.store import Store

    path = os.path.join(MF4_DIR, "tcs_wet60_normal.mf4")
    if not os.path.exists(path):
        pytest.skip("demo 数据缺失")
    cond = "tcs_full_throttle_wet_basalt_0to60kph"
    llm = _ScriptLlm([("run_analysis_on_files", {"condition_id": cond})])
    eng = ChatEngine(load_configs(CONFIG_DIR), llm,
                     KnowledgeBase(os.path.join(ROOT, "knowledge")),
                     Store(upload_dir=str(tmp_path / "uploads")))
    chat = eng.store.create_chat()
    eng.store.add_file(chat, "tcs.mf4", open(path, "rb").read())
    for _ in eng.events(chat, "跑一下这个 TCS 工况"):
        pass
    kinds = [m.kind for m in chat.messages]
    assert "analysis_result" in kinds
    assert "target_options" not in kinds
    card = next(m for m in chat.messages if m.kind == "analysis_result")
    assert card.data["profile"] == "4WD|DTCS"
    labels = [it["label"] for m in chat.messages if m.kind == "steps"
              for it in (m.data or {}).get("items", [])]
    assert "驱动形式识别" in labels and "档位守卫：暂不分析" not in labels
