"""``rules/spirit_order.py`` 的离线测试。

自带最小 FakeState（实现 window / price_change / volume_delta + history），
不依赖 arad.engine，全部离线可跑（见 docs/DATA_CONTRACT.md 第 9 节）。

**贯穿全篇的两个前提**（模块的核心诚实点，务必反复验证）：

* 相邻两轮快照的增量只是"单笔成交"的**近似**，首轮无上轮 -> 不报；
* 间隔超过 ``max_gap_seconds`` / 成交量回退 -> 不报（宁可漏，不可猜）。

基准算术（``FLOAT_CAP=50`` 亿、``price=10`` 元 => 流通股数 5 亿股）::

    1 手 = 100 股
    50 万股  =  5,000 手   机构买单/卖单、机构吃货的绝对量门槛
    80 万股  =  8,000 手   有大买盘/大卖盘的绝对量门槛
    100 万元 @10 元 = 100,000 股 = 1,000 手
    流通盘 0.10% = 500,000 股 = 5,000 手
    流通盘 0.25% = 1,250,000 股 = 12,500 手
    流通盘 0.80% = 4,000,000 股 = 40,000 手

反例断言一律用 ``"xxx" not in patterns_of(...)`` 而不是 ``== set()``：
一只票同一轮可能合法地命中别的信号（例如 6,000 手主动买同时是
「大笔买入」和「机构吃货」），把断言写宽只会掩盖真正的回归。
"""
from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timedelta

import pytest

from fakes import make_quote, make_snapshot

from arad.engine import AlertBus, EngineState
from arad.models import AlertKind, Quote
from arad.rules.base import RuleContext, bucket_of
from arad.rules.spirit_order import (
    DEFAULTS,
    PATTERN_CN,
    PATTERNS,
    RULE,
    SpiritOrderRule,
    build,
)
from arad.session import SessionPhase

NOW = datetime(2026, 9, 15, 10, 30, 0)          # 2026-09-15 是周二，交易日
NOW_EPOCH = NOW.timestamp()
COOLDOWN = 300                                  # NOW_EPOCH // 300 恰好整除，桶边界干净

FLOAT_CAP = 50.0                                # 流通市值（亿）
FLOAT_SHARES = 500_000_000.0                    # => 流通股数 5 亿股 @ 10 元

#: 关掉"有大买盘/大卖盘"（大墙类），用来隔离"机构买单/卖单"。
#: 为什么需要：10 元股上 6,000 手的单档同时也满足大墙的绝对量门槛，
#: 不断开就会两条一起报，负例断言就不精确了。
WALL_OFF = dict(wall_shares=1e12, wall_float_pct=1e12)
#: 反过来：关掉"机构买单/卖单"，用来隔离大墙类信号。
ORDER_OFF = dict(institution_order_shares=1e12, institution_order_amount=1e12,
                 institution_order_float_pct=1e12)


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


def ctx_of(
    state: FakeState,
    *,
    cfg: dict | None = None,
    session: SessionPhase = SessionPhase.MORNING,
    now: datetime = NOW,
    elapsed: float = 1800.0,
    minutes_to_close: float = 120.0,
):
    return RuleContext(
        state=state,
        cfg=dict(DEFAULTS) if cfg is None else cfg,
        now=now,
        session=session,
        elapsed_trading_seconds=elapsed,
        minutes_to_close=minutes_to_close,
    )


def cfg_of(**kw) -> dict:
    """DEFAULTS + 覆盖项。

    测试里默认**显式打开** ``enabled``：模块 ``DEFAULTS`` 是 ``False``
    （依赖快照增量近似单笔，噪声大，上线前要先观察），但单测要验的是信号逻辑
    本身，所以这里统一打开，另用 ``test_defaults_are_disabled_by_default``
    单独钉住"出厂默认是关的"。
    """
    merged = dict(DEFAULTS)
    merged["enabled"] = True
    merged.update(kw)
    return merged


class Rule:
    """每个用例独立的规则实例（避免边沿/上轮缓存跨用例串味）。

    刻意**不用**模块级 ``RULE``：那是单例，状态会在用例之间泄漏。
    ``ev`` 默认让 ``ctx.now == 快照 ts``，保证 ``now_epoch`` 与 ``ts`` 一致 ——
    否则两轮快照的"间隔"永远是 0，``max_gap_seconds`` 根本测不出来。
    """

    def __init__(self, **cfg):
        self.cfg = cfg_of(**cfg)
        self.rule = build(self.cfg)

    def ev(self, quotes, *, state=None, ts: datetime | None = None,
           now: datetime | None = None, **ctx_kw):
        t = NOW if ts is None else ts
        items = quotes if isinstance(quotes, list) else [quotes]
        snap = make_snapshot(items, ts=t)
        return self.rule.evaluate(
            snap, ctx_of(state or FakeState(), cfg=self.cfg,
                         now=t if now is None else now, **ctx_kw)
        )

    def ev2(self, cur, prev, *, gap: float = 6.0, base_ts: datetime = NOW, **ctx_kw):
        """两轮快照：先喂上一轮 ``prev``，再喂本轮 ``cur``，间隔 ``gap`` 秒。"""
        self.ev(prev, ts=base_ts - timedelta(seconds=gap), **ctx_kw)
        return self.ev(cur, ts=base_ts, **ctx_kw)


def by_pattern(alerts) -> dict[str, object]:
    return {a.metrics["pattern"]: a for a in alerts}


def patterns_of(alerts) -> set[str]:
    return {a.metrics["pattern"] for a in alerts}


# ---------------------------------------------------------------------------
# 行情构造
# ---------------------------------------------------------------------------
def depth_quote(code: str = "600000", *, bids=(100.0,) * 5, asks=(100.0,) * 5,
                bid_px: float = 10.00, ask_px: float = 10.01, **kw) -> Quote:
    """默认带**完整五档**的行情；每档 100 手，远低于任何门槛（干净基线）。"""
    base = dict(
        name="测试股",
        price=10.00, prev_close=10.00, volume_lots=100_000.0, turnover=2.0,
        float_cap=FLOAT_CAP, outer_vol=50_000.0, inner_vol=50_000.0,
        bid_prices=tuple(bid_px - 0.01 * i for i in range(5)),
        bid_vols=tuple(bids),
        ask_prices=tuple(ask_px + 0.01 * i for i in range(5)),
        ask_vols=tuple(asks),
        bid1=bid_px, ask1=ask_px, bid_vol=bids[0], ask_vol=asks[0],
    )
    base.update(kw)
    base.setdefault("amount", base["volume_lots"] * 100.0 * base["price"])
    return make_quote(code=code, **base)


def nodepth_quote(code: str = "600000", **kw) -> Quote:
    """不带五档（降级模式）：只有买一/卖一量。"""
    return depth_quote(code, bid_prices=(), bid_vols=(), ask_prices=(), ask_vols=(), **kw)


def advance(prev: Quote, *, d_outer: float = 0.0, d_inner: float = 0.0, **kw) -> Quote:
    """在 ``prev`` 基础上推进一步快照：外/内盘各增 ``d_outer``/``d_inner`` 手。

    ``volume_lots`` 与 ``amount`` 同步增加（总成交量 = 内外盘之和，金额 = 量 × 现价），
    这正是真实行情的行为。不这样做就会出现"内外盘涨了但总成交量没动"的自相矛盾
    快照，规则会（正确地）拒绝判定 —— 那样测的就不是规则逻辑而是假数据了。

    五档可用 ``bids=(...)``/``asks=(...)`` 简写覆盖（内部映射到 bid_vols/ask_vols）。
    """
    d_lots = d_outer + d_inner
    px = float(kw.get("price", prev.price))
    base = dict(
        volume_lots=prev.volume_lots + d_lots,
        amount=prev.amount + d_lots * 100.0 * px,
        outer_vol=prev.outer_vol + d_outer,
        inner_vol=prev.inner_vol + d_inner,
    )
    if "bids" in kw:
        base["bid_vols"] = tuple(kw.pop("bids"))
    if "asks" in kw:
        base["ask_vols"] = tuple(kw.pop("asks"))
    base.update(kw)
    return prev.copy_with(**base)


def quiet(code: str = "600000", **kw) -> Quote:
    """平静基线：外盘=内盘=50,000 手，累计 100,000 手。"""
    return depth_quote(code, **kw)


def buy_prev() -> Quote:
    """主动买的上一轮基准。"""
    return quiet()


