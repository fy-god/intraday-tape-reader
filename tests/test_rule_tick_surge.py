"""急拉急跌规则测试（离线，用极简 FakeState，不依赖 engine）。"""
from __future__ import annotations

from datetime import datetime

import pytest

from arad.models import AlertKind, Quote, Snapshot, board_of
from arad.rules.base import RuleContext, bucket_of
from arad.rules.tick_surge import TickSurgeRule, build
from arad.session import SessionPhase

from fakes import make_quote

T0 = 1_800_000_000.0        # 固定基准时间戳


class FakeState:
    """实现规则用到的 4 个接口：window / price_change / volume_delta / history。"""

    def __init__(self):
        self.history: dict[str, list[tuple[float, float, float]]] = {}
        self.quotes: dict[str, Quote] = {}

    def feed(self, code: str, points):
        """points: [(ts_epoch, price, cum_volume_lots)]"""
        self.history[code] = list(points)

    def window(self, code, seconds, now_epoch):
        pts = self.history.get(code, [])
        cutoff = now_epoch - float(seconds)
        return [p for p in pts if p[0] >= cutoff]

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


def mk_ctx(state, *, session=SessionPhase.MORNING, now_ep=T0, cfg=None):
    return RuleContext(
        state=state,
        cfg=cfg or {},
        now=datetime.fromtimestamp(now_ep),
        session=session,
        elapsed_trading_seconds=1800.0,
        minutes_to_close=120.0,
    )


