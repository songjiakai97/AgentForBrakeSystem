"""Prompt 组装（design.md §6.8 / §6.9 / §11.4）。"""

import json
from typing import List, Optional

from ..configs import AppConfigs
from ..schemas import AnalysisResult, ConditionConfig, FunctionConfig

SYSTEM_CHAT = """你是制动系统测试数据分析与标定指导助手，服务于台架/实车测试数据的分析与标定决策。

职责：
1. 从用户描述里判定功能（功能目录见下），再用 resolve_condition 按结构化槽位
   （动作 maneuver、路面 surface_code、工况参数 params）定位具体工况；
   描述不足以填满唯一键时不要猜测，用 list_conditions 取该功能工况清单后弹候选；
2. 目标唯一确定后使用 run_analysis_on_files 对会话内已上传文件执行分析；
3. 基于分析结果与规则结论，给出可执行的标定方向建议。

约束：
- 工况目录不常驻在本提示里：需要时按功能调 list_conditions，只有当次请求能看到；
- 只有 resolve_condition 返回 hop=resolved（唯一键精确命中）才可以直接分析；
  返回 hop=condition/function 表示信息不足，必须交用户确认，不得自选一条；
- 功能声明了档位（目录里的「档位:」）而用户没说选哪个时，先问档位，不要默认挑一个；
- 可标定量只能引用指标声明的 tuning_params，不得编造不存在的参数；
- 区分「确定规则结论」（来自规则引擎）与「建议性判断」（你的推断）；
- 不得基于 info 指标单独下异常结论，info 只能作为佐证或趋势描述；
- 数据缺失（missing）时优先提示补测或检查通道映射，不得臆断性能结论；
- 实测值一律直接引用 run_analysis_on_files 返回的 results（值/单位/限值/判定），
  不得让用户自查图表；
- 回答使用简体中文，简洁、面向工程师。"""

HISTORY_TAIL = 40   # 回灌的历史条数上限：只是保险丝，真正的长度由 token 预算控制
TOOL_PAYLOAD_CLIP_CHARS = 4000   # 单个工具结果写入上下文的字数上限


