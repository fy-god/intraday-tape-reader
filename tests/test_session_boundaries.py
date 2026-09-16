"""时段边界：开盘前后 / 午休 / 收盘那一刻，引擎该不该动。

为什么单独测这个：这些边界错了不会报错，只会表现为"开盘十分钟没有任何告警"
或"午休时突然刷一堆"。而且 A 股的时段比"上午+下午"复杂——09:15-09:25 是
集合竞价（价格在动，要抓），09:25-09:30 是**静默期**（可撤单不可成交，价格
不再变化，抓了是白抓），11:30 和 15:00 都是右开区间（整点即归入下一时段）。

判据用"行情源是否被调用"这个**外部可观测行为**，而不是内部标志位。
"""
from __future__ import annotations

import pytest

from arad.config import load_settings
from arad.engine import Engine
from arad.session import SessionPhase, TradingCalendar

DAY = 2026, 9, 17          # 周四，交易日（holidays 为空集，不查真实节假日表）

#: (时刻, 期望时段, 该时刻是否应当真的去抓行情)
CASES = [
    ("09:00:00", SessionPhase.CLOSED, False),
    ("09:14:59", SessionPhase.CLOSED, False),
    ("09:15:00", SessionPhase.PRE_OPEN, True),      # 集合竞价开始
    ("09:24:59", SessionPhase.PRE_OPEN, True),
    ("09:25:00", SessionPhase.AUCTION, False),      # 静默期开始
    ("09:29:59", SessionPhase.AUCTION, False),
    ("09:30:00", SessionPhase.MORNING, True),       # 连续竞价开始
    ("11:29:59", SessionPhase.MORNING, True),
    ("11:30:00", SessionPhase.LUNCH, False),        # 整点即午休
    ("12:59:59", SessionPhase.LUNCH, False),
    ("13:00:00", SessionPhase.AFTERNOON, True),
    ("14:59:59", SessionPhase.AFTERNOON, True),
    ("15:00:00", SessionPhase.POST, False),         # 整点即收盘
    ("20:00:00", SessionPhase.POST, False),         # 注意：已收盘 ≠ 休市(CLOSED)
]


class CountingSource:
    """记账用的假行情源（绝不联网）。"""

    name = "counting"

    def __init__(self):
        self.calls = 0

    def snapshots(self, codes):
        self.calls += 1
        return []                      # 空返回：本测试只关心"有没有去抓"

    def health(self):
        return {"name": self.name, "ok": True, "latency_ms": 0, "err": ""}


def _engine_at(hhmmss: str, src: CountingSource) -> Engine:
    h, m, s = (int(x) for x in hhmmss.split(":"))
    moment = __import__("datetime").datetime(*DAY, hour=h, minute=m, second=s)
    st = load_settings(use_cache=False)          # 不污染缓存的共享配置
    cal = TradingCalendar(holidays=set())        # 不查节假日表，避免年份相关
    eng = Engine(source=src, settings=st, rules=[], notifiers=[],
                 calendar=cal, now_fn=lambda t=moment: t)
    eng._codes = ["600000"]                      # 赋值即 pin，不会联网刷新股票池
    return eng


@pytest.mark.parametrize("hhmmss,want_phase,want_fetch", CASES)
def test_session_boundary(hhmmss, want_phase, want_fetch):
    src = CountingSource()
    eng = _engine_at(hhmmss, src)

    phase = eng.calendar.phase(eng.now())
    assert phase is want_phase, f"{hhmmss} 的时段应为 {want_phase}，实际 {phase}"

    eng.poll_once()                              # 不带 force：走真实时段判定
    assert (src.calls > 0) is want_fetch, (
        f"{hhmmss}（{phase.value}）"
        f"{'应当' if want_fetch else '不应当'}抓行情，实际调用 {src.calls} 次"
    )


def test_force_bypasses_closed_market():
    """``force=True`` 必须能穿透休市判定。

    这是回放/自检/盘中可用性探测的基础：它们常在收盘后运行，需要走完整数据
    链路。若 force 也失效，那些工具就会静默返回 0 条，看起来像"系统没坏"。
    """
    src = CountingSource()
    eng = _engine_at("20:00:00", src)
    assert eng.calendar.phase(eng.now()) is SessionPhase.POST

    eng.poll_once(force=True)
    assert src.calls > 0, "force=True 时休市也应抓取"


def test_auction_gap_does_not_fetch_but_pre_open_does():
    """09:25-09:30 静默期与 09:15-09:25 集合竞价必须区别对待。

    静默期可撤单不可成交，价格不再变化，抓取只会拿到重复数据并污染
    「急拉」的窗口计算（价格不动但时间在走 -> 速度被稀释）。
    """
    pre = CountingSource()
    _engine_at("09:20:00", pre).poll_once()
    assert pre.calls > 0, "集合竞价期间应当抓取"

    silent = CountingSource()
    _engine_at("09:27:00", silent).poll_once()
    assert silent.calls == 0, "静默期不应当抓取"


def test_minutes_to_close_and_elapsed_are_monotonic():
    """已交易分钟数随时间递增，距收盘递减——看板靠它们显示进度。"""
    cal = TradingCalendar(holidays=set())
    from datetime import datetime

    marks = ["09:30:00", "10:30:00", "11:30:00", "13:00:00", "14:00:00", "15:00:00"]
    elapsed, remain = [], []
    for t in marks:
        h, m, s = (int(x) for x in t.split(":"))
        moment = datetime(*DAY, hour=h, minute=m, second=s)
        elapsed.append(cal.elapsed_trading_seconds(moment))
        remain.append(cal.minutes_to_close(moment))

    assert elapsed == sorted(elapsed), f"已交易时长应单调不减：{elapsed}"
    assert remain == sorted(remain, reverse=True), f"距收盘应单调不增：{remain}"
    # 午休期间已交易时长不应继续增长（11:30 与 13:00 相同）
    assert elapsed[2] == elapsed[3], "午休不该计入交易时长"
