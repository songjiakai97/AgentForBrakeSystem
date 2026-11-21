"""Web 层共享单例与常量（design.md §7.2）。

拆 Router 前这里是 1000 行的 main.py：单例装配、常量与跨域 helper 全混在
端点中间。现在 state.py 只负责「装配与共享」，端点按域进 web/api/*.py，
main.py 只做 app 组装与生命周期事件。import 顺序即依赖顺序——api 模块
从这里取单例，state 不回 import api。
"""

import json
import logging
import os
import warnings

warnings.filterwarnings("ignore", module="asammdf")

from fastapi import HTTPException  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from brake_analyzer.agent.cond_index import ConditionIndex  # noqa: E402
from brake_analyzer.agent.engine import ChatEngine  # noqa: E402
from brake_analyzer.configs import (  # noqa: E402
    ConfigError, load_blf_config, load_configs,
)
from brake_analyzer.llm.client import LlmClient, TraceStore  # noqa: E402
from brake_analyzer.llm.context import ContextBudget  # noqa: E402
from brake_analyzer.llm.kb import KnowledgeBase  # noqa: E402
from brake_analyzer.mcp_bridge import McpHub, load_mcp_config  # noqa: E402
from brake_analyzer.records.service import RecordService  # noqa: E402
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
# 报告输出根目录：generate_report 工具写 <REPORT_DIR>/<chat_id>/report_*.docx，
# 下载走 GET /api/reports/{cid}/{name}，删除会话时整棵子目录级联清掉
REPORT_DIR = os.path.join(ROOT, "reports")
# BLF 需要 DBC 解码，给前端做能力提示（缺库时不阻止启动，只在分析该文件时报错）
try:
    import can as _can           # noqa: F401
    import cantools as _cantools  # noqa: F401
    BLF_READY = True
except ImportError:
    BLF_READY = False

# 上传白名单随依赖能力变化：没装 python-can/cantools 时 .blf 不收，避免收了不能分析
ALLOWED_EXT = {".mf4"} | ({".blf"} if BLF_READY else set())

# 单文件上限：全量读进内存，不设限会打爆服务
MAX_UPLOAD_BYTES = 200 * 1024 * 1024


class SafeJSONResponse(JSONResponse):
    """边界防护：内核合法的 NaN/Inf 归一为 null，避免 allow_nan=False 抛错。"""

    def render(self, content) -> bytes:
        from brake_analyzer.schemas import json_safe

        return super().render(json_safe(content))


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
# 会话持久化目录（JSONL 追加日志，启动重放）：SESSION_DIR 可改；默认入 data/（不入库）
STORE = Store(persist_dir=os.getenv("SESSION_DIR",
                                    os.path.join(ROOT, "data", "sessions")))
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
                    cond_index=COND_INDEX, records=RECORDS, report_dir=REPORT_DIR)
# 一轮 = 一个后台任务：与 HTTP 连接解耦，刷新/断网不再丢本轮回复
RUNS = RunManager()


def _chat_or_404(cid: str):
    chat = STORE.get_chat(cid)
    if chat is None:
        raise HTTPException(404, f"对话不存在: {cid}")
    return chat


def _find_analysis(analysis_id: str):
    """在所有 Session 缓存中定位 (AnalysisResult, RunSample)。"""
    for sess in STORE.sessions.values():
        for res in sess.analyses.values():
            for s in res.samples:
                if s.sample_id == analysis_id or s.analysis_id == analysis_id:
                    return res, s
    return None, None
