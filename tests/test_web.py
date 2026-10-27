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
    # 上下文预算随 .env 生效并被回显（design.md §6.10）
    ctx = m["context"]
    assert ctx["compress_tokens"] < ctx["max_tokens"]
    assert ctx["summarizer"] in ("llm", "extractive")


def test_context_stats(client):
    chat = client.post("/api/chats").json()
    cid = chat["chat_id"]
    client.post(f"/api/chats/{cid}/messages", json={"text": "低附着 ABS 分析一下"})
    r = client.get("/api/context/stats", params={"cid": cid})
    assert r.status_code == 200
    s = r.json()
    assert s["chat_id"] == cid and s["messages"] >= 2
    assert s["estimated_tokens"] > 0
    assert s["compress_tokens"] < s["max_tokens"]
    assert set(s) >= {"over_compress", "over_max"}
    assert client.get("/api/context/stats", params={"cid": "nope"}).status_code == 404
    # 不传 cid 时落到最近一个对话
    assert client.get("/api/context/stats").json()["chat_id"] == cid


def test_functions_conditions_kb(client):
    fns = client.get("/api/functions").json()
    assert {f["key"] for f in fns} == {"abs", "tcs"}
    tcs = [f for f in fns if f["key"] == "tcs"][0]
    assert tcs["profile_dims"] == [["4WD", "2WD"], ["DTCS", "TCS"]]
    conds = client.get("/api/conditions", params={"function_key": "abs"}).json()
    assert len(conds) == 9
    kb = client.get("/api/kb").json()
    # 手册内容随用户上传变化（demo 占位手册已删），只断言结构与案例存在
    assert isinstance(kb["manual"], list) and kb["cases"]


def test_reject_unsupported_ext(client):
    chat = client.post("/api/chats").json()
    r = client.post(
        f"/api/chats/{chat['chat_id']}/files",
        files={"files": ("x.csv", b"abc", "application/octet-stream")},
    )
    assert r.status_code == 400
    assert "仅接受" in r.json()["detail"]


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
    assert m["data"]["hop"] == "condition" and len(m["data"]["candidates"]) == 7

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
    # 过程可见：meta/reasoning delta + steps 消息
    assert 'event: delta' in body and '"channel": "meta"' in body
    assert '"channel": "reasoning"' in body
    assert '"kind": "steps"' in body


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
    kinds = [m["kind"] for m in r2["messages"]]
    assert "analysis_result" in kinds and "steps" in kinds, r2
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


def test_steps_and_chart_groups(client):
    """过程消息落库 + timeseries 按 chart_layout 输出多行共享 x 的分组。"""
    chat = client.post("/api/chats").json()
    cid = chat["chat_id"]
    _upload(client, cid, "abs_dry100_dist_abn.mf4")
    key = "abs_full_brake_dry_asphalt_100kph"
    r = client.post(f"/api/chats/{cid}/select",
                    json={"target_type": "condition", "target_key": key}).json()
    steps = [m for m in r["messages"] if m["kind"] == "steps"]
    assert steps
    rows = steps[0]["data"]["items"]
    assert any("run_analysis_on_files" in x["label"] for x in rows)
    assert all(set(x) == {"label", "detail", "status"} for x in rows)
    # 同一轮的两条 steps 事件其实是同一条消息（原地追加）
    assert client.get(f"/api/chats/{cid}").json()  # 存储可正常读回

    msg = [m for m in r["messages"] if m["kind"] == "analysis_result"][0]
    sid = msg["data"]["samples"][0]["sample_id"]
    body = client.get(f"/api/analyses/{sid}/timeseries").json()
    groups = body["groups"]
    labels = [g["label"] for g in groups]
    assert "车速 / 轮速" in labels
    # 同组信号共用一个 y 轴：车速+四轮速应聚在一行
    speed_g = next(g for g in groups if g["label"] == "车速 / 轮速")
    assert "speed_vbox" in speed_g["signals"] and "wheelSpeed_FL" in speed_g["signals"]
    opt = body["chart_option"]
    n = len(groups)
    assert len(opt["grid"]) == n == len(opt["xAxis"]) == len(opt["yAxis"])
    # 共享 x：所有行的量程锁定一致
    mins = {ax["min"] for ax in opt["xAxis"]}
    maxs = {ax["max"] for ax in opt["xAxis"]}
    assert len(mins) == 1 and len(maxs) == 1
    assert all(s.get("markArea") for s in opt["series"] if s["name"] == "speed_vbox")


