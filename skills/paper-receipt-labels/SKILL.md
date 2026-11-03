---
name: paper-receipt-labels
description: 把「紙本發票寄送紀錄」Excel（訂單編號、發票號碼、收件人姓名、收件人地址）合併列印成 A4 標籤紙，回傳 PDF 和 ZIP（內含 docx 與 PDF）。使用者傳來發票寄送紀錄、要印發票寄送標籤、要做紙本發票合併列印時，就用這個 skill，不要自己寫合併列印程式。
metadata: { "openclaw": { "requires": { "bins": ["python3"] } } }
---

# 紙本發票寄送標籤

`merge_labels.py` 會負責讀 Excel、套 Word 模板、轉 PDF 和打包。模板是 A4 標籤紙，一頁 16 張（2 欄 × 8 列），有幾筆就印幾格，用不到的格子和空白頁會自動拿掉。
你只要負責確認輸入檔、執行腳本、看輸出、交件。不要自己改模板，也不要用其他方式拼 docx。

腳本一律用**絕對路徑**執行（相對路徑、`~`、`{baseDir}` 字面值可能被 exec 白名單擋掉）：

- `/home/node/.openclaw/skills/paper-receipt-labels/scripts/merge_labels.py`

## 流程

### 1. 確認輸入檔

- 收 `.xlsx`、`.xlsm`、`.csv`。舊版 `.xls` 不支援，請使用者用 Excel 另存成 `.xlsx`。
- 第一個工作表的第一列是標題，必須有這 4 欄：**訂單編號、發票號碼、收件人姓名、收件人地址**。其他欄位會被忽略，欄位順序沒有關係。
- 使用者沒附檔案時，先請對方傳檔，不要自己編資料。

### 2. 執行

輸出資料夾用輸入檔的檔名（去掉副檔名和空白）當 `<slug>`：

```bash
python3 /home/node/.openclaw/skills/paper-receipt-labels/scripts/merge_labels.py \
  "<輸入檔絕對路徑>" \
  --out-dir /home/node/.openclaw/workspace/paper-receipts/<slug>
```

- 使用者只要 Word 檔時，加 `--no-pdf`。
- 輸出的檔名一律去掉空白，例如 `0800發票寄送紀錄 1141030.xlsx` 會產出 `0800發票寄送紀錄1141030.docx / .pdf / .zip`。

### 3. 看輸出、處理

| 輸出 | 意思 | 處理 |
|---|---|---|
| `✅ DOCX / PDF / ZIP` (exit 0) | 全部完成 | 交件 |
| `⚠️ 第 N 列缺少 …，已略過` | 那一列有欄位是空的，沒有印進標籤 | 照常交件，但在回覆裡**逐列**告訴使用者哪幾列被略過、缺什麼 |
| `❌ 缺少必要欄位` (exit 2) | 標題列對不上 | 把腳本列出的「實際欄位」告訴使用者，請對方改標題或確認傳錯檔。**不要自己改使用者的 Excel** |
| `❌ 沒有任何一列四個欄位都齊全` (exit 2) | 整份都缺資料 | 請使用者確認檔案 |
| `⚠️ 找不到 LibreOffice` (exit 3) | 環境沒有 PDF 轉檔工具，docx 和 ZIP 已經產出 | 只交 ZIP，並告訴使用者這次產不出 PDF |
| `⚠️ PDF 轉檔失敗` (exit 4) | LibreOffice 轉檔出錯，docx 和 ZIP 已經產出 | 只交 ZIP，附上錯誤訊息的第一行 |

### 4. 交付

用 message tool 的 `mediaUrls` 附上 **PDF 和 ZIP**（ZIP 裡有 docx 和 PDF，給要存檔或改字的人用）。沒有 PDF 時只附 ZIP。
如果只能用文字回覆，就在回覆最後每個檔案各放一行獨立的 `MEDIA:/絕對路徑`，不要加粗，也不要包在 code block 裡。

回覆要短：

- 印了幾筆、幾頁標籤紙
- 有略過的列就列出來
- 提醒列印時要選**實際大小（100%）**，不要選「符合頁面大小」，不然標籤會對不準

不要把收件人姓名和地址貼回對話。
