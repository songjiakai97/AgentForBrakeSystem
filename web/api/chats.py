"""会话/消息/运行/分析/反馈/报告下载端点（design.md §7.2 / §8）。"""

import json
import os
import shutil
import time
import uuid

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from typing import List, Optional

from brake_analyzer.charts.html import build_chart_option, timeseries_payload
from brake_analyzer.events.segment import is_valid
from brake_analyzer.llm.prompts import digest_from_analysis
from brake_analyzer.schemas import json_safe
from web import state as st
from web import state as st
from web.state import (
    ALLOWED_EXT, BLF_READY, CFG, ENGINE, LLM, MAX_UPLOAD_BYTES,
    REPORT_DIR, RUNS, STORE, TRACES, _chat_or_404, _find_analysis,
)

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

router = APIRouter()


def _upload_ext_check(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(
            400, f"仅接受 {'/'.join(sorted(ALLOWED_EXT))} 上传，收到: {name}"
                 + ("" if BLF_READY else "（当前环境缺 python-can/cantools，BLF 不可用）"))
    return ext


@router.post("/api/chats")
def api_create_chat():
    return st.STORE.create_chat().to_dict()


@router.get("/api/chats")
def api_list_chats():
    return st.STORE.list_chats()


@router.get("/api/chats/{cid}")
def api_get_chat(cid: str):
    return _chat_or_404(cid).to_dict()


@router.delete("/api/chats/{cid}")
def api_delete_chat(cid: str):
    _chat_or_404(cid)
    st.TRACES.remove_chat(cid)
    st.STORE.delete_chat(cid)
    # 该会话生成的报告文件一并清掉（upload 文件由 Store.delete_chat 级联删，报告不在其中）
    shutil.rmtree(os.path.join(REPORT_DIR, cid), ignore_errors=True)
    latest = st.STORE.latest_chat()
    return {"deleted": cid, "next_chat": latest.chat_id if latest else None}


@router.post("/api/chats/{cid}/files")
async def api_upload(cid: str, files: List[UploadFile] = File(...)):
    chat = _chat_or_404(cid)
    # 一次请求 = 一批（用户多选上传是一个动作），后续分析默认只取最新这批（§8.2）
    batch_id = "fb_" + uuid.uuid4().hex[:8]
    out = []
    for uf in files:
        name = uf.filename or "unnamed.mf4"
        _upload_ext_check(name)
        content = await uf.read()
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(400, f"文件超过 {MAX_UPLOAD_BYTES // 1048576}MB 上限: {name}")
        entry = st.STORE.add_file(chat, name, content, batch_id=batch_id)
        out.append(entry.to_dict())
        st.STORE.add_message(chat, "assistant", "attachment",
                          content=f"已接收文件 {name}（{os.path.splitext(name)[1].lstrip('.')}）",
                          data=entry.to_dict())
    return {"files": out}


class LocalFileIn(BaseModel):
    path: str


@router.post("/api/chats/{cid}/files/by-path")
def api_upload_by_path(cid: str, body: LocalFileIn):
    """引用本机已有文件（零拷贝）：服务和数据在同一台机器时，免掉整份文件的
    HTTP 上传，登记原路径直接分析。本地引用的文件在删除会话时不会被删除。"""
    chat = _chat_or_404(cid)
    raw = (body.path or "").strip().strip('"').strip("'")
    if not raw:
        raise HTTPException(400, "路径为空")
    name = os.path.basename(raw)
    _upload_ext_check(name)
    if not os.path.isfile(raw):
        raise HTTPException(400, f"文件不存在或不是普通文件: {raw}")
    size = os.path.getsize(raw)
    if size > MAX_UPLOAD_BYTES:
        raise HTTPException(400, f"文件超过 {MAX_UPLOAD_BYTES // 1048576}MB 上限: {name}")
    batch_id = "fb_" + uuid.uuid4().hex[:8]
    entry = st.STORE.add_file(chat, name, b"", batch_id=batch_id,
                           source="local", local_path=raw)
    st.STORE.add_message(chat, "assistant", "attachment",
                      content=f"已引用本地文件 {name}（原路径，未拷贝）",
                      data=entry.to_dict())
    return {"files": [entry.to_dict()]}


@router.delete("/api/chats/{cid}/files/{fid}")
def api_remove_file(cid: str, fid: str):
    chat = _chat_or_404(cid)
    ok = st.STORE.remove_file(chat, fid)
    if not ok:
        raise HTTPException(404, "文件不存在")
    return {"removed": fid}


class MessageIn(BaseModel):
    text: str


@router.post("/api/chats/{cid}/messages")
def api_send_message(cid: str, body: MessageIn):
    chat = _chat_or_404(cid)
    if not body.text.strip():
        raise HTTPException(400, "消息为空")
    run, started = st.RUNS.start(st.ENGINE, chat, body.text.strip())
    if not started:
        raise HTTPException(409, f"本对话已有一轮在进行中：{run.run_id}，请等待其完成")
    for _ in run.since(-1):
        pass
    if run.status == "error":
        raise HTTPException(500, f"本轮执行失败：{run.error}")
    added = [ev["data"] for ev in run.events if ev["event"] == "message"]
    return {"messages": [json_safe(a) for a in added], "run_id": run.run_id}


def _frame(ev: dict) -> str:
    """SSE 帧：id 即事件序号，断线重连时作为 after 传回即可精确续传。"""
    data = json.dumps(json_safe(ev["data"]), ensure_ascii=False, default=str, allow_nan=False)
    return f"id: {ev['seq']}\nevent: {ev['event']}\ndata: {data}\n\n"


def _run_stream(gen_run, after: int):
    yield ("event: run\ndata: "
           + json.dumps(json_safe(gen_run.info()), ensure_ascii=False, allow_nan=False) + "\n\n")
    for ev in gen_run.since(after):
        yield _frame(ev)


@router.post("/api/chats/{cid}/messages/stream")
async def api_send_stream(cid: str, body: MessageIn):
    """启动一轮并在 SSE 上回放；客户端断开不影响执行，刷新后走 /api/runs/{id}/stream 续看。"""
    chat = _chat_or_404(cid)
    if not body.text.strip():
        raise HTTPException(400, "消息为空")
    run, started = st.RUNS.start(st.ENGINE, chat, body.text.strip())
    if not started:
        # detail 带 run_id：前端据此连上正在跑的那一轮（拿到进度、也能停止它），
        # 而不是只弹一条「请求失败」——那会让界面死在既停不掉又等不出的状态。
        raise HTTPException(409, {
            "message": f"本对话已有一轮在进行中：{run.run_id}",
            "run_id": run.run_id,
            "status": run.status,
        })
    return StreamingResponse(_run_stream(run, -1), media_type="text/event-stream")


@router.get("/api/chats/{cid}/run")
def api_active_run(cid: str):
    """刷新后询问本会话是否有进行中的一轮（引擎在后台线程跑，不会因断线丢）。"""
    chat = _chat_or_404(cid)
    run = st.RUNS.active_for(chat.chat_id)
    return {"run": run.info() if run else None}


@router.post("/api/runs/{run_id}/stop")
def api_stop_run(run_id: str):
    """请求停止进行中的一轮：只置标记，引擎在下一个检查点收尾（半截正文落库）。"""
    run = st.RUNS.stop(run_id)
    if run is None:
        raise HTTPException(404, "该轮已回收或不存在（会话消息仍可从 GET /api/chats 读取）")
    return {"run_id": run.run_id, "stop_requested": run.stop_requested, "status": run.status}


@router.get("/api/runs/{run_id}/stream")
def api_run_stream(run_id: str, after: int = Query(-1)):
    """订阅/回放某轮事件；after 传已收到的最大 seq 做增量续传。"""
    run = st.RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "该轮已回收或不存在（会话消息仍可从 GET /api/chats 读取）")
    return StreamingResponse(_run_stream(run, after), media_type="text/event-stream")


