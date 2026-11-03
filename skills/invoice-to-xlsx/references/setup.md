# 安裝與環境需求

這份是給維護 OpenClaw 的人看的，agent 不需要讀。

## 需要的套件

| 套件 | 用途 | 沒裝會怎樣 |
|---|---|---|
| `pymupdf` | 從 PDF 抽文字 | skill 無法使用 |
| `openpyxl` | 產生 .xlsx | skill 無法使用 |

這裡不用 pypdf，因為 pypdf 會把部分 PDF 裡的 `-`、`–`、`−` 字元吃掉。以 Anthropic 的發票為例，
發票號碼 `EXAMPLE1-0001` 會變成 `EXAMPLE1 0001`，退款 `-$3.36` 會變成 `$3.36`。PyMuPDF 沒有這個問題。

PyMuPDF 的授權是 AGPL-3.0。在自己的 OpenClaw 裡用沒問題；要把這個 skill 包進商業產品發佈時，要先確認授權。

## OpenClaw Docker image

在 OpenClaw repo 的 `.env` 的 pip 套件加上 `pymupdf openpyxl`，然後重新 build image。和其他 skill 一起裝時寫成：

```bash
OPENCLAW_IMAGE_PIP_PACKAGES="python-pptx python-docx pypdf pymupdf openpyxl"
```

```bash
docker compose build && docker compose up -d
```

## 安裝 skill

把整個 `invoice-to-xlsx/` 資料夾複製到 `~/.openclaw/skills/`，容器內的路徑是 `/home/node/.openclaw/skills/invoice-to-xlsx/`。
SKILL.md 裡的指令都寫死這個路徑；裝到其他位置時，要記得一起改 SKILL.md。

## 限制

- 只讀得了**文字版** PDF。掃描檔或照片轉的 PDF 沒有文字層，`extract_text.py` 會以 exit 5 回報。要支援的話得另外加 OCR（例如 Tesseract + `chi_tra` 語言包），image 會大上數百 MB。
- 欄位是由 agent 判讀的，不是寫死的規則，所以格式各異的發票都能處理，但仍可能判讀錯。
  `build_xlsx.py` 會核對「項目加總 = 發票總額」，可以抓到大部分漏項和正負號錯誤，但抓不到日期或發票號碼抄錯。

## 自我測試

```bash
python3 /home/node/.openclaw/skills/invoice-to-xlsx/scripts/build_xlsx.py \
  /home/node/.openclaw/skills/invoice-to-xlsx/examples/invoices.json \
  --out /tmp/invoice-test.xlsx --overwrite
```

輸出 `✅ /tmp/invoice-test.xlsx（1 張發票，2 個項目）`、沒有任何 `⚠️`，就代表環境都裝好了。