def test_chart_option_carries_tooltip_contract(client):
    """游标读数的服务端半边：link 保留 + 每条系列带 groupIndex。

    前端 `web/frontend/chart.js` 用 `series[].groupIndex` 把读数按行分组，竖直游标靠
    `axisPointer.link` 跨行对齐；读数浮层是函数式渲染，进不了 JSON option，所以这里
    只卡住它能工作的前提：link 还在（跨行对齐不能为了读数而牺牲）、`tooltip.confine`
    仍在（旧 tooltip 路径的兜底），groupIndex 与所在行一致（分组的依据）。
    """
    chat = client.post("/api/chats").json()
    cid = chat["chat_id"]
    _upload(client, cid, "abs_dry100_dist_abn.mf4")
    r = client.post(f"/api/chats/{cid}/select", json={
        "target_type": "condition",
        "target_key": "abs_full_brake_dry_asphalt_100kph"}).json()
    sid = [m for m in r["messages"] if m["kind"] == "analysis_result"][0][
        "data"]["samples"][0]["sample_id"]
    opt = client.get(f"/api/analyses/{sid}/timeseries").json()["chart_option"]

    assert opt["tooltip"]["confine"] is True
    assert opt["tooltip"]["axisPointer"]["link"] == [{"xAxisIndex": "all"}]
    assert opt["axisPointer"]["link"] == [{"xAxisIndex": "all"}]
    # 每条系列都要带 groupIndex，且等于它实际挂在第几行上
    for s in opt["series"]:
        assert s.get("groupIndex") == s["yAxisIndex"], s["name"]
    rows = {g["index"]: set(g["signals"]) for g in opt["_groups"]}
    for s in opt["series"]:
        assert s["name"] in rows[s["groupIndex"]]
    # 车速/轮速必须是同一行：这正是用户报"少了几条轮速"的那一行
    speed_row = next(s["groupIndex"] for s in opt["series"] if s["name"] == "speed_vbox")
    assert rows[speed_row] == {"speed_vbox", "wheelSpeed_FL", "wheelSpeed_FR",
                               "wheelSpeed_RL", "wheelSpeed_RR"}


def test_chart_interactions_live_in_one_shared_module(client):
    """游标读数 / 拖拽框选缩放只有一份实现，两个页面都加载它。

    这两处都只能在真浏览器里验（本轮已用 Playwright 探针跑到过：读数列全 10 条、
    拖拽后 dataZoom 从 27% 变到 59%、双击复位），但回归不该依赖有人手动点一遍，
    所以把关键实现点固化成静态断言。
    """
    with open(os.path.join(ROOT, "web", "frontend", "chart.js"), encoding="utf-8") as f:
        ck = f.read()
    with open(os.path.join(ROOT, "web", "frontend", "index.html"), encoding="utf-8") as f:
        index = f.read()
    with open(os.path.join(ROOT, "web", "frontend", "records.html"), encoding="utf-8") as f:
        records = f.read()
    assert client.get("/chart.js").status_code == 200, "静态目录没把 chart.js 带出去"
    for src, name in ((index, "index.html"), (records, "records.html")):
        # 带版本参数：浏览器缓存过一个 404 的话，裸路径会一直白图
        assert src.count('src="./chart.js?v=1"') == 1, f"{name} 没加载共享图表模块"
        # ChartKit 不在时退化成纯 ECharts，不允许静默空白
        assert "function mkChart(el, opt)" in src and "window.ChartKit" in src
    # 主页面不再自己写 ECharts tooltip formatter（那条路会裁，见 design.md §6.6）
    assert "fmtRow" not in index
    # 读数按 groupIndex 分组、两列排版，且整组隐藏时不参与
    assert "if (hiddenGroups[s.groupIndex]) return" in ck
    assert "grid-template-columns:1fr 1fr" in ck
    # ECharts 的 tooltip / axisPointer 必须关掉：y 轴 label 显示鼠标 y 值、link 之后
    # 每行再画一个 x label 和标记点，正是「点不在游标 x 上」的来源
    assert "option.tooltip = {show: false}" in ck
    assert "option.axisPointer = {show: false}" in ck
    # 自绘游标竖线 + 信号名按 ECharts 调色板取色（与曲线同色）
    assert "border-left:1px dashed #64748b" in ck
    assert "function colorOf(idx)" in ck and "palette[idx % palette.length]" in ck
    # 像素 → 数据坐标必须用 gridIndex：多行一列时 xAxisIndex 取不到唯一 cartesian（返 NaN）
    assert "gridIndex: gi" in ck and "xAxisIndex: 0" not in ck
    # 拖拽框选：遮罩 → dataZoom(startValue/endValue)，只动 X；不足 4px 不算选区
    assert "zr.on('mousedown'" in ck and "zr.on('dblclick'" in ck
    assert "startValue: lo, endValue: hi" in ck
    assert "Math.abs(x2 - d.x1) >= 4" in ck
    # inside dataZoom 的平移要关掉，否则一拖是平移不是选区
    assert "z.moveOnMouseMove = false" in ck
    # 程序化显隐（主页面的分组勾选）不会触发 legend 事件，必须显式同步读数
    assert "setLegendSelected" in ck and "setLegendSelected" in index


