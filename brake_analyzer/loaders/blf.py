"""BLF Loader：基于 python-can + cantools 按 DBC 定义解码 CAN 信号。

通道归属由 loaders/blf_channels.py 按 `signals_blf.yaml` 里的 msg_id 自动识别
（`channels: auto`），识别结果逐文件决定，因此：

- 同一 ID 出现在两条总线上、甚至代表不同信号也不会串线——解码用的
  `frame_lookup` 键始终带通道号；
- 一条总线可以挂多份 DBC，按 yaml 里的声明顺序查找，先命中者胜。
"""

import logging
import os
from typing import Any, Dict, List, Tuple

import can
import cantools
import numpy as np

from ..configs import dbc_alias_bus
from .base import Loader, SignalData, _parse_msg_id
from .blf_channels import identify

log = logging.getLogger(__name__)


class BLFLoader(Loader):
    """
    从 BLF 文件中按候选名依次提取 CAN 信号（基于 cantools 解码）。

    用法：
        loader = BLFLoader(blf_cfg)          # signals_blf.yaml 的校验结果
        data_a = loader.load("a.blf")
        data_b = loader.load("b.blf")
    """

    def __init__(self, blf_cfg):
        self.cfg = blf_cfg
        self.buses: Dict[str, Dict[str, Any]] = blf_cfg.buses
        self.signal_maps: Dict[str, Dict[str, Any]] = blf_cfg.signal_maps
        # 解析中断告警：BLF 中途坏帧/IO 失败会让剩余帧全部丢失。半截数据出结论
        # 比报错更危险，所以保留部分数据但必须把中断事实暴露给调用方
        self.load_errors: list = []

        # DBC 在构造期解析：坏库/路径错在启动时就暴露，而不是等第一次分析。
        # 按别名建索引（候选写的是别名）；同一路径被两个别名引用时只解析一次
        self._db_by_alias: Dict[str, Any] = {}
        by_path: Dict[str, Any] = {}
        for info in self.buses.values():
            for alias, path in info.get("dbcs", {}).items():
                if path not in by_path:
                    by_path[path] = cantools.database.load_file(path)
                self._db_by_alias[alias] = by_path[path]
        self._alias_bus = dbc_alias_bus(self.buses)

        # 识别结果按「文件 stat + 路径」缓存：引擎会复用同一个 loader 实例，
        # 同一文件常常要被连算两次（分析 + 出图）
        self._channels_cache: Dict[tuple, Tuple[Dict[str, int], List[str]]] = {}

    # ------------------------------------------------------------ 通道识别
    def _stat_key(self, path: str) -> tuple:
        st = os.stat(path)
        return (path, st.st_size, st.st_mtime_ns)

    def _bus_channels(self, path: str, errors: List[str]) -> Dict[str, int]:
        """返回 {总线: 通道号}；没认出来的总线不在结果里（其信号按缺失处理）。

        `channels` 可以只写一部分：写死的总线和通道不参与自动识别，其余总线照
        常扫描推断。全部写死时连文件都不扫。
        """
        pinned = {bus: ch for ch, bus in (self.cfg.channels or {}).items()}
        if pinned and len(pinned) == len(self.buses):
            return dict(pinned)
        key = self._stat_key(path)
        cached = self._channels_cache.get(key)
        if cached is None:
            report = identify(path, self.buses, self.signal_maps,
                              self.cfg.channel_detect, pinned=pinned)
            cached = (report.bus_channels(), report.warnings)
            self._channels_cache[key] = cached
        channels, warnings = cached
        errors.extend(warnings)
        return channels

    # ------------------------------------------------------------ 解析
    def _resolve(self, bus_ch: Dict[str, int]) -> Dict[str, tuple]:
        """候选 → (bus, channel, msg_def, signal_name, signal_def)。

        候选按书写顺序尝试，首个「别名所属总线已识别出通道，且该 DBC 里同时有
        该帧与该信号」的候选胜出；一个逻辑信号只对应一个 (msg_def, signal)，
        不会因为两份 DBC 都定义了同一帧而重复出点。
        """
        resolved: Dict[str, tuple] = {}
        for logical_name, spec in self.signal_maps.items():
            for cand in spec.get("candidates", []):
                alias, msg_id, sig_name = cand[0], cand[1], cand[2]
                bus = self._alias_bus.get(alias)
                ch = bus_ch.get(bus) if bus is not None else None
                db = self._db_by_alias.get(alias)
                if ch is None or db is None:
                    continue
                frame_id, is_ext = _parse_msg_id(msg_id)
                try:
                    msg_def = db.get_message_by_frame_id(
                        frame_id, force_extended_id=is_ext)
                except KeyError:
                    continue
                sig_def = next((s for s in msg_def.signals if s.name == sig_name), None)
                if sig_def is None:
                    continue
                resolved[logical_name] = (bus, ch, msg_def, sig_name, sig_def)
                break
        return resolved

    def load(self, file_path: str) -> Dict[str, SignalData]:
        result: Dict[str, SignalData] = {}
        self.load_errors = []

        # 1. 通道识别 → 候选解析（总线未识别或 DBC 里找不到的候选逐条回退）
        bus_ch = self._bus_channels(file_path, self.load_errors)
        for bus in bus_ch:
            missing = self.buses.get(bus, {}).get("unavailable") or []
            if missing:      # 识别成功但本机没有库：说清楚是「解不了」而不是「没数据」
                self.load_errors.append(
                    f"总线 {bus} 的 DBC 未提供（{', '.join(missing)}），"
                    "该总线的信号无法解码")
        resolved = self._resolve(bus_ch)

        # 2. 按报文分组，避免同一帧重复解码
        decode_groups: Dict[tuple, list] = {}
        for logical_name, (_bus, ch, msg_def, sig_name, sig_def) in resolved.items():
            key = (id(msg_def), ch, bool(msg_def.is_extended_frame))
            decode_groups.setdefault(key, []).append((logical_name, sig_name, sig_def))

        # 3. 帧索引：(frame_id, is_extended, channel) → 待解码分组列表。
        #    键里带通道号是「同一 ID 在不同总线代表不同信号」不被解错的关键
        frame_lookup: Dict[tuple, list] = {}
        msg_def_by_id: Dict[int, Any] = {}
        for _logical_name, (_bus, ch, msg_def, _sig_name, _sig_def) in resolved.items():
            msg_def_by_id[id(msg_def)] = msg_def
            lookup_key = (msg_def.frame_id, bool(msg_def.is_extended_frame), ch)
            group_key = (id(msg_def), ch, bool(msg_def.is_extended_frame))
            frame_lookup.setdefault(lookup_key, [])
            if group_key not in frame_lookup[lookup_key]:
                frame_lookup[lookup_key].append(group_key)

        collected: Dict[str, list] = {ln: [[], [], None] for ln in resolved}

        # 4. 单次遍历文件
        n_frames = 0
        try:
            reader = can.BLFReader(file_path)
            for msg in reader:
                n_frames += 1
                keys = frame_lookup.get(
                    (msg.arbitration_id, msg.is_extended_id, msg.channel)
                )
                if not keys:
                    continue

                for group_key in keys:
                    msg_def = msg_def_by_id.get(group_key[0])
                    if msg_def is None:
                        continue

                    try:
                        decoded = msg_def.decode(
                            msg.data,
                            decode_choices=False,
                            allow_truncated=True,
                        )
                    except Exception:
                        continue

                    for logical_name, sig_name, sig_def in decode_groups.get(group_key, []):
                        if sig_name not in decoded:
                            continue
                        value = decoded[sig_name]
                        collected[logical_name][0].append(msg.timestamp)
                        collected[logical_name][1].append(value)
                        if sig_def.choices is not None:
                            collected[logical_name][2] = {
                                k: str(v) for k, v in sig_def.choices.items()
                            }
        except Exception as e:
            # 读到一半挂掉（坏帧潮/IO 错误）：生成器已死无法续读，保留已解码部分，
            # 但中断事实必须随 loader 带出去——静默截断会让分析基于不完整数据出结论
            self.load_errors.append(
                f"BLF 解析在第 {n_frames} 帧后中断（{type(e).__name__}: {str(e)[:200]}），"
                "其后的帧全部丢失，结果基于不完整数据")
            log.warning("BLF 解析中断 %s: 第 %d 帧后 (%s)", file_path, n_frames, e)

        # 5. 统一相对起点偏移
        all_start = [
            collected[ln][0][0]
            for ln in collected
            if collected[ln][0]
        ]
        start_timestamp = float(min(all_start)) if all_start else 0.0

        for logical_name, spec in self.signal_maps.items():
            description = spec.get("description", "")
            unit = spec.get("unit", "")
            if logical_name in collected and collected[logical_name][0]:
                ts_list, vals, choices = collected[logical_name]
                ts = np.asarray(ts_list, dtype=np.float64) - start_timestamp
                result[logical_name] = SignalData(
                    ts=ts,
                    values=np.asarray(vals),
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
