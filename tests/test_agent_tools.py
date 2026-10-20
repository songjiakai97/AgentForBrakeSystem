"""对话工具测试：search_manual（知识检索）与 list_files（文件/通道清单）。"""

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "configs")

from brake_analyzer.agent.engine import ChatEngine
from brake_analyzer.configs import load_configs
from brake_analyzer.llm.client import LlmReply
from brake_analyzer.llm.context import ContextBudget
from brake_analyzer.llm.kb import KnowledgeBase
from web.store import Store


class NoLlm:
    available = False        # 离线引擎：两个 helper 不依赖 LLM


class ToolLlm:
    """按脚本依次返回工具调用，最后给文本结论（与 test_targets 同款）。"""

    def __init__(self, rounds):
        self.available = True
        self.rounds = list(rounds)
        self.calls = []
        self.tools_seen = []

    def chat_stream(self, messages, tools=None, **kw):
        self.calls.append([dict(m) for m in messages])
        self.tools_seen.append([t["function"]["name"] for t in (tools or [])])
        if self.rounds:
            name, args = self.rounds.pop(0)
            yield {"type": "finish", "reply": LlmReply(tool_calls=[
                {"id": f"call_{len(self.calls)}", "name": name, "arguments": args}])}
        else:
            yield {"type": "finish", "reply": LlmReply(content="已引用手册给出说明。")}


def _engine(kb_dir=None):
    return ChatEngine(load_configs(CONFIG_DIR), NoLlm(),
                      KnowledgeBase(kb_dir or os.path.join(ROOT, "knowledge")), Store(),
                      budget=ContextBudget(max_tokens=8000, compress_tokens=6000))


def _kb_dir_with_manual(tmp_path):
    mdir = tmp_path / "manual"
    mdir.mkdir()
    (mdir / "m.md").write_text(
        "### m.s1 — 横摆稳定性\n\n判定：|yaw_rate| 峰值 ≤ 5 °/s，"
        "超限常见原因是左右轮缸压力建立不对称。\n", encoding="utf-8")
    return str(tmp_path)


# ---------------- search_manual ----------------

def test_search_manual_hits_and_transient(tmp_path):
    eng = _engine(_kb_dir_with_manual(tmp_path))
    payload = eng._kb_search_tool("横摆稳定性怎么判定")
    assert payload["ok"] is True and payload["transient"] is True
    assert payload["results"] and payload["results"][0]["id"] == "m.s1"
    assert "yaw_rate" in payload["results"][0]["snippet"]


def test_search_manual_miss_reports_honestly(tmp_path):
    eng = _engine(_kb_dir_with_manual(tmp_path))
    payload = eng._kb_search_tool("轮胎花纹深度标准")
    assert payload["ok"] is True and payload["results"] == []
    assert "不要编造" in payload["note"]


def test_search_manual_empty_query_rejected(tmp_path):
    eng = _engine(_kb_dir_with_manual(tmp_path))
    assert eng._kb_search_tool("   ")["ok"] is False


def _tail_manual_kb(tmp_path):
    """节尾埋参数、需要多节命中才能撑爆预算的知识库。"""
    mdir = tmp_path / "manual"
    mdir.mkdir()
    rows = "\n".join(f"| {i} | Opt_pad_{i:03d} | 填充参数{i} | 0.{i % 9} |"
                     for i in range(1, 25))
    (mdir / "big.md").write_text(
        "# ABS 手册\n\n"
        "### big.s1 — ABS 车型参数\n\n"
        "#### 车型基础参数表\n\n"
        "| 序号 | 参数 | 描述 | 值 |\n|---|---|---|---|\n"
        f"{rows}\n\n| 99 | Cp_RA | 后轴CP值 | 26.87 |\n\n"
        "### big.s2 — ABS 开关表\n\n"
        "#### 开关参数表\n\n"
        "| 序号 | 参数 | 描述 | 值 |\n|---|---|---|---|\n"
        f"{rows}\n\n| 99 | FS_ABS_ACH | ABS开关 | 1 |\n",
        encoding="utf-8")
    return str(tmp_path)