def test_records_detail_expands_under_the_clicked_row():
    """记录库详情：曲线和卡片必须展开在被点的那一行下面，不再挂页面底部。"""
    with open(os.path.join(ROOT, "web", "frontend", "records.html"), encoding="utf-8") as f:
        src = f.read()
    assert 'id="chart"' not in src, "旧的页面底部单图容器还在"
    assert "record-detail" in src and "tr.detail-row" in src
    # 同一 tbody 里，详情行紧跟被点行的 template v-for 之后
    body = src.index('<template v-for="r in records"')
    detail = src.index("v-if=\"openId===r.record_id && openSource==='list'\"")
    assert 0 < body < detail
    # 对比表点开也在该行下面展开（同一记录两处都有时不重复渲染）
    assert "openSource==='compare'" in src
    # 详情组件内的图表同样走 mkChart（ChartKit 在时带游标 / 缩放，不在时退化为纯图）
    detail_src = src.split("const RecordDetail")[1]
    assert "mkChart(el, opt)" in detail_src
    # 取不到时序 / 没有可画通道时要给出原因，而不是留一块空白
    assert "曲线数据获取失败（HTTP " in detail_src
    assert "没有可画的信号通道" in detail_src


def test_chart_layout_validation():
    from brake_analyzer.configs import AppConfigs, load_configs  # noqa: F401

    cfg = load_configs(os.path.join(ROOT, "configs"))
    assert cfg.chart_layout and cfg.chart_layout[0].label == "车速 / 轮速"
    # 未入组的逻辑信号自动补为独立行
    placed = {s for g in cfg.chart_layout for s in g.signals}
    assert "distance_vbox" in placed


# ---------------------------------------------------------------- BLF 上传链路

BLF_DATA = os.path.join(ROOT, "tests", "data", "blf")


def _blf_ready(client):
    m = client.get("/api/meta").json()
    return m.get("blf_ready", False) and ".blf" in m["allowed_upload_ext"]


def _upload_blf(client, cid, fname):
    with open(os.path.join(BLF_DATA, fname), "rb") as f:
        r = client.post(
            f"/api/chats/{cid}/files",
            files={"files": (fname, f, "application/octet-stream")},
        )
    assert r.status_code == 200, r.text
    return r.json()["files"][0]


def test_samples_list_both_formats(client):
    if not os.path.isdir(BLF_DATA):
        pytest.skip("BLF demo 数据缺失")
    samples = client.get("/api/samples").json()
    assert all({"name", "format"} <= set(s) for s in samples)
    assert any(s["format"] == "mf4" for s in samples)
    if _blf_ready(client):
        assert any(s["format"] == "blf" for s in samples), samples
        one = next(s for s in samples if s["format"] == "blf")
        r = client.get(f"/api/samples/{one['name']}")
        assert r.status_code == 200 and len(r.content) > 1000
    assert client.get("/api/samples/nope.mf4").status_code == 404


def test_sample_download_rejects_traversal():
    """basename 兜住 ../：只允许两个 demo 目录内的同格式文件。"""
    from fastapi import HTTPException

    from web.main import api_sample_download

    for bad in ("../../etc/passwd", "..%2Fetc/passwd", "x.exe", "passwd"):
        with pytest.raises(HTTPException) as ei:
            api_sample_download(bad)
        assert ei.value.status_code == 404