class SelectIn(BaseModel):
    target_type: str          # function | condition
    target_key: str
    profile: Optional[str] = None


@router.post("/api/chats/{cid}/select")
def api_select(cid: str, body: SelectIn):
    """统一选择：function → 该功能下的工况面板；condition → 写档位并分析。"""
    chat = _chat_or_404(cid)
    # 后台还有一轮在跑时不接受新动作：两者都会写同一份消息列表，且并发分析无意义
    running = st.RUNS.active_for(chat.chat_id)
    if running is not None:
        raise HTTPException(409, f"本对话有一轮回复正在进行（{running.run_id}），请等待完成后再选择")
    if body.target_type == "function":
        fn = st.CFG.function(body.target_key)
        if fn is None:
            raise HTTPException(400, f"未知功能 {body.target_key}")
        st.STORE.mark_panels_consumed(chat, chosen=fn.name)
        chat.selected_function = fn.key
        chat.selected_condition = None
        st.STORE.note_selection(chat)
        from brake_analyzer.agent.tools import recommend_targets

        rec = recommend_targets(st.CFG, fn.name, 8, function_key=fn.key)
        msg = st.ENGINE._options(chat, rec)
        return {"messages": [msg.to_dict()]}

    if body.target_type == "condition":
        cond = st.CFG.condition(body.target_key)
        if cond is None:
            raise HTTPException(400, f"未知工况 {body.target_key}")
        fn = st.CFG.function(cond.function)
        profile = (body.profile or "").strip() or None
        chosen = f"{cond.name}（{profile}）" if profile else cond.name
        if fn.profiles and not profile:
            # 需要档位但未提供 → 返回档位选择面板
            st.STORE.mark_panels_consumed(chat, chosen=chosen)
            msg = st.ENGINE._options(chat, {
                "hop": "profile", "function": fn.key,
                "target": {"key": cond.id, "name": cond.name},
                "profiles": fn.profiles, "profile_dims": fn.profile_dims,
            })
            return {"messages": [msg.to_dict()]}
        if profile:
            tokens = [t for t in profile.replace(",", "|").replace("_", "|").split("|") if t]
            bad = [t for t in tokens if fn.profiles and t not in fn.profiles]
            if bad:
                raise HTTPException(400, f"档位 {bad} 不在功能 {fn.key} 允许列表 {fn.profiles}")
        st.STORE.mark_panels_consumed(chat, chosen=chosen)
        st.STORE.note_selection(chat)
        added, _digests = st.ENGINE._analyze(chat, cond.id, profile)
        return {"messages": [m.to_dict() for m in added]}

    raise HTTPException(400, "target_type 必须为 function|condition")


