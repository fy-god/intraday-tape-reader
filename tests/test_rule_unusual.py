"""``rules/unusual.py`` 的离线测试。

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
from arad.rules.unusual import DEFAULTS, PATTERNS, UnusualRule, build, RULE
from arad.session import SessionPhase

NOW = datetime(2026, 9, 15, 14, 40, 0)          # 距收盘 20 分钟
NOW_EPOCH = NOW.timestamp()
COOLDOWN = 900


@pytest.fixture(autouse=True)
def _reset_edge_state():
    """每个用例前清空边沿检测状态。

    ``RULE`` 是模块级单例，而状态型形态（高开低走/低开高走/巨震）现在
    只在"刚成立"时报一次，状态会跨用例残留 —— 上一个用例把某只票标成
    "已在形态中"，下一个用例就再也看不到告警。必须逐个用例隔离。
    """
    RULE._edge_state.clear()
    RULE._edge_day = ""
    yield
    RULE._edge_state.clear()
    RULE._edge_day = ""


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
        dq = self.history.setdefault(code, deque(maxlen=4096))
        dq.clear()
        for p in points:
            dq.append(tuple(p))


def ctx_of(state: FakeState, *, minutes_to_close: float = 120.0, cfg: dict | None = None,
           session: SessionPhase = SessionPhase.AFTERNOON, elapsed: float = 12600.0,
           now: datetime = NOW):
    return RuleContext(
        state=state,
        cfg=dict(DEFAULTS) if cfg is None else cfg,
        now=now,
        session=session,
        elapsed_trading_seconds=elapsed,
        minutes_to_close=minutes_to_close,
    )


def patterns_of(alerts) -> list[str]:
    return [a.metrics["pattern"] for a in alerts]


def run(quotes, state: FakeState | None = None, **kw):
    return RULE.evaluate(make_snapshot(quotes, ts=NOW), ctx_of(state or FakeState(), **kw))


def run_fresh(quotes, state: FakeState | None = None, **kw):
    """用**全新规则实例**跑一轮。

    状态型形态（高开低走/低开高走/巨震）只在刚成立时报一次，同一个实例
    重复跑同一个静态条件不会再报。要测"某一轮的排序/限量"这类与历史无关的
    行为，就必须每轮用干净的实例。
    """
    fresh = build()
    return fresh.evaluate(make_snapshot(quotes, ts=NOW),
                          ctx_of(state or FakeState(), **kw))


def q_(code: str = "600000", name: str = "测试股", **kw):
    """构造自洽 Quote：amount = volume_lots*100*price（均价≈现价）。"""
    vol = float(kw.pop("volume_lots", 100_000.0))
    price = float(kw.get("price", 10.0))
    kw.setdefault("amount", vol * 100.0 * price)
    return make_quote(code=code, name=name, volume_lots=vol, **kw)


# ---------------------------------------------------------------------------
# 契约 / 工厂
# ---------------------------------------------------------------------------
def test_build_and_module_rule():
    assert RULE.name == "unusual"
    r = build({})
    assert isinstance(r, UnusualRule) and r.name == "unusual"
    assert r.cfg["max_per_round"] == 15
    assert r.cfg["cooldown_seconds"] == 900
    assert r.cfg["reseal_seconds"] == 600
    assert PATTERNS == ("high_open_fade", "low_open_rise", "wide_amplitude",
                        "late_surge", "reseal")
    assert build({"cooldown_seconds": 60}).cfg["cooldown_seconds"] == 60
    assert build({"late_session_minutes": None}).cfg["late_session_minutes"] == 30


# ---------------------------------------------------------------------------
# 1. 高开低走 high_open_fade
# ---------------------------------------------------------------------------
def test_high_open_fade_positive():
    # 昨收 10.00 -> 开 10.50（+5.0%）-> 现价 10.10（较开盘 -3.81%）
    q = q_(price=10.10, prev_close=10.00, open=10.50, high=10.55, low=10.05,
           turnover=4.0)
    alerts = run([q])
    assert patterns_of(alerts) == ["high_open_fade"]
    a = alerts[0]
    assert a.kind is AlertKind.UNUSUAL and a.severity == 2
    assert a.title == "高开低走 -3.8%"
    assert a.metrics["open_pct"] == pytest.approx(5.0)
    assert a.metrics["from_open_pct"] == pytest.approx(-3.81, abs=0.02)
    assert a.key == f"600000:{AlertKind.UNUSUAL.value}:high_open_fade:{int(NOW_EPOCH // COOLDOWN)}"
    assert "高开低走" in a.detail and "现价" in a.detail and "均价" in a.detail


def test_high_open_fade_negative():
    # 高开但只回落 -1.9% -> 不报
    q = q_(price=10.30, prev_close=10.00, open=10.50, high=10.55, low=10.25, turnover=4.0)
    assert run([q]) == []
    # 回落够深但没高开（开盘 +1.0%）-> 不报
    q2 = q_(price=10.00, prev_close=10.00, open=10.10, high=10.15, low=9.95, turnover=4.0)
    assert run([q2]) == []
    # 提高门槛 -> 不报
    q3 = q_(price=10.10, prev_close=10.00, open=10.50, high=10.55, low=10.05)
    assert run([q3], cfg=dict(DEFAULTS, high_open_pct=8.0)) == []
    assert run([q3], cfg=dict(DEFAULTS, high_open_fade_pct=8.0)) == []


# ---------------------------------------------------------------------------
# 2. 低开高走 low_open_rise
# ---------------------------------------------------------------------------
def test_low_open_rise_positive():
    # 昨收 10.00 -> 开 9.60（-4.0%）-> 现价 10.00（较开盘 +4.17%）
    q = q_(price=10.00, prev_close=10.00, open=9.60, high=10.05, low=9.55, turnover=4.0)
    alerts = run([q])
    assert patterns_of(alerts) == ["low_open_rise"]
    a = alerts[0]
    assert a.severity == 2
    assert a.title == "低开高走 +4.2%"
    assert a.metrics["open_pct"] == pytest.approx(-4.0)
    assert a.metrics["from_open_pct"] == pytest.approx(4.17, abs=0.02)
    assert a.key == f"600000:{AlertKind.UNUSUAL.value}:low_open_rise:{int(NOW_EPOCH // COOLDOWN)}"
    assert "低开高走" in a.detail


def test_low_open_rise_negative():
    # 低开但只反弹 +1.0% -> 不报
    q = q_(price=9.70, prev_close=10.00, open=9.60, high=9.75, low=9.55, turnover=4.0)
    assert run([q]) == []
    # 反弹够猛但平开 -> 不报
    q2 = q_(price=10.40, prev_close=10.00, open=10.00, high=10.45, low=9.98, turnover=4.0)
    assert run([q2]) == []


# ---------------------------------------------------------------------------
# 3. 巨震 wide_amplitude
# ---------------------------------------------------------------------------
def test_wide_amplitude_positive():
    # 振幅 (10.60-9.60)/10.00 = 10.0% >= 9.0%，成交额 1.02 亿 >= 1 亿
    q = q_(price=10.20, prev_close=10.00, open=10.00, high=10.60, low=9.60,
           volume_lots=100_000.0, turnover=4.0)          # amount = 1.02 亿
    alerts = run([q])
    assert patterns_of(alerts) == ["wide_amplitude"]
    a = alerts[0]
    assert a.severity == 2 and a.title == "巨震 10.0%"
    assert a.metrics["amplitude"] == pytest.approx(10.0)
    assert a.metrics["amount"] == pytest.approx(102_000_000.0)


def test_wide_amplitude_negative():
    # 振幅够但成交额只有 5100 万 -> 不报
    q = q_(price=10.20, prev_close=10.00, open=10.00, high=10.60, low=9.60,
           volume_lots=5_000.0, turnover=1.0)            # amount = 5100 万
    assert run([q]) == []
    # 成交额够但振幅只有 3% -> 不报
    q2 = q_(price=10.20, prev_close=10.00, open=10.00, high=10.30, low=10.00)
    assert run([q2]) == []


# ---------------------------------------------------------------------------
# 4. 尾盘异动 late_surge
# ---------------------------------------------------------------------------
def test_late_surge_positive_up_and_down():
    st = FakeState()
    st.feed("600000", [(NOW_EPOCH - 300.0, 10.00, 100_000.0), (NOW_EPOCH, 10.20, 110_000.0)])
    q = q_(price=10.20, prev_close=10.00, open=10.00, high=10.25, low=9.98,
           volume_lots=110_000.0, turnover=4.0)
    alerts = run([q], st, minutes_to_close=20.0)
    assert patterns_of(alerts) == ["late_surge"]
    a = alerts[0]
    assert a.severity == 2 and a.title == "尾盘急拉 +2.0%"
    assert a.metrics["late_change_pct"] == pytest.approx(2.0)
    assert a.metrics["minutes_to_close"] == pytest.approx(20.0)
    assert a.key == f"600000:{AlertKind.UNUSUAL.value}:late_surge:{int(NOW_EPOCH // COOLDOWN)}"

    # 尾盘跳水（同为 late_surge，标题不同）
    st2 = FakeState()
    st2.feed("600000", [(NOW_EPOCH - 300.0, 10.20, 100_000.0), (NOW_EPOCH, 10.00, 110_000.0)])
    q2 = q_(price=10.00, prev_close=10.20, open=10.20, high=10.25, low=9.98,
            volume_lots=110_000.0, turnover=4.0)
    a2 = run([q2], st2, minutes_to_close=10.0)[0]
    assert a2.metrics["pattern"] == "late_surge"
    assert a2.title == "尾盘跳水 -2.0%"


def test_late_surge_negative():
    st = FakeState()
    st.feed("600000", [(NOW_EPOCH - 300.0, 10.00, 100_000.0), (NOW_EPOCH, 10.20, 110_000.0)])
    q = q_(price=10.20, prev_close=10.00, open=10.00, high=10.25, low=9.98,
           volume_lots=110_000.0, turnover=4.0)
    # 距收盘还有 45 分钟 -> 不算尾盘
    assert run([q], st, minutes_to_close=45.0) == []
    # 是尾盘但近 5 分钟只动了 0.5% -> 不报
    st2 = FakeState()
    st2.feed("600000", [(NOW_EPOCH - 300.0, 10.00, 100_000.0), (NOW_EPOCH, 10.05, 110_000.0)])
    q2 = q_(price=10.05, prev_close=10.00, open=10.00, high=10.10, low=9.98,
            volume_lots=110_000.0, turnover=4.0)
    assert run([q2], st2, minutes_to_close=20.0) == []
    # 尾盘但没有足够历史 -> price_change 返回 None -> 不报
    assert run([q], FakeState(), minutes_to_close=20.0) == []


# ---------------------------------------------------------------------------
# 5. 快速回封 reseal
# ---------------------------------------------------------------------------
def reseal_quote(code: str = "600000", **kw):
    """昨收 10.00 -> 涨停价 11.00，现价封在 11.00。"""
    base = dict(price=11.00, prev_close=10.00, open=10.00, high=11.00, low=10.80,
                volume_lots=200_000.0, turnover=6.0)
    base.update(kw)
    return q_(code=code, **base)


def test_reseal_positive():
    st = FakeState()
    st.feed("600000", [
        (NOW_EPOCH - 540.0, 10.40, 100_000.0),
        (NOW_EPOCH - 480.0, 11.00, 120_000.0),   # 曾封上涨停
        (NOW_EPOCH - 300.0, 10.80, 150_000.0),   # 炸板跌离（<= 11.00*0.995=10.945）
        (NOW_EPOCH - 60.0, 10.90, 180_000.0),
        (NOW_EPOCH, 11.00, 200_000.0),           # 重新封回
    ])
    alerts = run([reseal_quote()], st)
    assert patterns_of(alerts) == ["reseal"]
    a = alerts[0]
    assert a.severity == 3 and a.title == "快速回封"
    assert a.metrics["limit_up_price"] == pytest.approx(11.00)
    assert a.key == f"600000:{AlertKind.UNUSUAL.value}:reseal:{int(NOW_EPOCH // COOLDOWN)}"
    assert "快速回封" in a.detail and "炸板" in a.detail


def test_reseal_negative_no_history():
    """历史里没有任何点 -> 不报（不瞎猜）。"""
    assert run([reseal_quote()], FakeState()) == []
    # history 非空但没有该 code
    st = FakeState()
    st.feed("000002", [(NOW_EPOCH - 480.0, 11.00, 1.0), (NOW_EPOCH - 300.0, 10.80, 2.0)])
    assert run([reseal_quote()], st) == []


def test_reseal_negative_never_sealed_or_no_pullback():
    # 历史里从未触及涨停
    st = FakeState()
    st.feed("600000", [
        (NOW_EPOCH - 480.0, 10.50, 100_000.0),
        (NOW_EPOCH - 300.0, 10.60, 120_000.0),
        (NOW_EPOCH, 11.00, 140_000.0),
    ])
    assert run([reseal_quote()], st) == []

    # 曾涨停但一路封住、从未跌离涨停 0.5% 以上 -> 不是「回封」
    st2 = FakeState()
    st2.feed("600000", [
        (NOW_EPOCH - 480.0, 11.00, 100_000.0),
        (NOW_EPOCH - 300.0, 10.99, 120_000.0),
        (NOW_EPOCH - 60.0, 11.00, 140_000.0),
        (NOW_EPOCH, 11.00, 160_000.0),
    ])
    assert run([reseal_quote()], st2) == []

    # 封板后又跌开，但**现在没在涨停价上** -> 不报（那是炸板，归 limit_board）
    st3 = FakeState()
    st3.feed("600000", [
        (NOW_EPOCH - 480.0, 11.00, 100_000.0),
        (NOW_EPOCH - 300.0, 10.50, 120_000.0),
        (NOW_EPOCH, 10.90, 160_000.0),
    ])
    assert run([reseal_quote(price=10.90, high=11.00, low=10.50)], st3) == []
    # 连快照本身都不在涨停价上时，连历史都不用看
    assert run([reseal_quote(price=10.90, high=11.00, low=10.50)], FakeState()) == []

    # 曾涨停的时间在 reseal_seconds 窗口之外（700s 前，窗口 600s）-> 不报
    st4 = FakeState()
    st4.feed("600000", [
        (NOW_EPOCH - 700.0, 11.00, 100_000.0),
        (NOW_EPOCH - 650.0, 10.80, 120_000.0),
        (NOW_EPOCH, 11.00, 160_000.0),
    ])
    assert run([reseal_quote()], st4) == []


def test_reseal_window_is_configurable():
    st = FakeState()
    st.feed("600000", [
        (NOW_EPOCH - 700.0, 11.00, 100_000.0),
        (NOW_EPOCH - 650.0, 10.80, 120_000.0),
        (NOW_EPOCH, 11.00, 160_000.0),
    ])
    # 窗口放宽到 900s -> 命中
    alerts = run([reseal_quote()], st, cfg=dict(DEFAULTS, reseal_seconds=900))
    assert patterns_of(alerts) == ["reseal"]
    # 窗口配 0 = 关闭该子检测
    assert run([reseal_quote()], st, cfg=dict(DEFAULTS, reseal_seconds=0)) == []


# ---------------------------------------------------------------------------
# 组合 / key / 排序
# ---------------------------------------------------------------------------
def test_multiple_patterns_same_stock_same_round():
    """同一只票同一轮可以同时出多个 pattern，且各自 key 独立。"""
    q = q_(price=10.10, prev_close=10.00, open=10.50, high=11.00, low=9.90,
           volume_lots=200_000.0, turnover=6.0)      # 振幅 11% + 高开低走
    alerts = run([q])
    assert sorted(patterns_of(alerts)) == ["high_open_fade", "wide_amplitude"]
    keys = {a.key for a in alerts}
    assert len(keys) == 2
    for a in alerts:
        assert a.key == (f"600000:{AlertKind.UNUSUAL.value}:{a.metrics['pattern']}"
                         f":{int(NOW_EPOCH // COOLDOWN)}")
    # 同一 pattern 只出一条
    assert len([a for a in alerts if a.metrics["pattern"] == "high_open_fade"]) == 1
    assert len([a for a in alerts if a.metrics["pattern"] == "wide_amplitude"]) == 1


def test_all_five_patterns_can_coexist_across_stocks():
    st = FakeState()
    # reseal：600009 曾涨停后炸板又封回
    st.feed("600009", [
        (NOW_EPOCH - 480.0, 11.00, 100_000.0),
        (NOW_EPOCH - 300.0, 10.80, 120_000.0),
        (NOW_EPOCH, 11.00, 160_000.0),
    ])
    # late_surge：600008 近 5 分钟 +2%
    st.feed("600008", [(NOW_EPOCH - 300.0, 10.00, 100_000.0), (NOW_EPOCH, 10.20, 110_000.0)])

    quotes = [
        q_(code="600001", price=10.10, prev_close=10.00, open=10.50,
           high=10.55, low=10.05),                                   # 高开低走
        q_(code="600002", price=10.00, prev_close=10.00, open=9.60,
           high=10.05, low=9.55),                                    # 低开高走
        q_(code="600003", price=10.20, prev_close=10.00, open=10.00,
           high=10.60, low=9.60, volume_lots=100_000.0),             # 巨震
        q_(code="600008", price=10.20, prev_close=10.00, open=10.00,
           high=10.25, low=9.98, volume_lots=110_000.0),             # 尾盘异动
        reseal_quote(code="600009"),                                 # 快速回封
    ]
    alerts = run(quotes, st, minutes_to_close=20.0)
    # 五种子形态全部出现（600009 同时命中 reseal + late_surge，这正是允许的行为）
    assert set(patterns_of(alerts)) == set(PATTERNS)
    # sev3 在最前
    assert alerts[0].metrics["pattern"] == "reseal"
    assert alerts[0].severity == 3
    assert all(a.severity == 2 for a in alerts[1:])
    # 每只票的 pattern 互不重复（同 pattern 每轮至多一条）
    for code in {a.code for a in alerts}:
        pats = [a.metrics["pattern"] for a in alerts if a.code == code]
        assert len(pats) == len(set(pats))
    # 600009 一票两 pattern，key 不冲突
    multi = [a for a in alerts if a.code == "600009"]
    assert len(multi) == 2
    assert len({a.key for a in multi}) == 2


def test_sorting_by_severity_then_abs_pct_and_limit():
    st = FakeState()
    st.feed("600001", [
        (NOW_EPOCH - 480.0, 11.00, 100_000.0),
        (NOW_EPOCH - 300.0, 10.80, 120_000.0),
        (NOW_EPOCH, 11.00, 160_000.0),
    ])
    a = reseal_quote(code="600001")                                  # sev3, pct +10.0
    b = q_(code="600002", price=10.10, prev_close=10.00, open=10.50,
           high=10.55, low=10.05)                                    # sev2, pct +1.0
    c = q_(code="600003", price=11.00, prev_close=11.00, open=11.60,
           high=11.60, low=11.00, volume_lots=5_000.0)               # sev2, pct 0.0
    alerts = run_fresh([b, c, a], st)
    assert [x.code for x in alerts] == ["600001", "600002", "600003"]
    assert [x.severity for x in alerts] == [3, 2, 2]
    assert [x.metrics["pattern"] for x in alerts] == \
        ["reseal", "high_open_fade", "high_open_fade"]
    # |pct| 降序：600002 (+1.0) 在 600003 (0.0) 之前
    assert abs(alerts[1].pct) > abs(alerts[2].pct)
    # 限量（每轮独立实例，保证边沿检测不干扰计数）
    lim = run_fresh([b, c, a], st, cfg=dict(DEFAULTS, max_per_round=2))
    assert [x.code for x in lim] == ["600001", "600002"]
    # 0 = 不限量
    assert len(run_fresh([b, c, a], st, cfg=dict(DEFAULTS, max_per_round=0))) == 3


def test_cooldown_bucket_changes():
    """key 里的时间桶仍在（用于下游区分事件），但状态型形态不再逐轮重报。"""
    cfg = dict(DEFAULTS, cooldown_seconds=COOLDOWN)
    q = q_(price=10.10, prev_close=10.00, open=10.50, high=10.55, low=10.05)
    st = FakeState()
    rule = build()
    a1 = rule.evaluate(make_snapshot([q], ts=NOW), ctx_of(st, cfg=cfg))[0]
    assert a1.key == f"600000:unusual:high_open_fade:{int(NOW_EPOCH // COOLDOWN)}"

    # 同桶内（+30s）：形态仍在，但**已在形态中** -> 不再重报
    t2 = NOW + timedelta(seconds=30)
    assert rule.evaluate(make_snapshot([q], ts=t2), ctx_of(st, cfg=cfg, now=t2)) == []

    # 跨桶（+16 分钟）：依然不重报 —— 这才是修复的核心
    t3 = NOW + timedelta(seconds=960)
    assert rule.evaluate(make_snapshot([q], ts=t3), ctx_of(st, cfg=cfg, now=t3)) == []

    # 同 code 不同 pattern 的 key 永不冲突
    multi = q_(price=10.10, prev_close=10.00, open=10.50, high=11.00, low=9.90,
               volume_lots=200_000.0)
    ks = [x.key for x in build().evaluate(
        make_snapshot([multi], ts=NOW), ctx_of(st, cfg=cfg))]
    assert len(ks) == len(set(ks)) == 2


def test_state_pattern_reports_once_then_rearms_after_recovery():
    """状态型形态：只报一次，恢复到阈值一半以下后可再次触发。"""
    cfg = dict(DEFAULTS, cooldown_seconds=COOLDOWN)
    st = FakeState()
    rule = build()

    # 处于"高开低走"形态
    faded = q_(price=10.10, prev_close=10.00, open=10.50, high=10.55, low=10.05)

    def ev(q, t):
        return rule.evaluate(make_snapshot([q], ts=t), ctx_of(st, cfg=cfg, now=t))

    first = ev(faded, NOW)
    assert [a.metrics["pattern"] for a in first] == ["high_open_fade"]

    # 条件完全没变，连续 10 轮都不应再报
    for i in range(1, 11):
        t = NOW + timedelta(seconds=60 * i)
        assert ev(faded, t) == [], f"第 {i} 轮不该重报"

    # 恢复：现价回到开盘上方（脱离形态）
    recovered = q_(price=10.52, prev_close=10.00, open=10.50, high=10.55, low=10.05)
    t_r = NOW + timedelta(seconds=700)
    assert ev(recovered, t_r) == []

    # 再次跌破 -> 重新告警
    t_a = NOW + timedelta(seconds=800)
    again = ev(faded, t_a)
    assert [a.metrics["pattern"] for a in again] == ["high_open_fade"], "恢复后应能再次触发"


def test_wide_amplitude_reports_once_per_day_per_stock():
    """巨震只报一次：振幅是当日最高/最低的函数，不会自己恢复。"""
    cfg = dict(DEFAULTS, cooldown_seconds=COOLDOWN)
    st = FakeState()
    rule = build()
    q = q_(price=10.20, prev_close=10.00, open=10.50, high=10.60, low=9.60,
           volume_lots=200_000.0)

    hits = 0
    for i in range(12):                       # 模拟一整个上午 + 下午
        t = NOW + timedelta(seconds=300 * i)
        hits += len([a for a in rule.evaluate(
            make_snapshot([q], ts=t), ctx_of(st, cfg=cfg, now=t))
            if a.metrics["pattern"] == "wide_amplitude"])
    assert hits == 1, f"巨震一整天只应报一次，实际 {hits} 次"


def test_edge_state_resets_on_new_trading_day():
    """边沿状态按自然日清零：第二天同样的形态要能重新报。"""
    cfg = dict(DEFAULTS, cooldown_seconds=COOLDOWN)
    st = FakeState()
    rule = build()
    q = q_(price=10.20, prev_close=10.00, open=10.50, high=10.60, low=9.60,
           volume_lots=200_000.0)

    d1 = NOW
    assert [a.metrics["pattern"] for a in rule.evaluate(
        make_snapshot([q], ts=d1), ctx_of(st, cfg=cfg, now=d1))] == ["wide_amplitude"]

    d2 = datetime(2026, 9, 16, 9, 40, 0)     # 次日早盘
    pats = [a.metrics["pattern"] for a in rule.evaluate(
        make_snapshot([q], ts=d2), ctx_of(st, cfg=cfg, now=d2))]
    assert "wide_amplitude" in pats, "跨日后应重新计为一次新事件"


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------
def test_edge_cases_do_not_crash_or_alert():
    st = FakeState()
    # 停牌（price<=0 / volume_lots<=0）
    assert run([q_(price=0.0, volume_lots=0.0, amount=0.0)], st) == []
    # prev_close <= 0
    assert run([q_(prev_close=0.0, price=0.0)], st) == []
    # open <= 0：高开/低开两项自动跳过，但巨震仍可判定（用昨收）
    q = q_(price=10.20, prev_close=10.00, open=0.0, high=10.60, low=9.60,
           volume_lots=100_000.0)
    assert patterns_of(run([q], st)) == ["wide_amplitude"]
    # 空快照
    assert run([], st) == []


def test_only_continuous_and_enabled_switches():
    st = FakeState()
    st.feed("600000", [(NOW_EPOCH - 300.0, 10.00, 100_000.0), (NOW_EPOCH, 10.20, 110_000.0)])
    q = q_(price=10.20, prev_close=10.00, open=10.00, high=10.25, low=9.98,
           volume_lots=110_000.0)
    for phase in (SessionPhase.CLOSED, SessionPhase.PRE_OPEN, SessionPhase.AUCTION,
                  SessionPhase.LUNCH, SessionPhase.POST):
        assert run([q], st, minutes_to_close=20.0, session=phase) == []
    for phase in (SessionPhase.MORNING, SessionPhase.AFTERNOON):
        assert len(run([q], st, minutes_to_close=20.0, session=phase)) == 1
    # only_continuous=False -> 休市也判（尾盘异动属连续竞价时段，但仍受该开关约束）
    assert len(run([q], st, minutes_to_close=20.0, session=SessionPhase.LUNCH,
                   cfg=dict(DEFAULTS, only_continuous=False))) == 1
    # enabled=False -> 直接返回
    assert run([q], st, minutes_to_close=20.0, cfg=dict(DEFAULTS, enabled=False)) == []
    # ctx.cfg 为空 dict -> 回落到实例自身 cfg
    ctx = RuleContext(state=st, cfg={}, now=NOW, session=SessionPhase.AFTERNOON,
                      elapsed_trading_seconds=12600.0, minutes_to_close=20.0)
    assert len(RULE.evaluate(make_snapshot([q], ts=NOW), ctx)) == 1


def test_reseal_no_limit_price_available():
    """涨停价算不出来（limit_up<=0 且 prev_close<=0）时不报也不崩。"""
    st = FakeState()
    st.feed("600000", [(NOW_EPOCH - 480.0, 11.00, 100_000.0),
                       (NOW_EPOCH - 300.0, 10.80, 120_000.0)])
    q = q_(price=11.00, prev_close=0.0, open=0.0, limit_up=0.0)
    assert run([q], st) == []


# ---------------------------------------------------------------------------
# 性能冒烟
# ---------------------------------------------------------------------------
def test_perf_1000_stocks_under_200ms():
    st = FakeState()
    quotes = []
    for i in range(1000):
        code = f"{600000 + i:06d}"
        # 每只都给一段历史（含曾涨停 + 炸板），最坏情况下也走满状态机
        st.feed(code, [
            (NOW_EPOCH - 540.0, 10.40, 100_000.0),
            (NOW_EPOCH - 480.0, 11.00, 120_000.0),
            (NOW_EPOCH - 300.0, 10.80, 150_000.0),
            (NOW_EPOCH - 60.0, 10.90, 180_000.0),
            (NOW_EPOCH, 11.00, 200_000.0),
        ])
        quotes.append(reseal_quote(code=code, name=f"票{i}"))
    snap = make_snapshot(quotes, ts=NOW)
    ctx = ctx_of(st)
    t0 = time.perf_counter()
    alerts = RULE.evaluate(snap, ctx)
    ms = (time.perf_counter() - t0) * 1000.0
    assert len(alerts) == 15           # 默认限量 15
    assert ms < 200.0, f"1000 只股票 evaluate 耗时 {ms:.1f}ms >= 200ms"


def test_perf_1000_normal_stocks_under_200ms():
    """绝大多数股票并不在涨停价上，reseal 必须早退。"""
    st = FakeState()
    quotes = [q_(code=f"{600000 + i:06d}", name=f"票{i}", price=10.20, prev_close=10.00,
                 open=10.10, high=10.30, low=10.05, volume_lots=80_000.0, turnover=3.0)
              for i in range(1000)]
    snap = make_snapshot(quotes, ts=NOW)
    ctx = ctx_of(st)
    t0 = time.perf_counter()
    RULE.evaluate(snap, ctx)
    ms = (time.perf_counter() - t0) * 1000.0
    assert ms < 200.0, f"1000 只股票 evaluate 耗时 {ms:.1f}ms >= 200ms"
