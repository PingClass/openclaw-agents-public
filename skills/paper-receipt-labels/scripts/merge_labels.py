#!/usr/bin/env python3
"""紙本發票寄送標籤：讀 Excel（或 CSV），用 Word 模板合併列印，輸出 docx、PDF 和 ZIP。

用法：
  python3 merge_labels.py 發票寄送紀錄.xlsx --out-dir /path/to/output
  python3 merge_labels.py 發票寄送紀錄.xlsx --out-dir /path/to/output --no-pdf

必要欄位（第一列是標題）：訂單編號、發票號碼、收件人姓名、收件人地址

exit code：
  0  全部完成
  2  輸入有錯（檔案讀不到、缺欄位、沒有任何有效資料）
  3  找不到 LibreOffice，只產出 docx（ZIP 裡也只有 docx）
  4  PDF 轉檔失敗，只產出 docx（ZIP 裡也只有 docx）
"""
import argparse
import csv
import datetime as dt
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REQUIRED_COLUMNS = ["訂單編號", "發票號碼", "收件人姓名", "收件人地址"]
LABELS_PER_PAGE = 16  # A4 一頁 2 欄 × 8 列
DEFAULT_TEMPLATE = Path(__file__).resolve().parent.parent / "assets" / "paper_receipt_template.docx"

# 模板用新細明體，Linux 容器裡沒有，轉 PDF 時對應到 Noto CJK
LINUX_FONT_ALIASES = {
    "新細明體": "Noto Sans CJK TC",
    "PMingLiU": "Noto Sans CJK TC",
    "細明體": "Noto Sans CJK TC",
    "MingLiU": "Noto Sans CJK TC",
}


class InputError(Exception):
    pass


# ---------------------------------------------------------------- 讀資料

