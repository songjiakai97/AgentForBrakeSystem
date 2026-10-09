"""上下文预算与压缩（design.md §6.10）的行为测试。"""

import os
import warnings

warnings.filterwarnings("ignore", module="asammdf")

from brake_analyzer.llm.context import (
    SUMMARY_MARK,
    ContextBudget,
    ContextCompressor,
    estimate_message_tokens,
    estimate_text_tokens,
    group_units,
    is_summary,
)
from brake_analyzer.llm.prompts import build_chat_messages


def _msgs(n_units=1, payload_chars=200, system_chars=400):
    """造一份典型请求：1 条 system + n 轮 user/assistant（含 tool 往返）。"""
    out = [{"role": "system", "content": "S" * system_chars}]
    for i in range(n_units):
        out.append({"role": "user", "content": f"第{i}轮提问 " + "内" * 60})
        out.append({"role": "assistant", "content": "",
                    "tool_calls": [{"id": f"c{i}", "type": "function",
                                    "function": {"name": "run_analysis_on_files",
                                                 "arguments": '{"condition_id":"abs_a"}'}}]})
        out.append({"role": "tool", "tool_call_id": f"c{i}",
                    "content": "x" * payload_chars})
        out.append({"role": "assistant", "content": f"第{i}轮结论 " + "结论" * 30})
    out.append({"role": "user", "content": "当前这一轮提问"})
    return out


# ---------------------------------------------------------------- 预算

def test_budget_defaults_and_derivation():
    b = ContextBudget.from_env({})
    assert (b.max_tokens, b.compress_tokens) == (8000, 6000)
    # 只给硬上限 → 压缩阈值取 75%
    b2 = ContextBudget.from_env({"CONTEXT_MAX_TOKENS": "4000"})
    assert b2.compress_tokens == 3000
    assert b2.summary_tokens == 600


def test_budget_validation():
    # 压缩阈值不低于硬上限 → 服务应拒绝启动
    try:
        ContextBudget.from_env({"CONTEXT_MAX_TOKENS": "2000", "CONTEXT_COMPRESS_TOKENS": "2000"})
    except ValueError as e:
        assert "必须小于" in str(e)
    else:
        raise AssertionError("应拒绝 compress >= max")
    for bad in ({"CONTEXT_MAX_TOKENS": "abc"}, {"CONTEXT_MAX_TOKENS": "0"}):
        try:
            ContextBudget.from_env(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"应拒绝非法值 {bad}")


def test_token_estimate_is_heuristic_not_char_count():
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("中" * 10) == 10          # CJK 1 字 1 token
    assert estimate_text_tokens("a" * 40) == 10           # 其余 4 字符 1 token
    est = estimate_message_tokens([{"role": "user", "content": "hello world"}])
    assert est > estimate_text_tokens("hello world")      # 消息级固定开销


# ---------------------------------------------------------------- 压缩

def test_below_threshold_untouched():
    b = ContextBudget(max_tokens=8000, compress_tokens=6000)
    c = ContextCompressor(b)
    raw = _msgs(n_units=1)
    assert estimate_message_tokens(raw) < b.compress_tokens
    out, note = c.fit(raw)
    assert out == raw and note is None


def test_fold_early_units_into_single_summary():
    b = ContextBudget(max_tokens=2000, compress_tokens=1200)
    c = ContextCompressor(b)
    out, note = c.fit(_msgs(n_units=8))
    assert note and "摘要" in note
    est = estimate_message_tokens(out)
    assert est <= b.max_tokens, f"压缩后仍超硬上限：{est}"
    summaries = [m for m in out if is_summary(m)]
    assert len(summaries) == 1
    assert out[0]["role"] == "system" and not is_summary(out[0])   # 系统提示保留
    assert out[-1]["content"] == "当前这一轮提问"                   # 本轮提问不会被压掉
    assert "早期对话要点" in summaries[0]["content"]                # 无 summarizer → 抽取式


def test_llm_summarizer_used_and_cached():
    calls = []

    def summarizer(transcript, budget):
        calls.append((transcript, budget))
        return "已确定 ABS / 低附着工况；异常集中在触发迟滞。"

    b = ContextBudget(max_tokens=2000, compress_tokens=1200)
    c = ContextCompressor(b, summarizer=summarizer)
    raw = _msgs(n_units=6)
    out, _ = c.fit(raw)
    out2, _ = c.fit(raw)          # 同一份待折叠内容 → 第二次不应再调 LLM
    assert len(calls) == 1
    assert out == out2
    assert "触发迟滞" in [m for m in out if is_summary(m)][0]["content"]
    # 摘要请求失败也不能拖垮主链路：退化到抽取式
    c2 = ContextCompressor(b, summarizer=lambda t, n: (_ for _ in ()).throw(RuntimeError("429")))
    out3, _ = c2.fit(_msgs(n_units=6, system_chars=400))
    assert any(is_summary(m) for m in out3)


