"""内核测试：配置校验 / 分段交叉点 / 指标判定 / pick 两维匹配 / 规则覆盖 / 端到端场景。"""

import math
import os
import warnings

import numpy as np
import pytest

warnings.filterwarnings("ignore", module="asammdf")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "configs")
DATA = os.path.join(ROOT, "tests", "data", "mf4")

from brake_analyzer.configs import ConfigError, load_configs, pick  # noqa: E402
from brake_analyzer.events.segment import _crossings, _gate_intervals  # noqa: E402
from brake_analyzer.loaders.base import SignalData  # noqa: E402
from brake_analyzer.pipeline import analyze_file  # noqa: E402


@pytest.fixture(scope="module")
def cfg():
    return load_configs(CONFIG_DIR)


def _sig(ts, vals, unit=""):
    return SignalData(ts=np.asarray(ts, float), values=np.asarray(vals, float), unit=unit)


# ---------------------------------------------------------------- 配置
def test_config_loads(cfg):
    assert set(cfg.functions) == {"abs", "tcs"}
    assert len(cfg.conditions) == 16
    assert len(cfg.rules) == 12
    assert cfg.functions["tcs"].profiles == ["4WD", "2WD", "DTCS", "TCS"]
    assert cfg.functions["tcs"].profile_dims == [["4WD", "2WD"], ["DTCS", "TCS"]]


def test_segmenter_registered(cfg):
    from brake_analyzer.events.segment import SEGMENTERS

    for fn in cfg.functions.values():
        assert fn.segmenter in SEGMENTERS


def test_bad_config_dir(tmp_path):
    with pytest.raises(ConfigError):
        load_configs(str(tmp_path))


def _copy_configs(tmp_path):
    import shutil

    d = str(tmp_path / "configs")
    if not os.path.isdir(d):
        shutil.copytree(CONFIG_DIR, d)
    return d


def _patch_yaml(d, name, mutate):
    import yaml

    path = os.path.join(d, name)
    raw = yaml.safe_load(open(path, encoding="utf-8"))
    mutate(raw)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)


def test_registry_is_single_source_of_unit(cfg):
    """单位/描述只在 signals.yaml 出现一次；MF4 侧合并后拿到的就是登记值。"""
    assert cfg.signal_registry["speed_vbox"]["unit"] == "km/h"
    assert cfg.signal_maps["speed_vbox"]["unit"] == "km/h"
    assert cfg.signal_maps["speed_vbox"]["description"] == "vbox 实际车速"


def test_required_signals_cover_metrics_and_segmenters(cfg):
    """计算必需集 = metrics.yaml inputs ∪ 分段器读取的通道（§9.6 覆盖度口径的依据）。"""
    from brake_analyzer.events.segment import SEGMENTER_SIGNALS

    want = {i for m in cfg.metrics.values() for i in m.inputs}
    for fn in cfg.functions.values():
        want |= set(SEGMENTER_SIGNALS.get(fn.segmenter, []))
    # 工况级覆盖的分段器（对接路面）读的通道也算必需
    for c in cfg.conditions.values():
        if c.segmenter:
            want |= set(SEGMENTER_SIGNALS.get(c.segmenter, []))
    assert set(cfg.required_signals) == want
    # 踏板门控与车速是切事件窗口的命脉，必须在里面
    assert {"speed_vbox", "BrakePedalPos", "ThrottlePedalPos"} <= set(cfg.required_signals)
    # 对接路面的 ABS 窗口靠状态位切，缺了切不出窗
    assert "ABS_Active" in cfg.required_signals
    # Ax 只用于看图，不参与计算 → 允许某一种格式不带
    assert "Ax" not in cfg.required_signals


def test_mf4_missing_required_signal_rejected(tmp_path):
    d = _copy_configs(tmp_path)
    _patch_yaml(d, "signals_mf4.yaml",
                lambda raw: raw["signal_maps"].pop("speed_vbox"))
    with pytest.raises(ConfigError, match="参与计算的信号"):
        load_configs(d)


