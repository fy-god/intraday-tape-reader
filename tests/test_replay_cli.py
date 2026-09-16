"""回放与 CLI 的离线测试。

全部离线、确定性：不碰网络、不依赖真实墙钟。
"""
from __future__ import annotations

import json
from datetime import datetime, time as dtime, timedelta

import pytest

from arad import cli, replay
from arad.replay import (MORNING_CLOSE, MORNING_OPEN, Replay, ReplayQuoteSource,
                         SimClock, ScriptedStock, build_script, default_universe,
                         generate_script, trading_timeline)
from arad.models import Board, Quote


# ==========================================================================
# 模拟时钟
# ==========================================================================
def test_sim_clock_is_callable_and_advances():
    c = SimClock(datetime(2026, 9, 16, 9, 30))
    assert c() == datetime(2026, 9, 16, 9, 30)      # 可直接当 now_fn
    c.advance(90)
    assert c.now == datetime(2026, 9, 16, 9, 31, 30)
    c.set(datetime(2026, 9, 16, 13, 0))
    assert c() == datetime(2026, 9, 16, 13, 0)


def test_sim_clock_ignores_real_wall_clock():
    """假时钟必须与真实时间无关（休市日也要能跑）。"""
    c = SimClock(datetime(2030, 1, 1, 10, 0))
    assert c().year == 2030


# ==========================================================================
# 时间轴
# ==========================================================================
def test_trading_timeline_covers_both_sessions_no_lunch():
    day = datetime(2026, 9, 16)
    tl = trading_timeline(day, tick_seconds=60)
    assert tl[0].time() == MORNING_OPEN
    times = [t.time() for t in tl]
    assert MORNING_CLOSE in times
    assert dtime(13, 0) in times
    # 午休时段不能出现在时间轴里
    assert not [t for t in times if dtime(11, 31) <= t <= dtime(12, 59)]
    assert tl == sorted(tl)                          # 单调递增
    assert len(tl) == len(set(tl))                   # 无重复点


def test_trading_timeline_minutes_limits_to_morning():
    day = datetime(2026, 9, 16)
    tl = trading_timeline(day, tick_seconds=60, minutes=30)
    assert tl[-1] <= datetime(2026, 9, 16, 10, 0)
    assert len(tl) == 31                             # 含端点
    assert all(t.time() < dtime(12, 0) for t in tl)


def test_trading_timeline_timestamps_strictly_increasing():
    """时间戳必须严格递增且无重复点（重复会破坏窗口中点计算）。

    注意 11:30 -> 13:00 是午休跳空（5400 秒），这是正常的、唯一的例外。
    """
    tl = trading_timeline(datetime(2026, 9, 16), tick_seconds=15)
    for a, b in zip(tl, tl[1:]):
        gap = (b - a).total_seconds()
        assert gap > 0, "时间戳必须严格递增"
        assert gap in (15.0, 5400.0), f"非预期间隔 {gap}s"
    assert len(tl) == len(set(tl))                   # 无重复
    # 午休跳空只应出现一次
    gaps = [(b - a).total_seconds() for a, b in zip(tl, tl[1:])]
    assert gaps.count(5400.0) == 1


# ==========================================================================
# 剧本生成
# ==========================================================================
def test_default_universe_covers_all_script_kinds():
    stocks = default_universe(20, seed=1)
    kinds = {s.kind for s in stocks}
    for need in (replay.KIND_SURGE, replay.KIND_PLUNGE, replay.KIND_LIMIT_SEAL,
                 replay.KIND_LIMIT_BREAK, replay.KIND_LIMIT_DOWN,
                 replay.KIND_VOLUME_BURST, replay.KIND_HIGH_OPEN_FADE,
                 replay.KIND_LATE_SURGE):
        assert need in kinds, f"缺少剧本 {need}"


def test_default_universe_has_unique_codes():
    codes = [s.code for s in default_universe(40, seed=3)]
    assert len(codes) == len(set(codes)) == 40


def test_default_universe_respects_count():
    assert len(default_universe(5, seed=1)) == 5
    assert len(default_universe(1, seed=1)) == 1


def test_default_universe_covers_multiple_boards():
    boards = {s.board for s in default_universe(30, seed=1)}
    assert Board.MAIN in boards
    assert Board.GEM in boards


