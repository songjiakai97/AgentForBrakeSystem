"""记录库的对话查询与横向对比（design.md §17.5）。

两个只读工具：
- records_query ：按编号/车型/工况查记录，附带指标汇总
- records_compare：同一工况下按车型/时间横向对比客观指标

两条口径：
1. 数值来自缓存表，不在这里重复跑流水线；未分析的点开时才惰性计算（§17.4），
   查询工具只读，不做隐式重算——一次对话里顺带算十几个文件既慢又不可预期。
2. 每条结果都带 record_id：前端据此画「整段曲线 + 窗口高亮」，模型只需要
   引用数值，不必描述图。
"""

from typing import Any, Dict, List, Optional

from ..configs import AppConfigs
from . import store
from .service import RecordService


def _summary(conn, record_id: str) -> Dict[str, dict]:
    """一个文件内多事件样本的指标汇总：均值 + 极值，单样本时就是原值。"""
    rows = store.metric_rows(conn, record_id)
    out: Dict[str, dict] = {}
    for row in rows:
        key = row["metric_key"]
        item = out.setdefault(key, {
            "name": row["name"], "unit": row["unit"],
            "_values": [], "_statuses": [], "_reasons": [],
        })
        item["_statuses"].append(row["status"])
        if row["value"] is not None:
            item["_values"].append(row["value"])
        elif row["missing_reason"]:
            item["_reasons"].append(row["missing_reason"])

    for key, item in out.items():
        values = item.pop("_values")
        statuses = item.pop("_statuses")
        reasons = item.pop("_reasons")
        if values:
            item["value"] = round(sum(values) / len(values), 3)
            if len(values) > 1:
                item["min"] = round(min(values), 3)
                item["max"] = round(max(values), 3)
                item["n"] = len(values)
            # 状态取缓存里最差的一档，避免“平均正常、单个样本超限”被读成正常
            item["status"] = "abnormal" if "abnormal" in statuses else (
                "ok" if "ok" in statuses else (statuses[0] if statuses else "missing"))
        else:
            item["value"] = None
            item["status"] = "missing"
            if reasons:
                item["missing_reason"] = reasons[0]
    return out


def _record_view(conn, cfg: AppConfigs, row, with_metrics: bool = True) -> dict:
    cond = cfg.condition(row["condition_id"])
    item = {
        "record_id": row["record_id"],
        "file_id": row["file_id"],
        "condition_id": row["condition_id"],
        "condition_name": cond.name if cond else row["condition_id"],
        "function": cond.function if cond else "",
        "vehicle": row["vehicle"],
        "file_name": row["file_name"],
        "recorded_at": row["recorded_at"],
        "analysis_status": row["analysis_status"],
    }
    if row["analysis_status"] == "file_gone":
        item["note"] = "文件已不在服务器上"
    elif row["analysis_status"] == "error":
        item["note"] = row["last_error"]
    if with_metrics and row["analysis_status"] == "ok":
        item["metrics"] = _summary(conn, row["record_id"])
    return item


