"""知识库读取与检索（design.md §11.4）。

- 手册：按 `### <section_id>` 标题切分，kb_ref → 原文段。
- 案例：index.yaml 过滤 + 关键词打分 top-k。
- 语义检索：sqlite-vec（knowledge/vectors.db，单文件无服务）。向量按
  「模型名 + 内容 hash」增量同步——手册增长只向量化新增/变更的节，永不
  全量重算。sqlite-vec 不可用或 embedding 失败自动退回关键词打分。
"""

import hashlib
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import yaml


@dataclass
class ManualSection:
    section_id: str
    title: str
    text: str


@dataclass
class CaseEntry:
    case_id: str
    function: str
    keywords: List[str]
    data: dict = field(default_factory=dict)


def _vec_table(model: str) -> str:
    """每个 embedding 模型一张 vec0 表：维度不同天然隔离，换模型不迁移。"""
    return "vec_" + re.sub(r"\W+", "_", model)


class KnowledgeBase:
    def __init__(self, kb_dir: str = "knowledge"):
        self.kb_dir = kb_dir
        self.sections: Dict[str, ManualSection] = {}
        self.cases: List[CaseEntry] = []
        # 语义检索：embedder 由外部注入（web 启动时按 ENABLE_RAG/可用性决定）；
        # 向量存 sqlite-vec（vectors.db），注入时打开，重载不清表（增量同步对账）
        self._embed_fn = None
        self._embed_model = ""
        self._vec_db = None
        self._vec_lock = threading.Lock()
        self.last_search_mode = ""   # 供 /api/kb/search 回显用了哪条通道
        self._load()

    def _load(self):
        self.sections = {}
        self.cases = []
        self._load_manual()
        self._load_cases()

    # 上传/删除后全量重载：知识量小（几十节），重载是毫秒级，不值得做增量
    def reload(self):
        self._load()

    def set_embedder(self, fn, model: str) -> None:
        """注入向量化回调并打开向量库；换模型 = 换 vec0 表，数据不迁移。"""
        self._embed_fn = fn
        self._embed_model = model
        self._vec_db = None
        self._open_vec_store()

    # ---------------- 手册 ----------------
    def _load_manual(self):
        mdir = os.path.join(self.kb_dir, "manual")
        if not os.path.isdir(mdir):
            return
        for fn in sorted(os.listdir(mdir)):
            if not fn.endswith(".md"):
                continue
            with open(os.path.join(mdir, fn), "r", encoding="utf-8") as f:
                text = f.read()
            # 以 "### id — 标题" 切 section
            pattern = re.compile(r"^###\s+([\w.\-]+)(?:\s+[—-]\s*(.+))?$", re.M)
            matches = list(pattern.finditer(text))
            for i, m in enumerate(matches):
                start = m.end()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                self.sections[m.group(1)] = ManualSection(
                    section_id=m.group(1),
                    title=(m.group(2) or "").strip(),
                    text=text[start:end].strip(),
                )

    def section(self, section_id: str) -> Optional[ManualSection]:
        return self.sections.get(section_id)

    # ---------------- 案例 ----------------
    def _load_cases(self):
        cdir = os.path.join(self.kb_dir, "cases")
        idx = os.path.join(cdir, "index.yaml")
        if not os.path.exists(idx):
            return
        with open(idx, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        for item in raw.get("cases", []):
            entry = CaseEntry(
                case_id=item.get("case_id", ""),
                function=item.get("function", ""),
                keywords=[str(k) for k in item.get("keywords", [])],
            )
            path = os.path.join(cdir, item.get("file", ""))
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    entry.data = yaml.safe_load(f) or {}
            self.cases.append(entry)

    def similar_cases(
        self,
        function_key: str,
        query_terms: List[str],
        top_k: int = 2,
    ) -> List[CaseEntry]:
        scored = []
        for c in self.cases:
            if function_key and c.function != function_key:
                continue
            score = sum(1 for t in query_terms if any(t in k or k in t for k in c.keywords))
            if c.function == function_key:
                score += 0.5
            scored.append((score, c))
        scored.sort(key=lambda x: -x[0])
        return [c for s, c in scored if s > 0][:top_k]

    # ---------------- 语义检索 ----------------
    def _section_units(self) -> List:
        """向量化单元 = 标题 + 正文（标题里有「横摆/制动距离」这类强判别词，必须参与）。"""
        return [(sid, f"{s.title}\n{s.text}") for sid, s in self.sections.items()]

    def _open_vec_store(self) -> bool:
        """打开/初始化 sqlite-vec 存储；扩展不可用时返回 False（语义通道关闭，走关键词兜底）。"""
        try:
            import sqlite3
            import sqlite_vec

            db = sqlite3.connect(os.path.join(self.kb_dir, "vectors.db"),
                                 check_same_thread=False)
            db.enable_load_extension(True)
            sqlite_vec.load(db)
            db.enable_load_extension(False)
            db.execute(
                "CREATE TABLE IF NOT EXISTS items("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "model TEXT NOT NULL, section_id TEXT NOT NULL, "
                "content_hash TEXT NOT NULL, UNIQUE(model, section_id))")
            self._vec_db = db
            # KB 是全局单例，FastAPI 线程池多线程并发调用 search：连接允许跨线程，
            # 但读写必须串行（sqlite 同连接并发写会报错）
            self._vec_lock = threading.Lock()
            return True
        except Exception:
            self._vec_db = None
            return False

    def _sync_vectors(self) -> bool:
        """把当前手册节增量同步进向量库：只 embed 新增/内容变更的节，删除已不存在的。

        返回 False 表示语义通道不可用（sqlite-vec 缺失或 embedding 失败）。
        """
        units = self._section_units()
        if not units:
            return False
        with self._vec_lock:
            new = {sid: (hashlib.sha256(f"{self._embed_model}|{text}".encode("utf-8")).hexdigest()[:16], text)
                   for sid, text in units}
            old = dict(self._vec_db.execute(
                "SELECT section_id, content_hash FROM items WHERE model=?",
                (self._embed_model,)).fetchall())
            changed = [sid for sid, (h, _) in new.items() if old.get(sid) != h]
            gone = [sid for sid in old if sid not in new]
            table = _vec_table(self._embed_model)
            if changed:
                vecs = self._embed_fn([new[sid][1] for sid in changed])
                if not vecs:
                    return False
                dim = len(vecs[0])
                self._vec_db.execute(
                    f'CREATE VIRTUAL TABLE IF NOT EXISTS "{table}" '
                    f"USING vec0(embedding float[{dim}])")
                for sid, v in zip(changed, vecs):
                    old_id = self._vec_db.execute(
                        "SELECT id FROM items WHERE model=? AND section_id=?",
                        (self._embed_model, sid)).fetchone()
                    if old_id:
                        self._vec_db.execute(f'DELETE FROM "{table}" WHERE rowid=?', (old_id[0],))
                        self._vec_db.execute("DELETE FROM items WHERE id=?", (old_id[0],))
                    cur = self._vec_db.execute(
                        "INSERT INTO items(model, section_id, content_hash) VALUES(?,?,?)",
                        (self._embed_model, sid, new[sid][0]))
                    self._vec_db.execute(
                        f'INSERT INTO "{table}"(rowid, embedding) VALUES(?, ?)',
                        (cur.lastrowid, np.asarray(v, dtype=np.float32).tobytes()))
            for sid in gone:
                old_id = self._vec_db.execute(
                    "SELECT id FROM items WHERE model=? AND section_id=?",
                    (self._embed_model, sid)).fetchone()
                if old_id:
                    self._vec_db.execute(f'DELETE FROM "{table}" WHERE rowid=?', (old_id[0],))
                    self._vec_db.execute("DELETE FROM items WHERE id=?", (old_id[0],))
            self._vec_db.commit()
            return True

    def search(self, query: str, top_k: int = 3) -> List[dict]:
        """混合检索：sqlite-vec KNN top-k；向量库不可用或 embedding 失败时
        关键词子串打分兜底。

        返回 [{"id","title","text","score"}]，按分数降序。last_search_mode
        记录实际走了哪条通道（semantic/keyword），调试端点回显用。
        """
        self.last_search_mode = ""
        if not query.strip() or not self.sections:
            return []
        if self._vec_db is not None and self._sync_vectors():
            qv = self._embed_fn([query])
            if qv:
                table = _vec_table(self._embed_model)
                qb = np.asarray(qv[0], dtype=np.float32).tobytes()
                out = []
                with self._vec_lock:
                    knn = self._vec_db.execute(
                        f'SELECT rowid, distance FROM "{table}" '
                        "WHERE embedding MATCH ? AND k=? ORDER BY distance",
                        (qb, top_k)).fetchall()
                    for rowid, dist in knn:
                        row = self._vec_db.execute(
                            "SELECT section_id FROM items WHERE id=?", (rowid,)).fetchone()
                        if not row:
                            continue
                        sec = self.sections.get(row[0])
                        if sec:
                            out.append({"id": sec.section_id, "title": sec.title,
                                        "text": sec.text, "score": float(dist)})
                if out:
                    self.last_search_mode = "semantic"
                    return out
        # 关键词兜底：查询词整体命中计 3 分；中文无分词，再用二元组（bigram）
        # 覆盖率计分——「横摆稳定性怎么判定」这类整句也能命中含「横摆/判定」的节
        terms = [t for t in re.split(r"[\s,，、;；]+", query) if t]
        scored = []
        for sid, sec in self.sections.items():
            hay = sec.title + "\n" + sec.text
            score = 0.0
            for t in terms:
                if t in hay:
                    score += 3.0
                    continue
                grams = [t[i:i + 2] for i in range(len(t) - 1)] if len(t) > 2 else [t]
                if grams:
                    score += sum(1.0 for g in grams if g in hay) / len(grams)
            if score > 0:
                scored.append((score, sid))
        scored.sort(key=lambda x: -x[0])
        self.last_search_mode = "keyword"
        return [{"id": sid,
                 "title": self.sections[sid].title,
                 "text": self.sections[sid].text,
                 "score": float(sc)}
                for sc, sid in scored[:top_k]]

    # ---------------- 浏览 ----------------
    def browse(self) -> dict:
        by_fn: Dict[str, list] = {}
        for sid, sec in self.sections.items():
            by_fn.setdefault(sid.split(".", 1)[0], []).append(
                {"section_id": sid, "title": sec.title,
                 "preview": sec.text[:80], "text": sec.text})
        return {
            "manual": [{"function": k, "sections": v} for k, v in by_fn.items()],
            "cases": [
                {
                    "case_id": c.case_id,
                    "function": c.function,
                    "keywords": c.keywords,
                    "anomaly": c.data.get("anomaly", {}),
                    "outcome": c.data.get("outcome", {}),
                }
                for c in self.cases
            ],
        }
