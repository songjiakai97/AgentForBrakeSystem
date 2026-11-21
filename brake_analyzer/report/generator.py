"""报告生成器（design.md §18）：已落库的分析结果 → docxtpl 填充 → docx。

报告版式（用户定版 2026-11-21，第二轮：全部口径以 config 为准）：
- 编号与工况标题：conditions.yaml 的 file_ids（如 4.1.1）+ name，编号即章节路径，
  顺序按 functions.yaml（功能）与 conditions.yaml（功能内工况）的文件顺序；
- 客观指标：conditions.yaml 里该工况声明的 metrics 清单与顺序（key → metrics.yaml
  的名称/unit，ok_range 按 profile 取 pick 解析结果）；结果缺的指标补一行无值，
  不让报告静默丢 config 里声明的指标；
- 时序图：与网页端同一口径——只画 functions.yaml 该功能的 signals，按
  signals.yaml 的 chart_layout 分组，hspace=0 紧贴；
- 两列表「客观指标 | 结果」，时序图居中；多文件/多样本用前导行区分块，
  单文件单样本自动消失；
- 规则结论与标定建议收进独立汇总章。

数据口径：只写「本会话已经分析过」的结果——扫描 chat.messages 里
kind=analysis_result 的完整 data（AnalysisResult.to_dict()），不依赖
Session 缓存（信号可能已被 LRU 淘汰，出图时按需重载，由调用方注入）。
本模块不做编排（不碰 Store/Engine），纯函数可直测。
"""

import math
import os
import shutil
import time
from typing import Callable, Dict, List, Optional, Tuple

from docxtpl import DocxTemplate, InlineImage
from docx.shared import Mm

from ..configs import AppConfigs, pick
from ..events.segment import EventWindow, is_valid
from ..schemas import json_safe
from .charts import render_sample_chart

DEFAULT_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "report_template.docx")

# 单份分析最多嵌多少张时序图：一个工况几十个事件样本时报告会被图片撑到几十 MB，
# 超出部分只进总览行不进图，告警里说明
MAX_CHARTS_PER_ANALYSIS = 10

_FB_CN = {"accepted": "已采纳", "verified": "验证有效", "ineffective": "验证无效"}

# 报告附录的缺失成因图例。不复用 prompts.MISSING_REASON_LEGEND：那张表是写给
# LLM 的行为指令（「别答成…应建议补做」），印进报告会带上一堆对话腔
REPORT_LEGEND = {
    "no_valid_event": "本文件未切出该工况的有效事件窗口：试验过程不完整或工况选错，"
                      "属于数据质量问题，不是信号缺失",
    "signal_absent": "所需通道没有数据：未映射或该数据格式未发布此信号",
    "not_computable": "窗口与通道都在，但无法算出值（如插值端点越界）",
    "no_matching_entry": "所选档位下没有该指标的判定条目，需先选对档位",
}


def collect_analyses(chat, condition_ids: Optional[List[str]] = None) -> List[dict]:
    """会话内全部分析结果（AnalysisResult.to_dict 形状），按发生顺序去重。

    condition_ids 给出时只保留指定工况；None/空 = 全部。消息 data 是落库时的
    深拷贝（含后续反馈同步过的 llm_suggestion），这里统一过 json_safe 防 NaN。
    """
    wanted = {str(c) for c in (condition_ids or []) if c}
    out: List[dict] = []
    seen = set()
    for m in getattr(chat, "messages", []):
        if getattr(m, "kind", "") != "analysis_result":
            continue
        d = getattr(m, "data", None)
        if not isinstance(d, dict) or not d.get("analysis_id"):
            continue
        if d["analysis_id"] in seen:
            continue
        if wanted and str(d.get("condition_id")) not in wanted:
            continue
        seen.add(d["analysis_id"])
        out.append(json_safe(d))
    return out


def _fmt_num(v) -> str:
    """数值 → 报告口径文本：3 位小数去尾零，非有限值显示为 —。"""
    if v is None:
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if not math.isfinite(f):
        return "—"
    return f"{f:.3f}".rstrip("0").rstrip(".")


