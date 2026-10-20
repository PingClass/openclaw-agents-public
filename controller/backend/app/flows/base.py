"""流程定義的介面（計畫 3.6）。

每種流程一個模組（flows/<type>.py），寫死步驟、指派、時限、失敗回到哪一步、最大重試。
不做通用的流程引擎：engine 只照這裡的定義推進。

流程狀態 flow.state 的結構：
    {
      "input": {...},                 # 啟動時的初始資料（已驗證）
      "steps": {"<step>": {...}},     # 每個 agent 步驟回報的結構化輸出
      "decisions": {"<step>": {"decision": "...", "comment": "..."}},
      "failures": {"<step>": {"count": n, "error": "...", "error_kind": "...", "output": {...}}},
      ...                             # 流程自己的欄位（例如 visit 的 parties、claim）
    }

步驟種類：
- AgentStep：派給助理 agent，agent 用 flow-report 回報。
- WaitStep：等待外部事件（例如業務回報取車還車里程），不派工。
- ApprovalStep：人工確認（一次性連結）。
- ControllerStep：controller 自己在交易內完成的動作（解析確認對象、發通知），不派工。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

# 路由目標：下一步（預設）、整條流程成功結束、整條流程失敗
NEXT = "__next__"
END = "__end__"
FAIL = "__fail__"

State = dict[str, Any]
# 失敗資訊：{"error_kind": ..., "error": ..., "output": {...}}
Failure = dict[str, Any]
# 時限：固定長度，或依流程狀態算出截止時間（例如「拜訪前一天 18:00」）
Deadline = timedelta | Callable[[State, datetime], datetime]


def resolve_deadline(deadline: Deadline, state: State, now: datetime) -> datetime:
    if isinstance(deadline, timedelta):
        return now + deadline
    return deadline(state, now)


@dataclass(frozen=True)
class Option:
    value: str
    label: str


@dataclass(frozen=True)
class OutputField:
    """agent 用 flow-report 回報時，output 裡的一個欄位（寫進派工指令，也用來檢查回報）。"""

    name: str
    type: str
    description: str
    required: bool = True


@dataclass(frozen=True)
class AgentStep:
    """派給 agent 的步驟。"""

    name: str
    title: str
    agent: str
    instruction: Callable[[State], str]
    timeout: Deadline = timedelta(minutes=5)
    # 最多派工幾次（含第一次）；用完就停下來交給人（卡住原因：重試用完）
    max_attempts: int = 1
    # 失敗且還有次數時回到哪一步：步驟名稱，或依失敗內容決定的函式（回傳 None 代表重試同一步）；
    # None 代表重試同一步
    on_failure: str | Callable[[State, Failure], str | None] | None = None
    # 成功後去哪：預設下一步；也可以依輸出決定
    route: Callable[[State, dict[str, Any]], str] | None = None
    # agent 要用的 skill（寫進派工指令）
    skill: str | None = None
    # 給 agent 的結構化輸入（寫進派工指令的 JSON 區塊）
    inputs: Callable[[State], dict[str, Any]] | None = None
    # 回報 status=ok 時 output 應有的欄位；required 的欄位缺少時回報會被拒（422）
    outputs: tuple[OutputField, ...] = ()
    # 一步裡有多個寫入時，各寫入用的冪等鍵後綴（例如 CRM 的冪等紀錄綁定 method＋path，
    # 同一個鍵用在兩個端點會回 409）。派工指令會列出 Idempotency-Key.<後綴>: {flow_id}:{step}:<後綴>
    write_keys: tuple[str, ...] = ()
    # 回報 status=ok 時，對 output 做進一步檢查（例如金額必須和主管決定一致）；有問題丟 ValueError（API 回 422）
    validate_output: Callable[[State, dict[str, Any]], None] | None = None
    # status=ok 的 output 存進流程前的整理（例如遮掉文字裡的電話）；None 代表原樣保存（sanitize 之後）
    transform_output: Callable[[State, dict[str, Any]], dict[str, Any]] | None = None
    kind: str = field(default="agent", init=False)


@dataclass(frozen=True)
class WaitStep:
    """等待外部事件（例如業務回報還車）。不主動派工，由指定 agent 回報。"""

    name: str
    title: str
    # 預設的回報者（agent id）；有 reporter 時進入步驟才依狀態決定
    agent: str
    timeout: Deadline
    # 逾時處理：expire（流程標為 expired）／fail／stuck（只標卡住，等人處理）
    on_timeout: str = "stuck"
    route: Callable[[State, dict[str, Any]], str] | None = None
    # 依流程狀態決定誰可以回報（例如「這條流程的業務本人」）；回傳 None 代表沒有人能回報
    reporter: Callable[[State], str | None] | None = None
    # 有設定時，只能用 POST /flows/{id}/events 送這個類型的事件完成這一步（不接受 flow-report）
    event_type: str | None = None
    # 驗證並整理事件內容，回傳寫進 output 的資料；有問題丟 ValueError（API 回 422）
    accept_event: Callable[[State, dict[str, Any]], dict[str, Any]] | None = None
    # 事件的摘要（時間軸顯示）
    event_summary: Callable[[dict[str, Any]], str] | None = None
    kind: str = field(default="wait", init=False)


@dataclass(frozen=True)
class ApprovalStep:
    """人工確認點：建立 Approval＋一次性 token，透過 Notifier 通知確認者。"""

    name: str
    title: str
    # 回傳確認者的 hrm_employee_id
    approver: Callable[[State], str]
    question: Callable[[State], str]
    options: tuple[Option, ...] = (Option("approve", "核准"), Option("reject", "退回"))
    # 決定 → 路由目標（步驟名稱、NEXT、END、FAIL）
    routes: dict[str, str] = field(default_factory=lambda: {"approve": NEXT, "reject": FAIL})
    timeout: Deadline = timedelta(hours=24)
    # 逾時處理：expire（流程標為 expired）／fail／stuck
    on_timeout: str = "expire"
    # 確認頁只顯示這裡挑出來的內容
    context: Callable[[State], dict[str, Any]] = lambda state: {}
    # 逾時時通知確認者的內容（None 代表不通知）
    timeout_notice: Callable[[State], str] | None = None
    # 確認頁要填的欄位（例如退款金額），回傳 list of dict：
    #   {"name", "label", "type": "int", "applies_to": [選項 value…], "default", "min", "max"}
    fields: Callable[[State], list[dict[str, Any]]] | None = None
    # 驗證決定附帶的資料（POST decision 的 data），回傳要一起存進 state["decisions"][step] 的欄位；
    # 有問題丟 ValueError（API 回 422）。meta 提供 approver（確認者名稱）、decided_at（ISO）。
    accept_decision: Callable[[State, str, dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None
    # 時間軸的摘要（預設是選項名稱＋意見）
    decision_summary: Callable[[str, dict[str, Any]], str] | None = None
    kind: str = field(default="approval", init=False)


class StepStop(Exception):
    """ControllerStep 執行時遇到無法自行解決的情況：步驟停下、通知 admin，不要猜。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class ControllerStep:
    """controller 自己在同一個交易內完成的步驟（不派工）。

    run(runtime, state) 回傳寫進 state["steps"][name] 的 output；
    遇到找不到人等設定問題時丟 StepStop，步驟停下並通知 admin。
    runtime 提供 db、now、notify(hrm_employee_id 或 Identity, title, body)。
    """

    name: str
    title: str
    run: Callable[[Any, State], dict[str, Any]]
    summary: Callable[[dict[str, Any]], str] | None = None
    route: Callable[[State, dict[str, Any]], str] | None = None
    kind: str = field(default="controller", init=False)