def cell_to_str(value):
    """Excel 儲存格轉字串：整數不帶 .0，日期去掉 00:00:00，前後空白去掉。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, dt.datetime) and value.time() == dt.time(0, 0):
        return value.date().isoformat()
    return str(value).strip()


def read_rows(path):
    """回傳 (標題列, [(Excel 列號, [值...]), ...])，值都已轉成字串。"""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with open(path, encoding="utf-8-sig", newline="") as f:
            raw = list(csv.reader(f))
    elif suffix in (".xlsx", ".xlsm"):
        import openpyxl
        try:
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        except Exception as e:
            raise InputError(f"Excel 讀取失敗：{e}")
        ws = wb.worksheets[0]
        raw = [list(r) for r in ws.iter_rows(values_only=True)]
        wb.close()
    elif suffix == ".xls":
        raise InputError("不支援舊版 .xls，請用 Excel 另存成 .xlsx 再試一次")
    else:
        raise InputError(f"不支援的檔案類型：{suffix}（只收 .xlsx、.xlsm、.csv）")

    rows = [(i + 1, [cell_to_str(v) for v in r]) for i, r in enumerate(raw)]
    rows = [(n, r) for n, r in rows if any(r)]
    if not rows:
        raise InputError("檔案是空的")
    _, header = rows[0]
    return header, rows[1:]


def build_records(header, rows):
    """挑出必要欄位。回傳 (records, skipped)，skipped 是 [(列號, 缺的欄位)]。"""
    index = {}
    for i, name in enumerate(header):
        name = name.strip()
        if name and name not in index:
            index[name] = i
    missing = [c for c in REQUIRED_COLUMNS if c not in index]
    if missing:
        found = "、".join(h for h in header if h) or "（無）"
        raise InputError(f"缺少必要欄位：{'、'.join(missing)}。檔案第一列的欄位是：{found}")

    records, skipped = [], []
    for row_no, values in rows:
        rec = {c: (values[index[c]] if index[c] < len(values) else "") for c in REQUIRED_COLUMNS}
        empty = [c for c, v in rec.items() if not v]
        if empty:
            skipped.append((row_no, empty))
        else:
            records.append(rec)
    return records, skipped


# ---------------------------------------------------------------- 合併列印

def merge_docx(template, records, out_path):
    from mailmerge import MailMerge

    with MailMerge(str(template)) as document:
        fields = set(document.get_merge_fields())
        unknown = set(REQUIRED_COLUMNS) - fields
        if unknown:
            raise InputError(f"模板裡沒有這些合併欄位：{'、'.join(sorted(unknown))}")
        # 模板是一個 72 列 × 2 欄的表格（9 頁），每格一筆，用 NEXT 換下一筆；
        # 超過 144 筆時整份模板接續往下排，不插分頁
        document.merge_templates(records, "continuous_section")
        document.write(str(out_path))
    prune_unused_cells(out_path, len(records))


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def prune_unused_cells(docx_path, used):
    """模板固定 144 格，資料不滿時，刪掉整列都沒用到的表格列，並清空最後一列多出來的那格。

    不處理的話，12 筆資料也會印出 9 頁，後面幾頁只剩「先生/小姐」。
    """
    from lxml import etree

    with zipfile.ZipFile(docx_path) as zf:
        parts = {i: zf.read(i.filename) for i in zf.infolist()}
    doc_info = next(i for i in parts if i.filename == "word/document.xml")
    root = etree.fromstring(parts[doc_info])

    cells = root.findall(f".//{W}tbl/{W}tr/{W}tc")
    if len(cells) < used:
        return  # 模板結構和預期不同，不動它
    position = {cell: i for i, cell in enumerate(cells)}
    for row in root.findall(f".//{W}tbl/{W}tr"):
        row_cells = row.findall(f"{W}tc")
        indices = [position[c] for c in row_cells]
        if all(i >= used for i in indices):
            row.getparent().remove(row)
            continue
        for cell, i in zip(row_cells, indices):
            if i >= used:
                for run in cell.findall(f".//{W}r"):
                    run.getparent().remove(run)
    for tbl in root.findall(f".//{W}tbl"):
        if tbl.find(f"{W}tr") is None:
            tbl.getparent().remove(tbl)
    shrink_trailing_paragraphs(root)

    parts[doc_info] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    with zipfile.ZipFile(docx_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for info, data in parts.items():
            zf.writestr(info, data)


def shrink_trailing_paragraphs(root):
    """表格剛好排滿一頁時，後面的空段落會被擠到下一頁，多出空白頁或把下一份模板往後推；
    把每個表格後面的空段落都壓到幾乎沒有高度。"""
    from lxml import etree

    body = root.find(f"{W}body")
    trailing = []
    for tbl in body.findall(f"{W}tbl"):
        for sib in tbl.itersiblings():
            if sib.tag != f"{W}p":
                break
            trailing.append(sib)
    for p in trailing:
        if "".join(p.itertext()).strip():
            continue
        ppr = p.find(f"{W}pPr")
        if ppr is None:
            ppr = etree.Element(f"{W}pPr")
            p.insert(0, ppr)
        for old in ppr.findall(f"{W}spacing"):
            ppr.remove(old)
        spacing = etree.Element(f"{W}spacing", {f"{W}before": "0", f"{W}after": "0",
                                                f"{W}line": "1", f"{W}lineRule": "exact"})
        # pPr 子元素有固定順序，spacing 要排在 ind、jc、rPr 這些之前
        later = [c for c in ppr if c.tag in SPACING_FOLLOWERS]
        if later:
            later[0].addprevious(spacing)
        else:
            ppr.append(spacing)
        rpr = ppr.find(f"{W}rPr")
        if rpr is None:
            rpr = etree.SubElement(ppr, f"{W}rPr")
        _set_child(rpr, "sz", "2", SZ_FOLLOWERS | {f"{W}szCs"})
        _set_child(rpr, "szCs", "2", SZ_FOLLOWERS)


def _set_child(parent, tag, val, followers):
    """設定 w:val；元素不存在時插在第一個「順序上該排在它後面」的兄弟之前。"""
    from lxml import etree

    el = parent.find(f"{W}{tag}")
    if el is None:
        el = etree.Element(f"{W}{tag}")
        later = [c for c in parent if c.tag in followers]
        if later:
            later[0].addprevious(el)
        else:
            parent.append(el)
    el.set(f"{W}val", val)


SPACING_FOLLOWERS = {f"{W}{t}" for t in (
    "ind", "contextualSpacing", "mirrorIndents", "suppressOverlap", "jc", "textDirection",
    "textAlignment", "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr", "sectPr", "pPrChange")}
SZ_FOLLOWERS = {f"{W}{t}" for t in (
    "highlight", "u", "effect", "bdr", "shd", "fitText", "vertAlign", "rtl", "cs", "em", "lang",
    "eastAsianLayout", "specVanish", "oMath", "ins", "del", "moveFrom", "moveTo", "rPrChange")}


# ---------------------------------------------------------------- PDF

def find_soffice():
    for name in ("soffice", "libreoffice"):
        path = shutil.which(name)
        if path:
            return path
    mac = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    return mac if os.path.exists(mac) else None


def to_pdf(soffice, docx_path):
    out_dir = docx_path.parent
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, HOME=tmp)
        if sys.platform.startswith("linux"):
            conf = Path(tmp) / "fonts.conf"
            aliases = "".join(
                f'<alias binding="same"><family>{src}</family><prefer><family>{dst}</family></prefer></alias>'
                for src, dst in LINUX_FONT_ALIASES.items()
            )
            conf.write_text(
                '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig>'
                '<include ignore_missing="yes">/etc/fonts/fonts.conf</include>'
                f"{aliases}</fontconfig>",
                encoding="utf-8",
            )
            env["FONTCONFIG_FILE"] = str(conf)
        cmd = [soffice, f"-env:UserInstallation=file://{tmp}/profile", "--headless",
               "--convert-to", "pdf", "--outdir", str(out_dir), str(docx_path)]
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
    pdf = docx_path.with_suffix(".pdf")
    if proc.returncode != 0 or not pdf.exists():
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"soffice exit {proc.returncode}")
    return pdf


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("excel", help="發票寄送紀錄（.xlsx / .xlsm / .csv）")
    ap.add_argument("--out-dir", help="輸出資料夾，預設和輸入檔同一個資料夾")
    ap.add_argument("--template", default=str(DEFAULT_TEMPLATE), help="Word 模板，預設用 skill 附的標籤模板")
    ap.add_argument("--no-pdf", action="store_true", help="只產出 docx，不轉 PDF")
    args = ap.parse_args()

    src = Path(args.excel).expanduser()
    template = Path(args.template).expanduser()
    out_dir = Path(args.out_dir).expanduser() if args.out_dir else src.parent
    if not src.is_file():
        print(f"❌ 找不到輸入檔：{src}")
        return 2
    if not template.is_file():
        print(f"❌ 找不到 Word 模板：{template}")
        return 2

    try:
        header, rows = read_rows(src)
        records, skipped = build_records(header, rows)
    except InputError as e:
        print(f"❌ {e}")
        return 2

    for row_no, empty in skipped:
        print(f"⚠️ 第 {row_no} 列缺少 {'、'.join(empty)}，已略過")
    if not records:
        print("❌ 沒有任何一列四個欄位都齊全，無法產生標籤")
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = src.stem.replace(" ", "")
    docx_path = out_dir / f"{stem}.docx"
    try:
        merge_docx(template, records, docx_path)
    except InputError as e:
        print(f"❌ {e}")
        return 2
    pages = -(-len(records) // LABELS_PER_PAGE)
    print(f"✅ DOCX：{docx_path}（{len(records)} 筆，{pages} 頁標籤紙）")

    status, pdf_path = 0, None
    if not args.no_pdf:
        soffice = find_soffice()
        if not soffice:
            print("⚠️ 找不到 LibreOffice（soffice），無法轉 PDF；docx 已產出")
            status = 3
        else:
            try:
                pdf_path = to_pdf(soffice, docx_path)
                print(f"✅ PDF：{pdf_path}")
            except (RuntimeError, subprocess.TimeoutExpired) as e:
                print(f"⚠️ PDF 轉檔失敗：{e}；docx 已產出")
                status = 4

    zip_path = out_dir / f"{stem}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(docx_path, docx_path.name)
        if pdf_path:
            zf.write(pdf_path, pdf_path.name)
    print(f"✅ ZIP：{zip_path}")
    if skipped:
        print(f"⚠️ 共略過 {len(skipped)} 列，請確認是否要補資料後重跑")
    return status


if __name__ == "__main__":
    sys.exit(main())