def test_step_label_for_data_tools_has_no_hop(tmp_path):
    """search_manual / get_param / list_files 不返回 hop，标签不该写成 hop=None。

    旧实现所有工具共用 `f"{name} → hop={hop}"`，数据类工具没有 hop 这个概念，
    trace 里就出现「search_manual → hop=None」，看着像检索失败（实际命中若干节）。
    """
    eng = _engine(_tail_manual_kb(tmp_path))
    hits = eng.kb.search("后轴CP值", top_k=5)
    label, detail, status = eng._tool_step_label(
        "search_manual", eng._kb_payload(hits))
    assert "hop=" not in label, label
    assert label == f"search_manual → 命中 {len(hits)} 节"
    assert "keyword" in detail or "hybrid" in detail or "semantic" in detail
    assert status == "ok"

    # 路由类仍然带 hop（面板判定依赖它，别顺手改坏）
    assert eng._tool_step_label("resolve_condition", {"hop": "resolved",
                                                     "candidates": []})[0] \
        == "resolve_condition → hop=resolved"
    assert eng._tool_step_label("list_conditions", {"hop": "catalog", "count": 6}) \
        == ("list_conditions → hop=catalog", "清单 6 条（仅本轮可见）", "ok")


def test_search_manual_empty_hit_label_is_warn(tmp_path):
    eng = _engine(_tail_manual_kb(tmp_path))
    label, detail, status = eng._tool_step_label(
        "search_manual", eng._kb_search_tool("手册里没有的词zzz"))
    assert label.endswith("命中 0 节") and status == "warn"


def test_search_manual_snippet_centers_on_hit_block(tmp_path):
    """命中块在节尾时，snippet 必须含节尾内容——旧实现 [:1500] 从节头裁会丢掉它。"""
    eng = _engine(_tail_manual_kb(tmp_path))
    payload = eng._kb_search_tool("后轴CP值")
    assert payload["results"], "应命中含 Cp_RA 的节"
    target = [r for r in payload["results"] if r["id"] == "big.s1"][0]
    assert "Cp_RA" in target["snippet"]
    assert "26.87" in target["snippet"]


def test_search_manual_keeps_all_hits_when_sections_are_long(tmp_path):
    """节很长（表格手册的常态）时，5 条命中都要进 payload，不能因预算核算漏项连锁丢。

    旧实现的 room 只扣 base 的一次性开销：每条 item 的 id/标题/JSON 字段名，以及
    表格里每个换行序列化后的 \\n（2 字符），都没算进去——n 条实际长度恒定略超闸，
    pop 后重算又超，最后只剩 1 条命中进模型。
    """
    from brake_analyzer.llm.prompts import TOOL_PAYLOAD_CLIP_CHARS

    mdir = tmp_path / "manual"
    mdir.mkdir()
    md = ["# 长表格手册"]
    for i in range(6):
        rows = "\n".join(f"| Cp_{i}_{j} | 值 | 单位 |" for j in range(300))
        md.append(f"\n### wide.s{i + 1} — 工况{i}\n\n"
                  f"| 参数 | 值 | 单位 |\n| --- | --- | --- |\n{rows}\n")
    (mdir / "wide.md").write_text("\n".join(md), encoding="utf-8")

    eng = _engine(str(tmp_path))
    hits = eng.kb.search("工况 参数 Cp 值", top_k=5)
    assert len(hits) >= 3, f"前提失效：长表格手册至少应命中 3 节，实得 {len(hits)}"
    payload = eng._kb_payload(hits)
    assert len(payload["results"]) == len(hits), \
        f"命中 {len(hits)} 节只喂入 {len(payload['results'])} 节：预算核算漏项导致连锁丢命中"
    js = json.dumps(payload, ensure_ascii=False)
    assert len(js) <= TOOL_PAYLOAD_CLIP_CHARS, f"payload {len(js)} 超闸"
    assert json.loads(js) == payload
    for r in payload["results"]:
        assert len(r["snippet"]) >= 240, "窗口被压到不可读"


