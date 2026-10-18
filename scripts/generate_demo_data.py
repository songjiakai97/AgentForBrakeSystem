"""合成 demo 数据生成器（demo_conditions_metrics.md §8 验收场景）。

生成 MF4 文件：通道名 = 逻辑名（signals_mf4.yaml demo 约定）。
速度剖面均为分段线性 + 轻噪声，交叉点可由 segmenter 线性插值复现。

ABS 场景:  巡航 v_start（默认高于标称 v0，如 105）→ t_press 踩踏板
           → 匀减速到 v_stop_below → 低速蠕行至停 → 静止尾段。
TCS 场景:  静止段 → t_press 全油门 → 匀加速到 v_target 以上 → 巡航尾段。

violations 支持:
  yaw        横摆幅值超限      decel_low / acc_low  加减速能力不足
  distance   制动距离超限      time_high            加速时间超限
  slip_high  打滑量超限        no_stop              车速降不到 v_stop 以下
  no_v0      初速从未达到 v0   never_starts         车速从未越过 0.8
  drop:NAME  删除通道 NAME
"""

import os
from typing import Iterable, List, Optional

import numpy as np
from asammdf import Signal
import asammdf


def _piecewise(ts_grid, breaks):
    """breaks: [(t, v), ...] 分段线性插值到 ts_grid。"""
    b_t = np.array([p[0] for p in breaks], dtype=np.float64)
    b_v = np.array([p[1] for p in breaks], dtype=np.float64)
    return np.interp(ts_grid, b_t, b_v)


def _add(name: str, ts: np.ndarray, values: np.ndarray, unit: str = ""):
    return Signal(timestamps=ts.astype(np.float64), samples=values.astype(np.float32),
                  name=name, unit=unit)


def gen_abs_run(
    v0_kph: float = 100.0,
    v_start_kph: Optional[float] = None,   # 实际初速，默认 v0 + 5（105 起踩场景）
    decel: float = 7.5,                    # m/s²（合成用）
    yaw_amp: float = 2.0,
    stop_margin: float = 2.0,              # 减速到 v_stop 以下多少
    press_t: float = 2.0,
    no_stop: bool = False,
    no_v0: bool = False,
    seed: int = 0,
) -> dict:
    rng = np.random.default_rng(seed)
    v_start = v_start_kph if v_start_kph is not None else v0_kph + 5.0
    if no_v0:
        v_start = v0_kph - 10.0  # 从未达到标称初速
    v_stop_below = max(0.0, 0.8 - stop_margin) if not no_stop else 4.0

    t_brake = (v_start - v_stop_below) / 3.6 / decel
    t_end = press_t + t_brake + 2.0 + max(2.0, t_brake * 0.15)
    ts = np.arange(0.0, t_end, 0.01)

    if no_stop:
        creep = [
            (press_t + t_brake, v_stop_below),
            (press_t + t_brake + 1.0, v_stop_below - 0.4),
            (t_end, v_stop_below - 0.6),
        ]
    else:
        creep = [
            (press_t + t_brake, v_stop_below),
            (press_t + t_brake + 1.0, max(v_stop_below - 0.3, 0.0)),
            (press_t + t_brake + 1.6, 0.0),
            (t_end, 0.0),
        ]
    speed = _piecewise(ts, [
        (0.0, v_start),
        (press_t, v_start),
    ] + creep)
    speed = np.maximum(speed + rng.normal(0, 0.05, ts.size), 0.0)

    dist = np.concatenate([[0.0], np.cumsum(np.diff(ts) * speed[:-1] / 3.6)])

    pedal = np.where(ts >= press_t, np.minimum(100.0, 80 + (ts - press_t) * 400), 0.0)
    pedal = np.where(ts > press_t + t_brake + 1.8, 0.0, pedal)
    abs_active = (((speed > 5) & (pedal > 80))).astype(float)

    yaw = yaw_amp * np.sin(2 * np.pi * 0.8 * (ts - press_t)) * np.clip((ts - press_t) / 0.5, 0, 1)
    yaw = yaw + rng.normal(0, 0.1, ts.size)

    ax = np.gradient(speed, ts) * (1000.0 / 3600.0)

    sigs = {
        "speed_vbox": _add("speed_vbox", ts, speed, "km/h"),
        "distance_vbox": _add("distance_vbox", ts, dist, "m"),
        "yaw_rate": _add("yaw_rate", ts, yaw, "deg/s"),
        "Ax": _add("Ax", ts, ax, "m/s2"),
        "BrakePedalPos": _add("BrakePedalPos", ts, pedal, "%"),
        "ThrottlePedalPos": _add("ThrottlePedalPos", ts, np.zeros_like(ts), "%"),
        "ABS_Active": _add("ABS_Active", ts, abs_active, ""),
        "TCS_Active": _add("TCS_Active", ts, np.zeros_like(ts), ""),
        "wheelSpeed_FL": _add("wheelSpeed_FL", ts, np.maximum(speed + rng.normal(0, 0.3, ts.size), 0), "km/h"),
        "wheelSpeed_FR": _add("wheelSpeed_FR", ts, np.maximum(speed + rng.normal(0, 0.3, ts.size), 0), "km/h"),
        "wheelSpeed_RL": _add("wheelSpeed_RL", ts, np.maximum(speed + rng.normal(0, 0.3, ts.size), 0), "km/h"),
        "wheelSpeed_RR": _add("wheelSpeed_RR", ts, np.maximum(speed + rng.normal(0, 0.3, ts.size), 0), "km/h"),
    }
    return {"ts": ts, "sigs": sigs}


