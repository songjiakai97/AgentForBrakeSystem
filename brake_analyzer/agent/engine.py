"""对话式编排引擎（design.md §6.9 / §6.10 / §13）。

- 所有对话经 ChatEngine 编排，统一走事件流：delta（推理/正文流式）→ message（落库）→ done。
- 有 OPENAI_API_KEY → LLM tool-calling（≤6 轮，流式），失败自动回退离线路由。
  目标解析走 §13 渐进披露：功能层常驻 system prompt，工况层由 resolve_condition /
  list_conditions 按需注入，未填满唯一键一律交用户确认。
- 无 Key → 离线确定性路由（规则打分两跳推荐 + 规则结论 + 可选模板建议）。
- 路由/工具/分析过程落库为 kind=steps 消息，前端折叠展示。
- 送给 LLM 的 messages 先过 ContextCompressor：超过 CONTEXT_COMPRESS_TOKENS 就把
  早期轮次折叠为摘要，并在步骤里显示压缩前后的估算 token；一次性清单/候选结果
  （transient）在离开最近窗口后压成一行存根，只保留有效内容。
"""

import json
import logging
import os
import re
import threading
from typing import Dict, List, Optional, Tuple

from ..agent.tools import list_conditions, recommend_targets, resolve_condition
from ..configs import AppConfigs
from ..llm.client import LlmClient
from ..llm.context import ContextBudget, ContextCompressor
from ..llm.kb import KnowledgeBase, SNIPPET_MIN_CHARS, CUT_NOTE_MAX_CHARS
from ..llm.prompts import (
    TOOL_PAYLOAD_CLIP_CHARS,
    EXTERNAL_TOOL_NOTE,
    MISSING_REASON_LEGEND,
    build_chat_messages,
    build_tool_specs,
    digest_from_analysis,
    digest_line,
    digest_payload,
    reasons_present,
    summarize_request,
    suggestion_prompt,
)
from ..mcp_bridge import NO_HUB
from ..pipeline import NoDataError, analyze_signals
from ..records.query import records_compare, records_query
from ..schemas import AnalysisResult

log = logging.getLogger(__name__)