def buy_cur(lots: float = 6_000.0, **kw) -> Quote:
    """主动买 ``lots`` 手（默认 6,000 手 = 60 万股 = 0.12% 流通盘）。"""
    return advance(quiet(), d_outer=lots, **kw)


def sell_cur(lots: float = 6_000.0, **kw) -> Quote:
    return advance(quiet(), d_inner=lots, **kw)


# ===========================================================================
# 1. 契约 / 工厂
# ===========================================================================
def test_build_and_module_rule():
    assert RULE.name == "spirit_order"
    r = build({})
    assert isinstance(r, SpiritOrderRule) and r.name == "spirit_order"
    assert r.cfg["max_per_round"] == 20
    assert DEFAULTS["max_per_round"] == 20
    assert build({"max_per_round": 5}).cfg["max_per_round"] == 5
    # 显式 None 不覆盖默认值
    assert build({"wall_shares": None}).cfg["wall_shares"] == DEFAULTS["wall_shares"]


def test_defaults_are_disabled_by_default():
    """出厂默认**关闭**（与 engine.RULE_MODULES 里 spirit_* 的约定一致）。

    引擎对没有配置节、或配置节里没写 enabled 的规则会 ``setdefault(True)``，
    所以模块自己的 DEFAULTS 必须是那条最后防线。
    """
    assert DEFAULTS["enabled"] is False
    assert build({}).cfg["enabled"] is False
    q = depth_quote(bids=(6_000.0, 1, 1, 1, 1))
    # 用模块真实默认（不经过 cfg_of 的显式打开）跑一遍 -> 什么都不出
    default_cfg = dict(DEFAULTS)
    rule = build(default_cfg)
    assert rule.evaluate(make_snapshot([q], ts=NOW),
                         ctx_of(FakeState(), cfg=default_cfg)) == []


def test_defaults_match_official_thresholds():
    """DEFAULTS 必须与同花顺/大智慧短线精灵的官方口径一致。"""
    assert DEFAULTS["big_turnover_pct"] == 0.1                      # 大笔买入 > 0.1%
    assert DEFAULTS["institution_order_shares"] == 500_000.0        # 50 万股
    assert DEFAULTS["institution_order_amount"] == 1_000_000.0      # 100 万元
    assert DEFAULTS["institution_order_float_pct"] == 0.25          # 流通盘 0.25%
    assert DEFAULTS["institution_eat_shares"] == 500_000.0
    assert DEFAULTS["institution_eat_amount"] == 1_000_000.0
    assert DEFAULTS["institution_eat_float_pct"] == 0.1
    assert DEFAULTS["wall_shares"] == 800_000.0                     # 80 万股
    assert DEFAULTS["wall_float_pct"] == 0.8
    assert DEFAULTS["max_gap_seconds"] == 120.0
    assert DEFAULTS["only_continuous"] is True


def test_patterns_registry_complete():
    assert len(PATTERNS) == 8 and len(set(PATTERNS)) == 8
    for p in ("big_buy", "big_sell", "institution_buy", "institution_sell",
              "institution_eat", "institution_vomit", "big_bid_wall", "big_ask_wall"):
        assert p in PATTERNS
        assert p in PATTERN_CN


# ===========================================================================
# 2. 大笔买入 / 大笔卖出（事件型，比例阈值）
# ===========================================================================
def test_big_buy_triggers():
    """外盘增量 6,000 手 = 60 万股 = 5 亿流通盘的 0.12% >= 0.1% -> 报。"""
    r = Rule()
    a = by_pattern(r.ev2(buy_cur(), buy_prev()))["big_buy"]
    assert a.code == "600000" and a.severity == 2
    assert a.metrics["d_outer_lots"] == pytest.approx(6_000.0)
    assert a.metrics["float_pct"] == pytest.approx(0.12)
    assert "大笔买入" in a.title and "0.120%" in a.detail


def test_big_buy_exact_threshold_boundary():
    """恰好 0.1%（5,000 手 = 50 万股）：>= 口径应当报。"""
    r = Rule()
    a = by_pattern(r.ev2(buy_cur(5_000.0), buy_prev()))["big_buy"]
    assert a.metrics["float_pct"] == pytest.approx(0.1)


def test_big_buy_below_threshold_rejected():
    """4,000 手 = 0.08% < 0.1% -> 「大笔买入」不报（看着像大单，但不够阈值）。"""
    r = Rule()
    assert "big_buy" not in patterns_of(r.ev2(buy_cur(4_000.0), buy_prev()))


def test_big_buy_ratio_and_absolute_both_need_ratio():
    """「大笔买入」只认比例：同样 6,000 手，换个大流通盘就不够了。

    流通盘 500 亿 @ 10 元 = 50 亿股，0.1% = 500 万股 = 50,000 手 -> 6,000 手远不够。
    """
    r = Rule()
    prev = quiet(float_cap=500.0)
    cur = advance(prev, d_outer=6_000.0, float_cap=500.0)
    assert "big_buy" not in patterns_of(r.ev2(cur, prev))


def test_big_buy_needs_float_shares():
    """比例型信号在缺流通盘（float_shares<=0）时必须不报。

    "大笔买入"官方口径只有换手率一个条件、没有绝对量兜底项，
    所以没有流通盘就无法判定 —— 宁可漏报，不能拿绝对值硬套。
    """
    r = Rule()
    prev = quiet(float_cap=0.0)
    cur = advance(prev, d_outer=150_000.0, float_cap=0.0)
    assert cur.float_shares == 0.0
    assert "big_buy" not in patterns_of(r.ev2(cur, prev))


def test_big_sell_triggers():
    r = Rule()
    a = by_pattern(r.ev2(sell_cur(8_000.0), buy_prev()))["big_sell"]
    assert a.metrics["d_inner_lots"] == pytest.approx(8_000.0)
    assert a.metrics["float_pct"] == pytest.approx(0.16)
    assert "大笔卖出" in a.title


def test_big_sell_below_threshold_rejected():
    r = Rule()
    assert "big_sell" not in patterns_of(r.ev2(sell_cur(3_000.0), buy_prev()))


def test_big_buy_does_not_fire_on_inner_side():
    """内盘增量够大时只出「大笔卖出」，绝不能串成「大笔买入」。"""
    r = Rule()
    got = patterns_of(r.ev2(sell_cur(8_000.0), buy_prev()))
    assert "big_sell" in got and "big_buy" not in got


def test_big_sell_does_not_fire_on_outer_side():
    r = Rule()
    got = patterns_of(r.ev2(buy_cur(8_000.0), buy_prev()))
    assert "big_buy" in got and "big_sell" not in got


def test_big_buy_and_big_sell_both_on_two_way_trade():
    """买卖都很猛时两条都要报，互不吞并。"""
    r = Rule()
    cur = advance(quiet(), d_outer=6_000.0, d_inner=7_000.0)
    got = patterns_of(r.ev2(cur, quiet()))
    assert "big_buy" in got and "big_sell" in got


def test_big_buy_disabled_by_zero_threshold():
    """门槛配 0 = 关闭该项。"""
    r = Rule(big_turnover_pct=0.0)
    assert "big_buy" not in patterns_of(r.ev2(buy_cur(20_000.0), buy_prev()))


def test_big_buy_custom_threshold():
    r = Rule(big_turnover_pct=1.0)          # 1% = 5,000,000 股 = 50,000 手
    assert "big_buy" not in patterns_of(r.ev2(buy_cur(20_000.0), buy_prev()))
    assert "big_buy" in patterns_of(r.ev2(buy_cur(60_000.0), buy_prev()))


# ===========================================================================
# 3. 机构吃货 / 机构吐货（事件型，「或」阈值）
# ===========================================================================
def test_institution_eat_by_shares():
    """主动买入 6,000 手 = 60 万股 > 50 万股 -> 报。"""
    r = Rule()
    a = by_pattern(r.ev2(buy_cur(), buy_prev()))["institution_eat"]
    assert a.severity == 3
    assert a.metrics["hit_shares"] == 1.0
    assert a.metrics["buy_shares"] == pytest.approx(600_000.0)
    assert "机构吃货" in a.title


def test_institution_eat_by_amount():
    """20 元股：600 手 = 6 万股（< 50 万股），但 6 万股 × 20 元 = 120 万 > 100 万。"""
    r = Rule()
    prev = quiet(float_cap=0.0, price=20.0, prev_close=20.0, bid_px=20.0, ask_px=20.01)
    cur = advance(prev, d_outer=600.0, float_cap=0.0, price=20.0)
    a = by_pattern(r.ev2(cur, prev))["institution_eat"]
    assert a.metrics["hit_shares"] == 0.0
    assert a.metrics["hit_amount"] == 1.0
    assert a.metrics["buy_amount"] == pytest.approx(1_200_000.0)
    assert "100万元" in a.detail


