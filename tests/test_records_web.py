"""记录库 Web 接口（§17.6）：清单、惰性分析、图表、横向对比。"""

import importlib.util
import os
import shutil

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "configs")
MF4_DIR = os.path.join(ROOT, "tests", "data", "mf4")


def _load_script():
    path = os.path.join(ROOT, "scripts", "import_records.py")
    spec = importlib.util.spec_from_file_location("import_records_web", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def client():
    from web.main import app

    return TestClient(app)


@pytest.fixture()
def records_db(tmp_path):
    """两车型各一份真 MF4，导入后替换全局记录服务，不动仓库里的 data/records.db。"""
    import web.main as web

    imp = _load_script()
    root = tmp_path / "data"
    for vehicle in ("carA", "carB"):
        dest = root / vehicle / "4-ABS" / "4.1.1_20260801.mf4"
        dest.parent.mkdir(parents=True)
        shutil.copy(os.path.join(MF4_DIR, "abs_dry100_normal.mf4"), dest)
    db_path = str(tmp_path / "records.db")
    imp.run_import(str(root), db_path, web.CFG)

    old_service, old_db = web.RECORDS, web.RECORDS_DB
    web.RECORDS = web.RecordService(web.CFG, db_path, blf_cfg=web.BLF_CFG)
    web.RECORDS_DB = db_path
    conn = web.RECORDS.connect()
    rows = {r["vehicle"]: r["record_id"]
            for r in web.record_store.query_records(conn)}
    conn.close()
    try:
        yield {"db": db_path, "ids": rows}
    finally:
        web.RECORDS, web.RECORDS_DB = old_service, old_db


def test_records_meta(client, records_db):
    meta = client.get("/api/records/meta").json()
    assert meta["vehicles"] == ["carA", "carB"]
    assert any(c["id"] == "abs_full_brake_dry_asphalt_100kph" for c in meta["conditions"])
    assert "4.1.1" in meta["file_ids"]


def test_records_list_filters(client, records_db):
    all_rows = client.get("/api/records").json()
    assert all_rows["count"] == 2
    assert all_rows["records"][0]["condition_name"]

    one = client.get("/api/records", params={"vehicle": "carB"}).json()
    assert one["count"] == 1 and one["records"][0]["vehicle"] == "carB"
    # 车型只给片段也命中
    assert client.get("/api/records", params={"vehicle": "car"}).json()["count"] == 2

    none = client.get("/api/records", params={"file_id": "9.9"}).json()
    assert none["count"] == 0

    kw = client.get("/api/records", params={"keyword": "20260801"}).json()
    assert kw["count"] == 2, "keyword 应跨文件名匹配"

    # 路面这类中文词只存在于 conditions.yaml：页面必须与对话里的 query_records
    # 同口径（先换成工况 id 再筛），否则同样的话在两个入口一个能搜一个不能
    surf = client.get("/api/records", params={"keyword": "干沥青"}).json()
    assert surf["count"] == 2, "keyword 应经配置侧解析到工况"


def test_record_detail_lazy_then_cached(client, records_db):
    rid = records_db["ids"]["carA"]
    d = client.get(f"/api/records/{rid}").json()
    assert d["analysis_status"] == "analyzed"
    assert d["sample_count"] >= 1
    assert d["windows"] and d["windows"][0]["t_end"] > d["windows"][0]["t_start"]
    card = next(m for m in d["metrics"] if m["metric_key"] == "brake_distance")
    assert card["value"] and card["unit"] == "m" and card["ok_range"]
    assert d["signals"], "记录页要知道该功能看哪些通道"

    again = client.get(f"/api/records/{rid}").json()
    assert again["analysis_status"] == "cached"
    assert again["metrics"] == d["metrics"]


def test_record_detail_refresh_and_404(client, records_db):
    rid = records_db["ids"]["carA"]
    client.get(f"/api/records/{rid}")
    redo = client.get(f"/api/records/{rid}", params={"refresh": True}).json()
    assert redo["analysis_status"] == "analyzed"
    assert client.get("/api/records/rec_nope").status_code == 404


def test_record_timeseries_full_curve_with_highlight(client, records_db):
    rid = records_db["ids"]["carA"]
    client.get(f"/api/records/{rid}")            # 先算一次，窗口才有
    r = client.get(f"/api/records/{rid}/timeseries")
    assert r.status_code == 200
    data = r.json()
    opt = data["chart_option"]
    assert opt["title"]["text"].startswith("carA")
    assert data["windows"]
    # 整段曲线：x 轴不被窗口裁掉；高亮区单独画
    with_mark = [s for s in opt["series"] if s.get("markArea")]
    assert with_mark, "窗口高亮丢了"
    fn = {"speed_vbox", "wheelSpeed_FL", "ABS_Active"}
    names = {s["name"] for s in opt["series"]}
    assert names & fn and "TCS_Active" not in names
    assert len(data["series"]) == len(opt["series"])


def test_compare_endpoint_applies_page_filters(client, records_db):
    """页面筛了车型/关键词，对比表就必须是同一批记录。"""
    for rid in records_db["ids"].values():
        client.get(f"/api/records/{rid}")
    cond = "abs_full_brake_dry_asphalt_100kph"
    full = client.get("/api/records/compare", params={"condition_id": cond}).json()
    assert full["record_count"] == 2
    one = client.get("/api/records/compare",
                     params={"condition_id": cond, "vehicle": "carA"}).json()
    assert one["record_count"] == 1
    assert one["groups"][0]["records"][0]["vehicle"] == "carA"
    kw = client.get("/api/records/compare",
                    params={"condition_id": cond, "keyword": "干沥青"}).json()
    assert kw["record_count"] == 2
    # 关键词指向另一个工况时不能越界返回全库
    other = client.get("/api/records/compare",
                       params={"condition_id": cond, "keyword": "湿沥青"}).json()
    assert other["record_count"] == 0 and other["groups"] == []


def test_records_compare_endpoint(client, records_db):
    for rid in records_db["ids"].values():
        client.get(f"/api/records/{rid}")
    res = client.get("/api/records/compare",
                     params={"condition_id": "abs_full_brake_dry_asphalt_100kph"}).json()
    assert res["ok"] and res["record_count"] == 2
    vehicles = {r["vehicle"] for r in res["groups"][0]["records"]}
    assert vehicles == {"carA", "carB"}


def test_records_page_served(client):
    r = client.get("/records")
    assert r.status_code == 200 and "记录" in r.text
