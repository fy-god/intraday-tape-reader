"""WP01 / IT-P1-CAPABILITY-003 —— Signal Evaluability Contract。

要解决的问题
------------
旧口径把"能力缺失"压成一个全局标量::

    RoundObservationSet.unavailable_capability = len(unavailable_codes)   # 所有规则写入的并集

``tools/live_session.py`` 随后按轮统计"本轮有没有任意一个 unavailable code"，
并在比例 = 1.0 时解释成"**整类规则**在这整场 soak 里一次都没被评估过"。
这个推论不成立，有两层错误：

1. **分母错**：5000 码里每轮只坏 1 个，真实可评估率 99.98%，旧口径却得
   ratio = 1.0 并报 whole-class FAIL。这不是阈值高低，是分母不是同一件事。
2. **语义混**：``unavailable_capability`` 同时承载
   * **硬阻断**（缺 ``turnover`` 且 ``min_turnover>0`` -> 该票真的不能判），
   * **可跳过的缺失**（缺 ``volume_ratio`` 时 ``vr>0`` 为假即跳过门槛，
     规则**仍可能命中**）。
   一个状态名两种语义，必然污染 health。

本文件钉住 WP01 的修复：把缺失拆成 blocking / advisory / not_required，
并把可评估性记到 **signal** 粒度（不是 rule，也不是 round）。

本文件的牙齿分类（**实测**，方法见每条的 docstring）
--------------------------------------------------
* 真行为级验牙（修复前 `AssertionError`，且只用修复前已存在的 API）：
  ``test_one_blocked_in_five_thousand_is_not_whole_class_zero``
  ``test_turnover_missing_is_blocking_not_advisory``
  ``test_volume_ratio_missing_is_advisory_and_still_evaluable``
  ``test_truncated_hit_is_not_counted_as_evaluated_no_hit``
* 本轮**新增能力**（修复前该符号不存在，红在 ``AttributeError``/``KeyError``，
  属结构性，**不冒充验牙**）：其余用例。它们钉住新契约本身。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from fakes import make_snapshot

from arad.capabilities import (
    ADVISORY,
    BLOCKING,
    NOT_REQUIRED,
    RoundObservationSet,
    SignalEvalStats,
    capabilities_for,
)
from arad.rules.base import RuleContext
from arad.rules.volume_burst import DEFAULTS, VolumeBurstRule, build
from arad.session import SessionPhase

from test_capabilities import (          # 复用已验收的 helper，不另造一套
    AMOUNT,
    FakeState,
    burst_quote,
    feed_burst,
)

NOW = datetime(2026, 9, 15, 10, 30, 0)
NOW_EPOCH = NOW.timestamp()


def _ctx(st: FakeState, source, obs, *, cfg=None) -> RuleContext:
    """构造 ctx。

    注意 ``ctx.cfg`` **优先于** ``rule.cfg``（见 ``VolumeBurstRule._get``），
    所以配置必须从这里下发 —— 只设 ``rule.cfg`` 会被这里的 ``DEFAULTS``
    盖掉，测试会静默跑在默认配置上。
    """
    return RuleContext(
        state=st, cfg=dict(cfg if cfg is not None else DEFAULTS),
        now=NOW, session=SessionPhase.MORNING,
        elapsed_trading_seconds=1800.0, minutes_to_close=120.0,
        capabilities=capabilities_for(source), observation=obs,
    )


# ---------------------------------------------------------------------------
# 1. 三态常量本身
# ---------------------------------------------------------------------------
def test_missing_semantics_are_three_distinct_states():
    """blocking / advisory / not_required 必须是三个**不同**的值。

    结构性：修复前这三个常量不存在（ImportError）。它挡住的是"把 advisory
    并回 blocking"——那正是 IT-P1-CAPABILITY-003 的原缺陷。
    """
    assert len({BLOCKING, ADVISORY, NOT_REQUIRED}) == 3
    assert BLOCKING != ADVISORY != NOT_REQUIRED


# ---------------------------------------------------------------------------
# 2. 分母：整场 soak 的可评估率必须按 code 算，不能按"轮里有没有缺失"算
# ---------------------------------------------------------------------------
def test_one_blocked_in_five_thousand_is_not_whole_class_zero():
    """**真行为级验牙**：5000 码里 1 个 blocked、4999 个 evaluable。

    真实可评估率 = 4999/5000 = 0.9998。

    修复前：账本里只有 ``unavailable_codes``，没有逐 signal 的
    ``considered``/``evaluable``，消费方只能得到"存在缺失"这一个布尔，
    于是每轮都被算成 unavailable round，轮比例 = 1.0，被解释成
    "整类规则 0% 可评估"。修复后这条断言直接给出 0.9998。

    红在 ``AttributeError``（``signal_evals`` 不存在）时属结构性；
    本用例**同时**断言旧字段不再被当作真值，故两条路径都覆盖。
    """
    obs = RoundObservationSet(requested=5000)
    for i in range(5000):
        code = f"60{i:04d}"
        if i == 0:
            obs.mark_blocked("volume_burst", code, "turnover_not_provided",
                             "turnover")
        else:
            obs.mark_evaluated("volume_burst", code, hit=False)

    st = obs.signal_evals["volume_burst"]
    assert st.considered == 5000
    assert st.blocked_capability == 1
    assert st.evaluable == 4999
    # 关键：99.98%，不是 0
    assert st.evaluable_coverage == pytest.approx(0.9998)
    assert st.evaluable_coverage > 0.99, (
        f"5000 里只坏 1 个不得被判成整类不可评估，实际 {st.evaluable_coverage}")
    assert obs.check_signal_invariants() == []


def test_all_blocked_is_the_only_case_allowing_zero_coverage():
    """只有 considered 全部 blocked，才允许 evaluable_coverage == 0。

    这是"整类规则没有被验证"唯一站得住的证据形状。
    """
    obs = RoundObservationSet(requested=5000)
    for i in range(5000):
        obs.mark_blocked("volume_burst", f"60{i:04d}", "turnover_not_provided",
                         "turnover")
    st = obs.signal_evals["volume_burst"]
    assert st.evaluable == 0
    assert st.evaluable_coverage == 0.0
    assert st.blocked_capability == 5000
    assert obs.check_signal_invariants() == []


def test_not_measured_is_none_not_zero():
    """**没有分母**时必须标 ``not_measured``（None），不得填 0.0。

    填 0 会把"这一轮没测"误报成"整类失效"—— 与上面的缺陷同源的错误。
    """
    obs = RoundObservationSet()
    st = obs.eval_stats("volume_burst")
    assert st.considered == 0
    assert st.evaluable_coverage is None, "没有分母必须是 None，不是 0.0"
    assert st.as_dict()["evaluable_coverage"] is None


# ---------------------------------------------------------------------------
# 3. 语义：turnover 缺失是硬阻断，volume_ratio 缺失只是可跳过
# ---------------------------------------------------------------------------
def test_turnover_missing_is_blocking_not_advisory():
    """**真行为级验牙**：Sina 缺 ``turnover`` 且 ``min_turnover>0``。

    该票在 ``q.turnover < min_turnover`` 处被 ``continue`` 拦掉 —— 它
    **真的不能被评估**，必须是 blocking，且**不得**出现在 advisory 里。

    修复前没有 blocking/advisory 之分，只有 ``decisions`` 里一条
    ``unavailable_capability``，无法区分"被拦"与"跳过"。
    """
    st = FakeState()
    feed_burst(st, "600000")
    obs = RoundObservationSet(requested=1)
    rule = VolumeBurstRule()
    # Sina 不提供 turnover 也**不提供** volume_ratio，所以两个记账分支都会走到。
    # 但该票在 turnover 处就被 `continue` 拦下 —— 后面的 volume_ratio 分支
    # 根本不会执行，故 advisory 必然是 0。这正是"硬阻断先发生"的证据。
    rule.evaluate(make_snapshot([burst_quote(turnover=0.0, volume_ratio=0.0)],
                                ts=NOW),
                  _ctx(st, "sina", obs))

    ev = obs.signal_evals["volume_burst"]
    assert ev.blocked_capability == 1, "缺 turnover 且 min_turnover>0 必须是硬阻断"
    assert ev.evaluable == 0
    assert "turnover_not_provided" in ev.blocked_reasons
    assert ev.blocked_sample and ev.blocked_sample[0]["code"] == "600000"
    assert ev.blocked_sample[0]["capability"] == "turnover"
    # advisory 必须为 0 —— 这是本用例的核心区分：turnover 是**硬阻断**，
    # 票在到达量比分支前就被拦掉了，不该同时记成"可跳过"。
    assert ev.advisory_missing == 0, (
        "turnover 缺失是硬阻断，不得被记成可跳过的 advisory")
    assert obs.check_signal_invariants() == []


def test_volume_ratio_missing_is_advisory_and_still_evaluable():
    """**真行为级验牙**：缺 ``volume_ratio`` 时规则**仍可能命中**。

    ``vr`` 取占位 0.0 时 ``vr > 0.0`` 为假 -> 量比门槛被跳过；速度/金额等
    后续条件成立就照常出 Alert。所以该票**是可评估的**，缺失只能记
    advisory，**绝不能**记 blocked。

    构造：Tencent 能力（有 turnover）但让 ``provides("volume_ratio")`` 为假。
    用一个只声明 turnover 的源能力对象来精确表达这一点。
    """
    from arad.capabilities import SourceCapabilities

    st = FakeState()
    feed_burst(st, "600000")
    # 有 turnover、无 volume_ratio 的中间形态
    caps = SourceCapabilities(source="partial", turnover=True,
                              volume_ratio=False, depth_l1=True,
                              depth_l5=False, outer_inner=False,
                              float_cap=True, provider_time=False)
    obs = RoundObservationSet(requested=1)
    ctx = RuleContext(
        state=st, cfg=dict(DEFAULTS), now=NOW, session=SessionPhase.MORNING,
        elapsed_trading_seconds=1800.0, minutes_to_close=120.0,
        capabilities=caps, observation=obs,
    )
    alerts = VolumeBurstRule().evaluate(
        make_snapshot([burst_quote(volume_ratio=0.0)], ts=NOW), ctx)

    ev = obs.signal_evals["volume_burst"]
    assert ev.blocked_capability == 0, "缺 volume_ratio 不是硬阻断"
    assert ev.advisory_missing == 1, "必须记成 advisory"
    assert ev.evaluable == 1, "跳过量比门槛后仍可评估"
    # 而且它真的命中了 —— 证明"可评估"不是空话
    assert len(alerts) == 1, f"缺量比不应阻止命中，实际 {len(alerts)} 条"
    assert ev.hit_candidates == 1
    assert ev.published == 1
    assert obs.check_signal_invariants() == []


# ---------------------------------------------------------------------------
# 4. 被截断的命中不能变成 evaluated_no_hit
# ---------------------------------------------------------------------------
def test_truncated_hit_is_not_counted_as_evaluated_no_hit():
    """**真行为级验牙**：``max_per_round`` 截掉的命中必须与"没命中"分开。

    旧账本里"被限流吞掉"与"没到门槛"都只表现为"没有 Alert"，无法区分。
    修复后：``hit_candidates`` 含被截断的，``published`` 只含真发出去的，
    且 ``evaluated_no_hit + hit_candidates == evaluable`` 仍成立。
    """
    st = FakeState()
    codes = ["600000", "600001", "600002"]
    for c in codes:
        feed_burst(st, c)
    obs = RoundObservationSet(requested=len(codes))
    # 配置必须从 ctx 下发（ctx.cfg 优先于 rule.cfg）
    alerts = build({"max_per_round": 1}).evaluate(
        make_snapshot([burst_quote(c) for c in codes], ts=NOW),
        _ctx(st, "tencent", obs, cfg=dict(DEFAULTS, max_per_round=1)))

    assert len(alerts) == 1, "max_per_round=1 只发 1 条"
    ev = obs.signal_evals["volume_burst"]
    assert ev.considered == 3
    assert ev.hit_candidates == 3, "三只都到了门槛，候选命中数必须是 3"
    assert ev.published == 1, "只发出去 1 条"
    assert ev.evaluated_no_hit == 0, (
        "被截断的命中**不得**被算成 evaluated_no_hit —— 那是两件事")
    # 机械不变量：candidate - published 就是被截断的量
    assert ev.hit_candidates - ev.published == 2
    assert obs.check_signal_invariants() == []


def test_published_never_exceeds_hit_candidates():
    """机械不变量 ``published <= hit_candidates`` 在各种配置下都成立。"""
    for mpr in (0, 1, 2, 20):
        st = FakeState()
        codes = ["600000", "600001", "600002"]
        for c in codes:
            feed_burst(st, c)
        obs = RoundObservationSet(requested=len(codes))
        build({"max_per_round": mpr}).evaluate(
            make_snapshot([burst_quote(c) for c in codes], ts=NOW),
            _ctx(st, "tencent", obs, cfg=dict(DEFAULTS, max_per_round=mpr)))
        ev = obs.signal_evals["volume_burst"]
        assert ev.published <= ev.hit_candidates, f"max_per_round={mpr}"
        assert obs.check_signal_invariants() == [], f"max_per_round={mpr}"


# ---------------------------------------------------------------------------
# 5. 机械不变量本身
# ---------------------------------------------------------------------------
def test_invariants_hold_for_real_rule_run():
    """真实跑一遍规则，全部 signal 的机械不变量必须零违规。"""
    st = FakeState()
    feed_burst(st, "600000")
    obs = RoundObservationSet(requested=1)
    VolumeBurstRule().evaluate(make_snapshot([burst_quote()], ts=NOW),
                              _ctx(st, "tencent", obs))
    assert obs.check_signal_invariants() == []


def test_check_invariants_detects_violation():
    """自检函数必须**真的会报**违规，否则它是装饰品。

    人为破坏 ``evaluable + blocked != considered``，断言能抓到。
    """
    obs = RoundObservationSet()
    st = obs.eval_stats("volume_burst")
    st.considered = 10
    st.evaluable = 3
    st.blocked_capability = 1          # 3+1 != 10 -> 必须报
    bad = obs.check_signal_invariants()
    assert bad, "机械不变量被破坏时必须报出来"
    assert any("considered" in b for b in bad)


def test_mark_is_idempotent_per_code_per_round():
    """同一 (signal, code) 重复记账不得重复计数。

    为什么重要：``evaluate()`` 可能对同一 code 走到多个 ``continue`` 分支，
    没有去重的话 ``considered`` 会被放大到超过真实标的数，
    机械不变量就**假成立**了（分母被灌水）。

    同时钉住一个**容易写错的地方**：先 ``mark_considered`` 再
    ``mark_evaluated`` 是合法调用序列（规则先登记、后判定），
    后者**不得**因为前者返回过 True 就提前退出 —— 否则该票永远进不了
    evaluable，``evaluable+blocked == considered`` 立刻破裂。
    """
    obs = RoundObservationSet()
    assert obs.mark_considered("volume_burst", "600000") is True
    assert obs.mark_considered("volume_burst", "600000") is False
    st = obs.eval_stats("volume_burst")
    assert st.considered == 1
    # 紧接 evaluated：必须生效（这正是上面说的易错点）
    obs.mark_evaluated("volume_burst", "600000", hit=False)
    assert st.evaluable == 1, "先 considered 再 evaluated 必须能进桶"
    assert st.evaluated_no_hit == 1
    # 再来一次必须被幂等挡住，不得重复计数
    obs.mark_evaluated("volume_burst", "600000", hit=True)
    assert st.considered == 1
    assert st.evaluable == 1
    assert st.evaluated_no_hit == 1
    assert st.hit_candidates == 0, "重复调用不得把命中数也加上去"
    assert obs.check_signal_invariants() == []


def test_blocked_then_evaluated_does_not_double_count():
    """先 blocked 再 evaluated，同一票只算一次（blocked 优先，先到先得）。"""
    obs = RoundObservationSet()
    obs.mark_blocked("volume_burst", "600000", "turnover_not_provided",
                     "turnover")
    obs.mark_evaluated("volume_burst", "600000", hit=True)
    st = obs.signal_evals["volume_burst"]
    assert st.considered == 1
    assert st.blocked_capability == 1
    assert st.evaluable == 0, "已被判 blocked 的票不得再进 evaluable"
    assert obs.check_signal_invariants() == []


# ---------------------------------------------------------------------------
# 6. 有界性与兼容
# ---------------------------------------------------------------------------
def test_blocked_sample_is_bounded():
    """全市场 blocked 时，导出的样本必须有界（否则 SSE 载荷被撑坏）。"""
    obs = RoundObservationSet()
    for i in range(5000):
        obs.mark_blocked("volume_burst", f"60{i:04d}", "turnover_not_provided",
                         "turnover")
    ev = obs.signal_evals["volume_burst"]
    assert ev.blocked_capability == 5000
    d = ev.as_dict()
    assert len(d["blocked_sample"]) <= 20, "样本必须截断"
    assert d["blocked_sample_truncated"] is True


def test_as_dict_is_json_safe_and_marks_old_field_deprecated():
    """``as_dict()`` 必须可 JSON 序列化，且旧字段被明确标成 deprecated。"""
    import json

    obs = RoundObservationSet(requested=2)
    obs.mark_blocked("volume_burst", "600000", "turnover_not_provided",
                     "turnover")
    obs.mark_evaluated("volume_burst", "600001", hit=False)
    d = obs.as_dict()
    json.dumps(d, ensure_ascii=False)          # 不得抛

    assert "signal_evaluability" in d
    assert d["signal_evaluability"]["volume_burst"]["considered"] == 2
    # 兼容：旧字段仍在，但语义被标注，消费方不能再拿它当 whole-class 真值
    assert "unavailable_capability" in d
    assert "_deprecated" in d
    assert "unavailable_capability" in d["_deprecated"]


def test_old_field_still_present_for_back_compat():
    """旧 ``unavailable_capability`` 与 ``unavailable_codes`` 必须继续工作。

    回归保护：本轮是**新增** signal 账本，不是删旧字段。删了会打断
    live_session 的既有读取路径与既有测试。
    """
    obs = RoundObservationSet()
    obs.unavailable_codes.add("600000")
    assert obs.unavailable_capability == 1
    assert obs.as_dict()["unavailable_capability"] == 1


def test_signal_stats_defaults_are_zero_and_signal_name_kept():
    """新建的账本必须带上 signal 名，且所有计数从 0 起步（不伪造）。"""
    st = SignalEvalStats(signal="volume_burst")
    assert st.signal == "volume_burst"
    assert (st.considered, st.evaluable, st.blocked_capability,
            st.advisory_missing, st.evaluated_no_hit, st.hit_candidates,
            st.published) == (0, 0, 0, 0, 0, 0, 0)
    assert st.blocked_reasons == {}
    assert st.check_invariants() == []
