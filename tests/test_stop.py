"""用户按「停止」后的引擎语义（design.md §6.9）。

要守住的三件事：
1. 停止是协作式的：引擎在检查点自己收尾，不从外部 close 生成器；
2. 方案 (a)：已经流出的半截正文必须落库，否则刷新就丢；
3. 停止不是失败：绝不回退离线路由（否则会凭空多出一段离线结论）。
"""

import os
import threading
import warnings

warnings.filterwarnings("ignore", module="asammdf")

import pytest

from brake_analyzer.configs import load_configs
from brake_analyzer.llm.context import ContextBudget

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "configs")
DATA = os.path.join(ROOT, "tests", "data", "mf4", "abs_dry100_dist_abn.mf4")


class StreamLlm:
    """流式吐正文；吐到第 stop_after 个 delta 时置位外部 stop_event（模拟用户按键）。"""

    available = True
    frags = ["实测制动距离 ", "85.6 m", "，已超过限值"]

    def __init__(self, stop_event=None, stop_after=None):
        self.stop_event = stop_event
        self.stop_after = stop_after
        self.calls = []
        self.reached_finish = False

    def chat_stream(self, messages, tools=None, **kw):
        from brake_analyzer.llm.client import LlmReply

        self.calls.append([dict(m) for m in messages])
        for i, frag in enumerate(self.frags, 1):
            yield {"type": "delta", "channel": "content", "text": frag}
            if self.stop_event is not None and self.stop_after == i:
                self.stop_event.set()
        self.reached_finish = True
        yield {"type": "finish", "reply": LlmReply(content="整段正文（不该被截到这里）")}

    def chat(self, messages, **kw):
        from brake_analyzer.llm.client import LlmReply

        return LlmReply(content="摘要")

    def complete_json(self, system, user, **kw):
        return None


class CountingEvent:
    """第 after 次自检之后才判为已停止：用来卡「分析到第几个文件停」。"""

    def __init__(self, after: int):
        self.after = after
        self.checks = 0

    def is_set(self) -> bool:
        self.checks += 1
        return self.checks > self.after


def _engine(llm, upload_dir=None):
    from brake_analyzer.agent.engine import ChatEngine
    from brake_analyzer.llm.kb import KnowledgeBase
    from web.store import Store

    store = Store(upload_dir=str(upload_dir)) if upload_dir else Store()
    return ChatEngine(load_configs(CONFIG_DIR), llm,
                      KnowledgeBase(os.path.join(ROOT, "knowledge")), store,
                      budget=ContextBudget(max_tokens=8000, compress_tokens=6000))


def _texts(chat):
    return [m for m in chat.messages if m.role == "assistant" and m.kind == "text"]


def test_stop_mid_stream_persists_partial_answer():
    """已流出的半截正文按方案 (a) 落库，并带「已停止」标记。"""
    ev = threading.Event()
    llm = StreamLlm(stop_event=ev, stop_after=2)
    eng = _engine(llm)
    chat = eng.store.create_chat()
    events = list(eng.events(chat, "干沥青 100 全力制动，制动距离超了吗", stop_event=ev))

    msgs = _texts(chat)
    assert len(msgs) == 1, "停止不该再多出一条正文"
    assert msgs[0].content.startswith("实测制动距离 85.6 m")
    assert "用户已停止本轮（回答未完整）" in msgs[0].content
    assert msgs[0].meta["stopped"] is True
    assert msgs[0].meta["streamed"] is True      # 前端据此收起 live 气泡，不重复显示
    # 未完整的那段正文绝不落库（模型本来还要继续写）
    assert "整段正文" not in msgs[0].content
    assert llm.reached_finish is False
    # 事件流照常收尾：前端凭 done 结束，不必特判停止
    assert events[-1]["event"] == "done"