def _inline_metric(name: str, ok_range, unit: str) -> str:
    """第 1 列「客观指标」：指标名与限值内联成一句（用户定版样式）。

    制动距离≤40m / MFDD≥6.5m/s²；对称限值 [-5,5] 写 ±5°/s；无限值只留名称。
    """
    if ok_range:
        lo, hi = (list(ok_range) + [None, None])[:2]
        if lo is not None and hi is not None and lo < 0 and hi == -lo:
            return f"{name}±{_fmt_num(hi)}{unit or ''}"
        from ..llm.prompts import _limit_text

        lim = _limit_text(ok_range)
        if lim:
            return f"{name}{lim}{unit or ''}"
    return name


def _result_cell(value, unit: str, status: str, missing_reason: str,
                 missing_note: str = "") -> str:
    """第 2 列「结果」：实测值；判定只在异常/缺失时以短注出现，达标保持素值。"""
    if value is not None:
        txt = f"{_fmt_num(value)} {unit or ''}".strip()
        if status == "abnormal":
            return txt + "（超限）"
        if status == "info":
            return txt + "（仅记录）"
        return txt
    if missing_note:
        return "无值（" + missing_note + "）"
    note = REPORT_LEGEND.get(missing_reason or "", missing_reason or "成因未知")
    return "无值（" + note.split("：")[0] + "）"


def _valid_window(w: Optional[dict]) -> Optional[EventWindow]:
    """dict 窗口 → EventWindow，供 is_valid 用与前端同一套口径判有效性。"""
    if not isinstance(w, dict) or "t_start" not in w:
        return None
    win = EventWindow(t_start=float(w.get("t_start") or 0.0),
                      t_end=float(w.get("t_end") or 0.0),
                      anchor=float(w.get("anchor") or 0.0))
    return win if is_valid(win) else None


def _window_label(win: EventWindow) -> str:
    return f"窗口 [{win.t_start:.2f}, {win.t_end:.2f}] s"


def _metric_rows(d: dict, s: dict, cfg: AppConfigs, cond) -> List[dict]:
    """两列表的行：清单与顺序来自 conditions.yaml，数值来自本样本结果。

    结果里没有的 config 指标补一行无值（结果不完整不该让报告变薄）；
    结果里没有且 pick 也解析不出条目 → 该档位无判定条目。
    """
    profile = d.get("profile")
    res_map: Dict[str, dict] = {}
    for m in s.get("metrics", []):
        k = (m.get("key") or "").rpartition(".")[2]
        if k and k not in res_map:
            res_map[k] = m
    keys = list(cond.enabled_metrics) if cond is not None else list(res_map)
    rows = []
    for key in keys:
        rm = res_map.get(key)
        if rm is not None:
            rows.append({"metric": _inline_metric(rm.get("name") or key,
                                                  rm.get("ok_range"), rm.get("unit") or ""),
                         "result": _result_cell(rm.get("value"), rm.get("unit") or "",
                                                rm.get("status") or "",
                                                rm.get("missing_reason") or "")})
            continue
        tmpl = cfg.metrics.get(key)
        name = tmpl.name if tmpl else key
        unit = tmpl.unit if tmpl else ""
        entry = pick(cond.metric_index, key, profile) if cond is not None else None
        missing_note = "" if entry is not None else "所选档位无判定条目"
        rows.append({
            "metric": _inline_metric(name, (entry or {}).get("ok_range"), unit),
            "result": _result_cell(None, unit, "missing", "",
                                   missing_note="未算出" if entry is not None
                                   else "所选档位无判定条目"),
        })
    return rows


def _order_analyses(analyses: List[dict], cfg: AppConfigs) -> List[dict]:
    """cfg 定顺序：functions.yaml 的功能顺序 × conditions.yaml 的工况文件顺序。

    未收录进 config 的工况排在最后（保持出现顺序）；同工况多文件由排序的
    稳定性保持原始出现顺序。
    """
    fn_pos = {k: i for i, k in enumerate(cfg.functions)}
    cond_pos: Dict[str, Tuple[int, int]] = {}
    for fkey, conds in cfg.conditions_by_function.items():
        for i, cid in enumerate(conds):
            cond_pos[cid] = (fn_pos.get(fkey, 999), i)
    return sorted(analyses,
                  key=lambda d: cond_pos.get(d.get("condition_id") or "", (999, 10 ** 9)))