def test_mf4_unregistered_logical_name_rejected(tmp_path):
    """MF4 侧也不能自创逻辑名：同一物理量两侧必须同名。"""
    d = _copy_configs(tmp_path)
    _patch_yaml(d, "signals_mf4.yaml",
                lambda raw: raw["signal_maps"].__setitem__(
                    "vbox_speed", {"candidates": ["vbox_speed"]}))
    with pytest.raises(ConfigError, match="未登记"):
        load_configs(d)


def test_moved_section_in_format_file_rejected(tmp_path):
    """chart_layout 搬到 signals.yaml 后，留在 signals_mf4.yaml 里必须报错，
    而不是"看着还生效"地静默失效。"""
    d = _copy_configs(tmp_path)
    _patch_yaml(d, "signals_mf4.yaml",
                lambda raw: raw.__setitem__("chart_layout", [{"signals": ["Ax"]}]))
    with pytest.raises(ConfigError, match="signals.yaml"):
        load_configs(d)


def test_observation_only_signal_may_appear_in_one_format_only(tmp_path):
    """MF4 侧少带一个纯观测量是合法的（两侧信号集不必相同）。"""
    d = _copy_configs(tmp_path)
    _patch_yaml(d, "signals_mf4.yaml", lambda raw: raw["signal_maps"].pop("Ax"))
    cfg2 = load_configs(d)
    assert "Ax" not in cfg2.signal_maps
    assert "Ax" in cfg2.signal_registry          # 仍登记在册，只是这个格式没候选
    assert "Ax" not in cfg2.required_signals
    # 图表分组里那一行仍在（按登记表编排），运行时缺通道由画图层跳过
    assert any("Ax" in g.signals for g in cfg2.chart_layout)


def test_chart_layout_lives_in_registry_file(cfg):
    """分组按逻辑信号名写，与数据格式无关 → 归属 signals.yaml。"""
    raw = __import__("yaml").safe_load(
        open(os.path.join(CONFIG_DIR, "signals.yaml"), encoding="utf-8"))
    assert raw.get("chart_layout"), "chart_layout 应在 signals.yaml"
    mf4 = __import__("yaml").safe_load(
        open(os.path.join(CONFIG_DIR, "signals_mf4.yaml"), encoding="utf-8"))
    assert "chart_layout" not in mf4
    assert cfg.chart_layout[0].label == "车速 / 轮速"
    # 未编排的信号自动补为独立行，图表不静默丢通道
    placed = {s for g in cfg.chart_layout for s in g.signals}
    assert set(cfg.signal_registry) == placed



# ---------------------------------------------------------------- 交叉点插值
def test_crossings_linear():
    ts = [0.0, 1.0, 2.0]
    vals = [10.0, 4.0, 0.0]
    # 下降穿越 6 kph: 在 0~1s 间, 10→4, frac=(6-10)/(4-10)=2/3 → t=0.667
    (t,) = _crossings(np.array(ts), np.array(vals), 6.0, "down")
    assert math.isclose(t, 2 / 3, abs_tol=1e-9)
    # 上升
    ts2 = [0.0, 1.0]
    vals2 = [0.0, 2.0]
    (t2,) = _crossings(np.array(ts2), np.array(vals2), 0.8, "up")
    assert math.isclose(t2, 0.4, abs_tol=1e-9)


def test_gate_expands_lead():
    signals = {"p": _sig([0, 1, 2, 3, 4], [0, 90, 90, 0, 0])}
    (a, b), = _gate_intervals(signals, "p", 80)
    assert a <= 1.0 and b >= 3.0
    assert a < 1.0  # 首端有放宽


# ---------------------------------------------------------------- 信号区间切窗
def test_high_intervals_merges_state_pulses():
    """状态位按脉冲发布：间隙小于 merge_gap 的 0/1 跳动算同一段。"""
    from brake_analyzer.events.segment import _high_intervals

    signals = {"f": _sig([0, 1, 2, 3, 4], [0, 1, 0, 1, 0])}
    (a, b), = _high_intervals(signals, "f", merge_gap=2.5)
    assert (a, b) == (1.0, 3.0)
    assert len(_high_intervals(signals, "f")) == 2       # 不合并就是两段
    # 单个毛刺采样点（持续 0s）被 min_dur 丢弃
    spike = {"f": _sig([0, 1, 2], [0, 1, 0])}
    assert _high_intervals(spike, "f", min_dur=0.2) == []


