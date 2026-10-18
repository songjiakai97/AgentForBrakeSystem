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


# ---------------------------------------------------------------- 停止的响应速度
#
# 「点了停止没反应、过好久才停止」的真实成因不是标记没送到，而是检查点不够密：
# 工具调用的参数是一个字符一个字符流回来的，不产生 delta，于是「每个 delta 自检」
# 在这一整段里一次都不会触发——必须等整轮模型输出跑完才停手。
# 下面几条把检查点钉在它们该在的位置：流式客户端逐块、工具执行前、非流式子请求前。

class _Fn:
    def __init__(self, name="", arguments=""):
        self.name, self.arguments = name, arguments


class _Tc:
    def __init__(self, index, id="", name="", arguments=""):
        self.index, self.id = index, id
        self.function = _Fn(name, arguments)


class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls
        self.reasoning_content = None
        self.model_extra = {}


class _Choice:
    def __init__(self, delta, finish_reason=None):
        self.delta, self.finish_reason = delta, finish_reason


class _Chunk:
    def __init__(self, delta, finish_reason=None):
        self.choices = [_Choice(delta, finish_reason)]


class FakeStream:
    """可控的流式响应：记录消费了多少块、有没有被 close()。"""

    def __init__(self, chunks, stop_at=None, stop_event=None):
        self.chunks = list(chunks)
        self.stop_at = stop_at
        self.stop_event = stop_event
        self.consumed = 0
        self.closed = False

    def __iter__(self):
        for i, c in enumerate(self.chunks):
            self.consumed = i + 1
            if self.stop_at is not None and i == self.stop_at and self.stop_event:
                self.stop_event.set()          # 等价于用户在流式中途按了停止
            yield c

    def close(self):
        self.closed = True


class FakeCompletions:
    def __init__(self, stream):
        self.stream = stream
        self.kwargs = {}

    def create(self, **kw):
        self.kwargs = kw
        if kw.get("stream"):
            return self.stream
        # 非流式路径（摘要 / 标定建议）也要能走通，才能检查它的 timeout
        return type("Resp", (), {"choices": [type("Ch", (), {
            "message": type("M", (), {"content": "ok", "tool_calls": None})(),
            "finish_reason": "stop"})()]})()


class FakeSdk:
    def __init__(self, stream):
        self.chat = type("C", (), {"completions": FakeCompletions(stream)})()


def _client(stream):
    """绕过「没装 openai SDK 就 available=False」：手工装一个假端点。"""
    from brake_analyzer.llm.client import LlmClient

    c = LlmClient()
    c.available = True
    c._client = FakeSdk(stream)
    return c


def _tool_arg_chunks(n=100, name="run_analysis_on_files"):
    """只有工具参数、没有任何正文 delta 的一轮：客户端旧代码在这里一个检查点都没有。

    拼出的 args 文本是 {"a":1,"a":1,...,"b":2}——重复键解析后仍是 {"a":1,"b":2}，
    好处是块数由 n 精确控制（每块一个字符片段的真实形状，而不是整段一口气吐完）。
    """
    pieces = ["{"] + ['"a":1,'] * (n - 2) + ['"b":2}']

    def chunk(i, tc):
        return _Chunk(_Delta(tool_calls=[tc]),
                      finish_reason="tool_calls" if i == len(pieces) - 1 else None)

    chunks = []
    for i, p in enumerate(pieces):
        tc = _Tc(0, id="call_1" if i == 0 else "", name=name if i == 0 else "", arguments=p)
        chunks.append(chunk(i, tc))
    return chunks


def test_client_checks_stop_between_chunks_and_closes_stream():
    ev = threading.Event()
    stream = FakeStream(_tool_arg_chunks(100), stop_at=5, stop_event=ev)
    client = _client(stream)
    events = list(client.chat_stream([{"role": "user", "content": "x"}], stop_event=ev))

    assert events and events[-1]["type"] == "finish"
    assert events[-1]["reply"].error == "llm_stopped", "停止要走独立通道，不能当正常回复"
    assert not events[-1]["reply"].tool_calls, "半截工具参数不该被当成一次调用交回上层"
    assert stream.consumed < 30, f"逐块自检没生效：吃了 {stream.consumed}/100 块才停"
    assert stream.closed, "提前收尾必须断掉 HTTP 流，否则白占端点并发"


