"""工具注册表（design.md §6.9）：一个工具的全部 harness 挂点收敛为一个条目。

此前加一个工具要同步五处：prompts.build_tool_specs 的规格、engine exec_tools
的执行器、_tool_step_label 的步骤标签、_step_hits 的命中明细、引擎循环里的
返回值分发——漏一处就是静默行为缺失（generate_report 接入时五处全走了一遍）。
收敛后加工具 = 本文件 TOOLS 里加一个 ToolSpec：

- build_tool_specs(cfg, scope)  从 TOOLS 生成规格（prompt 层消费）；
- engine 执行循环               按 result_kind 分发返回值，executor 执行；
- 步骤标签 / 命中明细            spec.step_label / spec.hits。

执行器签名统一为 (engine, chat, args, ctx)；ctx = {"text", "stop_event",
"read_budget"}（本轮作用域状态由引擎在循环前装配，注册表不持有会话状态）。
MCP 外部工具不在注册表：运行期动态发现、无本地规格。

工具分两类，步骤标签风格不同：路由类（resolve_condition / list_conditions /
recommend_targets）返回 hop，标签就是 hop；数据类（search_manual / get_param /
list_files / records / report）**没有 hop 概念**，硬套 hop=None 只会让人以为
检索失败了（实测困扰）——按返回条数/结果给标签。
"""

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..configs import AppConfigs
from ..records.query import records_compare, records_query
from .tools import list_conditions, recommend_targets, resolve_condition

# 对话「过程」里每条命中的展示正文上限（超出只给前段 + 一句去向说明）
STEP_HIT_TEXT_MAX = 600

_ROUTING_TOOLS = ("resolve_condition", "list_conditions", "recommend_targets")


# ---------------------------------------------------------------- 规格

@dataclass
class SpecCtx:
    """build_tool_specs 的共享件：enum 取值域与作用域收窄后的 schema 片段。"""
    cfg: AppConfigs
    scope: Optional[str]
    vocab: Dict[str, List[str]]
    fn_keys: List[str]
    cond_schema: dict
    param_props: dict
    profile_desc: str


def _spec_resolve_condition(c: SpecCtx) -> dict:
    return {
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
                        "type": "string", "enum": c.fn_keys,
                        "description": "已判定的功能；不确定时可省略",
                    },
                    "maneuver": {
                        "type": "string", "enum": c.vocab["maneuver"],
                        "description": "测试动作",
                    },
                    "surface_code": {
                        "type": "string", "enum": c.vocab["surface_code"],
                        "description": "路面/附着条件（匀质路面）；用户说的若是"
                                       "对开、对接这类组合路面，不要挑一个凑唯一命中",
                    },
                    "params": {
                        "type": "object",
                        "properties": c.param_props,
                        "description": "工况参数，键取自参数名（如 v0_kph），值按描述填",
                    },
                },
            },
        },
    }


def _spec_list_conditions(c: SpecCtx) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "list_conditions",
            "description": (
                "取某功能下的全部工况（含唯一键属性），仅本轮可见，用完即弃。"
                "在描述无法填满唯一键、或用户询问「这个功能下测了哪些」时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"function_key": {"type": "string", "enum": c.fn_keys}},
                "required": ["function_key"],
            },
        },
    }


def _spec_run_analysis(c: SpecCtx) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "run_analysis_on_files",
            "description": (
                "对会话内已上传数据文件执行指定工况分析。仅在目标工况已确定后调用。"
                "默认只分析「本轮默认对象」那批（即最近一次上传的文件）；"
                "需要对比历史文件或用户点名了某几份时，用 file_indexes 按编号指定。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "condition_id": c.cond_schema,
                    "condition_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "批量分析：工况 id 列表。长度必须等于所选文件数"
                            "（逐文件对齐工况——多文件不同工况时用）或为 1"
                            "（全部文件同一工况）；每个 id 须已通过 "
                            "resolve_condition 确认或来自用户明确指定。"
                        ),
                    },
                    "profile": {
                        "type": "string",
                        "description": c.profile_desc,
                    },
                    "file_indexes": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": (
                            "要分析的文件编号（取自本轮消息「会话数据文件」清单的编号，1 起）。"
                            "省略即分析最近一次上传的那批；只在用户明确要求对比多份时填写。"
                        ),
                    },
                    "force": {
                        "type": "boolean",
                        "description": "强制重算（默认同文件同工况同档位命中缓存时跳过重算）",
                    },
                },
                "required": ["condition_id"],
            },
        },
    }


