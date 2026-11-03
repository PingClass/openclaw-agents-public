# 安裝與環境需求

這份是給維護 OpenClaw 的人看的，agent 不需要讀。

## 需要的套件

| 套件 | 用途 | 沒裝會怎樣 |
|---|---|---|
| `docx-mailmerge2` | 合併列印（支援模板裡的 NEXT 欄位） | skill 無法使用 |
| `openpyxl` | 讀 .xlsx | 只能讀 .csv |
| `libreoffice-writer` | 把 .docx 轉成 PDF | 只產出 docx，腳本以 exit 3 回報 |
| `fonts-noto-cjk` | 轉 PDF 時用的繁中字型 | PDF 的中文字會變成方框 |

## OpenClaw Docker image

在 OpenClaw repo 的 `.env` 加上下面兩行，然後重新 build image。
如果已經裝了 slide-deck，就把套件併進原本那兩行，不要重複宣告：

```bash
OPENCLAW_IMAGE_APT_PACKAGES="libreoffice-writer fonts-noto-cjk"
OPENCLAW_IMAGE_PIP_PACKAGES="docx-mailmerge2==0.9.0 openpyxl"
```

和 slide-deck 一起裝時：

```bash
OPENCLAW_IMAGE_APT_PACKAGES="libreoffice-impress libreoffice-writer fonts-noto-cjk"
OPENCLAW_IMAGE_PIP_PACKAGES="python-pptx python-docx docx-mailmerge2==0.9.0 openpyxl"
```

```bash
docker compose build && docker compose up -d
```

## 安裝 skill

把整個 `paper-receipt-labels/` 資料夾複製到 `~/.openclaw/skills/`，容器內的路徑是 `/home/node/.openclaw/skills/paper-receipt-labels/`。
SKILL.md 裡的指令都寫死這個路徑；裝到其他位置時，要記得一起改 SKILL.md。

## 模板

`assets/paper_receipt_template.docx` 是 A4 標籤紙，一頁 16 張（2 欄 × 8 列，邊界 0）。整份模板是一個 72 列 × 2 欄的表格（9 頁、144 格），每格有 4 個 MERGEFIELD（訂單編號、發票號碼、收件人姓名、收件人地址），格與格之間用 NEXT 換下一筆資料。超過 144 筆時，`docx-mailmerge2` 會把整份模板接在後面再排一次。

合併完之後，`merge_labels.py` 會再處理兩件事：

- 刪掉整列都沒用到的表格列，奇數筆時清掉最後一格（不然 12 筆也會印出 9 頁，後面幾頁只剩「先生/小姐」）
- 把每個表格後面的空段落壓到 1 twip 高（表格剛好排滿一頁時，這個段落會被擠到下一頁，變成空白頁）

換模板時，合併欄位名稱要和這 4 個欄位一致，否則腳本會以 exit 2 回報。前面這兩項處理都假設**一格放一筆**；一頁的張數有變的話，也要一起改 `merge_labels.py` 裡的 `LABELS_PER_PAGE`（只影響回報的頁數）。

## 字型

模板用的字型是**新細明體**。Windows 有內建，下載 docx 直接用 Word 開不會跑版。
容器裡沒有這個字型，所以轉 PDF 時，腳本會用 fontconfig 把它對應到 Noto Sans CJK TC，字形會略有不同，但版面位置不變。

## 和原本 Raycast 腳本的差異

| | Raycast 版（`mail-merge-paper-receipt.py`） | 這個 skill |
|---|---|---|
| 轉 PDF | `docx2pdf`（要裝 Microsoft Word） | LibreOffice headless |
| 讀 Excel | pandas | openpyxl（數字欄位不會變成 `123.0`，也支援 .csv） |
| 缺資料的列 | 默默丟掉 | 一樣略過，但會逐列列出 |
| 頁數 | 固定 9 頁的倍數，沒用到的格子只剩「先生/小姐」 | 有幾筆印幾格，不多印空白頁 |
| 輸出檔名 | docx 和 PDF 保留空白，只有 ZIP 去掉空白 | 全部去掉空白 |

## 自我測試

```bash
python3 /home/node/.openclaw/skills/paper-receipt-labels/scripts/merge_labels.py \
  /home/node/.openclaw/skills/paper-receipt-labels/examples/sample.xlsx \
  --out-dir /tmp/paper-receipt-test
```

輸出 `✅ DOCX`、`✅ PDF`、`✅ ZIP` 三行，就代表環境都裝好了。`examples/sample.xlsx` 是 12 筆假資料。
