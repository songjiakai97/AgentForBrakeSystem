"""BLF demo 数据生成器（design.md §6.2 / §9.6）。

DBC 定义是唯一事实来源，两件事一次做完：

1. 用 cantools 构造单份 DBC（`configs/dbc/brake_demo.dbc`）：实际信号名 = 逻辑信号名，
   状态位占 1 bit，数值信号按物理量级选 factor/offset/bit 宽，报文 8 字节，同报文内不重叠；
2. 用 python-can 的 BLFWriter 写 `.blf`：物理剖面直接复用 MF4 生成器（同一套
   gen_abs_run / gen_tcs_run），逐帧按 10 ms 栅格发全部报文，**通道全部为 0**。

因为剖面与 MF4 同源，同一场景在 MF4 与 BLF 两条加载路径下的指标结果应当一致
（差异只来自 CAN 量化），这一点由 tests/test_blf_demo.py 卡住。

用法：
    python scripts/generate_demo_blf.py                 # → configs/dbc + tests/data/blf
    python scripts/generate_demo_blf.py --out DIR --dbc PATH --period 0.02
"""

import argparse
import os
import sys
from collections import OrderedDict
from typing import Dict, List

import can
import cantools
from cantools.database import Database, Message, Signal
from cantools.database.conversion import LinearConversion, NamedSignalConversion

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from brake_analyzer.configs import load_blf_config      # noqa: E402


DBC_PATH = os.path.join(ROOT, "configs", "dbc", "brake_demo.dbc")
DEFAULT_OUT = os.path.join(ROOT, "tests", "data", "blf")

RECEIVER = "TCU"
PERIOD_S = 0.01        # 与 MF4 demo 的 0.01 s 栅格一致，便于两条路径逐点对比

# ---------------------------------------------------------------- DBC 定义

def _num(name: str, start: int, length: int, factor: float, signed: bool = False,
         unit: str = "", minimum=None, maximum=None) -> Signal:
    """数值信号：小端、offset=0（物理量都从 0 或对称区间起，偏置留给有负量程要求的量）。"""
    return Signal(
        name=name, start=start, length=length, byte_order="little_endian",
        is_signed=signed,
        conversion=LinearConversion(scale=factor, offset=0.0, is_float=False),
        minimum=minimum, maximum=maximum, unit=unit or None, receivers=[RECEIVER],
    )


def _flag(name: str, start: int) -> Signal:
    """状态位：1 bit + 枚举表，解码为 INACTIVE/ACTIVE（loader 存原码 + choices）。"""
    return Signal(
        name=name, start=start, length=1, byte_order="little_endian", is_signed=False,
        conversion=NamedSignalConversion(
            scale=1.0, offset=0.0,
            choices=OrderedDict([(0, "INACTIVE"), (1, "ACTIVE")]), is_float=False),
        unit="", receivers=[RECEIVER],
    )


def dbc_messages() -> List[Message]:
    """单份 DBC 的报文清单（8 字节 = 64 bit，逐信号连续排布，故不可能重叠）。

    量化取值按「不改变 demo 判定」的原则选：
      轮速     0x100/0x101  4 × 16bit，0.01 km/h → 0~655.35（滑移量阈值 km/h 级，够用）
      整车动态 0x102        speed 0.01 km/h、distance 0.05 m（40 m 阈值不受影响）、
                            Ax 16bit 有符号 0.01 m/s²
      踏板状态 0x103        两踏板 8bit × 0.5 %（覆盖段板 arm 阈值 80/95 %），
                            ABS/TCS 各 1 bit（bit16/17）
      横摆     0x104        单独一帧，便于构造「缺通道」场景（整帧不发即为 missing）
    """
    return [
        Message(frame_id=0x100, name="WHEEL_SPEED_1", length=8, senders=["ABS"], signals=[
            _num("wheelSpeed_FL", 0, 16, 0.01, unit="km/h", minimum=0, maximum=655.35),
            _num("wheelSpeed_FR", 16, 16, 0.01, unit="km/h", minimum=0, maximum=655.35),
        ]),
        Message(frame_id=0x101, name="WHEEL_SPEED_2", length=8, senders=["ABS"], signals=[
            _num("wheelSpeed_RL", 0, 16, 0.01, unit="km/h", minimum=0, maximum=655.35),
            _num("wheelSpeed_RR", 16, 16, 0.01, unit="km/h", minimum=0, maximum=655.35),
        ]),
        Message(frame_id=0x102, name="VEH_DYN_1", length=8, senders=["ESP"], signals=[
            _num("speed_vbox", 0, 16, 0.01, unit="km/h", minimum=0, maximum=655.35),
            _num("distance_vbox", 16, 16, 0.05, unit="m", minimum=0, maximum=3276.75),
            _num("Ax", 32, 16, 0.01, signed=True, unit="m/s2",
                 minimum=-327.68, maximum=327.67),
        ]),
        Message(frame_id=0x103, name="PEDAL_STATUS_1", length=8, senders=["VCU"], signals=[
            _num("BrakePedalPos", 0, 8, 0.5, unit="%", minimum=0, maximum=127.5),
            _num("ThrottlePedalPos", 8, 8, 0.5, unit="%", minimum=0, maximum=127.5),
            _flag("ABS_Active", 16),
            _flag("TCS_Active", 17),
        ]),
        Message(frame_id=0x104, name="VEH_DYN_2", length=8, senders=["ESP"], signals=[
            _num("yaw_rate", 0, 16, 0.01, signed=True, unit="deg/s",
                 minimum=-327.68, maximum=327.67),
        ]),
    ]


