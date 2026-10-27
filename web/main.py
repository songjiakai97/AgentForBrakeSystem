"""Web API（design.md §7.2）。

启动：python -m uvicorn web.main:app --port 8000
"""

import json
import logging
import re
import os
import time
import uuid
import warnings

warnings.filterwarnings("ignore", module="asammdf")

from fastapi import FastAPI, HTTPException, UploadFile, File, Query  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from typing import List, Optional  # noqa: E402

from brake_analyzer.agent.cond_index import ConditionIndex  # noqa: E402
from brake_analyzer.agent.engine import ChatEngine  # noqa: E402
from brake_analyzer.charts.html import build_chart_option, timeseries_payload  # noqa: E402
from brake_analyzer.configs import (  # noqa: E402
    ConfigError, load_blf_config, load_configs, pick,
)
from brake_analyzer.events.segment import is_valid  # noqa: E402
from brake_analyzer.llm.client import LlmClient, TraceStore  # noqa: E402
from brake_analyzer.llm.context import ContextBudget  # noqa: E402
from brake_analyzer.llm.kb import KnowledgeBase  # noqa: E402
from brake_analyzer.llm.prompts import build_chat_messages, digest_from_analysis  # noqa: E402
from brake_analyzer.mcp_bridge import McpHub, load_mcp_config  # noqa: E402
from brake_analyzer.records import store as record_store  # noqa: E402
from brake_analyzer.records.query import records_compare  # noqa: E402
from brake_analyzer.records.service import RecordNotFound, RecordService  # noqa: E402
from brake_analyzer.schemas import json_safe  # noqa: E402
from web.runs import RunManager  # noqa: E402
from web.store import Store  # noqa: E402

# 全局日志基线：engine 兜底、loader 中断、MCP 失败等埋点经各模块 logger 汇到这里。
# uvicorn 只配置自己的 "uvicorn.*" logger，不动 root，basicConfig 在此是安全的。
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

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
# 只要求 BLF 覆盖「参与计算的信号」，纯观测量允许缺席（CAN 上没有就不带）
try:
    BLF_CFG = load_blf_config(CONFIG_DIR, required_signals=CFG.required_signals,
                              registry=CFG.signal_registry)
except ConfigError as e:
    raise RuntimeError(f"BLF 配置加载失败，服务拒绝启动：{e}")

TRACES = TraceStore(cap=int(os.getenv("TRACE_STORE_MAX", "500")))
STORE = Store()
KB = KnowledgeBase(KB_DIR)
LLM = LlmClient(traces=TRACES)
# 语义通道（RAG + 工况召回）统一开关：ENABLE_RAG=false 或 embedding 不可用时，
# 手册检索退关键词、resolve_condition 退纯唯一键——分析主链路不受任何影响
_rag_on = os.getenv("ENABLE_RAG", "true").strip().lower() != "false" and LLM.embed_available
if _rag_on:
    KB.set_embedder(LLM.embed, LLM.embed_model)

# 工况语义索引：resolve_condition 唯一键零命中时的召回层（口语→配置词汇的桥）
COND_INDEX = ConditionIndex(os.path.join(KB_DIR, "vectors.db"))
if _rag_on:
    COND_INDEX.set_embedder(LLM.embed, LLM.embed_model)
    COND_INDEX.sync(CFG)
try:
    BUDGET = ContextBudget.from_env()
except ValueError as e:
    raise RuntimeError(f"上下文预算配置无效，服务拒绝启动：{e}")

# 外部 MCP 服务（§6.11）：configs/mcp.yaml 是运维白名单，缺失 = 没有外部工具。
# 与 BLF 配置同一口径：**结构非法拒绝启动**（配置写错却静默少一批工具，
# 用户只会觉得"Agent 突然不会用了"）；但**连不上不拒绝启动**——第三方进程
# 起不来是常态，那属于运行期状态，由 /api/mcp 显示。
MCP_CONFIG_PATH = os.path.join(CONFIG_DIR, "mcp.yaml")
try:
    MCP_SPECS = load_mcp_config(MCP_CONFIG_PATH)
except ConfigError as e:
    raise RuntimeError(f"MCP 配置加载失败，服务拒绝启动：{e}")
