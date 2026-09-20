"""``arad/capabilities.py`` 与 IT-P1-CAPABILITY-001 的回归测试。

背景
----
``Quote.turnover`` / ``volume_ratio`` 是 ``float=0.0``，同一类型承载两种语义：

* **真业务零** —— 源提供了该字段，值就是 0；
* **能力缺失** —— 源不输出该字段，解析器只能填 ``0.0`` 占位。

``SinaSource`` 属于后者（源码里字面写 ``turnover=0.0``）。
``VolumeBurstRule`` 原先拿 ``q.turnover < min_turnover`` 直接判，于是
Tencent→Sina 热备时放量规则对**所有**股票静默失效，而 ``SourceManager``
认为调用成功、``health()`` 也正常 —— 用户只看到这类告警整类消失。

阶段一约定（本轮审计报告 §3.4）：**只显式化可评估性，不擅自改变误报率**。
即门槛判定保持原样，新增的是 ``unavailable_capability`` 记账。
"""
from __future__ import annotations

import time
from collections import deque
from datetime import datetime

import pytest

from fakes import make_quote, make_snapshot

from arad.capabilities import (
    CAPABILITY_KEYS,
    CAPABILITY_TABLE,
    UNKNOWN_CAPABILITIES,
    ObservationDecision,
    RoundObservationSet,
    SourceCapabilities,
    capabilities_for,
)
from arad.rules.base import RuleContext
from arad.rules.volume_burst import DEFAULTS, VolumeBurstRule
from arad.session import SessionPhase

NOW = datetime(2026, 9, 15, 10, 30, 0)
NOW_EPOCH = NOW.timestamp()
RULE = VolumeBurstRule()


class FakeState:
    """最小 EngineState 替身（与 test_rule_volume_burst.py 同款）。"""

    def __init__(self) -> None:
        self.quotes: dict = {}
        self.history: dict[str, deque] = {}
        self.last_price: dict[str, float] = {}
        self.first_seen: dict = {}
        self.day_open: dict[str, float] = {}

    def window(self, code: str, seconds: float, now_epoch: float):
        rows = self.history.get(code) or ()
        start = now_epoch - seconds
        return [r for r in rows if start <= r[0] <= now_epoch]

    def price_change(self, code: str, seconds: float, now_epoch: float):
        rows = self.window(code, seconds, now_epoch)
        if len(rows) < 2 or rows[0][1] <= 0:
            return None
        return (rows[-1][1] / rows[0][1] - 1.0) * 100.0

    def volume_delta(self, code: str, seconds: float, now_epoch: float) -> float:
        rows = self.window(code, seconds, now_epoch)
        if len(rows) < 2:
            return 0.0
        return max(0.0, rows[-1][2] - rows[0][2])

    def feed(self, code: str, points) -> None:
        dq = self.history.setdefault(code, deque(maxlen=4096))
        dq.clear()
        for p in points:
            dq.append(tuple(p))


def feed_burst(st: FakeState, code: str = "600000") -> None:
    """elapsed=1800s、cum=100000 手、近 60s 增量 32000 手 => 速度约 9.6 倍。"""
    st.feed(code, [
        (NOW_EPOCH - 3600.0, 9.90, 63_000.0),
        (NOW_EPOCH - 60.0, 10.05, 68_000.0),
        (NOW_EPOCH, 10.30, 100_000.0),
    ])


AMOUNT = 100_000.0 * 100.0 * ((10.40 + 9.98 + 10.30) / 3.0)


def burst_quote(code: str = "600000", **kw):
    base = dict(price=10.30, prev_close=10.00, open=10.00, high=10.40,
                low=9.98, volume_lots=100_000.0, amount=AMOUNT,
                turnover=3.0, volume_ratio=5.2, name="测试股")
    base.update(kw)
    return make_quote(code=code, **base)


def ctx_of(st: FakeState, *, source=None, observation=None):
    caps = capabilities_for(source) if source is not None else None
    return RuleContext(
        state=st, cfg=dict(DEFAULTS), now=NOW, session=SessionPhase.MORNING,
        elapsed_trading_seconds=1800.0, minutes_to_close=120.0,
        capabilities=caps, observation=observation,
    )


# ---------------------------------------------------------------------------
# 1. 能力表本身
# ---------------------------------------------------------------------------
def test_capability_keys_are_stable():
    assert CAPABILITY_KEYS == ("turnover", "volume_ratio", "depth_l1",
                               "depth_l5", "outer_inner", "float_cap",
                               "provider_time")


