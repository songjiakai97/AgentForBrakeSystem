"""BLF 通道自动识别（design.md §9.6）。

核心命题：通道归属必须先识别，否则同一报文 ID 在两条总线上代表不同信号时
会串线。这里的用例都用「同一 0x200 在两份 DBC 里叫不同名字」构造。
"""

import os

import pytest

from brake_analyzer.configs import BlfConfigs, ChannelDetect

can = pytest.importorskip("can")
cantools = pytest.importorskip("cantools")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "configs")
DEMO_DBC = os.path.join(CONFIG_DIR, "dbc", "brake_demo.dbc")

DBC_A = """VERSION "test"

BO_ 512 MSG_A: 8 ECU_A
 SG_ A_val : 0|8@1+ (1,0) [0|255] "" ECU_A
"""

DBC_B = """VERSION "test"

BO_ 512 MSG_B: 8 ECU_B
 SG_ B_val : 0|8@1+ (1,0) [0|255] "" ECU_B

BO_ 513 MSG_B2: 8 ECU_B
 SG_ B_other : 0|8@1+ (1,0) [0|255] "" ECU_B
"""

MAPS = {
    "a_val": {"candidates": [["A_DBC", "0x200", "A_val"]]},
    "b_val": {"candidates": [["B_DBC", "0x200", "B_val"]]},
    "b_other": {"candidates": [["B_DBC", "0x201", "B_other"]]},
}


# ---------------------------------------------------------------- 夹具

@pytest.fixture()
def dbcs(tmp_path):
    a = tmp_path / "a.dbc"
    b = tmp_path / "b.dbc"
    a.write_text(DBC_A, encoding="utf-8")
    b.write_text(DBC_B, encoding="utf-8")
    return str(a), str(b)


def _write_blf(path, frames):
    w = can.BLFWriter(str(path))
    try:
        for ts, ch, aid, val in frames:
            w(can.Message(timestamp=ts, arbitration_id=aid, is_extended_id=False,
                          channel=ch, dlc=8,
                          data=bytes([val & 0xFF, 0, 0, 0, 0, 0, 0, 0])))
    finally:
        w.stop()
    return str(path)


def _buses(dbc_a, dbc_b):
    return {
        "EC": {"label": "EC", "dbcs": {"A_DBC": dbc_a}, "unavailable": []},
        "BODY": {"label": "BODY", "dbcs": {"B_DBC": dbc_b}, "unavailable": []},
    }


def _cfg(dbc_a, dbc_b, maps=None, **kw):
    return BlfConfigs(buses=_buses(dbc_a, dbc_b),
                      signal_maps=MAPS if maps is None else maps, **kw)


def _two_bus_frames(n=20):
    """ch0: 0x200 = 1..n（EC）；ch1: 0x200 = 200+i、0x201 = 250-i（BODY）。"""
    frames = []
    for i in range(n):
        t = 100.0 + i * 0.01
        frames.append((t, 0, 0x200, i + 1))
        frames.append((t, 1, 0x200, 200 + i))
        frames.append((t, 1, 0x201, 250 - i))
    return frames


# ---------------------------------------------------------------- 识别