def test_search_manual_payload_survives_json_roundtrip(tmp_path):
    """payload 序列化必须 ≤ TOOL_PAYLOAD_CLIP_CHARS 且是合法 JSON。

    旧实现 5 条 ×1500 字 ≈ 7.8k，被外层硬砍成半截 JSON；现在预算均分 +
    装不下就丢最弱命中，外层 [:CLIP] 砍不到东西。
    """
    from brake_analyzer.llm.prompts import TOOL_PAYLOAD_CLIP_CHARS

    eng = _engine(_tail_manual_kb(tmp_path))
    payload = eng._kb_search_tool("参数表")
    js = json.dumps(payload, ensure_ascii=False)
    assert len(js) <= TOOL_PAYLOAD_CLIP_CHARS, f"payload {len(js)} 超闸，外层会砍出坏 JSON"
    assert json.loads(js[:TOOL_PAYLOAD_CLIP_CHARS]) == payload, "外层截断不应改变内容"
    for r in payload["results"]:
        assert r["snippet"], "预算降级丢命中，不交空串"


def test_search_manual_tool_roundtrip_through_events(tmp_path):
    """工具调用全链路：LLM 发 search_manual → 结果回灌为 tool 应答。"""
    eng = ChatEngine(load_configs(CONFIG_DIR), ToolLlm([("search_manual", {"query": "横摆"})]),
                     KnowledgeBase(_kb_dir_with_manual(tmp_path)), Store(),
                     budget=ContextBudget(max_tokens=8000, compress_tokens=6000))
    chat = eng.store.create_chat()
    for _ in eng.events(chat, "横摆超限一般是什么原因"):
        pass
    # 第二轮 messages 里应包含第一轮工具调用的 tool 应答
    tool_msgs = [m for m in eng.llm.calls[1] if m.get("role") == "tool"]
    assert tool_msgs, "search_manual 的结果必须作为 tool 应答回灌"
    payload = json.loads(tool_msgs[-1]["content"])
    assert payload["ok"] is True and payload["results"][0]["id"] == "m.s1"


def test_search_manual_step_row_shows_each_hit(tmp_path):
    """每条命中要在对话「过程」里逐条可见（steps 消息 data.items[].hits）。

    旧实现只有「命中 N 节」这一行计数：用户看不到到底命中了什么，只能去 debug
    页翻 JSON。snippet 展示的是**实际送进模型的那段**，不是节全文——两者在
    预算裁剪后不同，界面要说清送了多少字。
    """
    eng = ChatEngine(load_configs(CONFIG_DIR), ToolLlm([("search_manual", {"query": "后轴CP值"})]),
                     KnowledgeBase(_tail_manual_kb(tmp_path)), Store(),
                     budget=ContextBudget(max_tokens=8000, compress_tokens=6000))
    chat = eng.store.create_chat()
    for _ in eng.events(chat, "后轴CP值是多少"):
        pass
    steps = [m for m in chat.messages if m.kind == "steps"]
    assert steps, "必须有 steps 消息"
    rows = [r for m in steps for r in (m.data or {}).get("items", [])
            if r.get("label", "").startswith("search_manual")]
    assert rows, "应有一行 search_manual 步骤"
    hits = rows[-1]["hits"]
    assert hits, "hits 必须非空：用户要看到逐条命中"
    # 与喂给模型的 payload 条目一一对应；展示正文按 STEP_HIT_TEXT_MAX 截，
    # chars 仍是实际送入模型的字数（两者不同要说清，别让界面冒充全文）
    tool_msgs = [m for m in eng.llm.calls[1] if m.get("role") == "tool"]
    payload = json.loads(tool_msgs[-1]["content"])
    assert [h["id"] for h in hits] == [r["id"] for r in payload["results"]]
    cap = ChatEngine.STEP_HIT_TEXT_MAX
    for h, r in zip(hits, payload["results"]):
        assert h["chars"] == len(r["snippet"])
        assert h["title"]
        if len(r["snippet"]) <= cap:
            assert h["text"] == r["snippet"]
        else:
            assert h["text"] == r["snippet"][:cap] + "…"
            assert "/debug" in h.get("note", "")
    # 明细只随 steps 消息给前端；steps 不回灌 prompt（is_injected），模型那边不多占 token
    assert all("hits" not in str(m) for m in eng.llm.calls[1])


