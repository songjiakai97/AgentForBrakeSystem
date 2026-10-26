"""试验记录库（§17）：编号匹配、编号索引、功能信号列表。"""

import os

import pytest

from brake_analyzer.configs import ConfigError, load_configs
from brake_analyzer.records.naming import (
    FileIdIndex,
    is_valid_file_id,
    normalize_file_id,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "configs")


@pytest.fixture(scope="module")
def cfg():
    return load_configs(CONFIG_DIR)


# ---------------------------------------------------------------- 编号匹配
def test_file_id_format():
    assert is_valid_file_id("4.1.1") and is_valid_file_id("4.1.3.1")
    assert not is_valid_file_id("4.1.1a") and not is_valid_file_id("")
    assert not is_valid_file_id("4-1-1")          # 配置里统一写点


def test_normalize_accepts_dashes():
    assert normalize_file_id(" 4-1-3-1 ") == "4.1.3.1"
    idx = FileIdIndex({"4.1.3.1": "c"})
    assert idx.condition_id("4-1-3-1") == "c"


def test_prefix_match_allows_suffix_but_not_digits():
    idx = FileIdIndex({"4.1.1": "a", "4.1.10": "b"})
    assert idx.match("4.1.1_20260801_01.blf") == "4.1.1"
    assert idx.match("4-1-1-01.blf") == "4.1.1"
    assert idx.match("4.1.1.blf") == "4.1.1"
    # 4.1.10 不能被 4.1.1 吃掉，反之亦然
    assert idx.match("4.1.10_x.blf") == "4.1.10"
    assert idx.match("4.1.10.blf") == "4.1.10"
    # 不是开头的编号、或不认识的编号 → 不匹配
    assert idx.match("pre_4.1.1.blf") is None
    assert idx.match("4.1.2.blf") is None
    assert idx.match("foo.blf") is None


def test_longest_match_wins_for_nested_ids():
    idx = FileIdIndex({"4.1": "parent", "4.1.1": "child"})
    assert idx.match("4.1.1_x.blf") == "4.1.1"
    assert idx.match("4.1_x.blf") == "4.1"


def test_sub_suffix_matches_parent_id():
    """4.1.1.2 这类小节号归到 4.1.1（紧后一位不是数字）。"""
    idx = FileIdIndex({"4.1.1": "a"})
    assert idx.match("4.1.1.2.blf") == "4.1.1"


# ---------------------------------------------------------------- 配置里的编号
def test_conditions_have_expected_file_ids(cfg):
    assert cfg.match_file_id("4.1.1_20260801.blf") == "4.1.1"
    assert cfg.condition_by_file_id("4.1.1").id == "abs_full_brake_dry_asphalt_100kph"
    assert cfg.condition_by_file_id("4-1-3-1").id == "abs_full_brake_wet_asphalt_60kph"
    # 一个工况多个编号：对开左右
    assert cfg.condition_by_file_id("4.1.9").id == "abs_full_brake_split_80kph"
    assert cfg.condition_by_file_id("4.1.10").id == "abs_full_brake_split_80kph"
    assert cfg.condition_by_file_id("5.1.11").id == "tcs_full_throttle_split_0to30kph"
    assert cfg.condition_by_file_id("5.1.12").id == "tcs_full_throttle_split_0to30kph"
    # 对接方向
    assert cfg.condition_by_file_id("4.1.11").id == "abs_full_brake_low_to_high"
    assert cfg.condition_by_file_id("4.1.12").id == "abs_full_brake_high_to_low"


def test_unmatched_file_is_none(cfg):
    assert cfg.match_file_id("传感器标定_01.blf") is None
    assert cfg.condition_by_file_id("9.9.9") is None


def _copy_configs(tmp_path):
    import shutil

    d = tmp_path / "configs"
    shutil.copytree(CONFIG_DIR, d)
    return d


def _mutate_conditions(d, fn):
    import yaml

    p = os.path.join(d, "conditions.yaml")
    raw = yaml.safe_load(open(p, encoding="utf-8"))
    fn(raw)
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)


def test_duplicate_file_id_rejected(tmp_path):
    d = _copy_configs(tmp_path)
    _mutate_conditions(d, lambda raw: raw["abs"][1].__setitem__("file_ids", ["4.1.1"]))
    with pytest.raises(ConfigError, match="同时挂在"):
        load_configs(str(d))


def test_bad_file_id_format_rejected(tmp_path):
    d = _copy_configs(tmp_path)
    _mutate_conditions(d, lambda raw: raw["abs"][0].__setitem__("file_ids", ["4.1.1a"]))
    with pytest.raises(ConfigError, match="格式非法"):
        load_configs(str(d))


# ---------------------------------------------------------------- 功能信号列表
def test_function_signals_split_by_function(cfg):
    abs_sig = set(cfg.functions["abs"].signals)
    tcs_sig = set(cfg.functions["tcs"].signals)
    assert "ABS_Active" in abs_sig and "TCS_Active" not in abs_sig
    assert "TCS_Active" in tcs_sig and "ABS_Active" not in tcs_sig
    assert "BrakePedalPos" in abs_sig and "ThrottlePedalPos" in tcs_sig
    # 顺序有意义（图表按它筛序），不能退化成集合
    assert cfg.functions["abs"].signals[0] == "speed_vbox"


def test_function_signals_cover_metrics_and_window_channels(cfg):
    from brake_analyzer.events.segment import SEGMENTER_SIGNALS

    for fn in cfg.functions.values():
        declared = set(fn.signals)
        assert declared, f"{fn.key} 没写 signals"
        assert set(SEGMENTER_SIGNALS[fn.segmenter]) <= declared
        for cond in cfg.conditions_of(fn.key):
            if cond.segmenter:
                assert set(SEGMENTER_SIGNALS[cond.segmenter]) <= declared
            for entry in cond.metrics:
                assert set(cfg.metrics[entry["key"]].inputs) <= declared


def test_metric_outside_function_signals_rejected(tmp_path):
    d = _copy_configs(tmp_path)
    _mutate_conditions(
        d, lambda raw: raw["tcs"][0]["metrics"].append({"key": "brake_distance",
                                                       "ok_range": [None, 40]}))
    with pytest.raises(ConfigError, match="不在功能"):
        load_configs(str(d))


def test_unknown_function_signal_rejected(tmp_path):
    d = _copy_configs(tmp_path)
    import yaml

    p = os.path.join(d, "functions.yaml")
    raw = yaml.safe_load(open(p, encoding="utf-8"))
    raw["functions"][0]["signals"].append("no_such_signal")
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)
    with pytest.raises(ConfigError, match="未登记信号"):
        load_configs(str(d))
