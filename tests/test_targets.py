"""目标解析与渐进披露（design.md §13）+ 一次性内容折叠（§6.10）的行为测试。"""

import json
import os
import shutil
import warnings

warnings.filterwarnings("ignore", module="asammdf")

import pytest

from brake_analyzer.agent.tools import list_conditions, resolve_condition
from brake_analyzer.configs import ConfigError, load_configs
from brake_analyzer.llm.context import (
    STUB_PREFIX,
    ContextBudget,
    ContextCompressor,
    estimate_message_tokens,
    group_units,
    prune_transient,
)
from brake_analyzer.llm.prompts import build_tool_specs, function_catalog_text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "configs")


@pytest.fixture(scope="module")
def cfg():
    return load_configs(CONFIG_DIR)


# ---------------------------------------------------------------- 配置侧唯一键索引

def test_vocab_and_scope_conditions(cfg):
    v = cfg.vocab()
    assert set(v) == {"maneuver", "surface_code", "param_keys"}
    # 取值域的量级是「维度数」，不是「工况数」
    assert len(v["maneuver"]) <= len(cfg.conditions)
    assert "full_brake" in v["maneuver"] and "wet_basalt" in v["surface_code"]
    assert "v0_kph" in v["param_keys"]
    assert cfg.scope_conditions("abs") == [c.id for c in cfg.conditions_of("abs")]
    assert cfg.scope_conditions("nope") == []


def test_find_condition_exact_partial_none(cfg):
    hits = cfg.find_condition("abs", "full_brake", "wet_basalt", {"v0_kph": 60})
    assert [c.id for c in hits] == ["abs_full_brake_wet_basalt_60kph"]
    # 100 与 100.0 视为同值，不产生伪歧义
    hits2 = cfg.find_condition("abs", "full_brake", "dry_asphalt", {"v0_kph": 100.0})
    assert [c.id for c in hits2] == ["abs_full_brake_dry_asphalt_100kph"]
    # 键不全 → 多条命中，由调用方出面板
    assert len(cfg.find_condition("abs", "full_brake", None, {})) == 3
    # 不给功能 → 跨功能命中
    assert len(cfg.find_condition(None, "full_brake", "wet_basalt", {})) == 1
    assert cfg.find_condition("abs", "full_throttle", None, {}) == []


def test_find_condition_metric_only_params_are_subset(cfg):
    """params 用子集匹配：只报出 v0 也能定位，不必复述全部参数。"""
    hits = cfg.find_condition("abs", None, None, {"v0_kph": 50})
    assert [c.id for c in hits] == ["abs_full_brake_wet_tile_50kph"]


def test_config_rejects_ambiguous_slot(tmp_path):
    """同一功能下唯一键重复 → 结构化解析无法区分，加载时即失败。"""
    dst = tmp_path / "configs"
    shutil.copytree(CONFIG_DIR, dst)
    import yaml

    p = dst / "conditions.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    dup = json.loads(json.dumps(data["abs"][0], default=str))
    dup["id"] = "abs_duplicate_slot"
    data["abs"].append(dup)
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(ConfigError) as e:
        load_configs(str(dst))
    assert "完全相同" in str(e.value)


def test_config_rejects_missing_slot_fields(tmp_path):
    dst = tmp_path / "configs"
    shutil.copytree(CONFIG_DIR, dst)
    import yaml

    p = dst / "conditions.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    bad = json.loads(json.dumps(data["abs"][0], default=str))
    bad["id"] = "abs_no_surface"
    bad["surface_code"] = ""
    data["abs"].append(bad)
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(ConfigError) as e:
        load_configs(str(dst))
    assert "缺少 maneuver/surface_code" in str(e.value)


# ---------------------------------------------------------------- 工具语义

def test_list_conditions_is_transient_catalog(cfg):
    out = list_conditions(cfg, "abs")
    assert out["hop"] == "catalog" and out["transient"] is True
    assert out["count"] == len(cfg.conditions_of("abs"))
    first = out["conditions"][0]
    # 清单要带唯一键属性，否则模型看完清单也没法填槽
    assert {"maneuver", "surface_code", "params"} <= set(first)
    assert list_conditions(cfg, "nope")["hop"] == "error"


