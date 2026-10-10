"""Web API（design.md §7.2）。

启动：python -m uvicorn web.main:app --port 8000
"""

import json
import os
import warnings

warnings.filterwarnings("ignore", module="asammdf")

from fastapi import FastAPI, HTTPException, UploadFile, File, Query  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from typing import List, Optional  # noqa: E402

from brake_analyzer.agent.engine import ChatEngine  # noqa: E402
from brake_analyzer.charts.html import build_chart_option, timeseries_payload  # noqa: E402
from brake_analyzer.configs import (  # noqa: E402
    ConfigError, load_blf_config, load_configs,
)
from brake_analyzer.events.segment import is_valid  # noqa: E402
from brake_analyzer.llm.client import LlmClient, TraceStore  # noqa: E402
from brake_analyzer.llm.context import ContextBudget  # noqa: E402
from brake_analyzer.llm.kb import KnowledgeBase  # noqa: E402
from brake_analyzer.llm.prompts import build_chat_messages  # noqa: E402
from brake_analyzer.schemas import json_safe  # noqa: E402
from web.runs import RunManager  # noqa: E402
from web.store import Store  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "configs")
KB_DIR = os.path.join(ROOT, "knowledge")
FRONTEND = os.path.join(ROOT, "web", "frontend")
# BLF 需要 DBC 解码，给前端做能力提示（缺库时不阻止启动，只在分析该文件时报错）
try:
    import can as _can           # noqa: F401
    import cantools as _cantools  # noqa: F401
    BLF_READY = True
except ImportError:
    BLF_READY = False

# 上传白名单随依赖能力变化：没装 python-can/cantools 时 .blf 不收，避免收了不能分析
ALLOWED_EXT = {".mf4"} | ({".blf"} if BLF_READY else set())


class SafeJSONResponse(JSONResponse):
    """边界防护：内核合法的 NaN/Inf 归一为 null，避免 allow_nan=False 抛错。"""

    def render(self, content) -> bytes:
        return super().render(json_safe(content))


app = FastAPI(
    title="Brake Agent Demo",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    default_response_class=SafeJSONResponse,
)

try:
    CFG = load_configs(CONFIG_DIR)
except ConfigError as e:
    raise RuntimeError(f"配置加载失败，服务拒绝启动：{e}")

# BLF 侧配置同样启动期校验（结构非法直接拒绝启动；缺库只影响 .blf 文件本身）
try:
    BLF_CFG = load_blf_config(CONFIG_DIR, logical_names=set(CFG.signal_maps))
except ConfigError as e:
    raise RuntimeError(f"BLF 配置加载失败，服务拒绝启动：{e}")

TRACES = TraceStore(cap=int(os.getenv("TRACE_STORE_MAX", "500")))
STORE = Store()
KB = KnowledgeBase(KB_DIR)
LLM = LlmClient(traces=TRACES)
try:
    BUDGET = ContextBudget.from_env()
except ValueError as e:
    raise RuntimeError(f"上下文预算配置无效，服务拒绝启动：{e}")
ENGINE = ChatEngine(CFG, LLM, KB, STORE, budget=BUDGET, blf_cfg=BLF_CFG)
# 一轮 = 一个后台任务：与 HTTP 连接解耦，刷新/断网不再丢本轮回复
RUNS = RunManager()


@app.on_event("startup")
def _ensure_demo_data():
    """demo 样例可再生：缺失时自动生成（tests/data/* 不入库）。"""
    import sys

    sys.path.insert(0, ROOT)
    jobs = [
        (os.path.join(ROOT, "tests", "data", "mf4"), ".mf4",
         "scripts.generate_demo_data"),
        # BLF 侧依赖 can/cantools；缺库或生成失败都只是没有样例，不影响服务
        (os.path.join(ROOT, "tests", "data", "blf"), ".blf",
         "scripts.generate_demo_blf"),
    ]
    for data_dir, ext, module in jobs:
        if os.path.isdir(data_dir) and any(f.endswith(ext) for f in os.listdir(data_dir)):
            continue
        try:
            import importlib

            importlib.import_module(module).generate_all(data_dir)
        except Exception as e:  # 数据缺失不阻止服务启动
            print(f"[warn] demo 样例生成失败({ext}): {e}")


def _chat_or_404(cid: str):
    chat = STORE.get_chat(cid)
    if chat is None:
        raise HTTPException(404, f"对话不存在: {cid}")
    return chat


