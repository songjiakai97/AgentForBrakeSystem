"""Web API（design.md §7.2）。

启动：python -m uvicorn web.main:app --port 8000

本文件只做 app 组装与生命周期事件；单例装配在 web/state.py，端点按域在
web/api/*.py（meta/kb/chats/records/debug）。
"""

import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from web.api import chats, debug, kb, meta, records
from web import state as st
from web.state import (  # noqa: F401 —— 单例 re-export：测试与调试脚本从 web.main 取
    BLF_CFG, BUDGET, CFG, COND_INDEX, ENGINE, FRONTEND, KB, LLM, MCP_HUB,
    RECORDS, RECORDS_DB, REPORT_DIR, ROOT, RUNS, STORE, TRACES,
    SafeJSONResponse, _chat_or_404, _find_analysis,
)
from web.api.chats import (  # noqa: F401 —— 端点函数 re-export（旧导入面兼容）
    api_active_run, api_analysis, api_analysis_feedback,
    api_analysis_timeseries, api_create_chat, api_delete_chat, api_get_chat,
    api_list_chats, api_remove_file, api_report_download,
    api_run_stream, api_select, api_send_message, api_send_stream, api_stop_run,
    api_upload, api_upload_by_path,
)
from web.api.debug import api_trace, api_traces, page_debug, page_records  # noqa: F401
from web.api.kb import (  # noqa: F401 —— KB 目录常量定义在 api/kb.py
    KB_ARCHIVE_DIR, KB_MANUAL_DIR, api_kb, api_kb_delete, api_kb_search,
    api_kb_upload,
)
from web.api.meta import (  # noqa: F401
    api_conditions, api_context_stats, api_functions, api_meta, api_mcp,
    api_mcp_call, api_mcp_reconnect, api_sample_download, api_samples,
)
from web.api.records import (  # noqa: F401
    api_record, api_records, api_records_compare, api_records_meta,
    api_record_timeseries,
)

app = FastAPI(
    title="Brake Agent Demo",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    default_response_class=SafeJSONResponse,
)

app.include_router(meta.router)
app.include_router(kb.router)
app.include_router(chats.router)
app.include_router(records.router)
app.include_router(debug.router)


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
         "scripts.generate_demo_mf4"),
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
    # state 活引用：测试会整只替换 MCP_HUB（wst.MCP_HUB = hub），钩子要跟
    if st.MCP_HUB.configured:
        st.MCP_HUB.connect_all()


@app.on_event("shutdown")
def _shutdown_mcp():
    st.MCP_HUB.shutdown()


app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
