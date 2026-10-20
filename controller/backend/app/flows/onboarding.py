"""流程 ③：新人到職（計畫第 2 節 ③、3.6、7 的 ③ 驗收）。

計畫步驟與 controller 步驟的對照（每一步的輸入與 output 欄位見 docs/flows/onboarding.md）：

| 計畫 | controller 步驟   | 種類       | 執行者                         | skill                    |
|------|-------------------|------------|--------------------------------|--------------------------|
| 1    | （啟動流程）       | —          | hr-agent（onboarding-list）     | flow-start               |
| —    | resolve_parties   | controller | controller                     | —                        |
| 2    | draft_laptop      | agent      | it-agent                       | laptop-allocation-draft  |
| 3    | notify_it         | controller | controller → IT 人員收件匣       | —                        |
| 3    | await_laptop      | 等待事件    | IT 人員（laptop_assigned）       | —（souei：agent-adm assign-laptop） |
| 4    | book_room         | agent      | admin-agent                    | adm-room-book            |
| 5    | onboarding_notice | agent      | hr-agent                       | onboarding-notice        |
| 5    | confirm_manager   | controller | controller                     | —                        |
| 6    | ack_manager       | 人工確認    | 新人的部門主管                   | —                        |
| 6    | report_gap        | controller | controller → HR 收件匣（主管回報缺漏時） | —                 |

- 確認對象一律從 Identity 解析，**部門沒有主管（或不只一位）時停下並通知 HR 與 admin，不猜一個人**
  （7 的 ③ 驗收）。resolve_parties 先用 Identity 檢查一次；onboarding_notice 回報 HRM 的部門主管後，
  confirm_manager 再比對一次（HRM 查不到主管、或和 Identity 不一致，也停下通知 HR）。
- 筆電的保管人只能由 IT 人員在 ADM 指派（A4 僅限 session 登入的 staff）：controller 把配發建議送到 IT 人員的
  收件匣（kind=task），IT 人員的員工 agent 指派完用自己的 token 送 laptop_assigned 事件；
  controller 只接受「這條流程解析出來的 IT 人員（Identity role=it）」送來的事件。
- 到職說明的會議室代訂給 HR（role=hr）：HR 主持到職說明，且 HR 一定存在（部門主管可能缺）。
- 到職清單不含電話、地址：output 欄位名含 phone／address 的由 sanitize 濾掉；清單文字裡像電話的號碼、
  像台灣地址的片段，存進流程前就遮掉（AgentStep.transform_output）。
"""

from __future__ import annotations

import re
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
from app.notify import KIND_ALERT

TYPE = "onboarding"
TZ = ZoneInfo("Asia/Taipei")

# Identity.role 的值
ROLE_MANAGER = "manager"
ROLE_IT = "it"
ROLE_HR = "hr"

# 給 IT 人員的指派任務通知（收件匣 kind）；agent-inbox 只列出、不處理，由 IT 人員的 skill 處理
KIND_TASK = "task"
EVENT_LAPTOP_ASSIGNED = "laptop_assigned"

# 到職說明：到職當天 09:30–10:30，找不到時可在 09:00–12:00 之間調整
ORIENTATION_START = time(9, 30)
ORIENTATION_MINUTES = 60
ORIENTATION_EARLIEST = time(9, 0)
ORIENTATION_LATEST = time(12, 0)
# 等 IT 指派筆電的截止：到職前一個工作天 18:00（至少給 4 小時）
LAPTOP_CUTOFF = time(18, 0)
LAPTOP_MIN_WAIT = timedelta(hours=4)
# 主管確認收到：2 個工作天（計畫 3.6），逾時只標卡住
ACK_BUSINESS_DAYS = 2