def _spec_recommend_targets(c: SpecCtx) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "recommend_targets",
            "description": "规则打分兜底：无法结构化解析时，用原话取功能/工况候选。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "用户意图原话或要点"},
                    "max_items": {"type": "integer", "description": "最多返回候选数，1~15"},
                },
                "required": ["query"],
            },
        },
    }


def _spec_search_manual(c: SpecCtx) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "search_manual",
            "description": (
                "检索标定手册/知识库（语义+关键词混合）。用户问判定标准、超限成因、"
                "标定方向、工况流程等知识性问题时调用；查具体参数值优先用 get_param。"
                "回答时引用结果里的节 id。没有命中也如实说明，不要编手册内容。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": (
                        "检索要点，**不要照抄用户整句**。提炼主题词/参数名/工况词，"
                        "用空格分隔。例：用户问「干沥青工况下后轴最小保压滑移率是多少」"
                        "→ query 传「干沥青 后轴 最小保压滑移率」；疑问词（是什么/多少/"
                        "有哪些/怎么）会稀释关键词打分、降低召回精度。"
                        "对话中已确认的工况、功能名等上下文要一并带上作主题词。")},
                    "condition": {"type": "string",
                                  "description": "工况词（如 洒水玄武岩）。查询明确针对"
                                                 "某个工况时填写，避免同名内容跨工况串扰"},
                    "top_k": {"type": "integer", "description": "返回条数，默认 5"},
                },
                "required": ["query"],
            },
        },
    }


def _spec_get_param(c: SpecCtx) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "get_param",
            "description": (
                "按参数名精确查询手册表格中的参数定义/初始值/当前值/单位。"
                "「某参数是多少/当前值是什么」类问题首选本工具，值必须原样引用。"
                "参数名必须与手册一致（如 Cp_FA、Opt_Instabil_Lambda_HA）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "参数名，与手册一致"},
                    "condition": {"type": "string",
                                  "description": "工况词；同名参数存在于多个工况时必填，"
                                                 "否则会返回各工况的全部取值"},
                },
                "required": ["name"],
            },
        },
    }


def _spec_list_files(c: SpecCtx) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "查看会话内已上传数据文件清单（编号/名称/大小/是否本轮默认范围）。"
                "带 file_index 时加载该文件并返回信号通道名——用户问「采了哪些通道/"
                "有没有 xx 信号」时用。不带参数不加载信号，纯清单很快。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_index": {
                        "type": "integer",
                        "description": "要查看通道的文件编号（1 起，取自文件清单）。"
                                       "省略时只返回清单不加载信号",
                    },
                },
            },
        },
    }


def _spec_query_records(c: SpecCtx) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "query_records",
            "description": (
                "查询试验记录库（服务器上的历史/竞品数据）：按编号、关键词、工况、车型"
                "过滤，返回每条记录的元数据与已算好的指标汇总。编号/车型/关键词都是"
                "「包含即可」，用户只记得片段（如「4.1」「carB」）时也能查到。"
                "keyword 除了编号/车型/文件名，还匹配工况的中文语义（路面、动作、速度，"
                "如「干沥青」「60kph」）——用户描述的是工况而不是文件名时就用它。"
                "只读——未分析的记录不会在这里触发计算，只标 analysis_status。用户问"
                "「测过哪些/某编号是什么/有没有某车型的数据」时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {
                        "type": "string",
                        "description": "试验编号，可只给片段（如 4.1）；用户说编号时优先用这个",
                    },
                    "keyword": {
                        "type": "string",
                        "description": "模糊关键词，跨 编号/车型/文件名/工况语义 匹配；"
                                       "用户只记得文件名、说不清归哪一类，或只描述路面/"
                                       "动作/速度（如「干沥青」「60kph」）时用它",
                    },
                    "condition_id": c.cond_schema,
                    "vehicle": {
                        "type": "string",
                        "description": "车型代号，可只给片段（如 carB）",
                    },
                    "function_key": {"type": "string", "enum": c.fn_keys},
                    "recorded_from": {
                        "type": "string",
                        "description": "试验日期下限（ISO，如 2026-03-01）；看趋势时用",
                    },
                    "recorded_to": {
                        "type": "string",
                        "description": "试验日期上限（ISO，如 2026-04-30）",
                    },
                    "sort": {
                        "type": "string",
                        "enum": ["default", "time"],
                        "description": "time=同车同工况按试验日期排序（看某车的历次趋势时用）",
                    },
                    "limit": {"type": "integer", "description": "最多返回条数，1~50"},
                },
            },
        },
    }


