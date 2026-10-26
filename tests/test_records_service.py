"""记录库的惰性分析 + 缓存（§17.4）。"""

import importlib.util
import os
import shutil

import pytest

from brake_analyzer.configs import load_configs
from brake_analyzer.records import store
from brake_analyzer.records.service import RecordNotFound, RecordService

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "configs")
MF4_DIR = os.path.join(ROOT, "tests", "data", "mf4")


def _load_script():
    path = os.path.join(ROOT, "scripts", "import_records.py")
    spec = importlib.util.spec_from_file_location("import_records_svc", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cfg():
    return load_configs(CONFIG_DIR)


@pytest.fixture()
def imported(tmp_path, cfg):
    """根目录里放一份真 MF4，走完整导入 → 拿到 record_id。"""
    imp = _load_script()
    root = tmp_path / "data"
    dest = root / "carA" / "4-ABS" / "4.1.1_20260801.mf4"
    dest.parent.mkdir(parents=True)
    shutil.copy(os.path.join(MF4_DIR, "abs_dry100_normal.mf4"), dest)
    db = str(tmp_path / "records.db")
    imp.run_import(str(root), db, cfg)
    conn = store.connect(db)
    row = store.query_records(conn)[0]
    conn.close()
    return {"root": root, "db": db, "record": dict(row), "path": dest}


def _service(cfg, imported):
    return RecordService(cfg, imported["db"])


def test_analyze_then_cached(cfg, imported):
    svc = _service(cfg, imported)
    rid = imported["record"]["record_id"]

    status, result = svc.analyze(rid)
    assert status == "analyzed"
    assert result.condition_id == "abs_full_brake_dry_asphalt_100kph"
    assert result.samples and result.samples[0].metrics

    status, result = svc.analyze(rid)
    assert status == "cached" and result is None

    conn = store.connect(imported["db"])
    assert store.get_record(conn, rid)["analysis_status"] == "ok"
    values = store.metric_rows(conn, rid)
    assert values and any(m["value"] is not None for m in values)
    conn.close()


def test_force_recomputes(cfg, imported):
    svc = _service(cfg, imported)
    rid = imported["record"]["record_id"]
    svc.analyze(rid)
    status, result = svc.analyze(rid, force=True)
    assert status == "analyzed" and result is not None


def test_stale_fingerprint_reanalyzes(cfg, imported):
    svc = _service(cfg, imported)
    rid = imported["record"]["record_id"]
    svc.analyze(rid)
    svc.fingerprint = "fp_changed"
    assert svc.analyze(rid)[0] == "analyzed"


def test_missing_file_marked_gone(cfg, imported):
    svc = _service(cfg, imported)
    rid = imported["record"]["record_id"]
    os.remove(imported["path"])
    assert svc.analyze(rid)[0] == "file_gone"
    conn = store.connect(imported["db"])
    row = store.get_record(conn, rid)
    assert row["analysis_status"] == "file_gone"
    assert "不在服务器上" in row["last_error"]
    conn.close()
    # 后续批量补齐不再反复尝试它
    assert svc.analyze_pending()["file_gone"] == []


def test_unknown_record_raises(cfg, imported):
    with pytest.raises(RecordNotFound):
        _service(cfg, imported).analyze("rec_nope")


def test_record_detail_returns_metrics_and_verdicts(cfg, imported):
    svc = _service(cfg, imported)
    detail = svc.record_detail(imported["record"]["record_id"])
    assert detail["analysis_status"] == "analyzed"
    assert detail["sample_count"] >= 1
    assert {m["metric_key"] for m in detail["metrics"]} >= {"brake_distance"}
    assert detail["record"]["vehicle"] == "carA"


def test_analyze_pending_processes_all(cfg, imported):
    svc = _service(cfg, imported)
    out = svc.analyze_pending()
    assert out["analyzed"] == 1
    assert out["failed"] == [] and out["file_gone"] == []
    assert svc.analyze_pending()["analyzed"] == 0
