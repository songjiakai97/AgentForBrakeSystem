"""试验记录库端点（design.md §17）：清单/详情/对比/整段曲线。"""

from fastapi import APIRouter, HTTPException, Query
from typing import List, Optional

from brake_analyzer.charts.html import build_chart_option, timeseries_payload
from brake_analyzer.configs import pick
from brake_analyzer.drive import layout_with_drive
from brake_analyzer.records import store as record_store
from brake_analyzer.records.query import records_compare
from brake_analyzer.records.service import RecordNotFound
from web import state as st
from web.state import CFG, RECORDS, RECORDS_DB

router = APIRouter()


def _record_cond_cards(cond, metrics_rows):
    """把缓存指标行拼成结果卡片：补上 ok_range/单位（缓存里只存值）。"""
    out = []
    for m in metrics_rows:
        tmpl = st.CFG.metrics.get(m["metric_key"])
        entry = pick(cond.metric_index, m["metric_key"], None) if cond else None
        item = dict(m)
        item["category"] = tmpl.category if tmpl else ""
        item["ok_range"] = (entry or {}).get("ok_range")
        out.append(item)
    return out


@router.get("/api/records/meta")
def api_records_meta():
    """记录库筛选项 + 库内状态；页面首次加载用。"""
    conn = st.RECORDS.connect()
    try:
        return {
            "db": st.RECORDS_DB,
            "vehicles": record_store.distinct(conn, "vehicle"),
            "file_ids": record_store.distinct(conn, "file_id"),
            "status": record_store.status_counts(conn),
            "functions": [{"key": f.key, "name": f.name} for f in st.CFG.functions.values()],
            "conditions": [
                {"id": c.id, "name": c.name, "function": c.function,
                 "file_ids": list(c.file_ids), "params": dict(c.params)}
                for c in st.CFG.conditions.values()
            ],
            "metrics": [{"key": k, "name": t.name, "unit": t.unit}
                        for k, t in st.CFG.metrics.items()],
        }
    finally:
        conn.close()


@router.get("/api/records/compare")
def api_records_compare(condition_id: Optional[str] = Query(None),
                        function_key: Optional[str] = Query(None),
                        vehicles: Optional[List[str]] = Query(None),
                        metric: Optional[str] = Query(None),
                        vehicle: Optional[str] = Query(None),
                        file_id: Optional[str] = Query(None),
                        keyword: Optional[str] = Query(None)):
    """横向对比（页面表格用）：同一工况下不同车型的客观指标。

    车型/编号/关键词与清单页同口径传进来：页面筛了却比全库，会出现
    「对比表里的记录不是我搜出来的」。`vehicle` 是页面单选，`vehicles` 给对话工具用。
    """
    picked = list(vehicles or [])
    if vehicle and vehicle not in picked:
        picked.append(vehicle)
    return records_compare(st.RECORDS, st.CFG, condition_id=condition_id,
                           function_key=function_key,
                           vehicles=picked or None, metric=metric,
                           file_id=file_id, keyword=keyword,
                           keyword_condition_ids=(st.CFG.conditions_by_text(keyword)
                                                  if keyword else None))


@router.get("/api/records")
def api_records(file_id: Optional[str] = Query(None),
                condition_id: Optional[str] = Query(None),
                vehicle: Optional[str] = Query(None),
                function_key: Optional[str] = Query(None),
                keyword: Optional[str] = Query(None),
                recorded_from: Optional[str] = Query(None),
                recorded_to: Optional[str] = Query(None),
                sort: str = Query("default"),
                limit: int = Query(200, ge=1, le=500)):
    """记录清单：只读元数据，不触发解析。编号/车型/关键词按「包含」匹配。

    关键词还走一遍配置侧（工况名/路面/动作），与对话里的 query_records 同口径——
    否则页面上输「干沥青」搜不到、模型那边却能搜到，两个入口两套结论。
    """
    cond_ids = ([c.id for c in st.CFG.conditions_of(function_key)]
                if function_key else None)
    rows = st.RECORDS.list_records(file_id=file_id, condition_id=condition_id,
                                condition_ids=cond_ids, vehicle=vehicle,
                                keyword=keyword,
                                keyword_condition_ids=(st.CFG.conditions_by_text(keyword)
                                                       if keyword else None),
                                recorded_from=recorded_from,
                                recorded_to=recorded_to,
                                order=("time" if sort == "time" else "default"),
                                limit=limit)
    for r in rows:
        cond = st.CFG.condition(r["condition_id"])
        r["condition_name"] = cond.name if cond else r["condition_id"]
        r["function"] = cond.function if cond else ""
    return {"count": len(rows), "records": rows, "db": st.RECORDS_DB}


@router.get("/api/records/{record_id}")
def api_record(record_id: str, refresh: bool = Query(False)):
    """单条记录：必要时惰性分析，返回结果卡片数据 + 窗口。"""
    try:
        if refresh:
            status, _ = st.RECORDS.analyze(record_id, force=True)
            detail = st.RECORDS.record_detail(record_id, ensure=False)
            detail["analysis_status"] = status
        else:
            detail = st.RECORDS.record_detail(record_id)
    except RecordNotFound as e:
        raise HTTPException(404, str(e))
    cond = st.CFG.condition(detail["record"]["condition_id"])
    detail["metrics"] = _record_cond_cards(cond, detail["metrics"])
    detail["condition_name"] = cond.name if cond else detail["record"]["condition_id"]
    detail["function"] = cond.function if cond else ""
    fn = st.CFG.function(cond.function) if cond else None
    detail["signals"] = list(fn.signals) if fn else []
    return detail


@router.get("/api/records/{record_id}/timeseries")
def api_record_timeseries(record_id: str, signal: Optional[str] = Query(None)):
    """整段曲线 + 事件窗口高亮；默认只画该功能关心的通道。"""
    try:
        signals, row = st.RECORDS.load_signals(record_id)
    except RecordNotFound as e:
        raise HTTPException(404, str(e))
    cond = st.CFG.condition(row["condition_id"])
    fn = st.CFG.function(cond.function) if cond else None
    # 曲线要配窗口：先确保有缓存（已算过就是一次查询，没算过才解析）
    try:
        st.RECORDS.analyze(record_id)
    except RecordNotFound as e:
        raise HTTPException(404, str(e))
    conn = st.RECORDS.connect()
    try:
        windows = record_store.sample_windows(conn, record_id)
        metric_rows = record_store.metric_rows(conn, record_id)
    finally:
        conn.close()
    window = None
    if windows:
        window = (windows[0]["t_start"], windows[0]["t_end"])
    names = [signal] if signal else (list(fn.signals) if fn and fn.signals else None)
    statuses = {m["status"] for m in metric_rows}
    highlight = ("abnormal" if "abnormal" in statuses
                 else "missing" if "missing" in statuses or not metric_rows else "ok")
    payload = timeseries_payload(signals, window=None, series_names=names,
                                 layout=layout_with_drive(
                                     st.CFG, fn.key if fn else "", signals,
                                     None, st.CFG.chart_layout))
    payload["file_name"] = row["file_name"]
    payload["windows"] = windows
    payload["chart_option"] = build_chart_option(
        signals, window=None, series_names=names,
        layout=layout_with_drive(st.CFG, fn.key if fn else "", signals,
                                 None, st.CFG.chart_layout),
        highlight_status=highlight,
        highlight=window,
        title=f"{row['vehicle']} · {row['file_name']}",
    )
    return payload
