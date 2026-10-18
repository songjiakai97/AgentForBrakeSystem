"""指标计算引擎（design.md §10.5 / §10.6）。"""

from typing import List, Optional

import numpy as np

from ..configs import pick
from ..schemas import compose_metric_key
from ..schemas import ConditionConfig, EventWindow, MetricResult
from .base import get_tool
from . import tools as _tools  # noqa: F401  注册全部工具


def _classify(value, entry: dict) -> str:
    if value is None:
        return "missing"
    if "ok_range" not in entry or entry["ok_range"] is None:
        return "info"
    lo, hi = entry["ok_range"]
    ok = (lo is None or value >= lo) and (hi is None or value <= hi)
    return "ok" if ok else "abnormal"


def _missing_reason(signals: dict, window, tmpl) -> str:
    """missing 的成因。三种不能混为一谈，否则给用户的话就错了：

    - no_valid_event：分段器没切出该工况的完整过程（没踩到门限、没达到判定车速）。
      这是「试验没做出来 / 工况选错」，跟通道有没有无关。
    - signal_absent：窗口是好的，但指标要用的通道没有数据（未映射、CAN 上没发布）。
    - not_computable：窗口和通道都在，工具算不出值（如插值点落在信号范围外）。
    """
    t0, t1 = getattr(window, "t_start", None), getattr(window, "t_end", None)
    if (t0 is None or t1 is None or np.isnan(t0) or np.isnan(t1) or t1 <= t0):
        return "no_valid_event"
    for name in tmpl.inputs or []:
        sig = signals.get(name)
        if sig is None or len(sig.ts) == 0:
            return "signal_absent"
    return "not_computable"


def compute_metrics(
    signals: dict,
    function_cfg,
    condition_cfg: ConditionConfig,
    window: EventWindow,
    metrics_cfg: dict,
    profile: Optional[str] = None,
) -> List[MetricResult]:
    index = condition_cfg.metric_index
    enabled = condition_cfg.enabled_metrics

    results: List[MetricResult] = []
    for metric_key in enabled:
        tmpl = metrics_cfg.get(metric_key)
        if tmpl is None:
            continue

        entry = pick(index, metric_key, profile)
        if entry is None:
            results.append(
                MetricResult(
                    key=compose_metric_key(condition_cfg.id, metric_key),
                    name=tmpl.name,
                    category=tmpl.category,
                    value=None,
                    unit=tmpl.unit,
                    ts_range=None,
                    status="missing",
                    raw={"reason": "no_matching_entry"},
                    ok_range=None,
                    profile=profile,
                    missing_reason="no_matching_entry",
                )
            )
            continue

        try:
            tool = get_tool(tmpl.tool)
            tool_params = dict(entry.get("params") or {})
            if tmpl.inputs:
                tool_params.setdefault("signal", tmpl.inputs[0])
                tool_params.setdefault("signals", list(tmpl.inputs))
            tool_params["inputs"] = list(tmpl.inputs)
            raw = tool(
                signals,
                condition_cfg,
                (window.t_start, window.t_end),
                tool_params,
            )
            if not isinstance(raw, dict):
                raw = {"value": None}
            value = raw.get("value")
            if value is not None:
                value = float(value)
                if value != value:  # NaN → 视为无法计算
                    value = None
        except Exception as e:  # 工具契约要求不抛异常，此处兜底
            raw, value = {"error": str(e)}, None

        status = _classify(value, entry)
        reason = _missing_reason(signals, window, tmpl) if status == "missing" else None

        results.append(
            MetricResult(
                key=compose_metric_key(condition_cfg.id, metric_key),
                name=tmpl.name,
                category=tmpl.category,
                value=value,
                unit=tmpl.unit,
                ts_range=(window.t_start, window.t_end),
                status=status,
                raw=raw,
                ok_range=entry.get("ok_range"),
                profile=entry.get("profile"),
                missing_reason=reason,
            )
        )
    return results
