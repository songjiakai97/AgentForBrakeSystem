"""harness 快照级回归：持久化 roundtrip、坏参数回喂、软收尾轮、payload 裁剪、健康检查。

这些用例跨 store/engine/client 三层，守护的是「一轮对话的事件协议与消息形态」
——单测覆盖不到的组合行为。
"""

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "configs")

from brake_analyzer.agent.engine import ChatEngine
from brake_analyzer.configs import load_configs
from brake_analyzer.llm.client import LlmClient, LlmReply
from brake_analyzer.llm.context import ContextBudget
from brake_analyzer.llm.kb import KnowledgeBase
from brake_analyzer.llm.prompts import clip_payload
from web.store import Store

CFG = load_configs(CONFIG_DIR)


class NoLlm:
    available = False


def _engine(llm, tmp_path, persist_dir=None):
    return ChatEngine(CFG, llm,
                      KnowledgeBase(os.path.join(ROOT, "knowledge")),
                      Store(upload_dir=str(tmp_path / "uploads"), persist_dir=persist_dir),
                      budget=ContextBudget(max_tokens=8000, compress_tokens=6000))


class ToolThenAnswerLlm:
    """第一轮按脚本调工具（或原样返回 reply），其后给文本结论。记录请求。"""

    def __init__(self, name=None, args=None, reply=None):
        self.available = True
        self.name, self.args = name, args
        self.reply = reply      # 给了就第一轮原样返回（可带 bad_args）
        self.calls = []
        self.rounds = 0

    def chat_stream(self, messages, tools=None, **kw):
        self.calls.append([dict(m) for m in messages])
        self.rounds += 1
        if self.reply is not None and self.rounds == 1:
            yield {"type": "finish", "reply": self.reply}
            return
        if self.rounds == 1 and self.name is not None:
            yield {"type": "finish", "reply": LlmReply(tool_calls=[
                {"id": "c1", "name": self.name, "arguments": self.args}])}
            return
        yield {"type": "finish", "reply": LlmReply(content="收尾结论")}


# ---------------- 持久化 roundtrip ----------------

def test_store_persist_roundtrip(tmp_path):
    pd = str(tmp_path / "sessions")
    st = Store(upload_dir=str(tmp_path / "up"), persist_dir=pd)
    chat = st.create_chat()
    st.add_file(chat, "a.mf4", b"x" * 10, batch_id="b1")
    st.add_message(chat, "user", "text", content="干沥青 100 全力制动怎么样")
    st.add_message(chat, "assistant", "text", content="结论……")
    chat.selected_function = "abs"
    chat.selected_condition = "abs_full_brake_dry_asphalt_100kph"
    st.note_selection(chat)
    st.add_message(chat, "assistant", "target_options",
                   content="候选", data={"hop": "condition", "candidates": []})
    st.mark_panels_consumed(chat, chosen="ABS 干沥青 100kph 全力制动")
    st.add_message(chat, "assistant", "steps",
                   content="旧内容", data={"items": [{"label": "旧"}]}, meta={"sealed": False})
    steps = [m for m in chat.messages if m.kind == "steps"][0]
    st.journal_step(chat, steps)
    st.journal_seal(chat, steps)

    st2 = Store(upload_dir=str(tmp_path / "up2"), persist_dir=pd)
    back = st2.get_chat(chat.chat_id)
    assert back is not None, "重启后应恢复会话"
    assert back.title == "干沥青 100 全力制动怎么样"      # 自动标题规则可重放
    assert back.files and back.files[0].name == "a.mf4"   # 文件登记恢复
    # attachment 消息由 web 层在 add_file 之外另行落库，journal 的 file op 只还原登记
    kinds = [(m.kind, m.role) for m in back.messages]
    assert ("text", "user") in kinds and ("text", "assistant") in kinds
    assert ("steps", "assistant") in kinds
    steps2 = [m for m in back.messages if m.kind == "steps"][0]
    assert steps2.meta.get("sealed") is True and steps2.data["items"][0]["label"] == "旧"
    assert back.selected_condition == "abs_full_brake_dry_asphalt_100kph"
    panels = [m for m in back.messages if m.kind == "target_options"]
    assert panels and panels[0].meta.get("consumed")
    assert st2.sessions, "文件登记恢复（信号缓存为空，用时重载）"


def test_store_survives_corrupt_tail(tmp_path):
    """崩溃截断的尾行：跳过它，之前的消息照常恢复。"""
    pd = str(tmp_path / "sessions")
    st = Store(upload_dir=str(tmp_path / "up"), persist_dir=pd)
    chat = st.create_chat()
    st.add_message(chat, "user", "text", content="第一条")
    with open(os.path.join(pd, chat.chat_id + ".jsonl"), "a", encoding="utf-8") as f:
        f.write('{"op": "msg", "data": {"message_i')      # 半行

    st2 = Store(upload_dir=str(tmp_path / "up2"), persist_dir=pd)
    back = st2.get_chat(chat.chat_id)
    texts = [m.content for m in back.messages if m.kind == "text"]
    assert texts == ["第一条"]


def test_delete_chat_removes_journal(tmp_path):
    pd = str(tmp_path / "sessions")
    st = Store(upload_dir=str(tmp_path / "up"), persist_dir=pd)
    chat = st.create_chat()
    st.add_message(chat, "user", "text", content="x")
    assert os.path.exists(os.path.join(pd, chat.chat_id + ".jsonl"))
    st.delete_chat(chat.chat_id)
    assert not os.path.exists(os.path.join(pd, chat.chat_id + ".jsonl"))


# ---------------- 工具参数解析失败回喂 ----------------

