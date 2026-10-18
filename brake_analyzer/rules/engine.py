"""规则引擎（design.md §6.5）。

- 规则基于指标状态触发，不重复比较阈值。
- 先功能级，再工况级；同一指标被工况级命中时，功能级同指标规则被覆盖。
- 输出 RuleVerdict[]，含方向性说明。
"""

from typing import Dict, List

from ..schemas import MetricResult, RuleConfig, RuleVerdict, decompose_metric_key


# missing 的三种成因给出的建议是相反的，规则结论必须跟着成因走：
# 缺通道 → 补测/查映射；没切出事件 → 这趟试验本身没做出来或工况选错。
_MISSING_CAUSE = {
    "no_valid_event": "本文件未切出该工况的有效事件窗口（试验过程不完整或工况选错）",
    "signal_absent": "指标所需通道无数据（未映射 / 未发布）",
    "not_computable": "窗口与通道都在，但量值算不出（端点越界等）",
    "no_matching_entry": "该档位下没有对应的指标条目",
}


def _direction(m: MetricResult) -> str:
    if m.status == "missing":
        return _MISSING_CAUSE.get(m.missing_reason or "", "无法计算（成因未知）")
    if m.ok_range is None:
        return ""
    lo, hi = m.ok_range
    if lo is not None and m.value < lo:
        return f"低于下界 {lo}{m.unit}"
    if hi is not None and m.value > hi:
        return f"高于上界 {hi}{m.unit}"
    return ""



def run_rules(
    metrics: List[MetricResult],
    rules: List[RuleConfig],
    function_key: str,
    condition_id: str,
) -> List[RuleVerdict]:
    by_key: Dict[str, MetricResult] = {
        decompose_metric_key(m.key)[1]: m for m in metrics
    }

    def _match(rule: RuleConfig) -> bool:
        m = by_key.get(rule.metric)
        return m is not None and m.status == rule.status

    # 工况级实际命中的指标 → 覆盖功能级同指标规则
    fired_cond = {
        r.metric for r in rules
        if r.scope == "condition" and r.function == function_key
        and r.condition == condition_id and _match(r)
    }

    verdicts: List[RuleVerdict] = []
    # 功能级在前，工况级覆盖
    ordered = [
        r for r in rules
        if r.function == function_key
        and (
            r.scope == "function"
            or (r.scope == "condition" and r.condition == condition_id)
        )
    ]
    for rule in ordered:
        if rule.scope == "function" and rule.metric in fired_cond:
            continue
        if not _match(rule):
            continue
        m = by_key[rule.metric]
        verdicts.append(
            RuleVerdict(
                rule_id=rule.id,
                metric_key=m.key,
                metric_name=m.name,
                status=m.status,
                severity=rule.severity,
                fault_domain=rule.fault_domain,
                message=rule.message,
                kb_ref=rule.kb_ref,
                direction=_direction(m),
                scope=rule.scope,
            )
        )
    return verdicts