def gen_tcs_run(
    v_target_kph: float = 60.0,
    acc: float = 4.0,                     # m/s²（合成用）
    yaw_amp: float = 2.0,
    slip_peak: float = 20.0,              # km/h 打滑峰值
    press_t: float = 2.5,
    never_starts: bool = False,
    seed: int = 1,
) -> dict:
    rng = np.random.default_rng(seed)
    v_over = v_target_kph + 10.0          # 冲过目标速度
    if never_starts:
        v_over = 0.5                      # 从未越过 0.8 kph
    t_launch = press_t
    t_reach = press_t + (v_over - 0.0) / 3.6 / acc
    t_end = t_reach + 2.5

    ts = np.arange(0.0, t_end, 0.01)
    speed = _piecewise(ts, [
        (0.0, 0.0),
        (t_launch, 0.0),
        (t_reach, v_over),
        (t_end, v_over),
    ])
    speed = np.maximum(speed + rng.normal(0, 0.02, ts.size) * (ts > t_launch + 0.2), 0.0)

    # 打滑：起步后短暂峰值，随后收敛
    slip_env = slip_peak * np.exp(-np.clip(ts - t_launch, 0, None) / 0.6)
    slip_env = np.where(ts < t_launch + 0.05, 0.0, slip_env)
    wheel = np.maximum(speed + slip_env + rng.normal(0, 0.2, ts.size), 0.0)

    dist = np.concatenate([[0.0], np.cumsum(np.diff(ts) * speed[:-1] / 3.6)])
    throttle = np.where(ts >= t_launch, np.minimum(100.0, 60 + (ts - t_launch) * 200), 0.0)
    tcs_active = ((slip_env > 5) | ((ts > t_launch) & (ts < t_reach) & (slip_env > 2))).astype(float)
    yaw = yaw_amp * np.sin(2 * np.pi * 0.5 * (ts - t_launch)) * np.clip((ts - t_launch) / 0.6, 0, 1)
    yaw = yaw + rng.normal(0, 0.08, ts.size)
    ax = np.gradient(speed, ts) * (1000.0 / 3600.0)

    sigs = {
        "speed_vbox": _add("speed_vbox", ts, speed, "km/h"),
        "distance_vbox": _add("distance_vbox", ts, dist, "m"),
        "yaw_rate": _add("yaw_rate", ts, yaw, "deg/s"),
        "Ax": _add("Ax", ts, ax, "m/s2"),
        "BrakePedalPos": _add("BrakePedalPos", ts, np.zeros_like(ts), "%"),
        "ThrottlePedalPos": _add("ThrottlePedalPos", ts, throttle, "%"),
        "ABS_Active": _add("ABS_Active", ts, np.zeros_like(ts), ""),
        "TCS_Active": _add("TCS_Active", ts, tcs_active, ""),
        "wheelSpeed_FL": _add("wheelSpeed_FL", ts, wheel, "km/h"),
        "wheelSpeed_FR": _add("wheelSpeed_FR", ts, wheel + rng.normal(0, 0.2, ts.size), "km/h"),
        "wheelSpeed_RL": _add("wheelSpeed_RL", ts, np.maximum(speed + slip_env * 0.2 + rng.normal(0, 0.2, ts.size), 0), "km/h"),
        "wheelSpeed_RR": _add("wheelSpeed_RR", ts, np.maximum(speed + slip_env * 0.2 + rng.normal(0, 0.2, ts.size), 0), "km/h"),
    }
    return {"ts": ts, "sigs": sigs}