def test_script_price_path_is_geometric_and_within_limits():
    """价格必须夹在涨跌停之间，且不出现 0/负数。"""
    stock = ScriptedStock(code="600519", kind=replay.KIND_SURGE, start_price=100.0,
                          name="测试急拉")
    tl = trading_timeline(datetime(2026, 9, 16), tick_seconds=15, minutes=60)
    import random
    qs = build_script(stock, tl, random.Random(7))
    assert len(qs) == len(tl)
    lo, hi = 100.0 * 0.9, 100.0 * 1.1
    assert all(lo - 1e-6 <= q.price <= hi + 1e-6 for q in qs)
    assert all(q.price > 0 for q in qs)


def test_script_timestamps_are_datetime_and_monotonic():
    """Quote.ts 必须是 datetime：引擎会拿它和 now 比大小。"""
    stock = ScriptedStock(code="600000", kind=replay.KIND_NORMAL, start_price=10.0)
    tl = trading_timeline(datetime(2026, 9, 16), tick_seconds=15, minutes=10)
    qs = build_script(stock, tl, __import__("random").Random(1))
    assert all(isinstance(q.ts, datetime) for q in qs)
    assert [q.ts for q in qs] == sorted(q.ts for q in qs)
    assert len({q.ts for q in qs}) == len(qs)


def test_script_volume_is_monotonic():
    stock = ScriptedStock(code="600000", kind=replay.KIND_SURGE, start_price=10.0)
    tl = trading_timeline(datetime(2026, 9, 16), tick_seconds=15, minutes=30)
    qs = build_script(stock, tl, __import__("random").Random(2))
    vols = [q.volume_lots for q in qs]
    assert vols == sorted(vols)
    assert vols[-1] > vols[0]


def test_script_has_enough_amount_to_pass_filters():
    """合成行情必须有足够成交额，否则会被 min_amount 粗筛掉，回放会静默无告警。"""
    from arad.config import load_settings
    from arad.filters import Filters
    flt = Filters.from_cfg(load_settings().filters)
    stocks = default_universe(20, seed=42)
    frames = generate_script(stocks, datetime(2026, 9, 16), seed=42, minutes=45)
    for code, qs in frames.items():
        accepted = [q for q in qs if flt.accept(q, datetime(2026, 9, 16).date())]
        assert accepted, f"{code} 全程都不满足粗筛条件"


def test_generate_script_is_deterministic():
    stocks = default_universe(10, seed=5)
    a = generate_script(stocks, datetime(2026, 9, 16), seed=5, minutes=20)
    b = generate_script(stocks, datetime(2026, 9, 16), seed=5, minutes=20)
    for code in a:
        assert [q.price for q in a[code]] == [q.price for q in b[code]]
        assert [q.volume_lots for q in a[code]] == [q.volume_lots for q in b[code]]


def test_generate_script_different_seeds_differ():
    stocks = default_universe(10, seed=5)
    a = generate_script(stocks, datetime(2026, 9, 16), seed=1, minutes=20)
    b = generate_script(stocks, datetime(2026, 9, 16), seed=2, minutes=20)
    assert any([q.price for q in a[c]] != [q.price for q in b[c]] for c in a)


def test_seal_script_actually_reaches_limit_up():
    stock = ScriptedStock(code="601318", kind=replay.KIND_LIMIT_SEAL,
                          start_price=30.0, name="测试封板")
    tl = trading_timeline(datetime(2026, 9, 16), tick_seconds=15, minutes=60)
    qs = build_script(stock, tl, __import__("random").Random(9))
    limit_up = round(30.0 * 1.1, 2)
    assert qs[-1].price == pytest.approx(limit_up, abs=0.01)
    assert qs[-1].bid_vol > 0                        # 封单必须存在


def test_limit_down_script_reaches_limit_down():
    stock = ScriptedStock(code="000858", kind=replay.KIND_LIMIT_DOWN, start_price=25.0)
    tl = trading_timeline(datetime(2026, 9, 16), tick_seconds=15, minutes=60)
    qs = build_script(stock, tl, __import__("random").Random(4))
    assert qs[-1].price == pytest.approx(round(25.0 * 0.9, 2), abs=0.01)
    assert qs[-1].ask_vol > 0