# 清單文字裡像電話的號碼（台灣手機、市話、國際碼；區碼可加括號，分隔可用 - 空白 .），給主管看之前遮掉
PHONE_PATTERN = re.compile(
    r"(?<![0-9A-Za-z+])(?:\+?886[-\s.]?\(?0?|\(?0)(?:\d[-\s.)]{0,2}){7,9}\d(?![0-9A-Za-z])"
)
# 清單文字裡像台灣地址的片段（〔縣市〕〔區〕路／街＋〔段巷弄〕＋門牌號〔樓〕）
_DIGITS = "0-9０-９"
ADDRESS_PATTERN = re.compile(
    rf"(?:[\u4e00-\u9fff]{{2}}[市縣])?(?:[\u4e00-\u9fff]{{1,3}}[區鄉鎮市])?"
    rf"[\u4e00-\u9fff]{{1,8}}?(?:路|街|大道)(?:[一二三四五六七八九十{_DIGITS}]+段)?"
    rf"(?:[{_DIGITS}]+巷)?(?:[{_DIGITS}]+弄)?[{_DIGITS}]+(?:之[{_DIGITS}]+)?號"
    rf"(?:[{_DIGITS}]+樓)?(?:之[{_DIGITS}]+)?"
)
REDACTED = "［已移除］"


# ---------------------------------------------------------------- 工作天


def add_business_days(start: datetime, days: int) -> datetime:
    """往後數 days 個工作天（週一～週五），保留時刻。國定假日不處理（demo 環境）。"""
    current = start
    remaining = days
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def previous_business_day(day: date) -> date:
    current = day - timedelta(days=1)
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current


# ---------------------------------------------------------------- 初始資料


def _text(value: Any, field: str, *, limit: int, required: bool = True) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError(f"{field} 必填")
        return None
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError(f"{field} 必須是字串")
    return str(value).strip()[:limit]


def _department_id(value: Any) -> int | str:
    if isinstance(value, bool) or value in (None, ""):
        raise ValueError("department_id 必填（HRM 部門 id）")
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    if not isinstance(value, (int, str)):
        raise ValueError("department_id 格式錯誤")
    return value.strip() if isinstance(value, str) else value


def validate_input(data: dict[str, Any]) -> dict[str, Any]:
    raw_date = data.get("start_date")
    if not isinstance(raw_date, str) or not raw_date.strip():
        raise ValueError("start_date 必填（YYYY-MM-DD）")
    try:
        start_date = date.fromisoformat(raw_date.strip()[:10])
    except ValueError as exc:
        raise ValueError(f"start_date 格式錯誤：{raw_date}") from exc
    # 只取流程需要的欄位：姓名、員工編號、部門、職稱、到職日（不收電話、地址）
    return {
        "employee_id": _text(data.get("employee_id"), "employee_id", limit=64),
        "name": _text(data.get("name"), "name", limit=100),
        "employee_number": _text(data.get("employee_number"), "employee_number", limit=32),
        "department_id": _department_id(data.get("department_id")),
        "department_name": _text(data.get("department_name"), "department_name", limit=100),
        "job_title": _text(data.get("job_title"), "job_title", limit=100, required=False),
        "start_date": start_date.isoformat(),
    }


def _md(value: datetime | date) -> str:
    return f"{value.month}/{value.day}"


def _start_date(state: State) -> date:
    return date.fromisoformat(state["input"]["start_date"])


def make_title(data: dict[str, Any]) -> str:
    return f"{_md(date.fromisoformat(data['start_date']))} 到職 {data['name']}（{data['department_name']}）"


def _new_hire_text(state: State) -> str:
    data = state["input"]
    return f"{data['name']}（{data['department_name']}，{_md(_start_date(state))} 到職）"


def laptop_deadline(state: State, now: datetime) -> datetime:
    """等 IT 指派筆電：到職前一個工作天 18:00；離現在不到 4 小時就給 4 小時。"""
    cutoff = datetime.combine(previous_business_day(_start_date(state)), LAPTOP_CUTOFF, tzinfo=TZ)
    return max(cutoff, now + LAPTOP_MIN_WAIT)


def ack_deadline(state: State, now: datetime) -> datetime:
    """主管確認收到：2 個工作天。"""
    return add_business_days(now.astimezone(TZ), ACK_BUSINESS_DAYS)


def orientation_window(state: State) -> dict[str, str]:
    day = _start_date(state)
    start = datetime.combine(day, ORIENTATION_START, tzinfo=TZ)
    return {
        "preferred_start_at": start.isoformat(),
        "preferred_end_at": (start + timedelta(minutes=ORIENTATION_MINUTES)).isoformat(),
        "earliest_start_at": datetime.combine(day, ORIENTATION_EARLIEST, tzinfo=TZ).isoformat(),
        "latest_end_at": datetime.combine(day, ORIENTATION_LATEST, tzinfo=TZ).isoformat(),
    }


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


