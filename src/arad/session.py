"""交易时段判定：SessionPhase / TradingCalendar。

A股连续竞价：09:30-11:30、13:00-14:57
集合竞价：09:15-09:25（开盘）、14:57-15:00（收盘）
静默期：09:25-09:30

⚠ 修正（IT-P0-001）：旧注释把 13:00-15:00 整体当连续竞价、并把收盘集合竞价
标成"深市"专属。实际**上交所与深交所股票一致**：下午连续竞价到 14:57，
14:57-15:00 为收盘集合竞价。深圳 14:57-15:00 不接受撤单，上海同样按收盘
集合竞价撮合。旧注释与旧代码在这里是同一个错误的两面。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum

from .config import load_holidays

__all__ = ["SessionPhase", "TradingCalendar", "PHASE_CN",
           "CONTINUOUS", "CALL_AUCTIONS", "OBSERVABLE"]


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
    AFTERNOON = "afternoon"    # 13:00-14:57（**连续竞价**，不含收盘集合竞价）
    CLOSE_AUCTION = "close_auction"   # 14:57-15:00 收盘集合竞价（深沪两市）
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
    SessionPhase.CLOSE_AUCTION: "收盘集合竞价",
    SessionPhase.POST: "已收盘",
}

# 连续竞价时段（规则默认只在此时段告警）。
#
# ⚠ 这里**故意不含** CLOSE_AUCTION：14:57-15:00 是收盘集合竞价，
# 期间没有连续成交，价格由 15:00 一次性撮合决定。历史实现把它并进
# AFTERNOON，于是 ``is_open(14:58)`` 返回 True、告警按连续竞价语义触发
# （IT-P0-001）。本模块自己的 docstring 从第一天起就写着这个时段，
# 但代码从未实现 —— 属于「文档声明了、代码没做」。
CONTINUOUS = (SessionPhase.MORNING, SessionPhase.AFTERNOON)

#: 集合竞价时段（开盘 + 收盘）。价格在这些窗口里**会动**，与静默期不同。
CALL_AUCTIONS = (SessionPhase.PRE_OPEN, SessionPhase.CLOSE_AUCTION)

#: 可观测/可抓取窗口：连续竞价 + 两个集合竞价。
#: 静默期（AUCTION）不在其中 —— 它可撤单不可成交，价格冻结。
OBSERVABLE = (SessionPhase.MORNING, SessionPhase.AFTERNOON,
              SessionPhase.PRE_OPEN, SessionPhase.CLOSE_AUCTION)

_T_AUCTION_START = time(9, 15)
_T_SILENCE_START = time(9, 25)
_T_OPEN = time(9, 30)
_T_MORNING_END = time(11, 30)
_T_AFTERNOON_START = time(13, 0)
_T_CLOSE_AUCTION_START = time(14, 57)
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
            13:00:00-14:56:59  午盘     AFTERNOON（连续竞价）
            14:57:00-14:59:59  收盘集合竞价 CLOSE_AUCTION
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
        if t < _T_CLOSE_AUCTION_START:   # 13:00-14:57 午盘（连续竞价）
            return SessionPhase.AFTERNOON
        if t < _T_CLOSE:              # 14:57-15:00 收盘集合竞价（IT-P0-001）
            return SessionPhase.CLOSE_AUCTION
        return SessionPhase.POST

    def is_open(self, now: datetime | None = None) -> bool:
        """是否处于**连续竞价**（可连续成交）时段。

        14:57-15:00 的收盘集合竞价**不算**连续竞价（IT-P0-001）。
        """
        return self.phase(now) in CONTINUOUS

    def is_call_auction(self, now: datetime | None = None) -> bool:
        """是否处于集合竞价（开盘 09:15-09:25 或收盘 14:57-15:00）。"""
        return self.phase(now) in CALL_AUCTIONS

    def is_tradable_window(self, now: datetime | None = None) -> bool:
        """含集合竞价的可观测窗口。

        ⚠ 更正（IT-P1-INFO-001）：此前本 docstring 写"这个方法的两个历史调用点
        都把它当'要不要抓行情'用" —— **该陈述不实**，已由独立审计查证：
        ``git grep is_tradable_window`` 在 `src/`、`tools/` 中除本定义行外
        **零命中**。它是**纯测试 API**，从来没有生产调用点；引擎的抓取门控
        一直直接比较时段集合，从未调用本方法。

        保留它的理由是语义完备（"含集合竞价的窗口"是有用的判定），但不要
        把它当成生产链路的一环。收盘集合竞价属于该窗口 —— 价格确实在动。
        """
        return self.phase(now) in OBSERVABLE

    # ------------------------------------------------------------------
    def elapsed_trading_seconds(self, now: datetime | None = None) -> float:
        """当日已交易的**连续竞价**秒数（用于量能节奏对比）。

        严格按左闭右开，且**止于 14:57**（IT-P0-001）::

            09:30-11:30  7200 秒
            13:00-14:57  7020 秒
            全天         14220 秒

        14:57-15:00 是收盘集合竞价：期间**没有连续成交**，累计成交量要到
        15:00 一次性撮合才落地，因此它不属于"连续竞价时长"。

        ⚠ 因果更正（审计 13:39 指出，本机复核成立）：我此前在这里写"若把这段
        计入分母，``volume_burst`` 的 ``avg_per_min = volume_lots / elapsed * 60``
        会出现'分子不动、分母继续涨'，把放量速率稀释" —— **该因果链不成立**。
        ``volume_burst.py:119`` 的 ``only_continuous`` 门控在 **:123 读取
        ``elapsed`` 之前**就已 ``return []``；时段不是 CONTINUOUS 时，含
        ``avg_per_min`` 的整段根本不会执行。实测四种组合：在 CLOSE_AUCTION 下
        elapsed 取 14220 与 14400 结果**完全相同**（都是 0 条告警）。

        真实影响是**该时段 7 个 ``only_continuous`` 规则由"被评估"变为"被静音"**，
        而不是"速率被稀释"。这个时钟修正仍然正确且必要（时长口径本身不该含
        集合竞价），但不得再用"稀释"作为因果解释。

        注意：15:00 之后返回的是**全天连续竞价**时长（14220），不是 4 小时整。
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
            # 收口在 14:57：收盘集合竞价不计入连续竞价时长
            after_end = min(t, _T_CLOSE_AUCTION_START)
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
        if ph in CALL_AUCTIONS:
            # 集合竞价有撮合、价格会动，但无连续成交 —— 说清楚，别让用户
            # 以为"没在交易"或"在连续交易"（IT-P0-001）。
            return f"{label}（无连续成交）"
        if ph == SessionPhase.CLOSED:
            nxt = self.next_open(now)
            return f"{label} · 下次开盘 {nxt.strftime('%m-%d %H:%M')}"
        if ph == SessionPhase.POST:
            nxt = self.next_open(now)
            return f"{label} · 下次开盘 {nxt.strftime('%m-%d %H:%M')}"
        return label