def _condition_blocks(d: dict, cfg: AppConfigs, cond, tpl: DocxTemplate,
                      signals_of: Optional[Callable], chart_dir: str,
                      warnings: List[str], only: Optional[List[str]],
                      show_lead: bool, charts: str = "all",
                      stop_check: Optional[Callable[[], bool]] = None
                      ) -> Tuple[List[dict], int]:
    """一份分析（单文件）→ 逐样本块：前导行(可选) + 两列表 + 居中时序图(可选)。

    show_lead=False（条件内唯一文件且单样本）时前导行隐去，即用户草图的最小形态。
    stop_check 每个样本开始前自检：用户已按停止时立刻收尾（前序块保留），
    不把几十张图硬画完——工具内部的停止检查点（engine 循环只覆盖到工具之间）。
    """
    blocks: List[dict] = []
    n_charts = 0
    samples = d.get("samples", [])
    cname = (cond.name if cond is not None
             else d.get("condition_name") or d.get("condition_id") or "")
    for s in samples:
        if stop_check is not None and stop_check():
            warnings.append(f"{cname}：用户已停止，剩余样本未出图")
            break
        if n_charts >= MAX_CHARTS_PER_ANALYSIS:
            warnings.append(f"{cname}：事件样本多于 {MAX_CHARTS_PER_ANALYSIS} 个，其余未出图")
            break
        run = s.get("run_index")          # 从当前样本取：残留变量会标错图（实测踩中）
        win = _valid_window(s.get("window"))
        rows = _metric_rows(d, s, cfg, cond)
        image, caption = None, ""
        # 出图策略（用户定版）：abnormal_only 只给含异常/缺失指标的样本出图，
        # none 只出表格；all 维持现状。MAX_CHARTS 兜底上限不变
        want_chart = (charts != "none"
                      and (charts == "all" or any(
                          m.get("status") in ("abnormal", "missing")
                          for m in s.get("metrics", []))))
        if win is not None and want_chart:
            signals = signals_of(d.get("file_id")) if signals_of else None
            if not signals:
                warnings.append(f"{cname} run{run}：信号不可用，未出图")
            else:
                png = os.path.join(
                    chart_dir, f"chart_{d.get('analysis_id')}_{s.get('sample_id', run)}.png")
                bad = any(m.get("status") == "abnormal" for m in s.get("metrics", []))
                miss = any(m.get("status") == "missing" for m in s.get("metrics", []))
                if render_sample_chart(
                        signals, (win.t_start, win.t_end), cfg.chart_layout, png,
                        title=f"{cname} · run{run}",
                        highlight_status="abnormal" if bad else "missing" if miss else "ok",
                        only=only):
                    image = InlineImage(tpl, png, width=Mm(160))
                    caption = f"run{run} · {_window_label(win)}"
                    n_charts += 1
        if show_lead:
            parts = [f"run{run}"]
            if win is not None:
                parts.append(_window_label(win))
            blocks.append({"lead": " · ".join(parts), "rows": rows,
                           "image": image, "caption": caption})
        else:
            blocks.append({"lead": "", "rows": rows,
                           "image": image, "caption": ""})
    return blocks, n_charts


