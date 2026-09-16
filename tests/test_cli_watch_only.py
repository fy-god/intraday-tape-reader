"""``watch_only`` 模式（引擎 + CLI）的测试。

覆盖范围
--------
* ``Engine.run_forever(watch_only=True)``：股票池取自自选并 pin、跳过开跑前的
  全市场刷新、走 ``poll.watchlist_seconds`` 间隔、空自选立即返回不空转。
* ``watch_only=False``（默认）的旧行为不被破坏：仍然刷新股票池、仍然在降级时
  打 "自选股降级模式" 警告。
* CLI：``serve --watch-only`` 解析与向 ``run_forever`` 的透传。

离线纪律（见 docs/DATA_CONTRACT.md 第 9 节）
-------------------------------------------
* 绝不 ``Engine(source=None)`` —— 那会构造真实网络数据源链；一律注入 ``FakeSource``。
* ``load_settings(use_cache=False)``：``load_settings()`` 返回进程内共享的**可变**
  单例，改它会污染同进程其它测试。
* 循环间隔通过替换 ``arad.engine.time`` 观测，测试本身**不真的睡**。
"""
from __future__ import annotations

import argparse
import logging

import pytest

import arad.cli as cli
import arad.engine as engine_mod
from arad.engine import Engine
from arad.models import Alert, AlertKind
from arad.session import SessionPhase, TradingCalendar
from arad.store import AlertStore

from fakes import FakeSource, RecordingNotifier, make_quote

DEGRADE_WARNING = "自选股降级模式"


# ==========================================================================
# 测试替身 / 辅助
# ==========================================================================
def _settings():
    """每个测试拿到**独立**的 Settings 副本（``use_cache=False`` 既不吃也不写缓存）。"""
    from arad.config import load_settings

    return load_settings(use_cache=False)


def _morning_cal():
    """永远处于早盘时段的日历，绕过真实时钟与节假日。"""
    cal = TradingCalendar(holidays=set())
    cal.phase = lambda now=None: SessionPhase.MORNING      # type: ignore[method-assign]
    return cal


class _StubRule:
    """固定产出一条告警的桩规则。"""

    name = "stub"

    def __init__(self, code: str = "600000", kind: AlertKind = AlertKind.SURGE):
        self.code = code
        self.kind = kind

    def evaluate(self, snap, ctx):
        q = snap.quotes.get(self.code)
        if q is None:
            return []
        return [Alert(key=f"{self.code}:{self.kind.value}:0", kind=self.kind,
                      code=self.code, name=q.name, ts=snap.ts, price=q.price,
                      pct=q.pct, title="桩告警", detail="d")]


class _UniverseSpySource(FakeSource):
    """额外记录 ``universe()`` 被调用几次，用来证明"没抓全市场"。"""

    def __init__(self, quotes=None, **kw):
        super().__init__(quotes, **kw)
        self.universe_calls = 0

    def universe(self):
        self.universe_calls += 1
        return super().universe()


