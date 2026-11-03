---
name: invoice-to-xlsx
description: 讀取一或多份發票 PDF，整理出發票號碼、發生日期、付款日期、發票項目、金額、幣別，回傳 xlsx。使用者傳來發票、收據、invoice PDF，要整理成 Excel、做帳、對帳或報帳時，就用這個 skill，不要自己寫 PDF 解析或 Excel 程式。
metadata: { "openclaw": { "requires": { "bins": ["python3"] } } }
---

# 發票轉 Excel

你負責**讀懂發票**：從文字裡找出每個欄位，寫成一份 JSON。
`extract_text.py` 負責把 PDF 轉成文字，`build_xlsx.py` 負責驗證 JSON 和排版 xlsx。
不要自己寫 PDF 解析或 openpyxl 程式碼。

腳本一律用下面的**絕對路徑**執行（相對路徑、`~`、`{baseDir}` 字面值可能被 exec 白名單拒絕）：

- `/home/node/.openclaw/skills/invoice-to-xlsx/scripts/extract_text.py`
- `/home/node/.openclaw/skills/invoice-to-xlsx/scripts/build_xlsx.py`

## 流程

### 1. 抽出文字

一次可以給多份 PDF：

```bash
python3 /home/node/.openclaw/skills/invoice-to-xlsx/scripts/extract_text.py <發票1.pdf> [<發票2.pdf> ...]
```

| 輸出 | 意思 | 處理 |
|---|---|---|
| `===== 檔案：…=====` 後接內文 | 成功 | 往下做 |
| `⚠️ 第 N 頁幾乎沒有文字` (exit 5) | 掃描檔或照片，這個 skill 讀不了 | 告訴使用者這份需要文字版 PDF，其他檔案照常處理 |
| `❌ 檔案不存在 / 不是 PDF / 無法讀取 / 有開檔密碼` (exit 3) | 檔案有問題 | 跟使用者確認，其他檔案照常處理 |

### 2. 寫 JSON

第一次用先讀 `references/invoice-schema.md`，裡面有完整欄位、付款日期的判斷規則和常見陷阱。
範例在 `examples/invoices.json`，對應的發票文字在 `examples/sample-invoice.txt`。

存到 `/home/node/.openclaw/workspace/invoices/<slug>/invoices.json`。`<slug>` 用簡短英文，例如 `2026-06-anthropic`；多份發票就用月份，例如 `2026-06`。

重點：

- 每個欄位都要**照發票上的內容抄**，不要猜。發票上沒有的欄位依 schema 處理，不要自己編。
- 金額用該列的 Amount，不是單價。退款、折抵是**負數**。稅額另列一個項目。
- 日期一律 `YYYY-MM-DD`。

### 3. 產生 xlsx

```bash
python3 /home/node/.openclaw/skills/invoice-to-xlsx/scripts/build_xlsx.py \
  /home/node/.openclaw/workspace/invoices/<slug>/invoices.json \
  --out /home/node/.openclaw/workspace/invoices/<slug>/<檔名>.xlsx
```

檔名用 `發票明細-<slug>.xlsx`。使用者要求重做時加 `--overwrite`。

### 4. 看輸出、修正

| 輸出 | 意思 | 處理 |
|---|---|---|
| `❌ 發票資料有錯` (exit 2) | JSON 欄位缺漏或格式不對 | 照每條錯誤修正 JSON 再跑，不要跳過 |
| `⚠️ 項目加總…和發票總額…對不起來` | 漏了項目、稅額，或退款少了負號 | 回頭對照發票文字找出原因，修好 JSON 再跑。確定是發票本身算錯時，在回覆裡告訴使用者 |
| `⚠️ 沒有付款日期` | 發票上找不到付款日或付款期限 | 再看一次原文；真的沒有就保留，回覆時提一句 |
| `⚠️ 發票號碼…出現兩次` | 同一張發票放了兩次 | 刪掉重複的那筆再跑 |
| `❌ 輸出檔已存在` (exit 4) | 之前產過同名檔 | 使用者要重做就加 `--overwrite`，不然換檔名 |

加總對不起來的警告必須處理完才交件。

### 5. 交付

用 message tool 的 `mediaUrls` 附上 xlsx 路徑。如果只能用文字回覆，就在回覆最後放一行獨立的 `MEDIA:/絕對路徑`。不要加粗，也不要包在 code block 裡。

回覆內容要短：

- 幾張發票、幾個項目，各幣別的總額
- 付款日期用的是實際付款日還是付款期限
- 有讀不了的檔案或沒處理掉的警告，列出來

不要把整份 JSON 貼回對話。