def build_context(
    analyses: List[dict],
    *,
    cfg: AppConfigs,
    title: str,
    chat_title: str,
    generated_at: str,
    tpl: DocxTemplate,
    chart_dir: str,
    signals_of: Optional[Callable[[str], Optional[dict]]],
    warnings: List[str],
    progress: Optional[Callable[[str, str], None]],
    stop_check: Optional[Callable[[], bool]] = None,
    charts: str = "all",
) -> Tuple[dict, int]:
    """分析结果列表 → 模板上下文（契约见 scripts/make_report_template.py docstring）。

    编号/顺序/指标/信号全按 config：编号=conditions.yaml file_ids，顺序=功能×
    工况的 config 文件顺序，指标=该工况声明的 metrics，信号=功能 signals 过滤。
    同工况多文件合并进同一小节（编号唯一），文件进块前导行。
    返回 (context, 出图总数)。
    """
    ordered = _order_analyses(analyses, cfg)

    # 按 condition_id 聚合成小节（保序）；编号/名称以 config 为准
    cond_buckets: List[Tuple[str, str, str, List[dict]]] = []   # (cid, no, cname, items)
    seen: Dict[str, int] = {}
    for d in ordered:
        cid = d.get("condition_id") or ""
        cond = cfg.condition(cid)
        if cid in seen:
            cond_buckets[seen[cid]][3].append(d)
            continue
        if cond is not None:
            no = cond.file_ids[0] if cond.file_ids else f"未编号{len(cond_buckets) + 1}"
            cname = cond.name
        else:
            no = f"未编号{len(cond_buckets) + 1}"
            cname = d.get("condition_name") or cid
        seen[cid] = len(cond_buckets)
        cond_buckets.append((cid, no, cname, [d]))

    condition_sections: List[dict] = []
    flat_overview: List[dict] = []
    verdict_rows: List[dict] = []
    suggestion_rows: List[dict] = []
    chart_total = 0

    for cid, no, cname, items in cond_buckets:
        cond = cfg.condition(cid)
        fn = cfg.function(cond.function) if cond is not None else None
        # 图表口径与网页端一致：只画该功能 signals（functions.yaml），按 chart_layout 分组
        only = list(fn.signals) if fn is not None and fn.signals else None
        total_samples = sum(len(d.get("samples", [])) for d in items)
        show_lead = len(items) > 1 or total_samples > 1
        cond_blocks: List[dict] = []
        for d in items:
            if progress is not None:
                try:
                    progress(f"写报告：{cname}", "含时序图渲染，信号未缓存时先重载")
                except Exception:
                    pass          # 心跳失败不影响报告本身
            blocks, n = _condition_blocks(d, cfg, cond, tpl, signals_of,
                                          chart_dir, warnings, only, show_lead,
                                          charts=charts, stop_check=stop_check)
            cond_blocks.extend(blocks)
            chart_total += n
            if len(items) > 1:
                file_tag = d.get("file_name") or ""
                for b in blocks:
                    b["lead"] = (file_tag + " · " + b["lead"]) if b["lead"] else file_tag
            bad = sum(1 for s in d.get("samples", []) for m in s.get("metrics", [])
                      if m.get("status") in ("abnormal", "missing"))
            flat_overview.append({
                "index": len(flat_overview) + 1,
                "section_no": no,
                "condition_name": cname,
                "profile": d.get("profile"),
                "profile_suffix": f"（{d.get('profile')}）" if d.get("profile") else "",
                "file_name": d.get("file_name") or "",
                "sample_count": len(d.get("samples", [])),
                "bad_count": bad,
            })
            seen_v = set()
            for s in d.get("samples", []):
                for v in s.get("verdicts", []):
                    key = (v.get("rule_id"), v.get("metric_name"),
                           v.get("message"), v.get("severity"))
                    if key in seen_v:
                        continue          # 同一规则逐样本重复命中，报告只留一条
                    seen_v.add(key)
                    verdict_rows.append({
                        "cond": f"{no} {cname}", "rule": v.get("rule_id"),
                        "metric": v.get("metric_name"), "message": v.get("message"),
                        "severity": v.get("severity"),
                    })
            sug = d.get("llm_suggestion")
            if isinstance(sug, dict):
                fb = sug.get("feedback") or {}
                fb_text = _FB_CN.get(fb.get("status"), fb.get("status") or "")
                if fb.get("note"):
                    fb_text += f"：{fb['note']}"
                fu = sug.get("followup")
                if isinstance(fu, dict) and fu.get("summary"):
                    fb_text += (f"；下一步：{fu['summary']}" if fb_text
                                else f"下一步：{fu['summary']}")
                for a in (sug.get("calibration_actions") or []):
                    suggestion_rows.append({
                        "cond": f"{no} {cname}", "param": a.get("param"),
                        "direction": a.get("direction"), "metric": a.get("metric"),
                        "effect": a.get("expected_effect"), "risk": a.get("risk"),
                        "feedback": fb_text,
                    })
        condition_sections.append({"title": f"{no} {cname}", "blocks": cond_blocks})

    files = list(dict.fromkeys(
        f"{r['file_name']}（{r['sample_count']} 个事件样本）"
        for r in flat_overview if r["file_name"]))
    context = {
        "title": title or "制动数据分析报告",
        "generated_at": generated_at,
        "chat_title": chat_title or "-",
        "analyses_total": len(analyses),
        "bad_total": sum(r["bad_count"] for r in flat_overview),
        "files": files,
        "analyses": flat_overview,
        "condition_sections": condition_sections,
        "has_findings": bool(verdict_rows or suggestion_rows),
        "verdict_rows": verdict_rows,
        "suggestion_rows": suggestion_rows,
        "legend_lines": [f"{k}：{v}" for k, v in REPORT_LEGEND.items()],
    }
    return context, chart_total