def test_tool_calls_and_results_stay_paired():
    """折叠必须整轮次单元进行，否则兼容端点因孤立的 tool 消息报 400。"""
    b = ContextBudget(max_tokens=2000, compress_tokens=1200)
    out, _ = ContextCompressor(b).fit(_msgs(n_units=8))
    call_ids = set()
    for m in out:
        for tc in m.get("tool_calls") or []:
            call_ids.add(tc["id"])
        if m.get("role") == "tool":
            assert m["tool_call_id"] in call_ids, "出现没有对应 tool_calls 的 tool 消息"


def test_incremental_recompression_keeps_one_summary():
    b = ContextBudget(max_tokens=2000, compress_tokens=1200)
    c = ContextCompressor(b)
    out, _ = c.fit(_msgs(n_units=6))
    # 下一轮：把上轮结果当历史继续压，旧摘要应被并入新摘要而不是叠第二条
    grew = out + _msgs(n_units=6)[1:] + [{"role": "user", "content": "再看看后轴"}]
    assert estimate_message_tokens(grew) > b.compress_tokens
    out2, note2 = c.fit(grew)
    summaries = [m for m in out2 if is_summary(m)]
    assert len(summaries) == 1
    assert note2 and estimate_message_tokens(out2) <= b.max_tokens
    assert out2[-1]["content"] == "再看看后轴"
    # 旧摘要的内容确实并入了新摘要（否则早期信息会凭空消失）
    assert "早期对话要点" in summaries[0]["content"]


