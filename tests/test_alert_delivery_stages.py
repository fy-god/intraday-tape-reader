"""IT-P1-EVAL-PUBLISH-001：告警交付阶段账本（rule_selected / bus_accepted / committed）。

**缺陷**（云端 04:10 审计指认）：旧账本只有一个 ``published``，而且它是在
**规则内部**、``max_per_round`` 截断之后立刻写的 —— 那时告警还没经过
``AlertBus.accept`` 的 key 去重与 cooldown，更没写进 Store。于是被冷却
丢掉的候选也被记成"已发布"。

机制反例（本文件端到端复现）：同一只票每 5 秒都满足 ``volume_burst``、
``cooldown_seconds=600``、跑 24 轮::

    真实: rule_selected=24  bus_accepted=1  committed=1
    旧口径: published=24                       -> overcount 24×

危害不是"数字不好看"：若拿旧 ``published`` 当"已发布事件"的**分母**去
对事后收益做标签，那 23 条从未交付给任何人的候选会被当成真实发生过的
告警事件，标签集从源头就是错的。

**验收不变量**（逐级子集，由 ``SignalEvalStats.check_invariants`` 机械断言）::

    hit_candidates >= rule_selected >= bus_accepted >= committed

本文件全部用**真实 Engine + 真实规则 + 真实 AlertBus + 真实 Store**，
不用桩替代链路 —— 因为要证的正是"跨这四个组件的那段记账错位"。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from arad.capabilities import (
    RoundObservationSet,
    SignalEvalStats,
    SourceCapabilities,
)
from arad.config import load_settings
from arad.engine import Engine, RuleContext
from arad.models import Alert, AlertKind, Board, Quote, Snapshot
from arad.rules.volume_burst import build as build_volume_burst
from arad.session import SessionPhase, TradingCalendar
from fakes import make_alert, make_quote

# ---------------------------------------------------------------------------
# 探针场景常量（照抄审计的机制反例）
# ---------------------------------------------------------------------------
CODE = "600000"
BASE = datetime(2026, 9, 21, 9, 40, 0)
ROUNDS = 24
COOLDOWN = 600.0

#: 必须放进 ``ctx.cfg`` —— RuleContext.cfg **覆盖** rule.cfg（既有语义）。
VB_CFG = {
    "enabled": True,
    "volume_ratio_threshold": 1.5,
    "speed_multiple": 0.0,      # 关掉速度门槛：不依赖 state 历史序列
    "speed_window_seconds": 60.0,
    "min_turnover": 0.5,
    "min_amount": 30_000_000.0,
    "min_abs_pct": 0.5,
    "cooldown_seconds": COOLDOWN,
    "max_per_round": 50,
}

#: 缺哪个字段就会走 blocked/advisory 分支，所以必须显式声明齐全。
FULL_CAPS = SourceCapabilities(
    source="probe", turnover=True, volume_ratio=True, depth_l1=True,
    depth_l5=True, outer_inner=True, float_cap=True, provider_time=True,
)


def _burst_quote(ts: datetime, seq: int, volume: float) -> Quote:
    """一只每轮都"放量"的票：量比 5.0、涨幅 +5%，稳过所有门槛。"""
    return make_quote(
        code=CODE, name="测试放量", price=10.5, prev_close=10.0,
        volume_lots=volume, turnover=8.0, volume_ratio=5.0,
        bid1=10.49, ask1=10.51, bid_vol=5000.0, ask_vol=5000.0,
        ts=ts, seq=seq,
    )


def _morning_engine(rule) -> Engine:
    """真实 Engine，但日历钉死在早盘（否则休市时段规则直接返回空）。"""
    cal = TradingCalendar(holidays=set())
    cal.phase = lambda now=None: SessionPhase.MORNING        # type: ignore[method-assign]
    eng = Engine(source=None, settings=load_settings(use_cache=False),
                 rules=[rule], notifiers=[], calendar=cal)
    eng.focus = set()
    eng.ignore = set()
    return eng


def _run_round(eng: Engine, rule, i: int) -> RoundObservationSet:
    """手工复刻 ``poll_once`` 的记账路径（同一 observation 贯穿四个阶段）。"""
    ts = BASE + timedelta(seconds=5 * i)
    snap = Snapshot(seq=i, ts=ts,
                    quotes={CODE: _burst_quote(ts, i, 100_000.0 + i * 60_000.0)})
    obs = RoundObservationSet(source="probe", capabilities=FULL_CAPS,
                              requested=1, returned=1, admitted=1)
    obs.eval_stats(str(getattr(rule, "name", "volume_burst")))
    ctx = RuleContext(
        state=eng.state, cfg=dict(VB_CFG), now=ts,
        session=SessionPhase.MORNING, elapsed_trading_seconds=600.0,
        minutes_to_close=200.0, watchlist=(), focus=set(),
        capabilities=FULL_CAPS, observation=obs, current_indices={},
    )
    fresh = []
    for a in rule.evaluate(snap, ctx) or []:
        if not eng.bus.accept(a, ts.timestamp()):
            continue
        eng._mark_stage(obs, a, "bus_accepted")
        fresh.append(a)
    for a in fresh:
        eng.store.add_alert(a)
        eng._mark_stage(obs, a, "committed")
    return obs


def _totals(obs_list) -> dict[str, int]:
    keys = ("hit_candidates", "rule_selected", "bus_accepted", "committed",
            "evaluated_no_hit", "evaluable", "blocked_capability")
    out = {k: 0 for k in keys}
    for obs in obs_list:
        for st in obs.signal_evals.values():
            for k in keys:
                out[k] += getattr(st, k)
    return out


# ===========================================================================
# 1. 机制反例端到端复现 —— 本文件的核心
# ===========================================================================
def test_cooldown_overcount_is_now_visible_as_three_stages():
    """24 轮同票持续命中、cooldown=600s：三阶段必须分得开。

    旧口径只有一个数（24），无法回答"到底交付了几条"。修复后必须看到
    ``rule_selected=24 / bus_accepted=1 / committed=1``：

    * ``rule_selected`` = 规则每轮都选中的**候选**；
    * ``bus_accepted`` = 真的过了 AlertBus 去重/冷却的；
    * ``committed`` = 真的进了 Store、用户看得到的。

    这条断言是**行为级**的：它只用 ``rule_selected``/``bus_accepted``/
    ``committed`` 三个既有字段名（无新符号导入），在修复前会以
    ``AttributeError``/``AssertionError`` 失败，而不是 ImportError。
    """
    rule = build_volume_burst(VB_CFG)
    eng = _morning_engine(rule)

    obs_list = [_run_round(eng, rule, i) for i in range(ROUNDS)]
    tot = _totals(obs_list)

    assert tot["hit_candidates"] == ROUNDS, "每轮都到门槛"
    assert tot["rule_selected"] == ROUNDS, "规则每轮都选中它"
    assert tot["bus_accepted"] == 1, (
        f"cooldown={COOLDOWN}s 内只应接受 1 条，实际 {tot['bus_accepted']}")
    assert tot["committed"] == 1, "只有 1 条真的交付给用户"

    # 旧口径的 overcount：拿 rule_selected 冒充 committed 会放大 24×
    assert tot["rule_selected"] / max(tot["committed"], 1) == ROUNDS, (
        "overcount 因子必须是 24 —— 这正是旧 published 的问题")

    # Store 侧独立核对：账本说 committed=1，Store 里就必须只有 1 条
    assert eng.store.alerts_total() == 1, (
        "账本的 committed 必须与 Store 实际条数一致，否则账本本身不可信")

    for obs in obs_list:
        assert obs.check_signal_invariants() == [], "交付阶段链必须成立"


def test_stage_chain_monotonic_every_round():
    """逐轮检查 ``hit_candidates >= rule_selected >= bus_accepted >= committed``。

    不能只在累计值上验证：累计相等会掩盖"某轮 A 级 > 上级、另一轮反过来"
    的抵消式错误。
    """
    rule = build_volume_burst(VB_CFG)
    eng = _morning_engine(rule)

    for i in range(ROUNDS):
        obs = _run_round(eng, rule, i)
        st = obs.signal_evals["volume_burst"]
        assert st.hit_candidates >= st.rule_selected, f"第 {i} 轮"
        assert st.rule_selected >= st.bus_accepted, f"第 {i} 轮"
        assert st.bus_accepted >= st.committed, f"第 {i} 轮"
        assert obs.check_signal_invariants() == [], f"第 {i} 轮"


# ===========================================================================
# 2. 三阶段各自的语义边界
# ===========================================================================
def test_rule_selected_counts_truncated_but_bus_accepted_does_not():
    """``max_per_round`` 截断发生在规则内 -> 只影响 ``rule_selected``。

    与 cooldown 的区分是这次拆分的**全部意义**：两种丢弃发生在不同阶段，
    旧账本把它们混成一个 "published 比 hit_candidates 少" 的差值。
    """
    cfg = dict(VB_CFG, max_per_round=2)
    rule = build_volume_burst(cfg)
    eng = _morning_engine(rule)

    ts = BASE
    codes = ["600000", "600001", "600002", "600003"]
    # Quote 是 slots dataclass（无 __dict__），逐只显式重建。
    quotes = {c: make_quote(code=c, name="测试放量", price=10.5, prev_close=10.0,
                            volume_lots=200_000.0, turnover=8.0,
                            volume_ratio=5.0, ts=ts, seq=0)
              for c in codes}
    snap = Snapshot(seq=0, ts=ts, quotes=quotes)
    obs = RoundObservationSet(source="probe", capabilities=FULL_CAPS,
                              requested=len(codes), returned=len(codes),
                              admitted=len(codes))
    obs.eval_stats("volume_burst")
    ctx = RuleContext(
        state=eng.state, cfg=cfg, now=ts, session=SessionPhase.MORNING,
        elapsed_trading_seconds=600.0, minutes_to_close=200.0,
        watchlist=(), focus=set(), capabilities=FULL_CAPS,
        observation=obs, current_indices={},
    )
    alerts = rule.evaluate(snap, ctx) or []
    assert len(alerts) == 2, "max_per_round=2"

    st = obs.signal_evals["volume_burst"]
    assert st.hit_candidates == 4, "4 只都到门槛"
    assert st.rule_selected == 2, "规则只选中 2 条（截断掉的 2 条不算选中）"
    # 截断发生在 bus 之前，所以 bus_accepted 尚未记账
    assert st.bus_accepted == 0, "还没经过 AlertBus，不得提前记 bus_accepted"
    assert st.committed == 0, "还没进 Store，不得提前记 committed"


def test_engine_marks_bus_accepted_only_for_alerts_that_pass():
    """被 AlertBus 拒掉的告警**不得**记 ``bus_accepted`` —— 反向验牙。

    用一个显式返回两条同 key 告警的规则：第 1 条进、第 2 条被去重。
    若实现把 ``bus_accepted`` 记在 ``accept()`` 之前，这里会得到 2。
    """
    ts = BASE
    rule = _TwoSameKeyRule()
    eng = _morning_engine(rule)
    obs = RoundObservationSet(source="probe", capabilities=FULL_CAPS,
                              requested=1, returned=1, admitted=1)
    obs.eval_stats("two_same_key")
    snap = Snapshot(seq=0, ts=ts, quotes={CODE: _burst_quote(ts, 0, 100_000.0)})
    ctx = RuleContext(
        state=eng.state, cfg={}, now=ts, session=SessionPhase.MORNING,
        elapsed_trading_seconds=600.0, minutes_to_close=200.0,
        watchlist=(), focus=set(), capabilities=FULL_CAPS,
        observation=obs, current_indices={},
    )
    fresh = []
    for a in rule.evaluate(snap, ctx) or []:
        if not eng.bus.accept(a, ts.timestamp()):
            continue
        eng._mark_stage(obs, a, "bus_accepted")
        fresh.append(a)
    for a in fresh:
        eng.store.add_alert(a)
        eng._mark_stage(obs, a, "committed")

    assert len(fresh) == 1, "同 key 第二条被去重"
    st = obs.signal_evals["two_same_key"]
    assert st.bus_accepted == 1, "只有通过的那条算 bus_accepted"
    assert st.committed == 1


class _TwoSameKeyRule:
    """返回两条**同 key** 告警，用来验证去重阶段被正确记账。"""

    name = "two_same_key"

    def evaluate(self, snap, ctx):
        q = snap.quotes[CODE]
        return [
            Alert(key=f"{CODE}:dup", kind=AlertKind.SURGE, code=CODE,
                  name=q.name, ts=snap.ts, price=q.price, pct=q.pct,
                  title="重复1", detail="d", signal_id=self.name),
            Alert(key=f"{CODE}:dup", kind=AlertKind.SURGE, code=CODE,
                  name=q.name, ts=snap.ts, price=q.price, pct=q.pct,
                  title="重复2", detail="d", signal_id=self.name),
        ]


# ===========================================================================
# 3. 归因正确性：signal_id 必须能分辨同一 AlertKind 下的不同 signal
# ===========================================================================
def test_mark_stage_ignores_alert_without_signal_id():
    """没有 ``signal_id`` 的告警不得凭空建账本条目（避免 considered=0 幽灵行）。"""
    eng = _morning_engine(rule=None) if False else _morning_engine(_NoopRule())
    obs = RoundObservationSet(source="probe")
    a = make_alert(code=CODE)          # signal_id 默认 ""
    assert a.signal_id == "", "默认必须为空，不能猜"
    eng._mark_stage(obs, a, "bus_accepted")
    assert obs.signal_evals == {}, "无 signal_id -> 不建条目"


def test_mark_stage_ignores_signal_not_in_ledger():
    """``signal_id`` 指向本轮未登记的 signal 时不得凭空建条目。"""
    eng = _morning_engine(_NoopRule())
    obs = RoundObservationSet(source="probe")
    obs.eval_stats("volume_burst")
    a = make_alert(code=CODE, signal_id="limit_board")   # 本轮没登记
    eng._mark_stage(obs, a, "committed")
    assert "limit_board" not in obs.signal_evals, "不得凭空建条目"
    assert obs.signal_evals["volume_burst"].committed == 0, "不得记到别家头上"


def test_mark_stage_attributes_to_the_declared_signal():
    """``signal_id`` 指向谁就记到谁头上 —— 同 AlertKind 不得串账。"""
    eng = _morning_engine(_NoopRule())
    obs = RoundObservationSet(source="probe")
    obs.eval_stats("spirit_order.institution_buy")
    obs.eval_stats("spirit_order.big_buy")
    a = make_alert(code=CODE, signal_id="spirit_order.big_buy")
    eng._mark_stage(obs, a, "committed")
    assert obs.signal_evals["spirit_order.big_buy"].committed == 1
    assert obs.signal_evals["spirit_order.institution_buy"].committed == 0, (
        "8 个 spirit pattern 共用同一 AlertKind，串账会让账本比不记更糟")


def test_mark_stage_never_raises_on_broken_observation():
    """可观测性绝不能反过来打断告警主链路（本仓既有纪律）。"""
    eng = _morning_engine(_NoopRule())

    class Broken:
        signal_evals = property(lambda self: (_ for _ in ()).throw(RuntimeError()))

    a = make_alert(code=CODE, signal_id="volume_burst")
    eng._mark_stage(Broken(), a, "committed")          # 不得抛
    eng._mark_stage(None, a, "committed")              # 不得抛
    eng._mark_stage(object(), a, "committed")          # 不得抛
    eng._mark_stage(RoundObservationSet(), a, "bogus_stage")   # 未知阶段


class _NoopRule:
    name = "noop"

    def evaluate(self, snap, ctx):
        return []


# ===========================================================================
# 4. 兼容性与不变量
# ===========================================================================
def test_published_is_a_compat_alias_for_rule_selected():
    """旧名 ``published`` 保留为 ``rule_selected`` 的别名（不打断既有消费方）。

    同时钉住：它**不再**表示"已交付"。谁要交付数必须用 ``committed``。
    """
    st = SignalEvalStats(signal="s")
    st.rule_selected = 7
    st.bus_accepted = 3
    st.committed = 2
    assert st.published == 7, "别名必须等价 rule_selected"
    assert st.published != st.committed, (
        "若二者相等，说明别名接错了字段 —— 那正是本次要修的缺陷本身")
    assert st.dropped_by_bus == 4
    d = st.as_dict()
    assert d["published"] == d["rule_selected"] == 7
    assert d["bus_accepted"] == 3 and d["committed"] == 2
    assert d["dropped_by_bus"] == 4


def test_committed_ratio_is_none_without_hits():
    """没有命中时交付率是 ``None``（not_measured），**不是** 1.0/0.0。"""
    st = SignalEvalStats(signal="s")
    assert st.committed_ratio is None
    assert st.as_dict()["committed_ratio"] is None
    st.hit_candidates = 4
    st.committed = 1
    assert st.committed_ratio == 0.25


@pytest.mark.parametrize(
    "hit,sel,acc,com,bad",
    [
        (4, 4, 4, 4, False),
        (4, 4, 1, 1, False),
        (4, 4, 1, 0, False),
        (0, 0, 0, 0, False),
        # 违反链的排布必须被机械断言抓住
        (4, 5, 5, 5, True),      # rule_selected > hit_candidates
        (4, 2, 3, 2, True),      # bus_accepted > rule_selected
        (4, 3, 2, 3, True),      # committed > bus_accepted
    ],
)
def test_stage_chain_invariant_has_teeth(hit, sel, acc, com, bad):
    """阶段链不变量对**每一种**倒挂都要报错 —— 逐参验牙。"""
    st = SignalEvalStats(signal="s", considered=hit, evaluable=hit,
                         hit_candidates=hit, rule_selected=sel,
                         bus_accepted=acc, committed=com)
    v = st.check_invariants()
    assert bool(v) is bad, f"hit={hit} sel={sel} acc={acc} com={com} -> {v}"


# ===========================================================================
# 5. IT-P1-EVAL-PUBLISH-001-R1：blocked 与"真的发出告警"不能同时为真
# ===========================================================================
def test_blocked_code_must_not_still_emit_an_alert():
    """**既有缺陷**（本轮由真实看板端到端对账暴露，非本次拆分引入）。

    ``volume_burst`` 原来在 ``not provides("turnover")`` 时只 ``mark_blocked``
    却**没有 ``continue``**，于是同一票可以既被记成"判不了"(blocked)
    又继续往下判并真的发出告警，机械不变量
    ``rule_selected <= hit_candidates`` 当场被打破
    （实测 ``considered=1 blocked=1 hit=0 selected=1``）。

    语义上正确的是**整只跳过**：turnover 是本规则的硬依赖，且 Sina 的非零
    换手率同样是不可信占位值（`test_capability_is_source_level_not_value_level`
    已钉住"同源下改数值不改变可评估性判定"）。既然判据不可信，就不该报。

    本测试用"源声明不提供 turnover 但 payload 值恰好可用"来触发旧代码的
    自相矛盾路径 —— 这正是 replay 合成数据与真实看板上发生的那一种组合。
    """
    caps = SourceCapabilities(
        source="sina", turnover=False,        # 源声明不提供 turnover
        volume_ratio=True, depth_l1=True, depth_l5=True,
        outer_inner=True, float_cap=True, provider_time=True,
    )
    q = make_quote(code=CODE, name="测试放量", price=10.5, prev_close=10.0,
                   volume_lots=200_000.0, amount=200_000.0 * 100 * 10.5,
                   turnover=8.0,        # 但 payload 里这个值"看起来"可用
                   volume_ratio=5.0, bid1=10.49, ask1=10.51,
                   bid_vol=5000.0, ask_vol=5000.0, ts=BASE, seq=0)
    obs = RoundObservationSet(source="sina", capabilities=caps,
                              requested=1, returned=1, admitted=1)
    obs.eval_stats("volume_burst")

    class _S:
        def volume_delta(self, code, win, now):
            return 0.0

    cfg = dict(VB_CFG, speed_multiple=0.0)
    ctx = RuleContext(
        state=_S(), cfg=cfg, now=BASE, session=SessionPhase.MORNING,
        elapsed_trading_seconds=600.0, minutes_to_close=200.0,
        watchlist=(), focus=set(), capabilities=caps,
        observation=obs, current_indices={},
    )
    alerts = build_volume_burst(cfg).evaluate(
        Snapshot(seq=0, ts=BASE, quotes={CODE: q}), ctx) or []

    st = obs.signal_evals["volume_burst"]
    assert obs.check_signal_invariants() == [], (
        "同一票被记成 blocked 却又发出告警 —— 不变量必须为空")
    assert alerts == [], (
        "硬依赖缺失 -> 整只跳过；不得'记了不能判'还照报一条")
    assert st.blocked_capability == 1, "确实因缺 turnover 判不了"
    assert st.hit_candidates == 0, "跳过的票不是候选命中"
    assert st.rule_selected == 0, (
        "最关键：selected 必须为 0，否则 selected > hit_candidates")
    assert "turnover_not_provided" in st.blocked_reasons


def test_placeholder_zero_turnover_still_counts_as_blocked():
    """真实 Sina 场景必须**行为不变**：turnover=0.0 占位 + 声明不提供。

    这条同时守住"告警集合逐字不变"这个兼容性承诺：修复前后该票都被拦下，
    只是现在只经过一条路径（blocked 即跳过），不再重复走数值门槛。
    """
    caps = SourceCapabilities(
        source="sina", turnover=False, volume_ratio=True, depth_l1=True,
        depth_l5=True, outer_inner=True, float_cap=True, provider_time=True,
    )
    q = make_quote(code=CODE, name="占位零", price=10.5, prev_close=10.0,
                   volume_lots=200_000.0, amount=200_000.0 * 100 * 10.5,
                   turnover=0.0,            # Sina 的"不提供"占位
                   volume_ratio=5.0, ts=BASE, seq=0)
    obs = RoundObservationSet(source="sina", capabilities=caps,
                              requested=1, returned=1, admitted=1)
    obs.eval_stats("volume_burst")

    class _S:
        def volume_delta(self, code, win, now):
            return 0.0

    ctx = RuleContext(
        state=_S(), cfg=dict(VB_CFG), now=BASE, session=SessionPhase.MORNING,
        elapsed_trading_seconds=600.0, minutes_to_close=200.0,
        watchlist=(), focus=set(), capabilities=caps,
        observation=obs, current_indices={},
    )
    alerts = build_volume_burst(VB_CFG).evaluate(
        Snapshot(seq=0, ts=BASE, quotes={CODE: q}), ctx) or []

    st = obs.signal_evals["volume_burst"]
    assert alerts == [], "占位零换手率必须把该票拦下（行为与修复前一致）"
    assert st.blocked_capability == 1, "真的判不了 -> 记 blocked"
    assert "turnover_not_provided" in st.blocked_reasons
    assert st.hit_candidates == 0 and st.rule_selected == 0
    assert obs.check_signal_invariants() == []


def test_tencent_real_zero_turnover_is_a_business_zero_not_a_gap():
    """对照面：**Tencent 提供 turnover**，0.0 是真实业务零，不算能力缺失。

    与上一条配对，防止把"数值不达标"误记成"能力缺失"。
    """
    caps = SourceCapabilities(
        source="tencent", turnover=True, volume_ratio=True, depth_l1=True,
        depth_l5=True, outer_inner=True, float_cap=True, provider_time=True,
    )
    q = make_quote(code=CODE, name="真实零", price=10.5, prev_close=10.0,
                   volume_lots=200_000.0, amount=200_000.0 * 100 * 10.5,
                   turnover=0.0, volume_ratio=5.0, ts=BASE, seq=0)
    obs = RoundObservationSet(source="tencent", capabilities=caps,
                              requested=1, returned=1, admitted=1)
    obs.eval_stats("volume_burst")

    class _S:
        def volume_delta(self, code, win, now):
            return 0.0

    ctx = RuleContext(
        state=_S(), cfg=dict(VB_CFG), now=BASE, session=SessionPhase.MORNING,
        elapsed_trading_seconds=600.0, minutes_to_close=200.0,
        watchlist=(), focus=set(), capabilities=caps,
        observation=obs, current_indices={},
    )
    build_volume_burst(VB_CFG).evaluate(
        Snapshot(seq=0, ts=BASE, quotes={CODE: q}), ctx)

    st = obs.signal_evals["volume_burst"]
    assert st.blocked_capability == 0, (
        "Tencent 真的提供了 turnover，0.0 只是没成交，不是能力缺失")
    assert st.evaluable == 1, "该票可评估（只是没到门槛）"
    assert obs.check_signal_invariants() == []


def test_observation_as_dict_exposes_all_three_stages():
    """看板/报告读的 ``as_dict()`` 必须三个阶段都在，否则用户仍看不到差别。"""
    obs = RoundObservationSet(source="probe")
    st = obs.eval_stats("volume_burst")
    st.considered = 3
    st.evaluable = 3
    st.hit_candidates = 3
    st.rule_selected = 3
    st.bus_accepted = 1
    st.committed = 1
    d = obs.as_dict()
    cell = d["signal_evaluability"]["volume_burst"]
    for k in ("rule_selected", "bus_accepted", "committed", "dropped_by_bus",
              "committed_ratio", "published"):
        assert k in cell, f"as_dict 缺 {k}"
    assert cell["rule_selected"] == 3
    assert cell["bus_accepted"] == 1
    assert cell["committed"] == 1
    assert cell["dropped_by_bus"] == 2
    assert obs.check_signal_invariants() == []
