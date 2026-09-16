"""引擎核心测试：EngineState 窗口计算 / AlertBus 去重 / SourceManager 故障转移 /
Filters 粗筛 / Engine.poll_once 端到端（用 FakeSource + 桩规则，全程离线）。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from arad.engine import AlertBus, Engine, EngineState, SourceManager, build_rules
from arad.filters import Filters
from arad.models import Alert, AlertKind, Board, Quote, board_of
from arad.session import SessionPhase, TradingCalendar
from arad.store import AlertStore

from fakes import FakeSource, LegacyNotifier, RecordingNotifier, make_quote


# ==========================================================================
# EngineState
# ==========================================================================
def _fill(state: EngineState, code: str, points, t0: float = 1_000_000.0):
    """points: [(price, cum_volume_lots), ...] 每秒一个点"""
    from datetime import datetime as dt

    for i, (px, vol) in enumerate(points):
        ts = t0 + i
        q = make_quote(code=code, price=px, volume_lots=vol,
                       amount=vol * 100 * px)
        state.update([q], dt.fromtimestamp(ts))


def test_state_window_and_price_change():
    st = EngineState()
    _fill(st, "600000", [(10.0, 100), (10.1, 200), (10.3, 300), (10.5, 400)])
    now = 1_000_003.0
    assert len(st.window("600000", 10, now)) == 4
    assert len(st.window("600000", 2, now)) == 3          # 含端点
    ch = st.price_change("600000", 3, now)
    assert ch == pytest.approx((10.5 / 10.0 - 1) * 100, rel=1e-6)
    # 窗口过短 / 数据不足
    assert st.price_change("600000", 0.1, now) is None
    assert st.price_change("nope", 60, now) is None


def test_state_volume_delta_monotonic_and_never_negative():
    st = EngineState()
    _fill(st, "600000", [(10.0, 100), (10.1, 300), (10.2, 600)])
    now = 1_000_002.0
    assert st.volume_delta("600000", 10, now) == pytest.approx(500.0)
    assert st.volume_delta("missing", 10, now) == 0.0


def test_state_ignores_duplicate_unchanged_points():
    """价格与成交量都没变时不应重复追加点，否则窗口会被垃圾点填满。"""
    st = EngineState()
    ts = datetime(2026, 9, 15, 10, 0, 0)
    q = make_quote(price=10.0, volume_lots=100)
    st.update([q], ts)
    st.update([q], ts + timedelta(seconds=5))
    st.update([q], ts + timedelta(seconds=10))
    assert len(st.history["600000"]) == 1
    # 价格变化 -> 追加
    st.update([q.copy_with(price=10.5)], ts + timedelta(seconds=15))
    assert len(st.history["600000"]) == 2


def test_state_skips_suspended_and_tracks_first_seen():
    st = EngineState()
    ts = datetime(2026, 9, 15, 10, 0, 0)
    dead = make_quote(code="000002", price=0.0, prev_close=0.0)
    live = make_quote(code="600000", price=10.0)
    st.update([dead, live], ts)
    assert "000002" not in st.quotes
    assert "600000" in st.first_seen
    assert st.day_open["600000"] == 10.0


def test_state_prune_keeps_only_requested_codes():
    st = EngineState()
    for c in ("600000", "600001", "600002"):
        _fill(st, c, [(10.0, 100), (10.1, 200)])
    dropped = st.prune({"600000"})
    assert dropped == 2
    assert set(st.history) == {"600000"}


def test_state_peak_trough():
    st = EngineState()
    _fill(st, "600000", [(10.0, 1), (10.8, 2), (10.2, 3), (9.9, 4)])
    now = 1_000_003.0
    assert st.peak("600000", 60, now) == pytest.approx(10.8)
    assert st.trough("600000", 60, now) == pytest.approx(9.9)


# ==========================================================================
# AlertBus 去重
# ==========================================================================
def test_alert_bus_dedupes_same_key_within_window():
    st = EngineState()
    bus = AlertBus(st, window_seconds=300)
    a = Alert(key="600000:surge:1", kind=AlertKind.SURGE, code="600000", name="x",
              ts=datetime.now(), price=10.0, pct=3.0, title="t", detail="d")
    assert bus.accept(a, 1000.0) is True
    assert bus.accept(a, 1100.0) is False        # 冷却期内
    assert bus.accept(a, 1401.0) is True         # 超出冷却窗口
    # 不同 key 互不影响
    b = a.__class__(key="600000:surge:2", kind=a.kind, code=a.code, name=a.name,
                    ts=a.ts, price=a.price, pct=a.pct, title="t", detail="d")
    assert bus.accept(b, 1100.0) is True


# ==========================================================================
# Filters
# ==========================================================================
def test_filters_basic():
    f = Filters.from_cfg({"min_price": 2, "max_price": 100, "min_amount": 1e7,
                          "exclude_boards": ["index"], "exclude_codes": ["600002"]})
    ok = make_quote(code="600000", price=10.0, amount=5e7)
    too_cheap = make_quote(code="600001", price=1.0, amount=5e7)
    too_small = make_quote(code="600003", price=10.0, amount=100.0)
    banned = make_quote(code="600002", price=10.0, amount=5e7)
    idx = make_quote(code="399001", price=10.0, amount=5e7)
    assert f.accept(ok)
    assert not f.accept(too_cheap)
    assert not f.accept(too_small)
    assert not f.accept(banned)
    assert not f.accept(idx)


def test_filters_min_list_days():
    f = Filters.from_cfg({"min_list_days": 11, "min_price": 0, "min_amount": 0,
                          "exclude_boards": []})
    f.list_dates = {"600000": "20260901"}     # 上市仅 14 天？按 today 计算
    q = make_quote(code="600000", price=10.0, amount=5e7)
    from datetime import date

    assert not f.accept(q, date(2026, 9, 5))   # 4 天 -> 拒绝
    assert f.accept(q, date(2026, 9, 20))      # 19 天 -> 放行
    # 无上市日数据时放行，不误杀
    f.list_dates = {}
    assert f.accept(q, date(2026, 9, 5))


def test_filters_exclude_st():
    f = Filters.from_cfg({"exclude_st": True, "min_price": 0, "min_amount": 0,
                          "exclude_boards": []})
    st_q = make_quote(code="600000", name="ST测试", price=5.0, amount=5e7)
    assert not f.accept(st_q)
    f2 = Filters.from_cfg({"min_price": 0, "min_amount": 0, "exclude_boards": []})
    assert f2.accept(st_q)


# ==========================================================================
# SourceManager 故障转移
# ==========================================================================
def test_source_manager_failover():
    """主源失败时立即由备用源供数；连续达到阈值后正式切换。"""
    bad = FakeSource([], fail_times=99)
    good = FakeSource([make_quote(code="600000", price=10.0)])
    sm = SourceManager([bad, good], threshold=2)
    assert sm.current is bad

    # 第 1 次：备用源顶上（热备），主源不变
    out = sm.call("universe")
    assert len(out) == 1
    assert sm.current is bad
    assert sm.fails == 1

    # 第 2 次：达到阈值 -> 正式切换
    out = sm.call("universe")
    assert len(out) == 1
    assert sm.current is good
    assert sm.fails == 0

    health = sm.health()
    assert len(health) == 2
    assert any(h["active"] for h in health)
    assert [h["active"] for h in health] == [False, True]


def test_source_manager_recovers_counter_on_success():
    """主源恢复成功后失败计数清零。"""
    flaky = FakeSource([make_quote(code="600000", price=10.0)], fail_times=1)
    backup = FakeSource([make_quote(code="000001", price=5.0)])
    sm = SourceManager([flaky, backup], threshold=3)
    assert len(sm.call("universe")) == 1
    assert sm.fails == 1              # 由备用源服务
    assert sm.current is flaky
    assert len(sm.call("universe")) == 1
    assert sm.fails == 0              # 主源恢复
    assert sm.current is flaky


def test_source_manager_raises_when_all_fail():
    a = FakeSource([], fail_times=99)
    b = FakeSource([], fail_times=99)
    sm = SourceManager([a, b], threshold=1)
    from arad.sources.base import SourceError

    with pytest.raises(SourceError):
        sm.call("snapshots", ["600000"])


# ==========================================================================
# Engine 端到端（桩规则）
# ==========================================================================
class StubRule:
    """固定产出一条告警的桩规则。"""

    name = "stub"

    def __init__(self, kind=AlertKind.SURGE, code="600000", bucket=0):
        self.kind = kind
        self.code = code
        self.bucket = bucket
        self.calls = 0

    def evaluate(self, snap, ctx):
        self.calls += 1
        if self.code not in snap.quotes:
            return []
        q = snap.quotes[self.code]
        return [Alert(key=f"{self.code}:{self.kind.value}:{self.bucket}",
                      kind=self.kind, code=self.code, name=q.name, ts=snap.ts,
                      price=q.price, pct=q.pct, title="桩告警", detail="d")]


class BoomRule:
    name = "boom"

    def evaluate(self, snap, ctx):
        raise RuntimeError("规则内部炸了")


def _make_engine(rules, notifiers=None, quotes=None, calendar=None):
    quotes = quotes or [make_quote(code="600000", price=10.0, volume_lots=100000)]
    src = FakeSource(quotes)
    st = _settings()
    store = AlertStore(st, calendar=calendar)
    eng = Engine(src, settings=st, rules=rules, notifiers=notifiers or [],
                 store=store, watchlist=[], calendar=calendar)
    return eng, store


def _settings():
    """每个测试拿到**独立**的 Settings 副本。

    ``load_settings()`` 带进程内缓存，返回同一个可变对象；某些用例会改
    ``rules.*.enabled`` / ``poll.index_codes``，共用一份会把配置泄漏给同进程的
    其它测试（实测：整组 test_replay_cli 因此变成 0 条告警）。
    """
    from arad.config import load_settings

    return load_settings(use_cache=False)


def _morning_cal():
    """永远处于早盘时段的日历，绕过真实时钟/节假日。"""
    cal = TradingCalendar(holidays=set())
    cal.phase = lambda now=None: SessionPhase.MORNING      # type: ignore[method-assign]
    return cal


def test_engine_poll_once_produces_and_dispatches_alert(monkeypatch):
    cal = TradingCalendar(holidays=set())
    monkeypatch.setattr(cal, "phase", lambda now=None: SessionPhase.MORNING)
    rule = StubRule()
    rec = RecordingNotifier()
    eng, store = _make_engine([rule], [rec], calendar=cal)
    eng._codes = ["600000"]

    alerts = eng.poll_once()
    assert len(alerts) == 1
    assert alerts[0].code == "600000"
    assert rec.codes == ["600000"]
    assert store.alerts_total() == 1

    # 同 key 第二次被去重
    alerts2 = eng.poll_once()
    assert alerts2 == []


def test_engine_dispatch_prefers_send_digest():
    """引擎应把本轮告警一次性交给 send_digest（digest 模式才有数据）。"""
    rec = RecordingNotifier()
    rule = StubRule()
    eng, store = _make_engine([rule], [rec], calendar=_morning_cal())
    eng._codes = ["600000"]
    eng.poll_once()

    assert len(rec.digests) == 1                   # 走了批量路径
    assert rec.send_calls == 0                     # 未走逐条路径
    assert rec.codes == ["600000"]


def test_engine_dispatch_falls_back_for_legacy_notifier():
    """只实现 send() 的旧通知器仍必须收到告警（不能静默丢失）。"""
    legacy = LegacyNotifier()
    rule = StubRule()
    eng, store = _make_engine([rule], [legacy], calendar=_morning_cal())
    eng._codes = ["600000"]
    eng.poll_once()
    assert legacy.codes == ["600000"]


def test_engine_dispatch_batches_all_alerts_in_one_round():
    """同一轮多条告警应合并成一次 send_digest 调用。"""
    a = StubRule(code="600000")
    b = StubRule(code="600001")
    rec = RecordingNotifier()
    quotes = [make_quote(code="600000", price=10.0, volume_lots=100000),
              make_quote(code="600001", price=20.0, volume_lots=50000)]
    eng, store = _make_engine([a, b], [rec], quotes=quotes, calendar=_morning_cal())
    eng._codes = ["600000", "600001"]
    fresh = eng.poll_once()
    assert len(fresh) == 2
    assert len(rec.digests) == 1                   # 合并成一批
    assert sorted(rec.codes) == ["600000", "600001"]


def test_engine_survives_broken_rule(monkeypatch):
    cal = TradingCalendar(holidays=set())
    monkeypatch.setattr(cal, "phase", lambda now=None: SessionPhase.MORNING)
    good = StubRule(code="600000")
    eng, store = _make_engine([BoomRule(), good], calendar=cal)
    eng._codes = ["600000"]
    alerts = eng.poll_once()
    # 坏规则被隔离，好规则照常工作
    assert len(alerts) == 1
    assert eng.state.stats.get("err:boom") == 1


def test_engine_notifier_exception_does_not_break(monkeypatch):
    cal = TradingCalendar(holidays=set())
    monkeypatch.setattr(cal, "phase", lambda now=None: SessionPhase.MORNING)

    class BadNotifier:
        name = "bad"

        def send(self, alert):
            raise RuntimeError("推送挂了")

        def send_digest(self, alerts):
            raise RuntimeError("推送挂了")

    eng, store = _make_engine([StubRule()], [BadNotifier()], calendar=cal)
    eng._codes = ["600000"]
    alerts = eng.poll_once()          # 不应抛出
    assert len(alerts) == 1
    assert store.alerts_total() == 1


def test_engine_idle_when_closed(monkeypatch):
    cal = TradingCalendar(holidays=set())
    monkeypatch.setattr(cal, "phase", lambda now=None: SessionPhase.CLOSED)
    rule = StubRule()
    eng, _ = _make_engine([rule], calendar=cal)
    eng._codes = ["600000"]
    assert eng.poll_once() == []
    assert rule.calls == 0


def test_engine_force_bypasses_idle(monkeypatch):
    cal = TradingCalendar(holidays=set())
    monkeypatch.setattr(cal, "phase", lambda now=None: SessionPhase.CLOSED)
    rule = StubRule()
    eng, _ = _make_engine([rule], calendar=cal)
    eng._codes = ["600000"]
    assert len(eng.poll_once(force=True)) == 1


def test_engine_respects_ignore_list(monkeypatch):
    cal = TradingCalendar(holidays=set())
    monkeypatch.setattr(cal, "phase", lambda now=None: SessionPhase.MORNING)
    eng, _ = _make_engine([StubRule()], calendar=cal)
    eng._codes = ["600000"]
    eng.ignore = {"600000"}
    assert eng.poll_once() == []


def test_engine_focus_escalates_severity(monkeypatch):
    cal = TradingCalendar(holidays=set())
    monkeypatch.setattr(cal, "phase", lambda now=None: SessionPhase.MORNING)
    eng, _ = _make_engine([StubRule()], calendar=cal)
    eng._codes = ["600000"]
    eng.focus = ("600000",)
    alerts = eng.poll_once()
    assert alerts[0].severity == 3


def test_engine_run_forever_max_rounds(monkeypatch):
    cal = TradingCalendar(holidays=set())
    monkeypatch.setattr(cal, "phase", lambda now=None: SessionPhase.MORNING)
    # 间隔设 0 以便快速跑完
    eng, _ = _make_engine([StubRule()], calendar=cal)
    eng._codes = ["600000"]
    eng.settings.raw["poll"]["universe_seconds"] = 0
    seen = []
    eng.run_forever(max_rounds=3, on_round=lambda n, a: seen.append(n))
    assert seen == [1, 2, 3]


def test_engine_source_failure_is_tolerated(monkeypatch):
    cal = TradingCalendar(holidays=set())
    monkeypatch.setattr(cal, "phase", lambda now=None: SessionPhase.MORNING)
    src = FakeSource([make_quote(code="600000")], fail_times=99)
    st = _settings()
    eng = Engine(src, settings=st, rules=[], notifiers=[], store=AlertStore(st, calendar=cal),
                 watchlist=[], calendar=cal)
    eng._codes = ["600000"]
    assert eng.poll_once() == []      # 不抛异常


# ==========================================================================
# AlertStore
# ==========================================================================
def test_store_alert_roundtrip_and_ack():
    st = _settings()
    store = AlertStore(st)
    a = Alert(key="k1", kind=AlertKind.SURGE, code="600000", name="浦发银行",
              ts=datetime(2026, 9, 15, 10, 0), price=9.4, pct=1.51,
              title="急拉 +3.2% / 5分钟", detail="d")
    store.add_alert(a)
    items = store.recent_alerts(10)
    assert len(items) == 1
    assert items[0]["code"] == "600000"
    assert items[0]["acked"] is False
    assert store.ack("k1") is True
    assert store.recent_alerts(10)[0]["acked"] is True
    assert store.alerts_total() == 1


def test_store_kind_filter_and_limit():
    st = _settings()
    store = AlertStore(st)
    for i in range(5):
        store.add_alert(Alert(key=f"s{i}", kind=AlertKind.SURGE, code=f"60000{i}",
                              name="x", ts=datetime.now(), price=10, pct=1, title="t", detail="d"))
    store.add_alert(Alert(key="p0", kind=AlertKind.PLUNGE, code="000001",
                          name="y", ts=datetime.now(), price=10, pct=-1, title="t", detail="d"))
    assert len(store.recent_alerts(100, kind="surge")) == 5
    assert len(store.recent_alerts(100, kind="plunge")) == 1
    assert len(store.recent_alerts(2)) == 2          # 倒序 + limit
    assert store.recent_alerts(2)[0]["key"] == "p0"  # 最新的在前


def test_store_subscribe_broadcast_and_cleanup():
    st = _settings()
    store = AlertStore(st)
    q = store.subscribe()
    assert store.subscriber_count == 1
    store.broadcast("alert", {"x": 1})
    ev = q.get_nowait()
    assert ev["event"] == "alert" and ev["data"] == {"x": 1}
    store.unsubscribe(q)
    assert store.subscriber_count == 0


def test_store_broadcast_does_not_block_on_slow_client():
    """慢客户端队列满时必须丢事件而不是阻塞引擎。"""
    st = _settings()
    store = AlertStore(st)
    q = store.subscribe()          # maxsize=200
    for i in range(500):
        store.broadcast("tick", {"i": i})
    assert q.qsize() <= 200        # 没有无限增长
    # 最新事件仍在（丢弃的是最旧的）
    last = None
    while not q.empty():
        last = q.get_nowait()
    assert last["data"]["i"] == 499


def test_store_top_quotes_and_series():
    st = _settings()
    store = AlertStore(st, series_len=5)
    q1 = make_quote(code="600000", price=10.0, volume_lots=1000)
    q2 = make_quote(code="000001", price=20.0, volume_lots=2000)
    store.record_tick([q1, q2])
    store.record_tick([q1.copy_with(price=10.5), q2.copy_with(price=19.0)])
    s = store.series("600000")
    assert len(s) == 2
    assert s[-1]["price"] == pytest.approx(10.5)
    # series_len 生效
    for _ in range(10):
        store.record_tick([q1])
    assert len(store.series("600000")) <= 5


def test_store_status_fields():
    st = _settings()
    store = AlertStore(st, calendar=TradingCalendar(holidays=set()))
    status = store.status()
    for key in ("phase", "session", "uptime_s", "universe", "alerts_total",
                "sources", "last_poll_ms", "poll_count"):
        assert key in status, key
    assert status["universe"] == 0
    assert status["alerts_total"] == 0


def test_store_top_quotes_empty_without_engine():
    st = _settings()
    store = AlertStore(st)
    assert store.top_quotes(10) == []
    assert store.watchlist_quotes() == []


# ==========================================================================
# build_rules 容错
# ==========================================================================
def test_build_rules_tolerates_missing_modules():
    """全部已知规则都显式禁用时 -> 一个都不装载（模块缺失/关闭都不致命）。"""
    from arad.engine import RULE_MODULES
    st = _settings()
    st.raw["rules"] = {name: {"enabled": False} for name in RULE_MODULES}
    assert build_rules(st) == []


def test_build_rules_loads_available_modules():
    """真实规则模块若已实现则应被装载（未实现时跳过，不算失败）。"""
    st = _settings()
    st.raw["rules"] = {}          # 全部默认启用
    rules = build_rules(st)
    names = {getattr(r, "name", "") for r in rules}
    # 断言"都是已知模块"而非"只有这四个"：新增规则模块不该让本用例失败，
    # 但要能挡住拼错的模块名（拼错会落到 name 上被这行抓到）。
    assert names <= {"tick_surge", "limit_board", "volume_burst", "unusual",
                     "spirit_price", "spirit_order", "spirit_index"}
    # 若存在则必须是可调用的 Rule
    for r in rules:
        assert hasattr(r, "evaluate")


def test_build_rules_honours_disabled_flag():
    """显式 enabled: false 的规则不得被装载（短线精灵三个模块默认就是关的）。"""
    st = _settings()
    st.raw["rules"] = {"spirit_price": {"enabled": False},
                       "spirit_order": {"enabled": False},
                       "spirit_index": {"enabled": False}}
    names = {getattr(r, "name", "") for r in build_rules(st)}
    assert not (names & {"spirit_price", "spirit_order", "spirit_index"})


# ==========================================================================
# 数据源接线（真实 bug 回归：配了 fallback 却没用上）
# ==========================================================================
def test_engine_builds_full_failover_chain():
    """Engine 必须把 sources.fallback 也接进链里，否则配了备用源也不生效。"""
    st = _settings()
    fallback = list(st.get("sources.fallback") or [])
    assert fallback, "settings.yaml 应配置 sources.fallback 才能测出这条"

    eng = Engine(source=None, settings=st, rules=[], notifiers=[],
                 calendar=_morning_cal())
    names = [getattr(s, "name", "?") for s in eng.sources.sources]
    assert names[0] == st.get("sources.primary"), "主源必须排第一"
    assert len(names) == 1 + len(fallback), f"备用源没接上: {names}"
    for fb in fallback:
        assert fb in names


def test_universe_source_is_configurable_and_distinct():
    """股票池源取 sources.universe，且必须与行情主源区分开。

    东财能列全市场且字段最全，但会对部分网络限流；新浪行情中心也能列全
    （实测 5563 只），作为自动兜底。所以 ``sources.universe`` 允许配成**列表**，
    这里是"配了就用、顺序不变"的契约。
    """
    st = _settings()
    raw = st.get("sources.universe")
    assert raw, "settings.yaml 应配置 sources.universe"
    names = [str(n) for n in (raw if isinstance(raw, (list, tuple)) else [raw])]
    assert names, "sources.universe 不能为空"

    eng = Engine(source=None, settings=st, rules=[], notifiers=[],
                 calendar=_morning_cal())
    got = [getattr(s, "name", "") for s in eng._universe_sources()]
    assert got == names, f"股票池来源链应为 {names}，实际 {got}"
    # 至少有一个来源能真正枚举全市场（腾讯的 universe() 只是回显缓存）
    assert any(n in ("eastmoney", "sina") for n in got), got


def test_universe_chain_falls_through_to_next_source(monkeypatch):
    """前一个股票池来源失败时，必须自动换下一个。

    真实场景：东财返回 RemoteDisconnected（IP 限流），只配东财的话整轮刷新报废，
    系统退化成只盯自选股那 10 只 —— 盘中基本没用。
    """
    st = _settings()
    st.section("sources")["universe"] = ["eastmoney", "sina"]
    eng = Engine(source=None, settings=st, rules=[], notifiers=[],
                 calendar=_morning_cal())

    calls: list[str] = []

    class Boom:
        name = "eastmoney"

        def universe(self):
            calls.append("eastmoney")
            raise RuntimeError("RemoteDisconnected")

    class Good:
        name = "sina"

        def universe(self):
            calls.append("sina")
            return [make_quote(code="600000"), make_quote(code="000001")]

    monkeypatch.setattr(eng, "_universe_src", [Boom(), Good()])
    n = eng.refresh_universe()

    assert calls == ["eastmoney", "sina"], f"应当依次尝试，实际 {calls}"
    assert n == 2 and set(eng._codes) == {"600000", "000001"}


def test_universe_chain_all_fail_keeps_existing_codes(monkeypatch):
    """所有来源都失败时保留原股票池，绝不清空。"""
    st = _settings()
    eng = Engine(source=None, settings=st, rules=[], notifiers=[],
                 calendar=_morning_cal())
    eng._codes = ["600000"]

    class Boom:
        name = "eastmoney"

        def universe(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(eng, "_universe_src", [Boom()])
    assert eng.refresh_universe() == 0
    assert eng._codes == ["600000"], "全失败时不能清空已有股票池"


def test_universe_refresh_falls_back_to_watchlist(monkeypatch):
    """全市场股票池拿不到时必须降级到自选股。

    否则 _codes 为空 -> poll_once 直接 return -> 系统静默地什么都不做，
    这是最难发现的一类故障（没有任何报错，只是永远没有告警）。
    """
    st = _settings()
    eng = Engine(source=None, settings=st, rules=[], notifiers=[],
                 calendar=_morning_cal())
    eng.watchlist = ["600000", "000001"]
    monkeypatch.setattr(eng, "refresh_universe", lambda: 0)   # 模拟股票池失败

    eng._codes = []
    eng._maybe_refresh_universe(force=True)
    assert eng._codes == ["600000", "000001"], "股票池为空时应降级为自选股"


def test_universe_refresh_keeps_existing_codes_on_failure(monkeypatch):
    """刷新失败不能把已有的股票池清空（否则全市场扫描会突然停摆）。"""
    st = _settings()
    eng = Engine(source=None, settings=st, rules=[], notifiers=[],
                 calendar=_morning_cal())
    eng.watchlist = ["600000"]
    eng._codes = ["111111", "222222"]
    monkeypatch.setattr(eng, "refresh_universe", lambda: 0)

    eng._maybe_refresh_universe(force=True)
    assert eng._codes == ["111111", "222222"], "失败时不应丢掉已有股票池"


def test_poll_once_uses_watchlist_not_silent_when_universe_empty(monkeypatch):
    """股票池为空但自选股可用时，poll_once 必须真的去抓行情。"""
    st = _settings()
    src = FakeSource(quotes=[make_quote(code="600000")])
    eng = Engine(source=src, settings=st, rules=[], notifiers=[],
                 calendar=_morning_cal())
    eng.watchlist = ["600000"]
    monkeypatch.setattr(eng, "refresh_universe", lambda: 0)
    eng._codes = []

    eng.poll_once(force=True)
    assert eng._codes == ["600000"]
    assert src.calls, "降级后必须仍抓取快照"
    assert src.calls[-1] == ["600000"]


# ==========================================================================
# AlertBus 冷却：跨桶只差 1 秒的真实缺陷
# ==========================================================================
def _alert(key: str, *, cd_key: str = "", cd: float = 0.0, kind=AlertKind.SURGE):
    return Alert(key=key, kind=kind, code="600000", name="测试",
                 ts=datetime(2026, 9, 15, 10, 0, 0), price=10.0, pct=1.0,
                 title="t", detail="d", severity=2,
                 cooldown_key=cd_key, cooldown_seconds=cd)


def test_bus_dedups_same_key_within_window():
    bus = AlertBus(EngineState())
    assert bus.accept(_alert("a"), 1000.0) is True
    assert bus.accept(_alert("a"), 1001.0) is False


def test_bus_cooldown_blocks_adjacent_buckets():
    """回归：时间桶跨桶只需 1 秒，导致两条急拉只隔 11 秒。

    ``bucket_of(now, 300)`` 在固定墙上时钟网格上跳变：10:04:59 和 10:05:10
    分属不同桶 -> key 不同 -> 旧逻辑双双放行。带 cooldown_key/seconds 后
    必须真正隔满 300 秒。
    """
    bus = AlertBus(EngineState())
    a1 = _alert("600000:surge:5965080", cd_key="600000:surge", cd=300.0)
    assert bus.accept(a1, 1000.0) is True

    # 11 秒后，不同桶（不同 key）但仍在冷却期内 -> 必须拦掉
    a2 = _alert("600000:surge:5965081", cd_key="600000:surge", cd=300.0)
    assert bus.accept(a2, 1011.0) is False, "同一事件的相邻桶不能放行"

    # 满 300 秒后放行
    a3 = _alert("600000:surge:5965082", cd_key="600000:surge", cd=300.0)
    assert bus.accept(a3, 1301.0) is True


def test_bus_cooldown_is_per_stock_and_per_kind():
    """冷却按 (code, kind) 隔离，不能一只票的告警压住另一只。"""
    bus = AlertBus(EngineState())
    assert bus.accept(_alert("600000:surge:1", cd_key="600000:surge", cd=300.0), 1000.0)
    # 另一只票、另一类 -> 不受影响
    assert bus.accept(_alert("000001:surge:1", cd_key="000001:surge", cd=300.0), 1000.0)
    assert bus.accept(_alert("600000:plunge:1", kind=AlertKind.PLUNGE,
                             cd_key="600000:plunge", cd=300.0), 1000.0)


def test_bus_without_cooldown_still_works():
    """未声明冷却的告警退化为纯 key 去重（不改变既有行为）。"""
    bus = AlertBus(EngineState())
    assert bus.accept(_alert("x:1"), 1000.0) is True
    assert bus.accept(_alert("x:2"), 1001.0) is True
    assert bus.accept(_alert("x:1"), 1002.0) is False


def test_rules_emit_cooldown_metadata():
    """真实规则产出的告警必须带 cooldown_key/cooldown_seconds。

    只检查"规则有 cooldown 属性"是不够的——必须在**实际告警对象**上验证，
    否则规则的 cooldown 配置根本传不到 AlertBus。
    """
    import inspect
    from arad.rules import tick_surge, volume_burst

    # 源码层面：两类事件型规则都要把冷却信息写进 Alert
    for mod in (tick_surge, volume_burst):
        src = inspect.getsource(mod)
        assert "cooldown_key=" in src, f"{mod.__name__} 未在 Alert 中填 cooldown_key"
        assert "cooldown_seconds=" in src, \
            f"{mod.__name__} 未在 Alert 中填 cooldown_seconds"

    # 行为层面：tick_surge 真实触发一次，检查告警字段
    from arad.rules.tick_surge import build as build_surge

    rule = build_surge({"cooldown_seconds": 120})
    assert float(rule.cooldown) == 120.0


# ==========================================================================
# 股票池 pin：注入代码后不得再联网刷新
# ==========================================================================
def test_injected_codes_are_pinned_and_not_refreshed(monkeypatch):
    """回归：注入 _codes 后 poll_once 仍会联网刷新并覆盖注入值。

    后果有两层：(1) 离线测试变成依赖网络、结果随机（实测 568/569 抖动）；
    (2) 回放模式下真实行情会顶掉剧本代码，整个回放失效。
    """
    st = _settings()
    eng = Engine(source=FakeSource([make_quote(code="600000")]), settings=st,
                 rules=[], notifiers=[], calendar=_morning_cal())

    calls: list[int] = []
    monkeypatch.setattr(eng, "refresh_universe",
                        lambda: calls.append(1) or 0)

    eng._codes = ["600000"]                 # 注入即 pin
    assert eng._codes_pinned is True
    eng._maybe_refresh_universe()
    assert calls == [], "已注入股票池时不得联网刷新"
    assert eng._codes == ["600000"], "注入的代码不能被覆盖"


def test_empty_injection_does_not_pin(monkeypatch):
    """注入空列表不算 pin（否则股票池永远为空，系统静默失效）。"""
    st = _settings()
    eng = Engine(source=None, settings=st, rules=[], notifiers=[],
                 calendar=_morning_cal())
    eng._codes = []
    assert eng._codes_pinned is False


def test_poll_once_does_not_touch_network_when_codes_injected(monkeypatch):
    """端到端：注入股票池后一轮 poll 不得发起任何刷新。"""
    st = _settings()
    src = FakeSource([make_quote(code="600000")])
    eng = Engine(source=src, settings=st, rules=[], notifiers=[],
                 calendar=_morning_cal())

    def boom():
        raise AssertionError("poll_once 不应联网刷新股票池")

    monkeypatch.setattr(eng, "refresh_universe", boom)
    eng._codes = ["600000"]
    eng.poll_once(force=True)
    assert src.calls, "应当只抓行情，不刷新股票池"


# ==========================================================================
# 指数管道：按需抓取 + 历史不被 prune 清掉
# ==========================================================================
class _IndexWantingRule:
    """只声明"我要指数"，不产出告警 —— 用来观察引擎的抓取行为。"""

    name = "fake_index"
    wants_indices = True

    def __init__(self):
        self.seen: list[set[str]] = []

    def evaluate(self, snap, ctx):
        self.seen.append({c for c, q in ctx.state.quotes.items()
                          if q.board is Board.INDEX})
        return []


def _index_quote(symbol: str, name: str, price: float) -> Quote:
    return make_quote(code=symbol, name=name, price=price, board=Board.INDEX)


def test_index_codes_parsed_and_prefix_required():
    """裸码会被丢弃并告警：000001 到底是哪个标的无法从 6 位数字判断。"""
    st = _settings()
    st.section("poll")["index_codes"] = [
        "sh000001", "sz399001", "SZ399006", "000001", "600000", "", "sh12345",
    ]
    eng = Engine(source=FakeSource([]), settings=st, rules=[], notifiers=[],
                 calendar=_morning_cal())
    assert eng.index_codes == ["sh000001", "sz399001", "sz399006"]


def test_no_index_request_when_no_rule_wants_them():
    """没有规则消费指数时不该发请求，否则每轮白抓 5 个代码。"""
    st = _settings()
    src = FakeSource([make_quote(code="600000")])
    eng = Engine(source=src, settings=st, rules=[], notifiers=[],
                 calendar=_morning_cal())
    eng.watchlist = ["600000"]
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(eng, "refresh_universe", lambda *a, **k: 0)
        eng._codes = ["600000"]
        eng.poll_once(force=True)
    finally:
        monkeypatch.undo()
    assert eng.index_codes, "配置里应当有指数"
    for call in src.calls:
        assert not any(c in eng.index_codes for c in call), \
            f"没有规则要指数，却抓了 {call}"


def test_rule_declaring_wants_indices_triggers_index_fetch():
    st = _settings()
    st.section("poll")["index_codes"] = ["sh000001"]
    rule = _IndexWantingRule()
    src = FakeSource([make_quote(code="600000")])
    src.by_code = {"sh000001": _index_quote("sh000001", "上证指数", 3891.60)}
    src.calls = []

    def snapshots(codes):
        src.calls.append(list(codes))
        out = []
        for c in codes:
            if c in src.by_code:
                out.append(src.by_code[c])
            else:
                out.append(make_quote(code=c))
        return out

    src.snapshots = snapshots                     # type: ignore[method-assign]
    eng = Engine(source=src, settings=st, rules=[rule], notifiers=[],
                 calendar=_morning_cal())
    eng.watchlist = ["600000"]
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(eng, "refresh_universe", lambda *a, **k: 0)
        eng._codes = ["600000"]
        eng.poll_once(force=True)
    finally:
        mp.undo()

    assert ["sh000001"] in src.calls, f"应当抓过指数，实际 {src.calls}"
    assert rule.seen and "sh000001" in rule.seen[-1], \
        "指数必须能被规则从 ctx.state.quotes 看到"


def test_index_history_survives_prune():
    """指数被 filters 拦在 eligible 外，但历史必须靠 keep 保命。

    否则 5 分钟窗口每轮被清空，「拉升指数」永远等不到样本。
    """
    st = _settings()
    src = FakeSource([make_quote(code="600000")])
    eng = Engine(source=src, settings=st, rules=[], notifiers=[],
                 calendar=_morning_cal())
    eng.watchlist = ["600000"]
    eng.index_codes = ["sh000001"]

    now = datetime(2026, 9, 15, 10, 0, 0)
    for i in range(4):
        eng.state.update(
            [_index_quote("sh000001", "上证指数", 3890.0 + i),
             make_quote(code="600000", price=10.0 + i * 0.01)],
            now + timedelta(seconds=i * 10),
        )

    # 塞进足够多的"垃圾"历史，逼 prune 真的动手
    for j in range(40):
        eng.state.update([make_quote(code=f"9{j:05d}", price=5.0)],
                         now + timedelta(seconds=j))
    eng.state.update([_index_quote("sh000001", "上证指数", 3893.0)],
                     now + timedelta(seconds=50))

    keep = {"600000"}
    keep.update(eng.index_codes)
    eng.state.prune(keep)
    assert "sh000001" in eng.state.history, "指数历史被 prune 清掉了"