def test_giant_single_payload_trimmed_in_place():
    """最近一轮自己就超预算：没有可折叠内容时只能就地截断，且不破坏 tool 配对。"""
    b = ContextBudget(max_tokens=1500, compress_tokens=1000)
    raw = [
        {"role": "system", "content": "S" * 400},
        {"role": "user", "content": "先看后轴"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {
                "name": "run_analysis_on_files", "arguments": '{"condition_id":"abs_a"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "x" * 20000},
        {"role": "user", "content": "当前这一轮提问"},
    ]
    out, note = ContextCompressor(b).fit(raw)
    assert note and estimate_message_tokens(out) <= b.max_tokens
    tools = [m for m in out if m.get("role") == "tool"]
    assert tools and "超预算已截断" in tools[0]["content"]
    # 结构未被破坏：tool 仍能对上它自己的 tool_calls，系统提示与本轮提问都在
    assert out[0]["role"] == "system" and not is_summary(out[0])
    assert out[-1]["content"] == "当前这一轮提问"
    ids = {tc["id"] for m in out for tc in (m.get("tool_calls") or [])}
    assert all(t["tool_call_id"] in ids for t in tools)


def test_summary_message_carried_into_prompt_only_as_background():
    b = ContextBudget(max_tokens=2000, compress_tokens=1200)
    out, _ = ContextCompressor(b).fit(_msgs(n_units=6))
    s = [m for m in out if is_summary(m)][0]
    assert s["role"] == "system" and s["content"].startswith(SUMMARY_MARK)
    assert units_count(out) >= 2


def units_count(messages):
    body = [m for m in messages if m.get("role") != "system"]
    return len(group_units(body))


# ---------------------------------------------------------------- 引擎接入

class FakeLlm:
    """记录每次请求的 messages；摘要请求走 chat()，主对话走 chat_stream()。

    tool_rounds>0 时前 N 次流式请求返回 recommend_targets 工具调用，用于走
    多跳工具循环（每轮请求前都会重新 fit 上下文）。
    """

    def __init__(self, tool_rounds=0):
        self.available = True
        self.tool_rounds = tool_rounds
        self.calls = []
        self.summary_calls = []

    def chat_stream(self, messages, tools=None, **kw):
        self.calls.append([dict(m) for m in messages])
        from brake_analyzer.llm.client import LlmReply
        if len(self.calls) <= self.tool_rounds:
            yield {"type": "finish", "reply": LlmReply(tool_calls=[
                {"id": f"call_{len(self.calls)}", "name": "recommend_targets",
                 "arguments": {"query": "低附着 ABS", "max_items": 8}}])}
        else:
            yield {"type": "finish",
                   "reply": LlmReply(content="结论：本轮无需再调工具。")}

    def chat(self, messages, **kw):
        self.summary_calls.append(messages)
        from brake_analyzer.llm.client import LlmReply
        return LlmReply(content="摘要：已确定 ABS 低附着工况，异常集中在触发迟滞。")

    def complete_json(self, system, user, **kw):
        return None


def _engine(tool_rounds=0):
    import os

    from brake_analyzer.agent.engine import ChatEngine
    from brake_analyzer.configs import load_configs
    from brake_analyzer.llm.kb import KnowledgeBase
    from web.store import Store

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    llm = FakeLlm(tool_rounds=tool_rounds)
    eng = ChatEngine(load_configs(os.path.join(root, "configs")), llm,
                     KnowledgeBase(os.path.join(root, "knowledge")), Store(),
                     budget=ContextBudget(max_tokens=1200, compress_tokens=700))
    return eng, llm


def test_engine_compresses_before_sending_and_logs_step():
    eng, llm = _engine()
    chat = eng.store.create_chat()
    # 攒一段长历史：全部是会被回灌的 text 消息
    for i in range(10):
        eng.store.add_message(chat, "user", "text", content=f"第{i}轮提问 " + "描述" * 40)
        eng.store.add_message(chat, "assistant", "text", content=f"第{i}轮回答 " + "结论" * 40)
    unbounded = estimate_message_tokens(build_chat_messages(
        eng.cfg, [{"role": m.role, "kind": m.kind, "content": m.content} for m in chat.messages],
        "那后轴呢", [], {}))
    assert unbounded > eng.budget.max_tokens   # 不压就会超

    for _ in eng.events(chat, "那后轴呢"):
        pass

    sent = llm.calls[0]
    est = estimate_message_tokens(sent)
    assert est <= eng.budget.max_tokens, f"引擎把超预算的上下文发出去了：{est}"
    summaries = [m for m in sent if is_summary(m)]
    assert len(summaries) == 1 and llm.summary_calls, "应通过 LLM 摘要而非只用抽取式"
    assert sent[-1]["content"] == "那后轴呢"

    steps = [m for m in chat.messages if m.kind == "steps"]
    labels = [it["label"] for m in steps for it in (m.data or {}).get("items", [])]
    assert "上下文压缩" in labels
    note = [it["detail"] for m in steps for it in (m.data or {}).get("items", [])
            if it["label"] == "上下文压缩"][0]
    assert "tok" in note


def test_engine_no_compression_when_within_budget():
    eng, llm = _engine()
    chat = eng.store.create_chat()
    eng.store.add_message(chat, "user", "text", content="短提问")
    for _ in eng.events(chat, "再问一句"):
        pass
    steps = [m for m in chat.messages if m.kind == "steps"]
    labels = [it["label"] for m in steps for it in (m.data or {}).get("items", [])]
    assert "上下文压缩" not in labels


def test_engine_stays_within_budget_across_tool_hops():
    """多跳工具循环：每次请求都必须有界，且 tool_calls 与 tool 结果始终配对。"""
    eng, llm = _engine(tool_rounds=4)
    chat = eng.store.create_chat()
    for i in range(8):
        eng.store.add_message(chat, "user", "text", content=f"第{i}轮 " + "描述" * 40)
        eng.store.add_message(chat, "assistant", "text", content=f"回答{i} " + "结论" * 40)

    events = list(eng.events(chat, "等下再看后驱"))
    assert events[-1]["event"] == "done"
    assert len(llm.calls) >= 3, "应真的走完多跳"
    for k, sent in enumerate(llm.calls, start=1):
        est = estimate_message_tokens(sent)
        assert est <= eng.budget.max_tokens, f"第{k}次请求超限：{est}"
        ids = {tc["id"] for m in sent for tc in (m.get("tool_calls") or [])}
        for m in sent:
            if m.get("role") == "tool":
                assert m["tool_call_id"] in ids, f"第{k}次请求有孤立 tool 消息"
        # 每个带 tool_calls 的 assistant 后面必须紧跟其全部 tool 应答，否则端点报错
        for idx, m in enumerate(sent):
            for tc in (m.get("tool_calls") or []):
                following = [x.get("tool_call_id") for x in sent[idx + 1:]]
                assert tc["id"] in following, f"第{k}次请求有未应答的 tool_call"
    labels = [it["label"] for m in chat.messages if m.kind == "steps"
              for it in (m.data or {}).get("items", [])]
    assert "上下文压缩" in labels


def test_engine_budget_from_env_defaults_without_key():
    """无 Key 环境也要能读出预算：离线不调 LLM，但预算仍然生效于硬上限保护。"""
    b = ContextBudget.from_env({"CONTEXT_MAX_TOKENS": "3000",
                               "CONTEXT_COMPRESS_TOKENS": ""})
    assert b.max_tokens == 3000 and b.compress_tokens == 2250


def test_tiny_budget_keeps_current_question_and_states_truth():
    """预算小于固定开销这类极端配置：本轮提问不能被丢掉，note 要如实说明。"""
    b = ContextBudget(max_tokens=300, compress_tokens=200)
    out, note = ContextCompressor(b).fit(_msgs(n_units=4))
    assert out[-1]["content"] == "当前这一轮提问"
    assert note and "末轮仍超上限" not in note   # 这里压得下，不该报残留

    # 只剩系统提示就超预算 → 不假装压下了，如实报"预算小于固定开销"
    _, note2 = ContextCompressor(ContextBudget(400, 200)).fit(
        [{"role": "system", "content": "功能目录" * 400}])
    assert note2 and "不裁剪" in note2


def test_web_startup_rejects_invalid_budget():
    """预算配错要拒绝启动（与 §6.1 配置校验同一策略），用子进程隔离环境变量。"""
    import subprocess
    import sys

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, CONTEXT_MAX_TOKENS="2000", CONTEXT_COMPRESS_TOKENS="2000")
    p = subprocess.run([sys.executable, "-c", "import web.main"], cwd=root, env=env,
                       capture_output=True, text=True)
    assert p.returncode != 0
    assert "必须小于" in p.stderr and "上下文预算配置无效" in p.stderr
