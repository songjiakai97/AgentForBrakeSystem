"""外部 MCP 工具的注册表：命名、schema 转换、结果压平（design.md §6.11）。

这一层刻意做成**纯函数**（不碰 asyncio、不碰网络），因为真正会出错的地方都在这里：
工具名要能进 OpenAI 的 function name 字符集、外部 server 的 JSON Schema 要能当参数
规格用、外部返回的多块 content 要能压成一段模型读得懂的 JSON。异步那一层（hub.py）
只负责"把请求送到 server 再拿回来"。
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

# OpenAI 兼容端点对 function name 的通行约束：[a-zA-Z0-9_-]{1,64}。
# MCP 只要求工具名是字符串，实测见过 "get file" / "查询.工况" 这类写法，
# 原样送进去会被端点 400 拒掉——而且报错发生在 LLM 请求里，用户只会看到"回退离线"。
_NAME_OK = re.compile(r"[^A-Za-z0-9_-]+")
SEP = "__"
MAX_NAME_LEN = 64
# 外部工具描述进 prompt 的默认字数上限（可被 mcp.yaml 的 desc_chars 覆盖）
DEFAULT_DESC_CHARS = 400


def sanitize_raw(name: str) -> str:
    """把外部工具名清洗成合法字符片段（不含命名空间前缀）。

    全非 ASCII 的名字（中文工具名很常见）会清洗成空串，统一回退 "tool" 后由
    `_unique` 编号成 tool / tool_2。名字因此不表意——表意靠 description，
    而 /api/mcp 里同时给出 raw 原名，排查时能对上。
    """
    out = _NAME_OK.sub("_", (name or "").strip()).strip("_-")
    return out or "tool"


def external_tool_name(server: str, raw_tool: str) -> str:
    """mcp__<server>__<清洗后的原名>。

    内部四个工具名（resolve_condition 等）不含 `__`，所以带前缀的名字天然不会与
    它们撞车；不同 server 之间的同名工具也靠前缀区分。
    """
    prefix = f"mcp{SEP}{server}{SEP}"
    body = sanitize_raw(raw_tool)
    full = prefix + body
    if len(full) > MAX_NAME_LEN:
        # 原名过长时截原名、保住前缀：前缀是"这是外部工具"的唯一线索
        full = prefix + body[:max(1, MAX_NAME_LEN - len(prefix))].rstrip("_-")
    return full


def _unique(candidate: str, taken: set) -> str:
    """清洗后可能撞名（"get file" 与 "get/file" 都变 get_file）——加序号而不是覆盖。

    静默丢弃第二个会让用户看到「这个 server 有 8 个工具」却只注册了 7 个。
    序号本身也计入 64 字符上限：先给序号腾位置再截，否则撞名后的名字反而超长，
    被端点拒的是整份工具表（一个名字坏，所有工具都没了）。
    """
    if candidate not in taken:
        taken.add(candidate)
        return candidate
    i = 2
    while True:
        suffix = f"_{i}"
        base = candidate
        if len(base) + len(suffix) > MAX_NAME_LEN:
            base = base[:MAX_NAME_LEN - len(suffix)].rstrip("_-")
        out = base + suffix
        if out not in taken:
            taken.add(out)
            return out
        i += 1


def clean_description(text: Any, limit: Optional[int] = None) -> str:
    """外部描述是**不可信输入**：去标签 + 压空白 + （可选）截断。

    它会被拼进 system 级工具规格，等于让第三方 server 给我们的模型写提示词。
    去 `<...>` 防它仿冒我们自己的 <会话数据文件>/上下文标签；截断防"整段手册
    塞进每轮请求"。这不构成完整防御（见 §15 风险），但挡掉最省事的两种滥用。

    limit=None 表示"调用方已经按自己的额度截过了"——hub 里每个 server 有自己的
    desc_chars，清洗在连接期做一次，汇总期只做命名，不重复截断。
    """
    s = str(text or "")
    s = re.sub(r"<[^>]{0,80}>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if limit is not None and len(s) > limit:
        s = s[:limit].rstrip() + "…"
    return s


def _parameters(schema: Any) -> Dict[str, Any]:
    """MCP 的 input_schema → OpenAI 的 parameters。

    外部 server 可能给 null / 非 object / 带 $ref 的 schema。这里只保证两件事：
    一定是一个合法的 object schema（否则端点直接 400），以及不认识的键原样透传
    （多数端点会忽略，个别会用到 enum）。
    """
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    out = dict(schema)
    out.setdefault("type", "object")
    if out["type"] != "object":
        out["type"] = "object"
    props = out.get("properties")
    if not isinstance(props, dict):
        props = {}
    out["properties"] = props
    req = out.get("required")
    # required 里点名了 properties 中没有的键 → 端点会拒；剔掉而不是拒整个工具
    out["required"] = [k for k in req if isinstance(k, str) and k in props] \
        if isinstance(req, list) else []
    if not out["required"]:
        out.pop("required", None)
    return out


def build_external_specs(tools: List[Tuple[str, str, str, Any]],
                         desc_chars: Optional[int] = DEFAULT_DESC_CHARS
                         ) -> Tuple[List[dict], Dict[str, Tuple[str, str]]]:
    """(server, raw_name, description, input_schema) 列表 → OpenAI 工具规格 + 反查表。

    返回的 map 是 `外部工具名 -> (server, raw_name)`：执行时用它找回真正的 server 与
    原始工具名（清洗过的名字 server 不认识）。

    desc_chars=None 表示"描述已由调用方按各自额度清洗过"（hub 走这条：每个 server
    的 desc_chars 不同，截断在连接期做一次）。这里给默认值是为了直接调用时也不会
    把外部描述原样塞进 prompt。
    """
    specs: List[dict] = []
    taken: set = set()
    mapping: Dict[str, Tuple[str, str]] = {}
    for server, raw, desc, schema in tools:
        name = _unique(external_tool_name(server, raw), taken)
        note = clean_description(desc, desc_chars)
        specs.append({
            "type": "function",
            "function": {
                "name": name,
                "description": (f"[外部 MCP · {server}] {note}"
                                if note else f"[外部 MCP · {server}]（该服务未提供说明）"),
                "parameters": _parameters(schema),
            },
        })
        mapping[name] = (server, raw)
    return specs, mapping


def _block_text(block: Any) -> Optional[str]:
    t = getattr(block, "type", None)
    if t == "text":
        return str(getattr(block, "text", "") or "")
    if t in ("image", "audio"):
        # 本仓库的模型通道没有多模态输入，把 base64 塞进上下文只会白花 token
        mime = getattr(block, "mime_type", "") or ""
        return f"（外部返回 {t} 内容{('，' + mime) if mime else ''}，本 Agent 不解析二进制）"
    if t == "resource":
        return "（外部返回资源引用，本 Agent 暂不自动读取）"
    return f"（未支持的内容块类型 {t!r}）"


def flatten_result(content: Any, structured: Any = None, is_error: bool = False,
                   limit: int = 4000) -> Dict[str, Any]:
    """CallToolResult → 可直接回灌模型的 dict。

    三条口径：
    - 多个 text 块**按顺序拼接**而不是只取第一个：不少 server 把"结论"和"明细"
      分两块返回，只取第一块等于丢掉答案。
    - structuredContent 存在时优先给结构化字段（模型读得更好），但仍保留 text 兜底。
    - is_error 必须显式带出去：外部工具"成功返回了一条错误文本"很常见，
      当成正常结果回灌会让模型把错误当事实引用。
    """
    parts: List[str] = []
    if isinstance(content, list):
        for b in content:
            txt = _block_text(b)
            if txt:
                parts.append(txt)
    elif content is not None:
        parts.append(str(content))
    text = "\n".join(parts).strip()

    payload: Dict[str, Any] = {"ok": not is_error, "source": "mcp"}
    if structured is not None:
        payload["data"] = structured
    if text:
        payload["text"] = text
    if is_error:
        payload["note"] = "外部工具执行失败，以下是它给的原文，不要当成事实引用"
    # 序列化后超限就降级：半截 JSON 比长文本更坏（模型会照着坏结构继续编）
    s = json.dumps(payload, ensure_ascii=False, default=str)
    if len(s) > limit:
        if "data" in payload:
            payload.pop("data")
        body = payload.get("text", "")
        budget = limit - 200
        if len(body) > budget:
            payload["text"] = body[:budget].rstrip() + "…（外部返回过长，已截断）"
    return payload


def parse_args(raw: Any) -> Tuple[Dict[str, Any], Optional[str]]:
    """模型给的 arguments 必须是对象；不是对象时给明确错误而不是抛异常。

    兼容端点偶尔把参数发成 "null" 或字符串，直接 **args 会 TypeError，
    而那个异常在本仓库的编排里会被记成"LLM 失败"并回退离线——诊断信息全丢。
    """
    if raw is None or raw == "":
        return {}, None
    if isinstance(raw, dict):
        return raw, None
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            return {}, f"参数不是合法 JSON：{e}"
        if isinstance(obj, dict):
            return obj, None
        return {}, "参数必须是 JSON 对象"
    return {}, f"参数类型不支持：{type(raw).__name__}"
