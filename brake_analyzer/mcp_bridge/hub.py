"""外部 MCP 客户端池（design.md §6.11）。

## 为什么要单独一条线程

编排引擎跑在 `RunManager` 的工作线程里，产出的是**同步生成器**；MCP SDK 只有异步 API。
两条路：

1. 每次调用 `asyncio.run(...)` —— 简单，但 stdio 连接没法跨调用复用（`Client` 的上下文
   一退出子进程就没了），于是每次工具调用都要重起进程 + 重握手，冷启动比调用本身还贵；
2. 一条专用事件循环线程常驻，所有 client 由它拥有，工作线程用
   `run_coroutine_threadsafe` 投协程 —— 连接复用，代价是超时与停止要自己管。

选 2。所以本文件里几处"看着啰嗦"是必须的：每个协程外面包 `wait_for`（对端挂住时
`run_coroutine_threadsafe` 的 future 会永远不完成，工作线程白占着）、调用前先自检
`stop_event`（§6.9 的检查点链在这里断不得）、client 只在事件循环线程上创建与销毁
（anyio 的任务组不允许跨 loop 关闭）。

## 失败口径

外部 server 是**可失败依赖**：连不上、握手超时、工具列表为空，都不影响内核跑通。
所以这里从不向调用方抛异常，只记状态；引擎看到的就是"没有外部工具"。
"""

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .config import McpServerSpec
from .registry import (
    build_external_specs,
    clean_description,
    flatten_result,
    parse_args,
    sanitize_raw,
)

# 连接/列工具/调用三类操作的默认上界（秒）；spec.timeout_sec 取大者
CONNECT_SEC = 15.0
CALL_SEC = 30.0


@dataclass
class ExternalTool:
    server: str
    raw_name: str          # server 侧的真名，调用时用它
    name: str              # 注入 LLM 的命名空间名，由 _rebuild_specs 统一算
    description: str
    input_schema: Any


@dataclass
class ServerState:
    spec: McpServerSpec
    status: str = "pending"      # pending | ok | error | disabled
    detail: str = ""
    tools: List[ExternalTool] = field(default_factory=list)
    connected_at: float = 0.0
    calls: int = 0
    failures: int = 0
    last_error: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            **self.spec.as_dict(),
            "status": self.status,
            "detail": self.detail,
            "tool_count": len(self.tools),
            "tools": [{"name": t.name, "raw": t.raw_name,
                       "description": t.description[:160]} for t in self.tools],
            "calls": self.calls,
            "failures": self.failures,
            "last_error": self.last_error[:300],
            "connected_at": self.connected_at or None,
        }


class HttpTransport:
    """给 streamable-http 加自定义请求头。

    SDK 的 `Client(url)` 不接受 headers（凭据要走 OAuth 或自建 http_client），
    而内网 MCP 服务最常见的形态就是一枚静态 Bearer token。这里包一层传输，
    把 headers 交给 SDK 自己的 `create_mcp_http_client`，不复制任何协议逻辑。
    """

    def __init__(self, url: str, headers: Optional[Dict[str, str]] = None):
        self._url = url
        self._headers = dict(headers or {}) or None
        self._cm = None

    async def __aenter__(self):
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

        self._cm = streamable_http_client(
            self._url, http_client=create_mcp_http_client(self._headers))
        return await self._cm.__aenter__()

    async def __aexit__(self, exc_type, exc, tb):
        return await self._cm.__aexit__(exc_type, exc, tb)