class _FakeTime:
    """替换 ``arad.engine`` 里的 ``time`` 模块。

    ``sleep`` 只把虚拟时钟往前推并记录时长 —— 主循环的退避逻辑照常执行，
    但测试不花真实时间。``sum(slept)`` 因此等于本轮实际使用的轮询间隔。
    """

    def __init__(self, start: float = 1000.0):
        self._t = float(start)
        self.slept: list[float] = []

    def time(self) -> float:
        return self._t

    def perf_counter(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        seconds = float(seconds)
        self.slept.append(seconds)
        self._t += seconds


def _make_engine(*, watchlist, quotes=None, rules=None, notifiers=None,
                 settings=None, source=None):
    st = settings or _settings()
    cal = _morning_cal()
    src = source if source is not None else FakeSource(
        quotes if quotes is not None else [make_quote(code="600000", price=10.0,
                                                      volume_lots=100000)])
    store = AlertStore(st, calendar=cal)
    eng = Engine(src, settings=st, rules=rules if rules is not None else [],
                 notifiers=notifiers if notifiers is not None else [],
                 store=store, watchlist=list(watchlist), calendar=cal)
    return eng, src, store


def _boom_refresh_recorder(eng, monkeypatch):
    """把 ``refresh_universe`` 换成"记录 + 抛 AssertionError"。

    只断言"抛异常"是不够的：``run_forever`` 对轮询异常是**吞掉**的
    （``except Exception`` 只计数）。所以必须断言记录列表为空，
    再配合 ``eng._errors == 0``，才能证明它真的没被调用过。
    """
    calls: list[tuple] = []

    def spy(*a, **kw):
        calls.append((a, kw))
        raise AssertionError("watch_only 模式不得联网刷新股票池")

    monkeypatch.setattr(eng, "refresh_universe", spy)
    return calls


# ==========================================================================
# 1/2. 股票池取自自选并 pin
# ==========================================================================
def test_watch_only_sets_codes_from_watchlist_and_pins(monkeypatch):
    eng, src, _ = _make_engine(watchlist=["600000", "000001"])
    eng._codes = []                                  # 起始为空

    monkeypatch.setattr(engine_mod, "time", _FakeTime())
    eng.run_forever(max_rounds=1, watch_only=True)

    assert eng._codes == ["600000", "000001"]
    assert eng._codes_pinned is True, "赋值即 pin；否则下一轮 poll_once 会联网覆盖"
    assert eng.running is False


def test_watch_only_replaces_stale_universe_codes(monkeypatch):
    """自选模式必须**整体替换**旧股票池，不能把全市场代码留在里面。"""
    quotes = [make_quote(code="600000"), make_quote(code="999999")]
    eng, src, _ = _make_engine(watchlist=["600000"], quotes=quotes)
    eng._codes = ["999999"]                          # 上一轮留下的全市场残留

    monkeypatch.setattr(engine_mod, "time", _FakeTime())
    eng.run_forever(max_rounds=1, watch_only=True)

    assert eng._codes == ["600000"]
    assert eng._codes_pinned is True


def test_watch_only_polls_only_watchlist_codes(monkeypatch):
    """端到端：真正抓行情时只请求自选股代码。"""
    quotes = [make_quote(code="600000"), make_quote(code="000001")]
    src = _UniverseSpySource(quotes)
    rec = RecordingNotifier()
    rule = _StubRule(code="600000")
    eng, src, _ = _make_engine(watchlist=["600000"], rules=[rule],
                               notifiers=[rec], source=src)

    monkeypatch.setattr(engine_mod, "time", _FakeTime())
    seen: list[int] = []
    eng.run_forever(max_rounds=1, watch_only=True,
                    on_round=lambda n, a: seen.append(n))

    assert seen == [1], "必须真的跑了一轮（否则本测试无意义）"
    assert src.calls == [["600000"]], "只应抓自选股"
    assert src.universe_calls == 0, "不应抓全市场"
    assert rec.codes == ["600000"], "自选模式仍须正常出告警"


# ==========================================================================
# 2. 不调用 refresh_universe / 开跑前的 _maybe_refresh_universe
# ==========================================================================
def test_watch_only_never_calls_refresh_universe(monkeypatch):
    """把 ``refresh_universe`` 换成炸弹，run_forever 仍须安然跑完。"""
    eng, src, _ = _make_engine(watchlist=["600000"])
    calls = _boom_refresh_recorder(eng, monkeypatch)

    monkeypatch.setattr(engine_mod, "time", _FakeTime())
    seen: list[int] = []
    eng.run_forever(max_rounds=2, watch_only=True,
                    on_round=lambda n, a: seen.append(n))

    assert calls == [], "watch_only 模式绝不允许联网刷新股票池"
    assert seen == [1, 2], "炸弹没被触发 -> 两轮都应正常跑完"
    assert eng._errors == 0, "轮询异常会被吞掉，必须显式确认没有发生"
    assert eng._codes == ["600000"]


def test_watch_only_skips_preloop_universe_refresh(monkeypatch):
    """开跑前那次刷新（``force=True``）必须被跳过。

    ``poll_once`` 自己每轮也会调一次 ``_maybe_refresh_universe()``（此时已被 pin
    短路，不联网）。所以判据不是"有没有被调用"，而是**调用次数正好等于轮数**：
    一旦开跑前那次没被跳过，就会多出一次。
    """
    eng, src, _ = _make_engine(watchlist=["600000"])
    forced: list[bool] = []

    def spy(*, force=False):
        forced.append(force)

    monkeypatch.setattr(eng, "_maybe_refresh_universe", spy)
    monkeypatch.setattr(engine_mod, "time", _FakeTime())
    rounds: list[int] = []
    eng.run_forever(max_rounds=2, watch_only=True,
                    on_round=lambda n, a: rounds.append(n))

    assert rounds == [1, 2], "必须真的跑了两轮"
    assert len(forced) == 2, (
        "watch_only 只允许 poll_once 内部每轮一次；多出来的一次就是开跑前刷新。"
        "实际调用=%r" % (forced,))
    assert forced == [False, False], (
        "watch_only 下 self._codes 已非空，即便误调用也只能是 force=False；"
        "出现 force=True 说明走了 force=not self._codes 那条启动分支。实际=%r"
        % (forced,))


def test_normal_mode_refreshes_universe_at_startup(monkeypatch):
    """对照组：默认模式**必须**做开跑前的强制刷新（含 force=True）。"""
    eng, src, _ = _make_engine(watchlist=["600000"])
    forced: list[bool] = []

    def spy(*, force=False):
        forced.append(force)

    monkeypatch.setattr(eng, "_maybe_refresh_universe", spy)
    monkeypatch.setattr(engine_mod, "time", _FakeTime())
    eng.run_forever(max_rounds=1)

    assert True in forced, "默认模式启动时应 force 刷新一次股票池；实际=%r" % (forced,)
    assert len(forced) == 2, (
        "默认模式 = 开跑前 1 次 + 每轮 1 次 = 2 次；实际=%r" % (forced,))


def test_watch_only_does_not_reset_pin(monkeypatch):
    """整轮跑完 pin 仍是 True —— 说明刷新路径一次都没走。"""
    eng, src, _ = _make_engine(watchlist=["600000"])
    _boom_refresh_recorder(eng, monkeypatch)

    monkeypatch.setattr(engine_mod, "time", _FakeTime())
    eng.run_forever(max_rounds=3, watch_only=True)

    assert eng._codes_pinned is True
    assert eng._errors == 0


def test_watch_only_pin_survives_ttl_expiry(monkeypatch):
    """``universe_refresh_seconds`` 到点也不能触发刷新（pin 优先）。"""
    st = _settings()
    st.raw.setdefault("poll", {})
    st.raw["poll"]["universe_refresh_seconds"] = 0      # 每轮都"过期"

    eng, src, _ = _make_engine(watchlist=["600000"], settings=st)
    calls = _boom_refresh_recorder(eng, monkeypatch)
    monkeypatch.setattr(engine_mod, "time", _FakeTime())

    eng.run_forever(max_rounds=2, watch_only=True)

    assert calls == []
    assert eng._errors == 0
    assert eng._codes == ["600000"]


def test_watch_only_survives_poll_exception(monkeypatch):
    """单轮异常不能让循环崩掉，也不能把已 pin 的股票池弄丢。"""
    eng, src, _ = _make_engine(watchlist=["600000"])
    calls = _boom_refresh_recorder(eng, monkeypatch)
    monkeypatch.setattr(eng, "poll_once",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("轮询炸了")))
    monkeypatch.setattr(engine_mod, "time", _FakeTime())

    rounds: list[int] = []
    eng.run_forever(max_rounds=2, watch_only=True,
                    on_round=lambda n, a: rounds.append(n))

    assert rounds == [1, 2], "异常轮仍然要计数并继续"
    assert eng._errors == 2
    assert calls == []
    assert eng._codes == ["600000"]
    assert eng._codes_pinned is True


