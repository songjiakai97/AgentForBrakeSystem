"""Agent 工具（design.md §6.9 / §13）。

对 LLM 暴露六个工具：
- list_conditions / resolve_condition：工况层按需注入（§13 渐进披露），
  解析走 §2.2 唯一键精确匹配；唯一键零命中时降级语义召回（cond_index），
  按"足够像 + 明显领先"双阈值决定自动确认或交用户；
- run_analysis_on_files：执行分析；
- search_manual：手册知识检索；list_files：文件与通道清单；
- recommend_targets：两跳规则打分，保留为离线路径与兜底，不再是主推荐引擎。
离线确定性路由与在线 tool-calling 共用同一实现，保证降级行为一致。
"""

import re
from typing import Any, Dict, List, Optional

from ..configs import AppConfigs
from ..pipeline import NoDataError, analyze_file

# 语义召回决策阈值（归一化余弦相似度）：
# 第一名 ≥ AUTO_CONFIRM_SIM 且领先第二名 ≥ AUTO_CONFIRM_MARGIN 才自动 resolved，
# 否则一律带分数交用户确认——宁可多问一句，不可把相邻工况错认成唯一命中。
# 注意：阈值是按当前 embedding 模型（qwen3-embedding-0.6b，短文本相似度实测
# 分布 0.4~0.6）校准的；换 embedding 模型必须用真实查询重新校准这两个数。
AUTO_CONFIRM_SIM = 0.45
AUTO_CONFIRM_MARGIN = 0.05


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
    "dry_asphalt": ["干沥青", "干地", "沥青", "high mu", "高附着", "dry"],
    "wet_asphalt": ["湿沥青", "洒水沥青", "沥青湿", "wet asphalt"],
    "wet_basalt": ["玄武岩", "洒水", "湿", "low mu", "低附着", "wet"],
    "wet_tile": ["瓷砖", "洒瓷砖", "湿瓷砖", "wet"],
    "split": ["对开", "对开路面", "对开沥青", "分离路面", "split", "μ-split"],
    # 对接（rolling-μ）直接按方向编码：ABS/TCS 两侧含义一致，也方便和试验记录对账。
    "low_to_high": ["低到高", "低附到高附", "low to high", "对接", "跳变", "jump"],
    "high_to_low": ["高到低", "高附到低附", "high to low", "对接", "跳变", "jump"],
}
_SPEED_RE = re.compile(r"(\d{2,3})\s*(?:kph|kmh|km/h|公里)", re.I)


def _slot_justified(value: str, evidence_norm: str, keywords: List[str]) -> bool:
    """槽位取值是否在证据（用户原话+文件名）里有依据。

    模型会编造能唯一命中的槽位（实测：只说"2WD，TCS"却填了 surface_code=
    dry_asphalt，凑成唯一命中绕过面板直接按干沥青跑了分析）。防线：取值本身
    或其关键词表必须在证据里出现，找不到依据视为臆测、按未提供处理。
    """
    cands = [value, value.replace("_", ""), value.replace("_", " ")] + list(keywords)
    return any(_norm(k) and _norm(k) in evidence_norm for k in cands)


def _param_justified(value: Any, evidence_norm: str) -> bool:
    """数值参数依据：整数段（≥2 位）或字面值出现在证据里。

    0.8 这类个位数 token 到处都是（"0.8" 字面除外），放宽会失去鉴别力。
    """
    s = str(value)
    if s.endswith(".0"):
        s = s[:-2]
    if s in evidence_norm:
        return True
    return any(len(d) >= 2 and d in evidence_norm for d in re.findall(r"\d+", s))


