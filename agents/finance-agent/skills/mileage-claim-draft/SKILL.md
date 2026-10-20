---
name: mileage-claim-draft
description: 流程 ① 步驟 6（controller 步驟 draft_claim）：依公務車里程差 × 交通費費率，產生交通費報帳草稿並用 flow-report 回報給 workflow controller。收到 `[workflow-controller 步驟指令]` 且 `skill: mileage-claim-draft`、`step: draft_claim` 時一定要用這個 skill。只產生草稿，不寫入任何系統、不核准任何金額。
---

# 交通費報帳草稿（mileage-claim-draft）

controller 在業務拜訪、admin-agent 登記完取車／還車里程之後，會派 `draft_claim` 這一步給你（finance-agent）。
你要做的只有一件事：**里程差 × 費率 → 報帳草稿 → 回報 controller**。草稿由 controller 保存，接著由業務主管核准；
主管核准前，任何系統都沒有正式金額。

計算、費率讀取、驗證、回報格式全部寫在 `scripts/mileage_claim.py`，**不要自己算、不要自己組回報**。
實作契約：`controller/backend/docs/flows/visit.md` 的「draft_claim」。

## 怎麼做（一步）

把收到的步驟指令**原文**存成檔案（或直接從 stdin 餵），用 `--report` 讓腳本算完直接呼叫共用 skill `flow-report`：

```bash
cat > /tmp/draft_claim_msg.txt <<'EOF'
<controller 的步驟指令全文，從 [workflow-controller 步驟指令] 到最後一行>
EOF
python3 ~/.openclaw/workspace-finance-agent/skills/mileage-claim-draft/scripts/mileage_claim.py \
  --message-file /tmp/draft_claim_msg.txt --report --agent finance-agent
```

腳本會從指令標頭取出 `flow_id`、`step`、`attempt`，從「輸入（JSON）：」取出輸入，算完後執行：

```bash
python3 ~/.openclaw/skills/flow-report/scripts/flow_report.py --flow <flow_id> --step draft_claim \
  --status ok|needs_human --summary "<一句話>" --output-file - --agent finance-agent
```

想先看結果不回報：加 `--dry-run`（不連網、不回報，即使同時帶了 `--report`）。

## 看結果

腳本只輸出一行 JSON：

| 欄位 | 意思 |
|---|---|
| `ok` | `true`＝腳本正常結束（含 `needs_human`）且（有 `--report` 時）回報成功 |
| `status` | `ok`（草稿產生）／`needs_human`（需要人處理，見下表） |
| `summary` | 回報給 controller 的一句話 |
| `output` | 回報給 controller 的 output |
| `reported` | 是否已經回報成功 |
| `report_result` | flow-report 的回應或錯誤 |

- exit 0：完成（`reported=true` 就結束，回覆一句 `summary` 即可）。
- exit 1：`--report` 回報失敗（flow-report 本身已重試網路錯誤）。不要重算重送第二份不同的結果；
  回覆「回報失敗：<error>」，由 admin 在 controller 按「重試這一步」。
- exit 2：指令格式不對（不是 `draft_claim`／`skill` 不是這個／找不到輸入 JSON）。**不回報**，回覆錯誤即可。

## 規則（腳本已實作，這裡說明給人看）

1. **費率只讀環境變數 `MILEAGE_RATE_TWD_PER_KM`**（由財會提供，不寫死在任何檔案）。
   - 沒設定、不是數字、≤ 0 或 > 100 → `status=needs_human`，`output.reason` 為 `rate_missing`／`rate_invalid`。**不得猜費率。**
   - 輸入的 `rate_env` 只接受 `MILEAGE_RATE_TWD_PER_KM`；其他名稱 → `needs_human`（`rate_env_unsupported`），不會去讀別的環境變數。
   - `MILEAGE_RATE_IS_PLACEHOLDER`：預設視為 **demo 假費率**（`rate_is_placeholder=true`，說明裡標「demo 假費率」）；
     財會提供正式費率後設成 `false` 才會拿掉標示。
2. **里程差** `distance_km` 必須是正整數且不超過 2000 km：
   - 0 或負數 → `needs_human`（`distance_not_positive`），不產生草稿。
   - 不是整數（含字串、小數、空值）→ `needs_human`（`distance_invalid`）；> 2000 → `needs_human`（`distance_too_large`）。
3. **金額＝里程差 × 費率，四捨五入到元**：0.5 一律進位（`ROUND_HALF_UP`，不是銀行家捨入）。
   例：37 km × NT$5 = NT$185；5 km × NT$4.5 = 22.5 → NT$23；3 km × NT$4.49 = 13.47 → NT$13。
4. **草稿標示**：`description` 開頭固定是「【草稿・待主管核准】」，結尾「此為草稿，金額待業務主管核准後才生效」；
   summary 也標「待主管核准」。（output 不放 `is_draft`／`approval_status`：主管核准後 controller 會把整份 output
   複製到 `state.claim` 並加上 `status=approved`，這類欄位會變成過時的矛盾資訊。）說明只含日期、客戶名稱、里程、費率、金額、業務、ADM 預約編號，
   **不含電話、地址**。
5. **主管退回重擬**（輸入 `manager_comment` 不是 null）：依目前的費率重算，並把主管意見寫進說明。
   里程本身有誤時，這一步無法更正（里程由 admin-agent 登記），請在回覆中說明需由 admin 處理。
6. 不寫入任何系統（沒有會計系統），不核准、不承諾任何金額。

## status=ok 時的 output（契約欄位）

| 欄位 | 型別 | 說明 |
|---|---|---|
| `distance_km` | int | 里程差 |
| `rate_twd_per_km` | number | 費率（整數費率輸出成 int） |
| `amount_twd` | int | 金額（四捨五入到元） |
| `rate_is_placeholder` | bool | 是不是 demo 假費率 |
| `description` | str | 報帳說明（草稿標示、日期、客戶、里程） |

另附選填欄位：`rounding`、`booking_id`、`manager_comment`。needs_human 時 output 為 `reason`（與可用時的 `distance_km`）。

## 兩步做法（沒有 --report 時）

不想讓腳本代為回報時，可以先算再回報：

```bash
python3 .../mileage_claim.py --message-file /tmp/draft_claim_msg.txt > /tmp/claim.json
# 取 /tmp/claim.json 的 status、summary、output，再用 flow-report 回報（output 存成檔案後 --output-file 指向它）
```

一般情況請用 `--report`，避免手動抄錯欄位。

## 環境變數

| 變數 | 說明 |
|---|---|
| `MILEAGE_RATE_TWD_PER_KM` | 交通費費率（NT$/km）。demo 假費率 5 |
| `MILEAGE_RATE_IS_PLACEHOLDER` | 選填，預設 `true`；正式費率設 `false` |
| `OPENCLAW_AGENT_ID` | 選填；沒帶 `--agent` 時用它，再預設 `finance-agent` |
| `FLOW_REPORT_SCRIPT` | 選填；flow_report.py 路徑，預設 `~/.openclaw/skills/flow-report/scripts/flow_report.py` |
| `FREEFORJOY_ENV_FILE` | 選填；secrets 檔，預設 `~/.openclaw/secrets/freeforjoy.env`（只補未設定的變數，不覆蓋） |

flow-report 自己需要 `FLOW_CONTROLLER_URL`、`FLOW_CONTROLLER_TOKEN_FINANCE_AGENT`。

## 測試

```bash
python3 -m unittest discover -s agents/finance-agent/skills/mileage-claim-draft/tests -v
```
