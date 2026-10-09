"""Agent 工具（design.md §6.9 / §13）。

对 LLM 暴露四个工具：
- list_conditions / resolve_condition：工况层按需注入（§13 渐进披露），
  解析走 §2.2 唯一键精确匹配，不让模型在相似工况名之间挑；
- run_analysis_on_files：执行分析；
- recommend_targets：两跳规则打分，保留为离线路径与兜底，不再是主推荐引擎。
离线确定性路由与在线 tool-calling 共用同一实现，保证降级行为一致。
"""

import re
from typing import Any, Dict, List, Optional

from ..configs import AppConfigs
from ..pipeline import NoDataError, analyze_file


def _norm(s: str) -> str:
    return re.sub(r"[\s_\-/()（）:：]+", "", s.lower())


def _score_text(query: str, terms: List[str]) -> float:
    q = _norm(query)
    score = 0.0
    for t in terms:
        tn = _norm(t)
        if not tn:
            continue
        if tn in q:
            score += 2.0
        elif len(tn) >= 2 and any(tn[i:i + 2] in q for i in range(len(tn) - 1)):
            score += 0.4
    return score


# 工况关键词加权
_MANEUVER_TERMS = {
    "full_brake": ["全力制动", "紧急制动", "制动", "刹车", "brake", "减速", "停车"],
    "full_throttle": ["全油门", "加速", "起步", "throttle", "0到", "0-"],
}
_SURFACE_TERMS = {
    "dry_asphalt": ["干沥青", "干地", "沥青", "high mu", "高附着"],
    "wet_basalt": ["玄武岩", "洒水", "湿", "low mu", "低附着"],
    "wet_tile": ["瓷砖", "洒瓷砖"],
}
_SPEED_RE = re.compile(r"(\d{2,3})\s*(?:kph|kmh|km/h|公里)", re.I)


def recommend_targets(
    cfg: AppConfigs,
    query: str,
    max_items: int = 8,
    function_key: Optional[str] = None,
) -> dict:
    """两跳推荐。

    function_key=None → 第 1 跳（功能识别）；否则 → 第 2 跳（工况精排）。
    返回 {hop, function?, candidates:[{key,name,score,profiles?}], hint?}。
    """
    cap = min(max(1, int(max_items or 5)), 8)

    if function_key is None:
        scored = []
        for fn in cfg.functions.values():
            terms = [fn.key, fn.name] + fn.aliases
            s = _score_text(query, terms)
            # 功能下工况名命中加分
            for c in cfg.conditions_of(fn.key):
                if _norm(c.name) and _norm(c.name) in _norm(query):
                    s += 1.5
                else:
                    s += 0.2 * _score_text(query, [c.name])
            if s > 0:
                scored.append((s, fn))
        scored.sort(key=lambda x: -x[0])
        if not scored:
            return {
                "hop": "error",
                "transient": True,
                "hint": "未能识别功能。请描述更接近的目标，或从目录中选择。",
                "candidates": [
                    {"key": fn.key, "name": fn.name, "score": 0.0}
                    for fn in list(cfg.functions.values())[:cap]
                ],
            }
        if len(scored) == 1 or scored[0][0] >= scored[1][0] + 1.0:
            return recommend_targets(cfg, query, cap, function_key=scored[0][1].key)
        return {
            "hop": "function",
            "transient": True,
            "candidates": [
                {"key": fn.key, "name": fn.name, "score": round(s, 2)}
                for s, fn in scored[:cap]
            ],
        }

    fn = cfg.function(function_key)
    conds = cfg.conditions_of(function_key)
    scored_c = []
    q_speeds = [int(m.group(1)) for m in _SPEED_RE.finditer(query)]
    for c in conds:
        s = _score_text(query, [c.name, c.id, c.maneuver, c.surface_code])
        for term in _MANEUVER_TERMS.get(c.maneuver, []):
            if term in query.lower() or term in query:
                s += 1.2
        for term in _SURFACE_TERMS.get(c.surface_code, []):
            if term in query.lower() or term in query:
                s += 1.2
        if q_speeds:
            nums = [int(n) for n in re.findall(r"(\d{2,3})", c.id)]
            if any(q in nums for q in q_speeds):
                s += 1.5
        scored_c.append((s, c))
    scored_c.sort(key=lambda x: (-x[0], x[1].id))
    best = scored_c[0]
    if best[0] <= 0:
        return {
            "hop": "condition",
            "function": function_key,
            "transient": True,
            "hint": f"在「{fn.name}」下未匹配到明确工况，请从列表选择。",
            "candidates": [_cond_item(cfg, c, 0.0) for c in conds[:cap]],
        }
    second = scored_c[1][0] if len(scored_c) > 1 else -1.0
    if len(conds) == 1 or best[0] >= second + 1.0:
        item = _cond_item(cfg, best[1], best[0])
        return {"hop": "resolved", "function": function_key, "candidates": [item], "target": item}
    return {
        "hop": "condition",
        "function": function_key,
        "transient": True,
        "candidates": [_cond_item(cfg, c, s) for s, c in scored_c[:cap]],
    }


