"""工况语义召回（ConditionIndex + resolve_condition 兜底）测试。"""

import os

import pytest

from brake_analyzer.agent.cond_index import ConditionIndex
from brake_analyzer.agent.engine import ChatEngine
from brake_analyzer.agent.tools import resolve_condition
from web.store import Store
from brake_analyzer.llm.kb import KnowledgeBase
from brake_analyzer.configs import load_configs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "configs")


@pytest.fixture
def cfg():
    return load_configs(CONFIG_DIR)


class ScriptedIndex(ConditionIndex):
    """不走 sqlite：直接用脚本化的 search 结果测 resolve 的决策逻辑。"""

    def __init__(self, results):
        super().__init__(os.path.join(os.path.dirname(__file__), "data"))
        self._results = results
        self.available = True

    def search(self, query, top_k=3):
        return self._results


def test_condition_index_sync_and_ranking(tmp_path, cfg):
    """增量同步 + 语义排序：含「湿」的查询排到湿工况前。"""
    idx = ConditionIndex(str(tmp_path / "vectors.db"))

    def embed(texts):
        return [[1.0, 0.0] if "湿" in t else [0.0, 1.0] for t in texts]

    assert idx.set_embedder(embed, "fake") is True
    assert idx.sync(cfg) is True
    hits = idx.search("雨天湿滑路面", top_k=3)
    assert hits, "语义检索必须有结果"
    sims = [h["similarity"] for h in hits]
    assert sims == sorted(sims, reverse=True)
    # 湿工况（wet_basalt/wet_tile）应排在干沥青前面
    wet_first = [h["cond_id"] for h in hits if "wet" in h["cond_id"]]
    assert wet_first and hits[0]["cond_id"] in wet_first


def test_resolve_semantic_auto_confirm(cfg, tmp_path):
    """唯一键零命中 + 语义第一名明显领先 → 自动 resolved，带 via/reason。"""
    idx = ConditionIndex(str(tmp_path / "vectors.db"))
    idx._db = object()  # ScriptedIndex 不用 db；置 available
    idx.available = True
    idx.search = lambda q, top_k=3: [
        {"cond_id": "abs_full_brake_dry_asphalt_100kph", "distance": 0.4, "similarity": 0.92},
        {"cond_id": "abs_full_brake_wet_basalt_60kph", "distance": 1.2, "similarity": 0.28},
    ]
    out = resolve_condition(cfg, query="干沥青一百码急刹停住", index=idx)
    assert out["hop"] == "resolved"
    assert out["target"]["key"] == "abs_full_brake_dry_asphalt_100kph"
    assert out["target"]["via"] == "semantic"
    assert "语义匹配" in out["reason"]


def test_resolve_semantic_close_scores_go_to_user(cfg, tmp_path):
    """语义分数接近（无明显领先）→ 交用户确认，绝不自动开跑。"""
    idx = ConditionIndex(str(tmp_path / "vectors.db"))
    idx.available = True
    idx.search = lambda q, top_k=3: [
        {"cond_id": "abs_full_brake_dry_asphalt_100kph", "distance": 0.7, "similarity": 0.75},
        {"cond_id": "abs_full_brake_wet_basalt_60kph", "distance": 0.72, "similarity": 0.74},
        {"cond_id": "tcs_full_throttle_dry_asphalt_100kph", "distance": 0.75, "similarity": 0.72},
    ]
    out = resolve_condition(cfg, query="来一脚急刹看看", index=idx)
    assert out["hop"] == "condition"
    assert all(c.get("via") == "semantic" for c in out["candidates"])
    assert [c["similarity"] for c in out["candidates"]] == sorted(
        [c["similarity"] for c in out["candidates"]], reverse=True)


def test_resolve_exact_key_takes_priority_over_semantic(cfg, tmp_path):
    """唯一键命中时语义索引不参与——结构化解析永远是第一优先。"""
    idx = ConditionIndex(str(tmp_path / "vectors.db"))
    idx.available = True
    idx.search = lambda q, top_k=3: [
        {"cond_id": "abs_full_brake_wet_basalt_60kph", "distance": 0.1, "similarity": 0.99}]
    out = resolve_condition(cfg, function_key="abs", maneuver="full_brake",
                            surface_code="dry_asphalt", params={"v0_kph": 100}, index=idx)
    assert out["hop"] == "resolved"
    assert out["target"]["key"] == "abs_full_brake_dry_asphalt_100kph"
    assert out["target"]["via"] == "exact_key"