def test_institution_eat_by_float_pct_alone():
    """把两个绝对量阈值调到不可能达到，只留比例项，证明比例项独立生效。"""
    r = Rule(institution_eat_shares=1e12, institution_eat_amount=1e12)
    a = by_pattern(r.ev2(buy_cur(), buy_prev()))["institution_eat"]
    assert a.metrics["hit_float_pct"] == 1.0
    assert a.metrics["hit_shares"] == 0.0 and a.metrics["hit_amount"] == 0.0


def test_institution_eat_below_all_thresholds_rejected():
    """10 手 = 1,000 股 / 1 万元 / 0.00002% —— 三个口径全不达标 -> 不报。"""
    r = Rule()
    assert "institution_eat" not in patterns_of(r.ev2(buy_cur(10.0), buy_prev()))


def test_institution_eat_amount_just_below_rejected():
    """990 手 = 9.9 万股（< 50 万股）且 99 万元（< 100 万元）-> 不报。

    注意 10 元股上"100 万元"≈ 1,000 手，比"50 万股"（5,000 手）**更早**触发 ——
    这条正是为了钉住"金额口径确实独立生效"。
    """
    r = Rule()
    prev = quiet(float_cap=0.0)
    cur = advance(prev, d_outer=990.0, float_cap=0.0)
    a_metrics_shares = 990.0 * 100.0
    assert a_metrics_shares < DEFAULTS["institution_eat_shares"]
    assert a_metrics_shares * 10.0 < DEFAULTS["institution_eat_amount"]
    assert "institution_eat" not in patterns_of(r.ev2(cur, prev))
    # 对照组：再多买 110 手（= 110 万元）就跨过金额门槛
    r2 = Rule()
    cur2 = advance(prev, d_outer=1_100.0, float_cap=0.0)
    assert "institution_eat" in patterns_of(r2.ev2(cur2, prev))


def test_institution_vomit_triggers():
    r = Rule()
    a = by_pattern(r.ev2(sell_cur(8_000.0), buy_prev()))["institution_vomit"]
    assert a.severity == 3
    assert a.metrics["sell_shares"] == pytest.approx(800_000.0)
    assert "机构吐货" in a.title


def test_institution_vomit_below_threshold_rejected():
    """990 手 = 9.9 万股 / 99 万元 -> 都不达标。"""
    r = Rule()
    prev = quiet(float_cap=0.0)
    cur = advance(prev, d_inner=990.0, float_cap=0.0)
    assert "institution_vomit" not in patterns_of(r.ev2(cur, prev))


def test_eat_and_vomit_are_direction_specific():
    """只看外盘 -> 只有吃货；只看内盘 -> 只有吐货。"""
    r = Rule()
    only_buy = patterns_of(r.ev2(advance(quiet(), d_outer=6_000.0), quiet()))
    assert "institution_eat" in only_buy and "institution_vomit" not in only_buy

    r2 = Rule()
    only_sell = patterns_of(r2.ev2(advance(quiet(), d_inner=6_000.0), quiet()))
    assert "institution_vomit" in only_sell and "institution_eat" not in only_sell


def test_eat_hits_listed_in_detail():
    """命中项必须写进 detail，便于排查误报。"""
    r = Rule()
    a = by_pattern(r.ev2(buy_cur(), buy_prev()))["institution_eat"]
    assert "50万股" in a.detail


def test_eat_amount_attribution_splits_by_outer_inner_ratio():
    """区间成交额按 内外盘比例 归因：买 6,000 手 / 卖 2,000 手 => 买占 75%。"""
    r = Rule()
    cur = advance(quiet(), d_outer=6_000.0, d_inner=2_000.0)
    got = by_pattern(r.ev2(cur, quiet()))
    d_amount = 8_000.0 * 100.0 * 10.0
    assert got["institution_eat"].metrics["buy_amount"] == pytest.approx(d_amount * 0.75)
    assert got["institution_vomit"].metrics["sell_amount"] == pytest.approx(d_amount * 0.25)


# ===========================================================================
# 4. 机构买单 / 机构卖单（状态型，单档「或」）
# ===========================================================================
def test_institution_buy_by_shares():
    r = Rule(**WALL_OFF)
    a = by_pattern(r.ev([depth_quote(bids=(6_000.0, 1, 1, 1, 1))]))["institution_buy"]
    assert a.severity == 2
    assert a.metrics["bid_lots"] == pytest.approx(6_000.0)
    assert a.metrics["bid_shares"] == pytest.approx(600_000.0)
    assert a.metrics["bid_amount"] == pytest.approx(6_000_000.0)
    assert "机构买单" in a.title


def test_institution_buy_single_tranche_not_total():
    """合计 12,000 手 = 120 万股（过大墙的 80 万股），但每档只有 2,400 手 = 24 万股。

    -> 「有大买盘」报、「机构买单」**不报**。这条是两者口径的关键区别：
    前者看**五档合计**，后者看**单档**。

    用 1 元股 + 无流通盘，保证单档 24 万元也不碰"100 万元"这条金额口径，
    从而真的只测"单档 vs 合计"。
    """
    kw = dict(price=1.0, prev_close=1.0, bid_px=1.0, ask_px=1.01, float_cap=0.0)
    r = Rule(**ORDER_OFF)
    got = patterns_of(r.ev([depth_quote(bids=(2_400.0,) * 5, **kw)]))
    assert "big_bid_wall" in got

    r2 = Rule(**WALL_OFF)
    assert "institution_buy" not in patterns_of(r2.ev([depth_quote(bids=(2_400.0,) * 5, **kw)]))


def test_institution_buy_total_does_not_count_as_single_tranche():
    """反向对照：单档过 50 万股但合计不过 80 万股 -> 「机构买单」报、「大买盘」不报。"""
    kw = dict(price=1.0, prev_close=1.0, bid_px=1.0, ask_px=1.01, float_cap=0.0)
    q = depth_quote(bids=(6_000.0, 0.0, 0.0, 0.0, 0.0), **kw)   # 合计仅 6,000 手
    r = Rule()
    got = patterns_of(r.ev([q]))
    assert "institution_buy" in got and "big_bid_wall" not in got


def test_institution_buy_by_amount():
    """20 元股：600 手 = 6 万股 < 50 万股，但 6 万股 × 20 元 = 120 万 > 100 万。"""
    r = Rule(**WALL_OFF)
    q = depth_quote(price=20.0, prev_close=20.0, bid_px=20.0, ask_px=20.01,
                    bids=(600.0, 10.0, 10.0, 10.0, 10.0), float_cap=0.0)
    a = by_pattern(r.ev([q]))["institution_buy"]
    assert a.metrics["bid_amount"] == pytest.approx(1_200_000.0)
    assert "100万元" in a.detail


def test_institution_buy_by_float_pct():
    """流通盘 20 亿 @ 10 元 = 2 亿股；0.25% = 50 万股 = 5,000 手。

    5,000 手恰好**不大于** 50 万股，只能靠比例项 -> 用 5,200 手（0.26%）。
    """
    r = Rule(**WALL_OFF, institution_order_shares=1e12, institution_order_amount=1e12)
    q = depth_quote(float_cap=20.0, bids=(5_200.0, 10.0, 10.0, 10.0, 10.0))
    a = by_pattern(r.ev([q]))["institution_buy"]
    assert "流通盘0.25%" in a.detail


def test_institution_buy_below_all_thresholds_rejected():
    """1 元股上的 4,000 手 = 40 万股 / 40 万元 / 0.08% -> 三个口径都不够。

    用 1 元股是为了让"金额"口径不提前兜住：10 元股上 4,000 手 = 400 万元，
    早就越过 100 万元了。
    """
    r = Rule(**WALL_OFF)
    q = depth_quote(bids=(4_000.0, 1, 1, 1, 1),
                    price=1.0, prev_close=1.0, bid_px=1.0, ask_px=1.01)
    assert "institution_buy" not in patterns_of(r.ev([q]))


def test_institution_buy_amount_threshold_alone_fires():
    """反向对照：同样 4,000 手，10 元股上金额 400 万元 > 100 万元 -> 反而报。

    证明"金额"确实是独立的「或」分支（1 元股上不报、10 元股上报）。
    """
    r = Rule(**WALL_OFF)
    q = depth_quote(bids=(4_000.0, 1, 1, 1, 1), float_cap=0.0)
    a = by_pattern(r.ev([q]))["institution_buy"]
    assert a.metrics["bid_amount"] == pytest.approx(4_000.0 * 100.0 * 10.0)
    assert "100万元" in a.detail