MCP_HUB = McpHub(MCP_SPECS)

# 试验记录库（§17）：只登记路径，指标惰性计算。库文件不存在时首次连接自动建表；
# RECORDS_DB 指向别处（如挂载的共享盘）即可复用同一套服务。
RECORDS_DB = os.getenv("RECORDS_DB", os.path.join(ROOT, "data", "records.db"))
RECORDS = RecordService(CFG, RECORDS_DB, blf_cfg=BLF_CFG)

ENGINE = ChatEngine(CFG, LLM, KB, STORE, budget=BUDGET, blf_cfg=BLF_CFG, mcp=MCP_HUB,
                    cond_index=COND_INDEX, records=RECORDS)
# 一轮 = 一个后台任务：与 HTTP 连接解耦，刷新/断网不再丢本轮回复
RUNS = RunManager()


@app.on_event("startup")
def _bump_threadpool():
    """SSE 订阅与同步长端点各自占死一个线程池线程（默认仅 40 个）。

    每个活跃 SSE 在整轮 run 期间都占着一个线程（同步生成器经
    iterate_in_threadpool 迭代，next() 阻塞在事件等待上）；并发会话一多
    线程池耗尽，所有请求无限排队——表现就是「发消息永久卡住」。调大上界。
    """
    from anyio import to_thread

    to_thread.current_default_thread_limiter().total_tokens = 200


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


@app.on_event("startup")
def _connect_mcp():
    """后台连外部 MCP（§6.11）。

    刻意不 await：一个第三方进程起不来（没装、路径错、缺 node）不该让 web 启动
    多等 15 秒，更不该让它起不来。状态由 GET /api/mcp 显示，前端轮询看到变 ok。
    """
    if MCP_HUB.configured:
        MCP_HUB.connect_all()


@app.on_event("shutdown")
def _shutdown_mcp():
    MCP_HUB.shutdown()


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
        # 外部工具是"锦上添花"，界面只需要知道挂了几个（详情走 /api/mcp）
        "mcp": {"configured": MCP_HUB.configured, "tools": len(MCP_HUB.tool_specs())},
    }


# ---------------------------------------------------------------- 外部 MCP（§6.11）
class McpCallBody(BaseModel):
    args: Optional[dict] = None
    timeout_sec: Optional[float] = None


@app.get("/api/mcp")
def api_mcp():
    """外部 MCP 服务状态：连没连上、报了几个工具、白名单有没有落空。

    只回显 env/headers 的**键名**（值可能是凭据）。
    """
    return MCP_HUB.summary()


@app.post("/api/mcp/reconnect")
def api_mcp_reconnect(name: str = Query("")):
    """手动重连（改了 mcp.yaml 或对方服务刚起来）。同步等待，返回新状态。"""
    if not MCP_HUB.configured:
        raise HTTPException(400, "没有配置任何 MCP 服务（configs/mcp.yaml 缺失或为空）")
    statuses = MCP_HUB.reconnect(name or None)
    return {"statuses": statuses, **MCP_HUB.summary()}


@app.post("/api/mcp/call/{tool_name}")
def api_mcp_call(tool_name: str, body: Optional[McpCallBody] = None):
    """调试用：绕开 LLM 直接调一个外部工具。

    存在的理由：外部 server 的 schema 五花八门，出问题时最需要区分
    「模型参数填错了」和「这个工具本来就返回这个」——直连一次就知道是哪边。
    """
    if not MCP_HUB.is_external(tool_name):
        raise HTTPException(404, f"外部工具不存在：{tool_name}（看 GET /api/mcp 的清单）")
    payload = MCP_HUB.call(tool_name, (body.args if body else None),
                           timeout=(body.timeout_sec if body else None))
    return {"tool": tool_name, "payload": payload}