def ramp(code, *, start_price, end_price, seconds=180, step=15, start_vol=1000,
         vol_step=200, accel=True):
    """构造一段走势序列（默认每 15 秒一个点）。

    ``accel=True`` 时价格路径为**凸函数**（越涨越快），这才是真实的"急拉"；
    ``accel=False`` 为匀速直线（用于验证加速度过滤会拦掉非加速行情）。
    """
    pts = []
    n = int(seconds // step)
    expo = 1.6 if accel else 1.0
    for i in range(n + 1):
        frac = (i / n) ** expo
        price = start_price + (end_price - start_price) * frac
        # 成交量同样加速放大（真实急拉伴随量能脉冲），否则会被量能确认拦掉
        vol = start_vol + vol_step * (i ** 1.7)
        pts.append((T0 - seconds + i * step, price, vol))
    return pts


def snap_of(q: Quote) -> Snapshot:
    return Snapshot(ts=datetime.fromtimestamp(T0), seq=1, quotes={q.code: q})


# ==========================================================================
# 正例
# ==========================================================================
def test_surge_detected_typical():
    """3 分钟内 +4%，站上均价、量能放大 -> 命中 SURGE。"""
    st = FakeState()
    pts = ramp("600000", start_price=10.0, end_price=10.4, seconds=180,
               start_vol=1000, vol_step=400)
    st.feed("600000", pts)
    q = make_quote(code="600000", price=10.4, prev_close=10.0, open=10.0,
                   high=10.4, low=9.98, volume_lots=pts[-1][2],
                   amount=pts[-1][2] * 100 * 10.2, turnover=3.0)
    rule = build({})
    alerts = rule.evaluate(snap_of(q), mk_ctx(st))
    assert len(alerts) == 1
    a = alerts[0]
    assert a.kind is AlertKind.SURGE
    assert "急拉" in a.title
    assert a.code == "600000"
    assert a.metrics["window_pct"] >= 3.0
    assert a.severity in (2, 3)


def test_plunge_detected_typical():
    """1 分钟内 -2.5%，跌破均价 -> 命中 PLUNGE。"""
    st = FakeState()
    pts = ramp("600000", start_price=10.0, end_price=9.75, seconds=60,
               step=10, start_vol=1000, vol_step=500)
    st.feed("600000", pts)
    q = make_quote(code="600000", price=9.75, prev_close=10.0, open=10.0,
                   high=10.0, low=9.75, volume_lots=pts[-1][2],
                   amount=pts[-1][2] * 100 * 9.9)
    rule = build({})
    alerts = rule.evaluate(snap_of(q), mk_ctx(st))
    assert len(alerts) == 1
    a = alerts[0]
    assert a.kind is AlertKind.PLUNGE
    assert "急跌" in a.title
    assert a.metrics["window_pct"] <= -2.0


def test_multiple_windows_hit_only_one_alert():
    """多窗口同时命中只出一条，且取比值最大的窗口。"""
    st = FakeState()
    # 300 秒内涨 8%：60s/180s/300s 多个窗口命中
    pts = ramp("600000", start_price=10.0, end_price=10.8, seconds=300,
               step=15, start_vol=1000, vol_step=200)
    st.feed("600000", pts)
    q = make_quote(code="600000", price=10.8, prev_close=10.0, open=10.0,
                   high=10.8, low=9.98, volume_lots=pts[-1][2],
                   amount=pts[-1][2] * 100 * 10.4)
    rule = build({})
    alerts = rule.evaluate(snap_of(q), mk_ctx(st))
    assert len(alerts) == 1
    # 命中窗口应确实满足各自阈值
    win = alerts[0].metrics["window_seconds"]
    change = alerts[0].metrics["window_pct"]
    thresholds = {60: 2.0, 180: 3.0, 300: 4.0}
    assert change >= thresholds[win]


def test_evaluate_with_hits_reports_all_windows():
    st = FakeState()
    st.feed("600000", ramp("600000", start_price=10.0, end_price=10.8, seconds=300,
                           step=15, vol_step=200))
    q = make_quote(code="600000", price=10.8, prev_close=10.0,
                   volume_lots=5000, amount=5000 * 100 * 10.4)
    rule = build({})
    alerts = rule.evaluate_with_hits(snap_of(q), mk_ctx(st))
    assert len(alerts) == 1
    assert alerts[0].metrics["hits"] >= 1


# ==========================================================================
# 过滤条件
# ==========================================================================
def test_vwap_filter_blocks_surge_below_vwap():
    """急拉但价格低于分时均价 -> 过滤（诱多）。"""
    st = FakeState()
    pts = ramp("600000", start_price=10.0, end_price=10.4, seconds=180, vol_step=400)
    st.feed("600000", pts)
    # 均价远高于现价（成交额虚高）
    q = make_quote(code="600000", price=10.4, prev_close=10.0, open=10.0,
                   high=10.4, low=9.98, volume_lots=1000, amount=1000 * 100 * 11.0)
    rule = build({})
    assert rule.evaluate(snap_of(q), mk_ctx(st)) == []


def test_vwap_filter_can_be_disabled():
    st = FakeState()
    pts = ramp("600000", start_price=10.0, end_price=10.4, seconds=180, vol_step=400)
    st.feed("600000", pts)
    q = make_quote(code="600000", price=10.4, prev_close=10.0, open=10.0,
                   high=10.4, low=9.98, volume_lots=1000, amount=1000 * 100 * 11.0)
    rule = build({"require_above_vwap": False})
    assert len(rule.evaluate(snap_of(q), mk_ctx(st))) == 1


def test_volume_filter_blocks_weak_volume():
    """窗口量能相对前一窗口不足 -> 被量能确认拦下。

    注意：时间戳必须严格单调、不能有重复点——窗口取中点算前后半段，
    边界上的重复点会把中点算错。
    """
    st = FakeState()
    n = 24                      # 360s / 15s
    g = 1.08 ** (1.0 / n)       # 全程等比 +8% -> 前后半段涨幅相等
    pts = []
    for i in range(n + 1):
        price = 10.0 * (g ** i)
        # 累计成交量：前 180s 放量到 6 万手，后 180s 几乎枯竭（+240 手）
        cum = 1000 + (59000 * i / 12) if i <= 12 else 60000 + (i - 12) * 20
        pts.append((T0 - 360 + i * 15, price, cum))
    st.feed("600000", pts)
    q = make_quote(code="600000", price=pts[-1][1], prev_close=10.0, open=10.0,
                   high=pts[-1][1], low=10.0, volume_lots=pts[-1][2],
                   amount=pts[-1][2] * 100 * pts[-1][1] * 0.98)
    ctx = mk_ctx(st)
    # 只留 180s 窗口，隔离"更长的 300s 窗口因前一窗口为空而放行"的干扰
    only180 = {"windows": [{"seconds": 180, "pct": 3.0}]}
    # 前一窗口 59000 手 vs 当前窗口 240 手
    assert build(only180)._volume_ratio(ctx, "600000", 180, T0) < 0.05
    assert build(only180).evaluate(snap_of(q), ctx) == []
    # 关闭量能校验 -> 价格与加速度条件均满足，应告警
    assert len(build({**only180, "volume_confirm_ratio": 0}).evaluate(snap_of(q), ctx)) == 1


def test_volume_filter_passes_when_first_window_empty():
    """开盘第一分钟前一窗口为 0 -> 放行，不误杀。"""
    st = FakeState()
    pts = ramp("600000", start_price=10.0, end_price=10.3, seconds=60,
               step=10, start_vol=0, vol_step=100)
    st.feed("600000", pts)
    q = make_quote(code="600000", price=10.3, prev_close=10.0, open=10.0,
                   high=10.3, low=10.0, volume_lots=pts[-1][2],
                   amount=pts[-1][2] * 100 * 10.1)
    rule = build({})
    assert len(rule.evaluate(snap_of(q), mk_ctx(st))) == 1


def test_accel_filter():
    """前半段已涨很多、后半段滞涨 -> 加速度不足被过滤。"""
    st = FakeState()
    # 前 90 秒涨 3%，后 90 秒只涨 0.05%
    pts = [(T0 - 180 + i * 15, 10.0 + i * 0.05, 1000 + i * 500) for i in range(7)]
    pts += [(T0 - 90 + i * 15, 10.3 + i * 0.008, 4000 + i * 500) for i in range(7)]
    st.feed("600000", pts)
    q = make_quote(code="600000", price=10.348, prev_close=10.0, open=10.0,
                   high=10.35, low=10.0, volume_lots=pts[-1][2],
                   amount=pts[-1][2] * 100 * 10.15)
    rule = build({"accel_ratio": 1.0})
    assert rule.evaluate(snap_of(q), mk_ctx(st)) == []


# ==========================================================================
# 边界
# ==========================================================================
def test_no_alert_when_insufficient_history():
    st = FakeState()
    st.feed("600000", [(T0, 10.0, 1000)])
    q = make_quote(code="600000", price=10.4, prev_close=10.0)
    assert build({}).evaluate(snap_of(q), mk_ctx(st)) == []


def test_no_alert_outside_continuous_session():
    st = FakeState()
    st.feed("600000", ramp("600000", start_price=10.0, end_price=10.4, seconds=180, vol_step=400))
    q = make_quote(code="600000", price=10.4, prev_close=10.0,
                   volume_lots=4000, amount=4000 * 100 * 10.2)
    for phase in (SessionPhase.CLOSED, SessionPhase.LUNCH,
                  SessionPhase.PRE_OPEN, SessionPhase.POST):
        assert build({}).evaluate(snap_of(q), mk_ctx(st, session=phase)) == []
    # 关闭该开关后应能告警
    rule = build({"only_continuous": False})
    assert len(rule.evaluate(snap_of(q), mk_ctx(st, session=SessionPhase.LUNCH))) == 1


def test_suspended_and_invalid_quotes_skipped():
    st = FakeState()
    st.feed("600000", ramp("600000", start_price=10.0, end_price=10.4, seconds=180, vol_step=400))
    dead = make_quote(code="600000", price=0.0, prev_close=0.0, volume_lots=0)
    assert build({}).evaluate(snap_of(dead), mk_ctx(st)) == []


def test_negative_change_does_not_trigger_surge():
    st = FakeState()
    st.feed("600000", ramp("600000", start_price=10.4, end_price=10.0, seconds=180, vol_step=400))
    q = make_quote(code="600000", price=10.0, prev_close=10.0, open=10.4,
                   high=10.4, low=10.0, volume_lots=4000,
                   amount=4000 * 100 * 10.2)
    alerts = build({}).evaluate(snap_of(q), mk_ctx(st))
    assert all(a.kind is not AlertKind.SURGE for a in alerts)


def test_severity_escalates_when_extreme():
    """涨幅达到阈值 2 倍 -> severity 3。"""
    st = FakeState()
    pts = ramp("600000", start_price=10.0, end_price=10.5, seconds=60, step=10,
               start_vol=1000, vol_step=800)   # 60s 内 +5% >= 2.0*2.0
    st.feed("600000", pts)
    q = make_quote(code="600000", price=10.5, prev_close=10.0, open=10.0,
                   high=10.5, low=10.0, volume_lots=pts[-1][2],
                   amount=pts[-1][2] * 100 * 10.2)
    alerts = build({}).evaluate(snap_of(q), mk_ctx(st))
    assert len(alerts) == 1
    assert alerts[0].severity == 3
    assert alerts[0].metrics["window_seconds"] == 60.0


def test_severity_stays_normal_when_mild():
    """刚好越过阈值（不足 2 倍）-> severity 保持 2。"""
    st = FakeState()
    pts = ramp("600000", start_price=10.0, end_price=10.25, seconds=60, step=10,
               start_vol=1000, vol_step=200)   # 60s 内 +2.5%，< 4.0%
    st.feed("600000", pts)
    q = make_quote(code="600000", price=10.25, prev_close=10.0, open=10.0,
                   high=10.25, low=10.0, volume_lots=pts[-1][2],
                   amount=pts[-1][2] * 100 * 10.1)
    alerts = build({}).evaluate(snap_of(q), mk_ctx(st))
    assert len(alerts) == 1
    assert alerts[0].severity == 2


def test_limit_up_price_still_alerts_with_correct_to_limit():
    """涨停附近急拉仍告警，且 to_limit_pct 正确。"""
    st = FakeState()
    pts = ramp("600000", start_price=10.5, end_price=11.0, seconds=180,
               vol_step=400, start_vol=2000)
    st.feed("600000", pts)
    q = make_quote(code="600000", price=11.0, prev_close=10.0, open=10.0,
                   high=11.0, low=10.0, volume_lots=pts[-1][2],
                   amount=pts[-1][2] * 100 * 10.7)
    alerts = build({}).evaluate(snap_of(q), mk_ctx(st))
    assert len(alerts) == 1
    assert alerts[0].metrics["to_limit_pct"] == pytest.approx(0.0, abs=0.01)


def _plunge_case(price=9.70):
    """10.0 -> price 的一分钟急跌行情。"""
    st = FakeState()
    pts = ramp("600000", start_price=10.0, end_price=price, seconds=60,
               step=10, start_vol=1000, vol_step=500)
    st.feed("600000", pts)
    q = make_quote(code="600000", price=price, prev_close=10.0, open=10.00,
                   high=10.20, low=9.65, volume_lots=pts[-1][2],
                   amount=pts[-1][2] * 100 * 9.85)
    return build({}).evaluate(snap_of(q), mk_ctx(st))


def test_plunge_reports_distance_to_limit_down_not_limit_up():
    """急跌告警必须报「距跌停」，不能报「距涨停」。

    这是一条真实的用户可见缺陷：``_mk`` 只算涨停口径的 ``to_limit``，
    且急拉/急跌**共用同一行 detail**，于是急跌告警里印着
    「距涨停 +13.40%」。一条 ``kind=plunge``、标题为负的告警带着「+」号，
    在 A 股语境里「+」读作"涨"，与告警方向正好相反；
    而用户真正需要的「距跌停」在整条告警里根本不存在。

    数字本身都没算错，错的是**方向**。
    """
    alerts = _plunge_case(9.70)
    assert len(alerts) == 1
    a = alerts[0]
    assert a.kind is AlertKind.PLUNGE

    # (9.70 / 9.00 - 1) * 100 = +7.777…%
    assert a.metrics["to_limit_pct"] == pytest.approx(7.78, abs=0.01), (
        f"急跌应报距跌停 +7.78%，实际 {a.metrics['to_limit_pct']}")
    assert "距跌停" in a.detail, a.detail
    assert "距涨停" not in a.detail, (
        f"急跌告警里出现了「距涨停」：{a.detail!r}")

    # 涨停口径的 +13.40% 绝不能出现在这条告警里
    assert "+13.40" not in a.detail, a.detail
    assert a.metrics["to_limit_pct"] != pytest.approx(13.40, abs=0.01)


def test_surge_still_reports_distance_to_limit_up():
    """对照组：急拉仍然报「距涨停」—— 那里是对的，不能被一起改坏。

    现价 10.90，涨停价 11.00 -> (11.00/10.90-1)*100 = +0.92%
    """
    st = FakeState()
    pts = ramp("600000", start_price=10.0, end_price=10.9, seconds=180,
               step=15, start_vol=1000, vol_step=300)
    st.feed("600000", pts)
    q = make_quote(code="600000", price=10.90, prev_close=10.0, open=10.00,
                   high=10.95, low=10.00, volume_lots=pts[-1][2],
                   amount=pts[-1][2] * 100 * 10.45)
    alerts = build({}).evaluate(snap_of(q), mk_ctx(st))
    assert alerts and alerts[0].kind is AlertKind.SURGE
    a = alerts[0]
    assert a.metrics["to_limit_pct"] == pytest.approx(0.92, abs=0.02)
    assert "距涨停" in a.detail, a.detail
    assert "距跌停" not in a.detail, a.detail


def test_to_limit_is_omitted_when_limit_price_unavailable():
    """拿不到真实限价时**省略**该字段，而不是打 0.00% 假装"贴着了"。

    ⚠ 这是一个**防御性**分支，从 ``evaluate()`` 走不到：能算出 0 限价的
    只有 ``prev_close <= 0``，而 ``Quote.is_suspended`` 恰好把这种行情
    判为停牌并跳过。所以这里直接调 ``_make_alert`` 验渲染逻辑本身 ——
    而不是伪造一条 evaluate 能过的行情（那样只是自欺）。

    为什么仍然值得保留：``Quote.limit_up_price`` 会回落到
    ``prev_close * (1 ± rate)``，一旦哪天这个兜底改了、或 is_suspended
    的判据放宽，旧写法 ``to_limit = 0.0`` 就会印出「距涨停 +0.00%」——
    看上去像"已经涨停了"，是危险的误导。宁可省略这个字段。
    """
    st = FakeState()
    pts = ramp("600000", start_price=10.0, end_price=9.70, seconds=60,
               step=10, start_vol=1000, vol_step=500)
    st.feed("600000", pts)
    q = make_quote(code="600000", price=9.70, prev_close=0.0, open=9.70,
                   high=9.70, low=9.70, volume_lots=pts[-1][2],
                   amount=pts[-1][2] * 100 * 9.85)
    assert q.limit_up_price == 0.0 and q.limit_down_price == 0.0, (
        "前提：prev_close=0 时两个限价都应为 0")

    rule = build({})
    a = rule._make_alert(q, mk_ctx(st), 60.0, -3.0, AlertKind.PLUNGE, None)

    assert "距跌停" not in a.detail, a.detail
    assert "距涨停" not in a.detail, a.detail
    assert a.metrics["to_limit_pct"] == -1.0, "取不到限价时应为 -1.0（数据不足）"
    # 其余信息不能因为省略这个字段而丢
    assert "最高" in a.detail and "最低" in a.detail
    # 省略后也不能留下"开头就是空格"这种残缺排版
    last = a.detail.splitlines()[-1]
    assert last.startswith("最高"), f"末行排版残缺：{last!r}"


def test_cooldown_bucket_is_stable_within_window():
    st = FakeState()
    st.feed("600000", ramp("600000", start_price=10.0, end_price=10.4, seconds=180, vol_step=400))
    q = make_quote(code="600000", price=10.4, prev_close=10.0,
                   volume_lots=4000, amount=4000 * 100 * 10.2)
    rule = build({"cooldown_seconds": 300})
    a1 = rule.evaluate(snap_of(q), mk_ctx(st, now_ep=T0))[0]
    a2 = rule.evaluate(snap_of(q), mk_ctx(st, now_ep=T0 + 10))[0]
    assert a1.key == a2.key
    assert a1.key == f"600000:surge:{bucket_of(T0, 300)}"


def test_max_per_round_and_sorting():
    """限量为 2 时只取幅度最大的两只，且降序。"""
    st = FakeState()
    quotes = {}
    for i, end in enumerate([10.2, 10.6, 10.4]):
        code = f"60000{i}"
        st.feed(code, ramp(code, start_price=10.0, end_price=end, seconds=180, vol_step=400))
        quotes[code] = make_quote(code=code, price=end, prev_close=10.0, open=10.0,
                                  high=end, low=10.0, volume_lots=4000,
                                  amount=4000 * 100 * ((10.0 + end) / 2))
    snap = Snapshot(ts=datetime.fromtimestamp(T0), seq=1, quotes=quotes)
    rule = build({"max_per_round": 2})
    alerts = rule.evaluate(snap, mk_ctx(st))
    assert len(alerts) == 2
    assert alerts[0].metrics["window_pct"] >= alerts[1].metrics["window_pct"]
    assert alerts[0].code == "600001"      # +6% 最强


def test_direction_is_exclusive_per_stock():
    """同一只股票不会同时出急拉和急跌。"""
    st = FakeState()
    st.feed("600000", ramp("600000", start_price=10.0, end_price=10.4, seconds=180, vol_step=400))
    q = make_quote(code="600000", price=10.4, prev_close=10.0,
                   volume_lots=4000, amount=4000 * 100 * 10.2)
    alerts = build({}).evaluate(snap_of(q), mk_ctx(st))
    kinds = {a.kind for a in alerts}
    assert not ({AlertKind.SURGE, AlertKind.PLUNGE} <= kinds)


def test_empty_snapshot_returns_empty():
    st = FakeState()
    snap = Snapshot(ts=datetime.fromtimestamp(T0), seq=1, quotes={})
    assert build({}).evaluate(snap, mk_ctx(st)) == []


def test_metrics_keys_present():
    st = FakeState()
    st.feed("600000", ramp("600000", start_price=10.0, end_price=10.4, seconds=180, vol_step=400))
    q = make_quote(code="600000", price=10.4, prev_close=10.0, volume_lots=4000,
                   amount=4000 * 100 * 10.2, turnover=2.5)
    a = build({}).evaluate(snap_of(q), mk_ctx(st))[0]
    for k in ("window_pct", "window_seconds", "pct", "price", "vwap", "vol_ratio",
              "amount", "turnover", "amplitude", "to_limit_pct", "hits"):
        assert k in a.metrics, k
    assert a.detail.count("\n") >= 2       # 多行明细


def geometric(code, *, start_price, total_pct, seconds=180, step=15, start_vol=1000,
              vol_step=200):
    """等比路径：每步涨幅相同（前后半段涨幅完全相等）。

    注意：**等价差**的直线路径并不能带来相等涨幅——10.0→10.25 是 +2.50%，
    但 10.25→10.5 只有 +2.44%（基数变了）。所以只有等比路径才满足
    "后半段涨幅 == 前半段涨幅"。
    """
    n = int(seconds // step)
    r = (1.0 + total_pct / 100.0) ** (1.0 / n)
    pts = []
    for i in range(n + 1):
        price = start_price * (r ** i)
        vol = start_vol + vol_step * (i ** 1.7)
        pts.append((T0 - seconds + i * step, price, vol))
    return pts


def test_linear_ramp_rejected_by_accel_filter():
    """等价差直线上涨：后半段涨幅因基数变大而衰减 -> 不算急拉。

    真实"急拉"是越涨越快（凸函数），匀速爬升的百分比涨幅天然递减，
    默认 accel_ratio=1.0 就会把它拦掉。
    """
    st = FakeState()
    pts = ramp("600000", start_price=10.0, end_price=10.5, seconds=180,
               vol_step=400, accel=False)
    st.feed("600000", pts)
    q = make_quote(code="600000", price=10.5, prev_close=10.0, open=10.0,
                   high=10.5, low=10.0, volume_lots=pts[-1][2],
                   amount=pts[-1][2] * 100 * 10.25)
    assert build({}).evaluate(snap_of(q), mk_ctx(st)) == []
    # 关闭加速度校验 -> 同样的行情应被识别
    assert len(build({"accel_ratio": 0}).evaluate(snap_of(q), mk_ctx(st))) == 1


def test_geometric_ramp_satisfies_accel_at_one():
    """等比上涨：前后半段涨幅相等 -> accel_ratio=1.0 放行，1.5 拦下。"""
    st = FakeState()
    pts = geometric("600000", start_price=10.0, total_pct=6.0, seconds=180,
                    vol_step=400)
    st.feed("600000", pts)
    end = pts[-1][1]
    q = make_quote(code="600000", price=end, prev_close=10.0, open=10.0,
                   high=end, low=10.0, volume_lots=pts[-1][2],
                   amount=pts[-1][2] * 100 * 10.3)
    assert len(build({"accel_ratio": 1.0}).evaluate(snap_of(q), mk_ctx(st))) == 1
    assert build({"accel_ratio": 1.5}).evaluate(snap_of(q), mk_ctx(st)) == []


def test_window_requires_history_coverage():
    """只有 1 分钟数据时，不得声称"5 分钟急拉"。"""
    st = FakeState()
    # 只喂 60 秒数据，涨 5%
    pts = ramp("600000", start_price=10.0, end_price=10.5, seconds=60,
               step=10, start_vol=1000, vol_step=800)
    st.feed("600000", pts)
    q = make_quote(code="600000", price=10.5, prev_close=10.0, open=10.0,
                   high=10.5, low=10.0, volume_lots=pts[-1][2],
                   amount=pts[-1][2] * 100 * 10.2)
    alerts = build({}).evaluate(snap_of(q), mk_ctx(st))
    assert len(alerts) == 1
    # 只能命中 60 秒窗口，不能是 180/300
    assert alerts[0].metrics["window_seconds"] == 60.0


def test_performance_1000_stocks():
    """1000 只股票单次 evaluate 应远快于 200ms。

    `IT-P1-WALLCLOCK-BOUND-IS-A-CORRECTNESS-GATE-001`（云端 §2.1）：
    改用**中位数**采样，单次环境停顿不再翻转退出码。
    云端实测此处中位 **5.621ms**、最大 6.913ms，余量 **35.58x**。
    """
    from conftest import median_elapsed

    st = FakeState()
    quotes = {}
    for i in range(1000):
        code = f"{600000 + i}"
        st.feed(code, ramp(code, start_price=10.0, end_price=10.2, seconds=180, vol_step=50))
        quotes[code] = make_quote(code=code, price=10.2, prev_close=10.0,
                                  volume_lots=2000, amount=2000 * 100 * 10.1)
    snap = Snapshot(ts=datetime.fromtimestamp(T0), seq=1, quotes=quotes)
    rule = build({})
    ctx = mk_ctx(st)
    rule.evaluate(snap, ctx)                    # 预热（去掉首次分配开销）
    elapsed = median_elapsed(lambda: rule.evaluate(snap, ctx), repeat=7)
    assert elapsed < 0.2, f"evaluate 中位太慢: {elapsed * 1000:.1f}ms"


def test_evaluate_with_hits_reports_all_windows():
    st = FakeState()
    st.feed("600000", ramp("600000", start_price=10.0, end_price=10.8, seconds=300,
                           step=15, vol_step=200))
    q = make_quote(code="600000", price=10.8, prev_close=10.0,
                   volume_lots=5000, amount=5000 * 100 * 10.4)
    rule = build({})
    alerts = rule.evaluate_with_hits(snap_of(q), mk_ctx(st))
    assert len(alerts) == 1
    assert alerts[0].metrics["hits"] >= 1
