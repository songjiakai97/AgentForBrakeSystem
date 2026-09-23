"""内核共享数据结构（design.md §6 / §10）。"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class FunctionConfig:
    key: str
    name: str
    aliases: List[str] = field(default_factory=list)
    segmenter: str = ""
    kb_section: str = ""
    description: str = ""
    profiles: List[str] = field(default_factory=list)        # 展平后的全部单维标签
    profile_dims: List[List[str]] = field(default_factory=list)  # 维度串，如 [[4WD,2WD],[DTCS,TCS]]


@dataclass
class ConditionConfig:
    id: str
    function: str
    name: str
    maneuver: str = ""
    surface_code: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    metrics: List[Dict[str, Any]] = field(default_factory=list)
    # 加载期构建
    metric_index: Dict[str, dict] = field(default_factory=dict)
    enabled_metrics: List[str] = field(default_factory=list)


@dataclass
class MetricTemplate:
    key: str
    name: str
    tool: str
    category: str
    unit: str
    inputs: List[str] = field(default_factory=list)
    tuning_params: List[str] = field(default_factory=list)
    description: str = ""


@dataclass
class RuleConfig:
    id: str
    scope: str                 # function | condition
    function: str
    metric: str
    status: str                # abnormal | missing
    severity: str
    fault_domain: str
    kb_ref: str = ""
    message: str = ""
    condition: Optional[str] = None


@dataclass
class EventWindow:
    t_start: float
    t_end: float
    anchor: float = 0.0
    phases: List[str] = field(default_factory=list)


@dataclass
class MetricResult:
    key: str                   # {condition_id}.{metric_key}
    name: str
    category: str
    value: Optional[float]
    unit: str
    ts_range: Optional[Tuple[float, float]]
    status: str                # ok | abnormal | missing | info
    raw: Dict[str, Any] = field(default_factory=dict)
    ok_range: Optional[list] = None
    profile: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "category": self.category,
            "value": self.value,
            "unit": self.unit,
            "ts_range": list(self.ts_range) if self.ts_range else None,
            "status": self.status,
            "ok_range": self.ok_range,
            "profile": self.profile,
        }


@dataclass
class RuleVerdict:
    rule_id: str
    metric_key: str
    metric_name: str
    status: str
    severity: str
    fault_domain: str
    message: str
    kb_ref: str = ""
    direction: str = ""        # 方向性说明（低于下界/高于上界/缺失）
    scope: str = "function"

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "metric_key": self.metric_key,
            "metric_name": self.metric_name,
            "status": self.status,
            "severity": self.severity,
            "fault_domain": self.fault_domain,
            "message": self.message,
            "kb_ref": self.kb_ref,
            "direction": self.direction,
            "scope": self.scope,
        }


@dataclass
class RunSample:
    """一次分析中的一个事件样本（run-sample = 文件内一条事件窗口）。"""
    sample_id: str
    file_id: str
    file_name: str
    run_index: int
    window: EventWindow
    metrics: List[MetricResult] = field(default_factory=list)
    verdicts: List[RuleVerdict] = field(default_factory=list)
    analysis_id: str = ""

    def status_summary(self) -> Dict[str, int]:
        out = {"ok": 0, "abnormal": 0, "missing": 0, "info": 0}
        for m in self.metrics:
            out[m.status] = out.get(m.status, 0) + 1
        return out


@dataclass
class AnalysisResult:
    analysis_id: str
    condition_id: str
    condition_name: str
    function_key: str
    profile: Optional[str]
    file_id: str
    file_name: str
    samples: List[RunSample] = field(default_factory=list)
    llm_suggestion: Optional[dict] = None   # calibration_actions 等，缺 Key 时为 None
    degraded: bool = False                  # LLM 降级标记

    def to_dict(self, include_samples: bool = True) -> dict:
        d = {
            "analysis_id": self.analysis_id,
            "condition_id": self.condition_id,
            "condition_name": self.condition_name,
            "function_key": self.function_key,
            "profile": self.profile,
            "file_id": self.file_id,
            "file_name": self.file_name,
            "sample_count": len(self.samples),
            "degraded": self.degraded,
        }
        if self.llm_suggestion is not None:
            d["llm_suggestion"] = self.llm_suggestion
        if include_samples:
            d["samples"] = [
                {
                    "sample_id": s.sample_id,
                    "run_index": s.run_index,
                    "file_name": s.file_name,
                    "window": {
                        "t_start": s.window.t_start,
                        "t_end": s.window.t_end,
                        "anchor": s.window.anchor,
                    },
                    "analysis_id": s.analysis_id,
                    "status_summary": s.status_summary(),
                    "metrics": [m.to_dict() for m in s.metrics],
                    "verdicts": [v.to_dict() for v in s.verdicts],
                }
                for s in self.samples
            ]
        return d