# ==========================================================================
# 3. 轮询间隔取 poll.watchlist_seconds
# ==========================================================================
def _interval_settings(watchlist_seconds, universe_seconds):
    st = _settings()
    st.raw.setdefault("poll", {})
    st.raw["poll"]["watchlist_seconds"] = watchlist_seconds
    st.raw["poll"]["universe_seconds"] = universe_seconds
    return st


def test_watch_only_uses_watchlist_interval(monkeypatch):
    """自选模式用 ``poll.watchlist_seconds``，而不是 ``universe_seconds``。"""
    st = _interval_settings(watchlist_seconds=2, universe_seconds=6)
    ft = _FakeTime()
    monkeypatch.setattr(engine_mod, "time", ft)

    eng, src, _ = _make_engine(watchlist=["600000"], settings=st)
    eng.run_forever(max_rounds=2, watch_only=True)

    assert sum(ft.slept) == pytest.approx(2.0, abs=0.05), (
        "watch_only 应用 self.settings['poll.watchlist_seconds']；实际睡了 %.3fs"
        % sum(ft.slept))


def test_normal_mode_uses_universe_interval(monkeypatch):
    """对照组：默认模式仍是 ``poll.universe_seconds``。"""
    st = _interval_settings(watchlist_seconds=2, universe_seconds=6)
    ft = _FakeTime()
    monkeypatch.setattr(engine_mod, "time", ft)

    eng, src, _ = _make_engine(watchlist=["600000"], settings=st)
    eng._codes = ["600000"]                           # 已 pin，避免联网刷新
    monkeypatch.setattr(eng, "refresh_universe", lambda: 0)
    eng.run_forever(max_rounds=2)

    assert sum(ft.slept) == pytest.approx(6.0, abs=0.05), (
        "默认模式应用 poll.universe_seconds；实际睡了 %.3fs" % sum(ft.slept))


