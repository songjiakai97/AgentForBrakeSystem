"""知识库读取与检索（design.md §11.4）。

- 手册：按 `### <section_id>` 标题切分，kb_ref → 原文段。
- 案例：index.yaml 过滤 + 关键词打分 top-k。
demo 不启用向量检索。
"""

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

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


class KnowledgeBase:
    def __init__(self, kb_dir: str = "knowledge"):
        self.kb_dir = kb_dir
        self.sections: Dict[str, ManualSection] = {}
        self.cases: List[CaseEntry] = []
        self._load_manual()
        self._load_cases()

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

    # ---------------- 浏览 ----------------
    def browse(self) -> dict:
        by_fn: Dict[str, list] = {}
        for sid, sec in self.sections.items():
            by_fn.setdefault(sid.split(".", 1)[0], []).append(
                {"section_id": sid, "title": sec.title, "preview": sec.text[:80]}
            )
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
