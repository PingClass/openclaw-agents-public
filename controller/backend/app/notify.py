"""通知管道（計畫 0.5、3.5）。

先實作 InboxNotifier（寫入前端的通知收件匣）。LINE 接上後新增 LineNotifier，
在 make_notifier 加進管道即可，不改流程。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import Identity, Notification

KIND_APPROVAL_REQUEST = "approval_request"
KIND_APPROVAL_EXPIRED = "approval_expired"
KIND_INFO = "info"
# 需要 admin 處理（例如找不到確認對象，流程停下）
KIND_ALERT = "alert"


@dataclass(frozen=True)
class Message:
    recipient: Identity
    kind: str
    title: str
    body: str = ""
    link: str | None = None
    flow_id: str | None = None
    approval_id: str | None = None


class Notifier(ABC):
    """在同一個 DB 交易內呼叫。需要對外送出（例如 LINE push）的實作，
    應透過 ctx.after_commit 在交易提交後才送。"""

    @abstractmethod
    def send(self, ctx, message: Message) -> None: ...  # noqa: ANN001


class InboxNotifier(Notifier):
    def send(self, ctx, message: Message) -> None:  # noqa: ANN001
        db: Session = ctx.db
        notification = Notification(
            recipient_id=message.recipient.id,
            kind=message.kind,
            title=message.title,
            body=message.body,
            link=message.link,
            flow_id=message.flow_id,
            approval_id=message.approval_id,
            created_at=ctx.now,
        )
        db.add(notification)
        db.flush()
        ctx.emit(
            "notification.created",
            {
                "notification_id": notification.id,
                "recipient_id": message.recipient.id,
                "kind": message.kind,
                "flow_id": message.flow_id,
            },
        )


class MultiNotifier(Notifier):
    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = notifiers

    def send(self, ctx, message: Message) -> None:  # noqa: ANN001
        for notifier in self.notifiers:
            notifier.send(ctx, message)


def make_notifier(settings) -> Notifier:  # noqa: ANN001, ARG001
    return InboxNotifier()
