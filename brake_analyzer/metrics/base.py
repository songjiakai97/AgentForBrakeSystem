"""指标工具注册表与契约（design.md §10.7 / demo_conditions_metrics.md §4）。

工具契约：(signals, condition, window, params) -> {"value": float|None, ...}
- window: (t_start, t_end) 秒，端点可能为 NaN（退化窗口）
- 缺信号、窗口退化、无法计算 → {"value": None}，绝不抛异常
- value 即最终判定值，引擎不做符号加工
"""

from typing import Callable, Dict, Optional

import numpy as np

KMH_TO_MS = 1000.0 / 3600.0

# signals: Dict[str, SignalData]; condition: ConditionConfig;
# window: Tuple[float, float]; params: dict | None
Tool = Callable[[dict, object, tuple, Optional[dict]], dict]

TOOLS: Dict[str, Tool] = {}


def register_tool(name: str):
    def deco(fn: Tool) -> Tool:
        if name in TOOLS:
            raise ValueError(f"指标工具重复注册: {name}")
        TOOLS[name] = fn
        return fn

    return deco


def get_tool(name: str) -> Tool:
    if name not in TOOLS:
        raise KeyError(f"指标工具 '{name}' 未注册，可用: {sorted(TOOLS)}")
    return TOOLS[name]


def window_slice(signals: dict, name: str, window: tuple):
    """返回窗口内 (ts, values)；信号缺失或窗口退化返回 (None, None)。"""
    t0, t1 = window
    if t0 is None or t1 is None or np.isnan(t0) or np.isnan(t1) or t1 <= t0:
        return None, None
    sig = signals.get(name)
    if sig is None or len(sig.ts) == 0:
        return None, None
    ts = np.asarray(sig.ts, dtype=np.float64)
    vals = np.asarray(sig.values, dtype=np.float64)
    order = np.argsort(ts, kind="stable")
    ts, vals = ts[order], vals[order]
    mask = (ts >= t0) & (ts <= t1)
    if mask.sum() == 0:
        return None, None
    return ts[mask], vals[mask]


def interp_at(signals: dict, name: str, t: float) -> Optional[float]:
    """在时刻 t 对信号线性插值；t 超出信号范围返回 None。"""
    sig = signals.get(name)
    if sig is None or len(sig.ts) == 0:
        return None
    ts = np.asarray(sig.ts, dtype=np.float64)
    vals = np.asarray(sig.values, dtype=np.float64)
    order = np.argsort(ts, kind="stable")
    ts, vals = ts[order], vals[order]
    if t is None or np.isnan(t) or t < ts[0] or t > ts[-1]:
        return None
    return float(np.interp(t, ts, vals))
