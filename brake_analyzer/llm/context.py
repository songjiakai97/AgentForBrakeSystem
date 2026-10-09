"""上下文预算与压缩（design.md §6.10）。

一轮 LLM 请求的 messages 不能无限增长。预算来自 .env：

- CONTEXT_MAX_TOKENS：单次请求允许的最大上下文（估算 token），超出即硬截断兜底；
- CONTEXT_COMPRESS_TOKENS：达到该值即触发压缩，把最早的若干轮折叠成一条摘要消息。

不引入 tokenizer 依赖（core/web 环境未必装 tiktoken），用「中日韩字符≈1 token，
其余 4 字符≈1 token」的启发式估算；估算偏小时由 CONTEXT_MAX_TOKENS 硬截断兜底。
"""

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

SUMMARY_MARK = "[历史摘要]"
SUMMARY_ROLE = "system"

PER_MESSAGE_TOKENS = 4      # role/分隔符等固定开销，与 OpenAI 计数的常见近似一致
REPLY_PRIMING_TOKENS = 3    # 每次回复的 priming 开销
MIN_SUMMARY_TOKENS = 160    # 摘要再小就没信息量了
KEEP_RECENT_UNITS = 2       # 折叠时保留最近这么多个轮次单元不参与摘要
SUMMARY_SHARE = 0.15        # 摘要预算占硬上限的比例
EXTRACTIVE_CLIP_CHARS = 180  # 抽取式兜底摘要里，单条正文的字数上限
UNIT_HEADER_TOKENS = 12     # 摘要里每个轮次单元的编号头
SUMMARY_CACHE_MAX = 64      # 摘要缓存条数上限，防止长跑服务攒住旧对话文本
CLIP_MARK = "…（超预算已截断）"
CLIP_MARK_TOKENS = 12       # 截断标记自身占的 token，预留以免裁完反而变长

_CJK_RE = re.compile(
    "[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]"
)


def estimate_text_tokens(text: str) -> int:
    """启发式 token 估算：CJK 字符按 1，其余按 4 字符 1 token。"""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    return cjk + math.ceil((len(text) - cjk) / 4)


def estimate_message_tokens(messages: Sequence[dict]) -> int:
    """整份 messages 的估算 token，含 tool_calls 的参数 JSON。"""
    total = 0
    for m in messages:
        total += PER_MESSAGE_TOKENS
        content = m.get("content")
        if isinstance(content, str):
            total += estimate_text_tokens(content)
        elif content is not None:
            total += estimate_text_tokens(str(content))  # 结构化内容按字面量估
        for tc in m.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            total += estimate_text_tokens(str(fn.get("name") or ""))
            total += estimate_text_tokens(str(fn.get("arguments") or ""))
        if m.get("tool_call_id"):
            total += 4
    return total + REPLY_PRIMING_TOKENS


def _pos_int(raw: Optional[str], default: int, name: str) -> int:
    text = (raw or "").strip()
    if not text:
        return default
    try:
        value = int(text)
    except ValueError:
        raise ValueError(f"{name} 需为整数 token 数，当前为 {raw!r}")
    if value <= 0:
        raise ValueError(f"{name} 需为正整数 token 数，当前为 {value}")
    return value


@dataclass
class ContextBudget:
    """两级阈值；压缩阈值必须低于硬上限，否则来不及压就撞墙。"""

    max_tokens: int
    compress_tokens: int

    @property
    def summary_tokens(self) -> int:
        return max(MIN_SUMMARY_TOKENS, int(self.max_tokens * SUMMARY_SHARE))

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "ContextBudget":
        src = env if env is not None else os.environ
        max_tokens = _pos_int(src.get("CONTEXT_MAX_TOKENS"), 8000, "CONTEXT_MAX_TOKENS")
        compress_tokens = _pos_int(
            src.get("CONTEXT_COMPRESS_TOKENS"),
            max(1024, int(max_tokens * 0.75)),
            "CONTEXT_COMPRESS_TOKENS",
        )
        if compress_tokens >= max_tokens:
            raise ValueError(
                f"CONTEXT_COMPRESS_TOKENS({compress_tokens}) 必须小于 "
                f"CONTEXT_MAX_TOKENS({max_tokens})"
            )
        return cls(max_tokens=max_tokens, compress_tokens=compress_tokens)

    def to_dict(self) -> dict:
        return {"max_tokens": self.max_tokens, "compress_tokens": self.compress_tokens}


