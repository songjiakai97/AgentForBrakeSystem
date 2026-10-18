"""Loader 基类与 SignalData 数据结构。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np


@dataclass
class SignalData:
    ts: np.ndarray
    values: np.ndarray
    choices: Optional[dict] = None       # {raw_int: 枚举字符串}，非枚举通道为 None
    description: str = ""
    unit: str = ""
    start_timestamp: float = 0.0         # 全局最早时间戳，ts 已相对它偏移


def _parse_msg_id(msg_id) -> tuple:
    """
    解析候选中的 msg_id：
    - "0x123"  → (0x123, False)  标准帧
    - "0x123x" → (0x123, True)   扩展帧
    - int      → (int,   False)  兼容旧格式，按标准帧处理
    返回 (frame_id, is_extended)
    """
    if isinstance(msg_id, str):
        s = msg_id.strip().lower()
        if s.endswith('x'):
            return int(s[:-1], 16), True
        return int(s, 16), False
    return int(msg_id), False


class Loader(ABC):
    """
    信号提取器基类。

    约定：
    - 构造函数只接收「解析配置」（signal_maps / dbc_files），一次构建、多次复用。
    - load(file_path) 只接收「数据文件路径」，可对同一实例反复调用。
    """

    @abstractmethod
    def load(self, file_path: str) -> Dict[str, SignalData]:
        raise NotImplementedError
