"""``rules/volume_burst.py`` 的离线测试。

自带最小 FakeState（实现 window / price_change / volume_delta + history），
不依赖 arad.engine，全部离线可跑。
"""
from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timedelta

import pytest

from fakes import make_quote, make_snapshot

from arad.models import AlertKind
from arad.rules.base import RuleContext
from arad.rules.volume_burst import DEFAULTS, VolumeBurstRule, build, RULE
from arad.session import SessionPhase

NOW = datetime(2026, 9, 15, 10, 30, 0)
NOW_EPOCH = NOW.timestamp()
COOLDOWN = 600


class FakeState:
    """最小 EngineState 替身：只实现规则真正用到的那几个方法。"""

    def __init__(self) -> None:
        self.quotes: dict = {}
        self.history: dict[str, deque] = {}
        self.last_price: dict[str, float] = {}
        self.first_seen: dict = {}
        self.day_open: dict[str, float] = {}

    # --- 契约方法 -------------------------------------------------------
    def window(self, code: str, seconds: float, now_epoch: float):
        rows = self.history.get(code) or ()
        start = now_epoch - seconds
        return [r for r in rows if start <= r[0] <= now_epoch]

    def price_change(self, code: str, seconds: float, now_epoch: float):
        rows = self.window(code, seconds, now_epoch)
        if len(rows) < 2 or rows[0][1] <= 0:
            return None
        return (rows[-1][1] / rows[0][1] - 1.0) * 100.0

    def volume_delta(self, code: str, seconds: float, now_epoch: float) -> float:
        rows = self.window(code, seconds, now_epoch)
        if len(rows) < 2:
            return 0.0
        return max(0.0, rows[-1][2] - rows[0][2])

    # --- 构造辅助 -------------------------------------------------------
    def feed(self, code: str, points) -> None:
        """points: [(ts_epoch, price, cum_volume_lots), ...]，整体覆盖。"""
        dq = self.history.setdefault(code, deque(maxlen=4096))
        dq.clear()
        for p in points:
            dq.append(tuple(p))


def ctx_of(state: FakeState, *, elapsed: float = 1800.0, cfg: dict | None = None,
           session: SessionPhase = SessionPhase.MORNING, now: datetime = NOW,
           minutes_to_close: float = 120.0):
    return RuleContext(
        state=state,
        cfg=dict(DEFAULTS) if cfg is None else cfg,
        now=now,
        session=session,
        elapsed_trading_seconds=elapsed,
        minutes_to_close=minutes_to_close,
    )


# 一只「典型放量」的票：涨 3.0%，量比 5.2，换手 3%，成交额 ~1.02 亿，站上均价。
BURST = dict(price=10.30, prev_close=10.00, open=10.00, high=10.40, low=9.98,
             volume_lots=100_000.0, turnover=3.0, volume_ratio=5.2)
BURST_AMOUNT = 100_000.0 * 100.0 * ((10.40 + 9.98 + 10.30) / 3.0)   # => 均价 10.2267


def burst_quote(code: str = "600000", **kw):
    base = dict(BURST, amount=BURST_AMOUNT, name="测试股")
    base.update(kw)
    return make_quote(code=code, **base)


def feed_burst(st: FakeState, code: str = "600000", *, recent: float = 32_000.0,
               cum: float = 100_000.0, now_epoch: float = NOW_EPOCH) -> None:
    """elapsed=1800s、当日累计 cum 手 => 均速 cum/1800*60 手/分。

    recent 手 / 60s => 近一分钟 recent 手/分。
    cum=100000 时均速 3333.3 手/分：recent=32000 -> 速度 9.6 倍。
    """
    st.feed(code, [
        (now_epoch - 3600.0, 9.90, max(cum - recent - 5_000.0, 0.0)),
        (now_epoch - 60.0, 10.05, cum - recent),
        (now_epoch, 10.30, cum),
    ])


# ---------------------------------------------------------------------------
# 契约 / 工厂
# ---------------------------------------------------------------------------
def test_build_and_module_rule():
    assert RULE.name == "volume_burst"
    r = build({})
    assert isinstance(r, VolumeBurstRule) and r.name == "volume_burst"
    # 配置无 max_per_round 时默认 20
    assert r.cfg["max_per_round"] == 20
    assert DEFAULTS["max_per_round"] == 20
    assert build({"max_per_round": 5}).cfg["max_per_round"] == 5
    # 显式 None 不覆盖默认值
    assert build({"min_amount": None}).cfg["min_amount"] == DEFAULTS["min_amount"]


