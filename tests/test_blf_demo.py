"""BLF demo 链路测试：signals_blf.yaml + DBC + python-can 生成的 BLF。

覆盖三件事：
1. 配置自检（DBC 与 yaml 一致、逻辑信号集与 MF4 侧一致、结构非法能挡住）；
2. BLFLoader 端到端解码（量化、1bit 状态位 choices、通道 0 匹配、缺帧 → missing）；
3. MF4 / BLF 两条加载路径的指标口径一致（剖面同源，差异只来自 CAN 量化）。
"""

import os
import warnings

import numpy as np
import pytest

warnings.filterwarnings("ignore", module="asammdf")

from brake_analyzer.configs import (  # noqa: E402
    ConfigError, load_blf_config, load_configs,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "configs")
MF4_DIR = os.path.join(ROOT, "tests", "data", "mf4")
BLF_DIR = os.path.join(ROOT, "tests", "data", "blf")

can = pytest.importorskip("can")
cantools = pytest.importorskip("cantools")

# 与 scripts/generate_demo_blf.py 的场景清单一一对应（seed 同 MF4 下标，剖面才同源）
PARITY = [
    # (blf 文件名, mf4 文件名, 工况 id, profile)
    ("abs_dry100_normal.blf", "abs_dry100_normal.mf4",
     "abs_full_brake_dry_asphalt_100kph", None),
    ("abs_dry100_dist_abn.blf", "abs_dry100_dist_abn.mf4",
     "abs_full_brake_dry_asphalt_100kph", None),
    ("abs_wet60_yaw_abn.blf", "abs_wet60_yaw_abn.mf4",
     "abs_full_brake_wet_basalt_60kph", None),
    ("tcs_wet60_normal.blf", "tcs_wet60_normal.mf4",
     "tcs_full_throttle_wet_basalt_0to60kph", "4WD|DTCS"),
    ("tcs_wet60_slip_abn.blf", "tcs_wet60_slip_abn.mf4",
     "tcs_full_throttle_wet_basalt_0to60kph", "4WD|DTCS"),
]


@pytest.fixture(scope="module")
def cfg():
    return load_configs(CONFIG_DIR)


@pytest.fixture(scope="module")
def blf_cfg(cfg):
    return load_blf_config(CONFIG_DIR, logical_names=set(cfg.signal_maps))


@pytest.fixture(scope="module")
def blf_loader(blf_cfg):
    from brake_analyzer.loaders import BLFLoader

    return BLFLoader(blf_cfg.dbc_files, blf_cfg.signal_maps)


def _require(fname):
    path = os.path.join(BLF_DIR, fname)
    if not os.path.exists(path):
        pytest.skip(f"BLF demo 数据缺失，请先运行 scripts/generate_demo_blf.py：{path}")
    return path


# ---------------------------------------------------------------- 1. 配置