def _person(party: dict[str, Any]) -> dict[str, Any]:
    return {"hrm_employee_id": party["hrm_employee_id"], "display_name": _name(party)}


def _out(state: State, step: str) -> dict[str, Any]:
    return (state.get("steps") or {}).get(step) or {}


def _decision(state: State, step: str) -> dict[str, Any]:
    return (state.get("decisions") or {}).get(step) or {}


def _new_hire(state: State) -> dict[str, Any]:
    data = state["input"]
    return {
        "hrm_employee_id": data["employee_id"],
        "name": data["name"],
        "employee_number": data["employee_number"],
        "department_id": data["department_id"],
        "department_name": data["department_name"],
        "job_title": data.get("job_title"),
        "start_date": data["start_date"],
    }


def _local(value: str | None) -> str:
    if not value:
        return "（未提供）"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(TZ)
    except ValueError:
        return value
    return f"{_md(parsed)} {parsed.strftime('%H:%M')}"


def _room_text(state: State) -> str:
    room = _out(state, "book_room")
    if not room:
        return "（未預約）"
    name = room.get("room_name") or f"#{room.get('room_id')}"
    end = _local(room.get("end_at")).split(" ")[-1]
    return f"{name} {_local(room.get('start_at'))}–{end}"


def _laptop_code(state: State) -> str | None:
    return _out(state, "await_laptop").get("asset_code") or _out(state, "draft_laptop").get("asset_code")


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
        raise StepStop(f"{what}有 {len(found)} 位（{names}），無法決定由誰處理")
    return found[0]


def _stop_and_tell_hr(runtime: Any, hr: Identity | dict[str, Any], state: State, problem: str) -> None:
    """部門主管的問題：通知 HR（不猜一個人），再停下（StepStop 另外通知 admin）。"""
    recipient = hr if isinstance(hr, Identity) else hr["hrm_employee_id"]
    runtime.notify(
        recipient,
        f"新人到職流程停下：{runtime.flow.title}",
        f"{_new_hire_text(state)}的到職流程停下：{problem}。流程不會自己挑主管，"
        f"請在 HRM 指定{state['input']['department_name']}的主管（role=manager），"
        f"並請管理員更新 controller 的 Identity 後，在流程詳情按「重試這一步」。流程 {runtime.flow.id}",
        kind=KIND_ALERT,
    )
    raise StepStop(problem)


def _resolve_manager(runtime: Any, state: State, hr: Identity | dict[str, Any]) -> Identity:
    """新人的部門主管：Identity 同部門、role=manager、不是新人本人，剛好一位；否則通知 HR 並停下。"""
    department = state["input"]["department_name"]
    managers = list(runtime.db.scalars(
        select(Identity).where(Identity.department == department, Identity.role == ROLE_MANAGER,
                               Identity.hrm_employee_id != state["input"]["employee_id"])
    ).all())
    if not managers:
        _stop_and_tell_hr(runtime, hr, state, f"{department}沒有部門主管（Identity 同部門、role={ROLE_MANAGER}）")
    if len(managers) > 1:
        names = "、".join(i.display_name or i.hrm_employee_id for i in managers)
        _stop_and_tell_hr(runtime, hr, state, f"{department}有 {len(managers)} 位主管（{names}），無法決定由誰確認")
    return managers[0]


def resolve(runtime: Any, state: State) -> dict[str, Any]:
    """HR、IT 人員、新人的部門主管一律從 Identity 解析；找不到或不只一位就停下，不猜。"""
    db = runtime.db
    hr = _only_one(list(db.scalars(select(Identity).where(Identity.role == ROLE_HR)).all()),
                   f"人資（role={ROLE_HR}）")
    it = _only_one(list(db.scalars(select(Identity).where(Identity.role == ROLE_IT)).all()),
                   f"IT 人員（role={ROLE_IT}）")
    manager = _resolve_manager(runtime, state, hr)

    resolved = {"hr": _brief(hr), "it": _brief(it), "manager": _brief(manager)}
    runtime.set_state("parties", resolved)
    # IT 人員的員工 agent 要能 GET /flows/{id} 讀配發建議
    runtime.set_state("participants", [it.agent_id] if it.agent_id else [])
    return {key: _name(value) for key, value in resolved.items()}


