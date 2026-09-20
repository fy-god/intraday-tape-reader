"""涨跌停规则测试（离线，用极简 FakeState，不依赖 engine）。

覆盖：触板 / 封板 / 炸板 / 一字板 / 首板 / 幂等键 / 各板块涨跌停比例 / 边界与性能。

所有断言都对齐 ``src/arad/rules/limit_board.py`` 的**真实实现**，其中若干处与
``docs/DATA_CONTRACT.md`` 的通用约定略有差异，已在对应测试里注明（例如炸板 key
多了一段 ``break``）。测试全部离线、确定性：固定时间戳 ``T0``，不联网。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from arad.models import Alert, AlertKind, Quote, Snapshot, limit_rate_of
from arad.rules.base import RuleContext, bucket_of
from arad.rules.limit_board import DEFAULT_CFG, LimitBoardRule, build
from arad.session import SessionPhase

from fakes import make_quote

T0 = 1_800_000_000.0        # 固定基准时间戳（2027-01-15 16:00:00 CST）

# _check_side 三个状态下 metrics 的真实键名（严格对齐源码，不臆造）
# 注意 pattern / rising 是短线精灵展示层需要的字段：没有 pattern，精灵流会把
# "触及涨停 +16.40%" 显示成"涨停"（与实际不符）；rising 用于区分方向。
SEAL_METRIC_KEYS = {
    "price", "pct", "limit_up", "limit_down", "distance_to_limit_pct",
    "seal_amount_wan", "one_word_board", "is_first_board", "touch_count", "stage",
    "pattern", "rising",
}
# 炸板/撬板路径**故意不叫 seal_amount_wan**：板已经开了就不存在封单，
# 买一/卖一那点量只是普通挂单。旧代码复用 _seal_amount_wan 并把同一个数
# 写进 metrics，而 seal_amount_wan 在 spirit.to_feed_item 的 extra 白名单里，
# 看板悬停会照原样展示 —— 用户被明确告知"这是封单"。故用 bid1_amount_wan。
BREAK_METRIC_KEYS = (SEAL_METRIC_KEYS - {"seal_amount_wan"}) | {
    "retreat_pct", "bid1_amount_wan"}


# ==========================================================================
# 测试脚手架（结构照抄 tests/test_rule_tick_surge.py）
# ==========================================================================
class FakeState:
    """实现规则用到的唯一接口：window（limit_board 只用它取历史窗口）。"""

    def __init__(self):
        self.history: dict[str, list[tuple[float, float, float]]] = {}
        self.quotes: dict[str, Quote] = {}

    def feed(self, code: str, points):
        """points: [(ts_epoch, price, cum_volume_lots)]，时间戳必须严格递增。"""
        self.history[code] = list(points)

    def window(self, code, seconds, now_epoch):
        pts = self.history.get(code, [])
        cutoff = now_epoch - float(seconds)
        return [p for p in pts if p[0] >= cutoff]


class NoWindowState:
    """没有 window() 的状态对象：验证规则的历史兜底分支不炸。

    比 FakeState 更接近"历史不足"的真实情形（引擎尚未写入该 code 的历史）。
    """

    def __init__(self):
        self.history: dict[str, list] = {}
        self.quotes: dict[str, Quote] = {}


def mk_ctx(state, *, session=SessionPhase.MORNING, now_ep=T0, cfg=None):
    return RuleContext(
        state=state,
        cfg=cfg or {},
        now=datetime.fromtimestamp(now_ep),
        session=session,
        elapsed_trading_seconds=1800.0,
        minutes_to_close=120.0,
    )


def snap_of(*quotes: Quote) -> Snapshot:
    return Snapshot(ts=datetime.fromtimestamp(T0), seq=1,
                    quotes={q.code: q for q in quotes})


def limit_up_quote(code="600000", name="测试股", *, prev_close=10.0, price=None,
                   bid_vol=30000.0, high=None, **kw) -> Quote:
    """封涨停行情：默认现价 == 涨停价（按 limit_rate_of 推算），买一挂大单。

    封单额默认 30000 手 × 100 股 × 11 元 = 3300 万，远超 200 万门槛。
    """
    limit = round(prev_close * (1.0 + limit_rate_of(code, name)), 2)
    price = limit if price is None else price
    kw.setdefault("open", round(prev_close * 1.02, 2))
    kw.setdefault("low", round(prev_close * 1.01, 2))
    kw.setdefault("volume_lots", 50000.0)
    kw.setdefault("amount", 50000.0 * 100.0 * price * 0.98)
    kw.setdefault("turnover", 5.0)
    return make_quote(code=code, name=name, price=price, prev_close=prev_close,
                      high=price if high is None else high, bid_vol=bid_vol, **kw)


def limit_down_quote(code="600000", name="测试股", *, prev_close=10.0, price=None,
                     ask_vol=30000.0, high=None, **kw) -> Quote:
    """封跌停行情：默认现价 == 跌停价，卖一挂大单（封单额看 ask_vol）。"""
    limit = round(prev_close * (1.0 - limit_rate_of(code, name)), 2)
    price = limit if price is None else price
    kw.setdefault("open", round(prev_close * 0.97, 2))
    kw.setdefault("volume_lots", 50000.0)
    kw.setdefault("amount", 50000.0 * 100.0 * price)
    kw.setdefault("turnover", 5.0)
    kw.setdefault("low", price)
    return make_quote(code=code, name=name, price=price, prev_close=prev_close,
                      high=high if high is not None else round(prev_close * 0.98, 2),
                      ask_vol=ask_vol, **kw)


def sealed_history(code: str, *, limit=11.0, now_price=10.5):
    """一段"曾封板、现价回落"的历史（时间戳严格递增、无重复点）。"""
    return [
        (T0 - 600.0, limit, 1000.0),
        (T0 - 300.0, limit, 2000.0),
        (T0 - 60.0, now_price, 3000.0),
    ]


# ==========================================================================
# 封板（stage 2）
# ==========================================================================
def test_seal_limit_up_basic():
    """现价 == 涨停价 + 封单达标 -> LIMIT_UP / severity 3 / stage 2。"""
    st = FakeState()
    q = limit_up_quote()
    alerts = build({}).evaluate(snap_of(q), mk_ctx(st))

    assert len(alerts) == 1
    a = alerts[0]
    assert isinstance(a, Alert)
    assert a.kind is AlertKind.LIMIT_UP
    assert a.severity == 3
    assert a.metrics["stage"] == 2.0
    assert a.code == "600000"
    assert a.price == pytest.approx(11.0)
    assert a.pct == pytest.approx(10.0)
    assert a.ts == datetime.fromtimestamp(T0)
    assert "封涨停" in a.title


def test_seal_amount_matches_contract_arithmetic():
    """封单额 = bid_vol * 100 * price / 1e4（万元），且 metrics 用的是同一个值。"""
    st = FakeState()
    q = limit_up_quote(bid_vol=12345.0)
    rule = build({})

    expected = 12345.0 * 100.0 * 11.0 / 1e4        # = 1357.95 万元
    assert rule._seal_amount_wan(q, True) == pytest.approx(expected)
    assert rule._seal_amount_wan(q, True) == pytest.approx(1357.95)

    a = rule.evaluate(snap_of(q), mk_ctx(st))[0]
    assert a.metrics["seal_amount_wan"] == pytest.approx(expected, abs=0.05)
    assert f"封单{expected:.0f}万" in a.title


def test_seal_amount_uses_ask_volume_for_limit_down():
    """跌停封单看卖一量：ask_vol * 100 * price / 1e4。"""
    st = FakeState()
    q = limit_down_quote(ask_vol=20000.0)          # 20000 * 100 * 9 / 1e4 = 1800 万
    rule = build({})

    assert rule._seal_amount_wan(q, False) == pytest.approx(1800.0)
    # 涨停侧看买一量，此处 bid_vol 为 0 -> 封单额 0
    assert rule._seal_amount_wan(q, True) == pytest.approx(0.0)

    a = rule.evaluate(snap_of(q), mk_ctx(st))[0]
    assert a.kind is AlertKind.LIMIT_DOWN
    assert a.metrics["seal_amount_wan"] == pytest.approx(1800.0)
    assert a.metrics["stage"] == 2.0
    assert a.severity == 3
    assert "封跌停" in a.title


def test_seal_below_min_amount_is_rejected():
    """封单 110 万 < 默认 200 万门槛 -> 不算封板，且没有历史可回落 -> 无告警。"""
    st = FakeState()
    q = limit_up_quote(bid_vol=100.0)              # 100 * 100 * 11 / 1e4 = 11 万
    assert build({})._seal_amount_wan(q, True) == pytest.approx(11.0)
    assert build({}).evaluate(snap_of(q), mk_ctx(st)) == []


def test_seal_amount_threshold_boundary():
    """门槛是"小于才拒绝"：199.98 万拒绝，200.09 万放行。"""
    st = FakeState()
    below = limit_up_quote(bid_vol=1818.0)         # 1818*100*11/1e4 = 199.98 万
    above = limit_up_quote(bid_vol=1819.0)         # 1819*100*11/1e4 = 200.09 万
    rule = build({"min_seal_amount_wan": 200})

    assert rule._seal_amount_wan(below, True) == pytest.approx(199.98)
    assert rule._seal_amount_wan(above, True) == pytest.approx(200.09)
    assert build({"min_seal_amount_wan": 200}).evaluate(snap_of(below), mk_ctx(st)) == []
    assert len(build({"min_seal_amount_wan": 200}).evaluate(snap_of(above), mk_ctx(st))) == 1


def test_seal_amount_zero_threshold_disables_gate():
    """min_seal_amount_wan=0 表示不设门槛（只有一份买一挂单也算封板）。"""
    st = FakeState()
    q = limit_up_quote(bid_vol=1.0)
    rule = build({"min_seal_amount_wan": 0})
    alerts = rule.evaluate(snap_of(q), mk_ctx(st))
    assert len(alerts) == 1
    assert alerts[0].metrics["seal_amount_wan"] == pytest.approx(0.11, abs=0.05)


def test_detect_seal_switch_off_produces_nothing():
    """detect_seal=False 时，现价贴在限价上也不出告警。"""
    st = FakeState()
    q = limit_up_quote()
    assert build({"detect_seal": False}).evaluate(snap_of(q), mk_ctx(st)) == []


def test_tolerance_boundary_between_seal_and_touch():
    """容差 0.001 元：现价 >= 涨停价-0.001 算封板，再低一点降级为触板。"""
    st = FakeState()
    inside = limit_up_quote(price=10.9995, bid_vol=30000.0, high=11.0, low=10.0)
    outside = limit_up_quote(price=10.9975, bid_vol=30000.0, high=11.0, low=10.0)

    a = build({}).evaluate(snap_of(inside), mk_ctx(st))
    assert [x.metrics["stage"] for x in a] == [2.0]
    b = build({}).evaluate(snap_of(outside), mk_ctx(st))
    assert [x.metrics["stage"] for x in b] == [1.0]


def test_seal_detail_is_multiline_and_marks_first_board():
    """封板明细含封单/板别/换手/成交额/振幅，且是多行文本。"""
    st = FakeState()
    a = build({}).evaluate(snap_of(limit_up_quote()), mk_ctx(st))[0]
    assert a.detail.count("\n") >= 2
    assert "封单" in a.detail
    assert "首板" in a.detail
    assert "涨停价 11.00" in a.detail


# ==========================================================================
# 触板（stage 1）
# ==========================================================================
def test_touch_limit_up_basic():
    """现价贴近涨停但未封住（回落 0.27% < 0.3%）-> 触板，severity 用配置值。"""
    st = FakeState()
    q = limit_up_quote(price=10.97, bid_vol=0.0, high=11.0, low=10.0)
    alerts = build({}).evaluate(snap_of(q), mk_ctx(st))

    assert len(alerts) == 1
    a = alerts[0]
    assert a.kind is AlertKind.LIMIT_UP
    assert a.severity == 2                 # DEFAULT_CFG["severity"]
    assert a.metrics["stage"] == 1.0
    assert a.metrics["seal_amount_wan"] == 0.0
    assert "触及涨停" in a.title
    assert "限价 11.00" in a.detail


def test_touch_severity_follows_config():
    """触板 severity 可配；封板/炸板固定 severity=3，不受该配置影响。"""
    st = FakeState()
    q = limit_up_quote(price=10.97, bid_vol=0.0, high=11.0, low=10.0)
    assert build({"severity": 3}).evaluate(snap_of(q), mk_ctx(st))[0].severity == 3
    assert build({"severity": 1}).evaluate(snap_of(q), mk_ctx(st))[0].severity == 1
    assert build({"severity": 1}).evaluate(
        snap_of(limit_up_quote()), mk_ctx(st))[0].severity == 3


def test_touch_via_snapshot_high_without_history():
    """无历史时用快照当日最高价兜底判定"曾触及"。"""
    st = FakeState()                        # 完全没有喂历史
    q = limit_up_quote(price=10.97, bid_vol=0.0, high=11.0, low=10.0)
    assert len(build({}).evaluate(snap_of(q), mk_ctx(st))) == 1
    # 最高价也没碰到涨停 -> 什么也不出
    q2 = limit_up_quote(price=10.97, bid_vol=0.0, high=10.98, low=10.0)
    assert build({}).evaluate(snap_of(q2), mk_ctx(st)) == []


def test_detect_touch_switch_off_produces_nothing():
    """detect_touch=False 时，轻度回落不再出触板。"""
    st = FakeState()
    q = limit_up_quote(price=10.97, bid_vol=0.0, high=11.0, low=10.0)
    assert build({"detect_touch": False}).evaluate(snap_of(q), mk_ctx(st)) == []


# ==========================================================================
# 炸板（stage 3）
# ==========================================================================
def test_break_after_seal():
    """曾封涨停（历史窗口内）后回落到 10.5 -> 炸板 LIMIT_UP / severity 3。"""
    st = FakeState()
    st.feed("600000", sealed_history("600000"))
    q = limit_up_quote(price=10.5, bid_vol=0.0, high=11.0, low=10.2)
    alerts = build({}).evaluate(snap_of(q), mk_ctx(st))

    assert len(alerts) == 1
    a = alerts[0]
    assert a.kind is AlertKind.LIMIT_UP
    assert a.severity == 3
    assert a.metrics["stage"] == 3.0
    assert a.metrics["retreat_pct"] == pytest.approx(4.545, abs=0.01)
    assert a.metrics["distance_to_limit_pct"] == pytest.approx(-4.545, abs=0.01)
    assert a.title.startswith("炸板")
    assert "回落 4.55%" in a.detail
    assert a.metrics["touch_count"] == 1.0


def test_break_reported_once_until_resealed():
    """炸板是一次性事件：报过之后不再重报，直到该股重新封回涨停价。

    过去每个 ``break_bucket_seconds``（60s）换一个新 key，只要现价一直低于
    涨停价就会无限重报同一件事（实测一次炸板刷出几十条）。
    """
    st = FakeState()
    st.feed("600000", sealed_history("600000"))
    broke = limit_up_quote(price=10.5, bid_vol=0.0, high=11.0, low=10.2)
    rule = build({"cooldown_seconds": 300, "break_bucket_seconds": 60})

    a1 = rule.evaluate(snap_of(broke), mk_ctx(st, now_ep=T0))[0]
    assert a1.key == f"600000:limit_up:break:{bucket_of(T0, 60)}"
    assert a1.metrics["stage"] == 3.0

    # 只要没重新封板，后续任何时刻都不再重报
    for dt in (30, 61, 120, 300, 3600, 7200):
        assert rule.evaluate(snap_of(broke), mk_ctx(st, now_ep=T0 + dt)) == [], f"+{dt}s 重复报炸板"


def test_break_refires_after_resealing():
    """重新封回涨停后再度炸板 -> 新事件，必须再报一次。"""
    st = FakeState()
    st.feed("600000", sealed_history("600000"))
    broke = limit_up_quote(price=10.5, bid_vol=0.0, high=11.0, low=10.2)
    sealed = limit_up_quote(bid_vol=30000.0)          # 现价回到 11.0
    rule = build({})

    assert len(rule.evaluate(snap_of(broke), mk_ctx(st, now_ep=T0))) == 1
    assert rule.evaluate(snap_of(broke), mk_ctx(st, now_ep=T0 + 60)) == []

    # 重新封板（重新武装）
    resealed = rule.evaluate(snap_of(sealed), mk_ctx(st, now_ep=T0 + 120))
    assert [a.metrics["stage"] for a in resealed] == [2.0]

    # 再次炸板 -> 再报
    again = rule.evaluate(snap_of(broke), mk_ctx(st, now_ep=T0 + 300))
    assert len(again) == 1
    assert again[0].metrics["stage"] == 3.0


def test_seal_reported_once_while_continuously_sealed():
    """连续封板两小时只报一次：封板是事件，不是每轮重播的状态。"""
    st = FakeState()
    q = limit_up_quote(bid_vol=30000.0)
    rule = build({"cooldown_seconds": 300})

    assert len(rule.evaluate(snap_of(q), mk_ctx(st, now_ep=T0))) == 1
    for dt in (5, 60, 299, 300, 301, 3600, 7200):
        assert rule.evaluate(snap_of(q), mk_ctx(st, now_ep=T0 + dt)) == [], f"+{dt}s 重报封板"


def test_limit_down_seal_reports_once_then_after_reopen():
    """封跌停只报一次；撬开后重新封回跌停 -> 再报一次。"""
    st = FakeState()
    sealed = limit_down_quote(ask_vol=30000.0)
    opened = limit_down_quote(price=9.2, ask_vol=0.0)
    rule = build({})

    assert [a.metrics["stage"] for a in rule.evaluate(snap_of(sealed), mk_ctx(st, now_ep=T0))] == [2.0]
    assert rule.evaluate(snap_of(sealed), mk_ctx(st, now_ep=T0 + 60)) == []

    # 撬开：不告警，但状态必须复位
    assert rule.evaluate(snap_of(opened), mk_ctx(st, now_ep=T0 + 120)) == []

    # 重新封回 -> 新事件
    again = rule.evaluate(snap_of(sealed), mk_ctx(st, now_ep=T0 + 180))
    assert len(again) == 1, "撬开后重新封跌停必须再报"
    assert again[0].metrics["stage"] == 2.0


def test_touch_reported_once_while_price_hovers():
    """触板状态持续时不重报；离开后再次触板才再报。"""
    st = FakeState()
    st.feed("600000", [(T0 - 300.0, 10.98, 1000.0)])
    hover = limit_up_quote(price=10.97, bid_vol=0.0, high=11.0, low=10.9)
    # 真正"远离"涨停：历史与快照 high 都没碰过涨停价
    away = limit_up_quote(price=10.5, bid_vol=0.0, high=10.55, low=10.2)
    rule = build({})

    assert [a.metrics["stage"] for a in rule.evaluate(snap_of(hover), mk_ctx(st, now_ep=T0))] == [1.0]
    assert rule.evaluate(snap_of(hover), mk_ctx(st, now_ep=T0 + 120)) == []

    # 离开限价区（触板历史已滚出窗口）-> 状态复位为 away，且不告警
    st.feed("600000", [(T0 - 300.0, 10.5, 1000.0)])
    assert rule.evaluate(snap_of(away), mk_ctx(st, now_ep=T0 + 240)) == []

    # 再次触板 -> 再报
    st.feed("600000", [(T0 - 300.0, 10.98, 1000.0)])
    again = rule.evaluate(snap_of(hover), mk_ctx(st, now_ep=T0 + 360))
    assert len(again) == 1
    assert again[0].metrics["stage"] == 1.0


def test_state_resets_on_new_trading_day():
    """跨自然日必须清空状态机：昨天封板不影响今天重新报。"""
    st = FakeState()
    q = limit_up_quote(bid_vol=30000.0)
    rule = build({})
    day2 = T0 + 86400.0

    assert len(rule.evaluate(snap_of(q), mk_ctx(st, now_ep=T0))) == 1
    assert rule.evaluate(snap_of(q), mk_ctx(st, now_ep=T0 + 60)) == []
    # 次日：同一状态但属于新的一天，应重新告警
    assert len(rule.evaluate(snap_of(q), mk_ctx(st, now_ep=day2))) == 1


def test_break_without_history_still_deduped():
    """没有历史点（仅靠快照 high 兜底）时也必须幂等，不能每轮重报。"""
    st = FakeState()
    q = limit_up_quote(price=10.5, bid_vol=0.0, high=11.0, low=10.2)
    rule = build({})
    assert len(rule.evaluate(snap_of(q), mk_ctx(st, now_ep=T0))) == 1
    assert rule.evaluate(snap_of(q), mk_ctx(st, now_ep=T0 + 60)) == []
    assert rule.evaluate(snap_of(q), mk_ctx(st, now_ep=T0 + 3600)) == []


def test_break_key_still_carries_break_bucket():
    """保留分桶 key（便于落盘区分），但幂等由触板段保证。"""
    st = FakeState()
    st.feed("600000", sealed_history("600000"))
    q = limit_up_quote(price=10.5, bid_vol=0.0, high=11.0, low=10.2)
    a = build({"cooldown_seconds": 300, "break_bucket_seconds": 60}).evaluate(
        snap_of(q), mk_ctx(st, now_ep=T0))[0]
    assert a.key.endswith(f":break:{bucket_of(T0, 60)}")
    assert bucket_of(T0, 60) != bucket_of(T0, 300)


def test_break_retreat_threshold_is_configurable():
    """回落 0.64% 默认算炸板；把阈值抬到 2% 后降级为触板。"""
    st = FakeState()
    q = limit_up_quote(price=10.93, bid_vol=0.0, high=11.0, low=10.0)
    assert build({}).evaluate(snap_of(q), mk_ctx(st))[0].metrics["stage"] == 3.0
    assert build({"break_retreat_pct": 2.0}).evaluate(
        snap_of(q), mk_ctx(st))[0].metrics["stage"] == 1.0


def test_break_requires_touch_within_lookback():
    """触及点落在 break_lookback_seconds 之外 -> 不算炸板；放宽窗口才认。"""
    st = FakeState()
    st.feed("600000", [(T0 - 2000.0, 11.0, 1000.0), (T0 - 100.0, 10.6, 2000.0)])
    q = limit_up_quote(price=10.5, bid_vol=0.0, high=10.8, low=10.2)   # 快照最高也没到 11
    rule = build({})

    assert rule.evaluate(snap_of(q), mk_ctx(st)) == []                 # 默认 1800s，点被窗口裁掉
    assert rule._touch_count(q, mk_ctx(st), T0, 11.0) == 0
    widened = build({"break_lookback_seconds": 3600})
    assert [a.metrics["stage"] for a in widened.evaluate(snap_of(q), mk_ctx(st))] == [3.0]
    assert widened._touch_count(q, mk_ctx(st), T0, 11.0) == 1


def test_break_uses_snapshot_high_when_history_missing():
    """历史里没有触板点，但快照 high == 涨停价 -> 仍能识别炸板（源码兜底分支）。"""
    st = FakeState()
    q = limit_up_quote(price=10.5, bid_vol=0.0, high=11.0, low=10.2)
    alerts = build({}).evaluate(snap_of(q), mk_ctx(st))
    assert len(alerts) == 1
    assert alerts[0].metrics["stage"] == 3.0


def test_no_break_when_price_still_at_limit():
    """一字板不开板：现价仍在涨停价，绝不判炸板。"""
    st = FakeState()
    st.feed("600000", sealed_history("600000"))
    q = limit_up_quote(bid_vol=30000.0, high=11.0, low=11.0, open=11.0)
    a = build({}).evaluate(snap_of(q), mk_ctx(st))[0]
    assert a.metrics["stage"] == 2.0
    assert a.title.startswith("封涨停")


def test_limit_down_open_is_not_reported():
    """跌停被撬开不告警：源码里跌停侧只走封板分支（触板/炸板均 return None）。"""
    st = FakeState()
    kicked = limit_down_quote(price=9.2, ask_vol=30000.0)      # 撬板
    slight = limit_down_quote(price=9.001, ask_vol=0.0)        # 刚打开
    rule = build({})
    assert rule.evaluate(snap_of(kicked), mk_ctx(st)) == []
    assert rule.evaluate(snap_of(slight), mk_ctx(st)) == []
    # 同样的历史条件下涨停侧会出炸板，说明不是"没数据"而是方向性差异
    up = limit_up_quote(price=10.5, bid_vol=0.0, high=11.0, low=10.2)
    assert rule.evaluate(snap_of(up), mk_ctx(st))[0].metrics["stage"] == 3.0


def test_limit_down_break_is_recognized():
    """跌停被撬开（打开跌停）必须告警 —— 这是利好，不能漏报。

    这是一条真实缺陷：``_check_side`` 在跌停侧直接 ``return None``，
    于是 ``open_limit_down``（打开跌停）与 ``limit_down_touch``（触及跌停）
    这两个已在 ``spirit.SIGNALS`` 注册表里、且带 hint 的信号
    **永远不可能出现**，而看板仍宣称「涨跌停 11 个信号」。
    用户排查"为什么从没见过『打开跌停』"会怀疑自己的配置或数据源。

    而 ``_make_touch`` / ``_seal_amount_wan`` 本来就已经支持 rising=False
    （会输出"触及跌停"文案、取卖一量），所以这更像是漏做而非有意不做。

    语义与涨停侧相反，必须区分清楚：
      涨停打开 = 封单被砸开 = 利空（DOWN）
      跌停打开 = 封单被撬开 = 有资金接 = 利好（LIMIT_DOWN kind 但方向为 up）
    """
    st = FakeState()
    st.feed("000001", [(T0 - 600.0, 9.0, 1000.0), (T0 - 300.0, 9.0, 2000.0),
                       (T0 - 60.0, 9.05, 3000.0)])
    q = limit_down_quote(code="000001", name="平安银行", price=9.2, ask_vol=0.0)
    rule = build({})

    ctx = mk_ctx(st)
    assert rule._touched_recently(q, ctx, T0, 9.0, False) is True
    assert rule._touch_count(q, ctx, T0, 9.0) == 1

    alerts = rule.evaluate(snap_of(q), ctx)
    assert alerts, "跌停被撬开必须告警（曾触及跌停价，现价已回升到 9.2）"
    a = alerts[0]
    assert a.kind is AlertKind.LIMIT_DOWN
    assert a.metrics["pattern"] == "open_limit_down"
    assert a.metrics["rising"] == 0.0
    assert a.metrics["stage"] == 3.0
    assert a.severity == 3
    # 回升幅度 = (9.2 / 9.0 - 1) * 100 = 2.22%
    assert a.metrics["retreat_pct"] == pytest.approx(2.222, abs=0.01)
    # 文案必须说"跌停价/回升"，不能沿用药涨停侧的"涨停价/回落"
    assert "跌停价" in a.detail, a.detail
    assert "回升" in a.detail, a.detail
    assert "涨停" not in a.detail, f"跌停打开的文案里不该出现「涨停」：{a.detail!r}"

    # 单独调用跌停侧（**新实例**，避免上面 evaluate 已把状态置为 broken）
    # 旧代码在这里显式 return None，所以这行是"跌停侧真的会判"的直接证据。
    assert build({})._check_side(q, ctx, datetime.fromtimestamp(T0), T0,
                                 up=11.0, down=9.0, rising=False) is not None


def test_limit_down_not_masked_by_break_for_the_same_stock():
    """天地板（先涨停后跌停）两个方向都要报，不能互相遮蔽。

    过去 ``evaluate`` 先判涨停侧，一旦返回告警就不再判跌停侧：
    当日最高价碰过涨停价时，现价即使躺在跌停价上也会先返回"炸板"，封跌停被吞掉。
    """
    q = limit_up_quote(price=9.0, bid_vol=0.0, ask_vol=30000.0, high=11.0, low=9.0,
                       open=10.0, amount=50000.0 * 100.0 * 9.5)
    st = FakeState()

    kinds = [a.kind for a in build({}).evaluate(snap_of(q), mk_ctx(st))]
    assert AlertKind.LIMIT_UP in kinds, "炸板（曾涨停后回落）必须报"
    assert AlertKind.LIMIT_DOWN in kinds, "封跌停必须报，不能被涨停侧遮蔽"

    by_kind = {a.kind: a for a in build({}).evaluate(snap_of(q), mk_ctx(st))}
    assert by_kind[AlertKind.LIMIT_UP].metrics["stage"] == 3.0      # 炸板
    assert by_kind[AlertKind.LIMIT_DOWN].metrics["stage"] == 2.0    # 封跌停
    assert by_kind[AlertKind.LIMIT_UP].key != by_kind[AlertKind.LIMIT_DOWN].key

    # 当日最高价没碰过涨停价时只有跌停侧
    plain = q.copy_with(high=10.5)
    assert [a.kind for a in build({}).evaluate(snap_of(plain), mk_ctx(st))] == [AlertKind.LIMIT_DOWN]


def test_detect_break_false_alone_disables_break():
    """单独设 detect_break=False 就能关掉炸板（不需要同时关 detect_touch）。"""
    st = FakeState()
    st.feed("600000", sealed_history("600000"))
    q = limit_up_quote(price=10.5, bid_vol=0.0, high=11.0, low=10.2)

    # 关掉炸板：回落行情降级为触板（仍在限价附近语义）
    only_break_off = build({"detect_break": False})
    stages = [a.metrics["stage"] for a in only_break_off.evaluate(snap_of(q), mk_ctx(st))]
    assert stages == [1.0], "detect_break=False 时不应再产出 stage=3 的炸板"

    # 两个开关都关才真正静默
    both_off = build({"detect_break": False, "detect_touch": False})
    assert both_off.evaluate(snap_of(q), mk_ctx(st)) == []

    # 关掉触板但保留炸板：回落幅度够大时仍报炸板
    only_touch_off = build({"detect_touch": False})
    assert [a.metrics["stage"] for a in only_touch_off.evaluate(snap_of(q), mk_ctx(st))] == [3.0]


def test_touch_count_respects_direction():
    """_touch_count 必须按方向比较：跌停侧看 ``price <= limit + tol``。

    过去只按涨停方向数（``price >= limit - tol``），跌停侧把跌停价当"下方限价"传进去，
    于是**任何高于跌停价的点都被计入**，数值毫无意义。
    """
    st = FakeState()
    st.feed("000001", [(T0 - 300.0 + i * 30.0, 10.5, 1000.0 + i * 100.0)
                       for i in range(11)])          # 全天 10.5，跌停价是 9.0

    ctx = mk_ctx(st)
    # 从未触及跌停价 -> 0
    assert build({})._touch_count(
        limit_down_quote(code="000001", name="平安银行"), ctx, T0, 9.0, rising=False) == 0

    # 确实到过跌停价 -> 1
    st.feed("000002", [(T0 - 300.0, 9.0, 1000.0), (T0 - 200.0, 9.2, 2000.0),
                       (T0 - 100.0, 9.0, 3000.0)])
    down = limit_down_quote(code="000002", name="平安银行", ask_vol=20000.0)
    assert build({})._touch_count(down, ctx, T0, 9.0, rising=False) == 2   # 两段触板

    # 涨停侧同方向语义：历史全在 10.0，从未到过 11.0 -> 0
    up = make_quote(code="000001", name="平安银行", price=11.0, prev_close=10.0,
                    open=10.0, high=11.0, low=10.0, volume_lots=50000.0,
                    amount=50000.0 * 100.0 * 11.0, bid_vol=30000.0)
    assert build({})._touch_count(up, ctx, T0, 11.0, rising=True) == 0


def test_limit_down_touch_count_in_alert_is_meaningful():
    """封跌停告警里的 touch_count 必须是"真的碰过跌停"的次数。"""
    st = FakeState()
    st.feed("000001", [(T0 - 300.0 + i * 30.0, 10.5, 1000.0 + i * 100.0)
                       for i in range(11)])          # 全天 10.5，远离跌停
    q = limit_down_quote(code="000001", name="平安银行", ask_vol=20000.0)

    a = build({}).evaluate(snap_of(q), mk_ctx(st))[0]
    assert a.kind is AlertKind.LIMIT_DOWN
    assert a.metrics["touch_count"] == 0.0, "全天没碰过跌停价，计数必须为 0"

    # 先到过跌停价再封回去 -> 计数 1
    st.feed("000003", [(T0 - 300.0, 9.0, 1000.0), (T0 - 120.0, 9.05, 2000.0)])
    q2 = limit_down_quote(code="000003", name="平安银行", ask_vol=20000.0)
    a2 = build({}).evaluate(snap_of(q2), mk_ctx(st))[0]
    assert a2.metrics["touch_count"] == 1.0


def test_touch_count_counts_episodes_not_points():
    """touch_count 统计"进入限价区"的段数，不重复计连续点。"""
    st = FakeState()
    prices = [10.5, 11.0, 11.0, 10.5, 11.0]                 # 两段触板
    st.feed("600000", [(T0 - 300.0 + i * 30.0, p, 1000.0 + i * 100.0)
                       for i, p in enumerate(prices)])
    q = limit_up_quote(bid_vol=30000.0, high=11.0, low=10.2, open=10.5)
    a = build({}).evaluate(snap_of(q), mk_ctx(st))[0]
    assert a.metrics["touch_count"] == 2.0


# ==========================================================================
# 一字板 / 首板
# ==========================================================================
def test_one_word_board_recognized():
    """开盘即涨停且全天 high == low -> 一字板。"""
    st = FakeState()
    q = limit_up_quote(bid_vol=500000.0, open=11.0, high=11.0, low=11.0)
    rule = build({})
    assert rule._is_one_word(q, 11.0, True) is True
    assert rule._is_first_board(q, 11.0, True) is False

    a = rule.evaluate(snap_of(q), mk_ctx(st))[0]
    assert a.metrics["one_word_board"] == 1.0
    assert a.metrics["is_first_board"] == 0.0
    assert "一字板" in a.detail
    assert a.metrics["seal_amount_wan"] == pytest.approx(55000.0)     # 5.50 亿
    assert "亿" in a.title


def test_one_word_requires_high_equals_low():
    """开盘在涨停价但盘中有波动（low < high）-> 不是一字板。"""
    st = FakeState()
    rule = build({})
    q = limit_up_quote(bid_vol=30000.0, open=11.0, high=11.0, low=10.98)
    assert rule._is_one_word(q, 11.0, True) is False
    a = rule.evaluate(snap_of(q), mk_ctx(st))[0]
    assert a.metrics["one_word_board"] == 0.0
    # 开盘在涨停价，因此也不是首板（首板 = 开盘未涨停）
    assert a.metrics["is_first_board"] == 0.0


def test_non_one_word_limit_day_is_first_board():
    """低开/平开后拉到涨停 -> 首板（非一字）。"""
    st = FakeState()
    rule = build({})
    q = limit_up_quote(bid_vol=30000.0, open=10.0, high=11.0, low=9.95)
    assert rule._is_one_word(q, 11.0, True) is False
    assert rule._is_first_board(q, 11.0, True) is True
    a = rule.evaluate(snap_of(q), mk_ctx(st))[0]
    assert a.metrics["one_word_board"] == 0.0
    assert a.metrics["is_first_board"] == 1.0
    assert "首板" in a.detail


def test_first_board_helper_semantics():
    """_is_first_board = 开盘未到涨停价；开盘价缺失(0) 视为首板；跌停侧恒为 False。"""
    rule = build({})
    at_limit = limit_up_quote(open=11.0, high=11.0, low=11.0)
    below = limit_up_quote(open=10.0, high=11.0, low=10.0)
    tol_edge = limit_up_quote(open=10.9995, high=11.0, low=10.9995)   # 差 < tol 也算开盘涨停
    no_open = limit_up_quote(open=0.0, high=11.0, low=11.0)

    assert rule._is_first_board(at_limit, 11.0, True) is False
    assert rule._is_first_board(below, 11.0, True) is True
    assert rule._is_first_board(tol_edge, 11.0, True) is False
    assert rule._is_first_board(no_open, 11.0, True) is True
    assert rule._is_first_board(below, 9.0, False) is False


def test_one_word_limit_down_is_recognized():
    """一字跌停必须被认出来，且**不能**被标成「非一字」。

    这是一条真实的用户可见缺陷：``_is_one_word`` 在跌停侧直接
    ``return False``，于是 ``_make_seal`` 必然把一字跌停印成「非一字」——
    而同一条 detail 的下一行就写着「开盘 9.00（-10.00%）最高 9.00 最低 9.00
    振幅 0.00%」，用户在两行之间读到自相矛盾的话。

    对做短线的人来说「一字跌停」（想卖卖不掉）与「盘中跌停」差别很大，
    不该因为"跌停侧懒得判"就把这个信息丢掉或说反。

    注意 ``_is_first_board``（首板）仍然恒为 False：那是涨停侧的概念，
    跌停侧没有对应物 —— 所以跌停侧非一字时**不输出标签**，
    而不是输出一个没检查过的「非一字」。
    """
    rule = build({})
    q = limit_down_quote(ask_vol=30000.0, open=9.0, high=9.0, low=9.0)
    assert rule._is_one_word(q, 9.0, False) is True
    assert rule._is_first_board(q, 9.0, False) is False
    a = rule.evaluate(snap_of(q), mk_ctx(FakeState()))[0]
    assert a.metrics["one_word_board"] == 1.0
    assert a.metrics["is_first_board"] == 0.0
    assert "一字跌停" in a.detail, a.detail
    assert "非一字" not in a.detail, (
        f"一字跌停被标成「非一字」：{a.detail!r}")


def test_non_one_word_limit_down_does_not_claim_non_one_word():
    """半路封跌停：不输出「非一字」这种没检查过的断言。

    「不识别」和「识别了并给出肯定结论」是两件事。跌停侧不做首板判定，
    所以既不该显示「首板」，也不该显示「非一字」—— 后者等于宣称
    "我检查过，它不是一字板"，而实际根本没检查。
    """
    rule = build({})
    # 开在 9.80，盘中砸到跌停 9.00 —— 明显不是一字板
    q = limit_down_quote(ask_vol=30000.0, open=9.8, high=9.8, low=9.0)
    assert rule._is_one_word(q, 9.0, False) is False
    a = rule.evaluate(snap_of(q), mk_ctx(FakeState()))[0]
    assert a.metrics["one_word_board"] == 0.0
    assert "非一字" not in a.detail, a.detail
    assert "一字跌停" not in a.detail, a.detail
    # 封单等信息不能因为去掉标签而丢
    assert "封单" in a.detail
    assert "换手" in a.detail
    # 空标签被过滤掉后，不该留下"多余的一段空白"（字段间正常是 2 空格，
    # 4 个及以上说明有个字段是空的却仍占位）
    assert "    " not in a.detail.splitlines()[1], (
        f"去掉空标签后留下了多余空白：{a.detail.splitlines()[1]!r}")


# ==========================================================================
# 方向与板块涨跌停比例
# ==========================================================================
def test_both_directions_in_one_round():
    """同一轮里一只涨停、一只跌停，两条告警都要出且互不干扰。"""
    st = FakeState()
    up = limit_up_quote(code="600000")
    down = limit_down_quote(code="000001", name="平安银行")
    alerts = build({}).evaluate(snap_of(up, down), mk_ctx(st))

    assert len(alerts) == 2
    by_code = {a.code: a for a in alerts}
    assert by_code["600000"].kind is AlertKind.LIMIT_UP
    assert by_code["600000"].pct == pytest.approx(10.0)
    assert by_code["000001"].kind is AlertKind.LIMIT_DOWN
    assert by_code["000001"].pct == pytest.approx(-10.0)
    assert by_code["000001"].metrics["limit_down"] == pytest.approx(9.0)


def test_limit_down_can_be_disabled():
    """detect_limit_down=False 时只保留涨停侧。"""
    st = FakeState()
    up = limit_up_quote(code="600000")
    down = limit_down_quote(code="000001", name="平安银行")
    alerts = build({"detect_limit_down": False}).evaluate(snap_of(up, down), mk_ctx(st))
    assert [a.kind for a in alerts] == [AlertKind.LIMIT_UP]


@pytest.mark.parametrize("code,name,rate", [
    ("600519", "贵州茅台", 0.10),      # 沪主板 ±10%
    ("000001", "平安银行", 0.10),      # 深主板 ±10%
    ("300750", "宁德时代", 0.20),      # 创业板 ±20%
    ("301001", "凯淳股份", 0.20),      # 创业板注册制 ±20%
    ("688981", "中芯国际", 0.20),      # 科创板 ±20%
    ("830799", "艾融软件", 0.30),      # 北交所 ±30%
    ("600000", "ST中安", 0.05),        # ST 主板 ±5%
])
def test_board_limit_rate_drives_the_limit_price(code, name, rate):
    """涨停价一律按 limit_rate_of 推算（不写死 1.1），差一分钱就不算封板。"""
    st = FakeState()
    prev = 10.0
    limit = round(prev * (1.0 + rate), 2)
    assert limit_rate_of(code, name) == pytest.approx(rate)

    q = limit_up_quote(code=code, name=name, prev_close=prev)
    alerts = build({}).evaluate(snap_of(q), mk_ctx(st))
    assert len(alerts) == 1, (code, name, limit)
    assert alerts[0].kind is AlertKind.LIMIT_UP
    assert alerts[0].metrics["limit_up"] == pytest.approx(limit)
    assert alerts[0].metrics["limit_down"] == pytest.approx(round(prev * (1.0 - rate), 2))
    assert alerts[0].metrics["stage"] == 2.0

    # 少一分钱：封板不成立（若仍出告警只能是触板）
    lower = limit_up_quote(code=code, name=name, prev_close=prev,
                           price=round(limit - 0.01, 2), bid_vol=30000.0, high=limit)
    stages = [a.metrics["stage"] for a in build({}).evaluate(snap_of(lower), mk_ctx(st))]
    assert 2.0 not in stages, (code, name, stages)


def test_gem_ten_percent_move_is_not_a_limit():
    """创业板 +10% 只是半路，不是涨停：不应有任何告警。"""
    st = FakeState()
    q = make_quote(code="300750", name="宁德时代", price=11.0, prev_close=10.0,
                   open=10.2, high=11.0, low=10.1, volume_lots=50000.0,
                   amount=50000.0 * 100.0 * 10.6, bid_vol=30000.0)
    assert build({}).evaluate(snap_of(q), mk_ctx(st)) == []
    # 同样价格放主板就是涨停 -> 证明差异来自板块比例而非数据
    q_main = limit_up_quote(code="600519", name="贵州茅台", price=11.0, bid_vol=30000.0)
    assert build({}).evaluate(snap_of(q_main), mk_ctx(st))[0].metrics["stage"] == 2.0


# ==========================================================================
# 幂等键 / 冷却
# ==========================================================================
def test_same_bucket_same_key_and_repeat_evaluation_is_idempotent():
    """同一状态重复求值 -> 不再产出告警（状态未跃迁）。"""
    st = FakeState()
    q = limit_up_quote()
    rule = build({"cooldown_seconds": 300})

    a1 = rule.evaluate(snap_of(q), mk_ctx(st, now_ep=T0))[0]
    assert a1.key == f"600000:{AlertKind.LIMIT_UP.value}:seal:{bucket_of(T0, 300)}"

    # 连续封板：状态一直是 sealed，不再重报
    assert rule.evaluate(snap_of(q), mk_ctx(st, now_ep=T0 + 10)) == []

    # 模拟 AlertBus：同键在冷却窗口内只放行一次
    seen: set[str] = set()
    accepted = [a for a in (a1,) if not (a.key in seen or seen.add(a.key))]
    assert accepted == [a1]


def test_reseal_after_break_reports_again():
    """炸板后重新封板 -> 状态跃迁，再报一次（key 换到新桶）。"""
    st = FakeState()
    sealed = limit_up_quote()
    broke = limit_up_quote(price=10.5, bid_vol=0.0, high=11.0, low=10.2)
    rule = build({"cooldown_seconds": 300})

    k1 = rule.evaluate(snap_of(sealed), mk_ctx(st, now_ep=T0))[0].key
    assert k1 == f"600000:limit_up:seal:{bucket_of(T0, 300)}"
    assert [a.metrics["stage"] for a in rule.evaluate(snap_of(broke), mk_ctx(st, now_ep=T0 + 60))] == [3.0]

    again = rule.evaluate(snap_of(sealed), mk_ctx(st, now_ep=T0 + 301))
    assert len(again) == 1, "重新封板是新事件"
    assert again[0].key == f"600000:limit_up:seal:{bucket_of(T0 + 301, 300)}"
    assert again[0].key != k1


def test_break_uses_its_own_bucket_key():
    """炸板 key 带 break 段并使用 break_bucket_seconds 分桶。"""
    st = FakeState()
    st.feed("600000", sealed_history("600000"))
    q = limit_up_quote(price=10.5, bid_vol=0.0, high=11.0, low=10.2)
    a = build({"cooldown_seconds": 300, "break_bucket_seconds": 60}).evaluate(
        snap_of(q), mk_ctx(st, now_ep=T0))[0]
    assert a.key == f"600000:limit_up:break:{bucket_of(T0, 60)}"
    assert bucket_of(T0, 60) != bucket_of(T0, 300)


def test_seal_and_break_keys_are_distinguishable():
    """同一只股票：封板键与炸板键不同（炸板多了 break 段），两者不会互相吞掉。"""
    st = FakeState()
    rule = build({})
    seal = rule.evaluate(snap_of(limit_up_quote()), mk_ctx(st))[0]
    brk = rule.evaluate(
        snap_of(limit_up_quote(price=10.5, bid_vol=0.0, high=11.0, low=10.2)),
        mk_ctx(st))[0]
    assert seal.key != brk.key
    assert brk.key == f"600000:limit_up:break:{bucket_of(T0, 60)}"


def test_touch_then_seal_in_same_bucket_does_not_collide():
    """触板后同桶内封板：两条告警 key 必须不同。

    真实回归 bug：``_make_touch`` 与 ``_make_seal`` 都拼
    ``f"{code}:{kind}:{bucket}"``，同一 300 秒桶内先触板再封板时 key 完全相同，
    ``AlertBus`` 会把后到的 **severity 3 封板** 当成重复丢掉 —— 最紧急的信号丢失。
    """
    st = FakeState()
    rule = build({"cooldown_seconds": 300, "touch_retreat_pct": 0.3,
                  "min_seal_amount_wan": 0})

    # 1) 触板：触及涨停后小幅回落
    touch = limit_up_quote(price=10.99, high=11.0, bid_vol=0.0)
    ta = rule.evaluate(snap_of(touch), mk_ctx(st, now_ep=T0))
    assert ta, "应报触板"
    assert ta[0].metrics["stage"] == 1.0
    assert ta[0].severity == 2          # 默认 severity，低于封板

    # 2) 同一桶内（+20s，bucket 不变）真的封住涨停
    seal = limit_up_quote()
    sa = rule.evaluate(snap_of(seal), mk_ctx(st, now_ep=T0 + 20))
    assert sa, "封板必须报出来"
    assert sa[0].severity == 3
    assert sa[0].key != ta[0].key, "触板与封板 key 不能相同，否则封板会被去重吞掉"

    # 3) 模拟 AlertBus 的去重：两条都应通过
    seen: set[str] = set()
    passed = [a for a in (ta[0], sa[0]) if not (a.key in seen or seen.add(a.key))]
    assert len(passed) == 2, "触板和封板都必须送达"


def test_limit_down_reseal_after_reopen_is_not_deduped():
    """封跌停 -> 撬开 -> 再封跌停：第二次必须能报出来。

    这正是跌停侧状态复位存在的意义；若 key 与第一次相同，
    去重会把它吃掉，复位就白做了。
    """
    st = FakeState()
    rule = build({"cooldown_seconds": 300, "min_seal_amount_wan": 0})
    down = limit_down_quote()

    first = rule.evaluate(snap_of(down), mk_ctx(st, now_ep=T0))
    assert first and first[0].severity == 3

    # 撬开（价格离开跌停价）
    opened = limit_down_quote(price=9.35, ask_vol=0.0, low=9.2)
    rule.evaluate(snap_of(opened), mk_ctx(st, now_ep=T0 + 60))

    # 再封回：必须再报一次，且 key 不能与第一次冲突
    again = rule.evaluate(snap_of(down), mk_ctx(st, now_ep=T0 + 400))
    assert again, "重新封跌停必须再报"
    assert again[0].key != first[0].key

    seen: set[str] = set()
    passed = [a for a in (first[0], again[0]) if not (a.key in seen or seen.add(a.key))]
    assert len(passed) == 2


# ==========================================================================
# 边界
# ==========================================================================
def test_suspended_and_invalid_quotes_skipped():
    """停牌口径（price<=0 / prev_close<=0 / volume_lots<=0）一律跳过。"""
    st = FakeState()
    rule = build({})
    assert rule.evaluate(snap_of(make_quote(price=0.0, prev_close=0.0, volume_lots=0)), mk_ctx(st)) == []
    assert rule.evaluate(snap_of(make_quote(price=11.0, prev_close=0.0)), mk_ctx(st)) == []
    # 全天 0 成交（封单再大也是停牌/无效行情）
    zero_vol = limit_up_quote(bid_vol=99999.0)
    zero_vol = zero_vol.copy_with(volume_lots=0.0)
    assert rule.evaluate(snap_of(zero_vol), mk_ctx(st)) == []


def test_seal_amount_ignores_opposite_book_side():
    """封单额只看同侧挂单：涨停看买一量，跌停看卖一量，另一侧再大也不参与。"""
    st = FakeState()
    up = limit_up_quote(bid_vol=30000.0)
    up = up.copy_with(ask_vol=999999.0)            # 卖单再大也不影响涨停封单额
    down = limit_down_quote(ask_vol=30000.0)
    down = down.copy_with(bid_vol=999999.0)
    rule = build({})

    assert rule._seal_amount_wan(up, True) == pytest.approx(3300.0)
    assert rule._seal_amount_wan(down, False) == pytest.approx(2700.0)
    assert rule.evaluate(snap_of(up), mk_ctx(st))[0].metrics["seal_amount_wan"] == pytest.approx(3300.0)
    assert rule.evaluate(snap_of(down), mk_ctx(st))[0].metrics["seal_amount_wan"] == pytest.approx(2700.0)


def test_missing_limit_price_is_self_computed_from_board_rate():
    """数据源给 -1/0（停牌股常见）-> 按 prev_close × 板率自行推算涨停价。"""
    st = FakeState()
    for bad in (0.0, -1.0):
        q = limit_up_quote(bid_vol=30000.0)
        q = q.copy_with(limit_up=bad, limit_down=bad)
        alerts = build({}).evaluate(snap_of(q), mk_ctx(st))   # 每次用新实例
        assert len(alerts) == 1, bad
        assert alerts[0].metrics["limit_up"] == pytest.approx(11.0)
        assert alerts[0].metrics["limit_down"] == pytest.approx(9.0)


def test_unparsable_limit_price_skips_quote_without_raising():
    """limit_up/limit_down 是非数值串时按 0 处理并跳过，不抛异常。"""
    st = FakeState()
    q = limit_up_quote(bid_vol=30000.0)
    broken = q.copy_with(limit_up="abc", limit_down="xyz")     # type: ignore[arg-type]
    assert build({}).evaluate(snap_of(broken), mk_ctx(st)) == []
    assert build({})._limit_up_price(broken) == 0.0
    assert build({})._limit_down_price(broken) == 0.0


def test_stock_never_touching_limit_produces_no_alert():
    """全天没碰过涨停价 -> 无告警（即使涨幅不小）。"""
    st = FakeState()
    st.feed("600000", [(T0 - 300.0 + i * 30.0, 10.0 + i * 0.05, 1000.0 + i * 100.0)
                       for i in range(11)])
    q = make_quote(code="600000", price=10.5, prev_close=10.0, open=10.0,
                   high=10.6, low=9.9, volume_lots=50000.0, amount=50000.0 * 100.0 * 10.3)
    assert build({}).evaluate(snap_of(q), mk_ctx(st)) == []


def test_insufficient_history_does_not_break_break_detection():
    """历史不足（甚至状态对象没有 window）时，回退到快照极值，不抛异常。"""
    q = limit_up_quote(price=10.5, bid_vol=0.0, high=11.0, low=10.2)
    alerts = build({}).evaluate(snap_of(q), mk_ctx(NoWindowState()))
    assert len(alerts) == 1
    assert alerts[0].metrics["stage"] == 3.0
    assert alerts[0].metrics["touch_count"] == 0.0
    # 没有任何可用信息的普通股 -> 空
    plain = make_quote(code="600000", price=10.2, prev_close=10.0, high=10.3, low=10.0)
    assert build({}).evaluate(snap_of(plain), mk_ctx(NoWindowState())) == []


def test_empty_snapshot_returns_empty():
    st = FakeState()
    empty = Snapshot(ts=datetime.fromtimestamp(T0), seq=1, quotes={})
    assert build({}).evaluate(empty, mk_ctx(st)) == []


def test_outside_continuous_session_is_silent_unless_disabled():
    """默认只在连续竞价时段告警；only_continuous=False 时可放开。"""
    st = FakeState()
    q = limit_up_quote()
    for phase in (SessionPhase.CLOSED, SessionPhase.LUNCH,
                  SessionPhase.PRE_OPEN, SessionPhase.POST):
        assert build({}).evaluate(snap_of(q), mk_ctx(st, session=phase)) == [], phase
    assert build({}).evaluate(snap_of(q), mk_ctx(st, session=SessionPhase.AFTERNOON)) != []
    loose = build({"only_continuous": False})
    assert len(loose.evaluate(snap_of(q), mk_ctx(st, session=SessionPhase.LUNCH))) == 1


def test_one_alert_per_code_per_round():
    """同一方向每轮最多一条告警；天地板会有两条（方向不同，是既有设计）。

    注意**不是**"每只股票只能有一条"：涨停侧与跌停侧是独立事件，
    见 ``test_limit_down_not_masked_by_break_for_the_same_stock``
    （现价躺在跌停价上、当日最高碰过涨停价 -> 官方要求炸板与封跌停都报）。

    这条用例的行情是"现价在涨停价、当日最低碰过跌停价" —— 跌停侧此时**正在**
    跌停价上（at_limit），会正常报封跌停，所以两方向各一条是对的。
    """
    st = FakeState()
    q = limit_up_quote(bid_vol=30000.0, low=9.0)     # low 也碰到了跌停价
    alerts = build({}).evaluate(snap_of(q), mk_ctx(st))
    # 两个方向各一条，且方向不重复
    kinds = [a.kind for a in alerts]
    assert kinds.count(AlertKind.LIMIT_UP) == 1
    assert len(kinds) == len(set(kinds)) == len(alerts), f"同方向重复：{kinds}"
    # 同一方向的 key 不会重复
    assert len({a.key for a in alerts}) == len(alerts)


def test_result_sorted_by_severity_then_abs_pct():
    """排序：severity 降序，其次 |pct| 降序。"""
    st = FakeState()
    touch = limit_up_quote(code="600000", price=10.97, bid_vol=0.0, high=11.0, low=10.0)
    seal_up = limit_up_quote(code="600001")
    seal_down = limit_down_quote(code="300750", name="宁德时代")     # -20%，|pct| 最大
    alerts = build({}).evaluate(snap_of(touch, seal_up, seal_down), mk_ctx(st))

    assert [a.code for a in alerts] == ["300750", "600001", "600000"]
    assert [a.severity for a in alerts] == [3, 3, 2]
    assert [round(abs(a.pct), 2) for a in alerts] == [20.0, 10.0, 9.7]


def test_metrics_keys_exact_per_stage():
    """_check_side 三个状态各自产出的 metrics 键名（严格对齐源码）。"""
    st = FakeState()
    rule = build({})

    seal = rule.evaluate(snap_of(limit_up_quote()), mk_ctx(st))[0]
    assert set(seal.metrics) == SEAL_METRIC_KEYS

    touch = rule.evaluate(
        snap_of(limit_up_quote(price=10.97, bid_vol=0.0, high=11.0, low=10.0)),
        mk_ctx(st))[0]
    assert set(touch.metrics) == SEAL_METRIC_KEYS
    assert touch.metrics["stage"] == 1.0

    brk = rule.evaluate(
        snap_of(limit_up_quote(price=10.5, bid_vol=0.0, high=11.0, low=10.2)),
        mk_ctx(st))[0]
    assert set(brk.metrics) == BREAK_METRIC_KEYS
    assert isinstance(brk.metrics["retreat_pct"], float)


def test_break_does_not_claim_a_seal_order_exists():
    """炸板告警不能说板上还有"封单"—— 板都开了。

    这是一条真实的用户可见缺陷：``_check_break`` 复用了 ``_seal_amount_wan``，
    于是文案写「买一封单 126万」、``metrics["seal_amount_wan"]`` 也是同一个数。
    但触发炸板的前提就是现价已远离涨停价（默认回落 ≥0.3%），此时板上
    **不存在封单**，买一那点量只是普通挂单。而 ``seal_amount_wan`` 在
    ``spirit.to_feed_item`` 的 extra 白名单里，看板悬停会照原样展示 ——
    等于明确告诉用户"这是封单"，是指鹿为马。

    注意数字本身算得没错（126 万确实等于买一量 × 价），错的是**标签**；
    所以这里断言的是语义，不是数值。
    """
    st = FakeState()
    brk = build({}).evaluate(
        snap_of(limit_up_quote(price=10.5, bid_vol=1200.0, high=11.0, low=10.2)),
        mk_ctx(st))[0]
    assert brk.metrics["stage"] == 3.0

    # metrics 侧：不能再出现冒名的 seal_amount_wan
    assert "seal_amount_wan" not in brk.metrics, (
        "炸板路径不该产出 seal_amount_wan —— 板已开，没有封单")
    assert "bid1_amount_wan" in brk.metrics, "应改用语义正确的买一挂单额"
    # 数值仍要对：(1200 手 × 100 股 × 10.50) / 1e4 = 126.0 万
    assert brk.metrics["bid1_amount_wan"] == pytest.approx(126.0, abs=0.1)

    # 文案侧：说"挂单"，不说"封单"
    assert "买一挂单" in brk.detail, brk.detail
    assert "封单" not in brk.detail, (
        f"炸板文案里出现了「封单」：{brk.detail!r}")

    # 对照组：真封板时「封单」措辞是**对的**，不能被一起改坏
    seal = build({}).evaluate(snap_of(limit_up_quote()), mk_ctx(st))[0]
    assert seal.metrics["stage"] == 2.0
    assert "seal_amount_wan" in seal.metrics
    assert "封单" in seal.detail
    assert "挂单" not in seal.detail


def test_max_per_round_limits_output():
    """max_per_round 生效：一轮最多推 N 条，按 severity、|pct| 降序取前 N。"""
    assert DEFAULT_CFG["max_per_round"] == 20

    st = FakeState()
    codes = [f"60000{i}" for i in range(5)]
    quotes = [limit_up_quote(code=c) for c in codes]
    snap = snap_of(*quotes)

    assert len(build({"max_per_round": 2}).evaluate(snap, mk_ctx(st))) == 2
    assert len(build({"max_per_round": 3}).evaluate(snap, mk_ctx(st))) == 3
    assert len(build({"max_per_round": 99}).evaluate(snap, mk_ctx(st))) == 5
    # <=0 表示不限量
    assert len(build({"max_per_round": 0}).evaluate(snap, mk_ctx(st))) == 5
    assert len(build({"max_per_round": -1}).evaluate(snap, mk_ctx(st))) == 5


def test_max_per_round_keeps_most_severe():
    """截断时保留的是最严重/幅度最大的，而不是字典序靠前的。"""
    st = FakeState()
    mild = limit_up_quote(code="600001", prev_close=10.0)          # +10%
    severe = limit_down_quote(code="600002", prev_close=10.0)      # -10%
    snap = snap_of(mild, severe)
    picked = build({"max_per_round": 1}).evaluate(snap, mk_ctx(st))
    assert len(picked) == 1
    # 两者 severity 都是 3（封板/封跌停）-> 按 |pct| 打破平局，都是 10%，取任意一个即可
    assert abs(picked[0].pct) == pytest.approx(10.0, abs=0.01)


def test_default_config_matches_settings_yaml_contract():
    """默认配置键与 config/settings.yaml 的 limit_board 节一致。"""
    for key in ("enabled", "detect_touch", "detect_seal", "detect_break",
                "detect_limit_down", "touch_tolerance", "min_seal_amount_wan",
                "cooldown_seconds", "only_continuous", "severity"):
        assert key in DEFAULT_CFG, key
    rule = build({})
    assert (rule.tol, rule.min_seal_wan, rule.cooldown, rule.severity) == \
        (0.001, 200.0, 300.0, 2)
    assert rule.lookback == 1800.0
    assert rule.break_retreat == 0.3
    assert rule.break_bucket == 60.0
    assert isinstance(build(None), LimitBoardRule)


# ==========================================================================
# 性能
# ==========================================================================
def test_performance_1000_stocks():
    """1000 只（封板/炸板/触板/跌停/普通混合）单次 evaluate 应远快于 200ms。"""
    import time

    st = FakeState()
    quotes: dict[str, Quote] = {}
    for i in range(1000):
        code = f"{600000 + i}"
        kind = i % 5
        if kind == 0:            # 封涨停
            price, bid, ask = 11.0, 30000.0, 0.0
        elif kind == 1:          # 炸板
            price, bid, ask = 10.5, 0.0, 0.0
        elif kind == 2:          # 触板
            price, bid, ask = 10.97, 0.0, 0.0
        elif kind == 3:          # 封跌停
            price, bid, ask = 9.0, 0.0, 30000.0
        else:                    # 普通
            price, bid, ask = 10.3, 0.0, 0.0
        st.feed(code, [(T0 - 1800.0 + j * 30.0, price if j >= 55 else 10.2,
                        1000.0 + j * 100.0) for j in range(61)])
        quotes[code] = make_quote(
            code=code, price=price, prev_close=10.0, open=10.2,
            high=11.0 if kind in (0, 1, 2) else max(price, 10.2),
            low=9.0 if kind == 3 else 10.0,
            volume_lots=50000.0, amount=50000.0 * 100.0 * 10.5, turnover=4.0,
            bid_vol=bid, ask_vol=ask)

    snap = Snapshot(ts=datetime.fromtimestamp(T0), seq=1, quotes=quotes)
    ctx = mk_ctx(st)
    build({"max_per_round": 0}).evaluate(snap, ctx)   # 预热（去掉首次分配开销）

    rule = build({"max_per_round": 0})     # 全新实例：炸板幂等状态为空，全量求值
    t0 = time.perf_counter()
    alerts = rule.evaluate(snap, ctx)
    elapsed = time.perf_counter() - t0
    assert len(alerts) == 800
    assert elapsed < 0.2, f"evaluate 太慢: {elapsed * 1000:.1f}ms"


# ==========================================================================
# IT-P1-LIMIT-001：封单资格状态与价格状态必须分开
#
# 缺陷：``_check_side`` 在 ``at_limit`` 分支**无条件先写** ``_state = "sealed"``，
# 而封单门槛（``min_seal_amount_wan``）是在 ``_make_seal`` 里才检查的。
# 于是"价格贴板、封单只有 100 万（门槛 200 万）"的那一轮也留下了 "已封板"
# 记忆，下一轮封单涨到 300 万时命中 ``prev == "sealed"`` 直接 ``return None``
# —— **首次达标被永久吞掉，永远不发告警**（实测两轮均为 [None, None]，
# 应为 [None, Alert]）。
#
# 修复把状态拆成 ``at_limit_unqualified``（价格到板但封单不足）与
# ``sealed``（封单已达标），门槛判据收敛到 ``_seal_qualified`` 单一来源。
# 对外契约（kind / severity / key / metrics 键名）一律未变。
#
# 标记说明（三类，已按**实测**的回退验牙结果如实标注，不臆断）：
#   * 【验牙】修复前必红，且红在**告警丢失**这个核心行为上；
#   * 【回归保护】修复前后都绿，用于挡住未来的语义回退；
#   * 【结构性】修复前红，但红在状态名 / 新 helper 上（旧代码没有这些符号），
#     不是"告警被吞"的直接证据 —— 不冒充验牙用例。
# ==========================================================================
def vol_for_seal_wan(wan: float, price: float) -> float:
    """反解封单额（万元）-> 买一/卖一手数。

    ``seal_wan = vol * 100 * price / 1e4``，故 ``vol = wan * 1e4 / (100 * price)``。
    乘 1.000001 是为了避开浮点落在门槛**下方**一丝点的假失败
    （例如 909.0909... 手 × 100 × 11 / 1e4 = 99.99999... 万 < 100 万）。
    """
    return wan * 1e4 / (100.0 * price) * 1.000001


def test_seal_first_qualified_after_amount_crosses_threshold():
    """封单 100 万 -> 300 万（门槛 200 万）：第二轮首次达标**必须**发告警。

    IT-P1-LIMIT-001 的核心验收。价格全程贴在涨停价上，只有封单量在变 ——
    这正是一个状态机用例，必须用**同一个 Rule 实例**跨轮求值（每轮新建实例
    会把状态清零，永远测不出这个缺陷，既有用例就是因此漏掉的）。

    验牙：修复前 ``[None, None]``，本用例必红。
    """
    st = FakeState()
    weak = limit_up_quote(bid_vol=vol_for_seal_wan(100.0, 11.0))    # 100 万 < 200 万
    ok = limit_up_quote(bid_vol=vol_for_seal_wan(300.0, 11.0))      # 300 万 >= 200 万
    rule = build({"min_seal_amount_wan": 200})

    # 前提：两轮价格都在涨停价上，只有封单额不同
    assert weak.price == ok.price == pytest.approx(11.0)
    assert rule._seal_amount_wan(weak, True) == pytest.approx(100.0, abs=0.1)
    assert rule._seal_amount_wan(ok, True) == pytest.approx(300.0, abs=0.1)

    first = rule.evaluate(snap_of(weak), mk_ctx(st, now_ep=T0))
    assert first == [], "封单未达门槛时不该报封板"

    second = rule.evaluate(snap_of(ok), mk_ctx(st, now_ep=T0 + 60))
    assert len(second) == 1, (
        "封单首次跨过门槛必须发告警；被吞掉说明写 sealed 的时机仍在门槛检查之前")
    a = second[0]
    # 对外契约不得变化
    assert a.kind is AlertKind.LIMIT_UP
    assert a.severity == 3
    assert a.metrics["stage"] == 2.0
    assert a.metrics["pattern"] == "limit_up_seal"
    assert set(a.metrics) == SEAL_METRIC_KEYS
    assert a.metrics["seal_amount_wan"] == pytest.approx(300.0, abs=0.1)
    assert a.code == "600000"
    assert a.title.startswith("封涨停")
    assert a.key == f"600000:limit_up:seal:{bucket_of(T0 + 60, 300)}"

    # 状态此时才真正是 sealed（第一轮只是 at_limit_unqualified）
    assert rule._state["600000:up"] == "sealed"


def test_seal_first_qualified_limit_down_mirror():
    """跌停侧镜像：卖一封单 100 万 -> 300 万，首次达标必须发封跌停。

    验牙：修复前 ``[None, None]``，本用例必红。
    """
    st = FakeState()
    weak = limit_down_quote(code="000001", name="平安银行",
                            ask_vol=vol_for_seal_wan(100.0, 9.0))
    ok = limit_down_quote(code="000001", name="平安银行",
                          ask_vol=vol_for_seal_wan(300.0, 9.0))
    rule = build({"min_seal_amount_wan": 200})

    assert rule._seal_amount_wan(weak, False) == pytest.approx(100.0, abs=0.1)
    assert rule._seal_amount_wan(ok, False) == pytest.approx(300.0, abs=0.1)

    assert rule.evaluate(snap_of(weak), mk_ctx(st, now_ep=T0)) == []
    second = rule.evaluate(snap_of(ok), mk_ctx(st, now_ep=T0 + 60))
    assert len(second) == 1, "跌停侧封单首次达标也必须发告警（涨跌停镜像）"
    a = second[0]
    assert a.kind is AlertKind.LIMIT_DOWN
    assert a.severity == 3
    assert a.metrics["stage"] == 2.0
    assert a.metrics["pattern"] == "limit_down_seal"
    assert a.metrics["rising"] == 0.0
    assert a.title.startswith("封跌停")


def test_seal_cross_then_continuous_rounds_do_not_repeat():
    """跨过门槛那一轮报一次，之后持续达标不重复（幂等去重语义必须保留）。

    验牙：修复前 ``[None, None, None, None]``，本用例必红（第 2 轮就该有告警）。
    """
    st = FakeState()
    weak = limit_up_quote(bid_vol=vol_for_seal_wan(100.0, 11.0))
    ok = limit_up_quote(bid_vol=vol_for_seal_wan(300.0, 11.0))
    rule = build({"min_seal_amount_wan": 200, "cooldown_seconds": 300})

    assert rule.evaluate(snap_of(weak), mk_ctx(st, now_ep=T0)) == []
    assert len(rule.evaluate(snap_of(ok), mk_ctx(st, now_ep=T0 + 60))) == 1
    for dt in (0, 5, 60, 299, 300, 301, 3600, 7200):
        assert rule.evaluate(snap_of(ok), mk_ctx(st, now_ep=T0 + 60 + dt)) == [], \
            f"+{dt}s 重报封板：去重语义被破坏"
    assert rule._state["600000:up"] == "sealed"


def test_seal_starts_qualified_is_reported_once():
    """第一轮封单就已达标 -> 立即报，之后连续轮次不重报。

    回归保护：修复前后都绿。它钉住的是"幂等去重"这一半语义，
    防止修 IT-P1-LIMIT-001 时把去重一起删掉（每次求值都重报）。
    """
    st = FakeState()
    ok = limit_up_quote(bid_vol=vol_for_seal_wan(300.0, 11.0))
    rule = build({"min_seal_amount_wan": 200})
    assert len(rule.evaluate(snap_of(ok), mk_ctx(st, now_ep=T0))) == 1
    for dt in (5, 60, 299, 300, 301, 1800):
        assert rule.evaluate(snap_of(ok), mk_ctx(st, now_ep=T0 + dt)) == [], f"+{dt}s"


def test_seal_refires_after_amount_falls_back_below_threshold():
    """封单回落到门槛之下、再达标 -> 重新发告警（状态必须能复位）。

    这是 IT-P1-LIMIT-001 修复引入的关键语义：``sealed`` 必须能退回
    ``at_limit_unqualified``，否则"封单被抽走又补回来"这第二次有效封板
    会像首次达标一样被吞掉。价格全程不动，只有封单量变化。

    验牙：修复前 ``[None, None, None, None, None]``（全程被吞），本用例必红。
    """
    st = FakeState()
    weak = limit_up_quote(bid_vol=vol_for_seal_wan(100.0, 11.0))
    ok = limit_up_quote(bid_vol=vol_for_seal_wan(300.0, 11.0))
    rule = build({"min_seal_amount_wan": 200, "cooldown_seconds": 300})

    assert rule.evaluate(snap_of(weak), mk_ctx(st, now_ep=T0)) == []
    assert len(rule.evaluate(snap_of(ok), mk_ctx(st, now_ep=T0 + 60))) == 1

    # 封单被抽走（100 万 < 200 万）：不告警，但状态必须离开 sealed
    assert rule.evaluate(snap_of(weak), mk_ctx(st, now_ep=T0 + 120)) == []
    assert rule._state["600000:up"] == "at_limit_unqualified", \
        "封单跌破门槛后状态必须复位，否则再次达标永远不会被报出来"

    # 封单补回 -> 新事件，必须再报
    again = rule.evaluate(snap_of(ok), mk_ctx(st, now_ep=T0 + 400))
    assert len(again) == 1, "封单回落门槛之下后再达标必须重新发告警"
    assert again[0].metrics["stage"] == 2.0
    assert again[0].key != f"600000:limit_up:seal:{bucket_of(T0 + 60, 300)}"


def test_seal_refires_after_price_leaves_and_returns_qualified():
    """价格离开限价区后再回来且封单达标 -> 再报（原有"回封可重报"语义不变）。

    回归保护：修复前**也绿**。因为离开限价那一轮会把状态复位成 ``away``，
    "回封可重报"这条既有语义本来就没坏 —— 本用例的价值是挡住修复时的回退。
    """
    st = FakeState()                       # 无历史，且 high < 涨停价 -> 状态复位为 away
    weak = limit_up_quote(bid_vol=vol_for_seal_wan(100.0, 11.0))
    away = limit_up_quote(price=10.5, bid_vol=0.0, high=10.6, low=10.2)
    ok = limit_up_quote(bid_vol=vol_for_seal_wan(300.0, 11.0))
    rule = build({"min_seal_amount_wan": 200})

    assert rule.evaluate(snap_of(weak), mk_ctx(st, now_ep=T0)) == []
    # 离开限价：既没触板也没炸板（历史与快照 high 都没碰过涨停价）-> 静默复位
    assert rule.evaluate(snap_of(away), mk_ctx(st, now_ep=T0 + 60)) == []
    assert rule._state["600000:up"] == "away"

    again = rule.evaluate(snap_of(ok), mk_ctx(st, now_ep=T0 + 120))
    assert len(again) == 1, "重新封回且封单达标必须再报一次"
    assert again[0].metrics["stage"] == 2.0


def test_seal_never_reports_while_amount_stays_unqualified():
    """封单始终不达标 -> 一直不发告警（回归保护 + 结构性）。

    告警侧断言修复前后都绿（防止把门槛判断挪位置时顺手放宽成"贴板即封板"）；
    末尾的状态断言在修复前红，但红在**状态名**（旧代码写的是 ``sealed``），
    不是"告警被吞"的直接证据，所以只算结构性。
    """
    st = FakeState()
    weak = limit_up_quote(bid_vol=vol_for_seal_wan(100.0, 11.0))
    rule = build({"min_seal_amount_wan": 200})
    for dt in (0, 60, 120, 600, 3600):
        assert rule.evaluate(snap_of(weak), mk_ctx(st, now_ep=T0 + dt)) == [], f"+{dt}s"
    assert rule._state["600000:up"] == "at_limit_unqualified"


def test_seal_threshold_boundary_through_state_machine():
    """门槛边界（等于门槛放行、差一点拒绝）在跨轮状态机里同样成立。

    结构性：修复前红，但红在旧代码**没有** ``_seal_qualified`` 这个符号
    （AttributeError），不是"告警被吞"的证据，故不冒充验牙用例。
    它钉住的是门槛判据单一来源 —— ``_seal_qualified`` 与 ``_make_seal``
    两处若有 ``<=`` / ``<`` 漂移，这条会立刻红。
    """
    st = FakeState()
    rule = build({"min_seal_amount_wan": 200})
    just_below = limit_up_quote(bid_vol=vol_for_seal_wan(199.9, 11.0))
    exactly = limit_up_quote(bid_vol=vol_for_seal_wan(200.0, 11.0))

    assert rule._seal_qualified(just_below, True) is False
    assert rule._seal_qualified(exactly, True) is True
    assert rule.evaluate(snap_of(just_below), mk_ctx(st, now_ep=T0)) == []
    assert len(rule.evaluate(snap_of(exactly), mk_ctx(st, now_ep=T0 + 60))) == 1

    # min_seal_amount_wan=0 = 不设门槛 -> 任何贴板行情都算达标
    loose = build({"min_seal_amount_wan": 0})
    assert loose._seal_qualified(limit_up_quote(bid_vol=1.0), True) is True


def test_detect_seal_off_records_qualified_state_without_alerting():
    """detect_seal=False 只是不告警，"已达标封板"的状态仍要正确记账。

    回归保护 + 结构性：告警侧断言（两轮都静默）修复前后都绿；末尾状态断言
    修复前红在状态名，不是告警行为。若把资格判断和告警开关缠在一起
    （例如"不告警就干脆不写状态"），状态机会在每轮重复求值，
    一旦开关被打开就会暴出一串重复封板。
    """
    st = FakeState()
    weak = limit_up_quote(bid_vol=vol_for_seal_wan(100.0, 11.0))
    ok = limit_up_quote(bid_vol=vol_for_seal_wan(300.0, 11.0))
    rule = build({"min_seal_amount_wan": 200, "detect_seal": False})

    assert rule.evaluate(snap_of(weak), mk_ctx(st, now_ep=T0)) == []
    assert rule._state["600000:up"] == "at_limit_unqualified"
    assert rule.evaluate(snap_of(ok), mk_ctx(st, now_ep=T0 + 60)) == []
    assert rule._state["600000:up"] == "sealed", "达标与否必须照实记账，与告警开关无关"


# ---------------------------------------------------------------------------
# IT-P2-LIMIT-FIRST-BOARD-MULTI：max_per_round 截断把告警**永久**吃掉
# ---------------------------------------------------------------------------
#
# ``_check_side`` 会**顺带写状态**，而 ``max_per_round`` 的截断发生在它**之后**。
# 于是被截掉的那条告警状态已经记成 ``sealed``，下一轮命中 ``prev == "sealed"``
# 直接 ``return None`` —— 那条告警**永久消失**，不是"下轮再报"。
#
# 这是**既有遗留形态**（不是 IT-P1-LIMIT-001 引入的）：``max_per_round=0`` 时
# 三只都能报，说明单只路径本来就对；只有"同轮多只首达 + 截断"才暴露。
# 修复后：被截断的告警回滚状态，下一轮正常补报。


def test_truncated_alerts_are_reported_next_round_not_lost():
    """**真验牙**：3 只同轮首达 + ``max_per_round=2``，第 3 只必须下一轮补报。

    修复前实测三轮为 ``([], 2 条, [])`` —— 第 3 只（``600003``）第 2、3 轮
    都不出现，**永久丢失**。修复后为 ``([], 2 条, 1 条)``。
    """
    st = FakeState()
    rule = build({"min_seal_amount_wan": 200, "max_per_round": 2,
                  "cooldown_seconds": 0})
    codes = ("600001", "600002", "600003")

    weak = [limit_up_quote(code=c, bid_vol=vol_for_seal_wan(100.0, 11.0))
            for c in codes]
    ok = [limit_up_quote(code=c, bid_vol=vol_for_seal_wan(300.0, 11.0))
          for c in codes]

    # 第 1 轮：三只都不足门槛 -> 都不报（状态记 at_limit_unqualified）
    assert rule.evaluate(snap_of(*weak), mk_ctx(st, now_ep=T0)) == []

    # 第 2 轮：三只同时达标 -> 只报 2 只（截断生效）
    r2 = rule.evaluate(snap_of(*ok), mk_ctx(st, now_ep=T0 + 60))
    assert len(r2) == 2, f"max_per_round=2 应只报 2 条，实际 {len(r2)}"
    seen2 = {a.code for a in r2}

    # 第 3 轮：仍达标 -> 被截掉的那只必须补报（这正是修复前缺失的行为）
    r3 = rule.evaluate(snap_of(*ok), mk_ctx(st, now_ep=T0 + 120))
    seen3 = {a.code for a in r3}

    missing = set(codes) - seen2 - seen3
    assert not missing, (
        f"被 max_per_round 截断的票必须下一轮补报，不能永久丢失；"
        f"第2轮={sorted(seen2)} 第3轮={sorted(seen3)} 永久丢失={sorted(missing)}")

    # 补报之后不许再重复报（第 4 轮应静默）—— 回滚不能变成"永远重报"
    r4 = rule.evaluate(snap_of(*ok), mk_ctx(st, now_ep=T0 + 180))
    assert r4 == [], f"补报完成后必须恢复幂等，实际又报了 {[a.code for a in r4]}"


def test_no_truncation_reports_all_and_stays_idempotent():
    """回归保护：``max_per_round=0``（不限量）时三只都报，且随后幂等。

    这条修复前后**都绿** —— 它挡住的是"为了修截断而把幂等改坏"。
    """
    st = FakeState()
    rule = build({"min_seal_amount_wan": 200, "max_per_round": 0,
                  "cooldown_seconds": 0})
    codes = ("600001", "600002", "600003")
    weak = [limit_up_quote(code=c, bid_vol=vol_for_seal_wan(100.0, 11.0))
            for c in codes]
    ok = [limit_up_quote(code=c, bid_vol=vol_for_seal_wan(300.0, 11.0))
          for c in codes]

    assert rule.evaluate(snap_of(*weak), mk_ctx(st, now_ep=T0)) == []
    r2 = rule.evaluate(snap_of(*ok), mk_ctx(st, now_ep=T0 + 60))
    assert {a.code for a in r2} == set(codes), "不限量时三只都要报"
    assert rule.evaluate(snap_of(*ok), mk_ctx(st, now_ep=T0 + 120)) == []


def test_truncated_alert_state_rollback_is_exact():
    """被截断的票状态必须回滚到**调用前**的值，而不是一律清成 away。

    一只**已封板多轮**的票本轮不产生新告警，本就不在 ``out`` 里，不该被回滚
    逻辑碰到；只有"本轮真的产出了告警但被截掉"的才回滚。这条钉住回滚的精确性
    —— 若实现改成"截断后把超出的 tag 全 pop 掉"，会误删历史状态，
    下次封单波动就会重复报。
    """
    st = FakeState()
    rule = build({"min_seal_amount_wan": 200, "max_per_round": 1,
                  "cooldown_seconds": 0})
    weak = [limit_up_quote(code=c, bid_vol=vol_for_seal_wan(100.0, 11.0))
            for c in ("600001", "600002")]
    ok = [limit_up_quote(code=c, bid_vol=vol_for_seal_wan(300.0, 11.0))
          for c in ("600001", "600002")]

    assert rule.evaluate(snap_of(*weak), mk_ctx(st, now_ep=T0)) == []
    r2 = rule.evaluate(snap_of(*ok), mk_ctx(st, now_ep=T0 + 60))
    assert len(r2) == 1, "max_per_round=1 只报 1 条"

    reported = r2[0].code
    withheld = "600002" if reported == "600001" else "600001"

    # 报出去的那只：状态必须是 sealed（不能因为回滚而丢）
    assert rule._state[f"{reported}:up"] == "sealed", "发出去的告警状态必须保留"
    # 被截掉的那只：状态必须回滚到调用前的 at_limit_unqualified
    assert rule._state[f"{withheld}:up"] == "at_limit_unqualified", (
        f"被截断的 {withheld} 状态应回滚到 at_limit_unqualified，"
        f"实际 {rule._state.get(withheld + ':up')!r}")
