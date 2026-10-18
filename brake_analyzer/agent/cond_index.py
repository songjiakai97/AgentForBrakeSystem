"""工况语义索引（resolve_condition 的语义召回层）。

唯一键精确匹配是第一优先（结构化、零幻觉）；本索引只在唯一键**零命中**时
提供语义候选——用户口语（"雨天地板油"）与配置词汇（"湿玄武岩 60kph 全力制动"）
之间的措辞鸿沟由嵌入相似度弥合。

- 向量存 knowledge/vectors.db（与手册检索同一文件，cond_ 前缀隔离）；
- 内容 = 功能名+别名 + 工况名/id/maneuver/surface_code，hash 增量同步；
- 向量归一化后入库：L2 距离可换算为余弦（sim = 1 − d²/2），阈值跨模型可移植；
- 自动确认 vs 交用户的决策阈值见 tools.py 的 AUTO_CONFIRM_* 常量。
"""

import hashlib
import re
import threading

import numpy as np


def _table(model: str) -> str:
    return "vec_cond_" + re.sub(r"\W+", "_", model)


class ConditionIndex:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._embed_fn = None
        self._embed_model = ""
        self._db = None
        self._lock = threading.Lock()
        self.available = False

    def set_embedder(self, fn, model: str) -> bool:
        """注入向量化回调并打开存储；sqlite-vec 不可用 → available=False（回退纯唯一键）。"""
        self._embed_fn, self._embed_model = fn, model
        try:
            import sqlite3
            import sqlite_vec

            db = sqlite3.connect(self.db_path, check_same_thread=False)
            db.enable_load_extension(True)
            sqlite_vec.load(db)
            db.enable_load_extension(False)
            db.execute(
                "CREATE TABLE IF NOT EXISTS cond_items("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "model TEXT NOT NULL, cond_id TEXT NOT NULL, "
                "content_hash TEXT NOT NULL, UNIQUE(model, cond_id))")
            self._db = db
            self._lock = threading.Lock()
            self.available = True
        except Exception:
            self._db = None
            self.available = False
        return self.available

    @staticmethod
    def unit_text(cfg, cond) -> str:
        fn = cfg.function(cond.function)
        return " ".join(filter(None, [
            fn.name if fn else "",
            " ".join(fn.aliases) if fn else "",
            cond.name, cond.id, cond.maneuver, cond.surface_code,
        ]))

    def sync(self, cfg) -> bool:
        """把全部工况增量同步进向量库（配置变了才重算对应条目）。"""
        if not self.available or not self._embed_fn:
            return False
        units = [(c.id,
                  hashlib.sha256(f"{self._embed_model}|{self.unit_text(cfg, c)}".encode("utf-8")).hexdigest()[:16],
                  self.unit_text(cfg, c))
                 for c in cfg.conditions.values()]
        with self._lock:
            old = dict(self._db.execute(
                "SELECT cond_id, content_hash FROM cond_items WHERE model=?",
                (self._embed_model,)).fetchall())
            changed = [u for u in units if old.get(u[0]) != u[1]]
            gone = [cid for cid in old if cid not in {u[0] for u in units}]
            table = _table(self._embed_model)
            if changed:
                vecs = self._embed_fn([u[2] for u in changed])
                if not vecs:
                    return False
                mat = np.asarray(vecs, dtype=np.float32)
                mat = mat / np.maximum(np.linalg.norm(mat, axis=1, keepdims=True), 1e-9)
                self._db.execute(
                    f'CREATE VIRTUAL TABLE IF NOT EXISTS "{table}" '
                    f"USING vec0(embedding float[{mat.shape[1]}])")
                for (cid, h, _), v in zip(changed, mat):
                    rid = self._db.execute(
                        "SELECT id FROM cond_items WHERE model=? AND cond_id=?",
                        (self._embed_model, cid)).fetchone()
                    if rid:
                        self._db.execute(f'DELETE FROM "{table}" WHERE rowid=?', (rid[0],))
                        self._db.execute("DELETE FROM cond_items WHERE id=?", (rid[0],))
                    cur = self._db.execute(
                        "INSERT INTO cond_items(model, cond_id, content_hash) VALUES(?,?,?)",
                        (self._embed_model, cid, h))
                    self._db.execute(
                        f'INSERT INTO "{table}"(rowid, embedding) VALUES(?,?)',
                        (cur.lastrowid, v.tobytes()))
            for cid in gone:
                rid = self._db.execute(
                    "SELECT id FROM cond_items WHERE model=? AND cond_id=?",
                    (self._embed_model, cid)).fetchone()
                if rid:
                    self._db.execute(f'DELETE FROM "{table}" WHERE rowid=?', (rid[0],))
                    self._db.execute("DELETE FROM cond_items WHERE id=?", (rid[0],))
            self._db.commit()
            return True

    def search(self, query: str, top_k: int = 3) -> list:
        """语义最近邻；返回 [{cond_id, distance, similarity}]，相似度=归一化余弦。"""
        if not self.available or not self._embed_fn:
            return []
        qv = self._embed_fn([query])
        if not qv:
            return []
        v = np.asarray(qv[0], dtype=np.float32)
        v = v / max(float(np.linalg.norm(v)), 1e-9)
        table = _table(self._embed_model)
        with self._lock:
            try:
                knn = self._db.execute(
                    f'SELECT rowid, distance FROM "{table}" '
                    "WHERE embedding MATCH ? AND k=? ORDER BY distance",
                    (v.tobytes(), top_k)).fetchall()
            except Exception:
                return []
        out = []
        for rid, dist in knn:
            row = self._db.execute(
                "SELECT cond_id FROM cond_items WHERE id=?", (rid,)).fetchone()
            if row:
                out.append({"cond_id": row[0], "distance": float(dist),
                            "similarity": round(1.0 - float(dist) ** 2 / 2.0, 3)})
        return out
