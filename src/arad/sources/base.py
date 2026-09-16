"""数据源协议定义。"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Quote

__all__ = ["Source", "SourceError"]


class SourceError(RuntimeError):
    """数据源故障（网络/解析/限频）。引擎捕获后触发故障转移。"""


@runtime_checkable
class Source(Protocol):
    name: str

    def universe(self) -> list[Quote]:
        """全市场股票池快照。失败抛 SourceError。"""
        ...

    def snapshots(self, codes: list[str]) -> list[Quote]:
        """指定 6 位代码列表的最新快照。失败抛 SourceError。"""
        ...

    def health(self) -> dict:
        """{'name':str,'ok':bool,'latency_ms':int,'err':str}"""
        ...