def test_institution_buy_does_not_fire_from_ask_side():
    r = Rule(**WALL_OFF)
    got = patterns_of(r.ev([depth_quote(asks=(6_000.0, 1, 1, 1, 1))]))
    assert "institution_sell" in got and "institution_buy" not in got


def test_institution_sell_by_shares():
    r = Rule(**WALL_OFF)
    a = by_pattern(r.ev([depth_quote(asks=(7_000.0, 1, 1, 1, 1))]))["institution_sell"]
    assert a.metrics["ask_shares"] == pytest.approx(700_000.0)
    assert "机构卖单" in a.title


def test_institution_sell_below_threshold_rejected():
    """1 元股上 3,000 手 = 30 万股 / 30 万元 / 0.06% -> 都不够。"""
    r = Rule(**WALL_OFF)
    q = depth_quote(asks=(3_000.0, 1, 1, 1, 1),
                    price=1.0, prev_close=1.0, bid_px=1.0, ask_px=1.01)
    assert "institution_sell" not in patterns_of(r.ev([q]))


def test_institution_buy_and_sell_can_coexist():
    r = Rule(**WALL_OFF)
    got = patterns_of(r.ev([
        depth_quote(bids=(6_000.0, 1, 1, 1, 1), asks=(8_000.0, 1, 1, 1, 1)),
    ]))
    assert "institution_buy" in got and "institution_sell" in got


def test_order_scan_picks_largest_hitting_tranche():
    """多档都达标时取量最大的那档作为文案代表。"""
    r = Rule(**WALL_OFF)
    q = depth_quote(bids=(6_000.0, 9_000.0, 1, 1, 1))
    a = by_pattern(r.ev([q]))["institution_buy"]
    assert a.metrics["bid_lots"] == pytest.approx(9_000.0)
    assert a.metrics["bid_price"] == pytest.approx(9.99)


def test_order_price_missing_falls_back_to_bid1():
    """只有量没有价时用买一价估算金额，不能崩也不能漏。"""
    r = Rule(**WALL_OFF)
    q = depth_quote(bid_prices=(), ask_prices=(), bids=(6_000.0, 1, 1, 1, 1), bid_px=10.0)
    a = by_pattern(r.ev([q]))["institution_buy"]
    assert a.metrics["bid_price"] == pytest.approx(10.0)
    assert a.metrics["bid_amount"] == pytest.approx(6_000_000.0)


def test_order_vols_shorter_than_five_levels_ok():
    """档位不足 5 档（数据源只给 3 档）时按实际档数遍历。"""
    r = Rule(**WALL_OFF)
    q = depth_quote(bids=(6_000.0, 10.0, 10.0), bid_prices=(10.0, 9.99, 9.98))
    a = by_pattern(r.ev([q]))["institution_buy"]
    assert a.metrics["bid_lots"] == pytest.approx(6_000.0)


def test_order_thresholds_can_be_disabled():
    r = Rule(institution_order_shares=0.0, institution_order_amount=0.0,
             institution_order_float_pct=0.0)
    got = patterns_of(r.ev([depth_quote(bids=(6_000.0, 1, 1, 1, 1))]))
    assert "institution_buy" not in got and "institution_sell" not in got


# ===========================================================================
# 5. 有大买盘 / 有大卖盘（状态型，五档合计）
# ===========================================================================
def test_big_bid_wall_by_shares():
    """每档 1,700 手 => 合计 8,500 手 = 85 万股 > 80 万股 -> 报。"""
    r = Rule(**ORDER_OFF)
    a = by_pattern(r.ev([depth_quote(bids=(1_700.0,) * 5)]))["big_bid_wall"]
    assert a.severity == 1
    assert a.metrics["bid_total_lots"] == pytest.approx(8_500.0)
    assert a.metrics["bid_shares"] == pytest.approx(850_000.0)
    assert "有大买盘" in a.title


def test_big_bid_wall_below_threshold_rejected():
    """每档 1,500 手 => 合计 7,500 手 = 75 万股 < 80 万股 -> 不报。"""
    r = Rule(**ORDER_OFF)
    assert "big_bid_wall" not in patterns_of(r.ev([depth_quote(bids=(1_500.0,) * 5)]))


def test_big_bid_wall_by_float_pct():
    """流通盘 5 亿 @ 10 元 = 0.5 亿股；0.8% = 40 万股 = 4,000 手。

    合计 4,500 手 = 45 万股 < 80 万股（绝对量不够），但占 0.9% > 0.8% -> 比例项命中。
    """
    r = Rule(**ORDER_OFF)
    q = depth_quote(float_cap=5.0, bids=(900.0,) * 5)
    a = by_pattern(r.ev([q]))["big_bid_wall"]
    assert a.metrics["bid_shares"] == pytest.approx(450_000.0)
    assert "流通盘0.80%" in a.detail


def test_big_ask_wall_by_shares():
    r = Rule(**ORDER_OFF)
    a = by_pattern(r.ev([depth_quote(asks=(2_000.0,) * 5)]))["big_ask_wall"]
    assert a.metrics["ask_shares"] == pytest.approx(1_000_000.0)
    assert "有大卖盘" in a.title


def test_big_ask_wall_below_threshold_rejected():
    r = Rule(**ORDER_OFF)
    assert "big_ask_wall" not in patterns_of(r.ev([depth_quote(asks=(1_500.0,) * 5)]))


def test_big_bid_wall_skipped_without_depth():
    """没有五档 -> 必须跳过，绝不能拿买一量冒充五档合计。

    买一量 20,000 手 = 200 万股，远超 80 万股；若错误地退回买一量就会误报。
    """
    r = Rule(**ORDER_OFF)
    q = nodepth_quote(bid_vol=20_000.0, ask_vol=20_000.0)
    assert q.has_depth is False
    assert q.bid_total_vol == 20_000.0        # 确认"退回买一量"的行为确实存在
    assert "big_bid_wall" not in patterns_of(r.ev([q]))


def test_big_ask_wall_skipped_without_depth():
    r = Rule(**ORDER_OFF)
    assert "big_ask_wall" not in patterns_of(
        r.ev([nodepth_quote(bid_vol=1.0, ask_vol=50_000.0)]))


def test_depth_requires_all_five_levels():
    """只有 4 档不算"有五档"。"""
    r = Rule(**ORDER_OFF)
    q = depth_quote(bids=(5_000.0,) * 4, bid_prices=(10.0, 9.99, 9.98, 9.97),
                    asks=(1.0,) * 4, ask_prices=(10.0, 10.01, 10.02, 10.03))
    assert q.has_depth is False
    got = patterns_of(r.ev([q]))
    assert "big_bid_wall" not in got and "big_ask_wall" not in got


def test_big_bid_wall_does_not_fire_on_ask_side():
    r = Rule(**ORDER_OFF)
    got = patterns_of(r.ev([depth_quote(bids=(100.0,) * 5, asks=(2_000.0,) * 5)]))
    assert "big_ask_wall" in got and "big_bid_wall" not in got


def test_walls_can_coexist():
    r = Rule(**ORDER_OFF)
    got = patterns_of(r.ev([depth_quote(bids=(2_000.0,) * 5, asks=(2_500.0,) * 5)]))
    assert "big_bid_wall" in got and "big_ask_wall" in got


def test_wall_zero_vols_do_not_alert():
    r = Rule(**ORDER_OFF)
    got = patterns_of(r.ev([depth_quote(bids=(0.0,) * 5, asks=(0.0,) * 5)]))
    assert "big_bid_wall" not in got and "big_ask_wall" not in got


def test_wall_thresholds_disableable():
    r = Rule(wall_shares=0.0, wall_float_pct=0.0, **ORDER_OFF)
    got = patterns_of(r.ev([depth_quote(bids=(1_700.0,) * 5)]))
    assert "big_bid_wall" not in got and "big_ask_wall" not in got


def test_wall_tolerates_dirty_depth_values():
    """某一档是 None / 非数字时不能抛 TypeError（会炸掉整轮所有告警）。"""
    r = Rule(**ORDER_OFF)
    q = depth_quote(bids=(None, "abc", 8_500.0, "", 1.0), bid_vol=None)
    a = by_pattern(r.ev([q]))["big_bid_wall"]
    assert a.metrics["bid_total_lots"] == pytest.approx(8_501.0)
    assert a.metrics["bid_vol"] == pytest.approx(0.0)     # None 被宽松转成 0