def test_resolve_condition_only_resolves_on_exact_hit(cfg):
    ok = resolve_condition(cfg, "abs", "full_brake", "wet_basalt", {"v0_kph": 60})
    assert ok["hop"] == "resolved"
    assert ok["target"]["key"] == "abs_full_brake_wet_basalt_60kph"
    assert "transient" not in ok          # 命中结果不是一次性清单，后面还要引用

    amb = resolve_condition(cfg, "abs", "full_brake", None, {})
    assert amb["hop"] == "condition" and amb["transient"] is True
    assert len(amb["candidates"]) == 3 and "唯一键" in amb["hint"]

    miss = resolve_condition(cfg, "abs", "full_brake", "wet_basalt", {"v0_kph": 999})
    assert miss["hop"] == "condition" and miss["candidates"]

    assert resolve_condition(cfg, None, None, None, {})["hop"] == "function"
    assert resolve_condition(cfg, "nope")["hop"] == "error"


def test_resolve_condition_cross_function_ambiguity_goes_back_to_function_layer(cfg):
    """没定功能且命中跨功能 → 先回到功能层，不把别的功能的工况混进同一面板。"""
    out = resolve_condition(cfg, None, None, "wet_basalt", {})
    # wet_basalt 在 abs / tcs 下各有一条
    assert out["hop"] == "function"
    assert {c["key"] for c in out["candidates"]} == {"abs", "tcs"}


def test_recommend_targets_candidates_are_transient_too(cfg):
    """兜底工具的候选同样是一次性内容，也要能被折叠掉。"""
    from brake_analyzer.agent.tools import recommend_targets

    out = recommend_targets(cfg, "制动", max_items=8)
    assert out["hop"] in ("function", "condition")
    assert out["transient"] is True
    assert json.dumps(out, ensure_ascii=False).index('"transient": true') < 400


# ---------------------------------------------------------------- prompt 组装

def test_function_catalog_resident_only_function_layer(cfg):
    text = function_catalog_text(cfg)
    for fn in cfg.functions.values():
        assert fn.key in text and fn.name in text
    assert "工况数:" in text
    # 工况名不进常驻目录（否则常驻成本随工况数线性涨）
    cond = cfg.conditions_of("abs")[0]
    assert cond.name not in text
    assert cond.name in function_catalog_text(cfg, include_conditions=True)


def test_build_tool_specs_enums_come_from_config(cfg):
    specs = {s["function"]["name"]: s for s in build_tool_specs(cfg)}
    assert set(specs) == {"resolve_condition", "list_conditions",
                          "run_analysis_on_files", "recommend_targets"}
    props = specs["resolve_condition"]["function"]["parameters"]["properties"]
    v = cfg.vocab()
    assert props["maneuver"]["enum"] == v["maneuver"]
    assert props["surface_code"]["enum"] == v["surface_code"]
    assert set(props["params"]["properties"]) == set(v["param_keys"])
    # 部分兼容端点拒绝 type 数组，这里必须是单值字符串
    for p in props["params"]["properties"].values():
        assert p["type"] == "number"
    # 功能已定 → condition_id 取值被限定在该功能范围内
    scoped = {s["function"]["name"]: s for s in build_tool_specs(cfg, "abs")}
    cid = scoped["run_analysis_on_files"]["function"]["parameters"]["properties"]["condition_id"]
    assert cid["enum"] == cfg.scope_conditions("abs")
    unscoped = specs["run_analysis_on_files"]["function"]["parameters"]["properties"]["condition_id"]
    assert "enum" not in unscoped


def test_build_tool_specs_profile_guidance_when_in_scope(cfg):
    """功能已定且声明了档位：把档位取值域告诉模型，但不用 required 逼它编一个。"""
    scoped = {s["function"]["name"]: s for s in build_tool_specs(cfg, "tcs")}
    prof = scoped["run_analysis_on_files"]["function"]["parameters"]["properties"]["profile"]
    assert all(p in prof["description"] for p in cfg.function("tcs").profiles)
    assert "不要自己定" in prof["description"]
    assert scoped["run_analysis_on_files"]["function"]["parameters"]["required"] == ["condition_id"]
    # abs 没有档位 → 保持原来的可选说明
    plain = {s["function"]["name"]: s for s in build_tool_specs(cfg, "abs")}
    p2 = plain["run_analysis_on_files"]["function"]["parameters"]["properties"]["profile"]
    assert "该功能声明了档位" not in p2["description"]


def test_system_chat_forbids_autopick():
    from brake_analyzer.llm.prompts import SYSTEM_CHAT

    assert "hop=resolved" in SYSTEM_CHAT and "不得自选一条" in SYSTEM_CHAT
    assert "list_conditions" in SYSTEM_CHAT