def test_step_hits_only_for_data_tools_with_results(tmp_path):
    """hits 只给 search_manual / get_param 生成；空结果与路由类不产生该字段。"""
    eng = _engine(_tail_manual_kb(tmp_path))
    assert eng._step_hits("search_manual", eng._kb_search_tool("后轴CP值"))
    assert eng._step_hits("search_manual", eng._kb_search_tool("手册里没有的词zzz")) == []
    assert eng._step_hits("search_manual", {"ok": False, "note": "query 为空"}) == []
    assert eng._step_hits("list_conditions", {"hop": "catalog", "count": 6}) == []
    param = eng._get_param_tool("Cp_RA")
    rows = eng._step_hits("get_param", param)
    if param.get("results"):
        assert rows and rows[0]["title"] == param["results"][0]["param"]
    # 长 snippet 只展示前段，chars 仍是送入模型的全长
    long_hit = {"ok": True, "results": [{"id": "a.b", "title": "T",
                                        "snippet": "字" * (ChatEngine.STEP_HIT_TEXT_MAX + 300)}]}
    h = eng._step_hits("search_manual", long_hit)[0]
    assert h["chars"] == ChatEngine.STEP_HIT_TEXT_MAX + 300
    assert len(h["text"]) == ChatEngine.STEP_HIT_TEXT_MAX + 1   # 截断处补了省略号
    assert "/debug" in h["note"]


# ---------------- list_files ----------------

def _upload(eng, chat, fname):
    path = os.path.join(ROOT, "tests", "data", "mf4", fname)
    with open(path, "rb") as f:
        return eng.store.add_file(chat, fname, f.read())


def test_list_files_empty_chat_prompts_upload():
    eng = _engine()
    payload = eng._list_files_tool(eng.store.create_chat(), None)
    assert payload["ok"] is True and payload["files"] == []
    assert "上传" in payload["note"]


def test_list_files_inventory_without_loading(tmp_path):
    eng = _engine(_kb_dir_with_manual(tmp_path))
    chat = eng.store.create_chat()
    _upload(eng, chat, "abs_dry100_dist_abn.mf4")
    payload = eng._list_files_tool(chat, None)
    assert payload["files"][0]["index"] == 1
    assert payload["files"][0]["latest_batch"] is True
    assert "channels" not in payload["files"][0], "不带 file_index 不应加载信号"
    assert eng.store.session(chat.files[0].file_id).signals is None


def test_list_files_with_index_loads_channels(tmp_path):
    eng = _engine(_kb_dir_with_manual(tmp_path))
    chat = eng.store.create_chat()
    _upload(eng, chat, "abs_dry100_dist_abn.mf4")
    payload = eng._list_files_tool(chat, 1)
    entry = payload["files"][0]
    assert entry["channels"], "加载后必须给出通道名清单"
    assert "speed_vbox" in entry["channels"] or any("speed" in c for c in entry["channels"])
    assert eng.store.session(chat.files[0].file_id).signals is not None


def test_list_files_bad_index(tmp_path):
    eng = _engine(_kb_dir_with_manual(tmp_path))
    chat = eng.store.create_chat()
    _upload(eng, chat, "abs_dry100_dist_abn.mf4")
    assert eng._list_files_tool(chat, 99)["ok"] is False
    assert eng._list_files_tool(chat, "abc")["ok"] is False