# ===========================================================================
# 6. float_shares <= 0：退化为纯绝对量阈值
# ===========================================================================
def test_no_float_cap_order_still_fires_on_absolute_shares():
    """自选股降级模式（无流通市值）下，机构买单必须仍能按 50 万股触发。"""
    r = Rule(**WALL_OFF)
    q = depth_quote(float_cap=0.0, bids=(6_000.0, 1, 1, 1, 1))
    assert q.float_shares == 0.0
    a = by_pattern(r.ev([q]))["institution_buy"]
    assert a.metrics["bid_shares"] == pytest.approx(600_000.0)


def test_no_float_cap_wall_still_fires_on_absolute_shares():
    r = Rule(**ORDER_OFF)
    q = depth_quote(float_cap=0.0, bids=(1_700.0,) * 5)
    a = by_pattern(r.ev([q]))["big_bid_wall"]
    assert a.metrics["bid_shares"] == pytest.approx(850_000.0)


def test_no_float_cap_eat_still_fires_on_absolute_shares():
    r = Rule()
    prev = quiet(float_cap=0.0)
    cur = advance(prev, d_outer=6_000.0, float_cap=0.0)
    a = by_pattern(r.ev2(cur, prev))["institution_eat"]
    assert a.metrics["hit_shares"] == 1.0
    assert a.metrics["hit_float_pct"] == 0.0


def test_no_float_cap_float_pct_branch_is_inert():
    """只剩比例项 + 无流通盘 -> 什么都不报（而不是崩 / 全报）。"""
    r = Rule(institution_order_shares=0.0, institution_order_amount=0.0,
             institution_order_float_pct=0.25, wall_shares=0.0, wall_float_pct=0.0)
    assert "institution_buy" not in patterns_of(
        r.ev([depth_quote(float_cap=0.0, bids=(99_999.0, 1, 1, 1, 1))]))


# ===========================================================================
# 7. 首轮不报 / 间隔过长 / 成交量回退 / 跨日重置
# ===========================================================================
def test_first_round_never_alerts_on_trades():
    """首轮没有上轮快照 -> 4 个成交类信号全部不报（绝不猜测）。"""
    r = Rule()
    big = depth_quote(outer_vol=500_000.0, inner_vol=500_000.0,
                      volume_lots=1_000_000.0, amount=1_000_000.0 * 100.0 * 10.0)
    got = patterns_of(r.ev([big]))
    for p in ("big_buy", "big_sell", "institution_eat", "institution_vomit"):
        assert p not in got
    assert r.rule._prev, "首轮必须写入缓存，供下一轮做差"


def test_first_round_still_alerts_on_order_signals():
    """盘口类只看当前快照，首轮就该报（与成交类形成对照）。"""
    r = Rule(**WALL_OFF)
    assert "institution_buy" in patterns_of(r.ev([depth_quote(bids=(6_000.0, 1, 1, 1, 1))]))


def test_second_round_alerts_after_first():
    r = Rule()
    assert "big_buy" in patterns_of(r.ev2(buy_cur(), buy_prev()))


def test_gap_too_long_is_skipped():
    """间隔 300s > max_gap_seconds(120) -> 跳过（断线重连场景）。

    增量本身很大（150,000 手），不检查间隔就会误报成"一笔大单"。
    """
    r = Rule()
    prev = quiet()
    cur = advance(prev, d_outer=150_000.0, d_inner=150_000.0)
    got = patterns_of(r.ev2(cur, prev, gap=300.0))
    for p in ("big_buy", "big_sell", "institution_eat", "institution_vomit"):
        assert p not in got


def test_lunch_break_gap_is_skipped():
    """跨午休：11:29:55 -> 13:00:05，gap=610s，整段成交量绝不能算成一笔。"""
    t_am = datetime(2026, 9, 15, 11, 29, 55)
    t_pm = datetime(2026, 9, 15, 13, 0, 5)
    assert (t_pm - t_am).total_seconds() > DEFAULTS["max_gap_seconds"]
    r = Rule()
    prev = quiet()
    r.ev(prev, ts=t_am, session=SessionPhase.MORNING)
    cur = advance(prev, d_outer=500_000.0, d_inner=500_000.0)
    alerts = r.ev(cur, ts=t_pm, session=SessionPhase.AFTERNOON)
    assert [a.metrics["pattern"] for a in alerts] == []


def test_gap_exactly_at_limit_still_alerts():
    """gap == max_gap_seconds 不跳过（只有**超过**才跳）。"""
    r = Rule()
    assert "big_buy" in patterns_of(r.ev2(buy_cur(), buy_prev(), gap=120.0))


def test_max_gap_configurable_to_disable():
    """``max_gap_seconds <= 0`` = 关闭间隔检查（此时需自行承担跨段误判风险）。

    注意两轮必须落在**同一自然日**：跨日会走 `_reset_day` 清缓存（那是另一条
    更优先的规则），那样测的就不是 max_gap 了。
    """
    r = Rule(max_gap_seconds=0.0)
    prev = quiet()
    r.ev(prev, ts=datetime(2026, 9, 15, 9, 30, 0))     # 同一天、1 小时前
    assert "big_buy" in patterns_of(r.ev(buy_cur(), ts=NOW))
    # 对照组：默认 120s 门槛下，同样这对快照必须被拦掉
    r2 = Rule()
    r2.ev(prev, ts=datetime(2026, 9, 15, 9, 30, 0))
    assert "big_buy" not in patterns_of(r2.ev(buy_cur(), ts=NOW))


def test_after_skipped_gap_cache_is_refreshed():
    """跳过的这一轮必须刷新缓存：下一轮的增量只算正常区间，不补报跳过期。"""
    r = Rule()
    prev = quiet()
    r.ev(prev, ts=NOW - timedelta(seconds=300))
    big = advance(prev, d_outer=150_000.0, d_inner=150_000.0)
    assert r.ev(big, ts=NOW) == []
    small = advance(big, d_outer=100.0)
    got = patterns_of(r.ev(small, ts=NOW + timedelta(seconds=6)))
    assert "big_buy" not in got and "institution_eat" not in got


def test_volume_rollback_outer_is_skipped():
    """外盘回退（数据源重置）-> 只更新缓存、不报。"""
    r = Rule()
    prev = depth_quote(outer_vol=90_000.0, inner_vol=90_000.0, volume_lots=200_000.0)
    cur = depth_quote(outer_vol=1_000.0, inner_vol=95_000.0, volume_lots=201_000.0)
    got = patterns_of(r.ev2(cur, prev))
    assert "big_buy" not in got and "institution_eat" not in got
    assert r.rule._prev["600000"][0] == 1_000.0        # 缓存已刷新


def test_volume_rollback_inner_is_skipped():
    r = Rule()
    prev = depth_quote(outer_vol=90_000.0, inner_vol=90_000.0, volume_lots=200_000.0)
    cur = depth_quote(outer_vol=95_000.0, inner_vol=1_000.0, volume_lots=201_000.0)
    got = patterns_of(r.ev2(cur, prev))
    assert "big_sell" not in got and "institution_vomit" not in got


def test_volume_rollback_total_is_skipped():
    r = Rule()
    prev = depth_quote(volume_lots=200_000.0, outer_vol=90_000.0, inner_vol=90_000.0)
    cur = depth_quote(volume_lots=2_000.0, outer_vol=95_000.0, inner_vol=95_000.0)
    assert patterns_of(r.ev2(cur, prev)) == set()


def test_rollback_then_recovery_alerts_again():
    """回退那一轮不报，恢复后按新的基准正常报。"""
    r = Rule()
    r.ev(depth_quote(outer_vol=90_000.0, inner_vol=90_000.0, volume_lots=200_000.0),
         ts=NOW - timedelta(seconds=12))
    r.ev(depth_quote(outer_vol=1_000.0, inner_vol=1_000.0, volume_lots=2_000.0),
         ts=NOW - timedelta(seconds=6))
    cur = depth_quote(outer_vol=7_000.0, inner_vol=1_000.0, volume_lots=8_000.0)
    assert "big_buy" in patterns_of(r.ev(cur, ts=NOW))


def test_flat_deltas_do_not_alert():
    """两轮完全一样（没成交）-> 不报。"""
    r = Rule()
    assert patterns_of(r.ev2(quiet(), quiet())) == set()