def generate_report(
    analyses: List[dict],
    out_path: str,
    cfg: AppConfigs,
    template_path: str = DEFAULT_TEMPLATE,
    signals_of: Optional[Callable[[str], Optional[dict]]] = None,
    title: str = "制动数据分析报告",
    chat_title: str = "",
    progress: Optional[Callable[[str, str], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
    charts: str = "all",
) -> dict:
    """分析结果列表 → docx 报告。返回 {ok, path, file, analyses, charts, warnings}。

    analyses 是 collect_analyses 的输出（AnalysisResult.to_dict 形状）。
    cfg 必传：编号（conditions.yaml file_ids）、指标清单（该工况 metrics）、
    图表信号（functions.yaml）与分组（signals.yaml chart_layout）全以它为准。
    signals_of(file_id) → 该文件的 SignalData 字典；None/缺失则该样本跳过出图。
    progress(label, detail) 是慢操作心跳（渲染图表逐张回调），调用方接 _progress。
    stop_check() 非零即停：出图循环逐样本自检，用户停止后立即收尾不硬画完。
    charts 出图策略：all（默认）/ abnormal_only（只画含异常或缺失指标的样本）/
    none（只出表格）。
    """
    if not analyses:
        raise ValueError("没有可写入报告的分析结果（会话里还没有完成过任何分析）")
    if not os.path.isfile(template_path):
        raise FileNotFoundError(
            f"报告模板不存在: {template_path}（先运行 scripts/make_report_template.py 生成）")

    def _say(label: str, detail: str = "") -> None:
        if progress is not None:
            try:
                progress(label, detail)
            except Exception:
                pass          # 心跳失败不影响报告本身

    out_path = os.path.abspath(out_path)
    chart_dir = os.path.join(os.path.dirname(out_path),
                             ".charts_" + os.path.splitext(os.path.basename(out_path))[0])
    os.makedirs(chart_dir, exist_ok=True)
    warnings: List[str] = []
    try:
        tpl = DocxTemplate(template_path)
        _say("填充模板并保存 docx", "含时序图渲染，信号未缓存时先重载")
        context, n_charts = build_context(
            analyses, cfg=cfg, title=title, chat_title=chat_title,
            generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            tpl=tpl, chart_dir=chart_dir, signals_of=signals_of,
            warnings=warnings, progress=progress, stop_check=stop_check,
            charts=charts)
        tpl.render(context)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        tpl.save(out_path)
    finally:
        shutil.rmtree(chart_dir, ignore_errors=True)

    return {
        "ok": True,
        "path": out_path,
        "file": os.path.basename(out_path),
        "analyses": len(analyses),
        "charts": n_charts,
        "warnings": warnings,
    }
