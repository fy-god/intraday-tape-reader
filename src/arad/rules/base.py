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
    # 本轮供数源的能力声明（IT-P1-CAPABILITY-001）。为 None 时表示"未知"，
    # 规则必须按"字段不提供"保守处理，绝不能把占位 0.0 当真实业务零。
    capabilities: object | None = None
    # 本轮观测账本（requested/returned/…），供规则写可观测性用。
    observation: object | None = None
    #: 本轮**真正准入**的指数行情 ``code -> Quote``。
    #:
    #: IT-P1-INDEX-CURRENT-001：``state.quotes`` 是**累计 latest 缓存**（用于
    #: 历史/回看），不是"本轮 current"。指数规则若直接遍历它，在指数路由某轮
    #: 整体失败时会拿着**上一轮**的陈旧指数继续产告警 —— 看起来一切正常，
    #: 实际数据已经断了。这里显式传本轮 current view，规则只认它。
    #:
    #: ``None`` 表示调用方没提供（老调用方/单测）—— 规则须回退到旧行为，
    #: 但不能假装"本轮有指数"。
    current_indices: object | None = None

    @property
    def now_epoch(self) -> float:
        return (self.now or datetime.now()).timestamp()

    def opt(self, key: str, default):
        """取配置项，缺省或 None 时用 default。"""
        v = self.cfg.get(key, default)
        return default if v is None else v

    def provides(self, key: str) -> bool:
        """本轮供数源是否**提供** ``key`` 这个字段。

        没挂 capability（老调用方/单测）时返回 True —— 即保持改动前的
        行为（把字段当真实值），避免影响不涉及多源切换的既有路径。
        """
        caps = self.capabilities
        if caps is None:
            return True
        fn = getattr(caps, "supports", None)
        if callable(fn):
            return bool(fn(key))
        return True


@runtime_checkable
class Rule(Protocol):
    name: str

    def evaluate(self, snap: Snapshot, ctx: RuleContext) -> list[Alert]:
        """返回本轮新产生的告警（去重由 AlertBus 负责）。"""
        ...
