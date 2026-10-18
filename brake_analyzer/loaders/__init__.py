"""数据读取层：MF4 / BLF 信号提取。

约定（见 docs/design.md §6.2）：
- 构造函数只接收「解析配置」（signal_maps / dbc_files），一次构建、多次复用。
- load(file_path) 只接收「数据文件路径」，可对同一实例反复调用。
- 缺失信号返回空 ts/values，不抛异常，由指标/规则层标记 missing。
"""

from .base import Loader, SignalData
from .mf4 import MF4Loader
from .blf import BLFLoader

__all__ = ["Loader", "SignalData", "MF4Loader", "BLFLoader"]