def recommend_targets(
    cfg: AppConfigs,
    query: str,
    max_items: int = 15,
    function_key: Optional[str] = None,
) -> dict:
    """两跳推荐。

    function_key=None → 第 1 跳（功能识别）；否则 → 第 2 跳（工况精排）。
    返回 {hop, function?, candidates:[{key,name,score,profiles?}], hint?}。
    """
    cap = min(max(1, int(max_items or 15)), 15)

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
    blf_cfg=None,
) -> List[dict]:
    """对给定文件跑指定工况。files: [{file_id,name,path}]。

    范围由调用方决定（引擎默认只传最近一批，§8.2），本函数不再假设"会话内全部"。

    blf_cfg 为 signals_blf.yaml 的校验结果；给出时 .blf 文件走 BLFLoader。
    """
    out = []
    for f in files:
        try:
            res = analyze_file(
                f["path"], condition_id, cfg,
                file_id=f.get("file_id", ""), file_name=f.get("name", ""),
                profile=profile, blf_cfg=blf_cfg,
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


def _semantic_fallback(cfg: AppConfigs, index, query: Optional[str], fn) -> Optional[dict]:
    """唯一键零命中时的语义召回；index/query 缺失或 embedding 失败返回 None。"""
    if index is None or not (query or "").strip() or not getattr(index, "available", False):
        return None
    try:
        hits = index.search(query, top_k=5)
    except Exception:
        return None
    if not hits:
        return None
    # 功能已给定时优先限定在该功能内；限定后为空再放开——
    # 用户的措辞可能连功能词都偏离了配置词汇，此时跨功能候选反而是有效信息
    if fn is not None:
        def _of_fn(h):
            c = cfg.condition(h["cond_id"])
            return c is not None and c.function == fn.key
        scoped = [h for h in hits if _of_fn(h)]
        if scoped:
            hits = scoped
    best = hits[0]
    cond = cfg.condition(best["cond_id"])
    if cond is None:
        return None

    def _item(h):
        c = cfg.condition(h["cond_id"])
        if c is None:
            return None
        item = _cond_item(cfg, c)
        item["via"] = "semantic"
        item["similarity"] = h["similarity"]
        return item

    if best["similarity"] >= AUTO_CONFIRM_SIM and (
            len(hits) == 1 or best["similarity"] - hits[1]["similarity"] >= AUTO_CONFIRM_MARGIN):
        item = _item(best)
        return {"hop": "resolved", "function": cond.function,
                "candidates": [item], "target": item,
                "reason": f"唯一键未命中，语义匹配（相似度 {best['similarity']}）"}
    cands = [it for it in (_item(h) for h in hits) if it]
    if not cands:
        return None
    return {"hop": "condition", "function": fn.key if fn else cands[0]["function"],
            "transient": True,
            "hint": "唯一键未命中，以下是语义相近的工况（按相似度排序），请确认",
            "candidates": cands[:15]}


def resolve_condition(
    cfg: AppConfigs,
    function_key: Optional[str] = None,
    maneuver: Optional[str] = None,
    surface_code: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    query: Optional[str] = None,
    index=None,
    evidence: Optional[str] = None,
) -> dict:
    """按 §2.2 唯一键精确匹配工况：命中唯一 → resolved；命中多条 → 候选面板。

    零命中时降级语义召回（index 注入时）：按"够像 + 明显领先"双阈值决定
    自动 resolved（带 via=semantic 标记与相似度）或交用户确认——召回扩大了
    口语命中率，但绝不放松确认纪律。

    这是结构化解析优先，不是名称相似度挑选：模型只负责从描述里抽出维度取值，
    「哪条工况」由配置索引决定，因此不存在挑错相邻工况而无人报错的情况。
    """
    fn = cfg.function(function_key) if function_key else None
    if function_key and fn is None:
        return {"hop": "error", "hint": f"未知功能 {function_key}",
                "candidates": [{"key": k, "name": f.name} for k, f in cfg.functions.items()]}

    # 槽位证据校验：模型填的取值必须在用户原话/文件名里有依据，
    # 否则视为臆测（实测会编出 surface_code 凑唯一命中绕过面板）按未提供处理。
    # maneuver 由功能本身决定（TCS 即全油门），不参与校验。
    dropped: List[str] = []
    ev = _norm(evidence or "")
    if ev:
        if surface_code and not _slot_justified(
                surface_code, ev, _SURFACE_TERMS.get(surface_code, [])):
            dropped.append("路面")
            surface_code = None
        if params:
            kept = {}
            for k, v in params.items():
                if _param_justified(v, ev):
                    kept[k] = v
                else:
                    dropped.append(k)
            params = kept

    hits = cfg.find_condition(
        fn.key if fn else None,
        (maneuver or "").strip() or None,
        (surface_code or "").strip() or None,
        params or {},
    )
    drop_note = (f"（{'、'.join(dropped)} 在你的描述和文件名中都找不到依据，已忽略——请明确说明）"
                 if dropped else "")
    if len(hits) == 1:
        item = _cond_item(cfg, hits[0])
        item["via"] = "exact_key"
        return {"hop": "resolved", "function": hits[0].function,
                "candidates": [item], "target": item,
                "reason": "唯一键精确命中（maneuver+路面+速度 均匹配）" + drop_note}

    # 语义召回（唯一键没有给出唯一解时才有意义）：只有"显著领先"才自动确认
    sem = _semantic_fallback(cfg, index, query, fn)
    if sem and sem["hop"] == "resolved":
        return sem

    if len(hits) > 1:
        # 键不全（例如只说了路面没给速度）：候选交用户确认，绝不自动开跑
        if fn is None and len({c.function for c in hits}) > 1:
            # 两跳设计（§13）：跨功能的歧义先回到功能层，不把别的功能的工况混进面板。
            # 例外：语义候选全部达到"足够像"下限——带分数的工况面板比无序功能清单
            # 信息量大，此时允许跨功能直接给工况
            if sem and sem["candidates"] and \
                    sem["candidates"][0].get("similarity", 0) >= AUTO_CONFIRM_SIM:
                return sem
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
            "hint": "描述未填满 maneuver+路面+速度 唯一键，请从候选里确认" + drop_note,
            "candidates": [_cond_item(cfg, c) for c in hits[:15]],
        }
    if sem:
        return sem
    if fn is not None:
        return {
            "hop": "condition",
            "function": fn.key,
            "transient": True,
            "hint": f"在「{fn.name}」下没有匹配到工况，请从候选选择或补充路面/速度" + drop_note,
            "candidates": [_cond_item(cfg, c) for c in cfg.conditions_of(fn.key)[:8]],
        }
    return {"hop": "function",
            "hint": "未能定位工况，请先确定功能",
            "candidates": [{"key": k, "name": f.name} for k, f in cfg.functions.items()]}