# ---------------------------------------------------------------- analyses
FB_STATUSES = {"accepted": "已采纳", "verified": "验证有效", "ineffective": "验证无效"}


def _sync_suggestion_to_message(res) -> None:
    """res.llm_suggestion 更新后同步到对话卡片：卡片 data 是创建时的深拷贝，
    不同步的话前端要刷新才能看到反馈/追加建议。"""
    for chat in st.STORE.chats.values():
        for m in chat.messages:
            if (m.kind == "analysis_result" and isinstance(m.data, dict)
                    and m.data.get("analysis_id") == res.analysis_id
                    and isinstance(m.data.get("llm_suggestion"), dict)):
                m.data["llm_suggestion"] = res.llm_suggestion


class FeedbackIn(BaseModel):
    status: str                    # accepted | verified | ineffective
    note: str = ""


@router.post("/api/analyses/{analysis_id}/feedback")
def api_analysis_feedback(analysis_id: str, body: FeedbackIn):
    """标定建议闭环：记录用户反馈（采纳/验证结果），并让 st.LLM 基于反馈给出
    下一步标定动作（追加条目）。st.LLM 不可用时只落反馈，不阻塞。"""
    if body.status not in FB_STATUSES:
        raise HTTPException(400, f"status 必须是 {'/'.join(FB_STATUSES)}")
    res, _sample = _find_analysis(analysis_id)
    if res is None:
        raise HTTPException(404, "分析结果不存在（服务可能已重启）")
    if not res.llm_suggestion:
        raise HTTPException(400, "该分析没有 st.LLM 标定建议（可能已降级为仅规则结论）")

    fb = {"status": body.status, "note": body.note.strip()[:500], "ts": time.time()}
    res.llm_suggestion["feedback"] = fb

    # 闭环判断：采纳/验证反馈 + 原建议 + 实测摘要 → st.LLM 判断是否追加标定条目
    followup = None
    if st.LLM.available:
        try:
            digests = digest_from_analysis(res.to_dict())
            system = (
                "你是制动标定助手。用户对上一轮标定建议给出了执行反馈。"
                "请基于反馈与实测指标，判断原建议是否需要修正，并给出下一步标定动作。"
                '只输出 JSON：{"summary": "一段话判断", "calibration_actions": '
                '[{"param": "可标定量", "direction": "增大|减小", "metric": "针对指标", '
                '"expected_effect": "预期效果", "risk": "风险"}]}。'
                "若无需要追加的动作，calibration_actions 给空数组并在 summary 说明。"
            )
            user = json.dumps({
                "condition": res.condition_name, "profile": res.profile,
                "original_suggestion": res.llm_suggestion,
                "user_feedback": {"status": FB_STATUSES[body.status], "note": fb["note"]},
                "measured": digests,
            }, ensure_ascii=False, default=str)
            followup = st.LLM.complete_json(system, user)
        except Exception:
            followup = None        # 闭环建议是锦上添花，失败不吞掉反馈本身
    if followup and isinstance(followup.get("calibration_actions", []), list):
        res.llm_suggestion["followup"] = followup
    _sync_suggestion_to_message(res)
    return {"analysis_id": analysis_id,
            "feedback": fb,
            "followup": res.llm_suggestion.get("followup"),
            "llm_suggestion": res.llm_suggestion}