def test_hits_and_payload():
    st = FakeState()
    feed_burst(st)
    alerts = RULE.evaluate(make_snapshot([burst_quote()], ts=NOW), ctx_of(st))
    assert len(alerts) == 1
    a = alerts[0]
    assert a.kind is AlertKind.VOLUME_BURST
    assert a.code == "600000" and a.name == "测试股" and a.severity == 2
    # speed_ratio = (32000/60*60) / (100000/1800*60) = 32000/3333.33 = 9.6
    assert a.metrics["speed_ratio"] == pytest.approx(9.6, abs=0.05)
    assert a.metrics["volume_ratio"] == pytest.approx(5.2)
    assert a.metrics["turnover"] == pytest.approx(3.0)
    assert a.metrics["amount"] == pytest.approx(BURST_AMOUNT)
    assert a.metrics["pct"] == pytest.approx(3.0)
    assert a.metrics["price"] == pytest.approx(10.30)
    assert a.metrics["amplitude"] == pytest.approx(4.2)
    assert a.metrics["above_vwap"] == 1.0
    assert a.title == "放量异动 量比5.2 速度9.6倍"
    for token in ("现价", "涨跌幅", "量比", "速度", "换手", "亿", "振幅", "均价"):
        assert token in a.detail, token
    # key = f"{code}:{kind.value}:{bucket}"
    assert a.key == f"600000:{AlertKind.VOLUME_BURST.value}:{int(NOW_EPOCH // COOLDOWN)}"


def test_severity_rule():
    # 速度 9.6 >= 2*4.0 -> 升到 2
    st = FakeState()
    feed_burst(st, recent=32_000.0)
    assert RULE.evaluate(make_snapshot([burst_quote()], ts=NOW), ctx_of(st))[0].severity == 2

    # 速度 4.2、量比 3.1（都未到 2 倍门槛）-> 保持配置的 1
    st2 = FakeState()
    feed_burst(st2, recent=14_000.0)
    a2 = RULE.evaluate(make_snapshot([burst_quote(volume_ratio=3.1)], ts=NOW), ctx_of(st2))[0]
    assert a2.metrics["speed_ratio"] == pytest.approx(4.2, abs=0.05)
    assert a2.severity == 1

    # 量比达标 2 倍（6.5 >= 2*3.0）也升档，即使速度只有 4.2
    st3 = FakeState()
    feed_burst(st3, recent=14_000.0)
    a3 = RULE.evaluate(make_snapshot([burst_quote(volume_ratio=6.5)], ts=NOW), ctx_of(st3))[0]
    assert a3.severity == 2

    # 配置 severity=3 时不被降级
    st4 = FakeState()
    feed_burst(st4, recent=14_000.0)
    a4 = RULE.evaluate(
        make_snapshot([burst_quote(volume_ratio=3.1)], ts=NOW),
        ctx_of(st4, cfg=dict(DEFAULTS, severity=3)),
    )[0]
    assert a4.severity == 3


def test_missing_volume_ratio_does_not_kill():
    """数据源没给量比（0）时跳过该项，而不是判负。"""
    st = FakeState()
    feed_burst(st)
    a = RULE.evaluate(make_snapshot([burst_quote(volume_ratio=0.0)], ts=NOW), ctx_of(st))
    assert len(a) == 1
    assert a[0].metrics["volume_ratio"] == 0.0
    assert a[0].title == "放量异动 量比- 速度9.6倍"


def test_low_volume_ratio_is_rejected():
    st = FakeState()
    feed_burst(st)
    assert RULE.evaluate(make_snapshot([burst_quote(volume_ratio=1.2)], ts=NOW), ctx_of(st)) == []
    # 量比门槛配 0 = 关闭该项 -> 低量比也放行
    a = RULE.evaluate(
        make_snapshot([burst_quote(volume_ratio=1.2)], ts=NOW),
        ctx_of(st, cfg=dict(DEFAULTS, volume_ratio_threshold=0)),
    )
    assert len(a) == 1


def test_speed_ratio_gate():
    """均速 3333 手/分；近 60s 只成交 1000 手 => 0.3 倍 < 4.0 被拒。"""
    st = FakeState()
    feed_burst(st, recent=1_000.0)
    assert RULE.evaluate(make_snapshot([burst_quote()], ts=NOW), ctx_of(st)) == []
    # 门槛配 0 = 关闭该项 -> 放行
    a = RULE.evaluate(
        make_snapshot([burst_quote()], ts=NOW),
        ctx_of(st, cfg=dict(DEFAULTS, speed_multiple=0)),
    )
    assert len(a) == 1
    # 门槛为 4.0 时，速度 4.2 刚好越过
    st2 = FakeState()
    feed_burst(st2, recent=14_000.0)
    assert len(RULE.evaluate(make_snapshot([burst_quote()], ts=NOW), ctx_of(st2))) == 1


