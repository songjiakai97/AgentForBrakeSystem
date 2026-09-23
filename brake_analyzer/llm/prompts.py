"""Prompt 组装（design.md §6.8 / §6.9 / §11.4）。"""

import json
from typing import List, Optional

from ..configs import AppConfigs
from ..schemas import AnalysisResult, ConditionConfig, FunctionConfig

SYSTEM_CHAT = """你是制动系统测试数据分析与标定指导助手，服务于台架/实车测试数据的分析与标定决策。

职责：
1. 根据用户描述，使用 recommend_targets 工具识别要分析的功能与工况（两跳）；
2. 目标唯一后使用 run_analysis_on_files 对会话内已上传文件执行分析；
3. 基于分析结果与规则结论，给出可执行的标定方向建议。

约束：
- 可标定量只能引用指标声明的 tuning_params，不得编造不存在的参数；
- 区分「确定规则结论」（来自规则引擎）与「建议性判断」（你的推断）；
- 不得基于 info 指标单独下异常结论，info 只能作为佐证或趋势描述；
- 数据缺失（missing）时优先提示补测或检查通道映射，不得臆断性能结论；
- 回答使用简体中文，简洁、面向工程师。"""

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "recommend_targets",
            "description": "两跳推荐入口：根据用户 query 识别功能/工况目标。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "用户意图原话或要点"},
                    "max_items": {"type": "integer", "description": "最多返回候选数，1~8"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_analysis_on_files",
            "description": "对当前会话内全部已上传数据文件执行指定工况分析。仅在目标工况已确定后调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "condition_id": {"type": "string", "description": "已确定的工况 id"},
                    "profile": {"type": "string", "description": "档位标签，可选；多维度用竖线拼接如 4WD|DTCS"},
                },
                "required": ["condition_id"],
            },
        },
    },
]


def function_catalog_text(cfg: AppConfigs) -> str:
    lines = []
    for fn in cfg.functions.values():
        conds = "; ".join(c.name for c in cfg.conditions_of(fn.key))
        prof = ",".join(fn.profiles) if fn.profiles else "-"
        lines.append(f"- {fn.key}（{fn.name}；别名: {','.join(fn.aliases)}；档位: {prof}；工况: {conds}）")
    return "\n".join(lines)


def build_chat_messages(
    cfg: AppConfigs,
    history: List[dict],
    user_text: str,
    file_names: List[str],
    selected: dict,
) -> List[dict]:
    ctx = [
        f"可用功能目录:\n{function_catalog_text(cfg)}",
        f"会话已上传文件: {', '.join(file_names) if file_names else '（无）'}",
    ]
    if selected.get("condition"):
        ctx.append(f"当前已选定工况: {selected['condition']}" + (
            f"（档位 {selected['profile']}）" if selected.get("profile") else ""))
    system = SYSTEM_CHAT + "\n\n<上下文>\n" + "\n".join(ctx) + "\n</上下文>"
    messages = [{"role": "system", "content": system}]
    for h in history[-8:]:
        if h.get("role") in ("user", "assistant") and h.get("kind") in (None, "text"):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_text})
    return messages


def suggestion_prompt(
    cfg: AppConfigs,
    fn: FunctionConfig,
    cond: ConditionConfig,
    result: AnalysisResult,
    kb_snippets: List[str],
) -> Optional[str]:
    """规则结论 → LLM 标定建议的用户消息；无异常/缺失时返回 None（无需建议）。"""
    problems = []
    for s in result.samples:
        for m in s.metrics:
            if m.status in ("abnormal", "missing"):
                tmpl = cfg.metrics.get(m.key.rsplit(".", 1)[1])
                tp = tmpl.tuning_params if tmpl else []
                problems.append({
                    "run": s.run_index,
                    "metric": m.name,
                    "metric_key": m.key.rsplit(".", 1)[1],
                    "status": m.status,
                    "value": m.value,
                    "unit": m.unit,
                    "ok_range": m.ok_range,
                    "profile": m.profile,
                    "tuning_params": tp,
                    "verdicts": [
                        {"rule_id": v.rule_id, "message": v.message,
                         "direction": v.direction, "severity": v.severity}
                        for v in s.verdicts if v.metric_key == m.key
                    ],
                })
    if not problems:
        return None
    payload = {
        "function": f"{fn.key}（{fn.name}）",
        "condition": f"{cond.id}（{cond.name}）",
        "profile": result.profile,
        "problems": problems,
    }
    parts = [
        "以下是一次分析的异常/缺失指标与规则结论（JSON）：",
        json.dumps(payload, ensure_ascii=False, indent=1),
    ]
    if kb_snippets:
        parts.append("规范依据（标定手册摘录，只能作为硬约束引用）：")
        parts.extend(kb_snippets)
        parts.append("历史案例（仅供参考，注意与本车差异）：见上，若无则忽略。")
    parts.append(
        "请输出结构化 JSON：{\"summary\": \"一句话结论\", \"calibration_actions\": "
        "[{\"param\": \"<必须取自该指标 tuning_params>\", \"direction\": \"increase|decrease|investigate\", "
        "\"expected_effect\": \"...\", \"risk\": \"...\", \"metric\": \"<关联 metric_key>\"}]}。"
        "只处理 abnormal/missing 指标；missing 指标优先给数据质量动作。"
    )
    return "\n".join(parts)
