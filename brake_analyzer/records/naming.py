"""试验编号（file_id）解析（design.md §17）。

编号是试验大纲给的章节号，如 ``4.1.1``、``4.1.3.1``，一个工况可以有多个
（对开路面左右各一个）。历史数据直接用编号命名文件，因此**文件名是编号的
唯一来源**：``4.1.1_20260801_01.blf``、``4-1-1-01.blf`` 都归 4.1.1。

匹配口径（实测约定，不靠猜）：
- 编号内部的点写成 ``.`` 或 ``-`` 均可；
- 从文件名开头匹配，紧后一个字符不能是数字——否则 4.1.1 会把 4.1.10 吃掉；
- 多个编号同时命中时取最长的（最具体）那个，并可由调用方告警。

这里只做纯字符串运算，不碰文件系统、不读配置，方便单测。
"""

import os
import re
from typing import Dict, Iterable, List, Optional, Tuple

SEP = r"[.\-]"
_FILE_ID_RE = re.compile(r"^\d+(?:\.\d+)*$")


def is_valid_file_id(raw: str) -> bool:
    return bool(_FILE_ID_RE.match(raw or ""))


def normalize_file_id(raw: str) -> str:
    """查询侧归一：``4-1-3-1`` 与 ``4.1.3.1`` 视为同一编号。"""
    return re.sub(r"[-\s]+", ".", (raw or "").strip())


def compile_pattern(file_id: str) -> "re.Pattern[str]":
    parts = [re.escape(p) for p in file_id.split(".")]
    return re.compile("^" + SEP.join(parts) + r"(?!\d)")


def file_stem(filename: str) -> str:
    return os.path.splitext(os.path.basename(filename or ""))[0]


class FileIdIndex:
    """file_id ↔ condition_id 的双向索引 + 文件名匹配。"""

    def __init__(self, mapping: Dict[str, str]):
        self._by_id: Dict[str, str] = dict(mapping)
        # 长的在前：前缀互相包含时先试更具体的编号
        self._patterns: List[Tuple[str, "re.Pattern[str]"]] = [
            (fid, compile_pattern(fid))
            for fid in sorted(self._by_id, key=lambda s: (-len(s), s))
        ]

    def __contains__(self, file_id: str) -> bool:
        return normalize_file_id(file_id) in self._by_id

    def condition_id(self, file_id: str) -> Optional[str]:
        return self._by_id.get(normalize_file_id(file_id))

    def match(self, filename: str) -> Optional[str]:
        stem = file_stem(filename)
        for fid, pat in self._patterns:
            if pat.match(stem):
                return fid
        return None

    def all_ids(self) -> List[str]:
        return list(self._by_id)


def build_index(pairs: Iterable[Tuple[str, str]]) -> FileIdIndex:
    """pairs = (file_id, condition_id)。重复的 file_id 由配置层先拦下。"""
    return FileIdIndex(dict(pairs))
