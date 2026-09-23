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
    assert len(cfg.conditions) == 6
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
    v = res.samples[0].verdicts
    assert len(v) == 1 and v[0].rule_id == "abs_yaw_missing"
    assert v[0].fault_domain == "data_quality"


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