def test_client_without_stop_event_consumes_whole_stream():
    """不按停止时行为不变：整轮工具调用照常拼出来。"""
    stream = FakeStream(_tool_arg_chunks(20))
    events = list(_client(stream).chat_stream([{"role": "user", "content": "x"}]))
    reply = events[-1]["reply"]
    assert reply.error is None
    assert reply.tool_calls[0]["name"] == "run_analysis_on_files"
    assert reply.tool_calls[0]["arguments"] == {"a": 1, "b": 2}
    assert stream.consumed == 20 and stream.closed


def test_client_stream_request_has_bounded_read_timeout():
    """端点挂住不吐字节时，socket 读不到块就无从自检——必须有读超时兜底。"""
    from brake_analyzer.llm.client import (REQUEST_TIMEOUT_SEC, STREAM_READ_TIMEOUT_SEC)

    stream = FakeStream(_tool_arg_chunks(3))
    client = _client(stream)
    next(client.chat_stream([{"role": "user", "content": "x"}]))
    kw = client._client.chat.completions.kwargs
    assert kw["stream"] is True
    assert kw["timeout"] == STREAM_READ_TIMEOUT_SEC
    assert 0 < STREAM_READ_TIMEOUT_SEC <= 120, "挂死的一轮不该把会话锁住十几分钟"
    # 非流式（摘要 / 标定建议）同样要有界：它卡在分析阶段中间，没有任何检查点
    next(client.chat_stream([{"role": "user", "content": "x"}]))
    client.chat([{"role": "user", "content": "x"}])
    assert client._client.chat.completions.kwargs["timeout"] == REQUEST_TIMEOUT_SEC


class StoppedMidStreamLlm:
    """假客户端：流跑到一半就按契约返回 error="llm_stopped"（真实客户端的逐块自检）。"""

    available = True

    def __init__(self, frags=("实测制动距离 ", "85.6 m")):
        self.frags = list(frags)
        self.rounds = 0
        self.calls = []

    def chat_stream(self, messages, tools=None, stop_event=None, **kw):
        from brake_analyzer.llm.client import LlmReply

        self.calls.append([dict(m) for m in messages])
        self.rounds += 1
        for frag in self.frags:
            yield {"type": "delta", "channel": "content", "text": frag}
        # 这一轮只有工具参数：没有 delta，外层一次都检查不到，只能由客户端收尾
        yield {"type": "finish", "reply": LlmReply(error="llm_stopped")}

    def chat(self, messages, **kw):
        from brake_analyzer.llm.client import LlmReply

        return LlmReply(error="llm_stopped")

    def complete_json(self, system, user, **kw):
        return None


def test_llm_stopped_is_not_treated_as_failure():
    """客户端报停止：按停止收尾，绝不回退离线路由（那是「失败」才做的事）。"""
    ev = threading.Event()
    llm = StoppedMidStreamLlm()
    eng = _engine(llm)
    chat = eng.store.create_chat()
    events = list(eng.events(chat, "干沥青 100 全力制动", stop_event=ev))

    msgs = _texts(chat)
    assert len(msgs) == 1 and msgs[0].meta["stopped"] is True
    assert msgs[0].content.startswith("实测制动距离 85.6 m"), "已流出的正文要落库"
    assert not any(k == "target_options" for k in [m.kind for m in chat.messages])
    steps = [it["label"] for m in chat.messages if m.kind == "steps"
             for it in (m.data or {}).get("items", [])]
    assert not any("recommend_targets" in s for s in steps), "停止后不该再跑离线路由"
    assert events[-1]["event"] == "done"


class MultiToolLlm:
    """一轮里连发两个工具调用：停止命中时后面的那个不该再执行。"""

    available = True

    def __init__(self):
        self.calls = 0

    def chat_stream(self, messages, tools=None, stop_event=None, **kw):
        from brake_analyzer.llm.client import LlmReply

        self.calls += 1
        if self.calls == 1:
            yield {"type": "finish", "reply": LlmReply(tool_calls=[
                {"id": "c1", "name": "list_conditions", "arguments": {"function_key": "abs"}},
                {"id": "c2", "name": "recommend_targets", "arguments": {"query": "低附着"}},
            ])}
            return
        yield {"type": "finish", "reply": LlmReply(content="不该走到这里")}

    def chat(self, messages, **kw):
        from brake_analyzer.llm.client import LlmReply

        return LlmReply(content="摘要")

    def complete_json(self, system, user, **kw):
        return None


