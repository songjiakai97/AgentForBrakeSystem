"""记录库的惰性分析与缓存（design.md §17.4）。

导入只登记路径，指标等第一次被查询（网页点开、对话里对比、报表生成）时才算：
- 全量预跑会让导入很慢，而大量历史文件可能再也不会被翻到；
- 缓存键是「文件内容 sha1 + 配置指纹」，两者任一变化就从库里清掉旧结论；
- 文件在服务器上不存在时记 file_gone，不再反复尝试解析。
"""

import os
from typing import Any, Dict, List, Optional, Tuple

from ..configs import AppConfigs, pick
from ..pipeline import NoDataError, analyze_signals, make_loader
from ..schemas import AnalysisResult, compose_metric_key
from . import store


class RecordsError(RuntimeError):
    pass


class RecordNotFound(RecordsError):
    pass


def _loader_key(path: str) -> str:
    return os.path.splitext(path)[1].lower() or "default"


class RecordService:
    """按需分析 + 写回缓存。Web/工具各建一个实例即可，连接按需短开。"""

    def __init__(self, cfg: AppConfigs, db_path: str, blf_cfg=None):
        self.cfg = cfg
        self.db_path = db_path
        self.blf_cfg = blf_cfg
        self._loaders: Dict[str, Any] = {}
        # 配置指纹在进程内是常量：配置改动要重启服务，这是部署常态
        self.fingerprint = store.config_fingerprint(cfg.config_dir)

    # ---- 连接 ----
    def connect(self):
        return store.connect(self.db_path)

    def _loader(self, path: str):
        key = _loader_key(path)
        loader = self._loaders.get(key)
        if loader is None:
            loader = make_loader(path, self.cfg, blf_cfg=self.blf_cfg)
            self._loaders[key] = loader
        return loader

    # ---- 分析 ----
    def analyze(self, record_id: str, force: bool = False,
                conn=None) -> Tuple[str, Optional[AnalysisResult]]:
        """返回 (状态, 结果)。状态：cached | analyzed | file_gone | error。

        cached 时结果为 None——调用方若只要数值可直接读 metric_value；
        需要窗口/图表时传 force=True 重算。
        """
        own = conn is None
        conn = conn or self.connect()
        try:
            row = store.get_record(conn, record_id)
            if row is None:
                raise RecordNotFound(f"记录不存在: {record_id}")
            if not force and store.get_analysis(conn, record_id, self.fingerprint):
                return "cached", None
            path = row["file_path"]
            if not os.path.exists(path):
                store.mark_analysis_error(conn, record_id, "file_gone",
                                          f"文件不在服务器上: {path}")
                return "file_gone", None
            cond = self.cfg.condition(row["condition_id"])
            if cond is None:
                store.mark_analysis_error(conn, record_id, "error",
                                          f"工况已不在配置里: {row['condition_id']}")
                return "error", None
            try:
                signals = self._loader(path).load(path)
                if not any(len(s.ts) > 0 for s in signals.values()):
                    raise NoDataError("未解析到任何已配置信号")
                result = analyze_signals(
                    signals, cond.id, self.cfg,
                    file_id=row["file_id"], file_name=row["file_name"],
                )
            except NoDataError as e:
                store.mark_analysis_error(conn, record_id, "error", str(e))
                return "error", None
            except Exception as e:      # 解析器/工具内部异常不应带崩整轮查询
                store.mark_analysis_error(conn, record_id, "error",
                                          f"{type(e).__name__}: {e}")
                return "error", None
            store.save_analysis(conn, record_id, result, self.fingerprint)
            return "analyzed", result
        finally:
            if own:
                conn.close()

    def analyze_pending(self, limit: Optional[int] = None) -> Dict[str, Any]:
        """批量补齐（夜间任务/运维用）；不做全量预跑的默认路径。"""
        conn = self.connect()
        try:
            rows = store.iter_pending(conn, self.fingerprint)
            if limit is not None:
                rows = rows[:limit]
            done, failed, gone = [], [], []
            for row in rows:
                status, _ = self.analyze(row["record_id"], conn=conn)
                if status == "analyzed":
                    done.append(row["record_id"])
                elif status == "file_gone":
                    gone.append(row["record_id"])
                elif status == "error":
                    failed.append(row["record_id"])
            return {"analyzed": len(done), "failed": failed, "file_gone": gone}
        finally:
            conn.close()

    # ---- 读 ----
    def read(self, record_id: str, force: bool = False) -> Tuple[str, dict]:
        """读一条记录并给出 `analysis_result` 形状的卡片数据（§17.5）。

        与 `analyze` 的区别：`analyze` 只保证缓存有效（cached 时返回 None），这里
        还要把卡片凑齐——对话里要展示窗口、指标与规则命中。已有缓存时直接用库里
        的行拼装，不重跑流水线；未分析才解析一次。
        """
        conn = self.connect()
        try:
            row = store.get_record(conn, record_id)
            if row is None:
                raise RecordNotFound(f"记录不存在: {record_id}")
            status, result = self.analyze(record_id, force=force, conn=conn)
            card = result.to_dict() if result is not None else self._card(conn, row)
            row = store.get_record(conn, record_id) or row
        finally:
            conn.close()
        card.update({
            "record_id": record_id, "source": "record",
            "vehicle": row["vehicle"], "file_id": row["file_id"],
            "file_name": row["file_name"], "analysis_status": status,
        })
        if status in ("error", "file_gone") and row["last_error"]:
            card["note"] = row["last_error"]
        return status, card

    def _card(self, conn, row) -> dict:
        """用缓存行拼出 `AnalysisResult.to_dict()` 的形状。

        指标值/窗口/规则命中都在库里；`category` 与 `ok_range` 由配置补（库里只存
        值，限值随配置变），`direction` 库里没存就留空——分析卡片对它的使用只是
        展开时的一句方向说明，缺了不影响结论。
        """
        rid = row["record_id"]
        cond = self.cfg.condition(row["condition_id"])
        cached = store.get_analysis(conn, rid)
        metrics = store.metric_rows(conn, rid)
        verdicts = store.verdict_rows(conn, rid)
        windows = {w["sample_index"]: w for w in store.sample_windows(conn, rid)}
        names = {m["metric_key"]: m["name"] for m in metrics}

        def _metric(m) -> dict:
            key = m["metric_key"]
            tmpl = self.cfg.metrics.get(key)
            entry = pick(cond.metric_index, key, None) if cond else None
            item = {
                "key": compose_metric_key(cond.id, key) if cond else key,
                "name": m["name"] or (tmpl.name if tmpl else key),
                "category": tmpl.category if tmpl else "",
                "value": m["value"], "unit": m["unit"], "status": m["status"],
                "ts_range": ([m["ts_start"], m["ts_end"]]
                             if m["ts_start"] is not None else None),
                "ok_range": (entry or {}).get("ok_range"),
            }
            if m["missing_reason"]:
                item["missing_reason"] = m["missing_reason"]
            return item

        def _verdict(v) -> dict:
            return {
                "rule_id": v["rule_id"], "metric_key": v["metric_key"],
                "metric_name": names.get(v["metric_key"], v["metric_key"]),
                "status": v["status"], "severity": v["severity"],
                "fault_domain": v["fault_domain"], "message": v["message"],
                "direction": "",
            }

        count = cached["sample_count"] if cached else 0
        for m in metrics:
            count = max(count, m["sample_index"] + 1)
        for v in verdicts:
            count = max(count, v["sample_index"] + 1)

        samples: List[dict] = []
        for i in range(count):
            run_metrics = [_metric(m) for m in metrics if m["sample_index"] == i]
            run_verdicts = [_verdict(v) for v in verdicts if v["sample_index"] == i]
            win = windows.get(i)
            summary = {"ok": 0, "abnormal": 0, "missing": 0, "info": 0}
            for m in run_metrics:
                summary[m["status"]] = summary.get(m["status"], 0) + 1
            samples.append({
                # 卡片内唯一即可：曲线走 /api/records/{record_id}/timeseries，
                # 不用这个 id 去 /api/analyses 找
                "sample_id": f"{rid}#{i}",
                "run_index": i + 1,
                "file_name": row["file_name"],
                "window": ({"t_start": win["t_start"], "t_end": win["t_end"],
                            "anchor": win["t_start"]} if win else None),
                "analysis_id": cached["analysis_id"] if cached else None,
                "status_summary": summary,
                "metrics": run_metrics,
                "verdicts": run_verdicts,
            })

        return {
            "analysis_id": cached["analysis_id"] if cached else None,
            "condition_id": row["condition_id"],
            "condition_name": cond.name if cond else row["condition_id"],
            "function_key": cond.function if cond else "",
            "profile": None,          # 记录库按导入时的工况分析，不带档位
            "file_id": row["file_id"],
            "file_name": row["file_name"],
            "sample_count": count,
            "degraded": bool(cached["degraded"]) if cached else False,
            "samples": samples,
        }

    def load_signals(self, record_id: str) -> tuple:
        """取原始信号（图表用）；返回 (signals, record_row)。"""
        conn = self.connect()
        try:
            row = store.get_record(conn, record_id)
        finally:
            conn.close()
        if row is None:
            raise RecordNotFound(f"记录不存在: {record_id}")
        path = row["file_path"]
        if not os.path.exists(path):
            raise RecordNotFound(f"文件已不在服务器上: {path}")
        return self._loader(path).load(path), row

    def list_records(self, **filters) -> List[dict]:
        conn = self.connect()
        try:
            return [dict(r) for r in store.query_records(conn, **filters)]
        finally:
            conn.close()

    def record_detail(self, record_id: str, ensure: bool = True) -> dict:
        """一条记录的完整视图：元数据 + 指标 + 规则命中（必要时先算）。"""
        conn = self.connect()
        try:
            status, result = ("cached", None)
            if ensure:
                status, result = self.analyze(record_id, conn=conn)
            row = store.get_record(conn, record_id)
            if row is None:
                raise RecordNotFound(f"记录不存在: {record_id}")
            cached = store.get_analysis(conn, record_id)
            return {
                "record": dict(row),
                "analysis_status": status,
                "sample_count": len(result.samples) if result is not None
                else (cached["sample_count"] if cached else 0),
                "metrics": [dict(m) for m in store.metric_rows(conn, record_id)],
                "verdicts": [dict(v) for v in store.verdict_rows(conn, record_id)],
                "windows": store.sample_windows(conn, record_id),
                "analysis_id": result.analysis_id if result is not None
                else (cached["analysis_id"] if cached else None),
            }
        finally:
            conn.close()