def records_query(
    service: RecordService,
    cfg: AppConfigs,
    *,
    file_id: Optional[str] = None,
    condition_id: Optional[str] = None,
    vehicle: Optional[str] = None,
    function_key: Optional[str] = None,
    keyword: Optional[str] = None,
    limit: int = 20,
) -> dict:
    """按编号/关键词/工况/车型查记录 + 指标汇总；未命中时给出库里有哪些可查。

    编号与车型按「包含」匹配（说「4.1」「carB」都能查到）；keyword 再跨
    编号/车型/文件名做一次模糊搜索，用户只记得文件名片段时也能定位。
    """
    if function_key and cfg.function(function_key) is None:
        return {"ok": False, "hint": f"未知功能 {function_key}"}
    cond_ids = ([c.id for c in cfg.conditions_of(function_key)] if function_key else None)
    if condition_id and cfg.condition(condition_id) is None:
        return {"ok": False, "hint": f"未知工况 {condition_id}",
                "condition_ids": list(cfg.conditions)[:8]}
    conn = service.connect()
    try:
        rows = store.query_records(conn, file_id=file_id, condition_id=condition_id,
                                   condition_ids=cond_ids, vehicle=vehicle,
                                   keyword=keyword,
                                   limit=min(max(1, int(limit or 20)), 50))
        items = [_record_view(conn, cfg, r) for r in rows]
        result: Dict[str, Any] = {
            "ok": True,
            "count": len(items),
            "records": items,
            "db_stats": store.status_counts(conn),
        }
        if not items:
            result["hint"] = "库里没有符合条件的记录；可先确认编号/车型/工况是否已导入"
            result["vehicles"] = store.distinct(conn, "vehicle")
            result["file_ids"] = store.distinct(conn, "file_id")
        return result
    finally:
        conn.close()


def records_compare(
    service: RecordService,
    cfg: AppConfigs,
    *,
    condition_id: Optional[str] = None,
    function_key: Optional[str] = None,
    vehicles: Optional[List[str]] = None,
    metric: Optional[str] = None,
    limit: int = 30,
) -> dict:
    """同一工况下横向对比客观指标（默认该功能全部工况、全部车型）。

    只比已经算过的记录；没算过的在这里不触发重算，而是列进 not_analyzed，
    由用户点开单条记录再算——对比表里混进“正在计算”会让结论无法复现。
    """
    if condition_id:
        conds = [cfg.condition(condition_id)]
        if conds[0] is None:
            return {"ok": False, "hint": f"未知工况 {condition_id}"}
    elif function_key:
        if cfg.function(function_key) is None:
            return {"ok": False, "hint": f"未知功能 {function_key}"}
        conds = cfg.conditions_of(function_key)
    else:
        return {"ok": False, "hint": "至少给出 condition_id 或 function_key，避免跨工况乱比"}
    if metric and metric not in cfg.metrics:
        return {"ok": False, "hint": f"未知指标 {metric}"}

    conn = service.connect()
    try:
        groups = []
        not_analyzed: List[dict] = []
        compared_metric = metric
        for cond in conds:
            rows = store.query_records(conn, condition_id=cond.id,
                                       condition_ids=None, limit=limit)
            if vehicles:
                rows = [r for r in rows if r["vehicle"] in vehicles]
            if not rows:
                continue
            sample = []
            for r in rows:
                if r["analysis_status"] != "ok":
                    not_analyzed.append({
                        "record_id": r["record_id"], "vehicle": r["vehicle"],
                        "file_id": r["file_id"], "condition_id": cond.id,
                        "analysis_status": r["analysis_status"],
                    })
                    continue
                summary = _summary(conn, r["record_id"])
                if metric:
                    summary = {metric: summary[metric]} if metric in summary else {}
                sample.append({
                    "record_id": r["record_id"],
                    "vehicle": r["vehicle"],
                    "file_id": r["file_id"],
                    "file_name": r["file_name"],
                    "recorded_at": r["recorded_at"],
                    "metrics": summary,
                })
            if sample:
                groups.append({
                    "condition_id": cond.id,
                    "condition_name": cond.name,
                    "function": cond.function,
                    "file_ids": list(cond.file_ids),
                    "records": sample,
                })
        result: Dict[str, Any] = {
            "ok": True,
            "metric": compared_metric,
            "groups": groups,
            "record_count": sum(len(g["records"]) for g in groups),
            "not_analyzed": not_analyzed[:10],
        }
        if not groups:
            result["hint"] = ("没有可对比的已分析记录；先点开记录或运行 "
                              "scripts/import_records.py 后补齐分析")
        elif len(groups[0]["records"]) < 2 and not vehicles:
            result["hint"] = "该工况当前只有一条记录，暂无法构成横向对比"
        return result
    finally:
        conn.close()
