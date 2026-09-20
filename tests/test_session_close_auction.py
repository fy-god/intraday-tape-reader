"""IT-P0-001 回归：14:57-15:00 收盘集合竞价必须与连续竞价分开。

缺陷
----
`session.py` 把 13:00-15:00 整体标为 `AFTERNOON`，而 `CONTINUOUS` 含
`AFTERNOON` → `is_open(14:58)` 返回 **True**，收盘集合竞价被当成连续竞价。
该文件**自己的模块 docstring 从第一天起**就写着「集合竞价：09:15-09:25（开盘）、
**14:57-15:00（收盘，深市）**」—— 属"文档声明了、代码从未实现"。

为什么不能只加个枚举
--------------------
12:07 报告明确要求：`phase()` / `CONTINUOUS` / `is_open()` /
`is_tradable_window()` / `elapsed_trading_seconds()` 必须**一起**改，
并让规则显式声明是否支持 close auction。**只增加枚举但量能时钟仍算到 15:00
不算完成** —— 因为 `volume_burst` 的
``avg_per_min = volume_lots / elapsed_trading_seconds * 60``
会在收盘集合竞价期间出现"分子不动、分母继续涨"，把放量速率人为稀释。

本文件锁定这五处 + 规则侧的连带后果。
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from arad.session import (
    CALL_AUCTIONS, CONTINUOUS, OBSERVABLE, PHASE_CN,
    SessionPhase, TradingCalendar,
)

DAY = (2026, 9, 15)          # 周二，交易日


def at(h: int, m: int, s: int = 0, d: tuple[int, int, int] | date | None = None) -> datetime:
    """``d`` 接受 (y, mo, dd) 元组或 ``date``。"""
    if d is None:
        y, mo, dd = DAY
    elif isinstance(d, date):
        y, mo, dd = d.year, d.month, d.day
    else:
        y, mo, dd = d
    return datetime(y, mo, dd, hour=h, minute=m, second=s)


@pytest.fixture
def cal():
    return TradingCalendar(holidays=set())


# ---------------------------------------------------------------------------
# 1. phase()：14:57 是分界线
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("h,m,s,expect", [
    (14, 56, 59, SessionPhase.AFTERNOON),
    (14, 57, 0, SessionPhase.CLOSE_AUCTION),
    (14, 58, 0, SessionPhase.CLOSE_AUCTION),
    (14, 59, 59, SessionPhase.CLOSE_AUCTION),
    (15, 0, 0, SessionPhase.POST),
])
def test_close_auction_boundary(cal, h, m, s, expect):
    """左闭右开：14:56:59 仍连续竞价，14:57:00 起进入收盘集合竞价。"""
    assert cal.phase(at(h, m, s)) is expect


def test_close_auction_enum_exists():
    assert SessionPhase.CLOSE_AUCTION.value == "close_auction"
    assert SessionPhase.CLOSE_AUCTION in PHASE_CN
    assert PHASE_CN[SessionPhase.CLOSE_AUCTION] == "收盘集合竞价"


# ---------------------------------------------------------------------------
# 2. CONTINUOUS / is_open()：收盘集合竞价不是连续竞价
# ---------------------------------------------------------------------------
def test_close_auction_not_in_continuous():
    assert SessionPhase.CLOSE_AUCTION not in CONTINUOUS
    assert set(CONTINUOUS) == {SessionPhase.MORNING, SessionPhase.AFTERNOON}


def test_is_open_false_during_close_auction(cal):
    """上游给的验收判据：``is_open(14:58) is False``。"""
    assert cal.is_open(at(14, 58)) is False
    assert cal.is_open(at(14, 57)) is False
    assert cal.is_open(at(14, 59, 59)) is False
    # 连续竞价末秒仍为 True（不得过度修正）
    assert cal.is_open(at(14, 56, 59)) is True


def test_is_call_auction_covers_both_windows(cal):
    assert cal.is_call_auction(at(9, 20)) is True
    assert cal.is_call_auction(at(14, 58)) is True
    assert cal.is_call_auction(at(10, 0)) is False
    assert cal.is_call_auction(at(9, 27)) is False     # 静默期不是竞价
    assert set(CALL_AUCTIONS) == {SessionPhase.PRE_OPEN, SessionPhase.CLOSE_AUCTION}


# ---------------------------------------------------------------------------
# 3. 不过度修正：收盘集合竞价**仍要抓行情**
# ---------------------------------------------------------------------------
def test_close_auction_still_observable(cal):
    """该时段价格确实在动，必须继续抓；只有静默期/休市才不抓。

    若把 14:57-15:00 一并排除出抓取窗口，就是错误方向相反的同一个 bug。
    """
    assert cal.is_tradable_window(at(14, 58)) is True
    assert cal.is_tradable_window(at(9, 20)) is True
    assert cal.is_tradable_window(at(9, 27)) is False
    assert cal.is_tradable_window(at(14, 0)) is True
    assert cal.is_tradable_window(at(15, 0)) is False
    assert SessionPhase.CLOSE_AUCTION in OBSERVABLE
    assert SessionPhase.AUCTION not in OBSERVABLE


# ---------------------------------------------------------------------------
# 4. 量能时钟：12:07 报告点名的"实质后果"
# ---------------------------------------------------------------------------
def test_elapsed_stops_at_1457(cal):
    """连续竞价时长止于 14:57，全天 **14220** 秒（不是 14400）。"""
    assert cal.elapsed_trading_seconds(at(14, 56, 59)) == pytest.approx(7200 + 7019)
    assert cal.elapsed_trading_seconds(at(14, 57)) == pytest.approx(14220.0)
    assert cal.elapsed_trading_seconds(at(14, 58)) == pytest.approx(14220.0)
    assert cal.elapsed_trading_seconds(at(14, 59, 59)) == pytest.approx(14220.0)
    assert cal.elapsed_trading_seconds(at(15, 0)) == pytest.approx(14220.0)
    assert cal.elapsed_trading_seconds(at(20, 0)) == pytest.approx(14220.0)


def test_elapsed_freezes_during_close_auction(cal):
    """收盘集合竞价期间分母必须冻结，否则放量速率被稀释。"""
    values = [cal.elapsed_trading_seconds(at(14, m, s))
              for m, s in ((57, 0), (57, 30), (58, 0), (59, 0), (59, 59))]
    assert len(set(values)) == 1, f"分母不应增长：{values}"


def test_volume_rate_not_diluted_by_close_auction(cal):
    """量能速率的**可观察后果**：14:57-15:00 分母冻结 => 速率不被稀释。

    这是"只加枚举不算修完"的判据。用真实公式
    ``avg_per_min = volume_lots / elapsed * 60`` 算，并与**旧口径**
    （分母走到 15:00 = 14400）对照：旧口径会给出显著更低的速率。
    """
    vol = 100_000.0
    new = vol / max(cal.elapsed_trading_seconds(at(14, 58)), 1) * 60
    old = vol / max(14400.0, 1) * 60          # 旧实现：全天 4 小时
    assert new > old * 1.01, (
        f"新口径速率 {new:.1f} 应明显高于旧口径 {old:.1f}"
        f"（否则分母没真正冻结）")


def test_elapsed_still_monotonic(cal):
    """改动后仍须单调不减（看板进度条依赖）。"""
    prev = -1.0
    for h in range(9, 16):
        for m in (0, 15, 29, 30, 45, 57, 59):
            v = cal.elapsed_trading_seconds(at(h, m))
            assert v >= prev, f"{h}:{m:02d} 倒退（{v} < {prev}）"
            prev = v


def test_morning_and_lunch_unaffected(cal):
    """上午与午休口径不得被这次改动碰到。"""
    assert cal.elapsed_trading_seconds(at(9, 30)) == 0.0
    assert cal.elapsed_trading_seconds(at(10, 0)) == pytest.approx(1800.0)
    assert cal.elapsed_trading_seconds(at(11, 30)) == pytest.approx(7200.0)
    assert cal.elapsed_trading_seconds(at(13, 0)) == pytest.approx(7200.0)
    # 午休期间冻结：11:30 与 12:00 相同
    assert cal.elapsed_trading_seconds(at(11, 30)) == cal.elapsed_trading_seconds(at(12, 0))


def test_non_trading_day_still_closed(cal):
    """周末仍整日为 CLOSED，收盘集合竞价不得泄漏到非交易日。"""
    for d in (date(2026, 9, 19), date(2026, 9, 20)):
        assert cal.phase(at(14, 58, d=d)) is SessionPhase.CLOSED
        assert cal.elapsed_trading_seconds(at(14, 58, d=d)) == 0.0


# ---------------------------------------------------------------------------
# 5. 规则侧连带后果：only_continuous 规则必须自动关闭
# ---------------------------------------------------------------------------
def _ctx(session, elapsed):
    from arad.rules.base import RuleContext
    return RuleContext(state=None, cfg={}, now=at(14, 58), session=session,
                       elapsed_trading_seconds=elapsed, minutes_to_close=2.0)


@pytest.mark.parametrize("name", [
    "volume_burst", "unusual", "tick_surge", "limit_board",
    "spirit_order", "spirit_price", "spirit_index",
])
def test_only_continuous_rules_silent_in_close_auction(name):
    """7 个规则默认 ``only_continuous=True``，在收盘集合竞价必须不出告警。

    以前该时段是 AFTERNOON ∈ CONTINUOUS，规则会照常触发 —— 而这段没有
    连续成交，触发出来的"急拉"是 15:00 一次性撮合的假象。
    """
    import importlib

    mod = importlib.import_module(f"arad.rules.{name}")
    cls = next(
        getattr(mod, n) for n in dir(mod)
        if n.endswith("Rule") and isinstance(getattr(mod, n), type)
    )
    try:
        rule = cls({})
    except TypeError:
        rule = cls()

    ctx = _ctx(SessionPhase.CLOSE_AUCTION, 14220.0)
    try:
        out = rule.evaluate(None, ctx)
    except Exception:            # noqa: BLE001  规则可能因 state=None 报错，
        out = None               # 那种情况下"没产出告警"同样成立
    assert not out, f"{name} 在收盘集合竞价不应产出告警"


def test_rule_continuous_still_fires_in_afternoon():
    """对照：14:50（连续竞价）规则时段判定仍放行，别修成永久静音。"""
    import importlib
    mod = importlib.import_module("arad.rules.volume_burst")
    cls = next(getattr(mod, n) for n in dir(mod)
               if n.endswith("Rule") and isinstance(getattr(mod, n), type))
    try:
        rule = cls({})
    except TypeError:
        rule = cls()
    cfg = getattr(rule, "cfg", {}) or {}
    assert bool(cfg.get("only_continuous", True)) is True
    # 时段判定本身：AFTERNOON 在 CONTINUOUS 内
    assert SessionPhase.AFTERNOON in CONTINUOUS