# ---------------------------------------------------------------- draft_laptop（計畫步驟 2）


def _laptop_inputs(state: State) -> dict[str, Any]:
    return {
        "new_hire": _new_hire(state),
        "category": "laptop",
        "assets_query": "GET /api/v1/assets/?category=laptop&unassigned=true&is_active=true",
    }


def _laptop_instruction(state: State) -> str:
    return "\n".join([
        f"新人 {_new_hire_text(state)} 需要一台筆電，產生配發建議。",
        "用 ADM GET /api/v1/assets/?category=laptop&unassigned=true&is_active=true 查還沒有保管人的筆電，"
        "依新人的部門與職稱挑一台建議配發，附上規格與理由。",
        "只產生建議，**不得指派保管人**（ADM 的指派僅限 IT 人員以 session 登入操作）。",
        "沒有任何未指派的筆電時回報 status=needs_human，summary 說明原因。",
    ])


LAPTOP_OUTPUTS = (
    OutputField("asset_code", "str", "建議配發的 ADM 資產編號"),
    OutputField("asset_name", "str", "資產名稱或型號"),
    OutputField("specs", "str", "規格摘要（CPU／記憶體／儲存）", required=False),
    OutputField("reason", "str", "為什麼建議這一台（一句話）"),
    OutputField("alternatives", "list", "其他可配發的資產編號（依建議順序）", required=False),
    OutputField("available_count", "int", "目前未指派的筆電數量", required=False),
)


# ---------------------------------------------------------------- notify_it／await_laptop（計畫步驟 3）


def notify_it(runtime: Any, state: State) -> dict[str, Any]:
    """把配發建議送到 IT 人員的收件匣（kind=task），請 IT 人員在 ADM 指派後回報。"""
    it = _party(state, "it")
    draft = _out(state, "draft_laptop")
    data = state["input"]
    alternatives = draft.get("alternatives") or []
    alt_text = f"；備選 {'、'.join(str(a) for a in alternatives[:3])}" if alternatives else ""
    body = (
        f"請在 ADM 把筆電指派給新人 {data['name']}（員工編號 {data['employee_number']}，{data['department_name']}"
        f"{'，' + data['job_title'] if data.get('job_title') else ''}，{_md(_start_date(state))} 到職）。"
        f"建議資產 {draft.get('asset_code')}（{draft.get('asset_name') or '筆電'}）{alt_text}。"
        f"理由：{draft.get('reason') or '（無）'}。"
        f"指派完成後，用你自己的 controller token 回報："
        f"POST /api/flows/{runtime.flow.id}/events {{\"type\": \"{EVENT_LAPTOP_ASSIGNED}\", "
        f"\"asset_code\": \"<實際指派的資產編號>\"}}。流程 {runtime.flow.id}"
    )
    runtime.notify(it["hrm_employee_id"], f"IT 任務：配發筆電給新人 {data['name']}", body, kind=KIND_TASK)
    return {"notified": _name(it), "asset_code": draft.get("asset_code")}


def _it_agent(state: State) -> str | None:
    it = parties(state).get("it") or {}
    # 只有 Identity role=it 的人（resolve_parties 解析出來的 IT 人員）扮演的 agent 能回報
    if it.get("role") != ROLE_IT:
        return None
    return it.get("agent_id")


def accept_laptop_assigned(state: State, event: dict[str, Any]) -> dict[str, Any]:
    code = event.get("asset_code")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("asset_code 必填（實際指派的 ADM 資產編號）")
    code = code.strip()[:64]
    note = event.get("note")
    if note is not None and not isinstance(note, str):
        raise ValueError("note 必須是字串")
    suggested = _out(state, "draft_laptop").get("asset_code")
    return {
        "asset_code": code,
        "suggested_asset_code": suggested,
        "matches_suggestion": code == suggested,
        "custodian_hrm_employee_id": state["input"]["employee_id"],
        "note": (note or "").strip()[:500],
    }


def _assigned_summary(output: dict[str, Any]) -> str:
    extra = "" if output["matches_suggestion"] else f"（建議是 {output.get('suggested_asset_code')}）"
    return f"IT 已在 ADM 指派 {output['asset_code']}{extra}"


