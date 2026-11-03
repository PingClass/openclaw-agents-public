---
name: pdf-split
description: 依頁數範圍把一份 PDF 分割成多個檔案，例如「1-3 頁一份、4-10 頁一份」或「每 5 頁切一份」。只要使用者要拆 PDF、切 PDF、抽出某幾頁、分割講義或考卷，就用這個 skill，不要自己寫 PDF 處理程式。
metadata: { "openclaw": { "requires": { "bins": ["python3"] } } }
---

# PDF 分割

分割一律交給 `split_pdf.py`。不要自己寫 pypdf 程式碼。

腳本一律用下面的**絕對路徑**執行（相對路徑、`~`、`{baseDir}` 字面值可能被 exec 白名單拒絕）：

- `/home/node/.openclaw/skills/pdf-split/scripts/split_pdf.py`

## 流程

### 1. 確認 PDF 和總頁數

使用者傳來的檔案路徑直接拿來用。先查總頁數：

```bash
python3 /home/node/.openclaw/skills/pdf-split/scripts/split_pdf.py <PDF 路徑> --info
```

### 2. 把需求換成範圍

| 使用者說 | 參數 |
|---|---|
| 「1 到 3 頁一份，4 到 7 頁一份，剩下一份」 | `--ranges "1-3,4-7,8-"` |
| 「只要第 5 頁」 | `--ranges "5"` |
| 「從第 10 頁到最後」 | `--ranges "10-end"` |
| 「每 5 頁切一份」 | `--every 5` |
| 「每一頁都拆開」 | `--every 1` |
| 「切成兩半」 | 用總頁數算出中點，例如 12 頁 → `--ranges "1-6,7-12"` |

- 頁碼從 1 開始，和 PDF 閱讀器顯示的一樣。
- 使用者說的是「第幾章」「考卷第二部分」這種內容位置，而不是頁碼時，先問他是哪幾頁，不要自己猜。
- 範圍可以不連續，也可以重疊，照使用者說的給就好。

### 3. 執行

```bash
python3 /home/node/.openclaw/skills/pdf-split/scripts/split_pdf.py <PDF 路徑> \
  --ranges "1-3,4-7,8-" \
  --out-dir /home/node/.openclaw/workspace/pdf-split/<slug>
```

`<slug>` 用原檔名的簡短英文或拼音，例如 `unit5-handout`。輸出檔名是 `<原檔名>_part1.pdf`、`_part2.pdf`……，順序和範圍順序相同。

### 4. 看輸出、修正

| 輸出 | 意思 | 處理 |
|---|---|---|
| `✅ <路徑> (頁 a-b)` | 成功，一份一行 | 記下路徑，交件用 |
| `❌ 範圍有錯` (exit 2) | 頁碼超出、起始大於結束、格式不對 | 照訊息修正範圍再跑；超出頁數時告訴使用者這份只有幾頁 |
| `❌ 檔案不存在 / 不是 PDF / 無法讀取` (exit 3) | 路徑錯或檔案壞了 | 跟使用者確認檔案，不要換別的檔案亂試 |
| `❌ 這份 PDF 有開檔密碼` (exit 3) | 加密檔 | 告訴使用者需要先解除密碼 |
| `❌ 輸出檔已存在` (exit 4) | 之前切過同一份 | 使用者要重切就加 `--overwrite`；不確定就換一個 `--out-dir` |

### 5. 交付

用 message tool 的 `mediaUrls` 附上所有輸出檔的路徑。如果只能用文字回覆，就在回覆最後每個檔案各放一行獨立的 `MEDIA:/絕對路徑`。不要加粗，也不要包在 code block 裡。

回覆內容要短：切成幾份，每份是哪幾頁。
