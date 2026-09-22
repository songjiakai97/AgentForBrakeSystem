"""BLF Loader：基于 python-can + cantools 按 DBC 定义解码 CAN 信号。"""

from typing import Any, Dict

import can
import cantools
import numpy as np

from .base import Loader, SignalData, _parse_msg_id


class BLFLoader(Loader):
    """
    从 BLF 文件中按候选名依次提取 CAN 信号（基于 cantools 解码）。

    用法：
        loader = BLFLoader(dbc_files, signal_maps)
        data_a = loader.load("a.blf")
        data_b = loader.load("b.blf")
    """

    def __init__(
        self,
        dbc_files: Dict[str, Dict[str, Any]],
        signal_maps: Dict[str, Dict[str, Any]],
    ):
        self.dbc_files = dbc_files
        self.signal_maps = signal_maps

        self._db_by_alias_ch: Dict[tuple, Any] = {}
        for alias, info in dbc_files.items():
            db = cantools.database.load_file(info["path"])
            self._db_by_alias_ch[(alias, info["channel"])] = db

    def load(self, file_path: str) -> Dict[str, SignalData]:
        result: Dict[str, SignalData] = {}

        # 1. 候选解析：为每个逻辑信号确定 (alias, channel, msg_def, sig_name, sig_def)
        resolved: Dict[str, tuple] = {}
        for logical_name, spec in self.signal_maps.items():
            for cand in spec.get("candidates", []):
                alias, msg_id, sig_name = cand[0], cand[1], cand[2]
                alias_ch = self.dbc_files.get(alias, {}).get("channel")
                if alias_ch is None:
                    continue
                db = self._db_by_alias_ch.get((alias, alias_ch))
                if db is None:
                    continue

                frame_id, is_ext = _parse_msg_id(msg_id)
                try:
                    msg_def = db.get_message_by_frame_id(
                        frame_id, force_extended_id=is_ext
                    )
                except KeyError:
                    continue

                sig_def = next((s for s in msg_def.signals if s.name == sig_name), None)
                if sig_def is None:
                    continue

                resolved[logical_name] = (alias, alias_ch, msg_def, sig_name, sig_def)
                break

        # 2. 按报文分组，避免同一帧重复解码
        decode_groups: Dict[tuple, list] = {}
        for logical_name, (alias, ch, msg_def, sig_name, sig_def) in resolved.items():
            key = (id(msg_def), ch, bool(msg_def.is_extended_frame))
            decode_groups.setdefault(key, []).append((logical_name, sig_name, sig_def))

        # 3. 帧索引：(frame_id, is_extended, channel) → 待解码分组列表
        frame_lookup: Dict[tuple, list] = {}
        msg_def_by_id: Dict[int, Any] = {}
        for logical_name, (alias, ch, msg_def, sig_name, sig_def) in resolved.items():
            msg_def_by_id[id(msg_def)] = msg_def
            lookup_key = (msg_def.frame_id, bool(msg_def.is_extended_frame), ch)
            group_key = (id(msg_def), ch, bool(msg_def.is_extended_frame))
            frame_lookup.setdefault(lookup_key, [])
            if group_key not in frame_lookup[lookup_key]:
                frame_lookup[lookup_key].append(group_key)

        collected: Dict[str, list] = {ln: [[], [], None] for ln in resolved}

        # 4. 单次遍历文件
        try:
            reader = can.BLFReader(file_path)
            for msg in reader:
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
        except Exception:
            pass

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