def test_resolve_without_index_unchanged(cfg):
    """没注入索引（离线/未启用 RAG）：行为与之前完全一致。"""
    out = resolve_condition(cfg, query="干沥青 100 全力制动")
    assert out["hop"] in ("resolved", "condition", "function")
    assert all("via" not in c or c.get("via") != "semantic" for c in out["candidates"])


# ---------------- 槽位证据校验（防模型编造槽位绕过面板） ----------------

def test_fabricated_surface_dropped_to_panel(cfg):
    """只说"2WD，TCS"却填 surface_code=dry_asphalt：无依据 → 丢弃 → 面板，绝不开跑。"""
    out = resolve_condition(cfg, function_key="tcs", maneuver="full_throttle",
                            surface_code="dry_asphalt", query="2WD TCS",
                            evidence="2WD TCS")
    assert out["hop"] == "condition"
    assert len(out["candidates"]) == 3          # 三条 TCS 工况都进面板
    assert "路面" in out["hint"]


def test_filename_justifies_surface(cfg):
    """文件名 tcs_dry100_normal.mf4 含 dry：有依据 → 唯一命中开跑。"""
    out = resolve_condition(cfg, function_key="tcs", maneuver="full_throttle",
                            surface_code="dry_asphalt", query="2WD TCS",
                            evidence="2WD TCS tcs_dry100_normal.mf4")
    assert out["hop"] == "resolved"
    assert out["target"]["key"] == "tcs_full_throttle_dry_asphalt_0to100kph"


def test_user_word_justifies_surface(cfg):
    out = resolve_condition(cfg, function_key="tcs", maneuver="full_throttle",
                            surface_code="dry_asphalt", query="干沥青 TCS",
                            evidence="干沥青 TCS")
    assert out["hop"] == "resolved"


def test_fabricated_param_dropped(cfg):
    """速度参数不在证据里 → 丢弃 → 键不全走面板。"""
    out = resolve_condition(cfg, function_key="abs", maneuver="full_brake",
                            surface_code="dry_asphalt", params={"v0_kph": 100},
                            query="ABS", evidence="ABS 刹车数据")
    assert out["hop"] == "condition"


def test_param_with_evidence_kept(cfg):
    out = resolve_condition(cfg, function_key="abs", maneuver="full_brake",
                            surface_code="dry_asphalt", params={"v0_kph": 100},
                            query="100 码急刹", evidence="abs_dry100_normal.mf4")
    assert out["hop"] == "resolved"
    assert out["target"]["key"] == "abs_full_brake_dry_asphalt_100kph"


def test_no_evidence_keeps_legacy_behavior(cfg):
    """evidence 未传（直调/测试）：不做校验，行为与旧版一致。"""
    out = resolve_condition(cfg, function_key="tcs", maneuver="full_throttle",
                            surface_code="dry_asphalt")
    assert out["hop"] == "resolved"


# ---------------- 档位缺维守卫 ----------------

def test_profile_guard_partial_profile_preselects(cfg):
    """多维功能只给 "4WD"：拦下面板，preset 预选 4WD，hint 说明。"""
    from tests.test_agent_tools import NoLlm
    from brake_analyzer.llm.kb import KnowledgeBase
    from web.store import Store
    from web.store import Store
    eng = ChatEngine(load_configs(CONFIG_DIR), NoLlm(),
                     KnowledgeBase(os.path.join(ROOT, "knowledge")), Store())
    chat = eng.store.create_chat()
    msg = eng._profile_guard(chat, "tcs_full_throttle_dry_asphalt_0to100kph", "4WD")
    assert msg is not None and msg.data["hop"] == "profile"
    assert msg.data["preset"] == ["4WD"]
    assert "预选" in msg.data["hint"]


