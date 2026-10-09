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
import os
import threading
from typing import Dict, List, Optional, Tuple

from ..agent.tools import list_conditions, recommend_targets, resolve_condition
from ..configs import AppConfigs
from ..llm.client import LlmClient
from ..llm.context import ContextBudget, ContextCompressor
from ..llm.kb import KnowledgeBase
from ..llm.prompts import (
    TOOL_PAYLOAD_CLIP_CHARS,
    build_chat_messages,
    build_tool_specs,
    summarize_request,
    suggestion_prompt,
)
from ..pipeline import NoDataError, analyze_signals
from ..schemas import AnalysisResult


class ChatEngine:
    def __init__(self, cfg: AppConfigs, llm: LlmClient, kb: KnowledgeBase, store,
                 budget: Optional[ContextBudget] = None, blf_cfg=None):
        self.cfg = cfg
        self.llm = llm
        self.kb = kb
        self.store = store
        # BLF 侧配置（signals_blf.yaml 校验结果）；None 时 .blf 文件按"未启用 BLF"报错
        self.blf_cfg = blf_cfg
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
        r = self.llm.chat(
            summarize_request(transcript, budget_tokens),
            chat_id=getattr(self._tls, "chat_id", ""),
            temperature=0.1, max_tokens=min(800, max(120, budget_tokens)),
            round_no=0, user_text="context_summary",
        )
        return "" if r.error else r.content

    # ------------------------------------------------------------ 入口
    def events(self, chat, text: str):
        """统一事件流：{"event": "delta"|"message"|"done", "data": {...}}。

        - delta.channel: meta / reasoning / content / reset
        - message.data: 与 GET /api/chats 中同构的消息 dict
        - reset: LLM 预览失败，前端应丢弃本轮未落库的流式内容
        """
        yield self._delta("meta", "", engine=self._engine_name())
        yield from self._run_stream(chat, text)
        yield {"event": "done", "data": {"chat_id": chat.chat_id}}

    def _engine_name(self) -> str:
        return "llm" if self.llm.available else "offline"

    # ------------------------------------------------------------ 核心调度
    def _run_stream(self, chat, text: str):
        self.store.add_message(chat, "user", "text", content=text)
        # 用户另起一轮提问：此前的候选面板视为已回答，前端折叠为「已选择」
        self.store.mark_panels_consumed(chat)
        self._seal_steps(chat)

        if self.llm.available:
            state = {"claimed": False, "failed": False}
            yield from self._run_llm_stream(chat, text, state)
            if not state["failed"]:
                return
            if not state["claimed"]:
                # 只流出了预览、尚未落库 → 让前端清空，避免与离线正文重复
                yield self._delta("reset", "")
            # 已落库部分保留，继续离线兜底补全本轮结论

        yield from self._run_offline_stream(chat, text)

    # ------------------------------------------------------------ 离线路由（带步骤）
    def _run_offline_stream(self, chat, text: str):
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
        for m in self._analyze(chat, target["key"], profile=None):
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
        return fitted, self._step_on(chat, [("上下文压缩", note, "info")])

    def _run_llm_stream(self, chat, text: str, state: Dict[str, bool]):
        file_names = [f.name for f in chat.files]
        selected = {
            "function": chat.selected_function,
            "condition": chat.selected_condition,
            "profile": chat.selected_profile,
        }
        history = [
            {"role": m.role, "kind": m.kind, "content": m.content}
            for m in chat.messages[:-1]
        ]
        raw = build_chat_messages(self.cfg, history, text, file_names, selected)
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
            ),
            "list_conditions": lambda args: list_conditions(
                self.cfg,
                args.get("function_key") or chat.selected_function or "",
            ),
            "recommend_targets": lambda args: recommend_targets(
                self.cfg, args.get("query", text), args.get("max_items", 8),
                function_key=chat.selected_function,
            ),
            "run_analysis_on_files": lambda args: self._analyze(
                chat, args.get("condition_id"), args.get("profile"), as_tool=True
            ),
        }
        # 工具规格由配置生成（维度/工况 id 作 enum），已选定功能时进一步收窄取值域
        tool_specs = build_tool_specs(self.cfg, scope_function=chat.selected_function)
        previewed = False   # 是否已向用户流出了正文预览
        try:
            for rnd in range(1, 7):
                reply = None
                for ev in self.llm.chat_stream(
                        messages, tools=tool_specs, chat_id=chat.chat_id,
                        user_text=text, round_no=rnd):
                    if ev["type"] == "delta":
                        yield self._delta(ev["channel"], ev["text"])
                        if ev["channel"] == "content":
                            previewed = True
                    else:
                        reply = ev["reply"]
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

                for tc in reply.tool_calls:
                    name, args = tc["name"], tc["arguments"]
                    arg_s = json.dumps(args, ensure_ascii=False)[:160]
                    yield self._mevent(self._step_on(chat, [(f"调用工具 {name}", f"参数: {arg_s}", "run")]))
                    fn = exec_tools.get(name)
                    if fn is None:
                        payload = {"error": f"未知工具 {name}"}
                        yield self._mevent(self._step_on(chat, [(f"{name} 失败", "未知工具", "error")]))
                    else:
                        if name == "run_analysis_on_files" and not args.get("profile"):
                            missing = self._profile_guard(chat, args.get("condition_id"))
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
                            payload = {"ok": True, "note": "分析结果已生成并展示"}
                            for m in result or []:
                                state["claimed"] = True
                                yield self._mevent(m)
                            yield self._mevent(self._step_on(chat, [
                                (f"{name} 完成", f"生成 {len(result or [])} 条消息", "ok")]))
                        else:
                            payload = result
                            hop = (result or {}).get("hop")
                            n_cand = len(result.get("candidates", [])) if isinstance(result, dict) else 0
                            n_cond = (result.get("count") if isinstance(result, dict) else None)
                            yield self._mevent(self._step_on(chat, [(
                                f"{name} → hop={hop}",
                                (f"清单 {n_cond} 条（仅本轮可见）" if hop == "catalog" else
                                 f"候选：{n_cand} 项"),
                                "ok")]))
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
            state["failed"] = True

    def _profile_guard(self, chat, condition_id: str):
        """声明了档位的功能未选档 → 返回档位面板；不需要拦时返回 None。

        `hop=resolved` 只保证工况唯一，不保证档位唯一；离线路径本来就是"先选档再分析"，
        在线路径不给这个例外的话，同一个问题会有两套结论口径。
        """
        cond = self.cfg.condition(condition_id or "")
        if cond is None:
            return None
        fn = self.cfg.function(cond.function)
        if fn is None or not fn.profiles:
            return None
        return self._options(chat, {
            "hop": "profile", "function": fn.key,
            "target": {"key": cond.id, "name": cond.name},
            "candidates": [{"key": cond.id, "name": cond.name}],
            "profiles": fn.profiles, "profile_dims": fn.profile_dims,
            "hint": "该功能需先选档位再分析",
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
                 as_tool: bool = False) -> List:
        cond = self.cfg.condition(condition_id or "")
        if cond is None:
            return [self._msg(chat, "error", f"未知道况：{condition_id}")]
        if not chat.files:
            return [self._msg(chat, "error", "会话内还没有数据文件，请先上传 .mf4/.blf 文件再分析。")]

        chat.selected_function = cond.function
        chat.selected_condition = cond.id
        chat.selected_profile = profile

        # 新一轮动作（点选面板/工具调用）另起一个步骤块
        self._seal_steps(chat)
        step_msg = self._step_on(chat, [
            (f"run_analysis_on_files：{cond.name}",
             f"文件 {len(chat.files)} 个 · 工况 {cond.id}"
             + (f" · 档位 {profile}" if profile else ""), "run")])
        added: List = [step_msg]

        results: List[AnalysisResult] = []
        errors: List[str] = []
        for f in chat.files:
            sess = self.store.session(f.file_id)
            try:
                if sess.signals is None:
                    sess.signals = self._loader_for(f.path).load(f.path)
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
                errors.append(f"{f.name}: 分析异常 {e}")

        valid = sum(1 for r in results for s in r.samples if s.window is not None)
        step2 = self._step_on(chat, [
            (f"分段+指标：{len(results)} 份数据", f"样本窗口 {valid} 个 · 失败 {len(errors)} 份",
             "ok" if not errors else "warn")])
        if step2 is not step_msg:
            added.append(step2)
        for res in results:
            added.append(self._analysis_msg(chat, res))
        if errors:
            added.append(self._msg(chat, "error", "部分文件分析失败：\n" + "\n".join(errors)))
        if not results and not errors:
            added.append(self._msg(chat, "error", "没有可分析的文件。"))
        return added

    def _maybe_suggest(self, res: AnalysisResult, cond) -> None:
        """LLM 可用且有异常/缺失时，生成 calibration_actions；否则降级标记。"""
        fn = self.cfg.function(cond.function)
        if not self.llm.available:
            res.degraded = True
            return
        snippets = self._kb_snippets(res, fn)
        user = suggestion_prompt(self.cfg, fn, cond, res, snippets)
        if user is None:
            return
        case_hint = self._case_hint(res, fn)
        system = (
            "你是制动标定助手。基于给定的异常指标、规则结论与知识库片段，"
            "只输出结构化 JSON。可标定量必须取自各指标的 tuning_params。"
            + (f"\n{case_hint}" if case_hint else "")
        )
        sug = self.llm.complete_json(system, user)
        if sug:
            res.llm_suggestion = sug
        else:
            res.degraded = True

    def _kb_snippets(self, res: AnalysisResult, fn) -> List[str]:
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
                        out.append(f"[{ref}] {sec.text[:300]}")
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
            lines.append(f"- {c.case_id}: {c.data.get('root_cause_hypothesis', '')[:60]} 动作[{a}]")
        return "\n".join(lines)

    # ------------------------------------------------------------ 消息工厂
    def _msg(self, chat, kind: str, content: str, data=None):
        return self.store.add_message(chat, "assistant", kind, content=content, data=data)

    def _step_on(self, chat, items: List):
        """把步骤落进本轮最近的未封存 steps 消息（追加），否则新建一条。"""
        rows = []
        for it in items:
            label, detail, status = (list(it) + [None, "info"])[:3]
            rows.append({"label": label, "detail": detail, "status": status or "info"})
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
        head = (
            f"工况「{res.condition_name}」分析完成：{len(res.samples)} 个事件样本"
            + (f"，{n_bad} 项异常/缺失" if n_bad else "，全部达标")
            + ("（LLM 建议不可用，仅规则结论）" if res.degraded else "")
        )
        return self.store.add_message(chat, "assistant", "analysis_result", content=head, data=d)

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