StepDef = AgentStep | WaitStep | ApprovalStep | ControllerStep


@dataclass(frozen=True)
class FlowDefinition:
    type: str
    display_name: str
    steps: tuple[StepDef, ...]
    # 驗證並正規化初始資料；有問題丟 ValueError（API 回 422）
    validate_input: Callable[[dict[str, Any]], dict[str, Any]]
    make_title: Callable[[dict[str, Any]], str]
    # 可以用 agent token 啟動這種流程的 agent id；None 代表任何助理 agent（role=assistant）。
    # 依計畫「每隻 agent 的 token 只有它需要的 scope」，正式流程應明確列出。
    starters: tuple[str, ...] | None = None
    # 依初始資料決定去重鍵（例如 CRM 待辦跟進 id）；有設定時優先於呼叫端給的 dedupe_key
    dedupe_key: Callable[[dict[str, Any]], str] | None = None
    # 課堂示範模式（input 帶 demo=true，時限縮短）：可以用 agent token 啟動示範流程的 agent id。
    # 空的代表這種流程不支援示範模式；admin 從前端／API 啟動一律可以（只要流程支援）。
    demo_starters: tuple[str, ...] = ()
    # 示範模式的額外檢查（已驗證的 input, 現在時間）；不合理丟 ValueError（API 回 422 demo_input_rejected）。
    # 例如 visit 只接受「今天、幾小時內開始」的拜訪，避免 agent 被誘導把正式拜訪改成短時限。
    demo_check: Callable[[dict[str, Any], datetime], None] | None = None

    @property
    def supports_demo(self) -> bool:
        return bool(self.demo_starters)

    def step(self, name: str) -> StepDef:
        for step in self.steps:
            if step.name == name:
                return step
        raise KeyError(name)

    def next_after(self, name: str) -> str | None:
        names = [step.name for step in self.steps]
        index = names.index(name)
        return names[index + 1] if index + 1 < len(names) else None

    def __post_init__(self) -> None:
        names = [step.name for step in self.steps]
        if len(names) != len(set(names)):
            raise ValueError(f"{self.type}: 步驟名稱重複")
        valid = set(names) | {NEXT, END, FAIL}
        for step in self.steps:
            if isinstance(step, AgentStep) and isinstance(step.on_failure, str) \
                    and step.on_failure not in names:
                raise ValueError(f"{self.type}.{step.name}: on_failure 指向不存在的步驟")
            if isinstance(step, ApprovalStep):
                option_values = {opt.value for opt in step.options}
                if set(step.routes) != option_values:
                    raise ValueError(f"{self.type}.{step.name}: routes 必須涵蓋所有選項")
                for target in step.routes.values():
                    if target not in valid:
                        raise ValueError(f"{self.type}.{step.name}: 路由目標 {target} 不存在")
            if isinstance(step, WaitStep) and step.event_type and step.accept_event is None:
                raise ValueError(f"{self.type}.{step.name}: 有 event_type 就要有 accept_event")