def test_bad_tool_args_fed_back_not_executed(tmp_path):
    """arguments JSON 解析失败：不执行工具，回喂错误让模型自纠。"""
    bad = ToolThenAnswerLlm(
        "search_manual", {},
        reply=LlmReply(tool_calls=[{"id": "c1", "name": "search_manual",
                                    "arguments": {"query": ""}}],
                       bad_args=["c1"]))
    eng = _engine(bad, tmp_path)
    chat = eng.store.create_chat()
    list(eng.events(chat, "查一下滑移率限值"))
    assert bad.rounds == 2
    # 第二轮请求里，c1 的 tool 消息是「解析失败」说明而不是工具结果
    tool_msgs = [m for m in bad.calls[1] if m.get("role") == "tool"]
    assert tool_msgs and "JSON 解析失败" in tool_msgs[-1]["content"]


# ---------------- 软收尾轮 ----------------

def test_wrap_up_round_after_max_rounds(tmp_path, monkeypatch):
    """轮次耗尽：不再兜底离线，追加禁工具的收尾轮拿结论。"""
    import brake_analyzer.agent.engine as engine_mod

    monkeypatch.setattr(engine_mod, "MAX_TOOL_ROUNDS", 4)

    class AlwaysToolLlm:
        available = True

        def __init__(self):
            self.calls = []
            self.tools_seen = []

        def chat_stream(self, messages, tools=None, **kw):
            self.calls.append({"n": len(messages), "tools": tools})
            self.tools_seen.append(tools)
            if tools is None:                     # 收尾轮（无工具规格）
                yield {"type": "finish", "reply": LlmReply(content="收尾结论")}
                return
            yield {"type": "finish", "reply": LlmReply(tool_calls=[
                {"id": f"c{len(self.calls)}", "name": "list_conditions",
                 "arguments": {"function_key": "abs"}}])}

    llm = AlwaysToolLlm()
    eng = _engine(llm, tmp_path)
    chat = eng.store.create_chat()
    events = list(eng.events(chat, "看下工况"))
    assert llm.calls[-1]["tools"] is None, "收尾轮不带工具规格"
    assert all(t is not None for t in llm.tools_seen[:-1])
    assert "收尾结论" in chat.messages[-1].content
    labels = [it["label"] for m in chat.messages if m.kind == "steps"
              for it in (m.data or {}).get("items", [])]
    assert any("工具轮次已用尽" in lb for lb in labels)
    assert not any("recommend_targets" in lb for lb in labels), "耗尽不得兜底离线"
    assert events[-1]["event"] == "done"


# ---------------- payload 裁剪 ----------------

def test_clip_payload_always_valid_json():
    big = {"ok": True, "results": [{"n": f"r{i}", "d": "x" * 80} for i in range(300)]}
    out = clip_payload(big, limit=4000)
    d = json.loads(out)                       # 合法 JSON
    assert len(out) <= 4000 and len(d["results"]) < 300 and "截断" in d.get("note", "")
    tiny = json.loads(clip_payload({"ok": True}, 4000))
    assert tiny == {"ok": True}
    envelope = json.loads(clip_payload({"blob": "y" * 9000}, 500))
    assert envelope["ok"] is False and "重试" in envelope["note"]


# ---------------- LLM 健康检查 ----------------

def test_health_check_unconfigured_is_false_without_network():
    llm = LlmClient()
    if not llm.available:                     # conftest 已把测试环境置为离线
        assert llm.check_reachable() is False  # 未配置不发请求直接 False


def test_health_check_cache(monkeypatch):
    """available 时 ping 结果缓存 30s：force=False 不重复请求。"""
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:1")   # 不可达端口
    llm = LlmClient()
    assert llm.available
    assert llm.check_reachable() is False      # 连不上
    assert llm._reach_until > 0                # 已缓存
    first_ts = llm._reach_until
    assert llm.check_reachable() is False      # 走缓存，不再发请求
    assert llm._reach_until == first_ts


def test_health_check_models_404_is_still_reachable(monkeypatch):
    """只实现 chat/completions 的兼容网关：GET /models 404，但端点活着 → 可达。

    旧实现把所有异常都判成不可达，顶栏会出现「能聊却显示不可达」。
    """
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:1")
    llm = LlmClient()

    class Http404(Exception):
        status_code = 404

    class Models:
        def list(self):
            raise Http404("not found")

    class Client:
        def with_options(self, **kw):
            return self

        @property
        def models(self):
            return Models()

    llm._client = Client()
    assert llm.check_reachable(force=True) is True

    class Http500(Exception):
        status_code = 500

    class Models500(Models):
        def list(self):
            raise Http500("boom")

    class Client500(Client):
        @property
        def models(self):
            return Models500()

    llm._client = Client500()
    assert llm.check_reachable(force=True) is False, "5xx 是服务端故障，不该判可达"


def test_store_persist_roundtrip_file_id_hint(tmp_path):
    """编号识别字段随 JSONL 日志持久化，重启后恢复。"""
    pd = str(tmp_path / "sessions")
    st = Store(upload_dir=str(tmp_path / "up"), persist_dir=pd)
    chat = st.create_chat()
    st.add_file(chat, "4.1.1_20260801.mf4", b"x" * 8, batch_id="b1",
                file_id_no="4.1.1", cond_hint="abs_full_brake_dry_asphalt_100kph")
    st2 = Store(upload_dir=str(tmp_path / "up2"), persist_dir=pd)
    back = st2.get_chat(chat.chat_id)
    entry = back.files[0]
    assert entry.file_id_no == "4.1.1"
    assert entry.cond_hint == "abs_full_brake_dry_asphalt_100kph"