def test_amount_rollback_does_not_crash():
    """成交额回退（部分数据源会重算）不应崩，也不该产生负金额。"""
    r = Rule()
    prev = quiet()
    cur = prev.copy_with(outer_vol=56_000.0, inner_vol=50_000.0,
                         volume_lots=106_000.0, amount=1_000.0)
    a = by_pattern(r.ev2(cur, prev)).get("institution_eat")
    assert a is not None
    assert a.metrics["buy_amount"] == pytest.approx(0.0)


def test_next_day_resets_prev_and_does_not_alert():
    """跨自然日：次日第一轮不报（当日累计量从 0 重新开始）。"""
    r = Rule()
    r.ev(quiet(), ts=NOW)
    t_next = datetime(2026, 9, 16, 9, 35, 0)
    big = depth_quote(outer_vol=200_000.0, inner_vol=200_000.0,
                      volume_lots=400_000.0, amount=400_000.0 * 100.0 * 10.0)
    assert r.ev(big, ts=t_next) == []
    assert r.rule._day == "2026-09-16"


def test_next_day_resets_edge_state_and_can_rearm():
    """跨日边沿状态清空：昨天持续成立的"机构买单"今天重新报一次。"""
    r = Rule(**WALL_OFF)
    q = depth_quote(bids=(6_000.0, 1, 1, 1, 1))
    assert len(r.ev([q])) == 1                     # 当天首次 -> 报
    assert r.ev([q]) == []                         # 持续成立 -> 不重报
    t_next = datetime(2026, 9, 16, 9, 35, 0)
    assert len(r.ev([q], ts=t_next)) == 1


def test_prev_cache_is_cleared_on_day_change():
    r = Rule()
    r.ev(quiet(), ts=NOW)
    assert r.rule._prev
    t_next = datetime(2026, 9, 16, 9, 35, 0)
    r.ev(quiet(), ts=t_next)
    assert list(r.rule._prev) == ["600000"]        # 只剩新一天写入的这一条


def test_stale_code_cache_dropped_between_rounds():
    """某只票消失若干轮再回来 -> 视作首轮，不把缺席期的量算成一轮增量。"""
    r = Rule()
    r.ev([quiet("600000")], ts=NOW - timedelta(seconds=6))
    r.ev([quiet("000001")], ts=NOW)                # 本轮快照里没有 600000
    assert "600000" not in r.rule._prev
    big = depth_quote(code="600000", outer_vol=500_000.0, inner_vol=500_000.0,
                      volume_lots=1_000_000.0, amount=1_000_000.0 * 100.0 * 10.0)
    assert r.ev([big], ts=NOW + timedelta(seconds=6)) == []


# ===========================================================================
# 8. key 唯一性 / AlertBus 互不吞并
# ===========================================================================
def fire_all_eight(rule: Rule, code: str = "600000"):
    """构造一只同时触发 8 个信号的票（分两轮，成交类需要上轮数据）。

    单档 12,600 手 = 126 万股 -> 机构买单/卖单（> 50 万股）
    五档合计 12,604 手 = 126 万股 -> 有大买盘/大卖盘（> 80 万股）
    外盘 +6,000 手 = 0.12% -> 大笔买入 + 机构吃货（60 万股 > 50 万股）
    内盘 +7,000 手 = 0.14% -> 大笔卖出 + 机构吐货（70 万股 > 50 万股）
    """
    prev = quiet(code)
    cur = advance(
        prev, d_outer=6_000.0, d_inner=7_000.0,
        bids=(12_600.0, 1, 1, 1, 1), asks=(12_600.0, 1, 1, 1, 1),
    )
    return rule.ev2(cur, prev)


def test_fire_all_eight_helper_really_fires_all_eight():
    r = Rule(cooldown_seconds=COOLDOWN, max_per_round=0)
    assert patterns_of(fire_all_eight(r)) == set(PATTERNS)


def test_all_eight_patterns_have_distinct_keys_in_same_bucket():
    r = Rule(cooldown_seconds=COOLDOWN, max_per_round=0)
    alerts = fire_all_eight(r)
    got = by_pattern(alerts)
    assert set(got) == set(PATTERNS)
    keys = [a.key for a in alerts]
    assert len(keys) == len(set(keys)) == 8
    b = bucket_of(NOW_EPOCH, COOLDOWN)
    for p, a in got.items():
        assert a.key == f"600000:{AlertKind.UNUSUAL.value}:{p}:{b}"


def test_alertbus_does_not_swallow_any_of_the_eight():
    """真 AlertBus 上验证：8 条同桶告警必须全部放行（互不吞并）。"""
    r = Rule(cooldown_seconds=COOLDOWN, max_per_round=0)
    alerts = fire_all_eight(r)
    assert len(alerts) == 8
    bus = AlertBus(EngineState())
    accepted = [a for a in alerts if bus.accept(a, NOW_EPOCH)]
    assert len(accepted) == 8, "同桶内不同信号被互相吞掉了"
    assert bus.accept(alerts[0], NOW_EPOCH + 1.0) is False      # 同 key 再来一次被拦


def test_alertbus_keys_differ_from_other_rules():
    """与别的规则（volume_burst/unusual/surge）的 key 也不冲突。"""
    r = Rule(cooldown_seconds=COOLDOWN, max_per_round=0)
    mine = {a.key for a in fire_all_eight(r)}
    b = bucket_of(NOW_EPOCH, COOLDOWN)
    others = {
        f"600000:{AlertKind.VOLUME_BURST.value}:{b}",
        f"600000:{AlertKind.UNUSUAL.value}:{b}",             # 无 pattern 分段的旧式 key
        f"600000:{AlertKind.SURGE.value}:{b}",
    }
    assert mine & others == set()


def test_key_has_pattern_segment():
    """key 必须有信号名分段（本项目刚修过的严重 bug，回归保护）。"""
    r = Rule(**WALL_OFF)
    a = by_pattern(r.ev([depth_quote(bids=(6_000.0, 1, 1, 1, 1))]))["institution_buy"]
    parts = a.key.split(":")
    assert len(parts) == 4
    assert parts[0] == "600000" and parts[1] == "unusual" and parts[2] == "institution_buy"


def test_key_is_stable_within_bucket():
    """同一冷却桶内，同一信号重新触发时 key 不变（幂等）。"""
    t1 = NOW
    t2 = NOW + timedelta(seconds=30)
    b1 = bucket_of(t1.timestamp(), COOLDOWN)
    assert b1 == bucket_of(t2.timestamp(), COOLDOWN)
    r = Rule(cooldown_seconds=COOLDOWN, **WALL_OFF)
    q = depth_quote(bids=(6_000.0, 1, 1, 1, 1))
    k1 = r.ev([q], ts=t1)[0].key
    r.ev([depth_quote(bids=(100.0,) * 5)], ts=t1 + timedelta(seconds=6))   # 撤单 -> 恢复
    k2 = r.ev([q], ts=t2)[0].key
    assert k1 == k2 == f"600000:unusual:institution_buy:{b1}"


def test_key_bucket_changes_across_buckets():
    """跨桶（+600 秒）后 key 的时间桶分段变化。"""
    t2 = NOW + timedelta(seconds=600)
    b1 = bucket_of(NOW_EPOCH, COOLDOWN)
    b2 = bucket_of(t2.timestamp(), COOLDOWN)
    assert b1 != b2
    r = Rule(cooldown_seconds=COOLDOWN, **WALL_OFF)
    q = depth_quote(bids=(6_000.0, 1, 1, 1, 1))
    r.ev([q], ts=NOW)
    r.ev([depth_quote(bids=(100.0,) * 5)], ts=NOW + timedelta(seconds=300))
    a = r.ev([q], ts=t2)[0]
    assert a.key == f"600000:unusual:institution_buy:{b2}"


# ===========================================================================
# 9. 边沿触发（状态型）
# ===========================================================================
def test_institution_buy_edge_fires_once_while_holding():
    r = Rule(**WALL_OFF)
    q = depth_quote(bids=(6_000.0, 1, 1, 1, 1))
    assert len(r.ev([q])) == 1
    for i in range(1, 6):
        t = NOW + timedelta(seconds=6 * i)
        assert r.ev([q], ts=t) == [], f"第 {i} 轮不应重报"


def test_institution_buy_rearms_after_disappearing():
    r = Rule(**WALL_OFF)
    q = depth_quote(bids=(6_000.0, 1, 1, 1, 1))
    thin = depth_quote(bids=(100.0,) * 5)
    assert len(r.ev([q], ts=NOW)) == 1
    assert r.ev([thin], ts=NOW + timedelta(seconds=6)) == []
    assert len(r.ev([q], ts=NOW + timedelta(seconds=12))) == 1