class ChatEngine:
    def __init__(self, cfg: AppConfigs, llm: LlmClient, kb: KnowledgeBase, store,
                 budget: Optional[ContextBudget] = None, blf_cfg=None, mcp=None,
                 cond_index=None, records=None):
        self.cfg = cfg
        # 试验记录库服务（§17.5）；None → 记录工具明确回复「未启用」，不静默失败
        self.records = records
        self.llm = llm
        self.kb = kb
        self.store = store
        # BLF 侧配置（signals_blf.yaml 校验结果）；None 时 .blf 文件按"未启用 BLF"报错
        self.blf_cfg = blf_cfg
        # 工况语义索引（唯一键零命中时的召回层）；None → resolve 不做语义召回
        self.cond_index = cond_index
        # 外部 MCP 工具池（§6.11）。None → NO_HUB：没配 mcp.yaml 时规格为空，
        # 编排路径与接入前完全一致，不额外写分支
        self.mcp = mcp or NO_HUB
        # Loader 按扩展名复用：BLFLoader 构造时要解析 DBC，逐文件新建是纯浪费
        self._loaders: Dict[str, object] = {}
        self.budget = budget or ContextBudget.from_env()
        # 摘要子请求要知道是哪个会话（trace 按 chat_id 过滤），但多个会话并行跑线程
        self._tls = threading.local()
        self.compressor = ContextCompressor(
            self.budget,
            summarizer=self._summarize_with_llm if llm.available else None,
        )

    def _summarize_with_llm(self, transcript: str, budget_tokens: int) -> str:
        """压缩器注入的摘要回调；摘要请求失败时由压缩器退化到抽取式。"""
        if self._stopped(self._tls_stop):
            # 摘要是一次非流式请求，阻塞期间没有任何检查点：已被要求停止就走
            # 抽取式兜底（纯本地，不花钱也不等），让本轮尽快收尾
            return ""
        chat = getattr(self._tls, "chat", None)
        if chat is not None:
            self._progress(chat, "上下文压缩：正在生成摘要（LLM）",
                           "非流式请求，耗时取决于模型速度", "run")
        r = self.llm.chat(
            summarize_request(transcript, budget_tokens),
            chat_id=getattr(self._tls, "chat_id", ""),
            temperature=0.1, max_tokens=min(2500, max(200, budget_tokens)),
            round_no=0, user_text="context_summary",
        )
        if r.error:
            return ""
        # 思维链模型可能把 token 花在推理上导致 content 为空：退而取推理文本
        return (r.content or "").strip() or (r.reasoning or "").strip()

    @property
    def _tls_stop(self):
        """本轮的停止标记（线程局部）。

        stop_event 只在 events() 调用内传递，不存实例属性——ENGINE 是全局单例，
        多会话并行跑线程。但有两处拿不到形参：压缩器的摘要回调和 _maybe_suggest，
        两者都是一次非流式 LLM 请求、阻塞期间没有任何 delta 可做检查点。
        threading.local 既满足「不跨线程串号」，又让这两处能看到本轮的停止意图。
        """
        return getattr(self._tls, "stop_event", None)

    def _progress(self, chat, label: str, detail: str = "", status: str = "run") -> None:
        """慢操作心跳：把一条 step 消息直接发布进 Run 的事件缓冲。

        摘要/建议/文件加载都在函数调用栈深处执行，返回前外层没有任何 yield 机会；
        不发心跳的话 SSE 静默一到几分钟，看起来就是卡死。emit 未注入（直测引擎、
        /select 同步调用）时是 no-op。
        """
        emit = getattr(self._tls, "emit", None)
        if emit is None or chat is None:
            return
        try:
            emit("message", self._step_on(chat, [(label, detail, status)]).to_dict())
        except Exception:
            pass  # 心跳失败不值得打断本轮；外层事件通道仍在正常工作

    # ------------------------------------------------------------ 入口
    def events(self, chat, text: str, stop_event=None, emit=None):
        """统一事件流：{"event": "delta"|"message"|"done", "data": {...}}。

        - delta.channel: meta / reasoning / content / reset
        - message.data: 与 GET /api/chats 中同构的消息 dict
        - reset: LLM 预览失败，前端应丢弃本轮未落库的流式内容
        - stop_event: 可选 threading.Event，用户按「停止」时置位。引擎在下一个检查点
          自己收尾，不从外部 close 生成器（那会抛 generator already executing）。
          只在本次调用内传递，不存实例属性——ENGINE 是全局单例，多会话并行跑线程。
        - emit: 可选回调 (event, data)，慢操作心跳通道。摘要/建议/文件加载发生在
          普通函数调用栈深处，返回前外层没有任何 yield 机会；不直连发布的话
          SSE 会静默一到几分钟，用户只能当成卡死。由 RunManager 注入 run.publish。
        """
        yield self._delta("meta", "", engine=self._engine_name())
        yield from self._run_stream(chat, text, stop_event, emit)
        yield {"event": "done", "data": {"chat_id": chat.chat_id}}

    @staticmethod
    def _stopped(stop_event) -> bool:
        return stop_event is not None and stop_event.is_set()

    def _engine_name(self) -> str:
        return "llm" if self.llm.available else "offline"

    # ------------------------------------------------------------ 核心调度
    def _run_stream(self, chat, text: str, stop_event=None, emit=None):
        # 摘要/标定建议两处拿不到形参（见 _tls_stop），本轮的标记挂在线程局部；
        # 收尾一定清掉：RunManager 的线程名按 run 建，但 uvicorn 等会复用线程，
        # 留着旧标记会让下一轮刚开口就被判成「已停止」。
        self._tls.stop_event = stop_event
        self._tls.emit = emit
        self._tls.chat = chat
        try:
            yield from self._run_stream_body(chat, text, stop_event)
        finally:
            self._tls.stop_event = None
            self._tls.emit = None
            self._tls.chat = None

    def _run_stream_body(self, chat, text: str, stop_event=None):
        self.store.add_message(chat, "user", "text", content=text)
        # 用户另起一轮提问：此前的候选面板视为已回答，前端折叠为「已选择」
        self.store.mark_panels_consumed(chat)
        self._seal_steps(chat)

        if self.llm.available:
            state = {"claimed": False, "failed": False, "stopped": False}
            yield from self._run_llm_stream(chat, text, state, stop_event)
            if state["stopped"]:
                # 停止不是失败：绝不回退离线路由，否则会凭空多出一段离线结论
                return
            if not state["failed"]:
                return
            if not state["claimed"]:
                # 只流出了预览、尚未落库 → 让前端清空，避免与离线正文重复
                yield self._delta("reset", "")
            # 已落库部分保留，继续离线兜底补全本轮结论

        yield from self._run_offline_stream(chat, text, stop_event)

    # ------------------------------------------------------------ 离线路由（带步骤）
    def _run_offline_stream(self, chat, text: str, stop_event=None):
        if self._stopped(stop_event):
            yield self._mevent(self._msg(chat, "text", "本轮已按你的要求停止。"))
            return
        yield self._delta("reasoning", f"解析目标：「{text}」")
        scope = chat.selected_function or "全功能"
        yield self._delta("reasoning", f"两跳推荐（当前范围：{scope}）")

        rec = recommend_targets(self.cfg, text, max_items=8, function_key=chat.selected_function)
        hop = rec.get("hop")
        names = "、".join(c.get("name", c.get("key", "?")) for c in rec.get("candidates", [])[:8])
        yield self._mevent(self._step_on(chat, [
            (f"recommend_targets → hop={hop}", f"候选：{names or '无'}", "info")]))

        if hop == "error":
            yield self._mevent(self._msg(
                chat, "text", "未能从描述中确定分析目标。请补充路面/速度/动作，或直接选择功能。"))
            yield self._mevent(self._options(
                chat, {"hop": "function", "candidates": rec["candidates"], "hint": "可选功能："}))
            return

        if hop == "function":
            yield self._mevent(self._msg(
                chat, "text", f"识别到多个可能的功能，请选择：{rec.get('hint', '')}"))
            yield self._mevent(self._options(chat, rec))
            return

        if hop == "condition":
            fn = self.cfg.function(rec["function"])
            need_profile = bool(fn.profiles)
            yield self._mevent(self._msg(
                chat, "text", f"在「{fn.name}」下请选择要分析的工况"
                              + ("（随后选择档位）" if need_profile else "") + "："))
            yield self._mevent(self._options(chat, rec))
            return

        # resolved
        target = rec["target"]
        fn = self.cfg.function(rec["function"])
        chat.selected_function = rec["function"]
        chat.selected_condition = target["key"]
        if fn.profiles:
            # 需先选档位 → 交给前端 /select 触发分析
            yield self._mevent(self._msg(
                chat, "text", f"已定位工况「{target['name']}」，该功能需选择档位后再分析。"))
            yield self._mevent(self._options(chat, {
                "hop": "profile", "function": fn.key, "target": target,
                "candidates": [target], "profiles": fn.profiles,
                "profile_dims": fn.profile_dims}))
            return
        yield self._delta("reasoning", f"目标唯一命中「{target['name']}」，直接分析")
        # 分析阶段是离线路径最慢的一段，把停止标记传下去按文件自检
        for m in self._analyze(chat, target["key"], profile=None,
                               stop_event=stop_event)[0]:
            yield self._mevent(m)

    # ------------------------------------------------------------ LLM 编排（流式）
    def _fit_context(self, chat, raw: List[dict]) -> Tuple[List[dict], Optional[object]]:
        """上下文预算检查 + 压缩；压缩过程作为步骤显示，便于事后核对 token。"""
        self._tls.chat_id = chat.chat_id
        fitted, note = self.compressor.fit(raw)
        if not note:
            return fitted, None
        # 多跳循环里每轮都会 fit，同一份早期内容会算出同一句说明，别刷重复行
        if note == getattr(self._tls, "last_note", None):
            return fitted, None
        self._tls.last_note = note
        rows = [("上下文压缩", note, "info")]
        # 摘要文本一并落步骤：摘要只进 prompt，界面上这是核对"模型看到的背景"的唯一窗口
        if self.compressor.last_summary:
            rows.append(("发给模型的摘要（不显示在对话中）",
                         self.compressor.last_summary[:600], "info"))
        return fitted, self._step_on(chat, rows)

    def _run_llm_stream(self, chat, text: str, state: Dict[str, bool],
                        stop_event=None):
        file_names = [f.name for f in chat.files]
        files = [{"name": f.name, "batch_id": f.batch_id} for f in chat.files]
        selected = {
            "function": chat.selected_function,
            "condition": chat.selected_condition,
            "profile": chat.selected_profile,
        }
        history = [
            {"role": m.role, "kind": m.kind, "content": m.content}
            for m in chat.messages[:-1]
        ]
        raw = build_chat_messages(self.cfg, history, text, file_names, selected,
                                  files=files,
                                  external_note=(EXTERNAL_TOOL_NOTE
                                                 if self.mcp.tool_specs() else ""))
        messages, step = self._fit_context(chat, raw)
        if step is not None:
            yield self._mevent(step)
        exec_tools = {
            # 结构化槽位解析（§13）：命中与否由配置唯一键决定，不靠工况名相似度
            "resolve_condition": lambda args: resolve_condition(
                self.cfg,
                function_key=args.get("function_key") or chat.selected_function or None,
                maneuver=args.get("maneuver"),
                surface_code=args.get("surface_code"),
                params=args.get("params") or {},
                query=text,
                index=self.cond_index,
                # 槽位证据源：用户原话 + 文件名（文件名是合法的工况线索）
                evidence=text + " " + " ".join(f.name for f in chat.files),
            ),
            "list_conditions": lambda args: list_conditions(
                self.cfg,
                args.get("function_key") or chat.selected_function or "",
            ),
            "recommend_targets": lambda args: recommend_targets(
                self.cfg, args.get("query", text), args.get("max_items", 15),
                function_key=chat.selected_function,
            ),
            "search_manual": lambda args: self._kb_search_tool(
                args.get("query", ""), condition=args.get("condition")),
            "get_param": lambda args: self._get_param_tool(args.get("name", "")),
            "list_files": lambda args: self._list_files_tool(chat, args.get("file_index")),
            "run_analysis_on_files": lambda args: self._analyze(
                chat, args.get("condition_id"), args.get("profile"), stop_event,
                file_indexes=args.get("file_indexes"),
            ),
            # 记录库只读工具（§17.5）：不做隐式重算，避免一次对话顺带解析十几个文件
            "query_records": lambda args: self._record_tool(
                records_query, args, chat.selected_function),
            "compare_records": lambda args: self._record_tool(
                records_compare, args, chat.selected_function),
        }
        # 工具规格由配置生成（维度/工况 id 作 enum），已选定功能时进一步收窄取值域
        tool_specs = build_tool_specs(self.cfg, scope_function=chat.selected_function)
        # 外部 MCP 工具并列进同一份规格表（§6.11）：模型看到的是一次 list，
        # 不需要知道哪些来自外部——分开列会让它以为有两套调用规则。
        # 名字带 mcp__ 前缀，与内部四个工具（都不含 __）结构上不可能撞车。
        ext_specs = self.mcp.tool_specs()
        if ext_specs:
            tool_specs = tool_specs + ext_specs
            yield self._mevent(self._step_on(chat, [(
                f"外部工具已挂载：{len(ext_specs)} 个",
                "、".join(s["function"]["name"] for s in ext_specs)[:600],
                "info")]))
        previewed = False   # 是否已向用户流出了正文预览
        preview_buf: List[str] = []   # 已流出的正文，停止时按方案 (a) 落库
        try:
            for rnd in range(1, 7):
                if self._stopped(stop_event):
                    yield from self._finish_stopped(chat, state, preview_buf)
                    return
                reply = None
                stop_mid = False
                for ev in self.llm.chat_stream(
                        messages, tools=tool_specs, chat_id=chat.chat_id,
                        user_text=text, round_no=rnd, stop_event=stop_event):
                    if ev["type"] == "delta":
                        yield self._delta(ev["channel"], ev["text"])
                        if ev["channel"] == "content":
                            previewed = True
                            preview_buf.append(ev["text"])
                        if self._stopped(stop_event):
                            stop_mid = True
                            break
                    else:
                        reply = ev["reply"]
                if stop_mid or (reply is not None and reply.error == "llm_stopped"):
                    # 停止有两种来路：外层每个 delta 自检命中，或者客户端在块与块之间
                    # 自检命中（工具参数没有 delta，只有后者能覆盖）。两种都按方案 (a)
                    # 收尾，都不算失败。
                    yield from self._finish_stopped(chat, state, preview_buf,
                                                   note="用户已停止本轮（回答未完整）")
                    return
                if reply is None or reply.error:
                    state["failed"] = True
                    return

                if reply.reasoning:
                    yield self._mevent(self._step_on(chat, [
                        (f"LLM 推理（第 {rnd} 轮）", reply.reasoning[:600], "info")]))

                if not reply.tool_calls:
                    content = reply.content or "（无内容）"
                    if previewed:
                        # 正文已经流式给出，不再重复落一条文本消息
                        m = self._msg(chat, "text", content)
                        m.meta["streamed"] = True
                        state["claimed"] = True
                        yield self._mevent(m)
                    else:
                        m = self._msg(chat, "text", content)
                        state["claimed"] = True
                        yield self._mevent(m)
                    return

                asst = {"role": "assistant", "content": reply.content or ""}
                asst["tool_calls"] = [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"],
                                  "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
                    for tc in reply.tool_calls
                ]
                messages.append(asst)
                if reply.content:
                    m = self._msg(chat, "text", reply.content)
                    m.meta["streamed"] = True
                    state["claimed"] = True
                    yield self._mevent(m)
                    # 这段预览已落库：清空缓冲，否则后续轮次被停止时会把它再写一遍
                    preview_buf.clear()

                for tc in reply.tool_calls:
                    name, args = tc["name"], tc["arguments"]
                    # 工具执行没有 delta，是停止检查点之间的真空区：每个工具开始前自检。
                    # 多个 tool_calls 连发时，前面的已经落库，剩下的不再跑。
                    if self._stopped(stop_event):
                        yield from self._finish_stopped(
                            chat, state, preview_buf,
                            note=f"用户已停止本轮（{name} 未执行）",
                            receipt=f"本轮已按你的要求停止：工具 {name} 未执行，"
                                    "之前的步骤结果都已保留。")
                        return
                    arg_s = json.dumps(args, ensure_ascii=False)[:160]
                    yield self._mevent(self._step_on(chat, [(f"调用工具 {name}", f"参数: {arg_s}", "run")]))
                    fn = exec_tools.get(name)
                    if fn is None and self.mcp.is_external(name):
                        # 外部 MCP 工具（§6.11）。hub.call 设计上永不抛异常：外部挂了
                        # 要给模型一条读得懂的错误说明，而不是让整轮 failed → 回退离线
                        payload = self.mcp.call(name, args, stop_event=stop_event)
                        ok = bool(payload.get("ok"))
                        yield self._mevent(self._step_on(chat, [(
                            f"{name} → 外部 MCP" + ("" if ok else "（失败）"),
                            (str(payload.get("text") or payload.get("note") or ""))[:200],
                            "ok" if ok else "warn")]))
                    elif fn is None:
                        payload = {"error": f"未知工具 {name}"}
                        yield self._mevent(self._step_on(chat, [(f"{name} 失败", "未知工具", "error")]))
                    else:
                        # 缺维守卫：没给 profile 或只给了部分维度（如仅 "4WD"）都拦下——
                        # 部分档位的其余维度指标会静默走 default 条目（§10.8）
                        if name == "run_analysis_on_files":
                            missing = self._profile_guard(chat, args.get("condition_id"),
                                                          args.get("profile"))
                            if missing is not None:
                                # 与离线路径同一约束：声明了档位的功能没选档就不分析
                                cond = self.cfg.condition(args.get("condition_id") or "")
                                state["claimed"] = True
                                yield self._mevent(self._step_on(chat, [(
                                    "档位守卫：暂不分析",
                                    f"工况 {cond.id if cond else args.get('condition_id')} "
                                    "所属功能需先选档位", "warn")]))
                                yield self._mevent(missing)
                                payload = {"ok": False, "hop": "profile",
                                           "note": "该功能需先选档位，已把档位面板给用户"}
                                messages.append({
                                    "role": "tool", "tool_call_id": tc["id"],
                                    "content": json.dumps(payload, ensure_ascii=False)})
                                continue
                        result = fn(args)
                        if name == "run_analysis_on_files":
                            msgs, digests = result if isinstance(result, tuple) else (result or [], [])
                            for m in msgs:
                                state["claimed"] = True
                                yield self._mevent(m)
                            failed = " / ".join(
                                (m.content or "") for m in msgs if m.kind == "error")[:400]
                            if digests:
                                # 数值 + 判定 + 规则结论原文回灌：模型据此直接下结论。
                                # 预算扣掉外层信封（ok/note），避免整段被硬截成坏 JSON
                                kept = digest_payload(digests, TOOL_PAYLOAD_CLIP_CHARS - 256)
                                payload = {
                                    "ok": True,
                                    "note": "results 为本次分析的实测值与判定，"
                                            "回答实测值直接引用它，不要让用户查看图表",
                                    "results": kept,
                                }
                                # 成因图例只随实际送出的 reason 附带：缺信号与缺事件的
                                # 建议相反，让模型从「无值」猜成因就会说错话（§10.1）
                                legend = reasons_present(kept)
                                if legend:
                                    payload["reason_legend"] = {
                                        r: MISSING_REASON_LEGEND.get(r, r) for r in legend}
                                if failed:
                                    payload["failed"] = failed
                            else:
                                payload = {
                                    "ok": False,
                                    "note": "未产生分析结果：" + (failed or "没有可分析的文件"),
                                }
                            yield self._mevent(self._step_on(chat, [
                                (f"{name} 完成", f"生成 {len(msgs)} 条消息 · {len(digests)} 份结果",
                                 "ok" if digests else "warn")]))
                        else:
                            payload = result
                            res = result if isinstance(result, dict) else {}
                            hop = res.get("hop")
                            label, detail, status = self._tool_step_label(name, res)
                            row = {"label": label, "detail": detail, "status": status}
                            hits = self._step_hits(name, res)
                            if hits:
                                # 「命中 N 节」只说明有条数，用户看不到到底命中了什么；
                                # 把每条命中摊进这一行，前端可逐条展开（截断说明一并给出）
                                row["hits"] = hits
                            yield self._mevent(self._step_on(chat, [row]))
                            # 面板只在信息不足时弹；hop=function 的候选由工具保证是功能级，
                            # 工况清单（catalog）不会走到这里，不混进用户可见选项
                            if hop in ("function", "condition", "profile"):
                                state["claimed"] = True
                                yield self._mevent(self._options(chat, result))
                            elif hop in ("catalog", "resolved", "error"):
                                # 清单/命中结果只作为 tool 应答回灌给 LLM；
                                # error 也交还 LLM 组织措辞，不直接落用户可见消息
                                pass
                    messages.append({
                        "role": "tool", "tool_call_id": tc["id"],
                        "content": json.dumps(payload, ensure_ascii=False, default=str)[
                            :TOOL_PAYLOAD_CLIP_CHARS],
                    })
                # 工具结果会把上下文撑大：下一轮请求前再压一次
                fitted, step = self._fit_context(chat, messages)
                messages = fitted
                if step is not None:
                    yield self._mevent(step)
                previewed = False
            # 轮数耗尽：按失败处理，让调用方兜离线
            state["failed"] = True
            if state["claimed"]:
                state["failed"] = False
        except Exception:
            # 兜底必须留日志：这里吞掉的异常会表现为"静默回退离线"，
            # 没有堆栈的话事后只能靠 trace 反推
            log.exception("LLM 编排异常，本轮回退离线路径")
            state["failed"] = True

    def _finish_stopped(self, chat, state: Dict[str, bool],
                        previewed: List[str], note: str = "用户已停止本轮",
                        receipt: Optional[str] = None):
        """方案 (a)：把已经流出的半截正文落成消息再结束，信息不丢。

        预览正文此前只以 delta 形式流出、尚未落库；停止后若直接结束，刷新就看不到它。
        这里补一条带「已停止」标记的 text 消息，并置 state["stopped"]，
        让上层知道这不是失败（绝不能回退离线路由，否则凭空多出一段离线结论）。

        receipt：一个字都还没出来时的回执。停止点不同（工具没跑 vs 正文写了一半），
        用户看到的后果不同，所以调用方可以把话说具体，而不是都停在同一句。
        """
        self._seal_steps(chat)
        text = "".join(previewed).strip()
        state["claimed"] = True
        if text:
            # 这段正文此前只以 delta 流出，标 streamed 让前端 live 气泡让位给落库版本
            m = self._msg(chat, "text", text + f"\n\n—— {note}",
                          meta={"stopped": True, "streamed": True})
            yield self._mevent(m)
        else:
            # 一个字都还没出来：给一行明确回执，避免界面停在「处理中…」
            yield self._mevent(self._msg(
                chat, "text", receipt or "本轮已按你的要求停止。",
                meta={"stopped": True}))
        state["stopped"] = True

    def _profile_guard(self, chat, condition_id: str, profile: Optional[str] = None):
        """声明了档位的功能未选档或档位缺维 → 返回档位面板；不需要拦时返回 None。

        `hop=resolved` 只保证工况唯一，不保证档位唯一；离线路径本来就是"先选档再分析"，
        在线路径不给这个例外的话，同一个问题会有两套结论口径。

        缺维守卫：多维功能只给一维（如仅 "4WD"）时，其余维度的指标会静默走 default
        条目（§10.8）——结果里看不出"这个值不是用户选的档位算出来的"。面板把已给的
        维度预选上，用户只需确认剩余维度。
        """
        cond = self.cfg.condition(condition_id or "")
        if cond is None:
            return None
        fn = self.cfg.function(cond.function)
        if fn is None or not fn.profiles:
            return None
        given = [t for t in re.split(r"[|,_\s]+", profile or "") if t]
        if given:
            dims = fn.profile_dims or []
            complete = bool(dims) and all(any(t in d for t in given) for d in dims)
            if complete:
                return None
        preset = [t for t in given if any(t in d for d in (fn.profile_dims or []))]
        return self._options(chat, {
            "hop": "profile", "function": fn.key,
            "target": {"key": cond.id, "name": cond.name},
            "candidates": [{"key": cond.id, "name": cond.name}],
            "profiles": fn.profiles, "profile_dims": fn.profile_dims,
            **({"preset": preset} if preset else {}),
            "hint": ("档位不完整：已按 " + "|".join(preset) + " 预选，请确认其余维度"
                     if preset else "该功能需先选档位再分析"),
        })

    def _loader_for(self, path: str):
        """按扩展名取 Loader 实例（同一实例复用，DBC 只解析一次）。"""
        from ..pipeline import make_loader

        ext = os.path.splitext(path)[1].lower() or "?"
        if ext not in self._loaders:
            self._loaders[ext] = make_loader(path, self.cfg, blf_cfg=self.blf_cfg)
        return self._loaders[ext]

    # ------------------------------------------------------------ 分析
    def _analyze(self, chat, condition_id: str, profile: Optional[str],
                 stop_event=None, file_indexes: Optional[List[int]] = None
                 ) -> Tuple[List, List[dict]]:
        """执行分析并落库可见消息。

        返回 (消息列表, 数值摘要列表)。摘要与结果卡片同源，既拼进卡片正文
        （跨轮追问时由 history 回灌），也直接回灌给 LLM 当轮 tool 应答 ——
        否则模型只拿到"分析已完成"，正文会退化成让用户自己看图（§6.9）。

        分析范围默认只取「最近一次上传的那批文件」（§8.2）：用户新传数据后说的
        「分析」指的是新那份，把会话里的历史文件一起重跑，结论就混了上一趟的数据。
        file_indexes 是模型按 prompt 里编号显式点名的文件（1 起），供「对比全部文件」
        这类明确要求使用；点不名的编号忽略，不让模型凭空造范围。
        """
        cond = self.cfg.condition(condition_id or "")
        if cond is None:
            return [self._msg(chat, "error", f"未知道况：{condition_id}")], []
        if not chat.files:
            return [self._msg(chat, "error",
                              "会话内还没有数据文件，请先上传 .mf4/.blf 文件再分析。")], []
        targets = self._pick_files(chat, file_indexes)
        if not targets:
            return [self._msg(chat, "error",
                              "没有选中任何数据文件（file_indexes 与本会话文件对不上）。")], []

        chat.selected_function = cond.function
        chat.selected_condition = cond.id
        chat.selected_profile = profile

        # 新一轮动作（点选面板/工具调用）另起一个步骤块
        self._seal_steps(chat)
        skipped = len(chat.files) - len(targets)
        step_msg = self._step_on(chat, [
            (f"run_analysis_on_files：{cond.name}",
             f"文件 {len(targets)} 个"
             + (f"（会话内另有 {skipped} 个非本轮范围，未重跑）" if skipped else "")
             + f" · 工况 {cond.id}"
             + (f" · 档位 {profile}" if profile else ""), "run")])
        added: List = [step_msg]

        results: List[AnalysisResult] = []
        errors: List[str] = []
        stopped_early = False
        for i, f in enumerate(targets):
            # 工具阶段是整轮最慢的一段，且期间没有 delta 可供上层检查停止：
            # 每个文件开始前自检，已完成的文件结果照常保留落库
            if self._stopped(stop_event):
                stopped_early = True
                self._step_on(chat, [(f"已停止：{len(targets) - i} 个文件未分析",
                                      "前序文件的结果已保留", "warn")])
                break
            sess = self.store.session(f.file_id)
            first_load = sess.signals is None
            self._progress(
                chat, f"分析 {f.name}（{i + 1}/{len(targets)}）",
                "首次分析该文件需先加载信号，可能较慢" if first_load else "",
                "run")
            try:
                if sess.signals is None:
                    sess.signals = self._loader_for(f.path).load(f.path)
                    self.store.note_signals_loaded(f.file_id)
                    # loader 解析中断（坏帧/IO）不抛异常，但事实必须进 errors：
                    # 用户会在结果卡片上看到"结果基于不完整数据"，而不是被骗
                    for err in getattr(self._loader_for(f.path), "load_errors", None) or []:
                        errors.append(f"{f.name}: {err}")
                res = analyze_signals(
                    sess.signals, cond.id, self.cfg,
                    file_id=f.file_id, file_name=f.name, profile=profile,
                )
                self._maybe_suggest(res, cond)
                sess.analyses[sess.analyze_key(cond.id, profile)] = res
                results.append(res)
            except NoDataError as e:
                errors.append(f"{f.name}: {e}")
            except Exception as e:
                log.exception("分析异常 %s / %s", f.name, cond.id)
                errors.append(f"{f.name}: 分析异常 {e}")

        valid = sum(1 for r in results for s in r.samples if s.window is not None)
        step2 = self._step_on(chat, [
            (f"分段+指标：{len(results)} 份数据", f"样本窗口 {valid} 个 · 失败 {len(errors)} 份",
             "ok" if not errors else "warn")])
        if step2 is not step_msg:
            added.append(step2)
        digests: List[dict] = []
        for res in results:
            added.append(self._analysis_msg(chat, res))
            digests.append(digest_from_analysis(res.to_dict()))
        if errors:
            added.append(self._msg(chat, "error", "部分文件分析失败：\n" + "\n".join(errors)))
        elif stopped_early and not results:
            # 一个文件都还没跑就停了：别报"没有可分析的文件"，那会误导模型和用户
            added.append(self._msg(chat, "text", "本轮已按你的要求停止：尚未开始分析文件。",
                                   meta={"stopped": True}))
        elif not results and not errors:
            added.append(self._msg(chat, "error", "没有可分析的文件。"))
        return added, digests

    # ------------------------------------------------------------ 知识/文件工具
    # 工具分两类，trace 步骤标签不能共用一个格式：
    # 路由类（resolve_condition / list_conditions / recommend_targets）返回 hop，
    # 标签就是 hop；数据类（search_manual / get_param / list_files）**没有 hop 这个
    # 概念**，硬套 `hop=None` 只会让人以为检索失败了（实测困扰）。数据类按返回内容
    # 给命中数/通道数，并带上检索通道 mode。
    _ROUTING_TOOLS = ("resolve_condition", "list_conditions", "recommend_targets")
    # 对话「过程」里每条命中的展示正文上限（超出只给前段 + 一句去向说明）
    STEP_HIT_TEXT_MAX = 600

    @staticmethod
    def _tool_step_label(name: str, res: dict) -> Tuple[str, str, str]:
        """(标签, 详情, 状态)：路由类看 hop，数据类看返回条数。"""
        status = "ok" if res.get("ok", True) else "warn"
        if name in ChatEngine._ROUTING_TOOLS:
            hop = res.get("hop")
            if hop == "error":
                status = "warn"
            if hop == "catalog":
                return (f"{name} → hop=catalog",
                        f"清单 {res.get('count')} 条（仅本轮可见）", status)
            n_cand = len(res.get("candidates", []) or [])
            label = f"{name} → hop={hop}" if hop else f"{name} 完成"
            if hop == "resolved":
                # target 的形状是 {key, name, via, ...}（tools.resolve_condition），
                # detail 给工况名比给「候选：1 项」有用得多
                tgt = res.get("target") or {}
                return (label, str(tgt.get("name") or tgt.get("key") or ""), status)
            return (label, f"候选：{n_cand} 项", status)
        if name == "search_manual":
            if res.get("ok") is False:      # 参数错（query 为空）：把 note 摊开
                return (f"{name} 参数有误", str(res.get("note", ""))[:120], "warn")
            n = len(res.get("results", []) or [])
            if not n:
                # 空命中不是工具失败，但对 trace 是信号（模型接下来可能开始编）
                return (f"{name} → 命中 0 节", "手册无相关内容", "warn")
            mode = res.get("mode") or "keyword"
            chars = sum(len(r.get("snippet", "")) for r in (res.get("results") or []))
            return (f"{name} → 命中 {n} 节", f"通道 {mode} · 送入 {chars} 字", status)
        if name == "get_param":
            rows = res.get("results", []) or []
            first = (rows or [{}])[0].get("param", "")
            secs = len({r.get("section", "") for r in rows})
            # 多工况同参数是常态（不再按工况过滤）：detail 直接点出有几处，
            # 免得「命中 3 条」被当成三个不同参数
            detail = f"{first} · {secs} 个工况" if secs > 1 else str(first)
            return (f"{name} → 命中 {len(rows)} 条", detail, status)
        if name == "list_files":
            rows = res.get("files", []) or []
            ch = next((r for r in rows if r.get("channels")), None)
            detail = f"{ch['name']}：{len(ch['channels'])} 个通道" if ch \
                else f"{len(rows)} 个文件"
            return (f"{name} → {detail}", "", status)
        if name == "query_records":
            return (f"{name} → {res.get('count', 0)} 条记录",
                    f"库内状态 {res.get('db_stats', {})}", status)
        if name == "compare_records":
            groups = res.get("groups", []) or []
            return (f"{name} → {res.get('record_count', 0)} 条记录",
                    f"{len(groups)} 个工况", status)
        return (f"{name} 完成", "", status)

    @staticmethod
    def _step_hits(name: str, res: dict) -> List[dict]:
        """每条工具命中的展示行（对话「过程」里逐条展开用）。

        search_manual 走喂给模型的 payload 条目（同一份 snippet，所见即所喂；
        被预算丢掉的弱命中不在 payload 里，本来就没影响回答，不展示）。
        get_param 走原始行：fields 在 payload 与展示之间需求不同，直接格式化。

        正文按 STEP_HIT_TEXT_MAX 截：steps 消息会随每次轮询整体序列化给前端，
        一次检索最多带 10k 字进来，长会话白占内存与带宽。完整原文在 /debug 的
        tool 返回区看，这里只保证「看得清命中了什么」。
        """
        if not isinstance(res, dict) or res.get("ok") is False:
            return []
        if name == "search_manual":
            out = []
            for r in res.get("results", []) or []:
                snip = r.get("snippet", "")
                note = r.get("note", "")
                shown = snip[:ChatEngine.STEP_HIT_TEXT_MAX]
                if len(snip) > len(shown):
                    shown += "…"
                    note = (note + "；" if note else "") + "界面只展示前段全文，完整内容看 /debug"
                line = {"title": r.get("title", ""), "id": r.get("id", ""),
                        "chars": len(snip), "text": shown}
                if note:
                    line["note"] = note
                out.append(line)
            return out
        if name == "get_param":
            out = []
            for r in res.get("results", []) or []:
                fields = r.get("fields") or {}
                line = {"title": r.get("param", ""),
                        "id": r.get("section", ""),
                        "text": " · ".join(f"{k}={v}" for k, v in fields.items())}
                if r.get("match") == "partial":
                    line["note"] = "子串近似匹配，引用前确认是同一个参数"
                out.append(line)
            return out
        if name in ("query_records", "compare_records"):
            rows = res.get("records") or [
                r for g in res.get("groups", []) or [] for r in g.get("records", [])
            ]
            out = []
            for r in rows[:10]:
                values = []
                for key, m in (r.get("metrics") or {}).items():
                    if m.get("value") is None:
                        values.append(f"{key}=missing")
                    else:
                        values.append(f"{key}={m['value']}{m.get('unit', '')}")
                line = {
                    "title": f"{r.get('vehicle', '')} {r.get('file_id', '')}".strip(),
                    "id": r.get("record_id", ""),
                    "text": " · ".join(values)[:ChatEngine.STEP_HIT_TEXT_MAX],
                }
                out.append(line)
            return out
        return []

    @staticmethod
    def _kb_item_frame(h: dict) -> int:
        """一条命中除 snippet 之外必须付的 JSON 包装长度。

        = 字段名/引号/逗号 + id 与面包屑标题的实际字数 + 最坏情况的裁剪标记。
        snippet_window 在窗口装不下整节时一定会附带 note，所以按最坏情况预留
        （CUT_NOTE_MAX_CHARS 是 kb.py 各处 note 文案的字数上界）。
        """
        probe = {"id": h.get("id", ""), "title": h.get("title", ""),
                 "snippet": "", "note": "x" * CUT_NOTE_MAX_CHARS}
        return len(json.dumps(probe, ensure_ascii=False)) - 2   # 扣掉 snippet 的两个引号

    def _kb_payload(self, hits: List[dict]) -> dict:
        """把命中装进 TOOL_PAYLOAD_CLIP_CHARS：预算均分 + 中心窗口 + 主动降级。

        原先每条固定 [:1500]、top_k=5，序列化后约 7.8k 字，必然超 4000 的硬闸；
        而硬闸是按字符砍 JSON（engine 外层），砍出来是坏 JSON——模型既读不到第
        3 条之后的命中，`transient` 标记也只剩字符串嗅探能认。small-to-big 之后
        节长更容易超预算，所以这里改为主动分配：每条命中拿「剩余预算/命中数」
        的中心窗口，均分后低于可读下限就丢掉最弱的一条重算——宁可少给一条完整
        命中，不给五条半截。

        剩余预算必须先扣掉每条的 JSON 包装（_kb_item_frame）；扣完也不能直接用满
        room：换行、引号序列化后各占 2 字符，markdown 表格每行都要付一次，实测
        长表格手册每条仍额外放大 ~70 字。所以按实际超出量回收 room 重试，压到
        可读下限仍装不下才丢整条命中——旧实现两项都没算，5 节命中最后只剩
        1 节进模型，其余 4 节白检索。
        """
        base = {"ok": True, "transient": True, "mode": self.kb.last_search_mode,
                "note": "" if hits else "手册中没有相关内容；如实告知用户，不要编造手册内容",
                "results": []}
        overhead = len(json.dumps(base, ensure_ascii=False)) + 32   # 数组外框余量
        kept = list(hits)
        while kept:
            frames = [self._kb_item_frame(h) for h in kept]
            room = ((TOOL_PAYLOAD_CLIP_CHARS - overhead - sum(frames)
                     - (len(kept) - 1)) // len(kept))
            # 转义放大量只有裁完才知道：按超出的字数从每条窗口里等量扣回。
            # 窗口是行边界对齐的，扣一次不会正好；最多试 4 次，免得为一两字
            # 反复重裁。仍装不下就丢最弱的一条重算
            for _ in range(4):
                if room < SNIPPET_MIN_CHARS:
                    break
                payload = {**base,
                           "results": [self._kb_hit_item(h, room) for h in kept]}
                excess = len(json.dumps(payload, ensure_ascii=False)) - TOOL_PAYLOAD_CLIP_CHARS
                if excess <= 0:
                    return payload
                room -= excess // len(kept) + 1
            kept.pop()
        return base

    @staticmethod
    def _kb_hit_item(h: dict, room: int) -> dict:
        """一条命中 → payload 条目：以命中块为中心裁窗口，附带裁剪说明。"""
        snippet, cut = KnowledgeBase.snippet_window(h, room)
        if not snippet:            # 预算被标题吃满：退回从头裁，别交空串
            snippet, cut = h["text"][:room], ""
        item = {"id": h["id"], "title": h["title"], "snippet": snippet}
        if cut:
            item["note"] = cut
        return item

    def _kb_search_tool(self, query: str, condition: Optional[str] = None) -> dict:
        """手册语义检索工具：知识性问答的数据来源，无命中也如实回执（防编造）。"""
        query = (query or "").strip()
        if not query:
            return {"ok": False, "note": "query 为空：给一句检索问句"}
        hits = self.kb.search(query, top_k=5, condition=condition)
        # 窗口以命中块为中心（KnowledgeBase.snippet_window）：从节头裁会让窗口
        # 永远是「节标题 + 第一个块的前半」，命中块在表尾时答案连同表格一起被切掉
        return self._kb_payload(hits)

    def _get_param_tool(self, name: str) -> dict:
        """参数精确查询工具（P1 参数索引）：参数值类问答的首选，值原样引用。

        不带工况过滤：同名参数在多个工况表里各有一行时全部返回，由模型按
        `section` 挑。曾带 condition 硬过滤，词序/空格差一点就返回空，工具却
        回 ok=true、note 写「手册中没有该参数名」——模型照念，参数名明明对。
        """
        name = (name or "").strip()
        if not name:
            return {"ok": False, "note": "name 为空：给参数名，如 Cp_FA"}
        rows = self.kb.get_param(name)
        secs = {r["section"] for r in rows}
        partial = any(r["match"] == "partial" for r in rows)
        if not rows:
            note = ("手册表格中没有该参数名：确认拼写与手册一致（如 Cp_FA），"
                    "或用 search_manual 检索手册内容")
        elif len(secs) > 1:
            # 多工况同参数各自取值不同：让模型看清有几处，别默认取第一条
            note = (f"该参数出现在 {len(secs)} 个工况/章节，取值可能不同："
                    "按用户说的工况选，没指明就把各工况的值一并列出，不要默认取第一条")
        elif partial:
            note = "精确名未命中，以下是子串近似匹配（match=partial）：引用前确认是同一个参数"
        else:
            note = ""
        return {
            "ok": True,
            "transient": True,
            "note": note,
            "results": [{"param": r["param"], "fields": r["fields"],
                         "section": r["section"], "match": r["match"]}
                        for r in rows],
        }

    def _record_tool(self, fn, args: dict, selected_function: Optional[str]) -> dict:
        """记录库工具的统一入口：未启用/查询异常都回 ok=false + 说明。

        记录库是旁路能力，挂了它不该让整轮 LLM 编排回退离线——
        工具异常在这里收敛成模型读得懂的一句话。
        """
        if self.records is None:
            return {"ok": False, "hint": "记录库未启用（未配置 RECORDS_DB）"}
        allowed = {
            "records_query": {"file_id", "condition_id", "vehicle", "function_key", "limit"},
            "records_compare": {"condition_id", "function_key", "vehicles", "metric",
                                "limit"},
        }.get(getattr(fn, "__name__", ""), set())
        kwargs = {k: v for k, v in (args or {}).items()
                  if k in allowed and v not in (None, "", [])}
        if not kwargs.get("function_key") and selected_function:
            kwargs["function_key"] = selected_function
        try:
            return fn(self.records, self.cfg, **kwargs)
        except Exception as e:
            log.exception("记录库查询失败")
            return {"ok": False, "hint": f"记录库查询失败: {type(e).__name__}: {e}"}

    def _list_files_tool(self, chat, file_index) -> dict:
        """文件清单工具：不带参数纯清单（不加载信号）；带 file_index 加载后给通道名。"""
        latest = {f.file_id for f in chat.latest_batch()}

        def row(i, f, channels=None):
            r = {"index": i + 1, "name": f.name,
                 "size_mb": round(f.size / 1048576, 2),
                 "latest_batch": f.file_id in latest}
            if channels is not None:
                r["channels"] = channels
            return r

        rows = [row(i, f) for i, f in enumerate(chat.files)]
        if not chat.files:
            return {"ok": True, "transient": True, "files": [],
                    "note": "会话内没有数据文件，请先上传 .mf4/.blf"}
        if file_index is None:
            return {"ok": True, "transient": True, "files": rows,
                    "note": "channels 未加载；要查看某文件的信号通道，带 file_index 再调一次"}
        try:
            i = int(file_index) - 1
        except (TypeError, ValueError):
            return {"ok": False, "note": f"file_index 非法: {file_index}"}
        if not 0 <= i < len(chat.files):
            return {"ok": False, "note": f"file_index 超出范围（共 {len(chat.files)} 个文件）"}
        f = chat.files[i]
        sess = self.store.session(f.file_id)
        if sess.signals is None:
            # 首次加载可能要几十秒（大文件逐帧解码）：发心跳让界面知道在干什么
            self._progress(chat, f"加载信号通道：{f.name}",
                           "首次查看该文件需解析全部帧，可能较慢", "run")
            sess.signals = self._loader_for(f.path).load(f.path)
            self.store.note_signals_loaded(f.file_id)
        rows[i] = row(i, f, sorted(sess.signals.keys()))
        load_errors = getattr(self._loader_for(f.path), "load_errors", None) or []
        note = "；".join(load_errors) if load_errors else ""
        return {"ok": True, "transient": True, "files": rows, **({"note": note} if note else {})}

    @staticmethod
    def _pick_files(chat, file_indexes: Optional[List[int]]) -> List:
        """本轮要分析的文件。

        默认 = 最近一次上传的那一批；file_indexes 给了就按编号取（1 起，越界丢弃），
        让「把这几个文件对比一下」这类要求有明确出口，而不是只能全跑。
        """
        if not file_indexes:
            return chat.latest_batch() if hasattr(chat, "latest_batch") else list(chat.files)
        picked = []
        for n in file_indexes:
            try:
                i = int(n)
            except (TypeError, ValueError):
                continue
            if 1 <= i <= len(chat.files) and chat.files[i - 1] not in picked:
                picked.append(chat.files[i - 1])
        return picked

    def _maybe_suggest(self, res: AnalysisResult, cond) -> None:
        """LLM 可用且有异常/缺失时，生成 calibration_actions；否则降级标记。"""
        fn = self.cfg.function(cond.function)
        if not self.llm.available:
            res.degraded = True
            return
        if self._stopped(self._tls_stop):
            # 用户已经按了停止：这是整轮里唯一一处「停下来还要再等一次 LLM」的阻塞
            # （一次非流式请求，没有 delta 可做检查点）。跳过建议只让本轮早点返回，
            # 结果卡片与规则结论都已落库，不损失已有信息。
            res.degraded = True
            return
        snippets = self._kb_snippets(res, fn, query=" ".join(
            [cond.name] + list(dict.fromkeys(
                v.fault_domain for s in res.samples for v in s.verdicts))),
            condition=cond.name)
        user = suggestion_prompt(self.cfg, fn, cond, res, snippets)
        if user is None:
            return
        case_hint = self._case_hint(res, fn)
        system = (
            "你是制动标定助手。基于给定的异常指标、规则结论与知识库片段，"
            "只输出结构化 JSON。可标定量必须取自各指标的 tuning_params。"
            + (f"\n{case_hint}" if case_hint else "")
        )
        # _maybe_suggest 拿不到 chat 形参（签名只有 res/cond），从线程局部取：
        # _run_stream 入口已挂上本轮会话；直调（/select、测试）时为 None，
        # _progress 自身对 None/无 emit 都是 no-op
        self._progress(getattr(self._tls, "chat", None), "正在生成标定建议（LLM）",
                       f"{cond.name} · 非流式请求，耗时取决于模型速度", "run")
        sug = self.llm.complete_json(system, user)
        if sug:
            res.llm_suggestion = sug
        else:
            res.degraded = True

    def _kb_snippets(self, res: AnalysisResult, fn, query: str = "",
                     condition: Optional[str] = None) -> List[str]:
        out = []
        seen = set()
        for s in res.samples:
            for v in s.verdicts:
                ref = v.kb_ref
                if ref and ref not in seen:
                    seen.add(ref)
                    sec = self.kb.section(ref)
                    if sec is None:
                        for sid in self.kb.sections:
                            if sid.startswith(ref):
                                sec = self.kb.section(sid)
                                break
                    if sec:
                        # 1200：覆盖整张参数表；精确引用没有命中块可围绕，退而求
                        # 其次锚到节内第一张表格——kb_ref 进建议 prompt 就是冲
                        # 限值表去的，从节头叙述裁会让表尾丢在窗口外
                        anchor = 0 if sec.text.startswith("|") else sec.text.find("\n|")
                        start = anchor if anchor >= 0 else 0
                        out.append(f"[{ref}] {sec.text[start:start + 1200]}")
        # 语义补充：kb_ref 是规则写死的引用，规则没引用 ≠ 手册里没有相关内容；
        # 按工况名+故障域做语义召回 top-2（带工况过滤防同名表跨工况串扰），
        # 注入前与精确引用去重
        if query:
            for hit in self.kb.search(query, top_k=2, condition=condition):
                key = hit.get("chunk") or hit["id"]
                if key not in seen and hit.get("text"):
                    seen.add(key)
                    # 同样按命中块取窗：从节头裁会让建议 prompt 只看到第一个块
                    snip, _ = KnowledgeBase.snippet_window(hit, 500)
                    out.append(f"[{hit['id']}] {snip}")
        return out

    def _case_hint(self, res: AnalysisResult, fn) -> str:
        terms = []
        for s in res.samples:
            for v in s.verdicts:
                terms.append(v.fault_domain)
        cases = self.kb.similar_cases(fn.key, list(set(terms)) + [fn.key], top_k=2)
        if not cases:
            return ""
        lines = ["历史案例（参考）："]
        for c in cases:
            act = c.data.get("tuning_actions", [])
            a = "; ".join(f"{x.get('param')} {x.get('direction')}" for x in act[:2])
            lines.append(f"- {c.case_id}: {c.data.get('root_cause_hypothesis', '')[:120]} 动作[{a}]")
        return "\n".join(lines)

    # ------------------------------------------------------------ 消息工厂
    def _msg(self, chat, kind: str, content: str, data=None, meta=None):
        return self.store.add_message(chat, "assistant", kind, content=content,
                                      data=data, meta=meta)

    def _step_on(self, chat, items: List):
        """把步骤落进本轮最近的未封存 steps 消息（追加），否则新建一条。

        items 元素可以是 (label, detail, status) 三元组，也可以是已经带额外字段
        （如 hits）的 dict —— 后者让工具命中明细随步骤行一起持久化给前端。
        """
        rows = []
        for it in items:
            if isinstance(it, dict):
                row = {"label": it.get("label", ""), "detail": it.get("detail", ""),
                       "status": it.get("status") or "info"}
                if it.get("hits"):
                    row["hits"] = it["hits"]
            else:
                label, detail, status = (list(it) + [None, "info"])[:3]
                row = {"label": label, "detail": detail, "status": status or "info"}
            rows.append(row)
        for m in chat.messages[::-1]:
            if m.kind == "steps" and not m.meta.get("sealed"):
                # 写时替换而非原地 extend：本轮在后台线程跑，
                # 另一个线程可能正在序列化同一份 data，原地改 list 会让 json 迭代炸掉
                m.data = {**(m.data or {}), "items": list((m.data or {}).get("items", [])) + rows}
                m.content = self._steps_text(m.data["items"])
                chat.touch()
                return m
        return self.store.add_message(
            chat, "assistant", "steps",
            content=self._steps_text(rows),
            data={"items": rows},
            meta={"sealed": False},
        )

    def _seal_steps(self, chat) -> None:
        for m in chat.messages[::-1]:
            if m.kind == "steps" and not m.meta.get("sealed"):
                m.meta["sealed"] = True
                return

    @staticmethod
    def _steps_text(rows: List[dict]) -> str:
        return " · ".join(r["label"] for r in rows[-6:]) or "执行步骤"

    def _options(self, chat, rec: dict):
        return self.store.add_message(
            chat, "assistant", "target_options",
            content=self._options_text(rec),
            data=rec,
        )

    def _analysis_msg(self, chat, res: AnalysisResult):
        d = res.to_dict()
        n_bad = sum(1 for s in res.samples for m in s.metrics if m.status in ("abnormal", "missing"))
        # 「没切出事件」必须在卡片标题就说明：否则卡片读起来像「1 个样本，指标全缺失」，
        # 用户和模型都会把它当成通道没采到，而真实原因是这趟试验没做出来（§10.1）。
        no_event = sum(1 for s in res.samples
                       if any(m.missing_reason == "no_valid_event" for m in s.metrics))
        head = (
            f"工况「{res.condition_name}」分析完成：{len(res.samples)} 个事件样本"
            + (f"，{n_bad} 项异常/缺失" if n_bad else "，全部达标")
            + (f"；其中 {no_event} 份未切出有效事件窗口（非信号缺失）" if no_event else "")
            + ("（LLM 建议不可用，仅规则结论）" if res.degraded else "")
        )
        # 正文带上数值+判定：卡片 kind 不是 text，默认不回灌 prompt，
        # 后续轮次追问"实测多少"时模型只能看到这一行（is_injected 放行 analysis_result）
        return self.store.add_message(
            chat, "assistant", "analysis_result",
            content=head + "\n" + digest_line(d), data=d)

    @staticmethod
    def _options_text(rec: dict) -> str:
        hop = rec.get("hop")
        cands = rec.get("candidates", [])
        if hop == "profile":
            return "请选择档位："
        names = "；".join(f"{i + 1}. {c.get('name', c.get('key'))}" for i, c in enumerate(cands))
        return f"候选（{hop}）：{names}"

    @staticmethod
    def _mevent(msg) -> dict:
        return {"event": "message", "data": msg.to_dict()}

    @staticmethod
    def _delta(channel: str, text: str, engine: str = "") -> dict:
        data = {"channel": channel, "text": text}
        if engine:
            data["engine"] = engine
        return {"event": "delta", "data": data}
