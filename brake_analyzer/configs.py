"""配置加载与加载期校验/建索引（design.md §6.1、§10.3）。

加载失败抛 ConfigError，阻止启动。
"""

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

from .events.segment import (
    SEGMENTERS,
    SEGMENTER_SIGNALS,
    SPEED_SPAN_METRICS,
    SPEED_SPAN_SEGMENTERS,
)
from .records.naming import FileIdIndex, build_index, is_valid_file_id
from .schemas import ConditionConfig, FunctionConfig, MetricTemplate, RuleConfig

SEVERITIES = {"info", "low", "high", "critical"}
FAULT_DOMAINS = {
    "stability",
    "brake_performance",
    "traction",
    "powertrain_interaction",
    "data_quality",
}
RULE_STATUSES = {"abnormal", "missing"}


class ConfigError(RuntimeError):
    pass


def _load_yaml(path: str) -> Any:
    if not os.path.exists(path):
        raise ConfigError(f"配置文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ConfigError(f"配置文件 {path} 解析失败: {e}") from e
    if data is None:
        raise ConfigError(f"配置文件 {path} 为空")
    return data


# 段落搬家后留下的"看着还生效"的配置是最难查的：它不报错，只是不起作用。
# 因此格式侧文件只允许声明自己该声明的段，其余顶层键直接拒绝。
_MOVED_HINTS = {
    "chart_layout": "已移到 configs/signals.yaml（分组按逻辑信号名写，与数据格式无关）",
    "logical_signals": "已移到 configs/signals.yaml（名称/单位/描述的唯一出处）",
}


def _reject_unknown_sections(raw: Any, allowed: set, fname: str) -> None:
    if not isinstance(raw, dict):
        return                      # 类型错误由各自的必需段检查报，信息更具体
    for key in raw:
        if key in allowed:
            continue
        hint = _MOVED_HINTS.get(str(key))
        raise ConfigError(
            f"{fname} 不支持顶层段 '{key}'" + (f"：{hint}" if hint else "（未识别的段）"))


def _reject_signal_extras(spec: dict, fname: str, name: str) -> None:
    """格式侧信号条目只允许 candidates（+ 少量格式专属键）。

    unit/description 属于登记表；允许写在格式文件里就等于允许两处口径，
    而"以谁为准"最终只有人眼能判断。宁可启动期报错，也不静默忽略。
    """
    bad = [k for k in spec if k in ("unit", "description")]
    if bad:
        raise ConfigError(
            f"{fname} 信号 {name} 不该声明 {sorted(bad)}：单位与描述只在 "
            f"configs/signals.yaml 的 logical_signals 里定义一次")


# ---------------------------------------------------------------- 索引构建

def build_metric_index(metrics: List[Dict[str, Any]]) -> Dict[str, dict]:
    """{metric_key: {"default": entry|None, "by_profile": {profile: entry}}}"""
    index: Dict[str, dict] = {}
    for entry in metrics:
        key = entry.get("key")
        if not key:
            raise ConfigError(f"指标条目缺少 key: {entry}")
        slot = index.setdefault(key, {"default": None, "by_profile": {}})
        profile = entry.get("profile")
        if profile is None:
            if slot["default"] is not None:
                raise ConfigError(f"指标 {key} 出现多条默认条目")
            slot["default"] = entry
        else:
            if profile in slot["by_profile"]:
                raise ConfigError(f"指标 {key} 的 profile {profile} 重复")
            slot["by_profile"][profile] = entry
    return index


def collect_enabled(metrics: List[Dict[str, Any]]) -> List[str]:
    """去重且保持首次出现顺序（§10.3）。"""
    seen: List[str] = []
    for entry in metrics:
        if entry["key"] not in seen:
            seen.append(entry["key"])
    return seen


def pick(
    condition_index: Dict[str, dict], metric_key: str, profile: Optional[str]
) -> Optional[dict]:
    """运行期条目选择（§10.4 + demo 两维子串匹配）。

    demo 约定（demo_conditions_metrics.md §3.3-3 / §7-⑦）：配置只用单维标签
    （4WD/2WD/DTCS/TCS），一次分析可传多个档位词，用 "|" 或 "," 拼接
    （如 "4WD|DTCS"）。命中规则：
    1. 档位词的每个分量精确匹配 by_profile，取全部命中的条目（多条目展开）；
    2. 本函数针对单个 metric_key：优先返回分量精确命中的条目，
       回退 profile 子串匹配（兼容反向：条目 "4WD" 命中传入 "4WD_DTCS"），
       再回退通用条目；均无则 None（引擎判 missing）。
    """
    slot = condition_index.get(metric_key)
    if slot is None:
        return None
    if profile is not None:
        tokens = [t for t in _profile_tokens(profile) if t]
        for tok in tokens:
            entry = slot["by_profile"].get(tok)
            if entry is not None:
                return entry
        # 子串兜底：条目标签是所选复合标签的一部分
        for tok, entry in slot["by_profile"].items():
            if tok in profile:
                return entry
    return slot["default"]


def _profile_tokens(profile: str) -> List[str]:
    out: List[str] = []
    for chunk in profile.replace(",", "|").replace("_", "|").split("|"):
        chunk = chunk.strip()
        if chunk:
            out.append(chunk)
    return out


# ---------------------------------------------------------------- 运行时配置对象

@dataclass
class ChartGroup:
    """图表一行子图：同单位信号共用 y 轴（signals.yaml chart_layout）。"""
    label: str
    unit: str
    signals: List[str]


def _slot_key(cond: ConditionConfig) -> tuple:
    """§2.2 唯一键：maneuver + surface_code + params 唯一确定一个工况。"""
    params = tuple(sorted((k, _norm_scalar(v)) for k, v in (cond.params or {}).items()))
    return (cond.maneuver, cond.surface_code, params)


def _norm_scalar(v: Any) -> str:
    """数值归一：100 / 100.0 / "100" 视为同一个值，避免伪唯一。"""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        f = float(v)
        return str(int(f)) if f.is_integer() else repr(f)
    return str(v).strip()


@dataclass
class AppConfigs:
    config_dir: str
    functions: Dict[str, FunctionConfig] = field(default_factory=dict)
    conditions: Dict[str, ConditionConfig] = field(default_factory=dict)
    conditions_by_function: Dict[str, List[str]] = field(default_factory=dict)
    metrics: Dict[str, MetricTemplate] = field(default_factory=dict)
    rules: List[RuleConfig] = field(default_factory=list)
    signal_maps: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    chart_layout: List[ChartGroup] = field(default_factory=list)
    # 参与计算的信号：metrics.yaml 的 inputs ∪ 分段器读取的通道。
    # 每种数据格式都必须覆盖它；纯观测量允许只出现在某一种格式里（§9.6）。
    required_signals: List[str] = field(default_factory=list)
    # 逻辑信号登记表（signals.yaml）：名称 → {description, unit}
    signal_registry: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # 试验编号索引（conditions.yaml 的 file_ids → condition_id），文件名匹配见 §17
    file_ids: Optional[FileIdIndex] = None

    # ---- 查询辅助 ----
    def function(self, key: str) -> Optional[FunctionConfig]:
        return self.functions.get(key)

    def condition(self, cid: str) -> Optional[ConditionConfig]:
        return self.conditions.get(cid)

    def condition_by_file_id(self, file_id: str) -> Optional[ConditionConfig]:
        """编号 → 工况；`4-1-1` 与 `4.1.1` 等价。"""
        if self.file_ids is None:
            return None
        cid = self.file_ids.condition_id(file_id)
        return self.conditions.get(cid) if cid else None

    def match_file_id(self, filename: str) -> Optional[str]:
        """按文件名前缀取编号；不认编号的文件返回 None（导入时跳过）。"""
        return self.file_ids.match(filename) if self.file_ids is not None else None

    def conditions_of(self, function_key: str) -> List[ConditionConfig]:
        return [self.conditions[c] for c in self.conditions_by_function.get(function_key, [])]

    def enabled_metric_keys(self) -> List[str]:
        """全部工况实际启用的 metric_key（首次出现顺序），用于规则校验。"""
        seen: List[str] = []
        for cond in self.conditions.values():
            for k in cond.enabled_metrics:
                if k not in seen:
                    seen.append(k)
        return seen

    def profiles_for_metric(self, cid: str, metric_key: str) -> List[str]:
        cond = self.conditions.get(cid)
        if cond is None:
            return []
        slot = cond.metric_index.get(metric_key) or {}
        return sorted(slot.get("by_profile", {}))

    # ---- 目标解析辅助（§13：功能层常驻、工况层按需）----
    def vocab(self) -> Dict[str, List[str]]:
        """工况维度的取值域：动作、路面、params 键。

        这三项进 LLM 工具参数的 enum（量级是「维度数」而非「工况数」），
        工况条目本身只按功能注入当次请求。
        """
        maneuvers: List[str] = []
        surfaces: List[str] = []
        keys: List[str] = []
        for c in self.conditions.values():
            for bucket, val in ((maneuvers, c.maneuver), (surfaces, c.surface_code)):
                if val and val not in bucket:
                    bucket.append(val)
            for k in c.params:
                if k not in keys:
                    keys.append(k)
        return {"maneuver": maneuvers, "surface_code": surfaces, "param_keys": keys}

    def scope_conditions(self, function_key: str) -> List[str]:
        """本功能下全部可执行目标，限定成 LLM 工具参数的 enum（非法 id 结构上不可能）。"""
        return list(self.conditions_by_function.get(function_key, []))

    def find_condition(
        self,
        function_key: Optional[str],
        maneuver: Optional[str],
        surface_code: Optional[str],
        params: Optional[Dict[str, Any]],
    ) -> List[ConditionConfig]:
        """按 §2.2 唯一键做精确匹配，返回全部命中（0/1/多条由调用方判）。

        约束（§13）：一个功能下同一 maneuver+surface+params 组合只能对应一个工况，
        所以给定完整键时命中唯一；键不全时返回候选，交调用方出面板。
        """
        want = {k: _norm_scalar(v) for k, v in (params or {}).items() if v is not None}
        hits = []
        for cond in self.conditions.values():
            if function_key and cond.function != function_key:
                continue
            if maneuver and cond.maneuver != maneuver:
                continue
            if surface_code and cond.surface_code != surface_code:
                continue
            got = {k: _norm_scalar(v) for k, v in (cond.params or {}).items()}
            if all(got.get(k) == v for k, v in want.items()):
                hits.append(cond)
        return hits


def load_configs(config_dir: str = "configs") -> AppConfigs:
    cfg = AppConfigs(config_dir=config_dir)

    # ---- signals.yaml：格式无关的一层（逻辑信号登记 + 图表分行）----
    # 名称/单位/描述只在这里定义一次；signals_mf4.yaml / signals_blf.yaml 只回答
    # 「该逻辑信号在这个格式里叫什么」。同一个物理量写两遍必然漂移，而漂移只有人眼能发现。
    reg_raw = _load_yaml(os.path.join(config_dir, "signals.yaml"))
    _reject_unknown_sections(reg_raw, {"logical_signals", "chart_layout"}, "signals.yaml")
    registry = reg_raw.get("logical_signals") if isinstance(reg_raw, dict) else None
    if not isinstance(registry, dict) or not registry:
        raise ConfigError("signals.yaml 缺少非空 logical_signals 段")
    for name, spec in registry.items():
        if not isinstance(spec, dict):
            raise ConfigError(f"signals.yaml 逻辑信号 {name} 必须是映射（description/unit）")
    cfg.signal_registry = {k: dict(v) for k, v in registry.items()}
    logical_names = set(registry)

    # ---- signals_mf4.yaml：只声明候选通道名 ----
    sig_raw = _load_yaml(os.path.join(config_dir, "signals_mf4.yaml"))
    _reject_unknown_sections(sig_raw, {"signal_maps"}, "signals_mf4.yaml")
    mf4_maps = sig_raw.get("signal_maps") if isinstance(sig_raw, dict) else None
    if not isinstance(mf4_maps, dict) or not mf4_maps:
        raise ConfigError("signals_mf4.yaml 缺少非空 signal_maps 段")
    unknown = [n for n in mf4_maps if n not in logical_names]
    if unknown:
        raise ConfigError(
            f"signals_mf4.yaml 引用未登记的逻辑信号: {unknown}（请先在 signals.yaml 登记）")
    signal_maps: Dict[str, Dict[str, Any]] = {}
    for name, spec in mf4_maps.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("candidates"), list) \
                or not spec["candidates"]:
            raise ConfigError(f"信号 {name} 缺少非空 candidates 列表")
        _reject_signal_extras(spec, "signals_mf4.yaml", name)
        # 单位/描述只认登记表；格式侧再写一份就是第二处口径，直接拒（见 §9.6）
        merged = dict(registry[name])
        merged.update({k: v for k, v in spec.items() if k != "candidates"})
        merged["candidates"] = spec["candidates"]
        signal_maps[name] = merged
    cfg.signal_maps = signal_maps

    # chart_layout：一行子图 = 一组同单位信号（可选段，缺失时图表按单位自动分组）
    layout_raw = reg_raw.get("chart_layout") if isinstance(reg_raw, dict) else None
    if layout_raw is not None:
        if not isinstance(layout_raw, list) or not layout_raw:
            raise ConfigError("signals.yaml 的 chart_layout 必须是非空列表")
        placed: set = set()
        groups: List[ChartGroup] = []
        for i, g in enumerate(layout_raw):
            if not isinstance(g, dict):
                raise ConfigError(f"chart_layout[{i}] 必须是映射: {g}")
            sigs = [str(s) for s in (g.get("signals") or [])]
            if not sigs:
                raise ConfigError(f"chart_layout[{i}] 缺少非空 signals")
            unknown = [s for s in sigs if s not in logical_names]
            if unknown:
                raise ConfigError(f"chart_layout[{i}] 引用未登记信号: {unknown}")
            dup = [s for s in sigs if s in placed]
            if dup:
                raise ConfigError(f"chart_layout 信号重复分组: {dup}")
            placed.update(sigs)
            groups.append(ChartGroup(label=str(g.get("label") or sigs[0]),
                                     unit=str(g.get("unit") or ""), signals=sigs))
        # 未编排的信号各自成组，保证图表不静默丢通道
        for name in registry:
            if name not in placed:
                groups.append(ChartGroup(label=name, unit=str(registry[name].get("unit") or ""),
                                         signals=[name]))
        cfg.chart_layout = groups

    # ---- functions.yaml ----
    fn_raw = _load_yaml(os.path.join(config_dir, "functions.yaml"))
    fn_list = fn_raw.get("functions") if isinstance(fn_raw, dict) else None
    if not isinstance(fn_list, list) or not fn_list:
        raise ConfigError("functions.yaml 缺少非空 functions 列表")
    for item in fn_list:
        key = item.get("key")
        if not key:
            raise ConfigError(f"功能条目缺少 key: {item}")
        if key in cfg.functions:
            raise ConfigError(f"功能 key 重复: {key}")
        seg = item.get("segmenter", "")
        if seg not in SEGMENTERS:
            raise ConfigError(
                f"功能 {key} 的 segmenter '{seg}' 未注册，可用: {sorted(SEGMENTERS)}"
            )
        dims: List[List[str]] = []
        profiles: List[str] = []
        for p in item.get("profiles") or []:
            parts = [t.strip() for t in str(p).split("|") if t.strip()]
            dims.append(parts)
            profiles.extend(t for t in parts if t not in profiles)
        sig_list = [str(s) for s in (item.get("signals") or [])]
        if len(set(sig_list)) != len(sig_list):
            raise ConfigError(f"功能 {key} 的 signals 有重复项: {sig_list}")
        unknown_sig = [s for s in sig_list if s not in logical_names]
        if unknown_sig:
            raise ConfigError(f"功能 {key} 的 signals 引用未登记信号: {unknown_sig}")
        cfg.functions[key] = FunctionConfig(
            key=key,
            name=item.get("name", key),
            aliases=[str(a) for a in item.get("aliases") or []],
            segmenter=seg,
            kb_section=item.get("kb_section", ""),
            description=item.get("description", ""),
            profiles=profiles,
            profile_dims=dims,
            signals=sig_list,
        )
    fn_keys = set(cfg.functions)

    # 分段器读取的通道同样是计算必需：缺了就切不出事件窗口，指标全判 missing。
    for fn in cfg.functions.values():
        for sig in SEGMENTER_SIGNALS.get(fn.segmenter, []):
            if sig not in logical_names:
                raise ConfigError(
                    f"功能 {fn.key} 的分段器 {fn.segmenter} 读取未登记信号: {sig}")
            if fn.signals and sig not in fn.signals:
                raise ConfigError(
                    f"功能 {fn.key} 的分段器 {fn.segmenter} 读取 {sig}，但它不在该功能的 signals 里")
            if sig not in cfg.required_signals:
                cfg.required_signals.append(sig)

    # ---- metrics.yaml ----
    mt_raw = _load_yaml(os.path.join(config_dir, "metrics.yaml"))
    mt_list = mt_raw.get("metrics") if isinstance(mt_raw, dict) else None
    if not isinstance(mt_list, list) or not mt_list:
        raise ConfigError("metrics.yaml 缺少非空 metrics 列表")
    for item in mt_list:
        key = item.get("key")
        if not key:
            raise ConfigError(f"指标模板缺少 key: {item}")
        if key in cfg.metrics:
            raise ConfigError(f"指标 key 重复: {key}")
        if not item.get("tool"):
            raise ConfigError(f"指标 {key} 缺少 tool")
        inputs = item.get("inputs") or []
        unknown = [i for i in inputs if i not in logical_names]
        if unknown:
            raise ConfigError(f"指标 {key} 引用未定义信号: {unknown}")
        for i in inputs:
            if i not in cfg.required_signals:
                cfg.required_signals.append(i)
        cfg.metrics[key] = MetricTemplate(
            key=key,
            name=item.get("name", key),
            tool=item["tool"],
            category=item.get("category", ""),
            unit=item.get("unit", ""),
            inputs=list(inputs),
            tuning_params=[str(t) for t in item.get("tuning_params") or []],
            description=item.get("description", ""),
        )

    # ---- conditions.yaml ----
    cond_raw = _load_yaml(os.path.join(config_dir, "conditions.yaml"))
    if not isinstance(cond_raw, dict):
        raise ConfigError("conditions.yaml 顶层必须是 {function_key: [condition, ...]}")
    seen_file_ids: Dict[str, str] = {}
    for fkey, items in cond_raw.items():
        if fkey not in fn_keys:
            raise ConfigError(f"conditions.yaml 顶层键 '{fkey}' 不在 functions.yaml 中")
        if not isinstance(items, list) or not items:
            raise ConfigError(f"功能 {fkey} 的工况列表为空")
        fn = cfg.functions[fkey]
        for item in items:
            cid = item.get("id")
            if not cid:
                raise ConfigError(f"功能 {fkey} 存在缺少 id 的工况: {item}")
            if cid in cfg.conditions:
                raise ConfigError(f"工况 id 重复: {cid}")
            metrics = item.get("metrics") or []
            if not metrics:
                raise ConfigError(f"工况 {cid} 未声明任何指标")
            cond_seg = str(item.get("segmenter") or "")
            if cond_seg and cond_seg not in SEGMENTERS:
                raise ConfigError(
                    f"工况 {cid} 的 segmenter '{cond_seg}' 未注册，可用: {sorted(SEGMENTERS)}")
            file_ids = item.get("file_ids") or []
            if isinstance(file_ids, str):
                raise ConfigError(f"工况 {cid} 的 file_ids 必须是列表，不能写成字符串")
            file_ids = [str(x).strip() for x in file_ids]
            for fid in file_ids:
                if not is_valid_file_id(fid):
                    raise ConfigError(
                        f"工况 {cid} 的 file_id '{fid}' 格式非法：只允许数字和点，如 4.1.1")
                if fid in seen_file_ids:
                    raise ConfigError(
                        f"file_id '{fid}' 同时挂在工况 {seen_file_ids[fid]} 和 {cid} 上")
                seen_file_ids[fid] = cid
            eff_seg = cond_seg or fn.segmenter
            if eff_seg not in SPEED_SPAN_SEGMENTERS:
                # 窗口不再是整段速度区间，「窗口时长 = 加速时间」「窗口起止
                # 距离差 = 制动距离」这两条物理含义不成立，宁可加载期报错。
                bad = [e.get("key") for e in metrics if e.get("key") in SPEED_SPAN_METRICS]
                if bad:
                    raise ConfigError(
                        f"工况 {cid} 使用分段器 {eff_seg}（窗口不是整段速度区间），"
                        f"不能声明指标 {bad}：它们的物理含义依赖速度穿越切窗")
            seen_pairs = set()
            for entry in metrics:
                mkey = entry.get("key")
                if mkey not in cfg.metrics:
                    raise ConfigError(f"工况 {cid} 引用未定义指标: {mkey}")
                prof = entry.get("profile")
                pair = (mkey, prof)
                if pair in seen_pairs:
                    raise ConfigError(f"工况 {cid} 指标条目重复: {pair}")
                seen_pairs.add(pair)
                if prof is not None and fn.profiles and prof not in fn.profiles:
                    raise ConfigError(
                        f"工况 {cid} 的 profile '{prof}' 不在功能 {fkey} 的 profiles {fn.profiles} 内"
                    )
                if "ok_range" in entry and entry["ok_range"] is not None:
                    rng = entry["ok_range"]
                    if not isinstance(rng, list) or len(rng) != 2:
                        raise ConfigError(f"工况 {cid} 指标 {mkey} 的 ok_range 必须为 [lo, hi]")
                    lo, hi = rng
                    if lo is not None and hi is not None and lo > hi:
                        raise ConfigError(f"工况 {cid} 指标 {mkey} 的 ok_range 下界大于上界")
            cfg.conditions[cid] = ConditionConfig(
                id=cid,
                function=fkey,
                name=item.get("name", cid),
                maneuver=item.get("maneuver", ""),
                surface_code=item.get("surface_code", ""),
                segmenter=cond_seg,
                file_ids=file_ids,
                params=dict(item.get("params") or {}),
                metrics=metrics,
                metric_index=build_metric_index(metrics),
                enabled_metrics=collect_enabled(metrics),
            )
            cfg.conditions_by_function.setdefault(fkey, []).append(cid)

    # §13 结构化解析的前提：一个功能内 maneuver+surface_code+params 必须唯一，
    # 否则「按属性匹配」会命中多条，无法自动开跑（只能出面板）。
    for fkey in cfg.conditions_by_function:
        seen_slot: Dict[tuple, str] = {}
        for cond in cfg.conditions_of(fkey):
            slot = _slot_key(cond)
            if not (cond.maneuver and cond.surface_code):
                raise ConfigError(
                    f"工况 {cond.id} 缺少 maneuver/surface_code，无法参与结构化目标解析（§13）"
                )
            if slot in seen_slot:
                raise ConfigError(
                    f"功能 {fkey} 下工况 {cond.id} 与 {seen_slot[slot]} 的 "
                    "maneuver+surface_code+params 完全相同，结构化解析无法区分"
                )
            seen_slot[slot] = cond.id

    # 工况级分段器覆盖的通道同样是计算必需：对接路面用 ABS_Active 切窗，
    # 缺这条通道就不是判定偏严，而是根本切不出窗口。
    for cond in cfg.conditions.values():
        fn = cfg.functions[cond.function]
        # 指标要用的通道也必须在功能 signals 里：漏写会让图能画、指标却算不出
        if fn.signals:
            for entry in cond.metrics:
                tmpl = cfg.metrics.get(entry.get("key"))
                if tmpl is None:
                    continue
                outside = [i for i in tmpl.inputs if i not in fn.signals]
                if outside:
                    raise ConfigError(
                        f"工况 {cond.id} 的指标 {tmpl.key} 需要 {outside}，"
                        f"但它们不在功能 {fn.key} 的 signals 里")
        if not cond.segmenter:
            continue
        for sig in SEGMENTER_SIGNALS.get(cond.segmenter, []):
            if sig not in logical_names:
                raise ConfigError(
                    f"工况 {cond.id} 的分段器 {cond.segmenter} 读取未登记信号: {sig}")
            if fn.signals and sig not in fn.signals:
                raise ConfigError(
                    f"工况 {cond.id} 的分段器 {cond.segmenter} 读取 {sig}，"
                    f"但它不在功能 {fn.key} 的 signals 里")
            if sig not in cfg.required_signals:
                cfg.required_signals.append(sig)

    # 编号索引：文件 → 编号 → 工况（§17）。重复编号在解析条件时已拦下。
    cfg.file_ids = build_index(
        (fid, cid) for cid, cond in cfg.conditions.items() for fid in cond.file_ids)

    # ---- rules.yaml ----
    rules_raw = _load_yaml(os.path.join(config_dir, "rules.yaml"))
    if not isinstance(rules_raw, list):
        raise ConfigError("rules.yaml 顶层必须是规则列表")
    seen_ids = set()
    for item in rules_raw:
        rid = item.get("id")
        if not rid:
            raise ConfigError(f"规则缺少 id: {item}")
        if rid in seen_ids:
            raise ConfigError(f"规则 id 重复: {rid}")
        seen_ids.add(rid)
        scope = item.get("scope")
        if scope not in ("function", "condition"):
            raise ConfigError(f"规则 {rid} 的 scope 必须为 function|condition，实际: {scope}")
        fkey = item.get("function")
        if fkey not in fn_keys:
            raise ConfigError(f"规则 {rid} 引用未定义功能: {fkey}")
        cond = item.get("condition")
        if scope == "condition":
            if not cond:
                raise ConfigError(f"规则 {rid} scope=condition 但缺少 condition")
            if cond not in cfg.conditions:
                raise ConfigError(f"规则 {rid} 引用未定义工况: {cond}")
            elif cfg.conditions[cond].function != fkey:
                raise ConfigError(f"规则 {rid} 的工况 {cond} 不属于功能 {fkey}")
        metric = item.get("metric")
        if metric not in cfg.metrics:
            raise ConfigError(f"规则 {rid} 引用未定义指标: {metric}")
        status = item.get("status")
        if status not in RULE_STATUSES:
            raise ConfigError(f"规则 {rid} 的 status 必须为 abnormal|missing，实际: {status}")
        sev = item.get("severity")
        if sev not in SEVERITIES:
            raise ConfigError(f"规则 {rid} 的 severity 非法: {sev}，可用: {sorted(SEVERITIES)}")
        fd = item.get("fault_domain")
        if fd not in FAULT_DOMAINS:
            raise ConfigError(f"规则 {rid} 的 fault_domain 非法: {fd}，可用: {sorted(FAULT_DOMAINS)}")
        cfg.rules.append(
            RuleConfig(
                id=rid,
                scope=scope,
                function=fkey,
                metric=metric,
                status=status,
                severity=sev,
                fault_domain=fd,
                kb_ref=item.get("kb_ref", ""),
                message=item.get("message", ""),
                condition=cond,
            )
        )

    # info 指标不得被规则引用：规则 status 枚举已限制为 abnormal|missing；
    # 额外检查规则引用的指标在某工况启用且存在「无 ok_range 的通用条目」（即纯 info 指标）。
    for rule in cfg.rules:
        candidates = (
            [cfg.conditions[rule.condition]]
            if rule.scope == "condition"
            else cfg.conditions_of(rule.function)
        )
        usable = [
            c for c in candidates
            if rule.metric in c.enabled_metrics
            and any(
                e.get("key") == rule.metric and ("ok_range" in e and e["ok_range"] is not None)
                for e in c.metrics
            )
        ]
        if candidates and not usable:
            raise ConfigError(
                f"规则 {rule.id} 引用的指标 {rule.metric} 在目标工况均为 info（无 ok_range），不允许触发规则"
            )

    # 计算必需的信号，MF4 侧必须齐全（MF4Loader 无条件启用）。
    # 观测量不参与这项检查：它们只出现在某一种格式里是正常情况。
    missing = [s for s in cfg.required_signals if s not in cfg.signal_maps]
    if missing:
        raise ConfigError(
            f"signals_mf4.yaml 缺少参与计算的信号: {missing}"
            f"（这些信号被 metrics.yaml 或分段器读取；纯观测量才允许只在单一格式出现）")

    return cfg