def test_stop_does_not_fall_back_to_offline_routing():
    """停止不是失败：不能因为「本轮没给出结论」就再跑一遍离线路由。"""
    ev = threading.Event()
    llm = StreamLlm(stop_event=ev, stop_after=1)
    eng = _engine(llm)
    chat = eng.store.create_chat()
    kinds = []
    for _ in eng.events(chat, "干沥青 100 全力制动", stop_event=ev):
        pass
    kinds = [m.kind for m in chat.messages]
    assert "target_options" not in kinds
    steps = [it["label"] for m in chat.messages if m.kind == "steps"
             for it in (m.data or {}).get("items", [])]
    assert not any("recommend_targets" in s for s in steps), steps
    assert len(_texts(chat)) == 1


def test_stop_before_first_round_gives_receipt_without_llm_call():
    """一个字都还没出来就停：给一行明确回执，界面不卡在「处理中…」，也不白跑模型。"""
    ev = threading.Event()
    ev.set()
    llm = StreamLlm()
    eng = _engine(llm)
    chat = eng.store.create_chat()
    for _ in eng.events(chat, "随便看看", stop_event=ev):
        pass
    assert llm.calls == []
    msgs = _texts(chat)
    assert len(msgs) == 1 and "停止" in msgs[0].content
    assert [m.kind for m in chat.messages].count("target_options") == 0


def test_stop_before_offline_round_stops_before_routing():
    """离线引擎同样可停：还没开始路由就直接回执，不做任何推荐计算。"""
    ev = threading.Event()
    ev.set()
    eng = _engine(StreamLlm())
    eng.llm.available = False
    chat = eng.store.create_chat()
    for _ in eng.events(chat, "干沥青 100 全力制动", stop_event=ev):
        pass
    msgs = _texts(chat)
    assert len(msgs) == 1 and "停止" in msgs[0].content
    assert not any(k == "target_options" for k in [m.kind for m in chat.messages])


class TwoRoundLlm:
    """第 1 轮带正文的工具调用（正文应已落库），第 2 轮流到一半被停止。"""

    available = True

    def __init__(self, stop_event):
        self.stop_event = stop_event
        self.round_no = 0

    def chat_stream(self, messages, tools=None, **kw):
        from brake_analyzer.llm.client import LlmReply

        self.round_no += 1
        if self.round_no == 1:
            yield {"type": "delta", "channel": "content", "text": "第一轮：先定位工况"}
            yield {"type": "finish", "reply": LlmReply(
                content="第一轮：先定位工况",
                tool_calls=[{"id": "c1", "name": "list_conditions",
                             "arguments": {"function_key": "abs"}}])}
        else:
            yield {"type": "delta", "channel": "content", "text": "第二轮：实测制动距离 "}
            self.stop_event.set()
            yield {"type": "delta", "channel": "content", "text": "85.6 m，超过限值"}
            yield {"type": "finish", "reply": LlmReply(content="第二轮不该写完")}

    def chat(self, messages, **kw):
        from brake_analyzer.llm.client import LlmReply

        return LlmReply(content="摘要")

    def complete_json(self, system, user, **kw):
        return None


def test_stop_in_later_round_does_not_duplicate_earlier_answer():
    """上一轮的正文已经落库了：这一轮被停止时不该把它再写一遍（预览缓冲要清空）。"""
    ev = threading.Event()
    eng = _engine(TwoRoundLlm(ev))
    chat = eng.store.create_chat()
    for _ in eng.events(chat, "干沥青 100 全力制动，制动距离超了吗", stop_event=ev):
        pass

    msgs = _texts(chat)
    # 第一条是工具轮已落库的正文；第二条才是本轮停止时补的半截（不含第一轮那句）
    assert len(msgs) == 2, [m.content for m in msgs]
    assert msgs[0].content == "第一轮：先定位工况"
    assert msgs[0].meta.get("stopped") is None
    assert msgs[1].content.startswith("第二轮：实测制动距离")
    assert msgs[1].content.endswith("—— 用户已停止本轮（回答未完整）")
    assert "第一轮" not in msgs[1].content, "预览缓冲没清，停止时把旧正文又写了一遍"
    assert msgs[1].meta["stopped"] is True


