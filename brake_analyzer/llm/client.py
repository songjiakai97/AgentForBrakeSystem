"""OpenAI Compatible 客户端封装（design.md §6.8 / §12）。

- base_url / api_key / model 来自环境变量（.env）。
- 无 Key → available=False，全部调用方自行降级。
- 每次 API 请求产出一条 trace（由注入的 TraceStore 记录，可为 None）。
- openai SDK 为可选依赖：未安装时 available=False。
"""

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

try:
    from openai import OpenAI
except Exception:  # SDK 未安装 → 离线模式
    OpenAI = None


# 流式请求的读超时（秒）：按「相邻两个数据块之间的最大间隔」计，不限制整轮总时长。
# 端点挂住不吐字节时，SDK 的读循环阻塞在 socket 上，任何停止标记都自检不到——
# 没有这个上界，那一种「点了停止没反应」永远无解，会话还会一直被这一轮占着（409）。
STREAM_READ_TIMEOUT_SEC = 60.0

# 非流式请求的整轮超时（秒）：chat() 用于摘要与标定建议，都在工具/分析阶段中间，
# 阻塞期间没有任何停止检查点，所以必须有上界（openai 默认是 10 分钟，太久了）。
REQUEST_TIMEOUT_SEC = 120.0


@dataclass
class LlmReply:
    content: str = ""
    tool_calls: List[dict] = field(default_factory=list)   # {id,name,arguments(dict)}
    finish_reason: str = "stop"
    error: Optional[str] = None
    reasoning: str = ""    # 思维链（部分兼容端点提供 reasoning_content）
    # tool_calls 里 arguments JSON 解析失败的 id 集合：引擎据此不执行该调用，
    # 改为把「参数解析失败」回喂给模型自纠，而不是拿空参数去跑工具
    bad_args: List[str] = field(default_factory=list)
    # 本轮 token 用量（端点提供时）：{prompt_tokens, completion_tokens, total_tokens, elapsed_ms}
    usage: Optional[dict] = None


def _usage_of(usage_obj, t0: float) -> dict:
    """usage 对象（或 None）→ trace 用量字典；至少给出耗时。"""
    out = {"elapsed_ms": int((time.time() - t0) * 1000)}
    if usage_obj is not None:
        out.update({"prompt_tokens": getattr(usage_obj, "prompt_tokens", None)
                    if not isinstance(usage_obj, dict) else usage_obj.get("prompt_tokens"),
                    "completion_tokens": getattr(usage_obj, "completion_tokens", None)
                    if not isinstance(usage_obj, dict) else usage_obj.get("completion_tokens"),
                    "total_tokens": getattr(usage_obj, "total_tokens", None)
                    if not isinstance(usage_obj, dict) else usage_obj.get("total_tokens")})
    return out


class TraceStore:
    """内存 trace 环形存储（§12）。离线路由不写 trace。"""

    def __init__(self, cap: int = 500):
        self.cap = cap
        self._traces: List[dict] = []

    def add(self, trace: dict) -> str:
        trace.setdefault("trace_id", "tr_" + uuid.uuid4().hex[:8])
        trace.setdefault("ts", time.time())
        self._traces.append(trace)
        if len(self._traces) > self.cap:
            self._traces = self._traces[-self.cap:]
        return trace["trace_id"]

    def list(self, chat_id: Optional[str] = None, limit: int = 100) -> List[dict]:
        items = self._traces
        if chat_id:
            items = [t for t in items if t.get("chat_id") == chat_id]
        return list(reversed(items[-limit:]))

    def get(self, tid: str) -> Optional[dict]:
        for t in self._traces:
            if t.get("trace_id") == tid:
                return t
        return None

    def remove_chat(self, chat_id: str) -> None:
        self._traces = [t for t in self._traces if t.get("chat_id") != chat_id]


