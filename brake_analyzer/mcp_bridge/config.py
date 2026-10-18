"""外部 MCP 服务配置（design.md §6.11）。

配置文件 configs/mcp.yaml 由**运维**写，不是用户输入：里面是"可以往这台机器上挂什么"
的白名单（命令、URL、透传的环境变量），所以它不能通过 API 修改，也不接受前端传参。

三条刻意的约束，都是被这个仓库已有的口径逼出来的：
- **不并整进程环境**：`env` 写的是**额外**要给的键。stdio 子进程的实际环境是 SDK 的
  `get_default_environment()`（一份安全白名单：POSIX 为 HOME/LOGNAME/PATH/SHELL/TERM/USER）
  并上这里列的键。所以 PATH 默认就有——MATLAB 这类靠 PATH 找可执行文件的 server 不会被
  本实现坑到；但本进程的 OPENAI_API_KEY 不会漏给第三方 server。要给别人
  （MW_MCP_SERVER_*、LM_LICENSE_FILE）必须在这里点名。
- **name 是命名空间的一部分**：外部工具最终叫 `mcp__<name>__<tool>`，所以 name 限制为
  `[a-z0-9][a-z0-9_-]{0,31}`，避免拼出歧义名字（内部四个工具名不含 `__`，天然不冲突）。
- **加载期就拒**：与 load_configs 同一风格——结构非法直接 ConfigError 并说明原因，
  不留到运行期"静默少了一批工具"。少工具是可感知的能力退化，拼错 key 不是。
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 与 configs.py 同一异常类型：调用方（web/main.py）按 ConfigError 拒绝启动
from ..configs import ConfigError

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
TRANSPORTS = ("stdio", "http")
# 单次外部工具调用的硬上界（秒）。理由同 §6.9 的 HTTP 超时：一次阻塞调用期间
# 没有任何停止检查点，没有上界就是"点了停止没反应"的又一个来源。
MAX_TIMEOUT_SEC = 300.0


@dataclass
class McpServerSpec:
    name: str
    transport: str = "stdio"
    enabled: bool = True
    command: str = ""
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    timeout_sec: float = 20.0
    # 白名单：空 = 该 server 报什么工具就接什么；非空 = 只接这些（原始名）
    tools: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)
    # 外部工具描述注入 prompt 的字数上限（描述是不可信内容，见 §15）
    desc_chars: int = 400

    def as_dict(self) -> Dict[str, Any]:
        """给 /api/mcp 用：**不含** env/headers 的值，只含键名。

        配置里写的可能是凭据（Authorization: Bearer ...），调试接口回显值就等于
        把凭据贴到浏览器里；键名足够定位"哪个 header 没生效"。
        """
        return {
            "name": self.name,
            "transport": self.transport,
            "enabled": self.enabled,
            "command": self.command or None,
            "args": self.args,
            "env_keys": sorted(self.env),
            "url": self.url or None,
            "header_keys": sorted(self.headers),
            "timeout_sec": self.timeout_sec,
            "tools": self.tools,
            "exclude": self.exclude,
        }


def _as_dict_str(raw: Any, fname: str, where: str) -> Dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{fname} {where} 必须是映射（键值都是字符串）")
    out = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ConfigError(f"{fname} {where}.{k} 的键与值都必须是字符串")
        out[k] = v
    return out


def _as_str_list(raw: Any, fname: str, where: str) -> List[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or any(not isinstance(x, str) for x in raw):
        raise ConfigError(f"{fname} {where} 必须是字符串列表")
    return list(raw)


def _parse_one(entry: Any, fname: str, idx: int) -> McpServerSpec:
    where = f"mcp_servers[{idx}]"
    if not isinstance(entry, dict):
        raise ConfigError(f"{fname} {where} 必须是映射")
    known = {"name", "transport", "enabled", "command", "args", "env", "cwd",
             "url", "headers", "timeout_sec", "tools", "exclude", "desc_chars"}
    unknown = sorted(set(entry) - known)
    if unknown:
        raise ConfigError(f"{fname} {where} 不支持的键 {unknown}（可用：{sorted(known)}）")

    name = entry.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ConfigError(
            f"{fname} {where}.name 必须匹配 [a-z0-9][a-z0-9_-]{{0,31}}（它要做工具名前缀）")

    transport = entry.get("transport", "stdio")
    if transport not in TRANSPORTS:
        raise ConfigError(f"{fname} {where}.transport 只能是 {TRANSPORTS}，当前 {transport!r}")

    enabled = entry.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{fname} {where}.enabled 必须是布尔值")

    try:
        timeout_sec = float(entry.get("timeout_sec", 20.0))
    except (TypeError, ValueError):
        raise ConfigError(f"{fname} {where}.timeout_sec 必须是数字")
    if not 0.5 <= timeout_sec <= MAX_TIMEOUT_SEC:
        raise ConfigError(
            f"{fname} {where}.timeout_sec 必须在 0.5~{MAX_TIMEOUT_SEC} 之间"
            "（外部调用阻塞期间没有停止检查点，不设上界等于允许卡死一轮）")

    desc_chars = entry.get("desc_chars", 400)
    if not isinstance(desc_chars, int) or not 40 <= desc_chars <= 2000:
        raise ConfigError(f"{fname} {where}.desc_chars 必须是 40~2000 的整数")

    command = entry.get("command", "") or ""
    url = entry.get("url", "") or ""
    if transport == "stdio":
        if not command.strip():
            raise ConfigError(f"{fname} {where}.command 不能为空（stdio 要起子进程）")
    else:
        if not url.startswith(("http://", "https://")):
            raise ConfigError(f"{fname} {where}.url 必须是 http(s):// 开头的地址")

    return McpServerSpec(
        name=name,
        transport=transport,
        enabled=enabled,
        command=command.strip(),
        args=_as_str_list(entry.get("args"), fname, f"{where}.args"),
        env=_as_dict_str(entry.get("env"), fname, f"{where}.env"),
        cwd=str(entry.get("cwd") or ""),
        url=url.strip(),
        headers=_as_dict_str(entry.get("headers"), fname, f"{where}.headers"),
        timeout_sec=timeout_sec,
        tools=_as_str_list(entry.get("tools"), fname, f"{where}.tools"),
        exclude=_as_str_list(entry.get("exclude"), fname, f"{where}.exclude"),
        desc_chars=desc_chars,
    )


def load_mcp_config(path: Optional[str]) -> List[McpServerSpec]:
    """读 configs/mcp.yaml；文件不存在 = 没有外部工具（不是错误）。

    缺失与非法要分开：没配是正常部署形态（离线单机跑），配错了必须当场说，
    否则用户只会觉得"Agent 怎么突然不会用那个工具了"。
    """
    if not path:
        return []
    import os

    import yaml

    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"mcp.yaml 解析失败：{e}")
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise ConfigError("mcp.yaml 顶层必须是映射（mcp_servers: [...]）")
    unknown = sorted(set(raw) - {"mcp_servers"})
    if unknown:
        raise ConfigError(f"mcp.yaml 不支持顶层段 {unknown}（只认 mcp_servers）")
    entries = raw.get("mcp_servers")
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise ConfigError("mcp.yaml 的 mcp_servers 必须是列表")

    specs = [_parse_one(e, "mcp.yaml", i) for i, e in enumerate(entries)]
    seen = set()
    for s in specs:
        if s.name in seen:
            raise ConfigError(f"mcp.yaml 有重名服务 {s.name!r}（工具名前缀会撞车）")
        seen.add(s.name)
    dup = sorted({t for s in specs for t in s.tools if s.tools.count(t) > 1})
    if dup:
        raise ConfigError(f"mcp.yaml tools 列表内有重复项 {dup}")
    return specs
