"""规则协议与共享上下文。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from ..models import Alert, Snapshot
from ..session import SessionPhase

__all__ = ["Rule", "RuleContext", "clamp", "bucket_of", "fmt_pct"]


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def bucket_of(now_epoch: float, cooldown_seconds: float) -> int:
    """把时间按冷却粒度分桶，用于生成幂等 key。"""
    cd = max(float(cooldown_seconds), 1.0)
    return int(now_epoch // cd)


def fmt_pct(v: float) -> str:
    return f"{v:+.2f}%"


@dataclass
class RuleContext:
    """规则求值上下文。state 为 EngineState（见 arad/engine.py）。"""

    state: object                    # EngineState，避免循环 import
    cfg: dict = field(default_factory=dict)
    now: datetime | None = None
    session: SessionPhase = SessionPhase.CLOSED
    # 辅助：当日已交易秒数 / 距收盘分钟数，由引擎填好
    elapsed_trading_seconds: float = 0.0
    minutes_to_close: float = 0.0
    watchlist: tuple[str, ...] = ()
    focus: tuple[str, ...] = ()

    @property
    def now_epoch(self) -> float:
        return (self.now or datetime.now()).timestamp()

    def opt(self, key: str, default):
        """取配置项，缺省或 None 时用 default。"""
        v = self.cfg.get(key, default)
        return default if v is None else v


@runtime_checkable
class Rule(Protocol):
    name: str

    def evaluate(self, snap: Snapshot, ctx: RuleContext) -> list[Alert]:
        """返回本轮新产生的告警（去重由 AlertBus 负责）。"""
        ...
