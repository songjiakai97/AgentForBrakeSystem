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
    return load_blf_config(CONFIG_DIR, required_signals=cfg.required_signals,
                           registry=cfg.signal_registry)


@pytest.fixture(scope="module")
def blf_loader(blf_cfg):
    from brake_analyzer.loaders import BLFLoader

    return BLFLoader(blf_cfg)


def _require(fname):
    path = os.path.join(BLF_DIR, fname)
    if not os.path.exists(path):
        pytest.skip(f"BLF demo 数据缺失，请先运行 scripts/generate_demo_blf.py：{path}")
    return path


# ---------------------------------------------------------------- 1. 配置

class TestBlfConfig:
    def test_single_bus_auto_channels(self, blf_cfg):
        assert list(blf_cfg.buses) == ["BRAKE"], "demo 只用一条总线"
        info = blf_cfg.buses["BRAKE"]
        assert blf_cfg.channels is None, "channels: auto = 运行时按 msg_id 识别"
        assert list(info["dbcs"]) == ["BRAKE_MAIN"], "demo 一条总线挂一份 DBC"
        path = info["dbcs"]["BRAKE_MAIN"]
        assert os.path.isabs(path) and os.path.exists(path)

    def test_paths_resolved_from_any_cwd(self, blf_cfg):
        """yaml 里写仓库根相对路径，Loader 拿到的必须已是绝对路径。"""
        from brake_analyzer.loaders import BLFLoader

        # 不依赖进程 cwd（pytest 可能在任意目录启动）
        assert BLFLoader(blf_cfg) is not None

    def test_coverage_contract(self, cfg, blf_cfg):
        """两侧不再要求信号集相同；卡的是「逻辑名已登记 + 计算必需信号齐全」。"""
        assert set(blf_cfg.signal_maps) <= set(cfg.signal_registry)
        assert set(cfg.required_signals) <= set(blf_cfg.signal_maps)
        # 单位/描述来自登记表，Loader 拿到的就是它
        for name in blf_cfg.signal_maps:
            assert blf_cfg.signal_maps[name]["unit"] == cfg.signal_registry[name]["unit"]
            assert blf_cfg.signal_maps[name]["description"] == \
                cfg.signal_registry[name]["description"]

    def test_candidates_all_resolvable_in_dbc(self, blf_cfg):
        db = cantools.database.load_file(blf_cfg.buses["BRAKE"]["dbcs"]["BRAKE_MAIN"])
        from brake_analyzer.configs import dbc_alias_bus

        aliases = dbc_alias_bus(blf_cfg.buses)
        for name, spec in blf_cfg.signal_maps.items():
            for alias, msg_id, sig_name in spec["candidates"]:
                assert alias in aliases
                frame_id, is_ext = _parse(msg_id)
                msg = db.get_message_by_frame_id(frame_id)
                assert msg is not None, f"{name} 帧 {msg_id} 不在 DBC"
                assert bool(msg.is_extended_frame) == is_ext
                assert any(s.name == sig_name for s in msg.signals), f"{name} 信号缺失"

    def test_dbc_layout_8bytes_no_overlap(self, blf_cfg):
        db = cantools.database.load_file(blf_cfg.buses["BRAKE"]["dbcs"]["BRAKE_MAIN"])
        for msg in db.messages:
            assert msg.length == 8
            used = {}
            for sig in msg.signals:
                for bit in range(sig.start, sig.start + sig.length):
                    assert bit not in used, f"{msg.name} bit {bit} 重叠"
                    used[bit] = sig.name

    def test_status_bits_are_one_bit_enums(self, blf_cfg):
        db = cantools.database.load_file(blf_cfg.buses["BRAKE"]["dbcs"]["BRAKE_MAIN"])
        for name in ("ABS_Active", "TCS_Active"):
            sig = next(s for m in db.messages for s in m.signals if s.name == name)
            assert sig.length == 1
            assert dict(sig.choices) == {0: "INACTIVE", 1: "ACTIVE"}

    def test_bad_msg_id_rejected(self, tmp_path):
        import yaml

        d = _copy_config(tmp_path)
        raw = yaml.safe_load(open(os.path.join(CONFIG_DIR, "signals_blf.yaml"), encoding="utf-8"))
        raw["signal_maps"]["Ax"]["candidates"] = [["BRAKE_MAIN", "102", "Ax"]]
        _write(d, "signals_blf.yaml", raw)
        with pytest.raises(ConfigError, match="msg_id"):
            load_blf_config(d)

    def test_unknown_bus_rejected(self, tmp_path):
        import yaml

        d = _copy_config(tmp_path)
        raw = yaml.safe_load(open(os.path.join(CONFIG_DIR, "signals_blf.yaml"), encoding="utf-8"))
        raw["signal_maps"]["Ax"]["candidates"] = [["NOPE", "0x102", "Ax"]]
        _write(d, "signals_blf.yaml", raw)
        with pytest.raises(ConfigError, match="bus"):
            load_blf_config(d)

    def test_missing_dbc_file_rejected(self, tmp_path):
        import yaml

        d = _copy_config(tmp_path)
        raw = yaml.safe_load(open(os.path.join(CONFIG_DIR, "signals_blf.yaml"), encoding="utf-8"))
        raw["buses"]["BRAKE"]["dbcs"]["BRAKE_MAIN"] = "dbc/not_here.dbc"
        _write(d, "signals_blf.yaml", raw)
        with pytest.raises(ConfigError, match="不存在"):
            load_blf_config(d)

    def test_observation_only_signal_may_be_absent(self, cfg, tmp_path):
        """只用于观测量（Ax 不参与 metrics/分段）→ BLF 侧可以不带，这是 §9.6 的放宽。

        旧口径要求两侧逻辑信号集完全相同，逼着 CAN 上根本没有的通道也得编一个
        候选出来；现在只卡「参与计算的信号一个不缺」。
        """
        import yaml

        from brake_analyzer.loaders import BLFLoader

        assert "Ax" not in cfg.required_signals, "Ax 若变成计算必需，本用例前提失效"
        d = _copy_config(tmp_path)
        raw = yaml.safe_load(open(os.path.join(CONFIG_DIR, "signals_blf.yaml"), encoding="utf-8"))
        raw["signal_maps"].pop("Ax")
        _write(d, "signals_blf.yaml", raw)
        blf_cfg = load_blf_config(d, required_signals=cfg.required_signals,
                                  registry=cfg.signal_registry)
        assert "Ax" not in blf_cfg.signal_maps
        # 计算必需的信号仍在，缺的观测量由 Loader 回退成"该通道不存在"
        assert "speed_vbox" in blf_cfg.signal_maps
        data = BLFLoader(blf_cfg).load(
            _require("abs_dry100_normal.blf"))
        assert "Ax" not in data and "speed_vbox" in data

    def test_required_signal_missing_rejected(self, cfg, tmp_path):
        """参与计算的信号（speed_vbox：metrics + 两个分段器都要）缺了就拒绝启动。"""
        import yaml

        assert "speed_vbox" in cfg.required_signals
        d = _copy_config(tmp_path)
        raw = yaml.safe_load(open(os.path.join(CONFIG_DIR, "signals_blf.yaml"), encoding="utf-8"))
        raw["signal_maps"].pop("speed_vbox")
        _write(d, "signals_blf.yaml", raw)
        with pytest.raises(ConfigError, match="参与计算的信号"):
            load_blf_config(d, required_signals=cfg.required_signals,
                            registry=cfg.signal_registry)

    def test_bare_load_without_registry_ok(self):
        """不传 registry / required_signals 的裸加载是合法用法。

        曾是回归点：覆盖度校验块里残留旧 `logical_names` 兜底分支，裸加载一执行到
        那里就 NameError。而其它用例要么成对传 registry，要么在更早的结构校验里就
        抛错了，没有一条真正走到这条分支 —— 结构校验用例（msg_id/alias/DBC 缺失）
        用的正是裸调用，但错误发生在覆盖度校验之前。
        """
        b = load_blf_config(CONFIG_DIR)
        assert "Ax" in b.signal_maps and b.signal_maps["Ax"]["candidates"]
        # 没有登记表时不合并单位：格式侧本来就不声明 unit
        assert "unit" not in b.signal_maps["Ax"]

    def test_required_check_independent_of_registry(self, cfg, tmp_path):
        """计算必需信号这条把关不依附 registry：只传 required_signals 也照样拦。"""
        import yaml

        d = _copy_config(tmp_path)
        raw = yaml.safe_load(open(os.path.join(CONFIG_DIR, "signals_blf.yaml"), encoding="utf-8"))
        raw["signal_maps"].pop("yaw_rate")
        _write(d, "signals_blf.yaml", raw)
        with pytest.raises(ConfigError, match="参与计算的信号"):
            load_blf_config(d, required_signals=cfg.required_signals)

    def test_unregistered_logical_name_rejected(self, cfg, tmp_path):
        """同一物理量两侧必须同名：BLF 侧自创别名（vbox_speed）直接拦下。"""
        import yaml

        d = _copy_config(tmp_path)
        raw = yaml.safe_load(open(os.path.join(CONFIG_DIR, "signals_blf.yaml"), encoding="utf-8"))
        raw["signal_maps"]["vbox_speed"] = {"candidates": [["BRAKE_MAIN", "0x102", "speed_vbox"]]}
        _write(d, "signals_blf.yaml", raw)
        with pytest.raises(ConfigError, match="未登记"):
            load_blf_config(d, required_signals=cfg.required_signals,
                            registry=cfg.signal_registry)

    def test_unit_declared_in_blf_file_rejected(self, cfg, tmp_path):
        """单位只有 signals.yaml 一个出处：格式侧再写一份直接拒，不静默忽略。"""
        import yaml

        d = _copy_config(tmp_path)
        raw = yaml.safe_load(open(os.path.join(CONFIG_DIR, "signals_blf.yaml"), encoding="utf-8"))
        raw["signal_maps"]["speed_vbox"]["unit"] = "mph"      # 故意与登记表冲突
        _write(d, "signals_blf.yaml", raw)
        with pytest.raises(ConfigError, match="不该声明"):
            load_blf_config(d, required_signals=cfg.required_signals,
                            registry=cfg.signal_registry)

    def test_registry_unit_reaches_loader(self, cfg, blf_cfg):
        """正常路径下 Loader 拿到的单位就是登记表里的值。"""
        assert blf_cfg.signal_maps["speed_vbox"]["unit"] == \
            cfg.signal_registry["speed_vbox"]["unit"] == "km/h"


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

    def test_explicit_wrong_channel_would_not_match(self, blf_cfg):
        """显式 channels 写错通道号 → 全空；显式优先，不会偷偷回退到自动识别。"""
        from dataclasses import replace

        from brake_analyzer.loaders import BLFLoader

        bad = replace(blf_cfg, channels={3: "BRAKE"})
        data = BLFLoader(bad).load(_require("abs_dry100_normal.blf"))
        assert all(sd.values.size == 0 for sd in data.values())

    def test_loader_instance_reusable(self, blf_loader):
        p = _require("tcs_wet60_normal.blf")
        a, b = blf_loader.load(p), blf_loader.load(p)
        assert np.allclose(a["wheelSpeed_FL"].values, b["wheelSpeed_FL"].values)