def test_capability_table_matches_known_parsers():
    """表里三家的能力必须与解析器实际赋的字段一致（防表与实现漂移）。"""
    t = CAPABILITY_TABLE["tencent"]
    assert t.turnover and t.volume_ratio and t.depth_l5 and t.outer_inner

    s = CAPABILITY_TABLE["sina"]
    # Sina 的源码字面写 turnover=0.0 / volume_ratio=0.0 / float_cap=0.0
    assert not s.turnover
    assert not s.volume_ratio
    assert not s.float_cap
    assert s.depth_l1 and not s.depth_l5 and not s.outer_inner

    e = CAPABILITY_TABLE["eastmoney"]
    assert e.turnover and e.volume_ratio and e.float_cap
    assert not e.depth_l1 and not e.depth_l5     # 快照路径没有盘口


def test_unknown_source_is_conservative():
    """不认识的源一律按"不提供"处理，绝不假定它有字段。"""
    for name in ("", "whatever", "TENCENTX"):
        caps = capabilities_for(name)
        assert caps is UNKNOWN_CAPABILITIES or caps.missing() == list(CAPABILITY_KEYS)
        assert not caps.supports("turnover")


def test_capabilities_for_prefers_instance_declaration():
    """源自己声明的 capabilities 优先于内置表（第三方源可覆盖）。"""

    class Src:
        name = "sina"
        capabilities = SourceCapabilities(source="sina", turnover=True)

    assert capabilities_for(Src()).supports("turnover") is True
    # 实例没声明时回落到内置表
    class Bare:
        name = "sina"
    assert capabilities_for(Bare()).supports("turnover") is False


def test_supports_unknown_key_is_false():
    caps = CAPABILITY_TABLE["tencent"]
    assert caps.supports("turnover") is True
    assert caps.supports("nonexistent_field") is False


# ---------------------------------------------------------------------------
# 2. RuleContext.provides —— 未挂 capability 时必须保持旧行为
# ---------------------------------------------------------------------------
def test_provides_without_capabilities_is_permissive():
    """老调用方/单测不挂 capability 时 provides() 返回 True。

    这是**向后兼容的关键**：不涉及多源切换的既有路径行为必须逐字不变。
    """
    st = FakeState()
    ctx = ctx_of(st, source=None)
    assert ctx.capabilities is None
    assert ctx.provides("turnover") is True
    assert ctx.provides("anything") is True


def test_provides_follows_capabilities():
    st = FakeState()
    assert ctx_of(st, source="tencent").provides("turnover") is True
    assert ctx_of(st, source="sina").provides("turnover") is False


# ---------------------------------------------------------------------------
# 3. 核心回归：同一行情，不同 source，可评估性必须可区分
# ---------------------------------------------------------------------------
def test_tencent_evaluates_and_hits():
    st = FakeState()
    feed_burst(st)
    obs = RoundObservationSet(source="tencent",
                              capabilities=CAPABILITY_TABLE["tencent"])
    alerts = RULE.evaluate(make_snapshot([burst_quote()], ts=NOW),
                           ctx_of(st, source="tencent", observation=obs))
    assert len(alerts) == 1
    assert obs.unavailable_capability == 0


def test_sina_marks_unavailable_instead_of_silent_zero():
    """IT-P1-CAPABILITY-001 的正面回归。

    Sina 不提供 turnover，必须产生明确的 ``unavailable_capability`` 记录，
    而不是无声无息地 ``0 alerts`` 让人以为"这段时间没放量"。

    阶段一约定：门槛判定不变，所以这里仍然不产出告警 —— 但**必须留下痕迹**。
    """
    st = FakeState()
    feed_burst(st)
    obs = RoundObservationSet(source="sina",
                              capabilities=CAPABILITY_TABLE["sina"])
    alerts = RULE.evaluate(
        make_snapshot([burst_quote(turnover=0.0, volume_ratio=0.0)], ts=NOW),
        ctx_of(st, source="sina", observation=obs))

    assert alerts == []                                  # 行为未变（阶段一）
    assert obs.unavailable_capability == 1               # 但不再静默
    assert "600000" in obs.unavailable_codes
    reasons = {d.reason for d in obs.decisions}
    assert "turnover_not_provided" in reasons
    assert all(d.status == "unavailable_capability" for d in obs.decisions)