def test_surge_script_does_not_seal():
    """急拉剧本必须"急拉但不封板"，否则与封板剧本无法区分。"""
    stock = ScriptedStock(code="600519", kind=replay.KIND_SURGE, start_price=100.0)
    tl = trading_timeline(datetime(2026, 9, 16), tick_seconds=15, minutes=60)
    qs = build_script(stock, tl, __import__("random").Random(7))
    limit_up = round(100.0 * 1.1, 2)
    assert max(q.price for q in qs) < limit_up - 0.01


# ==========================================================================
# 回放数据源
# ==========================================================================
def _tiny_frames(n=5):
    tl = trading_timeline(datetime(2026, 9, 16), tick_seconds=15, minutes=5)[:n]
    import random
    s = ScriptedStock(code="600000", kind=replay.KIND_NORMAL, start_price=10.0)
    return {"600000": build_script(s, tl, random.Random(1))}


def test_replay_source_advances_and_exhausts():
    src = ReplayQuoteSource(_tiny_frames(5))
    assert len(src.snapshots(["600000"])) == 1
    assert src.cursor == 1
    for _ in range(4):
        src.snapshots(["600000"])
    assert src.at_end()
    assert src.snapshots(["600000"]) == []
    assert src.exhausted


def test_replay_source_filters_by_requested_codes():
    src = ReplayQuoteSource(_tiny_frames(3))
    assert src.snapshots(["999999"]) == []


def test_replay_source_health_shape():
    h = ReplayQuoteSource(_tiny_frames(3)).health()
    assert set(h) >= {"name", "ok", "latency_ms", "err"}
    assert h["ok"] is True


def test_replay_source_universe_returns_everything():
    src = ReplayQuoteSource(_tiny_frames(3))
    assert len(src.universe()) == 1


def test_replay_source_loop_wraps_around():
    src = ReplayQuoteSource(_tiny_frames(3), loop=True)
    for _ in range(10):
        assert len(src.snapshots(["600000"])) == 1


def test_replay_source_current_time_is_datetime():
    src = ReplayQuoteSource(_tiny_frames(3))
    assert isinstance(src.current_time(), datetime)


# ==========================================================================
# 回放器：端到端
# ==========================================================================
def test_replay_produces_alerts_end_to_end():
    """核心断言：回放必须真的产出告警（否则链路是断的）。"""
    rp = Replay(seed=42, minutes=45)
    res = rp.run()
    assert res.total > 0
    assert res.rounds > 0
    assert res.ticks > 0


def test_replay_hits_every_scripted_kind():
    """每种剧本都必须被对应规则捕获（不是"碰巧有几条告警"）。"""
    rp = Replay(seed=42, minutes=45)
    res = rp.run()
    assert res.missed() == [], f"未命中: {[(s.code, s.kind) for s in res.missed()]}"


def test_replay_covers_key_alert_kinds():
    res = Replay(seed=42, minutes=45).run()
    for need in ("surge", "plunge", "limit_up", "limit_down"):
        assert res.by_kind.get(need, 0) > 0, f"缺少 {need}"


def test_replay_is_deterministic():
    a = Replay(seed=11, minutes=30).run()
    b = Replay(seed=11, minutes=30).run()
    assert [(x.key, x.code, x.ts, x.price) for x in a.alerts] == \
           [(x.key, x.code, x.ts, x.price) for x in b.alerts]


def test_replay_different_seeds_give_different_results():
    a = Replay(seed=1, minutes=30).run()
    b = Replay(seed=2, minutes=30).run()
    assert [x.key for x in a.alerts] != [x.key for x in b.alerts]


def test_replay_uses_simulated_clock_not_wall_clock():
    """引擎看到的"现在"必须落在模拟交易时段内。"""
    rp = Replay(seed=42, minutes=30)
    eng = rp.build_engine()
    src = rp.source
    seen = []
    eng._codes = [s.code for s in rp.stocks]
    eng._universe_refreshed_at = float("inf")
    while not src.at_end():
        t = src.current_time()
        if t:
            rp.clock.set(t)
        seen.append(eng.now())
        eng.poll_once(force=True)
    assert all(dtime(9, 30) <= x.time() <= dtime(11, 30) for x in seen)
    assert rp.clock.now.year == 2026


def test_replay_does_not_mutate_global_random():
    """必须用私有 Random，绝不能污染全局 random 状态。"""
    import random
    random.seed(12345)
    before = random.random()
    random.seed(12345)
    Replay(seed=42, minutes=10).run()
    assert random.random() == before


