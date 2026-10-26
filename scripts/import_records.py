#!/usr/bin/env python
"""把服务器上的试验文件登记进记录库（design.md §17）。

    python scripts/import_records.py --root /mnt/testdata [--db data/records.db] [--dry-run]

只登记「路径 + 编号 + 工况 + 车型」，不解析信号；指标在第一次被查询时才惰性
计算并缓存（§17.4）。目录约定：

    <根>/<车型代号>/<4-ABS|5-TCS>/<4.1.1_xxx.blf>

- 车型代号取第一级目录名（导入前由人工整理好，不猜）；
- 文件名必须是试验编号开头，认不出编号的文件跳过（大纲外的临时文件很常见）；
- 科室目录（4-ABS）与编号所属功能不一致时跳过并告警：这种多半是文件放错位置，
  静默入库会让后续对比拿错工况。
"""

import argparse
import json
import os
import re
import sys
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brake_analyzer.configs import AppConfigs, load_configs  # noqa: E402
from brake_analyzer.records import store  # noqa: E402

SUPPORTED_EXT = {".blf", ".mf4", ".mdf"}
_FOLDER_RE = re.compile(r"^\d+\s*[-_. ]\s*(.+)$")
_DATE_RE = re.compile(r"(20\d{6})")


def folder_function(parts: List[str], cfg: AppConfigs) -> Optional[str]:
    """从目录名猜功能（``4-ABS`` → abs）；认不出返回 None。

    只认中间层级：第一级是车型代号，最后一级是文件名。
    """
    for part in parts[1:-1]:
        m = _FOLDER_RE.match(part.strip())
        if not m:
            continue
        token = m.group(1).strip().casefold()
        for key, fn in cfg.functions.items():
            names = [key, fn.name] + list(fn.aliases)
            if any(token == str(n).casefold() for n in names):
                return key
            # "ABS制动" 这类目录名包含别名，也认
            if any(str(n).casefold() in token for n in names if len(str(n)) >= 3):
                return key
    return None


def recorded_at_from_name(name: str) -> Optional[str]:
    """文件名里的 20260801 → 2026-08-01；没有就留空，不拿 mtime 冒充试验日期。"""
    m = _DATE_RE.search(name)
    if not m:
        return None
    raw = m.group(1)
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"


def scan(root: str, cfg: AppConfigs) -> List[Dict[str, object]]:
    """纯扫描：返回每个文件的归类结果，不写库（dry-run 与测试共用）。"""
    items: List[Dict[str, object]] = []
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fname in sorted(filenames):
            if fname.startswith("."):
                continue
            path = os.path.join(dirpath, fname)
            rel = os.path.relpath(path, root)
            parts = rel.split(os.sep)
            item: Dict[str, object] = {
                "rel_path": rel.replace(os.sep, "/"),
                "file_path": path,
                "file_name": fname,
                "vehicle": parts[0] if len(parts) > 1 else "",
                "status": "skipped",
                "reason": "",
                "file_id": None,
                "condition_id": None,
                "recorded_at": recorded_at_from_name(fname),
            }
            ext = os.path.splitext(fname)[1].lower()
            if ext not in SUPPORTED_EXT:
                item["reason"] = "unsupported_type"
                items.append(item)
                continue
            file_id = cfg.match_file_id(fname)
            cond = cfg.condition_by_file_id(file_id) if file_id else None
            if file_id is None or cond is None:
                item["reason"] = "no_file_id"
                items.append(item)
                continue
            item.update(file_id=file_id, condition_id=cond.id)
            expected = folder_function(parts, cfg)
            if expected and expected != cond.function:
                item["reason"] = "folder_mismatch"
                item["expected_function"] = expected
                items.append(item)
                continue
            if not item["vehicle"]:
                item["reason"] = "no_vehicle_dir"
                items.append(item)
                continue
            item["status"] = "pending"
            items.append(item)
    return items