# ---------------------------------------------------------------- demo 样例数据
SAMPLE_DIRS = {
    "mf4": os.path.join(ROOT, "tests", "data", "mf4"),
    "blf": os.path.join(ROOT, "tests", "data", "blf"),
}
MAX_SAMPLE_BYTES = 20 * 1024 * 1024


@app.get("/api/samples")
def api_samples():
    """合成样例清单：MF4 在前、BLF 在后（BLF 仅在依赖可用时列出）。"""
    out = []
    for fmt in ("mf4", "blf"):
        if fmt == "blf" and not BLF_READY:
            continue
        d = SAMPLE_DIRS[fmt]
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if not f.endswith("." + fmt):
                continue
            if os.path.getsize(os.path.join(d, f)) >= MAX_SAMPLE_BYTES:
                continue
            out.append({"name": f, "format": fmt})
    return out


@app.get("/api/samples/{name}")
def api_sample_download(name: str):
    base = os.path.basename(name)
    fmt = os.path.splitext(base)[1].lstrip(".").lower()
    path = os.path.join(SAMPLE_DIRS.get(fmt, ""), base) if fmt in SAMPLE_DIRS else ""
    if not path or not os.path.isfile(path):
        raise HTTPException(404, "样例不存在")
    from fastapi.responses import FileResponse

    return FileResponse(path, filename=base, media_type="application/octet-stream")


# ---------------------------------------------------------------- meta
@app.get("/api/meta")
def api_meta():
    return {
        "llm_available": LLM.available,
        "model": LLM.model if LLM.available else None,
        "engine": "llm" if LLM.available else "offline",
        "allowed_upload_ext": sorted(ALLOWED_EXT),
        "blf_ready": BLF_READY,
        "context": {
            **BUDGET.to_dict(),
            "summarizer": "llm" if LLM.available else "extractive",
        },
    }


@app.get("/api/context/stats")
def api_context_stats(cid: str = Query("")):
    """当前会话上下文的估算 token 与两条阈值的关系（调试用）。"""
    chat = STORE.get_chat(cid) if cid else STORE.latest_chat()
    if chat is None:
        raise HTTPException(status_code=404, detail="没有可用对话")
    msgs = build_chat_messages(
        CFG,
        [{"role": m.role, "kind": m.kind, "content": m.content} for m in chat.messages],
        "", [f.name for f in chat.files],
        {"condition": chat.selected_condition, "profile": chat.selected_profile},
    )
    return {"chat_id": chat.chat_id, "messages": len(msgs), **ENGINE.compressor.stats(msgs)}


@app.get("/api/functions")
def api_functions():
    return [
        {
            "key": fn.key, "name": fn.name, "aliases": fn.aliases,
            "profiles": fn.profiles, "profile_dims": fn.profile_dims,
            "description": fn.description,
            "condition_count": len(CFG.conditions_of(fn.key)),
        }
        for fn in CFG.functions.values()
    ]


@app.get("/api/conditions")
def api_conditions(function_key: Optional[str] = Query(None)):
    items = []
    src = CFG.conditions_of(function_key) if function_key else list(CFG.conditions.values())
    for c in src:
        items.append({
            "id": c.id, "function": c.function, "name": c.name,
            "maneuver": c.maneuver, "surface_code": c.surface_code,
            "enabled_metrics": c.enabled_metrics, "params": c.params,
        })
    return items


@app.get("/api/kb")
def api_kb():
    return KB.browse()


# ---------------------------------------------------------------- chats
@app.post("/api/chats")
def api_create_chat():
    return STORE.create_chat().to_dict()


@app.get("/api/chats")
def api_list_chats():
    return STORE.list_chats()


@app.get("/api/chats/{cid}")
def api_get_chat(cid: str):
    return _chat_or_404(cid).to_dict()


@app.delete("/api/chats/{cid}")
def api_delete_chat(cid: str):
    _chat_or_404(cid)
    TRACES.remove_chat(cid)
    STORE.delete_chat(cid)
    latest = STORE.latest_chat()
    return {"deleted": cid, "next_chat": latest.chat_id if latest else None}


@app.post("/api/chats/{cid}/files")
async def api_upload(cid: str, files: List[UploadFile] = File(...)):
    chat = _chat_or_404(cid)
    out = []
    for uf in files:
        name = uf.filename or "unnamed.mf4"
        ext = os.path.splitext(name)[1].lower()
        if ext not in ALLOWED_EXT:
            raise HTTPException(
                400, f"仅接受 {'/'.join(sorted(ALLOWED_EXT))} 上传，收到: {name}"
                     + ("" if BLF_READY else "（当前环境缺 python-can/cantools，BLF 不可用）"))
        content = await uf.read()
        entry = STORE.add_file(chat, name, content)
        out.append(entry.to_dict())
        STORE.add_message(chat, "assistant", "attachment",
                          content=f"已接收文件 {name}（{ext.lstrip('.')}）", data=entry.to_dict())
    return {"files": out}