def test_abs_active_window_from_flag(cfg):
    """对接路面 ABS：窗口 = ABS_Active 高电平区间，与车速无关。"""
    from brake_analyzer.events.segment import seg_abs_active

    t = np.arange(0.0, 6.0, 0.01)
    flag = ((t >= 2.0) & (t <= 3.5)).astype(float)
    (w,) = seg_abs_active({"ABS_Active": _sig(t, flag)}, None, None, {})
    assert math.isclose(w.t_start, 2.0, abs_tol=0.02)
    assert math.isclose(w.t_end, 3.5, abs_tol=0.02)
    assert w.phases == ["abs_regulation"]


def test_tcs_pedal_time_is_exact_throttle_span(cfg):
    """对接路面 TCS：窗口 = 全油门时间，端点不外扩（否则时长就虚了）。"""
    from brake_analyzer.events.segment import seg_tcs_pedal_time

    t = np.arange(0.0, 5.0, 0.01)
    ped = np.where((t >= 1.0) & (t <= 4.0), 100.0, 0.0)
    (w,) = seg_tcs_pedal_time({"ThrottlePedalPos": _sig(t, ped)}, None, None,
                              {"pedal_arm_pct": 95})
    assert math.isclose(w.t_start, 1.0, abs_tol=0.02)
    assert math.isclose(w.t_end, 4.0, abs_tol=0.02)


def test_condition_segmenter_override_is_used(cfg):
    """工况级 segmenter 覆盖功能默认模板，指标只在 ABS 激活区间内算。"""
    from brake_analyzer.pipeline import analyze_signals

    cond = cfg.condition("abs_full_brake_pos_jump_50kph")
    assert cond.segmenter == "seg_abs_active"
    t = np.arange(0.0, 6.0, 0.01)
    active = ((t >= 2.0) & (t <= 3.0)).astype(float)
    signals = {
        "speed_vbox": _sig(t, np.clip(50 - 10 * t, 0, None)),
        "distance_vbox": _sig(t, t * 10),
        "ABS_Active": _sig(t, active),
        # 窗外 20 度/秒的横摆不得计入：窗口起点若仍按车速穿越取，这里会露馅
        "yaw_rate": _sig(t, np.where(active > 0, 3.0, 20.0)),
    }
    res = analyze_signals(signals, cond.id, cfg)
    s = res.samples[0]
    assert math.isclose(s.window.t_start, 2.0, abs_tol=0.02)
    assert math.isclose(s.window.t_end, 3.0, abs_tol=0.02)
    m = {x.key.rsplit(".", 1)[1]: x for x in s.metrics}
    assert m["yaw_rate_max"].value == pytest.approx(3.0, abs=0.05)


def test_jump_missing_flag_channel_says_signal_absent(cfg):
    """缺 ABS_Active → missing 且成因是通道缺失，不是「试验没做出来」。"""
    from brake_analyzer.pipeline import analyze_signals

    t = np.arange(0.0, 4.0, 0.01)
    signals = {"speed_vbox": _sig(t, np.clip(30 - 8 * t, 0, None)),
               "yaw_rate": _sig(t, np.zeros_like(t))}
    res = analyze_signals(signals, "abs_full_brake_pos_jump_50kph", cfg)
    (m,) = res.samples[0].metrics
    assert m.status == "missing" and m.missing_reason == "signal_absent"


def test_jump_flag_never_high_says_no_valid_event(cfg):
    """通道在但 ABS 从未激活 → 确实是没切出事件，不能报成缺信号。"""
    from brake_analyzer.pipeline import analyze_signals

    t = np.arange(0.0, 4.0, 0.01)
    signals = {"speed_vbox": _sig(t, np.clip(30 - 8 * t, 0, None)),
               "ABS_Active": _sig(t, np.zeros_like(t)),
               "yaw_rate": _sig(t, np.zeros_like(t))}
    res = analyze_signals(signals, "abs_full_brake_pos_jump_50kph", cfg)
    (m,) = res.samples[0].metrics
    assert m.status == "missing" and m.missing_reason == "no_valid_event"