# ---------------------------------------------------------------- BLF 侧配置（§9.6）

_MSG_ID_RE = re.compile(r"^0x[0-9a-f]+x?$", re.I)
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# 自动识别通道的缺省参数（见 loaders/blf_channels.py）
_DETECT_DEFAULTS: Dict[str, Any] = {
    "scan_seconds": 15.0,
    "scan_frames": 500_000,
    "min_frames": 2,
    "min_hits": 2,
    "min_score": 0.6,
    "min_margin": 0.15,
}


@dataclass
class ChannelDetect:
    """自动识别通道的阈值（§9.6）。

    scan_seconds / scan_frames 只约束「采样识别」这一遍；识别失败时的回退全量
    重扫不受它们限制。min_frames 是「这个 ID 算在场」的最小帧数，用来挡噪声帧。
    min_margin 是互换损失阈值：把某总线换到别的通道后总分掉不到这个值，就判为
    分不清，需要显式 channels。
    """
    scan_seconds: float = 15.0
    scan_frames: int = 500_000
    min_frames: int = 2
    min_hits: int = 2
    min_score: float = 0.6
    min_margin: float = 0.15


@dataclass
class BlfConfigs:
    """signals_blf.yaml 的校验结果；dbc 已解析为绝对路径，可直接喂 BLFLoader。

    channels 为 None 表示自动识别（`channels: auto`）；否则是显式的
    {通道号: 总线名}。buses[name]["dbcs"] 是 {DBC 别名: 绝对路径}，
    ["unavailable"] 是因 ${ENV} 未设置而跳过的别名（识别照常，解码不了）。
    """
    buses: Dict[str, Dict[str, Any]]
    signal_maps: Dict[str, Dict[str, Any]]
    channels: Optional[Dict[int, str]] = None
    channel_detect: ChannelDetect = field(default_factory=ChannelDetect)