# ---------------------------------------------------------------- transient 折叠

def _unit(payload, transient=True, tail=""):
    flag = ", " if tail else ""
    body = json.dumps({"hop": "catalog", "transient": transient,
                       "conditions": [{"key": "c1", "name": "工况甲"},
                                      {"key": "c2", "name": "工况乙"}]} if not payload
                      else payload, ensure_ascii=False)
    return [
        {"role": "user", "content": f"提问{tail}"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"t{tail}", "type": "function", "function": {
                "name": "list_conditions", "arguments": '{"function_key":"abs"}'}}]},
        {"role": "tool", "tool_call_id": f"t{tail}", "content": body + flag + tail},
    ]


def test_prune_transient_stubs_old_keeps_recent(cfg):
    catalog = list_conditions(cfg, "abs")
    msgs = [{"role": "system", "content": "S"}]
    for i in range(4):
        msgs += _unit(catalog, tail=str(i))
    msgs.append({"role": "user", "content": "本轮提问"})

    out, n = prune_transient(msgs)
    # 5 个轮次单元（4 个工具往返 + 本轮提问），保留最近 2 个 → 折叠 3 条
    assert n == 3, "只折叠最近 2 个单元之外的 transient 结果"
    tools = [m for m in out if m.get("role") == "tool"]
    stubbed = [m for m in tools if m["content"].startswith(STUB_PREFIX)]
    assert len(stubbed) == 3
    # 最近 2 个单元原样保留（其中含 1 次工具往返），模型才能据此继续填槽
    assert not tools[-1]["content"].startswith(STUB_PREFIX)
    assert sum(1 for m in tools if not m["content"].startswith(STUB_PREFIX)) == 1
    assert out[-1]["content"] == "本轮提问" and out[0]["role"] == "system"
    # 消息壳保留：tool_calls 与 tool 应答仍配对，否则端点报 400
    ids = {tc["id"] for m in out for tc in (m.get("tool_calls") or [])}
    assert all(m["tool_call_id"] in ids for m in tools)
    assert len(group_units([m for m in out if m.get("role") != "system"])) == \
        len(group_units([m for m in msgs if m.get("role") != "system"]))
    # 存根比原文小得多，且仍带 hop 供追溯
    assert estimate_message_tokens(out) < estimate_message_tokens(msgs)
    assert "hop=catalog" in stubbed[0]["content"]


def test_prune_transient_skips_non_transient_and_idempotent(cfg):
    resolved = resolve_condition(cfg, "abs", "full_brake", "wet_basalt", {"v0_kph": 60})
    msgs = [{"role": "system", "content": "S"}]
    for i in range(4):
        msgs += _unit(resolved, tail=str(i))
    out, n = prune_transient(msgs)
    assert (n, out) == (0, msgs), "resolved 结果不是清单，不该被折叠"

    catalog = list_conditions(cfg, "abs")
    msgs2 = [{"role": "system", "content": "S"}]
    for i in range(4):
        msgs2 += _unit(catalog, tail=str(i))
    once, _ = prune_transient(msgs2)
    twice, n2 = prune_transient(once)
    assert n2 == 0 and twice == once, "已折叠的存根不重复计数"


def test_fit_reports_pruning_below_compress_threshold(cfg):
    """没到压缩阈值也要折叠 transient 并如实报告（context 只保留有效内容）。"""
    catalog = list_conditions(cfg, "abs")
    msgs = [{"role": "system", "content": "S" * 40}]
    for i in range(5):
        msgs += _unit(catalog, tail=str(i))
    msgs.append({"role": "user", "content": "本轮提问"})
    b = ContextBudget(max_tokens=8000, compress_tokens=6000)
    assert estimate_message_tokens(msgs) < b.compress_tokens
    out, note = ContextCompressor(b).fit(msgs)
    assert note and "一次性清单" in note
    assert estimate_message_tokens(out) < estimate_message_tokens(msgs)

# ---------------------------------------------------------------- 引擎接入