def test_condition_segmenter_must_be_registered(tmp_path):
    d = _copy_configs(tmp_path)
    _patch_yaml(d, "conditions.yaml",
                lambda raw: raw["abs"][0].__setitem__("segmenter", "seg_nope"))
    with pytest.raises(ConfigError, match="未注册"):
        load_configs(d)


def test_signal_window_rejects_speed_span_metrics(tmp_path):
    """窗口不是整段速度区间时，「制动距离/加速时间」的物理含义不成立。"""
    d = _copy_configs(tmp_path)
    _patch_yaml(d, "conditions.yaml",
                lambda raw: raw["abs"][0].__setitem__("segmenter", "seg_abs_active"))
    with pytest.raises(ConfigError, match="不能声明指标"):
        load_configs(d)


# ---------------------------------------------------------------- pick
def test_pick_two_dim_substring(cfg):
    cond = cfg.condition("tcs_full_throttle_wet_basalt_0to60kph")
    idx = cond.metric_index
    # 通用条目
    assert pick(idx, "yaw_rate_max", "4WD|DTCS")["ok_range"] == [None, 5]
    # 两维同时命中
    e1 = pick(idx, "acc_avg", "4WD|DTCS")
    assert e1["ok_range"] == [1.6, None]
    e2 = pick(idx, "slip_max", "4WD|DTCS")
    assert e2["ok_range"] == [None, 36]
    e3 = pick(idx, "acc_avg", "2WD|TCS")
    assert e3["ok_range"] == [0.8, None]
    e4 = pick(idx, "slip_max", "2WD|TCS")
    assert e4["ok_range"] == [None, 54]
    # 无通用 + 不匹配 → None → missing
    assert pick(idx, "acc_avg", None) is None
    # 反向子串兼容（单标量复合标签）
    assert pick(idx, "acc_avg", "4WD_DTCS")["ok_range"] == [1.6, None]


# ---------------------------------------------------------------- 端到端
def _analyze(cfg, fname, cid, profile=None):
    return analyze_file(os.path.join(DATA, fname), cid, cfg, file_name=fname, profile=profile)


def _metrics(res):
    return {m.key.rsplit(".", 1)[1]: m for m in res.samples[0].metrics}


ABS100 = "abs_full_brake_dry_asphalt_100kph"
ABS60 = "abs_full_brake_wet_basalt_60kph"
ABS50 = "abs_full_brake_wet_tile_50kph"
TCS100 = "tcs_full_throttle_dry_asphalt_0to100kph"
TCS60 = "tcs_full_throttle_wet_basalt_0to60kph"
TCS50 = "tcs_full_throttle_wet_tile_0to50kph"


def test_normal_samples_all_ok(cfg):
    for f, cid, prof in [
        ("abs_dry100_normal.mf4", ABS100, None),
        ("abs_wet60_normal.mf4", ABS60, None),
        ("abs_wet50_normal.mf4", ABS50, None),
        ("tcs_dry100_normal.mf4", TCS100, None),
        ("tcs_wet60_normal.mf4", TCS60, "4WD|DTCS"),
        ("tcs_wet50_normal.mf4", TCS50, "2WD|TCS"),
    ]:
        res = _analyze(cfg, f, cid, prof)
        ms = _metrics(res)
        assert all(m.status == "ok" for m in ms.values()), (f, {k: v.status for k, v in ms.items()})
        assert res.samples[0].verdicts == []


