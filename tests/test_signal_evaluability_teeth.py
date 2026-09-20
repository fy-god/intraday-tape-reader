"""WP01 / IT-P1-CAPABILITY-003 —— **真行为级验牙**（只用修复前已存在的 API）。

为什么单独一个文件
------------------
``tests/test_signal_evaluability.py`` 在模块顶层 ``import ADVISORY/BLOCKING/
SignalEvalStats`` —— 这些符号是**本轮新增**的。把整套实现 stash 掉之后，
那个文件会整体 ``ImportError`` 收集失败，红是"符号不存在"（**结构性**），
不是"语义错"（**行为级**）。结构性的红证明不了任何行为问题，
**不能冒充验牙**。

本文件刻意只 import 修复前就存在的符号，于是回退实现后它**能正常收集**，
失败必然发生在 ``assert`` 上 —— 这才是真验牙。

本文件的每一条断言在修复前都会以 ``AssertionError`` 失败，理由写在各自
docstring 里。
"""
from __future__ import annotations

from datetime import datetime

from fakes import make_snapshot

from arad.capabilities import (
    CAPABILITY_TABLE,
    RoundObservationSet,
    SourceCapabilities,
    capabilities_for,
)
from arad.rules.base import RuleContext
from arad.rules.volume_burst import DEFAULTS, VolumeBurstRule, build
from arad.session import SessionPhase

from test_capabilities import FakeState, burst_quote, feed_burst

NOW = datetime(2026, 9, 15, 10, 30, 0)


def _ctx(st: FakeState, caps, obs, *, cfg=None) -> RuleContext:
    return RuleContext(
        state=st, cfg=dict(cfg if cfg is not None else DEFAULTS),
        now=NOW, session=SessionPhase.MORNING,
        elapsed_trading_seconds=1800.0, minutes_to_close=120.0,
        capabilities=caps, observation=obs,
    )


def test_advisory_missing_is_not_counted_as_capability_unavailable():
    """**真行为级验牙**：只缺 ``volume_ratio`` 的票**不得**进能力缺失计数。

    语义依据（修复前就已成立、且代码里写明了）：``vr`` 取占位 0.0 时
    ``vr > 0.0`` 为假 -> 量比门槛被**跳过**，规则靠速度/金额**仍能命中**。
    所以该票是**可评估**的，把它记进"能力缺失"就是把"少判一项"夸大成
    "整类不可评估" —— 这正是 IT-P1-CAPABILITY-003。

    **修复前**：``_mark_unavailable(ctx, code, "volume_ratio")`` 无条件执行，
    于是 ``unavailable_capability == 1`` -> 本断言 ``assert 0 == 1`` **失败**。
    **修复后**：记的是 advisory（不进 ``unavailable_codes``），计数为 0。

    这条只用 ``unavailable_capability``（修复前已存在）判定，故回退实现后
    红在 AssertionError，是真验牙而非缺符号。
    """
    st = FakeState()
    feed_burst(st, "600000")
    # 有 turnover、无 volume_ratio：只有量比这一项缺失
    caps = SourceCapabilities(source="partial", turnover=True,
                              volume_ratio=False, depth_l1=True,
                              depth_l5=False, outer_inner=False,
                              float_cap=True, provider_time=False)
    obs = RoundObservationSet(requested=1)
    ctx = _ctx(st, caps, obs)

    alerts = build({}).evaluate(
        make_snapshot([burst_quote(volume_ratio=0.0)], ts=NOW), ctx)

    assert len(alerts) == 1, (
        "缺 volume_ratio 只是跳过量比门槛，速度/金额条件成立就应命中")
    assert obs.unavailable_capability == 0, (
        "缺 volume_ratio 允许跳过 -> 该票仍可评估，"
        "不得计入能力缺失（否则会把它夸大成整类不可评估）")