def _spec_read_record(c: SpecCtx) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "read_record",
            "description": (
                "读取一条试验记录的原始数据并展示曲线（整段过程 + 事件窗口高亮）："
                "未分析的记录会先惰性分析。返回窗口、指标实测值与规则结论。"
                "用户要看某条记录的数据/曲线时用；record_id 必须先用 query_records "
                "查到。一次只读一条，看多条时分别调用（同一轮最多 3 条）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "record_id": {
                        "type": "string",
                        "description": "记录 id（rec_ 开头），来自 query_records 的结果",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "强制重算（用户要求重新解析时置 true）；默认用缓存",
                    },
                },
                "required": ["record_id"],
            },
        },
    }


def _spec_compare_records(c: SpecCtx) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "compare_records",
            "description": (
                "同一工况下横向对比不同车型/不同批次的客观指标（制动距离、加速时间、"
                "横摆等）。只比已分析的记录；未分析的会列在 not_analyzed 里，"
                "让用户点开后再比。用户问「A 车和 B 车哪个制动距离短/差多少」时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "condition_id": c.cond_schema,
                    "function_key": {"type": "string", "enum": c.fn_keys},
                    "vehicles": {
                        "type": "array", "items": {"type": "string"},
                        "description": "限定对比的车型代号；省略即全部车型",
                    },
                    "metric": {
                        "type": "string", "enum": list(c.cfg.metrics),
                        "description": "只比这一个指标；省略则返回全部指标",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "每个工况最多取几条记录，默认 30",
                    },
                },
            },
        },
    }


def _spec_generate_report(c: SpecCtx) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "generate_report",
            "description": (
                "把本会话已完成的分析结果汇总生成一份 Word（docx）分析报告："
                "总览 + 每份分析的指标表（指标/限值/结果/判定）、规则结论、"
                "标定建议与事件窗口时序图。用户要报告/导出 Word/汇总成文档时调用。"
                "报告数值全部取自已落库的分析结果，不要先编内容再生成；"
                "会话里还没有任何分析结果时，先分析再出报告。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "condition_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "可选：只把指定工况的分析写进报告（工况 id）。"
                            "省略即收录会话内全部分析结果"
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": "报告标题，可选；默认「制动数据分析报告」",
                    },
                    "charts": {
                        "type": "string",
                        "enum": ["all", "abnormal_only", "none"],
                        "description": (
                            "出图策略：all=每个事件样本都出时序图（默认）；"
                            "abnormal_only=只给含异常/缺失指标的样本出图（样本多时"
                            "报告更聚焦）；none=只出表格不出图"
                        ),
                    },
                },
            },
        },
    }


# ---------------------------------------------------------------- 执行器
# 签名统一 (engine, chat, args, ctx)；ctx 见模块 docstring。

def _ex_resolve_condition(eng, chat, args, ctx) -> dict:
    return resolve_condition(
        eng.cfg,
        function_key=args.get("function_key") or chat.selected_function or None,
        maneuver=args.get("maneuver"),
        surface_code=args.get("surface_code"),
        params=args.get("params") or {},
        query=ctx["text"],
        index=eng.cond_index,
        # 槽位证据源：用户原话 + 文件名（文件名是合法的工况线索）
        evidence=ctx["text"] + " " + " ".join(f.name for f in chat.files),
    )


def _ex_list_conditions(eng, chat, args, ctx) -> dict:
    return list_conditions(
        eng.cfg,
        args.get("function_key") or chat.selected_function or "",
    )


