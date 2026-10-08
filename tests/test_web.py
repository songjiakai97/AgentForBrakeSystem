"""Web 链路端到端测试（离线模式，覆盖 demo §8 验收）。"""

import math
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


def test_degenerate_window_serializes_as_null(client):
    """退化窗口端点是 NaN：响应边界必须归一为 null，否则 JSONResponse 直接 500。"""
    chat = client.post("/api/chats").json()
    cid = chat["chat_id"]
    _upload(client, cid, "abs_dry100_no_v0.mf4")
    key = "abs_full_brake_dry_asphalt_100kph"
    r = client.post(f"/api/chats/{cid}/select",
                    json={"target_type": "condition", "target_key": key})
    assert r.status_code == 200, r.text
    msgs = [m for m in r.json()["messages"] if m["kind"] == "analysis_result"]
    assert msgs, r.json()
    sample = msgs[0]["data"]["samples"][0]
    assert sample["window"]["t_start"] is None
    assert sample["window"]["t_end"] is None
    assert all(mm["ts_range"] is None or all(v is None or math.isfinite(v) for v in mm["ts_range"])
               for mm in sample["metrics"])
    assert set(sample["status_summary"]) and sample["status_summary"]["missing"] > 0

    sid = sample["sample_id"]
    for url in (f"/api/analyses/{msgs[0]['data']['analysis_id']}",
                f"/api/analyses/{sid}", f"/api/analyses/{sid}/timeseries"):
        rr = client.get(url)
        assert rr.status_code == 200, f"{url}: {rr.text[:120]}"
    # 文本确认没有泄漏 NaN/Infinity 字面量
    assert "NaN" not in client.get(f"/api/analyses/{sid}").text
    assert "Infinity" not in client.get(f"/api/analyses/{sid}").text


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


def test_panel_becomes_receipt_after_click(client):
    """候选面板是一次性的：点选后标记 consumed，重复点击不会复活旧选项。"""
    chat = client.post("/api/chats").json()
    cid = chat["chat_id"]
    _upload(client, cid, "abs_dry100_dist_abn.mf4")

    # 模糊提问 → 工况面板（ABS 三个工况并列）
    r = client.post(f"/api/chats/{cid}/messages", json={"text": "制动表现怎么样"}).json()
    panels = [m for m in r["messages"] if m["kind"] == "target_options"]
    assert panels and panels[0]["data"]["hop"] == "condition"
    pid = panels[0]["message_id"]
    key = "abs_full_brake_dry_asphalt_100kph"

    # 点选面板项 → 面板在存储层被消费，并记录点了什么
    r2 = client.post(f"/api/chats/{cid}/select",
                     json={"target_type": "condition", "target_key": key}).json()
    assert [m["kind"] for m in r2["messages"]] == ["analysis_result"]
    stored = client.get(f"/api/chats/{cid}").json()["messages"]
    done = next(m for m in stored if m["message_id"] == pid)
    assert done["meta"]["consumed"] is True
    assert done["meta"]["chosen"] == "ABS 干沥青 100kph 全力制动"

    # 分析结果本身不是面板：重复点选只会新增结果，不会把旧面板退回可点状态
    client.post(f"/api/chats/{cid}/select",
                json={"target_type": "condition", "target_key": key})
    stored2 = client.get(f"/api/chats/{cid}").json()["messages"]
    again = next(m for m in stored2 if m["message_id"] == pid)
    assert again["meta"]["consumed"] is True


def test_message_run_consumes_panels(client):
    """改用文字提问时，此前的面板同样失效（避免用户回头误点旧选项）。"""
    chat = client.post("/api/chats").json()
    cid = chat["chat_id"]
    _upload(client, cid, "abs_wet50_normal.mf4")
    r = client.post(f"/api/chats/{cid}/messages", json={"text": "制动表现怎么样"})
    first = [m for m in r.json()["messages"] if m["kind"] == "target_options"]
    assert first, r.json()
    pid = first[0]["message_id"]

    client.post(f"/api/chats/{cid}/messages", json={"text": "玄武岩 60kph 全力制动"})
    stored = client.get(f"/api/chats/{cid}").json()["messages"]
    old = next(m for m in stored if m["message_id"] == pid)
    assert old["meta"].get("consumed") is True


def test_debug_pages(client):
    assert client.get("/debug").status_code == 200
    assert isinstance(client.get("/api/debug/traces").json(), list)
    assert client.get("/").status_code == 200 or client.get("/index.html").status_code == 200
