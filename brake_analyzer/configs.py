"""配置加载与加载期校验/建索引（design.md §6.1、§10.3）。

加载失败抛 ConfigError，阻止启动。
"""

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

from .events.segment import SEGMENTERS
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


# ---------------------------------------------------------------- 加载器

@dataclass
class AppConfigs:
    config_dir: str
    functions: Dict[str, FunctionConfig] = field(default_factory=dict)
    conditions: Dict[str, ConditionConfig] = field(default_factory=dict)
    conditions_by_function: Dict[str, List[str]] = field(default_factory=dict)
    metrics: Dict[str, MetricTemplate] = field(default_factory=dict)
    rules: List[RuleConfig] = field(default_factory=list)
    signal_maps: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # ---- 查询辅助 ----
    def function(self, key: str) -> Optional[FunctionConfig]:
        return self.functions.get(key)

    def condition(self, cid: str) -> Optional[ConditionConfig]:
        return self.conditions.get(cid)

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


def load_configs(config_dir: str = "configs") -> AppConfigs:
    cfg = AppConfigs(config_dir=config_dir)

    # ---- signals_mf4.yaml ----
    sig_raw = _load_yaml(os.path.join(config_dir, "signals_mf4.yaml"))
    signal_maps = sig_raw.get("signal_maps") if isinstance(sig_raw, dict) else None
    if not isinstance(signal_maps, dict) or not signal_maps:
        raise ConfigError("signals_mf4.yaml 缺少非空 signal_maps 段")
    for name, spec in signal_maps.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("candidates"), list) or not spec["candidates"]:
            raise ConfigError(f"信号 {name} 缺少非空 candidates 列表")
    cfg.signal_maps = signal_maps
    logical_names = set(signal_maps)

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
        cfg.functions[key] = FunctionConfig(
            key=key,
            name=item.get("name", key),
            aliases=[str(a) for a in item.get("aliases") or []],
            segmenter=seg,
            kb_section=item.get("kb_section", ""),
            description=item.get("description", ""),
            profiles=profiles,
            profile_dims=dims,
        )
    fn_keys = set(cfg.functions)

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
                params=dict(item.get("params") or {}),
                metrics=metrics,
                metric_index=build_metric_index(metrics),
                enabled_metrics=collect_enabled(metrics),
            )
            cfg.conditions_by_function.setdefault(fkey, []).append(cid)

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

    return cfg