@app.get("/api/context/stats")
def api_context_stats(cid: str = Query("")):
    """当前会话上下文的估算 token 与两条阈值的关系（调试用）。"""
    chat = STORE.get_chat(cid) if cid else STORE.latest_chat()
    if chat is None:
        raise HTTPException(status_code=404, detail="没有可用对话")
    history = [{"role": m.role, "kind": m.kind, "content": m.content} for m in chat.messages]
    msgs = build_chat_messages(
        CFG,
        history,
        "", [f.name for f in chat.files],
        {"condition": chat.selected_condition, "profile": chat.selected_profile},
    )
    # baseline = 同会话但历史清空的估算值（系统提示 + 文件/选择上下文）：
    # 「2 条消息」的困惑来自系统提示——空会话也有它，单看条数没意义
    baseline_msgs = build_chat_messages(
        CFG, [], "", [f.name for f in chat.files],
        {"condition": chat.selected_condition, "profile": chat.selected_profile},
    )
    return {"chat_id": chat.chat_id, "messages": len(msgs),
            "baseline_tokens": ENGINE.compressor.stats(baseline_msgs)["estimated_tokens"],
            **ENGINE.compressor.stats(msgs)}


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


@app.get("/api/kb/search")
def api_kb_search(q: str = Query(...), top_k: int = Query(3)):
    """检索调试：一条查询命中哪些节、走了哪条通道（semantic=向量 / keyword=兜底）。

    mode=keyword 且配置了 embedding 时，通常是 embedding 端点没起——去 LM Studio 看。
    """
    hits = KB.search(q, top_k=min(max(top_k, 1), 10))
    return {"query": q, "mode": KB.last_search_mode,
            "rag_enabled": KB._embed_fn is not None, "hits": hits}


# ---------------------------------------------------------------- 知识库上传/删除
KB_MANUAL_DIR = os.path.join(KB_DIR, "manual")
KB_ARCHIVE_DIR = os.path.join(KB_DIR, "archive")
KB_ALLOWED_EXT = {".md", ".docx"}
MAX_KB_BYTES = 50 * 1024 * 1024


def _kb_safe_name(name: str) -> str:
    """落盘文件名：只要 basename，防路径穿越；空结果给默认名。"""
    import re as _re

    base = os.path.basename(name or "").strip()
    base = _re.sub(r"[\\/:*?\"<>|]", "_", base)
    return base or "manual.md"


@app.post("/api/kb/manual")
async def api_kb_upload(files: List[UploadFile] = File(...)):
    """上传手册：.md 直存，.docx 走摄取层转 md（原件存 knowledge/archive/ 留底）。

    文件不在部署机上也没关系——浏览器在哪台机器，就从哪台上传。
    上传前对比 reload 前后的节集合：贡献 0 节的文件拒绝（防止"死文件"）。
    """
    os.makedirs(KB_MANUAL_DIR, exist_ok=True)
    os.makedirs(KB_ARCHIVE_DIR, exist_ok=True)
    out = []
    for uf in files:
        name = _kb_safe_name(uf.filename or "manual.md")
        ext = os.path.splitext(name)[1].lower()
        if ext not in KB_ALLOWED_EXT:
            raise HTTPException(400, f"仅接受 {'/'.join(sorted(KB_ALLOWED_EXT))}，收到: {name}")
        content = await uf.read()
        if len(content) > MAX_KB_BYTES:
            raise HTTPException(400, f"文件超过 {MAX_KB_BYTES // 1048576}MB 上限: {name}")

        if ext == ".docx":
            tmp = os.path.join(KB_ARCHIVE_DIR, name)
            with open(tmp, "wb") as f:
                f.write(content)
            try:
                from markitdown import MarkItDown

                md_text = MarkItDown().convert(tmp).text_content
            except Exception as e:
                # 转换失败不留坏原件占着 archive：删掉再报
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise HTTPException(400, f"{name}: markitdown 转换失败 "
                                         f"{type(e).__name__}: {str(e)[:200]}")
            # 图片 → 占位说明：markitdown 把图转成 base64 内嵌，直接进块会撑爆块预算
            n_img = len(re.findall(r"!\[[^\]]*\]\([^)]*\)", md_text))
            if n_img:
                md_text = re.sub(r"!\[[^\]]*\]\([^)]*\)",
                                 "> [图片占位]（原文档含图，见 knowledge/archive/）", md_text)
            if not re.search(r"^#{1,3}\s+\S", md_text, re.M):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise HTTPException(400, f"{name}: 转换结果没有任何标题（# ~ ###），"
                                         "无法切出可检索的节——确认文档使用了标题样式")
        else:
            # 编码探测：UTF-8 优先；失败时用 chardet 识别（GBK 编辑器存的中文
            # md 很常见），都不行才拒绝——errors="replace" 会把整篇中文静默
            # 变成乱码入库，检索到的是乱码且无任何报错
            try:
                md_text = content.decode("utf-8-sig")
            except UnicodeDecodeError:
                import chardet

                enc = (chardet.detect(content[:65536]) or {}).get("encoding") or ""
                try:
                    md_text = content.decode(enc)
                except (UnicodeDecodeError, LookupError):
                    raise HTTPException(
                        400, f"{name}: 无法识别文件编码（尝试过 UTF-8/{enc}），"
                             "请另存为 UTF-8 后重新上传")
            if not re.search(r"^#{1,3}\s+\S", md_text, re.M):
                raise HTTPException(400, f"{name}: 文件没有任何标题（# ~ ###），"
                                         "切不出可检索的节")

        md_name = os.path.splitext(name)[0] + ".md"
        overwritten = os.path.exists(os.path.join(KB_MANUAL_DIR, md_name))
        before = {sid: (s.title, s.text) for sid, s in KB.sections.items()}
        with open(os.path.join(KB_MANUAL_DIR, md_name), "w", encoding="utf-8") as f:
            f.write(md_text)
        KB.reload()
        # 回执节对照（⑤）：reload 前后新增或内容变化的节——docx/md 统一口径，
        # 用户据此确认切分与 id 是否符合预期（规则 kb_ref 将来要引用这些 id）
        changed = [{"id": sid, "title": sec.title}
                   for sid, sec in KB.sections.items()
                   if before.get(sid) != (sec.title, sec.text)]
        out.append({
            "file": md_name,
            "source": "docx" if ext == ".docx" else "md",
            "overwritten": overwritten,
            "sections": changed,
            "section_count": len(changed),
            "kb_sections_total": len(KB.sections),
        })
    return {"files": out}