class ToolLlm:
    """按脚本依次返回工具调用，最后给文本结论。"""

    def __init__(self, rounds):
        self.available = True
        self.rounds = list(rounds)
        self.calls = []
        self.tools_seen = []

    def chat_stream(self, messages, tools=None, **kw):
        from brake_analyzer.llm.client import LlmReply

        self.calls.append([dict(m) for m in messages])
        self.tools_seen.append([t["function"]["name"] for t in (tools or [])])
        if self.rounds:
            name, args = self.rounds.pop(0)
            yield {"type": "finish", "reply": LlmReply(tool_calls=[
                {"id": f"call_{len(self.calls)}", "name": name, "arguments": args}])}
        else:
            yield {"type": "finish", "reply": LlmReply(content="已按唯一键定位，结论见分析卡片。")}

    def chat(self, messages, **kw):
        from brake_analyzer.llm.client import LlmReply

        return LlmReply(content="摘要：已确定 ABS 湿玄武岩 60kph。")

    def complete_json(self, system, user, **kw):
        return None


def _engine(llm, budget=None):
    from brake_analyzer.agent.engine import ChatEngine
    from brake_analyzer.llm.kb import KnowledgeBase
    from web.store import Store

    eng = ChatEngine(load_configs(CONFIG_DIR), llm,
                     KnowledgeBase(os.path.join(ROOT, "knowledge")), Store(),
                     budget=budget or ContextBudget(max_tokens=8000, compress_tokens=6000))
    return eng


def _kinds(chat):
    return [m.kind for m in chat.messages]


def test_engine_resolved_creates_no_panel():
    """hop=resolved 是结构化命中，不该再多弹一次确认面板。"""
    llm = ToolLlm([("resolve_condition", {"function_key": "abs", "maneuver": "full_brake",
                                         "surface_code": "wet_basalt", "params": {"v0_kph": 60}})])
    eng = _engine(llm)
    chat = eng.store.create_chat()
    for _ in eng.events(chat, "湿玄武岩 60 全力制动的 ABS"):
        pass
    assert "target_options" not in _kinds(chat)
    tool_msgs = [m for m in llm.calls[-1] if m.get("role") == "tool"]
    assert tool_msgs and json.loads(tool_msgs[-1]["content"])["hop"] == "resolved"


def test_engine_function_panel_lists_only_functions():
    """hop=function 的面板不能混进工况条目——清单只回灌模型。"""
    llm = ToolLlm([("resolve_condition", {})])
    eng = _engine(llm)
    chat = eng.store.create_chat()
    for _ in eng.events(chat, "全力制动看下"):
        pass
    panels = [m for m in chat.messages if m.kind == "target_options"]
    assert len(panels) == 1 and panels[0].data["hop"] == "function"
    assert {c["key"] for c in panels[0].data["candidates"]} == set(eng.cfg.functions)


def test_engine_blocks_analysis_when_profile_missing():
    """resolved 只保证工况唯一：声明档位的功能未选档时与离线口径一致，先弹档位。"""
    cond = "tcs_full_throttle_wet_basalt_0to60kph"
    assert _engine(ToolLlm([])).cfg.function("tcs").profiles, "用例依赖 tcs 声明了 profiles"
    llm = ToolLlm([("run_analysis_on_files", {"condition_id": cond})])
    eng = _engine(llm)
    chat = eng.store.create_chat()
    eng.store.add_message(chat, "user", "attachment", content="a.mf4")
    for _ in eng.events(chat, "直接跑这个 TCS 工况"):
        pass
    panels = [m for m in chat.messages if m.kind == "target_options"]
    assert len(panels) == 1 and panels[0].data["hop"] == "profile"
    # 面板要能被前端现有渲染路径接住：档位下拉 + 工况 key
    assert panels[0].data["profiles"] and panels[0].data["target"]["key"] == cond
    assert "analysis_result" not in _kinds(chat), "未选档不该出分析结果"
    tool_msgs = [m for m in llm.calls[-1] if m.get("role") == "tool"]
    assert tool_msgs and json.loads(tool_msgs[-1]["content"])["hop"] == "profile"
    labels = [it["label"] for m in chat.messages if m.kind == "steps"
              for it in (m.data or {}).get("items", [])]
    assert "档位守卫：暂不分析" in labels, "拦截过程要在步骤里看得见"


def test_engine_allows_analysis_with_profile_or_no_profiles():
    """没声明档位的功能（abs）命中即分析；带档位的 TCS 给了 profile 就放行。"""
    llm = ToolLlm([("run_analysis_on_files",
                    {"condition_id": "tcs_full_throttle_wet_basalt_0to60kph",
                     "profile": "4WD"})])
    eng = _engine(llm)
    chat = eng.store.create_chat()
    for _ in eng.events(chat, "4WD 跑 TCS"):
        pass
    errs = [m for m in chat.messages if m.kind == "error"]
    # 没有上传文件 → _analyze 报"请先上传"，但绝不能是档位面板
    assert not [m for m in chat.messages if m.kind == "target_options"]
    assert errs and "上传" in errs[0].content