def test_turnover_and_amount_filters():
    st = FakeState()
    feed_burst(st)
    assert RULE.evaluate(make_snapshot([burst_quote(turnover=0.2)], ts=NOW), ctx_of(st)) == []
    assert RULE.evaluate(make_snapshot([burst_quote(amount=10_000_000.0)], ts=NOW), ctx_of(st)) == []
    # 门槛为 0 = 关闭
    a = RULE.evaluate(
        make_snapshot([burst_quote(turnover=0.2, amount=1.0)], ts=NOW),
        ctx_of(st, cfg=dict(DEFAULTS, min_turnover=0, min_amount=0)),
    )
    assert len(a) == 1


def test_min_abs_pct_filters_flat_wash_trading():
    """横盘对倒：量能脉冲很猛但价格几乎不动 -> 不报。"""
    st = FakeState()
    feed_burst(st)
    flat = burst_quote(price=10.00, high=10.02, low=9.99)   # pct == 0
    assert RULE.evaluate(make_snapshot([flat], ts=NOW), ctx_of(st)) == []
    # 关掉该门槛就报（证明确实是这条拦下的）
    a = RULE.evaluate(
        make_snapshot([flat], ts=NOW), ctx_of(st, cfg=dict(DEFAULTS, min_abs_pct=0))
    )
    assert len(a) == 1


def test_sorting_and_max_per_round():
    cfg = dict(DEFAULTS, max_per_round=3)
    st = FakeState()
    quotes = []
    # 5 只票，速度倍数递增：4.2 / 5.1 / 6.0 / 6.9 / 7.8
    for i in range(5):
        code = f"60000{i}"
        feed_burst(st, code, recent=14_000.0 + i * 3_000.0)
        quotes.append(burst_quote(code=code, name=f"票{i}"))
    snap = make_snapshot(quotes, ts=NOW)

    alerts = RULE.evaluate(snap, ctx_of(st, cfg=cfg))
    assert len(alerts) == 3
    ratios = [a.metrics["speed_ratio"] for a in alerts]
    assert ratios == sorted(ratios, reverse=True)
    assert [a.code for a in alerts] == ["600004", "600003", "600002"]
    # max_per_round=0 -> 不限量
    all_alerts = RULE.evaluate(snap, ctx_of(st, cfg=dict(DEFAULTS, max_per_round=0)))
    assert len(all_alerts) == 5
    assert [a.code for a in all_alerts] == ["600004", "600003", "600002", "600001", "600000"]


def test_cooldown_key_is_stable_within_bucket():
    cfg = dict(DEFAULTS, cooldown_seconds=COOLDOWN)
    q = burst_quote()
    st = FakeState()
    feed_burst(st)
    a1 = RULE.evaluate(make_snapshot([q], ts=NOW), ctx_of(st, cfg=cfg))[0]
    assert a1.key == f"600000:volume_burst:{int(NOW_EPOCH // COOLDOWN)}"

    # 同一 600s 桶内（+30s），行情同步推进 -> key 不变
    t2 = NOW + timedelta(seconds=30)
    st2 = FakeState()
    feed_burst(st2, recent=32_000.0, cum=103_000.0, now_epoch=t2.timestamp())
    a2 = RULE.evaluate(make_snapshot([q], ts=t2), ctx_of(st2, cfg=cfg, now=t2))[0]
    assert a2.key == a1.key

    # 跨桶（+11 分钟）-> key 变化
    t3 = NOW + timedelta(seconds=660)
    st3 = FakeState()
    feed_burst(st3, recent=32_000.0, cum=140_000.0, now_epoch=t3.timestamp())
    a3 = RULE.evaluate(make_snapshot([q], ts=t3), ctx_of(st3, cfg=cfg, now=t3))[0]
    assert a3.key != a1.key
    assert a3.key == f"600000:volume_burst:{int(t3.timestamp() // COOLDOWN)}"


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------
def test_edge_cases_do_not_alert():
    st = FakeState()
    feed_burst(st)
    # 停牌：volume_lots=0
    assert RULE.evaluate(
        make_snapshot([burst_quote(volume_lots=0.0, amount=0.0)], ts=NOW), ctx_of(st)) == []
    # prev_close <= 0
    assert RULE.evaluate(make_snapshot([burst_quote(prev_close=0.0)], ts=NOW), ctx_of(st)) == []
    # price <= 0
    assert RULE.evaluate(make_snapshot([burst_quote(price=0.0)], ts=NOW), ctx_of(st)) == []
    # elapsed <= 0（盘前 / 休市 / 非交易日）
    for bad in (0.0, -1.0):
        assert RULE.evaluate(
            make_snapshot([burst_quote()], ts=NOW), ctx_of(st, elapsed=bad)) == []
    # 空快照
    assert RULE.evaluate(make_snapshot([], ts=NOW), ctx_of(st)) == []
    # 本轮无 history -> volume_delta 为 0 -> 不达标
    assert RULE.evaluate(make_snapshot([burst_quote()], ts=NOW), ctx_of(FakeState())) == []


