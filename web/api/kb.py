"""知识库端点：浏览/检索/上传/删除（design.md §11）。"""

import os
import re

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from typing import List

from web import state as st
from web.state import KB, KB_DIR

router = APIRouter()

KB_MANUAL_DIR = os.path.join(KB_DIR, "manual")
KB_ARCHIVE_DIR = os.path.join(KB_DIR, "archive")
KB_ALLOWED_EXT = {".md", ".docx"}
MAX_KB_BYTES = 50 * 1024 * 1024


def _kb_safe_name(name: str) -> str:
    """落盘文件名：只要 basename，防路径穿越；空结果给默认名。"""
    base = os.path.basename(name or "").strip()
    base = re.sub(r"[\\/:*?\"<>|]", "_", base)
    return base or "manual.md"


@router.get("/api/kb")
def api_kb():
    return st.KB.browse()


@router.get("/api/kb/search")
def api_kb_search(q: str = Query(...), top_k: int = Query(3)):
    """检索调试：一条查询命中哪些节、走了哪条通道（semantic=向量 / keyword=兜底）。

    mode=keyword 且配置了 embedding 时，通常是 embedding 端点没起——去 LM Studio 看。
    """
    hits = st.KB.search(q, top_k=min(max(top_k, 1), 10))
    return {"query": q, "mode": st.KB.last_search_mode,
            "rag_enabled": st.KB._embed_fn is not None, "hits": hits}


@router.post("/api/kb/manual")
async def api_kb_upload(files: List[UploadFile] = File(...)):
    """上传手册：.md 直存，.docx 走摄取层转 md（原件存 knowledge/archive/ 留底）。

    文件不在部署机上也没关系——浏览器在哪台机器，就从哪台上传。
    上传前对比 reload 前后的节集合：贡献 0 节的文件拒绝（防止"死文件"）。
    """
    os.makedirs(KB_MANUAL_DIR, exist_ok=True)
    os.makedirs(KB_ARCHIVE_DIR, exist_ok=True)
    out = []
    for uf in files:
        name = _kb_safe_name(uf.filename or "manual.md")
        ext = os.path.splitext(name)[1].lower()
        if ext not in KB_ALLOWED_EXT:
            raise HTTPException(400, f"仅接受 {'/'.join(sorted(KB_ALLOWED_EXT))}，收到: {name}")
        content = await uf.read()
        if len(content) > MAX_KB_BYTES:
            raise HTTPException(400, f"文件超过 {MAX_KB_BYTES // 1048576}MB 上限: {name}")

        if ext == ".docx":
            tmp = os.path.join(KB_ARCHIVE_DIR, name)
            with open(tmp, "wb") as f:
                f.write(content)
            try:
                from markitdown import MarkItDown

                md_text = MarkItDown().convert(tmp).text_content
            except Exception as e:
                # 转换失败不留坏原件占着 archive：删掉再报
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise HTTPException(400, f"{name}: markitdown 转换失败 "
                                         f"{type(e).__name__}: {str(e)[:200]}")
            # 图片 → 占位说明：markitdown 把图转成 base64 内嵌，直接进块会撑爆块预算
            n_img = len(re.findall(r"!\[[^\]]*\]\([^)]*\)", md_text))
            if n_img:
                md_text = re.sub(r"!\[[^\]]*\]\([^)]*\)",
                                 "> [图片占位]（原文档含图，见 knowledge/archive/）", md_text)
            if not re.search(r"^#{1,3}\s+\S", md_text, re.M):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise HTTPException(400, f"{name}: 转换结果没有任何标题（# ~ ###），"
                                         "无法切出可检索的节——确认文档使用了标题样式")
        else:
            # 编码探测：UTF-8 优先；失败时用 chardet 识别（GBK 编辑器存的中文
            # md 很常见），都不行才拒绝——errors="replace" 会把整篇中文静默
            # 变成乱码入库，检索到的是乱码且无任何报错
            try:
                md_text = content.decode("utf-8-sig")
            except UnicodeDecodeError:
                import chardet

                enc = (chardet.detect(content[:65536]) or {}).get("encoding") or ""
                try:
                    md_text = content.decode(enc)
                except (UnicodeDecodeError, LookupError):
                    raise HTTPException(
                        400, f"{name}: 无法识别文件编码（尝试过 UTF-8/{enc}），"
                             "请另存为 UTF-8 后重新上传")
            if not re.search(r"^#{1,3}\s+\S", md_text, re.M):
                raise HTTPException(400, f"{name}: 文件没有任何标题（# ~ ###），"
                                         "切不出可检索的节")

        md_name = os.path.splitext(name)[0] + ".md"
        overwritten = os.path.exists(os.path.join(KB_MANUAL_DIR, md_name))
        before = {sid: (s.title, s.text) for sid, s in st.KB.sections.items()}
        with open(os.path.join(KB_MANUAL_DIR, md_name), "w", encoding="utf-8") as f:
            f.write(md_text)
        st.KB.reload()
        # 回执节对照（⑤）：reload 前后新增或内容变化的节——docx/md 统一口径，
        # 用户据此确认切分与 id 是否符合预期（规则 kb_ref 将来要引用这些 id）
        changed = [{"id": sid, "title": sec.title}
                   for sid, sec in st.KB.sections.items()
                   if before.get(sid) != (sec.title, sec.text)]
        out.append({
            "file": md_name,
            "source": "docx" if ext == ".docx" else "md",
            "overwritten": overwritten,
            "sections": changed,
            "section_count": len(changed),
            "kb_sections_total": len(st.KB.sections),
        })
    return {"files": out}


@router.delete("/api/kb/manual/{name}")
def api_kb_delete(name: str):
    stem = os.path.splitext(_kb_safe_name(name))[0]
    md_name = stem + ".md"
    path = os.path.join(KB_MANUAL_DIR, md_name)
    if not os.path.isfile(path):
        raise HTTPException(404, f"手册不存在: {md_name}")
    os.remove(path)
    # 上传时留底的原件一并清掉，否则删除后 archive 里留着不可见的孤儿文件
    for ext in (".docx", ".md"):
        orig = os.path.join(KB_ARCHIVE_DIR, stem + ext)
        if os.path.isfile(orig):
            os.remove(orig)
    st.KB.reload()
    return {"deleted": md_name, "kb_sections_total": len(st.KB.sections)}