def test_engine_returns_numbers_to_llm_after_analysis(tmp_path):
    """分析后 tool 应答必须带实测值与判定，否则模型正文只能让用户自己看图（§6.9）。"""
    data = os.path.join(ROOT, "tests", "data", "mf4", "abs_dry100_dist_abn.mf4")
    if not os.path.exists(data):
        pytest.skip("demo 数据未生成")
    from brake_analyzer.agent.engine import ChatEngine
    from brake_analyzer.llm.kb import KnowledgeBase
    from web.store import Store

    llm = ToolLlm([("run_analysis_on_files",
                    {"condition_id": "abs_full_brake_dry_asphalt_100kph"})])
    eng = ChatEngine(load_configs(CONFIG_DIR), llm,
                     KnowledgeBase(os.path.join(ROOT, "knowledge")),
                     Store(upload_dir=str(tmp_path / "uploads")))
    chat = eng.store.create_chat()
    with open(data, "rb") as f:
        eng.store.add_file(chat, "abs_dry100_dist_abn.mf4", f.read())
    for _ in eng.events(chat, "干沥青 100 全力制动，制动距离超了吗"):
        pass

    tool_msgs = [m for m in llm.calls[-1] if m.get("role") == "tool"]
    payload = json.loads(tool_msgs[-1]["content"])
    assert payload["ok"] is True
    got = {m["metric"]: m for s in payload["results"][0]["samples"] for m in s["metrics"]}
    assert got["制动距离"]["status"] == "abnormal"
    assert got["制动距离"]["value"] == pytest.approx(85.65, abs=0.5)
    assert got["制动距离"]["limit"] == "≤40" and got["制动距离"]["unit"] == "m"
    assert got["最大横摆角速度"]["status"] == "ok"
    # 规则结论原文一并给出，模型不必自己措辞
    assert any(v["rule"] == "abs_dist_abnormal"
               for s in payload["results"][0]["samples"] for v in s.get("verdicts", []))
    # 结果卡片正文自带数值行，跨轮追问时由 history 回灌
    card = [m for m in chat.messages if m.kind == "analysis_result"][0]
    assert "制动距离=85" in card.content and "不合格" in card.content

    # 下一轮只追问数值、不再调工具：模型必须能从历史里读到那行数字
    for _ in eng.events(chat, "那实测制动距离到底多少"):
        pass
    sent = llm.calls[-1]
    assert any("制动距离=85" in (m.get("content") or "") for m in sent), \
        "结果卡片的数值行没有回灌，跨轮追问又会变成「请查看图表」"


def test_engine_reports_no_result_without_files():
    """没跑出结果时 tool 应答说清楚"没有结果"，别给模型一个成功的假象。"""
    llm = ToolLlm([("run_analysis_on_files",
                    {"condition_id": "abs_full_brake_dry_asphalt_100kph"})])
    eng = _engine(llm)
    chat = eng.store.create_chat()
    for _ in eng.events(chat, "跑一下干沥青 100"):
        pass
    tool_msgs = [m for m in llm.calls[-1] if m.get("role") == "tool"]
    payload = json.loads(tool_msgs[-1]["content"])
    assert payload["ok"] is False and payload.get("results") is None
    assert "上传" in payload["note"]


def test_digest_payload_degrades_instead_of_breaking_json():
    """摘要超预算时按结论原文 → verdicts → 旧结果逐级降级，产出必须是合法 JSON。"""
    from brake_analyzer.llm.prompts import TOOL_PAYLOAD_CLIP_CHARS, digest_payload

    def fake(i):
        return {"condition_name": f"工况{i}", "samples": [{
            "run": i, "file": f"f{i}.mf4", "window_s": [1.0, 2.0],
            "metrics": [{"metric": "制动距离", "value": 40.0 + i, "unit": "m",
                         "status": "abnormal", "limit": "≤40"}],
            "verdicts": [{"rule": f"r{i}", "metric": "制动距离",
                          "conclusion": "说明" * 60, "direction": "高于上界 40m",
                          "severity": "high"}]}]}

    many = [fake(i) for i in range(40)]
    kept = digest_payload(many, TOOL_PAYLOAD_CLIP_CHARS - 256)
    assert kept and len(json.dumps(kept, ensure_ascii=False)) <= TOOL_PAYLOAD_CLIP_CHARS
    assert len(kept) < len(many)
    # 保留的是最近的几份，且数值仍完整
    assert kept[-1]["condition_name"] == many[-1]["condition_name"]
    assert kept[-1]["samples"][0]["metrics"][0]["value"] == 79.0
    # 单份就超限的极端情况不崩：宁可不给，也不给坏 JSON
    huge = [{"condition_name": "x", "samples": [{"run": 1, "file": "f",
              "metrics": [{"metric": "m" * 3000, "value": 1, "unit": "", "status": "ok"}]}]}]
    assert digest_payload(huge, 100) == []


