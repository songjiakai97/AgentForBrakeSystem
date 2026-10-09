"""运行管理：把一轮引擎执行与 HTTP 连接解耦。

问题：此前引擎在 SSE 响应生成器里跑，客户端刷新/断网 → 生成器被关闭 →
这一轮回复直接丢在半路（已落库的消息在，思考流与后续消息都没了）。

方案：
- 引擎在后台线程跑，产生的每个事件按单调 seq 缓存进 Run；
- SSE 只是缓冲区的订阅者，支持从任意 seq 回放后实时跟随；
- 客户端断开不影响执行，刷新后凭 chat 上的 active_run 重新订阅、回放整轮过程。
"""

import threading
import time
import uuid
from typing import Dict, List, Optional


class Run:
    """一轮引擎执行的事件缓冲（线程安全，append-only）。"""

    def __init__(self, run_id: str, chat_id: str, text: str):
        self.run_id = run_id
        self.chat_id = chat_id
        self.text = text
        self.events: List[dict] = []          # {"seq", "event", "data"}
        self.status = "running"               # running | done | error
        self.error: str = ""
        self.created_at = time.time()
        self.finished_at: Optional[float] = None
        self.cond = threading.Condition()

    def publish(self, event: str, data: dict) -> int:
        with self.cond:
            seq = len(self.events)
            self.events.append({"seq": seq, "event": event, "data": data})
            self.cond.notify_all()
            return seq

    def finish(self, status: str = "done", error: str = "") -> None:
        with self.cond:
            self.status = status
            self.error = error
            self.finished_at = time.time()
            self.cond.notify_all()

    def since(self, after: int = -1):
        """从 after 之后开始：先回放缓存，再实时跟随，直到本轮结束。"""
        idx = max(0, after + 1)
        while True:
            with self.cond:
                while idx >= len(self.events) and self.status == "running":
                    self.cond.wait(0.5)
                batch = self.events[idx:]
                idx += len(batch)
                status, err = self.status, self.error
            for ev in batch:
                yield ev
            if status != "running":
                if err:
                    yield {"seq": idx, "event": "run_error", "data": {"error": err}}
                return

    def info(self) -> dict:
        return {
            "run_id": self.run_id,
            "chat_id": self.chat_id,
            "status": self.status,
            "event_count": len(self.events),
            "text": self.text,
            "error": self.error,
            "created_at": self.created_at,
        }


class RunManager:
    """按 chat 串行：一个会话同时只跑一轮，避免两个 writer 交叉写消息。"""

    KEEP_DONE_SEC = 600.0
    KEEP_MAX = 100

    def __init__(self):
        self._runs: Dict[str, Run] = {}
        self._active: Dict[str, Run] = {}      # chat_id -> running run
        self._lock = threading.Lock()

    def start(self, engine, chat, text: str):
        """启动本会话的一轮；已有进行中一轮时返回 (existing, False)（不重复跑）。"""
        with self._lock:
            cur = self._active.get(chat.chat_id)
            if cur is not None and cur.status == "running":
                return cur, False
            run = Run("run_" + uuid.uuid4().hex[:8], chat.chat_id, text)
            self._runs[run.run_id] = run
            self._active[chat.chat_id] = run
            self._gc()
        threading.Thread(target=self._worker, args=(engine, chat, text, run),
                         daemon=True, name=f"agent-run-{run.run_id}").start()
        return run, True

    def _worker(self, engine, chat, text: str, run: Run) -> None:
        try:
            for ev in engine.events(chat, text):
                run.publish(ev.get("event", "message"), ev.get("data", {}))
            run.finish("done")
        except Exception as e:                      # 引擎异常：状态置 error，前端可见
            run.finish("error", f"{type(e).__name__}: {e}")
        finally:
            with self._lock:
                if self._active.get(chat.chat_id) is run:
                    self._active.pop(chat.chat_id, None)

    def get(self, run_id: str) -> Optional[Run]:
        return self._runs.get(run_id)

    def active_for(self, chat_id: str) -> Optional[Run]:
        with self._lock:
            run = self._active.get(chat_id)
        if run is not None and run.status == "running":
            return run
        return None

    def _gc(self) -> None:
        now = time.time()
        done = [r for r in self._runs.values() if r.status != "running"]
        for r in done:
            if r.finished_at and now - r.finished_at > self.KEEP_DONE_SEC:
                self._runs.pop(r.run_id, None)
        if len(self._runs) > self.KEEP_MAX:
            keep = sorted(self._runs.values(), key=lambda r: r.created_at)
            for r in keep[: len(self._runs) - self.KEEP_MAX]:
                if r.status != "running":
                    self._runs.pop(r.run_id, None)