def test_blf_upload_and_analysis(client):
    """上传 .blf → 离线两跳 → 分析结果与图表走同一链路。"""
    if not _blf_ready(client) or not os.path.isdir(BLF_DATA):
        pytest.skip("环境无 python-can/cantools 或缺 BLF demo 数据")
    chat = client.post("/api/chats").json()
    cid = chat["chat_id"]
    _upload_blf(client, cid, "abs_dry100_dist_abn.blf")
    att = [m for m in client.get(f"/api/chats/{cid}").json()["messages"]
           if m["kind"] == "attachment"]
    assert "（blf）" in att[-1]["content"]

    r = client.post(f"/api/chats/{cid}/messages",
                    json={"text": "干沥青 100kph 全力制动，制动距离超了吗"}).json()
    msg = [m for m in r["messages"] if m["kind"] == "analysis_result"]
    assert msg, r
    sample = msg[0]["data"]["samples"][0]
    st = {m["key"].rsplit(".", 1)[1]: m["status"] for m in sample["metrics"]}
    assert st["brake_distance"] == "abnormal" and st["yaw_rate_max"] == "ok"
    assert any(v["rule_id"] == "abs_dist_abnormal" for v in sample["verdicts"])
    assert msg[0]["data"]["file_name"] == "abs_dry100_dist_abn.blf"

    # 图表数据来自 BLF 解码后的信号（通道 0 + DBC 量化）
    body = client.get(f"/api/analyses/{sample['sample_id']}/timeseries").json()
    labels = [g["label"] for g in body["groups"]]
    assert "车速 / 轮速" in labels
    speed = next(g for g in body["groups"] if g["label"] == "车速 / 轮速")
    assert "speed_vbox" in speed["signals"]


def test_blf_missing_channel_reported(client):
    """缺横摆帧的 BLF：指标 missing + data_quality 规则命中，链路不报错。"""
    if not _blf_ready(client) or not os.path.isdir(BLF_DATA):
        pytest.skip("环境无 python-can/cantools 或缺 BLF demo 数据")
    chat = client.post("/api/chats").json()
    cid = chat["chat_id"]
    _upload_blf(client, cid, "abs_wet60_no_yaw.blf")
    r = client.post(f"/api/chats/{cid}/select", json={
        "target_type": "condition",
        "target_key": "abs_full_brake_wet_basalt_60kph"}).json()
    msg = [m for m in r["messages"] if m["kind"] == "analysis_result"][0]
    sample = msg["data"]["samples"][0]
    st = {m["key"].rsplit(".", 1)[1]: m["status"] for m in sample["metrics"]}
    assert st["yaw_rate_max"] == "missing"
    assert any(v["rule_id"] == "abs_yaw_missing" for v in sample["verdicts"])


def test_mixed_formats_in_one_chat(client):
    """同一次上传（多选）混放 MF4 与 BLF：两个文件都出结果，Loader 各按扩展名选。

    两个文件走同一个请求 = 同一批，因此都落在默认分析范围内（§8.2）。
    """
    if not _blf_ready(client) or not os.path.isdir(BLF_DATA):
        pytest.skip("环境无 python-can/cantools 或缺 BLF demo 数据")
    chat = client.post("/api/chats").json()
    cid = chat["chat_id"]
    with open(os.path.join(DATA, "abs_dry100_dist_abn.mf4"), "rb") as f1, \
         open(os.path.join(BLF_DATA, "abs_dry100_dist_abn.blf"), "rb") as f2:
        r = client.post(f"/api/chats/{cid}/files", files=[
            ("files", ("abs_dry100_dist_abn.mf4", f1, "application/octet-stream")),
            ("files", ("abs_dry100_dist_abn.blf", f2, "application/octet-stream")),
        ])
    assert r.status_code == 200, r.text
    assert len(r.json()["files"]) == 2
    r = client.post(f"/api/chats/{cid}/select", json={
        "target_type": "condition",
        "target_key": "abs_full_brake_dry_asphalt_100kph"}).json()
    msg = [m for m in r["messages"] if m["kind"] == "analysis_result"]
    assert len(msg) == 2, r
    names = {m["data"]["file_name"] for m in msg}
    assert names == {"abs_dry100_dist_abn.mf4", "abs_dry100_dist_abn.blf"}
    # 同一剖面两条路径的判定一致
    for m in msg:
        st = {x["key"].rsplit(".", 1)[1]: x["status"] for x in m["data"]["samples"][0]["metrics"]}
        assert st["brake_distance"] == "abnormal"


