"""短线精灵价格类信号测试（离线，极简 FakeState）。

本文件的重点是**反例**：``rocket`` 与 ``rebound``、``dive`` 与 ``accel_down``
在"幅度"上完全一样，区分它们靠的是形态证据。所以每个信号都要证明
"看起来像但不该报"的情况确实不报。
"""
from __future__ import annotations

from datetime import datetime
import time

import pytest

from arad.models import AlertKind, Quote, Snapshot, board_of
from arad.rules.base import RuleContext, bucket_of
from arad.rules.spirit_price import SIGNALS, SpiritPriceRule, build
from arad.session import SessionPhase

T0 = 1_800_000_000.0


class FakeState:
    def __init__(self):
        self.history: dict[str, list[tuple[float, float, float]]] = {}

    def feed(self, code, points):
        self.history[code] = list(points)

    def window(self, code, seconds, now_epoch):
        cutoff = now_epoch - float(seconds)
        return [p for p in self.history.get(code, []) if p[0] >= cutoff]

    def price_change(self, code, seconds, now_epoch):
        pts = self.window(code, seconds, now_epoch)
        if len(pts) < 2 or pts[0][1] <= 0:
            return None
        return (pts[-1][1] / pts[0][1] - 1.0) * 100.0

    def volume_delta(self, code, seconds, now_epoch):
        pts = self.window(code, seconds, now_epoch)
        if len(pts) < 2:
            return 0.0
        return max(0.0, pts[-1][2] - pts[0][2])


def mk_ctx(state, *, session=SessionPhase.MORNING, now_ep=T0):
    return RuleContext(state=state, cfg={}, now=datetime.fromtimestamp(now_ep),
                       session=session, elapsed_trading_seconds=1800.0,
                       minutes_to_close=120.0)


def snap_of(*qs: Quote) -> Snapshot:
    return Snapshot(ts=datetime.fromtimestamp(T0), seq=1,
                    quotes={q.code: q for q in qs})


def q_(code="600000", name="测试股", *, prev_close=10.0, price=None, high=None,
       low=None, open_=None, volume_lots=100000.0, **kw) -> Quote:
    """构造自洽 Quote（amount 与 volume_lots/price 一致，使 vwap 约等于 price）。"""
    price = prev_close if price is None else price
    high = max(price, prev_close) if high is None else high
    low = min(price, prev_close) if low is None else low
    open_ = prev_close if open_ is None else open_
    kw.setdefault("amount", volume_lots * 100.0 * price)
    return Quote(code=code, name=name, board=board_of(code, name), price=price,
                 prev_close=prev_close, open=open_, high=high, low=low,
                 volume_lots=volume_lots, **kw)


