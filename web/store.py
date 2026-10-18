"""会话内存模型（design.md §7.1）。仅内存存储，服务重启即丢失。"""

import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from brake_analyzer.loaders.base import SignalData


@dataclass
class FileEntry:
    file_id: str
    name: str
    path: str
    size: int
    uploaded_at: float = field(default_factory=time.time)
    # 同一次上传请求进来的一组文件共享 batch_id。默认分析范围是「最新一批」而不是
    # 「会话内全部」：多选上传是一个动作（一批），之后每次新上传都只想看新数据
    # （design.md §8.2 文件选择）。
    batch_id: str = ""
    # source=upload：文件已拷贝进 upload_dir，删除会话时一并删除；
    # source=local：只引用本机原路径（零拷贝），删除会话时绝不能动原文件
    source: str = "upload"

    def to_dict(self) -> dict:
        return {
            "file_id": self.file_id,
            "name": self.name,
            "size": self.size,
            "uploaded_at": self.uploaded_at,
            "batch_id": self.batch_id,
            "source": self.source,
        }


@dataclass
class Session:
    """绑定一个数据文件，缓存信号与各工况分析结果。"""
    file_id: str
    signals: Optional[Dict[str, SignalData]] = None
    analyses: Dict[str, object] = field(default_factory=dict)   # key: cond|profile → AnalysisResult

    def analyze_key(self, condition_id: str, profile: Optional[str]) -> str:
        return f"{condition_id}|{profile or ''}"


@dataclass
class Message:
    message_id: str
    role: str            # user | assistant
    kind: str            # text | target_options | analysis_result | attachment | error
    content: str = ""
    data: Optional[dict] = None
    ts: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {
            "message_id": self.message_id,
            "role": self.role,
            "kind": self.kind,
            "content": self.content,
            "ts": self.ts,
        }
        if self.data is not None:
            d["data"] = self.data
        if self.meta:
            d["meta"] = self.meta
        return d


@dataclass
class Chat:
    chat_id: str
    title: str = "新对话"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    files: List[FileEntry] = field(default_factory=list)
    messages: List[Message] = field(default_factory=list)
    selected_function: Optional[str] = None
    selected_condition: Optional[str] = None
    selected_profile: Optional[str] = None

    def touch(self):
        self.updated_at = time.time()

    def latest_batch(self) -> List["FileEntry"]:
        """会话内「最近一次上传」的那批文件。

        默认只分析这批：用户的「分析」意图几乎总是针对刚传的数据，把历史文件一起
        重跑既慢又会让结论混进上一趟的数据。同一次上传请求（多选）算一批，
        未分批登记的文件（脚本 / 测试直接 add_file）batch_id 为空，天然归为同一批。
        """
        if not self.files:
            return []
        bid = self.files[-1].batch_id
        return [f for f in self.files if f.batch_id == bid]

    def summary(self) -> dict:
        return {
            "chat_id": self.chat_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "file_count": len(self.files),
            "message_count": len(self.messages),
            "selected_function": self.selected_function,
            "selected_condition": self.selected_condition,
            "selected_profile": self.selected_profile,
        }

    def to_dict(self) -> dict:
        d = self.summary()
        d["files"] = [f.to_dict() for f in self.files]
        d["messages"] = [m.to_dict() for m in self.messages]
        return d