def test_second_upload_only_analyses_latest_batch(client):
    """分两次上传 → 第三次分析只跑最新那份（用户诉求：新上传的就分析新的）。

    旧行为是把会话内所有文件一起重跑：结论会混进上一趟数据，而且文件越攒越多、
    每轮越来越慢。历史文件仍在会话里，要对比时用 file_indexes 点名。
    """
    if not _blf_ready(client) or not os.path.isdir(BLF_DATA):
        pytest.skip("环境无 python-can/cantools 或缺 BLF demo 数据")
    chat = client.post("/api/chats").json()
    cid = chat["chat_id"]
    _upload(client, cid, "abs_dry100_dist_abn.mf4")        # 第一批
    _upload_blf(client, cid, "abs_dry100_dist_abn.blf")    # 第二批 = 最新
    r = client.post(f"/api/chats/{cid}/select", json={
        "target_type": "condition",
        "target_key": "abs_full_brake_dry_asphalt_100kph"}).json()
    msg = [m for m in r["messages"] if m["kind"] == "analysis_result"]
    assert len(msg) == 1, r
    assert msg[0]["data"]["file_name"] == "abs_dry100_dist_abn.blf"
    # 未重跑要说得出来，否则用户以为两份都算了
    steps = [m for m in r["messages"] if m["kind"] == "steps"]
    detail = " ".join(i.get("detail", "") for m in steps for i in (m.get("data") or {}).get("items", []))
    assert "非本轮范围" in detail, detail


def test_debug_pages(client):
    assert client.get("/debug").status_code == 200
    assert isinstance(client.get("/api/debug/traces").json(), list)
    assert client.get("/").status_code == 200 or client.get("/index.html").status_code == 200


def test_detail_toggle_key_matches_template():
    """结果卡片的明细展开：toggle 收到的 key 必须就是 `opened` 表的索引 key。

    这里埋过一个静默失效——`toggle()` 自己又拼了 `sample_id + ':all'`（残留的
    `_curMid()`），而模板 `v-if="opened[s.sample_id+':'+m.message_id]"` 读的是另一个串，
    两边永远对不上，明细面板（含 missing 成因 chip）从上线起就点不开。Vue 模板表达式
    静态解析不报错、也不影响页面加载，只能在浏览器里点出来（本轮由 Playwright 探针发现）；
    这条测试把不变式变成跑得红的：key 只在模板里算一次，方法只用形参查表，不再二次拼接。
    """
    import re

    path = os.path.join(ROOT, "web", "frontend", "index.html")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    key = re.search(r'v-if="opened\[(.+?)\]"', src)
    assert key, "模板里找不到明细展开的状态表 key"
    key = key.group(1)
    assert re.search(r'toggle\((\w+)\)\{\s*this\.opened\[\1\]=!', src), \
        "toggle 形参应与 opened 的索引 key 同名且不经过二次拼接"
    assert f'@click="toggle({key})"' in src, f"模板传入的 key 与 v-if 读的 key 不一致：{key}"


def test_wait_notice_is_revocable():
    """静默提示要能撤销，且只在真有可停的 run 时挂。

    历史 bug：定时器只负责"置"、不负责"清"，事件恢复后计数冻在旧值；一轮已收尾
    （正文已展示、服务端还在写库/摘要）而连接尚未关闭时，界面一直停在
    「仍在等待模型响应（已静默 Ns）」。这段是 Vue 方法体，加载与渲染都不报错，
    只能在浏览器里等 30 秒才复现，所以把不变式固化成静态断言。
    """
    import re

    path = os.path.join(ROOT, "web", "frontend", "index.html")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    # 只在有 run 可停（currentRunId 来自 run 帧）时提示
    assert "if(idle > 30 && this.currentRunId)" in src
    # 有帧进来就撤提示（feed 内紧跟 lastEventAt 复位）
    assert re.search(r"lastEventAt=Date\.now\(\);\s*\n\s*this\._clearWaitNotice\(\)", src), \
        "收到 SSE 帧必须复位静默提示，否则计数会冻在旧值"

    def block(head: str) -> str:
        m = re.search(re.escape(head) + r"\{(.*?)\n      \}", src, re.S)
        assert m, f"找不到分支 {head}"
        return m.group(1)

    for head in ("if(name==='run_error')", "} else if(name==='done')"):
        assert "_stopSilenceWatch(false)" in block(head), f"{head} 必须停表并撤提示"
    # 订阅到的 run 已结束（刷新/重连）同样不能再数静默
    assert re.search(r"data\.status!=='running'\)\{[^}]*_stopSilenceWatch\(false\)", src)