class TestIdentify:
    def test_same_id_two_buses_not_cross_decoded(self, dbcs, tmp_path):
        from brake_analyzer.loaders import BLFLoader

        blf = _write_blf(tmp_path / "run.blf", _two_bus_frames())
        data = BLFLoader(_cfg(*dbcs)).load(blf)
        # 0x200 在两个通道都存在：A_val 只能来自 ch0，B_val 只能来自 ch1
        assert data["a_val"].values.tolist() == list(range(1, 21))
        assert data["b_val"].values.tolist() == [200 + i for i in range(20)]
        assert data["b_other"].values.tolist() == [250 - i for i in range(20)]

    def test_gateway_forwarded_id_does_not_swap_buses(self, dbcs, tmp_path):
        """ch1 上也有 0x200（网关转发）：仍有唯一的 0x201 把 BODY 定在 ch1。"""
        from brake_analyzer.loaders import BLFLoader

        frames = _two_bus_frames()
        frames += [(t, 1, 0x200, 200 + i) for i, (t, ch, aid, v)
                   in enumerate([f for f in frames if f[1] == 0])]
        blf = _write_blf(tmp_path / "gw.blf", frames)
        loader = BLFLoader(_cfg(*dbcs))
        data = loader.load(blf)
        assert data["a_val"].values[:20].tolist() == list(range(1, 21))
        assert data["b_val"].values[:20].tolist() == [200 + i for i in range(20)]
        # 同一 ID 跨通道是「只提示」级：解码键带通道号，结论照常可用，
        # 不该被算进 load_errors（那会在界面上显示成「部分文件分析失败」）
        assert loader.load_errors == [], loader.load_errors
        assert any("0x200" in w and "多个通道" in w for w in loader.load_warnings), \
            loader.load_warnings

    def test_report_evidence(self, dbcs, tmp_path):
        from brake_analyzer.loaders.blf_channels import identify

        blf = _write_blf(tmp_path / "run.blf", _two_bus_frames())
        report = identify(blf, _buses(*dbcs), MAPS, ChannelDetect())
        assert report.bus_channels() == {"EC": 0, "BODY": 1}
        by_bus = {e.bus: e for e in report.evidence}
        assert by_bus["BODY"].hits == 2 and by_bus["BODY"].observed == 2
        assert by_bus["BODY"].status == "matched"
        # EC 的所有候选 ID 两条通道都有（0x200）：单看 EC 分不出，但 BODY 的
        # 0x201 唯一指向 ch1，互换会掉分 → 归属仍然确定
        assert by_bus["EC"].status == "matched"
        # 同一 ID 跨通道只进 notes（提示级），不进 warnings（会影响结论的那一级）
        assert any("0x200" in w and "多个通道" in w for w in report.notes)
        assert not any("多个通道" in w for w in report.warnings)

    def test_not_observed_bus_is_reported(self, dbcs, tmp_path):
        from brake_analyzer.loaders.blf_channels import identify

        # BODY 只引用 0x201，而文件里只有 EC 的 0x200
        maps = {"a_val": MAPS["a_val"], "b_other": MAPS["b_other"]}
        frames = [(100.0 + i * 0.01, 0, 0x200, i) for i in range(10)]
        blf = _write_blf(tmp_path / "only_a.blf", frames)
        report = identify(blf, _buses(*dbcs), maps, ChannelDetect())
        status = {e.bus: e.status for e in report.evidence}
        assert status == {"EC": "matched", "BODY": "not_observed"}
        assert report.assignment == {0: "EC"}
        assert any("没有出现" in w for w in report.warnings)

    def test_sampling_stops_early(self, dbcs, tmp_path):
        from brake_analyzer.loaders.blf_channels import scan

        frames = []
        for i in range(3000):
            t = 100.0 + i * 0.01
            frames.append((t, 0, 0x200, i % 256))
            frames.append((t, 1, 0x201, i % 256))
        blf = _write_blf(tmp_path / "long.blf", frames)

        stats, n, span, truncated, err = scan(blf, scan_seconds=1.0, scan_frames=10**9)
        assert truncated and err is None
        assert n < len(frames) / 2, "按时间剪枝，不应读完整份文件"
        assert set(stats) == {0, 1}
        assert stats[0].ids[(0x200, False)] >= 2


# ---------------------------------------------------------------- 部分显式绑定

class TestPartialPin:
    def test_pinned_bus_and_auto_others(self, dbcs, tmp_path):
        """只写死 BODY → 其余总线仍自动识别，写死的通道不会被抢走。"""
        from brake_analyzer.loaders import BLFLoader
        from brake_analyzer.loaders.blf_channels import identify

        blf = _write_blf(tmp_path / "run.blf", _two_bus_frames())
        cfg = _cfg(*dbcs, channels={1: "BODY"})
        data = BLFLoader(cfg).load(blf)
        assert data["b_val"].values.tolist() == [200 + i for i in range(20)]
        assert data["b_other"].values.tolist() == [250 - i for i in range(20)]
        assert data["a_val"].values.tolist() == list(range(1, 21))

        report = identify(blf, _buses(*dbcs), MAPS, ChannelDetect(),
                          pinned={"BODY": 1})
        assert report.bus_channels() == {"EC": 0, "BODY": 1}
        assert {e.bus: e.status for e in report.evidence} == {
            "EC": "matched", "BODY": "explicit"}

    def test_pinned_absent_channel_leaves_bus_empty(self, dbcs, tmp_path):
        """绑定到文件里不存在的通道：该总线信号为空，其它总线照常自动识别。"""
        from brake_analyzer.loaders import BLFLoader

        blf = _write_blf(tmp_path / "run.blf", _two_bus_frames())
        loader = BLFLoader(_cfg(*dbcs, channels={3: "BODY"}))
        data = loader.load(blf)
        assert data["b_val"].values.size == 0
        assert data["b_other"].values.size == 0
        assert data["a_val"].values.tolist() == list(range(1, 21))
        assert any("通道 3" in w and "没有任何报文" in w
                   for w in loader.load_errors)

    def test_pinned_channel_absent_warns(self, dbcs, tmp_path):
        from brake_analyzer.loaders.blf_channels import identify

        frames = [(100.0 + i * 0.01, 0, 0x200, i) for i in range(10)]
        blf = _write_blf(tmp_path / "only0.blf", frames)
        report = identify(blf, _buses(*dbcs), MAPS, ChannelDetect(),
                          pinned={"BODY": 3})
        assert report.bus_channels() == {"EC": 0, "BODY": 3}
        assert any("通道 3" in w and "没有任何报文" in w for w in report.warnings)
        # 写死的总线不再被阈值告警打扰
        body = next(e for e in report.evidence if e.bus == "BODY")
        assert body.status == "explicit"
        assert not any("BODY" in w and "证据不足" in w for w in report.warnings)

    def test_all_pinned_skips_scan(self, dbcs, tmp_path, monkeypatch):
        from brake_analyzer.loaders import blf as blf_mod

        blf = _write_blf(tmp_path / "run.blf", _two_bus_frames())

        def boom(*a, **kw):
            raise AssertionError("全部显式绑定时不应扫描文件")

        monkeypatch.setattr(blf_mod, "identify", boom)
        data = blf_mod.BLFLoader(_cfg(*dbcs, channels={0: "EC", 1: "BODY"})).load(blf)
        assert data["a_val"].values.tolist() == list(range(1, 21))
        assert data["b_other"].values.tolist() == [250 - i for i in range(20)]