def test_institution_sell_edge_fires_once():
    r = Rule(**WALL_OFF)
    q = depth_quote(asks=(6_000.0, 1, 1, 1, 1))
    assert len(r.ev([q])) == 1
    assert r.ev([q], ts=NOW + timedelta(seconds=6)) == []


def test_institution_sell_rearms():
    r = Rule(**WALL_OFF)
    q = depth_quote(asks=(6_000.0, 1, 1, 1, 1))
    thin = depth_quote(asks=(100.0,) * 5)
    assert len(r.ev([q], ts=NOW)) == 1
    assert r.ev([thin], ts=NOW + timedelta(seconds=6)) == []
    assert len(r.ev([q], ts=NOW + timedelta(seconds=12))) == 1


def test_big_bid_wall_edge_fires_once_and_rearms():
    r = Rule(**ORDER_OFF)
    wall = depth_quote(bids=(1_700.0,) * 5)
    thin = depth_quote(bids=(100.0,) * 5)
    assert len(r.ev([wall], ts=NOW)) == 1
    assert r.ev([wall], ts=NOW + timedelta(seconds=6)) == []
    assert r.ev([thin], ts=NOW + timedelta(seconds=12)) == []
    assert len(r.ev([wall], ts=NOW + timedelta(seconds=18))) == 1


def test_big_ask_wall_edge_fires_once_and_rearms():
    r = Rule(**ORDER_OFF)
    wall = depth_quote(asks=(1_700.0,) * 5)
    thin = depth_quote(asks=(100.0,) * 5)
    assert len(r.ev([wall], ts=NOW)) == 1
    assert r.ev([wall], ts=NOW + timedelta(seconds=6)) == []
    assert r.ev([thin], ts=NOW + timedelta(seconds=12)) == []
    assert len(r.ev([wall], ts=NOW + timedelta(seconds=18))) == 1


def test_edge_states_are_per_stock():
    """一只票的边沿状态不能影响另一只票。"""
    r = Rule(**WALL_OFF)
    q1 = depth_quote(code="600000", bids=(6_000.0, 1, 1, 1, 1))
    q2 = depth_quote(code="000001", bids=(6_000.0, 1, 1, 1, 1))
    assert len(r.ev([q1, q2])) == 2


def test_edge_states_are_per_pattern():
    """同一只票的"机构买单"不能把"有大买盘"的边沿状态吃掉。"""
    r = Rule()
    q = depth_quote(bids=(6_000.0, 1, 1, 1, 1))     # 单档过 50 万股，合计 6,004 手
    got = patterns_of(r.ev([q]))
    assert "institution_buy" in got
    assert "big_bid_wall" not in got                # 6,004 手 = 60 万股 < 80 万股
    # 换成合计也过关的挂单：两条必须同时出现
    r2 = Rule()
    got2 = patterns_of(r2.ev([depth_quote(bids=(6_000.0, 3_000.0, 1, 1, 1))]))
    assert "institution_buy" in got2 and "big_bid_wall" in got2


def test_trade_signals_are_event_type_not_edge():
    """成交类**是事件型**：每轮都有足够增量就该每轮报（不是边沿）。"""
    r = Rule()
    prev = quiet()
    assert "big_buy" in patterns_of(r.ev2(buy_cur(), prev))
    nxt = advance(quiet(), d_outer=12_000.0)       # 新一轮又从 50,000 起算 -> +6,000 手
    t = NOW + timedelta(seconds=6)
    assert "big_buy" in patterns_of(r.ev(nxt, ts=t))


# ===========================================================================
# 10. 冷却字段
# ===========================================================================
def test_cooldown_fields_on_every_alert():
    r = Rule(cooldown_seconds=COOLDOWN, max_per_round=0)
    alerts = fire_all_eight(r)
    assert len(alerts) == 8
    for a in alerts:
        assert a.cooldown_key == f"600000:unusual:{a.metrics['pattern']}"
        assert a.cooldown_seconds == pytest.approx(float(COOLDOWN))


def test_cooldown_key_is_stable_across_buckets():
    """cooldown_key 不含时间桶：跨桶也要能靠它判断"距上次不足 N 秒"。"""
    t1 = datetime(2026, 9, 15, 10, 4, 59)
    t2 = datetime(2026, 9, 15, 10, 5, 10)          # 跨桶只差 11 秒
    r = Rule(cooldown_seconds=COOLDOWN, **WALL_OFF)
    q = depth_quote(bids=(6_000.0, 1, 1, 1, 1))
    a1 = r.ev([q], ts=t1)[0]
    r.ev([depth_quote(bids=(100.0,) * 5)], ts=t1 + timedelta(seconds=2))
    a2 = r.ev([q], ts=t2)[0]
    assert a1.key != a2.key                        # 分桶不同
    assert a1.cooldown_key == a2.cooldown_key      # 稳定身份相同
    bus = AlertBus(EngineState())
    assert bus.accept(a1, t1.timestamp()) is True
    assert bus.accept(a2, t2.timestamp()) is False, "跨桶只差 11 秒不应放行"


def test_cooldown_configurable():
    r = Rule(cooldown_seconds=45.0, **WALL_OFF)
    a = r.ev([depth_quote(bids=(6_000.0, 1, 1, 1, 1))])[0]
    assert a.cooldown_seconds == pytest.approx(45.0)
    assert a.key.endswith(f":{bucket_of(NOW_EPOCH, 45.0)}")


# ===========================================================================
# 11. 停牌 / 无效行情 / 时段 / enabled
# ===========================================================================
def test_suspended_and_invalid_quotes_are_skipped():
    r = Rule()
    big = dict(bids=(6_000.0, 1, 1, 1, 1), outer_vol=500_000.0, inner_vol=500_000.0)
    assert r.ev([depth_quote(volume_lots=0.0, **big)]) == []        # 停牌
    assert r.ev([depth_quote(price=0.0, **big)]) == []              # 无价
    assert r.ev([depth_quote(prev_close=0.0, **big)]) == []         # 昨收异常
    assert r.ev([depth_quote(prev_close=-1.0, **big)]) == []


def test_prev_close_zero_blocks_trade_signals_too():
    r = Rule()
    r.ev(quiet(), ts=NOW - timedelta(seconds=6))
    cur = depth_quote(prev_close=0.0, outer_vol=200_000.0, inner_vol=200_000.0,
                      volume_lots=400_000.0)
    assert r.ev(cur) == []


def test_invalid_numeric_fields_do_not_crash():
    """None / 字符串量（数据源脏数据）不能炸掉整轮。"""
    r = Rule(**WALL_OFF)
    q = depth_quote(bids=(None, "abc", 6_000.0, "", 1.0))
    assert patterns_of(r.ev([q])) == {"institution_buy"}


def test_only_continuous_switch():
    q = depth_quote(bids=(6_000.0, 1, 1, 1, 1))
    for phase in (SessionPhase.CLOSED, SessionPhase.LUNCH, SessionPhase.POST,
                  SessionPhase.PRE_OPEN, SessionPhase.AUCTION):
        assert Rule().ev([q], session=phase) == []
    for phase in (SessionPhase.MORNING, SessionPhase.AFTERNOON):
        assert len(Rule().ev([q], session=phase)) == 1


def test_only_continuous_false_alerts_in_lunch():
    r = Rule(only_continuous=False, **WALL_OFF)
    assert len(r.ev([depth_quote(bids=(6_000.0, 1, 1, 1, 1))],
                    session=SessionPhase.LUNCH)) == 1


def test_enabled_false_returns_nothing():
    r = Rule(enabled=False)
    assert r.ev([depth_quote(bids=(6_000.0, 1, 1, 1, 1))]) == []


def test_enabled_false_skips_trade_side_too():
    r = Rule(enabled=False)
    assert r.ev2(buy_cur(), buy_prev()) == []


def test_detect_group_switches():
    """detect_order / detect_trade 可分别关闭两组信号。"""
    prev = quiet()
    cur = advance(prev, d_outer=6_000.0, d_inner=7_000.0,
                  bids=(12_600.0, 1, 1, 1, 1), asks=(12_600.0, 1, 1, 1, 1))

    r_order_off = Rule(detect_order=False)
    got = patterns_of(r_order_off.ev2(cur, prev))
    assert "institution_buy" not in got and "big_bid_wall" not in got
    assert "big_buy" in got and "institution_eat" in got

    r_trade_off = Rule(detect_trade=False)
    got2 = patterns_of(r_trade_off.ev2(cur, prev))
    assert "big_buy" not in got2 and "institution_eat" not in got2
    assert "institution_buy" in got2


