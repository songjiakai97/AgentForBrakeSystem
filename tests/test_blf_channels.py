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
    "a_val": {"candidates": [["EC", "0x200", "A_val"]]},
    "b_val": {"candidates": [["BODY", "0x200", "B_val"]]},
    "b_other": {"candidates": [["BODY", "0x201", "B_other"]]},
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


def _cfg(dbc_a, dbc_b, maps=None, **kw):
    return BlfConfigs(
        buses={"EC": {"label": "EC", "dbcs": [dbc_a], "unavailable": []},
               "BODY": {"label": "BODY", "dbcs": [dbc_b], "unavailable": []}},
        signal_maps=MAPS if maps is None else maps,
        **kw,
    )


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
        cfg = _cfg(*dbcs)
        data = BLFLoader(cfg).load(blf)
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
        data = BLFLoader(_cfg(*dbcs)).load(blf)
        assert data["a_val"].values[:20].tolist() == list(range(1, 21))
        assert data["b_val"].values[:20].tolist() == [200 + i for i in range(20)]

    def test_report_evidence(self, dbcs, tmp_path):
        from brake_analyzer.loaders.blf_channels import identify

        blf = _write_blf(tmp_path / "run.blf", _two_bus_frames())
        report = identify(blf, ["EC", "BODY"], MAPS, ChannelDetect())
        assert report.bus_channels() == {"EC": 0, "BODY": 1}
        by_bus = {e.bus: e for e in report.evidence}
        assert by_bus["BODY"].hits == 2 and by_bus["BODY"].observed == 2
        assert by_bus["BODY"].status == "matched"
        # EC 的所有候选 ID 两条通道都有（0x200）：单看 EC 分不出，但 BODY 的
        # 0x201 唯一指向 ch1，互换会掉分 → 归属仍然确定
        assert by_bus["EC"].status == "matched"
        assert any("0x200" in w and "多个通道" in w for w in report.warnings)

    def test_not_observed_bus_is_reported(self, dbcs, tmp_path):
        from brake_analyzer.loaders.blf_channels import identify

        # BODY 只引用 0x201，而文件里只有 EC 的 0x200
        maps = {"a_val": MAPS["a_val"], "b_other": MAPS["b_other"]}
        frames = [(100.0 + i * 0.01, 0, 0x200, i) for i in range(10)]
        blf = _write_blf(tmp_path / "only_a.blf", frames)
        report = identify(blf, ["EC", "BODY"], maps, ChannelDetect())
        status = {e.bus: e.status for e in report.evidence}
        assert status == {"EC": "matched", "BODY": "not_observed"}
        assert report.assignment == {0: "EC"}
        assert any("没有出现" in w for w in report.warnings)

    def test_explicit_channels_skip_identification(self, dbcs, tmp_path):
        from brake_analyzer.loaders.blf_channels import identify

        # 显式绑定在 loader 层生效；identify 只负责 auto 分支
        blf = _write_blf(tmp_path / "run.blf", _two_bus_frames())
        report = identify(blf, ["EC", "BODY"], MAPS, ChannelDetect())
        assert report.assignment[0] == "EC"

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
    dbc: {os.path.join(CONFIG_DIR, 'dbc', 'brake_demo.dbc')}
  VBOX:
    dbc: ${{NO_SUCH_DBC_VAR}}
channels: auto
signal_maps:
  speed_vbox:
    candidates:
      - [CHASSIS, "0x102", speed_vbox]
      - [VBOX, "0x18FEF100x", speed]
""")
        cfg = load_blf_config(d)
        assert cfg.buses["VBOX"]["dbcs"] == []
        assert cfg.buses["VBOX"]["unavailable"] == ["${NO_SUCH_DBC_VAR}"]
        assert cfg.buses["CHASSIS"]["dbcs"]

    def test_env_var_set_resolves(self, tmp_path, monkeypatch):
        from brake_analyzer.configs import load_blf_config

        dbc = os.path.join(CONFIG_DIR, "dbc", "brake_demo.dbc")
        monkeypatch.setenv("TEST_DBC_PATH", dbc)
        d = self._mini(tmp_path, """
buses:
  CHASSIS:
    dbc: ${TEST_DBC_PATH}
signal_maps:
  speed_vbox:
    candidates:
      - [CHASSIS, "0x102", speed_vbox]