class TestBlfConfig:
    def test_single_dbc_channel_zero(self, blf_cfg):
        assert list(blf_cfg.dbc_files) == ["BRAKE"], "demo 只用一份 DBC"
        info = blf_cfg.dbc_files["BRAKE"]
        assert info["channel"] == 0, "通道全部设成 0"
        assert os.path.isabs(info["path"]) and os.path.exists(info["path"])

    def test_paths_resolved_from_any_cwd(self, blf_cfg):
        """yaml 里写仓库根相对路径，Loader 拿到的必须已是绝对路径。"""
        from brake_analyzer.loaders import BLFLoader

        # 不依赖进程 cwd（pytest 可能在任意目录启动）
        assert BLFLoader(blf_cfg.dbc_files, blf_cfg.signal_maps) is not None

    def test_signal_set_matches_mf4_side(self, cfg, blf_cfg):
        assert set(blf_cfg.signal_maps) == set(cfg.signal_maps)
        for name in cfg.signal_maps:
            assert blf_cfg.signal_maps[name]["unit"] == cfg.signal_maps[name].get("unit", "")

    def test_candidates_all_resolvable_in_dbc(self, blf_cfg):
        db = cantools.database.load_file(blf_cfg.dbc_files["BRAKE"]["path"])
        for name, spec in blf_cfg.signal_maps.items():
            for alias, msg_id, sig_name in spec["candidates"]:
                assert alias in blf_cfg.dbc_files
                frame_id, is_ext = _parse(msg_id)
                msg = db.get_message_by_frame_id(frame_id)
                assert msg is not None, f"{name} 帧 {msg_id} 不在 DBC"
                assert bool(msg.is_extended_frame) == is_ext
                assert any(s.name == sig_name for s in msg.signals), f"{name} 信号缺失"

    def test_dbc_layout_8bytes_no_overlap(self, blf_cfg):
        db = cantools.database.load_file(blf_cfg.dbc_files["BRAKE"]["path"])
        for msg in db.messages:
            assert msg.length == 8
            used = {}
            for sig in msg.signals:
                for bit in range(sig.start, sig.start + sig.length):
                    assert bit not in used, f"{msg.name} bit {bit} 重叠"
                    used[bit] = sig.name

    def test_status_bits_are_one_bit_enums(self, blf_cfg):
        db = cantools.database.load_file(blf_cfg.dbc_files["BRAKE"]["path"])
        for name in ("ABS_Active", "TCS_Active"):
            sig = next(s for m in db.messages for s in m.signals if s.name == name)
            assert sig.length == 1
            assert dict(sig.choices) == {0: "INACTIVE", 1: "ACTIVE"}

    def test_bad_msg_id_rejected(self, tmp_path):
        import yaml

        d = _copy_config(tmp_path)
        raw = yaml.safe_load(open(os.path.join(CONFIG_DIR, "signals_blf.yaml"), encoding="utf-8"))
        raw["signal_maps"]["Ax"]["candidates"] = [["BRAKE", "102", "Ax"]]
        _write(d, "signals_blf.yaml", raw)
        with pytest.raises(ConfigError, match="msg_id"):
            load_blf_config(d)

    def test_unknown_alias_rejected(self, tmp_path):
        import yaml

        d = _copy_config(tmp_path)
        raw = yaml.safe_load(open(os.path.join(CONFIG_DIR, "signals_blf.yaml"), encoding="utf-8"))
        raw["signal_maps"]["Ax"]["candidates"] = [["NOPE", "0x102", "Ax"]]
        _write(d, "signals_blf.yaml", raw)
        with pytest.raises(ConfigError, match="alias"):
            load_blf_config(d)

    def test_missing_dbc_file_rejected(self, tmp_path):
        import yaml

        d = _copy_config(tmp_path)
        raw = yaml.safe_load(open(os.path.join(CONFIG_DIR, "signals_blf.yaml"), encoding="utf-8"))
        raw["dbc_files"]["BRAKE"]["path"] = "dbc/not_here.dbc"
        _write(d, "signals_blf.yaml", raw)
        with pytest.raises(ConfigError, match="不存在"):
            load_blf_config(d)

    def test_signal_set_mismatch_rejected(self, tmp_path):
        import yaml

        d = _copy_config(tmp_path)
        raw = yaml.safe_load(open(os.path.join(CONFIG_DIR, "signals_blf.yaml"), encoding="utf-8"))
        raw["signal_maps"].pop("Ax")
        _write(d, "signals_blf.yaml", raw)
        with pytest.raises(ConfigError, match="不一致"):
            load_blf_config(d, logical_names=set(load_configs(CONFIG_DIR).signal_maps))


def _parse(msg_id):
    from brake_analyzer.loaders.base import _parse_msg_id

    return _parse_msg_id(msg_id)


def _copy_config(tmp_path) -> str:
    import shutil

    d = str(tmp_path / "configs")
    if not os.path.isdir(d):
        shutil.copytree(CONFIG_DIR, d)
    return d


def _write(config_dir, name, obj):
    import yaml

    with open(os.path.join(config_dir, name), "w", encoding="utf-8") as f:
        yaml.safe_dump(obj, f, allow_unicode=True, sort_keys=False)


# ---------------------------------------------------------------- 2. Loader

