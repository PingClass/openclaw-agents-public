#!/usr/bin/env python3
"""把發票 JSON 轉成 xlsx，一個發票項目一列。

用法：
  python3 build_xlsx.py INVOICES.json --out OUTPUT.xlsx [--overwrite]

JSON 格式見 references/invoice-schema.md。
欄位：發票號碼、發生日期、付款日期、發票項目、金額、幣別。

exit code：
  0 成功（可能附帶 ⚠️ 警告，要看完再交件）
  2 JSON 格式或欄位有錯（會列出每一條錯在哪，不會寫出檔案）
  4 輸出檔已存在且沒有加 --overwrite
"""
import argparse
import json
import re
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HEADERS = ["發票號碼", "發生日期", "付款日期", "發票項目", "金額", "幣別"]
WIDTHS = [20, 12, 12, 48, 14, 8]
# 這些幣別沒有小數位
ZERO_DECIMAL = {"TWD", "JPY", "KRW", "VND", "IDR", "CLP", "ISK", "HUF"}
# 加總和總額差多少以內視為一致（四捨五入誤差）
TOLERANCE = Decimal("0.01")


def parse_date(value, where, errors):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        errors.append(f"{where}：「{value}」不是 YYYY-MM-DD 格式")
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        errors.append(f"{where}：「{value}」不是存在的日期")
        return None


def parse_amount(value, where, errors):
    # 不接受字串，避免 "$1,200" 或 "1.200,00" 這種寫法被誤讀
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{where}：金額必須是數字，收到「{value}」")
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        errors.append(f"{where}：「{value}」不是有效的金額")
        return None


def validate(data):
    """回傳 (rows, errors, warnings)。rows 是要寫進 xlsx 的每一列。"""
    errors, warnings, rows = [], [], []
    invoices = data.get("invoices") if isinstance(data, dict) else None
    if not isinstance(invoices, list) or not invoices:
        return [], ["最外層必須是 {\"invoices\": [...]}，而且至少一張發票"], []

    seen = {}
    for n, inv in enumerate(invoices, 1):
        where = f"第 {n} 張發票"
        if not isinstance(inv, dict):
            errors.append(f"{where}：必須是物件")
            continue

        number = inv.get("invoice_number")
        if not isinstance(number, str) or not number.strip():
            errors.append(f"{where}：缺少 invoice_number")
            number = ""
        else:
            number = number.strip()
            where = f"發票 {number}"
            if number in seen:
                warnings.append(f"發票號碼 {number} 出現兩次（第 {seen[number]} 張和第 {n} 張），確認是不是重複放了同一張")
            seen.setdefault(number, n)

        issue = parse_date(inv.get("issue_date"), f"{where} issue_date", errors)
        if inv.get("issue_date") is None:
            errors.append(f"{where}：缺少 issue_date")
        paid = parse_date(inv.get("payment_date"), f"{where} payment_date", errors)
        if inv.get("payment_date") is None:
            warnings.append(f"{where}：沒有付款日期，該欄留白")

        currency = inv.get("currency")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            errors.append(f"{where}：currency 必須是 3 碼大寫幣別代碼（如 USD、TWD），收到「{currency}」")
            currency = None

        items = inv.get("items")
        if not isinstance(items, list) or not items:
            errors.append(f"{where}：items 必須是非空陣列")
            continue

        total_items = Decimal(0)
        for m, item in enumerate(items, 1):
            iw = f"{where} 第 {m} 個項目"
            if not isinstance(item, dict):
                errors.append(f"{iw}：必須是物件")
                continue
            desc = item.get("description")
            if not isinstance(desc, str) or not desc.strip():
                errors.append(f"{iw}：缺少 description")
                desc = ""
            amount = parse_amount(item.get("amount"), iw, errors)
            if amount is not None:
                total_items += amount
            rows.append([number, issue, paid, desc.strip(), amount, currency])

        if "total" in inv:
            total = parse_amount(inv["total"], f"{where} total", errors)
            if total is not None and abs(total - total_items) > TOLERANCE:
                warnings.append(
                    f"{where}：項目加總 {total_items} 和發票總額 {total} 對不起來，"
                    f"差 {total - total_items}（漏了稅額、運費、折扣，或退款少了負號？）"
                )
        else:
            warnings.append(f"{where}：沒有 total，無法核對項目加總")

    return rows, errors, warnings


def write_xlsx(rows, out):
    wb = Workbook()
    ws = wb.active
    ws.title = "發票明細"
    ws.append(HEADERS)
    header_fill = PatternFill("solid", fgColor="F2F2F2")
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    for row in rows:
        ws.append([float(v) if isinstance(v, Decimal) else v for v in row])
        r = ws.max_row
        for col in (2, 3):
            ws.cell(r, col).number_format = "yyyy-mm-dd"
        decimals = "" if row[5] in ZERO_DECIMAL else ".00"
        ws.cell(r, 5).number_format = f"#,##0{decimals};-#,##0{decimals}"
        ws.cell(r, 4).alignment = Alignment(wrap_text=True, vertical="top")

    for i, width in enumerate(WIDTHS, 1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(out)


def main():
    ap = argparse.ArgumentParser(description="發票 JSON 轉 xlsx")
    ap.add_argument("json", help="發票 JSON 檔")
    ap.add_argument("--out", required=True, help="輸出的 .xlsx 路徑")
    ap.add_argument("--overwrite", action="store_true", help="輸出檔已存在時直接覆蓋")
    args = ap.parse_args()

    out = Path(args.out).expanduser().resolve()
    if out.suffix.lower() != ".xlsx":
        print("❌ --out 必須是 .xlsx 檔", file=sys.stderr)
        sys.exit(2)

    try:
        data = json.loads(Path(args.json).expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"❌ 找不到 JSON 檔：{args.json}", file=sys.stderr)
        sys.exit(2)
    except json.JSONDecodeError as e:
        print(f"❌ JSON 格式有錯：第 {e.lineno} 行第 {e.colno} 欄：{e.msg}", file=sys.stderr)
        sys.exit(2)

    rows, errors, warnings = validate(data)
    if errors:
        print("❌ 發票資料有錯：", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(2)

    if out.exists() and not args.overwrite:
        print(f"❌ 輸出檔已存在：{out}（加 --overwrite 覆蓋，或換檔名）", file=sys.stderr)
        sys.exit(4)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_xlsx(rows, out)

    for w in warnings:
        print(f"⚠️ {w}")
    print(f"✅ {out}（{len(data['invoices'])} 張發票，{len(rows)} 個項目）")


if __name__ == "__main__":
    main()