def _ex_recommend_targets(eng, chat, args, ctx) -> dict:
    return recommend_targets(
        eng.cfg, args.get("query", ctx["text"]), args.get("max_items", 15),
        function_key=chat.selected_function,
    )


def _ex_search_manual(eng, chat, args, ctx) -> dict:
    return eng._kb_search_tool(args.get("query", ""), condition=args.get("condition"))


def _ex_get_param(eng, chat, args, ctx) -> dict:
    return eng._get_param_tool(args.get("name", ""))


def _ex_list_files(eng, chat, args, ctx) -> dict:
    return eng._list_files_tool(chat, args.get("file_index"))


def _ex_run_analysis(eng, chat, args, ctx) -> Any:
    ids = args.get("condition_ids") or [args["condition_id"]]
    if not isinstance(ids, list):
        ids = [ids]
    return eng._analyze(
        chat, ids, args.get("profile"), ctx["stop_event"],
        file_indexes=args.get("file_indexes"),
        force=bool(args.get("force")),
    )


def _ex_query_records(eng, chat, args, ctx) -> dict:
    return eng._record_tool(records_query, args, chat.selected_function)


def _ex_read_record(eng, chat, args, ctx) -> Any:
    return eng._read_record_tool(chat, args, ctx["read_budget"])


def _ex_compare_records(eng, chat, args, ctx) -> dict:
    return eng._record_tool(records_compare, args, chat.selected_function)


def _ex_generate_report(eng, chat, args, ctx) -> Any:
    return eng._report_tool(
        chat,
        condition_ids=args.get("condition_ids"),
        title=args.get("title") or "",
        charts=args.get("charts") or "all",
    )


# ---------------------------------------------------------------- 步骤标签

def _lb_routing(res: dict) -> Tuple[str, str, str]:
    hop = res.get("hop")
    status = "ok" if res.get("ok", True) else "warn"
    if hop == "error":
        status = "warn"
    if hop == "catalog":
        return (f"→ hop=catalog", f"清单 {res.get('count')} 条（仅本轮可见）", status)
    n_cand = len(res.get("candidates", []) or [])
    label = f"→ hop={hop}" if hop else "完成"
    if hop == "resolved":
        # target 的形状是 {key, name, via, ...}（tools.resolve_condition），
        # detail 给工况名比给「候选：1 项」有用得多
        tgt = res.get("target") or {}
        return (label, str(tgt.get("name") or tgt.get("key") or ""), status)
    return (label, f"候选：{n_cand} 项", status)


def _lb_search_manual(res: dict) -> Tuple[str, str, str]:
    if res.get("ok") is False:      # 参数错（query 为空）：把 note 摊开
        return ("参数有误", str(res.get("note", ""))[:120], "warn")
    n = len(res.get("results", []) or [])
    if not n:
        # 空命中不是工具失败，但对 trace 是信号（模型接下来可能开始编）
        return ("→ 命中 0 节", "手册无相关内容", "warn")
    mode = res.get("mode") or "keyword"
    chars = sum(len(r.get("snippet", "")) for r in (res.get("results") or []))
    return (f"→ 命中 {n} 节", f"通道 {mode} · 送入 {chars} 字", "ok")


def _lb_get_param(res: dict) -> Tuple[str, str, str]:
    rows = res.get("results", []) or []
    first = (rows or [{}])[0].get("param", "")
    secs = len({r.get("section", "") for r in rows})
    # 多工况同参数是常态（不再按工况过滤）：detail 直接点出有几处，
    # 免得「命中 3 条」被当成三个不同参数
    detail = f"{first} · {secs} 个工况" if secs > 1 else str(first)
    return (f"→ 命中 {len(rows)} 条", detail,
            "ok" if res.get("ok", True) else "warn")


def _lb_list_files(res: dict) -> Tuple[str, str, str]:
    rows = res.get("files", []) or []
    ch = next((r for r in rows if r.get("channels")), None)
    detail = f"{ch['name']}：{len(ch['channels'])} 个通道" if ch \
        else f"{len(rows)} 个文件"
    return (f"→ {detail}", "", "ok" if res.get("ok", True) else "warn")