@router.get("/api/analyses/{analysis_id}")
def api_analysis(analysis_id: str):
    res, sample = _find_analysis(analysis_id)
    if res is None:
        raise HTTPException(404, "分析结果不存在（服务可能已重启）")
    if sample is not None and sample.sample_id == analysis_id:
        # 单样本视图
        return {
            "analysis_id": sample.analysis_id,
            "sample_id": sample.sample_id,
            "run_index": sample.run_index,
            "file_name": sample.file_name,
            "condition_id": res.condition_id,
            "condition_name": res.condition_name,
            "profile": res.profile,
            "window": {"t_start": sample.window.t_start, "t_end": sample.window.t_end},
            "metrics": [m.to_dict() for m in sample.metrics],
            "verdicts": [v.to_dict() for v in sample.verdicts],
            "llm_suggestion": res.llm_suggestion,
            "degraded": res.degraded,
        }
    return res.to_dict()


@router.get("/api/analyses/{analysis_id}/timeseries")
def api_analysis_timeseries(analysis_id: str, signal: Optional[str] = Query(None)):
    res, sample = _find_analysis(analysis_id)
    if res is None or sample is None:
        raise HTTPException(404, "分析结果不存在")
    sess = st.STORE.sessions.get(sample.file_id)
    if sess is None or sess.signals is None:
        raise HTTPException(404, "信号缓存不存在（服务可能已重启）")
    window = None
    if is_valid(sample.window):
        window = (sample.window.t_start, sample.window.t_end)
    # 默认只画该功能关心的通道（ABS 不画 TCS_Active），分组仍按 signals.yaml
    fn = st.CFG.function(res.function_key)
    names = [signal] if signal else (list(fn.signals) if fn and fn.signals else None)
    layout = st.CFG.chart_layout
    payload = timeseries_payload(sess.signals, window=window, series_names=names, layout=layout)
    payload["file_name"] = sample.file_name
    payload["chart_option"] = build_chart_option(
        sess.signals, window=window, series_names=names,
        layout=layout,
        highlight_status=("abnormal" if any(m.status == "abnormal" for m in sample.metrics)
                          else "missing" if any(m.status == "missing" for m in sample.metrics)
                          else "ok"),
        title=f"{sample.file_name} · run {sample.run_index}",
    )
    return payload


# ---------------------------------------------------------------- 报告下载（§18）
@router.get("/api/reports/{cid}")
def api_report_list(cid: str):
    """列出某会话已生成的报告（名字/大小/生成时间），供前端报告卡片带历史。"""
    _chat_or_404(cid)
    d = os.path.join(REPORT_DIR, cid)
    out = []
    if os.path.isdir(d):
        for name in sorted(os.listdir(d), reverse=True):
            path = os.path.join(d, name)
            if os.path.isfile(path) and name.endswith(".docx"):
                out.append({"file": name,
                            "size": os.path.getsize(path),
                            "created_at": os.path.getmtime(path),
                            "download": f"/api/reports/{cid}/{name}"})
    return {"reports": out}


@router.get("/api/reports/{cid}/{name}")
def api_report_download(cid: str, name: str):
    """下载某会话生成的分析报告。文件名由服务端生成（无用户可控成分），
    basename 是纵深防御：路径穿越在这里结构上走不通。"""
    _chat_or_404(cid)
    base = os.path.basename(name)
    path = os.path.join(REPORT_DIR, cid, base)
    if not os.path.isfile(path) or not base.endswith(".docx"):
        raise HTTPException(404, "报告不存在（可能已被删除或未生成）")
    return FileResponse(path, filename=base, media_type=DOCX_MIME)