# ---------------------------------------------------------------- 轮次单元

def group_units(body: Sequence[dict]) -> List[List[dict]]:
    """切分轮次单元：一条非 tool 消息起头，其后的 tool 结果归入同一单元。

    折叠必须整单元进行，否则 assistant 的 tool_calls 会与对应的 tool 消息拆散，
    OpenAI 兼容端点直接报 400。
    """
    units: List[List[dict]] = []
    for m in body:
        if m.get("role") == "tool" and units:
            units[-1].append(m)
        else:
            units.append([m])
    return units


def flatten(units: Sequence[Sequence[dict]]) -> List[dict]:
    return [m for u in units for m in u]


def is_summary(msg: dict) -> bool:
    content = msg.get("content")
    return (msg.get("role") == SUMMARY_ROLE
            and isinstance(content, str)
            and content.startswith(SUMMARY_MARK))


def _char_limit(tokens: int, text: str) -> int:
    """把 token 预算换成字符上限：纯 ASCII 文本 1 token ≈ 4 字符。"""
    return tokens if _CJK_RE.search(text[:400]) else tokens * 4


Summarizer = Callable[[str, int], str]

# 按需注入的工具结果标了 transient：清单原文只服务当次定位，之后换成一行存根。
TRANSIENT_KEY = "transient"
STUB_PREFIX = "[已折叠]"


def _is_transient_tool(msg: dict) -> bool:
    """tool 结果是否为按需注入的一次性清单。

    工具 payload 会被截到 TOOL_PAYLOAD_CLIP_CHARS，截断后 JSON 解析失败，
    因此先看能否 parse，不能就退化为查前若干字符里的 "transient": true
    （工具的 dict 把这个键放在前面，截断不影响识别）。
    """
    content = msg.get("content")
    if msg.get("role") != "tool" or not isinstance(content, str):
        return False
    try:
        payload = json.loads(content)
        return isinstance(payload, dict) and bool(payload.get(TRANSIENT_KEY))
    except Exception:
        return '"transient": true' in content[:400]


def prune_transient(messages: Sequence[dict],
                    keep_last: int = KEEP_RECENT_UNITS) -> Tuple[List[dict], int]:
    """把较早的一次性清单/候选结果换成一行存根，保留最近 keep_last 个轮次单元。

    动机（§13 + §6.10）：工况清单、候选面板是"用完即弃"的内容，留着既占预算，
    又会让后续轮次误把旧候选当成可选项。但模型至少要在清单给出的下一次请求里
    看到它，所以最近 keep_last 个单元原样保留；也不能整条删掉——assistant 的
    tool_calls 失去应答，兼容端点会报 400，故只压正文、保留消息壳。
    """
    msgs = list(messages)
    i = 0
    while i < len(msgs) and msgs[i].get("role") == "system":
        i += 1
    head, body = msgs[:i], msgs[i:]
    units = group_units(body)
    if len(units) <= keep_last:
        return msgs, 0
    old, fresh = units[:-keep_last], units[-keep_last:]
    out: List[dict] = []
    n = 0
    for u in old:
        new_u = []
        for m in u:
            if _is_transient_tool(m) and not str(m.get("content", "")).startswith(STUB_PREFIX):
                m2 = dict(m)
                m2["content"] = _stub_for(str(m.get("content") or ""))
                new_u.append(m2)
                n += 1
            else:
                new_u.append(m)
        out.extend(new_u)
    if n == 0:
        return msgs, 0
    return head + out + flatten(fresh), n


def _stub_for(content: str) -> str:
    hop = "?"
    m = re.search(r'"hop":\s*"([a-z_]+)"', content)
    if m:
        hop = m.group(1)
    rows = len(re.findall(r'"key":', content))
    names = "、".join(re.findall(r'"name":\s*"([^"]{1,40})"', content)[:6])
    stub = (f"{STUB_PREFIX} 一次性候选/清单已折叠：hop={hop} 约 {rows} 项"
            + (f"（{names}）" if names else "") + "…（原文已折叠）")
    return stub[:240]


