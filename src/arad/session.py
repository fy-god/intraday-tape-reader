"""交易时段判定：SessionPhase / TradingCalendar。

A股连续竞价：09:30-11:30、13:00-15:00
集合竞价：09:15-09:25（开盘）、14:57-15:00（收盘，深市）
静默期：09:25-09:30
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum

from .config import load_holidays

__all__ = ["SessionPhase", "TradingCalendar", "PHASE_CN"]


class SessionPhase(str, Enum):
    """时段枚举。

    ⚠ 历史命名陷阱：成员 **名** 与 **值** 都装反了——

    ==================  ==========  ==================================
    成员名（历史）        值          真实含义
    ==================  ==========  ==================================
    ``PRE_OPEN``        pre_open    09:15-09:25 **开盘集合竞价**
    ``AUCTION``         auction     09:25-09:30 **静默**（可撤单不可成交）
    ==================  ==========  ==================================

    值和名都是既有契约的一部分（会进 JSON、被规则引用），故**不改动**，
    但请一律使用下面语义正确的别名，避免"以为是竞价、实际拿到静默"。
    """

    CLOSED = "closed"
    PRE_OPEN = "pre_open"      # 09:15-09:25 开盘集合竞价（历史名）
    AUCTION = "auction"        # 09:25-09:30 静默（历史名）
    MORNING = "morning"        # 09:30-11:30
    LUNCH = "lunch"            # 11:30-13:00
    AFTERNOON = "afternoon"    # 13:00-15:00
    POST = "post"              # 15:00 之后

    # --- 语义正确的别名（推荐使用） -------------------------------------
    CALL_AUCTION = "pre_open"  # == PRE_OPEN，09:15-09:25 集合竞价
    SILENCE = "auction"        # == AUCTION，09:25-09:30 静默


PHASE_CN: dict[SessionPhase, str] = {
    SessionPhase.CLOSED: "休市",
    SessionPhase.PRE_OPEN: "集合竞价",
    SessionPhase.AUCTION: "开盘静默",
    SessionPhase.MORNING: "早盘",
    SessionPhase.LUNCH: "午间休市",
    SessionPhase.AFTERNOON: "午盘",
    SessionPhase.POST: "已收盘",
}

# 连续竞价时段（规则默认只在此时段告警）
CONTINUOUS = (SessionPhase.MORNING, SessionPhase.AFTERNOON)

_T_AUCTION_START = time(9, 15)
_T_SILENCE_START = time(9, 25)
_T_OPEN = time(9, 30)
_T_MORNING_END = time(11, 30)
_T_AFTERNOON_START = time(13, 0)
_T_CLOSE = time(15, 0)

# 兼容旧名
_T_PRE_OPEN = _T_AUCTION_START


@dataclass
class TradingCalendar:
    """交易日与时段判定。holidays 为 {'YYYY-MM-DD'} 字符串集合。"""

    holidays: set[str] = field(default_factory=set)
    # 允许注入 now（测试用）
    _now_fn: object = None

    @classmethod
    def load(cls, holidays_file: str | None = None, *,
             settings: "Settings | None" = None) -> "TradingCalendar":
        """载入交易日历。

        ``holidays_file`` 显式优先；未给时读 ``session.holidays_file`` 配置键
        （见 ``config.load_holidays`` 的说明：这个键早先是写了不生效的）。
        """
        if settings is None:
            # 延迟导入：session 是最底层模块之一，不想在导入期就依赖 config。
            try:
                from .config import load_settings
                settings = load_settings()
            except Exception:  # noqa: BLE001 - 拿不到配置也要能用默认表
                settings = None
        return cls(holidays=load_holidays(holidays_file, settings=settings))

    # ------------------------------------------------------------------
    def is_trading_day(self, d: date | datetime | None = None) -> bool:
        if d is None:
            d = datetime.now()
        if isinstance(d, datetime):
            d = d.date()
        if d.weekday() >= 5:          # 周六周日
            return False
        return d.isoformat() not in self.holidays

    def phase(self, now: datetime | None = None) -> SessionPhase:
        """返回当前所处时段。非交易日一律 CLOSED。

        边界口径（左闭右开，收盘/午休瞬间即视为不在竞价中）::

            09:15:00-09:24:59  集合竞价 PRE_OPEN
            09:25:00-09:29:59  静默     AUCTION
            09:30:00-11:29:59  早盘     MORNING
            11:30:00-12:59:59  午休     LUNCH
            13:00:00-14:59:59  午盘     AFTERNOON
            15:00:00 起         已收盘   POST
        """
        now = now or datetime.now()
        if not self.is_trading_day(now):
            return SessionPhase.CLOSED
        t = now.time()
        if t < _T_AUCTION_START:
            return SessionPhase.CLOSED
        if t < _T_SILENCE_START:      # 09:15-09:25 集合竞价
            return SessionPhase.PRE_OPEN
        if t < _T_OPEN:               # 09:25-09:30 静默
            return SessionPhase.AUCTION
        if t < _T_MORNING_END:        # 09:30-11:30 早盘（11:30 整已休市）
            return SessionPhase.MORNING
        if t < _T_AFTERNOON_START:    # 11:30-13:00 午休
            return SessionPhase.LUNCH
        if t < _T_CLOSE:              # 13:00-15:00 午盘（15:00 整已收盘）
            return SessionPhase.AFTERNOON
        return SessionPhase.POST

    def is_open(self, now: datetime | None = None) -> bool:
        """是否处于连续竞价（可交易）时段。"""
        return self.phase(now) in CONTINUOUS

    def is_tradable_window(self, now: datetime | None = None) -> bool:
        """含集合竞价的可观测窗口。"""
        return self.phase(now) in (
            SessionPhase.PRE_OPEN, SessionPhase.AUCTION,
            SessionPhase.MORNING, SessionPhase.AFTERNOON,
        )

    # ------------------------------------------------------------------
    def elapsed_trading_seconds(self, now: datetime | None = None) -> float:
        """当日已交易的连续竞价秒数（用于量能节奏对比）。

        严格按左闭右开：11:30 整不再计入上午，15:00 整当天计满 14400 秒（4 小时）。
        """
        now = now or datetime.now()
        if not self.is_trading_day(now):
            return 0.0
        t = now.time()
        total = 0.0
        if t > _T_OPEN:
            morning_end = min(t, _T_MORNING_END)
            total += max(0.0, (datetime.combine(now.date(), morning_end)
                               - datetime.combine(now.date(), _T_OPEN)).total_seconds())
        if t > _T_AFTERNOON_START:
            after_end = min(t, _T_CLOSE)
            total += max(0.0, (datetime.combine(now.date(), after_end)
                               - datetime.combine(now.date(), _T_AFTERNOON_START)).total_seconds())
        return total

    def minutes_to_close(self, now: datetime | None = None) -> float:
        """距离收盘还有多少分钟（已收盘返回 0）。"""
        now = now or datetime.now()
        if not self.is_trading_day(now):
            return 0.0
        close_dt = datetime.combine(now.date(), _T_CLOSE)
        if now >= close_dt:
            return 0.0
        return (close_dt - now).total_seconds() / 60.0

    def next_open(self, now: datetime | None = None) -> datetime:
        """下一个开盘时间（09:30），用于非交易时段提示。"""
        now = now or datetime.now()
        d = now.date()
        for _ in range(30):
            cand = datetime.combine(d, _T_OPEN)
            if self.is_trading_day(d) and cand > now:
                return cand
            d = d + timedelta(days=1)
        return datetime.combine(now.date() + timedelta(days=1), _T_OPEN)

    def describe(self, now: datetime | None = None) -> str:
        now = now or datetime.now()
        ph = self.phase(now)
        label = PHASE_CN.get(ph, ph.value)
        if ph in CONTINUOUS:
            return f"{label} 交易中"
        if ph == SessionPhase.CLOSED:
            nxt = self.next_open(now)
            return f"{label} · 下次开盘 {nxt.strftime('%m-%d %H:%M')}"
        if ph == SessionPhase.POST:
            nxt = self.next_open(now)
            return f"{label} · 下次开盘 {nxt.strftime('%m-%d %H:%M')}"
        return label
