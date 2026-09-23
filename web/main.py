"""Web API（design.md §7.2）。

启动：python -m uvicorn web.main:app --port 8000
"""

import json
import os
import warnings

warnings.filterwarnings("ignore", module="asammdf")

from fastapi import FastAPI, HTTPException, UploadFile, File, Query  # noqa: E402
from fastapi.responses import HTMLResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from typing import List, Optional  # noqa: E402

from brake_analyzer.agent.engine import ChatEngine  # noqa: E402
from brake_analyzer.charts.html import build_chart_option, timeseries_payload  # noqa: E402
from brake_analyzer.configs import ConfigError, load_configs  # noqa: E402
from brake_analyzer.events.segment import is_valid  # noqa: E402
from brake_analyzer.llm.client import LlmClient, TraceStore  # noqa: E402
from brake_analyzer.llm.kb import KnowledgeBase  # noqa: E402
from web.store import Store  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "configs")
KB_DIR = os.path.join(ROOT, "knowledge")
FRONTEND = os.path.join(ROOT, "web", "frontend")
ALLOWED_EXT = {".mf4"}

app = FastAPI(title="Brake Agent Demo", docs_url="/api/docs", openapi_url="/api/openapi.json")

try:
    CFG = load_configs(CONFIG_DIR)
except ConfigError as e:
    raise RuntimeError(f"配置加载失败，服务拒绝启动：{e}")

TRACES = TraceStore(cap=int(os.getenv("TRACE_STORE_MAX", "500")))
STORE = Store()
KB = KnowledgeBase(KB_DIR)
LLM = LlmClient(traces=TRACES)
ENGINE = ChatEngine(CFG, LLM, KB, STORE)


@app.on_event("startup")
def _ensure_demo_data():
    """demo 样例可再生：缺失时自动生成（tests/data/mf4 不入库）。"""
    data_dir = os.path.join(ROOT, "tests", "data", "mf4")
    has_data = os.path.isdir(data_dir) and any(
        f.endswith(".mf4") for f in os.listdir(data_dir)
    )
    if not has_data:
        try:
            import sys

            sys.path.insert(0, ROOT)
            from scripts.generate_demo_data import generate_all

            generate_all(data_dir)
        except Exception as e:  # 数据缺失不阻止服务启动
            print(f"[warn] demo 样例生成失败: {e}")


def _chat_or_404(cid: str):
    chat = STORE.get_chat(cid)
    if chat is None:
        raise HTTPException(404, f"对话不存在: {cid}")
    return chat


# ---------------------------------------------------------------- demo 样例数据
SAMPLE_DIR = os.path.join(ROOT, "tests", "data", "mf4")


@app.get("/api/samples")
def api_samples():
    if not os.path.isdir(SAMPLE_DIR):
        return []
    return sorted(
        f for f in os.listdir(SAMPLE_DIR)
        if f.endswith(".mf4") and os.path.getsize(os.path.join(SAMPLE_DIR, f)) < 20 * 1024 * 1024
    )


@app.get("/api/samples/{name}")
def api_sample_download(name: str):
    base = os.path.basename(name)
    path = os.path.join(SAMPLE_DIR, base)
    if not base.endswith(".mf4") or not os.path.isfile(path):
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
    }


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
        if os.path.splitext(name)[1].lower() not in ALLOWED_EXT:
            raise HTTPException(400, f"demo 仅接受 .mf4 上传，收到: {name}")
        content = await uf.read()
        entry = STORE.add_file(chat, name, content)
        out.append(entry.to_dict())
        STORE.add_message(chat, "assistant", "attachment",
                          content=f"已接收文件 {name}", data=entry.to_dict())
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
    added = ENGINE.run(chat, body.text.strip())
    return {"messages": added}


@app.post("/api/chats/{cid}/messages/stream")
async def api_send_stream(cid: str, body: MessageIn):
    chat = _chat_or_404(cid)
    if not body.text.strip():
        raise HTTPException(400, "消息为空")

    def gen():
        for ev in ENGINE.stream(chat, body.text.strip()):
            yield f"event: {ev['event']}\ndata: {json.dumps(ev['data'], ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


class SelectIn(BaseModel):
    target_type: str          # function | condition
    target_key: str
    profile: Optional[str] = None


@app.post("/api/chats/{cid}/select")
def api_select(cid: str, body: SelectIn):
    """统一选择：function → 第二跳工况精排；condition → 写档位并分析。"""
    chat = _chat_or_404(cid)
    if body.target_type == "function":
        fn = CFG.function(body.target_key)
        if fn is None:
            raise HTTPException(400, f"未知功能 {body.target_key}")
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
        if fn.profiles and not profile:
            # 需要档位但未提供 → 返回档位选择面板
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
        added = ENGINE._analyze(chat, cond.id, profile)
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
    payload = timeseries_payload(sess.signals, window=window, series_names=names)
    payload["file_name"] = sample.file_name
    payload["chart_option"] = build_chart_option(
        sess.signals, window=window, series_names=names,
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