def test_ctx_cfg_overrides_instance_cfg():
    """引擎下发的 ctx.cfg 优先于实例自身 cfg。"""
    rule = build(cfg_of(**WALL_OFF))
    snap = make_snapshot([depth_quote(bids=(6_000.0, 1, 1, 1, 1))], ts=NOW)
    assert rule.evaluate(snap, ctx_of(FakeState(), cfg=cfg_of(enabled=False))) == []
    assert len(rule.evaluate(snap, ctx_of(FakeState(), cfg=cfg_of(**WALL_OFF)))) == 1


def test_empty_ctx_cfg_falls_back_to_instance():
    rule = build(cfg_of(**WALL_OFF))
    snap = make_snapshot([depth_quote(bids=(6_000.0, 1, 1, 1, 1))], ts=NOW)
    ctx = RuleContext(state=FakeState(), cfg={}, now=NOW, session=SessionPhase.MORNING,
                      elapsed_trading_seconds=1800.0, minutes_to_close=120.0)
    assert len(rule.evaluate(snap, ctx)) == 1


def test_empty_snapshot():
    r = Rule()
    assert r.ev([]) == []


# ===========================================================================
# 12. 排序 / 限量 / 载荷
# ===========================================================================
def test_detail_payload_tokens():
    r = Rule(cooldown_seconds=COOLDOWN, max_per_round=0)
    a = by_pattern(fire_all_eight(r))["institution_eat"]
    for token in ("机构吃货", "现价", "涨跌幅", "换手", "成交额", "外盘", "内盘", "均价"):
        assert token in a.detail, token
    assert a.kind is AlertKind.UNUSUAL
    assert a.name == "测试股"


def test_metrics_payload_has_traceability_fields():
    r = Rule(cooldown_seconds=COOLDOWN, max_per_round=0)
    a = by_pattern(fire_all_eight(r))["big_buy"]
    for key in ("pattern", "pct", "price", "amount", "turnover",
                "outer_vol", "inner_vol", "float_shares", "above_vwap",
                "d_outer_lots", "float_pct"):
        assert key in a.metrics, key
    assert a.metrics["d_outer_lots"] == pytest.approx(6_000.0)
    assert a.metrics["float_shares"] == pytest.approx(FLOAT_SHARES)


def test_alert_to_dict_is_json_safe():
    """metrics 里不得出现 NaN/Inf（会炸 SSE 的 JSON.parse）。"""
    import json

    r = Rule(cooldown_seconds=COOLDOWN, max_per_round=0)
    for a in fire_all_eight(r):
        json.dumps(a.to_dict(), ensure_ascii=False, allow_nan=False)


def test_severity_by_pattern():
    r = Rule(cooldown_seconds=COOLDOWN, max_per_round=0)
    sev = {p: a.severity for p, a in by_pattern(fire_all_eight(r)).items()}
    assert sev["institution_eat"] == 3 and sev["institution_vomit"] == 3
    assert sev["big_buy"] == 2 and sev["big_sell"] == 2
    assert sev["institution_buy"] == 2 and sev["institution_sell"] == 2
    assert sev["big_bid_wall"] == 1 and sev["big_ask_wall"] == 1


def test_sorting_is_severity_descending():
    r = Rule(cooldown_seconds=COOLDOWN, max_per_round=0)
    sevs = [a.severity for a in fire_all_eight(r)]
    assert sevs == sorted(sevs, reverse=True)
    assert sevs[0] == 3 and sevs[-1] == 1


def test_max_per_round_truncates():
    r = Rule(max_per_round=3, cooldown_seconds=COOLDOWN)
    assert len(fire_all_eight(r)) == 3


def test_max_per_round_zero_means_unlimited():
    r = Rule(max_per_round=0, cooldown_seconds=COOLDOWN)
    assert len(fire_all_eight(r)) == 8


def test_ties_broken_by_code_ascending():
    r = Rule(max_per_round=0, cooldown_seconds=COOLDOWN, **WALL_OFF)
    quotes = [depth_quote(code=c, bids=(6_000.0, 1, 1, 1, 1))
              for c in ("600003", "600001", "600002")]
    assert [a.code for a in r.ev(quotes)] == ["600001", "600002", "600003"]


# ===========================================================================
# 13. 确定性 / 性能
# ===========================================================================
def test_determinism_same_input_same_output():
    def run():
        r = Rule(cooldown_seconds=COOLDOWN, max_per_round=0)
        return [(a.key, a.title, a.detail, a.severity, tuple(sorted(a.metrics)))
                for a in fire_all_eight(r)]

    assert run() == run()


def test_determinism_across_quote_insertion_order():
    def build_quotes(order):
        return [depth_quote(code=c, bids=(6_000.0, 1, 1, 1, 1)) for c in order]

    codes = ("600001", "600002", "600003")
    r1, r2 = Rule(**WALL_OFF), Rule(**WALL_OFF)
    a1 = [a.code for a in r1.ev(build_quotes(codes))]
    a2 = [a.code for a in r2.ev(build_quotes(tuple(reversed(codes))))]
    assert a1 == a2 == list(codes)


def test_perf_1000_stocks_under_200ms():
    """1000 只股票、两轮快照、每只 **8 个信号全中**，须 < 200ms。

    这是生产配置（``max_per_round=20``）。截断发生在 Alert 构造之前，所以
    真正昂贵的只有"截断后那 20 条"的文本拼装 —— 这也是本模块最关键的性能设计。
    取 3 次里最快的一次，避免 CI 上其它用例的 GC/JIT 抖动把断言打飘。
    """
    r = Rule()                      # max_per_round 默认 20
    prev, cur = [], []
    for i in range(1000):
        code = f"{600000 + i:06d}"
        p = quiet(code, name=f"票{i}")
        prev.append(p)
        cur.append(advance(p, d_outer=6_000.0, d_inner=7_000.0,
                           bids=(12_600.0, 1, 1, 1, 1), asks=(12_600.0, 1, 1, 1, 1)))
    r.ev(prev, ts=NOW - timedelta(seconds=6))
    best, alerts = float("inf"), []
    for _ in range(3):
        # 每次都得重新喂上一轮：调用一次之后 _prev 就变成 cur 了，
        # 再喂同样的 cur 增量为 0（而且边沿状态也已点亮）-> 会得到 0 条。
        r.ev(prev, ts=NOW - timedelta(seconds=6))
        t0 = time.perf_counter()
        alerts = r.ev(cur, ts=NOW)
        best = min(best, (time.perf_counter() - t0) * 1000.0)
    assert len(alerts) == 20        # 8000 个命中被截断到 20
    assert best < 200.0, f"1000 只股票 evaluate 最快 {best:.1f}ms >= 200ms"


def test_perf_1000_stocks_unlimited_materializes_all():
    """``max_per_round=0``（不限量）时必须把 8000 条全建出来 —— 只验正确性。

    这里刻意**不**套 200ms 预算：不限量意味着真的构造 8000 个 Alert（每个带
    4 行 detail 文本），本身就不是生产配置（默认 20）。它的价值在于钉住
    "延迟构造不会改变输出条数"这个语义，性能预算由上面那条生产配置用例负责。
    """
    r = Rule(max_per_round=0)
    prev, cur = [], []
    for i in range(1000):
        code = f"{600000 + i:06d}"
        p = quiet(code, name=f"票{i}")
        prev.append(p)
        cur.append(advance(p, d_outer=6_000.0, d_inner=7_000.0,
                           bids=(12_600.0, 1, 1, 1, 1), asks=(12_600.0, 1, 1, 1, 1)))
    r.ev(prev, ts=NOW - timedelta(seconds=6))
    t0 = time.perf_counter()
    alerts = r.ev(cur, ts=NOW)
    ms = (time.perf_counter() - t0) * 1000.0
    assert len(alerts) == 8_000
    assert all(a.key and a.detail for a in alerts)
    assert ms < 2000.0, f"不限量 8000 条耗时 {ms:.1f}ms 异常（预期数百毫秒量级）"


def test_perf_1000_stocks_first_round_under_200ms():
    """首轮（无上轮缓存、无信号）也要快。"""
    r = Rule(max_per_round=0)
    quotes = [quiet(f"{600000 + i:06d}", name=f"票{i}") for i in range(1000)]
    t0 = time.perf_counter()
    alerts = r.ev(quotes)
    ms = (time.perf_counter() - t0) * 1000.0
    assert patterns_of(alerts) == set()
    assert ms < 200.0, f"首轮 1000 只股票耗时 {ms:.1f}ms >= 200ms"
