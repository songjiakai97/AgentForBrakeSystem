"""调试与页面端点：traces、/debug、/records 页面（design.md §12）。"""

import os

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from typing import Optional

from web import state as st
from web.state import FRONTEND, TRACES

router = APIRouter()


@router.get("/api/debug/traces")
def api_traces(chat_id: Optional[str] = None, limit: int = 100):
    return st.TRACES.list(chat_id=chat_id, limit=min(limit, 500))


@router.get("/api/debug/traces/{tid}")
def api_trace(tid: str):
    t = st.TRACES.get(tid)
    if t is None:
        raise HTTPException(404, "trace 不存在")
    return t


@router.get("/records", response_class=HTMLResponse)
def page_records():
    with open(os.path.join(FRONTEND, "records.html"), "r", encoding="utf-8") as f:
        return f.read()


@router.get("/debug", response_class=HTMLResponse)
def page_debug():
    with open(os.path.join(FRONTEND, "debug.html"), "r", encoding="utf-8") as f:
        return f.read()