@pytest.mark.skipif(not os.path.exists(DATA), reason="demo 数据未生成")
def test_analyze_stops_between_files_and_keeps_earlier_results(tmp_path):
    """分析阶段（整轮最慢的一段）按文件自检：停手后前序文件结果照旧保留。"""
    from brake_analyzer.agent.engine import ChatEngine
    from brake_analyzer.llm.kb import KnowledgeBase
    from web.store import Store

    cond = "abs_full_brake_dry_asphalt_100kph"
    store = Store(upload_dir=str(tmp_path / "uploads"))
    eng = ChatEngine(load_configs(CONFIG_DIR), StreamLlm(),
                     KnowledgeBase(os.path.join(ROOT, "knowledge")), store)
    chat = store.create_chat()
    with open(DATA, "rb") as f:
        blob = f.read()
    for name in ("f1.mf4", "f2.mf4"):
        store.add_file(chat, name, blob)

    stop = CountingEvent(after=1)     # 第一个文件前放行，第二个文件前停
    added, digests = eng._analyze(chat, cond, None, stop)
    assert stop.checks == 2
    assert len(digests) == 1, "停手后不该再产出第二份结果"
    assert [s["file"] for s in digests[0]["samples"]] == ["f1.mf4"]
    cards = [m for m in added if m.kind == "analysis_result"]
    assert len(cards) == 1
    labels = [it["label"] for m in chat.messages if m.kind == "steps"
              for it in (m.data or {}).get("items", [])]
    assert any(l.startswith("已停止") for l in labels), labels
    assert any("未分析" in l for l in labels), "还剩几个没跑要说得出来"


@pytest.mark.skipif(not os.path.exists(DATA), reason="demo 数据未生成")
@pytest.mark.skipif(not os.path.exists(DATA), reason="demo 数据未生成")
def test_analyze_stopped_before_any_file_says_so(tmp_path):
    """一个文件都没跑就停：话要说成「已停止」，不能报「没有可分析的文件」。"""
    from brake_analyzer.agent.engine import ChatEngine
    from brake_analyzer.llm.kb import KnowledgeBase
    from web.store import Store

    cond = "abs_full_brake_dry_asphalt_100kph"
    store = Store(upload_dir=str(tmp_path / "uploads"))
    eng = ChatEngine(load_configs(CONFIG_DIR), StreamLlm(),
                     KnowledgeBase(os.path.join(ROOT, "knowledge")), store)
    chat = store.create_chat()
    with open(DATA, "rb") as f:
        store.add_file(chat, "f1.mf4", f.read())

    ev = threading.Event()
    ev.set()
    added, digests = eng._analyze(chat, cond, None, ev)
    assert digests == []
    assert not [m for m in added if m.kind == "error"], [m.content for m in added]
    text = [m for m in added if m.kind == "text"]
    assert len(text) == 1 and "停止" in text[0].content and "尚未开始" in text[0].content
    assert text[0].meta["stopped"] is True


def test_analyze_without_stop_event_runs_every_file(tmp_path):
    """不传 stop_event（点选面板/脚本调用）行为不变：所有文件都跑。"""
    from brake_analyzer.agent.engine import ChatEngine
    from brake_analyzer.llm.kb import KnowledgeBase
    from web.store import Store

    store = Store(upload_dir=str(tmp_path / "uploads"))
    eng = ChatEngine(load_configs(CONFIG_DIR), StreamLlm(),
                     KnowledgeBase(os.path.join(ROOT, "knowledge")), store)
    chat = store.create_chat()
    with open(DATA, "rb") as f:
        blob = f.read()
    for name in ("f1.mf4", "f2.mf4"):
        store.add_file(chat, name, blob)
    _, digests = eng._analyze(chat, "abs_full_brake_dry_asphalt_100kph", None)
    assert len(digests) == 2
