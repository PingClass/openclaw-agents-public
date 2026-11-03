# 安裝與環境需求

這份是給維護 OpenClaw 的人看的，agent 不需要讀。

## 需要的套件

| 套件 | 用途 | 沒裝會怎樣 |
|---|---|---|
| `pypdf`（6.0 以上） | 讀寫 PDF | skill 無法使用，腳本一啟動就 `ModuleNotFoundError` |

## OpenClaw Docker image

在 OpenClaw repo 的 `.env` 的 pip 套件加上 `pypdf`，然後重新 build image。和 slide-deck 一起裝時寫成：

```bash
OPENCLAW_IMAGE_PIP_PACKAGES="python-pptx python-docx pypdf"
```

```bash
docker compose build && docker compose up -d
```

## 安裝 skill

把整個 `pdf-split/` 資料夾複製到 `~/.openclaw/skills/`，容器內的路徑是 `/home/node/.openclaw/skills/pdf-split/`。
SKILL.md 裡的指令都寫死這個路徑；裝到其他位置時，要記得一起改 SKILL.md。

## 和原本 Raycast 版的差別

這支腳本改寫自 `~/custom_scripts/pdf-editor/split-pdf-by-pages.py`：

- 檔案路徑改用參數傳入，不讀剪貼簿；也不會切換到 venv。
- 新增 `--info`、`--every N`、`--out-dir`，範圍語法多了 `8-` 和 `end`。
- 輸出檔已存在時預設不覆蓋（exit 4），要加 `--overwrite`。
- 只有擁有者密碼的 PDF 會自動解開；有開檔密碼的會回報錯誤。

## 自我測試

```bash
python3 /home/node/.openclaw/skills/pdf-split/scripts/split_pdf.py \
  /home/node/.openclaw/skills/pdf-split/examples/sample-10p.pdf \
  --ranges "1-3,4-" --out-dir /tmp/pdf-split-test --overwrite
```

輸出兩行 `✅` 和 `🎉 完成，共 2 個檔案`，就代表環境都裝好了。