def _resolve_config_path(path: str, config_dir: str) -> str:
    """dbc 路径：绝对路径原样；相对路径先按 config_dir 解析，再按仓库根解析。

    signals_blf.yaml 里写 `configs/dbc/brake_demo.dbc`（仓库根相对），
    这样进程从任意 cwd 启动都能找到文件。
    """
    if os.path.isabs(path):
        return path
    for base in (config_dir, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))):
        cand = os.path.normpath(os.path.join(base, path))
        if os.path.exists(cand):
            return cand
    # 都不存在时返回 config_dir 版本，交给后面的存在性检查报错
    return os.path.normpath(os.path.join(config_dir, path))


def _expand_env(path: str) -> Optional[str]:
    """展开 ${VAR}；有变量没设置就返回 None（= 这份 DBC 在本机未提供）。

    真实 VBOX DBC 常因保密不能入库，只能写成部署机本地路径。缺变量不是配置
    错误，缺的是「这台机器上没有这份库」：识别照常（识别不读 DBC），只是解码
    不了该总线的信号。
    """
    missing = [m for m in _ENV_RE.findall(path) if m not in os.environ]
    if missing:
        return None
    return _ENV_RE.sub(lambda m: os.environ[m.group(1)], path)


def _parse_buses(raw: Any, config_dir: str) -> Dict[str, Dict[str, Any]]:
    """buses: {总线: {label, dbcs: {DBC 别名: 路径}}}。

    别名是 DBC 的身份（信号名属于某份 DBC，不属于总线），候选三元组写别名；
    一条总线可以持有多个别名。别名必须全局唯一，否则「这个别名属于哪条总线」
    就没有答案，通道识别也就无从谈起。
    """
    if not isinstance(raw, dict) or not raw:
        raise ConfigError("signals_blf.yaml 缺少非空 buses 段")
    buses: Dict[str, Dict[str, Any]] = {}
    owner: Dict[str, str] = {}
    for name, info in raw.items():
        if not isinstance(info, dict):
            raise ConfigError(f"buses.{name} 必须是映射（label + dbcs）")
        extra = set(info) - {"label", "dbcs"}
        if extra:
            raise ConfigError(f"buses.{name} 不支持字段 {sorted(extra)}")
        raw_dbcs = info.get("dbcs")
        if raw_dbcs is None:
            raw_dbcs = {}
        if not isinstance(raw_dbcs, dict):
            raise ConfigError(
                f"buses.{name}.dbcs 必须是 {{DBC 别名: 路径}} 映射，实际: {raw_dbcs!r}")
        dbcs: Dict[str, str] = {}
        unavailable: List[str] = []
        for alias, item in raw_dbcs.items():
            if not isinstance(alias, str) or not alias.strip():
                raise ConfigError(f"buses.{name}.dbcs 的别名必须是非空字符串: {alias!r}")
            alias = alias.strip()
            if alias in owner:
                raise ConfigError(
                    f"DBC 别名 {alias} 重复（buses.{owner[alias]} 与 buses.{name}）"
                    "：一个别名只能属于一条总线")
            owner[alias] = name
            if not isinstance(item, str) or not item.strip():
                raise ConfigError(f"buses.{name}.dbcs.{alias} 必须是非空路径")
            item = item.strip()
            expanded = _expand_env(item)
            if expanded is None:
                unavailable.append(alias)
                continue
            real = _resolve_config_path(expanded, config_dir)
            if not os.path.exists(real):
                raise ConfigError(f"DBC {alias} 的文件不存在: {item}")
            dbcs[alias] = real
        buses[name] = {"label": str(info.get("label") or name),
                       "dbcs": dbcs, "unavailable": unavailable}
    return buses