class Store:
    # 已加载信号的文件最多驻留几个：信号是大头（小时级数据每文件几十上百 MB），
    # analyses 很小可全部保留。超出即把最久未用的 signals 置 None（用时重载）
    SIGNALS_LRU_CAP = 5

    def __init__(self, upload_dir: str = "/tmp/brake-agent-uploads"):
        self.upload_dir = upload_dir
        os.makedirs(upload_dir, exist_ok=True)
        self.chats: Dict[str, Chat] = {}
        self.sessions: Dict[str, Session] = {}
        self._lock = threading.RLock()
        self._sig_lru: List[str] = []      # 最近加载过信号的 file_id，最旧在前

    def note_signals_loaded(self, file_id: str) -> None:
        """engine 每次加载信号后调用：维护 LRU，超限淘汰最旧文件的信号缓存。"""
        if file_id in self._sig_lru:
            self._sig_lru.remove(file_id)
        self._sig_lru.append(file_id)
        while len(self._sig_lru) > self.SIGNALS_LRU_CAP:
            old = self._sig_lru.pop(0)
            sess = self.sessions.get(old)
            if sess is not None:
                sess.signals = None

    # ---------------- chat ----------------
    def create_chat(self, title: str = "") -> Chat:
        with self._lock:
            cid = "ch_" + uuid.uuid4().hex[:8]
            chat = Chat(chat_id=cid, title=title or "新对话")
            self.chats[cid] = chat
            return chat

    def get_chat(self, cid: str) -> Optional[Chat]:
        return self.chats.get(cid)

    def list_chats(self) -> List[dict]:
        return sorted((c.summary() for c in self.chats.values()), key=lambda x: -x["updated_at"])

    def latest_chat(self) -> Optional[Chat]:
        if not self.chats:
            return None
        return max(self.chats.values(), key=lambda c: c.updated_at)

    def delete_chat(self, cid: str) -> bool:
        """级联删除：messages、files（含落盘文件）、Sessions（缓存）、traces 由调用方处理。"""
        with self._lock:
            chat = self.chats.pop(cid, None)
            if chat is None:
                return False
            for f in chat.files:
                self.sessions.pop(f.file_id, None)
                try:
                    # 只删自己拷贝进 upload_dir 的副本；本地引用的原始文件绝不能动
                    if f.source == "upload" and os.path.exists(f.path):
                        os.remove(f.path)
                except OSError:
                    pass
            return True

    # ---------------- file / session ----------------
    def add_file(self, chat: Chat, name: str, content: bytes,
                 batch_id: str = "", source: str = "upload",
                 local_path: str = "") -> FileEntry:
        """登记数据文件。source=upload 时 content 落盘到 upload_dir；
        source=local 时零拷贝，直接引用本机原路径（local_path）。"""
        with self._lock:
            fid = "fx_" + uuid.uuid4().hex[:8]
            safe = f"{fid}_{os.path.basename(name)}"
            if source == "local" and local_path:
                path = local_path
            else:
                path = os.path.join(self.upload_dir, safe)
                with open(path, "wb") as f:
                    f.write(content)
            entry = FileEntry(file_id=fid, name=os.path.basename(name), path=path,
                              size=len(content), batch_id=batch_id, source=source)
            chat.files.append(entry)
            self.sessions[fid] = Session(file_id=fid)
            chat.touch()
            return entry

    def remove_file(self, chat: Chat, file_id: str) -> bool:
        with self._lock:
            before = len(chat.files)
            chat.files = [f for f in chat.files if f.file_id != file_id]
            if len(chat.files) == before:
                return False
            self.sessions.pop(file_id, None)
            if file_id in self._sig_lru:
                self._sig_lru.remove(file_id)
            prefix = file_id + "_"
            try:
                for n in os.listdir(self.upload_dir):
                    if n.startswith(prefix):
                        try:
                            os.remove(os.path.join(self.upload_dir, n))
                        except OSError:
                            pass
            except OSError:
                pass
            chat.touch()
            return True

    def session(self, file_id: str) -> Session:
        if file_id not in self.sessions:
            self.sessions[file_id] = Session(file_id=file_id)
        return self.sessions[file_id]

    def mark_panels_consumed(self, chat: "Chat", chosen: Optional[str] = None) -> int:
        """面板是一次性的：新一轮选择/提问后把旧的候选面板标记为已消费。

        chosen 记录用户实际点选的项，用于前端回显「已选择 …」；
        只有最新的一个未消费面板会被写入 chosen。
        """
        n = 0
        latest_open = None
        for m in chat.messages:
            if m.kind == "target_options" and not m.meta.get("consumed"):
                latest_open = m
        for m in chat.messages:
            if m.kind != "target_options" or m.meta.get("consumed"):
                continue
            m.meta["consumed"] = True
            n += 1
        if chosen is not None and latest_open is not None:
            latest_open.meta["chosen"] = chosen
        chat.touch()
        return n

    # ---------------- message ----------------
    def add_message(self, chat: Chat, role: str, kind: str, content: str = "",
                    data: Optional[dict] = None, meta: Optional[dict] = None) -> Message:
        msg = Message(
            message_id="ms_" + uuid.uuid4().hex[:10],
            role=role, kind=kind, content=content, data=data,
            meta=meta or {},
        )
        chat.messages.append(msg)
        if kind == "text" and role == "user" and chat.title == "新对话":
            chat.title = content.strip()[:24] or chat.title
        chat.touch()
        return msg