def test_only_continuous_and_enabled_switches():
    st = FakeState()
    feed_burst(st)
    q = burst_quote()
    for phase in (SessionPhase.CLOSED, SessionPhase.LUNCH, SessionPhase.POST):
        assert RULE.evaluate(make_snapshot([q], ts=NOW), ctx_of(st, session=phase)) == []
    for phase in (SessionPhase.MORNING, SessionPhase.AFTERNOON):
        assert len(RULE.evaluate(make_snapshot([q], ts=NOW), ctx_of(st, session=phase))) == 1
    # only_continuous=false -> 午休也报
    assert len(RULE.evaluate(
        make_snapshot([q], ts=NOW),
        ctx_of(st, session=SessionPhase.LUNCH, cfg=dict(DEFAULTS, only_continuous=False)),
    )) == 1
    # enabled=false -> 直接返回
    assert RULE.evaluate(
        make_snapshot([q], ts=NOW), ctx_of(st, cfg=dict(DEFAULTS, enabled=False))) == []
    # ctx.cfg 为空 dict 时回落到实例自身的 cfg
    ctx = RuleContext(state=st, cfg={}, now=NOW, session=SessionPhase.MORNING,
                      elapsed_trading_seconds=1800.0)
    assert len(RULE.evaluate(make_snapshot([q], ts=NOW), ctx)) == 1


def test_non_positive_volume_delta_is_ignored():
    """历史倒挂（volume_delta<0）不应产生负速度倍数。"""
    st = FakeState()
    st.feed("600000", [
        (NOW_EPOCH - 60.0, 10.05, 100_000.0),
        (NOW_EPOCH, 10.30, 90_000.0),
    ])
    assert RULE.evaluate(make_snapshot([burst_quote()], ts=NOW), ctx_of(st)) == []


def test_volume_delta_single_point_window():
    """窗口内只有 1 个点（刚开盘 / 行情稀疏）-> volume_delta 0 -> 不报。"""
    st = FakeState()
    st.feed("600000", [(NOW_EPOCH, 10.30, 100_000.0)])
    assert RULE.evaluate(make_snapshot([burst_quote()], ts=NOW), ctx_of(st)) == []


# ---------------------------------------------------------------------------
# 性能冒烟
# ---------------------------------------------------------------------------
def test_perf_1000_stocks_under_200ms():
    st = FakeState()
    quotes = []
    for i in range(1000):
        code = f"{600000 + i:06d}"
        feed_burst(st, code, recent=15_000.0)      # 速度 4.5 倍，全部命中
        quotes.append(burst_quote(code=code, name=f"票{i}"))
    snap = make_snapshot(quotes, ts=NOW)
    ctx = ctx_of(st)

    t0 = time.perf_counter()
    alerts = RULE.evaluate(snap, ctx)
    ms = (time.perf_counter() - t0) * 1000.0
    assert len(alerts) == 20           # 默认限量 20
    assert ms < 200.0, f"1000 只股票 evaluate 耗时 {ms:.1f}ms >= 200ms"


def test_perf_1000_stocks_no_history_under_200ms():
    """无历史（刚启动）时也要快。"""
    st = FakeState()
    quotes = [burst_quote(code=f"{600000 + i:06d}", name=f"票{i}") for i in range(1000)]
    snap = make_snapshot(quotes, ts=NOW)
    ctx = ctx_of(st)
    t0 = time.perf_counter()
    assert RULE.evaluate(snap, ctx) == []
    ms = (time.perf_counter() - t0) * 1000.0
    assert ms < 200.0, f"1000 只股票 evaluate 耗时 {ms:.1f}ms >= 200ms"