def test_engine_ambiguous_opens_condition_panel_only_from_that_function():
    llm = ToolLlm([("resolve_condition", {"function_key": "abs", "maneuver": "full_brake"})])
    eng = _engine(llm)
    chat = eng.store.create_chat()
    for _ in eng.events(chat, "ABS 全力制动看下"):
        pass
    panels = [m for m in chat.messages if m.kind == "target_options"]
    assert len(panels) == 1 and panels[0].data["hop"] == "condition"
    assert {c["function"] for c in panels[0].data["candidates"]} == {"abs"}


def test_engine_catalog_does_not_open_panel():
    """工况清单是给模型填槽用的，不是给用户看的选项。"""
    llm = ToolLlm([("list_conditions", {"function_key": "abs"}),
                   ("resolve_condition", {"function_key": "abs", "maneuver": "full_brake",
                                          "surface_code": "wet_tile", "params": {"v0_kph": 50}})])
    eng = _engine(llm)
    chat = eng.store.create_chat()
    for _ in eng.events(chat, "瓷砖那个工况"):
        pass
    assert "target_options" not in _kinds(chat)
    labels = [it["label"] for m in chat.messages if m.kind == "steps"
              for it in (m.data or {}).get("items", [])]
    assert any(l == "list_conditions → hop=catalog" for l in labels)


def test_engine_sends_config_derived_tool_specs():
    llm = ToolLlm([])
    eng = _engine(llm)
    chat = eng.store.create_chat()
    chat.selected_function = "abs"
    for _ in eng.events(chat, "先说下结论"):
        pass
    names = llm.tools_seen[0]
    assert {"resolve_condition", "list_conditions", "run_analysis_on_files",
            "recommend_targets"} == set(names), "四个工具都要暴露，兜底路径同一实现"


def test_engine_prunes_old_catalog_from_later_requests():
    """清单只服务当次定位：离开最近窗口后，后续请求里只剩存根（§6.10）。"""
    budget = ContextBudget(max_tokens=4000, compress_tokens=3600)
    rounds = [("list_conditions", {"function_key": "abs"})] * 5
    llm = ToolLlm(rounds)
    eng = _engine(llm, budget)
    chat = eng.store.create_chat()
    for _ in eng.events(chat, "瓷砖那个工况"):
        pass
    assert len(llm.calls) >= 4, "应真的走过多跳"
    for k, sent in enumerate(llm.calls, start=1):
        est = estimate_message_tokens(sent)
        assert est <= budget.max_tokens, f"第{k}次请求超限：{est}"
        ids = {tc["id"] for m in sent for tc in (m.get("tool_calls") or [])}
        for m in sent:
            if m.get("role") == "tool":
                assert m["tool_call_id"] in ids, f"第{k}次请求有孤立 tool 消息"
    # 最后一次请求里，早期清单已折叠，只有最近的还带 conditions 原文
    tools = [m for m in llm.calls[-1] if m.get("role") == "tool"]
    stubs = [m for m in tools if m["content"].startswith(STUB_PREFIX)]
    full = [m for m in tools if not m["content"].startswith(STUB_PREFIX)]
    assert stubs, f"早期清单应折叠为存根，实际：{[t['content'][:20] for t in tools]}"
    assert full, "最近一次清单要留原文，否则模型无法继续填槽"
    assert len(tools) == len(rounds), "折叠只压正文，不删消息壳"
    labels = [it["label"] for m in chat.messages if m.kind == "steps"
              for it in (m.data or {}).get("items", [])]
    assert "上下文压缩" in labels, "折叠发生在阈值之下也要报告"
