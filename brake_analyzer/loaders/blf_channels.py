"""BLF 通道 ↔ 总线自动识别（design.md §9.6）。

只回答一件事：文件里的第 N 个 CAN 通道是哪条总线。

- **不读 DBC**：指纹就是 `signals_blf.yaml` 里写下的 msg_id。DBC 只在识别
  完成之后用来解码，所以没有 DBC 的总线（例如只能部署在客户现场的 VBOX）
  照样能被识别。
- **投票单位是逻辑信号**：一个信号在总线 b 上的候选 ID 只要有一个出现在通道
  上，就给 (通道, 总线) 记一票。同一 ID 在不同网代表不同信号时，三元组里的
  bus 决定了这一票归谁，不会串线。
- **归一化 + 排他性**：按「本文件里这条总线可能看得见的信号数」归一化，避免
  候选多的总线通吃；网关转发导致同一信号在两条通道都命中时，纯度过低会拉低
  分数。
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import can

from ..configs import ChannelDetect, dbc_alias_bus
from .base import _parse_msg_id

log = logging.getLogger(__name__)

IdKey = Tuple[int, bool]        # (frame_id, is_extended)


@dataclass
class ChannelStats:
    channel: int
    frames: int = 0
    ids: Dict[IdKey, int] = field(default_factory=dict)

    def present(self, min_frames: int) -> Set[IdKey]:
        return {k for k, n in self.ids.items() if n >= min_frames}


@dataclass
class BusEvidence:
    """一条总线的识别结论与依据（报告/日志用，不参与解码）。"""
    bus: str
    channel: Optional[int] = None
    hits: int = 0
    observed: int = 0
    recall: float = 0.0
    purity: float = 0.0
    score: float = 0.0
    margin: float = 0.0
    status: str = "not_observed"        # explicit | matched | ambiguous | low_confidence | not_observed

    def as_dict(self) -> Dict[str, Any]:
        return {"bus": self.bus, "channel": self.channel, "status": self.status,
                "hits": self.hits, "observed": self.observed,
                "score": round(self.score, 3), "recall": round(self.recall, 3),
                "purity": round(self.purity, 3), "margin": round(self.margin, 3)}


@dataclass
class ChannelReport:
    assignment: Dict[int, str]              # 通道 → 总线
    evidence: List[BusEvidence]
    # warnings：会影响结论可用性（通道没报文、证据不足/歧义、扫描中断）
    # notes：只提示不决策（同一 ID 出现在多个通道、回退全量重扫）——解码键带通道号，
    # 这类事实不改变结果，界面不该按「分析失败」呈现
    warnings: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    scanned_frames: int = 0
    scanned_seconds: float = 0.0

    def bus_channels(self) -> Dict[str, int]:
        return {bus: ch for ch, bus in self.assignment.items()}

    def as_dict(self) -> Dict[str, Any]:
        return {"assignment": dict(self.assignment),
                "evidence": [e.as_dict() for e in self.evidence],
                "warnings": list(self.warnings),
                "notes": list(self.notes),
                "scanned_frames": self.scanned_frames,
                "scanned_seconds": round(self.scanned_seconds, 3)}


# ---------------------------------------------------------------- 采样

def scan(path: str, scan_seconds: float = 15.0,
         scan_frames: int = 500_000) -> Tuple[Dict[int, ChannelStats], int, float, bool, Optional[str]]:
    """只计数不解码地扫一遍 BLF，返回 (通道统计, 已读帧数, 时间跨度, 是否提前停, 错误)。

    CAN 报文是周期性的，识别只需要「哪些 ID 在哪个通道」：默认取前 15 s（或
    50 万帧，先到者为准）就够。解一次码都没做，所以这一遍比正常加载便宜得多。
    """
    stats: Dict[int, ChannelStats] = {}
    n = 0
    t0: Optional[float] = None
    tmax: Optional[float] = None
    truncated = False
    err: Optional[str] = None
    try:
        for msg in can.BLFReader(path):
            n += 1
            try:
                ch = 0 if msg.channel is None else int(msg.channel)
            except (TypeError, ValueError):
                ch = 0
            st = stats.get(ch)
            if st is None:
                st = stats[ch] = ChannelStats(channel=ch)
            st.frames += 1
            key = (int(msg.arbitration_id), bool(msg.is_extended_id))
            st.ids[key] = st.ids.get(key, 0) + 1
            ts = msg.timestamp
            if t0 is None:
                t0 = ts
            tmax = ts if tmax is None else max(tmax, ts)
            if n >= scan_frames or (tmax - t0) >= scan_seconds:
                truncated = True
                break
    except Exception as e:      # 读到一半坏帧/IO 失败：已读部分仍可用于识别
        err = (f"通道识别扫描在 {n} 帧后中断（{type(e).__name__}: {str(e)[:120]}）")
        log.warning("BLF 识别扫描中断 %s: %s", path, e)
    span = (tmax - t0) if (t0 is not None and tmax is not None) else 0.0
    return stats, n, span, truncated, err


# ---------------------------------------------------------------- 打分

def candidate_index(alias_bus: Dict[str, str],
                    signal_maps: Dict[str, Dict[str, Any]]
                    ) -> Dict[str, Dict[str, Set[IdKey]]]:
    """bus → 逻辑信号 → 该总线上的候选 ID 集合。

    候选写的是 DBC 别名，这里先换成所属总线：同一个逻辑信号在两条总线上各有一
    个别名时，两票各自归位，不会互相抵赖。
    """
    index: Dict[str, Dict[str, Set[IdKey]]] = {}
    for logical, spec in signal_maps.items():
        for cand in spec.get("candidates", []):
            alias, msg_id = cand[0], cand[1]
            bus = alias_bus.get(alias)
            if bus is None:         # 配置校验已保证不会发生，防御性跳过
                continue
            index.setdefault(bus, {}).setdefault(logical, set()).add(_parse_msg_id(msg_id))
    return index


def _cells(present: Dict[int, Set[IdKey]], bus_names: Iterable[str],
           index: Dict[str, Dict[str, Set[IdKey]]]):
    """(通道, 总线) → (score, hits, purity)，以及 observable / owners 供归一化。"""
    all_ids: Set[IdKey] = set().union(*present.values()) if present else set()
    observable = {b: {s for s, ids in index.get(b, {}).items() if ids & all_ids}
                  for b in bus_names}
    # 一个信号能被几条总线认领：>1 说明这个信号无法区分总线，不该算作强证据
    owners: Dict[str, Set[str]] = {}
    for bus, sigs in index.items():
        for sig, ids in sigs.items():
            if ids & all_ids:
                owners.setdefault(sig, set()).add(bus)

    cells: Dict[Tuple[int, str], Tuple[float, int, float]] = {}
    for ch, ids in present.items():
        for bus in bus_names:
            hits = {s for s, cand in index.get(bus, {}).items() if cand & ids}
            if not hits:
                cells[(ch, bus)] = (0.0, 0, 0.0)
                continue
            recall = len(hits) / len(observable[bus]) if observable[bus] else 0.0
            pure = sum(1 for s in hits if len(owners.get(s, ())) == 1) / len(hits)
            cells[(ch, bus)] = (recall * (0.5 + 0.5 * pure), len(hits), pure)
    return cells, observable


def score_channels(stats: Dict[int, ChannelStats], buses: Dict[str, Dict[str, Any]],
                   signal_maps: Dict[str, Dict[str, Any]],
                   detect: Optional[ChannelDetect] = None,
                   scanned: Tuple[int, float] = (0, 0.0),
                   pinned: Optional[Dict[str, int]] = None) -> ChannelReport:
    """由通道统计得出 assignment 与逐总线证据（不读文件，便于单测）。

    `pinned` 是 `{总线: 通道}` 的显式绑定（signals_blf.yaml 的 channels）。只绑
    一部分总线时，其余总线照常自动识别；已绑定的总线与其通道都不进入自动指派，
    所以新加一个网段可以「老网段写死、新网段自动认」。
    """
    detect = detect or ChannelDetect()
    pinned = dict(pinned or {})
    pinned_ch = set(pinned.values())
    bus_names = list(buses)
    present = {ch: st.present(detect.min_frames) for ch, st in stats.items()}
    index = candidate_index(dbc_alias_bus(buses), signal_maps)
    cells, observable = _cells(present, bus_names, index)

    # 全局指派：边按分数降序贪心，通道与总线各用一次（总线数很少，够用且确定）。
    # 已显式绑定的总线/通道不参与，避免自动结果把用户的绑定顶掉
    assignment: Dict[int, str] = {}
    used: Set[str] = set(pinned)
    for (ch, bus), (score, hits, _pure) in sorted(
            cells.items(), key=lambda kv: (-kv[1][0], -kv[1][1], kv[0][0], kv[0][1])):
        if score <= 0 or ch in assignment or ch in pinned_ch or bus in used:
            continue
        assignment[ch] = bus
        used.add(bus)

    def _s(ch: int, bus: str) -> float:
        return cells.get((ch, bus), (0.0, 0, 0.0))[0]

    # 贪心之后做一轮对调，保证不存在「换一下总分更高」的指派——否则下面的
    # 互换损失会出现负值，置信度也就没有意义了
    improved = True
    while improved:
        improved = False
        items = list(assignment.items())
        for i, (c1, b1) in enumerate(items):
            for c2, b2 in items[i + 1:]:
                if _s(c1, b2) + _s(c2, b1) > _s(c1, b1) + _s(c2, b2) + 1e-9:
                    assignment[c1], assignment[c2] = b2, b1
                    improved = True
                    break
            if improved:
                break

    bus_ch = {bus: ch for ch, bus in assignment.items()}

    # 置信度用「互换损失」衡量：把这条总线换到别的通道，或与另一条已分配总线
    # 对调，总分至少要掉多少。只在单条总线上比分数会把「本总线 ID 两条通道都有、
    # 但靠另一条总线的独有 ID 反推出来」误判成分不清。
    free = [c for c in present if c not in assignment and c not in pinned_ch]
    margins: Dict[str, float] = {}
    items = list(assignment.items())
    for i, (c1, b1) in enumerate(items):
        for c2, b2 in items[i + 1:]:
            loss = _s(c1, b1) + _s(c2, b2) - _s(c1, b2) - _s(c2, b1)
            margins[b1] = min(margins.get(b1, 1.0), loss)
            margins[b2] = min(margins.get(b2, 1.0), loss)
        # 空通道同样能安放这条总线时，归属并不唯一
        for c2 in free:
            loss = _s(c1, b1) - _s(c2, b1)
            margins[b1] = min(margins.get(b1, 1.0), loss)

    evidence: List[BusEvidence] = []
    for bus in bus_names:
        if bus in pinned:
            ch = pinned[bus]
            score, hits, pure = cells.get((ch, bus), (0.0, 0, 0.0))
            obs = len(observable.get(bus, ()))
            evidence.append(BusEvidence(
                bus=bus, channel=ch, hits=hits, observed=obs,
                recall=(hits / obs) if obs else 0.0, purity=pure,
                score=score, margin=1.0, status="explicit"))
            continue
        ch = bus_ch.get(bus)
        score, hits, pure = cells.get((ch, bus), (0.0, 0, 0.0)) if ch is not None \
            else (0.0, 0, 0.0)
        obs = len(observable.get(bus, ()))
        margin = margins.get(bus, 1.0) if ch is not None else 0.0
        if obs == 0:
            status = "not_observed"
        elif ch is None:
            status = "ambiguous"            # 有证据，但通道被更强的总线占了
        elif score < detect.min_score or hits < min(detect.min_hits, obs):
            status = "low_confidence"
        elif margin < detect.min_margin:
            status = "ambiguous"
        else:
            status = "matched"
        evidence.append(BusEvidence(
            bus=bus, channel=ch, hits=hits, observed=obs,
            recall=(hits / obs) if obs else 0.0, purity=pure,
            score=score, margin=margin, status=status))

    # 显式绑定并入结果；自动指派已排除这些通道，不会互相覆盖
    for bus, ch in pinned.items():
        assignment[ch] = bus

    report = ChannelReport(assignment=assignment, evidence=evidence,
                           scanned_frames=scanned[0], scanned_seconds=scanned[1])
    report.warnings.extend(_pinned_warnings(pinned, stats))
    report.warnings.extend(_evidence_warnings(evidence, detect))
    report.notes.extend(_conflict_warnings(present))
    return report


def _conflict_warnings(present: Dict[int, Set[IdKey]]) -> List[str]:
    """同一 ID 出现在多个通道：网关转发还是 ID 复用，交给人看。"""
    by_id: Dict[IdKey, List[int]] = {}
    for ch, ids in present.items():
        for key in ids:
            by_id.setdefault(key, []).append(ch)
    shared = {k: v for k, v in by_id.items() if len(v) > 1}
    if not shared:
        return []
    items = []
    for (frame_id, is_ext), chs in sorted(shared.items())[:5]:
        mid = f"0x{frame_id:X}" + ("x" if is_ext else "")
        items.append(f"{mid}@{sorted(chs)}")
    more = "" if len(shared) <= 5 else f" 等 {len(shared)} 个"
    return [f"有报文 ID 同时出现在多个通道（网关转发或 ID 复用）：{'、'.join(items)}{more}"]


def _pinned_warnings(pinned: Dict[str, int], stats: Dict[int, ChannelStats]) -> List[str]:
    """显式绑定的通道里没有任何报文：多半是通道号写错或文件里没有这条网段。"""
    out = []
    for bus, ch in sorted(pinned.items()):
        st = stats.get(ch)
        if st is None or st.frames == 0:
            out.append(
                f"总线 {bus} 在 channels 里绑定到通道 {ch}，"
                "但扫描范围内该通道没有任何报文，请确认通道号")
    return out


def _evidence_warnings(evidence: List[BusEvidence], detect: ChannelDetect) -> List[str]:
    out = []
    for e in evidence:
        if e.status == "explicit":       # 用户自己写的，不必再提示
            continue
        if e.status == "not_observed":
            out.append(f"总线 {e.bus} 在文件里没有出现任何候选报文，其信号按缺失处理")
        elif e.status == "ambiguous":
            if e.channel is None:
                out.append(
                    f"总线 {e.bus} 的候选报文出现了，但通道已被证据更强的总线占用，"
                    "请在 signals_blf.yaml 的 channels 里显式指定")
            else:
                out.append(
                    f"总线 {e.bus} 的通道归属分不清"
                    f"（换到其它通道后总分几乎不变，margin={e.margin:.2f} < "
                    f"{detect.min_margin}），请在 signals_blf.yaml 的 channels 里显式指定")
        elif e.status == "low_confidence":
            where = f"通道 {e.channel}" if e.channel is not None else "未分配通道"
            out.append(
                f"总线 {e.bus} 识别为{where}，但证据不足"
                f"（命中 {e.hits}/{e.observed} 个信号，得分 {e.score:.2f}，"
                f"阈值 {detect.min_score}/{detect.min_hits}）")
    return out


# ---------------------------------------------------------------- 入口

def identify(path: str, buses: Dict[str, Dict[str, Any]],
             signal_maps: Dict[str, Dict[str, Any]],
             detect: Optional[ChannelDetect] = None,
             pinned: Optional[Dict[str, int]] = None) -> ChannelReport:
    """识别一个 BLF 文件的通道归属。

    先按 scan_seconds/scan_frames 采样；若有总线证据不足（含显式绑定的通道没
    采到报文）且采样确实提前停了，再全量重扫一次（识别不解码，重扫很便宜），
    避免把「窗口没覆盖到」当成「这条总线不存在」。
    """
    detect = detect or ChannelDetect()
    stats, frames, span, truncated, err = scan(
        path, detect.scan_seconds, detect.scan_frames)
    report = score_channels(stats, buses, signal_maps, detect, (frames, span), pinned)
    if err:
        report.warnings.insert(0, err)

    weak = any(e.status in ("low_confidence", "ambiguous") for e in report.evidence)
    missing_pinned = any(ch not in stats or stats[ch].frames == 0
                         for ch in (pinned or {}).values())
    if truncated and (weak or missing_pinned):
        stats2, frames2, span2, _t, err2 = scan(path, float("inf"), float("inf"))
        full = score_channels(stats2, buses, signal_maps, detect, (frames2, span2), pinned)
        full.notes.append("采样窗口内识别证据不足，已回退全量重扫")
        if err2:
            full.warnings.insert(0, err2)
        report = full

    log.info("BLF 通道识别 %s: %s", path, report.as_dict())
    return report