# ---------------------------------------------------------------- 配置

class TestBusesConfig:
    def _mini(self, tmp_path, body: str):
        d = tmp_path / "configs"
        d.mkdir()
        (d / "signals_blf.yaml").write_text(body, encoding="utf-8")
        return str(d)

    def test_env_var_missing_is_not_an_error(self, tmp_path, monkeypatch):
        """现场库用 ${ENV} 指向仓外路径：没设置 = 本机没有这份库，服务照常启动。"""
        from brake_analyzer.configs import load_blf_config

        monkeypatch.delenv("NO_SUCH_DBC_VAR", raising=False)
        d = self._mini(tmp_path, f"""
buses:
  CHASSIS:
    dbcs:
      CHASSIS_MAIN: {DEMO_DBC}
  VBOX:
    dbcs:
      VBOX_MAIN: ${{NO_SUCH_DBC_VAR}}
channels: auto
signal_maps:
  speed_vbox:
    candidates:
      - [CHASSIS_MAIN, "0x102", speed_vbox]
      - [VBOX_MAIN, "0x18FEF100x", speed]
""")
        cfg = load_blf_config(d)
        assert cfg.buses["VBOX"]["dbcs"] == {}
        assert cfg.buses["VBOX"]["unavailable"] == ["VBOX_MAIN"]
        assert cfg.buses["CHASSIS"]["dbcs"] == {"CHASSIS_MAIN": DEMO_DBC}

    def test_env_var_set_resolves(self, tmp_path, monkeypatch):
        from brake_analyzer.configs import load_blf_config

        monkeypatch.setenv("TEST_DBC_PATH", DEMO_DBC)
        d = self._mini(tmp_path, """
buses:
  CHASSIS:
    dbcs:
      CHASSIS_MAIN: ${TEST_DBC_PATH}
signal_maps:
  speed_vbox:
    candidates:
      - [CHASSIS_MAIN, "0x102", speed_vbox]
""")
        cfg = load_blf_config(d)
        assert cfg.buses["CHASSIS"]["dbcs"] == {"CHASSIS_MAIN": DEMO_DBC}

    def test_multiple_dbcs_per_bus_keep_order(self, tmp_path):
        from brake_analyzer.configs import load_blf_config

        d = self._mini(tmp_path, f"""
buses:
  CHASSIS:
    dbcs:
      CHASSIS_MAIN: {DEMO_DBC}
      CHASSIS_PRIV: {DEMO_DBC}
signal_maps:
  speed_vbox:
    candidates:
      - [CHASSIS_MAIN, "0x102", speed_vbox]
      - [CHASSIS_PRIV, "0x102", speed_vbox]
""")
        cfg = load_blf_config(d)
        assert list(cfg.buses["CHASSIS"]["dbcs"]) == ["CHASSIS_MAIN", "CHASSIS_PRIV"]

    def test_duplicate_alias_across_buses_rejected(self, tmp_path):
        from brake_analyzer.configs import ConfigError, load_blf_config

        d = self._mini(tmp_path, f"""
buses:
  CHASSIS:
    dbcs:
      MAIN: {DEMO_DBC}
  VBOX:
    dbcs:
      MAIN: {DEMO_DBC}
signal_maps:
  speed_vbox:
    candidates:
      - [MAIN, "0x102", speed_vbox]
""")
        with pytest.raises(ConfigError, match="重复"):
            load_blf_config(d)

    def test_channels_override_parsed(self, tmp_path):
        from brake_analyzer.configs import load_blf_config

        d = self._mini(tmp_path, f"""
buses:
  CHASSIS:
    dbcs:
      CHASSIS_MAIN: {DEMO_DBC}
channels:
  1: CHASSIS
signal_maps:
  speed_vbox:
    candidates:
      - [CHASSIS_MAIN, "0x102", speed_vbox]
""")
        cfg = load_blf_config(d)
        assert cfg.channels == {1: "CHASSIS"}

    def test_partial_channels_parsed(self, tmp_path):
        """channels 只写一部分是合法的：其余总线保持自动识别。"""
        from brake_analyzer.configs import load_blf_config

        d = self._mini(tmp_path, f"""
buses:
  CHASSIS:
    dbcs:
      CHASSIS_MAIN: {DEMO_DBC}
  VBOX:
    dbcs:
      VBOX_MAIN: {DEMO_DBC}
channels:
  1: VBOX
signal_maps:
  speed_vbox:
    candidates:
      - [CHASSIS_MAIN, "0x102", speed_vbox]
      - [VBOX_MAIN, "0x102", speed_vbox]
""")
        cfg = load_blf_config(d)
        assert cfg.channels == {1: "VBOX"}

    def test_channel_detect_validated(self, tmp_path):
        from brake_analyzer.configs import ConfigError, load_blf_config

        d = self._mini(tmp_path, f"""
buses:
  CHASSIS:
    dbcs:
      CHASSIS_MAIN: {DEMO_DBC}
channel_detect:
  min_score: 2
signal_maps:
  speed_vbox:
    candidates:
      - [CHASSIS_MAIN, "0x102", speed_vbox]
""")
        with pytest.raises(ConfigError, match="min_score"):
            load_blf_config(d)

    def test_unused_bus_rejected(self, tmp_path):
        from brake_analyzer.configs import ConfigError, load_blf_config

        d = self._mini(tmp_path, f"""
buses:
  CHASSIS:
    dbcs:
      CHASSIS_MAIN: {DEMO_DBC}
  VBOX:
    dbcs:
      VBOX_MAIN: {DEMO_DBC}
signal_maps:
  speed_vbox:
    candidates:
      - [CHASSIS_MAIN, "0x102", speed_vbox]
""")
        with pytest.raises(ConfigError, match="没有被任何候选引用"):
            load_blf_config(d)

    def test_unknown_alias_rejected(self, tmp_path):
        from brake_analyzer.configs import ConfigError, load_blf_config

        d = self._mini(tmp_path, f"""
buses:
  CHASSIS:
    dbcs:
      CHASSIS_MAIN: {DEMO_DBC}
signal_maps:
  speed_vbox:
    candidates:
      - [NOPE, "0x102", speed_vbox]
""")
        with pytest.raises(ConfigError, match="DBC 别名"):
            load_blf_config(d)