def test_same_market_state_differs_only_by_capability():
    """同一 Quote 业务状态：Tencent 可评估，Sina 记为能力缺失。

    这条锁住问题本质 —— 差异必须来自 capability，而不是行情数值本身。
    """
    quote = burst_quote(turnover=0.0, volume_ratio=0.0)   # Sina 视角的占位值

    st_t = FakeState()
    feed_burst(st_t)
    obs_t = RoundObservationSet(source="tencent")
    RULE.evaluate(make_snapshot([quote], ts=NOW),
                  ctx_of(st_t, source="tencent", observation=obs_t))

    st_s = FakeState()
    feed_burst(st_s)
    obs_s = RoundObservationSet(source="sina")
    RULE.evaluate(make_snapshot([quote], ts=NOW),
                  ctx_of(st_s, source="sina", observation=obs_s))

    # 同一份 Quote：Tencent 判定为"真业务零"（走门槛），Sina 判定为"能力缺失"
    assert obs_t.unavailable_capability == 0
    assert obs_s.unavailable_capability == 1


def test_restoring_tencent_restores_evaluable():
    """切回 Tencent 后 unavailable 必须归零（状态不残留）。"""
    st = FakeState()
    feed_burst(st)

    obs_sina = RoundObservationSet(source="sina")
    RULE.evaluate(make_snapshot([burst_quote(turnover=0.0, volume_ratio=0.0)],
                                ts=NOW),
                  ctx_of(st, source="sina", observation=obs_sina))
    assert obs_sina.unavailable_capability == 1

    obs_tencent = RoundObservationSet(source="tencent")   # 新的一轮，新账本
    alerts = RULE.evaluate(make_snapshot([burst_quote()], ts=NOW),
                           ctx_of(st, source="tencent", observation=obs_tencent))
    assert len(alerts) == 1
    assert obs_tencent.unavailable_capability == 0


def test_unavailable_counted_per_code_not_per_field():
    """同一只票即使多个字段缺失，也只能算 1 只不可评估。

    按字段次数记会把"多少标的可评估"夸大。注意：换手率门槛不达标时规则
    会 ``continue``，所以量比那一步根本不会执行 —— 这本身是正确行为
    （不产出无意义的二次判定）。两个字段各自独立缺失的场景见下一条。
    """
    st = FakeState()
    feed_burst(st)
    obs = RoundObservationSet(source="sina")
    RULE.evaluate(
        make_snapshot([burst_quote(turnover=0.0, volume_ratio=0.0)], ts=NOW),
        ctx_of(st, source="sina", observation=obs))

    assert obs.unavailable_capability == 1
    assert obs.unavailable_codes == {"600000"}
    # 换手率门槛在量比之前，提前 continue，故只留 turnover 一条明细
    assert [d.reason for d in obs.decisions] == ["turnover_not_provided"]


def test_two_missing_fields_recorded_when_both_reachable():
    """能力是**源级**的：换手率可用、量比不可用时，两者**分账**记录。

    WP01 / IT-P1-CAPABILITY-003 **语义修正**：本条以前断言
    ``unavailable_capability == 1``，理由是"量比缺失也算能力缺失"。
    但量比缺失在本规则里的真实语义是**可跳过的**：``vr`` 取占位 0.0 时
    ``vr > 0.0`` 为假 -> 量比门槛被跳过，规则靠速度/金额**仍能命中**。
    把它记成"不可评估"正是审计指出的语义混淆（一个状态名同时承载
    "硬阻断"与"可跳过"），会让 5000 码里只坏 1 个的场景被误判成
    整类规则 0% 可评估。

    所以现在：``turnover`` 缺失（且 min_turnover>0）= **blocking**，
    进 ``unavailable_codes``；``volume_ratio`` 缺失 = **advisory**，
    只留明细、不进缺失计数。**这不是为了让测试变绿而放宽断言，而是修正
    断言本身所依据的语义**；blocking 侧的可见性由
    ``test_sina_marks_unavailable_instead_of_silent_zero`` 继续钉住。
    """
    st = FakeState()
    feed_burst(st)
    obs = RoundObservationSet(source="partial")
    caps = SourceCapabilities(source="partial", turnover=True, volume_ratio=False)
    ctx = RuleContext(
        state=st, cfg=dict(DEFAULTS), now=NOW, session=SessionPhase.MORNING,
        elapsed_trading_seconds=1800.0, minutes_to_close=120.0,
        capabilities=caps, observation=obs)
    alerts = RULE.evaluate(
        make_snapshot([burst_quote(turnover=3.0, volume_ratio=0.0)], ts=NOW), ctx)

    # 缺量比是 advisory：该票仍可评估，且靠速度/金额真的命中了
    assert obs.unavailable_capability == 0, (
        "缺 volume_ratio 允许跳过 -> 不得记进能力缺失计数")
    assert len(alerts) == 1, "跳过量比门槛后速度条件成立，应当命中"
    # 明细仍在，但状态区分成 advisory_missing（不是 unavailable_capability）
    assert [d.reason for d in obs.decisions] == ["volume_ratio_not_provided"]
    assert [d.status for d in obs.decisions] == ["advisory_missing"]


