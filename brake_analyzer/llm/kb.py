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
import unicodedata
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

# 单条命中的最小可读长度：窗口分配再紧张也不能低于它，否则只能丢命中
SNIPPET_MIN_CHARS = 240

# snippet_window 可能返回的最长裁剪标记（note）的字数上界。调用方按它给每条
# 命中预留 JSON 包装预算——新增/加长 note 文案时必须同步这里，低估会让
# payload 撞外层硬闸、多丢一条命中。
CUT_NOTE_MAX_CHARS = 40

# 参数精确命中的返回上限。同一参数名在多个工况表里各有一行是常态，所以不按
# top_k=5 截断；但也不能无界——整份 payload 还受 TOOL_PAYLOAD_CLIP_CHARS 约束。
PARAM_EXACT_MAX_ROWS = 20


# 参数名/信号名这类标识符：字母数字段用 . _ - / 连接（Cp_FA、Opt-Instabil、
# abs.s1、m_Vehicle）。只用来把「标识符」和自然语言词分开，不做通用分词。
_IDENT_RE = re.compile(r"[A-Za-z0-9]+(?:[._\-/][A-Za-z0-9]+)*")

# 破折号族（U+2010..U+2015、U+2212）：Word 导出的 U+2011 非断行连字符是常客，
# NFKC 只把它折成 U+2010，仍需统一成 ASCII '-'，否则标识符会被切成两段
_DASH_RE = re.compile(r"[\u2010-\u2015\u2212]")


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
            self._load_manual_text(text, fn)

    def _load_manual_text(self, text: str, filename: str) -> None:
        """md 手册解析（§11.6）：与 docx 摄取同一套层级语义。

        - 转义还原：Word→md 导出会把参数名写成 m\_Vehicle，不还原则
          get_param 的归一化匹配全空（真实文档实测踩坑）。
        - 层级切节：H1/H2/H3 都参与切节（真实手册是 章→节→小节 的多层结构，
          只认 `###` 会把章导语粘进相邻节、且 H2 标题全丢）；`####` 及更深
          留在节内，由 _index_chunks 继续切块。
        - 两种节标题形态：
          * `### <ascii_id> — 标题`（本仓库约定，规则 kb_ref 引用的锚）→
            用声明的 id，扁平化，不参与面包屑；
          * 其余（中文标题的 H1/H2/H3）→ 生成 `slug.NN` id，节标题带上级
            面包屑路径（`实车标定 · 对开工况`）。
        - 首个节标题之前的内容（封面/前言/概述）收为 `<slug>.00` 前言节，
          不静默丢弃（功能目的/物理原理是高价值知识）。
        """
        text = text.replace("\_", "_").replace("\*", "*")
        slug = re.sub(r"[^\w\-]+", "_", os.path.splitext(os.path.basename(filename))[0])             .strip("_").lower() or "doc"
        pat = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.M)
        heads = list(pat.finditer(text))
        if not heads:
            return
        # doc_title 兜底链：首个 H1 是通用词（前言/引言——章节式手册的常见开头，
        # 不具备文档区分度）时退回文件名；否则多文档场景下 condition 过滤失效
        first_h1 = next((m.group(2) for m in heads if len(m.group(1)) == 1), "")
        _generic = {"前言", "引言", "概述", "目录", "简介", "附录", "正文"}
        doc_title = first_h1 if first_h1 and first_h1 not in _generic             else os.path.splitext(filename)[0]
        declared = re.compile(r"^([A-Za-z0-9_.\-]+)(?:\s+[—-]\s*(.+))?$")

        def _emit(sid: str, title: str, body: str) -> None:
            if not body.strip():
                return          # 纯标题行（如只有一行文档大标题）不产生空节
            self.sections[sid] = ManualSection(
                section_id=sid, title=title, text=body.strip(), doc_title=doc_title)

        # 首个节标题前的内容 → 前言节
        pre = text[:heads[0].start()].strip()
        if pre:
            _emit(f"{slug}.00", f"{doc_title} · 前言", pre)

        path: List[str] = []          # 层级 → 标题（面包屑，按层级覆盖）
        for k, m in enumerate(heads):
            lvl = len(m.group(1))
            raw = m.group(2).strip()
            dm = declared.match(raw) if lvl == 3 else None
            if lvl == 3 and dm and dm.group(1):
                # 仓库约定形态：扁平 id，标题照旧，面包屑复位。
                # 声明 id 只保证单文件内唯一；跨文件撞车（两本手册都有
                # "### abs.s1"）时自动加文件前缀，避免后加载的静默覆盖前者
                sid = dm.group(1)
                if sid in self.sections:
                    sid = f"{slug}.{sid}"
                title = (dm.group(2) or raw).strip()
                _emit(sid, title, text[m.end(): heads[k + 1].start()
                                       if k + 1 < len(heads) else len(text)])
                continue
            path = path[:lvl - 1]
            path.append(raw)
            crumb = " · ".join(p for p in path[:lvl] if p)
            start = m.end()
            end = heads[k + 1].start() if k + 1 < len(heads) else len(text)
            _emit(f"{slug}.{k + 1:02d}", crumb, text[start:end])

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

        body 是不含标题前缀与子标题前缀的原文切片（必为节正文的连续子串），
        合并回节时用它定位块的字节区间，供 snippet_window 取中心窗口。
        """
        chunks = []
        for sid, sec in self.sections.items():
            pieces = []
            for head, body in self._split_chunks(sec.text):
                head_txt = f"{head}\n" if head else ""
                # _split_chunks 保留了 #### 原行：块正文已以该子标题开头时不再
                # 注入裸标题，否则合并回节会出现「子标题\n#### 子标题」双写
                if head_txt and body.lstrip().startswith(f"#### {head}"):
                    head_txt = ""
                if len(head_txt) + len(body) <= MAX_CHUNK_CHARS:
                    pieces.append((body, f"{head_txt}{body}"))
                else:
                    for piece in self._split_oversized(body, MAX_CHUNK_CHARS - len(head_txt)):
                        pieces.append((piece, f"{head_txt}{piece}"))
            if not pieces:
                pieces = [(sec.text, sec.text)]
            prefix = f"{sec.title}\n" if sec.title else ""
            if len(pieces) == 1:
                chunks.append(self._decorate_chunk(
                    {"chunk_id": sid, "section_id": sid,
                     "body": pieces[0][0], "text": prefix + pieces[0][1]}))
                continue
            for k, (body, piece) in enumerate(pieces, start=1):
                cid = sid if k == 1 else f"{sid}#{k}"
                chunks.append(self._decorate_chunk(
                    {"chunk_id": cid, "section_id": sid,
                     "body": body, "text": prefix + piece}))
        return chunks

    @staticmethod
    def _decorate_chunk(c: dict) -> dict:
        """给块补检索用的派生字段：折叠文本 + 归一化标识符集合。

        NFKC + casefold 的副本供关键词通道做大小写/全角无关的子串比较；
        标识符集合供 _ident_score 精确比对。每次 search 都会重建 chunks
        （手册只有几十块，重算是毫秒级），所以放在这里就够，查询侧不必
        再逐块做 Unicode 折叠。
        """
        folded = KnowledgeBase._fold_text(c["text"])
        c["text_fold"] = folded
        c["idents"] = {KnowledgeBase._norm_param(t)
                       for t in KnowledgeBase._iter_identifiers(folded)}
        return c

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
        """关键词通道：整词命中计 3 分，中文二元组覆盖率计分（无分词场景的兜底）。

        查询按「标识符 / 普通词」分开打分（_split_query），标识符走归一化比较、
        不产出二元组。旧实现对 Cp_FA 切出 Cp/p_/_F/FA，其中 p_ 会把含 Cp_RA
        的节一起拉进 top-k，于是「查一个参数」变成「两个参数都给你」——假阳性
        比搜不到更坏，模型会引用错值。
        """
        idents, plain = KnowledgeBase._split_query(query)
        if not idents and not plain:
            return []
        # 相邻短片段拼回一个标识符：用户/模型可能写成「cp fa」，那是同一个名字
        merged = "".join(idents) if len(idents) > 1 else ""
        scored = []
        for cid, c in chunks.items():
            hay = c["text_fold"]
            idset = c["idents"]
            score = 0.0
            if merged and KnowledgeBase._ident_score(merged, idset) >= 3.0:
                score += 3.0
            else:
                score += sum(KnowledgeBase._ident_score(t, idset) for t in idents)
            for t in plain:
                score += KnowledgeBase._plain_score(t, hay)
            if score > 0:
                scored.append((score, cid))
        scored.sort(key=lambda x: -x[0])
        return scored

    @staticmethod
    def _split_query(query: str):
        """查询分词：(标识符列表, 普通词列表)，标识符已归一化。

        分类只在 NFKC+折叠后按形状判断，不猜语言：含 ASCII 字母、由字母数字与
        . _ - / 组成的是标识符（Cp_FA、Opt-Instabil、abs.s1），其余按分隔符切。
        """
        idents, plain = [], []
        for t in re.split(r"[\s,，、;；]+", KnowledgeBase._fold_text(query)):
            if not t:
                continue
            if re.fullmatch(r"[a-z0-9._\-/]*[a-z][a-z0-9._\-/]*", t):
                idents.append(KnowledgeBase._norm_param(t))
            else:
                plain.append(t)
        return idents, plain

    @staticmethod
    def _ident_score(nq: str, idents: set) -> float:
        """归一化标识符与块内标识符集合的匹配分：全等 3，包含 1.5。

        包含匹配要求查询侧 ≥3 字符（与 get_param 的子串兜底同一门槛）：
        「cp」「fa」这种碎片前缀会把所有 Cp_* 一网打尽，是噪声不是召回。
        """
        if not nq:
            return 0.0
        if nq in idents:
            return 3.0
        if len(nq) >= 3 and any(nq in i for i in idents):
            return 1.5
        return 0.0

    @staticmethod
    def _plain_score(term: str, hay: str) -> float:
        """普通词（中文为主）：整词 3 分，否则二元组覆盖率；hay 已折叠大小写。"""
        if term in hay:
            return 3.0
        grams = [term[i:i + 2] for i in range(len(term) - 1)] if len(term) > 2 else [term]
        if not grams:
            return 0.0
        return sum(1.0 for g in grams if g in hay) / len(grams)

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

    @staticmethod
    def _locate_piece(text: str, body: str, cursor: int = 0):
        """在节原文里定位一个块的 [start, end)。

        精确 find 优先；尺寸硬切会丢掉段落间的空行，导致精确匹配失败，此时用
        「首行 + 尾行」锚定近似区间（窗口裁剪只需落在行边界，不要求逐字对齐）。
        定位不到返回 None，调用方整节降级。
        """
        if not body:
            return None
        idx = text.find(body, cursor)
        if idx < 0:
            idx = text.find(body)
        if idx >= 0:
            return (idx, idx + len(body))
        first = body.splitlines()[0]
        last = body.splitlines()[-1]
        s = text.find(first, cursor)
        if s < 0:
            s = text.find(first)
        if s < 0:
            return None
        e = text.find(last, s)
        if e < 0:
            return None
        return (s, e + len(last))

    @staticmethod
    def _table_head_row(text: str, pos: int):
        """pos 落在 Markdown 表格内时返回该表格块（表头行）的起点，否则 None。

        窗口只截到表格中段时，裸值行没有列名，模型会把「初始值」当「当前值」
        念——那是比漏行更坏的错，所以调用方宁可丢尾部几行也要把表头带进来。
        """
        lines = text.splitlines(keepends=True)
        offs, acc = [], 0
        for ln in lines:
            offs.append(acc)
            acc += len(ln)
        idx = 0
        for k, o in enumerate(offs):
            if o <= pos:
                idx = k
            else:
                break
        if idx >= len(lines) or not lines[idx].lstrip().startswith("|"):
            return None
        head = idx
        while head - 1 >= 0 and lines[head - 1].lstrip().startswith("|"):
            head -= 1
        return offs[head] if head != idx else None

    @staticmethod
    def snippet_window(hit: dict, budget: int):
        """以命中块为中心裁出 ≤ budget 字符的窗口（small-to-big 的下半段）。

        small-to-big 把块还原成整节返回，但节长轻易超过给 LLM 的片段预算；若从
        节头开始裁，窗口永远是「节标题 + 第一个块的前半」，命中块（常在表尾或
        后几块）连同答案一起被切掉——这就是「只搜到某个 #### 的前面部分」。
        这里围绕最佳命中块取窗，右扩步长大于左扩（表格阅读顺序向下），切点吸附
        到行边界，避免给出半行参数名。

        返回 (snippet, note)：note 非空表示仍有内容没给到模型，调用方据此提示。
        """
        title = (hit.get("title") or "").strip()
        text = hit.get("full") or hit.get("text") or ""
        header = f"## {title}\n" if title else ""
        # text 已带 "标题\n" 前缀（_merge_section_hits 的合并字段），剥掉避免双写
        if title and text.startswith(title + "\n"):
            text = text[len(title) + 1:]
        # "… " / " …" 省略标记各 2 字，预算要为它们留位，否则窗口反而超 budget
        room = budget - len(header) - 4
        if room < SNIPPET_MIN_CHARS:
            return ("", "（窗口预算不足，未给出正文）")
        if len(text) <= room:
            return (header + text, "")

        best = hit.get("best")
        if hit.get("fallback") or not best:
            return (header + text[:room], "…（未能按命中块定位，已按节头截断）")
        s0, e0 = best

        def _snap_start(i):
            j = text.rfind("\n", 0, i)
            return 0 if j < 0 else j + 1

        def _snap_end(i):
            j = text.find("\n", i)
            return len(text) if j < 0 else j

        lo, hi = _snap_start(s0), _snap_end(e0)
        if hi - lo > room:                      # 命中块自己就超预算：保住它本身
            return (header + "… " + text[lo:lo + room] + " …",
                    "…（命中块超出窗口：按参数名用 get_param 精查）")

        # 贪心扩张：每轮先试右（表格阅读顺序向下，权重更高），再试左；
        # 只有两侧都扩不动才停——单侧到文本边界不能终止另一侧
        def _expand(side):
            nonlocal lo, hi
            steps = ((240, 120, 60, 24) if side == "r" else (160, 80, 40, 16))
            for step in steps:
                if side == "r":
                    n_hi = _snap_end(min(len(text), hi + step))
                    if n_hi > hi and n_hi - lo <= room:
                        hi = n_hi
                        return True
                else:
                    n_lo = _snap_start(max(0, lo - step))
                    if n_lo < lo and hi - n_lo <= room:
                        lo = n_lo
                        return True
            return False

        while True:
            if not _expand("r") and not _expand("l"):
                break

        # 窗口起点落在表格中段时，把起点提到表头行：裸值行脱离列名，模型会把
        # 「初始值」当「当前值」念——比漏行更坏的错。前提是表头+命中行仍装得下，
        # 装不下就保持原窗口（命中块本身永远优先）
        th = KnowledgeBase._table_head_row(text, lo)
        if th is not None and th < lo:
            hit_end = _snap_end(min(len(text), e0 + 1))
            if hit_end - th <= room:
                lo = th
                hi = max(_snap_start(th + room), hit_end)
        notes = []
        if lo > 0:
            notes.append("前文已省略")
        if hi < len(text):
            notes.append("后文已省略：需要具体参数值用 get_param 按参数名查询")
        return ((header + ("… " if lo > 0 else "") + text[lo:hi]
                 + (" …" if hi < len(text) else "")),
                ("（" + "；".join(notes) + "）" if notes else ""))

    def _merge_section_hits(self, selected: List[str], chunks: Dict[str, dict],
                            chunk_scores: Dict[str, float]) -> List[dict]:
        """small-to-big：**块做排序精度，节做返回完整性**。

        命中的块所属节，返回时合并为节级完整文本（直接用节原文 sec.text，
        不再按块拼接——硬切块用空行 join 会在表格中间插空行，后半段行脱离
        表头变成裸值）。score 取该节所有命中块中的最高分。

        同时给出每个块在 sec.text 里的区间（pieces）与最佳块（best），供
        snippet_window 做「以命中块为中心」的窗口裁剪；正文里定位失败的节
        标记 fallback=True，调用方退回从头裁剪。
        """
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
            # 定位各块在节原文中的区间：body 是切块时的原文连续子串，
            # 顺序游标查找复原位置；尺寸硬切可能吃掉段落间的空行，故精确查找
            # 失败时退化为「首行+尾行」锚定（窗口只需近似边界，落在行首即可）
            spans, cursor, fallback = [], 0, False
            for p in pieces:
                loc = self._locate_piece(sec.text, p["body"], cursor)
                if loc is None:
                    fallback = True
                    break
                spans.append((p["chunk_id"], loc[0], loc[1]))
                cursor = loc[1]
            best_cid, best = None, None
            if not fallback and spans:
                best_cid, s0, e0 = max(spans, key=lambda t: (chunk_scores.get(t[0], 0.0),
                                                             -t[1]))
                best = (s0, e0)
            merged = f"{sec.title}\n{sec.text}" if sec.title else sec.text
            out.append({"id": sid, "chunk": best_cid or sid, "title": sec.title,
                        "doc": sec.doc_title,
                        "text": merged, "full": sec.text, "pieces": spans,
                        "best": best, "fallback": fallback or best is None,
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

        返回 [{"id"(节id), "title", "text"(节完整文本), "score"}]，按相关度降序；
        另带三个内部字段供调用方做窗口裁剪：full（不含标题前缀的节正文）、
        pieces（[(块id, 起, 止)] 在 full 里的区间）、best（最佳命中块区间）、
        fallback（区间定位失败）。last_search_mode 记录实际通道：
        hybrid / semantic / keyword。
        """
        self.last_search_mode = ""
        query = (query or "").strip()
        if not query or not self.sections:
            return []
        chunks = {c["chunk_id"]: c for c in self._index_chunks()}
        if not chunks:
            return []
        if condition and condition.strip():
            cond = condition.strip().lower()
            def _in_cond(c):
                sec = self.sections.get(c["section_id"])
                if sec is None:
                    return False
                # 节 id 前缀（=文件名 slug，如 abs_calib.NN）是天然的文档标识：
                # 手册首行常是「前言」这类通用词，标题里反而没有功能名
                return (cond in c["section_id"].lower()
                        or cond in (sec.title or "").lower()
                        or cond in (sec.doc_title or "").lower())
            scoped = {cid: c for cid, c in chunks.items() if _in_cond(c)}
            if scoped:
                chunks = scoped

        kw_scored = self._keyword_rank(query, chunks)
        # 标题通道（第三路）：节标题/文档标题命中查询词（整词+bigram）的节是强证据。
        # 用户措辞与节标题常有微差（"对开路面" vs 节标题"对开工况"），整词匹配不上，
        # bigram 命中能把同一主题的多处内容一起抬进结果。
        title_rank: Dict[str, int] = {}
        # 标题通道与关键词通道共用同一套分词：标识符按归一化全等/包含比，
        # 中文按整词+二元组。旧实现这里 lower() 而正文通道不折叠，同一查询
        # 在两条通道是两套「相同」的定义。
        _t_idents, _t_plain = self._split_query(query)

        def _title_score(sec: ManualSection) -> float:
            # 只匹配节标题：doc_title 对所有节相同，计入只会把「无命中」
            # 误抬成「全体命中」，且不提供任何节间区分度
            hay = self._fold_text(sec.title)
            if not hay:
                return 0.0
            idset = {self._norm_param(t) for t in self._iter_identifiers(hay)}
            sc = 0.0
            for t in _t_idents:
                sc += self._ident_score(t, idset)
            for t in _t_plain:
                sc += self._plain_score(t, hay)
            return sc

        _tsc = []
        for sid, sec in self.sections.items():
            if sid not in chunks:
                continue
            _s = _title_score(sec)
            if _s > 0:
                _tsc.append((_s, sid))
        _tsc.sort(key=lambda x: -x[0])
        title_rank = {sid: r for r, (_s, sid) in enumerate(_tsc)}
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
                        (qb, min(16, max(top_k * 3, 8)))).fetchall()
                for rowid, _dist in knn:
                    row = self._vec_db.execute(
                        "SELECT section_id FROM items WHERE id=?", (rowid,)).fetchone()
                    # sem 通道查的是全库向量：condition 过滤只作用于 chunks 字典，
                    # 这里必须把过滤掉的块剔掉，否则它们带着旧 id 进合并层直接 KeyError
                    if row and row[0] in chunks:
                        sem_order.append(row[0])

        scores: Dict[str, float] = {}
        for r, cid in enumerate(sem_order):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (60 + r)
        for r, (_s, cid) in enumerate(kw_scored):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (60 + r)
        for r, sid in enumerate(title_rank):
            b = 1.0 / (60 + r)
            for c in chunks.values():
                if c["section_id"] == sid:
                    scores[c["chunk_id"]] = scores.get(c["chunk_id"], 0.0) + b
        mode = "hybrid" if (sem_order and kw_scored) else "keyword"
        if not scores:
            return []
        # 选择按节去重：返回时同节会合并（small-to-big），若选择仍按块取 top-k，
        # 同节的第二块会挤掉另一个节的名额（实测把实车·对开工况挤到第 6 位）
        selected, seen_sec = [], set()
        for cid in sorted(scores, key=lambda c: -scores[c]):
            sid = chunks[cid]["section_id"]
            if sid in seen_sec:
                continue
            seen_sec.add(sid)
            selected.append((cid, scores[cid]))
            if len(selected) >= top_k:
                break
        chunk_scores = {cid: s for cid, s in selected}
        hits = self._merge_section_hits([cid for cid, _ in selected], chunks, chunk_scores)
        if hits:
            self.last_search_mode = mode
            return hits
        return []

    # ---------------- 参数索引（表格行结构化抽取，§11.4） ----------------
    @staticmethod
    def _fold_text(text: str) -> str:
        """检索用折叠：NFKC + 破折号族归一到 ASCII '-' + casefold。

        索引（块文本、标题）与查询两侧都走这一个函数，保证「同一个名字」只有
        一种定义。NFKC 负责全角字母/下划线与 NBSP，但它只把 U+2011 折成 U+2010，
        仍是独立字符，所以破折号还要显式统一。
        """
        return _DASH_RE.sub("-", unicodedata.normalize("NFKC", text or "")).casefold()

    @staticmethod
    def _iter_identifiers(text: str) -> List[str]:
        """从文本里抠出标识符候选（需含字母，长度 ≥2）。

        纯数字（26.87、100）不是标识符：它们做子串匹配会命中任何数值，
        是噪声。中文不会被 _IDENT_RE 匹配，所以中文关键词仍走二元组通道。
        """
        folded = KnowledgeBase._fold_text(text)
        return [t for t in _IDENT_RE.findall(folded)
                if len(t) >= 2 and any(ch.isalpha() for ch in t)]

    @staticmethod
    def _norm_param(name: str) -> str:
        """参数名归一化：大小写/下划线/连字符/空白统一，用于精确匹配。

        NFKC 先做兼容折叠：全角字母与下划线（Ｃｐ＿ＦＡ→cp_fa）、NBSP 与全角
        空格（→普通空格）都能对齐。**但 NFKC 不处理破折号族**——U+2011（Word
        导出的非断行连字符，真实手册常见）只折成 U+2010，仍是独立字符，
        所以连字符要按字符类一并吃掉，否则「手册里是 Opt‑Tbl2、用户打
        Opt-Tbl2」这种同名不同码的写法永远对不上。
        """
        folded = KnowledgeBase._fold_text(name)
        return re.sub(r"[\s_\-]+", "", folded)

    def _extract_param_rows(self) -> List[dict]:
        """从手册 md 表格逐行抽取结构化参数记录（§11.4 参数索引）。

        兼容两种表头形态：
        - 常规：表头行 + 分隔行 + 数据行；
        - Word 导出（真实手册实测）：空表头行 + 分隔行 + **加粗表头行** + 数据行
          —— 加粗行必须识别为表头，否则 key 取到序号数字、参数名查询全空。
        key 取该行首个"像标识符"的单元格（含字母、非纯数字/区间），归一化后
        作为精确匹配键；单元格剥掉 ** 加粗标记。
        """
        rows: List[dict] = []
        for sid, sec in self.sections.items():
            lines = sec.text.splitlines()
            i = 0
            while i < len(lines):
                stripped = lines[i].lstrip()
                if not stripped.startswith("|") or i + 1 >= len(lines)                         or not re.match(r"^\s*\|[\s:\-|]+\|\s*$", lines[i + 1]):
                    i += 1
                    continue
                headers = [self._clean_cell(c) for c in stripped.strip("|").split("|")]
                j = i + 2
                # Word 导出的加粗表头行：紧跟分隔行且单元格带 **，用它替换空/占位表头
                if j < len(lines) and lines[j].lstrip().startswith("|")                         and "**" in lines[j]:
                    bold = [self._clean_cell(c)
                            for c in lines[j].strip().strip("|").split("|")]
                    if any(bold) and not any(headers):
                        headers = bold
                    j += 1
                # Word 导出的非加粗真实表头行：空表头 + 分隔行 + 首格非纯数字的行
                # （如表5.9「参数名|参数含义|默认值|标定思路」）→ 升格为表头。
                # 不升格的后果：首列表头为空触发剥列逻辑，每行数据都丢参数名列
                if j < len(lines) and lines[j].lstrip().startswith("|")                         and not any(headers):
                    promo = [self._clean_cell(c)
                             for c in lines[j].strip().strip("|").split("|")]
                    if promo and promo[0] and not re.fullmatch(r"[\d.\-]+", promo[0]):
                        headers = promo
                        j += 1
                while j < len(lines) and lines[j].lstrip().startswith("|"):
                    cells = [self._clean_cell(c)
                             for c in lines[j].strip().strip("|").split("|")]
                    h, c = headers, cells
                    # 剥序号列：仅当表头明确写「序号」且首格是纯数字（空表头不能触发，
                    # 否则 Word 导出表格每行都丢第一列）
                    if h and h[0] in ("序号", "#", "No", "no") and c                             and re.fullmatch(r"\d+", c[0] or ""):
                        h, c = h[1:], c[1:]
                    # 识别「像标识符」的单元格：含字母、非纯数字/区间（判定在
                    # _ident_like_cell 里先做 NFKC 折叠）
                    key_c = next((x for x in c if self._ident_like_cell(x)), "")
                    if c and any(c) and self._norm_param(key_c):
                        # key 为空的行（纯数值/片段行）不进索引：空串是任何
                        # 查询的子串，会在 get_param 的部分匹配里污染全部结果
                        rows.append({"section_id": sid, "title": sec.title,
                                     "doc_title": sec.doc_title,
                                     "headers": h, "cells": c,
                                     "key": self._norm_param(key_c)})
                    j += 1
                i = j
        return rows

    @staticmethod
    def _clean_cell(c: str) -> str:
        """单元格清洗：剥 ** 加粗与首尾空白，其余（±/·/U+2011/方括号）原样保留。"""
        return (c or "").strip().strip("*").strip()

    @staticmethod
    def _ident_like_cell(x: str) -> bool:
        """这一格像不像参数标识符：含字母、且不是纯数字/区间。

        判定前先 NFKC 折叠：Word 导出可能给出全角参数名（Ｃｐ＿ＦＡ），不折叠
        时 [A-Za-z] 一个字母都匹配不到，整行进不了参数索引，get_param 永远
        查不到它——而 _norm_param 早就能把它和半角写法对齐，是判定这一步漏了。
        """
        f = unicodedata.normalize("NFKC", x or "")
        return bool(re.search(r"[A-Za-z]", f)) and not re.fullmatch(r"[\d.\s\[\]\-]+", f)

    def get_param(self, name: str, top_k: int = 5) -> List[dict]:
        """参数精确/归一化查询（P1 参数索引）。

        name 传参数名（如 Cp_FA、Opt_Instabil_Lambda_HA），归一化后精确匹配；
        精确无命中时降级**子串匹配**（双向：模型记不全参数名也能接住，命中标注
        `match: partial`）。

        不按工况过滤：同名参数在多个工况表里各有一行时**全部返回**，每条带
        `section`，由调用方按用户说的工况自行挑。早先有 condition 硬过滤，
        词序/空格/大小写任一不符就返回空列表，而工具层照样回 ok=true，模型
        于是断言「手册里没有这个参数」——参数名明明是对的。假阴性比多给几行
        坏得多：条件还可以凭 section 反推，空结果没有任何补救余地。

        返回 [{param, fields, section, section_id, doc_title, match}]。
        """
        key = self._norm_param(name)
        if not key:
            return []

        exact, partial = [], []
        for r in self._param_rows:
            if r["key"] == key:
                exact.append(r)
            elif key in r["key"] or (len(key) >= 3 and r["key"] in key):
                partial.append(r)
        # 精确命中按工况数返回（上限只防病态重复），部分匹配才受 top_k 约束
        rows = [(r, "exact") for r in exact[:PARAM_EXACT_MAX_ROWS]] or                [(r, "partial") for r in partial[:top_k]]
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
