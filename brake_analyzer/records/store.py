"""试验记录库的 SQLite 存取层（design.md §17）。

只记录「编号 + 服务器文件路径 + 解析出的指标结论」，不存原始信号：
- record  ：一个文件一条，路径唯一；批次、车型、编号、工况都在这里
- analysis：一条记录的最新一次分析（带配置指纹，配置一改就失效重算）
- metric_value / verdict：按事件样本展开，供对话查询与横向对比

所有函数都接 ``sqlite3.Connection``，不做全局连接：Web 进程与导入脚本
各自管理生命周期。时间统一存 ISO-8601 字符串（UTC），排序即字典序。
"""

import glob
import hashlib
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import yaml

from ..schemas import AnalysisResult, decompose_metric_key

SCHEMA_VERSION = 1

# 分析状态：pending 还没算过；ok/error/file_gone 是三种终态
ANALYSIS_STATUSES = {"pending", "ok", "error", "file_gone"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_batch (
    batch_id           TEXT PRIMARY KEY,
    started_at         TEXT NOT NULL,
    root               TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    total_files        INTEGER NOT NULL DEFAULT 0,
    imported           INTEGER NOT NULL DEFAULT 0,
    duplicated         INTEGER NOT NULL DEFAULT 0,
    skipped            INTEGER NOT NULL DEFAULT 0,
    failed             INTEGER NOT NULL DEFAULT 0,
    note               TEXT
);

CREATE TABLE IF NOT EXISTS record (
    record_id       TEXT PRIMARY KEY,
    file_id         TEXT NOT NULL,
    condition_id    TEXT NOT NULL,
    vehicle         TEXT NOT NULL,
    rel_path        TEXT NOT NULL UNIQUE,
    file_path       TEXT NOT NULL,
    file_name       TEXT NOT NULL,
    file_size       INTEGER,
    file_sha1       TEXT,
    file_mtime      REAL,
    recorded_at     TEXT,
    batch_id        TEXT,
    imported_at     TEXT NOT NULL,
    analysis_status TEXT NOT NULL DEFAULT 'pending',
    last_error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_record_file_id      ON record(file_id);
CREATE INDEX IF NOT EXISTS idx_record_condition_id ON record(condition_id);
CREATE INDEX IF NOT EXISTS idx_record_vehicle      ON record(vehicle);
CREATE INDEX IF NOT EXISTS idx_record_sha1         ON record(file_sha1);

CREATE TABLE IF NOT EXISTS analysis (
    record_id          TEXT PRIMARY KEY,
    analysis_id        TEXT,
    config_fingerprint TEXT NOT NULL,
    analyzed_at        TEXT NOT NULL,
    sample_count       INTEGER NOT NULL DEFAULT 0,
    degraded           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS metric_value (
    record_id      TEXT NOT NULL,
    sample_index   INTEGER NOT NULL,
    metric_key     TEXT NOT NULL,
    name           TEXT,
    value          REAL,
    unit           TEXT,
    status         TEXT,
    missing_reason TEXT,
    ts_start       REAL,
    ts_end         REAL,
    PRIMARY KEY (record_id, sample_index, metric_key)
);
CREATE INDEX IF NOT EXISTS idx_metric_value_key ON metric_value(metric_key);

CREATE TABLE IF NOT EXISTS verdict (
    record_id    TEXT NOT NULL,
    sample_index INTEGER NOT NULL,
    rule_id      TEXT NOT NULL,
    metric_key   TEXT,
    status       TEXT,
    severity     TEXT,
    fault_domain TEXT,
    message      TEXT,
    PRIMARY KEY (record_id, sample_index, rule_id)
);
"""

MIGRATIONS: Dict[int, List[str]] = {}


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: str) -> sqlite3.Connection:
    """打开（必要时新建）数据库；行可按列名取值。"""
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    cur = conn.execute("SELECT value FROM meta WHERE key='schema_version'")
    row = cur.fetchone()
    version = int(row["value"]) if row else 0
    if version == 0:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    elif version < SCHEMA_VERSION:
        for v in range(version + 1, SCHEMA_VERSION + 1):
            for stmt in MIGRATIONS.get(v, []):
                conn.execute(stmt)
        conn.execute("UPDATE meta SET value=? WHERE key='schema_version'",
                     (str(SCHEMA_VERSION),))
    conn.commit()


# ---------------------------------------------------------------- 指纹/哈希

def _hash_file(h, path: str, chunk_size: int = 1 << 20) -> None:
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)


def _dbc_refs(config_dir: str) -> List[tuple]:
    """signals_blf.yaml 里引用的 DBC：("file", 绝对路径) 或 ("absent", 原始写法)。

    现场库常以 ${ENV} 指向仓外路径；未提供时把 "absent" 也写进指纹，库装上
    那次分析才会重算，而不是沿用没有该总线信号时的旧结论。
    """
    # 局部导入：configs 会反过来引 records.naming，模块级互相 import 容易踩顺序坑
    from ..configs import _expand_env, _resolve_config_path

    path = os.path.join(config_dir, "signals_blf.yaml")
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return []
    buses = raw.get("buses") if isinstance(raw, dict) else None
    if not isinstance(buses, dict):
        return []
    out: List[tuple] = []
    for info in buses.values():
        if not isinstance(info, dict):
            continue
        dbcs = info.get("dbcs")
        if not isinstance(dbcs, dict):
            continue
        for alias, item in dbcs.items():
            if not isinstance(item, str) or not item.strip():
                continue
            item = item.strip()
            expanded = _expand_env(item)
            if expanded is None:
                out.append(("absent", f"{alias}={item}"))
                continue
            real = _resolve_config_path(expanded, config_dir)
            out.append(("file", real) if os.path.exists(real) else ("absent", f"{alias}={item}"))
    return out


def config_fingerprint(config_dir: str) -> str:
    """配置指纹：yaml 与 DBC 内容一变就换指纹，缓存随之失效。

    只算配置不算代码：改指标实现而配置不变的场景，用户可以删库重导或
    手动把 analysis 行删掉，比每次启动强制重算更可控。

    DBC 必须计入：它决定信号怎么解码，只哈希 yaml 会出现「配置没动、
    曲线却该变」的静默陈旧，尤其是同一总线挂多份 DBC 之后。
    """
    h = hashlib.sha1()
    if not os.path.isdir(config_dir):
        return ""
    for name in sorted(os.listdir(config_dir)):
        if not name.endswith((".yaml", ".yml")):
            continue
        path = os.path.join(config_dir, name)
        if not os.path.isfile(path):
            continue
        h.update(name.encode("utf-8"))
        _hash_file(h, path)

    # 1) configs 目录下所有 DBC（提交入库的那部分，含子目录）
    root = os.path.abspath(config_dir)
    for path in sorted(glob.glob(os.path.join(root, "**", "*.dbc"), recursive=True)):
        h.update(b"#dbc:")
        h.update(os.path.relpath(path, root).encode("utf-8"))
        _hash_file(h, path)

    # 2) signals_blf.yaml 里指向仓外的库（现场 ${ENV} 路径）
    for kind, value in _dbc_refs(config_dir):
        if kind == "file" and os.path.abspath(value).startswith(root + os.sep):
            continue                    # 已在 1) 里算过
        h.update(f"#dbc-ref:{kind}:{value}".encode("utf-8"))
        if kind == "file":
            _hash_file(h, value)
    return h.hexdigest()[:16]


def file_sha1(path: str, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def record_id_for(rel_path: str) -> str:
    """按仓库内相对路径生成稳定 id：文件挪出根目录也不影响同一记录的身份。"""
    norm = rel_path.replace(os.sep, "/")
    return "rec_" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- 导入

def create_batch(conn: sqlite3.Connection, *, root: str, fingerprint: str,
                 batch_id: Optional[str] = None) -> str:
    batch_id = batch_id or "bat_" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    conn.execute(
        "INSERT OR REPLACE INTO import_batch(batch_id, started_at, root, config_fingerprint)"
        " VALUES(?,?,?,?)",
        (batch_id, utcnow(), root, fingerprint),
    )
    conn.commit()
    return batch_id


def finalize_batch(conn: sqlite3.Connection, batch_id: str, counts: Dict[str, int],
                   note: str = "") -> None:
    conn.execute(
        "UPDATE import_batch SET total_files=?, imported=?, duplicated=?, skipped=?,"
        " failed=?, note=? WHERE batch_id=?",
        (counts.get("total", 0), counts.get("imported", 0), counts.get("duplicated", 0),
         counts.get("skipped", 0), counts.get("failed", 0), note or None, batch_id),
    )
    conn.commit()


def find_by_path(conn: sqlite3.Connection, rel_path: str) -> Optional[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM record WHERE rel_path=?", (rel_path,))
    return cur.fetchone()


def upsert_record(
    conn: sqlite3.Connection,
    *,
    rel_path: str,
    file_path: str,
    file_id: str,
    condition_id: str,
    vehicle: str,
    file_size: Optional[int] = None,
    file_sha1: Optional[str] = None,
    file_mtime: Optional[float] = None,
    recorded_at: Optional[str] = None,
    batch_id: Optional[str] = None,
) -> str:
    """插入或更新一条记录；内容（sha1/大小/mtime）没变时不动分析结果。

    路径是身份：同一路径重导只更新元数据。sha1 变了说明文件被替换，
    旧分析结论必须作废，否则库里会长期留着已经不成立的数值。
    """
    existing = find_by_path(conn, rel_path)
    now = utcnow()
    if existing is None:
        conn.execute(
            "INSERT INTO record(record_id, file_id, condition_id, vehicle, rel_path,"
            " file_path, file_name, file_size, file_sha1, file_mtime, recorded_at,"
            " batch_id, imported_at, analysis_status)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'pending')",
            (record_id_for(rel_path), file_id, condition_id, vehicle, rel_path,
             file_path, os.path.basename(rel_path), file_size, file_sha1, file_mtime,
             recorded_at, batch_id, now),
        )
        conn.commit()
        return "inserted"
    changed = (
        existing["file_sha1"] != file_sha1
        or existing["file_size"] != file_size
        or existing["file_mtime"] != file_mtime
        or existing["condition_id"] != condition_id
    )
    conn.execute(
        "UPDATE record SET file_id=?, condition_id=?, vehicle=?, file_path=?,"
        " file_size=?, file_sha1=?, file_mtime=?, recorded_at=?, batch_id=?,"
        " analysis_status=?, last_error=? WHERE record_id=?",
        (file_id, condition_id, vehicle, file_path, file_size, file_sha1, file_mtime,
         recorded_at, batch_id, "pending" if changed else existing["analysis_status"],
         None if changed else existing["last_error"], existing["record_id"]),
    )
    if changed:
        conn.execute("DELETE FROM analysis WHERE record_id=?", (existing["record_id"],))
        conn.execute("DELETE FROM metric_value WHERE record_id=?", (existing["record_id"],))
        conn.execute("DELETE FROM verdict WHERE record_id=?", (existing["record_id"],))
    conn.commit()
    return "replaced" if changed else "duplicated"


# ---------------------------------------------------------------- 分析结果

def save_analysis(conn: sqlite3.Connection, record_id: str,
                  result: AnalysisResult, fingerprint: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO analysis(record_id, analysis_id, config_fingerprint,"
        " analyzed_at, sample_count, degraded) VALUES(?,?,?,?,?,?)",
        (record_id, result.analysis_id, fingerprint, utcnow(),
         len(result.samples), 1 if result.degraded else 0),
    )
    conn.execute("DELETE FROM metric_value WHERE record_id=?", (record_id,))
    conn.execute("DELETE FROM verdict WHERE record_id=?", (record_id,))
    for i, sample in enumerate(result.samples):
        for m in sample.metrics:
            _, metric_key = decompose_metric_key(m.key)
            ts = m.ts_range or (None, None)
            conn.execute(
                "INSERT OR REPLACE INTO metric_value(record_id, sample_index, metric_key,"
                " name, value, unit, status, missing_reason, ts_start, ts_end)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (record_id, i, metric_key, m.name, m.value, m.unit, m.status,
                 m.missing_reason, ts[0], ts[1]),
            )
        for v in sample.verdicts:
            _, metric_key = decompose_metric_key(v.metric_key)
            conn.execute(
                "INSERT OR REPLACE INTO verdict(record_id, sample_index, rule_id,"
                " metric_key, status, severity, fault_domain, message)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (record_id, i, v.rule_id, metric_key, v.status, v.severity,
                 v.fault_domain, v.message),
            )
    conn.execute(
        "UPDATE record SET analysis_status='ok', last_error=NULL WHERE record_id=?",
        (record_id,),
    )
    conn.commit()


def mark_analysis_error(conn: sqlite3.Connection, record_id: str, status: str,
                        error: str) -> None:
    if status not in ANALYSIS_STATUSES:
        raise ValueError(f"未知分析状态: {status}")
    conn.execute(
        "UPDATE record SET analysis_status=?, last_error=? WHERE record_id=?",
        (status, error[:500], record_id),
    )
    conn.commit()


def get_analysis(conn: sqlite3.Connection, record_id: str,
                 fingerprint: Optional[str] = None) -> Optional[sqlite3.Row]:
    """取缓存；给了指纹且不一致时视为未分析（由调用方重算）。"""
    row = conn.execute("SELECT * FROM analysis WHERE record_id=?",
                       (record_id,)).fetchone()
    if row is None:
        return None
    if fingerprint is not None and row["config_fingerprint"] != fingerprint:
        return None
    return row


def get_record(conn: sqlite3.Connection, record_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM record WHERE record_id=?",
                        (record_id,)).fetchone()


def metric_rows(conn: sqlite3.Connection, record_id: str) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM metric_value WHERE record_id=? ORDER BY sample_index, metric_key",
        (record_id,),
    ).fetchall()


def sample_windows(conn: sqlite3.Connection, record_id: str) -> List[dict]:
    """每个事件样本的切窗区间（图上的高亮区）。

    指标行里存了 ts_range（= 窗口起止），按 sample_index 取并集即可——
    同一窗口内的所有指标 ts_range 相同，退化窗口没有值。
    """
    rows = conn.execute(
        "SELECT sample_index, MIN(ts_start) AS t_start, MAX(ts_end) AS t_end"
        " FROM metric_value WHERE record_id=? AND ts_start IS NOT NULL"
        " GROUP BY sample_index ORDER BY sample_index",
        (record_id,),
    ).fetchall()
    return [{"sample_index": r["sample_index"], "t_start": r["t_start"],
             "t_end": r["t_end"]} for r in rows]


def verdict_rows(conn: sqlite3.Connection, record_id: str) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM verdict WHERE record_id=? ORDER BY sample_index, rule_id",
        (record_id,),
    ).fetchall()


def query_records(
    conn: sqlite3.Connection,
    *,
    file_id: Optional[str] = None,
    condition_id: Optional[str] = None,
    condition_ids: Optional[List[str]] = None,
    vehicle: Optional[str] = None,
    limit: int = 200,
) -> List[sqlite3.Row]:
    """按编号/工况/车型过滤；功能维度的工况集合由调用方从配置算好传进来。"""
    where: List[str] = []
    args: List[Any] = []
    if file_id:
        where.append("file_id=?")
        args.append(file_id)
    if condition_id:
        where.append("condition_id=?")
        args.append(condition_id)
    if condition_ids is not None:
        if not condition_ids:
            return []
        where.append("condition_id IN (%s)" % ",".join("?" * len(condition_ids)))
        args.extend(condition_ids)
    if vehicle:
        where.append("vehicle=?")
        args.append(vehicle)
    sql = "SELECT * FROM record"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY vehicle, condition_id, file_name LIMIT ?"
    args.append(limit)
    return conn.execute(sql, args).fetchall()


def status_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute(
        "SELECT analysis_status, COUNT(*) AS n FROM record GROUP BY analysis_status"
    ).fetchall()
    return {r["analysis_status"]: r["n"] for r in rows}


def distinct(conn: sqlite3.Connection, column: str) -> List[str]:
    if column not in ("vehicle", "condition_id", "file_id"):
        raise ValueError(f"不支持的列: {column}")
    rows = conn.execute(
        f"SELECT DISTINCT {column} FROM record ORDER BY {column}").fetchall()
    return [r[0] for r in rows]


def iter_pending(conn: sqlite3.Connection, fingerprint: str) -> Iterable[sqlite3.Row]:
    """待分析：没算过、算失败过，或指纹过期。

    file_gone 不算待办：文件已不在服务器上，重复尝试只会把日志刷满；
    重新导入同一路径会自动把状态改回 pending。
    """
    return conn.execute(
        "SELECT r.* FROM record r LEFT JOIN analysis a ON a.record_id=r.record_id"
        " WHERE r.analysis_status<>'file_gone'"
        " AND (a.record_id IS NULL OR a.config_fingerprint<>? OR r.analysis_status='error')"
        " ORDER BY r.vehicle, r.condition_id",
        (fingerprint,),
    ).fetchall()
