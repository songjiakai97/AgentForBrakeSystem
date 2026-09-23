"""图表（design.md §6.6）：信号时序 → ECharts option JSON。

多测线叠加 + 事件窗口 markArea 高亮；异常指标所在窗口高亮。
"""

from typing import Dict, List, Optional

import numpy as np

# 默认叠加的测线（存在即画）
DEFAULT_SERIES = [
    "speed_vbox", "yaw_rate", "Ax",
    "wheelSpeed_FL", "wheelSpeed_FR", "wheelSpeed_RL", "wheelSpeed_RR",
    "BrakePedalPos", "ThrottlePedalPos", "ABS_Active", "TCS_Active",
]

STATUS_COLORS = {
    "abnormal": "rgba(220, 68, 68, 0.15)",
    "missing": "rgba(148, 163, 184, 0.20)",
    "ok": "rgba(34, 197, 94, 0.10)",
}


def _downsample(ts: np.ndarray, vals: np.ndarray, max_points: int = 1200):
    if ts.size <= max_points:
        return ts, vals
    idx = np.linspace(0, ts.size - 1, max_points).round().astype(int)
    return ts[idx], vals[idx]


def build_chart_option(
    signals: dict,
    window: Optional[tuple] = None,
    series_names: Optional[List[str]] = None,
    highlight_status: Optional[str] = None,
    title: str = "",
) -> dict:
    """window: (t_start, t_end) 用于截取范围与 markArea；None 画全程。"""
    names = series_names or DEFAULT_SERIES
    t0, t1 = (window if window else (None, None))
    datasets: List[dict] = []
    for name in names:
        sig = signals.get(name)
        if sig is None or len(sig.ts) == 0:
            continue
        ts = np.asarray(sig.ts, dtype=np.float64)
        vals = np.asarray(sig.values, dtype=np.float64)
        if t0 is not None and not np.isnan(t0) and t1 is not None and not np.isnan(t1):
            pad = max(0.5, (t1 - t0) * 0.1)
            mask = (ts >= t0 - pad) & (ts <= t1 + pad)
            if mask.sum() >= 2:
                ts, vals = ts[mask], vals[mask]
        ts, vals = _downsample(ts, vals)
        datasets.append(
            {
                "name": name,
                "unit": getattr(sig, "unit", ""),
                "data": [[round(float(a), 4), None if np.isnan(b) else round(float(b), 4)] for a, b in zip(ts, vals)],
            }
        )

    mark_area = None
    if window and t0 is not None and not np.isnan(t0) and not np.isnan(t1):
        color = STATUS_COLORS.get(highlight_status or "ok", STATUS_COLORS["ok"])
        mark_area = {
            "silent": True,
            "itemStyle": {"color": color},
            "data": [[{"xAxis": round(float(t0), 4)}, {"xAxis": round(float(t1), 4)}]],
        }

    option = {
        "title": {"text": title, "left": "center", "textStyle": {"fontSize": 13}},
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "cross"}},
        "legend": {"type": "scroll", "bottom": 0},
        "grid": {"left": 55, "right": 25, "top": 40, "bottom": 50},
        "xAxis": {"type": "value", "name": "s", "scale": True},
        "yAxis": {"type": "value", "scale": True},
        "dataZoom": [
            {"type": "inside"},
            {"type": "slider", "bottom": 25, "height": 14},
        ],
        "series": [],
        "_datasets": datasets,  # 前端按单位拆分 y 轴时使用的原始数据
    }
    # 兼容单 series 直绘（主信号）
    if datasets:
        primary = next((d for d in datasets if d["name"] == "speed_vbox"), datasets[0])
        series = {
            "name": primary["name"],
            "type": "line",
            "showSymbol": False,
            "data": primary["data"],
        }
        if mark_area:
            series["markArea"] = mark_area
        option["series"] = [series]
        option["yAxis"]["name"] = primary.get("unit", "")
    return option


def timeseries_payload(
    signals: dict,
    window: Optional[tuple] = None,
    series_names: Optional[List[str]] = None,
) -> Dict:
    """GET /api/analyses/{id}/timeseries 的 data 段。"""
    option = build_chart_option(signals, window=window, series_names=series_names)
    return {
        "window": None if window is None else [float(window[0]), float(window[1])],
        "series": option["_datasets"],
    }