# ---------------------------------------------------------------- book_room（計畫步驟 4）


def _room_inputs(state: State) -> dict[str, Any]:
    hr = _party(state, "hr")
    manager = _party(state, "manager")
    data = state["input"]
    return {
        "date": data["start_date"],
        **orientation_window(state),
        "duration_minutes": ORIENTATION_MINUTES,
        "capacity": 3,
        "purpose": f"新人到職說明：{data['name']}（{data['department_name']}）",
        "booked_for_hrm_employee_id": hr["hrm_employee_id"],
        "booked_for": _person(hr),
        "attendees": [data["name"], _name(manager), _name(hr)],
    }


def _room_instruction(state: State) -> str:
    hr = _party(state, "hr")
    return "\n".join([
        f"預約新人 {_new_hire_text(state)} 到職第一天的說明會議室，代訂給人資 {_name(hr)}（由人資主持到職說明）。",
        "用 ADM GET /api/v1/rooms/availability/?start_at&end_at&capacity 查空會議室，"
        "優先 preferred_start_at～preferred_end_at；沒有就在 earliest_start_at～latest_end_at 之間找 duration_minutes 的時段。",
        "用 ADM POST /api/v1/room-bookings/ 預約，body 帶 booked_for_hrm_employee_id，"
        "Header 的 Idempotency-Key 一律用上面給的值（重試也一樣）。",
        "預約前先查 GET /api/v1/room-bookings/：已經有自己代訂、booked_for 與 purpose 相同、同一天的預約"
        "（上一次嘗試其實已成功）就直接回報那一筆（created=false），不要再訂。",
        "ADM 回 409 時段衝突：換一間或換時段再試（同一個 Idempotency-Key 不能用在不同內容，"
        "換內容時改用 Idempotency-Key 加上 :2、:3 後綴）；整個範圍都沒有空會議室時 status=needs_human。",
    ])


ROOM_OUTPUTS = (
    OutputField("booking_id", "int", "ADM 會議室預約 id"),
    OutputField("room_id", "int", "ADM 會議室 id"),
    OutputField("room_name", "str", "會議室名稱"),
    OutputField("start_at", "str", "預約開始時間（ISO 8601，含時區）"),
    OutputField("end_at", "str", "預約結束時間（ISO 8601，含時區）"),
    OutputField("created", "bool", "true＝這次新建立；false＝冪等重放回傳既有預約", required=False),
)


# ---------------------------------------------------------------- onboarding_notice（計畫步驟 5）


def _notice_inputs(state: State) -> dict[str, Any]:
    manager = _party(state, "manager")
    hr = _party(state, "hr")
    room = _out(state, "book_room")
    decision = _decision(state, "ack_manager")
    return {
        "new_hire": _new_hire(state),
        "department_id": state["input"]["department_id"],
        "managers_query": f"GET /api/departments/{state['input']['department_id']}/managers/",
        "expected_manager": _person(manager),
        "hr": _person(hr),
        "laptop": {"asset_code": _laptop_code(state),
                   "asset_name": _out(state, "draft_laptop").get("asset_name")},
        "orientation": {"room_name": room.get("room_name"), "start_at": room.get("start_at"),
                        "end_at": room.get("end_at"), "booking_id": room.get("booking_id")},
        "manager_comment": decision.get("comment") if decision.get("decision") == "missing" else None,
    }


def _notice_instruction(state: State) -> str:
    return "\n".join([
        f"為新人 {_new_hire_text(state)} 產生到職清單，給部門主管確認收到。",
        f"先用 HRM GET /api/departments/{state['input']['department_id']}/managers/ 查部門主管，"
        "把查到的每一位主管的員工 id 放進 manager_hrm_employee_ids（查不到就回空陣列，仍然 status=ok）。"
        "**不要自己挑一個人當主管**，由 controller 比對並決定要不要停下通知 HR。",
        "到職清單列出：姓名、員工編號、部門、職稱、到職日、筆電資產編號、到職說明的會議室與時間、"
        "主管第一週要做的事。**不得包含電話、地址**（新人與任何人的都不行）。",
    ])