def test_violations(cfg):
    res = _analyze(cfg, "abs_dry100_yaw_abn.mf4", ABS100)
    assert _metrics(res)["yaw_rate_max"].status == "abnormal"
    assert {v.rule_id for v in res.samples[0].verdicts} == {"abs_yaw_abnormal"}

    res = _analyze(cfg, "abs_dry100_dist_abn.mf4", ABS100)
    assert _metrics(res)["brake_distance"].status == "abnormal"
    assert {v.rule_id for v in res.samples[0].verdicts} == {"abs_dist_abnormal"}

    res = _analyze(cfg, "abs_wet60_decel_abn.mf4", ABS60)
    assert _metrics(res)["decel_avg"].status == "abnormal"

    res = _analyze(cfg, "tcs_dry100_time_abn.mf4", TCS100)
    assert _metrics(res)["acc_time"].status == "abnormal"

    res = _analyze(cfg, "tcs_wet60_acc_abn.mf4", TCS60, "4WD|DTCS")
    ms = _metrics(res)
    assert ms["acc_avg"].status == "abnormal" and ms["acc_avg"].profile == "4WD"

    res = _analyze(cfg, "tcs_wet60_slip_abn.mf4", TCS60, "4WD|DTCS")
    ms = _metrics(res)
    assert ms["slip_max"].status == "abnormal" and ms["slip_max"].profile == "DTCS"

    res = _analyze(cfg, "tcs_wet50_slip_abn.mf4", TCS50, "2WD|TCS")
    assert _metrics(res)["slip_max"].status == "abnormal"


def test_brake_distance_from_v0_not_data_start(cfg):
    """105 kph 起踩：窗口起点必须是 100 kph 下降插值穿越点（t≈2+5/41.4），
    制动距离应为 100→0.8 段而非 105→0.8 段。"""
    res = _analyze(cfg, "abs_dry100_normal.mf4", ABS100)
    s = res.samples[0]
    assert math.isclose(s.window.t_start, 2.0 + 5.0 / (11.5 * 3.6), abs_tol=0.05)
    dist = _metrics(res)["brake_distance"].value
    ideal_100 = (100 ** 2 - 0.8 ** 2) / 3.6 ** 2 / (2 * 11.5)
    ideal_105 = (105 ** 2 - 0.8 ** 2) / 3.6 ** 2 / (2 * 11.5)
    assert abs(dist - ideal_100) < 2.0, (dist, ideal_100)
    assert abs(dist - ideal_100) < abs(dist - ideal_105)


def test_acc_time_from_08_crossing(cfg):
    """静止起步：窗口起点 = 0.8 kph 上升穿越（≈ press_t + 0.8/3.6/acc）。"""
    res = _analyze(cfg, "tcs_dry100_normal.mf4", TCS100)
    s = res.samples[0]
    t_expect = 2.5 + (0.8 / 3.6) / 8.0
    assert math.isclose(s.window.t_start, t_expect, abs_tol=0.05)
    dur = _metrics(res)["acc_time"].value
    t_end_expect = 2.5 + (100 / 3.6) / 8.0  # 上升穿越 v_target=100
    assert math.isclose(dur, t_end_expect - t_expect, abs_tol=0.1)


def test_missing_channel_rule(cfg):
    res = _analyze(cfg, "abs_wet50_no_yaw.mf4", ABS50)
    ms = _metrics(res)
    assert ms["yaw_rate_max"].status == "missing"
    assert ms["yaw_rate_max"].missing_reason == "signal_absent"
    v = res.samples[0].verdicts
    assert len(v) == 1 and v[0].rule_id == "abs_yaw_missing"
    assert v[0].fault_domain == "data_quality"
    # 结论不能把成因写死成「信号缺失」之外的东西，也不该不提成因
    assert "不可算" in v[0].message
    assert "通道" in v[0].direction or "未映射" in v[0].direction


def test_no_event_and_no_signal_have_different_reasons(cfg):
    """「没切出事件」与「缺通道」必须可区分：给用户的建议是相反的。

    两者过去都是 status=missing、值都是 None，模型与用户只能猜，于是
    「这趟试验没做出来」被说成「传感器没装/信号缺失」（design.md §10.1）。
    """
    no_event = _metrics(_analyze(cfg, "abs_dry100_no_v0.mf4", ABS100))
    no_signal = _metrics(_analyze(cfg, "abs_wet50_no_yaw.mf4", ABS50))
    assert no_event["brake_distance"].missing_reason == "no_valid_event"
    assert no_signal["yaw_rate_max"].missing_reason == "signal_absent"
    # 同一指标在两种成因下状态相同、成因不同 —— 说明区分靠的是 reason 不是 status
    assert no_event["brake_distance"].status == no_signal["yaw_rate_max"].status == "missing"
    assert no_event["brake_distance"].value is None


