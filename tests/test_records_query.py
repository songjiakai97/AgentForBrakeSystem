"""记录库的对话查询/对比工具（§17.5）。"""

import importlib.util
import os

import pytest

from brake_analyzer.agent.engine import ChatEngine
from brake_analyzer.configs import load_configs
from brake_analyzer.llm.prompts import build_tool_specs
from brake_analyzer.records import store
from brake_analyzer.records.query import records_compare, records_query
from brake_analyzer.records.service import RecordService
from brake_analyzer.schemas import (
    AnalysisResult,
    EventWindow,
    MetricResult,
    RunSample,
    compose_metric_key,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "configs")


def _load_script():
    path = os.path.join(ROOT, "scripts", "import_records.py")
    spec = importlib.util.spec_from_file_location("import_records_q", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cfg():
    return load_configs(CONFIG_DIR)


def _analysis(condition_id, distance, distance_status="ok"):
    key = compose_metric_key(condition_id, "brake_distance")
    metrics = [
        MetricResult(key=key, name="制动距离", category="brake_performance",
                     value=distance, unit="m", ts_range=(1.0, 4.0),
                     status=distance_status, ok_range=[0, 45]),
        MetricResult(key=compose_metric_key(condition_id, "yaw_rate_max"),
                     name="最大横摆角速度", category="stability", value=None,
                     unit="deg/s", ts_range=None, status="missing",
                     missing_reason="signal_absent"),
    ]
    sample = RunSample(sample_id="s1", file_id="4.1.1", file_name="f.blf",
                       run_index=1, window=EventWindow(1.0, 4.0),
                       metrics=metrics, analysis_id="an_1")
    return AnalysisResult(analysis_id="an_1", condition_id=condition_id,
                          condition_name="工况", function_key="abs", profile=None,
                          file_id="4.1.1", file_name="f.blf", samples=[sample])


@pytest.fixture()
def db(tmp_path, cfg):
    """两车型 × 两工况；其中一条故意不分析，用来验证“只比已分析的”。"""
    imp = _load_script()
    root = tmp_path / "data"
    for rel, data in [
        ("carA/4-ABS/4.1.1_20260801.blf", b"a1"),
        ("carA/4-ABS/4.1.3.1_20260801.blf", b"a2"),
        ("carB/4-ABS/4.1.1_20260802.blf", b"b1"),
        ("carB/4-ABS/4.1.3.1_20260802.blf", b"b2"),
    ]:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    db_path = str(tmp_path / "records.db")
    imp.run_import(str(root), db_path, cfg)

    conn = store.connect(db_path)
    rows = {r["file_name"]: r for r in store.query_records(conn)}
    fp = store.config_fingerprint(CONFIG_DIR)
    store.save_analysis(conn, rows["4.1.1_20260801.blf"]["record_id"],
                        _analysis("abs_full_brake_dry_asphalt_100kph", 40.1), fp)
    store.save_analysis(conn, rows["4.1.1_20260802.blf"]["record_id"],
                        _analysis("abs_full_brake_dry_asphalt_100kph", 42.3,
                                  distance_status="abnormal"), fp)
    store.save_analysis(conn, rows["4.1.3.1_20260801.blf"]["record_id"],
                        _analysis("abs_full_brake_wet_asphalt_60kph", 26.5), fp)
    conn.close()
    return {"db": db_path, "rows": {k: dict(v) for k, v in rows.items()}}


def _svc(cfg, db):
    return RecordService(cfg, db["db"])


def test_query_by_file_id(cfg, db):
    res = records_query(_svc(cfg, db), cfg, file_id="4.1.1")
    assert res["ok"] and res["count"] == 2
    assert {r["vehicle"] for r in res["records"]} == {"carA", "carB"}
    car_a = next(r for r in res["records"] if r["vehicle"] == "carA")
    assert car_a["metrics"]["brake_distance"]["value"] == 40.1
    assert car_a["record_id"].startswith("rec_")


def test_query_marks_unanalyzed(cfg, db):
    res = records_query(_svc(cfg, db), cfg, condition_id="abs_full_brake_wet_asphalt_60kph")
    statuses = {r["vehicle"]: r["analysis_status"] for r in res["records"]}
    assert statuses == {"carA": "ok", "carB": "pending"}


def test_query_filters_by_vehicle_and_function(cfg, db):
    svc = _svc(cfg, db)
    assert records_query(svc, cfg, vehicle="carB")["count"] == 2
    assert records_query(svc, cfg, function_key="abs")["count"] == 4
    assert records_query(svc, cfg, function_key="tcs")["count"] == 0
    assert records_query(svc, cfg, function_key="nope")["ok"] is False


def test_query_empty_gives_inventory(cfg, db):
    res = records_query(_svc(cfg, db), cfg, file_id="9.9.9")
    assert res["ok"] and res["count"] == 0
    assert res["vehicles"] == ["carA", "carB"]
    assert "4.1.1" in res["file_ids"]


def test_query_matches_substrings(cfg, db):
    """编号/车型/关键词都按「包含」匹配：只记得片段时也查得到。"""
    svc = _svc(cfg, db)
    assert records_query(svc, cfg, file_id="4.1.3")["count"] == 2
    assert records_query(svc, cfg, vehicle="car")["count"] == 4
    assert records_query(svc, cfg, keyword="4.1.1")["count"] == 2
    assert records_query(svc, cfg, keyword="20260802")["count"] == 2      # 只在文件名里
    assert records_query(svc, cfg, keyword="nothing-here")["count"] == 0


def test_query_keyword_escapes_like_wildcards(cfg, db):
    """关键词里的 % / _ 是字面量，不能被当成 LIKE 通配符。"""
    svc = _svc(cfg, db)
    assert records_query(svc, cfg, keyword="%")["count"] == 0
    assert records_query(svc, cfg, file_id="4.1.1%")["count"] == 0


def test_compare_groups_by_condition(cfg, db):
    res = records_compare(_svc(cfg, db), cfg, condition_id="abs_full_brake_dry_asphalt_100kph")
    assert res["ok"] and res["record_count"] == 2
    group = res["groups"][0]
    by_vehicle = {r["vehicle"]: r for r in group["records"]}
    assert by_vehicle["carA"]["metrics"]["brake_distance"]["value"] == 40.1
    assert by_vehicle["carB"]["metrics"]["brake_distance"]["status"] == "abnormal"
    assert by_vehicle["carA"]["metrics"]["yaw_rate_max"]["missing_reason"] == "signal_absent"


def test_compare_lists_not_analyzed(cfg, db):
    res = records_compare(_svc(cfg, db), cfg, condition_id="abs_full_brake_wet_asphalt_60kph")
    assert res["record_count"] == 1
    assert [r["vehicle"] for r in res["not_analyzed"]] == ["carB"]
    assert res["not_analyzed"][0]["analysis_status"] == "pending"


def test_compare_metric_filter_and_unknown(cfg, db):
    svc = _svc(cfg, db)
    res = records_compare(svc, cfg, condition_id="abs_full_brake_dry_asphalt_100kph",
                          metric="yaw_rate_max")
    metrics = res["groups"][0]["records"][0]["metrics"]
    assert list(metrics) == ["yaw_rate_max"]
    assert records_compare(svc, cfg, condition_id="abs_full_brake_dry_asphalt_100kph",
                           metric="nope")["ok"] is False


def test_compare_requires_scope(cfg, db):
    svc = _svc(cfg, db)
    assert records_compare(svc, cfg)["ok"] is False
    assert records_compare(svc, cfg, function_key="tcs")["groups"] == []


def test_compare_vehicle_filter(cfg, db):
    res = records_compare(_svc(cfg, db), cfg, function_key="abs", vehicles=["carB"])
    for group in res["groups"]:
        assert [r["vehicle"] for r in group["records"]] == ["carB"]


def test_record_tool_guards_and_filters(cfg, db):
    engine = ChatEngine.__new__(ChatEngine)
    engine.cfg = cfg
    engine.records = None
    out = engine._record_tool(records_query, {"file_id": "4.1.1"}, None)
    assert out["ok"] is False and "未启用" in out["hint"]

    engine.records = _svc(cfg, db)
    out = engine._record_tool(records_query, {"file_id": "4.1.1", "bogus": 1}, "abs")
    assert out["ok"] and out["count"] == 2
    # keyword 也要在白名单里，否则模糊搜索会被静默丢掉
    out = engine._record_tool(records_query, {"keyword": "carB"}, "abs")
    assert out["ok"] and out["count"] == 2


def test_tool_specs_expose_record_tools(cfg):
    specs = {s["function"]["name"]: s for s in build_tool_specs(cfg, "abs")}
    q = specs["query_records"]["function"]["parameters"]["properties"]
    assert "file_id" in q and "condition_id" in q and "keyword" in q
    assert q["condition_id"]["enum"] == cfg.scope_conditions("abs")
    r = specs["read_record"]["function"]["parameters"]
    assert "record_id" in r["properties"] and r["required"] == ["record_id"]
    c = specs["compare_records"]["function"]["parameters"]["properties"]
    assert "vehicles" in c and "metric" in c
    assert set(c["metric"]["enum"]) == set(cfg.metrics)


def test_read_record_card_from_cache(cfg, db):
    """已分析的记录：卡片直接由库里的行拼，不重跑流水线。"""
    svc = _svc(cfg, db)
    rid = db["rows"]["4.1.1_20260801.blf"]["record_id"]
    status, card = svc.read(rid)
    assert status == "cached"
    assert card["source"] == "record" and card["record_id"] == rid
    assert card["vehicle"] == "carA" and card["condition_name"]
    sample = card["samples"][0]
    assert sample["run_index"] == 1
    assert sample["window"]["t_start"] == 1.0 and sample["window"]["t_end"] == 4.0
    distance = next(m for m in sample["metrics"] if m["name"] == "制动距离")
    assert distance["value"] == 40.1 and distance["ok_range"]
    missing = next(m for m in sample["metrics"] if m["status"] == "missing")
    assert missing["missing_reason"] == "signal_absent"


def test_read_payload_is_compact(cfg, db):
    from brake_analyzer.records.query import read_payload

    svc = _svc(cfg, db)
    rid = db["rows"]["4.1.1_20260802.blf"]["record_id"]
    status, card = svc.read(rid)
    payload = read_payload(card, status)
    assert payload["ok"] and payload["record_id"] == rid
    metric = next(m for m in payload["samples"][0]["metrics"] if m["value"] is not None)
    assert metric["value"] == 42.3 and metric["status"] == "abnormal"
    # 回灌给模型的是数值摘要，不是原始序列
    assert "series" not in payload and "chart_option" not in payload


def test_read_record_tool_emits_card_and_caps_reads(cfg, db, tmp_path):
    from web.store import Store

    store_ = Store(upload_dir=str(tmp_path / "up"))
    chat = store_.create_chat()
    engine = ChatEngine.__new__(ChatEngine)
    engine.cfg = cfg
    engine.records = _svc(cfg, db)
    engine.store = store_

    rid = db["rows"]["4.1.1_20260801.blf"]["record_id"]
    budget = {"n": 0}
    msgs, payload = engine._read_record_tool(chat, {"record_id": rid}, budget)
    assert payload["ok"] and payload["record_id"] == rid
    assert len(msgs) == 1 and msgs[0].kind == "analysis_result"
    assert msgs[0].data["source"] == "record"
    assert "record_id" in msgs[0].data and "制动距离" in msgs[0].content

    # 未知记录：返回说明而不是抛异常
    _, bad = engine._read_record_tool(chat, {"record_id": "rec_nope"}, budget)
    assert bad["ok"] is False and "失败" in bad["hint"]

    # 每轮上限：budget 用尽后直接拒绝，不再解析
    budget["n"] = ChatEngine.MAX_READS_PER_TURN
    msgs, over = engine._read_record_tool(chat, {"record_id": rid}, budget)
    assert msgs == [] and over["ok"] is False and "本轮已读取" in over["hint"]


def test_step_label_and_hits(cfg, db):
    res = records_query(_svc(cfg, db), cfg, file_id="4.1.1")
    label, detail, _ = ChatEngine._tool_step_label("query_records", res)
    assert "2 条记录" in label
    hits = ChatEngine._step_hits("query_records", res)
    assert len(hits) == 2
    assert hits[0]["id"].startswith("rec_")
    assert "brake_distance=40.1m" in hits[0]["text"]

    cmp_res = records_compare(_svc(cfg, db), cfg,
                              condition_id="abs_full_brake_dry_asphalt_100kph")
    label, detail, _ = ChatEngine._tool_step_label("compare_records", cmp_res)
    assert "2 条记录" in label and "1 个工况" in detail
    assert len(ChatEngine._step_hits("compare_records", cmp_res)) == 2


class _RecordLlm:
    """脚本化的两轮：先 query_records，再 read_record，最后给文本结论。"""

    def __init__(self, record_id):
        self.available = True
        self.record_id = record_id
        self.calls = 0

    def chat_stream(self, messages, tools=None, **kw):
        from brake_analyzer.llm.client import LlmReply

        self.calls += 1
        if self.calls == 1:
            yield {"type": "finish", "reply": LlmReply(tool_calls=[
                {"id": "c1", "name": "query_records", "arguments": {"file_id": "4.1.3"}}])}
        elif self.calls == 2:
            yield {"type": "finish", "reply": LlmReply(tool_calls=[
                {"id": "c2", "name": "read_record",
                 "arguments": {"record_id": self.record_id}}])}
        else:
            yield {"type": "finish", "reply": LlmReply(content="曲线已展示，实测值见卡片。")}

    def complete_json(self, system, user, **kw):
        return None


def test_engine_loop_displays_record_card(cfg, db, tmp_path):
    """模型自己串两步：查记录 → 读记录，对话里出现 analysis_result 卡片。"""
    from brake_analyzer.llm.kb import KnowledgeBase
    from web.store import Store

    rid = db["rows"]["4.1.3.1_20260801.blf"]["record_id"]
    store_ = Store(upload_dir=str(tmp_path / "up"))
    engine = ChatEngine(cfg, _RecordLlm(rid),
                        KnowledgeBase(os.path.join(ROOT, "knowledge")), store_,
                        records=_svc(cfg, db))
    chat = store_.create_chat()
    state = {"claimed": False, "failed": False}
    list(engine._run_llm_stream(chat, "查一下 4.1.3 并展示", state))

    cards = [m for m in chat.messages if m.kind == "analysis_result"]
    assert len(cards) == 1
    card = cards[0]
    assert card.data["source"] == "record" and card.data["record_id"] == rid
    assert card.data["samples"] and card.data["samples"][0]["window"]
    assert state["claimed"] and not state["failed"]
    # 卡片只带数值与窗口，原始序列不进卡片也不进上下文
    assert "chart_option" not in card.data
    assert not any("series" in (m.meta or {}) for m in chat.messages)