def test_turnover_missing_still_counted_unavailable():
    """回归保护：缺 ``turnover`` 且 ``min_turnover>0`` **仍然**要记缺失。

    这条修复前后**都绿** —— 它挡住的是"把 blocking 也一起删掉"这种过度修正：
    缺 turnover 时该票是真的不能判，必须继续可见。
    """
    st = FakeState()
    feed_burst(st, "600000")
    obs = RoundObservationSet(requested=1)
    ctx = _ctx(st, CAPABILITY_TABLE["sina"], obs)

    alerts = build({}).evaluate(
        make_snapshot([burst_quote(turnover=0.0, volume_ratio=0.0)], ts=NOW), ctx)

    assert alerts == [], "阶段一约定：门槛判定不变，仍不产出告警"
    assert obs.unavailable_capability == 1, "缺 turnover 的票必须仍然可见"
    assert "600000" in obs.unavailable_codes


def test_volume_ratio_only_missing_hits_alert_but_not_flagged():
    """把两条合起来看：同一轮里 volume_ratio 缺失**既命中又不进缺失计数**。

    这是最直接的"语义混淆"反例 —— 修复前会出现
    "命中了告警，同时被记成能力不可评估"这种自相矛盾的账。

    **修复前** ``assert obs.unavailable_capability == 0`` 失败（实际 1）。
    """
    st = FakeState()
    feed_burst(st, "600000")
    caps = SourceCapabilities(source="partial", turnover=True,
                              volume_ratio=False, depth_l1=True,
                              depth_l5=False, outer_inner=False,
                              float_cap=True, provider_time=False)
    obs = RoundObservationSet(requested=1)
    alerts = build({}).evaluate(
        make_snapshot([burst_quote(volume_ratio=0.0)], ts=NOW),
        _ctx(st, caps, obs))

    assert len(alerts) == 1
    assert obs.unavailable_capability == 0, (
        "已经命中告警的票不可能同时是'能力不可评估' —— 修复前正是这个矛盾")


def test_tencent_full_capability_has_no_unavailable():
    """回归保护：Tencent 全能力时必须零缺失（修复前后都绿）。"""
    st = FakeState()
    feed_burst(st, "600000")
    obs = RoundObservationSet(requested=1)
    alerts = build({}).evaluate(
        make_snapshot([burst_quote()], ts=NOW),
        _ctx(st, capabilities_for("tencent"), obs))
    assert len(alerts) == 1
    assert obs.unavailable_capability == 0


def test_capability_absence_does_not_cross_sources():
    """回归保护：一个源缺能力不得污染另一个源的表现。

    修复前后都绿 —— 挡的是"为了记账而改了全局状态"。
    """
    strong = FakeState()
    feed_burst(strong, "600000")
    obs_strong = RoundObservationSet(requested=1)
    strong_alerts = build({}).evaluate(
        make_snapshot([burst_quote()], ts=NOW),
        _ctx(strong, capabilities_for("tencent"), obs_strong))

    weak = FakeState()
    feed_burst(weak, "600000")
    obs_weak = RoundObservationSet(requested=1)
    build({}).evaluate(
        make_snapshot([burst_quote(turnover=0.0, volume_ratio=0.0)], ts=NOW),
        _ctx(weak, CAPABILITY_TABLE["sina"], obs_weak))

    assert len(strong_alerts) == 1, "另一源的缺失不得影响本源的命中"
    assert obs_strong.unavailable_capability == 0
    assert obs_weak.unavailable_capability == 1


def test_rule_still_works_with_broken_observation():
    """回归保护：账本坏掉绝不能把异常抛进信号主链路。

    可观测性是加分项 —— 它坏掉时规则必须照常产出告警。修复前后都绿，
    但本轮的 WP01 记账新增了若干 ``getattr`` 调用点，这条保证它们都被
    防御式包裹。
    """
    class Bad:
        pass

    st = FakeState()
    feed_burst(st, "600000")
    alerts = VolumeBurstRule().evaluate(
        make_snapshot([burst_quote()], ts=NOW),
        _ctx(st, capabilities_for("tencent"), Bad()))
    assert len(alerts) == 1, "账本坏掉时规则仍须正常出告警"