def _cond_item(cfg: AppConfigs, cond, score: float = 0.0) -> dict:
    fn = cfg.function(cond.function)
    item = {
        "key": cond.id,
        "name": cond.name,
        "score": round(score, 2),
        "function": cond.function,
        "maneuver": cond.maneuver,
        "surface_code": cond.surface_code,
        "params": dict(cond.params),
    }
    if fn.profiles:
        item["profiles"] = fn.profiles
        item["profile_dims"] = fn.profile_dims
    return item


def run_analysis_on_files(
    cfg: AppConfigs,
    files: List[dict],
    condition_id: str,
    profile: Optional[str] = None,
) -> List[dict]:
    """对会话内全部文件跑指定工况。files: [{file_id,name,path}]。"""
    out = []
    for f in files:
        try:
            res = analyze_file(
                f["path"], condition_id, cfg,
                file_id=f.get("file_id", ""), file_name=f.get("name", ""),
                profile=profile,
            )
            out.append({"ok": True, "result": res})
        except NoDataError as e:
            out.append({"ok": False, "file": f, "error": str(e)})
        except Exception as e:  # 加载兜底
            out.append({"ok": False, "file": f, "error": f"分析失败: {e}"})
    return out


# ---------------------------------------------------------------- 工况层按需注入（§13）

def list_conditions(cfg: AppConfigs, function_key: str) -> dict:
    """按需给出某功能的全部工况（含 §2.2 唯一键属性），只服务当次请求。

    标注 transient：清单原文属于「用完即弃」的内容，下一轮请求前会被换成一行存根，
    不参与摘要（§6.10「context 只保留有效内容」）。
    """
    fn = cfg.function(function_key or "")
    if fn is None:
        return {"hop": "error", "hint": f"未知功能 {function_key}",
                "candidates": [{"key": k, "name": f.name} for k, f in cfg.functions.items()]}
    items = [_cond_item(cfg, c) for c in cfg.conditions_of(fn.key)]
    return {
        "hop": "catalog",
        "function": fn.key,
        "transient": True,
        "count": len(items),
        "conditions": items,
        "note": "清单仅供本轮定位目标；定位请用 resolve_condition",
    }


def resolve_condition(
    cfg: AppConfigs,
    function_key: Optional[str] = None,
    maneuver: Optional[str] = None,
    surface_code: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> dict:
    """按 §2.2 唯一键精确匹配工况：命中唯一 → resolved；命中多条 → 候选面板；零 → 兜底。

    这是结构化解析，不是名称相似度挑选：模型只负责从描述里抽出维度取值，
    「哪条工况」由配置索引决定，因此不存在挑错相邻工况而无人报错的情况。
    """
    fn = cfg.function(function_key) if function_key else None
    if function_key and fn is None:
        return {"hop": "error", "hint": f"未知功能 {function_key}",
                "candidates": [{"key": k, "name": f.name} for k, f in cfg.functions.items()]}

    hits = cfg.find_condition(
        fn.key if fn else None,
        (maneuver or "").strip() or None,
        (surface_code or "").strip() or None,
        params or {},
    )
    if len(hits) == 1:
        item = _cond_item(cfg, hits[0])
        return {"hop": "resolved", "function": hits[0].function,
                "candidates": [item], "target": item}
    if len(hits) > 1:
        # 键不全（例如只说了路面没给速度）：候选交用户确认，绝不自动开跑
        if fn is None and len({c.function for c in hits}) > 1:
            # 两跳设计（§13）：跨功能的歧义先回到功能层，不把别的功能的工况混进面板
            return {
                "hop": "function",
                "transient": True,
                "hint": "描述还不足以判定功能，请先选择功能",
                "candidates": [{"key": k, "name": cfg.function(k).name}
                               for k in dict.fromkeys(c.function for c in hits)],
            }
        return {
            "hop": "condition",
            "function": fn.key if fn else hits[0].function,
            "transient": True,
            "hint": "描述未填满 maneuver+路面+速度 唯一键，请从候选里确认",
            "candidates": [_cond_item(cfg, c) for c in hits[:8]],
        }
    if fn is not None:
        return {
            "hop": "condition",
            "function": fn.key,
            "transient": True,
            "hint": f"在「{fn.name}」下没有匹配到工况，请从候选选择或补充路面/速度",
            "candidates": [_cond_item(cfg, c) for c in cfg.conditions_of(fn.key)[:8]],
        }
    return {"hop": "function",
            "hint": "未能定位工况，请先确定功能",
            "candidates": [{"key": k, "name": f.name} for k, f in cfg.functions.items()]}
