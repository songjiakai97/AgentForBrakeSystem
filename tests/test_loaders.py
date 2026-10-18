"""MF4 / BLF Loader 端到端测试（合成数据，无外部样例依赖）。"""

import numpy as np
import pytest

from brake_analyzer.loaders import BLFLoader, MF4Loader

can = pytest.importorskip("can")
cantools = pytest.importorskip("cantools")
asammdf = pytest.importorskip("asammdf")


# ---------- MF4 ----------

@pytest.fixture()
def mf4_file(tmp_path):
    from asammdf import MDF, Signal

    ts1 = np.linspace(100.0, 105.0, 501)
    s1 = Signal(
        timestamps=ts1,
        samples=np.sin(ts1).astype(np.float64),
        name="VehicleSpeed",
        unit="km/h",
    )
    s2 = Signal(
        timestamps=np.linspace(102.0, 108.0, 301),
        samples=np.arange(301, dtype=np.float64),
        name="BrakePressure_cyl",
        unit="bar",
    )
    mdf = MDF(version=4.10)
    # 分开 append：同批 append 会共用时基
    mdf.append(s1)
    mdf.append(s2)
    path = str(tmp_path / "run.mf4")
    mdf.save(path)
    mdf.close()
    return path


MF4_MAPS = {
    "veh_speed": {"description": "车速", "unit": "km/h", "candidates": ["NoSuchCh", "VehicleSpeed"]},
    "brake_p": {"unit": "bar", "candidates": ["BrakePressure_cyl"]},
    "missing_sig": {"candidates": ["Ghost"]},
}


class TestMF4Loader:
    def test_load(self, mf4_file):
        data = MF4Loader(MF4_MAPS).load(mf4_file)
        assert set(data) == set(MF4_MAPS)
        assert data["veh_speed"].start_timestamp == 100.0
        # ts 相对全局最早时间戳偏移
        assert abs(data["veh_speed"].ts[0]) < 1e-9
        assert abs(data["brake_p"].ts[0] - 2.0) < 1e-9
        assert len(data["veh_speed"].ts) == 501
        assert len(data["brake_p"].ts) == 301
        assert np.allclose(
            data["veh_speed"].values,
            np.sin(np.linspace(100.0, 105.0, 501)).astype(np.float32),
            atol=1e-5,
        )
        assert data["veh_speed"].description == "车速"
        assert data["veh_speed"].unit == "km/h"

    def test_missing_signal_no_raise(self, mf4_file):
        data = MF4Loader(MF4_MAPS).load(mf4_file)
        assert data["missing_sig"].ts.size == 0
        assert data["missing_sig"].values.size == 0

    def test_instance_reusable(self, mf4_file):
        loader = MF4Loader(MF4_MAPS)
        a = loader.load(mf4_file)
        b = loader.load(mf4_file)
        assert np.allclose(a["veh_speed"].values, b["veh_speed"].values)


# ---------- BLF ----------

DBC_EC = """VERSION "test"

BO_ 291 ENG_1: 8 Gateway
 SG_ WheelSpeedFL : 0|16@1+ (0.05625,0) [0|3686.34375] "km/h" VCU
 SG_ ActFailSt : 16|2@1+ (1,0) [0|3] "None" VCU
VAL_ 291 ActFailSt 0 "NO_FAIL" 1 "MINOR" 2 "MAJOR" ;
"""

DBC_BODY = """VERSION "test"

BO_ 2147488308 ENVM_1: 8 Body
 SG_ AmbTemp : 0|8@1- (1,-40) [-40|85] "degC" BCM
"""


@pytest.fixture()
def blf_files(tmp_path):
    p_ec = str(tmp_path / "ec.dbc")
    p_body = str(tmp_path / "body.dbc")
    with open(p_ec, "w") as f:
        f.write(DBC_EC)
    with open(p_body, "w") as f:
        f.write(DBC_BODY)

    m291 = cantools.database.load_file(p_ec).get_message_by_name("ENG_1")
    m4660 = cantools.database.load_file(p_body).get_message_by_name("ENVM_1")

    blf_path = str(tmp_path / "run.blf")
    writer = can.BLFWriter(blf_path)
    for i in range(50):
        t = 200.0 + i * 0.02
        data = m291.encode(
            {"WheelSpeedFL": i * 10, "ActFailSt": min(i // 20, 3)}, padding=True
        )
        writer(
            can.Message(
                timestamp=t, arbitration_id=0x123,
                is_extended_id=False, channel=0, data=data,
            )
        )
        data2 = m4660.encode({"AmbTemp": i - 20}, padding=True)
        writer(
            can.Message(
                timestamp=t, arbitration_id=0x1234,
                is_extended_id=True, channel=1, data=data2,
            )
        )
    writer.stop()
    return p_ec, p_body, blf_path


BLF_MAPS = {
    # 首候选 alias 不对 → 回退第二候选
    "wheel_speed": {
        "description": "轮速", "unit": "km/h",
        "candidates": [["BODY", "0x123", "WheelSpeedFL"], ["EC", "0x123", "WheelSpeedFL"]],
    },
    "fail_state": {"candidates": [["EC", "0x123", "ActFailSt"]]},
    # 扩展帧：0x 十六进制 + x 后缀
    "amb_temp": {"unit": "degC", "candidates": [["BODY", "0x1234x", "AmbTemp"]]},
    "not_found": {"candidates": [["EC", "0x999", "X"], ["NOPE", "0x123", "Y"]]},
}


class TestBLFLoader:
    def _loader(self, blf_files):
        p_ec, p_body, _ = blf_files
        dbc_files = {
            "EC": {"path": p_ec, "channel": 0},
            "BODY": {"path": p_body, "channel": 1},
        }
        return BLFLoader(dbc_files, BLF_MAPS), blf_files[2]

    def test_load(self, blf_files):
        loader, blf_path = self._loader(blf_files)
        data = loader.load(blf_path)
        assert set(data) == set(BLF_MAPS)
        assert len(data["wheel_speed"].ts) == 50
        # 物理值：encode 传物理值 → decode 回物理值
        assert abs(data["wheel_speed"].values[10] - 100.0) < 0.1
        assert abs(data["wheel_speed"].ts[0]) < 1e-12
        assert len(data["amb_temp"].ts) == 50
        assert abs(data["amb_temp"].values[0] - (-20)) < 1e-9

    def test_enum_choices(self, blf_files):
        loader, blf_path = self._loader(blf_files)
        data = loader.load(blf_path)
        assert data["fail_state"].choices == {0: "NO_FAIL", 1: "MINOR", 2: "MAJOR"}
        assert data["fail_state"].values[5] == 0
        assert data["fail_state"].values[45] == 2

    def test_missing_signal_no_raise(self, blf_files):
        loader, blf_path = self._loader(blf_files)
        data = loader.load(blf_path)
        assert data["not_found"].ts.size == 0
        assert data["not_found"].values.size == 0

    def test_instance_reusable(self, blf_files):
        loader, blf_path = self._loader(blf_files)
        a = loader.load(blf_path)
        b = loader.load(blf_path)
        assert np.allclose(a["wheel_speed"].values, b["wheel_speed"].values)
