# 排程：每月 25 日加班單提醒

> 對應課程單元 6（cron）。依 `docs/hr-policy.md` 第 19 條：當月加班須於每月 25 日前完成申請。

## 建立（先停用）

```bash
openclaw automations create "0 9 25 * *" \
  "檢查本月尚未送出加班單的員工，列出名單與缺少的日期；沒有人缺就回覆『無異常』並結束。不要核准任何申請，不要列出薪資或加班費金額。" \
  --name "hr 每月 25 日加班單提醒" \
  --tz "Asia/Taipei" \
  --session isolated \
  --disabled
```

## 驗證後才啟用

```bash
openclaw automations list --all          # 找到任務 id
openclaw automations run <任務id>         # 手動跑一次，看輸出
openclaw automations runs --id <任務id>   # 看執行紀錄
# 確認內容正確、沒有洩漏個資後，才啟用；不要就 disable / remove
openclaw automations disable <任務id>
openclaw automations remove <任務id>
```

## 原則（U6p13）

- 一定加 `--tz`。
- 先 `--disabled`，手動 `run` 確認後再啟用。
- 沒事時預設安靜（回「無異常」就結束）。
- 對外發送要人確認：提醒訊息發給員工前，第一次先送給 HR 自己看。

## 尚待確認

- **人事系統 API 未連線前**，Agent 查不到「誰還沒送加班單」，這個排程只能提醒 HR「今天是加班單結算日」，不能點名。
- **結果怎麼送到 LINE**：課程標為「課前待實測」，`--announce --channel line --to <userId>` 的寫法尚未實測（未查證）。