# ---------------------------------------------------------------- 多 DBC

def test_candidate_order_across_dbcs(dbcs, tmp_path):
    """一条总线挂两份库：候选按书写顺序尝试，首个能解析的胜出，不重复出点。"""
    from brake_analyzer.loaders import BLFLoader

    empty = tmp_path / "empty.dbc"
    empty.write_text('VERSION "test"\n\nBO_ 1024 OTHER: 8 X\n'
                     ' SG_ Z : 0|8@1+ (1,0) [0|255] "" X\n', encoding="utf-8")
    frames = [(100.0 + i * 0.01, 0, 0x200, i) for i in range(10)]
    blf = _write_blf(tmp_path / "run.blf", frames)
    cfg = BlfConfigs(
        buses={"EC": {"label": "EC",
                      "dbcs": {"EMPTY": str(empty), "A_DBC": dbcs[0]},
                      "unavailable": []}},
        signal_maps={"a_val": {"candidates": [
            ["EMPTY", "0x200", "A_val"], ["A_DBC", "0x200", "A_val"]]}},
    )
    data = BLFLoader(cfg).load(blf)
    assert data["a_val"].values.tolist() == list(range(10))


def test_identified_bus_without_dbc_warns(dbcs, tmp_path):
    """识别成功但本机没有库：信号为空，且告警说清是「解不了」而不是「没数据」。"""
    from brake_analyzer.loaders import BLFLoader

    frames = [(100.0 + i * 0.01, 0, 0x200, i) for i in range(5)]
    blf = _write_blf(tmp_path / "run.blf", frames)
    cfg = BlfConfigs(
        buses={"EC": {"label": "EC", "dbcs": {"A_DBC": dbcs[0]}, "unavailable": []},
               "VBOX": {"label": "VBOX", "dbcs": {}, "unavailable": ["VBOX_MAIN"]}},
        signal_maps={"a_val": MAPS["a_val"],
                     "vbox_speed": {"candidates": [["VBOX_MAIN", "0x300", "speed"]]}},
        channels={0: "VBOX"},          # 显式绑定，避免 VBOX 因文件里没有 0x300 未识别
    )
    loader = BLFLoader(cfg)
    data = loader.load(blf)
    assert data["vbox_speed"].values.size == 0
    assert any("VBOX" in e and "未提供" in e for e in loader.load_errors)
