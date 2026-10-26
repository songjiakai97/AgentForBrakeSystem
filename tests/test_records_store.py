"""试验记录库（§17）：导入脚本的归类规则 + SQLite 存取层。"""

import importlib.util
import os
import shutil

import pytest

from brake_analyzer.configs import load_configs
from brake_analyzer.records import store
from brake_analyzer.schemas import (
    AnalysisResult,
    EventWindow,
    MetricResult,
    RuleVerdict,
    RunSample,
    compose_metric_key,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "configs")


def _load_script():
    path = os.path.join(ROOT, "scripts", "import_records.py")
    spec = importlib.util.spec_from_file_location("import_records", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def imp():
    return _load_script()


@pytest.fixture(scope="module")
def cfg():
    return load_configs(CONFIG_DIR)


def _touch(root, rel, data=b"x"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _make_tree(root):
    _touch(root, "carA/4-ABS/4.1.1_20260801_01.blf")
    _touch(root, "carA/4-ABS/4.1.10_20260801_01.blf")
    _touch(root, "carA/4-ABS/4.1.1_notes.txt")
    _touch(root, "carB/5-TCS/4.1.9_20260801.blf")          # 放错目录
    _touch(root, "carB/5-TCS/5.1.5_20260802.blf")
    _touch(root, "carC/5-TCS/random_name.blf")             # 无编号
    _touch(root, "5.1.1_20260801.blf")                     # 缺车型目录


# ---------------------------------------------------------------- 归类

def test_scan_classifies_files(tmp_path, imp, cfg):
    _make_tree(tmp_path)
    items = {i["rel_path"]: i for i in imp.scan(str(tmp_path), cfg)}

    ok = items["carA/4-ABS/4.1.1_20260801_01.blf"]
    assert ok["status"] == "pending"
    assert ok["file_id"] == "4.1.1"
    assert ok["condition_id"] == "abs_full_brake_dry_asphalt_100kph"
    assert ok["vehicle"] == "carA"
    assert ok["recorded_at"] == "2026-08-01"

    # 最长匹配：4.1.10 不能被 4.1.1 吃掉
    assert items["carA/4-ABS/4.1.10_20260801_01.blf"]["file_id"] == "4.1.10"
    assert items["carA/4-ABS/4.1.1_notes.txt"]["reason"] == "unsupported_type"
    assert items["carB/5-TCS/4.1.9_20260801.blf"]["reason"] == "folder_mismatch"
    assert items["carB/5-TCS/4.1.9_20260801.blf"]["expected_function"] == "tcs"
    assert items["carC/5-TCS/random_name.blf"]["reason"] == "no_file_id"
    assert items["5.1.1_20260801.blf"]["reason"] == "no_vehicle_dir"


def test_scan_maps_folder_by_alias(tmp_path, imp, cfg):
    _touch(tmp_path, "carA/4-ABS制动/x.blf")
    item = imp.scan(str(tmp_path), cfg)[0]
    assert imp.folder_function(item["rel_path"].split("/"), cfg) == "abs"


def test_dry_run_does_not_create_db(tmp_path, imp, cfg):
    _make_tree(tmp_path)
    db = tmp_path / "out" / "records.db"
    result = imp.run_import(str(tmp_path), str(db), cfg, dry_run=True)
    assert not db.exists()
    assert result["counts"]["imported"] == 3
    assert result["counts"]["skipped"] == 4


# ---------------------------------------------------------------- 入库

def test_import_inserts_then_dedupes(tmp_path, imp, cfg):
    root = tmp_path / "data"
    _make_tree(root)
    db = str(tmp_path / "records.db")

    first = imp.run_import(str(root), db, cfg)
    assert first["counts"]["imported"] == 3
    conn = store.connect(db)
    rows = store.query_records(conn)
    assert len(rows) == 3
    assert all(r["analysis_status"] == "pending" for r in rows)
    batch = conn.execute("SELECT * FROM import_batch").fetchone()
    assert batch["imported"] == 3
    conn.close()

    second = imp.run_import(str(root), db, cfg)
    assert second["counts"]["imported"] == 0
    assert second["counts"]["duplicated"] == 3


def test_replaced_file_invalidates_analysis(tmp_path, imp, cfg):
    root = tmp_path / "data"
    path = _touch(root, "carA/4-ABS/4.1.1_20260801_01.blf", b"first")
    db = str(tmp_path / "records.db")
    imp.run_import(str(root), db, cfg)

    conn = store.connect(db)
    row = store.query_records(conn)[0]
    store.save_analysis(conn, row["record_id"], _result("abs_full_brake_dry_asphalt_100kph"),
                        store.config_fingerprint(CONFIG_DIR))
    assert store.get_record(conn, row["record_id"])["analysis_status"] == "ok"
    assert store.metric_rows(conn, row["record_id"])

    path.write_bytes(b"second-content")
    os.utime(path, (1, 1))
    imp.run_import(str(root), db, cfg)
    assert store.get_record(conn, row["record_id"])["analysis_status"] == "pending"
    assert store.get_analysis(conn, row["record_id"]) is None
    assert store.metric_rows(conn, row["record_id"]) == []
    conn.close()


def test_same_file_twice_in_one_root_is_deduped_per_path(tmp_path, imp, cfg):
    """两条记录路径不同就是两条记录（同一份数据复测也可能同名）。"""
    root = tmp_path / "data"
    _touch(root, "carA/4-ABS/4.1.1_a.blf", b"same")
    _touch(root, "carA/4-ABS/sub/4.1.1_b.blf", b"same")
    db = str(tmp_path / "records.db")
    imp.run_import(str(root), db, cfg)
    conn = store.connect(db)
    rows = store.query_records(conn)
    assert len(rows) == 2
    assert len({r["file_sha1"] for r in rows}) == 1     # 内容相同也不合并
    conn.close()


# ---------------------------------------------------------------- 分析缓存

def _result(condition_id, metric_key="brake_distance"):
    key = compose_metric_key(condition_id, metric_key)
    metric = MetricResult(
        key=key, name="制动距离", category="brake_performance", value=41.2,
        unit="m", ts_range=(1.0, 4.5), status="ok", ok_range=[0, 45],
    )
    missing = MetricResult(
        key=compose_metric_key(condition_id, "acc_time"), name="加速时间",
        category="traction", value=None, unit="s", ts_range=None,
        status="missing", missing_reason="no_valid_event",
    )
    verdict = RuleVerdict(
        rule_id="r1", metric_key=key, metric_name="制动距离", status="ok",
        severity="info", fault_domain="brake_performance", message="正常",
    )
    sample = RunSample(
        sample_id="s1", file_id="4.1.1", file_name="f.blf", run_index=1,
        window=EventWindow(t_start=1.0, t_end=4.5), metrics=[metric, missing],
        verdicts=[verdict], analysis_id="an_1",
    )
    return AnalysisResult(
        analysis_id="an_1", condition_id=condition_id, condition_name="干沥青",
        function_key="abs", profile=None, file_id="4.1.1", file_name="f.blf",
        samples=[sample],
    )


def test_save_analysis_roundtrip(tmp_path, imp, cfg):
    root = tmp_path / "data"
    _touch(root, "carA/4-ABS/4.1.1_a.blf")
    db = str(tmp_path / "records.db")
    imp.run_import(str(root), db, cfg)
    conn = store.connect(db)
    row = store.query_records(conn)[0]

    fp = store.config_fingerprint(CONFIG_DIR)
    store.save_analysis(conn, row["record_id"], _result(row["condition_id"]), fp)

    cached = store.get_analysis(conn, row["record_id"], fp)
    assert cached["sample_count"] == 1
    assert store.get_analysis(conn, row["record_id"], "other") is None

    metrics = {m["metric_key"]: m for m in store.metric_rows(conn, row["record_id"])}
    assert metrics["brake_distance"]["value"] == 41.2
    assert metrics["brake_distance"]["ts_start"] == 1.0
    assert metrics["acc_time"]["missing_reason"] == "no_valid_event"
    verdicts = store.verdict_rows(conn, row["record_id"])
    assert [v["rule_id"] for v in verdicts] == ["r1"]
    conn.close()


def test_config_fingerprint_tracks_yaml_edits(tmp_path):
    cfg_dir = tmp_path / "configs"
    shutil.copytree(CONFIG_DIR, cfg_dir)
    before = store.config_fingerprint(str(cfg_dir))
    (cfg_dir / "conditions.yaml").write_text("changed: true\n", encoding="utf-8")
    assert store.config_fingerprint(str(cfg_dir)) != before


def test_config_fingerprint_tracks_dbc_edits(tmp_path):
    """DBC 决定怎么解码：库改了而 yaml 没动，缓存也必须失效。"""
    cfg_dir = tmp_path / "configs"
    shutil.copytree(CONFIG_DIR, cfg_dir)
    before = store.config_fingerprint(str(cfg_dir))
    dbc = cfg_dir / "dbc" / "brake_demo.dbc"
    dbc.write_text(dbc.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert store.config_fingerprint(str(cfg_dir)) != before


def test_config_fingerprint_tracks_missing_env_dbc(tmp_path, monkeypatch):
    """现场库未提供时留 absent 占位：库装上后指纹必须变，否则会沿用旧结论。"""
    cfg_dir = tmp_path / "configs"
    shutil.copytree(CONFIG_DIR, cfg_dir)
    yml = cfg_dir / "signals_blf.yaml"
    yml.write_text(yml.read_text(encoding="utf-8").replace(
        "BRAKE_MAIN: configs/dbc/brake_demo.dbc", "BRAKE_MAIN: ${LATE_DBC}"), encoding="utf-8")

    monkeypatch.delenv("LATE_DBC", raising=False)
    before = store.config_fingerprint(str(cfg_dir))

    late = tmp_path / "late.dbc"
    late.write_text('VERSION "t"\n', encoding="utf-8")
    monkeypatch.setenv("LATE_DBC", str(late))
    assert store.config_fingerprint(str(cfg_dir)) != before


def test_iter_pending_includes_stale_fingerprint(tmp_path, imp, cfg):
    root = tmp_path / "data"
    _touch(root, "carA/4-ABS/4.1.1_a.blf")
    db = str(tmp_path / "records.db")
    imp.run_import(str(root), db, cfg)
    conn = store.connect(db)
    row = store.query_records(conn)[0]
    store.save_analysis(conn, row["record_id"], _result(row["condition_id"]), "fp_old")
    assert len(store.iter_pending(conn, "fp_new")) == 1
    assert store.iter_pending(conn, "fp_old") == []
    conn.close()


# ---------------------------------------------------------------- 查询

def test_query_records_filters(tmp_path, imp, cfg):
    root = tmp_path / "data"
    _make_tree(root)
    db = str(tmp_path / "records.db")
    imp.run_import(str(root), db, cfg)
    conn = store.connect(db)

    assert len(store.query_records(conn, vehicle="carA")) == 2
    # 编号按「包含」匹配：4.1.1 同时命中 4.1.1 与 4.1.10
    assert len(store.query_records(conn, file_id="4.1.1")) == 2
    assert len(store.query_records(conn, file_id="4.1.10")) == 1
    assert len(store.query_records(conn, vehicle="car")) == 3
    assert len(store.query_records(conn, keyword="20260802")) == 1
    assert len(store.query_records(conn, keyword="%")) == 0          # 通配符按字面量
    assert len(store.query_records(
        conn, condition_ids=["abs_full_brake_split_80kph"])) == 1
    assert store.query_records(conn, condition_ids=[]) == []
    assert store.distinct(conn, "vehicle") == ["carA", "carB"]
    assert store.status_counts(conn) == {"pending": 3}
    with pytest.raises(ValueError):
        store.distinct(conn, "file_path")
    conn.close()


def test_mark_analysis_error_rejects_unknown_status(tmp_path):
    conn = store.connect(str(tmp_path / "records.db"))
    with pytest.raises(ValueError):
        store.mark_analysis_error(conn, "rec_x", "weird", "boom")
    conn.close()
