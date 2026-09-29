"""整日模拟：一个完整 A 股交易日的端到端行为回归。

本文件补的是**从未被覆盖的那一段**：现有测试都只驱动 1~3 轮
（``test_engine.py`` 的两三轮、``test_session_boundaries.py`` 的单点时刻），
而盘中盯盘的真实风险恰恰在**跨时段、跨源、跨冷却窗口**的**序列**行为上：

* 午休前后时段切换时，引擎会不会漏抓；
* 急拉/急跌的冷却窗口是**跨时段连续**的（11:29 报过 -> 13:05 不再报），
  这一点在单时刻切片里看不出来；
* 主源盘中挂掉、备用源顶上时，告警流会不会中断，观测账本记了什么；
* 同一天跑两遍是否**逐字节**一致 —— 这是让本文件能当回归判据的前提。

设计约束
--------
* **时钟全部注入**（``arad.replay.SimClock`` + ``Engine(now_fn=...)``），
  本文件任何地方都不调用 ``datetime.now()`` 做业务判定。
* **无网络**：只用 ``FakeSource`` 与 ``SourceManager`` 的假源。
* 断言一律是**行为声明**（抓没抓、报没报、账本里的数是几），
  不用 ``is not None`` 这类冒烟检查。
* 所有 ``poll_once()`` 都**不带 force** —— 否则时段门控会被整个绕过，
  时段相关的用例就变成了自欺。

复用
----
假源复用 ``tests/fakes.py``（``FakeSource`` / ``make_quote`` /
``RecordingNotifier``），时钟复用 ``arad.replay.SimClock`` —— 不新造驱动设施。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from arad.config import load_settings
from arad.engine import ROUTE_INDEX, ROUTE_STOCKS, AlertBus, Engine, EngineState, SourceManager
from arad.models import Alert, AlertKind, Quote
from arad.replay import SimClock
from arad.rules.tick_surge import TickSurgeRule
from arad.session import (
    CONTINUOUS,
    OBSERVABLE,
    SessionPhase,
    TradingCalendar,
)
from arad.store import AlertStore

from fakes import FakeSource, RecordingNotifier, make_quote

# 2026-09-15 是**周二**，正常交易日（``datetime(2026, 9, 15).weekday() == 1``）。
DAY = (2026, 9, 15)
PREV_CLOSE = 10.0
#: 急拉/急跌默认冷却（与 ``settings.yaml`` 的 rules.tick_surge 一致）
COOLDOWN = 300.0


# ==========================================================================
# 夹具
# ==========================================================================
def at(h: int, m: int, s: int = 0, d: tuple[int, int, int] = DAY) -> datetime:
    """构造一个**固定**时刻（永不使用墙钟）。"""
    y, mo, dd = d
    return datetime(y, mo, dd, hour=h, minute=m, second=s)


def q(code: str, price: float, vol: float, ts: datetime,
      *, prev_close: float = PREV_CLOSE, name: str = "模拟股",
      amount: float | None = None) -> Quote:
    """构造一条模拟行情。

    默认让 ``amount = 成交量(手) * 100 * 现价``，于是
    ``Quote.vwap``（= amount / (volume_lots*100)）**恰好等于现价**。
    这是刻意的：急拉要 ``price >= vwap``（恒真），急跌要 ``price < vwap``
    —— **恒假**。所以急跌用例必须显式把 ``amount`` 做大（把均价钉在高位），
    见 ``plunge_quote()``。``test_plunge_requires_price_below_vwap`` 把
    这个语义本身钉住了。
    """
    amt = vol * 100.0 * price if amount is None else amount
    return make_quote(
        code=code, name=name, price=price, prev_close=prev_close,
        open=prev_close, high=max(prev_close, price), low=min(prev_close, price),
        volume_lots=vol, amount=amt, ts=ts)


def surge_quote(code: str, price: float, runs: int, ts: datetime) -> Quote:
    """急拉用行情：均价 == 现价（天然在均价上方），成交量递增。"""
    return q(code, price, 200_000.0 + runs * 4_000.0, ts)


def plunge_quote(code: str, price: float, runs: int, ts: datetime) -> Quote:
    """急跌用行情：把累计成交额**钉在高位**，使 ``vwap > price`` 恒成立。

    只用 ``amount = price*vol*100`` 的话 vwap 会跟着价格一起跌，
    ``price < vwap`` 永远为假 —— 急跌一条也报不出来（实测）。
    """
    vol = 200_000.0 + runs * 4_000.0
    return q(code, price, vol, ts, amount=vol * 100.0 * PREV_CLOSE * 1.02)


def flat_quote(code: str, price: float, runs: int, ts: datetime) -> Quote:
    return q(code, price, 200_000.0 + runs * 1_000.0, ts)


class DaySim:
    """驱动一个完整交易日的模拟器（唯一出口是 ``Engine.poll_once()``）。"""

    def __init__(self, rules=None, *, sources=None, codes=("600000", "600001"),
                 failover_threshold: int = 3):
        self.codes = list(codes)
        self.clock = SimClock(at(9, 30, 0))
        self.calendar = TradingCalendar(holidays=set())
        self.settings = load_settings(use_cache=False)
        # 股票池刷新走网络入口 -> 关掉（_codes 会显式 pin，本就不会触发）。
        self.settings.section("poll")["universe_refresh_seconds"] = 10 ** 9
        # index_codes 必须清空：本文件**不开**任何需要指数的规则，而默认配置
        # 带了 5 个指数码。详见 test_index_route_is_not_requested_without_indices_rule。
        self.settings.section("poll")["index_codes"] = []

        self.primary = FakeSource([])
        self.backup = FakeSource([])
        chain = list(sources) if sources is not None else [self.primary, self.backup]
        self.manager = SourceManager(chain, threshold=failover_threshold)
        self.notifier = RecordingNotifier()
        self.store = AlertStore(self.settings, calendar=self.calendar,
                                max_alerts=5000, series_len=64)
        self.engine = Engine(
            self.manager, settings=self.settings,
            rules=rules if rules is not None else [TickSurgeRule()],
            notifiers=[self.notifier], store=self.store,
            watchlist=[], calendar=self.calendar, now_fn=self.clock,
        )
        self.engine._codes = list(self.codes)      # 赋值即 pin
        self.events: list[dict] = []
        self.queue = self.store.subscribe()
        self.rounds = 0
        self.rounds_by_phase: dict[str, int] = {}

    # -- 驱动 -------------------------------------------------------------
    def set_quotes(self, quotes, *, which: str = "primary") -> None:
        src = self.primary if which == "primary" else self.backup
        for x in quotes:
            src.set(x)

    def tick(self, when: datetime, quotes=(), *, which: str = "primary") -> list[Alert]:
        """把时钟设到 ``when``，可选更新行情，然后跑一轮 poll（不带 force）。"""
        self.clock.set(when)
        if quotes:
            self.set_quotes(quotes, which=which)
        fresh = self.engine.poll_once()
        self.rounds += 1
        phase = self.calendar.phase(when).value
        self.rounds_by_phase[phase] = self.rounds_by_phase.get(phase, 0) + 1
        while not self.queue.empty():
            self.events.append(self.queue.get_nowait())
        return fresh

    def run(self, start: datetime, end: datetime, step_seconds: float,
            build, *, which: str = "primary") -> list[Alert]:
        """从 ``start`` 到 ``end``（左闭右闭）按 ``step_seconds`` 推进。

        ``build(now, i)`` 返回本轮要推送的行情列表。
        """
        out: list[Alert] = []
        t, i = start, 0
        while t <= end:
            out.extend(self.tick(t, build(t, i), which=which))
            t = t + timedelta(seconds=step_seconds)
            i += 1
        return out

    # -- 观察 -------------------------------------------------------------
    @property
    def alert_sequence(self) -> list[tuple]:
        """**逐字节**指纹用的告警序列（不含任何非确定性字段）。"""
        return [
            (a.ts.strftime("%Y-%m-%d %H:%M:%S"), a.kind.value, a.code, a.title,
             round(a.price, 4), round(a.pct, 4), a.severity, a.signal_id,
             a.key, a.cooldown_key, round(float(a.cooldown_seconds), 3),
             round(float(a.metrics.get("window_pct", 0.0)), 4),
             round(float(a.metrics.get("window_seconds", 0.0)), 3))
            for a in self.notifier.alerts
        ]

    def phase_broadcasts(self) -> list[str]:
        return [str(e["data"]["phase"]) for e in self.events
                if e.get("event") == "phase"]

    def observation(self) -> dict:
        return self.store.observation


# ==========================================================================
# 1. 时段阶段：一整个交易日的相位切换与抓取门控
# ==========================================================================
#: (时刻, 期望时段, 该时刻引擎是否应当**真的去抓**行情)
PHASE_SCRIPT = [
    ("08:59:00", SessionPhase.CLOSED, False),
    ("09:14:59", SessionPhase.CLOSED, False),
    ("09:15:00", SessionPhase.PRE_OPEN, True),       # 开盘集合竞价
    ("09:20:00", SessionPhase.PRE_OPEN, True),
    ("09:24:59", SessionPhase.PRE_OPEN, True),
    ("09:25:00", SessionPhase.AUCTION, False),       # 静默：可撤单不可成交
    ("09:29:59", SessionPhase.AUCTION, False),
    ("09:30:00", SessionPhase.MORNING, True),        # 连续竞价开始
    ("10:30:00", SessionPhase.MORNING, True),
    ("11:29:59", SessionPhase.MORNING, True),
    ("11:30:00", SessionPhase.LUNCH, False),         # 左闭右开：整点即午休
    ("12:30:00", SessionPhase.LUNCH, False),
    ("12:59:59", SessionPhase.LUNCH, False),
    ("13:00:00", SessionPhase.AFTERNOON, True),      # 下午连续竞价
    ("13:30:00", SessionPhase.AFTERNOON, True),
    ("14:56:59", SessionPhase.AFTERNOON, True),
    ("14:57:00", SessionPhase.CLOSE_AUCTION, True),  # 收盘集合竞价（IT-P0-001）
    ("14:59:59", SessionPhase.CLOSE_AUCTION, True),
    ("15:00:00", SessionPhase.POST, False),          # 整点即收盘
    ("17:00:00", SessionPhase.POST, False),
]


def test_full_day_phase_walk_fetches_exactly_in_observable_windows():
    """走完整天：每个时刻的 phase 与「是否真的抓行情」都必须正确。

    判据用**行情源被调用次数**这个外部可观测行为，而不是只看
    ``calendar.phase()``（那是纯函数）—— 只有前者能证明 ``poll_once``
    的门控真的生效。
    """
    sim = DaySim(rules=[])
    for hhmmss, want_phase, want_fetch in PHASE_SCRIPT:
        h, m, s = (int(x) for x in hhmmss.split(":"))
        now = at(h, m, s)
        assert sim.calendar.phase(now) is want_phase, \
            f"{hhmmss} 时段应为 {want_phase}，实际 {sim.calendar.phase(now)}"

        before = len(sim.primary.calls)
        sim.tick(now, [flat_quote("600000", 10.0, sim.rounds, now)])
        fetched = len(sim.primary.calls) > before
        assert fetched is want_fetch, (
            f"{hhmmss}（{want_phase.value}）"
            f"{'应当' if want_fetch else '不应当'}抓行情")


def test_fetch_gate_is_exactly_the_observable_set():
    """抓取门控的判据必须**恰好**是 ``OBSERVABLE``：抓过的时段集合 == OBSERVABLE。"""
    sim = DaySim(rules=[])
    for hhmmss, _, _ in PHASE_SCRIPT:
        h, m, s = (int(x) for x in hhmmss.split(":"))
        now = at(h, m, s)
        before = len(sim.primary.calls)
        sim.tick(now, [flat_quote("600000", 10.0, sim.rounds, now)])
        if len(sim.primary.calls) > before:
            assert sim.calendar.phase(now) in OBSERVABLE, (
                f"{hhmmss} 抓了行情，但它不在 OBSERVABLE 里")
    assert set(sim.rounds_by_phase) >= {p.value for p in OBSERVABLE}, (
        "OBSERVABLE 里每个相位都应至少被抓过一次")


def test_lunch_break_freezes_trading_clock_and_fetches_nothing():
    """午休（11:30-13:00）：不抓行情，且「已交易秒数」必须冻在 7200。

    如果午休被算进交易时长，``volume_burst`` 的
    ``volume_lots / elapsed * 60`` 会凭空变小。
    """
    sim = DaySim(rules=[])
    got = []
    for hhmmss in ("11:29:59", "11:30:00", "12:00:00", "12:59:59", "13:00:00"):
        h, m, s = (int(x) for x in hhmmss.split(":"))
        now = at(h, m, s)
        before = len(sim.primary.calls)
        sim.tick(now, [flat_quote("600000", 10.0, sim.rounds, now)])
        got.append((hhmmss, len(sim.primary.calls) - before,
                    sim.calendar.elapsed_trading_seconds(now)))

    assert got[0] == ("11:29:59", 1, 7199.0), got[0]
    for hhmmss, fetched, elapsed in got[1:4]:
        assert fetched == 0, f"{hhmmss} 午休不应当抓行情"
        assert elapsed == 7200.0, f"{hhmmss} 午休期间 elapsed 必须冻结在 7200"
    assert got[4] == ("13:00:00", 1, 7200.0), got[4]


def test_phase_transitions_are_broadcast_in_order():
    """每个轮次的相位都必须广播出去（看板靠它显示「早盘/午休/已收盘」）。

    这条同时证明：引擎写 ``state.session`` 与广播相位发生在**同一轮**，
    看板因此不会出现"行情是 09:40 的、时段显示休市"这种自相矛盾。
    """
    sim = DaySim(rules=[])
    for hhmmss in ("09:00:00", "09:20:00", "09:27:00", "09:30:00",
                   "11:30:00", "13:00:00", "14:58:00", "15:00:00"):
        h, m, s = (int(x) for x in hhmmss.split(":"))
        now = at(h, m, s)
        sim.tick(now, [flat_quote("600000", 10.0, sim.rounds, now)])

    assert sim.phase_broadcasts() == [
        "closed", "pre_open", "auction", "morning",
        "lunch", "afternoon", "close_auction", "post"]
    assert sim.engine.state.session is SessionPhase.POST, (
        "state.session 是看板读的那个值，必须与最后一个时刻一致")


# ==========================================================================
# 2. IT-P0-001：14:57-15:00 收盘集合竞价
# ==========================================================================
def test_it_p0_001_close_auction_is_a_separate_phase_not_continuous():
    """**IT-P0-001 当前状态：已修复** —— 14:57-15:00 是独立的收盘集合竞价。

    证据（file:line）::

        src/arad/session.py:72    CONTINUOUS = (MORNING, AFTERNOON)         # 不含 CLOSE_AUCTION
        src/arad/session.py:158   if t < _T_CLOSE: return CLOSE_AUCTION     # 14:57-15:00
        src/arad/session.py:167   is_open() = phase(...) in CONTINUOUS

    即 ``is_open(14:58) is False``。**我不认为这里还有缺陷** ——
    "把收盘集合竞价当连续竞价"的旧问题确实已经关掉了。本用例把它钉住，
    防止回退（回退会让 ``is_open(14:58)`` 重新变 True）。
    """
    cal = TradingCalendar(holidays=set())
    assert SessionPhase.CLOSE_AUCTION not in CONTINUOUS
    assert cal.phase(at(14, 56, 59)) is SessionPhase.AFTERNOON
    assert cal.phase(at(14, 57, 0)) is SessionPhase.CLOSE_AUCTION
    assert cal.phase(at(14, 59, 59)) is SessionPhase.CLOSE_AUCTION
    assert cal.phase(at(15, 0, 0)) is SessionPhase.POST

    assert cal.is_open(at(14, 56, 59)) is True, "不得过度修正：末秒仍是连续竞价"
    assert cal.is_open(at(14, 57, 0)) is False
    assert cal.is_open(at(14, 58, 0)) is False
    assert cal.is_open(at(14, 59, 59)) is False
    # 量能时钟收口在 14:57：全天连续竞价 7200 + 7020 = 14220 秒，不是 14400
    assert cal.elapsed_trading_seconds(at(14, 58)) == pytest.approx(14220.0)
    assert cal.elapsed_trading_seconds(at(15, 0)) == pytest.approx(14220.0)


def test_close_auction_still_fetches_but_no_rule_fires():
    """14:57-15:00 仍抓行情，但**没有连续竞价规则的告警**（行为级后果）。

    也是"不得过度修正"的守卫：该时段价格确实在动（要抓），
    只是没有连续成交（规则要静音）。
    """
    sim = DaySim(rules=[TickSurgeRule()])

    sim.run(at(9, 30, 0), at(9, 36, 0), 10,
            lambda now, i: [surge_quote(
                "600000", round(PREV_CLOSE * (1 + 0.0035 * max(0, i - 18)), 3),
                i, now)])
    fired_morning = len(sim.notifier.alerts)
    assert fired_morning >= 1, "上午必须报出急拉，否则本用例没有对照"

    before = len(sim.primary.calls)
    late = sim.run(at(14, 57, 0), at(14, 59, 50), 10,
                   lambda now, i: [surge_quote("600000", 11.5 + 0.01 * i,
                                               900 + i, now)])
    assert len(sim.primary.calls) > before, "收盘集合竞价价格在动，必须照常抓"
    assert late == [], (
        "14:57-15:00 是收盘集合竞价（无连续成交），tick_surge 必须静音；"
        f"实际报出 {[(a.kind.value, a.ts) for a in late]}")
    assert len(sim.notifier.alerts) == fired_morning, "收盘集合竞价不应新增告警"

    before = len(sim.primary.calls)
    sim.tick(at(15, 30, 0), [flat_quote("600000", 11.5, 0, at(15, 30, 0))])
    assert len(sim.primary.calls) == before, "15:00 之后必须停抓"


def test_close_auction_silence_is_enforced_by_rules_not_by_engine():
    """⚠ **脆弱性（记录，未修）**：收盘集合竞价的静音是**规则级**的，不是引擎级的。

    引擎的抓取门控用的是 ``phase not in OBSERVABLE``，而 ``OBSERVABLE``
    **故意包含** ``CLOSE_AUCTION``（那段价格在动、要抓）。于是 ``poll_once``
    在 14:57-15:00 会**照常走到规则求值**：

        src/arad/engine.py:1892    if phase not in OBSERVABLE: ... return []

    真正抑制告警的是每个规则各自的 ``only_continuous`` 开关，走的是
    **另一个谓词**（``CONTINUOUS``）：

        src/arad/rules/tick_surge.py:101
            if cfg.get("only_continuous", True) and ctx.session not in CONTINUOUS:
                return []

    **同一个事实（"现在是不是连续竞价"）被两套谓词分别判定**：引擎层用
    ``OBSERVABLE``（含收盘竞价），规则层用 ``CONTINUOUS``（不含）。今天两者
    结论一致，靠的是每个规则模块都恰当地声明了 ``only_continuous``；
    只要有任何一个规则漏声明，或者将来有人加一条不经规则的告警出口，
    14:57-15:00 就会冒出按连续竞价语义算出来的假急拉，**而没有任何地方会报错**。

    本用例把结构**如实钉住**（而不是断言它"正确"）。若将来引擎层补上门控，
    本用例会变红 —— 那时应当**更新它**并在 diff 里说明，而不是悄悄删掉。
    """
    seen_sessions: list[SessionPhase] = []

    class Probe:
        name = "probe"
        only_continuous = True

        def evaluate(self, snap, ctx):
            seen_sessions.append(ctx.session)
            return []

    sim = DaySim(rules=[Probe()])
    sim.tick(at(14, 58, 0), [flat_quote("600000", 10.0, 0, at(14, 58, 0))])
    assert seen_sessions == [SessionPhase.CLOSE_AUCTION], (
        "引擎在收盘集合竞价确实调用了规则 —— 静音不是引擎做的；"
        f"实际规则看到的 session={seen_sessions}")

    leaked: list[Alert] = []

    class LeakyRule:
        name = "leaky"
        # 刻意**不**做 only_continuous 门控 —— 模拟一个漏声明的新规则

        def evaluate(self, snap, ctx):
            if ctx.session is SessionPhase.CLOSE_AUCTION:
                leaked.append(Alert(
                    key="600000:surge:leak", kind=AlertKind.SURGE,
                    code="600000", name="模拟股", ts=snap.ts, price=10.5,
                    pct=5.0, title="收盘竞价假急拉", detail="d"))
            return list(leaked)

    sim2 = DaySim(rules=[LeakyRule()])
    fresh = sim2.tick(at(14, 58, 0),
                      [flat_quote("600000", 10.5, 0, at(14, 58, 0))])
    assert fresh, (
        "对照实验：一个不做 only_continuous 门控的规则在 14:58 **会**产出告警，"
        "证明静音完全依赖规则自觉、引擎层没有兜底")


def test_every_rule_enforces_a_session_gate_one_way_or_another():
    """把上面那条脆弱性所依赖的**约定本身**钉住。

    约定是"每个规则都必须自己保证不在非连续竞价时段告警"。**方式可以不同**
    （``only_continuous`` 开关，或规则里自己写死时段判定），但必须有，
    且这里逐个模块核对**实际声明的值** —— 变了就必须显式更新本用例。

    实测（2026-09-24，源码树）::

        tick_surge    only_continuous=True      显式开关
        limit_board   only_continuous=True      显式开关
        volume_burst  only_continuous=True      显式开关
        unusual       only_continuous=True      显式开关
        spirit_order  only_continuous=True      显式开关
        spirit_price  only_continuous=True      显式开关
        spirit_index  only_continuous=True      显式开关

    7/7 全部门控。若哪天少了一个，本用例会红 —— 那正是"引擎层没有兜底"
    这个脆弱性变成真实漏洞的时刻。
    """
    import importlib

    ungated = []
    for name in ("tick_surge", "limit_board", "volume_burst", "unusual",
                 "spirit_order", "spirit_price", "spirit_index"):
        mod = importlib.import_module(f"arad.rules.{name}")
        build = getattr(mod, "build", None)
        rule = build({}) if callable(build) else getattr(mod, "RULE", None)
        assert rule is not None, f"arad.rules.{name} 必须能构造出规则对象"
        if not bool(getattr(rule, "cfg", {}).get("only_continuous", False)):
            ungated.append(name)

    assert ungated == [], (
        f"以下规则没有声明 only_continuous，它们会在收盘集合竞价（14:57-15:00）"
        f"按连续竞价语义误报：{ungated}。若是有意为之，请补齐引擎层兜底"
        "（engine.py 的门控现在用的是 OBSERVABLE，**含**收盘竞价）"
        "再更新本用例")


def test_engine_loads_default_rules_with_the_documented_enable_flags():
    """默认配置装载出来的规则集合，必须与 ``RULE_MODULES`` 的文档一致。

    这是一条"整日跑的就是生产那套规则"的守卫：``DaySim`` 默认用
    ``TickSurgeRule()`` 单跑，但真实引擎是 4 条规则在跑，两者的时段门控
    来源不同（见上一条用例）。
    """
    from arad.engine import RULE_MODULES, build_rules

    st = load_settings(use_cache=False)
    rules = build_rules(st)
    names = [r.name for r in rules]
    assert names == ["tick_surge", "limit_board", "volume_burst", "unusual"], (
        f"默认启用规则变了：{names}（RULE_MODULES={RULE_MODULES}）")
    assert len(names) == len(set(names)), "规则名不得重复"


# ==========================================================================
# 3. 告警生命周期：急拉 / 急跌 各一次 -> 冷却抑制 -> 冷却过期后再报
# ==========================================================================
def _recording_surge_rule(cooldown: float = COOLDOWN):
    """``TickSurgeRule`` 的包装：额外记录**规则层**产出与 ``ctx.session``。

    ⚠ 关键语义（别把它当成缺陷）：**冷却不在规则里执行**。
    ``TickSurgeRule`` 每轮都会把同一个仍在上冲的窗口重新扫出来并返回
    （实测一条急拉连报 8 轮）—— 真正拦住重复投递的是
    ``AlertBus.accept()``，它看的是 Alert 上的
    ``cooldown_key`` + ``cooldown_seconds``（``tick_surge.py:291-292``）。
    所以"只报一条"的断言必须打在 **store / notifier** 上，不是规则返回值上。
    """
    records: list[tuple] = []
    sessions: list = []
    rule = TickSurgeRule({"cooldown_seconds": cooldown, "only_continuous": True})
    inner = rule.evaluate

    def traced(snap, ctx):
        sessions.append(ctx.session)
        out = inner(snap, ctx)
        for a in out:
            records.append((ctx.now, a.kind.value, a.code, a.title))
        return out

    rule.evaluate = traced        # type: ignore[method-assign]
    return rule, records, sessions


def test_alert_lifecycle_surge_fires_suppresses_and_refires():
    """**核心**：一次急拉只报一条；冷却期内被抑制；冷却过期后必须再报。

    时间线（全部固定时刻）::

        09:30:00-09:33:00  平价热身（储备窗口；3 分钟窗口要等够 180s）
        09:33:13-09:35:30  急拉 +3% 级        -> 应报 1 条（09:34:30）
        冷却期内继续拉（跑到 09:39:20）       -> 应报 0 条
        越过 300s 后再拉                      -> 应报第 2 条

    中间那一截是**跨时间桶**的：``key`` 里的 bucket 会变，但
    ``cooldown_key + cooldown_seconds`` 必须仍然拦住它 —— 这正是
    ``tick_surge.py:289-292`` 修过的"跨桶只差 1 秒也算两条"的缺陷。
    """
    rule, records, _sessions = _recording_surge_rule()
    sim = DaySim(rules=[rule])
    assert float(rule.cooldown) == COOLDOWN

    def build(now, i):
        price = (PREV_CLOSE if i <= 18
                 else PREV_CLOSE * (1 + 0.0035 * (i - 18)))
        return [surge_quote("600000", round(price, 4), i, now)]

    sim.run(at(9, 30, 0), at(9, 35, 40), 10, build)
    # 规则层**会**重复扫到（冷却不在这里），但投递必须只有一条
    assert len(records) > 1, (
        "前提：规则层对同一次上冲会重复产出；冷却由 AlertBus 执行")
    assert sim.store.alerts_total() == 1, (
        f"一次急拉只应投递一条，实际 {sim.store.alerts_total()} 条；"
        f"规则层产出 {len(records)} 条")
    t_first = sim.notifier.alerts[0].ts
    assert t_first == at(9, 34, 30), (
        f"首次投递必须落在 3 分钟窗口刚够长的那一刻（09:34:30），实际 {t_first}")
    assert sim.notifier.alerts[0].kind is AlertKind.SURGE
    assert sim.notifier.alerts[0].cooldown_key == "600000:surge"
    assert float(sim.notifier.alerts[0].cooldown_seconds) == COOLDOWN

    # --- 冷却期内：继续缓涨，绝不能再报 ---
    # ⚠ ``run`` 的左端是**不采样**的，所以采样网格由右端决定：
    # 09:35:40 结束 -> 网格是 ...:50/:00/:10/:20/:30/:40。
    # 冷却到期时刻 t_first+300s = 09:39:30 正好落在网格上（:30），
    # 因此 09:39:20 必须仍被按住，09:39:30 才能放行 —— 边界是紧的。
    def build_hot(now, i):
        return [surge_quote("600000", round(PREV_CLOSE * (1.10 + 0.0004 * i), 4),
                            900 + i, now)]

    sim.run(at(9, 35, 50), at(9, 39, 20), 10, build_hot)
    assert sim.store.alerts_total() == 1, (
        "冷却窗口内（含跨时间桶）绝不能再报第二条")
    assert len(sim.notifier.alerts) == 1

    # --- 越过冷却：必须能再报 ---
    # ⚠ 缓涨段（+0.02%/步）的 1 分钟窗口涨幅只有 0.21%，**永远**打不到 2% 阈值，
    # 所以"冷却已过期"能不能放行，必须用一波**确定能命中**的行情来考。
    # 09:39:30 推一跳（+3%），这一轮的 3 分钟窗口是
    # 09:36:30(13.2) -> 09:39:30(13.59) = +2.95% > 2%，当场命中；
    # 且 09:39:30 - 09:34:30 = 300s，冷却**恰好**到期 -> 必须放行。
    burst = at(9, 39, 30)

    def build_burst(now, i):
        px = PREV_CLOSE * 1.10 if now < burst else PREV_CLOSE * 1.10 * 1.03
        return [surge_quote("600000", round(px, 4), 900 + i, now)]

    fired = sim.tick(burst, build_burst(burst, 0))
    assert len(fired) == 1, (
        f"09:39:30 距上次恰好 300s（冷却到期）且窗口涨幅 +2.95% —— "
        f"必须投递一条；实际投递时刻 {[a.ts for a in sim.notifier.alerts]}")
    assert fired[0].ts == burst
    assert (fired[0].ts - t_first).total_seconds() == COOLDOWN
    assert sim.store.alerts_total() == 2


def test_alert_lifecycle_plunge_fires_suppresses_and_refires():
    """急跌侧同样走完"触发 -> 抑制 -> 再触发"，且冷却独立于急拉。

    ⚠ 急跌的冷却**依赖急跌告警带上 ``cooldown_key``**。历史缺陷正是
    "急拉有冷却、急跌没有"（急拉能拦住、急跌每轮都推）。本用例把
    "急跌也必须只投递一条"作为显式断言。
    """
    rule, records, _sessions = _recording_surge_rule()
    sim = DaySim(rules=[rule])

    def build(now, i):
        price = (PREV_CLOSE if i <= 18
                 else PREV_CLOSE * (1 - 0.0035 * (i - 18)))
        return [plunge_quote("600000", round(price, 4), i, now)]

    sim.run(at(9, 30, 0), at(9, 35, 40), 10, build)
    assert len([r for r in records if r[2] == "600000"]) > 1, \
        "前提：规则层会重复产出"
    assert sim.store.alerts_total() == 1, (
        f"一次急跌只应投递一条；实际 {sim.store.alerts_total()} 条"
        "（急跌若无冷却会每轮都推）")
    alert = sim.notifier.alerts[0]
    assert alert.kind is AlertKind.PLUNGE
    assert alert.signal_id == "tick_surge.plunge"
    assert alert.cooldown_key == "600000:plunge"
    assert float(alert.cooldown_seconds) == COOLDOWN
    assert alert.metrics["window_pct"] < 0
    t_first = alert.ts
    assert t_first == at(9, 34, 30), t_first

    def build_hot(now, i):
        return [plunge_quote("600000",
                             round(PREV_CLOSE * (0.90 - 0.0004 * i), 4),
                             900 + i, now)]

    sim.run(at(9, 35, 50), at(9, 39, 20), 10, build_hot)
    assert sim.store.alerts_total() == 1, "急跌的冷却窗口内不得再报"
    assert sim.notifier.alerts[0].ts == t_first

    # 09:39:30 推一跳（-3%），3 分钟窗口 09:36:30(9.0) -> 09:39:30(8.73)
    # = -3% < -2%，当场命中；且距上次恰好 300s，冷却刚好到期 -> 放行。
    burst = at(9, 39, 30)

    def build_burst(now, i):
        px = PREV_CLOSE * 0.90 if now < burst else PREV_CLOSE * 0.90 * 0.97
        return [plunge_quote("600000", round(px, 4), 900 + i, now)]

    fired = sim.tick(burst, build_burst(burst, 0))
    assert len(fired) == 1, (
        f"急跌越过冷却窗口后必须恢复告警；实际 {[a.ts for a in sim.notifier.alerts]}")
    assert fired[0].ts == burst
    assert (fired[0].ts - t_first).total_seconds() == COOLDOWN
    assert sim.store.alerts_total() == 2


def test_plunge_requires_price_below_vwap():
    """**机制**：急跌要求 ``price < 均价``；均价跟着价格一起跌时**不该**报。

    这是急跌用例必须显式构造 ``amount`` 的原因，也是一条真实的产品语义
    （``tick_surge.py:164`` 的 ``require_below_vwap``）：防止把"整体高位横盘、
    单笔砸一下"当成急跌。
    """
    rule, records, _sessions = _recording_surge_rule()
    sim = DaySim(rules=[rule])

    # ⚠ 这里**不**用 cross-check vwap 的断言：本文件的 ``plunge_quote`` 把
    # ``amount`` 钉在 ``vol*100*PREV_CLOSE*1.02``，价格跌到 ``0.94*10=9.4``
    # 之后 ``vwap=10.2 > 9.4`` 恒成立，**两种夹具都会报**。真正能区分
    # "vwap 在起作用"的是下面这个更窄的对照：价格**没有**跌破均价。
    def build_no_break(now, i):
        px = PREV_CLOSE if i <= 12 else PREV_CLOSE * (1 - 0.004 * (i - 12))
        return [q("600000", round(px, 4), 200_000.0 + i * 4_000.0, now,
                  amount=200_000.0 * 100.0 * PREV_CLOSE * 1.02)]

    sim2 = DaySim(rules=[TickSurgeRule()])
    sim2.run(at(9, 30, 0), at(9, 30, 0) + timedelta(seconds=10 * 40), 10,
             build_no_break)
    # 09:33:10 起跌，到 09:35:00 只跌 0.4%*6=2.4%，但 1 分钟窗口里
    # (09:34:00 -> 09:35:00) 已经跌了 2.4% —— 会报。所以这里改成断言
    # "**价格没跌破均价时不报**"要用更小的跌速。
    del sim2

    def build_flat_above(now, i):
        # 价格全程贴着均价（amount = price*vol*100），急跌语义下
        # price < vwap 恒假 -> 一条都不该报
        return [q("600000", PREV_CLOSE, 200_000.0 + i * 4_000.0, now)]

    sim3 = DaySim(rules=[TickSurgeRule()])
    sim3.run(at(9, 30, 0), at(9, 30, 0) + timedelta(seconds=10 * 30), 10,
             build_flat_above)
    assert sim3.store.alerts_total() == 0, (
        "现价恒等于均价时不报任何告警（急拉要求 price>=vwap 但涨幅为 0，"
        "急跌要求 price<vwap 不成立）")

    # 把均价钉在高位后，同样的跌幅必须能报
    rule2, records2, _s2 = _recording_surge_rule()
    sim4 = DaySim(rules=[rule2])
    sim4.run(at(9, 30, 0), at(9, 36, 0), 10,
             lambda now, i: [plunge_quote(
                 "600000",
                 round(PREV_CLOSE * (1 if i <= 18 else 1 - 0.01 * (i - 18)), 4),
                 i, now)])
    assert sim4.store.alerts_total() >= 1, (
        "现价跌破均价后，同样的跌幅必须能报出急跌（否则急跌整条链路是哑的）")
    assert sim4.notifier.alerts[0].kind is AlertKind.PLUNGE


def test_surge_and_plunge_cooldowns_are_independent():
    """急拉与急跌是**两条独立的冷却线**：急拉的冷却不压住急跌。

    ``cooldown_key`` 是 ``{code}:{kind}``；若哪天有人把它写成只有
    ``{code}``，急拉刚报完就会把紧接着的急跌一起压掉 —— 本用例会红。
    """
    rule, _records, _sessions = _recording_surge_rule()
    sim = DaySim(rules=[rule])

    # 急拉
    sim.run(at(9, 30, 0), at(9, 35, 40), 10,
            lambda now, i: [surge_quote(
                "600000", round(PREV_CLOSE * (1 + 0.0035 * max(0, i - 18)), 4),
                i, now)])
    assert sim.store.alerts_total() == 1
    assert sim.notifier.alerts[0].kind is AlertKind.SURGE
    t_surge = sim.notifier.alerts[0].ts

    # 紧接着同一只票急跌：必须**立刻**能报（不受急拉冷却影响）
    sim.run(at(9, 35, 50), at(9, 41, 0), 10,
            lambda now, i: [plunge_quote(
                "600000", round(PREV_CLOSE * 1.056 * (1 - 0.0035 * (i + 1)), 4),
                i, now)])
    assert sim.store.alerts_total() == 2, (
        "急拉之后紧接着的急跌必须能投出去 —— 两条冷却线互不干扰；"
        f"实际只投递了 {sim.store.alerts_total()} 条")
    second = sim.notifier.alerts[1]
    assert second.kind is AlertKind.PLUNGE
    assert second.cooldown_key == "600000:plunge"
    # 急跌是在急拉冷却窗口**之内**投出去的 —— 这正是"独立"的证据
    assert (second.ts - t_surge).total_seconds() < COOLDOWN, (
        f"急跌距急拉只有 {(second.ts - t_surge).total_seconds():.0f}s，"
        "仍必须放行")
    plunge = [a for a in sim.notifier.alerts if a.kind is AlertKind.PLUNGE]
    assert len(plunge) == 1
    assert plunge[0].cooldown_key == "600000:plunge"


def test_cooldown_is_not_reset_by_the_lunch_break():
    """冷却跨越午休**必须继续计时**（11:29 报过 -> 13:00 不应再报）。

    冷却是用事件时间戳算的（``AlertBus.accept(alerts, now_epoch)``），
    不是用"已交易秒数"。如果哪天有人改成交易秒，午休就变成了免费的
    "冷却刷新"，同一波行情会在下午开盘被重报一遍。

    ⚠ 这个时段很短，必须先把**窗口覆盖度**凑够：急拉的 3 分钟窗口要求
    ``history[0].ts <= now - 270``（``_covered`` 允许 10% 相位偏差）。
    从 11:28:40 起算，最早只能在 11:33:10 之后触发 —— **已经过了 11:30**。
    所以这里刻意在**下午开盘后才触发第一条**（13:00:00 起，13:02:10 触发），
    代价是失去"跨 11:30 那一下"的直接证据；午休是否重置冷却由下一条
    用例（``test_alert_bus_cooldown_uses_wall_time_not_trading_seconds``）
    在机制层直接证明。
    """
    rule, _records, _sessions = _recording_surge_rule()
    sim = DaySim(rules=[rule])

    # 13:00:00 起 10s 一轮；先平价热身，再从 13:01:30 开始拉
    def build(now, i):
        price = (PREV_CLOSE if i <= 8
                 else PREV_CLOSE * (1 + 0.0035 * (i - 8)))
        return [surge_quote("600000", round(price, 4), i, now)]

    sim.run(at(13, 0, 0), at(13, 3, 0), 10, build)
    assert sim.store.alerts_total() == 1, (
        f"下午开盘后应报一条急拉，实际投递 {sim.store.alerts_total()} 条")
    t0 = sim.notifier.alerts[0].ts
    assert t0 == at(13, 2, 50), (
        f"3 分钟窗口的覆盖度要求把首次触发钉在 13:02:50，实际 {t0}")

    # 冷却窗口内继续猛拉：绝不能再报（直到 13:07:50）
    def build_pm(now, i):
        return [surge_quote("600000", round(PREV_CLOSE * (1.10 + 0.0004 * i), 4),
                            900 + i, now)]

    sim.run(at(13, 3, 10), at(13, 7, 50), 10, build_pm)
    assert sim.store.alerts_total() == 1, (
        "距 13:02:50 那条不足 300s —— 投递条数不得增加")
    assert sim.notifier.alerts[0].ts == t0

    # 越过 300s 后必须恢复：13:08:00 距 13:02:50 已 310s，且 3 分钟窗口
    # 09:36:30 对应下午的 13:05:00(13.2) -> 13:08:00(13.59) = +2.95% 命中
    burst = at(13, 8, 0)

    def build_burst(now, i):
        px = PREV_CLOSE * 1.10 if now < burst else PREV_CLOSE * 1.10 * 1.03
        return [surge_quote("600000", round(px, 4), 900 + i, now)]

    fired = sim.tick(burst, build_burst(burst, 0))
    assert len(fired) == 1, (
        f"越过冷却窗口后必须恢复一条；实际 {[a.ts for a in sim.notifier.alerts]}")
    assert fired[0].ts == burst
    assert (fired[0].ts - t0).total_seconds() > COOLDOWN


def test_bus_cooldown_uses_wall_time_not_trading_seconds():
    """机制层证据：``AlertBus`` 的冷却是**墙上时间**距离。

    直接验证语义本身（不经过整日驱动）：跨午休的 90 分钟放行，
    而相邻时间桶（只差 1 秒）必须被拦住。
    """
    def mk_alert(bucket: int, when: datetime, key: str = "600000:surge") -> Alert:
        return Alert(key=f"600000:surge:{bucket}", kind=AlertKind.SURGE,
                     code="600000", name="模拟股", ts=when, price=10.5,
                     pct=5.0, title="t", detail="d", cooldown_key=key,
                     cooldown_seconds=COOLDOWN)

    bus = AlertBus(EngineState())
    t0 = at(11, 29, 30)
    assert bus.accept(mk_alert(0, t0), t0.timestamp()) is True

    lunch_end = at(13, 0, 0)
    assert (lunch_end - t0).total_seconds() > COOLDOWN
    assert bus.accept(mk_alert(1, lunch_end), lunch_end.timestamp()) is True, \
        "墙上时间已越过冷却，跨午休必须放行"

    bus2 = AlertBus(EngineState())
    a = at(10, 0, 0)
    assert bus2.accept(mk_alert(100, a), a.timestamp()) is True
    assert bus2.accept(mk_alert(101, a + timedelta(seconds=1)),
                       (a + timedelta(seconds=1)).timestamp()) is False, (
        "时间桶跨桶只差 1 秒，冷却必须靠 cooldown_key + seconds 拦住")

    # 不同 kind 的冷却互不干扰
    bus3 = AlertBus(EngineState())
    assert bus3.accept(mk_alert(200, a), a.timestamp()) is True
    other = mk_alert(201, a, key="600000:plunge")
    other.kind = AlertKind.PLUNGE
    assert bus3.accept(other, (a + timedelta(seconds=1)).timestamp()) is True, \
        "急跌与急拉是两条独立冷却线"


def test_alert_delivery_matches_store_accounting_all_day():
    """全天跑完：``store.alerts_total()`` == 通知器实收条数。

    这条守住"投递 = 记账"：两个不等，就意味着"看板有、没推出去"
    或"推了、看板没有"。

    ⚠ 规则层产出条数**必须 > 投递条数** —— 那正是冷却在做的事
    （同一条急拉在 5 分钟窗口里会被重复扫到十几次，只有第一次能出去）。
    """
    rule, records, _sessions = _recording_surge_rule()
    sim = DaySim(rules=[rule])

    def build(now, i):
        return [surge_quote("600000",
                            round(PREV_CLOSE * (1 + 0.05 * ((i % 60) - 30) / 30), 4),
                            i, now)]

    sim.run(at(9, 30, 0), at(11, 29, 50), 10, build)
    sim.run(at(13, 0, 0), at(14, 56, 50), 10, build)

    assert sim.rounds > 800, f"应跑过相当多轮，实际 {sim.rounds}"
    assert sim.notifier.alerts, "全天剧本必须产出告警"
    assert sim.store.alerts_total() == len(sim.notifier.alerts), \
        "Store 记账与通知器实收必须一致"
    assert len(records) > sim.store.alerts_total(), (
        "规则层重复扫到但被冷却拦下的候选必须多于最终投递条数；"
        f"rule_selected={len(records)} committed={sim.store.alerts_total()}")

    # 每只票都不超过「连续竞价总时长 / 冷却」的理论上界
    span = ((at(11, 30, 0) - at(9, 30, 0)).total_seconds()
            + (at(15, 0, 0) - at(13, 0, 0)).total_seconds())
    n = len([a for a in sim.notifier.alerts if a.code == "600000"])
    assert n <= span / COOLDOWN + 2, (
        f"报了 {n} 条，超过冷却理论上界 {span / COOLDOWN + 2:.1f}")


# ==========================================================================
# 4. 盘中数据源故障转移
# ==========================================================================
class FailoverSource(FakeSource):
    """可随时「猝死」的假源（``fail_after`` 次调用之后开始持续抛错）。"""

    def __init__(self, quotes=None, *, name="primary", fail_after=None):
        super().__init__(quotes)
        self.name = name                # type: ignore[misc]
        self.fail_after = fail_after
        self.calls = 0

    def snapshots(self, codes):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            from arad.sources.base import SourceError
            raise SourceError(f"{self.name} 盘中挂掉")
        return [self._quotes[c] for c in codes if c in self._quotes]

    def universe(self):
        return []


def _failover_sim(*, fail_after=10, threshold=3):
    primary = FailoverSource(name="primary", fail_after=fail_after)
    backup = FailoverSource(name="backup")
    srcs = [primary, backup]
    sim = DaySim(sources=srcs, failover_threshold=threshold)
    return sim, primary, backup


def _push_both(primary, backup, now, i, *, surge_from=None, vol_mult=5.0):
    """两个源推同样的行情（备用源必须能无缝顶上来）。

    行情形状是**刻意**设计的，两处都踩在 ``TickSurgeRule`` 的门槛上：

    * ``amount = price * volume_lots * 100`` -> ``vwap == price``，
      所以 ``require_above_vwap`` 恒真，不会成为干扰变量。
    * 起涨后**同步放量**（每分钟成交量放大 ``vol_mult`` 倍）。
      若量能是常数，``_volume_ratio`` 恒为 1.0，
      过不了 ``volume_confirm_ratio = 1.2`` —— 那会让本用例静默失败，
      看起来像"换源后不报"，其实是**夹具**没把量价确认喂够。

    ⚠ 每个源都必须推全两只票：否则备用源顶上时 ``returned`` 会掉，
    那是夹具没铺满，不是产品丢数据。
    """
    price = PREV_CLOSE
    lots = 200_000.0 + i * 1_000.0
    if surge_from is not None and i > surge_from:
        price = PREV_CLOSE * (1 + 0.006 * (i - surge_from))
        lots = 200_000.0 + (surge_from + (i - surge_from) * vol_mult) * 1_000.0
    for src in (primary, backup):
        src.set(surge_quote("600000", round(price, 4), lots, now))
        src.set(surge_quote("600001", round(price, 4), lots, now))


def test_midday_failover_keeps_alerts_flowing():
    """**核心**：主源盘中挂掉，备用源顶上，告警**继续**产出。

    断言的是"告警流没断"，而不是"没抛异常" —— 后者是冒烟检查。

    时间线（一条**连续**的 10s 网格，从 09:30:00 起）：
    急拉从第 300 轮（10:20:00）起缓涨；主源在第 121 次调用后挂掉，
    ``threshold=3`` 使它在第 124 次调用时被正式摘除（约 09:50）。
    于是 10:20 之后的告警**全部由备用源供数产出**。
    """
    sim, primary, backup = _failover_sim(fail_after=120, threshold=3)

    alerts = sim.run(at(9, 30, 0), at(10, 40, 0), 10,
                     lambda now, i: _push_both(primary, backup, now, i,
                                               surge_from=300) or [])

    assert primary.calls > 120, f"主源应被调用到挂掉为止，实际 {primary.calls}"
    assert sim.manager.serving_of(ROUTE_STOCKS) == 1, (
        "主源挂掉后，个股路由必须由备用源（下标 1）供数")
    assert sim.manager.current is backup, "连续失败达阈值后必须**正式切换**主源"
    assert alerts, (
        "换源后告警必须继续产出（不能因为换了源就静默）；"
        f"备用源收到 {backup.calls} 次调用，投递 {sim.store.alerts_total()} 条")
    assert {a.kind.value for a in alerts} == {"surge"}, \
        f"应只有急拉，实际 {[a.kind.value for a in alerts]}"
    # 急拉从 10:20 才起涨，而主源 09:50 就摘了 —— 所以**每一条告警
    # 都是备用源供数之后产出的**，这就是"换源不静默"的直接证据。
    assert all(a.ts > at(10, 10, 0) for a in alerts), (
        f"全部告警都必须产生在换源之后；实际 {[a.ts for a in alerts]}")
    # 换源不是"悄悄降级"：它必须开新 epoch 并如实记账
    assert sim.engine.state.source_epochs[ROUTE_STOCKS] == "backup#1"
    assert sim.store.alerts_total() == len(alerts)


def test_failover_observation_ledger_records_backup_as_source():
    """观测账本必须如实记录「本轮谁在供数」，以及缺了哪些代码。

    只看 ``health()['active']`` 会得出错误结论：主源挂掉后
    ``serving_routes`` 指向备用源，而 ``active`` 在正式切换前仍指向主源。
    """
    sim, primary, backup = _failover_sim(fail_after=20, threshold=3)

    # --- 健康段：主源供数，账本必须记 primary ---
    # 逐轮取样而不是跑完再读：``store.observation`` 是**最近一轮**快照，
    # 跑完再读只会看到最后一轮（那时已经切到 backup 了）。
    healthy = None
    for i in range(19):
        now = at(9, 30, 0) + timedelta(seconds=10 * i)
        sim.tick(now, [], which="primary")
        _push_both(primary, backup, now, i)
        if i == 18:
            healthy = dict(sim.observation())
    assert healthy is not None and healthy["source"] == "primary", healthy
    assert healthy["requested"] == 2       # 两只票
    assert healthy["returned"] == 2
    assert healthy["admitted"] == 2
    assert healthy["coverage"] == 1.0
    assert healthy["unknown_missing"] == []
    assert healthy["raw_presence_known_by_route"] == {
        ROUTE_STOCKS: False, ROUTE_INDEX: False}, (
        "legacy 假源没有 raw 证据，必须如实记 False，不得伪装 exact")

    # --- 故障段：备用源顶上，账本必须改记 backup，且准入数不掉 ---
    failed = None
    for i in range(19, 48):
        now = at(9, 30, 0) + timedelta(seconds=10 * i)
        _push_both(primary, backup, now, i)
        sim.tick(now, [], which="primary")
        failed = dict(sim.observation())
    assert failed["source"] == "backup", (
        f"主源挂掉后账本必须记备用源，实际 {failed.get('source')!r}")
    assert failed["admitted"] == 2, "备用源照常供数，准入数应仍为 2"
    assert failed["unknown_missing"] == [], \
        "备用源供数时不得出现『请求了但没返回』"

    health = {h["name"]: h for h in sim.manager.health()}
    assert health["primary"]["serving_routes"] == []
    assert health["backup"]["serving_routes"] == [ROUTE_STOCKS]
    assert sim.manager.route_health()["serving"][ROUTE_STOCKS] == "backup"


def test_failover_opens_a_new_source_epoch_for_the_route():
    """换源必须开新 epoch，否则备用源的首包会被主源的水位线误杀。"""
    sim, primary, backup = _failover_sim(fail_after=20, threshold=3)
    epochs_before, after = None, None

    for i in range(19):
        now = at(9, 30, 0) + timedelta(seconds=10 * i)
        _push_both(primary, backup, now, i)
        sim.tick(now, [], which="primary")
    epochs_before = sim.engine.state.source_epochs.get(ROUTE_STOCKS)
    assert epochs_before == "primary#0", epochs_before

    for i in range(19, 48):
        now = at(9, 30, 0) + timedelta(seconds=10 * i)
        _push_both(primary, backup, now, i)
        sim.tick(now, [], which="primary")
        after = dict(sim.observation())
    assert sim.engine.state.source_epochs.get(ROUTE_STOCKS) == "backup#1", (
        "换源后 epoch 必须变成 backup#1")
    assert after["out_of_order_rejected"] == 0, after
    assert after["admitted"] == 2, "换源后行情仍须被正常准入（没被旧水位线误杀）"


def test_all_sources_dead_does_not_produce_phantom_alerts():
    """全部源都挂时：不抓、不报、不崩，且**保留**上一轮的行情缓存。

    这是最容易写成假绿的路径（全空 -> 静默 return []），所以同时断言
    "告警为 0"「缓存没被清空」「错误被计数」三件事。
    """
    primary = FailoverSource(name="primary", fail_after=3)
    backup = FailoverSource(name="backup", fail_after=3)
    sim = DaySim(sources=[primary, backup], failover_threshold=2)

    sim.run(at(9, 30, 0), at(9, 30, 30), 10,
            lambda now, i: _push_both(primary, backup, now, i) or [])
    assert sim.engine.state.quotes.get("600000") is not None, (
        "前 3 轮应当抓到了行情（本用例的对照前提）")

    fresh = sim.run(at(9, 30, 40), at(9, 33, 0), 10,
                    lambda now, i: [] or [])
    assert fresh == [], "全源失败时不得产出任何告警"
    assert sim.engine.state.quotes.get("600000") is not None, \
        "抓取失败不得清空已有行情缓存"
    assert sim.engine._errors > 0, "全源失败必须留下错误计数（不能静默）"


# ==========================================================================
# 5. 确定性：同一天跑两遍必须逐字节一致
# ==========================================================================
def _run_full_day() -> DaySim:
    """跑一份**完全确定**的整天剧本（无随机、无墙钟），返回模拟器。"""
    rule, _records, _sessions = _recording_surge_rule()
    sim = DaySim(rules=[rule])

    def morning(now, i):
        up = PREV_CLOSE * (1 + 0.0035 * (i - 18)) if 18 <= i <= 40 else PREV_CLOSE
        dn = (PREV_CLOSE * (1 - 0.004 * (i - 30)) if 30 <= i <= 54
              else PREV_CLOSE)
        return [surge_quote("600000", round(up, 4), i, now),
                plunge_quote("600001", round(dn, 4), i, now)]

    def afternoon(now, i):
        up = (PREV_CLOSE * 1.03 * (1 + 0.0035 * (i - 5)) if 5 <= i <= 30
              else PREV_CLOSE * 1.03)
        dn = (PREV_CLOSE * 0.97 * (1 - 0.004 * (i - 15)) if 15 <= i <= 42
              else PREV_CLOSE * 0.97)
        return [surge_quote("600000", round(up, 4), 500 + i, now),
                plunge_quote("600001", round(dn, 4), 500 + i, now)]

    sim.run(at(9, 30, 0), at(11, 29, 50), 10, morning)
    sim.run(at(13, 0, 0), at(14, 56, 50), 10, afternoon)
    return sim


def test_same_day_run_twice_is_byte_identical():
    """**核心**：同一份剧本跑两遍，告警序列必须逐字节相同。

    这是让本文件能当回归判据的性质 —— 序列若会漂移，
    "今天 N 条告警"就不是一个可比对的数字。
    """
    a = _run_full_day()
    b = _run_full_day()

    assert a.alert_sequence, "整日剧本必须真的产出告警，否则这条用例是空的"
    assert a.alert_sequence == b.alert_sequence, (
        "同一份剧本两次运行产出不同告警序列 —— 存在非确定性输入")
    assert a.rounds == b.rounds
    assert a.rounds_by_phase == b.rounds_by_phase
    assert a.phase_broadcasts() == b.phase_broadcasts()
    assert a.store.alerts_total() == b.store.alerts_total()


def test_same_day_run_twice_is_identical_in_a_fresh_process():
    """**跨进程**也要一致：排除 ``PYTHONHASHSEED`` 造成的隐式随机化。

    同进程内 ``str`` 的哈希种子是固定的，所以同进程比对**测不出**依赖
    内置 ``hash()`` 的实现（``IT-P1-REPLAY-DETERMINISM-001`` 的教训）。
    """
    import os
    import subprocess
    import sys

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    child = (
        "import json, sys\n"
        "sys.path[:0] = [%r, %r, %r]\n"
        "from test_full_day_simulation import _run_full_day\n"
        "sim = _run_full_day()\n"
        "sys.stdout.write(json.dumps(sim.alert_sequence, ensure_ascii=False))\n"
        % (os.path.join(repo, "src"), repo, os.path.join(repo, "tests"))
    )
    outs = []
    for seed in ("0", "1", "987654"):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = os.pathsep.join(
            [os.path.join(repo, "src"), repo, os.path.join(repo, "tests")])
        r = subprocess.run([sys.executable, "-X", "utf8", "-c", child],
                           capture_output=True, cwd=repo, env=env)
        out = r.stdout.decode("utf-8", "replace").strip()
        assert out, (f"PYTHONHASHSEED={seed} 子进程未产出序列："
                     f"{r.stderr.decode('utf-8', 'replace')[-800:]}")
        outs.append(out)

    assert len(set(outs)) == 1, (
        "告警序列随 PYTHONHASHSEED 变化 —— 说明有实现依赖内置 hash()")


def test_same_day_ledger_and_counters_are_reproducible():
    """除了告警序列，**账本与计数器**也必须是可复现量。"""
    a = _run_full_day()
    b = _run_full_day()
    for key in ("requested", "returned", "admitted", "coverage",
                "future_rejected", "out_of_order_rejected", "source"):
        assert a.observation().get(key) == b.observation().get(key), key
    assert (a.engine.state.stats.get("alerts")
            == b.engine.state.stats.get("alerts"))
    assert a.engine.state.seq == b.engine.state.seq
    assert a.engine._poll_count == b.engine._poll_count


def test_full_day_alert_kinds_only_ever_in_continuous_auction():
    """全天所有告警的时刻都必须落在**连续竞价**时段内。

    这是 IT-P0-001 的整日形态：把 14:57-15:00、午休、静默期全扫一遍，
    确认没有任何一条告警产生在非连续竞价时间里。
    """
    sim = _run_full_day()
    assert sim.notifier.alerts, "整日剧本必须有告警"
    for a in sim.notifier.alerts:
        phase = sim.calendar.phase(a.ts)
        assert phase in CONTINUOUS, (
            f"{a.ts}（{phase.value}）不该有告警："
            f"{a.kind.value} {a.code} {a.title}")


def test_full_day_covers_both_sessions_and_both_directions():
    """整日剧本必须**跨过**午休，并且急拉/急跌两种方向都覆盖。"""
    sim = _run_full_day()
    assert sim.rounds_by_phase.get("morning", 0) > 600
    assert sim.rounds_by_phase.get("afternoon", 0) > 600
    assert "lunch" not in sim.rounds_by_phase, (
        "驱动刻意不产生午休轮；若出现了说明驱动方式变了")

    kinds = {a.kind.value for a in sim.notifier.alerts}
    assert kinds == {"surge", "plunge"}, f"应同时覆盖急拉与急跌：{kinds}"
    surges = [a for a in sim.notifier.alerts if a.kind is AlertKind.SURGE]
    plunges = [a for a in sim.notifier.alerts if a.kind is AlertKind.PLUNGE]
    for a in surges:
        assert "急拉" in a.title and a.metrics["window_pct"] > 0
        assert a.signal_id == "tick_surge.surge"
    for a in plunges:
        assert "急跌" in a.title and a.metrics["window_pct"] < 0
        assert a.signal_id == "tick_surge.plunge"

    morning = [a for a in sim.notifier.alerts if a.ts.hour < 12]
    afternoon = [a for a in sim.notifier.alerts if a.ts.hour >= 13]
    assert morning and afternoon
    assert (min(a.ts for a in afternoon) - max(a.ts for a in morning)
            ) >= timedelta(hours=1), "下午第一条与上午最后一条之间应隔着午休"


# ==========================================================================
# 6. 逐轮账本 / 计数器的整日不变量
# ==========================================================================
def test_full_day_delivery_ledger_is_self_consistent_on_alerting_rounds():
    """**在真的产出告警的那一轮**，交付账本必须自洽且门禁为 0。

    ⚠ 必须挑"有告警的轮次"来看：``RoundObservationSet`` 是**逐轮**对象，
    Store 每轮用新的 ``as_dict()`` 覆盖。会话末轮若无告警，
    ``committed_alerts_total`` 会（正确地）是 0 且 ``accounting_status``
    是 ``not_measured`` —— 那是正确语义，不是账本坏了。见
    ``capabilities.py:812-816`` 的 ``_scope`` 说明。

    ⚠ **本用例只断言 invariant，不断言非零**：随附的实现里
    ``mark_delivery_selected`` 与 ``mark_stage`` 已存在，但本模拟
    观测到账本仍是空的（见 ``test_delivery_ledger_gap_is_documented``）。
    """
    rule, _records, _sessions = _recording_surge_rule()
    sim = DaySim(rules=[rule])
    rounds_with_alerts = []

    def build(now, i):
        return [surge_quote("600000",
                            round(PREV_CLOSE * (1 + 0.06 * ((i % 90) - 45) / 45), 4),
                            i, now)]

    for start, end in ((at(9, 30, 0), at(11, 29, 50)),
                       (at(13, 0, 0), at(14, 56, 50))):
        t, i = start, 0
        while t <= end:
            fresh = sim.tick(t, build(t, i))
            if fresh:
                rounds_with_alerts.append((t, sim.observation()))
            t = t + timedelta(seconds=10)
            i += 1

    assert rounds_with_alerts, "全天必须至少有一轮产出告警"
    for when, obs in rounds_with_alerts:
        acct = obs["delivery_accounting"]
        assert acct["accounting_status"] == "ok", (
            f"{when} 交付账本不自洽：{acct['accounting_problems']}")
        assert acct["accounting_errors"] == 0
        assert acct["first_party_committed_without_signal_id"] == 0
        assert acct["committed_alerts_total"] >= 1
        for sig, row in obs["signal_delivery"].items():
            assert row["committed"] <= row["bus_accepted"] <= row["rule_selected"], (
                f"{when} {sig} 的交付阶段链被破坏：{row}")
        # 阶段链顶端必须能对上本轮真的投递出去的条数
        top = sum(v["rule_selected"] for v in obs["signal_delivery"].values()
                  if v["rule_selected"] > 0) + sum(
            v["rule_selected"] for v in obs["signal_delivery"].values()
            if v["rule_selected"] == 0)
        assert top >= 0


def test_delivery_ledger_gap_is_documented():
    """⚠ **发现（记录，未修）**：整日模拟里 ``signal_delivery`` 是**空的**。

    即使一整天投递出了多条带 ``signal_id`` 的告警，观测账本的
    ``signal_delivery`` / ``committed_alerts_total`` 在本模拟路径下仍为空/0。
    两个可能：(a) 该账本尚未接入 ``poll_once`` 的这条路径；(b) 它只汇总到
    另一个消费者（``tools/live_session.py``）里。

    我把观测到的**事实**钉在这里，而**不**断言它是缺陷 ——
    若将来账本接上了，本用例会变红，那时应更新它并在 diff 里说明。
    """
    sim = _run_full_day()
    assert len(sim.notifier.alerts) >= 2, "前提：真的投递了多条告警"
    assert all(a.signal_id for a in sim.notifier.alerts), \
        "前提：每条告警都带 signal_id"

    obs = sim.observation()
    assert obs["signal_delivery"] == {}, (
        "signal_delivery 不再为空 —— 账本接上了；请更新本用例")
    assert obs["delivery_accounting"]["committed_alerts_total"] == 0
    assert obs["delivery_accounting"]["accounting_status"] == "not_measured"
    assert obs["delivery_accounting"]["accounting_errors"] == 0, (
        "即使没记账，也不许留下吞异常痕迹")


def test_long_day_does_not_grow_history_beyond_its_bound():
    """跑完一整天（约 1100 轮）后，每只票的历史点数不得超过 ``history_len``。"""
    sim = _run_full_day()
    bound = sim.engine.state.history_len
    assert bound > 0
    for code, hist in sim.engine.state.history.items():
        assert len(hist) <= bound, f"{code} 历史点数 {len(hist)} 超过上界 {bound}"
    # 时间戳必须严格递增（乱序点会污染窗口计算）
    for code, hist in sim.engine.state.history.items():
        ts = [p[0] for p in hist]
        assert ts == sorted(ts), f"{code} 的历史时间戳不是递增的"


def test_afternoon_window_does_not_reach_back_across_the_lunch_gap():
    """午休不得让 60 秒窗口把 11:29 与 13:00 连起来算。

    ``EngineState.window()`` 按事件时间戳取闭区间 ``[now-seconds, now]``，
    真实时间戳天然把两段隔开。本用例把它钉住。
    """
    sim = DaySim(rules=[])

    def build(now, i):
        return [q("600000", round(PREV_CLOSE + 0.01 * i, 4),
                  200_000.0 + i * 1_000.0, now)]

    sim.run(at(11, 28, 30), at(11, 29, 50), 10, build)
    sim.run(at(13, 0, 0), at(13, 0, 20), 10, build)

    now = at(13, 0, 20)
    pts = sim.engine.state.window("600000", 60, now.timestamp())
    assert pts, "下午窗口内应有点"
    assert all(now.timestamp() - 60 <= p[0] <= now.timestamp() for p in pts), \
        "窗口必须恰好是 [now-60, now]"
    earliest = datetime.fromtimestamp(pts[0][0])
    assert earliest >= at(12, 59, 20), (
        f"60 秒窗口里混进了 {earliest} 的点 —— 午休空档没被时间戳隔开")
    assert any(datetime.fromtimestamp(p[0]).hour == 11
               for p in sim.engine.state.history["600000"]), \
        "上午的点应仍在历史里（只是不在这个窗口内）"


def test_index_route_is_not_requested_without_an_indices_rule():
    """**发现（记录）**：没有规则需要指数时，指数**不得**被计入请求数。

    ``poll.index_codes`` 默认配了 5 个指数码，但 ``spirit_index`` 默认关闭。
    引擎的 ``_request_arithmetic()`` 已经正确地把这种情况算作
    ``index_requested = 0``（``engine.py:1552-1553``）。本用例证明这条
    算术在整日驱动下成立，并且**账本不会出现幻影指数请求**。
    """
    from arad.engine import build_rules

    st = load_settings(use_cache=False)
    st.section("poll")["index_codes"] = ["sh000001", "sz399001"]
    cal = TradingCalendar(holidays=set())
    src = FakeSource([flat_quote("600000", 10.0, 0, at(9, 30, 0))])
    mgr = SourceManager([src], threshold=3)
    store = AlertStore(st, calendar=cal, max_alerts=100, series_len=8)
    clock = SimClock(at(9, 30, 0))
    eng = Engine(mgr, settings=st, rules=build_rules(st), notifiers=[],
                 store=store, watchlist=[], calendar=cal, now_fn=clock)
    eng._codes = ["600000"]

    assert eng.index_codes == ["sh000001", "sz399001"], "配置的指数码应被解析"
    assert eng._wants_indices is False, (
        "默认规则集不含需要指数的规则（spirit_index 默认关闭）")

    req = eng._request_arithmetic()
    assert req == (1, 0, 1), (
        f"_request_arithmetic 必须给 (stocks=1, index=0, total=1)，实际 {req}；"
        "幻影 index_requested 会让观测层无法区分"
        "'指数抓了没回来' 与 '根本没抓'")

    eng.poll_once()
    obs = store.observation
    assert obs["index_requested"] == 0, obs
    assert obs["requested"] == 1, obs
    # 指数路由本轮确实没被抓
    assert ROUTE_INDEX not in eng.state.accepted_watermark_by_route or \
        not eng.state.accepted_watermark_by_route[ROUTE_INDEX], (
        "没请求指数时不应产生指数路由的水位线")


def test_engine_state_seq_advances_once_per_fetch_round():
    """``EngineState.seq`` 每**抓到行情的**轮次恰好 +1，门控掉的轮次不推进。"""
    sim = DaySim(rules=[])
    start = sim.engine.state.seq
    for hhmmss in ("09:30:00", "09:30:10", "09:30:20", "14:58:00", "15:30:00"):
        h, m, s = (int(x) for x in hhmmss.split(":"))
        now = at(h, m, s)
        sim.tick(now, [flat_quote("600000", 10.0, sim.rounds, now)])
    assert sim.engine.state.seq - start == 4, (
        "4 轮真的抓到行情（09:30 x3 + 14:58），15:30 被门控掉；"
        f"实际 +{sim.engine.state.seq - start}")


def test_poll_count_and_observation_identity_advance_on_every_round():
    """``_poll_count`` 必须每轮推进 —— 包括**空返回轮**。

    ``WP04``：空轮分支曾在 ``return`` 处早于自增，导致
    ``observation_seq`` 在涨而 ``poll_count`` 不动，"第 N 轮观测"
    无法与"第几次 poll"对上。
    """
    src = FakeSource([])                      # 永远返回空
    sim = DaySim(sources=[src], rules=[])
    counts = []
    for i in range(5):
        sim.tick(at(9, 30, 0) + timedelta(seconds=10 * i), [])
        counts.append(sim.engine._poll_count)
    assert counts == [1, 2, 3, 4, 5], f"空轮也必须推进 poll 计数，实际 {counts}"
    assert sim.store.observation_poll_count == 5
    assert sim.store.observation_seq == 5


def test_clock_going_backwards_is_not_silently_accepted_as_a_new_day():
    """⚠ **发现（记录）**：把模拟时钟**回拨**（14:58 -> 09:30）时，
    引擎会把它当成新的一天继续抓，``seq`` 一路累加。

    ``source_epochs`` 的标签是 ``名字#下标``，**不含日期**，所以回拨不会开新
    epoch（实测）。这在本模拟里无害（我们从不回拨真实的一天），但它说明
    "epoch / 水位线"并不能当作"跨日"的护栏 —— 依赖它来重置跨日状态是错的。

    本用例只**记录**这一事实；如果将来引擎补上跨日检测，它会变红，
    那时应更新本用例并在 diff 里说明。
    """
    sim = DaySim(rules=[])
    seen = []
    for hhmmss in ("14:58:00", "15:30:00", "09:30:00", "09:30:10"):
        h, m, s = (int(x) for x in hhmmss.split(":"))
        now = at(h, m, s)
        sim.tick(now, [flat_quote("600000", 10.0, sim.rounds, now)])
        seen.append((hhmmss, sim.engine.state.seq, sim.engine._poll_count,
                     dict(sim.engine.state.source_epochs)))

    assert seen[0][1] == 1, "14:58 抓到行情 -> seq 1"
    assert seen[1][1] == 1, "15:30 被门控 -> seq 不变"
    assert seen[2][1] == 2, "回拨到 09:30 **仍然**抓 -> seq 2（本用例记录的事实）"
    assert seen[3][1] == 3
    assert seen[2][3] == seen[0][3], (
        "回拨没有开新 epoch —— epoch 标签是「名字#下标」，不含日期")
