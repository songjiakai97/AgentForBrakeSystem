"""meta / 样例 / MCP / 目录类端点（design.md §7.2）。"""

import os

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional

from brake_analyzer.llm.prompts import build_chat_messages
from web import state as st
from web import state as st
from web.state import (
    ALLOWED_EXT, BUDGET, BLF_READY, CFG, ENGINE, FRONTEND, LLM, MCP_HUB,
    ROOT, STORE,
)

router = APIRouter()

# ---------------------------------------------------------------- demo 样例数据
SAMPLE_DIRS = {
    "mf4": os.path.join(ROOT, "tests", "data", "mf4"),
    "blf": os.path.join(ROOT, "tests", "data", "blf"),
}
MAX_SAMPLE_BYTES = 20 * 1024 * 1024


@router.get("/api/samples")
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


@router.get("/api/samples/{name}")
def api_sample_download(name: str):
    base = os.path.basename(name)
    fmt = os.path.splitext(base)[1].lstrip(".").lower()
    path = os.path.join(SAMPLE_DIRS.get(fmt, ""), base) if fmt in SAMPLE_DIRS else ""
    if not path or not os.path.isfile(path):
        raise HTTPException(404, "样例不存在")
    return FileResponse(path, filename=base, media_type="application/octet-stream")


# ---------------------------------------------------------------- meta
@router.get("/api/meta")
def api_meta():
    return {
        "llm_available": st.LLM.available,
        # 实测可达（models 列表，缓存 30s）：available 只说明配置齐了，
        # LM Studio 没启动时用户应该在页面头部就看到，而不是发消息才失败
        "llm_reachable": st.LLM.check_reachable(),
        "model": st.LLM.model if st.LLM.available else None,
        "engine": "llm" if st.LLM.available else "offline",
        "allowed_upload_ext": sorted(ALLOWED_EXT),
        "blf_ready": BLF_READY,
        "context": {
            **st.BUDGET.to_dict(),
            "summarizer": "llm" if st.LLM.available else "extractive",
        },
        # 外部工具是"锦上添花"，界面只需要知道挂了几个（详情走 /api/mcp）
        "mcp": {"configured": st.MCP_HUB.configured, "tools": len(st.MCP_HUB.tool_specs())},
    }


# ---------------------------------------------------------------- 外部 MCP（§6.11）
class McpCallBody(BaseModel):
    args: Optional[dict] = None
    timeout_sec: Optional[float] = None


@router.get("/api/mcp")
def api_mcp():
    """外部 MCP 服务状态：连没连上、报了几个工具、白名单有没有落空。

    只回显 env/headers 的**键名**（值可能是凭据）。
    """
    return st.MCP_HUB.summary()


@router.post("/api/mcp/reconnect")
def api_mcp_reconnect(name: str = Query("")):
    """手动重连（改了 mcp.yaml 或对方服务刚起来）。同步等待，返回新状态。"""
    if not st.MCP_HUB.configured:
        raise HTTPException(400, "没有配置任何 MCP 服务（configs/mcp.yaml 缺失或为空）")
    statuses = st.MCP_HUB.reconnect(name or None)
    return {"statuses": statuses, **st.MCP_HUB.summary()}


@router.post("/api/mcp/call/{tool_name}")
def api_mcp_call(tool_name: str, body: Optional[McpCallBody] = None):
    """调试用：绕开 st.LLM 直接调一个外部工具。

    存在的理由：外部 server 的 schema 五花八门，出问题时最需要区分
    「模型参数填错了」和「这个工具本来就返回这个」——直连一次就知道是哪边。
    """
    if not st.MCP_HUB.is_external(tool_name):
        raise HTTPException(404, f"外部工具不存在：{tool_name}（看 GET /api/mcp 的清单）")
    payload = st.MCP_HUB.call(tool_name, (body.args if body else None),
                           timeout=(body.timeout_sec if body else None))
    return {"tool": tool_name, "payload": payload}


@router.get("/api/context/stats")
def api_context_stats(cid: str = Query("")):
    """当前会话上下文的估算 token 与两条阈值的关系（调试用）。"""
    chat = st.STORE.get_chat(cid) if cid else st.STORE.latest_chat()
    if chat is None:
        raise HTTPException(status_code=404, detail="没有可用对话")
    history = [{"role": m.role, "kind": m.kind, "content": m.content} for m in chat.messages]
    msgs = build_chat_messages(
        st.CFG,
        history,
        "", [f.name for f in chat.files],
        {"condition": chat.selected_condition, "profile": chat.selected_profile},
    )
    # baseline = 同会话但历史清空的估算值（系统提示 + 文件/选择上下文）：
    # 「2 条消息」的困惑来自系统提示——空会话也有它，单看条数没意义
    baseline_msgs = build_chat_messages(
        st.CFG, [], "", [f.name for f in chat.files],
        {"condition": chat.selected_condition, "profile": chat.selected_profile},
    )
    return {"chat_id": chat.chat_id, "messages": len(msgs),
            "baseline_tokens": st.ENGINE.compressor.stats(baseline_msgs)["estimated_tokens"],
            **st.ENGINE.compressor.stats(msgs)}


@router.get("/api/functions")
def api_functions():
    return [
        {
            "key": fn.key, "name": fn.name, "aliases": fn.aliases,
            "profiles": fn.profiles, "profile_dims": fn.profile_dims,
            "description": fn.description,
            "condition_count": len(st.CFG.conditions_of(fn.key)),
        }
        for fn in st.CFG.functions.values()
    ]


@router.get("/api/conditions")
def api_conditions(function_key: Optional[str] = Query(None)):
    items = []
    src = st.CFG.conditions_of(function_key) if function_key else list(st.CFG.conditions.values())
    for c in src:
        items.append({
            "id": c.id, "function": c.function, "name": c.name,
            "maneuver": c.maneuver, "surface_code": c.surface_code,
            "enabled_metrics": c.enabled_metrics, "params": c.params,
        })
    return items
