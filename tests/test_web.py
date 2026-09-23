"""Web 链路端到端测试（离线模式，覆盖 demo §8 验收）。"""

import os
import warnings

warnings.filterwarnings("ignore", module="asammdf")

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "tests", "data", "mf4")


@pytest.fixture(scope="module")
def client():
    from web.main import app

    return TestClient(app)


def _upload(client, cid, fname):
    with open(os.path.join(DATA, fname), "rb") as f:
        r = client.post(
            f"/api/chats/{cid}/files",
            files={"files": (fname, f, "application/octet-stream")},
        )
    assert r.status_code == 200, r.text
    return r.json()["files"][0]


def test_meta_offline(client):
    m = client.get("/api/meta").json()
    assert m["engine"] in ("offline", "llm")
    assert ".mf4" in m["allowed_upload_ext"]


def test_functions_conditions_kb(client):
    fns = client.get("/api/functions").json()
    assert {f["key"] for f in fns} == {"abs", "tcs"}
    tcs = [f for f in fns if f["key"] == "tcs"][0]
    assert tcs["profile_dims"] == [["4WD", "2WD"], ["DTCS", "TCS"]]
    conds = client.get("/api/conditions", params={"function_key": "abs"}).json()
    assert len(conds) == 3
    kb = client.get("/api/kb").json()
    assert kb["manual"] and kb["cases"]


def test_reject_non_mf4(client):
    chat = client.post("/api/chats").json()
    r = client.post(
        f"/api/chats/{chat['chat_id']}/files",
        files={"files": ("x.blf", b"abc", "application/octet-stream")},
    )
    assert r.status_code == 400


def test_offline_two_hop_and_analysis(client):
    chat = client.post("/api/chats").json()
    cid = chat["chat_id"]
    _upload(client, cid, "abs_dry100_dist_abn.mf4")

    # 第一跳→唯一→第二跳 resolved（ABS 无档位 → 直接分析）
    r = client.post(f"/api/chats/{cid}/messages",
                    json={"text": "干沥青 100kph 全力制动，制动距离超了吗"}).json()
    kinds = [m["kind"] for m in r["messages"]]
    assert "analysis_result" in kinds, r
    msg = [m for m in r["messages"] if m["kind"] == "analysis_result"][0]
    sample = msg["data"]["samples"][0]
    st = {m["key"].rsplit(".", 1)[1]: m["status"] for m in sample["metrics"]}
    assert st["brake_distance"] == "abnormal" and st["yaw_rate_max"] == "ok"
    assert any(v["rule_id"] == "abs_dist_abnormal" for v in sample["verdicts"])

    # 图表时序
    ts = client.get(f"/api/analyses/{sample['sample_id']}/timeseries")
    assert ts.status_code == 200
    body = ts.json()
    assert body["series"] and body["window"]

    # 单样本分析详情
    one = client.get(f"/api/analyses/{sample['sample_id']}").json()
    assert one["sample_id"] == sample["sample_id"]


def test_condition_panel_then_select_profile(client):
    chat = client.post("/api/chats").json()
    cid = chat["chat_id"]
    _upload(client, cid, "tcs_wet60_slip_abn.mf4")

    # 模糊 query → 功能/工况面板（hop 非 resolved）
    r = client.post(f"/api/chats/{cid}/messages",
                    json={"text": "看看加速的数据"}).json()
    opts = [m for m in r["messages"] if m["kind"] == "target_options"]
    assert opts, r
    assert opts[0]["data"]["hop"] in ("function", "condition")

    # 显式选功能 → 工况面板
    r = client.post(f"/api/chats/{cid}/select",
                    json={"target_type": "function", "target_key": "tcs"}).json()
    m = r["messages"][0]
    assert m["data"]["hop"] == "condition" and len(m["data"]["candidates"]) == 3

    # 选工况但不给档位 → 返回 profile 面板
    key = "tcs_full_throttle_wet_basalt_0to60kph"
    r = client.post(f"/api/chats/{cid}/select",
                    json={"target_type": "condition", "target_key": key}).json()
    m = r["messages"][0]
    assert m["data"]["hop"] == "profile"

    # 给两维档位 → 分析，DTCS 打滑超限命中
    r = client.post(f"/api/chats/{cid}/select",
                    json={"target_type": "condition", "target_key": key,
                          "profile": "4WD|DTCS"}).json()
    msgs = [m for m in r["messages"] if m["kind"] == "analysis_result"]
    assert msgs
    sample = msgs[0]["data"]["samples"][0]
    st = {mm["key"].rsplit(".", 1)[1]: mm["status"] for mm in sample["metrics"]}
    assert st["slip_max"] == "abnormal"
    assert msgs[0]["data"]["profile"] == "4WD|DTCS"

    # 非法档位拒绝
    r = client.post(f"/api/chats/{cid}/select",
                    json={"target_type": "condition", "target_key": key, "profile": "RWD"})
    assert r.status_code == 400


def test_profile_switch_changes_verdict(client):
    chat = client.post("/api/chats").json()
    cid = chat["chat_id"]
    _upload(client, cid, "tcs_wet60_acc_abn.mf4")   # acc=1.0
    key = "tcs_full_throttle_wet_basalt_0to60kph"
    r4 = client.post(f"/api/chats/{cid}/select",
                     json={"target_type": "condition", "target_key": key, "profile": "4WD|DTCS"}).json()
    r2 = client.post(f"/api/chats/{cid}/select",
                     json={"target_type": "condition", "target_key": key, "profile": "2WD|DTCS"}).json()
    s4 = [m for m in r4["messages"] if m["kind"] == "analysis_result"][0]["data"]["samples"][0]
    s2 = [m for m in r2["messages"] if m["kind"] == "analysis_result"][0]["data"]["samples"][0]
    a4 = [m for m in s4["metrics"] if m["key"].endswith("acc_avg")][0]
    a2 = [m for m in s2["metrics"] if m["key"].endswith("acc_avg")][0]
    assert a4["status"] == "abnormal" and a4["profile"] == "4WD"
    assert a2["status"] == "ok" and a2["profile"] == "2WD"


def test_delete_chat_cascades(client):
    chat = client.post("/api/chats").json()
    cid = chat["chat_id"]
    f = _upload(client, cid, "abs_wet50_no_yaw.mf4")
    client.post(f"/api/chats/{cid}/messages",
                json={"text": "瓷砖 50 全力制动帮我看看"}).json()
    assert client.get(f"/api/chats/{cid}").status_code == 200
    r = client.delete(f"/api/chats/{cid}").json()
    assert r["deleted"] == cid
    assert client.get(f"/api/chats/{cid}").status_code == 404
    # 上传文件已从磁盘删除（按 file_id 前缀检查）
    updir = "/tmp/brake-agent-uploads"
    assert not any(n.startswith(f["file_id"] + "_") for n in os.listdir(updir))


def test_sse_stream(client):
    chat = client.post("/api/chats").json()
    cid = chat["chat_id"]
    _upload(client, cid, "abs_wet60_decel_abn.mf4")
    with client.stream("POST", f"/api/chats/{cid}/messages/stream",
                       json={"text": "玄武岩 60kph 全力制动"}) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    assert "event: message" in body and "event: done" in body


def test_debug_pages(client):
    assert client.get("/debug").status_code == 200
    assert isinstance(client.get("/api/debug/traces").json(), list)
    assert client.get("/").status_code == 200 or client.get("/index.html").status_code == 200
