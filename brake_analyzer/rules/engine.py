"""规则引擎（design.md §6.5）。

- 规则基于指标状态触发，不重复比较阈值。
- 先功能级，再工况级；同一指标被工况级命中时，功能级同指标规则被覆盖。
- 输出 RuleVerdict[]，含方向性说明。
"""

from typing import Dict, List

from ..schemas import MetricResult, RuleConfig, RuleVerdict


def _direction(m: MetricResult) -> str:
    if m.status == "missing":
        return "无法计算（信号缺失、窗口退化或档位条目未命中）"
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
    by_key: Dict[str, MetricResult] = {m.key.split(".", 1)[1]: m for m in metrics}

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