def test_degenerate_window_card_says_not_signal_absence(cfg):
    """结果卡片标题要点明「未切出有效事件（非信号缺失）」，不能只说「N 项缺失」。"""
    from brake_analyzer.llm.prompts import digest_line

    res = _analyze(cfg, "abs_dry100_no_v0.mf4", ABS100)
    d = res.to_dict()
    line = digest_line(d)
    assert "未切出有效事件" in line, line
    assert "信号缺失" not in line


def test_missing_reason_omitted_when_computable(cfg):
    res = _analyze(cfg, "abs_dry100_dist_abn.mf4", ABS100)
    d = res.to_dict()
    m = [x for s in d["samples"] for x in s["metrics"]][0]
    assert m["status"] != "missing" and "missing_reason" not in m


def test_unreachable_crossover_missing(cfg):
    for f, cid in [("abs_dry100_no_stop.mf4", ABS100), ("abs_dry100_no_v0.mf4", ABS100)]:
        res = _analyze(cfg, f, cid)
        ms = _metrics(res)
        assert ms["brake_distance"].status == "missing", f
    res = _analyze(cfg, "tcs_wet60_never_starts.mf4", TCS60, "4WD|DTCS")
    ms = _metrics(res)
    assert all(m.status == "missing" for m in ms.values())


def test_decel_avg_equals_window_slope(cfg):
    res = _analyze(cfg, "abs_wet60_decel_abn.mf4", ABS60)
    m = _metrics(res)["decel_avg"]
    assert math.isclose(m.value, 1.2, abs_tol=0.15)


def test_slip_unit_kmh(cfg):
    res = _analyze(cfg, "tcs_wet60_slip_abn.mf4", TCS60, "4WD|DTCS")
    m = _metrics(res)["slip_max"]
    # 合成 slip_peak=50 衰减，量级应为几十 km/h 而非 0~1 或 m/s
    assert 10 < m.value < 60


def test_slip_vbox_interpolated_over_full_series():
    """车速用整条序列 np.interp 到参考轴：窗口边缘由窗外样本括住，
    不再被窗口内子段钳位；参考轴越出车速范围则判 missing 而非静默错值。"""
    from brake_analyzer.metrics.tools import wheel_slip_max

    window = (1.0, 2.0)
    wheels = {
        f"wheelSpeed_{p}": _sig(np.linspace(1.0, 2.0, 11), np.full(11, 60.0))
        for p in ("FL", "FR", "RL", "RR")
    }

    # 车速稀疏：窗口内只有一个 50 km/h 样本，但窗外是 10/90 —— 旧口径会把它
    # 钳成常数 50，得 slip=10；新口径按整条序列插值。
    sparse = dict(wheels)
    sparse["speed_vbox"] = _sig([0.0, 1.5, 3.0], [10.0, 50.0, 90.0])
    out = wheel_slip_max(sparse, None, window, {"signals": list(sparse)})
    # slip 在窗口起点最大：vbox(1.0) = 10 + (1.0/1.5)*(50-10) = 36.67
    assert out["value"] == pytest.approx(60.0 - (10.0 + 40.0 / 1.5))

    # 参考轴越出车速时间范围 → 明确不可算，而不是钳到端点值
    outside = dict(wheels)
    outside["speed_vbox"] = _sig([2.2, 3.0], [70.0, 90.0])
    out = wheel_slip_max(outside, None, window, {"signals": list(outside)})
    assert out["value"] is None and out["reason"] == "out_of_range:speed_vbox"

    # 车速通道缺失 → missing
    no_vbox = dict(wheels)
    out = wheel_slip_max(no_vbox, None, window, {"signals": list(no_vbox)})
    assert out["value"] is None and out["reason"] == "missing:speed_vbox"