class TestBlfLoaderOnDemo:
    def test_all_signals_present(self, blf_loader, cfg):
        data = blf_loader.load(_require("abs_dry100_normal.blf"))
        assert set(data) == set(cfg.signal_maps)
        for name, sd in data.items():
            assert sd.values.size == 854, f"{name} 点数应等于 10 ms 栅格点数"
            assert sd.unit == cfg.signal_maps[name].get("unit", "")

    def test_quantization_steps(self, blf_loader):
        data = blf_loader.load(_require("abs_dry100_normal.blf"))
        # speed factor 0.01 → 解码值必须是 0.01 的整数倍；distance factor 0.05
        assert np.allclose(data["speed_vbox"].values * 100,
                           np.round(data["speed_vbox"].values * 100), atol=1e-6)
        assert np.allclose(data["distance_vbox"].values * 20,
                           np.round(data["distance_vbox"].values * 20), atol=1e-6)

    def test_status_flags_raw_and_choices(self, blf_loader):
        data = blf_loader.load(_require("abs_dry100_normal.blf"))
        abs_sd = data["ABS_Active"]
        assert abs_sd.choices == {0: "INACTIVE", 1: "ACTIVE"}
        assert set(np.unique(abs_sd.values).tolist()) <= {0, 1}
        # 踩刹车后 ABS 置位；TCS 在 ABS 场景始终为 0
        assert abs_sd.values.max() == 1
        assert set(np.unique(data["TCS_Active"].values).tolist()) == {0}

    def test_signed_ax_negative(self, blf_loader):
        data = blf_loader.load(_require("abs_dry100_normal.blf"))
        assert float(np.min(data["Ax"].values)) < 0

    def test_channel_matching_and_relative_ts(self, blf_loader):
        data = blf_loader.load(_require("abs_dry100_normal.blf"))
        # 全部帧通道 0 → 每信号都能取到；ts 相对全局最早点偏移
        assert abs(data["speed_vbox"].ts[0]) < 1e-9
        assert abs(float(data["speed_vbox"].ts[-1]) - 8.53) < 1e-6
        assert np.allclose(np.diff(data["speed_vbox"].ts), 0.01, atol=1e-6)

    def test_missing_frame_gives_empty_signal(self, blf_loader):
        """abs_wet60_no_yaw：整帧 0x104 不发 → yaw_rate 空，其余不受影响。"""
        data = blf_loader.load(_require("abs_wet60_no_yaw.blf"))
        assert data["yaw_rate"].values.size == 0
        assert data["yaw_rate"].ts.size == 0
        assert data["speed_vbox"].values.size > 0

    def test_wrong_channel_would_not_match(self, blf_cfg):
        from brake_analyzer.loaders import BLFLoader

        bad = {"BRAKE": {"path": blf_cfg.dbc_files["BRAKE"]["path"], "channel": 3}}
        data = BLFLoader(bad, blf_cfg.signal_maps).load(_require("abs_dry100_normal.blf"))
        assert all(sd.values.size == 0 for sd in data.values())

    def test_loader_instance_reusable(self, blf_loader):
        p = _require("tcs_wet60_normal.blf")
        a, b = blf_loader.load(p), blf_loader.load(p)
        assert np.allclose(a["wheelSpeed_FL"].values, b["wheelSpeed_FL"].values)


# ---------------------------------------------------------------- 3. 生成器自检

