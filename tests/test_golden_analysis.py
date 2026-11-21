"""金标准回归：demo 文件 × 标准工况的分析指标值固化。

这是 segmenter/metrics/rules 物理计算逻辑的**数值级**护栏——test_kernel 只断言
结构（有没有指标、状态对不对），这里断言具体数值：任何切窗/计算改动导致的
漂移都会在这里暴露。demo 数据由 scripts/generate_demo_mf4.py 确定性合成，
数值可复现；有意改变计算口径时，先确认物理合理性再更新本文件的 GOLDEN。

abs_wet60_normal 的 decel_avg 标 abnormal 是当前行为的固化：demo 合成的减速度
(6.5 m/s²) 低于湿沥青限值（≥8.5）——文件名里的 normal 指数据完整性，不是判定。
"""

import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "configs")
MF4_DIR = os.path.join(ROOT, "tests", "data", "mf4")

from brake_analyzer.configs import load_configs
from brake_analyzer.loaders.mf4 import MF4Loader
from brake_analyzer.pipeline import analyze_signals

CFG = load_configs(CONFIG_DIR)
_LOADER = MF4Loader(CFG.signal_maps)

ABS100 = "abs_full_brake_dry_asphalt_100kph"
ABS_WET60 = "abs_full_brake_wet_asphalt_60kph"
TCS100 = "tcs_full_throttle_dry_asphalt_0to100kph"
TCS_WET50 = "tcs_full_throttle_wet_tile_0to50kph"

GOLDEN = {
    ("abs_dry100_normal.mf4", ABS100, None): {
        "yaw_rate_max": (2.131, "ok"), "brake_distance": (33.708, "ok")},
    ("abs_dry100_dist_abn.mf4", ABS100, None): {
        "yaw_rate_max": (2.255, "ok"), "brake_distance": (85.655, "abnormal")},
    ("abs_wet60_normal.mf4", ABS_WET60, None): {
        "yaw_rate_max": (1.649, "ok"), "decel_avg": (6.501, "abnormal")},
    ("tcs_dry100_normal.mf4", TCS100, "4WD|DTCS"): {
        "yaw_rate_max": (2.248, "ok"), "acc_time": (3.444, "ok")},
    ("tcs_wet50_slip_abn.mf4", TCS_WET50, "4WD|DTCS"): {
        "yaw_rate_max": (1.686, "ok"), "acc_avg": (1.2, "ok"),
        "slip_max": (58.294, "abnormal")},
}


@pytest.mark.parametrize("case", sorted(GOLDEN))
def test_golden_metrics(case):
    fname, cid, profile = case
    path = os.path.join(MF4_DIR, fname)
    if not os.path.exists(path):
        pytest.skip("demo 数据未生成")
    sig = _LOADER.load(path)
    res = analyze_signals(sig, cid, CFG, file_id="golden", file_name=fname,
                          profile=profile)
    golden = GOLDEN[case]
    current = {}
    for s in res.samples:
        for m in s.metrics:
            current.setdefault(m.key.rsplit(".", 1)[1],
                               (m.value, m.status))
    for key, (want_v, want_status) in golden.items():
        assert key in current, f"{fname}: 指标 {key} 缺失（被新增/改名？同步 GOLDEN）"
        got_v, got_status = current[key]
        assert got_status == want_status, \
            f"{fname}.{key}: 判定 {got_status} != 金标准 {want_status}"
        assert got_v == pytest.approx(want_v, abs=5.1e-4), \
            f"{fname}.{key}: 数值 {got_v} 漂移，金标准 {want_v}（确认物理合理后更新 GOLDEN）"
    # 结果里不该有 golden 之外的键静默增减——新增指标要显式同步本文件
    extra = set(current) - set(golden)
    assert not extra, f"{fname}: 出现金标准之外的指标 {extra}（同步 GOLDEN）"
