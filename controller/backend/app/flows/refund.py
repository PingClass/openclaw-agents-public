"""流程 ②：客訴升級 → 退款評估 → 主管決定 → 回覆客戶（計畫第 2 節 ②、3、9.5）。

計畫步驟與 controller 步驟的對照（每一步的輸入與 output 欄位見 docs/flows/refund.md）：

| 計畫 | controller 步驟   | 種類       | 執行者                     | skill                |
|------|-------------------|------------|----------------------------|----------------------|
| 1-2  | （啟動流程）       | —          | cs-agent（crm-intake）      | flow-start           |
| —    | resolve_parties   | controller | controller                 | —                    |
| 3    | assess_refund     | agent      | finance-agent              | refund-assessment    |
| 4    | decide_refund     | 人工確認    | 客服主管                    | —                    |
| 4    | record_decision   | agent      | finance-agent              | refund-decision-note |
| 5    | draft_reply       | agent      | cs-agent                   | crm-reply-draft      |
| 6    | review_reply      | 人工確認    | 客服主管                    | —                    |
| 7    | deliver_reply     | controller | controller（模擬通道）       | —                    |
| 7    | log_reply         | agent      | cs-agent                   | crm-reply-log        |
| —    | handoff_human     | controller | controller → 客服主管收件匣  | —（只有「轉人工」走這裡） |

- 客服主管＝Identity 裡 department=客服部、role=manager 的人（剛好一位，目前是 hinata），找不到就停下通知 admin。
- 主管決定（退款金額／不退款／轉人工）寫進 state.decisions.decide_refund，再由 finance-agent 寫一筆
  CaseNote kind=decision（CRM 的 case_note:write 只發給 finance-agent，cs-agent 只有 case_note:read）。
  備註內容由 controller 組好（note_body），agent 照抄，不自己改寫決定。
- 回覆客戶：沒有 LINE 前，「送出」＝controller 記一則對外訊息（ChannelMessage，模擬通道）；
  如果這位客戶有 /sim/messages 的模擬對話，記在同一個對話裡。之後 cs-agent 在 CRM 記互動、建後續待辦。
- 時限：退款決定 4 小時、回覆確認 4 小時，逾時都只標卡住（不結束流程，客訴不能因為沒人看就自動結案）；
  agent 步驟 5 分鐘（3.6）。
- 安全：controller 不會因為客戶訊息內容建立或決定任何人工確認；啟動資料只收白名單欄位，
  多帶的欄位（例如 approved=true）一律丟掉。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.flows.base import (
    END,
    NEXT,
    AgentStep,
    ApprovalStep,
    ControllerStep,
    FlowDefinition,
    Option,
    OutputField,
    State,
    StepStop,
)
from app.models import Identity

TYPE = "refund"
TZ = ZoneInfo("Asia/Taipei")

# 客服主管＝這個部門、role=manager 的人
CS_DEPARTMENT = "客服部"
ROLE_MANAGER = "manager"

CATEGORIES = {"refund": "退款", "compensation": "補償"}

AGENT_TIMEOUT = timedelta(minutes=5)
# 退款決定 4 小時（計畫 3.6）；回覆確認沿用同一位主管、同樣的急迫程度
DECISION_TIMEOUT = timedelta(hours=4)
REVIEW_TIMEOUT = timedelta(hours=4)
# 送出回覆後幾天追蹤（後續待辦的到期日，台北時間 10:00）
FOLLOW_UP_DAYS = 3
FOLLOW_UP_TIME = time(10, 0)

MAX_AMOUNT_TWD = 1_000_000
MAX_REPLY_CHARS = 1000

DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"
DECISION_ESCALATE = "escalate"
DECISION_LABELS = {
    DECISION_APPROVE: "同意退款",
    DECISION_REJECT: "不退款",
    DECISION_ESCALATE: "轉人工處理",
}


# ---------------------------------------------------------------- 初始資料


def _id(value: Any, field: str) -> str:
    """CRM 的 id（UUID 字串；整數也接受，轉成字串）。"""
    if isinstance(value, bool) or value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{field} 必填（CRM id）")
    if not isinstance(value, (str, int)):
        raise ValueError(f"{field} 格式錯誤")
    text = str(value).strip()
    if len(text) > 64:
        raise ValueError(f"{field} 太長")
    return text


def _text(value: Any, field: str, *, limit: int, required: bool = True) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError(f"{field} 必填")
        return None
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError(f"{field} 必須是字串")
    return str(value).strip()[:limit]


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    """只收白名單欄位；其他欄位（例如 approved、decision）一律丟掉，不能用來跳過人工確認。"""
    category = data.get("category")
    if not isinstance(category, str) or category.strip().lower() not in CATEGORIES:
        raise ValueError("category 必須是 refund 或 compensation（CRM C3 的案件分類）")
    return {
        "case_id": _id(data.get("case_id"), "case_id"),
        "customer_id": _id(data.get("customer_id"), "customer_id"),
        "external_customer_id": _text(data.get("external_customer_id"), "external_customer_id",
                                      limit=64, required=False),
        "customer_name": _text(data.get("customer_name"), "customer_name", limit=100, required=False),
        "category": category.strip().lower(),
        "summary": _text(data.get("summary"), "summary", limit=500),
    }


def _customer_label(data: dict[str, Any]) -> str:
    return data.get("customer_name") or data.get("external_customer_id") or "客戶"


def make_title(data: dict[str, Any]) -> str:
    return f"客訴{CATEGORIES[data['category']]}評估：{_customer_label(data)}"


# ---------------------------------------------------------------- 狀態輔助


def _input(state: State) -> dict[str, Any]:
    return state["input"]


def _out(state: State, step: str) -> dict[str, Any]:
    return (state.get("steps") or {}).get(step) or {}


def _decision(state: State, step: str) -> dict[str, Any]:
    return (state.get("decisions") or {}).get(step) or {}


def _manager(state: State) -> dict[str, Any]:
    manager = (state.get("parties") or {}).get("manager")
    if not manager:
        raise KeyError("流程狀態沒有 manager，resolve_parties 尚未完成")
    return manager


def _name(party: dict[str, Any]) -> str:
    return party.get("display_name") or party.get("employee_number") or party["hrm_employee_id"]


def _category_label(state: State) -> str:
    return CATEGORIES[_input(state)["category"]]


def _twd(amount: Any) -> str:
    return f"NT${amount:,}" if isinstance(amount, int) else f"NT${amount}"


def _local(value: datetime) -> str:
    local = value.astimezone(TZ)
    return f"{local.month}/{local.day} {local.strftime('%H:%M')}"


def _case_brief(state: State) -> dict[str, Any]:
    data = _input(state)
    return {
        "case_id": data["case_id"],
        "customer_id": data["customer_id"],
        "external_customer_id": data["external_customer_id"],
        "customer_name": data["customer_name"],
        "category": data["category"],
        "summary": data["summary"],
    }


def refund_decision(state: State) -> dict[str, Any]:
    """主管的決定（給後續步驟與前端）：decision、amount_twd、comment、decided_by、decided_at。"""
    return _decision(state, "decide_refund")


def _decision_text(record: dict[str, Any]) -> str:
    decision = record.get("decision")
    label = DECISION_LABELS.get(decision, decision or "（未決定）")
    if decision == DECISION_APPROVE:
        return f"{label} {_twd(record.get('amount_twd'))}"
    return label


# ---------------------------------------------------------------- resolve_parties


def _brief(identity: Identity) -> dict[str, Any]:
    return {
        "hrm_employee_id": identity.hrm_employee_id,
        "employee_number": identity.employee_number,
        "display_name": identity.display_name,
        "department": identity.department,
        "role": identity.role,
        "agent_id": identity.agent_id,
    }


def resolve(runtime: Any, state: State) -> dict[str, Any]:
    """客服主管一律從 Identity 解析：客服部、role=manager，剛好一位；否則停下通知 admin，不猜。"""
    managers = runtime.db.scalars(
        select(Identity).where(Identity.department == CS_DEPARTMENT, Identity.role == ROLE_MANAGER)
    ).all()
    what = f"{CS_DEPARTMENT}的客服主管（department={CS_DEPARTMENT}、role={ROLE_MANAGER}）"
    if not managers:
        raise StepStop(f"找不到{what}，請先在 Identity 補上（import-identities）")
    if len(managers) > 1:
        names = "、".join(i.display_name or i.hrm_employee_id for i in managers)
        raise StepStop(f"{what}有 {len(managers)} 位（{names}），無法決定由誰確認")
    manager = _brief(managers[0])
    runtime.set_state("parties", {"manager": manager})
    return {"manager": _name(manager)}


# ---------------------------------------------------------------- assess_refund（計畫步驟 3）


def _assess_instruction(state: State) -> str:
    return "\n".join([
        f"客戶「{_customer_label(_input(state))}」的{_category_label(state)}案件需要退款評估。",
        "讀 CRM GET /api/v1/customers/{customer_id}/context/（客戶完整資料、過去互動與案件）與 "
        "GET /api/v1/cases/{case_id}/，產生退款評估。",
        "評估寫進案件內部備註：POST /api/v1/cases/{case_id}/notes/，kind=assessment，"
        "Header 的 Idempotency-Key 用上面給的值。不得寫進互動紀錄（客戶看得到性質的資料），也不得聯絡客戶。",
        "建議金額一律標成「推估」並寫出依據，不得寫成已核定的金額；要不要退、退多少由客服主管決定。",
        "建議不退款時 suggested_amount_twd 填 0。資料不足、無法評估時回報 status=needs_human。",
    ])


def _validate_assessment(state: State, output: dict[str, Any]) -> None:
    amount = output.get("suggested_amount_twd")
    if isinstance(amount, bool) or not isinstance(amount, int) or not 0 <= amount <= MAX_AMOUNT_TWD:
        raise ValueError(f"suggested_amount_twd 必須是 0～{MAX_AMOUNT_TWD:,} 的整數（NT$）")
    if output.get("amount_is_estimate") is not True:
        raise ValueError("amount_is_estimate 必須是 true：評估金額只是推估，不是核定金額")


ASSESS_OUTPUTS = (
    OutputField("note_id", "str", "CRM CaseNote id（kind=assessment，UUID 字串）"),
    OutputField("suggested_amount_twd", "int", "推估的建議退款金額（NT$，不退款填 0）"),
    OutputField("amount_is_estimate", "bool", "固定 true：金額是推估，不是核定"),
    OutputField("basis", "str", "推估依據（商品金額、瑕疵情況、退款政策……），不含電話地址"),
    OutputField("history", "str", "客戶過去紀錄摘要（過去案件、退款次數）", required=False),
    OutputField("recommendation", "str", "建議：refund／partial／no_refund／escalate", required=False),
)


# ---------------------------------------------------------------- decide_refund（計畫步驟 4）


def _assessment(state: State) -> dict[str, Any]:
    return _out(state, "assess_refund")


def _decide_question(state: State) -> str:
    assessment = _assessment(state)
    return (f"請決定「{_customer_label(_input(state))}」的{_category_label(state)}案件："
            f"財會推估建議 {_twd(assessment.get('suggested_amount_twd'))}（推估，非核定）。"
            "可以同意退款（金額可調整）、不退款，或轉人工處理。")


def _decide_context(state: State) -> dict[str, Any]:
    assessment = _assessment(state)
    return {
        "customer": _customer_label(_input(state)),
        "category": _category_label(state),
        "case_summary": _input(state)["summary"],
        "suggested_amount": f"{_twd(assessment.get('suggested_amount_twd'))}（推估）",
        "basis": assessment.get("basis"),
        "history": assessment.get("history"),
        "recommendation": assessment.get("recommendation"),
    }


def _decide_fields(state: State) -> list[dict[str, Any]]:
    return [{
        "name": "amount_twd",
        "label": "退款金額（NT$）",
        "type": "int",
        "applies_to": [DECISION_APPROVE],
        "default": _assessment(state).get("suggested_amount_twd"),
        "min": 1,
        "max": MAX_AMOUNT_TWD,
    }]


def accept_refund_decision(state: State, decision: str, data: dict[str, Any],
                           meta: dict[str, Any]) -> dict[str, Any]:
    """主管決定附帶的資料。同意退款時 amount_twd 可調整（沒帶就用推估金額），必須 > 0。"""
    record: dict[str, Any] = {"amount_twd": None, "amount_source": None,
                              "decided_by": meta.get("approver"), "decided_at": meta.get("decided_at")}
    if decision != DECISION_APPROVE:
        return record
    suggested = _assessment(state).get("suggested_amount_twd")
    amount = data.get("amount_twd")
    source = "manager"
    if amount is None or amount == "":
        amount, source = suggested, "suggested"
    if isinstance(amount, str) and amount.strip().isdigit():
        amount = int(amount.strip())
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise ValueError("amount_twd（退款金額）必須是整數")
    if amount <= 0:
        raise ValueError("退款金額要大於 0；不退款請選「不退款」")
    if amount > MAX_AMOUNT_TWD:
        raise ValueError(f"退款金額不能超過 {_twd(MAX_AMOUNT_TWD)}")
    if amount == suggested:
        source = "suggested"
    record.update(amount_twd=amount, amount_source=source)
    return record


def _decide_summary(label: str, record: dict[str, Any]) -> str:
    text = _decision_text(record)
    if record.get("decision") == DECISION_APPROVE:
        text += "（依推估金額）" if record.get("amount_source") == "suggested" else "（主管調整金額）"
    return text + (f"：{record['comment']}" if record.get("comment") else "")


# ---------------------------------------------------------------- record_decision（計畫步驟 4，寫 CaseNote）


def decision_note_body(state: State) -> str:
    """寫進 CaseNote kind=decision 的內容，由 controller 組好，agent 照抄。"""
    record = refund_decision(state)
    assessment = _assessment(state)
    lines = [f"【客服主管決定】{_decision_text(record)}"]
    if record.get("decision") == DECISION_APPROVE:
        source = "依財會推估金額" if record.get("amount_source") == "suggested" else "主管調整金額"
        lines[0] += f"（{source}；財會推估 {_twd(assessment.get('suggested_amount_twd'))}）"
    elif record.get("decision") == DECISION_ESCALATE:
        lines.append("本案轉由專人處理，不自動回覆客戶；結案由人在 CRM 操作。")
    decided_at = record.get("decided_at")
    when = _local(datetime.fromisoformat(decided_at)) if decided_at else "（未記錄）"
    lines.append(f"決定人：{record.get('decided_by') or '（未記錄）'}，{when}")
    if record.get("comment"):
        lines.append(f"意見：{record['comment']}")
    if assessment.get("note_id"):
        lines.append(f"評估備註：{assessment['note_id']}")
    return "\n".join(lines)


def _record_inputs(state: State) -> dict[str, Any]:
    record = refund_decision(state)
    return {
        "case_id": _input(state)["case_id"],
        "decision": record.get("decision"),
        "decision_label": DECISION_LABELS.get(record.get("decision"), record.get("decision")),
        "amount_twd": record.get("amount_twd"),
        "comment": record.get("comment"),
        "decided_by": record.get("decided_by"),
        "decided_at": record.get("decided_at"),
        "assessment_note_id": _assessment(state).get("note_id"),
        "note_kind": "decision",
        "note_body": decision_note_body(state),
    }


def _record_instruction(state: State) -> str:
    return "\n".join([
        f"客服主管已對「{_customer_label(_input(state))}」的案件做出決定：{_decision_text(refund_decision(state))}。",
        "在 CRM 案件寫一筆內部備註：POST /api/v1/cases/{case_id}/notes/，kind=decision，"
        "body 一字不改用輸入的 note_body；Header 的 Idempotency-Key 用上面給的值。",
        "只寫備註，不改案件狀態、不結案、不聯絡客戶。",
    ])


def _after_record(state: State, output: dict[str, Any]) -> str:
    return "handoff_human" if refund_decision(state).get("decision") == DECISION_ESCALATE else NEXT


RECORD_OUTPUTS = (
    OutputField("note_id", "str", "CRM CaseNote id（kind=decision，UUID 字串）"),
)


# ---------------------------------------------------------------- draft_reply（計畫步驟 5）


# 金額寫法：NT$1,200、NTD 1200、新台幣 1200、$1200、1,200 元、1200 塊（全形數字與符號先轉半形）
_AMOUNT_IN_TEXT = re.compile(r"(?:NT\$|NTD|新台幣|台幣|\$)\s*([\d,]+)|([\d,]+)\s*(?:元|塊)")


def amounts_in(text: str) -> list[int]:
    found = []
    for match in _AMOUNT_IN_TEXT.finditer(unicodedata.normalize("NFKC", text or "")):
        digits = (match.group(1) or match.group(2) or "").replace(",", "")
        if digits.isdigit():
            found.append(int(digits))
    return found


def _review_decision(state: State) -> dict[str, Any]:
    return _decision(state, "review_reply")


def _draft_inputs(state: State) -> dict[str, Any]:
    record = refund_decision(state)
    review = _review_decision(state)
    revising = review.get("decision") == "revise"
    return {
        **_case_brief(state),
        "decision": record.get("decision"),
        "decision_label": DECISION_LABELS.get(record.get("decision"), record.get("decision")),
        "amount_twd": record.get("amount_twd"),
        "manager_comment": review.get("comment") if revising else None,
        "previous_reply": _out(state, "draft_reply").get("reply_text") if revising else None,
        "max_chars": MAX_REPLY_CHARS,
    }


def _draft_instruction(state: State) -> str:
    record = refund_decision(state)
    lines = [f"依客服主管的決定（{_decision_text(record)}），擬一則回覆客戶「{_customer_label(_input(state))}」的訊息。"]
    if record.get("decision") == DECISION_APPROVE:
        lines.append(f"金額只能寫主管核定的 {_twd(record.get('amount_twd'))}，不得出現其他金額、"
                     "不得加碼或承諾其他補償。")
    else:
        lines.append("這次不退款：委婉說明，不得出現任何金額，也不得暗示之後會退款或補償。")
    lines += [
        f"語氣有禮、具體，{MAX_REPLY_CHARS} 字以內；不要提到內部評估、推估金額、主管姓名或流程。",
        "只產生草稿交給 controller，不要傳給客戶、不要寫入 CRM；要等客服主管確認後才會送出。",
    ]
    review = _review_decision(state)
    if review.get("decision") == "revise":
        lines.append(f"客服主管退回重擬，意見：{review.get('comment') or '（未說明）'}；上一版在 previous_reply。")
    return "\n".join(lines)


def _validate_reply(state: State, output: dict[str, Any]) -> None:
    text = output.get("reply_text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("reply_text 不能是空的")
    if len(text) > MAX_REPLY_CHARS:
        raise ValueError(f"reply_text 超過 {MAX_REPLY_CHARS} 字")
    record = refund_decision(state)
    amounts = amounts_in(text)
    if record.get("decision") == DECISION_APPROVE:
        wrong = [a for a in amounts if a != record.get("amount_twd")]
        if wrong:
            raise ValueError(f"回覆裡的金額 {wrong} 和主管核定的 {_twd(record.get('amount_twd'))} 不一致")
    elif amounts:
        raise ValueError("這次不退款，回覆不得出現任何金額")


DRAFT_OUTPUTS = (
    OutputField("reply_text", "str", f"回覆客戶的內容（{MAX_REPLY_CHARS} 字以內，金額只能是主管核定的金額）"),
    OutputField("tone_notes", "str", "給主管看的補充（為什麼這樣寫），不會送給客戶", required=False),
)


# ---------------------------------------------------------------- review_reply（計畫步驟 6）


def _reply_text(state: State) -> str:
    return _out(state, "draft_reply").get("reply_text") or ""


def _review_question(state: State) -> str:
    return (f"請確認回覆客戶「{_customer_label(_input(state))}」的內容"
            f"（決定：{_decision_text(refund_decision(state))}）。核准後就會送出給客戶。")


def _review_context(state: State) -> dict[str, Any]:
    return {
        "customer": _customer_label(_input(state)),
        "decision": _decision_text(refund_decision(state)),
        "reply_text": _reply_text(state),
        "tone_notes": _out(state, "draft_reply").get("tone_notes"),
    }


# ---------------------------------------------------------------- deliver_reply（計畫步驟 7，送出）


def deliver_reply(runtime: Any, state: State) -> dict[str, Any]:
    """主管核准後，controller 送出回覆（LINE 接上前＝記在模擬通道）。送出的就是主管確認過的那一版。"""
    text = _reply_text(state)
    if not text.strip():
        raise StepStop("沒有可送出的回覆內容（draft_reply 沒有 reply_text）")
    message = runtime.send_customer_message(
        external_customer_id=_input(state)["external_customer_id"], body=text, agent_id="cs-agent")
    reply = {
        "message_id": message.id,
        "channel": message.channel,
        "conversation_id": message.conversation_id,
        "sent_at": runtime.now.isoformat(),
        "text": text,
    }
    runtime.set_state("reply", reply)
    runtime.set_state("outcome", "replied")
    return {key: reply[key] for key in ("message_id", "channel", "conversation_id", "sent_at")}


def _deliver_summary(output: dict[str, Any]) -> str:
    where = f"模擬對話 {output['conversation_id']}" if output.get("conversation_id") else "沒有對應的模擬對話"
    return f"已送出回覆（{output['channel']} 通道，{where}）"


# ---------------------------------------------------------------- log_reply（計畫步驟 7，記互動、建待辦）


def follow_up_due(state: State) -> datetime:
    sent_at = datetime.fromisoformat((state.get("reply") or {}).get("sent_at")).astimezone(TZ)
    return datetime.combine(sent_at.date() + timedelta(days=FOLLOW_UP_DAYS), FOLLOW_UP_TIME, tzinfo=TZ)


def _log_inputs(state: State) -> dict[str, Any]:
    reply = state.get("reply") or {}
    record = refund_decision(state)
    return {
        **_case_brief(state),
        "reply_text": reply.get("text"),
        "sent_at": reply.get("sent_at"),
        "channel": reply.get("channel"),
        "message_id": reply.get("message_id"),
        "decision": record.get("decision"),
        "amount_twd": record.get("amount_twd"),
        "follow_up": {
            "kind": "general",
            "due_at": follow_up_due(state).isoformat(),
            "title": f"追蹤{_category_label(state)}案件回覆：{_customer_label(_input(state))}",
        },
    }


def _log_instruction(state: State) -> str:
    return "\n".join([
        f"controller 已把客服主管核准的回覆送給客戶「{_customer_label(_input(state))}」，在 CRM 留下紀錄。",
        "1) POST /api/v1/interactions/ 記一筆對外回覆的互動（customer=customer_id，關聯 case_id，內容用 reply_text），"
        "Idempotency-Key 用 Idempotency-Key.interaction。互動紀錄不得寫入內部評估或推估金額。",
        "2) POST /api/v1/follow-ups/ 建一筆後續待辦（kind、due_at、title 用輸入的 follow_up），"
        "Idempotency-Key 用 Idempotency-Key.follow_up。",
        "不要再傳訊息給客戶（已經送出了），不要結案（結案由人在 CRM 操作）。",
    ])


LOG_OUTPUTS = (
    OutputField("interaction_id", "str", "CRM 互動紀錄 id（UUID 字串）"),
    OutputField("follow_up_id", "str", "CRM 後續待辦 id（UUID 字串）"),
)


# ---------------------------------------------------------------- handoff_human（轉人工）


def handoff_human(runtime: Any, state: State) -> dict[str, Any]:
    manager = _manager(state)
    record = refund_decision(state)
    body = (f"「{_customer_label(_input(state))}」的{_category_label(state)}案件已依你的決定轉人工處理。"
            "controller 不會自動回覆客戶（客戶已收到「已轉專人處理」）；請直接在 CRM 跟進、回覆並結案。"
            + (f"你的意見：{record['comment']}。" if record.get("comment") else "")
            + f"流程 {runtime.flow.id}")
    runtime.notify(manager["hrm_employee_id"], f"已轉人工處理：{runtime.flow.title}", body)
    runtime.set_state("outcome", "escalated")
    return {"notified": _name(manager)}


# ---------------------------------------------------------------- 定義

DEFINITION = FlowDefinition(
    type=TYPE,
    display_name="客訴退款評估",
    validate_input=validate_input,
    make_title=make_title,
    starters=("cs-agent",),
    dedupe_key=lambda data: f"crm:case:{data['case_id']}",
    steps=(
        ControllerStep(
            name="resolve_parties",
            title="確認客服主管",
            run=resolve,
            summary=lambda out: f"客服主管 {out['manager']}",
        ),
        AgentStep(
            name="assess_refund",
            title="退款評估（內部備註）",
            agent="finance-agent",
            skill="refund-assessment",
            instruction=_assess_instruction,
            inputs=_case_brief,
            outputs=ASSESS_OUTPUTS,
            validate_output=_validate_assessment,
            timeout=AGENT_TIMEOUT,
            max_attempts=2,
        ),
        ApprovalStep(
            name="decide_refund",
            title="客服主管決定退款",
            approver=lambda state: _manager(state)["hrm_employee_id"],
            question=_decide_question,
            options=(
                Option(DECISION_APPROVE, DECISION_LABELS[DECISION_APPROVE]),
                Option(DECISION_REJECT, DECISION_LABELS[DECISION_REJECT]),
                Option(DECISION_ESCALATE, DECISION_LABELS[DECISION_ESCALATE]),
            ),
            # 三種決定都要先寫 CaseNote kind=decision，再分流（轉人工 → handoff_human）
            routes={DECISION_APPROVE: NEXT, DECISION_REJECT: NEXT, DECISION_ESCALATE: NEXT},
            timeout=DECISION_TIMEOUT,
            on_timeout="stuck",
            context=_decide_context,
            fields=_decide_fields,
            accept_decision=accept_refund_decision,
            decision_summary=_decide_summary,
        ),
        AgentStep(
            name="record_decision",
            title="寫入主管決定（案件備註）",
            agent="finance-agent",
            skill="refund-decision-note",
            instruction=_record_instruction,
            inputs=_record_inputs,
            outputs=RECORD_OUTPUTS,
            timeout=AGENT_TIMEOUT,
            max_attempts=2,
            route=_after_record,
        ),
        AgentStep(
            name="draft_reply",
            title="擬回覆客戶",
            agent="cs-agent",
            skill="crm-reply-draft",
            instruction=_draft_instruction,
            inputs=_draft_inputs,
            outputs=DRAFT_OUTPUTS,
            validate_output=_validate_reply,
            timeout=AGENT_TIMEOUT,
            max_attempts=2,
        ),
        ApprovalStep(
            name="review_reply",
            title="客服主管確認回覆內容",
            approver=lambda state: _manager(state)["hrm_employee_id"],
            question=_review_question,
            options=(Option("approve", "核准送出"), Option("revise", "退回重擬")),
            routes={"approve": NEXT, "revise": "draft_reply"},
            timeout=REVIEW_TIMEOUT,
            on_timeout="stuck",
            context=_review_context,
        ),
        ControllerStep(
            name="deliver_reply",
            title="送出回覆給客戶",
            run=deliver_reply,
            summary=_deliver_summary,
        ),
        AgentStep(
            name="log_reply",
            title="記回覆互動、建後續待辦",
            agent="cs-agent",
            skill="crm-reply-log",
            instruction=_log_instruction,
            inputs=_log_inputs,
            outputs=LOG_OUTPUTS,
            write_keys=("interaction", "follow_up"),
            timeout=AGENT_TIMEOUT,
            max_attempts=2,
            route=lambda state, output: END,
        ),
        ControllerStep(
            name="handoff_human",
            title="轉人工處理",
            run=handoff_human,
            summary=lambda out: f"已通知 {out['notified']} 親自處理",
            route=lambda state, output: END,
        ),
    ),
)