class TestGenerator:
    def test_check_layout_passes_on_committed_dbc(self, blf_cfg):
        from scripts.generate_demo_blf import check_layout

        db = cantools.database.load_file(blf_cfg.dbc_files["BRAKE"]["path"])
        check_layout(db, CONFIG_DIR)

    def test_check_layout_catches_overlap(self):
        """cantools 自身就拒绝重叠信号，故用桩对象测 check_layout 的位图检查。"""
        from scripts.generate_demo_blf import _num, check_layout

        class _Msg:
            name, frame_id, length, is_extended_frame = "BAD", 0x100, 8, False
            signals = [_num("wheelSpeed_FL", 0, 16, 0.01, unit="km/h"),
                       _num("wheelSpeed_FR", 8, 16, 0.01, unit="km/h")]  # 与 FL 重叠

        class _Db:
            messages = [_Msg()]

        with pytest.raises(AssertionError, match="位冲突"):
            check_layout(_Db(), CONFIG_DIR)

    def test_check_layout_catches_wrong_length(self, blf_cfg):
        from scripts.generate_demo_blf import check_layout

        db = cantools.database.load_file(blf_cfg.dbc_files["BRAKE"]["path"])
        db.messages[0].length = 6
        with pytest.raises(AssertionError, match="8 字节"):
            check_layout(db, CONFIG_DIR)

    def test_committed_dbc_matches_generator(self, blf_cfg):
        """入库的 .dbc 必须与生成器定义一致（防止有人手改 DBC 而 yaml/剖面跟不上）。"""
        from scripts.generate_demo_blf import build_database

        def digest(db):
            return sorted(
                (m.frame_id, m.length, s.name, s.start, s.length, s.is_signed,
                 float(s.scale or 1.0), float(s.offset or 0.0), s.unit or "")
                for m in db.messages for s in m.signals)

        assert digest(build_database()) == digest(
            cantools.database.load_file(blf_cfg.dbc_files["BRAKE"]["path"]))

    def test_scenario_matrix_coverage(self):
        from scripts.generate_demo_blf import scenario_matrix

        files = [sc["file"] for sc in scenario_matrix()]
        assert any(sc.get("drop") for sc in scenario_matrix()), "要有缺通道场景"
        kinds = {sc["kind"] for sc in scenario_matrix()}
        assert kinds == {"abs", "tcs"}
        for sc in scenario_matrix():
            assert isinstance(sc.get("seed"), int), "seed 需显式对齐 MF4 下标"
        assert len(files) == len(set(files))


# ---------------------------------------------------------------- 4. 端到端口径一致

def _metric_map(result):
    return {m.key: m for s in result.samples for m in s.metrics}


@pytest.mark.parametrize("blf_name,mf4_name,cid,profile", PARITY)
def test_pipeline_parity_with_mf4(cfg, blf_loader, mf4_name, cid, profile, blf_name):
    from brake_analyzer.loaders import MF4Loader
    from brake_analyzer.pipeline import analyze_signals

    blf_res = analyze_signals(blf_loader.load(_require(blf_name)), cid, cfg,
                              file_name=blf_name, profile=profile)
    mf4_res = analyze_signals(MF4Loader(cfg.signal_maps).load(os.path.join(MF4_DIR, mf4_name)),
                              cid, cfg, file_name=mf4_name, profile=profile)
    mb, mm = _metric_map(blf_res), _metric_map(mf4_res)
    assert set(mb) == set(mm)
    for key in mb:
        # 判定档位必须一致（量化不能把 ok 变成 abnormal，反之亦然）
        assert mb[key].status == mm[key].status, f"{blf_name} {key} 状态漂移"
        if mb[key].value is None:
            assert mm[key].value is None
            continue
        # 数值差 must 落在 CAN 量化 + 分段插值的可解释范围内
        tol = _tolerance_for(key)
        assert abs(float(mb[key].value) - float(mm[key].value)) <= tol, (
            f"{blf_name} {key}: blf={mb[key].value} mf4={mm[key].value}")


def _tolerance_for(metric_key):
    """量化主导：distance 0.05 m/点、speed 0.01 km/h；给分段插值留一倍余量。

    metric.key 形如 "<工况 id>.brake_distance"，按指标名取容差。
    """
    name = metric_key.split(".")[-1]
    return {"brake_distance": 0.2, "yaw_rate_max": 0.05, "slip_max": 0.05,
            "decel_avg": 0.02, "acc_avg": 0.02, "acc_time": 0.05}.get(name, 0.05)


