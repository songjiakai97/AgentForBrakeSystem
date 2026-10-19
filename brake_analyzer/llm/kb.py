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
    doc_title: str = ""          # 所属文档标题（breadcrumb 顶层，工况过滤用）


@dataclass
class CaseEntry:
    case_id: str
    function: str
    keywords: List[str]
    data: dict = field(default_factory=dict)


def _vec_table(model: str) -> str:
    """每个 embedding 模型一张 vec0 表：维度不同天然隔离，换模型不迁移。"""
    return "vec_" + re.sub(r"\W+", "_", model)


# 块尺寸上限（字符）：embedding 输入上限最小的模型也要能整块编码。
# 按 bge-large-zh-v1.5 的 512 token 校准（中文 ≈1 字符/token，留特殊 token 余量）；
# 上限更大的模型（如 qwen3-embedding 的 32K）下只是多切几块，无害。
MAX_CHUNK_CHARS = 480


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
        self._param_rows = []      # 表格行抽取（§11.4 参数索引），内存态，线性查足够
        self._load_manual()
        self._load_cases()
        self._param_rows = self._extract_param_rows()

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
            doc_title_m = re.search(r"^#\s+(.+)$", text, re.M)
            doc_title = doc_title_m.group(1).strip() if doc_title_m else ""
            for i, m in enumerate(matches):
                start = m.end()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                self.sections[m.group(1)] = ManualSection(
                    section_id=m.group(1),
                    title=(m.group(2) or "").strip(),
                    text=text[start:end].strip(),
                    doc_title=doc_title,
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

    # ---------------- 语义检索（分块 + 混合） ----------------
    def _section_units(self) -> List:
        """兼容入口：整节单元（不带 #### 分块）。"""
        return [(sid, f"{s.title}\n{s.text}") for sid, s in self.sections.items()]

    @staticmethod
    def _split_chunks(text: str) -> List:
        """节内按 #### 子标题切块；无子标题 → 整节一块。返回 [(子标题|None, 块文本)]。"""
        blocks, head, buf = [], None, []
        for line in text.splitlines():
            m = re.match(r"^####\s+(.*)$", line)
            if m:
                if head is not None or any(l.strip() for l in buf):
                    blocks.append((head, "\n".join(buf).strip()))
                head, buf = m.group(1).strip(), [line]
            else:
                buf.append(line)
        blocks.append((head, "\n".join(buf).strip()))
        return [(h, t) for h, t in blocks if t.strip()]

    @staticmethod
    def _split_oversized(text: str, max_chars: int = MAX_CHUNK_CHARS) -> List[str]:
        """尺寸兜底：结构切不动（无 ####）或单块仍超限时按尺寸强制切。

        优先按空行段落边界分组（不破坏段落语义）；单个巨型段落（无空行的
        大表格/超长行）再按行、按字符硬切。产出各段都 ≤ max_chars。
        """
        if len(text) <= max_chars:
            return [text]
        paras, buf = [], []
        for line in text.splitlines():
            if line.strip():
                buf.append(line)
            elif buf:
                paras.append("\n".join(buf))
                buf = []
        if buf:
            paras.append("\n".join(buf))
        units = []
        for para in paras:
            if len(para) <= max_chars:
                units.append(para)
                continue
            cur = ""
            for line in para.splitlines():
                if len(line) > max_chars:
                    # 单行超限（无换行的巨型段落/表格）：按字符硬切，不留死角
                    if cur:
                        units.append(cur)
                        cur = ""
                    for i in range(0, len(line), max_chars):
                        units.append(line[i:i + max_chars])
                    continue
                cand = f"{cur}\n{line}" if cur else line
                if len(cand) > max_chars and cur:
                    units.append(cur)
                    cur = line
                else:
                    cur = cand
            if cur:
                units.append(cur)
        return units

    def _index_chunks(self) -> List[dict]:
        """索引单元 = 块（§11.4 分层分块 + 尺寸兜底）。

        第一层按 #### 子标题切（结构边界）；切出的块超过 MAX_CHUNK_CHARS 时
        按尺寸强制再切（段落边界 → 行 → 字符），保证 embedding 输入永远有界——
        超限的块会被 embedding 端点静默截断，块尾内容就永远搜不到了。
        单块节沿用 section_id；多块节为 `section_id#N`。每块文本都带节标题前缀。
        """
        chunks = []
        for sid, sec in self.sections.items():
            pieces = []
            for head, body in self._split_chunks(sec.text):
                head_txt = f"{head}\n" if head else ""
                if len(head_txt) + len(body) <= MAX_CHUNK_CHARS:
                    pieces.append(f"{head_txt}{body}")
                else:
                    for piece in self._split_oversized(body, MAX_CHUNK_CHARS - len(head_txt)):
                        pieces.append(f"{head_txt}{piece}")
            if not pieces:
                pieces = [sec.text]
            if len(pieces) == 1:
                chunks.append({"chunk_id": sid, "section_id": sid,
                               "text": f"{sec.title}\n{pieces[0]}"})
                continue
            for k, body in enumerate(pieces, start=1):
                cid = sid if k == 1 else f"{sid}#{k}"
                chunks.append({"chunk_id": cid, "section_id": sid,
                               "text": f"{sec.title}\n{body}"})
        return chunks

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
        """把当前知识块增量同步进向量库：只 embed 新增/内容变更的块，删除已不存在的。

        块是切分粒度（§11.4）：改一节里的一个 #### 小块只重算那一块。
        返回 False 表示语义通道不可用（sqlite-vec 缺失或 embedding 失败）。
        """
        chunks = self._index_chunks()
        if not chunks:
            return False
        with self._vec_lock:
            new = {c["chunk_id"]: (hashlib.sha256(
                        f"{self._embed_model}|{c['text']}".encode("utf-8")).hexdigest()[:16],
                    c["text"]) for c in chunks}
            old = dict(self._vec_db.execute(
                "SELECT section_id, content_hash FROM items WHERE model=?",
                (self._embed_model,)).fetchall())
            changed = [cid for cid, (h, _) in new.items() if old.get(cid) != h]
            gone = [cid for cid in old if cid not in new]
            table = _vec_table(self._embed_model)
            if changed:
                vecs = self._embed_fn([new[cid][1] for cid in changed])
                if not vecs:
                    return False
                dim = len(vecs[0])
                self._vec_db.execute(
                    f'CREATE VIRTUAL TABLE IF NOT EXISTS "{table}" '
                    f"USING vec0(embedding float[{dim}])")
                for cid, v in zip(changed, vecs):
                    old_id = self._vec_db.execute(
                        "SELECT id FROM items WHERE model=? AND section_id=?",
                        (self._embed_model, cid)).fetchone()
                    if old_id:
                        self._vec_db.execute(f'DELETE FROM "{table}" WHERE rowid=?', (old_id[0],))
                        self._vec_db.execute("DELETE FROM items WHERE id=?", (old_id[0],))
                    cur = self._vec_db.execute(
                        "INSERT INTO items(model, section_id, content_hash) VALUES(?,?,?)",
                        (self._embed_model, cid, new[cid][0]))
                    self._vec_db.execute(
                        f'INSERT INTO "{table}"(rowid, embedding) VALUES(?, ?)',
                        (cur.lastrowid, np.asarray(v, dtype=np.float32).tobytes()))
            for cid in gone:
                old_id = self._vec_db.execute(
                    "SELECT id FROM items WHERE model=? AND section_id=?",
                    (self._embed_model, cid)).fetchone()
                if old_id:
                    self._vec_db.execute(f'DELETE FROM "{table}" WHERE rowid=?', (old_id[0],))
                    self._vec_db.execute("DELETE FROM items WHERE id=?", (old_id[0],))
            self._vec_db.commit()
            return True

    @staticmethod
    def _keyword_rank(query: str, chunks: Dict[str, dict]) -> List:
        """关键词通道：整词命中计 3 分，中文二元组覆盖率计分（无分词场景的兜底）。"""
        terms = [t for t in re.split(r"[\s,，、;；]+", query) if t]
        scored = []
        for cid, c in chunks.items():
            hay = c["text"]
            score = 0.0
            for t in terms:
                if t in hay:
                    score += 3.0
                    continue
                grams = [t[i:i + 2] for i in range(len(t) - 1)] if len(t) > 2 else [t]
                if grams:
                    score += sum(1.0 for g in grams if g in hay) / len(grams)
            if score > 0:
                scored.append((score, cid))
        scored.sort(key=lambda x: -x[0])
        return scored

    @staticmethod
    def _piece_sort_key(chunk_id: str):
        """块序还原：section_id 在前，#N 按序号（字典序会把 #10 排到 #2 前）。"""
        if "#" not in chunk_id:
            return (chunk_id, 0)
        base, n = chunk_id.rsplit("#", 1)
        try:
            return (base, int(n))
        except ValueError:
            return (chunk_id, 0)

    def _merge_section_hits(self, selected: List[str], chunks: Dict[str, dict],
                            chunk_scores: Dict[str, float]) -> List[dict]:
        """small-to-big：**块做排序精度，节做返回完整性**。

        命中的块所属节，返回时合并为节级完整文本（该节全部 #### 块按序拼接）——
        否则 19 行参数表被切成 3 块后，top-5 里凑不齐就漏行，"有哪些参数"类
        问题答不全。score 取该节所有命中块中的最高分。"""
        seen, order = [], []
        for cid in selected:
            sid = chunks[cid]["section_id"]
            if sid not in seen:
                seen.append(sid)
                order.append(sid)
        out = []
        for sid in order:
            sec = self.sections.get(sid)
            if sec is None:
                continue
            pieces = sorted((c for c in chunks.values() if c["section_id"] == sid),
                            key=lambda c: self._piece_sort_key(c["chunk_id"]))
            bodies = []
            for p in pieces:
                t = p["text"]
                if t.startswith(sec.title + "\n"):
                    t = t[len(sec.title) + 1:]
                bodies.append(t)
            out.append({"id": sid, "chunk": sid, "title": sec.title,
                        "text": sec.title + "\n" + "\n\n".join(bodies),
                        "score": round(max((chunk_scores.get(c["chunk_id"], 0.0)
                                            for c in pieces), default=0.0), 4)})
        return out

    def search(self, query: str, top_k: int = 3, condition: Optional[str] = None) -> List[dict]:
        """混合检索（§11.4）：块做排序精度（语义 KNN + 关键词 RRF 融合），
        节做返回完整性（small-to-big 合并，表格/条款不因分块残缺）。
        embedding 不可用时排序退纯关键词。永不抛异常、永不阻塞主链路。

        condition：工况词硬过滤——按节标题/文档标题过滤块，防同名参数表/条款
        跨工况串扰（真实标定手册多个工况下有同名"标定参数说明"表）。
        过滤后为空则回退不过滤（宁多勿缺，LLM 侧有 query 上下文兜着）。

        返回 [{"id"(节id), "title", "text"(节完整文本), "score"}]，按相关度降序。
        last_search_mode 记录实际通道：hybrid / semantic / keyword。
        """
        self.last_search_mode = ""
        query = (query or "").strip()
        if not query or not self.sections:
            return []
        chunks = {c["chunk_id"]: c for c in self._index_chunks()}
        if not chunks:
            return []
        if condition and condition.strip():
            cond = condition.strip()
            def _in_cond(c):
                sec = self.sections.get(c["section_id"])
                return sec is not None and (cond in sec.title or cond in sec.doc_title)
            scoped = {cid: c for cid, c in chunks.items() if _in_cond(c)}
            if scoped:
                chunks = scoped

        kw_scored = self._keyword_rank(query, chunks)
        sem_order: List[str] = []
        if self._vec_db is not None and self._sync_vectors():
            qv = self._embed_fn([query])
            if qv:
                qb = np.asarray(qv[0], dtype=np.float32).tobytes()
                table = _vec_table(self._embed_model)
                with self._vec_lock:
                    knn = self._vec_db.execute(
                        f'SELECT rowid, distance FROM "{table}" '
                        "WHERE embedding MATCH ? AND k=? ORDER BY distance",
                        (qb, min(10, max(top_k * 2, 6)))).fetchall()
                for rowid, _dist in knn:
                    row = self._vec_db.execute(
                        "SELECT section_id FROM items WHERE id=?", (rowid,)).fetchone()
                    if row:
                        sem_order.append(row[0])

        if not sem_order:
            selected = [(cid, s) for s, cid in kw_scored[:top_k]]
            mode = "keyword"
        else:
            scores: Dict[str, float] = {}
            for r, cid in enumerate(sem_order):
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (60 + r)
            for r, (_s, cid) in enumerate(kw_scored):
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (60 + r)
            selected = [(cid, scores[cid])
                        for cid in sorted(scores, key=lambda c: -scores[c])[:top_k]]
            mode = "hybrid" if kw_scored else "semantic"
        if not selected:
            return []
        chunk_scores = {cid: s for cid, s in selected}
        hits = self._merge_section_hits([cid for cid, _ in selected], chunks, chunk_scores)
        if hits:
            self.last_search_mode = mode
            return hits
        return []

    # ---------------- 参数索引（表格行结构化抽取，§11.4） ----------------
    @staticmethod
    def _norm_param(name: str) -> str:
        """参数名归一化：大小写/下划线/连字符/空白统一，用于精确匹配。"""
        return re.sub(r"[\s_\-]+", "", (name or "").lower())

    def _extract_param_rows(self) -> List[dict]:
        """从手册 md 表格逐行抽取结构化参数记录。

        表头首列是序号列（序号/#/No）时剔除；行 key = 归一化后的首个数据格
        （约定为参数名列）。标定手册的表格是主数据载体，参数值查询走这里
        精确命中，不走向量。"""
        rows: List[dict] = []
        for sid, sec in self.sections.items():
            lines = sec.text.splitlines()
            i = 0
            while i < len(lines):
                stripped = lines[i].lstrip()
                if stripped.startswith("|") and i + 1 < len(lines)                         and re.match(r"^\s*\|[\s:\-|]+\|\s*$", lines[i + 1]):
                    headers = [c.strip() for c in stripped.strip("|").split("|")]
                    j = i + 2
                    while j < len(lines) and lines[j].lstrip().startswith("|"):
                        cells = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                        h, c = headers, cells
                        if h and h[0] in ("序号", "#", "No", "no"):
                            h, c = h[1:], c[1:]
                        if c and any(c):
                            key = self._norm_param(c[0]) if c[0] else ""
                            rows.append({"section_id": sid, "title": sec.title,
                                         "doc_title": sec.doc_title,
                                         "headers": h, "cells": c, "key": key})
                        j += 1
                    i = j
                else:
                    i += 1
        return rows

    def get_param(self, name: str, condition: Optional[str] = None,
                  top_k: int = 5) -> List[dict]:
        """参数精确/归一化查询（P1 参数索引）。

        name 传参数名（如 Cp_FA、Opt_Instabil_Lambda_HA），归一化后精确匹配；
        精确无命中时降级**子串匹配**（双向：模型记不全参数名也能接住，命中标注
        `match: partial`）。condition 传工况词时按 breadcrumb（节标题/文档标题）
        过滤，防同名参数表跨工况串扰。
        返回 [{param, fields, section, section_id, doc_title, match}]。
        """
        key = self._norm_param(name)
        if not key:
            return []

        def _cond_ok(r):
            if not condition:
                return True
            return condition in r["title"] or condition in r["doc_title"]

        exact, partial = [], []
        for r in self._param_rows:
            if not _cond_ok(r):
                continue
            if r["key"] == key:
                exact.append(r)
            elif key in r["key"] or (len(key) >= 3 and r["key"] in key):
                partial.append(r)
        rows = [(r, "exact") for r in exact[:top_k]] or                [(r, "partial") for r in partial[:top_k]]
        return [{
            "param": r["cells"][0] if r["cells"] else name,
            "fields": dict(zip(r["headers"], r["cells"])),
            "section": r["title"], "section_id": r["section_id"],
            "doc_title": r["doc_title"], "match": m,
        } for r, m in rows]

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