NOTICE_OUTPUTS = (
    OutputField("manager_hrm_employee_ids", "list", "HRM 回傳的部門主管員工 id（查不到就是空陣列）"),
    OutputField("checklist", "str", "到職清單全文（給主管看，不含電話、地址）"),
    OutputField("items", "list", "清單項目（字串陣列）", required=False),
)


# ---------------------------------------------------------------- confirm_manager（計畫步驟 5 之後）


def redact_pii(text: str) -> tuple[str, bool]:
    """遮掉文字裡像電話、地址的片段；回傳（遮過的文字, 有沒有遮）。"""
    cleaned, phones = PHONE_PATTERN.subn(REDACTED, text or "")
    cleaned, addresses = ADDRESS_PATTERN.subn(REDACTED, cleaned)
    return cleaned, phones + addresses > 0


def clean_notice(state: State, output: dict[str, Any]) -> dict[str, Any]:
    """onboarding_notice 的 output 存進流程前，遮掉清單文字裡像電話、地址的片段（7 的 ③ 驗收：清單不含電話地址）。"""
    checklist, redacted = redact_pii(str(output.get("checklist") or ""))
    output["checklist"] = checklist
    items = output.get("items")
    if isinstance(items, list):
        cleaned_items = []
        for item in items:
            text, changed = redact_pii(str(item))
            redacted = redacted or changed
            cleaned_items.append(text)
        output["items"] = cleaned_items
    output["pii_redacted"] = redacted
    return output


def confirm_manager(runtime: Any, state: State) -> dict[str, Any]:
    """比對 HRM 的部門主管（hr-agent 回報）與 Identity 解析的主管；對不上就停下通知 HR，不猜。

    停下後 admin 按「重試這一步」：HRM 的結果已經過時，所以先從 Identity 重新解析主管（管理員可能更新了對照表），
    再改回 onboarding_notice 請 hr-agent 重查（state.manager_recheck 記住上一次是停下的）。
    """
    hr = _party(state, "hr")
    if state.get("manager_recheck"):
        # 停下的原因可能是 Identity 的對照表錯了：管理員更新 Identity 後，主管也要重新解析，
        # 否則重查 HRM 後仍拿舊的 parties.manager 比對，永遠對不上
        identity = _resolve_manager(runtime, state, hr)
        refreshed = {**parties(state), "manager": _brief(identity)}
        runtime.set_state("parties", refreshed)
        runtime.set_state("manager_recheck", False)
        return {"manager": _name(refreshed["manager"]), "pii_redacted": False, "recheck": True}
    manager = _party(state, "manager")
    notice = _out(state, "onboarding_notice")
    raw_ids = notice.get("manager_hrm_employee_ids") or []
    ids = [str(item) for item in raw_ids] if isinstance(raw_ids, list) else []
    department = state["input"]["department_name"]
    if not ids or manager["hrm_employee_id"] not in ids or len(ids) > 1:
        runtime.set_state("manager_recheck", True)
    if not ids:
        _stop_and_tell_hr(runtime, hr, state, f"HRM 查不到{department}的部門主管")
    if manager["hrm_employee_id"] not in ids:
        _stop_and_tell_hr(runtime, hr, state,
                          f"HRM 的{department}主管和 controller 的對照表（{_name(manager)}）不一致")
    if len(ids) > 1:
        _stop_and_tell_hr(runtime, hr, state, f"HRM 的{department}有 {len(ids)} 位主管，無法決定由誰確認")
    return {"manager": _name(manager), "pii_redacted": bool(notice.get("pii_redacted"))}


def _confirm_summary(out: dict[str, Any]) -> str:
    if out.get("recheck"):
        return "重試：請 hr-agent 重新查 HRM 的部門主管"
    return f"主管 {out['manager']}" + ("（清單裡的電話已遮掉）" if out["pii_redacted"] else "")


# ---------------------------------------------------------------- ack_manager（計畫步驟 6）


def _ack_question(state: State) -> str:
    return f"請確認收到新人 {_new_hire_text(state)} 的到職清單；有缺漏請選「有缺漏」並說明，會轉給人資處理"


