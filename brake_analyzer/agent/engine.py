"""对话式编排引擎（design.md §6.9 / §13）。

- 所有对话经 ChatEngine 编排。
- 有 OPENAI_API_KEY → LLM tool-calling（≤6 轮），失败自动回退离线路由。
- 无 Key → 离线确定性路由（规则打分两跳推荐 + 规则结论 + 可选模板建议）。
- 产出统一消息：text / target_options / analysis_result / attachment / error。
"""

import json
from typing import List, Optional

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
    def run(self, chat, text: str) -> List[dict]:
        """同步入口：处理一条用户消息，返回新增 assistant 消息 dict 列表。"""
        return [m.to_dict() for m in self._run(chat, text)]

    def stream(self, chat, text: str):
        """SSE 流式入口，产出事件 dict。"""
        yield {"event": "reasoning", "data": {"stage": "route", "engine": self._engine_name()}}
        msgs = self._run(chat, text)
        for m in msgs:
            yield {"event": "message", "data": m.to_dict()}
        yield {"event": "done", "data": {"chat_id": chat.chat_id}}

    def _engine_name(self) -> str:
        return "llm" if self.llm.available else "offline"

    # ------------------------------------------------------------ 核心
    def _run(self, chat, text: str) -> List:
        self.store.add_message(chat, "user", "text", content=text)

        if self.llm.available:
            out = self._run_llm(chat, text)
            if out is not None:
                return out

        return self._run_offline(chat, text)

    # ------------------------------------------------------------ 离线路由
    def _run_offline(self, chat, text: str) -> List:
        rec = recommend_targets(self.cfg, text, max_items=8, function_key=chat.selected_function)
        hop = rec.get("hop")

        if hop == "error":
            return [
                self._msg(chat, "text", "未能从描述中确定分析目标。请补充路面/速度/动作，"
                                       "或直接选择功能。"),
                self._options(chat, {"hop": "function", "candidates": rec["candidates"],
                                     "hint": "可选功能："}),
            ]

        if hop == "function":
            return [
                self._msg(chat, "text", f"识别到多个可能的功能，请选择：{rec.get('hint', '')}"),
                self._options(chat, rec),
            ]

        if hop == "condition":
            fn = self.cfg.function(rec["function"])
            need_profile = bool(fn.profiles)
            return [
                self._msg(chat, "text", f"在「{fn.name}」下请选择要分析的工况"
                                       + ("（随后选择档位）" if need_profile else "") + "："),
                self._options(chat, rec),
            ]

        # resolved
        target = rec["target"]
        fn = self.cfg.function(rec["function"])
        chat.selected_function = rec["function"]
        chat.selected_condition = target["key"]
        if fn.profiles:
            # 需先选档位 → 交给前端 /select 触发分析
            return [
                self._msg(chat, "text", f"已定位工况「{target['name']}」，该功能需选择档位后再分析。"),
                self._options(chat, {"hop": "profile", "function": fn.key,
                                     "target": target, "candidates": [target],
                                     "profiles": fn.profiles,
                                     "profile_dims": fn.profile_dims}),
            ]
        return self._analyze(chat, target["key"], profile=None)

    # ------------------------------------------------------------ LLM 编排
    def _run_llm(self, chat, text: str) -> Optional[List]:
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
        added: List = []
        try:
            for rnd in range(1, 7):
                reply = self.llm.chat(
                    messages, tools=TOOL_SPECS, chat_id=chat.chat_id,
                    user_text=text, round_no=rnd,
                )
                if reply.error:
                    # 回退离线路由，保留已产出的文字
                    return None if not added else added + self._run_offline(chat, text)
                if reply.tool_calls:
                    asst = {"role": "assistant", "content": reply.content or ""}
                    asst["tool_calls"] = [
                        {"id": tc["id"], "type": "function",
                         "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
                        for tc in reply.tool_calls
                    ]
                    messages.append(asst)
                    for tc in reply.tool_calls:
                        fn = exec_tools.get(tc["name"])
                        if fn is None:
                            payload = {"error": f"未知工具 {tc['name']}"}
                        else:
                            result = fn(tc["arguments"])
                            if tc["name"] == "run_analysis_on_files":
                                # _analyze 已直接写消息；工具返回摘要
                                payload = {"ok": True, "note": "分析结果已生成并展示"}
                                added.extend(result or [])
                            else:
                                payload = result
                                hop = result.get("hop")
                                if hop in ("function", "condition", "profile"):
                                    added.append(self._options(chat, result))
                                elif hop == "resolved":
                                    pass
                        messages.append({
                            "role": "tool", "tool_call_id": tc["id"],
                            "content": json.dumps(payload, ensure_ascii=False, default=str)[:4000],
                        })
                    continue
                # 纯文本收尾
                if reply.content:
                    added.append(self._msg(chat, "text", reply.content))
                return added or [self._msg(chat, "text", "（无内容）")]
            return added or self._run_offline(chat, text)
        except Exception:
            return None if not added else added + self._run_offline(chat, text)

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

        added: List = []
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
