"""对话式编排引擎（design.md §6.9 / §13）。

- 所有对话经 ChatEngine 编排，统一走事件流：delta（推理/正文流式）→ message（落库）→ done。
- 有 OPENAI_API_KEY → LLM tool-calling（≤6 轮，流式），失败自动回退离线路由。
- 无 Key → 离线确定性路由（规则打分两跳推荐 + 规则结论 + 可选模板建议）。
- 路由/工具/分析过程落库为 kind=steps 消息，前端折叠展示。
"""

import json
from typing import Dict, List, Optional

from ..agent.tools import recommend_targets
from ..configs import AppConfigs
from ..llm.client import LlmClient
from ..llm.kb import KnowledgeBase
from ..llm.prompts import TOOL_SPECS, build_chat_messages, suggestion_prompt
from ..pipeline import NoDataError, analyze_signals
from ..schemas import AnalysisResult


class ChatEngine:
    def __init__(self, cfg: AppConfigs, llm: LlmClient, kb: KnowledgeBase, store):
        self.cfg = cfg
        self.llm = llm
        self.kb = kb
        self.store = store

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

    def stream(self, chat, text: str):
        """兼容旧调用名。"""
        return self.events(chat, text)

    def run(self, chat, text: str) -> List[dict]:
        """同步入口：处理一条用户消息，返回新增 assistant 消息 dict 列表。"""
        return [ev["data"] for ev in self.events(chat, text) if ev["event"] == "message"]

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
        messages = build_chat_messages(self.cfg, history, text, file_names, selected)
        exec_tools = {
            "recommend_targets": lambda args: recommend_targets(
                self.cfg, args.get("query", text), args.get("max_items", 8),
                function_key=chat.selected_function,
            ),
            "run_analysis_on_files": lambda args: self._analyze(
                chat, args.get("condition_id"), args.get("profile"), as_tool=True
            ),
        }
        previewed = False   # 是否已向用户流出了正文预览
        try:
            for rnd in range(1, 7):
                reply = None
                for ev in self.llm.chat_stream(
                        messages, tools=TOOL_SPECS, chat_id=chat.chat_id,
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
                            yield self._mevent(self._step_on(chat, [
                                (f"{name} → hop={hop}",
                                 f"候选：{len(result.get('candidates', []))} 项", "ok")]))
                            if hop in ("function", "condition", "profile"):
                                state["claimed"] = True
                                yield self._mevent(self._options(chat, result))
                            elif hop == "resolved":
                                # 把命中结果交还 LLM，由其决定文本/档位面板/分析
                                pass
                    messages.append({
                        "role": "tool", "tool_call_id": tc["id"],
                        "content": json.dumps(payload, ensure_ascii=False, default=str)[:4000],
                    })
                previewed = False
            # 轮数耗尽：按失败处理，让调用方兜离线
            state["failed"] = True
            if state["claimed"]:
                state["failed"] = False
        except Exception:
            state["failed"] = True

    # ------------------------------------------------------------ 分析
    def _analyze(self, chat, condition_id: str, profile: Optional[str],
                 as_tool: bool = False) -> List:
        cond = self.cfg.condition(condition_id or "")
        if cond is None:
            return [self._msg(chat, "error", f"未知道况：{condition_id}")]
        if not chat.files:
            return [self._msg(chat, "error", "会话内还没有数据文件，请先上传 .mf4 文件再分析。")]

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
                    from ..loaders.mf4 import MF4Loader

                    sess.signals = MF4Loader(self.cfg.signal_maps).load(f.path)
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
                m.data["items"].extend(rows)
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
