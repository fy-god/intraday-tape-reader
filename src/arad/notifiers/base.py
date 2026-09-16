"""通知器协议。"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Alert

__all__ = ["Notifier"]


@runtime_checkable
class Notifier(Protocol):
    name: str

    def send(self, alert: Alert) -> bool:
        """推送单条告警。返回是否成功；**绝不抛异常**。"""
        ...

    def send_digest(self, alerts: list[Alert]) -> bool:
        """批量/摘要推送。返回是否成功；**绝不抛异常**。"""
        ...
