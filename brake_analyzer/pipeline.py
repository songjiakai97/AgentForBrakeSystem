"""编排流水线（design.md §6.7）。

单文件 + 单工况：load → segment → 每事件（metrics → rules → LLM 可选）→ AnalysisResult。
无有效事件时补一条退化窗口样本，使指标全 missing、规则命中 data_quality。
"""

import uuid
from typing import Dict, List, Optional

from .configs import AppConfigs
from .events.segment import SEGMENTERS, degenerate_window, is_valid
from .loaders.base import SignalData
from .metrics.engine import compute_metrics
from .rules.engine import run_rules
from .schemas import AnalysisResult, EventWindow, RunSample


class NoDataError(RuntimeError):
    pass


def analyze_signals(
    signals: Dict[str, SignalData],
    condition_id: str,
    cfg: AppConfigs,
    file_id: str = "",
    file_name: str = "",
    profile: Optional[str] = None,
) -> AnalysisResult:
    cond = cfg.condition(condition_id)
    if cond is None:
        raise NoDataError(f"未知道况: {condition_id}")
    fn = cfg.function(cond.function)
    segmenter = SEGMENTERS.get(fn.segmenter)
    if segmenter is None:
        raise NoDataError(f"分段模板未注册: {fn.segmenter}")

    params = dict(cond.params)
    windows: List[EventWindow] = segmenter(signals, fn, cond, params)
    windows = [w for w in windows if is_valid(w)]
    if not windows:
        windows = [degenerate_window("no_valid_event")]

    analysis_id = f"an_{uuid.uuid4().hex[:10]}"
    samples: List[RunSample] = []
    for i, window in enumerate(windows, start=1):
        metrics = compute_metrics(
            signals, fn, cond, window, cfg.metrics, profile=profile
        )
        verdicts = run_rules(metrics, cfg.rules, fn.key, cond.id)
        samples.append(
            RunSample(
                sample_id=f"{analysis_id}_s{i}",
                file_id=file_id,
                file_name=file_name,
                run_index=i,
                window=window,
                metrics=metrics,
                verdicts=verdicts,
                analysis_id=analysis_id,
            )
        )

    return AnalysisResult(
        analysis_id=analysis_id,
        condition_id=cond.id,
        condition_name=cond.name,
        function_key=fn.key,
        profile=profile,
        file_id=file_id,
        file_name=file_name,
        samples=samples,
    )


def analyze_file(
    path: str,
    condition_id: str,
    cfg: AppConfigs,
    file_id: str = "",
    file_name: Optional[str] = None,
    profile: Optional[str] = None,
) -> AnalysisResult:
    from .loaders.mf4 import MF4Loader

    loader = MF4Loader(cfg.signal_maps)
    signals = loader.load(path)
    if not any(len(s.ts) > 0 for s in signals.values()):
        raise NoDataError(f"文件未解析到任何已配置信号: {file_name or path}")
    if file_id == "":
        file_id = f"fx_{uuid.uuid4().hex[:8]}"
    return analyze_signals(
        signals,
        condition_id,
        cfg,
        file_id=file_id,
        file_name=file_name or path.split("/")[-1],
        profile=profile,
    )
