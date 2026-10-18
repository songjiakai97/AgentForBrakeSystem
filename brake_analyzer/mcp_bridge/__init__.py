"""外部 MCP 接入（design.md §6.11）。

- config.py：读 configs/mcp.yaml（运维白名单，加载期校验）
- registry.py：命名空间、schema 转换、结果压平（纯函数，可脱开异步测）
- hub.py：事件循环线程 + client 池 + 超时/停止口径

SDK 未安装时本包整体降级为「没有外部工具」，内核照常跑（与 openai SDK 同一口径）。
"""

from .config import McpServerSpec, load_mcp_config
from .hub import NO_HUB, HttpTransport, McpHub, ServerState

__all__ = ["McpServerSpec", "load_mcp_config", "McpHub", "NO_HUB",
           "ServerState", "HttpTransport"]
