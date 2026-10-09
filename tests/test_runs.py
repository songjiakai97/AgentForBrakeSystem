"""一轮执行与 HTTP 连接解耦（刷新不丢回复）的行为测试。"""

import os
import threading
import time
import warnings

warnings.filterwarnings("ignore", module="asammdf")

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "tests", "data", "mf4")


class SlowEngine:
    """模拟一轮慢回复：先流式思考，再在 release 上卡住，供测试断开/查询。"""

    def __init__(self, store):
        self.store = store
        self.started = threading.Event()
        self.release = threading.Event()

    def events(self, chat, text):
        yield {"event": "delta",
               "data": {"channel": "meta", "text": "", "engine": "offline"}}
        self.started.set()
        yield {"event": "delta", "data": {"channel": "reasoning", "text": "解析目标中"}}
        self.release.wait(timeout=10)     # 卡点：此刻客户端已断开，后台仍应继续
        msg = self.store.add_message(chat, "assistant", "text", content="结论：完成")
        yield {"event": "message", "data": msg.to_dict()}
        yield {"event": "done", "data": {"chat_id": chat.chat_id}}


@pytest.fixture()
def client():
    from web.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def slow(client):
    """把慢引擎换进 web.main.ENGINE（RUNS.start 取的就是它），测完还原。"""
    import web.main as m

    fake = SlowEngine(m.STORE)
    original = m.ENGINE
    m.ENGINE = fake
    try:
        yield m, fake, client
    finally:
        m.ENGINE = original


def _wait_active(m, client, cid, want_running=True, tries=40):
    for _ in range(tries):
        run = client.get(f"/api/chats/{cid}/run").json()["run"]
        if (run is not None) == want_running:
            return run
        time.sleep(0.05)
    return client.get(f"/api/chats/{cid}/run").json()["run"]


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(m, port: int):
    """在本线程起一个真 uvicorn（同一 app 实例）：TestClient 会等生成器排空，
    无法表达“客户端中途断开”，只能用真服务器验证。"""
    import uvicorn

    cfg = uvicorn.Config(m.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, daemon=True, name="uvicorn-test")
    t.start()
    server.thread = t
    server.test_port = port
    deadline = time.time() + 15
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("uvicorn 未启动")
        time.sleep(0.05)
    return server


