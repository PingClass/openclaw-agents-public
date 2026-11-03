---
name: slide-deck
description: 依使用者的需求描述、Markdown 檔或 Word（.docx）檔產生 Apple 風格的投影片，回傳可編輯的 .pptx，需要時一併附上 PDF。只要使用者要做簡報、投影片、slides、deck、PPT，或要把教案、講稿、文件轉成投影片，就用這個 skill，不要自己用其他方式拼 pptx，也不要上傳到 Google Drive。
metadata: { "openclaw": { "requires": { "bins": ["python3"] } } }
---

# 投影片產生器

你負責**內容**：讀懂素材、規劃敘事、寫出一份大綱 JSON。
**排版**全部交給 `build_deck.py`：它決定字級、字距、留白、配色，每次產出風格一致。
不要自己寫 python-pptx 程式碼，也不要在大綱裡放座標、顏色或字級。

腳本一律用下面的**絕對路徑**執行（相對路徑、`~`、`{baseDir}` 字面值可能被 exec 白名單拒絕）：

- `/home/node/.openclaw/skills/slide-deck/scripts/docx2md.py`
- `/home/node/.openclaw/skills/slide-deck/scripts/build_deck.py`

## 流程

### 1. 取得素材

| 使用者給的 | 你要做的 |
|---|---|
| 一段需求描述 | 直接規劃。缺主題、對象或頁數這類關鍵資訊時，先問一句再做 |
| `.md` 檔 | 讀檔內容 |
| `.docx` 檔 | 先轉成 Markdown，圖片會一起存出來 |

```bash
python3 /home/node/.openclaw/skills/slide-deck/scripts/docx2md.py 教案.docx \
  --images-dir /home/node/.openclaw/workspace/slides/<slug>/images
```

`<slug>` 用簡短英文或拼音當這份簡報的資料夾名稱，例如 `unit5-photosynthesis`。

### 2. 規劃敘事（先想清楚再寫 JSON）

- **先定一句核心訊息**：聽眾聽完這份簡報要記得什麼。
- 再排順序：開場 → 章節 → 每個重點一頁 → 收尾。
- 素材很長時是**濃縮**，不是照抄。一份文件一般整理成 8–20 頁。
- 使用者有指定頁數、語氣或對象時，照他說的做。

### 3. 寫大綱 JSON

存到 `/home/node/.openclaw/workspace/slides/<slug>/outline.json`。
完整欄位見 `references/outline-schema.md`，第一次用先讀一遍。範例在 `examples/outline.json`。

Apple 風格的寫法：

- **一頁只講一件事。** 標題寫成一句主張，例如「葉綠素負責吸收光能」，不要只寫「葉綠素」。標題控制在 20 字以內。
- **字越少越好。** 一頁最多 6 個重點，每個重點不超過 25 字。說明、例子、提問都寫進 `notes`（講者備註），不要塞在投影片上。
- **版型要有變化。** 不要整份都用 `bullets`：
  - 最重要的一句話 → `statement`
  - 有數字 → `stats`
  - 有先後順序 → `steps`
  - 兩件事比較 → `two-column`
  - 有圖 → `image` 或 `image-text`
- `kicker` 是標題上方的小字分類標籤，用來標「觀察」「核心概念」「練習」這類分類。不必每頁都放。
- 一律使用**繁體中文**與台灣用語。不要用 emoji。
- 圖片只能用使用者提供的，或 docx 轉出來的。不要自己編造圖片路徑。

### 4. 產生檔案

```bash
python3 /home/node/.openclaw/skills/slide-deck/scripts/build_deck.py \
  /home/node/.openclaw/workspace/slides/<slug>/outline.json \
  --out /home/node/.openclaw/workspace/slides/<slug>/<檔名>.pptx --pdf
```

- 使用者只要 pptx 時，拿掉 `--pdf`。
- 深色主題：在大綱最外層加 `"theme": "dark"`。
- 換主色：加 `"accent": "34C759"`（6 碼 hex）。

### 5. 看輸出、修正

| 輸出 | 意思 | 處理 |
|---|---|---|
| `❌ 大綱有錯` (exit 2) | JSON 格式或欄位不對 | 照每條錯誤修正大綱再跑，不要跳過 |
| `⚠️ 第 N 頁：…太長` | 字太多，可能超出版面 | 縮短文字或拆成兩頁，再跑一次 |
| `⚠️ 找不到 LibreOffice` (exit 3) | 環境沒裝 PDF 轉檔工具，pptx 已經產出 | 只交 pptx，並告訴使用者 PDF 這次產不出來 |

警告都處理完才交件。

### 6. 交付

用 message tool 的 `mediaUrls` 附上檔案路徑，pptx 和 PDF 都附。如果只能用文字回覆，就在回覆最後各放一行獨立的 `MEDIA:/絕對路徑`。不要加粗，也不要包在 code block 裡。

回覆內容要短：

- 一句說明這份簡報的核心訊息
- 頁數
- 用了哪些版型，或你做了哪些取捨，例如「原文第三節偏離主題，沒放進簡報」
- 提醒使用者講者備註裡有什麼

不要把整份大綱貼回對話。