@app.delete("/api/chats/{cid}/files/{fid}")
def api_remove_file(cid: str, fid: str):
    chat = _chat_or_404(cid)
    ok = STORE.remove_file(chat, fid)
    if not ok:
        raise HTTPException(404, "文件不存在")
    return {"removed": fid}


class MessageIn(BaseModel):
    text: str


@app.post("/api/chats/{cid}/messages")
def api_send_message(cid: str, body: MessageIn):
    chat = _chat_or_404(cid)
    if not body.text.strip():
        raise HTTPException(400, "消息为空")
    run, started = RUNS.start(ENGINE, chat, body.text.strip())
    if not started:
        raise HTTPException(409, f"本对话已有一轮在进行中：{run.run_id}，请等待其完成")
    for _ in run.since(-1):
        pass
    if run.status == "error":
        raise HTTPException(500, f"本轮执行失败：{run.error}")
    added = [ev["data"] for ev in run.events if ev["event"] == "message"]
    return {"messages": [json_safe(a) for a in added], "run_id": run.run_id}


def _frame(ev: dict) -> str:
    """SSE 帧：id 即事件序号，断线重连时作为 after 传回即可精确续传。"""
    data = json.dumps(json_safe(ev["data"]), ensure_ascii=False, default=str, allow_nan=False)
    return f"id: {ev['seq']}\nevent: {ev['event']}\ndata: {data}\n\n"


def _run_stream(gen_run, after: int):
    yield ("event: run\ndata: "
           + json.dumps(json_safe(gen_run.info()), ensure_ascii=False, allow_nan=False) + "\n\n")
    for ev in gen_run.since(after):
        yield _frame(ev)


@app.post("/api/chats/{cid}/messages/stream")
async def api_send_stream(cid: str, body: MessageIn):
    """启动一轮并在 SSE 上回放；客户端断开不影响执行，刷新后走 /api/runs/{id}/stream 续看。"""
    chat = _chat_or_404(cid)
    if not body.text.strip():
        raise HTTPException(400, "消息为空")
    run, started = RUNS.start(ENGINE, chat, body.text.strip())
    if not started:
        raise HTTPException(409, f"本对话已有一轮在进行中：{run.run_id}")
    return StreamingResponse(_run_stream(run, -1), media_type="text/event-stream")


@app.get("/api/chats/{cid}/run")
def api_active_run(cid: str):
    """刷新后询问本会话是否有进行中的一轮（引擎在后台线程跑，不会因断线丢）。"""
    chat = _chat_or_404(cid)
    run = RUNS.active_for(chat.chat_id)
    return {"run": run.info() if run else None}


@app.get("/api/runs/{run_id}/stream")
def api_run_stream(run_id: str, after: int = Query(-1)):
    """订阅/回放某轮事件；after 传已收到的最大 seq 做增量续传。"""
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "该轮已回收或不存在（会话消息仍可从 GET /api/chats 读取）")
    return StreamingResponse(_run_stream(run, after), media_type="text/event-stream")


class SelectIn(BaseModel):
    target_type: str          # function | condition
    target_key: str
    profile: Optional[str] = None