def build_tool_specs(cfg: AppConfigs, scope_function: Optional[str] = None) -> List[dict]:
    """工具规格：维度取值与工况 id 用配置生成的 enum 约束（§13）。

    - 常驻成本由「维度数」决定，不由「工况数」决定：maneuver/surface_code 的取值域
      通常十几个以内，工况清单不进 system prompt。
    - scope_function 已知时，condition_id 直接限定为该功能下的工况 id，非法 id
      在结构上不可能出现；未知时退化为自由字符串。
    """
    vocab = cfg.vocab()
    fn_keys = list(cfg.functions)
    # number 同时覆盖整数与小数（整数是 number 的子集），避免个别兼容端点拒绝 type 数组
    param_props = {k: {"type": "number", "description": f"工况参数 {k}"}
                   for k in vocab["param_keys"]}
    cond_schema = ({"type": "string", "enum": cfg.scope_conditions(scope_function)}
                   if scope_function and cfg.scope_conditions(scope_function)
                   else {"type": "string",
                         "description": "工况 id，必须是 resolve_condition 命中的那条"})
    # 档位：resolved 只保证工况唯一，不保证档位唯一。这里只描述、不设为 required ——
    # 设为必填会逼模型编一个档位，正确做法是先问用户，未选档时由引擎守卫弹面板（§6.9）
    scope_fn = cfg.function(scope_function) if scope_function else None
    profile_desc = "档位标签，可选；多维度用竖线拼接如 4WD|DTCS"
    if scope_fn is not None and scope_fn.profiles:
        profile_desc = ("该功能声明了档位，取值 " + "|".join(scope_fn.profiles)
                        + "（多维度用竖线拼接如 4WD|DTCS）。用户没说选哪个时不要自己定，"
                        "先向用户确认；未带档位调用时引擎不会执行分析，而是给用户弹档位面板")
    return [
        {
            "type": "function",
            "function": {
                "name": "resolve_condition",
                "description": (
                    "按 §2.2 唯一键（maneuver+surface_code+params）精确定位工况。"
                    "命中唯一返回 hop=resolved，信息不足返回 hop=condition/function 并给候选——"
                    "此时必须交用户确认，不得自行选择。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "function_key": {
                            "type": "string", "enum": fn_keys,
                            "description": "已判定的功能；不确定时可省略",
                        },
                        "maneuver": {
                            "type": "string", "enum": vocab["maneuver"],
                            "description": "测试动作",
                        },
                        "surface_code": {
                            "type": "string", "enum": vocab["surface_code"],
                            "description": "路面/附着条件",
                        },
                        "params": {
                            "type": "object",
                            "properties": param_props,
                            "description": "工况参数，键取自参数名（如 v0_kph），值按描述填",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_conditions",
                "description": (
                    "取某功能下的全部工况（含唯一键属性），仅本轮可见，用完即弃。"
                    "在描述无法填满唯一键、或用户询问「这个功能下测了哪些」时调用。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"function_key": {"type": "string", "enum": fn_keys}},
                    "required": ["function_key"],
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
                        "condition_id": cond_schema,
                        "profile": {
                            "type": "string",
                            "description": profile_desc,
                        },
                    },
                    "required": ["condition_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "recommend_targets",
                "description": "规则打分兜底：无法结构化解析时，用原话取功能/工况候选。",
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
    ]


def function_catalog_text(cfg: AppConfigs, include_conditions: bool = False) -> str:
    """常驻目录只到功能层（§13）；工况层由 list_conditions 按需注入。

    include_conditions=True 保留旧的完整拼法，供离线展示与回归对比用。
    """
    lines = []
    for fn in cfg.functions.values():
        prof = ",".join(fn.profiles) if fn.profiles else "-"
        base = (f"- {fn.key}（{fn.name}；别名: {','.join(fn.aliases)}；档位: {prof}；"
                f"工况数: {len(cfg.conditions_of(fn.key))}）")
        if include_conditions:
            conds = "; ".join(c.name for c in cfg.conditions_of(fn.key))
            base = (f"- {fn.key}（{fn.name}；别名: {','.join(fn.aliases)}；档位: {prof}；"
                    f"工况: {conds}）")
        lines.append(base)
    return "\n".join(lines)


SUMMARY_SYSTEM = """你是对话压缩器。把制动测试分析对话的早期内容压成要点摘要，供后续轮次作为背景。

要求：
- 只保留后续决策仍需要的信息：已确定的功能/工况/档位、已分析的文件与结论要点、
  异常或缺失指标（含关键数值与单位）、用户明确表达过的约束与偏好、未完成的待办；
- 丢弃寒暄、重复表述、候选列表原文、工具调用的实现细节；
- 用简体中文陈述条目，不加评论、不编造未出现过的数值；
- 输出控制在给定 token 预算内，超预算时优先丢弃信息量最低的条目。"""


def summarize_request(transcript: str, budget_tokens: int) -> List[dict]:
    """摘要子请求的 messages：独立于主对话，不占主对话预算。"""
    return [
        {"role": "system", "content": SUMMARY_SYSTEM},
        {"role": "user", "content":
            f"请压缩为不超过 {budget_tokens} token 的要点摘要：\n\n{transcript}"},
    ]


def build_chat_messages(
    cfg: AppConfigs,
    history: List[dict],
    user_text: str,
    file_names: List[str],
    selected: dict,
    history_limit: int = HISTORY_TAIL,
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
    # 条数只是保险丝；真正的长度上限由 ContextCompressor 按 token 预算决定（§6.10）
    tail = [h for h in history if is_injected(h)][-history_limit:]
    for h in tail:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_text})
    return messages


def is_injected(h: dict) -> bool:
    """哪些历史消息回灌 prompt。

    steps / 候选面板 / 附件不回灌（非结论内容）；analysis_result 只回灌 content 文本
    ——那行文本是数值+判定摘要（digest_line），否则模型跨轮追问数值时只能"让你看图"。
    """
    if h.get("role") not in ("user", "assistant"):
        return False
    return h.get("kind") in (None, "text", "analysis_result")


def _limit_text(ok_range) -> str:
    """ok_range → 简短限值串：[None,40] → ≤40；[1.6,None] → ≥1.6；[a,b] → a~b。"""
    if not ok_range:
        return ""
    lo, hi = (list(ok_range) + [None, None])[:2]
    if lo is not None and hi is not None:
        return f"{lo}~{hi}"
    if hi is not None:
        return f"≤{hi}"
    if lo is not None:
        return f"≥{lo}"
    return ""


_STATUS_CN = {"ok": "达标", "abnormal": "不合格", "missing": "不可算", "info": "仅记录"}


def _round3(v):
    """给 LLM 的数值统一保留 3 位：全精度既占字数也不像工程口径。"""
    return round(float(v), 3) if isinstance(v, (int, float)) else v


def digest_from_analysis(d: dict) -> dict:
    """把 res.to_dict() 压成给 LLM 的数值摘要（design.md §6.9）。

    只留下结论需要的：指标名/值/单位/判定状态/限值/命中档位 + 规则结论原文。
    字段名自解释、空值省略；单工况约 0.3~0.9k 字，远低于 TOOL_PAYLOAD_CLIP_CHARS。
    """
    samples = []
    for s in (d or {}).get("samples", []):
        w = s.get("window") or {}
        t0, t1 = w.get("t_start"), w.get("t_end")
        metrics = []
        for m in s.get("metrics", []):
            mi = {
                "metric": m.get("name"),
                "value": _round3(m["value"]) if m.get("value") is not None else None,
                "unit": m.get("unit"),
                "status": m.get("status"),
            }
            lim = _limit_text(m.get("ok_range"))
            if lim:
                mi["limit"] = lim
            if m.get("profile"):
                mi["profile"] = m["profile"]
            metrics.append(mi)
        item = {"run": s.get("run_index"), "file": s.get("file_name")}
        if t0 is not None and t1 is not None:
            item["window_s"] = [round(float(t0), 3), round(float(t1), 3)]
        item["metrics"] = metrics
        verdicts = [
            {"rule": v.get("rule_id"), "metric": v.get("metric_name"),
             "conclusion": v.get("message"), "direction": v.get("direction"),
             "severity": v.get("severity")}
            for v in s.get("verdicts", [])
        ]
        if verdicts:
            item["verdicts"] = verdicts
        samples.append(item)
    out = {"condition_name": d.get("condition_name"), "samples": samples}
    if d.get("profile"):
        out["profile"] = d["profile"]
    if d.get("degraded"):
        out["note"] = "LLM 标定建议不可用，仅规则结论"
    return out


def digest_payload(digests: List[dict],
                   limit: int = TOOL_PAYLOAD_CLIP_CHARS) -> List[dict]:
    """把多份数值摘要收进工具应答，保证序列化后不超过 limit。

    工具 payload 会被硬截到 limit，截断后 JSON 就废了（§6.10），所以这里主动降级：
    先摘掉最长的规则结论原文，再摘掉 verdicts，最后只保留最近若干份分析——
    更早的数值仍在结果卡片正文里，跨轮追问时由 history 回灌。
    """
    def size(items):
        return len(json.dumps(items, ensure_ascii=False))

    kept = [dict(d) for d in digests]
    if size(kept) <= limit:
        return kept
    for d in kept:                      # 1) 去掉结论原文（最长且重复于 verdict 摘要）
        for s in d.get("samples", []):
            for v in s.get("verdicts", []):
                v.pop("conclusion", None)
    if size(kept) <= limit:
        return kept
    for d in kept:                      # 2) 去掉 verdicts，只留数值+判定
        for s in d.get("samples", []):
            s.pop("verdicts", None)
    if size(kept) <= limit:
        return kept
    out: List[dict] = []                # 3) 从最新往前保留能装下的份数
    for d in reversed(kept):
        if not out and size([d]) > limit:
            break
        if out and size(out + [d]) > limit:
            break
        out.insert(0, d)
    return out


def digest_line(d: dict) -> str:
    """数值摘要 → 一行文本，拼进结果卡片 content，供后续轮次回灌（不含工况名，卡片头已有）。"""
    runs = []
    for s in digest_from_analysis(d)["samples"]:
        seg = "，".join(
            f"{m['metric']}="
            + (f"{m['value']}{m['unit'] or ''}" if m.get("value") is not None else "无值")
            + f" {_STATUS_CN.get(m['status'], m['status'])}"
            + (f"（限值 {m['limit']}" + (f"，档位 {m['profile']}" if m.get("profile") else "") + "）"
               if m.get("limit") else "")
            for m in s["metrics"]
        )
        runs.append(f"run{s.get('run')}·{s.get('file')}：{seg}")
    return "；".join(runs)



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
