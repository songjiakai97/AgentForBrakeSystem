"""试验编号（file_id）解析（design.md §17）。

编号是试验大纲给的章节号，如 ``4.1.1``、``4.1.3.1``，一个工况可以有多个
（对开路面左右各一个）。历史数据直接用编号命名文件，因此**文件名是编号的
唯一来源**：``4.1.1_20260801_01.blf``、``4-1-1-01.blf`` 都归 4.1.1。

匹配口径（token 化，2026-11-22 定版；用户确认支持车型前缀）：
- stem 按 ``[-_. ]+`` 切 token；编号各分量（int 归一，``01`` ≡ ``1``）须在
  token 流中**连续**出现且逐个相等；
- **前缀允许**：编号前的 token 可以是车型代号等非纯数字内容
  （``SA3-4-1-1``、``carA 4.1.1``），也可以是 8 位试验日期
  （``20260801-4-1-1``）；
- 编号 token 的**前邻若是其它纯数字**（非 8 位日期）→ 拒绝：那是更长数字链
  的后缀（``3-4-1-1`` 是 3.4.1.1 的一部分，不能当成 4.1.1）；
- 多个编号同时命中时取分量数最长（最具体）的那个：
  4.1 与 4.1.1 都配置时，``4.1.1_x`` 归 4.1.1；4.1.1.2 未配置时 ``4.1.1.2_x``
  回落到 4.1.1（前缀兜底，与历史行为一致）；
- 编号后缀的纯数字不做拒绝（同样走最长优先兜底）；
- 扩展名无关（stem 匹配）。

这里只做纯字符串运算，不碰文件系统、不读配置，方便单测。
"""

import os
import re
from typing import Dict, Iterable, List, Optional, Tuple

SEP = r"[.\-]"
# token 切分：编号/前缀/日期之间的分隔符（点、连字符、下划线、空格均等价）
_SEP_TOKENS = re.compile(r"[-_. ]+")
_FILE_ID_RE = re.compile(r"^\d+(?:\.\d+)*$")


def is_valid_file_id(raw: str) -> bool:
    return bool(_FILE_ID_RE.match(raw or ""))


def normalize_file_id(raw: str) -> str:
    """查询侧归一：``4-1-3-1`` 与 ``4.1.3.1`` 视为同一编号。"""
    return re.sub(r"[-\s]+", ".", (raw or "").strip())


_NUM_TOKEN_RE = re.compile(r"\d+")
_DATE_TOKEN_RE = re.compile(r"20\d{6}")


def _tokens_contain_id(tokens: List[str], file_id: str) -> bool:
    """编号分量序列（int 归一）是否在 token 流中连续出现，且不被前邻数字链截胡。"""
    seq = [int(p) for p in file_id.split(".")]
    if len(seq) > len(tokens):
        return False
    for i in range(len(tokens) - len(seq) + 1):
        window = tokens[i:i + len(seq)]
        if not all(_NUM_TOKEN_RE.fullmatch(t) for t in window):
            continue                      # 窗口里有非数字 token，不可能组成编号
        if [int(t) for t in window] != seq:
            continue                      # 数值对不上
        if i > 0:
            prev = tokens[i - 1]
            # 前邻是纯数字（且不是 8 位试验日期）→ 这是更长数字链的后缀，拒绝：
            # 3-4-1-1 不能当成 4.1.1；20260801-4-1-1 的日期前缀则放行
            if _NUM_TOKEN_RE.fullmatch(prev) and not _DATE_TOKEN_RE.match(prev):
                continue
        return True
    return False


def file_stem(filename: str) -> str:
    return os.path.splitext(os.path.basename(filename or ""))[0]


class FileIdIndex:
    """file_id ↔ condition_id 的双向索引 + 文件名匹配。"""

    def __init__(self, mapping: Dict[str, str]):
        self._by_id: Dict[str, str] = dict(mapping)
        # 长的在前：前缀互相包含时先试更具体的编号（多命中取最长）
        self._sorted_ids: List[str] = sorted(self._by_id, key=lambda s: (-len(s), s))

    def __contains__(self, file_id: str) -> bool:
        return normalize_file_id(file_id) in self._by_id

    def condition_id(self, file_id: str) -> Optional[str]:
        return self._by_id.get(normalize_file_id(file_id))

    def match(self, filename: str) -> Optional[str]:
        stem = file_stem(filename)
        tokens = [t for t in _SEP_TOKENS.split(stem) if t]
        for fid in self._sorted_ids:
            if _tokens_contain_id(tokens, fid):
                return fid
        return None

    def all_ids(self) -> List[str]:
        return list(self._by_id)


def build_index(pairs: Iterable[Tuple[str, str]]) -> FileIdIndex:
    """pairs = (file_id, condition_id)。重复的 file_id 由配置层先拦下。"""
    return FileIdIndex(dict(pairs))
