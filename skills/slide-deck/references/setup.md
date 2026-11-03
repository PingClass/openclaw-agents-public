# 安裝與環境需求

這份是給維護 OpenClaw 的人看的，agent 不需要讀。

## 需要的套件

| 套件 | 用途 | 沒裝會怎樣 |
|---|---|---|
| `python-pptx` | 產生 .pptx | skill 無法使用 |
| `python-docx` | 把 .docx 轉成 Markdown | 不能用 docx 當輸入 |
| `libreoffice-impress` | 把 .pptx 轉成 PDF | 只能產出 pptx，腳本會以 exit 3 回報 |
| `fonts-noto-cjk` | 轉 PDF 時用的繁中字型 | PDF 的中文字會變成方框或用錯字型 |

## OpenClaw Docker image

在 OpenClaw repo 的 `.env` 加上下面兩行，然後重新 build image：

```bash
OPENCLAW_IMAGE_APT_PACKAGES="libreoffice-impress fonts-noto-cjk"
OPENCLAW_IMAGE_PIP_PACKAGES="python-pptx python-docx"
```

```bash
docker compose build && docker compose up -d
```

## 安裝 skill

把整個 `slide-deck/` 資料夾複製到 `~/.openclaw/skills/`，容器內的路徑是 `/home/node/.openclaw/skills/slide-deck/`。
SKILL.md 裡的指令都寫死這個路徑；裝到其他位置時，要記得一起改 SKILL.md。

## 字型

pptx 裡指定的字型是**微軟正黑體**（Microsoft JhengHei）。Windows 和 Office for Mac 都有內建，
老師下載後直接開來修改不會跑版。容器裡沒有這個字型，所以轉 PDF 時，腳本會用 fontconfig
把它對應到 Noto Sans CJK TC。

想換字型可以加 `--font "字型名稱"`。

## 自我測試

```bash
python3 /home/node/.openclaw/skills/slide-deck/scripts/build_deck.py \
  /home/node/.openclaw/skills/slide-deck/examples/outline.json \
  --out /tmp/slide-deck-test.pptx --pdf
```

輸出 `✅ PPTX` 和 `✅ PDF` 兩行，就代表環境都裝好了。