def test_profile_guard_complete_profile_passes(cfg):
    from tests.test_agent_tools import NoLlm
    from brake_analyzer.llm.kb import KnowledgeBase
    from web.store import Store
    from web.store import Store
    eng = ChatEngine(load_configs(CONFIG_DIR), NoLlm(),
                     KnowledgeBase(os.path.join(ROOT, "knowledge")), Store())
    chat = eng.store.create_chat()
    assert eng._profile_guard(chat, "tcs_full_throttle_dry_asphalt_0to100kph",
                              "4WD|DTCS") is None


def test_profile_guard_no_profile_still_panels(cfg):
    from tests.test_agent_tools import NoLlm
    from brake_analyzer.llm.kb import KnowledgeBase
    from web.store import Store
    from web.store import Store
    eng = ChatEngine(load_configs(CONFIG_DIR), NoLlm(),
                     KnowledgeBase(os.path.join(ROOT, "knowledge")), Store())
    chat = eng.store.create_chat()
    msg = eng._profile_guard(chat, "tcs_full_throttle_dry_asphalt_0to100kph", None)
    assert msg is not None and "preset" not in msg.data


def test_engine_partial_profile_never_runs_analysis(cfg, tmp_path):
    """全链路：模型只给一维档位 → 档位面板，绝不静默走 default 条目分析。"""
    from tests.test_agent_tools import ToolLlm
    from brake_analyzer.llm.kb import KnowledgeBase
    from web.store import Store
    from web.store import Store
    llm = ToolLlm([("run_analysis_on_files", {
        "condition_id": "tcs_full_throttle_dry_asphalt_0to100kph", "profile": "4WD"})])
    eng = ChatEngine(load_configs(CONFIG_DIR), llm,
                     KnowledgeBase(os.path.join(ROOT, "knowledge")), Store())
    chat = eng.store.create_chat()
    for _ in eng.events(chat, "4WD 跑一下"):
        pass
    cards = [m for m in chat.messages if m.kind == "analysis_result"]
    assert not cards, "部分档位绝不能静默分析"
    panels = [m for m in chat.messages if m.kind == "target_options"
              and (m.data or {}).get("hop") == "profile"]
    assert panels and panels[0].data.get("preset") == ["4WD"]


# ---------------- P1 参数索引 + P2 工况过滤 + P3 符号保护 ----------------

def _kb_with_param_table(tmp_path):
    mdir = tmp_path / "manual"
    mdir.mkdir()
    (mdir / "cal.md").write_text(
        "# ABS 标定手册\n\n"
        "### cal.s1 — 车型基础参数表\n\n"
        "| 序号 | 参数名称 | 描述 | 参数定义 | 单位 |\n"
        "|---|---|---|---|---|\n"
        "| 15 | Cp_FA | 前轴CP值 | 53.95 | N·m/bar |\n"
        "| 16 | Cp_RA | 后轴CP值 | 26.87 | N·m/bar |\n\n"
        "### cal.s2 — 干沥青100km/h全力制动工况\n\n"
        "#### 标定参数说明\n\n"
        "| 序号 | 参数 | 初始值 | 当前值 |\n"
        "|---|---|---|---|\n"
        "| 1 | Opt_Instabil_Lambda_HA | 0.11 | 0.06 |\n"
        "| 2 | Opt_Tbl2 | [-4 ‑4 ‑4 ‑4 ‑4 ‑4] | [0 0 0 0 0 0] |\n\n"
        "### cal.s3 — 洒水玄武岩60km/h全力制动工况\n\n"
        "#### 标定参数说明\n\n"
        "| 序号 | 参数 | 初始值 | 当前值 |\n"
        "|---|---|---|---|\n"
        "| 1 | Opt_Instabil_Lambda_HA | 0.08 | 0.05 |\n",
        encoding="utf-8")
    return KnowledgeBase(str(tmp_path))


