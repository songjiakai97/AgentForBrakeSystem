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


def test_tool_specs_expose_record_tools(cfg):
    specs = {s["function"]["name"]: s for s in build_tool_specs(cfg, "abs")}
    q = specs["query_records"]["function"]["parameters"]["properties"]
    assert "file_id" in q and "condition_id" in q
    assert q["condition_id"]["enum"] == cfg.scope_conditions("abs")
    c = specs["compare_records"]["function"]["parameters"]["properties"]
    assert "vehicles" in c and "metric" in c
    assert set(c["metric"]["enum"]) == set(cfg.metrics)


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
