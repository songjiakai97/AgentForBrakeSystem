"""指标计算引擎（design.md §10.5 / §10.6）。"""

from typing import List, Optional

from ..configs import pick
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
                    key=f"{condition_cfg.id}.{metric_key}",
                    name=tmpl.name,
                    category=tmpl.category,
                    value=None,
                    unit=tmpl.unit,
                    ts_range=None,
                    status="missing",
                    raw={"reason": "no_matching_entry"},
                    ok_range=None,
                    profile=profile,
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

        results.append(
            MetricResult(
                key=f"{condition_cfg.id}.{metric_key}",
                name=tmpl.name,
                category=tmpl.category,
                value=value,
                unit=tmpl.unit,
                ts_range=(window.t_start, window.t_end),
                status=status,
                raw=raw,
                ok_range=entry.get("ok_range"),
                profile=entry.get("profile"),
            )
        )
    return results
