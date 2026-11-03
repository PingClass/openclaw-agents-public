#!/usr/bin/env python3
"""依頁數範圍把一份 PDF 分割成多個檔案。

用法：
  python3 split_pdf.py INPUT.pdf --info
  python3 split_pdf.py INPUT.pdf --ranges "1-3,4-7,8-" [--out-dir DIR] [--overwrite]
  python3 split_pdf.py INPUT.pdf --every 5 [--out-dir DIR] [--overwrite]

範圍語法：逗號分隔；`3` 是單頁，`1-3` 是區間，`8-` 是第 8 頁到最後一頁，
`end` 代表最後一頁（例如 `5-end`）。頁碼從 1 開始。

輸出檔名是 `<原檔名>_part<N>.pdf`。每產出一份印一行 `✅ <絕對路徑> (頁 a-b)`。

exit code：
  0 成功
  2 參數或範圍有錯（訊息會說明哪一段錯）
  3 檔案不存在、不是 PDF、無法讀取或有密碼
  4 輸出檔已存在且沒有加 --overwrite（什麼都不會寫出）
"""
import argparse
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from pypdf import PdfReader, PdfWriter


class RangeError(ValueError):
    pass


def normalize_path(raw: str) -> Path:
    """容忍常見的路徑寫法：前後引號、file:// URL、~。"""
    s = raw.strip().strip("'\"")
    if s.startswith("file://"):
        s = unquote(urlparse(s).path)
    return Path(s).expanduser().resolve()


def parse_page(token: str, total: int, part: str) -> int:
    token = token.strip().lower()
    if token in ("end", "last"):
        return total
    if not token.isdigit():
        raise RangeError(f"「{part}」不是有效的頁碼")
    return int(token)


def parse_ranges(range_str: str, total: int) -> list[tuple[int, int]]:
    """回傳 1-indexed、含頭含尾的 (start, end) 清單。"""
    ranges = []
    parts = [p.strip() for p in range_str.split(",")]
    if not any(parts):
        raise RangeError("範圍是空的")
    for part in parts:
        if not part:
            raise RangeError(f"範圍「{range_str}」裡有空的一段（多了逗號？）")
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start = parse_page(start_s, total, part)
            end = total if not end_s.strip() else parse_page(end_s, total, part)
        else:
            start = end = parse_page(part, total, part)

        if start < 1:
            raise RangeError(f"「{part}」：頁碼必須從 1 開始")
        if start > end:
            raise RangeError(f"「{part}」：起始頁不能大於結束頁")
        if end > total:
            raise RangeError(f"「{part}」：超出範圍，這份 PDF 只有 {total} 頁")
        ranges.append((start, end))
    return ranges


def every_ranges(n: int, total: int) -> list[tuple[int, int]]:
    if n < 1:
        raise RangeError("--every 必須是正整數")
    return [(s, min(s + n - 1, total)) for s in range(1, total + 1, n)]


def open_pdf(path: Path) -> PdfReader:
    if not path.exists():
        fail(3, f"檔案不存在：{path}")
    if not path.is_file():
        fail(3, f"不是檔案：{path}")
    if path.suffix.lower() != ".pdf":
        fail(3, f"不是 PDF 檔：{path}")
    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            # 只有「擁有者密碼」、開檔不需密碼的 PDF，用空密碼就能解開
            if not reader.decrypt(""):
                fail(3, "這份 PDF 有開檔密碼，無法分割")
        len(reader.pages)
    except SystemExit:
        raise
    except Exception as e:  # pypdf 讀壞檔的例外種類很多
        fail(3, f"無法讀取 PDF：{e}")
    return reader


def fail(code: int, msg: str) -> None:
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(code)


def main() -> None:
    ap = argparse.ArgumentParser(description="依頁數範圍分割 PDF")
    ap.add_argument("input", help="PDF 檔案路徑")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--info", action="store_true", help="只印出總頁數")
    mode.add_argument("--ranges", help='頁數範圍，例如 "1-3,4-7,8-"')
    mode.add_argument("--every", type=int, metavar="N", help="每 N 頁切成一份")
    ap.add_argument("--out-dir", help="輸出資料夾（預設是原檔所在的資料夾）")
    ap.add_argument("--overwrite", action="store_true", help="輸出檔已存在時直接覆蓋")
    args = ap.parse_args()

    src = normalize_path(args.input)
    reader = open_pdf(src)
    total = len(reader.pages)

    if args.info:
        print(f"📄 {src}")
        print(f"📊 總頁數：{total}")
        return

    try:
        if args.every is not None:
            ranges = every_ranges(args.every, total)
        else:
            ranges = parse_ranges(args.ranges, total)
    except RangeError as e:
        fail(2, f"範圍有錯：{e}")

    out_dir = normalize_path(args.out_dir) if args.out_dir else src.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = [out_dir / f"{src.stem}_part{i}.pdf" for i in range(1, len(ranges) + 1)]

    # 先全部檢查完再寫，避免寫到一半才發現衝突
    if not args.overwrite:
        existing = [p for p in outputs if p.exists()]
        if existing:
            names = "、".join(p.name for p in existing)
            fail(4, f"輸出檔已存在：{names}（加 --overwrite 覆蓋，或換 --out-dir）")

    print(f"📄 輸入：{src}（共 {total} 頁）")
    for out, (start, end) in zip(outputs, ranges):
        writer = PdfWriter()
        for i in range(start - 1, end):
            writer.add_page(reader.pages[i])
        with open(out, "wb") as f:
            writer.write(f)
        label = f"{start}" if start == end else f"{start}-{end}"
        print(f"✅ {out} (頁 {label})")
    print(f"🎉 完成，共 {len(outputs)} 個檔案")


if __name__ == "__main__":
    main()