def test_watch_only_interval_falls_back_when_zero(monkeypatch):
    """``watchlist_seconds: 0`` 视为未配置 -> 退回 3 秒（不能变成忙等）。"""
    st = _interval_settings(watchlist_seconds=0, universe_seconds=6)
    ft = _FakeTime()
    monkeypatch.setattr(engine_mod, "time", ft)

    eng, src, _ = _make_engine(watchlist=["600000"], settings=st)
    eng.run_forever(max_rounds=2, watch_only=True)

    assert sum(ft.slept) == pytest.approx(3.0, abs=0.05)
    assert min(ft.slept) > 0, "间隔为 0 会变成忙等，必须兜底"


def test_watch_only_single_round_does_not_sleep(monkeypatch):
    """``max_rounds=1`` 时循环在 sleep 之前 break（顺带证明测试不会真的等）。"""
    ft = _FakeTime()
    monkeypatch.setattr(engine_mod, "time", ft)
    eng, src, _ = _make_engine(watchlist=["600000"])
    eng.run_forever(max_rounds=1, watch_only=True)
    assert ft.slept == []


# ==========================================================================
# 4. 空自选：立即返回、不空转、不抛异常
# ==========================================================================
def test_watch_only_empty_watchlist_returns_immediately(monkeypatch, caplog):
    ft = _FakeTime()
    monkeypatch.setattr(engine_mod, "time", ft)
    eng, src, _ = _make_engine(watchlist=[])
    calls = _boom_refresh_recorder(eng, monkeypatch)

    rounds: list[int] = []
    with caplog.at_level(logging.ERROR, logger="arad.engine"):
        eng.run_forever(max_rounds=5, watch_only=True,     # 不抛异常
                        on_round=lambda n, a: rounds.append(n))

    assert rounds == [], "空自选必须立即返回，一轮都不能跑"
    assert ft.slept == [], "更不能进入退避睡眠（否则就是空转）"
    assert eng.running is False
    assert calls == []
    assert eng._errors == 0
    assert eng._codes == [], "提前 return，不能顺手写入半个股票池"
    assert eng._codes_pinned is False