def test_missing_channel_marks_metric_missing_and_rule_hits(cfg, blf_loader):
    from brake_analyzer.pipeline import analyze_signals

    res = analyze_signals(blf_loader.load(_require("abs_wet60_no_yaw.blf")),
                          "abs_full_brake_wet_basalt_60kph", cfg)
    m = _metric_map(res)["abs_full_brake_wet_basalt_60kph.yaw_rate_max"]
    assert m.status == "missing"
    assert m.value is None
    verdicts = [v.rule_id for s in res.samples for v in s.verdicts]
    assert "abs_yaw_missing" in verdicts


def test_abnormal_case_flags_distance(cfg, blf_loader):
    from brake_analyzer.pipeline import analyze_signals

    res = analyze_signals(blf_loader.load(_require("abs_dry100_dist_abn.blf")),
                          "abs_full_brake_dry_asphalt_100kph", cfg)
    m = _metric_map(res)["abs_full_brake_dry_asphalt_100kph.brake_distance"]
    assert m.status == "abnormal"
    assert float(m.value) > 40


# ---------------------------------------------------------------- 5. 按扩展名选 Loader

class TestLoaderSelection:
    def _engine(self, blf_cfg, tmp_path):
        from brake_analyzer.agent.engine import ChatEngine
        from brake_analyzer.llm.client import LlmClient
        from brake_analyzer.llm.kb import KnowledgeBase
        from web.store import Store

        store = Store(upload_dir=str(tmp_path / "uploads"))
        return ChatEngine(load_configs(CONFIG_DIR), LlmClient(),
                          KnowledgeBase(os.path.join(ROOT, "knowledge")), store,
                          blf_cfg=blf_cfg), store

    def test_make_loader_by_ext(self, cfg, blf_cfg):
        from brake_analyzer.loaders import BLFLoader, MF4Loader
        from brake_analyzer.pipeline import NoDataError, make_loader

        assert isinstance(make_loader("a.mf4", cfg, blf_cfg=blf_cfg), MF4Loader)
        assert isinstance(make_loader("a.blf", cfg, blf_cfg=blf_cfg), BLFLoader)
        # 没给 blf_cfg 时 .blf 明确报"未启用 BLF"，不静默按 MF4 解
        with pytest.raises(NoDataError, match="BLF"):
            make_loader("a.blf", cfg)

    def test_engine_caches_loader_per_ext(self, blf_cfg, tmp_path):
        """BLFLoader 构造要解析 DBC，同一扩展名必须复用同一实例。"""
        eng, _ = self._engine(blf_cfg, tmp_path)
        first = eng._loader_for("x.blf")
        assert eng._loader_for("y.blf") is first
        assert eng._loader_for("z.mf4") is not first

    def test_engine_analyzes_blf_when_cfg_given(self, blf_cfg, tmp_path):
        """会话内只有 .blf 时，引擎走 BLF 路径并出分析结果。"""
        eng, store = self._engine(blf_cfg, tmp_path)
        chat = store.create_chat()
        with open(_require("abs_dry100_dist_abn.blf"), "rb") as f:
            store.add_file(chat, "abs_dry100_dist_abn.blf", f.read())
        added = eng._analyze(chat, "abs_full_brake_dry_asphalt_100kph", None)
        kinds = [m.kind for m in added]
        assert "analysis_result" in kinds, kinds
        res = [m for m in added if m.kind == "analysis_result"][0]
        st = {x["key"].rsplit(".", 1)[1]: x["status"]
              for s in res.data["samples"] for x in s["metrics"]}
        assert st["brake_distance"] == "abnormal"

    def test_engine_without_blf_cfg_reports_error(self, tmp_path):
        """没配 BLF 时不崩：按文件报错并汇总，不产出分析结果。"""
        eng, store = self._engine(None, tmp_path)
        chat = store.create_chat()
        with open(_require("abs_dry100_dist_abn.blf"), "rb") as f:
            store.add_file(chat, "abs_dry100_dist_abn.blf", f.read())
        added = eng._analyze(chat, "abs_full_brake_dry_asphalt_100kph", None)
        errs = [m for m in added if m.kind == "error"]
        assert errs and "BLF" in errs[0].content
        assert not [m for m in added if m.kind == "analysis_result"]