def ramp(code, *, start, end, seconds=180, step=15, expo=1.6, base=False):
    """生成走势序列；``expo`` >1 为加速（凸），=1 为匀速。

    ``base=True`` 时时间戳从很早开始，保证窗口"覆盖"校验通过。
    """
    pts = []
    n = int(seconds // step)
    origin = (T0 - 3600.0) if base else (T0 - seconds)
    for i in range(n + 1):
        frac = (i / n) ** expo
        pts.append((origin + i * step, start + (end - start) * frac,
                    1000.0 + 200.0 * i))
    return pts


RULE = build({"enabled": True})


def run(quotes, st, cfg=None, *, session=SessionPhase.MORNING, now_ep=T0):
    rule = build(dict({"enabled": True}, **(cfg or {})))
    qs = quotes if isinstance(quotes, (list, tuple)) else [quotes]
    return rule.evaluate(snap_of(*qs),
                         mk_ctx(st, session=session, now_ep=now_ep))


def patterns(alerts):
    return [a.metrics["pattern"] for a in alerts]


# ==========================================================================
# 模块契约
# ==========================================================================
def test_module_contract():
    assert set(SIGNALS) == {"rocket", "rebound", "dive", "accel_down"}
    assert build().name == "spirit_price"
    assert build().windows == [60.0, 180.0]
    assert build().cooldown == 300.0
    assert build().max_per_round == 15


def test_disabled_by_default():
    """默认关闭：tick_surge 已覆盖"涨得快"，避免同波行情两条告警。"""
    assert build().cfg["enabled"] is False
    st = FakeState()
    st.feed("600000", ramp("600000", start=10.0, end=10.5))
    q = q_(price=10.5, high=10.5)
    assert build().evaluate(snap_of(q), mk_ctx(st)) == []


def test_config_override():
    r = build({"enabled": True, "cooldown_seconds": 60, "rocket_pct": 5.0,
               "windows": [30]})
    assert r.cooldown == 60.0 and r.windows == [30.0]
    assert r._g("rocket_pct", 0) == 5.0


def test_invalid_windows_dropped():
    r = build({"windows": [60, -5, 0, "x", None, 180]})
    assert r.windows == [60.0, 180.0]


def test_only_continuous_filter():
    st = FakeState()
    st.feed("600000", ramp("600000", start=10.0, end=10.5, seconds=180))
    q = q_(price=10.5, high=10.5)
    cfg = {"windows": [180], "rocket_pct": 1.0}
    for ph in (SessionPhase.CLOSED, SessionPhase.LUNCH, SessionPhase.POST,
               SessionPhase.PRE_OPEN, SessionPhase.AUCTION):
        assert run([q], st, cfg=cfg, session=ph) == [], ph
    for ph in (SessionPhase.MORNING, SessionPhase.AFTERNOON):
        assert patterns(run([q], st, cfg=cfg, session=ph)) == ["rocket"], ph


def test_only_continuous_can_be_disabled():
    st = FakeState()
    st.feed("600000", ramp("600000", start=10.0, end=10.5, seconds=180))
    q = q_(price=10.5, high=10.5)
    r = build({"enabled": True, "only_continuous": False, "windows": [180],
               "rocket_pct": 2.0})
    out = r.evaluate(snap_of(q), mk_ctx(st, session=SessionPhase.POST))
    assert patterns(out) == ["rocket"]


# ==========================================================================
# rocket 火箭发射
# ==========================================================================
def test_rocket_on_new_high():
    """快速上涨 + 站在当日最高价上 -> 火箭发射。"""
    st = FakeState()
    # 涨 3%（未达阈值 2.0 的两倍 4%）-> severity 2
    st.feed("600000", ramp("600000", start=10.0, end=10.3, seconds=180))
    q = q_(price=10.3, high=10.3, low=10.0)
    out = run([q], st, cfg={"windows": [180], "rocket_pct": 2.0,
                            "urgent_multiple": 2.0})
    assert patterns(out) == ["rocket"]
    assert out[0].kind is AlertKind.SURGE
    assert out[0].severity == 2


def test_rocket_counterexample_not_at_high():
    """涨得快但**没创当日新高**（已从高点回落）-> 不是火箭发射。"""
    st = FakeState()
    st.feed("600000", ramp("600000", start=10.0, end=10.4, seconds=180))
    q = q_(price=10.4, high=10.9, low=10.0)      # 高点 10.9，现价离得远
    out = run([q], st, cfg={"windows": [180], "dive_prior_rise_pct": 99.0})
    assert "rocket" not in patterns(out)


def test_rocket_counterexample_insufficient_gain():
    """涨幅不够 -> 不报。"""
    st = FakeState()
    st.feed("600000", ramp("600000", start=10.0, end=10.05, seconds=180, expo=1.0))
    q = q_(price=10.05, high=10.05)
    out = run([q], st, cfg={"windows": [180], "rocket_pct": 2.0})
    assert out == []


def test_rocket_counterexample_no_history_coverage():
    """历史只有 40 秒却按 180 秒判定 -> 必须跳过（否则开盘就误报）。"""
    st = FakeState()
    st.feed("600000", [(T0 - 40, 10.0, 1000), (T0 - 20, 10.3, 1200),
                       (T0, 10.5, 1400)])
    q = q_(price=10.5, high=10.5)
    out = run([q], st, cfg={"windows": [180]})
    assert out == [], "窗口未被历史覆盖时不得判定"


def test_rocket_urgent_severity():
    """涨幅达阈值 2 倍 -> severity 3。"""
    st = FakeState()
    st.feed("600000", ramp("600000", start=10.0, end=10.8, seconds=180))
    q = q_(price=10.8, high=10.8, low=10.0)
    out = run([q], st, cfg={"windows": [180], "rocket_pct": 2.0,
                            "urgent_multiple": 2.0})
    assert patterns(out) == ["rocket"]
    assert out[0].severity == 3


def test_rocket_min_high_guard():
    """要求当日高点较昨收有涨幅时，下跌股的小反弹不算火箭发射。"""
    st = FakeState()
    # 从 10.0 跌到 9.5 再拉到 9.7；9.7 是"当日新高"但仍是亏损的
    st.feed("600000", [(T0 - 180, 10.0, 1000), (T0 - 120, 9.5, 1300),
                       (T0 - 60, 9.6, 1600), (T0, 9.7, 1900)])
    q = q_(price=9.7, high=9.7, low=9.5)
    out = run([q], st, cfg={"windows": [180], "rocket_pct": 1.0,
                            "rocket_min_high_pct": 1.0})
    assert "rocket" not in patterns(out)


# ==========================================================================
# rebound 快速反弹
# ==========================================================================
def test_rebound_after_drop():
    """先跌后拉 -> 快速反弹。

    注意窗口涨跌幅是 **-1.0%**（10.2 -> 10.1），但自低点 9.9 拉起了 2.0%，
    这正是本信号不能用窗口涨跌幅判定的原因。
    """
    st = FakeState()
    st.feed("600000", [(T0 - 180, 10.2, 1000), (T0 - 120, 10.0, 1300),
                       (T0 - 60, 9.9, 1600), (T0 - 30, 10.0, 1800),
                       (T0, 10.1, 2100)])
    q = q_(price=10.1, high=10.25, low=9.9, prev_close=10.0)
    out = run([q], st, cfg={"windows": [180], "rebound_pct": 1.0,
                            "rebound_prior_drop_pct": 1.0})
    assert patterns(out) == ["rebound"]
    assert out[0].kind is AlertKind.SURGE
    assert out[0].metrics["window_pct"] < 0, "窗口涨跌幅确实是负的"
    assert out[0].metrics["signal_pct"] > 0, "但信号强度（自低点拉起）是正的"

    # ⚠ 标题必须印**信号强度**（正），不能印窗口涨跌幅（负）。
    # 这是一条真实的用户可见缺陷：旧代码的标题取的是 window_pct，
    # 于是这里会显示「快速反弹 -1.00%」—— 一个叫"反弹"的信号带着负数，
    # 读起来完全相反。更极端的例子是现价恰好涨回窗口起点时，
    # 自低点已拉起 5%（severity 3，会弹窗），标题却是「快速反弹 +0.00%」。
    #
    # 上面两行断言只验证了 metrics 里两个数一正一负（旧代码也通过），
    # 所以这个缺陷在测试里潜伏了很久 —— 必须直接盯标题。
    title_pct = out[0].title.replace("快速反弹", "").strip()
    assert title_pct.startswith("+"), (
        f"「快速反弹」的标题不应是负数或零，实际是 {out[0].title!r}；"
        f"窗口涨跌幅 {out[0].metrics['window_pct']} 与信号强度 "
        f"{out[0].metrics['signal_pct']} 是两个量，标题要印后者")
    assert f"{out[0].metrics['signal_pct']:.2f}" in title_pct, (
        f"标题 {out[0].title!r} 里的数字与分级依据 signal_pct "
        f"{out[0].metrics['signal_pct']} 对不上")


def test_rebound_counterexample_no_prior_drop():
    """一路上涨（没先跌过）-> 不是反弹，是火箭发射。"""
    st = FakeState()
    st.feed("600000", ramp("600000", start=10.0, end=10.4, seconds=180))
    q = q_(price=10.4, high=10.4, low=10.0)      # 在当日最高 -> rocket
    out = run([q], st, cfg={"windows": [180], "rebound_pct": 1.0,
                            "rebound_prior_drop_pct": 1.0,
                            "rocket_pct": 1.0})
    assert "rebound" not in patterns(out)
    assert patterns(out) == ["rocket"], "创新高的应归 rocket"


def _rebound_state():
    """10.2 -> 9.9 -> 10.1：自低点拉起 2.02%，窗口涨跌幅 -0.98%。"""
    st = FakeState()
    st.feed("600000", [(T0 - 180, 10.2, 1000), (T0 - 120, 10.0, 1300),
                       (T0 - 60, 9.9, 1600), (T0 - 30, 10.0, 1800),
                       (T0, 10.1, 2100)])
    return st


def test_rebound_legacy_key_rebound_off_low_pct_is_honoured():
    """旧配置名 ``rebound_off_low_pct`` 必须真的生效（不只是注释里说兼容）。

    这是一条真实的死键：``config/settings.yaml`` 那行写着
    "旧配置名，等价于 rebound_pct（保留兼容）"，但代码从来没读过它 ——
    只有注释在承诺，实现没做。于是把旧名改成一个新值完全不生效，也不报错。

    由 ``tools/check_config_consumed.py`` 抓出（它把各模块 DEFAULTS 的
    声明点挖掉后再找读取点），对应 docs/TEXT_AUDIT.md D11。

    这里用**行为**验证：把门槛设成 2.5%（大于实际的 2.02%），
    若旧名真被读了，反弹就不该触发；设成 1.0% 则应当触发。
    """
    # 自低点拉起 2.02%：门槛 2.5% -> 不报
    out_high = run([q_(price=10.1, high=10.25, low=9.9)], _rebound_state(),
                   cfg={"windows": [180], "rebound_off_low_pct": 2.5,
                        "rebound_prior_drop_pct": 1.0})
    assert "rebound" not in patterns(out_high), (
        "旧名 rebound_off_low_pct=2.5 没生效 —— 2.02% 的反弹仍被报了出来")

    # 门槛 1.0% -> 应报
    out_low = run([q_(price=10.1, high=10.25, low=9.9)], _rebound_state(),
                  cfg={"windows": [180], "rebound_off_low_pct": 1.0,
                       "rebound_prior_drop_pct": 1.0})
    assert "rebound" in patterns(out_low), (
        "旧名 rebound_off_low_pct=1.0 没生效 —— 2.02% 的反弹反而没报出来")


def test_rebound_new_key_still_wins_over_legacy_default():
    """新名不能被 DEFAULTS 里那个旧名的默认值压死。

    兼容实现最容易踩的坑：DEFAULTS 里**新旧名都有**（都是 2.0）。
    若用 merged 判断"用户设了旧名没有"，答案永远为真，于是用户设的
    ``rebound_pct`` 会被 DEFAULTS 里的旧名默认值静默压掉 ——
    那就把"兼容"做成了"新名失效"。
    """
    out = run([q_(price=10.1, high=10.25, low=9.9)], _rebound_state(),
              cfg={"windows": [180], "rebound_pct": 5.0,
                   "rebound_prior_drop_pct": 1.0})
    assert "rebound" not in patterns(out), (
        "只设了新名 rebound_pct=5.0（门槛高于 2.02%），却仍报出反弹 —— "
        "说明新名被 DEFAULTS 里的旧名默认值 2.0 压掉了")


def test_rebound_counterexample_drop_too_shallow():
    """跌得太浅（不构成"原来在跌"）-> 不报反弹。"""
    st = FakeState()
    st.feed("600000", [(T0 - 180, 10.00, 1000), (T0 - 90, 9.97, 1300),
                       (T0, 10.05, 1600)])
    q = q_(price=10.05, high=10.10, low=9.97, prev_close=10.0)
    out = run([q], st, cfg={"windows": [180], "rebound_pct": 0.5,
                            "rebound_prior_drop_pct": 1.0})
    assert "rebound" not in patterns(out)


def test_rebound_counterexample_off_low_insufficient():
    """跌过但只拉回一点点 -> 不报。"""
    st = FakeState()
    st.feed("600000", [(T0 - 180, 10.20, 1000), (T0 - 90, 9.90, 1300),
                       (T0, 9.92, 1600)])
    q = q_(price=9.92, high=10.25, low=9.90, prev_close=10.0)
    out = run([q], st, cfg={"windows": [180], "rebound_pct": 1.5,
                            "rebound_prior_drop_pct": 1.0})
    assert "rebound" not in patterns(out)


def test_rebound_needs_enough_points():
    """窗口内点太少 -> 不报（证据不足不猜）。"""
    st = FakeState()
    st.feed("600000", [(T0 - 3600, 10.2, 1000), (T0, 10.1, 2000)])
    q = q_(price=10.1, high=10.25, low=9.9)
    out = run([q], st, cfg={"windows": [180], "rebound_pct": 0.5,
                            "rebound_prior_drop_pct": 0.5})
    assert "rebound" not in patterns(out)


# ==========================================================================
# dive 高台跳水
# ==========================================================================
def test_dive_after_rise():
    """先涨后跌 -> 高台跳水。"""
    st = FakeState()
    st.feed("600000", [(T0 - 180, 10.0, 1000), (T0 - 120, 10.5, 1300),
                       (T0 - 60, 10.4, 1600), (T0, 10.2, 1900)])
    q = q_(price=10.2, high=10.5, low=10.0, prev_close=10.0)
    out = run([q], st, cfg={"windows": [180], "dive_prior_rise_pct": 2.0,
                            "dive_off_high_pct": 2.0})
    assert patterns(out) == ["dive"]
    assert out[0].kind is AlertKind.PLUNGE


def test_dive_counterexample_never_rose():
    """一路阴跌（没先涨过）-> 不是高台跳水。"""
    st = FakeState()
    st.feed("600000", [(T0 - 180, 10.0, 1000), (T0 - 90, 9.8, 1300),
                       (T0, 9.6, 1600)])
    q = q_(price=9.6, high=10.02, low=9.6, prev_close=10.0)
    out = run([q], st, cfg={"windows": [180], "dive_prior_rise_pct": 2.0,
                            "dive_off_high_pct": 2.0})
    assert "dive" not in patterns(out), "没涨过的下跌是加速下跌，不是跳水"


def test_dive_counterexample_shallow_retreat():
    """从高点只回落一点点 -> 不报。"""
    st = FakeState()
    st.feed("600000", [(T0 - 180, 10.0, 1000), (T0 - 90, 10.5, 1300),
                       (T0, 10.45, 1600)])
    q = q_(price=10.45, high=10.5, low=10.0, prev_close=10.0)
    out = run([q], st, cfg={"windows": [180], "dive_prior_rise_pct": 2.0,
                            "dive_off_high_pct": 3.0})
    assert "dive" not in patterns(out)


def test_dive_wins_over_accel_down():
    """先涨后跌的形态只报跳水，不同时报加速下跌。"""
    st = FakeState()
    st.feed("600000", [(T0 - 180, 10.0, 1000), (T0 - 135, 10.4, 1100),
                       (T0 - 90, 10.5, 1300), (T0 - 45, 10.2, 1500),
                       (T0, 10.0, 1800)])
    q = q_(price=10.0, high=10.5, low=10.0, prev_close=10.0)
    out = run([q], st, cfg={"windows": [180], "dive_prior_rise_pct": 2.0,
                            "dive_off_high_pct": 2.0, "accel_down_pct": -1.0,
                            "accel_ratio": 1.0, "accel_min_range_ratio": 0.0})
    assert patterns(out) == ["dive"]


# ==========================================================================
# accel_down 加速下跌
# ==========================================================================
def test_accel_down_slope_steepens():
    """全程下跌且后半段更陡 -> 加速下跌。"""
    st = FakeState()
    # 10.4 -> 10.2（前半 -1.9%）-> 9.9（后半再 -2.9%），总 -4.8%
    st.feed("600000", [(T0 - 180, 10.4, 1000), (T0 - 135, 10.35, 1100),
                       (T0 - 90, 10.2, 1300), (T0 - 45, 10.05, 1500),
                       (T0, 9.9, 1800)])
    q = q_(price=9.9, high=10.45, low=9.9, prev_close=10.0)
    out = run([q], st, cfg={"windows": [180], "accel_down_pct": -2.0,
                            "accel_ratio": 1.2, "accel_min_range_ratio": 0.3})
    assert patterns(out) == ["accel_down"]


def test_accel_down_counterexample_uniform_decline():
    """匀速下跌（斜率没变陡）-> 不报加速下跌。"""
    st = FakeState()
    st.feed("600000", [(T0 - 180, 10.4, 1000), (T0 - 90, 10.2, 1300),
                       (T0, 10.0, 1600)])
    q = q_(price=10.0, high=10.45, low=10.0, prev_close=10.0)
    out = run([q], st, cfg={"windows": [180], "accel_down_pct": -1.0,
                            "accel_ratio": 1.5, "accel_min_range_ratio": 0.0})
    assert "accel_down" not in patterns(out)


def test_accel_down_counterexample_first_half_rising():
    """前半段还在涨（不是"延续下跌"）-> 不属于加速下跌。"""
    st = FakeState()
    st.feed("600000", [(T0 - 180, 10.0, 1000), (T0 - 90, 10.3, 1300),
                       (T0, 10.05, 1600)])
    q = q_(price=10.05, high=10.35, low=10.0, prev_close=10.0)
    out = run([q], st, cfg={"windows": [180], "accel_down_pct": -1.0,
                            "accel_ratio": 1.0, "accel_min_range_ratio": 0.0})
    assert "accel_down" not in patterns(out)


def test_accel_down_counterexample_noise_in_flat_range():
    """横盘里的小波动 -> 不报（总跌幅占振幅比例不足）。"""
    st = FakeState()
    st.feed("600000", [(T0 - 180, 10.10, 1000), (T0 - 90, 10.08, 1300),
                       (T0, 10.05, 1600)])
    q = q_(price=10.05, high=10.60, low=9.90, prev_close=10.0)   # 振幅 7%
    out = run([q], st, cfg={"windows": [180], "accel_down_pct": -0.1,
                            "accel_ratio": 1.0, "accel_min_range_ratio": 0.5})
    assert "accel_down" not in patterns(out)


# ==========================================================================
# 互斥与排序
# ==========================================================================
def test_one_signal_per_stock_per_round():
    """一只票一轮只出一个价格信号（语义互斥，同时报等于自相矛盾）。"""
    st = FakeState()
    st.feed("600000", ramp("600000", start=10.0, end=10.5, seconds=180))
    q = q_(price=10.5, high=10.5, low=10.0)
    out = run([q], st, cfg={"windows": [180], "rocket_pct": 1.0,
                            "rebound_pct": 1.0, "rebound_prior_drop_pct": 0.1})
    assert len(out) == 1
    assert patterns(out) == ["rocket"], "rocket 优先"


def test_max_per_round():
    st = FakeState()
    quotes = []
    for i in range(6):
        code = f"60000{i}"
        st.feed(code, ramp(code, start=10.0, end=10.5, seconds=180))
        quotes.append(q_(code=code, price=10.5, high=10.5))
    out = run(quotes, st, cfg={"windows": [180], "rocket_pct": 1.0,
                               "max_per_round": 2})
    assert len(out) == 2


def test_max_per_round_zero_means_unlimited():
    st = FakeState()
    quotes = []
    for i in range(6):
        code = f"60000{i}"
        st.feed(code, ramp(code, start=10.0, end=10.5, seconds=180))
        quotes.append(q_(code=code, price=10.5, high=10.5))
    out = run(quotes, st, cfg={"windows": [180], "rocket_pct": 1.0,
                               "max_per_round": 0})
    assert len(out) == 6


def test_sorted_by_severity_desc():
    """severity 3 排在 severity 2 前面。"""
    st = FakeState()
    # 600001 窗口涨 10%（>= 2 倍阈值 1.0）-> sev3
    # 600002 窗口涨 1.5%（不足 2 倍）-> sev2
    st.feed("600001", ramp("600001", start=10.0, end=11.0, seconds=180))
    st.feed("600002", ramp("600002", start=10.0, end=10.15, seconds=180,
                           expo=1.0))
    a = q_(code="600001", price=11.0, high=11.0, low=10.0)
    b = q_(code="600002", price=10.15, high=10.15, low=10.0)
    out = run([b, a], st, cfg={"windows": [180], "rocket_pct": 1.0,
                               "urgent_multiple": 2.0})
    assert [x.code for x in out] == ["600001", "600002"]
    assert [x.severity for x in out] == [3, 2]


# ==========================================================================
# key 唯一性 / 冷却
# ==========================================================================
def test_keys_unique_across_signals_same_bucket():
    """四个信号在同一时间桶内 key 必须互不相同。

    回归背景：本项目曾因"触板与封板 key 相同"导致严重的封板告警被去重吞掉。
    """
    keys = set()
    for sig in SIGNALS:
        keys.add(f"600000:surge:{sig}:{bucket_of(T0, 300)}")
        keys.add(f"600000:plunge:{sig}:{bucket_of(T0, 300)}")
    assert len(keys) == len(SIGNALS) * 2


def test_key_format_and_cooldown_fields():
    st = FakeState()
    st.feed("600000", ramp("600000", start=10.0, end=10.5, seconds=180))
    q = q_(price=10.5, high=10.5)
    out = run([q], st, cfg={"windows": [180], "rocket_pct": 1.0})
    a = out[0]
    assert a.key == f"600000:surge:rocket:{bucket_of(T0, 300)}"
    assert a.cooldown_key == "600000:surge:rocket"
    assert a.cooldown_seconds == 300.0


def test_key_contains_pattern_segment():
    """key 必须含信号名分段，否则不同信号会同 key 互相吞掉。"""
    st = FakeState()
    st.feed("600000", ramp("600000", start=10.0, end=10.5, seconds=180))
    q = q_(price=10.5, high=10.5)
    a = run([q], st, cfg={"windows": [180], "rocket_pct": 1.0})[0]
    assert ":rocket:" in a.key


def _alert(key, ck):
    from arad.models import Alert
    return Alert(key=key, kind=AlertKind.SURGE, code="600000", name="x",
                 ts=datetime.fromtimestamp(T0), price=10.0, pct=1.0,
                 title="t", detail="d", cooldown_key=ck, cooldown_seconds=300.0)


def test_alertbus_does_not_swallow_two_signals():
    """模拟 AlertBus：rocket 与 dive 同桶内都该放行。"""
    from arad.engine import AlertBus, EngineState
    bus = AlertBus(EngineState())
    a1 = _alert("600000:surge:rocket:1", "600000:surge:rocket")
    a2 = _alert("000001:plunge:dive:1", "000001:plunge:dive")
    assert bus.accept(a1, T0) and bus.accept(a2, T0)


def test_metrics_payload():
    st = FakeState()
    st.feed("600000", ramp("600000", start=10.0, end=10.5, seconds=180))
    q = q_(price=10.5, high=10.5, low=10.0)
    a = run([q], st, cfg={"windows": [180], "rocket_pct": 1.0})[0]
    for k in ("pattern", "window_seconds", "window_pct", "signal_pct", "pct",
              "price", "high", "low", "vwap", "amplitude", "turnover", "amount"):
        assert k in a.metrics, k
    assert a.metrics["pattern"] == "rocket"
    assert a.metrics["window_seconds"] == 180.0


# ==========================================================================
# 边界
# ==========================================================================
def test_edge_cases_do_not_crash():
    st = FakeState()
    assert run([], st) == []
    assert run([q_(price=0.0, prev_close=0.0)], st) == []
    assert run([q_(price=0.0, prev_close=10.0)], st) == []
    assert run([q_(price=10.0, prev_close=0.0)], st) == []
    assert run([q_(price=10.5, high=10.5)], st) == []


def test_index_excluded():
    """指数不参与个股价格信号。"""
    st = FakeState()
    st.feed("000001", ramp("000001", start=3000.0, end=3100.0, seconds=180))
    idx = q_(code="000001", name="上证指数", prev_close=3000.0, price=3100.0,
             high=3100.0, low=3000.0, volume_lots=1e9)
    assert run([idx], st, cfg={"windows": [180], "rocket_pct": 1.0}) == []
    st.feed("399001", ramp("399001", start=10000.0, end=10300.0, seconds=180))
    idx2 = q_(code="399001", name="深证成指", prev_close=10000.0, price=10300.0,
              high=10300.0, low=10000.0, volume_lots=1e9)
    assert run([idx2], st, cfg={"windows": [180], "rocket_pct": 1.0}) == []


def test_stock_000001_still_works():
    """000001 若名称是平安银行，仍应正常参与价格信号（指数消歧反例）。"""
    st = FakeState()
    st.feed("000001", ramp("000001", start=10.0, end=10.5, seconds=180))
    q = q_(code="000001", name="平安银行", prev_close=10.0, price=10.5, high=10.5)
    assert patterns(run([q], st, cfg={"windows": [180], "rocket_pct": 1.0})) \
        == ["rocket"]


def test_deterministic():
    st = FakeState()
    st.feed("600000", ramp("600000", start=10.0, end=10.5, seconds=180))
    q = q_(price=10.5, high=10.5)
    cfg = {"windows": [180], "rocket_pct": 1.0}
    r1 = [a.key for a in run([q], st, cfg=cfg)]
    r2 = [a.key for a in run([q], st, cfg=cfg)]
    assert r1 == r2


def test_performance_1000_stocks():
    """1000 只股票单轮求值 < 200ms。"""
    st = FakeState()
    quotes = []
    for i in range(1000):
        code = f"{600000 + i:06d}"
        st.feed(code, ramp(code, start=10.0, end=10.5, seconds=180))
        quotes.append(q_(code=code, price=10.5, high=10.5))
    rule = build({"enabled": True, "windows": [180], "rocket_pct": 1.0,
                  "max_per_round": 0})
    snap = snap_of(*quotes)
    ctx = mk_ctx(st)
    t0 = time.perf_counter()
    out = rule.evaluate(snap, ctx)
    el = (time.perf_counter() - t0) * 1000.0
    assert len(out) == 1000, "max_per_round=0 应不限量"
    assert el < 200.0, f"耗时 {el:.1f}ms 超出 200ms 预算"