""")
        cfg = load_blf_config(d)
        assert cfg.buses["CHASSIS"]["dbcs"] == [dbc]

    def test_multiple_dbcs_keep_declared_order_and_dedupe(self, tmp_path):
        from brake_analyzer.configs import load_blf_config

        dbc = os.path.join(CONFIG_DIR, "dbc", "brake_demo.dbc")
        d = self._mini(tmp_path, f"""
buses:
  CHASSIS:
    dbc:
      - {dbc}
      - {dbc}
signal_maps:
  speed_vbox:
    candidates:
      - [CHASSIS, "0x102", speed_vbox]
""")
        cfg = load_blf_config(d)
        assert cfg.buses["CHASSIS"]["dbcs"] == [dbc]

    def test_channels_override_parsed(self, tmp_path):
        from brake_analyzer.configs import load_blf_config

        dbc = os.path.join(CONFIG_DIR, "dbc", "brake_demo.dbc")
        d = self._mini(tmp_path, f"""
buses:
  CHASSIS:
    dbc: {dbc}
channels:
  1: CHASSIS
signal_maps:
  speed_vbox:
    candidates:
      - [CHASSIS, "0x102", speed_vbox]
""")
        cfg = load_blf_config(d)
        assert cfg.channels == {1: "CHASSIS"}

    def test_channel_detect_validated(self, tmp_path):
        from brake_analyzer.configs import ConfigError, load_blf_config

        dbc = os.path.join(CONFIG_DIR, "dbc", "brake_demo.dbc")
        d = self._mini(tmp_path, f"""
buses:
  CHASSIS:
    dbc: {dbc}
channel_detect:
  min_score: 2
signal_maps:
  speed_vbox:
    candidates:
      - [CHASSIS, "0x102", speed_vbox]
""")
        with pytest.raises(ConfigError, match="min_score"):
            load_blf_config(d)

    def test_unused_bus_rejected(self, tmp_path):
        from brake_analyzer.configs import ConfigError, load_blf_config

        dbc = os.path.join(CONFIG_DIR, "dbc", "brake_demo.dbc")
        d = self._mini(tmp_path, f"""
buses:
  CHASSIS:
    dbc: {dbc}
  VBOX:
    dbc: {dbc}
signal_maps:
  speed_vbox:
    candidates:
      - [CHASSIS, "0x102", speed_vbox]
""")
        with pytest.raises(ConfigError, match="没有被任何候选引用"):
            load_blf_config(d)


# ---------------------------------------------------------------- 多 DBC

def test_multiple_dbcs_first_match_wins(dbcs, tmp_path):
    """一个总线挂多份库：按声明顺序找，先命中者胜，不会重复出点。"""
    from brake_analyzer.loaders import BLFLoader

    empty = tmp_path / "empty.dbc"
    empty.write_text('VERSION "test"\n\nBO_ 1024 OTHER: 8 X\n'
                     ' SG_ Z : 0|8@1+ (1,0) [0|255] "" X\n', encoding="utf-8")
    frames = [(100.0 + i * 0.01, 0, 0x200, i) for i in range(10)]
    blf = _write_blf(tmp_path / "run.blf", frames)
    cfg = BlfConfigs(
        buses={"EC": {"label": "EC", "dbcs": [str(empty), dbcs[0]], "unavailable": []}},
        signal_maps={"a_val": MAPS["a_val"]},
    )
    data = BLFLoader(cfg).load(blf)
    assert data["a_val"].values.tolist() == list(range(10))


def test_identified_bus_without_dbc_warns(dbcs, tmp_path):
    """识别成功但本机没有库：信号为空，且告警说清是「解不了」而不是「没数据」。"""
    from brake_analyzer.loaders import BLFLoader

    frames = [(100.0 + i * 0.01, 0, 0x200, i) for i in range(5)]
    blf = _write_blf(tmp_path / "run.blf", frames)
    cfg = BlfConfigs(
        buses={"EC": {"label": "EC", "dbcs": [dbcs[0]], "unavailable": []},
               "VBOX": {"label": "VBOX", "dbcs": [], "unavailable": ["${VBOX_DBC}"]}},
        signal_maps={"a_val": MAPS["a_val"],
                     "vbox_speed": {"candidates": [["VBOX", "0x300", "speed"]]}},
        channels={0: "VBOX"},          # 显式绑定，避免 VBOX 因文件里没有 0x300 未识别
    )
    loader = BLFLoader(cfg)
    data = loader.load(blf)
    assert data["vbox_speed"].values.size == 0
    assert any("VBOX" in e and "未提供" in e for e in loader.load_errors)
