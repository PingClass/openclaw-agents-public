#!/usr/bin/env python3
"""流程 ③ 步驟 draft_laptop：查還沒有保管人的筆電，依新人職務產生配發建議（不指派）。

契約：controller/backend/docs/flows/onboarding.md 的「draft_laptop」。

    python3 laptop_draft.py --instruction-file <步驟指令檔> --report

- ADM A3：`GET /api/v1/assets/?category=laptop&unassigned=true&is_active=true`，
  token 是 it-agent 的 `ADM_API_TOKEN_IT`（scope 只有 assets:read）。
- **只讀、只產生建議**：這支腳本只送 GET，沒有任何呼叫指派 API（A4）的程式碼。
  指派保管人僅限 IT 人員以 session 登入 ADM 操作（A4 對任何 agent token 一律 403）。
- 挑選規則：依新人部門與職稱判斷用途（開發／業務／文書），名稱符合用途的筆電優先，
  同一類依資產編號排序；其他可配發的筆電依序列為備選（最多 5 台）。
- 沒有任何未指派的筆電 → status=needs_human。
- 連線失敗、5xx → status=failed、error_kind=system（controller 重試同一步）。
- 401／403（token 或 scope 有問題）、400 → status=needs_human，summary 帶 ADM 原文。
"""

from __future__ import annotations

import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import adm_flow as f  # noqa: E402

STEP = "draft_laptop"
AGENT = "it-agent"
TOKEN_ENV = "ADM_API_TOKEN_IT"
QUERY = {"category": "laptop", "unassigned": "true", "is_active": "true"}
MAX_ALTERNATIVES = 5

# 用途 → (判斷新人職務的關鍵字, 判斷筆電名稱的關鍵字, 說明)
PROFILES = (
    ("dev", ("資訊", "工程", "開發", "程式", "設計", "IT", "RD", "研發", "資料"),
     ("開發", "工作站", "Pro", "16 吋", "16吋", "32GB"), "開發用的高規格筆電"),
    ("sales", ("業務", "行銷", "客戶", "門市"),
     ("業務", "15 吋", "15吋", "輕薄"), "業務外出用的筆電"),
    ("office", (), ("文書", "14 吋", "14吋", "Air"), "一般文書用的筆電"),
)


def has_word(word: str, text: str) -> bool:
    """中文關鍵字用子字串比對；英數關鍵字（IT、RD、Pro、Air）要是獨立的字，
    避免 Recruiter 被當成 IT、Standard 被當成 RD、Chair 被當成 Air。"""
    if word.isascii():
        return re.search(rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])", text, re.IGNORECASE) is not None
    return word.lower() in text.lower()


def profile_for(new_hire: dict) -> tuple:
    """依部門與職稱決定用途；都沒對到就當文書。"""
    text = f"{new_hire.get('department_name') or ''} {new_hire.get('job_title') or ''}"
    for profile in PROFILES:
        if any(has_word(word, text) for word in profile[1]):
            return profile
    return PROFILES[-1]


def _code(asset: dict) -> str:
    return str(asset.get("code") or "")


def usable(assets) -> list:
    """再檢查一次 ADM 回來的資料：筆電、啟用中、沒有保管人（防止篩選參數被忽略時誤配）。"""
    rows = []
    for asset in assets if isinstance(assets, list) else []:
        if not isinstance(asset, dict) or not _code(asset):
            continue
        if str(asset.get("category") or "").lower() != "laptop":
            continue
        if asset.get("is_active") is False:
            continue
        if asset.get("custodian") is not None or asset.get("custodian_hrm_employee_id"):
            continue
        rows.append(asset)
    return rows


def rank(assets: list, profile: tuple) -> list:
    """符合用途的在前，其次依資產編號。"""
    words = profile[2]

    def fits(asset: dict) -> bool:
        name = str(asset.get("name") or "")
        return any(has_word(word, name) for word in words)

    return sorted(assets, key=lambda a: (0 if fits(a) else 1, _code(a))), fits


def specs_text(asset: dict) -> str:
    parts = [str(asset.get("name") or "筆電")]
    if asset.get("serial_number"):
        parts.append(f"序號 {asset['serial_number']}")
    if asset.get("location"):
        parts.append(f"存放於{asset['location']}")
    return "，".join(parts)


def handle(ctx: dict, args) -> dict:
    data = ctx["input"]
    if data is None:
        return f.result("needs_human", "步驟指令沒有輸入（JSON），無法產生配發建議")
    new_hire = data.get("new_hire")
    if not isinstance(new_hire, dict) or not new_hire.get("name"):
        return f.result("needs_human", "輸入缺少 new_hire（新人資料），無法產生配發建議")
    path = f"/assets/?{urllib.parse.urlencode(QUERY)}"
    who = f"{new_hire['name']}（{new_hire.get('department_name') or '部門未提供'}"
    who += f"，{new_hire['job_title']}）" if new_hire.get("job_title") else "）"
    profile = profile_for(new_hire)

    if args.dry_run:
        return f.result("ok", f"（試跑）會查未指派的筆電，替 {who} 挑一台{profile[3]}",
                        {"would_query": f"GET {f.adm_base()}{path}", "profile": profile[0]})

    adm = f.adm_client(TOKEN_ENV, AGENT)
    status, body = adm.request("GET", path)
    if status >= 500 or status == 429:
        return f.result("failed", f"ADM 查筆電回 {status}：{f.detail_text(body)}", error_kind="system")
    if status != 200 or not isinstance(body, list):
        return f.result("needs_human", f"ADM 查筆電失敗（HTTP {status}）：{f.detail_text(body)}",
                        {"http_status": status})
    assets = usable(body)
    if not assets:
        return f.result("needs_human",
                        f"ADM 目前沒有未指派、啟用中的筆電，無法替 {who} 產生配發建議；"
                        "請 IT 人員採購或回收筆電後，在流程詳情重試這一步",
                        {"available_count": 0})
    ordered, fits = rank(assets, profile)
    best = ordered[0]
    if fits(best):
        reason = f"{who}適合{profile[3]}，{_code(best)}（{best.get('name') or '筆電'}）目前沒有保管人"
    else:
        reason = (f"{who}適合{profile[3]}，但目前沒有這類筆電；先建議未指派的 {_code(best)}"
                  f"（{best.get('name') or '筆電'}），IT 可依實際需求改用備選")
    output = {
        "asset_code": _code(best),
        "asset_name": str(best.get("name") or _code(best)),
        "specs": specs_text(best),
        "reason": reason,
        "alternatives": [_code(a) for a in ordered[1:1 + MAX_ALTERNATIVES]],
        "available_count": len(assets),
    }
    alt = f"，備選 {'、'.join(output['alternatives'][:3])}" if output["alternatives"] else ""
    return f.result("ok", f"建議配發 {output['asset_code']}（{output['asset_name']}）給 {new_hire['name']}{alt}"
                          "；指派由 IT 人員在 ADM 操作", output)


def main(argv=None) -> None:
    f.run_step(STEP, "流程 ③ draft_laptop：查未指派筆電，產生配發建議（不指派）", handle, argv,
               default_agent=AGENT)


if __name__ == "__main__":
    main()