def run_import(root: str, db_path: str, cfg: AppConfigs,
               dry_run: bool = False) -> Dict[str, object]:
    items = scan(root, cfg)
    # 库文件常常就放在数据根目录下：它自己不是试验数据，别让它出现在清单里
    db_abs = os.path.abspath(db_path)
    items = [i for i in items
             if not (i["file_path"] == db_abs
                     or str(i["file_path"]).startswith(db_abs + "-"))]
    fingerprint = store.config_fingerprint(cfg.config_dir)
    counts = {"total": len(items), "imported": 0, "duplicated": 0,
              "skipped": 0, "failed": 0}
    skipped_reasons: Dict[str, int] = {}
    batch_id = None
    conn = None
    if not dry_run:
        conn = store.connect(db_path)
        batch_id = store.create_batch(conn, root=os.path.abspath(root),
                                      fingerprint=fingerprint)

    for item in items:
        if item["status"] != "pending":
            counts["skipped"] += 1
            reason = str(item["reason"])
            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
            continue
        try:
            stat = os.stat(str(item["file_path"]))
            digest = store.file_sha1(str(item["file_path"]))
            if dry_run:
                outcome = "inserted"
            else:
                outcome = store.upsert_record(
                    conn,
                    rel_path=str(item["rel_path"]),
                    file_path=str(item["file_path"]),
                    file_id=str(item["file_id"]),
                    condition_id=str(item["condition_id"]),
                    vehicle=str(item["vehicle"]),
                    file_size=stat.st_size,
                    file_sha1=digest,
                    file_mtime=stat.st_mtime,
                    recorded_at=item["recorded_at"],
                    batch_id=batch_id,
                )
            if outcome == "duplicated":
                counts["duplicated"] += 1
            else:
                counts["imported"] += 1
            item["outcome"] = outcome
        except OSError as e:
            counts["failed"] += 1
            item["status"] = "failed"
            item["reason"] = f"read_error: {e}"

    if conn is not None:
        store.finalize_batch(conn, batch_id, counts)
        conn.close()
    return {"counts": counts, "skipped_reasons": skipped_reasons,
            "items": items, "batch_id": batch_id, "dry_run": dry_run}


_REASON_TEXT = {
    "no_file_id": "文件名没有可识别的编号",
    "folder_mismatch": "目录功能与编号所属功能不一致",
    "no_vehicle_dir": "缺少车型目录（应为 <根>/<车型代号>/...）",
    "unsupported_type": "不支持的文件类型",
}


def print_report(result: Dict[str, object], limit: int = 20) -> None:
    counts = result["counts"]
    head = "[dry-run] 只统计，不写库" if result["dry_run"] else "扫描完成"
    print(f"{head}：共 {counts['total']} 个文件")
    verb = "将入库" if result["dry_run"] else "已入库"
    print(f"  {verb} {counts['imported']} / 重复 {counts['duplicated']} / "
          f"跳过 {counts['skipped']} / 失败 {counts['failed']}")
    reasons = result["skipped_reasons"]
    if reasons:
        detail = "，".join(f"{_REASON_TEXT.get(k, k)} {v}" for k, v in sorted(reasons.items()))
        print(f"  跳过原因：{detail}")
    pending = [i for i in result["items"] if i["status"] == "pending"]
    if pending:
        print(f"  入库明细（前 {min(limit, len(pending))} 条）：")
        for item in pending[:limit]:
            print(f"    {item['vehicle']} {item['file_id']} → {item['condition_id']}"
                  f"  {item['rel_path']}")
    bad = [i for i in result["items"] if i["status"] in ("skipped", "failed")]
    if bad:
        print(f"  未入库（前 {min(limit, len(bad))} 条）：")
        for item in bad[:limit]:
            extra = f"（目录指向 {item['expected_function']}）" if item.get("expected_function") else ""
            print(f"    [{_REASON_TEXT.get(str(item['reason']), item['reason'])}]"
                  f"{extra} {item['rel_path']}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="把试验文件登记进记录库")
    parser.add_argument("--root", required=True, help="数据根目录（其下按 车型/编号目录 组织）")
    parser.add_argument("--db", default="data/records.db", help="SQLite 路径")
    parser.add_argument("--configs", default="configs", help="配置目录")
    parser.add_argument("--dry-run", action="store_true", help="只打印清单，不写库")
    parser.add_argument("--json", action="store_true", help="输出机器可读结果")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.root):
        print(f"根目录不存在: {args.root}", file=sys.stderr)
        return 2
    cfg = load_configs(args.configs)
    result = run_import(args.root, args.db, cfg, dry_run=args.dry_run)
    if args.json:
        payload = {k: result[k] for k in ("counts", "skipped_reasons", "batch_id")}
        payload["items"] = [
            {k: v for k, v in item.items() if k != "file_path"}
            for item in result["items"]
            if item["status"] != "skipped" or item["reason"] != "unsupported_type"
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_report(result)
    return 0 if result["counts"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