def test_capability_is_source_level_not_value_level():
    """同源下改数值不改变可评估性判定 —— 判定只看 capability。

    这条锁住语义：Sina 即使某只票"碰巧"报出 turnover=3.0，也依然算
    能力缺失（因为占位值不可信）；Tencent 报 0.0 则算真实业务零。
    """
    st = FakeState()
    feed_burst(st)

    obs_high = RoundObservationSet(source="sina")
    RULE.evaluate(make_snapshot([burst_quote(turnover=3.0, volume_ratio=5.2)],
                                ts=NOW),
                  ctx_of(st, source="sina", observation=obs_high))

    obs_low = RoundObservationSet(source="sina")
    RULE.evaluate(make_snapshot([burst_quote(turnover=0.0, volume_ratio=0.0)],
                                ts=NOW),
                  ctx_of(st, source="sina", observation=obs_low))

    assert obs_high.unavailable_capability == 1
    assert obs_low.unavailable_capability == 1            # 与数值无关


def test_no_observation_is_silent_not_crash():
    """没挂 observation 时只记账失败，不得影响规则主流程。"""
    st = FakeState()
    feed_burst(st)
    alerts = RULE.evaluate(make_snapshot([burst_quote()], ts=NOW),
                           ctx_of(st, source="sina", observation=None))
    assert isinstance(alerts, list)                      # 没抛异常即可


def test_broken_observation_does_not_break_rule():
    """observation 是个坏对象时，可观测性失败绝不能影响出告警。"""
    class Bad:
        @property
        def unavailable_codes(self):
            raise RuntimeError("boom")

    st = FakeState()
    feed_burst(st)
    alerts = RULE.evaluate(make_snapshot([burst_quote()], ts=NOW),
                           ctx_of(st, source="sina", observation=Bad()))
    assert isinstance(alerts, list)


# ---------------------------------------------------------------------------
# 4. RoundObservationSet 账本
# ---------------------------------------------------------------------------
def test_coverage_math():
    obs = RoundObservationSet(requested=100, returned=90, admitted=80)
    assert obs.coverage() == pytest.approx(0.9)
    assert obs.as_dict()["coverage"] == pytest.approx(0.9)


def test_coverage_zero_requested_is_zero_not_one():
    """requested=0 时不许伪造 100% 覆盖（休市/空池的真实语义是"没数据"）。"""
    assert RoundObservationSet(requested=0, returned=0).coverage() == 0.0


def test_unknown_missing_listed():
    obs = RoundObservationSet(requested=3, returned=2,
                              unknown_missing=("000002",))
    assert obs.missing_count == 1
    assert obs.as_dict()["unknown_missing"] == ["000002"]


def test_as_dict_is_json_safe():
    import json
    obs = RoundObservationSet(source="sina",
                              capabilities=CAPABILITY_TABLE["sina"],
                              requested=10, returned=9, admitted=8,
                              unknown_missing=("000002",))
    obs.unavailable_codes.add("600000")
    obs.decisions.append(ObservationDecision(
        code="600000", status="unavailable_capability",
        rule="volume_burst", reason="turnover_not_provided",
        missing=("turnover",)))
    s = json.dumps(obs.as_dict(), ensure_ascii=False)     # 不抛即通过
    assert '"unavailable_capability": 1' in s
    assert '"source": "sina"' in s


def test_capabilities_as_dict_roundtrip():
    d = CAPABILITY_TABLE["tencent"].as_dict()
    assert d["source"] == "tencent"
    assert d["turnover"] is True
    assert set(CAPABILITY_KEYS).issubset(d)
