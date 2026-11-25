"""驱动形式识别与按轴扭矩出图（design.md §9.5）。

输入是已加载的逻辑信号（逻辑名 → SignalData，见 loader），输出两类东西：

1. 档位识别：VehDriveType → 4WD/2WD；有没有 DMC 目标扭矩 → DTCS/TCS。
   显式选择的档位优先（用户说了算），识别只补空缺维度。
2. 图表分组：前驱一根轴、后驱一根轴、四驱两根轴，其余驱动形式不出扭矩轴。
   每根轴一行，组内两条线 = 实际扭矩 + TCS/DTCS(DMC) 目标扭矩。

为什么扭矩轴不写进 signals.yaml 的 chart_layout：看哪根轴取决于数据里的
驱动类型，是每次分析才知道的，静态布局只能二选一地多画或少画。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from .configs import ChartGroup, DriveConfig


@dataclass
class DriveInfo:
    code: Optional[int]
    name: str
    layout: str                   # front | rear | awd | both | other
    known: bool                   # 驱动类型信号存在且解码出了可用的形式
    dtcs: bool
    target_kind: str              # "DTCS" | "TCS"
    target_signals: Dict[str, str] = field(default_factory=dict)   # axle → 目标扭矩


def _data(signals: dict, name: str):
    """取有数据的信号；缺失/空序列一律当没有。"""
    sig = signals.get(name) if signals else None
    if sig is None or not len(getattr(sig, "ts", [])):
        return None
    return sig


def _values(sig) -> np.ndarray:
    vals = np.asarray(sig.values, dtype=float)
    return vals[np.isfinite(vals)]


def has_data(signals: dict, name: str) -> bool:
    return _data(signals, name) is not None


def read_drive_code(cfg, signals: dict) -> Optional[int]:
    """VehDriveType 的众数（底盘网偶发坏帧/切换帧，不该由首帧决定）。"""
    if cfg.drive is None:
        return None
    sig = _data(signals, cfg.drive.signal)
    if sig is None:
        return None
    vals = _values(sig)
    if vals.size == 0:
        return None
    codes = np.rint(vals).astype(int)
    codes = codes[codes >= 0]              # 负值不是合法编码，直接丢弃
    if codes.size == 0:
        return None
    return int(np.bincount(codes).argmax())


def _split(profile: Optional[str]) -> List[str]:
    if not profile:
        return []
    return [t.strip() for t in profile.replace(",", "|").replace("_", "|").split("|")
            if t.strip()]


def _base_layout(d: DriveConfig, code: Optional[int]) -> str:
    item = d.types.get(code) if code is not None else None
    return item["layout"] if item else "other"


def infer_assist_dtcs(cfg, signals: dict) -> bool:
    """有 DMC 目标扭矩信号且有数据 → DTCS，否则 TCS。"""
    d = cfg.drive
    if d is None:
        return False
    return any(has_data(signals, sig) for sig in d.torque.get("dtcs", {}).values())


def infer_tokens(cfg, signals: dict) -> Dict[str, str]:
    """从数据识别出的档位维度（不含显式选择）：{"layout": token, "assist": token}。"""
    d = cfg.drive
    if d is None:
        return {}
    out: Dict[str, str] = {}
    code = read_drive_code(cfg, signals)
    layout = _base_layout(d, code)
    token = d.layouts.get(layout)
    if token:
        out["layout"] = token
    # 一条扭矩信号都没有时连 TCS/DTCS 都判不了：宁可不猜，让面板去问
    dtcs = infer_assist_dtcs(cfg, signals)
    tcs_present = any(has_data(signals, sig) for sig in d.torque.get("tcs", {}).values())
    if dtcs or tcs_present or token:
        out["assist"] = d.assist["dtcs"] if dtcs else d.assist["tcs"]
    return out


def drive_info(cfg, signals: dict, profile: Optional[str] = None) -> Optional[DriveInfo]:
    """输出「看哪根轴、目标扭矩取 TCS 还是 DTCS」；显式档位优先于数据识别。"""
    d = cfg.drive
    if d is None:
        return None
    tokens = _split(profile)
    code = read_drive_code(cfg, signals)
    base = _base_layout(d, code)
    known = code is not None and base != "other"
    name = d.types[code]["name"] if code in d.types else (
        f"未识别（0x{code:X}）" if code is not None else "未采集")

    layout = base
    token_awd = d.layouts.get("awd")
    token_2wd = d.layouts.get("front") or d.layouts.get("rear")
    if token_awd and token_awd in tokens:
        layout = "awd"
    elif token_2wd and token_2wd in tokens:
        # 2WD 没说是哪根轴：数据知道就按数据，不知道就两根都画
        layout = layout if layout in ("front", "rear") else "both"

    if d.assist["dtcs"] in tokens:
        dtcs = True
    elif d.assist["tcs"] in tokens:
        dtcs = False
    else:
        dtcs = infer_assist_dtcs(cfg, signals)

    kind = "dtcs" if dtcs else "tcs"
    return DriveInfo(code=code, name=name, layout=layout, known=known, dtcs=dtcs,
                     target_kind="DTCS" if dtcs else "TCS",
                     target_signals=dict(d.torque[kind]))


def drive_chart_groups(cfg, function_key: str, signals: dict,
                       profile: Optional[str] = None) -> List[ChartGroup]:
    """按驱动形式生成轴扭矩子图；不适用（非驱动功能/其他驱动形式）返回 []。"""
    d = cfg.drive
    if d is None or not function_key or function_key != d.function:
        return []
    info = drive_info(cfg, signals, profile)
    if info is None:
        return []
    axles = {"front": ["front"], "rear": ["rear"],
             "awd": ["front", "rear"], "both": ["front", "rear"]}.get(info.layout, [])
    groups: List[ChartGroup] = []
    for axle in axles:
        actual = d.torque["actual"].get(axle)
        # 一根轴固定两条线：实际扭矩 + 当次请求（TCS 用 MMotTarFast，DTCS 用 DMC）。
        # 选中的那类请求在本轴没数据时回退另一类，避免轴行只剩实际扭矩一条线
        kind = "dtcs" if info.dtcs else "tcs"
        target = d.torque[kind].get(axle)
        if not (target and has_data(signals, target)):
            other = "tcs" if kind == "dtcs" else "dtcs"
            alt = d.torque[other].get(axle)
            if alt and has_data(signals, alt):
                target, kind = alt, other
        sigs = [s for s in (actual, target) if s and has_data(signals, s)]
        if not sigs:
            continue
        kind_label = "DTCS" if kind == "dtcs" else "TCS"
        label = f"{'前轴' if axle == 'front' else '后轴'}扭矩（{kind_label}）"
        unit = str(cfg.signal_registry.get(actual or "", {}).get("unit") or "")
        groups.append(ChartGroup(label=label, unit=unit, signals=sigs))
    return groups


def layout_with_drive(cfg, function_key: str, signals: dict,
                      profile: Optional[str] = None, layout=None) -> list:
    """静态 chart_layout + 本次驱动形式对应的轴扭矩分组（保持静态行在前）。"""
    return list(layout or []) + drive_chart_groups(cfg, function_key, signals, profile)