def test_get_param_exact_and_normalized(tmp_path):
    kb = _kb_with_param_table(tmp_path)
    rows = kb.get_param("Cp_FA")
    assert rows and rows[0]["fields"]["参数定义"] == "53.95"
    assert rows[0]["fields"]["单位"] == "N·m/bar"
    # 归一化：大小写/下划线差异不影响
    assert kb.get_param("cp_fa")[0]["param"] == "Cp_FA"
    assert kb.get_param("CP--FA")[0]["param"] == "Cp_FA"


def test_get_param_condition_disambiguation(tmp_path):
    """同名参数存在于多个工况：不过滤返回全部并带归属；过滤只留指定工况。"""
    kb = _kb_with_param_table(tmp_path)
    all_rows = kb.get_param("Opt_Instabil_Lambda_HA")
    assert {r["section"] for r in all_rows} >= {"干沥青100km/h全力制动工况",
                                                "洒水玄武岩60km/h全力制动工况"}
    scoped = kb.get_param("Opt_Instabil_Lambda_HA", condition="洒水玄武岩")
    assert len(scoped) == 1 and scoped[0]["fields"]["当前值"] == "0.05"


def test_get_param_unknown_returns_empty(tmp_path):
    kb = _kb_with_param_table(tmp_path)
    assert kb.get_param("Not_A_Param") == []


def test_special_symbols_survive_ingest_and_search(tmp_path):
    """U+2011 连字符 / ± / · / 方括号数组必须逐字符保留（P3 符号保护）。"""
    kb = _kb_with_param_table(tmp_path)
    hits = kb.search("Opt_Tbl2 初始值", top_k=1)
    text = hits[0]["text"]
    assert "[-4 ‑4 ‑4 ‑4 ‑4 ‑4]" in text, "U+2011 非断行连字符必须原样保留"
    assert "[0 0 0 0 0 0]" in text
    row = kb.get_param("Opt_Tbl2")
    assert row[0]["fields"]["初始值"] == "[-4 ‑4 ‑4 ‑4 ‑4 ‑4]"


def test_search_condition_filter_scopes_chunks(tmp_path):
    """工况过滤：查询词泛化时按工况硬过滤块，防同名表跨工况串扰。"""
    kb = _kb_with_param_table(tmp_path)
    hits = kb.search("标定参数说明", condition="洒水玄武岩", top_k=5)
    assert hits and all("洒水玄武岩" in h["title"] for h in hits)


def test_get_param_partial_name_fallback(tmp_path):
    """记不全参数名：子串双向匹配兜底，标注 match=partial。"""
    kb = _kb_with_param_table(tmp_path)
    rows = kb.get_param("Lambda_HA")             # 只记得尾部片段
    assert rows and rows[0]["match"] == "partial"
    assert {r["param"] for r in rows} == {"Opt_Instabil_Lambda_HA"}
    rows2 = kb.get_param("cp")                    # 短名前缀
    assert rows2 and {r["param"] for r in rows2} == {"Cp_FA", "Cp_RA"}
    assert all(r["match"] == "partial" for r in rows2)


def test_get_param_exact_wins_over_partial(tmp_path):
    kb = _kb_with_param_table(tmp_path)
    rows = kb.get_param("Cp_FA")
    assert all(r["match"] == "exact" for r in rows)


def test_kb_snippets_passes_condition(tmp_path):
    """标定建议通道把工况传给检索（防同名表跨工况串扰）。"""
    from tests.test_agent_tools import NoLlm
    from brake_analyzer.schemas import AnalysisResult
    eng = ChatEngine(load_configs(CONFIG_DIR), NoLlm(),
                     _kb_with_param_table(tmp_path), Store())
    captured = {}
    orig = eng.kb.search
    def spy(query, top_k=3, condition=None):
        captured["query"] = query
        captured["condition"] = condition
        return orig(query, top_k=top_k, condition=condition)
    eng.kb.search = spy
    res = AnalysisResult(analysis_id="an_x", condition_id="c1",
                         condition_name="干沥青100km/h全力制动",
                         function_key="abs", profile=None,
                         file_id="", file_name="f.mf4")
    fn = load_configs(CONFIG_DIR).function("abs")
    eng._kb_snippets(res, fn, query="干沥青 全力制动", condition=res.condition_name)
    assert captured["condition"] == "干沥青100km/h全力制动"
