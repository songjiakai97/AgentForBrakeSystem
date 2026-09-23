"""事件分段与时间窗（design.md §6.3 / demo_conditions_metrics.md §5）。

分段模板在代码中实现并注册于 SEGMENTERS，不进入 YAML。
所有速度交叉点在本层用线性插值定位，指标工具只在窗口内做纯计算。

事件有效性由踏板信号门控（pedal_arm_pct），最终窗口端点由速度交叉点裁剪：
- seg_abs_full_brake:      BrakePedalPos >= arm 区间内，speed_vbox 下降穿越 v0_kph
                           → 下降穿越 v_stop_kph 的配对。
- seg_tcs_full_throttle:   ThrottlePedalPos >= arm 区间内，speed_vbox 上升穿越 v_start_kph
                           → 上升穿越 v_target_kph 的配对。

交叉点任一找不到 → 不产出该 EventWindow；全部无窗口时由 pipeline 补一个
退化窗口，使指标判 missing、规则命中 data_quality。
"""

from typing import Callable, Dict, List, Tuple

import numpy as np

from ..schemas import ConditionConfig, EventWindow, FunctionConfig

# signals: Dict[str, SignalData]
Segmenter = Callable[
    [dict, FunctionConfig, ConditionConfig, dict], List[EventWindow]
]


# ---------------------------------------------------------------- 基础工具

def _series(signals: dict, name: str) -> Tuple[np.ndarray, np.ndarray]:
    sig = signals.get(name)
    if sig is None or len(sig.ts) == 0:
        return np.array([]), np.array([])
    ts = np.asarray(sig.ts, dtype=np.float64)
    vals = np.asarray(sig.values, dtype=np.float64)
    order = np.argsort(ts, kind="stable")
    return ts[order], vals[order]


def _crossings(ts: np.ndarray, vals: np.ndarray, level: float, direction: str) -> List[float]:
    """相邻样本线性插值求穿越时刻列表。direction: 'down' | 'up'。"""
    out: List[float] = []
    if len(ts) < 2:
        return out
    for i in range(len(ts) - 1):
        v0, v1 = vals[i], vals[i + 1]
        if direction == "down":
            hit = v0 >= level > v1
        else:  # up
            hit = v0 <= level < v1
        if not hit or v1 == v0:
            continue
        frac = (level - v0) / (v1 - v0)  # ∈ [0, 1)
        out.append(float(ts[i] + frac * (ts[i + 1] - ts[i])))
    return out


def _gate_intervals(signals: dict, name: str, arm: float) -> List[Tuple[float, float]]:
    """信号值 >= arm 的连续时间区间。

    首端向后放宽 max(0.5s, 50*dt)：踏板到位总略早于车辆响应（TCS 起步的
    0.8 kph 穿越可能发生在踏板拉满曲线的毫秒级窗口内），放宽避免误裁剪；
    末端外扩 2 个采样间隔。
    """
    ts, vals = _series(signals, name)
    if len(ts) == 0:
        return []
    mask = vals >= arm
    intervals: List[Tuple[float, float]] = []
    start = None
    dt = float(np.median(np.diff(ts))) if len(ts) > 1 else 0.0
    lead = max(0.5, 50.0 * dt)
    for i, m in enumerate(mask):
        if m and start is None:
            start = ts[i]
        elif not m and start is not None:
            intervals.append((float(start - lead), float(ts[i] + 2 * dt)))
            start = None
    if start is not None:
        intervals.append((float(start - lead), float(ts[-1] + 2 * dt)))
    # 合并重叠区间
    merged: List[Tuple[float, float]] = []
    for a, b in intervals:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _pair_windows(
    gate: List[Tuple[float, float]],
    start_times: List[float],
    end_times: List[float],
) -> List[Tuple[float, float]]:
    """在门控区间内，把每个起点交叉与其后的最近终点交叉配对；丢弃找不到的。"""
    pairs: List[Tuple[float, float]] = []
    used_end = -1.0
    for g0, g1 in gate:
        s = [t for t in start_times if g0 <= t <= g1]
        for st in s:
            if st <= used_end:
                continue
            cands = [t for t in end_times if t > st]
            if not cands:
                continue
            et = min(cands)
            pairs.append((st, et))
            used_end = et
            break  # 每个门控区间最多一个事件
    return pairs


# ---------------------------------------------------------------- 分段模板

def seg_abs_full_brake(
    signals: dict,
    function_cfg: FunctionConfig,
    condition_cfg: ConditionConfig,
    params: dict,
) -> List[EventWindow]:
    v0 = float(params.get("v0_kph", 100.0))
    v_stop = float(params.get("v_stop_kph", 0.8))
    arm = float(params.get("pedal_arm_pct", 80.0))

    ts, sv = _series(signals, "speed_vbox")
    if len(ts) == 0:
        return []
    gates = _gate_intervals(signals, "BrakePedalPos", arm)
    if not gates:
        return []
    starts = _crossings(ts, sv, v0, "down")
    ends = _crossings(ts, sv, v_stop, "down")
    windows: List[EventWindow] = []
    for t0, t1 in _pair_windows(gates, starts, ends):
        if t1 <= t0:
            continue
        windows.append(EventWindow(t_start=t0, t_end=t1, anchor=t0,
                                   phases=["brake_request", "abs_regulation", "stop"]))
    return windows


def seg_tcs_full_throttle(
    signals: dict,
    function_cfg: FunctionConfig,
    condition_cfg: ConditionConfig,
    params: dict,
) -> List[EventWindow]:
    v_start = float(params.get("v_start_kph", 0.8))
    v_target = float(params.get("v_target_kph", 60.0))
    arm = float(params.get("pedal_arm_pct", 95.0))

    ts, sv = _series(signals, "speed_vbox")
    if len(ts) == 0:
        return []
    gates = _gate_intervals(signals, "ThrottlePedalPos", arm)
    if not gates:
        return []
    starts = _crossings(ts, sv, v_start, "up")
    ends = _crossings(ts, sv, v_target, "up")
    windows: List[EventWindow] = []
    for t0, t1 in _pair_windows(gates, starts, ends):
        if t1 <= t0:
            continue
        windows.append(EventWindow(t_start=t0, t_end=t1, anchor=t0,
                                   phases=["launch", "tcs_control", "target_speed"]))
    return windows


SEGMENTERS: Dict[str, Segmenter] = {
    "seg_abs_full_brake": seg_abs_full_brake,
    "seg_tcs_full_throttle": seg_tcs_full_throttle,
}


def degenerate_window(reason: str) -> EventWindow:
    """无有效事件时的退化窗口：指标层对其一律返回 missing。"""
    nan = float("nan")
    return EventWindow(t_start=nan, t_end=nan, anchor=nan, phases=[reason])


def is_valid(window: EventWindow) -> bool:
    return (
        not np.isnan(window.t_start)
        and not np.isnan(window.t_end)
        and window.t_end > window.t_start
    )