def build_database() -> Database:
    return Database(messages=dbc_messages())


def write_dbc(path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    cantools.database.dump_file(build_database(), path, database_format="dbc")
    return path


def check_layout(db: Database, config_dir: str = None) -> None:
    """自检：signals_blf.yaml 与 DBC 对得上，且 8 字节 / 位不重叠 / 单位一致。"""
    from brake_analyzer.configs import load_configs
    from brake_analyzer.loaders.base import _parse_msg_id     # 只依赖 numpy

    cfg = load_configs(config_dir or os.path.join(ROOT, "configs"))
    blf_cfg = load_blf_config(config_dir or os.path.join(ROOT, "configs"),
                              logical_names=set(cfg.signal_maps))
    logical = set(cfg.signal_maps)

    ids = [m.frame_id for m in db.messages]
    assert len(ids) == len(set(ids)), "frame_id 重复"
    for msg in db.messages:
        assert msg.length == 8, f"{msg.name} 长度应为 8 字节"
        assert not msg.is_extended_frame, f"{msg.name} 应为标准帧（候选 id 写作 0x100）"
        used: Dict[int, str] = {}
        for sig in msg.signals:
            assert sig.name in logical, f"{msg.name}.{sig.name} 不是已定义的逻辑信号"
            expect = str(cfg.signal_maps[sig.name].get("unit") or "")
            assert (sig.unit or "") == expect, (
                f"{sig.name} 单位 {sig.unit!r} 与 signals_mf4.yaml {expect!r} 不一致")
            for bit in range(sig.start, sig.start + sig.length):
                assert bit not in used, (
                    f"{msg.name} 位冲突：{sig.name} 与 {used[bit]} 抢占 bit {bit}")
                used[bit] = sig.name
    missing = logical - {s.name for m in db.messages for s in m.signals}
    assert not missing, f"DBC 信号未覆盖全部逻辑信号：{sorted(missing)}"

    # yaml 侧候选三元组必须能在 DBC 里解析到，且 alias 的 channel 与本帧一致
    for name, spec in blf_cfg.signal_maps.items():
        alias, msg_id, sig_name = spec["candidates"][0]
        frame_id, is_ext = _parse_msg_id(msg_id)
        msg = db.get_message_by_frame_id(frame_id)
        assert msg is not None, f"{name}: DBC 里没有帧 {msg_id}"
        assert bool(msg.is_extended_frame) == is_ext, f"{name}: 帧 {msg_id} 标准/扩展不匹配"
        assert any(s.name == sig_name for s in msg.signals), (
            f"{name}: 帧 {msg_id} 里没有信号 {sig_name}")
        assert int(blf_cfg.dbc_files[alias]["channel"]) == 0, (
            f"demo 约定通道全部为 0，实际 {alias}={blf_cfg.dbc_files[alias]['channel']}")


# ---------------------------------------------------------------- BLF 写入

def scenario_matrix() -> List[dict]:
    """BLF 场景比 MF4 少，但必须覆盖：正常 / 超限 / 状态位 / 缺通道。

    seed 显式取该场景在 MF4 scenario_matrix() 里的下标 —— MF4 生成器按 seed 决定
    噪声，只有 seed 相同，两条加载路径才真是「同一份物理剖面」，差异才只来自量化。
    abs_wet60_no_yaw 在 MF4 侧没有同名文件（用 yaw_abn 的剖面去掉横摆帧），不参与逐点对比。
    """
    return [
        {"file": "abs_dry100_normal.blf", "kind": "abs", "seed": 0,
         "kw": {"v0_kph": 100, "decel": 11.5, "yaw_amp": 2.0}},
        {"file": "abs_dry100_dist_abn.blf", "kind": "abs", "seed": 2,
         "kw": {"v0_kph": 100, "decel": 4.5, "yaw_amp": 2.0}},
        {"file": "abs_wet60_yaw_abn.blf", "kind": "abs", "seed": 4,
         "kw": {"v0_kph": 60, "decel": 6.5, "yaw_amp": 6.0}},
        # yaw 整帧不发 → 验证 BLF 路径下的 missing 口径
        {"file": "abs_wet60_no_yaw.blf", "kind": "abs", "seed": 4,
         "kw": {"v0_kph": 60, "decel": 6.5, "yaw_amp": 1.5}, "drop": ("yaw_rate",)},
        {"file": "tcs_wet60_normal.blf", "kind": "tcs", "seed": 14,
         "kw": {"v_target_kph": 60, "acc": 2.2, "yaw_amp": 1.5, "slip_peak": 25.0}},
        {"file": "tcs_wet60_slip_abn.blf", "kind": "tcs", "seed": 16,
         "kw": {"v_target_kph": 60, "acc": 2.2, "yaw_amp": 1.5, "slip_peak": 50.0}},
    ]


def _sig(data: dict, name: str):
    """MF4 生成器返回 asammdf Signal 对象，取其 samples/timestamps 作为物理序列。"""
    return data["sigs"][name]


def _encode_value(sig, raw: float):
    """物理值 → 可编码值：枚举位取整，数值信号夹到 DBC 量程内。

    合成数据在零速附近有 ±0.3 km/h 噪声（MF4 侧保留负值），CAN 侧无符号位量程
    下界为 0，夹到 0 是物理上正确的读法；对指标的影响 < 0.05 km/h，远小于阈值余量。
    """
    if sig.choices:
        return int(round(raw))
    v = float(raw)
    if sig.minimum is not None:
        v = max(v, float(sig.minimum))
    if sig.maximum is not None:
        v = min(v, float(sig.maximum))
    return v


def write_blf(out_path: str, data: dict, db: Database,
              period_s: float = PERIOD_S, drop=()) -> str:
    """把物理序列按固定周期发成 CAN 帧写入 BLF；通道全部为 0。"""
    dropped = set(drop)
    ts = _sig(data, "speed_vbox").timestamps
    base = float(ts[0])
    # 时间戳保留物理秒值（BLFLoader 会统一减去全局最早时间戳，与 MF4 侧同口径）
    n = max(1, int(round((float(ts[-1]) - base) / period_s)) + 1)
    # 按发送栅格对源序列取最近点：period == 源栅格（0.01 s）时逐点一一对应
    src_dt = float(ts[1] - ts[0]) if len(ts) > 1 else period_s
    idx = [min(len(ts) - 1, max(0, int(round(k * period_s / src_dt)))) for k in range(n)]

    groups: List[tuple] = []          # (msg_def, [signal_def, ...])
    for msg in db.messages:
        picked = [s for s in msg.signals if s.name not in dropped]
        if picked:
            groups.append((msg, picked))

    writer = can.BLFWriter(out_path)
    try:
        for k in range(n):
            j = min(len(ts) - 1, idx[k])
            t = base + k * period_s
            for msg, picked in groups:
                payload = {s.name: _encode_value(s, float(_sig(data, s.name).samples[j]))
                           for s in picked}
                writer(can.Message(timestamp=t, arbitration_id=msg.frame_id,
                                   is_extended_id=False, channel=0,
                                   dlc=msg.length,
                                   data=msg.encode(payload, padding=True)))
    finally:
        writer.stop()
    return out_path


def generate_all(out_dir: str = DEFAULT_OUT, dbc_path: str = DBC_PATH,
                 period_s: float = PERIOD_S) -> List[str]:
    from scripts.generate_demo_data import gen_abs_run, gen_tcs_run

    write_dbc(dbc_path)
    db = cantools.database.load_file(dbc_path)
    check_layout(db)

    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for sc in scenario_matrix():
        kw = dict(sc["kw"])
        drop = tuple(sc.get("drop", ()))
        seed = sc["seed"]
        data = (gen_abs_run(seed=seed, **kw) if sc["kind"] == "abs"
                else gen_tcs_run(seed=seed, **kw))
        p = os.path.join(out_dir, sc["file"])
        write_blf(p, data, db=db, period_s=period_s, drop=drop)
        paths.append(p)
    return paths


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="生成 demo DBC 与 BLF")
    ap.add_argument("--out", default=DEFAULT_OUT, help="BLF 输出目录")
    ap.add_argument("--dbc", default=DBC_PATH, help="DBC 输出路径")
    ap.add_argument("--period", type=float, default=PERIOD_S, help="发送周期（秒）")
    args = ap.parse_args()
    files = generate_all(args.out, args.dbc, args.period)
    print(f"DBC → {args.dbc}")
    print(f"生成 {len(files)} 个 BLF（周期 {args.period}s，通道 0）→ {args.out}/")
    for f in files:
        print(" ", os.path.basename(f), os.path.getsize(f), "bytes")
