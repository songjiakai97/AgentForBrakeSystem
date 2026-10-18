"""工况语义召回（ConditionIndex + resolve_condition 兜底）测试。"""

import os

import pytest

from brake_analyzer.agent.cond_index import ConditionIndex
from brake_analyzer.agent.engine import ChatEngine
from brake_analyzer.agent.tools import resolve_condition
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
    eng = ChatEngine(load_configs(CONFIG_DIR), NoLlm(),
                     KnowledgeBase(os.path.join(ROOT, "knowledge")), Store())
    chat = eng.store.create_chat()
    assert eng._profile_guard(chat, "tcs_full_throttle_dry_asphalt_0to100kph",
                              "4WD|DTCS") is None


def test_profile_guard_no_profile_still_panels(cfg):
    from tests.test_agent_tools import NoLlm
    from brake_analyzer.llm.kb import KnowledgeBase
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