def _lb_query_records(res: dict) -> Tuple[str, str, str]:
    return (f"→ {res.get('count', 0)} 条记录",
            f"库内状态 {res.get('db_stats', {})}",
            "ok" if res.get("ok", True) else "warn")


def _lb_compare_records(res: dict) -> Tuple[str, str, str]:
    groups = res.get("groups", []) or []
    return (f"→ {res.get('record_count', 0)} 条记录", f"{len(groups)} 个工况",
            "ok" if res.get("ok", True) else "warn")


def _lb_generate_report(res: dict) -> Tuple[str, str, str]:
    if res.get("ok") is False:      # 未配置/无结果/生成失败：把 note 摊开
        return ("失败", str(res.get("note", ""))[:160], "warn")
    warn = len(res.get("warnings") or [])
    return ("→ 报告已生成",
            f"{res.get('analyses')} 份分析 · 图 {res.get('charts')} 张"
            + (f" · 告警 {warn}" if warn else ""),
            "ok")


# ---------------------------------------------------------------- 命中明细

def _hits_search_manual(res: dict) -> List[dict]:
    out = []
    for r in res.get("results", []) or []:
        snip = r.get("snippet", "")
        note = r.get("note", "")
        shown = snip[:STEP_HIT_TEXT_MAX]
        if len(snip) > len(shown):
            shown += "…"
            note = (note + "；" if note else "") + "界面只展示前段全文，完整内容看 /debug"
        line = {"title": r.get("title", ""), "id": r.get("id", ""),
                "chars": len(snip), "text": shown}
        if note:
            line["note"] = note
        out.append(line)
    return out


def _hits_get_param(res: dict) -> List[dict]:
    out = []
    for r in res.get("results", []) or []:
        fields = r.get("fields") or {}
        line = {"title": r.get("param", ""),
                "id": r.get("section", ""),
                "text": " · ".join(f"{k}={v}" for k, v in fields.items())}
        if r.get("match") == "partial":
            line["note"] = "子串近似匹配，引用前确认是同一个参数"
        out.append(line)
    return out


def _hits_records(res: dict) -> List[dict]:
    rows = res.get("records") or [
        r for g in res.get("groups", []) or [] for r in g.get("records", [])
    ]
    out = []
    for r in rows[:10]:
        values = []
        for key, m in (r.get("metrics") or {}).items():
            if m.get("value") is None:
                values.append(f"{key}=missing")
            else:
                values.append(f"{key}={m['value']}{m.get('unit', '')}")
        line = {
            "title": f"{r.get('vehicle', '')} {r.get('file_id', '')}".strip(),
            "id": r.get("record_id", ""),
            "text": " · ".join(values)[:STEP_HIT_TEXT_MAX],
        }
        out.append(line)
    return out


# ---------------------------------------------------------------- 注册表

@dataclass
class ToolSpec:
    name: str
    spec: Callable[[SpecCtx], dict]                 # 规格（cfg/作用域相关）
    executor: Callable[..., Any]                    # (engine, chat, args, ctx)
    # 引擎循环如何消费返回值：
    #   payload      dict 直接回灌 LLM（hop 面板由引擎按 hop 字段弹出）
    #   analysis     (msgs, digests)：msgs 作为 message 事件，digests 回灌
    #   record_read  (msgs, payload)：msgs 事件 + 定制步骤行
    #   report       (payload, msg)：下载卡片事件 + 定制步骤行
    result_kind: str = "payload"
    # 执行前跑档位守卫（声明了 profiles 的功能没选档就不分析，§6.9）
    guard_profile: bool = False
    step_label: Optional[Callable[[dict], Tuple[str, str, str]]] = None
    hits: Optional[Callable[[dict], List[dict]]] = None


