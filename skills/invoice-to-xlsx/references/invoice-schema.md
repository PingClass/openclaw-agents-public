# 發票 JSON 格式

```json
{
  "invoices": [
    {
      "invoice_number": "EXAMPLE1-0001",
      "issue_date": "2026-06-30",
      "payment_date": "2026-06-30",
      "currency": "USD",
      "total": 196.64,
      "items": [
        { "description": "Claude Pro（2026-06-30 至 2027-06-30）", "amount": 200.00 },
        { "description": "Unused time on Claude Pro after 01 Jul 2026（2026-06-30 至 2026-07-05）", "amount": -3.36 }
      ]
    }
  ]
}
```

一份 PDF 一張發票。多份 PDF 就在 `invoices` 裡放多筆，全部會寫進同一個 xlsx。

## 欄位

| 欄位 | 必填 | xlsx 欄 | 說明 |
|---|---|---|---|
| `invoice_number` | 是 | 發票號碼 | 照發票上的寫法抄，保留 `-`。台灣統一發票寫成 `AB-12345678` |
| `issue_date` | 是 | 發生日期 | `YYYY-MM-DD`。發票上的 Date of issue、Invoice date、開立日期 |
| `payment_date` | 否 | 付款日期 | `YYYY-MM-DD`。規則見下方。找不到就填 `null`，該欄會留白 |
| `currency` | 是 | 幣別 | ISO 4217 三碼大寫：`USD`、`TWD`、`JPY`、`EUR`…… |
| `total` | 建議 | （不輸出） | 發票總額，只用來核對項目加總。用 Total 或 Amount due，不要用 Subtotal |
| `items[].description` | 是 | 發票項目 | 項目名稱。有服務期間就用括號附在後面，日期改成 `YYYY-MM-DD` |
| `items[].amount` | 是 | 金額 | 該列的**金額**（Amount），不是單價。必須是數字，不要加 `$` 或千分位逗號 |

## 付款日期怎麼填

依序找，找到第一個就用：

1. 實際付款日：Date paid、Paid on、付款日期、扣款日期
2. 付款期限：Date due、Due date、繳費期限

兩個都沒有就填 `null`。

## 容易出錯的地方

- **退款、折抵、折扣是負數。** 發票上寫 `-$3.36`、`($3.36)`、`Credit`、`Unused time`、`Discount`、`折讓` 的項目，`amount` 要填負數。
- **稅額要列成一個項目。** 發票上另外列出 Tax、VAT、營業稅時，加一個 `{"description": "稅額（VAT 5%）", "amount": ...}`，這樣項目加總才會等於總額。
- **民國年要換算成西元。** `115/06/30` → `2026-06-30`（民國年 + 1911）。
- **英文日期要換成數字。** `June 30, 2026`、`30 Jun 2026` → `2026-06-30`。日期只寫 `06/07/2026` 這種時，要看發票的國家判斷月日順序，判斷不了就問使用者。
- **幣別看符號和文字。** `NT$`、`新台幣`、`元`（台灣發票）→ `TWD`；只有 `$` 時要看發票上有沒有寫 `USD`、`CAD` 之類的代碼。