@app.post("/api/chats/{cid}/select")
def api_select(cid: str, body: SelectIn):
    """统一选择：function → 该功能下的工况面板；condition → 写档位并分析。"""
    chat = _chat_or_404(cid)
    # 后台还有一轮在跑时不接受新动作：两者都会写同一份消息列表，且并发分析无意义
    running = RUNS.active_for(chat.chat_id)
    if running is not None:
        raise HTTPException(409, f"本对话有一轮回复正在进行（{running.run_id}），请等待完成后再选择")
    if body.target_type == "function":
        fn = CFG.function(body.target_key)
        if fn is None:
            raise HTTPException(400, f"未知功能 {body.target_key}")
        STORE.mark_panels_consumed(chat, chosen=fn.name)
        chat.selected_function = fn.key
        chat.selected_condition = None
        from brake_analyzer.agent.tools import recommend_targets

        rec = recommend_targets(CFG, fn.name, 8, function_key=fn.key)
        msg = ENGINE._options(chat, rec)
        return {"messages": [msg.to_dict()]}

    if body.target_type == "condition":
        cond = CFG.condition(body.target_key)
        if cond is None:
            raise HTTPException(400, f"未知工况 {body.target_key}")
        fn = CFG.function(cond.function)
        profile = (body.profile or "").strip() or None
        chosen = f"{cond.name}（{profile}）" if profile else cond.name
        if fn.profiles and not profile:
            # 需要档位但未提供 → 返回档位选择面板
            STORE.mark_panels_consumed(chat, chosen=chosen)
            msg = ENGINE._options(chat, {
                "hop": "profile", "function": fn.key,
                "target": {"key": cond.id, "name": cond.name},
                "profiles": fn.profiles, "profile_dims": fn.profile_dims,
            })
            return {"messages": [msg.to_dict()]}
        if profile:
            tokens = [t for t in profile.replace(",", "|").replace("_", "|").split("|") if t]
            bad = [t for t in tokens if fn.profiles and t not in fn.profiles]
            if bad:
                raise HTTPException(400, f"档位 {bad} 不在功能 {fn.key} 允许列表 {fn.profiles}")
        STORE.mark_panels_consumed(chat, chosen=chosen)
        added, _digests = ENGINE._analyze(chat, cond.id, profile)
        return {"messages": [m.to_dict() for m in added]}

    raise HTTPException(400, "target_type 必须为 function|condition")


# ---------------------------------------------------------------- analyses
def _find_analysis(analysis_id: str):
    """在所有 Session 缓存中定位 (AnalysisResult, RunSample)。"""
    for sess in STORE.sessions.values():
        for res in sess.analyses.values():
            for s in res.samples:
                if s.sample_id == analysis_id or s.analysis_id == analysis_id:
                    return res, s
    return None, None


@app.get("/api/analyses/{analysis_id}")
def api_analysis(analysis_id: str):
    res, sample = _find_analysis(analysis_id)
    if res is None:
        raise HTTPException(404, "分析结果不存在（服务可能已重启）")
    if sample is not None and sample.sample_id == analysis_id:
        # 单样本视图
        return {
            "analysis_id": sample.analysis_id,
            "sample_id": sample.sample_id,
            "run_index": sample.run_index,
            "file_name": sample.file_name,
            "condition_id": res.condition_id,
            "condition_name": res.condition_name,
            "profile": res.profile,
            "window": {"t_start": sample.window.t_start, "t_end": sample.window.t_end},
            "metrics": [m.to_dict() for m in sample.metrics],
            "verdicts": [v.to_dict() for v in sample.verdicts],
            "llm_suggestion": res.llm_suggestion,
            "degraded": res.degraded,
        }
    return res.to_dict()


@app.get("/api/analyses/{analysis_id}/timeseries")
def api_analysis_timeseries(analysis_id: str, signal: Optional[str] = Query(None)):
    res, sample = _find_analysis(analysis_id)
    if res is None or sample is None:
        raise HTTPException(404, "分析结果不存在")
    sess = STORE.sessions.get(sample.file_id)
    if sess is None or sess.signals is None:
        raise HTTPException(404, "信号缓存不存在（服务可能已重启）")
    window = None
    if is_valid(sample.window):
        window = (sample.window.t_start, sample.window.t_end)
    names = [signal] if signal else None
    layout = CFG.chart_layout if not names else None
    payload = timeseries_payload(sess.signals, window=window, series_names=names, layout=layout)
    payload["file_name"] = sample.file_name
    payload["chart_option"] = build_chart_option(
        sess.signals, window=window, series_names=names,
        layout=layout,
        highlight_status=("abnormal" if any(m.status == "abnormal" for m in sample.metrics)
                          else "missing" if any(m.status == "missing" for m in sample.metrics)
                          else "ok"),
        title=f"{sample.file_name} · run {sample.run_index}",
    )
    return payload


# ---------------------------------------------------------------- debug
@app.get("/api/debug/traces")
def api_traces(chat_id: Optional[str] = None, limit: int = 100):
    return TRACES.list(chat_id=chat_id, limit=min(limit, 500))


@app.get("/api/debug/traces/{tid}")
def api_trace(tid: str):
    t = TRACES.get(tid)
    if t is None:
        raise HTTPException(404, "trace 不存在")
    return t


# ---------------------------------------------------------------- pages
@app.get("/debug", response_class=HTMLResponse)
def page_debug():
    with open(os.path.join(FRONTEND, "debug.html"), "r", encoding="utf-8") as f:
        return f.read()


app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
