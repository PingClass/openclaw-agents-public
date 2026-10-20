"""流程 ①：客戶拜訪 → 派車 → 出車 → 報帳（計畫第 2 節 ①、3.4～3.6、9.4）。

計畫步驟與 controller 步驟的對照（每一步的輸入與 output 欄位見 docs/flows/visit.md）：

| 計畫 | controller 步驟   | 種類       | 執行者                    | skill               |
|------|-------------------|------------|---------------------------|---------------------|
| 1    | （啟動流程）       | —          | cs-agent（crm-visit-list） | flow-start          |
| —    | resolve_parties   | controller | controller                | —                   |
| 2    | draft_vehicle     | agent      | admin-agent               | adm-vehicle-draft   |
| 3    | confirm_vehicle   | 人工確認    | 業務本人                   | —                   |
| 4    | book_vehicle      | agent      | admin-agent               | adm-vehicle-book    |
| 5    | await_trip        | 等待事件    | 業務本人（trip_report）     | —                   |
| 5    | record_trip       | agent      | admin-agent               | adm-vehicle-trip    |
| 6    | draft_claim       | agent      | finance-agent             | mileage-claim-draft |
| 7    | approve_claim     | 人工確認    | 業務主管                   | —                   |
| 7    | notify_finance    | controller | controller → 財會收件匣     | —                   |
| 8    | close_visit       | agent      | cs-agent                  | crm-visit-close     |

- 派車預約時段＝拜訪開始 → 拜訪當天 23:59：業務還車後才一次回報取車、還車里程，
  ADM 只接受預約時段內登記取車（見 booking_window）。
- 時限（3.6）：agent 步驟 5 分鐘；派車確認截止＝拜訪前一天 18:00（逾時 → 流程 expired，
  通知業務自行預約）；等還車回報截止＝拜訪當天 23:59（逾時只標卡住）。
- 失敗：book_vehicle 遇到 ADM 409 時段衝突 → 回 draft_vehicle 重查空車，最多 2 次，之後交給人。
  重查後直接預約，不再請業務確認一次（7 驗收：「時段衝突時自動重查空車」）。
- 確認對象一律從 Identity 解析，找不到人就停下並通知 admin，不猜。
- 課堂示範模式（input.demo=true，只有 admin 或 cs-agent 能啟動，見 docs/flows/visit.md「示範模式」）：
  派車確認截止改為拜訪開始前 5 分鐘、等還車回報截止＝拜訪結束（預約時段也只到拜訪結束）、
  報帳核准 15 分鐘，讓整條流程能在同一堂課內跑完；其他行為不變。
- 報帳草稿只存在 flow state；業務主管核准後才通知財會入帳（代替 LINE），核准前沒有任何系統寫入金額。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
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
    WaitStep,
)
from app.models import Identity

TYPE = "visit"
TZ = ZoneInfo("Asia/Taipei")

# Identity.role 的值
ROLE_MANAGER = "manager"
ROLE_FINANCE = "finance"

# 派車確認截止：拜訪前一天幾點
CONFIRM_CUTOFF = time(18, 0)
# 等還車回報截止：拜訪當天幾點
TRIP_CUTOFF = time(23, 59)
# 報帳核准的時限（逾時只標卡住，不結束流程：車已經出過了）
CLAIM_APPROVAL_TIMEOUT = timedelta(hours=48)
# 里程差合理上限（超過就請業務確認，不寫進流程）
MAX_TRIP_KM = 2000

# 示範模式（課堂 demo）的時限
# 派車確認截止：拜訪開始前幾分鐘（ADM 從預約開始前 15 分鐘起才能登記取車，所以 demo 的拜訪排在約 20 分鐘後）
DEMO_CONFIRM_LEAD = timedelta(minutes=5)
# 派車確認至少保留多久（拜訪排得太近時，避免確認一建立就逾時）
DEMO_CONFIRM_MIN = timedelta(minutes=5)
# 報帳核准的時限（逾時只標卡住；過一半就會出現在總覽的卡住清單）
DEMO_CLAIM_APPROVAL_TIMEOUT = timedelta(minutes=15)
# 示範模式的標題前綴
DEMO_TITLE_PREFIX = "［示範］"
# 示範模式只接受「今天、這麼久之內開始」的拜訪（課堂上 prepare 排在約 20 分鐘後）
DEMO_MAX_LEAD = timedelta(hours=3)


# ---------------------------------------------------------------- 初始資料


def _parse_dt(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必填（ISO 8601，例如 2026-10-15T10:00:00+08:00）")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} 格式錯誤：{value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def _text(value: Any, field: str, *, limit: int, required: bool = True) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError(f"{field} 必填")
        return None
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError(f"{field} 必須是字串")
    return str(value).strip()[:limit]


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    follow_up_id = data.get("follow_up_id")
    if isinstance(follow_up_id, bool) or follow_up_id in (None, ""):
        raise ValueError("follow_up_id 必填（CRM 待辦跟進 id）")
    if isinstance(follow_up_id, str) and follow_up_id.strip().isdigit():
        follow_up_id = int(follow_up_id.strip())
    if not isinstance(follow_up_id, (int, str)):
        raise ValueError("follow_up_id 格式錯誤")

    customer = data.get("customer")
    if not isinstance(customer, dict):
        raise ValueError("customer 必填：{name, external_id}")
    customer_name = _text(customer.get("name"), "customer.name", limit=100)
    external_id = _text(customer.get("external_id"), "customer.external_id", limit=64, required=False)

    start = _parse_dt(data.get("visit_start_at"), "visit_start_at")
    end = _parse_dt(data.get("visit_end_at"), "visit_end_at")
    if end <= start:
        raise ValueError("visit_end_at 必須晚於 visit_start_at")
    if end - start > timedelta(hours=24):
        raise ValueError("拜訪時段不能超過 24 小時")

    seats = data.get("seats", 1)
    if isinstance(seats, bool) or not isinstance(seats, int) or not 1 <= seats <= 20:
        raise ValueError("seats（人數）必須是 1～20 的整數")

    demo = data.get("demo", False)
    if demo is not None and not isinstance(demo, bool):
        raise ValueError("demo 必須是 true／false")

    clean = {
        "follow_up_id": follow_up_id,
        "customer": {"name": customer_name, "external_id": external_id},
        "visit_start_at": start.isoformat(),
        "visit_end_at": end.isoformat(),
        "location": _text(data.get("location"), "location", limit=200),
        "seats": seats,
        "assignee_hrm_employee_id": _text(data.get("assignee_hrm_employee_id"),
                                          "assignee_hrm_employee_id", limit=64),
        "subject": _text(data.get("subject"), "subject", limit=200, required=False),
    }
    if demo:
        # 只有示範流程才有這個欄位，正式流程的 input 不變
        clean["demo"] = True
    return clean


def demo_check(clean: dict[str, Any], now: datetime) -> None:
    """示範模式只用在課堂上「今天、幾小時內開始、還沒結束」的拜訪。

    示範模式會把派車確認截止與預約時段縮短；限制在這個範圍內，cs-agent 就算被誘導帶 demo=true，
    也改不到明天以後的正式拜訪。
    """
    start = datetime.fromisoformat(clean["visit_start_at"]).astimezone(TZ)
    end = datetime.fromisoformat(clean["visit_end_at"]).astimezone(TZ)
    local_now = now.astimezone(TZ)
    if start.date() != local_now.date():
        raise ValueError("示範模式只能用在今天的拜訪")
    if start > local_now + DEMO_MAX_LEAD:
        hours = int(DEMO_MAX_LEAD.total_seconds() // 3600)
        raise ValueError(f"示範模式的拜訪要在 {hours} 小時內開始")
    if end <= local_now:
        raise ValueError("示範模式的拜訪已經結束")


def is_demo(state_or_input: dict[str, Any]) -> bool:
    """流程狀態（或已驗證的 input）是不是示範模式。"""
    data = state_or_input.get("input", state_or_input) if isinstance(state_or_input, dict) else {}
    return bool(isinstance(data, dict) and data.get("demo"))


def _visit_start(state: State) -> datetime:
    return datetime.fromisoformat(state["input"]["visit_start_at"]).astimezone(TZ)


def _visit_end(state: State) -> datetime:
    return datetime.fromisoformat(state["input"]["visit_end_at"]).astimezone(TZ)


def _visit_day(state: State) -> date:
    return _visit_start(state).date()


def _md(value: datetime | date) -> str:
    return f"{value.month}/{value.day}"


def _hm(value: datetime) -> str:
    return value.strftime("%H:%M")


def visit_window_text(state: State) -> str:
    start, end = _visit_start(state), _visit_end(state)
    return f"{_md(start)} {_hm(start)}–{_hm(end)}"


def make_title(data: dict[str, Any]) -> str:
    start = datetime.fromisoformat(data["visit_start_at"]).astimezone(TZ)
    prefix = DEMO_TITLE_PREFIX if data.get("demo") else ""
    return f"{prefix}{_md(start)} 拜訪 {data['customer']['name']}"


def confirm_deadline(state: State, now: datetime) -> datetime:
    """派車確認截止：拜訪前一天 18:00（台北時間）。

    示範模式：拜訪開始前 5 分鐘，但至少從現在起保留 5 分鐘。
    """
    if is_demo(state):
        return max(_visit_start(state) - DEMO_CONFIRM_LEAD, now + DEMO_CONFIRM_MIN)
    return datetime.combine(_visit_day(state) - timedelta(days=1), CONFIRM_CUTOFF, tzinfo=TZ)


def trip_deadline(state: State, now: datetime) -> datetime:
    """等還車回報截止：拜訪當天 23:59（台北時間）。

    示範模式：拜訪結束（逾時只標卡住）。預約時段也因此只到拜訪結束，見 booking_window。
    """
    if is_demo(state):
        return _visit_end(state)
    return datetime.combine(_visit_day(state), TRIP_CUTOFF, tzinfo=TZ)


def claim_approval_deadline(state: State, now: datetime) -> datetime:
    """報帳核准：48 小時；示範模式 15 分鐘（逾時都只標卡住）。"""
    return now + (DEMO_CLAIM_APPROVAL_TIMEOUT if is_demo(state) else CLAIM_APPROVAL_TIMEOUT)


def booking_window(state: State) -> tuple[datetime, datetime]:
    """派車預約的時段：拜訪開始 → max(拜訪結束, 拜訪當天 23:59)。

    業務（員工 agent 的 agent-crm report-trip）是在拜訪當天傍晚、還車後才一次回報取車＋還車里程，
    admin-agent 收到後才去 ADM 登記取車；ADM 只接受「預約開始前 15 分鐘～預約結束」之間登記取車
    （過了回 409 Booking window has ended）。所以預約要涵蓋到等還車回報的截止時間，
    否則 record_trip 一定失敗。
    """
    start = _visit_start(state)
    end = max(_visit_end(state), trip_deadline(state, start))
    return start, end


# ---------------------------------------------------------------- 狀態輔助


def parties(state: State) -> dict[str, Any]:
    return state.get("parties") or {}


def _party(state: State, key: str) -> dict[str, Any]:
    party = parties(state).get(key)
    if not party:
        raise KeyError(f"流程狀態沒有 {key}，resolve_parties 尚未完成")
    return party


def _name(party: dict[str, Any]) -> str:
    return party.get("display_name") or party.get("employee_number") or party["hrm_employee_id"]


def _out(state: State, step: str) -> dict[str, Any]:
    return (state.get("steps") or {}).get(step) or {}


def _decision(state: State, step: str) -> dict[str, Any]:
    return (state.get("decisions") or {}).get(step) or {}


def _failure(state: State, step: str) -> dict[str, Any]:
    return (state.get("failures") or {}).get(step) or {}


def _customer(state: State) -> dict[str, Any]:
    return state["input"]["customer"]


def _vehicle_label(draft: dict[str, Any]) -> str:
    code = draft.get("vehicle_code") or f"#{draft.get('vehicle_id')}"
    name = draft.get("vehicle_name")
    return f"{code}（{name}）" if name else str(code)


def _local(value: str | None) -> str:
    if not value:
        return "（未提供）"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(TZ)
    except ValueError:
        return value
    return f"{_md(parsed)} {_hm(parsed)}"


def _person(party: dict[str, Any]) -> dict[str, Any]:
    return {"hrm_employee_id": party["hrm_employee_id"], "display_name": _name(party)}


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


def _only_one(found: list[Identity], what: str) -> Identity:
    if not found:
        raise StepStop(f"找不到{what}，請先在 Identity 補上（import-identities）")
    if len(found) > 1:
        names = "、".join(i.display_name or i.hrm_employee_id for i in found)
        raise StepStop(f"{what}有 {len(found)} 位（{names}），無法決定由誰確認")
    return found[0]


def resolve(runtime: Any, state: State) -> dict[str, Any]:
    """業務、業務主管、財會一律從 Identity 解析；找不到或不只一位就停下，不猜。"""
    db = runtime.db
    sales_id = state["input"]["assignee_hrm_employee_id"]
    sales = db.scalar(select(Identity).where(Identity.hrm_employee_id == sales_id))
    if sales is None:
        raise StepStop(f"找不到負責業務（hrm_employee_id={sales_id}），請先在 Identity 補上")
    if not sales.department:
        raise StepStop(f"業務 {sales.display_name or sales_id} 沒有設定部門，無法決定業務主管")
    managers = db.scalars(
        select(Identity).where(Identity.department == sales.department, Identity.role == ROLE_MANAGER,
                               Identity.id != sales.id)
    ).all()
    manager = _only_one(list(managers), f"{sales.department}的業務主管（同部門、role={ROLE_MANAGER}）")
    finances = db.scalars(select(Identity).where(Identity.role == ROLE_FINANCE)).all()
    finance = _only_one(list(finances), f"財會人員（role={ROLE_FINANCE}）")

    resolved = {"sales": _brief(sales), "manager": _brief(manager), "finance": _brief(finance)}
    runtime.set_state("parties", resolved)
    # 業務本人的員工 agent 可以查這條流程（GET /flows/{id}）
    runtime.set_state("participants", [sales.agent_id] if sales.agent_id else [])
    return {key: _name(value) for key, value in resolved.items()}


# ---------------------------------------------------------------- draft_vehicle（計畫步驟 2）


def conflict_vehicle_ids(state: State) -> list[Any]:
    """所有 409 時段衝突過的車（依發生順序、不重複），重查空車時全部排除，不只最後一台。"""
    failure = _failure(state, "book_vehicle")
    outputs = [entry.get("output") or {} for entry in failure.get("history") or []]
    if not outputs and failure:
        outputs = [failure.get("output") or {}]  # 舊版 state 沒有 history
    exclude: list[Any] = []
    for output in outputs:
        if not (output.get("conflict") or output.get("http_status") == 409):
            continue
        vehicle_id = output.get("vehicle_id")
        if vehicle_id is not None and vehicle_id not in exclude:
            exclude.append(vehicle_id)
    return exclude


def _draft_inputs(state: State) -> dict[str, Any]:
    sales = _party(state, "sales")
    exclude = conflict_vehicle_ids(state)
    decision = _decision(state, "confirm_vehicle")
    booking_start, booking_end = booking_window(state)
    return {
        "visit_start_at": state["input"]["visit_start_at"],
        "visit_end_at": state["input"]["visit_end_at"],
        "booking_start_at": booking_start.isoformat(),
        "booking_end_at": booking_end.isoformat(),
        "seats": state["input"]["seats"],
        "destination": state["input"]["location"],
        "purpose": f"客戶拜訪：{_customer(state)['name']}",
        "customer": _customer(state),
        "sales": _person(sales),
        "exclude_vehicle_ids": exclude,
        "sales_comment": decision.get("comment") if decision.get("decision") == "revise" else None,
    }


def _draft_instruction(state: State) -> str:
    lines = [
        f"查 {visit_window_text(state)}（拜訪「{_customer(state)['name']}」）可用的公務車，產生派車草稿。",
        "用 ADM GET /api/v1/vehicles/availability/?start_at&end_at&seats 查空車，start_at／end_at 用輸入的 "
        "booking_start_at／booking_end_at（預約要涵蓋到拜訪當天 23:59，業務還車後才回報里程，"
        "ADM 只接受預約時段內登記取車）；草稿的 start_at／end_at 也用這兩個值。只產生草稿，不要預約。",
        "沒有任何可用車輛時回報 status=needs_human，summary 說明原因。",
    ]
    if _failure(state, "book_vehicle"):
        lines.append("上一次預約遇到時段衝突（ADM 409），請排除 exclude_vehicle_ids 重新挑車；"
                     "這次草稿會直接拿去預約，不再請業務確認。")
    decision = _decision(state, "confirm_vehicle")
    if decision.get("decision") == "revise":
        lines.append(f"業務退回重排，意見：{decision.get('comment') or '（未說明）'}")
    return "\n".join(lines)


def _after_draft(state: State, output: dict[str, Any]) -> str:
    # 預約遇到 409 後重查的草稿：直接預約（自動重查空車，不再請業務確認）
    return "book_vehicle" if _failure(state, "book_vehicle") else NEXT


DRAFT_OUTPUTS = (
    OutputField("vehicle_id", "int", "ADM 車輛 id"),
    OutputField("vehicle_code", "str", "車號或車輛代碼（給人看）"),
    OutputField("vehicle_name", "str", "車名或車型", required=False),
    OutputField("seats", "int", "座位數", required=False),
    OutputField("start_at", "str", "預約開始時間（ISO 8601，含時區），等於輸入的 booking_start_at"),
    OutputField("end_at", "str", "預約結束時間（ISO 8601，含時區），等於輸入的 booking_end_at"),
    OutputField("destination", "str", "目的地"),
    OutputField("purpose", "str", "用途（例如「客戶拜訪：客戶 C」）"),
)


# ---------------------------------------------------------------- confirm_vehicle（計畫步驟 3）


def _confirm_question(state: State) -> str:
    draft = _out(state, "draft_vehicle")
    return (f"請確認 {visit_window_text(state)} 拜訪「{_customer(state)['name']}」的派車："
            f"{_vehicle_label(draft)}，目的地 {draft.get('destination') or state['input']['location']}")


def _confirm_context(state: State) -> dict[str, Any]:
    draft = _out(state, "draft_vehicle")
    deadline = confirm_deadline(state, datetime.now(TZ))
    return {
        "customer": _customer(state)["name"],
        "visit": visit_window_text(state),
        "location": state["input"]["location"],
        "seats": state["input"]["seats"],
        "vehicle": _vehicle_label(draft),
        "booking_window": f"{_local(draft.get('start_at'))}–{_local(draft.get('end_at'))}",
        "deadline": f"{_md(deadline)} {_hm(deadline)}",
    }


def _confirm_timeout_notice(state: State) -> str:
    deadline = confirm_deadline(state, datetime.now(TZ))
    return (f"{visit_window_text(state)} 拜訪「{_customer(state)['name']}」的派車確認已超過截止時間"
            f"（{_md(deadline)} {_hm(deadline)}），流程已結束、沒有預約車輛。需要用車請自行到 ADM 預約公務車。")


# ---------------------------------------------------------------- book_vehicle（計畫步驟 4）


def _book_inputs(state: State) -> dict[str, Any]:
    draft = _out(state, "draft_vehicle")
    return {
        "vehicle_id": draft.get("vehicle_id"),
        "start_at": draft.get("start_at"),
        "end_at": draft.get("end_at"),
        "purpose": draft.get("purpose"),
        "destination": draft.get("destination"),
        "booked_for_hrm_employee_id": _party(state, "sales")["hrm_employee_id"],
    }


def _book_instruction(state: State) -> str:
    return "\n".join([
        f"依已確認的派車草稿，代業務 {_name(_party(state, 'sales'))} 預約公務車 {_vehicle_label(_out(state, 'draft_vehicle'))}。",
        "用 ADM POST /api/v1/vehicle-bookings/，body 帶 booked_for_hrm_employee_id，"
        "Header 的 Idempotency-Key 一律用上面給的值（重試也一樣）。",
        "ADM 回 409 時段衝突時：status=failed、error_kind=business、"
        "output 帶 conflict=true、http_status=409、vehicle_id。",
        "ADM 回 409「Idempotency key already used …」不是時段衝突：status=needs_human。",
    ])


def book_on_failure(state: State, failure: dict[str, Any]) -> str | None:
    """ADM 409 時段衝突 → 回到 draft_vehicle 重查空車；其他失敗重試同一步。"""
    output = failure.get("output") or {}
    if failure.get("error_kind") == "business" and (output.get("conflict") or output.get("http_status") == 409):
        return "draft_vehicle"
    return None


BOOK_OUTPUTS = (
    OutputField("booking_id", "int", "ADM 預約 id"),
    OutputField("vehicle_id", "int", "ADM 車輛 id"),
    OutputField("start_at", "str", "預約開始時間（ISO 8601）"),
    OutputField("end_at", "str", "預約結束時間（ISO 8601）"),
    OutputField("created", "bool", "true＝這次新建立；false＝冪等重放回傳既有預約", required=False),
)


# ---------------------------------------------------------------- await_trip（計畫步驟 5，業務回報）


def _strict_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} 必須是整數（公里）")
    if value < 0:
        raise ValueError(f"{field} 不能是負數")
    return value


def accept_trip_report(state: State, event: dict[str, Any]) -> dict[str, Any]:
    pickup = _strict_int(event.get("pickup_odometer"), "pickup_odometer")
    returned = _strict_int(event.get("return_odometer"), "return_odometer")
    if returned < pickup:
        raise ValueError("return_odometer 不能小於 pickup_odometer")
    if returned - pickup > MAX_TRIP_KM:
        raise ValueError(f"里程差超過 {MAX_TRIP_KM} km，請確認里程數")
    note = event.get("note")
    if note is not None and not isinstance(note, str):
        raise ValueError("note 必須是字串")
    return {
        "pickup_odometer": pickup,
        "return_odometer": returned,
        "distance_km": returned - pickup,
        "note": (note or "").strip()[:500],
    }


def _trip_summary(output: dict[str, Any]) -> str:
    return (f"取車 {output['pickup_odometer']} km、還車 {output['return_odometer']} km，"
            f"里程差 {output['distance_km']} km")


def _sales_agent(state: State) -> str | None:
    return parties(state).get("sales", {}).get("agent_id")


# ---------------------------------------------------------------- record_trip（計畫步驟 5，代登記）


def _trip_inputs(state: State) -> dict[str, Any]:
    trip = _out(state, "await_trip")
    return {
        "booking_id": _out(state, "book_vehicle").get("booking_id"),
        "pickup_odometer": trip.get("pickup_odometer"),
        "return_odometer": trip.get("return_odometer"),
        "condition_notes": trip.get("note") or "",
    }


def _trip_instruction(state: State) -> str:
    return "\n".join([
        f"業務 {_name(_party(state, 'sales'))} 已回報取車、還車里程，代為在 ADM 登記。",
        "先 POST /api/v1/vehicle-bookings/{booking_id}/pickup/，再 /return/，"
        "body {\"odometer\": ..., \"condition_notes\": ...}。已經登記過的步驟（狀態已是 picked_up／returned）不要重送。",
        "回報 ADM 實際登記的里程與里程差。",
        "ADM 回 409「Pickup is too early」或「Booking window has ended」（不在預約時段內）時："
        "status=needs_human，summary 說明，不要改預約。",
    ])


TRIP_OUTPUTS = (
    OutputField("booking_id", "int", "ADM 預約 id"),
    OutputField("pickup_odometer", "int", "ADM 登記的取車里程"),
    OutputField("return_odometer", "int", "ADM 登記的還車里程"),
    OutputField("distance_km", "int", "里程差（還車 − 取車）"),
)


# ---------------------------------------------------------------- draft_claim（計畫步驟 6）


def _distance(state: State) -> Any:
    return _out(state, "record_trip").get("distance_km", _out(state, "await_trip").get("distance_km"))


def _claim_inputs(state: State) -> dict[str, Any]:
    decision = _decision(state, "approve_claim")
    return {
        "distance_km": _distance(state),
        "visit_date": _visit_day(state).isoformat(),
        "customer": _customer(state),
        "sales": _person(_party(state, "sales")),
        "booking_id": _out(state, "book_vehicle").get("booking_id"),
        "rate_env": "MILEAGE_RATE_TWD_PER_KM",
        "manager_comment": decision.get("comment") if decision.get("decision") == "revise" else None,
    }


def _claim_instruction(state: State) -> str:
    lines = [
        "依里程差 × 費率產生交通費報帳草稿。費率讀環境變數 MILEAGE_RATE_TWD_PER_KM（demo 是假費率，要標明）。",
        "只產生草稿交給 controller 保存，不得寫入任何系統；金額要等業務主管核准才算數。",
    ]
    decision = _decision(state, "approve_claim")
    if decision.get("decision") == "revise":
        lines.append(f"業務主管退回重擬，意見：{decision.get('comment') or '（未說明）'}")
    return "\n".join(lines)


CLAIM_OUTPUTS = (
    OutputField("distance_km", "int", "里程差（km）"),
    OutputField("rate_twd_per_km", "number", "費率（NT$/km）"),
    OutputField("amount_twd", "int", "報帳金額（NT$，四捨五入到元）"),
    OutputField("rate_is_placeholder", "bool", "費率是不是 demo 假費率"),
    OutputField("description", "str", "報帳說明（日期、客戶、里程），不含電話地址"),
)


# ---------------------------------------------------------------- approve_claim（計畫步驟 7）


def _amount_text(claim: dict[str, Any]) -> str:
    placeholder = "（demo 假費率）" if claim.get("rate_is_placeholder") else ""
    return (f"{claim.get('distance_km')} km × NT${claim.get('rate_twd_per_km')}/km{placeholder}"
            f" = NT${claim.get('amount_twd')}")


def _claim_question(state: State) -> str:
    claim = _out(state, "draft_claim")
    return (f"請核准 {_name(_party(state, 'sales'))} {_md(_visit_day(state))} 拜訪「{_customer(state)['name']}」"
            f"的交通費：{_amount_text(claim)}")


def _claim_context(state: State) -> dict[str, Any]:
    claim = _out(state, "draft_claim")
    return {
        "sales": _name(_party(state, "sales")),
        "customer": _customer(state)["name"],
        "visit_date": _visit_day(state).isoformat(),
        "distance_km": claim.get("distance_km"),
        "rate_twd_per_km": claim.get("rate_twd_per_km"),
        "rate_is_placeholder": claim.get("rate_is_placeholder"),
        "amount_twd": claim.get("amount_twd"),
        "description": claim.get("description"),
    }


# ---------------------------------------------------------------- notify_finance（計畫步驟 7 之後）


def finance_notice(state: State, *, approver: str, flow_id: str) -> str:
    """給財會的入帳通知，全部由 controller 依 flow state 組成。

    不引用 finance-agent 草稿的 description：草稿是在核准前寫的，常帶「草稿・待主管核准」之類的字，
    和「已核准」並列會讓財會誤會。
    """
    claim = _out(state, "draft_claim")
    sales = _party(state, "sales")
    booking_id = _out(state, "book_vehicle").get("booking_id")
    vehicle = _vehicle_label(_out(state, "draft_vehicle"))
    lines = [
        f"{_name(sales)} {_md(_visit_day(state))} 拜訪「{_customer(state)['name']}」的交通費已由 {approver} 核准，請入帳。",
        f"金額：{_amount_text(claim)}",
        f"車輛：{vehicle}" + (f"（ADM 預約 #{booking_id}）" if booking_id is not None else ""),
        f"報帳人：{_name(sales)}（{sales.get('employee_number') or sales['hrm_employee_id']}）",
        f"目前沒有會計系統，請手動入帳。流程 {flow_id}",
    ]
    return "\n".join(lines)


def notify_finance(runtime: Any, state: State) -> dict[str, Any]:
    """主管核准後，把報帳結果推到財會的收件匣（LINE 接上前的替代），請財會手動入帳。"""
    finance = _party(state, "finance")
    claim = _out(state, "draft_claim")
    approver = _name(_party(state, "manager"))
    body = finance_notice(state, approver=approver, flow_id=runtime.flow.id)
    runtime.notify(finance["hrm_employee_id"], f"交通費報帳已核准：{runtime.flow.title}", body)
    runtime.set_state("claim", {
        **claim,
        "status": "approved",
        "approved_by": approver,
        "approved_at": runtime.now.isoformat(),
    })
    return {"notified": _name(finance), "amount_twd": claim.get("amount_twd")}


# ---------------------------------------------------------------- close_visit（計畫步驟 8）


def _claim_status(state: State) -> str:
    decision = _decision(state, "approve_claim").get("decision")
    return {"approve": "approved", "reject": "rejected"}.get(decision, "pending")


def _close_inputs(state: State) -> dict[str, Any]:
    trip = _out(state, "await_trip")
    recorded = _out(state, "record_trip")
    return {
        "follow_up_id": state["input"]["follow_up_id"],
        "customer": _customer(state),
        "visit_start_at": state["input"]["visit_start_at"],
        "visit_end_at": state["input"]["visit_end_at"],
        "location": state["input"]["location"],
        "subject": state["input"].get("subject"),
        "sales": _person(_party(state, "sales")),
        "visit_note": trip.get("note") or "",
        "distance_km": _distance(state),
        "claim_status": _claim_status(state),
        # C5 拜訪摘要的交通欄位：車牌、起訖里程（以 ADM 實際登記的為準）
        "vehicle_code": _out(state, "draft_vehicle").get("vehicle_code"),
        "pickup_odometer": recorded.get("pickup_odometer", trip.get("pickup_odometer")),
        "return_odometer": recorded.get("return_odometer", trip.get("return_odometer")),
    }


def _close_instruction(state: State) -> str:
    return "\n".join([
        f"拜訪「{_customer(state)['name']}」已結束，在 CRM 記錄並完成待辦跟進。",
        "先 POST /api/v1/interactions/ 記一筆 kind=visit 的拜訪互動（摘要格式依 CRM docs 的拜訪摘要建議，"
        "內容用 visit_note，交通欄位用 vehicle_code、pickup_odometer、return_odometer、distance_km），再 POST /api/v1/follow-ups/{follow_up_id}/complete/。",
        "互動紀錄不要寫報帳金額。Idempotency-Key：記互動用 Idempotency-Key.interaction，"
        "完成待辦用 Idempotency-Key.complete（CRM 的冪等鍵綁定端點，兩個寫入共用同一個鍵會回 409）。",
    ])


CLOSE_OUTPUTS = (
    OutputField("interaction_id", "str", "CRM 互動紀錄 id（UUID 字串）"),
    OutputField("follow_up_id", "str", "CRM 待辦跟進 id（UUID 字串）"),
    OutputField("follow_up_status", "str", "待辦跟進完成後的狀態（例如 completed）"),
)


# ---------------------------------------------------------------- 定義

DEFINITION = FlowDefinition(
    type=TYPE,
    display_name="客戶拜訪派車報帳",
    validate_input=validate_input,
    make_title=make_title,
    starters=("cs-agent",),
    dedupe_key=lambda data: f"crm:follow-up:{data['follow_up_id']}",
    # 示範模式：cs-agent（crm-visit-list --demo）或 admin 才能啟動
    demo_starters=("cs-agent",),
    demo_check=demo_check,
    steps=(
        ControllerStep(
            name="resolve_parties",
            title="確認業務、主管與財會",
            run=resolve,
            summary=lambda out: f"業務 {out['sales']}、主管 {out['manager']}、財會 {out['finance']}",
        ),
        AgentStep(
            name="draft_vehicle",
            title="查空車、產生派車草稿",
            agent="admin-agent",
            skill="adm-vehicle-draft",
            instruction=_draft_instruction,
            inputs=_draft_inputs,
            outputs=DRAFT_OUTPUTS,
            timeout=timedelta(minutes=5),
            max_attempts=2,
            route=_after_draft,
        ),
        ApprovalStep(
            name="confirm_vehicle",
            title="確認派車",
            approver=lambda state: _party(state, "sales")["hrm_employee_id"],
            question=_confirm_question,
            options=(
                Option("approve", "核准派車"),
                Option("revise", "退回重排"),
                Option("reject", "不需要派車"),
            ),
            # 不需要派車：流程正常結束（沒有預約、沒有報帳），CRM 待辦由業務自己完成
            routes={"approve": NEXT, "revise": "draft_vehicle", "reject": END},
            timeout=confirm_deadline,
            on_timeout="expire",
            context=_confirm_context,
            timeout_notice=_confirm_timeout_notice,
        ),
        AgentStep(
            name="book_vehicle",
            title="代業務預約公務車",
            agent="admin-agent",
            skill="adm-vehicle-book",
            instruction=_book_instruction,
            inputs=_book_inputs,
            outputs=BOOK_OUTPUTS,
            timeout=timedelta(minutes=5),
            # 409 時段衝突回 draft_vehicle 重查，最多 2 次（共 3 次），之後交給人
            max_attempts=3,
            on_failure=book_on_failure,
        ),
        WaitStep(
            name="await_trip",
            title="等業務回報取車還車里程",
            agent="sales",
            reporter=_sales_agent,
            event_type="trip_report",
            accept_event=accept_trip_report,
            event_summary=_trip_summary,
            timeout=trip_deadline,
            on_timeout="stuck",
        ),
        AgentStep(
            name="record_trip",
            title="代登記取車、還車",
            agent="admin-agent",
            skill="adm-vehicle-trip",
            instruction=_trip_instruction,
            inputs=_trip_inputs,
            outputs=TRIP_OUTPUTS,
            timeout=timedelta(minutes=5),
            max_attempts=2,
        ),
        AgentStep(
            name="draft_claim",
            title="產生交通費報帳草稿",
            agent="finance-agent",
            skill="mileage-claim-draft",
            instruction=_claim_instruction,
            inputs=_claim_inputs,
            outputs=CLAIM_OUTPUTS,
            timeout=timedelta(minutes=5),
            max_attempts=2,
        ),
        ApprovalStep(
            name="approve_claim",
            title="核准交通費報帳",
            approver=lambda state: _party(state, "manager")["hrm_employee_id"],
            question=_claim_question,
            options=(
                Option("approve", "核准"),
                Option("revise", "退回重擬"),
                Option("reject", "不核准"),
            ),
            # 不核准：不通知財會，照常完成 CRM 的拜訪紀錄
            routes={"approve": NEXT, "revise": "draft_claim", "reject": "close_visit"},
            timeout=claim_approval_deadline,
            on_timeout="stuck",
            context=_claim_context,
        ),
        ControllerStep(
            name="notify_finance",
            title="通知財會入帳",
            run=notify_finance,
            summary=lambda out: f"已通知 {out['notified']} 入帳 NT${out['amount_twd']}",
        ),
        AgentStep(
            name="close_visit",
            title="記拜訪互動、完成待辦跟進",
            agent="cs-agent",
            skill="crm-visit-close",
            instruction=_close_instruction,
            inputs=_close_inputs,
            outputs=CLOSE_OUTPUTS,
            write_keys=("interaction", "complete"),
            timeout=timedelta(minutes=5),
            max_attempts=2,
            route=lambda state, output: END,
        ),
    ),
)
