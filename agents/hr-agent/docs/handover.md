# hr-agent 交接表

> 對應課程單元 6（U6p22）。下表已先填入本 repo 的現況，部署與測試後請更新。

| 欄位 | 內容 |
|---|---|
| 原型說明 | 給全體員工用的人資助理：依員工手冊回答請假、加班等規定並附條號，協助填申請草稿；不核准、不談薪資。 |
| 操作方式 | 掃 LINE 官方帳號 QR code 加好友 → 送出 pairing 代碼由管理者核准 → 直接提問。示範問句：「新人的特休有幾天？」（hr-policy-qa）、「幫我填 11/3 下午的特休假單」（leave-request-draft）。 |
| 已完成的功能 | skills：`hr-policy-qa`、`leave-request-draft`、`meeting-summary`；cron：每月 25 日 09:00 加班單提醒（`automations/overtime-reminder.md`，預設停用，送達 LINE 的方式待實測）。 |
| 測試集結果 | 待測（`tests/test-cases.md`，共 10 題） |
| 建置時遇到的問題 | 待補 |
| 待改善項目 | 1. 串接人事系統 API（查假別餘額、建立草稿）　2. 確認 cron 結果送到 LINE 的寫法　3. 換成真實規章（去識別化後） |
| 已知風險 | 規章外的問題可能被模型用常識補答；越權與冒充題需每次改版重測；API 未連線前無法查個人資料。 |
