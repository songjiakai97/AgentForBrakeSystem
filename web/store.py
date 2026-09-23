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

    def to_dict(self) -> dict:
        return {
            "file_id": self.file_id,
            "name": self.name,
            "size": self.size,
            "uploaded_at": self.uploaded_at,
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
    def __init__(self, upload_dir: str = "/tmp/brake-agent-uploads"):
        self.upload_dir = upload_dir
        os.makedirs(upload_dir, exist_ok=True)
        self.chats: Dict[str, Chat] = {}
        self.sessions: Dict[str, Session] = {}
        self._lock = threading.RLock()

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
                    if os.path.exists(f.path):
                        os.remove(f.path)
                except OSError:
                    pass
            return True

    # ---------------- file / session ----------------
    def add_file(self, chat: Chat, name: str, content: bytes) -> FileEntry:
        with self._lock:
            fid = "fx_" + uuid.uuid4().hex[:8]
            safe = f"{fid}_{os.path.basename(name)}"
            path = os.path.join(self.upload_dir, safe)
            with open(path, "wb") as f:
                f.write(content)
            entry = FileEntry(file_id=fid, name=os.path.basename(name), path=path, size=len(content))
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