def test_replay_summary_lines_are_chinese_and_informative():
    res = Replay(seed=42, minutes=30).run()
    text = "\n".join(res.summary_lines())
    assert "回放完成" in text
    assert "剧本命中情况" in text
    assert "✓" in text


def test_replay_notifiers_receive_alerts():
    """回放应能把告警送进通知层。"""
    from fakes import RecordingNotifier
    rec = RecordingNotifier()
    res = Replay(seed=42, minutes=30, notifiers=[rec]).run()
    assert res.total > 0
    assert len(rec.alerts) == res.total


def test_replay_result_missed_is_empty_when_all_hit():
    res = Replay(seed=42, minutes=45).run()
    assert res.missed() == []
    assert res.hit()


def test_replay_handles_many_stocks_quickly():
    """回放必须足够快，才能当演示/自检用。"""
    import time
    t0 = time.perf_counter()
    res = Replay(default_universe(60), seed=42, minutes=60).run()
    el = time.perf_counter() - t0
    assert res.total > 0
    assert el < 20.0, f"回放太慢: {el:.1f}s"


# ==========================================================================
# CLI
# ==========================================================================
def test_cli_parser_has_all_subcommands():
    p = cli.build_parser()
    for name in ("check", "once", "serve", "replay", "selftest"):
        assert name in p.format_help()


def test_cli_no_args_prints_help_and_succeeds(capsys):
    assert cli.main([]) == 0
    assert "usage" in capsys.readouterr().out.lower()


@pytest.mark.parametrize("sub", ["check", "once", "serve", "replay", "selftest"])
def test_cli_subcommand_help_exits_zero(sub, capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main([sub, "--help"])
    assert ei.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_cli_check_reports_config_and_phase(capsys):
    assert cli.main(["check"]) == 0
    out = capsys.readouterr().out
    assert "配置体检" in out
    assert "规则" in out
    assert "时段" in out
    assert "下次开盘" in out


def test_cli_selftest_passes_and_exits_zero(capsys):
    rc = cli.main(["selftest", "--seed", "42", "--stocks", "16", "--minutes", "40"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "全链路正常" in out


def test_cli_selftest_fails_when_no_alerts(monkeypatch, capsys):
    """自检必须在"没告警"时失败 —— 否则它毫无意义。"""
    class FakeResult:
        alerts: list = []
        rounds = 1
        ticks = 1
        by_kind: dict = {}
        by_stock: dict = {}
        stocks: list = []

        @property
        def total(self):
            return 0

        def missed(self):
            return []

        def summary_lines(self):
            return ["fake"]

    class FakeReplay:
        stocks: list = []

        def run(self):
            return FakeResult()

    monkeypatch.setattr(cli, "_do_replay", lambda args: (FakeReplay(), FakeResult()))
    rc = cli.main(["selftest", "--stocks", "5", "--minutes", "5"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "未产出任何告警" in out


def test_cli_replay_writes_jsonl(tmp_path, capsys):
    out_file = tmp_path / "alerts.jsonl"
    rc = cli.main(["replay", "--seed", "42", "--stocks", "16",
                   "--minutes", "30", "--out", str(out_file)])
    assert rc == 0
    assert out_file.exists()
    lines = [json.loads(x) for x in
             out_file.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert lines
    for rec in lines:
        assert {"key", "kind", "code", "ts", "price", "pct", "title"} <= set(rec)
    # 中文必须可读（ensure_ascii=False）
    assert any(any("\u4e00" <= ch <= "\u9fff" for ch in str(r.get("title", "")))
               for r in lines)


def test_cli_replay_prints_summary(capsys):
    rc = cli.main(["replay", "--seed", "42", "--stocks", "16", "--minutes", "30"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "离线回放" in out
    assert "汇总" in out


def test_cli_setup_stdout_is_safe():
    cli._setup_stdout()          # 不抛异常即可


def test_cli_returns_2_on_internal_error(monkeypatch, capsys):
    def boom(args):
        raise RuntimeError("故意炸")

    monkeypatch.setattr(cli, "cmd_check", boom)
    p = cli.build_parser()
    args = p.parse_args(["check"])
    args.func = boom
    monkeypatch.setattr(cli, "build_parser", lambda: _StubParser(args))
    assert cli.main(["check"]) == 2
    assert "故意炸" in capsys.readouterr().out


class _StubParser:
    def __init__(self, args):
        self._args = args

    def parse_args(self, argv=None):
        return self._args

    def print_help(self):
        pass