@app.delete("/api/kb/manual/{name}")
def api_kb_delete(name: str):
    stem = os.path.splitext(_kb_safe_name(name))[0]
    md_name = stem + ".md"
    path = os.path.join(KB_MANUAL_DIR, md_name)
    if not os.path.isfile(path):
        raise HTTPException(404, f"手册不存在: {md_name}")
    os.remove(path)
    # 上传时留底的原件一并清掉，否则删除后 archive 里留着不可见的孤儿文件
    for ext in (".docx", ".md"):
        orig = os.path.join(KB_ARCHIVE_DIR, stem + ext)
        if os.path.isfile(orig):
            os.remove(orig)
    KB.reload()
    return {"deleted": md_name, "kb_sections_total": len(KB.sections)}


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


MAX_UPLOAD_BYTES = 200 * 1024 * 1024      # 单文件上限：全量读进内存，不设限会打爆服务


def _upload_ext_check(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(
            400, f"仅接受 {'/'.join(sorted(ALLOWED_EXT))} 上传，收到: {name}"
                 + ("" if BLF_READY else "（当前环境缺 python-can/cantools，BLF 不可用）"))
    return ext


@app.post("/api/chats/{cid}/files")
async def api_upload(cid: str, files: List[UploadFile] = File(...)):
    chat = _chat_or_404(cid)
    # 一次请求 = 一批（用户多选上传是一个动作），后续分析默认只取最新这批（§8.2）
    batch_id = "fb_" + uuid.uuid4().hex[:8]
    out = []
    for uf in files:
        name = uf.filename or "unnamed.mf4"
        _upload_ext_check(name)
        content = await uf.read()
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(400, f"文件超过 {MAX_UPLOAD_BYTES // 1048576}MB 上限: {name}")
        entry = STORE.add_file(chat, name, content, batch_id=batch_id)
        out.append(entry.to_dict())
        STORE.add_message(chat, "assistant", "attachment",
                          content=f"已接收文件 {name}（{os.path.splitext(name)[1].lstrip('.')}）",
                          data=entry.to_dict())
    return {"files": out}


class LocalFileIn(BaseModel):
    path: str


@app.post("/api/chats/{cid}/files/by-path")
def api_upload_by_path(cid: str, body: LocalFileIn):
    """引用本机已有文件（零拷贝）：服务和数据在同一台机器时，免掉整份文件的
    HTTP 上传，登记原路径直接分析。本地引用的文件在删除会话时不会被删除。"""
    chat = _chat_or_404(cid)
    raw = (body.path or "").strip().strip('"').strip("'")
    if not raw:
        raise HTTPException(400, "路径为空")
    name = os.path.basename(raw)
    _upload_ext_check(name)
    if not os.path.isfile(raw):
        raise HTTPException(400, f"文件不存在或不是普通文件: {raw}")
    size = os.path.getsize(raw)
    if size > MAX_UPLOAD_BYTES:
        raise HTTPException(400, f"文件超过 {MAX_UPLOAD_BYTES // 1048576}MB 上限: {name}")
    batch_id = "fb_" + uuid.uuid4().hex[:8]
    entry = STORE.add_file(chat, name, b"", batch_id=batch_id,
                           source="local", local_path=raw)
    STORE.add_message(chat, "assistant", "attachment",
                      content=f"已引用本地文件 {name}（原路径，未拷贝）",
                      data=entry.to_dict())
    return {"files": [entry.to_dict()]}


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
        # detail 带 run_id：前端据此连上正在跑的那一轮（拿到进度、也能停止它），
        # 而不是只弹一条「请求失败」——那会让界面死在既停不掉又等不出的状态。
        raise HTTPException(409, {
            "message": f"本对话已有一轮在进行中：{run.run_id}",
            "run_id": run.run_id,
            "status": run.status,
        })
    return StreamingResponse(_run_stream(run, -1), media_type="text/event-stream")


@app.get("/api/chats/{cid}/run")
def api_active_run(cid: str):
    """刷新后询问本会话是否有进行中的一轮（引擎在后台线程跑，不会因断线丢）。"""
    chat = _chat_or_404(cid)
    run = RUNS.active_for(chat.chat_id)
    return {"run": run.info() if run else None}


@app.post("/api/runs/{run_id}/stop")
def api_stop_run(run_id: str):
    """请求停止进行中的一轮：只置标记，引擎在下一个检查点收尾（半截正文落库）。"""
    run = RUNS.stop(run_id)
    if run is None:
        raise HTTPException(404, "该轮已回收或不存在（会话消息仍可从 GET /api/chats 读取）")
    return {"run_id": run.run_id, "stop_requested": run.stop_requested, "status": run.status}


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


FB_STATUSES = {"accepted": "已采纳", "verified": "验证有效", "ineffective": "验证无效"}


def _sync_suggestion_to_message(res) -> None:
    """res.llm_suggestion 更新后同步到对话卡片：卡片 data 是创建时的深拷贝，
    不同步的话前端要刷新才能看到反馈/追加建议。"""
    for chat in STORE.chats.values():
        for m in chat.messages:
            if (m.kind == "analysis_result" and isinstance(m.data, dict)
                    and m.data.get("analysis_id") == res.analysis_id
                    and isinstance(m.data.get("llm_suggestion"), dict)):
                m.data["llm_suggestion"] = res.llm_suggestion


class FeedbackIn(BaseModel):
    status: str                    # accepted | verified | ineffective
    note: str = ""


@app.post("/api/analyses/{analysis_id}/feedback")
def api_analysis_feedback(analysis_id: str, body: FeedbackIn):
    """标定建议闭环：记录用户反馈（采纳/验证结果），并让 LLM 基于反馈给出
    下一步标定动作（追加条目）。LLM 不可用时只落反馈，不阻塞。"""
    if body.status not in FB_STATUSES:
        raise HTTPException(400, f"status 必须是 {'/'.join(FB_STATUSES)}")
    res, _sample = _find_analysis(analysis_id)
    if res is None:
        raise HTTPException(404, "分析结果不存在（服务可能已重启）")
    if not res.llm_suggestion:
        raise HTTPException(400, "该分析没有 LLM 标定建议（可能已降级为仅规则结论）")

    fb = {"status": body.status, "note": body.note.strip()[:500], "ts": time.time()}
    res.llm_suggestion["feedback"] = fb

    # 闭环判断：采纳/验证反馈 + 原建议 + 实测摘要 → LLM 判断是否追加标定条目
    followup = None
    if LLM.available:
        try:
            digests = digest_from_analysis(res.to_dict())
            system = (
                "你是制动标定助手。用户对上一轮标定建议给出了执行反馈。"
                "请基于反馈与实测指标，判断原建议是否需要修正，并给出下一步标定动作。"
                '只输出 JSON：{"summary": "一段话判断", "calibration_actions": '
                '[{"param": "可标定量", "direction": "增大|减小", "metric": "针对指标", '
                '"expected_effect": "预期效果", "risk": "风险"}]}。'
                "若无需要追加的动作，calibration_actions 给空数组并在 summary 说明。"
            )
            user = json.dumps({
                "condition": res.condition_name, "profile": res.profile,
                "original_suggestion": res.llm_suggestion,
                "user_feedback": {"status": FB_STATUSES[body.status], "note": fb["note"]},
                "measured": digests,
            }, ensure_ascii=False, default=str)
            followup = LLM.complete_json(system, user)
        except Exception:
            followup = None        # 闭环建议是锦上添花，失败不吞掉反馈本身
    if followup and isinstance(followup.get("calibration_actions", []), list):
        res.llm_suggestion["followup"] = followup
    _sync_suggestion_to_message(res)
    return {"analysis_id": analysis_id,
            "feedback": fb,
            "followup": res.llm_suggestion.get("followup"),
            "llm_suggestion": res.llm_suggestion}


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
    # 默认只画该功能关心的通道（ABS 不画 TCS_Active），分组仍按 signals.yaml
    fn = CFG.function(res.function_key)
    names = [signal] if signal else (list(fn.signals) if fn and fn.signals else None)
    layout = CFG.chart_layout
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


# ---------------------------------------------------------------- 记录库（§17）

def _record_cond_cards(cond, metrics_rows):
    """把缓存指标行拼成结果卡片：补上 ok_range/单位（缓存里只存值）。"""
    out = []
    for m in metrics_rows:
        tmpl = CFG.metrics.get(m["metric_key"])
        entry = pick(cond.metric_index, m["metric_key"], None) if cond else None
        item = dict(m)
        item["category"] = tmpl.category if tmpl else ""
        item["ok_range"] = (entry or {}).get("ok_range")
        out.append(item)
    return out


@app.get("/api/records/meta")
def api_records_meta():
    """记录库筛选项 + 库内状态；页面首次加载用。"""
    conn = RECORDS.connect()
    try:
        return {
            "db": RECORDS_DB,
            "vehicles": record_store.distinct(conn, "vehicle"),
            "file_ids": record_store.distinct(conn, "file_id"),
            "status": record_store.status_counts(conn),
            "functions": [{"key": f.key, "name": f.name} for f in CFG.functions.values()],
            "conditions": [
                {"id": c.id, "name": c.name, "function": c.function,
                 "file_ids": list(c.file_ids), "params": dict(c.params)}
                for c in CFG.conditions.values()
            ],
            "metrics": [{"key": k, "name": t.name, "unit": t.unit}
                        for k, t in CFG.metrics.items()],
        }
    finally:
        conn.close()


@app.get("/api/records/compare")
def api_records_compare(condition_id: Optional[str] = Query(None),
                        function_key: Optional[str] = Query(None),
                        vehicles: Optional[List[str]] = Query(None),
                        metric: Optional[str] = Query(None),
                        vehicle: Optional[str] = Query(None),
                        file_id: Optional[str] = Query(None),
                        keyword: Optional[str] = Query(None)):
    """横向对比（页面表格用）：同一工况下不同车型的客观指标。

    车型/编号/关键词与清单页同口径传进来：页面筛了却比全库，会出现
    「对比表里的记录不是我搜出来的」。`vehicle` 是页面单选，`vehicles` 给对话工具用。
    """
    picked = list(vehicles or [])
    if vehicle and vehicle not in picked:
        picked.append(vehicle)
    return records_compare(RECORDS, CFG, condition_id=condition_id,
                           function_key=function_key,
                           vehicles=picked or None, metric=metric,
                           file_id=file_id, keyword=keyword,
                           keyword_condition_ids=(CFG.conditions_by_text(keyword)
                                                  if keyword else None))


@app.get("/api/records")
def api_records(file_id: Optional[str] = Query(None),
                condition_id: Optional[str] = Query(None),
                vehicle: Optional[str] = Query(None),
                function_key: Optional[str] = Query(None),
                keyword: Optional[str] = Query(None),
                limit: int = Query(200, ge=1, le=500)):
    """记录清单：只读元数据，不触发解析。编号/车型/关键词按「包含」匹配。

    关键词还走一遍配置侧（工况名/路面/动作），与对话里的 query_records 同口径——
    否则页面上输「干沥青」搜不到、模型那边却能搜到，两个入口两套结论。
    """
    cond_ids = ([c.id for c in CFG.conditions_of(function_key)]
                if function_key else None)
    rows = RECORDS.list_records(file_id=file_id, condition_id=condition_id,
                                condition_ids=cond_ids, vehicle=vehicle,
                                keyword=keyword,
                                keyword_condition_ids=(CFG.conditions_by_text(keyword)
                                                       if keyword else None),
                                limit=limit)
    for r in rows:
        cond = CFG.condition(r["condition_id"])
        r["condition_name"] = cond.name if cond else r["condition_id"]
        r["function"] = cond.function if cond else ""
    return {"count": len(rows), "records": rows, "db": RECORDS_DB}


@app.get("/api/records/{record_id}")
def api_record(record_id: str, refresh: bool = Query(False)):
    """单条记录：必要时惰性分析，返回结果卡片数据 + 窗口。"""
    try:
        if refresh:
            status, _ = RECORDS.analyze(record_id, force=True)
            detail = RECORDS.record_detail(record_id, ensure=False)
            detail["analysis_status"] = status
        else:
            detail = RECORDS.record_detail(record_id)
    except RecordNotFound as e:
        raise HTTPException(404, str(e))
    cond = CFG.condition(detail["record"]["condition_id"])
    detail["metrics"] = _record_cond_cards(cond, detail["metrics"])
    detail["condition_name"] = cond.name if cond else detail["record"]["condition_id"]
    detail["function"] = cond.function if cond else ""
    fn = CFG.function(cond.function) if cond else None
    detail["signals"] = list(fn.signals) if fn else []
    return detail


@app.get("/api/records/{record_id}/timeseries")
def api_record_timeseries(record_id: str, signal: Optional[str] = Query(None)):
    """整段曲线 + 事件窗口高亮；默认只画该功能关心的通道。"""
    try:
        signals, row = RECORDS.load_signals(record_id)
    except RecordNotFound as e:
        raise HTTPException(404, str(e))
    cond = CFG.condition(row["condition_id"])
    fn = CFG.function(cond.function) if cond else None
    # 曲线要配窗口：先确保有缓存（已算过就是一次查询，没算过才解析）
    try:
        RECORDS.analyze(record_id)
    except RecordNotFound as e:
        raise HTTPException(404, str(e))
    conn = RECORDS.connect()
    try:
        windows = record_store.sample_windows(conn, record_id)
        metric_rows = record_store.metric_rows(conn, record_id)
    finally:
        conn.close()
    window = None
    if windows:
        window = (windows[0]["t_start"], windows[0]["t_end"])
    names = [signal] if signal else (list(fn.signals) if fn and fn.signals else None)
    statuses = {m["status"] for m in metric_rows}
    highlight = ("abnormal" if "abnormal" in statuses
                 else "missing" if "missing" in statuses or not metric_rows else "ok")
    payload = timeseries_payload(signals, window=None, series_names=names,
                                 layout=CFG.chart_layout)
    payload["file_name"] = row["file_name"]
    payload["windows"] = windows
    payload["chart_option"] = build_chart_option(
        signals, window=None, series_names=names, layout=CFG.chart_layout,
        highlight_status=highlight,
        highlight=window,
        title=f"{row['vehicle']} · {row['file_name']}",
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
@app.get("/records", response_class=HTMLResponse)
def page_records():
    with open(os.path.join(FRONTEND, "records.html"), "r", encoding="utf-8") as f:
        return f.read()


@app.get("/debug", response_class=HTMLResponse)
def page_debug():
    with open(os.path.join(FRONTEND, "debug.html"), "r", encoding="utf-8") as f:
        return f.read()


app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