TOOLS: Dict[str, ToolSpec] = {}
for _name, _spec_fn, _executor, _kind, _guard, _label, _hits in [
    # (名称, 规格构建器, 执行器, 结果分发类型, 档位守卫, 步骤标签, 命中明细)
    ("resolve_condition", _spec_resolve_condition, _ex_resolve_condition,
     "payload", False, _lb_routing, None),
    ("list_conditions", _spec_list_conditions, _ex_list_conditions,
     "payload", False, _lb_routing, None),
    ("run_analysis_on_files", _spec_run_analysis, _ex_run_analysis,
     "analysis", True, None, None),
    ("recommend_targets", _spec_recommend_targets, _ex_recommend_targets,
     "payload", False, _lb_routing, None),
    ("search_manual", _spec_search_manual, _ex_search_manual,
     "payload", False, _lb_search_manual, _hits_search_manual),
    ("get_param", _spec_get_param, _ex_get_param,
     "payload", False, _lb_get_param, _hits_get_param),
    ("list_files", _spec_list_files, _ex_list_files,
     "payload", False, _lb_list_files, None),
    ("query_records", _spec_query_records, _ex_query_records,
     "payload", False, _lb_query_records, _hits_records),
    ("read_record", _spec_read_record, _ex_read_record,
     "record_read", False, None, None),
    ("compare_records", _spec_compare_records, _ex_compare_records,
     "payload", False, _lb_compare_records, _hits_records),
    ("generate_report", _spec_generate_report, _ex_generate_report,
     "report", False, _lb_generate_report, None),
]:
    TOOLS[_name] = ToolSpec(name=_name, spec=_spec_fn, executor=_executor,
                            result_kind=_kind, guard_profile=_guard,
                            step_label=_label, hits=_hits)


def build_tool_specs(cfg: AppConfigs, scope_function: Optional[str] = None) -> List[dict]:
    """工具规格（prompt 层消费）：维度取值域与工况 id 用 cfg 生成的 enum 约束（§13）。

    - 常驻成本由「维度数」决定，不由「工况数」决定：maneuver/surface_code 的取值域
      通常十几个以内，工况清单不进 system prompt。
    - scope_function 已知时，condition_id 直接限定为该功能下的工况 id，非法 id
      在结构上不可能出现；未知时退化为自由字符串。
    """
    # number 同时覆盖整数与小数（整数是 number 的子集），避免个别兼容端点拒绝 type 数组
    param_props = {k: {"type": "number", "description": f"工况参数 {k}"}
                   for k in cfg.vocab()["param_keys"]}
    cond_schema = ({"type": "string", "enum": cfg.scope_conditions(scope_function)}
                   if scope_function and cfg.scope_conditions(scope_function)
                   else {"type": "string",
                         "description": "工况 id，必须是 resolve_condition 命中的那条"})
    # 档位：resolved 只保证工况唯一，不保证档位唯一。这里只描述、不设为 required ——
    # 设为必填会逼模型编一个档位，正确做法是先问用户，未选档时由引擎守卫弹面板（§6.9）
    scope_fn = cfg.function(scope_function) if scope_function else None
    profile_desc = ("档位标签，可选。多维档位用竖线拼接、每维各取一个标签，"
                    "如 4WD|DTCS（不是从全部标签里挑一个）")
    if scope_fn is not None and scope_fn.profiles:
        if scope_fn.profile_dims:
            dims_txt = "，".join(
                f"第{i + 1}维 {'/'.join(d)}" for i, d in enumerate(scope_fn.profile_dims))
            combo = "|".join(d[0] for d in scope_fn.profile_dims)
            profile_desc = (
                f"该功能声明了 {len(scope_fn.profile_dims)} 个档位维度：{dims_txt}。"
                f"取值是「每维各选一个」再用竖线拼接，例如 {combo}；"
                "维度之间不是互斥候选，不要把 4 个标签当成 4 选 1 列给用户。"
                "用户没说选哪个时不要自己定，按维度分别向用户确认；"
                "未带档位调用时引擎不会执行分析，而是给用户弹档位面板")
        else:
            profile_desc = ("该功能声明了档位，取值 " + "|".join(scope_fn.profiles)
                            + "（多维度用竖线拼接如 4WD|DTCS）。用户没说选哪个时不要自己定，"
                              "先向用户确认；未带档位调用时引擎不会执行分析，"
                              "而是给用户弹档位面板")
    ctx = SpecCtx(cfg=cfg, scope=scope_function, vocab=cfg.vocab(),
                  fn_keys=list(cfg.functions), cond_schema=cond_schema,
                  param_props=param_props, profile_desc=profile_desc)
    return [t.spec(ctx) for t in TOOLS.values()]
