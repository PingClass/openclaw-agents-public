#!/usr/bin/env python3
"""把 .docx 轉成 Markdown，給整理投影片大綱用。

用法：
  python3 docx2md.py INPUT.docx [--images-dir DIR]

Markdown 印到 stdout。保留標題層級、條列、表格；有 --images-dir 時把文件內的
圖片存出來，並在原位置留下 ![](路徑)，之後可以直接放進大綱的 image 欄位。
"""
import argparse
import os
import sys

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph


def iter_blocks(doc):
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield Table(child, doc)


def heading_level(p):
    name = (p.style.name or "").lower() if p.style is not None else ""
    if name in ("title", "標題"):
        return 1
    # 文件標題佔掉 #，Word 的「標題 1」從 ## 開始
    for prefix in ("heading ", "標題 "):
        if name.startswith(prefix):
            try:
                return min(int(name[len(prefix):]) + 1, 6)
            except ValueError:
                return None
    return None


def list_depth(p):
    ppr = p._p.pPr
    if ppr is not None and ppr.numPr is not None:
        ilvl = ppr.numPr.ilvl
        return int(ilvl.val) if ilvl is not None else 0
    name = (p.style.name or "").lower() if p.style is not None else ""
    if "list" in name or "清單" in name:
        return 0
    return None


def run_text(p):
    parts = []
    for run in p.runs:
        t = run.text
        if not t:
            continue
        if run.bold and t.strip():
            t = f"**{t.strip()}**"
        parts.append(t)
    return "".join(parts).strip()


def save_images(p, doc, images_dir, counter):
    paths = []
    for blip in p._p.iter(qn("a:blip")):
        rid = blip.get(qn("r:embed"))
        part = doc.part.related_parts.get(rid) if rid else None
        if part is None:
            continue
        counter[0] += 1
        ext = os.path.splitext(part.partname)[1] or ".png"
        path = os.path.join(images_dir, f"image{counter[0]:02d}{ext}")
        with open(path, "wb") as f:
            f.write(part.blob)
        paths.append(os.path.abspath(path))
    return paths


def table_md(table):
    rows = [[c.text.strip().replace("\n", " ").replace("|", "\\|") for c in r.cells]
            for r in table.rows]
    if not rows:
        return ""
    out = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * len(rows[0])]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("docx")
    ap.add_argument("--images-dir", help="把文件內的圖片存到這個資料夾")
    args = ap.parse_args()

    try:
        doc = Document(args.docx)
    except Exception as e:  # python-docx 對壞檔會丟各種例外
        print(f"❌ 讀不到 docx：{e}", file=sys.stderr)
        return 2
    if args.images_dir:
        os.makedirs(args.images_dir, exist_ok=True)

    lines, counter = [], [0]
    for block in iter_blocks(doc):
        if isinstance(block, Table):
            lines += ["", table_md(block), ""]
            continue
        if args.images_dir:
            for path in save_images(block, doc, args.images_dir, counter):
                lines += ["", f"![]({path})", ""]
        text = run_text(block)
        if not text:
            continue
        level = heading_level(block)
        depth = list_depth(block)
        if level:
            lines += ["", "#" * level + " " + text.replace("**", ""), ""]
        elif depth is not None:
            lines.append("  " * depth + "- " + text)
        else:
            lines += ["", text, ""]

    md = "\n".join(lines)
    while "\n\n\n" in md:
        md = md.replace("\n\n\n", "\n\n")
    print(md.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
