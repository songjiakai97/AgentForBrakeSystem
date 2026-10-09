"""指标工具实现。每个工具只在给定窗口内做纯计算，不自行寻找交叉点。"""

from typing import Optional

import numpy as np

from ..base import KMH_TO_MS, register_tool, interp_at, window_slice


@register_tool("max_abs")
def max_abs(signals: dict, condition, window: tuple, params: Optional[dict]) -> dict:
    """窗口内信号绝对值最大值。绑定输入见 metrics.yaml inputs。"""
    name = (params or {}).get("signal", "yaw_rate")
    ts, vals = window_slice(signals, name, window)
    if vals is None:
        return {"value": None}
    return {"value": float(np.max(np.abs(vals))), "n": int(vals.size)}


@register_tool("signal_span")
def signal_span(signals: dict, condition, window: tuple, params: Optional[dict]) -> dict:
    """窗口终止与起始的信号值之差（如距离增量）。端点值用插值。"""
    name = (params or {}).get("signal", "distance_vbox")
    t0, t1 = window
    v0 = interp_at(signals, name, t0)
    v1 = interp_at(signals, name, t1)
    if v0 is None or v1 is None:
        return {"value": None}
    return {"value": float(v1 - v0), "v_start": v0, "v_end": v1}


@register_tool("speed_slope")
def speed_slope(signals: dict, condition, window: tuple, params: Optional[dict]) -> dict:
    """窗口内车速变化率幅值 |v_end − v_start| / (t_end − t_start)，km/h→m/s，恒非负。"""
    name = (params or {}).get("signal", "speed_vbox")
    t0, t1 = window
    v0 = interp_at(signals, name, t0)
    v1 = interp_at(signals, name, t1)
    if v0 is None or v1 is None:
        return {"value": None}
    dt = t1 - t0
    if dt <= 0 or np.isnan(dt):
        return {"value": None}
    value = abs((v1 - v0) * KMH_TO_MS) / dt
    return {"value": float(value), "delta_v_kmh": float(v1 - v0), "duration_s": float(dt)}


@register_tool("window_duration")
def window_duration(signals: dict, condition, window: tuple, params: Optional[dict]) -> dict:
    """窗口时长（秒）。signal 仅用于确认速度通道存在。"""
    name = (params or {}).get("signal", "speed_vbox")
    t0, t1 = window
    sig = signals.get(name)
    if sig is None or len(sig.ts) == 0 or t1 <= t0 or np.isnan(t0) or np.isnan(t1):
        return {"value": None}
    return {"value": float(t1 - t0)}


@register_tool("wheel_slip_max")
def wheel_slip_max(signals: dict, condition, window: tuple, params: Optional[dict]) -> dict:
    """窗口内 max(四轮轮速) − speed_vbox 的最大值，单位 km/h（不做换算）。"""
    p = params or {}
    wheels = [s for s in p.get("signals", []) if s != "speed_vbox"] or [
        "wheelSpeed_FL", "wheelSpeed_FR", "wheelSpeed_RL", "wheelSpeed_RR"
    ]
    vbox_name = "speed_vbox"
    t0, t1 = window
    if t1 <= t0 or np.isnan(t0) or np.isnan(t1):
        return {"value": None}
    ref_ts = None
    max_arr = None
    for w in wheels:
        ts, vals = window_slice(signals, w, window)
        if vals is None:
            return {"value": None, "reason": f"missing:{w}"}
        if ref_ts is None:
            ref_ts = ts
            max_arr = vals.copy()
        else:
            max_arr = np.maximum(max_arr, np.interp(ref_ts, ts, vals))
    sig_v = signals.get(vbox_name)
    if sig_v is None or len(sig_v.ts) == 0:
        return {"value": None, "reason": f"missing:{vbox_name}"}
    # 车速用整条序列插值到参考轴：窗口边缘由窗外样本真实括住，不在窗口内钳位；
    # 参考轴落在车速时间范围外则判不可算（与 interp_at 口径一致）。
    ts_v = np.asarray(sig_v.ts, dtype=np.float64)
    v_vbox = np.asarray(sig_v.values, dtype=np.float64)
    order = np.argsort(ts_v, kind="stable")
    ts_v, v_vbox = ts_v[order], v_vbox[order]
    if ref_ts[0] < ts_v[0] or ref_ts[-1] > ts_v[-1]:
        return {"value": None, "reason": f"out_of_range:{vbox_name}"}
    vbox = np.interp(ref_ts, ts_v, v_vbox)
    slip = max_arr - vbox
    return {"value": float(np.max(slip))}