def dbc_alias_bus(buses: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """DBC 别名 → 所属总线；包含因 ${ENV} 未设置而没加载的别名。"""
    out: Dict[str, str] = {}
    for bus, info in buses.items():
        for alias in info.get("dbcs", {}):
            out[alias] = bus
        for alias in info.get("unavailable", []):
            out.setdefault(alias, bus)
    return out


def _parse_channels(raw: Any, buses: Dict[str, Any]) -> Optional[Dict[int, str]]:
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip().lower() == "auto":
        return None
    if not isinstance(raw, dict) or not raw:
        raise ConfigError("channels 必须是 auto 或 {通道号: 总线名}")
    out: Dict[int, str] = {}
    for ch, bus in raw.items():
        if isinstance(ch, bool) or not isinstance(ch, int) or ch < 0:
            raise ConfigError(f"channels 的键必须是通道号（非负 int），实际: {ch!r}")
        if bus not in buses:
            raise ConfigError(f"channels.{ch} 指向未定义的总线: {bus}")
        out[int(ch)] = str(bus)
    if len(set(out.values())) != len(out):
        raise ConfigError("channels 里同一总线不能绑定多个通道")
    return out


def _parse_detect(raw: Any) -> ChannelDetect:
    if raw is None:
        return ChannelDetect()
    if not isinstance(raw, dict):
        raise ConfigError("channel_detect 必须是映射")
    unknown = set(raw) - set(_DETECT_DEFAULTS)
    if unknown:
        raise ConfigError(f"channel_detect 不支持字段 {sorted(unknown)}")
    kw = {**_DETECT_DEFAULTS, **raw}
    for key in ("scan_seconds", "min_score", "min_margin"):
        v = kw[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ConfigError(f"channel_detect.{key} 必须是非负数: {v!r}")
    if kw["scan_seconds"] <= 0:
        raise ConfigError("channel_detect.scan_seconds 必须大于 0")
    for key in ("scan_frames", "min_frames", "min_hits"):
        v = kw[key]
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise ConfigError(f"channel_detect.{key} 必须是正整数: {v!r}")
    for key in ("min_score", "min_margin"):
        if kw[key] > 1:
            raise ConfigError(f"channel_detect.{key} 必须在 0~1 之间: {kw[key]!r}")
    return ChannelDetect(**kw)


def load_blf_config(config_dir: str = "configs",
                    required_signals: Optional[List[str]] = None,
                    registry: Optional[Dict[str, Dict[str, Any]]] = None) -> BlfConfigs:
    """加载并校验 signals_blf.yaml（结构错误抛 ConfigError，阻止启动）。

    只校验结构，不加载 DBC（不引入 python-can/cantools 依赖）：候选三元组解析失败
    由 BLFLoader 逐条回退，最终缺信号返回空序列。

    §9.6 的通道口径：`buses` 声明总线与各自的 DBC，`channels: auto` 时由
    BLFLoader 按「config 里出现的 msg_id」自动识别每条总线落在哪个 CAN 通道，
    不再手写 channel 号。识别不读 DBC，所以没有 DBC 的总线一样能被识别。

    覆盖度口径（§9.6）：BLF 是 CAN 报文，天然只有总线上发布的信号；MF4 常额外带
    观测量。所以两侧不要求信号集相同，只要求
      1) 用到的逻辑名都在登记表里（名字必须一致，不允许各起一名）；
      2) required_signals（参与计算的信号）一个不缺。
    观测量缺失是正常情况，BLFLoader 对其返回空序列，图表按现有逻辑跳过该行。

    registry（signals.yaml 的 logical_signals）给出时，单位/描述以它为准，
    格式侧不再重复声明。
    """
    path = os.path.join(config_dir, "signals_blf.yaml")
    raw = _load_yaml(path)          # 缺失/空文件由 _load_yaml 抛 ConfigError
    if not isinstance(raw, dict):
        raise ConfigError("signals_blf.yaml 顶层必须是映射（buses + signal_maps）")
    _reject_unknown_sections(
        raw, {"buses", "channels", "channel_detect", "signal_maps"}, "signals_blf.yaml")

    buses = _parse_buses(raw.get("buses"), config_dir)
    channels = _parse_channels(raw.get("channels"), buses)
    detect = _parse_detect(raw.get("channel_detect"))

    alias_bus = dbc_alias_bus(buses)

    signal_maps = raw.get("signal_maps")
    if not isinstance(signal_maps, dict) or not signal_maps:
        raise ConfigError("signals_blf.yaml 缺少非空 signal_maps 段")
    for name, spec in signal_maps.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("candidates"), list) \
                or not spec["candidates"]:
            raise ConfigError(f"signals_blf.yaml 信号 {name} 缺少非空 candidates 列表")
        _reject_signal_extras(spec, "signals_blf.yaml", name)
        for cand in spec["candidates"]:
            if not isinstance(cand, (list, tuple)) or len(cand) != 3:
                raise ConfigError(
                    f"信号 {name} 的候选必须是 [dbc_alias, msg_id, signal_name] 三元组: {cand}")
            dbc, msg_id, _sig = cand
            if dbc not in alias_bus:
                raise ConfigError(
                    f"信号 {name} 的候选 DBC 别名不在 buses.*.dbcs: {dbc}"
                    f"（已声明: {sorted(alias_bus)}）")
            if isinstance(msg_id, bool) or not isinstance(msg_id, (str, int)):
                raise ConfigError(f"信号 {name} 的 msg_id 必须是字符串或 int: {msg_id!r}")
            if isinstance(msg_id, str) and not _MSG_ID_RE.match(msg_id.strip()):
                raise ConfigError(
                    f"信号 {name} 的 msg_id 非法: {msg_id!r}（约定 \"0x123\" 标准帧、"
                    "\"0x123x\" 扩展帧）")

    # 每条总线至少要有一个候选引用它的某个 DBC：没有候选就既识别不了也解不了码
    used_buses = {alias_bus[c[0]] for spec in signal_maps.values()
                  for c in spec["candidates"]}
    orphan = sorted(set(buses) - used_buses)
    if orphan:
        raise ConfigError(
            f"buses 里的总线没有被任何候选引用: {orphan}"
            "（不用的总线请删掉；它有 DBC 但没有候选，识别时也帮不上忙）")

    # 三段校验各自独立，不以 registry 为前提：裸加载（不传 registry）是合法用法
    if registry is not None:
        # 1) 不许自创逻辑名：同一物理量两侧必须同名，否则跨格式对齐只能靠人
        stray = [n for n in signal_maps if n not in registry]
        if stray:
            raise ConfigError(
                f"signals_blf.yaml 引用未登记的逻辑信号: {sorted(stray)}"
                "（请先在 signals.yaml 登记；同一物理量两侧必须用同一逻辑名）")
    # 2) 参与计算的信号一个不能少
    if required_signals is not None:
        lacking = [s for s in required_signals if s not in signal_maps]
        if lacking:
            raise ConfigError(
                f"signals_blf.yaml 缺少参与计算的信号: {lacking}"
                "（纯观测量可以不带，参与计算的必须带）")
    # 单位/描述以登记表为准（格式侧声明已在上面拦下），candidates 保留格式侧声明
    if registry is not None:
        merged = {}
        for name, spec in signal_maps.items():
            entry = {k: v for k, v in spec.items() if k != "candidates"}
            entry.update(registry[name])
            entry["candidates"] = spec["candidates"]
            merged[name] = entry
        signal_maps = merged

    return BlfConfigs(buses=buses, signal_maps=signal_maps,
                      channels=channels, channel_detect=detect)