def test_stop_takes_effect_between_tool_calls(tmp_path):
    """工具执行是「没有 delta 的真空区」：每个工具开始前必须自检，否则停止要等整批跑完。"""
    from brake_analyzer.agent import engine as engine_mod

    ev = threading.Event()
    llm = MultiToolLlm()
    eng = _engine(llm)
    calls = []
    orig_list = engine_mod.list_conditions
    orig_rec = engine_mod.recommend_targets

    def spy_list(*a, **kw):
        calls.append("list_conditions")
        ev.set()                       # 第一个工具跑完 ≈ 用户按了停止
        return orig_list(*a, **kw)

    def spy_rec(*a, **kw):
        calls.append("recommend_targets")
        return orig_rec(*a, **kw)

    engine_mod.list_conditions = spy_list
    engine_mod.recommend_targets = spy_rec
    try:
        chat = eng.store.create_chat()
        events = list(eng.events(chat, "看下有哪些工况", stop_event=ev))
    finally:
        engine_mod.list_conditions = orig_list
        engine_mod.recommend_targets = orig_rec

    assert calls == ["list_conditions"], f"第二个工具不该再执行：{calls}"
    assert events[-1]["event"] == "done"
    stopped = [m for m in chat.messages if m.kind == "text" and m.meta.get("stopped")]
    assert stopped and "recommend_targets 未执行" in stopped[0].content, stopped


def test_non_streaming_subrequests_honour_stop(tmp_path):
    """标定建议/上下文摘要各是一次非流式请求：停止后不该再花钱等它。"""
    ev = threading.Event()
    ev.set()

    class SpyLlm(StoppedMidStreamLlm):
        def __init__(self):
            super().__init__()
            self.json_calls = 0
            self.chat_calls = 0

        def complete_json(self, system, user, **kw):
            self.json_calls += 1
            return None

        def chat(self, messages, **kw):
            self.chat_calls += 1
            from brake_analyzer.llm.client import LlmReply

            return LlmReply(content="摘要")

    llm = SpyLlm()
    eng = _engine(llm)
    # 直接打 _maybe_suggest / 摘要回调：它们拿不到 stop_event 形参，靠线程局部标记
    eng._tls.stop_event = ev
    try:
        res = _fake_result()
        eng._maybe_suggest(res, eng.cfg.condition("abs_full_brake_dry_asphalt_100kph"))
        assert res.degraded is True, "已停止仍要照常去请求建议"
        assert llm.json_calls == 0
        assert eng._summarize_with_llm("早期对话", 200) == "", "摘要应退化为抽取式"
        assert llm.chat_calls == 0
    finally:
        eng._tls.stop_event = None

    # 没按停止时照旧会去请求（否则上面两条断言可能是空过的）
    llm2 = SpyLlm()
    eng2 = _engine(llm2)
    eng2._maybe_suggest(_fake_result(),
                        eng2.cfg.condition("abs_full_brake_dry_asphalt_100kph"))
    assert llm2.json_calls == 1, "用例前提失效：正常路径本来就会调建议"


def _fake_result():
    from brake_analyzer.schemas import (AnalysisResult, EventWindow, MetricResult,
                                        RuleVerdict, RunSample)

    cond = "abs_full_brake_dry_asphalt_100kph"
    m = MetricResult(key=f"{cond}.braking_distance", name="制动距离", category="distance",
                     value=95.4, unit="m", ts_range=(1.0, 5.0), status="abnormal",
                     ok_range=[None, 80.0])
    v = RuleVerdict(rule_id="r1", metric_key=m.key, metric_name=m.name,
                    status="abnormal", severity="high", fault_domain="abs",
                    message="制动距离超出限值", direction="高于上界")
    sample = RunSample(sample_id="s1", file_id="f1", file_name="f1.mf4", run_index=1,
                       window=EventWindow(t_start=1.0, t_end=5.0),
                       metrics=[m], verdicts=[v])
    return AnalysisResult(analysis_id="a1", condition_id=cond,
                          condition_name="干沥青 100km/h 全力制动", function_key="abs",
                          profile=None, file_id="f1", file_name="f1.mf4",
                          samples=[sample])

