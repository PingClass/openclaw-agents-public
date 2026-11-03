# 大綱 JSON 格式

```json
{
  "title": "簡報名稱（不會顯示，只用來辨識）",
  "theme": "light",
  "accent": "0071E3",
  "slides": [ { "layout": "...", ... } ]
}
```

| 欄位 | 必填 | 說明 |
|---|---|---|
| `theme` | | `light`（白底，預設）或 `dark`（黑底） |
| `accent` | | 主色，6 碼 hex。預設是 Apple 藍 `0071E3`（深色主題是 `2997FF`） |
| `slides` | ✅ | 投影片陣列，順序就是頁序 |

每一頁都可以加：

- `notes`：講者備註，純文字，可以用 `\n` 換行。
- `kicker`：標題上方的小字分類標籤（`end`、`quote`、`image` 以外的版型才有）。

## 版型

### `title` 封面
```json
{"layout": "title", "kicker": "自然科學・單元五", "title": "光合作用",
 "subtitle": "植物如何把陽光變成養分", "byline": "七年級自然・第 5 課"}
```
必填 `title`。`subtitle`、`byline`（左下角小字，可以放作者、日期、課別）都是選填。

### `section` 章節頁
```json
{"layout": "section", "number": "01", "title": "植物需要什麼？", "subtitle": "從日常觀察開始"}
```
必填 `title`。`number` 會用大號主色字顯示。

### `bullets` 條列重點
```json
{"layout": "bullets", "title": "植物生長的四個條件",
 "bullets": ["陽光：提供能量", {"text": "水：從根部吸收", "sub": ["經由莖運送到葉子"]}]}
```
必填 `title`、`bullets`。bullet 可以是字串，也可以是 `{text, sub}`，`sub` 是第二層。最多 6 個。

### `statement` 一句話主張
```json
{"layout": "statement", "text": "光合作用，就是植物用光能製造葡萄糖。", "caption": "補充說明"}
```
必填 `text`。整頁只放這一句大字，用來強調最重要的概念。

### `two-column` 左右比較
```json
{"layout": "two-column", "title": "光合作用 vs. 呼吸作用",
 "left":  {"heading": "光合作用", "bullets": ["只在有光時進行"]},
 "right": {"heading": "呼吸作用", "bullets": ["白天晚上都進行"]}}
```
必填 `title`、`left`、`right`。兩欄各是一張淺灰底的卡片。

### `stats` 數字重點
```json
{"layout": "stats", "title": "地球上的光合作用",
 "stats": [{"value": "50%", "label": "大氣中的氧氣來自海洋藻類"}]}
```
必填 `stats`，1–4 個。`value` 要短，例如 `50%`、`3 倍`、`1.2 億`。`title` 選填。

### `steps` 流程步驟
```json
{"layout": "steps", "title": "光合作用的流程",
 "steps": [{"title": "吸收光能", "text": "葉綠素吸收紅光與藍光"}]}
```
必填 `title`、`steps`，2–5 個。編號會自動加上（01、02…）。

### `quote` 引言
```json
{"layout": "quote", "quote": "陽光是地球上幾乎所有生命的能量來源。", "author": "課本第 82 頁"}
```
必填 `quote`。引號會自動加上，不用自己寫。

### `image` 大圖
```json
{"layout": "image", "title": "顯微鏡下的葉片", "image": "images/image01.png", "caption": "圖說"}
```
必填 `image`。路徑可以是絕對路徑，或相對於 outline.json 所在的資料夾。圖片會等比例縮放、置中。

### `image-text` 左文右圖
```json
{"layout": "image-text", "title": "葉綠體在哪裡？", "image": "images/image02.png",
 "bullets": ["位在葉肉細胞裡"]}
```
必填 `title`、`image`、`bullets`。

### `table` 表格
```json
{"layout": "table", "title": "實驗紀錄表", "columns": ["組別", "光照"],
 "rows": [["A", "有"], ["B", "無"]]}
```
必填 `title`、`columns`、`rows`。最多 6 欄；超過 8 列會出現警告，建議拆頁。每一列的欄數要跟 `columns` 一樣。

### `end` 結尾
```json
{"layout": "end", "title": "謝謝大家", "subtitle": "下週：呼吸作用"}
```
必填 `title`。文字置中。
