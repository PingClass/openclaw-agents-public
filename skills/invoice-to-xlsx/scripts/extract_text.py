#!/usr/bin/env python3
"""把一或多份發票 PDF 的文字印出來，給 agent 讀。

用法：
  python3 extract_text.py INVOICE.pdf [MORE.pdf ...]

每份檔案以 `===== 檔案：<絕對路徑>（共 N 頁）=====` 開頭，每頁以 `--- 第 N 頁 ---` 分隔。
文字依版面位置排序，表格的欄位會盡量留在同一行。

用 PyMuPDF 而不用 pypdf：pypdf 會把部分 PDF 裡的 `-`、`–`、`−` 字元吃掉，
發票號碼和退款的負號都會因此出錯。

exit code：
  0 全部成功
  3 有檔案不存在、不是 PDF、無法讀取或有密碼（其他檔案照常輸出）
  5 有檔案抽不到文字，多半是掃描檔或照片（其他檔案照常輸出）
"""
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import pymupdf

# 一頁有圖片、但非空白字元少於這個數，就當成掃描檔
MIN_CHARS_PER_PAGE = 20


def normalize_path(raw: str) -> Path:
    """容忍常見的路徑寫法：前後引號、file:// URL、~。"""
    s = raw.strip().strip("'\"")
    if s.startswith("file://"):
        s = unquote(urlparse(s).path)
    return Path(s).expanduser().resolve()


def extract(path: Path) -> int:
    if not path.is_file():
        print(f"❌ 檔案不存在：{path}", file=sys.stderr)
        return 3
    if path.suffix.lower() != ".pdf":
        print(f"❌ 不是 PDF 檔：{path}", file=sys.stderr)
        return 3
    try:
        doc = pymupdf.open(path)
    except Exception as e:
        print(f"❌ 無法讀取 PDF：{path}：{e}", file=sys.stderr)
        return 3
    if doc.needs_pass and not doc.authenticate(""):
        print(f"❌ 這份 PDF 有開檔密碼：{path}", file=sys.stderr)
        return 3

    print(f"===== 檔案：{path}（共 {doc.page_count} 頁）=====")
    code = 0
    for i, page in enumerate(doc, 1):
        text = page.get_text(sort=True)
        # 去掉排版造成的大量空行，保留段落間的一個空行
        lines = [ln.rstrip() for ln in text.splitlines()]
        compact = []
        for ln in lines:
            if ln or (compact and compact[-1]):
                compact.append(ln)
        print(f"--- 第 {i} 頁 ---")
        print("\n".join(compact).strip())
        few_chars = sum(not c.isspace() for c in text) < MIN_CHARS_PER_PAGE
        if few_chars and page.get_images():
            print(f"⚠️ 第 {i} 頁幾乎沒有文字，可能是掃描檔或照片", file=sys.stderr)
            code = 5
    print()
    return code


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__.strip().split("\n\n")[1], file=sys.stderr)
        sys.exit(2)
    codes = [extract(normalize_path(a)) for a in sys.argv[1:]]
    sys.exit(max(codes))


if __name__ == "__main__":
    main()