class LlmClient:
    def __init__(self, traces: Optional[TraceStore] = None):
        self.base_url = os.getenv("OPENAI_BASE_URL", "").strip()
        self.api_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.model = os.getenv("OPENAI_MODEL", "").strip() or "gpt-4o-mini"
        self.traces = traces
        self.available = bool(self.api_key) and OpenAI is not None
        self._client = None
        # embedding 端点可以和主对话不同源：只配 OPENAI_EMBED_MODEL 时回落主
        # BASE_URL/KEY（本地 LM Studio 场景）；OpenAI 官方则三者都配。切换只动
        # 环境变量，调用方代码不分支。
        self.embed_base_url = os.getenv("OPENAI_EMBED_BASE_URL", "").strip() or self.base_url
        self.embed_api_key = os.getenv("OPENAI_EMBED_API_KEY", "").strip() or self.api_key
        self.embed_model = os.getenv("OPENAI_EMBED_MODEL", "").strip()
        self._embed_client = None
        # 端点可达性实测缓存：available 只是"配置齐了"，端点挂没挂要 ping 过才知道。
        # 结果缓存 30s——meta 页轮询每次都 ping 会把小端点打爆
        self._reach_cached = False
        self._reach_until = 0.0
        if self.available:
            kwargs = {"api_key": self.api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            try:
                self._client = OpenAI(**kwargs)
            except Exception:
                self.available = False

    def check_reachable(self, force: bool = False) -> bool:
        """实测端点可达性（models 列表），结果缓存 30s；未配置直接 False 不发请求。

        「可达」= 端点活着，不等于 /models 一定实现：不少 OpenAI 兼容网关
        （中转/自建代理）只实现 chat/completions，GET /models 返回 404/405，
        但对话完全正常。旧实现把所有异常都判成不可达，顶栏就会出现「能聊却
        显示不可达」。这里按错误类型区分：
        - 连接/超时/DNS（无 status_code）→ 不可达；
        - 401/403 → 鉴权不通，chat 同样会失败 → 不可达；
        - 5xx → 服务端故障 → 不可达；
        - 其余 4xx（404/405 = 未实现 models，400 = 参数被拒）→ 端点活着 → 可达。
        """
        if not self.available:
            return False
        now = time.time()
        if not force and now < self._reach_until:
            return self._reach_cached
        try:
            # 探活不该把 /api/meta 卡住：给硬超时且不重试
            list(self._client.with_options(timeout=5.0, max_retries=0).models.list())
            ok = True
        except Exception as e:
            status = getattr(e, "status_code", None)
            ok = status is not None and status not in (401, 403) and status < 500
        self._reach_cached = ok
        self._reach_until = now + 30.0
        return ok

    # ------------------------------------------------------------ embedding
    @property
    def embed_available(self) -> bool:
        return bool(self.embed_model) and OpenAI is not None and bool(self.embed_api_key)

    def embed(self, texts: List[str]) -> Optional[List[List[float]]]:
        """向量化一批文本；未配置或失败返回 None，调用方退回关键词检索。

        失败不抛异常：向量检索是锦上添花，端点挂了不该阻塞分析主链路。
        """
        if not self.embed_available:
            return None
        try:
            if self._embed_client is None:
                kwargs = {"api_key": self.embed_api_key}
                if self.embed_base_url:
                    kwargs["base_url"] = self.embed_base_url
                self._embed_client = OpenAI(**kwargs)
            resp = self._embed_client.embeddings.create(
                model=self.embed_model, input=list(texts))
            return [d.embedding for d in resp.data]
        except Exception:
            self._embed_client = None
            return None

    # ------------------------------------------------------------ chat
    def chat(
        self,
        messages: List[dict],
        tools: Optional[List[dict]] = None,
        chat_id: str = "",
        user_text: str = "",
        round_no: int = 1,
        temperature: float = 0.2,
        max_tokens: int = 1600,
    ) -> LlmReply:
        if not self.available:
            return LlmReply(error="llm_unavailable")
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools or None,
                temperature=temperature,
                max_tokens=max_tokens,
                # 非流式调用（摘要、标定建议）都夹在工具/分析阶段中间，阻塞期间
                # 一个停止检查点都没有；SDK 默认 10 分钟太久，这里给硬上界
                timeout=REQUEST_TIMEOUT_SEC,
            )
            # 解析必须与请求同一个 try：端点返回 choices=None/空（如 base_url 配错、
            # 兼容端点 quirks）时这里的 TypeError 会炸穿到引擎，被吞成"静默回退离线"
            msg = resp.choices[0].message
            finish = resp.choices[0].finish_reason or "stop"
            tool_calls = []
            bad_args = []
            for tc in msg.tool_calls or []:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except Exception:
                    args = {}
                    bad_args.append(tc.id)
                tool_calls.append({"id": tc.id, "name": tc.function.name, "arguments": args})
            reasoning = (getattr(msg, "reasoning_content", None)
                         or getattr(msg, "reasoning", None) or "")
            reply = LlmReply(content=msg.content or "", tool_calls=tool_calls,
                             finish_reason=finish, reasoning=reasoning,
                             bad_args=bad_args, usage=_usage_of(getattr(resp, "usage", None), t0))
        except Exception as e:
            self._trace(messages, None, [], chat_id, user_text, round_no, error=str(e))
            return LlmReply(error=f"llm_error: {e}")
        self._trace(
            messages,
            {"content": reply.content, "tool_calls": reply.tool_calls,
             "usage": reply.usage, "bad_args": reply.bad_args},
            tool_calls, chat_id, user_text, round_no, finish=finish,
        )
        return reply

    # ------------------------------------------------------------ 流式 chat
    def chat_stream(self, messages: List[dict], tools: Optional[List[dict]] = None,
                    chat_id: str = "", user_text: str = "", round_no: int = 1,
                    temperature: float = 0.2, max_tokens: int = 1600,
                    stop_event=None):
        """流式对话：yield 事件 dict，最后必发一个 {"type":"finish","reply":LlmReply}。

        事件：
        - {"type":"delta","channel":"content"|"reasoning","text":str}
        - {"type":"finish","reply":LlmReply}   # reply.error 非空表示失败

        stop_event：用户按「停止」时置位。这里必须自己逐块检查——工具调用的参数是
        一个字符一个字符流回来的，不产生 delta，外层引擎的「每个 delta 自检」
        在这段完全没有检查点，于是要等整段工具参数（甚至整轮回答）跑完才停手，
        这就是「点了停止没反应、过好久才停止」。命中后立刻 break 并 close() 断流，
        以 error="llm_stopped" 收尾（调用方据此判定为停止，不是失败）。
        """
        if not self.available:
            yield {"type": "finish", "reply": LlmReply(error="llm_unavailable")}
            return
        try:
            t0 = time.time()
            stream = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools or None,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                # 末块带 usage：每轮 token 用量进 trace，调上下文预算的实证依据。
                # 个别兼容端点不认 stream_options 会 400——LLM_STREAM_USAGE=0 可关。
                **({"stream_options": {"include_usage": True}}
                   if os.getenv("LLM_STREAM_USAGE", "1").strip() == "1" else {}),
                # read 超时按「两个块之间的最大间隔」计，不限制总时长：块与块之间
                # 长期静默（端点挂住）时让这一轮有界失败，而不是永远等下去——
                # 那时停止标记也无从自检，用户只会看到「点了停止没反应」。
                timeout=STREAM_READ_TIMEOUT_SEC,
            )
        except Exception as e:
            self._trace(messages, None, [], chat_id, user_text, round_no, error=str(e))
            yield {"type": "finish", "reply": LlmReply(error=f"llm_error: {e}")}
            return

        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        calls: dict = {}          # index -> {id,name,arguments}
        finish = "stop"
        stopped = False
        stream_usage: Optional[dict] = None
        try:
            for chunk in stream:
                # usage 块没有 choices，先收下来再 continue
                u = getattr(chunk, "usage", None)
                if u is not None:
                    stream_usage = {"prompt_tokens": getattr(u, "prompt_tokens", None),
                                    "completion_tokens": getattr(u, "completion_tokens", None),
                                    "total_tokens": getattr(u, "total_tokens", None)}
                # 每个块都是一次自检机会：工具参数、思维链空白期、首 token 之前都覆盖到
                if stop_event is not None and stop_event.is_set():
                    stopped = True
                    break
                if not getattr(chunk, "choices", None):
                    continue
                ch = chunk.choices[0]
                if ch.finish_reason:
                    finish = ch.finish_reason
                delta = ch.delta
                text = getattr(delta, "content", None)
                if text:
                    content_parts.append(text)
                    yield {"type": "delta", "channel": "content", "text": text}
                rc = getattr(delta, "reasoning_content", None)
                if not rc:
                    extra = getattr(delta, "model_extra", None) or {}
                    rc = extra.get("reasoning") or extra.get("reasoning_content")
                if rc:
                    reasoning_parts.append(rc)
                    yield {"type": "delta", "channel": "reasoning", "text": rc}
                for tc in getattr(delta, "tool_calls", None) or []:
                    slot = calls.setdefault(
                        tc.index if tc.index is not None else len(calls),
                        {"id": "", "name": "", "args": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            slot["name"] += tc.function.name
                        if tc.function.arguments:
                            slot["args"] += tc.function.arguments
        except Exception as e:  # 中断的流：已完成部分随 error 一并上报
            self._trace(messages, None, [], chat_id, user_text, round_no, error=str(e))
            yield {"type": "finish", "reply": LlmReply(error=f"llm_stream_error: {e}")}
            return
        finally:
            # 断掉 HTTP 流：上层 break 出这个生成器时（外层也是靠 refcount 才 close，
            # 未必立刻发生），留着连接只是白占端点的并发额度
            try:
                closer = getattr(stream, "close", None)
                if closer is not None:
                    closer()
            except Exception:
                pass

        if stopped or (stop_event is not None and stop_event.is_set()):
            # 停止单独一条 error 通道：调用方要把它和「端点出错」分开处理——
            # 停止不回退离线路由，失败要回退（§6.9）。
            # 循环自然跑完时才置位的也要认：那一轮的半截工具参数已经拼好了，
            # 当成正常回复返回就会被上层立刻执行——停止又慢了一整步。
            self._trace(messages, None, [], chat_id, user_text, round_no, error="stopped")
            yield {"type": "finish", "reply": LlmReply(error="llm_stopped")}
            return

        tool_calls = []
        bad_args = []
        for i in sorted(calls):
            slot = calls[i]
            if not slot["name"]:
                continue
            try:
                args = json.loads(slot["args"] or "{}")
            except Exception:
                args = {}
                bad_args.append(slot["id"] or f"call_{i}")
            tool_calls.append({"id": slot["id"] or f"call_{i}",
                               "name": slot["name"], "arguments": args})
        usage = _usage_of(stream_usage, t0)
        reply = LlmReply(content="".join(content_parts), tool_calls=tool_calls,
                         finish_reason=finish, reasoning="".join(reasoning_parts),
                         bad_args=bad_args, usage=usage)
        self._trace(messages,
                    {"content": reply.content, "tool_calls": reply.tool_calls,
                     "usage": reply.usage, "bad_args": reply.bad_args},
                    tool_calls, chat_id, user_text, round_no, finish=finish)
        yield {"type": "finish", "reply": reply}

    def _trace(self, request, reply, tool_execs, chat_id, user_text, round_no,
               finish="stop", error=None):
        if self.traces is None:
            return
        t = {
            "chat_id": chat_id,
            "user_text": user_text,
            "model": self.model,
            "round": round_no,
            "finish_reason": finish if error is None else "error",
            "request": request,
            "reply": reply,
            "tools": tool_execs,
        }
        # usage / bad_args 提到顶层：/debug 的行内展示直接读，不用钻 reply
        if isinstance(reply, dict):
            if reply.get("usage"):
                t["usage"] = reply["usage"]
            if reply.get("bad_args"):
                t["bad_args"] = reply["bad_args"]
        if error:
            t["error"] = error
        self.traces.add(t)

    # ------------------------------------------------------------ 简单文本生成
    def complete_json(self, system: str, user: str, chat_id: str = "",
                      user_text: str = "", max_tokens: int = 8192) -> Optional[dict]:
        # 8192：思维链模型要先推理再写 JSON，4096 常在中途截断（finish=length）
        # 导致 JSON 解析失败——宁可多等，不可半截
        r = self.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            chat_id=chat_id, user_text=user_text, temperature=0.1,
            max_tokens=max_tokens,
        )
        if r.error:
            return None
        text = (r.content or "").strip()
        if not text and r.reasoning:
            # 思维链模型常把 max_tokens 全花在推理上、content 为空：
            # 从思维链里抠 JSON（结论通常在思考末尾成形）
            text = r.reasoning.strip()
        if text.startswith("```"):
            text = strip_fence(text)
        try:
            return json.loads(text)
        except Exception:
            i, j = text.find("{"), text.rfind("}")
            if 0 <= i < j:
                try:
                    return json.loads(text[i:j + 1])
                except Exception:
                    return None
            return None


def strip_fence(text: str) -> str:
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("```")]
    return "\n".join(lines)