def write_mf4(out_path: str, data: dict, drop: Iterable[str] = ()) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    dropped = set(drop)
    mdf = asammdf.MDF(version=4.10)
    for name, sig in data["sigs"].items():
        if name in dropped:
            continue
        mdf.append([sig])
    mdf.save(out_path, overwrite=True)
    mdf.close()
    return out_path


# ---------------------------------------------------------------- 场景清单

def scenario_matrix() -> List[dict]:
    """返回 (文件名, 工况族参数) 列表，覆盖 §8 验收场景。"""
    s = []

    def add(name, kind, **kw):
        s.append({"file": f"{name}.mf4", "kind": kind, "kw": kw})

    # ---- ABS ----（干沥青正常样本需 decel ≳ 10 才能压进 40 m）
    add("abs_dry100_normal", "abs", v0_kph=100, decel=11.5, yaw_amp=2.0)
    add("abs_dry100_yaw_abn", "abs", v0_kph=100, decel=11.5, yaw_amp=7.0)
    add("abs_dry100_dist_abn", "abs", v0_kph=100, decel=4.5, yaw_amp=2.0)      # >40 m
    add("abs_wet60_normal", "abs", v0_kph=60, decel=6.5, yaw_amp=1.5)
    add("abs_wet60_yaw_abn", "abs", v0_kph=60, decel=6.5, yaw_amp=6.0)
    add("abs_wet60_decel_abn", "abs", v0_kph=60, decel=1.2, yaw_amp=1.5)       # <1.5
    add("abs_wet50_normal", "abs", v0_kph=50, decel=4.0, yaw_amp=1.5)
    add("abs_wet50_decel_abn", "abs", v0_kph=50, decel=0.6, yaw_amp=1.5)       # <0.8
    add("abs_wet50_no_yaw", "abs", v0_kph=50, decel=4.0, yaw_amp=1.5, drop=("yaw_rate",))
    add("abs_dry100_no_stop", "abs", v0_kph=100, decel=11.5, yaw_amp=2.0, no_stop=True)
    add("abs_dry100_no_v0", "abs", v0_kph=100, decel=11.5, yaw_amp=2.0, no_v0=True)

    # ---- TCS ----
    add("tcs_dry100_normal", "tcs", v_target_kph=100, acc=8.0, yaw_amp=2.0)
    add("tcs_dry100_time_abn", "tcs", v_target_kph=100, acc=2.3, yaw_amp=2.0)  # >10 s
    add("tcs_dry100_yaw_abn", "tcs", v_target_kph=100, acc=8.0, yaw_amp=6.5)
    add("tcs_wet60_normal", "tcs", v_target_kph=60, acc=2.2, yaw_amp=1.5, slip_peak=25.0)
    add("tcs_wet60_acc_abn", "tcs", v_target_kph=60, acc=1.0, yaw_amp=1.5, slip_peak=25.0)   # <1.6 (4WD)
    add("tcs_wet60_slip_abn", "tcs", v_target_kph=60, acc=2.2, yaw_amp=1.5, slip_peak=50.0)  # >36 (DTCS)
    add("tcs_wet60_no_yaw", "tcs", v_target_kph=60, acc=2.2, yaw_amp=1.5, slip_peak=25.0, drop=("yaw_rate",))
    add("tcs_wet60_never_starts", "tcs", v_target_kph=60, acc=2.2, yaw_amp=1.5, slip_peak=25.0, never_starts=True)
    add("tcs_wet50_normal", "tcs", v_target_kph=50, acc=1.2, yaw_amp=1.5, slip_peak=25.0)    # ok for 2WD(>=0.4)
    add("tcs_wet50_acc_abn", "tcs", v_target_kph=50, acc=0.2, yaw_amp=1.5, slip_peak=25.0)   # <0.4
    add("tcs_wet50_slip_abn", "tcs", v_target_kph=50, acc=1.2, yaw_amp=1.5, slip_peak=80.0)  # >54 (TCS)

    return s


def generate_all(out_dir: str = "tests/data/mf4") -> List[str]:
    paths = []
    for i, sc in enumerate(scenario_matrix()):
        kw = dict(sc["kw"])
        drop = kw.pop("drop", ())
        if sc["kind"] == "abs":
            data = gen_abs_run(seed=i, **kw)
        else:
            data = gen_tcs_run(seed=i, **kw)
        p = os.path.join(out_dir, sc["file"])
        write_mf4(p, data, drop=drop)
        paths.append(p)
    return paths


if __name__ == "__main__":
    out = "tests/data/mf4"
    files = generate_all(out)
    print(f"生成 {len(files)} 个 MF4 文件 → {out}/")
    for f in files:
        print(" ", f)
