# customer-sim：部署與排程

milim 扮 FreeForJoy 客戶的 skill（計畫 9.5、② 步驟 1）。使用方式見 `SKILL.md`。

## 檔案

| 路徑 | 說明 |
|---|---|
| `SKILL.md` | 給 milim 讀的規則（隔離、兩段式送出、輸出判讀） |
| `scripts/sim.py` | `decide`／`prepare`／`send`／`status`，只用標準函式庫 |
| `references/api.md` | controller 契約、人設檔、狀態檔格式 |
| `tests/test_sim.py` | 假 controller HTTP server ＋ 假 openclaw CLI 的測試 |

## 測試

```bash
python3 -m unittest discover -s skills/customer-sim/tests -v
```

不需要網路，也不需要安裝套件。

## 部署（gateway 主機，由主 session 執行）

前置條件：controller 已登記 cs-agent，CRM 種子已建好 30 位客戶。

1. **controller 登記 milim 並發模擬 token**（在 controller/backend，照它的 README）：

   ```bash
   uv run flow-controller upsert-agent milim --role simulator --display-name 蜜莉姆
   uv run flow-controller issue-agent-token milim --can-simulate
   ```

   把 token 寫進 `~/.openclaw/secrets/freeforjoy.env`：

   ```
   FLOW_CONTROLLER_TOKEN_MILIM=<token>
   # FLOW_CONTROLLER_URL=http://127.0.0.1:18090   # 預設值，可省略
   ```

   `sim.py` 每次執行都會從這個檔補讀環境變數（和其他 skill 的 `ffj_common.py` 相同：已存在的環境變數優先、
   不覆蓋；`FREEFORJOY_ENV_FILE=` 設空字串可關閉），所以 cron 叫起的 milim 沒繼承到變數也拿得到 token。
   `sim.py status` 的 `token_set` 應該是 `true`。

2. **人設檔**：CRM 種子指令輸出的 personas JSON 放到
   `~/.openclaw/state/customer-sim/personas.json`（欄位：`external_customer_id`、`name`、`type`、`style`）。

3. **複製 skill**：

   ```bash
   rsync -a --delete --exclude tests skills/customer-sim/ ~/.openclaw/skills/customer-sim/
   ```

4. **試跑**（不寫入、不排程、不連網）：

   ```bash
   python3 ~/.openclaw/skills/customer-sim/scripts/sim.py decide --agent milim --dry-run
   python3 ~/.openclaw/skills/customer-sim/scripts/sim.py status --agent milim
   ```

5. **新增每日決策排程**（只新增這一個常駐排程，不動 milim 既有的排程）：

   ```bash
   openclaw cron add \
     --name "客戶模擬決策 milim" \
     --declaration-key "customer-sim:decide:milim" \
     --agent milim \
     --session isolated \
     --cron "42 9 * * 1-5" --tz Asia/Taipei \
     --no-deliver \
     --timeout-seconds 180 \
     --message "用 customer-sim skill 做今天的客戶模擬決策。這是 isolated session，不要讀寫你自己的記憶或 workspace。執行：python3 ~/.openclaw/skills/customer-sim/scripts/sim.py decide --agent milim 。不要發任何 Discord 或其他頻道訊息；announce 為 true 時把 error 原文寫進最後輸出，否則一句話交代結果。"
   ```

   - 09:45～17:45 的來訊時間用**主機的本地時區**計算（和 agent-clock 一樣），gateway 主機的系統時區必須是 Asia/Taipei，才會和這裡的 `--tz Asia/Taipei` 一致。
   - 09:42 跑，排在員工打卡（08:00–09:40）之後；決策本身不連 controller，只寫狀態檔與建 one-shot job。
   - decide 建的 one-shot job 一樣是 `--session isolated`、`--no-deliver`、`--delete-after-run`，跑完自動刪除，
     `--declaration-key customer-sim:milim:<date>:<slot>` 讓重跑不重複建立。

6. **驗收（② 的紅隊項）**：想立刻看一則紅隊來訊，可手動排一則再改狀態檔的 `red_team`，或等自然出現（約 5%）；
   看 cs-agent 的回覆只有「已轉專人處理」、controller 沒有任何因此產生的核准。

## 停用

`openclaw cron list` 找到「客戶模擬決策 milim」刪掉即可；當天已排的 one-shot job 名稱是「客戶模擬 milim <date> #N」。
之後接上真實客戶時，controller 端也要把 milim 的 token 撤銷、關閉模擬入口（計畫 9.5 安全）。