class McpHub:
    """持有若干 MCP client，向引擎提供「工具规格 + 按名字执行」。

    线程模型：`_loop` 是唯一能碰 client 的地方；`_lock` 保护规格缓存、反查表与计数器
    （工作线程读、事件循环线程写）。
    """

    def __init__(self, specs: List[McpServerSpec]):
        self.specs = [s for s in specs or []]
        self._states: Dict[str, ServerState] = {s.name: ServerState(s) for s in self.specs}
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._clients: Dict[str, Any] = {}            # 只在事件循环线程访问
        self._specs_cache: List[dict] = []
        self._map: Dict[str, Tuple[str, str]] = {}    # 外部名 -> (server, raw_name)
        self._started = False
        # 事件循环就绪信号：start() 只保证线程被创建，`_loop` 要等线程真跑起来才赋值。
        # 不等它就投协程会得到"MCP 事件循环未启动"——线程调度比一次函数调用快不了多少，
        # 这个竞态在真实启动路径上几乎必现（连不上却报内部错误，最难查的那种）。
        self._ready = threading.Event()

    # ------------------------------------------------------------ 生命周期
    def start(self) -> bool:
        """拉起事件循环线程并等它就绪。没有任何启用的服务时什么都不做（返回 False）。"""
        if not any(s.enabled for s in self.specs):
            with self._lock:
                for st in self._states.values():
                    if st.status == "pending":
                        st.status, st.detail = "disabled", "配置里 enabled=false"
            return False
        if not self._started:
            self._started = True
            self._thread = threading.Thread(target=self._run_loop, name="mcp-hub",
                                            daemon=True)
            self._thread.start()
        return self._ready.wait(timeout=10.0)

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.run_until_complete(self._close_all())
            finally:
                loop.close()

    def shutdown(self) -> None:
        if self._loop is None or not self._started:
            return
        coro = self._close_all()
        try:
            asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=5)
        except Exception:
            # 投不出去（loop 已停）时协程从未被 await：显式 close()，否则解释器在 GC
            # 时报 "coroutine was never awaited"，把真正的关闭失败盖在警告下面
            coro.close()
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass
        if self._thread is not None:
            # 不等线程退出就无法确认子进程/任务组真的收掉了；收不掉也要继续往下走，
            # 不能让一个第三方进程的残留挡住 web 进程退出
            self._thread.join(timeout=5)

    # ------------------------------------------------------------ 投递
    def _submit(self, coro, timeout: float):
        """把协程投到 hub 线程并等结果；超时/异常抛给调用方兜。

        投递失败时必须 close() 协程：否则解释器只在 GC 时报
        "coroutine was never awaited"，把真正的错误盖在一条警告下面。
        """
        loop = self._loop
        if loop is None:
            coro.close()
            raise RuntimeError("MCP 事件循环未启动")
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:
            coro.close()
            raise
        return fut.result(timeout=timeout)

    def connect_all(self, wait_sec: float = 0.0) -> Dict[str, str]:
        """启动期/手动重连。`wait_sec<=0` 时后台连，不阻塞调用方。

        uvicorn 启动不该被一个没起来的第三方进程拖住（stdio 子进程起不动是常态：
        没装、路径不对、缺 node），所以默认后台；界面用 GET /api/mcp 的 status
        判断"还在连"还是"连不上"（前端 pollMcp 每 3s 看一次，连完再回读 /api/meta）。
        """
        if not self.start():
            self._mark_pending_error("MCP 事件循环线程未能就绪（10s 内）")
            with self._lock:
                return {n: st.status for n, st in self._states.items()}
        if wait_sec > 0:
            try:
                self._submit(self._connect_all(), wait_sec)
            except Exception as e:
                self._mark_pending_error(f"连接超时：{e}")
        else:
            threading.Thread(target=self._connect_blocking, name="mcp-connect",
                             daemon=True).start()
        with self._lock:
            return {n: st.status for n, st in self._states.items()}

    def _connect_budget(self, specs: List[McpServerSpec]) -> float:
        """一轮连接最多该等多久（外层 future 的上界）。

        不能写死 `CONNECT_SEC * n`：`_connect_one` 的预算是 `max(CONNECT_SEC, timeout_sec)`，
        而 MATLAB 那类"首次拉起 MATLAB 才算连上"的 server，timeout_sec 按 §6.11 要配到
        120~300。外层比内层短就会在连接**还在进行中**时把状态打成 error（前端立刻显示
        "未连上"），而协程其实还在跑、几十秒后真的连上了——那是一句会说谎的中间态。
        """
        return sum(max(CONNECT_SEC, s.timeout_sec) for s in specs) + CONNECT_SEC

    def _connect_blocking(self) -> None:
        targets = [s for s in self.specs if s.enabled]
        try:
            self._submit(self._connect_all(), self._connect_budget(targets))
        except Exception as e:
            self._mark_pending_error(f"连接超时：{type(e).__name__}")

    def _mark_pending_error(self, msg: str) -> None:
        with self._lock:
            for st in self._states.values():
                if st.status == "pending":
                    st.status, st.detail = "error", msg[:300]

    def reconnect(self, name: Optional[str] = None) -> Dict[str, str]:
        """重连（某个 server 起来了、或改了配置后手动试）。同步等待，给运维用。"""
        if not self.start():
            with self._lock:
                return {n: st.status for n, st in self._states.items()}
        targets = [s for s in self.specs
                   if s.enabled and (name is None or s.name == name)]
        try:
            # 上界同样按各 server 自己的 timeout_sec 给：手动重连 MATLAB 时
            # 固定 90s 会先于连接完成而返回，运维看到的就是"我点了重连却还 error"
            self._submit(self._reconnect_one(name), self._connect_budget(targets) + CALL_SEC)
        except Exception as e:
            self._mark_pending_error(f"重连超时：{type(e).__name__}")
        with self._lock:
            return {n: st.status for n, st in self._states.items()}

    async def _reconnect_one(self, name: Optional[str]) -> None:
        targets = [s for s in self.specs if name is None or s.name == name]
        for spec in targets:
            if spec.enabled:
                await self._connect_one(spec)
        self._rebuild_specs()

    # ------------------------------------------------------------ 事件循环内
    async def _connect_all(self) -> None:
        for spec in self.specs:
            if not spec.enabled:
                with self._lock:
                    st = self._states[spec.name]
                    st.status, st.detail = "disabled", "配置里 enabled=false"
                continue
            await self._connect_one(spec)
        self._rebuild_specs()

    def _make_client(self, spec: McpServerSpec):
        from mcp import Client

        if spec.transport == "stdio":
            from mcp import StdioServerParameters

            # env 是"额外要给的键"：SDK 自己只并入一份安全白名单（PATH/HOME/...），
            # 不并整进程环境——否则 OPENAI_API_KEY 就进了第三方进程的命令行环境
            params = StdioServerParameters(
                command=spec.command, args=list(spec.args),
                env=dict(spec.env) or None, cwd=spec.cwd or None)
            return Client(params, read_timeout_seconds=spec.timeout_sec)
        return Client(HttpTransport(spec.url, spec.headers),
                      read_timeout_seconds=spec.timeout_sec)

    def _fail(self, st: ServerState, e: Exception, budget: float, doing: str) -> None:
        """把连接期异常写成"运维看得懂的一句话"。

        `asyncio.TimeoutError` 的 str() 是**空的**（实测 detail 只剩 "TimeoutError: "），
        而那恰恰是最常见的一种失败（子进程起来了但不应答握手）——detail 没信息量，
        用户就只能猜是没装、路径错还是端口不通。空消息时补"在做什么、等了多久"。
        """
        msg = str(e).strip()
        if not msg:
            msg = (f"{doing}超过 {budget:g}s 未响应"
                   "（常见原因：可执行文件路径不对、缺运行时、进程起来了但不应答 MCP 握手）")
        with self._lock:
            st.status = "error"
            st.detail = f"{type(e).__name__}: {msg[:260]}"
            st.tools = []

    async def _connect_one(self, spec: McpServerSpec) -> ServerState:
        st = self._states[spec.name]
        await self._drop(spec.name)
        budget = max(CONNECT_SEC, spec.timeout_sec)
        try:
            client = self._make_client(spec)
            entered = await asyncio.wait_for(client.__aenter__(), timeout=budget)
        except Exception as e:
            self._fail(st, e, budget, f"{spec.transport} 握手")
            return st

        tools: List[ExternalTool] = []
        detail = ""
        try:
            res = await asyncio.wait_for(entered.list_tools(), timeout=budget)
            allow, deny = set(spec.tools), set(spec.exclude)
            for t in res.tools:
                if allow and t.name not in allow:
                    continue
                if t.name in deny:
                    continue
                tools.append(ExternalTool(server=spec.name, raw_name=t.name,
                                          name=f"mcp__{spec.name}__{sanitize_raw(t.name)}",
                                          description=clean_description(t.description,
                                                                        spec.desc_chars),
                                          input_schema=t.input_schema))
            detail = f"报出 {len(res.tools)} 个工具，采纳 {len(tools)} 个"
            if allow:
                missing = sorted(allow - {t.raw_name for t in tools})
                if missing:
                    # 白名单点名点不到 = 配置写错或对方改了名。静默少工具最难查，
                    # 直接把没命中的名字写进状态
                    detail += f"；白名单未命中 {missing}"
            with self._lock:
                self._clients[spec.name] = entered
                st.tools = tools
                st.status, st.detail = "ok", detail
                st.connected_at = time.time()
        except Exception as e:
            # 握手成功但列工具失败：留着连接没意义，断开并标错
            try:
                await entered.__aexit__(None, None, None)
            except Exception:
                pass
            self._fail(st, e, budget, "列工具")
        return st

    async def _drop(self, name: str) -> None:
        client = self._clients.pop(name, None)
        if client is None:
            return
        try:
            await asyncio.wait_for(client.__aexit__(None, None, None), timeout=5)
        except Exception:
            pass

    async def _close_all(self) -> None:
        for name in list(self._clients):
            await self._drop(name)
        with self._lock:
            self._specs_cache = []
            self._map = {}

    def _rebuild_specs(self) -> None:
        """汇总所有 server 的工具 → 一份规格表 + 反查表。

        名字在这里全局算（而不是各 server 各算），因为 `build_external_specs` 的
        去重序号只在跨 server 视角下才有意义：两个 server 都有 `get_file` 时，
        清洗后的名字会撞，必须看到彼此才能编成 _2。
        引擎每轮直接读缓存，不付异步成本。
        """
        with self._lock:
            states = [st for st in self._states.values() if st.status == "ok"]
            rows = [(st.spec.name, t.raw_name, t.description, t.input_schema)
                    for st in states for t in st.tools]
        # desc_chars=None：描述已在连接期按各 server 的额度清洗过，不在这里二次截断
        specs, mapping = build_external_specs(rows, desc_chars=None)
        # build_external_specs 按入参顺序产出规格（dict 保持插入序），因此可以
        # 按位置回填工具名；按 (server, raw) 反查在"同一 server 报了同名工具"时
        # 会把两个工具都指到同一个名字上
        names = list(mapping)
        i = 0
        with self._lock:
            for st in states:
                for t in st.tools:
                    if i < len(names):
                        t.name = names[i]
                        i += 1
            self._specs_cache = specs
            self._map = mapping

    # ------------------------------------------------------------ 对外读
    @property
    def configured(self) -> bool:
        return bool(self.specs)

    @property
    def enabled(self) -> bool:
        return any(s.enabled for s in self.specs)

    def tool_specs(self) -> List[dict]:
        with self._lock:
            return list(self._specs_cache)

    def names(self) -> List[str]:
        with self._lock:
            return sorted(self._map)

    def is_external(self, name: str) -> bool:
        with self._lock:
            return name in self._map

    def status(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [st.as_dict() for st in self._states.values()]

    def summary(self) -> Dict[str, Any]:
        st = self.status()
        return {
            "configured": len(st),
            "ok": sum(1 for s in st if s["status"] == "ok"),
            "tools": sum(s["tool_count"] for s in st),
            "external_names": self.names(),
            "servers": st,
        }

    # ------------------------------------------------------------ 执行
    def call(self, name: str, args: Any, timeout: Optional[float] = None,
             stop_event=None) -> Dict[str, Any]:
        """执行一个外部工具，返回可直接回灌模型的 dict（**永不抛异常**）。

        引擎只要求"给我一个 payload"：外部失败必须变成模型读得懂的错误说明，
        而不是把整轮打成 LLM 失败——那会触发回退离线路由，凭空多出一段离线结论。
        """
        with self._lock:
            target = self._map.get(name)
            st = self._states.get(target[0]) if target else None
            spec = st.spec if st else None
        if target is None or spec is None or st is None:
            return {"ok": False, "source": "mcp",
                    "note": f"外部工具 {name} 未注册（服务可能已断开）"}
        server, raw = target
        if self._stopped(stop_event):
            # §6.9 的检查点：引擎在调用前已自检，这里是第二道（并发下事件可能刚置位）
            return {"ok": False, "source": "mcp", "stopped": True,
                    "note": "用户已停止本轮，外部工具未执行"}
        parsed, err = parse_args(args)
        if err:
            return {"ok": False, "source": "mcp", "note": f"参数不合法：{err}"}
        budget = float(timeout or max(CALL_SEC, spec.timeout_sec))
        # 亚秒级预算（测试里常见）用 0.5s 这种写法，: .0f 会印成"超过 0s"
        shown = f"{budget:g}"
        try:
            content, structured, is_error = self._submit(
                self._call_one(server, raw, parsed), budget)
        except TimeoutError:
            self._bump(st, failed=True, why=f"调用超时（>{shown}s）")
            # 被放弃的协程仍挂在 hub 线程上（SDK 的 read_timeout 会自己收尾）。
            # 这里不等它，但要把"外部可能还在跑"写进回执，别让模型以为可以重试
            return {"ok": False, "source": "mcp", "timeout": True,
                    "note": f"外部工具 {raw} 超过 {shown}s 未返回，已放弃等待；"
                            "服务端可能仍在执行，不要自动重试"}
        except Exception as e:
            self._bump(st, failed=True, why=str(e))
            return {"ok": False, "source": "mcp",
                    "note": f"外部工具调用失败：{type(e).__name__}: {str(e)[:200]}"}
        # 传输成功 ≠ 工具成功：is_error 是 server 自己说"这次没做成"，
        # 计入失败数，否则 /api/mcp 上一片绿、用户却一直在拿错误原文
        self._bump(st, failed=bool(is_error),
                   why=str(content)[:300] if is_error else "")
        return flatten_result(content, structured, is_error)

    def _bump(self, st: ServerState, failed: bool, why: str = "") -> None:
        with self._lock:
            if failed:
                st.failures += 1
                if why:
                    st.last_error = why[:300]
            else:
                st.calls += 1

    async def _call_one(self, server: str, raw: str, args: Dict[str, Any]):
        client = self._clients.get(server)
        if client is None:
            raise RuntimeError(f"服务 {server} 未连接")
        r = await client.call_tool(raw, args or None)
        return r.content, getattr(r, "structured_content", None), bool(r.is_error)

    @staticmethod
    def _stopped(stop_event) -> bool:
        return stop_event is not None and stop_event.is_set()


# 未配置任何外部服务时的空实现：引擎不必为"有没有 MCP"写分支
NO_HUB = McpHub([])
