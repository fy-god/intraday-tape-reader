"""IT-P0-002-R1 回归：时间准入必须闭合到规则 Snapshot。

缺陷（上游审计 2026-09-20 04:05 JST 提出）
----------------------------------------
`a3cfd56` 已修 `EngineState.window()` 的未来点上界，并在 `update()` 里拒绝
远未来 provider 时间。**但准入结果没有成为 `poll_once()` 的单一事实来源**：

1. `poll_once()` 随后从**原始 `quotes`** 重建 `returned/eligible/Snapshot`，
   于是已被 State 拒绝的 far-future quote 照样进规则；
2. `update()` 的 out-of-order 分支虽不追加 `history`，但在判断**之前**已写
   `state.quotes[q.code] = q`，最后仍无条件写 `last_price` —— 迟到的旧价会
   把 latest cache 与 `last_price` 倒退。

本文件与前一份 `test_engine_time_admission.py` 的区别：那一份测 **State 内部**
语义，这一份测**端到端**——规则到底看见了什么。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from fakes import make_quote

from arad.engine import (
    FUTURE_TOLERANCE_SECONDS,
    Engine,
    EngineState,
)
from arad.models import Snapshot
from arad.rules.base import RuleContext
from arad.session import SessionPhase, TradingCalendar
from arad.store import AlertStore

NOW = datetime(2026, 9, 15, 10, 30, 0)   # 周一上午盘中
EP = NOW.timestamp()


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------
class _ScriptedSource:
    """按脚本逐轮返回快照；每轮可以给不同的 ts（模拟 provider 时间异常）。"""

    name = "scripted"

    def __init__(self, rounds: list[list]):
        self.rounds = rounds
        self.round = 0
        self.calls = 0

    def universe(self):
        return []

    def snapshots(self, codes):
        self.calls += 1
        idx = min(self.round, len(self.rounds) - 1)
        self.round += 1
        return list(self.rounds[idx])

    def health(self):
        return {"name": self.name, "ok": True, "latency_ms": 0, "err": ""}


class _CapturingRule:
    """记录每轮 Snapshot 里到底有哪些代码（以及它们的价格）。"""

    name = "capture"

    def __init__(self):
        self.seen: list[list[str]] = []
        self.prices: list[dict] = []
        self.snaps: list[Snapshot] = []
        self.observations: list = []

    def evaluate(self, snap, ctx):
        self.seen.append(sorted(snap.quotes))
        self.prices.append({c: q.price for c, q in snap.quotes.items()})
        self.snaps.append(snap)
        self.observations.append(getattr(ctx, "observation", None))
        return []


def _morning_cal() -> TradingCalendar:
    """永远处于早盘时段的日历，绕过真实时钟/节假日。"""
    cal = TradingCalendar(holidays=set())
    cal.phase = lambda now=None: SessionPhase.MORNING      # type: ignore[method-assign]
    return cal


def _settings():
    """每个测试拿到**独立**的 Settings 副本（load_settings 带进程内缓存）。"""
    from arad.config import load_settings
    return load_settings(use_cache=False)


class _Clock:
    """可手动推进的时钟 —— 乱序/正常推进的测试需要轮次之间有真实时间差。"""

    def __init__(self, start: datetime = NOW):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now = self.now + timedelta(seconds=seconds)
        return self.now


def _engine(rounds, codes, *, clock: "_Clock | None" = None):
    rule = _CapturingRule()
    src = _ScriptedSource(rounds)
    clk = clock or _Clock()
    eng = Engine(source=src, settings=_settings(), rules=[rule], notifiers=[],
                 calendar=_morning_cal(), now_fn=clk)
    eng._codes = list(codes)
    eng._codes_pinned = True          # 固化股票池，别被 universe 刷新清空
    eng.watchlist = []
    eng.refresh_universe = lambda *a, **k: 0
    return eng, rule, src, clk


def _q(code, price=10.0, *, vol=200_000.0, ts=None):
    return make_quote(code=code, price=price, volume_lots=vol, ts=ts)


# ===========================================================================
# 1. far-future 不得进入规则 Snapshot
# ===========================================================================
def test_far_future_quote_never_reaches_rules():
    """被 update() 拒绝的远未来行情，绝不能出现在规则 Snapshot 里。

    这是 R1 的核心断言：准入必须保护规则，而不只是保护 State。
    """
    far = _q("600000", 99.0, ts=NOW + timedelta(seconds=FUTURE_TOLERANCE_SECONDS + 300))
    good = _q("600001", 10.0)
    eng, rule, _, clk = _engine([[far, good]], ["600000", "600001"])

    eng.poll_once(force=True)

    assert rule.seen[-1] == ["600001"], (
        f"far-future 绕过准入进了规则：{rule.seen[-1]}")
    assert rule.prices[-1]["600001"] == pytest.approx(10.0)
    # State 侧同样不该有
    assert "600000" not in eng.state.quotes


def test_far_future_not_counted_as_admitted():
    """far-future 不得计入 RoundObservationSet.admitted。"""
    far = _q("600000", 99.0, ts=NOW + timedelta(seconds=FUTURE_TOLERANCE_SECONDS + 300))
    good = _q("600001", 10.0)
    eng, rule, _, clk = _engine([[far, good]], ["600000", "600001"])

    eng.poll_once(force=True)

    obs = rule.observations[-1]
    assert obs is not None, "observation 必须挂到 RuleContext 上"
    assert obs.admitted == 1, f"admitted 应为 1（只有 600001），实际 {obs.admitted}"
    # provider 原始返回了 2 只 —— returned 记录原始量，admitted 记录准入量
    assert obs.returned == 2
    # requested = 个股 2 + 配置里的指数（settings.yaml poll.index_codes 有 5 个）
    assert obs.requested == 2 + len(eng.index_codes), (
        f"requested 应为 个股+指数 = {2 + len(eng.index_codes)}，实际 {obs.requested}")
    # far-future 必须被记为 stale_rejected（而不是静默丢掉）
    assert obs.stale_rejected == 1


def test_all_far_future_yields_empty_snapshot():
    """整轮全是远未来数据时，规则应看到空快照（而不是"全是未来价"）。"""
    eng, rule, _, clk = _engine([[
        _q("600000", 99.0, ts=NOW + timedelta(seconds=9999)),
        _q("600001", 88.0, ts=NOW + timedelta(seconds=9999)),
    ]], ["600000", "600001"])

    eng.poll_once(force=True)

    assert rule.seen[-1] == []
    assert rule.observations[-1].admitted == 0


# ===========================================================================
# 2. out-of-order 不得覆盖 cache / last_price / Snapshot
# ===========================================================================
def test_out_of_order_does_not_reach_rules():
    """迟到旧价不得进入规则 Snapshot。"""
    eng, rule, _, clk = _engine([
        [_q("600000", 10.0)],                                  # 第 1 轮
        [_q("600000", 10.5)],                                  # 第 2 轮
    ], ["600000"])
    eng.poll_once(force=True)
    clk.advance(60)
    eng.poll_once(force=True)
    assert rule.prices[-1]["600000"] == pytest.approx(10.5)

    # 第 3 轮：provider 给一个"迟到"的旧价（事件时间早于已记录的最后一点）
    late = _q("600000", 8.0, ts=NOW)
    eng.source.rounds.append([late])
    eng.poll_once(force=True)

    # 规则侧：迟到的 8.0 不得出现；该股本轮无有效观测 => 空快照
    assert rule.seen[-1] == [], (
        f"迟到旧价进了规则 Snapshot：{rule.prices[-1]}")
    # State 侧：缓存与 last_price 都不能被倒退
    assert eng.state.quotes["600000"].price == pytest.approx(10.5)
    assert eng.state.last_price["600000"] == pytest.approx(10.5)


def test_out_of_order_does_not_regress_latest_cache():
    """直接对 State 断言：迟到旧价不覆盖 quotes / last_price。"""
    st = EngineState()
    st.update([_q("600000", 10.0, ts=NOW)], NOW)
    later = NOW + timedelta(seconds=60)
    st.update([_q("600000", 10.5, ts=later)], later)

    # 迟到 10 秒的旧价
    out = st.update([_q("600000", 8.0, ts=NOW + timedelta(seconds=10))], later)

    assert st.quotes["600000"].price == pytest.approx(10.5)
    assert st.last_price["600000"] == pytest.approx(10.5)
    assert [p[1] for p in st.history["600000"]] == [10.0, 10.5]
    assert out == {}, "乱序点不应出现在 admitted 里"
    assert st.stats.get("t_reject:out_of_order") == 1


def test_out_of_order_not_counted_as_admitted():
    """乱序点不计入 admitted，且计入 out_of_order_rejected。"""
    eng, rule, _, clk = _engine([
        [_q("600000", 10.0)],
        [_q("600000", 10.5)],
    ], ["600000"])
    eng.poll_once(force=True)
    clk.advance(60)
    eng.poll_once(force=True)

    clk.advance(60)
    eng.source.rounds.append([_q("600000", 8.0, ts=NOW)])
    eng.poll_once(force=True)

    obs = rule.observations[-1]
    assert obs.admitted == 0
    assert obs.out_of_order_rejected == 1


# ===========================================================================
# 3. 正常路径不得退化
# ===========================================================================
def test_normal_progression_still_admitted():
    """正常时间推进必须照常进规则（准入不能误杀）。"""
    eng, rule, _, clk = _engine([
        [_q("600000", 10.0)],
        [_q("600000", 10.5)],
        [_q("600000", 11.0)],
    ], ["600000"])

    for _ in range(3):
        eng.poll_once(force=True)
        clk.advance(60)

    assert rule.prices[-1]["600000"] == pytest.approx(11.0)
    assert rule.observations[-1].admitted == 1
    assert rule.observations[-1].out_of_order_rejected == 0
    assert rule.observations[-1].stale_rejected == 0


def test_slightly_future_is_clamped_and_still_admitted():
    """轻微超前（时钟抖动内）应保留并进规则，不算拒绝。"""
    eng, rule, _, clk = _engine([
        [_q("600000", 10.0, ts=NOW + timedelta(seconds=5))],
    ], ["600000"])

    eng.poll_once(force=True)

    assert rule.seen[-1] == ["600000"]
    assert rule.observations[-1].admitted == 1
    assert rule.observations[-1].stale_rejected == 0


def test_quotes_without_ts_pass_through():
    """多数 source 不填 ts —— 必须照常进规则（既有行为不变）。"""
    eng, rule, _, clk = _engine([[_q("600000", 10.0, ts=None)]], ["600000"])

    eng.poll_once(force=True)

    assert rule.seen[-1] == ["600000"]
    assert rule.observations[-1].admitted == 1


def test_one_bad_quote_does_not_block_others():
    """单只出问题不能连累同轮其它标的。"""
    eng, rule, _, clk = _engine([[
        _q("600000", 99.0, ts=NOW + timedelta(seconds=9999)),
        _q("600001", 10.0),
        _q("600002", 11.0),
    ]], ["600000", "600001", "600002"])

    eng.poll_once(force=True)

    assert rule.seen[-1] == ["600001", "600002"]
    assert rule.observations[-1].admitted == 2


# ===========================================================================
# 4. update() 的返回值就是准入合同
# ===========================================================================
def test_update_returns_admitted_set():
    """update() 必须返回 admitted 集合 —— 这是 R1 的根因修复。"""
    st = EngineState()
    good = _q("600000", 10.0)
    far = _q("600001", 99.0, ts=NOW + timedelta(seconds=9999))
    out = st.update([good, far], NOW)

    assert set(out) == {"600000"}
    assert isinstance(out, dict)
    assert out["600000"] is good


def test_update_return_is_single_source_of_truth():
    """admitted 的键必须与 state.quotes 本轮新增的部分一致。"""
    st = EngineState()
    out = st.update([_q("600000", 10.0), _q("600001", 11.0)], NOW)
    assert set(out) == {"600000", "600001"}
    for c, q in out.items():
        assert st.quotes[c] is q
        assert st.last_price[c] == q.price


def test_zero_price_still_excluded_from_admitted():
    """零价（停牌/坏行）不进 admitted —— 既有 IT-P0-003 语义保持。"""
    st = EngineState()
    out = st.update([
        _q("600000", 10.0),
        make_quote(code="600001", price=0.0, prev_close=0.0, volume_lots=0.0),
    ], NOW)
    assert set(out) == {"600000"}


# ===========================================================================
# 5. 指数路径共用同一准入合同（WP01 第 5 条）
# ===========================================================================
def test_index_path_shares_admission_contract():
    """指数与个股必须走同一准入：超前指数不得进入 state.quotes。"""
    st = EngineState()
    idx_far = make_quote(code="sh000001", price=3000.0,
                         ts=NOW + timedelta(seconds=FUTURE_TOLERANCE_SECONDS + 300))
    admitted = st.update([idx_far], NOW)
    assert admitted == {}
    assert "sh000001" not in st.quotes
    assert st.stats.get("t_reject:future") == 1


def test_index_and_stock_use_same_rule():
    """同一批里个股与指数受同一规则约束（不因 board 不同而放宽）。"""
    st = EngineState()
    out = st.update([
        _q("600000", 10.0),
        make_quote(code="sh000001", price=3000.0,
                   ts=NOW + timedelta(seconds=9999)),
        make_quote(code="sz399001", price=9000.0, ts=NOW),
    ], NOW)
    assert set(out) == {"600000", "sz399001"}