def test_watch_only_empty_watchlist_logs_actionable_error(monkeypatch, caplog):
    monkeypatch.setattr(engine_mod, "time", _FakeTime())
    eng, src, _ = _make_engine(watchlist=[])
    with caplog.at_level(logging.ERROR, logger="arad.engine"):
        eng.run_forever(max_rounds=1, watch_only=True)
    assert "--watch-only" in caplog.text
    assert "watchlist" in caplog.text.lower()


def test_watch_only_empty_watchlist_does_not_touch_source(monkeypatch):
    """空自选时不抓任何行情（省钱且不触发限流）。"""
    monkeypatch.setattr(engine_mod, "time", _FakeTime())
    src = _UniverseSpySource([make_quote(code="600000")])
    eng, _, _ = _make_engine(watchlist=[], source=src)
    eng.run_forever(max_rounds=5, watch_only=True)
    assert src.calls == []
    assert src.universe_calls == 0


# ==========================================================================
# 5. 默认模式（watch_only=False）行为不变
# ==========================================================================
def test_default_watch_only_is_false(monkeypatch):
    """不传 watch_only 时不得改变既有语义（仍然走全市场）。"""
    eng, src, _ = _make_engine(watchlist=["600000"])
    forced: list[bool] = []
    monkeypatch.setattr(eng, "_maybe_refresh_universe",
                        lambda *, force=False: forced.append(force))
    monkeypatch.setattr(engine_mod, "time", _FakeTime())
    eng.run_forever(max_rounds=1)
    assert forced.count(True) == 1


# ==========================================================================
# 6. "自选股降级模式" 警告：默认模式有、watch_only 没有
# ==========================================================================
def test_degradation_warning_fires_in_default_mode(monkeypatch, caplog):
    eng, src, _ = _make_engine(watchlist=["600000"])
    eng._codes = ["600000"]                    # len(_codes) <= len(watchlist)
    monkeypatch.setattr(eng, "_maybe_refresh_universe", lambda *, force=False: None)
    monkeypatch.setattr(engine_mod, "time", _FakeTime())

    rounds: list[int] = []
    with caplog.at_level(logging.WARNING, logger="arad.engine"):
        eng.run_forever(max_rounds=1, on_round=lambda n, a: rounds.append(n))

    assert rounds == [1], "必须真的跑了一轮（否则警告可能来自别处）"
    assert DEGRADE_WARNING in caplog.text


def test_no_degradation_warning_in_watch_only_mode(monkeypatch, caplog):
    """同样的代码规模，自选模式是**有意为之**，不该报警告。"""
    eng, src, _ = _make_engine(watchlist=["600000"])
    monkeypatch.setattr(eng, "_maybe_refresh_universe", lambda *, force=False: None)
    monkeypatch.setattr(engine_mod, "time", _FakeTime())

    rounds: list[int] = []
    with caplog.at_level(logging.WARNING, logger="arad.engine"):
        eng.run_forever(max_rounds=1, watch_only=True,
                        on_round=lambda n, a: rounds.append(n))

    assert rounds == [1], "必须真的跑了一轮，否则 '没有警告' 毫无说服力"
    assert len(eng._codes) <= len(eng.watchlist)
    assert DEGRADE_WARNING not in caplog.text


# ==========================================================================
# 7. CLI：serve --watch-only
# ==========================================================================
def test_cli_serve_parser_accepts_watch_only():
    args = cli.build_parser().parse_args(["serve", "--watch-only"])
    assert args.watch_only is True
    assert args.cmd == "serve"
    assert args.func is cli.cmd_serve


