"""事件分段与时间窗（design.md §6.3 / demo_conditions_metrics.md §5）。

分段模板在代码中实现并注册于 SEGMENTERS，不进入 YAML；按功能选一个，
工况可以覆盖（如对接路面不看速度区间，改看 ABS 激活区间/全油门时长）。
所有速度交叉点在本层用线性插值定位，指标工具只在窗口内做纯计算。

车速穿越模板（窗口 = 整段考核过程，可支撑「制动距离/加速时间」）：
- seg_abs_full_brake:      BrakePedalPos >= arm 区间内，speed_vbox 下降穿越 v0_kph
                           → 下降穿越 v_stop_kph 的配对。
- seg_tcs_full_throttle:   ThrottlePedalPos >= arm 区间内，speed_vbox 上升穿越 v_start_kph
                           → 上升穿越 v_target_kph 的配对。

信号区间模板（窗口 = 某个状态位/踏板的持续区间，与车速无关；对接路面两端
车速都不确定，速度穿越切不出可靠的窗）：
- seg_abs_active:          ABS_Active 高电平区间（相邻脉冲间隙小于
                           ABS_ACTIVE_MERGE_GAP_S 视为同一段调节）。
- seg_tcs_pedal_time:      ThrottlePedalPos >= arm 的整段，即「全油门时间」。

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
    """相邻样本线性插值求穿越时刻列表（numpy 向量化）。direction: 'down' | 'up'。"""
    if len(ts) < 2:
        return []
    v0, v1 = vals[:-1], vals[1:]
    if direction == "down":
        hit = (v0 >= level) & (v1 < level)
    else:  # up
        hit = (v0 <= level) & (v1 > level)
    idx = np.nonzero(hit & (v0 != v1))[0]
    if len(idx) == 0:
        return []
    frac = (level - v0[idx]) / (v1[idx] - v0[idx])   # ∈ [0, 1)
    return [float(t) for t in ts[idx] + frac * (ts[idx + 1] - ts[idx])]


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


def _high_intervals(
    signals: dict,
    name: str,
    level: float = 0.5,
    merge_gap: float = 0.0,
    min_dur: float = 0.0,
) -> List[Tuple[float, float]]:
    """信号值 >= level 的连续区间，端点取真实边沿（不外扩）。

    merge_gap：间隙小于该值的相邻区间合并——状态位（如 ABS_Active）按压力
    调节脉冲发布，0/1 会来回跳，不合并会把一段调节切成一堆毫秒级窗口。
    min_dur：合并后仍短于该值的丢弃，防单个毛刺采样点变成一个事件。
    """
    ts, vals = _series(signals, name)
    if len(ts) == 0:
        return []
    mask = vals >= level
    raw: List[Tuple[float, float]] = []
    start = None
    for i, m in enumerate(mask):
        if m and start is None:
            start = float(ts[i])
        elif not m and start is not None:
            raw.append((start, float(ts[i - 1])))
            start = None
    if start is not None:
        raw.append((start, float(ts[-1])))

    merged: List[Tuple[float, float]] = []
    for a, b in raw:
        if merged and a - merged[-1][1] <= merge_gap:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return [(a, b) for a, b in merged if b - a >= min_dur]


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


SEGMENTERS: Dict[str, Segmenter] = {}

# 信号区间模板的判据常量。写死在代码里而不是塞进 conditions.yaml 的 params：
# params 是工况唯一键的一部分（maneuver+surface_code+params），加进去会改变
# 工况身份，也让「这些数是什么」暴露给模型和用户。
ABS_ACTIVE_LEVEL = 0.5
ABS_ACTIVE_MERGE_GAP_S = 0.3   # 压力调节脉冲间隙，小于它的 0/1 跳动算同一段
ABS_ACTIVE_MIN_DUR_S = 0.2


def seg_abs_active(
    signals: dict,
    function_cfg: FunctionConfig,
    condition_cfg: ConditionConfig,
    params: dict,
) -> List[EventWindow]:
    """窗口 = ABS 激活区间（对接路面两端车速不确定，不能按速度穿越切）。"""
    windows: List[EventWindow] = []
    for t0, t1 in _high_intervals(
        signals, "ABS_Active",
        level=ABS_ACTIVE_LEVEL,
        merge_gap=ABS_ACTIVE_MERGE_GAP_S,
        min_dur=ABS_ACTIVE_MIN_DUR_S,
    ):
        windows.append(EventWindow(t_start=t0, t_end=t1, anchor=t0,
                                   phases=["abs_regulation"]))
    return windows


def seg_tcs_pedal_time(
    signals: dict,
    function_cfg: FunctionConfig,
    condition_cfg: ConditionConfig,
    params: dict,
) -> List[EventWindow]:
    """窗口 = 全油门时间（ThrottlePedalPos >= arm 的整段），不设目标车速。"""
    arm = float(params.get("pedal_arm_pct", 95.0))
    windows: List[EventWindow] = []
    for t0, t1 in _high_intervals(signals, "ThrottlePedalPos", level=arm):
        windows.append(EventWindow(t_start=t0, t_end=t1, anchor=t0,
                                   phases=["full_throttle"]))
    return windows


SEGMENTERS.update({
    "seg_abs_full_brake": seg_abs_full_brake,
    "seg_tcs_full_throttle": seg_tcs_full_throttle,
    "seg_abs_active": seg_abs_active,
    "seg_tcs_pedal_time": seg_tcs_pedal_time,
})

# 窗口 = 整段车速区间的模板。只有它们切出的窗才能支撑「窗口时长即加速时间」
# 「窗口起止距离差即制动距离」这两类指标；换了模板这两个指标的物理含义就不成立，
# 由配置层拦下（§9.4）。
SPEED_SPAN_SEGMENTERS = {"seg_abs_full_brake", "seg_tcs_full_throttle"}
SPEED_SPAN_METRICS = {"brake_distance", "acc_time"}

# 分段器实际读取的通道。配置层据此算出「参与计算的信号集」（§9.6）：
# 这些通道任何数据格式都必须提供，缺了就不是判定偏严，而是根本切不出事件窗口。
# 写在这里而不是让 configs.py 猜函数体，是因为只有本模块知道自己读了什么。
SEGMENTER_SIGNALS: Dict[str, List[str]] = {
    "seg_abs_full_brake": ["speed_vbox", "BrakePedalPos"],
    "seg_tcs_full_throttle": ["speed_vbox", "ThrottlePedalPos"],
    "seg_abs_active": ["ABS_Active"],
    "seg_tcs_pedal_time": ["ThrottlePedalPos"],
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