# ---------------------------------------------------------------- 3. 生成器自检

class TestGenerator:
    def test_check_layout_passes_on_committed_dbc(self, blf_cfg):
        from scripts.generate_demo_blf import check_layout

        db = cantools.database.load_file(blf_cfg.buses["BRAKE"]["dbcs"]["BRAKE_MAIN"])
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

        db = cantools.database.load_file(blf_cfg.buses["BRAKE"]["dbcs"]["BRAKE_MAIN"])
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
            cantools.database.load_file(blf_cfg.buses["BRAKE"]["dbcs"]["BRAKE_MAIN"]))

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
        added, digests = eng._analyze(chat, "abs_full_brake_dry_asphalt_100kph", None)
        kinds = [m.kind for m in added]
        assert "analysis_result" in kinds, kinds
        assert digests and digests[0]["samples"][0]["metrics"], "BLF 结果同样要出数值摘要"
        res = [m for m in added if m.kind == "analysis_result"][0]
        st = {x["key"].rsplit(".", 1)[1]: x["status"]
              for s in res.data["samples"] for x in s["metrics"]}
        assert st["brake_distance"] == "abnormal"

    def test_engine_channel_note_is_not_a_failure(self, blf_cfg, tmp_path, monkeypatch):
        """识别提示（同一 ID 跨通道）：结果照常出，不许冒「部分文件分析失败」。

        埋过的实际困扰：`_bus_channels` 把通道识别的全部告警塞进 load_errors，
        于是网关转发/ID 复用这种「只提示」的事实被引擎当成失败上报，
        界面一边出完整结果一边报错误。
        """
        eng, store = self._engine(blf_cfg, tmp_path)
        chat = store.create_chat()
        with open(_require("abs_dry100_dist_abn.blf"), "rb") as f:
            store.add_file(chat, "abs_dry100_dist_abn.blf", f.read())
        loader = eng._loader_for("x.blf")
        real_load = loader.load

        def load(path):
            data = real_load(path)
            loader.load_warnings.append(
                "有报文 ID 同时出现在多个通道（网关转发或 ID 复用）：0x200@[0, 1]")
            return data

        monkeypatch.setattr(loader, "load", load)
        added, digests = eng._analyze(chat, "abs_full_brake_dry_asphalt_100kph", None)
        assert digests, "提示级事实不该让这一轮没有结果"
        assert not [m for m in added if m.kind == "error"], [m.content for m in added]
        items = [it for m in chat.messages if m.kind == "steps"
                 for it in (m.data or {}).get("items", [])]
        assert any(i["label"] == "识别提示（不影响结果）" for i in items), items

    def test_engine_without_blf_cfg_reports_error(self, tmp_path):
        """没配 BLF 时不崩：按文件报错并汇总，不产出分析结果。"""
        eng, store = self._engine(None, tmp_path)
        chat = store.create_chat()
        with open(_require("abs_dry100_dist_abn.blf"), "rb") as f:
            store.add_file(chat, "abs_dry100_dist_abn.blf", f.read())
        added, digests = eng._analyze(chat, "abs_full_brake_dry_asphalt_100kph", None)
        errs = [m for m in added if m.kind == "error"]
        assert errs and "BLF" in errs[0].content
        assert digests == [], "没跑出结果就不该给模型一份看起来成功的摘要"
        assert not [m for m in added if m.kind == "analysis_result"]