def test_send_message_still_returns_messages(client):
    """非流式入口走同一后台轮：返回结构保持兼容，并额外给出 run_id。"""
    chat = client.post("/api/chats").json()
    cid = chat["chat_id"]
    with open(os.path.join(DATA, "abs_dry100_dist_abn.mf4"), "rb") as f:
        client.post(f"/api/chats/{cid}/files",
                    files={"files": ("a.mf4", f, "application/octet-stream")})
    r = client.post(f"/api/chats/{cid}/messages",
                    json={"text": "干沥青 100kph 全力制动，制动距离超了吗"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "analysis_result" in [m["kind"] for m in body["messages"]]
    assert body["run_id"].startswith("run_")
    # 该轮已收尾：不再有进行中的一轮
    assert client.get(f"/api/chats/{cid}/run").json()["run"] is None


def test_subscriber_disconnect_does_not_kill_run(slow):
    """订阅者中途断开（Run.since 生成器被 close）不影响后台线程继续产出。

    这正是刷新页面时发生的事：HTTP 响应生成器被关闭，但它现在只是缓冲区
    的订阅者，引擎在另一个线程跑。
    """
    m, fake, client = slow
    chat = m.STORE.create_chat("sub")
    run, started = m.RUNS.start(m.ENGINE, chat, "随便看看")
    assert started
    assert fake.started.wait(3)

    sub = run.since(-1)
    seen = [next(sub), next(sub)]          # 读到思考流就停手
    assert any("解析目标中" in str(ev["data"]) for ev in seen)
    sub.close()                             # 等价于客户端断开

    assert run.status == "running"          # 后台并未被拖死
    fake.release.set()
    for _ in range(60):
        if run.status != "running":
            break
        time.sleep(0.05)
    assert run.status == "done"
    assert m.RUNS.active_for(chat.chat_id) is None
    # 整轮事件都在缓冲里，重连可回放
    text = " ".join(str(ev["data"]) for ev in run.events)
    assert "解析目标中" in text and "结论：完成" in text


def test_replay_and_incremental_resume(slow):
    """跑完的一轮：整轮回放带单调 seq，after 参数可增量续传不重复。"""
    m, fake, client = slow
    cid = m.STORE.create_chat("replay").chat_id
    run, started = m.RUNS.start(m.ENGINE, m.STORE.get_chat(cid), "随便看看")
    assert started and fake.started.wait(3)
    fake.release.set()
    assert _wait_active(m, client, cid, want_running=False) is None

    with client.stream("GET", f"/api/runs/{run.run_id}/stream?after=-1") as rr:
        assert rr.status_code == 200
        body = "\n".join(list(rr.iter_lines())) + "\n"
    assert "event: delta" in body and "event: done" in body
    assert "解析目标中" in body and "结论：完成" in body
    seqs = [int(line.split(":")[1]) for line in body.splitlines() if line.startswith("id:")]
    assert seqs == list(range(len(seqs)))

    with client.stream("GET", f"/api/runs/{run.run_id}/stream?after={seqs[-2]}") as rr:
        it = rr.iter_lines()
        lines = list(it)
        tail = "\n".join(lines)
    assert "event: done" in tail and "解析目标中" not in tail


def test_refresh_then_recover_over_http(client):
    """真实 HTTP 断连（uvicorn，非 TestClient）：刷新后凭 active run 找回整轮。"""
    import httpx

    import web.main as m

    fake = SlowEngine(m.STORE)
    original = m.ENGINE
    m.ENGINE = fake
    server = None
    try:
        port = _free_port()
        server = _serve(m, port)
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=base, timeout=10) as c:
            cid = c.post("/api/chats").json()["chat_id"]
            # 起流后立刻掐断连接 —— 模拟用户看到思考过程时刷新页面
            with c.stream("POST", f"/api/chats/{cid}/messages/stream",
                          json={"text": "随便看看"}) as resp:
                assert resp.status_code == 200
                first = next(resp.iter_lines())
                assert first.startswith("event: run")
            assert fake.started.wait(5)

            run = c.get(f"/api/chats/{cid}/run").json()["run"]
            assert run and run["status"] == "running", run      # 断开没杀掉本轮
            fake.release.set()
            for _ in range(100):
                if c.get(f"/api/chats/{cid}/run").json()["run"] is None:
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("后台轮未收尾")

            # 刷新后的页面：先读会话（结论已落库），再重连整轮回放（含思考流）
            stored = c.get(f"/api/chats/{cid}").json()["messages"]
            assert any(x["content"] == "结论：完成" for x in stored)
            body = c.get(f"/api/runs/{run['run_id']}/stream?after=-1").text
            assert "解析目标中" in body and "结论：完成" in body and "event: done" in body
    finally:
        m.ENGINE = original
        if server is not None:
            server.should_exit = True
            server.thread.join(timeout=8)


def test_conflict_while_running(slow):
    """一轮未完时同会话再提问/点选返回 409：避免两个 writer 交叉写同一份消息。"""
    m, fake, client = slow
    cid = m.STORE.create_chat("busy").chat_id
    run, started = m.RUNS.start(m.ENGINE, m.STORE.get_chat(cid), "第一轮")
    assert started
    assert fake.started.wait(3)

    dup = client.post(f"/api/chats/{cid}/messages/stream", json={"text": "第二轮"})
    assert dup.status_code == 409, dup.text
    assert client.post(f"/api/chats/{cid}/messages", json={"text": "第二轮"}).status_code == 409
    sel = client.post(f"/api/chats/{cid}/select",
                      json={"target_type": "condition",
                            "target_key": "abs_full_brake_dry_asphalt_100kph"})
    assert sel.status_code == 409

    # 重复 stream 请求本身是幂等的加入（返回同一 run），不新建轮
    assert m.RUNS.start(m.ENGINE, m.STORE.get_chat(cid), "第二轮")[1] is False
    assert m.RUNS.active_for(cid).run_id == run.run_id

    fake.release.set()
    assert _wait_active(m, client, cid, want_running=False) is None


def test_engine_error_is_visible(slow):
    """后台抛错：状态置 error，重连能看到 run_error，会话不会卡在 running。"""
    m, fake, client = slow

    class Boom:
        def events(self, chat, text):
            yield {"event": "delta", "data": {"channel": "meta", "text": "", "engine": "offline"}}
            raise RuntimeError("引擎炸了")

    m.ENGINE = Boom()
    cid = m.STORE.create_chat("boom").chat_id
    r = client.post(f"/api/chats/{cid}/messages", json={"text": "触发异常"})
    assert r.status_code == 500, r.text          # 本轮失败要能明确报错，而不是静默半截
    assert _wait_active(m, client, cid, want_running=False) is None

    run = next(rr for rr in m.RUNS._runs.values() if rr.chat_id == cid)
    assert run.status == "error" and "引擎炸了" in run.error
    with client.stream("GET", f"/api/runs/{run.run_id}/stream?after=-1") as rr:
        body = "".join(line + "\n" for line in rr.iter_lines())
    assert "event: run_error" in body


def test_unknown_run_is_404(client):
    assert client.get("/api/runs/run_nope/stream").status_code == 404
