"""图表（design.md §6.6）：信号时序 → ECharts option JSON。

多 y 轴共享 x 轴：按 configs/signals_mf4.yaml 的 chart_layout 分组成
「多行一列」子图，同组信号共用一个 y 轴；事件窗口在每行都画 markArea。
"""

from typing import Dict, List, Optional, Tuple

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


def _series_data(signals: dict, name: str, window: Optional[tuple]):
    """单通道 → 降采样后的 [t, v] 点列；缺通道返回 None。"""
    sig = signals.get(name)
    if sig is None or len(sig.ts) == 0:
        return None
    ts = np.asarray(sig.ts, dtype=np.float64)
    vals = np.asarray(sig.values, dtype=np.float64)
    if window:
        t0, t1 = window
        if t0 is not None and not np.isnan(t0) and t1 is not None and not np.isnan(t1):
            pad = max(0.5, (t1 - t0) * 0.1)
            mask = (ts >= t0 - pad) & (ts <= t1 + pad)
            if mask.sum() >= 2:
                ts, vals = ts[mask], vals[mask]
    ts, vals = _downsample(ts, vals)
    return [
        [round(float(a), 4), None if np.isnan(b) else round(float(b), 4)]
        for a, b in zip(ts, vals)
    ]


def _groups_from_layout(signals: dict, layout, only: Optional[List[str]] = None) -> List[Tuple[str, str, List[str]]]:
    """配置分组 (label, unit, signals)；只保留数据里存在的通道。"""
    groups = []
    for g in layout or []:
        sigs = [s for s in g.signals if signals.get(s) is not None and len(signals[s].ts)]
        if only is not None:
            sigs = [s for s in sigs if s in only]
        if sigs:
            groups.append((g.label, g.unit or _unit_of(signals, sigs), sigs))
    return groups


def _groups_by_unit(signals: dict, names: List[str]) -> List[Tuple[str, str, List[str]]]:
    """无 chart_layout 时按 unit 自动分组，避免把不同量纲混在一轴。"""
    buckets: Dict[str, List[str]] = {}
    for n in names:
        sig = signals.get(n)
        if sig is None or len(sig.ts) == 0:
            continue
        buckets.setdefault(getattr(sig, "unit", "") or "", []).append(n)
    return [(unit or "值", unit, ns) for unit, ns in buckets.items()]


def _unit_of(signals: dict, names: List[str]) -> str:
    for n in names:
        u = getattr(signals.get(n), "unit", "") if signals.get(n) else ""
        if u:
            return u
    return ""


