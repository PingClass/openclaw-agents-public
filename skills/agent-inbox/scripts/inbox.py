#!/usr/bin/env python3
"""員工 agent 扮演確認者：處理 controller 收件匣裡的人工確認（計畫 9.4）。

  check   讀自己的收件匣（GET /api/inbox，員工 agent 的 controller token），
          對還沒處理的確認依角色規則決定，用通知連結裡的一次性 token 送出
  status  不連網：從狀態檔統計近 N 天（--days，預設 7）各分類的核准／退回（／轉人工）比例，
          和規則的預期比例並列，方便觀察擲骰結果（不改決定規則）

決定規則：
  - 業務（mjolmire-businessman、gobta）對派車草稿：90% 核准
  - 業務主管（main）對報帳草稿：金額 > NT$500 時 15% 退回，其餘核准
  - 客服主管（hinata）對退款決定（流程 ② decide_refund）：80% 同意退款（用推估金額）、10% 不退款、10% 轉人工
  - 客服主管（hinata）對回覆客戶內容（流程 ② review_reply）：90% 核准送出、10% 退回重擬
  - 主管對新人到職清單（流程 ③）：一律「確認收到」
  - 其他確認：95% 核准

admin 在 controller 前端把確認者切成「真人確認」時（GET /api/inbox/confirm-mode 回 human），
什麼都不做（outcome=paused），由真人在前端確認；本機也可以用 PAUSED 檔或 AGENT_INBOX_PAUSED 暫停。

收件匣裡 kind=task 的通知（例如給 IT 人員的「配發筆電」任務）不在這裡處理，只列在輸出的 tasks，
由負責的 skill（souei：agent-adm assign-laptop）處理。

決定在送出前就寫進狀態檔（~/.openclaw/state/agent-inbox/<agent>/handled.json），
重跑不會重擲骰，處理過的確認也不會再處理。
一律輸出單一行 JSON；成功 exit 0，失敗 exit 1。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.parse
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ffj_common as c  # noqa: E402

SKILL = "agent-inbox"
KEEP_DAYS = 30

DISPATCH_APPROVE = 0.90
EXPENSE_REJECT_OVER_500 = 0.15
OTHER_APPROVE = 0.95
EXPENSE_THRESHOLD = 500
REFUND_APPROVE = 0.80
REFUND_REJECT = 0.90  # 0.80～0.90 不退款，其餘轉人工
REPLY_APPROVE = 0.90

DISPATCH_WORDS = ("派車", "公務車", "用車", "車輛", "取車")
EXPENSE_WORDS = ("報帳", "交通費", "里程費", "費用", "核銷")
ONBOARDING_WORDS = ("到職", "新人")
APPROVE_VALUES = ("approve", "approved", "confirm", "confirmed", "accept", "yes", "ok", "ack")
APPROVE_LABELS = ("核准", "確認", "同意", "收到", "可以")
REJECT_VALUES = ("reject", "rejected", "deny", "decline", "no", "return", "revise")
REJECT_LABELS = ("退回", "拒絕", "不核准", "不同意", "駁回")
ESCALATE_VALUES = ("escalate", "handoff", "human")
ESCALATE_LABELS = ("轉人工", "專人")
# 流程 ② 的兩種確認（controller docs/flows/refund.md）
REFUND_DECISION_WORDS = ("決定退款", "退款決定")
REPLY_REVIEW_WORDS = ("確認回覆", "回覆客戶")

COMMENTS = {
    ("dispatch", "approve"): (
        "車子和時段都沒問題，就照這個安排，我當天會準時取車。",
        "這台車的座位夠用，時間也對得上，麻煩幫我訂。",
        "謝謝幫忙安排，路線和時間都 OK，就這樣訂吧。",
    ),
    ("dispatch", "reject"): (
        "客戶那邊剛說時間可能要調整，這次派車先退回，我確認好再重新安排。",
        "這個時段我可能趕不上，先不用訂車，之後再麻煩你重排。",
    ),
    ("expense", "approve"): (
        "里程和金額都對得上，NT${amount} 這筆核准。",
        "看過了，這趟拜訪的交通費 NT${amount} 沒問題。",
        "金額合理，NT${amount} 核准，辛苦了。",
    ),
    ("expense", "reject"): (
        "這筆 NT${amount} 的交通費想再確認一下里程紀錄，麻煩補上說明後再送一次。",
        "NT${amount} 比我預期的高一些，先退回，請附上這趟的路線再送。",
    ),
    ("onboarding", "approve"): (
        "收到到職清單了，我會照清單安排新人第一週的工作。",
        "清單看過了，新人到職當天我會到說明會議室帶他認識團隊。",
        "收到，謝謝人資和 IT 的準備，新人報到後我來接手。",
    ),
    ("other", "approve"): (
        "內容確認過了，沒有問題。",
        "收到，就照這樣進行。",
        "看過了，可以，謝謝。",
    ),
    ("other", "reject"): (
        "這部分我想再多了解一下，先退回，補充說明後再送給我。",
    ),
    ("refund_decision", "approve"): (
        "看過評估，依建議金額退款，請盡快處理並跟客戶說明入帳時間。",
        "客戶的情況屬實，同意退款，金額照財會的推估。",
    ),
    ("refund_decision", "reject"): (
        "依目前的資料不符合退款條件，這次不退款，回覆時請委婉說明原因。",
    ),
    ("refund_decision", "escalate"): (
        "這個案子情況比較複雜，我親自跟客戶聯絡處理。",
    ),
    ("reply_review", "approve"): (
        "回覆內容清楚、語氣也得體，可以送出。",
        "看過了，就照這個版本回覆客戶。",
    ),
    ("reply_review", "reject"): (
        "語氣再誠懇一點，並說明清楚後續的處理時間，麻煩重擬。",
    ),
}


# --- 規則 ---------------------------------------------------------------


def flatten_text(*parts) -> str:
    out = []

    def visit(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                out.append(str(k))
                visit(v)
        elif isinstance(obj, list):
            for v in obj:
                visit(v)
        elif obj is not None:
            out.append(str(obj))

    for part in parts:
        visit(part)
    return " ".join(out)


def classify(view: dict, notification: dict) -> str:
    """派車草稿／報帳草稿／退款決定／回覆確認／到職清單／其他。以確認頁的標題、問題、選項、內容判斷。"""
    head = flatten_text(view.get("step_title"), view.get("question"), notification.get("body"))
    option_values = {str(opt.get("value", "")).lower() for opt in view.get("options") or []}
    if any(w in head for w in REFUND_DECISION_WORDS) or option_values & set(ESCALATE_VALUES):
        return "refund_decision"
    if any(w in head for w in REPLY_REVIEW_WORDS):
        return "reply_review"
    full = flatten_text(head, view.get("flow_title"), view.get("context"), notification.get("title"))
    if any(w in flatten_text(view.get("step_title"), view.get("question")) for w in ONBOARDING_WORDS):
        return "onboarding"
    for text in (head, full):
        if any(w in text for w in EXPENSE_WORDS):
            return "expense"
        if any(w in text for w in DISPATCH_WORDS):
            return "dispatch"
    return "other"


AMOUNT_KEYS = ("amount", "amount_twd", "total", "total_twd", "金額", "報帳金額", "交通費")
AMOUNT_IN_TEXT = re.compile(r"(?:NT\$|NTD|新台幣)\s*([\d,]+(?:\.\d+)?)|([\d,]+(?:\.\d+)?)\s*元")


def extract_amount(view: dict) -> float | None:
    def numeric(value):
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            cleaned = value.replace(",", "").replace("NT$", "").replace("元", "").strip()
            try:
                return float(cleaned)
            except ValueError:
                return None
        return None

    def search(obj, depth=0):
        if depth > 5:
            return None
        if isinstance(obj, dict):
            for k, v in obj.items():
                if str(k).lower() in AMOUNT_KEYS:
                    got = numeric(v)
                    if got is not None:
                        return got
            for v in obj.values():
                got = search(v, depth + 1)
                if got is not None:
                    return got
        elif isinstance(obj, list):
            for v in obj:
                got = search(v, depth + 1)
                if got is not None:
                    return got
        return None

    found = search(view.get("context"))
    if found is not None:
        return found
    # 問題文字裡常同時有費率與總額（「37 km × NT$5/km = NT$185」），取最大的那個當報帳金額
    amounts = []
    for match in AMOUNT_IN_TEXT.finditer(flatten_text(view.get("question"), view.get("context"))):
        try:
            amounts.append(float((match.group(1) or match.group(2)).replace(",", "")))
        except ValueError:
            continue
    return max(amounts) if amounts else None


def find_option(options: list, values: tuple, labels: tuple) -> str | None:
    for opt in options:
        if str(opt.get("value", "")).lower() in values:
            return opt["value"]
    for opt in options:
        label = str(opt.get("label", ""))
        if any(word in label for word in labels):
            # 「不核准」也含「核准」：核准類要排除否定詞
            if labels is APPROVE_LABELS and any(neg in label for neg in ("不", "退", "拒")):
                continue
            return opt["value"]
    return None


def decide(role: str, category: str, amount: float | None, roll: float) -> tuple:
    """回傳 (approve: bool, rule)。roll 是 0～1 的亂數，事先擲好存進狀態檔。"""
    if category == "onboarding":
        # 流程 ③ 主管確認收到到職清單：一律確認收到（缺漏由真人主管回報）
        return True, "onboarding_ack"
    if role == "sales" and category == "dispatch":
        return roll < DISPATCH_APPROVE, "sales_dispatch_90"
    if role == "sales_manager" and category == "expense":
        if amount is not None and amount > EXPENSE_THRESHOLD:
            return roll >= EXPENSE_REJECT_OVER_500, "manager_expense_over_500_reject_15"
        return True, "manager_expense_approve"
    if role == "cs_manager" and category == "reply_review":
        return roll < REPLY_APPROVE, "cs_manager_reply_90"
    return roll < OTHER_APPROVE, "other_95"


def refund_action(role: str, roll: float) -> tuple:
    """退款決定是三選一：回傳 (action, rule)，action 為 approve／reject／escalate。"""
    if role != "cs_manager":
        approve, rule = decide(role, "other", None, roll)
        return ("approve" if approve else "reject"), rule
    if roll < REFUND_APPROVE:
        return "approve", "cs_manager_refund_80_10_10"
    if roll < REFUND_REJECT:
        return "reject", "cs_manager_refund_80_10_10"
    return "escalate", "cs_manager_refund_80_10_10"


def refund_has_default_amount(view: dict) -> bool:
    """確認頁的 amount_twd 欄位有沒有大於 0 的預設值（＝財會推估金額）。

    同意退款時 inbox 不帶金額，controller 用推估金額；推估是 0 時 controller 會回 422。
    舊版確認頁沒有 fields 時視為有（沿用原本行為）。
    """
    fields = view.get("fields")
    if not isinstance(fields, list) or not fields:
        return True
    for field in fields:
        if isinstance(field, dict) and field.get("name") == "amount_twd":
            default = field.get("default")
            return isinstance(default, (int, float)) and not isinstance(default, bool) and default > 0
    return True


def make_comment(category: str, approve: bool, amount: float | None, action: str | None = None) -> str:
    bucket = "expense" if category == "expense" and amount is not None else (
        "other" if category == "expense" else category)
    templates = COMMENTS[(bucket, action or ("approve" if approve else "reject"))]
    text = c.RNG.choice(templates)
    if amount is not None:
        text = text.replace("{amount}", f"{amount:,.0f}")
    return text


# --- 統計（status）-------------------------------------------------------

CATEGORY_LABELS = {
    "dispatch": "派車草稿",
    "expense": "報帳草稿",
    "refund_decision": "退款決定",
    "reply_review": "回覆確認",
    "onboarding": "到職清單",
    "other": "其他確認",
}
ACTION_LABELS = {"approve": "核准", "reject": "退回", "escalate": "轉人工"}
STATUS_DAYS = 7


def record_action(record: dict) -> str:
    """approve／reject／escalate。新紀錄有 action 欄位；舊紀錄由 approve 與送出的選項值推回來。"""
    action = record.get("action")
    if action in ACTION_LABELS:
        return action
    if record.get("approve"):
        return "approve"
    if str(record.get("decision", "")).lower() in ESCALATE_VALUES:
        return "escalate"
    return "reject"


def expected_rates(role: str, category: str) -> dict | None:
    """這個角色對這一類確認，規則預期的比例（報帳看金額，沒有單一比例 → None）。"""
    if category == "onboarding":
        return {"approve": 1.0}
    if category == "refund_decision":
        if role == "cs_manager":
            return {"approve": REFUND_APPROVE, "reject": round(REFUND_REJECT - REFUND_APPROVE, 4),
                    "escalate": round(1 - REFUND_REJECT, 4)}
        return {"approve": OTHER_APPROVE, "reject": round(1 - OTHER_APPROVE, 4)}
    if role == "sales_manager" and category == "expense":
        return None
    if role == "sales" and category == "dispatch":
        rate = DISPATCH_APPROVE
    elif role == "cs_manager" and category == "reply_review":
        rate = REPLY_APPROVE
    else:
        rate = OTHER_APPROVE
    return {"approve": rate, "reject": round(1 - rate, 4)}


def record_day(record: dict) -> str:
    """紀錄算在哪一天：送出的時間（decided_at），沒有就用第一次看到的日期。"""
    decided_at = str(record.get("decided_at") or "")
    return decided_at[:10] if len(decided_at) >= 10 else str(record.get("first_seen") or "")


def build_stats(role: str, handled: dict, today: date, days: int) -> dict:
    since = (today - timedelta(days=days - 1)).isoformat()
    until = today.isoformat()
    by_category: dict = {}
    pending = closed = 0
    for record in handled.values():
        if not isinstance(record, dict):
            continue
        day = record_day(record)
        if not (since <= day <= until):
            continue
        if record.get("status") == "pending":
            pending += 1
            continue
        if record.get("status") != "decided":
            closed += 1  # 別人先處理了、連結過期：不算進比例
            continue
        category = record.get("category") or "other"
        row = by_category.setdefault(category, {"label": CATEGORY_LABELS.get(category, category), "decided": 0,
                                                "approve": 0, "reject": 0, "escalate": 0, "rolls": []})
        row["decided"] += 1
        row[record_action(record)] += 1
        if isinstance(record.get("roll"), (int, float)):
            row["rolls"].append(record["roll"])
    for category, row in by_category.items():
        for action in ACTION_LABELS:
            row[f"{action}_rate"] = round(row[action] / row["decided"], 4)
        expected = expected_rates(role, category)
        if expected:
            row["expected"] = expected
    return {"days": days, "since": since, "until": until, "by_category": by_category,
            "pending": pending, "closed": closed}


def stats_text(stats: dict) -> str:
    parts = []
    for row in sorted(stats["by_category"].values(), key=lambda r: -r["decided"]):
        counts = "／".join(f"{ACTION_LABELS[a]} {row[a]}" for a in ACTION_LABELS if row[a] or a != "escalate")
        text = f"{row['label']} {counts}（{row['decided']} 件，核准率 {row['approve_rate']:.0%}"
        if row.get("expected"):
            text += f"，預期 {row['expected']['approve']:.0%}"
        parts.append(text + "）")
    head = f"近 {stats['days']} 天"
    if not parts:
        return f"{head}沒有送出過的確認"
    return f"{head}：" + "；".join(parts)


def cmd_status(agent: str, today: date, args, result: dict) -> bool:
    days = args.days
    if days < 1 or days > KEEP_DAYS:
        raise c.ConfigError(f"--days 要在 1～{KEEP_DAYS} 之間（狀態檔只保留 {KEEP_DAYS} 天）")
    role = c.AGENTS[agent]["role"]
    data = c.load_json(c.state_path(SKILL, agent, "handled.json"))
    stats = build_stats(role, data.get("approvals") or {}, today, days)
    result.update(role=role, outcome="status", paused=is_paused(agent), stats=stats, detail=stats_text(stats))
    return True


# --- controller ---------------------------------------------------------


class Controller:
    def __init__(self, agent: str):
        self.agent = agent
        self.http = c.HttpClient(c.controller_base(), "controller",
                                 {"Authorization": f"Bearer {c.controller_token(agent)}"})

    def get(self, path: str):
        return self.http.request("GET", f"/api{path}")

    def post(self, path: str, body: dict | None = None):
        # 一次性 token：只在連線根本沒建立時重試；回應遺失的話重跑會拿到 410，視為已結束
        return self.http.request("POST", f"/api{path}", body, idempotent=False)


def token_from_link(link: str | None) -> str | None:
    if not link:
        return None
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
    values = query.get("token")
    return values[0] if values else None


def prune(handled: dict, today: date) -> None:
    cutoff = (today - timedelta(days=KEEP_DAYS)).isoformat()
    for key in [k for k, v in handled.items() if (v.get("first_seen") or "9999") < cutoff]:
        handled.pop(key, None)


def confirm_mode(ctl: "Controller") -> str | None:
    """controller 上這位確認者目前由誰確認（admin 在前端切換）：agent／human；查不到回 None。

    舊版 controller 沒有這個端點（404 not_found）、或暫時查不到時回 None，照舊處理：
    controller 在 human 時本來就不給 agent 確認連結，所以不會誤代確認。
    """
    status, body = ctl.get("/inbox/confirm-mode")
    if status in (401, 403):
        raise c.ServiceError(f"controller 拒絕查詢確認方式 {status}: {c.short(body)}", announce=True,
                             kind="inbox_denied")
    if status == 404 and c.error_code(body) == "recipient_not_found":
        raise c.ConfigError(f"controller 沒有 {ctl.agent} 的 Identity，請先 import-identities")
    if status == 200 and isinstance(body, dict) and body.get("confirm_mode") in ("agent", "human"):
        return body["confirm_mode"]
    return None


def is_paused(agent: str) -> bool:
    """講師要親自確認時（課堂 demo），讓 agent 先不要代為決定（本機開關；controller 端的切換見 confirm_mode）。"""
    flag = c.env("AGENT_INBOX_PAUSED")
    if flag and (flag in ("1", "all", "true") or agent in [x.strip() for x in flag.split(",")]):
        return True
    return os.path.exists(c.state_path(SKILL, agent, "PAUSED"))


# --- 子指令 -------------------------------------------------------------


def cmd_check(agent: str, today: date, now: datetime, args, result: dict) -> bool:
    role = c.AGENTS[agent]["role"]
    result["role"] = role
    if is_paused(agent):
        result.update(outcome="paused", detail="收件匣代理已暫停（由真人確認），不處理")
        return True

    if args.dry_run:
        # 不連網：示範這個角色對某一類確認會怎麼決定
        category = args.simulate or "other"
        roll = round(c.RNG.random(), 4)
        if category == "refund_decision":
            action, rule = refund_action(role, roll)
            approve = action == "approve"
        else:
            approve, rule = decide(role, category, args.amount, roll)
            action = "approve" if approve else "reject"
        result.update(
            outcome="would_decide",
            simulated={"category": category, "amount": args.amount, "roll": roll, "rule": rule,
                       "decision": action,
                       "comment": make_comment(category, approve, args.amount, action)},
            detail="dry-run：沒有連線 controller，只示範決定規則",
        )
        return True

    path = c.state_path(SKILL, agent, "handled.json")
    data = c.load_json(path)
    handled = data.setdefault("approvals", {})
    skipped_notes = data.setdefault("skipped_notifications", {})

    def persist():
        prune(handled, today)
        cutoff = (today - timedelta(days=KEEP_DAYS)).isoformat()
        for key in [k for k, v in skipped_notes.items() if str(v) < cutoff]:
            skipped_notes.pop(key, None)
        c.save_json(path, data)

    ctl = Controller(agent)
    mode = confirm_mode(ctl)
    result["confirm_mode"] = mode
    if mode == "human":
        # admin 在 controller 前端把這位確認者切成「真人確認」（課堂 demo 由講師親自確認）
        result.update(outcome="paused", detail="controller 設定由真人確認（confirm_mode=human），不處理")
        return True

    status, body = ctl.get("/inbox?" + urllib.parse.urlencode({"limit": args.limit}))
    if status in (401, 403):
        raise c.ServiceError(f"controller 拒絕讀收件匣 {status}: {c.short(body)}", announce=True,
                             kind="inbox_denied")
    if status == 404 and c.error_code(body) == "recipient_not_found":
        raise c.ConfigError(f"controller 沒有 agent_id={agent} 的 Identity，請先 import-identities")
    if status != 200 or not isinstance(body, dict):
        raise c.ServiceError(f"讀 controller 收件匣失敗 {status}: {c.short(body)}")

    # 指派給自己的任務（kind=task，例如 IT 配發筆電）：只列出，交給負責的 skill 處理
    tasks = [{"notification_id": item.get("id"), "flow_id": item.get("flow_id"), "title": item.get("title")}
             for item in body.get("items") or []
             if item.get("kind") == "task" and not item.get("read_at")]

    # 同一個確認可能有多則通知（重送會換新 token）：只用最新那則
    latest: dict = {}
    for item in body.get("items") or []:
        approval_id = item.get("approval_id")
        if item.get("kind") != "approval_request" or not approval_id:
            continue
        if not item.get("link") or str(item.get("id")) in skipped_notes:
            continue
        prev = latest.get(approval_id)
        if prev is None or (item.get("created_at") or "") > (prev.get("created_at") or ""):
            latest[approval_id] = item

    decided, skipped, errors = [], [], []
    for approval_id, item in latest.items():
        record = handled.get(approval_id)
        if record and record.get("status") in ("decided", "closed"):
            continue
        token = token_from_link(item.get("link"))
        if not token:
            continue
        status, view = ctl.get(f"/approvals/{approval_id}?" + urllib.parse.urlencode({"token": token}))
        if status == 410:
            handled[approval_id] = {**(record or {}), "status": "closed",
                                    "reason": c.error_code(view) or "gone",
                                    "flow_id": item.get("flow_id"),
                                    "first_seen": (record or {}).get("first_seen") or today.isoformat()}
            persist()
            skipped.append({"approval_id": approval_id, "reason": handled[approval_id]["reason"]})
            continue
        if status == 403:
            # 連結失效（多半是重送過）：記下這則通知，等新通知
            skipped_notes[str(item.get("id"))] = today.isoformat()
            persist()
            skipped.append({"approval_id": approval_id, "reason": c.error_code(view) or "invalid_token"})
            continue
        if status != 200 or not isinstance(view, dict):
            errors.append({"approval_id": approval_id, "error": f"讀確認頁失敗 {status}: {c.short(view)}"})
            continue

        if not record:
            category = classify(view, item)
            amount = extract_amount(view) if category == "expense" else None
            roll = round(c.RNG.random(), 4)
            options = view.get("options") or []
            approve_value = find_option(options, APPROVE_VALUES, APPROVE_LABELS)
            reject_value = find_option(options, REJECT_VALUES, REJECT_LABELS)
            if category == "refund_decision":
                action, rule = refund_action(role, roll)
                escalate_value = find_option(options, ESCALATE_VALUES, ESCALATE_LABELS)
                if action == "approve" and not refund_has_default_amount(view):
                    # 推估金額是 0（或沒有預設金額）時，不帶金額同意會被 controller 422 擋下、
                    # 每小時重送同一個決定卡住；改成不退款
                    action, rule = "reject", rule + "_no_suggested_amount"
                value = {"approve": approve_value, "reject": reject_value, "escalate": escalate_value}[action]
                if value is None:
                    action, value = "approve", approve_value
                approve = action == "approve"
            else:
                approve, rule = decide(role, category, amount, roll)
                if not approve and reject_value is None:
                    approve = True  # 沒有退回選項（例如「確認收到」）就只能確認
                action = "approve" if approve else "reject"
                value = approve_value if approve else reject_value
            if value is None:
                value = options[0]["value"] if options else "approve"
            record = {
                "status": "pending",
                "flow_id": item.get("flow_id"),
                "flow_title": view.get("flow_title"),
                "step_title": view.get("step_title"),
                "category": category,
                "amount": amount,
                "roll": roll,
                "rule": rule,
                "decision": value,
                "approve": approve,
                "action": action,
                "comment": make_comment(category, approve, amount, action),
                # 給 agent-crm report-trip 查拜訪時間用（派車確認頁的內容）
                "context": view.get("context"),
                "first_seen": today.isoformat(),
            }
            handled[approval_id] = record
            persist()

        status, resp = ctl.post(f"/approvals/{approval_id}/decision",
                                {"token": token, "decision": record["decision"],
                                 "comment": record["comment"]})
        if status == 200:
            record.update(status="decided", decided_at=c.iso(now))
            persist()
            decided.append({"approval_id": approval_id, "category": record["category"],
                            "decision": record["decision"], "comment": record["comment"],
                            "flow_title": record.get("flow_title")})
        elif status == 410:
            record.update(status="closed", reason=c.error_code(resp) or "gone")
            persist()
            skipped.append({"approval_id": approval_id, "reason": record["reason"]})
        elif status == 403:
            skipped_notes[str(item.get("id"))] = today.isoformat()
            persist()
            skipped.append({"approval_id": approval_id, "reason": c.error_code(resp) or "invalid_token"})
        else:
            errors.append({"approval_id": approval_id,
                           "error": f"送出決定失敗 {status}: {c.short(resp)}"})

    result.update(decided=decided, skipped=skipped, errors=errors, tasks=tasks)
    if decided:
        result.update(outcome="decided", detail=f"處理了 {len(decided)} 個確認")
    elif errors:
        result.update(outcome="error", error=errors[0]["error"], detail="有確認處理失敗")
    else:
        result.update(outcome="nothing_to_do", detail="收件匣沒有待處理的確認")
    return not errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="員工 agent 扮演確認者，處理 controller 收件匣")
    parser.add_argument("command", choices=["check", "status"])
    parser.add_argument("--agent", help="OpenClaw agent id（預設讀 OPENCLAW_AGENT_ID）")
    parser.add_argument("--dry-run", action="store_true",
                        help="不連網、不送出，只示範決定規則（搭配 --simulate、--amount）")
    parser.add_argument("--simulate", choices=["dispatch", "expense", "refund_decision", "reply_review", "onboarding",
                                               "other"],
                        help="dry-run：模擬哪一類確認")
    parser.add_argument("--amount", type=float, help="dry-run：模擬報帳金額")
    parser.add_argument("--limit", type=int, default=100, help="一次讀幾則通知")
    parser.add_argument("--days", type=int, default=STATUS_DAYS,
                        help=f"status：統計最近幾天（含今天，1～{KEEP_DAYS}，預設 {STATUS_DAYS}）")
    parser.add_argument("--date", help="覆寫今天的日期 YYYY-MM-DD（測試用）")
    parser.add_argument("--now", help="覆寫現在時間（ISO，測試用）")
    parser.add_argument("--seed", type=int, help="固定亂數種子（測試用）")
    return parser


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    c.load_env_file()
    if args.seed is not None:
        c.RNG.seed(args.seed)
    result: dict = {"command": args.command, "agent": args.agent or os.environ.get("OPENCLAW_AGENT_ID"),
                    "announce": False, "dry_run": args.dry_run}

    def handler() -> bool:
        agent = c.resolve_agent(args.agent)
        now = c.now_tz(args.now)
        today = date.fromisoformat(args.date) if args.date else now.date()
        result.update(agent=agent, date=today.isoformat())
        if args.command == "status":
            return cmd_status(agent, today, args, result)
        return cmd_check(agent, today, now, args, result)

    c.run_main(handler, result)


if __name__ == "__main__":
    main()
