"""MF4 Loader：基于 asammdf 按候选通道名提取信号。"""

from typing import Any, Dict, List

import numpy as np
from asammdf import MDF

from .base import Loader, SignalData


class MF4Loader(Loader):
    """
    从 MF4 文件中按候选名依次提取信号。

    用法：
        loader = MF4Loader(signal_maps)
        data_a = loader.load("a.mf4")
        data_b = loader.load("b.mf4")
    """

    def __init__(self, signal_maps: Dict[str, Dict[str, Any]]):
        self.signal_maps = signal_maps

    def load(self, file_path: str) -> Dict[str, SignalData]:
        result: Dict[str, SignalData] = {}
        raw_data: Dict[str, tuple] = {}

        with MDF(file_path) as mdf:
            selections: List[tuple] = []
            for logical_name, spec in self.signal_maps.items():
                for cand in spec.get("candidates", []):
                    positions = mdf.whereis(cand)
                    if positions:
                        group, channel = positions[0]
                        selections.append((logical_name, cand, group, channel))
                        break

            if selections:
                select_list = [(n, g, c) for _, n, g, c in selections]
                try:
                    sigs_phy = mdf.select(select_list, raw=False)
                    if not isinstance(sigs_phy, list):
                        sigs_phy = [sigs_phy]
                except Exception:
                    sigs_phy = [None] * len(selections)

                # 物理值为文本的通道视为枚举：回取原始编码值构造 choices
                enum_indices = [
                    i for i, s in enumerate(sigs_phy)
                    if s is not None and s.samples.dtype.kind in ('U', 'S', 'O')
                ]
                if enum_indices:
                    enum_select_list = [select_list[i] for i in enum_indices]
                    try:
                        sigs_raw = mdf.select(enum_select_list, raw=True)
                        if not isinstance(sigs_raw, list):
                            sigs_raw = [sigs_raw]
                    except Exception:
                        sigs_raw = [None] * len(enum_indices)
                else:
                    sigs_raw = []

                raw_iter = iter(sigs_raw)
                for i, (logical_name, _, _, _) in enumerate(selections):
                    sig_phy = sigs_phy[i]
                    if sig_phy is None:
                        continue

                    timestamps = np.asarray(sig_phy.timestamps, dtype=np.float64)
                    phy_samples = sig_phy.samples
                    choices = None

                    if phy_samples.dtype.kind in ('U', 'S', 'O'):
                        sig_raw = next(raw_iter, None)
                        if sig_raw is None:
                            continue
                        raw_samples = sig_raw.samples
                        values = raw_samples.astype(np.float32)

                        choices = {}
                        unique_raw, unique_idx = np.unique(raw_samples, return_index=True)
                        for r, idx in zip(unique_raw, unique_idx):
                            p = phy_samples[idx]
                            if isinstance(p, bytes):
                                p_str = p.decode('utf-8', errors='replace')
                            elif isinstance(p, str):
                                p_str = p
                            else:
                                p_str = str(p)
                            choices[int(r)] = str(p_str)
                    else:
                        values = phy_samples.astype(np.float32)

                    raw_data[logical_name] = (timestamps, values, choices)

        all_start = [ts[0] for ts, _, _ in raw_data.values() if len(ts) > 0]
        start_timestamp = float(min(all_start)) if all_start else 0.0

        for logical_name, spec in self.signal_maps.items():
            description = spec.get("description", "")
            unit = spec.get("unit", "")
            if logical_name in raw_data:
                timestamps, values, choices = raw_data[logical_name]
                ts = timestamps - start_timestamp if len(timestamps) > 0 else timestamps
                result[logical_name] = SignalData(
                    ts=ts,
                    values=values,
                    choices=choices,
                    description=description,
                    unit=unit,
                    start_timestamp=start_timestamp,
                )
            else:
                result[logical_name] = SignalData(
                    ts=np.array([]),
                    values=np.array([]),
                    choices=None,
                    description=description,
                    unit=unit,
                    start_timestamp=start_timestamp,
                )

        return result