def build_chart_option(
    signals: dict,
    window: Optional[tuple] = None,
    series_names: Optional[List[str]] = None,
    highlight_status: Optional[str] = None,
    title: str = "",
    layout=None,
) -> dict:
    """window: (t_start, t_end) 用于截取范围与 markArea；None 画全程。

    layout: AppConfigs.chart_layout（List[ChartGroup]）；给出时按配置分行，
    否则按 unit 自动分行。返回的 option 为多 grid（多行一列、共享 x 轴）。
    """
    groups = _groups_from_layout(signals, layout, only=series_names) if layout else []
    if not groups:
        groups = _groups_by_unit(signals, series_names or DEFAULT_SERIES)

    n = max(1, len(groups))
    # 多行一列：绘图区从 12% 到 92% 均分，行间留 2% 间隔（x 轴刻度只在末行显示）
    span, pad_top, row_gap = 80.0, 12.0, 2.0
    row_h = (span - row_gap * (n - 1)) / n
    grids, x_axes, y_axes, series = [], [], [], []
    for i, (label, unit, sigs) in enumerate(groups):
        grids.append({
            "left": 62, "right": 26,
            "top": f"{pad_top + i * (row_h + row_gap):.2f}%",
            "height": f"{row_h:.2f}%",
        })
        x_axes.append({
            "gridIndex": i, "type": "value", "scale": True,
            "name": "s" if i == n - 1 else "", "nameTextStyle": {"fontSize": 10},
            "axisLabel": {"fontSize": 10, "show": i == n - 1},
            "splitLine": {"show": False},
        })
        y_axes.append({
            "gridIndex": i, "type": "value", "scale": True,
            "name": f"{label}" + (f" ({unit})" if unit else ""),
            "nameLocation": "end", "nameGap": 4,
            "nameTextStyle": {"fontSize": 10, "align": "left"},
            "axisLabel": {"fontSize": 9},
            "splitLine": {"lineStyle": {"color": "#1e293b"}},
        })

    color = STATUS_COLORS.get(highlight_status or "ok", STATUS_COLORS["ok"])
    mark_area = None
    if window:
        t0, t1 = window
        if t0 is not None and t1 is not None and not np.isnan(t0) and not np.isnan(t1):
            mark_area = {
                "silent": True,
                "itemStyle": {"color": color},
                "data": [[{"xAxis": round(float(t0), 4)}, {"xAxis": round(float(t1), 4)}]],
            }

    datasets: List[dict] = []
    x_min: Optional[float] = None
    x_max: Optional[float] = None
    for i, (label, unit, sigs) in enumerate(groups):
        first_in_row = True
        for name in sigs:
            data = _series_data(signals, name, window)
            if data is None:
                continue
            if data:
                lo, hi = data[0][0], data[-1][0]
                x_min = lo if x_min is None else min(x_min, lo)
                x_max = hi if x_max is None else max(x_max, hi)
            datasets.append({"name": name, "unit": unit, "group": label,
                             "groupIndex": i, "groupLabel": label, "data": data})
            s = {
                "name": name, "type": "line", "showSymbol": False, "large": True,
                "xAxisIndex": i, "yAxisIndex": i, "data": data,
                "groupIndex": i,  # 前端按组显隐时过滤用（ECharts 忽略未知键）
            }
            # 每行都标事件窗口（各 grid 独立，需各自画一份）
            if mark_area and first_in_row:
                s["markArea"] = mark_area
                first_in_row = False
            series.append(s)

    # 共享 x 轴：所有行锁定同一量程，否则各 grid 自动缩放导致视觉上不对齐
    if x_min is not None and x_max is not None and x_max > x_min:
        for ax in x_axes:
            ax["min"] = round(x_min, 3)
            ax["max"] = round(x_max, 3)
            ax["scale"] = False

    option = {
        "title": {"text": title, "left": "center", "textStyle": {"fontSize": 13}},
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "cross", "link": [{"xAxisIndex": "all"}]}},
        "axisPointer": {"link": [{"xAxisIndex": "all"}]},
        # 测线选择交给前端分组勾选，ECharts 图例关闭以免与多行标题抢空间
        "legend": {"show": False},
        "grid": grids,
        "xAxis": x_axes,
        "yAxis": y_axes,
        "dataZoom": [
            {"type": "inside", "xAxisIndex": list(range(len(grids)))},
            {"type": "slider", "xAxisIndex": list(range(len(grids))),
             "bottom": 6, "height": 14, "start": 0, "end": 100},
        ],
        "series": series,
        "_datasets": datasets,          # 前端按组渲染 y 轴/图例时使用
        "_groups": [                   # 组元信息：前端画小标题、按组勾选
            {"index": i, "label": g[0], "unit": g[1], "signals": g[2]}
            for i, g in enumerate(groups)
        ],
    }
    return option


def timeseries_payload(
    signals: dict,
    window: Optional[tuple] = None,
    series_names: Optional[List[str]] = None,
    layout=None,
) -> Dict:
    """GET /api/analyses/{id}/timeseries 的 data 段。"""
    option = build_chart_option(signals, window=window, series_names=series_names, layout=layout)
    return {
        "window": None if window is None else [float(window[0]), float(window[1])],
        "series": option["_datasets"],
        "groups": option["_groups"],
    }