class ContextCompressor:
    """超预算时把早期轮次折叠为一条摘要，必要时硬截断兜底。

    summarizer(transcript, budget_tokens) -> str 由调用方注入（走 LLM）；
    未注入或返回空时退化为抽取式（逐条截断拼接），摘要失败不会拖垮主链路。
    返回 (messages, note)：note 为人类可读的压缩说明，None 表示未触发。
    """

    def __init__(self, budget: ContextBudget, summarizer: Optional[Summarizer] = None):
        self.budget = budget
        self.summarizer = summarizer
        self._cache: Dict[str, str] = {}    # transcript+预算 → 摘要文本

    def fit(self, messages: Sequence[dict]) -> Tuple[List[dict], Optional[str]]:
        raw = estimate_message_tokens(list(messages))
        msgs, n_pruned = prune_transient(messages)
        before = estimate_message_tokens(msgs)
        prune_note = f"先折叠 {n_pruned} 条一次性清单" if n_pruned else ""
        if before <= self.budget.compress_tokens:
            if not n_pruned:
                return msgs, None
            # 折叠 transient 本身也是"只保留有效内容"，即便没到压缩阈值也要报告
            return msgs, (f"上下文 {raw} → {before} tok · {prune_note}")

        # 固定的系统提示在最前，逐条按角色分流；摘要可重复出现，折叠时并入新摘要
        i = 0
        while i < len(msgs) and msgs[i].get("role") == "system" and not is_summary(msgs[i]):
            i += 1
        head, body = msgs[:i], msgs[i:]
        # 既有摘要单独并入新摘要，不参与轮次折叠（历史里最多一条）
        summary_old = [m for m in body if is_summary(m)]
        body = [m for m in body if not is_summary(m)]
        units = group_units(body)

        fold_n = max(0, len(units) - KEEP_RECENT_UNITS)
        fold, keep = units[:fold_n], units[fold_n:]

        if fold or summary_old:
            summary = self._make_summary(fold, summary_old)
            out = head + [self._summary_msg(summary)]
            detail = f"摘要覆盖 {len(fold) + len(summary_old)} 段早期内容"
        else:
            # 没有可折叠的早期内容：正文只能就地裁；系统提示（功能目录）不裁，
            # 超限说明预算配得比固定开销还小，如实写进 note 而不是假装压下了
            out = list(head)
            detail = ("仅系统提示超限（预算小于固定开销，不裁剪）"
                      if not _trimmable(keep) else "单轮正文超预算，就地裁剪")

        kept, clip_note = self._fit_keep(out, keep)
        note = f"上下文 {raw} → {estimate_message_tokens(kept)} tok · {detail}"
        if n_pruned:
            note += f" · {prune_note}"
        if clip_note:
            note += f" · {clip_note}"
        return kept, note

    # ---------------- 保留部分的装入：先就地裁剪，装不下再整单元丢弃
    def _fit_keep(self, prefix: List[dict], keep: List[List[dict]]) -> Tuple[List[dict], str]:
        """把保留单元装进剩余预算。最后一个单元含本轮提问，绝不整单元丢弃。"""
        notes = []
        avail = self.budget.max_tokens - estimate_message_tokens(prefix)
        clipped = self._clip_units(keep, avail)
        if clipped:
            notes.append(f"截断 {clipped} 条超长内容")
        # 每条正文已有最小保留长度，裁到不能再裁时只能丢最老的整单元
        while len(keep) > 1 and _units_tokens(keep) > avail:
            dropped = keep.pop(0)
            notes.append(f"丢弃 {len(dropped)} 条（超硬上限）")
        if keep and _units_tokens(keep) > avail:
            notes.append("末轮仍超上限（已到最小保留）")
        return prefix + flatten(keep), " · ".join(notes)

    def _clip_units(self, units: List[List[dict]], avail: int) -> int:
        """从最老的正文开始就地截断，直到装得下；最新消息尽量保全。

        度量口径与 _fit_keep 一致（含每单元的固定余量），否则会裁完仍被整单元丢弃。
        """
        targets = [(i, j) for i, u in enumerate(units) for j, m in enumerate(u)
                   if isinstance(m.get("content"), str) and m["content"]]
        if not targets or avail <= 0:
            return 0
        total = _units_tokens(units)
        if total <= avail:
            return 0
        n = 0
        for i, j in targets:
            if total <= avail:
                break
            m = units[i][j]
            here = estimate_text_tokens(m["content"])
            keep = max(40, here - (total - avail) - CLIP_MARK_TOKENS)
            if keep >= here:
                continue
            new = dict(m)
            new["content"] = new["content"][:_char_limit(keep, new["content"])] + CLIP_MARK
            units[i][j] = new
            total -= here - estimate_text_tokens(new["content"])
            n += 1
        return n

    # ---------------- 摘要
    def _make_summary(self, units: List[List[dict]], old: List[dict]) -> str:
        transcript = self._transcript(units, old)
        budget = self.budget.summary_tokens
        key = hashlib.sha1(f"{budget}\n{transcript}".encode("utf-8")).hexdigest()
        # 同一份待折叠内容只摘要一次：多跳工具循环里每轮都会 fit，否则重复烧钱
        if key in self._cache:
            return self._cache[key]
        text = ""
        if self.summarizer:
            try:
                text = (self.summarizer(transcript, budget) or "").strip()
            except Exception:
                text = ""
        if not text:
            text = self._extractive(transcript)
        text = _clip_text(text, budget)
        self._cache[key] = text
        if len(self._cache) > SUMMARY_CACHE_MAX:
            self._cache = dict(list(self._cache.items())[-SUMMARY_CACHE_MAX:])
        return text

    def _transcript(self, units: List[List[dict]], old: List[dict]) -> str:
        parts: List[str] = []
        for m in old:
            parts.append(str(m.get("content", ""))[len(SUMMARY_MARK):].strip())
        for k, u in enumerate(units, start=1):
            lines = [f"轮次{k}"]
            for m in u:
                role = m.get("role", "?")
                content = m.get("content")
                if isinstance(content, str) and content.strip():
                    lines.append(f"{role}: {content.strip()[:300]}")
                for tc in m.get("tool_calls") or []:
                    fn = (tc or {}).get("function") or {}
                    lines.append(f"{role} 调用 {fn.get('name', '?')} "
                                 f"{str(fn.get('arguments') or '')[:120]}")
            parts.append(" / ".join(lines))
        return "\n".join(parts)

    def _extractive(self, transcript: str) -> str:
        lines = [ln.strip()[:EXTRACTIVE_CLIP_CHARS] for ln in transcript.splitlines()
                 if ln.strip()]
        return "早期对话要点：" + "；".join(lines) if lines else "早期对话要点：（无）"

    def _summary_msg(self, summary: str) -> dict:
        return {"role": SUMMARY_ROLE,
                "content": f"{SUMMARY_MARK} 更早对话的压缩摘要（非新的用户请求）：\n{summary}"}

    def stats(self, messages: Sequence[dict]) -> dict:
        """观测用：当前估算值与两条阈值的关系。"""
        est = estimate_message_tokens(messages)
        return {
            "estimated_tokens": est,
            "max_tokens": self.budget.max_tokens,
            "compress_tokens": self.budget.compress_tokens,
            "over_compress": est > self.budget.compress_tokens,
            "over_max": est > self.budget.max_tokens,
        }


def _units_tokens(units: Sequence[Sequence[dict]]) -> int:
    total = 0
    for u in units:
        total += UNIT_HEADER_TOKENS + estimate_message_tokens(list(u))
    return total


def _trimmable(units: Sequence[Sequence[dict]]) -> bool:
    """保留部分里是否还有可裁的正文（全空的 tool_calls 消息裁了也没用）。"""
    return any(isinstance(m.get("content"), str) and m["content"]
               for u in units for m in u)


def _clip_text(text: str, budget: int) -> str:
    if estimate_text_tokens(text) <= budget:
        return text
    limit = _char_limit(budget, text)
    return text[:max(0, limit)].rstrip() + "…"
