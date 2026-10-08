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


@dataclass
class LlmReply:
    content: str = ""
    tool_calls: List[dict] = field(default_factory=list)   # {id,name,arguments(dict)}
    finish_reason: str = "stop"
    error: Optional[str] = None
    reasoning: str = ""    # 思维链（部分兼容端点提供 reasoning_content）


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
        if self.available:
            kwargs = {"api_key": self.api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            try:
                self._client = OpenAI(**kwargs)
            except Exception:
                self.available = False

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
            )
        except Exception as e:
            self._trace(messages, None, [], chat_id, user_text, round_no, error=str(e))
            return LlmReply(error=f"llm_error: {e}")

        msg = resp.choices[0].message
        finish = resp.choices[0].finish_reason or "stop"
        tool_calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
            tool_calls.append({"id": tc.id, "name": tc.function.name, "arguments": args})
        reply = LlmReply(content=msg.content or "", tool_calls=tool_calls, finish_reason=finish)
        self._trace(
            messages,
            {"content": reply.content, "tool_calls": reply.tool_calls},
            tool_calls, chat_id, user_text, round_no, finish=finish,
        )
        return reply

    # ------------------------------------------------------------ 流式 chat
    def chat_stream(self, messages: List[dict], tools: Optional[List[dict]] = None,
                    chat_id: str = "", user_text: str = "", round_no: int = 1,
                    temperature: float = 0.2, max_tokens: int = 1600):
        """流式对话：yield 事件 dict，最后必发一个 {"type":"finish","reply":LlmReply}。

        事件：
        - {"type":"delta","channel":"content"|"reasoning","text":str}
        - {"type":"finish","reply":LlmReply}   # reply.error 非空表示失败
        """
        if not self.available:
            yield {"type": "finish", "reply": LlmReply(error="llm_unavailable")}
            return
        try:
            stream = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools or None,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
            )
        except Exception as e:
            self._trace(messages, None, [], chat_id, user_text, round_no, error=str(e))
            yield {"type": "finish", "reply": LlmReply(error=f"llm_error: {e}")}
            return

        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        calls: dict = {}          # index -> {id,name,arguments}
        finish = "stop"
        try:
            for chunk in stream:
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

        tool_calls = []
        for i in sorted(calls):
            slot = calls[i]
            if not slot["name"]:
                continue
            try:
                args = json.loads(slot["args"] or "{}")
            except Exception:
                args = {}
            tool_calls.append({"id": slot["id"] or f"call_{i}",
                               "name": slot["name"], "arguments": args})
        reply = LlmReply(content="".join(content_parts), tool_calls=tool_calls,
                         finish_reason=finish, reasoning="".join(reasoning_parts))
        self._trace(messages,
                    {"content": reply.content, "tool_calls": reply.tool_calls},
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
        if error:
            t["error"] = error
        self.traces.add(t)

    # ------------------------------------------------------------ 简单文本生成
    def complete_json(self, system: str, user: str, chat_id: str = "",
                      user_text: str = "") -> Optional[dict]:
        r = self.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            chat_id=chat_id, user_text=user_text, temperature=0.1,
        )
        if r.error or not r.content:
            return None
        text = r.content.strip()
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