def test_cli_watch_only_defaults_to_false():
    args = cli.build_parser().parse_args(["serve"])
    assert args.watch_only is False


def test_cli_watch_only_not_accepted_by_other_subcommands(capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main(["check", "--watch-only"])
    assert ei.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_cli_serve_help_mentions_watch_only(capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main(["serve", "--help"])
    assert ei.value.code == 0
    assert "--watch-only" in capsys.readouterr().out


# --- cmd_serve 透传（全程不建服务器、不绑端口） ---------------------------
class _FakeCal:
    def describe(self, now=None):
        return "测试时段"

    def is_open(self, now=None):
        return True


class _FakeServer:
    def __init__(self):
        self.server_address = ("127.0.0.1", 0)
        self.serve_forever_calls = 0
        self.shutdown_calls = 0
        self.closed = False

    def serve_forever(self):
        self.serve_forever_calls += 1

    def shutdown(self):
        self.shutdown_calls += 1

    def server_close(self):
        self.closed = True


class _FakeEngine:
    """只记录 ``run_forever`` 收到了什么，绝不联网。"""

    instances: list["_FakeEngine"] = []

    def __init__(self, *a, **kw):
        self.init_args = (a, kw)
        self.store = object()
        self.calendar = _FakeCal()
        self.run_calls: list[dict] = []
        self.stop_calls = 0
        _FakeEngine.instances.append(self)

    def run_forever(self, **kw):
        self.run_calls.append(kw)

    def stop(self):
        self.stop_calls += 1


@pytest.fixture
def serve_harness(monkeypatch):
    """把 cmd_serve 的外部依赖全部换掉；返回 (args, server)。"""
    from arad import session as session_mod
    from arad.server import web as web_mod

    _FakeEngine.instances = []
    srv = _FakeServer()

    monkeypatch.setattr(engine_mod, "Engine", _FakeEngine)
    monkeypatch.setattr(engine_mod, "build_rules", lambda st: [])
    monkeypatch.setattr(engine_mod, "build_notifiers", lambda st: [])
    monkeypatch.setattr(session_mod.TradingCalendar, "load",
                        staticmethod(lambda: _FakeCal()))
    monkeypatch.setattr(web_mod, "create_server",
                        lambda store, cfg, host=None, port=None: srv)
    return srv


def _serve_args(*, watch_only=None):
    """构造 cmd_serve 的 Namespace；``watch_only=None`` 表示**不带该属性**。"""
    base = {"host": None, "port": None, "config": None, "func": cli.cmd_serve}
    if watch_only is not None:
        base["watch_only"] = watch_only
    return argparse.Namespace(**base)


def test_cmd_serve_forwards_watch_only_true(serve_harness):
    rc = cli.cmd_serve(_serve_args(watch_only=True))
    assert rc == 0
    eng = _FakeEngine.instances[-1]
    assert eng.run_calls == [{"watch_only": True}]
    assert eng.stop_calls == 1
    assert serve_harness.closed is True


def test_cmd_serve_forwards_watch_only_false_by_default(serve_harness):
    rc = cli.cmd_serve(_serve_args(watch_only=False))
    assert rc == 0
    eng = _FakeEngine.instances[-1]
    assert eng.run_calls == [{"watch_only": False}]


def test_cmd_serve_tolerates_missing_watch_only_attribute(serve_harness):
    """老调用方传进来的 Namespace 可能没有该字段 -> 走 getattr 兜底为 False。"""
    rc = cli.cmd_serve(_serve_args())          # 默认就不带 watch_only 属性
    assert rc == 0
    assert _FakeEngine.instances[-1].run_calls == [{"watch_only": False}]


def test_cli_main_serve_watch_only_reaches_engine(serve_harness):
    """端到端（除真服务器外全真）：argv -> parser -> cmd_serve -> run_forever。"""
    rc = cli.main(["serve", "--watch-only"])
    assert rc == 0
    assert _FakeEngine.instances[-1].run_calls == [{"watch_only": True}]


def test_cli_main_serve_without_flag_reaches_engine(serve_harness):
    rc = cli.main(["serve"])
    assert rc == 0
    assert _FakeEngine.instances[-1].run_calls == [{"watch_only": False}]


# ==========================================================================
# 附加：模块级便捷入口 run_forever(..., watch_only=)
# ==========================================================================
def _spy_module_run_forever(monkeypatch):
    captured: dict = {}

    class _SpyEngine:
        def __init__(self, source=None, **kw):
            captured["engine_kw"] = kw
            self.store = object()

        def run_forever(self, **kw):
            captured["run_kw"] = kw

    monkeypatch.setattr(engine_mod, "Engine", _SpyEngine)
    return captured


def test_module_run_forever_forwards_watch_only_true(monkeypatch):
    captured = _spy_module_run_forever(monkeypatch)
    engine_mod.run_forever(settings=_settings(), source=FakeSource([]),
                           use_web=False, watch_only=True)
    assert captured["run_kw"] == {"watch_only": True}


def test_module_run_forever_forwards_watch_only_false(monkeypatch):
    captured = _spy_module_run_forever(monkeypatch)
    engine_mod.run_forever(settings=_settings(), source=FakeSource([]),
                           use_web=False)
    assert captured["run_kw"] == {"watch_only": False}


# ==========================================================================
# 附加：watch_only 不破坏既有离线约束
# ==========================================================================
def test_watch_only_does_not_break_injected_pin_contract(monkeypatch):
    """回归护栏：watch_only 之后 ``_maybe_refresh_universe`` 仍被 pin 短路。

    这是 ``test_engine.py::test_injected_codes_are_pinned_and_not_refreshed``
    的 watch_only 版本 —— 自选模式新增了一条写 ``_codes`` 的路径，
    必须同样满足"赋值即 pin"的契约。
    """
    eng, src, _ = _make_engine(watchlist=["600000"])
    monkeypatch.setattr(engine_mod, "time", _FakeTime())
    eng.run_forever(max_rounds=1, watch_only=True)

    calls: list[int] = []
    monkeypatch.setattr(eng, "refresh_universe", lambda: calls.append(1) or 0)
    eng._maybe_refresh_universe()               # 非 force
    eng._maybe_refresh_universe(force=False)
    assert calls == [], "watch_only 写入的股票池同样不能被自动刷新覆盖"
    assert eng._codes == ["600000"]


def test_watch_only_empty_watchlist_unpinned_state_is_consistent(monkeypatch):
    """空自选提前 return 后，``_codes`` 与 pin 标志必须自洽（都是"空"）。"""
    monkeypatch.setattr(engine_mod, "time", _FakeTime())
    eng, src, _ = _make_engine(watchlist=[])
    eng.run_forever(max_rounds=1, watch_only=True)
    assert eng._codes == []
    assert eng._codes_pinned is False, "空列表不算 pin（与 _codes setter 语义一致）"


def test_watch_only_uses_injected_store_and_notifiers(monkeypatch):
    """自选模式不得绕过既有的 store / notifier 管道。"""
    rec = RecordingNotifier()
    st = _settings()
    cal = _morning_cal()
    store = AlertStore(st, calendar=cal)
    rule = _StubRule(code="600000")
    src = FakeSource([make_quote(code="600000", price=11.0, volume_lots=100000)])
    eng = Engine(src, settings=st, rules=[rule], notifiers=[rec], store=store,
                 watchlist=["600000"], calendar=cal)

    monkeypatch.setattr(engine_mod, "time", _FakeTime())
    eng.run_forever(max_rounds=1, watch_only=True)

    assert store.alerts_total() == 1
    assert rec.codes == ["600000"]