def _ack_context(state: State) -> dict[str, Any]:
    data = state["input"]
    return {
        "new_hire": data["name"],
        "employee_number": data["employee_number"],
        "department": data["department_name"],
        "job_title": data.get("job_title"),
        "start_date": data["start_date"],
        "laptop": _laptop_code(state),
        "orientation": _room_text(state),
        "hr": _name(_party(state, "hr")),
        "checklist": _out(state, "onboarding_notice").get("checklist") or "",
    }


# ---------------------------------------------------------------- report_gap（主管回報缺漏）


def report_gap(runtime: Any, state: State) -> dict[str, Any]:
    """主管回報清單有缺漏：把意見轉到 HR 收件匣，流程結束（後續由 HR 處理）。"""
    hr = _party(state, "hr")
    manager = _party(state, "manager")
    comment = _decision(state, "ack_manager").get("comment") or "（未說明）"
    runtime.notify(
        hr["hrm_employee_id"],
        f"到職清單有缺漏：{runtime.flow.title}",
        f"{_name(manager)} 回報 {_new_hire_text(state)} 的到職清單有缺漏：{comment}。請補齊後直接聯絡主管。"
        f"流程 {runtime.flow.id}",
    )
    return {"notified": _name(hr), "comment": comment}


# ---------------------------------------------------------------- 定義

DEFINITION = FlowDefinition(
    type=TYPE,
    display_name="新人到職",
    validate_input=validate_input,
    make_title=make_title,
    starters=("hr-agent",),
    dedupe_key=lambda data: f"hrm:employee:{data['employee_id']}",
    steps=(
        ControllerStep(
            name="resolve_parties",
            title="確認人資、IT 與部門主管",
            run=resolve,
            summary=lambda out: f"人資 {out['hr']}、IT {out['it']}、主管 {out['manager']}",
        ),
        AgentStep(
            name="draft_laptop",
            title="查未指派筆電、產生配發建議",
            agent="it-agent",
            skill="laptop-allocation-draft",
            instruction=_laptop_instruction,
            inputs=_laptop_inputs,
            outputs=LAPTOP_OUTPUTS,
            timeout=timedelta(minutes=5),
            max_attempts=2,
        ),
        ControllerStep(
            name="notify_it",
            title="通知 IT 人員指派筆電",
            run=notify_it,
            summary=lambda out: f"已通知 {out['notified']} 指派 {out['asset_code']}",
        ),
        WaitStep(
            name="await_laptop",
            title="等 IT 人員在 ADM 指派筆電",
            agent="it",
            reporter=_it_agent,
            event_type=EVENT_LAPTOP_ASSIGNED,
            accept_event=accept_laptop_assigned,
            event_summary=_assigned_summary,
            timeout=laptop_deadline,
            on_timeout="stuck",
        ),
        AgentStep(
            name="book_room",
            title="預約到職說明會議室",
            agent="admin-agent",
            skill="adm-room-book",
            instruction=_room_instruction,
            inputs=_room_inputs,
            outputs=ROOM_OUTPUTS,
            timeout=timedelta(minutes=5),
            max_attempts=2,
        ),
        AgentStep(
            name="onboarding_notice",
            title="查部門主管、產生到職清單",
            agent="hr-agent",
            skill="onboarding-notice",
            instruction=_notice_instruction,
            inputs=_notice_inputs,
            outputs=NOTICE_OUTPUTS,
            transform_output=clean_notice,
            timeout=timedelta(minutes=5),
            max_attempts=2,
        ),
        ControllerStep(
            name="confirm_manager",
            title="比對部門主管",
            run=confirm_manager,
            summary=_confirm_summary,
            route=lambda state, output: "onboarding_notice" if output.get("recheck") else NEXT,
        ),
        ApprovalStep(
            name="ack_manager",
            title="主管確認收到到職清單",
            approver=lambda state: _party(state, "manager")["hrm_employee_id"],
            question=_ack_question,
            options=(
                Option("ack", "確認收到"),
                Option("missing", "有缺漏（請在意見說明）"),
            ),
            routes={"ack": END, "missing": "report_gap"},
            timeout=ack_deadline,
            on_timeout="stuck",
            context=_ack_context,
        ),
        ControllerStep(
            name="report_gap",
            title="轉告人資清單缺漏",
            run=report_gap,
            summary=lambda out: f"已轉告 {out['notified']}：{out['comment']}",
            route=lambda state, output: END,
        ),
    ),
)
