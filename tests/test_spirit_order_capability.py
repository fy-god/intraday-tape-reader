"""IT-P1-CAPABILITY-004 —— ``spirit_order`` 的 level-1 退路与逐 pattern 可评估性。

审计原始缺陷
------------
``spirit_order`` 的 8 个 pattern 各自依赖**不同的** capability，但整条规则
共用一份"可评估性"叙事，而 ``_best_order()`` 又只遍历 ``q.bid_vols`` /
``q.ask_vols``（五档数量数组），调用点传进去的兜底值却是 ``_num(q.bid1, 0.0)``
—— 那是**价格**，不是数量。于是：

* Sina（``depth_l1=T`` / ``depth_l5=F``，五档数组为空）下，``_best_order``
  一档都找不到 -> **机构买单/卖单静默 0 告警**，源 health 完全正常，
  用户只看到"今天没信号"；
* ``git grep -c "unavailable_capability\\|_mark_unavailable"`` 在本文件的
  返回值是 **0** —— 连"这批信号在当前源下不可评估"都没人记。

本文件按三块验收：

1. **L1 数量退路**（第 1 节）：Sina 形态下用**真实的** ``bid_vol``/``ask_vol``
   （手）判定，绝不用价格冒充数量；有五档时行为一字不变。
2. **逐 pattern 账本**（第 2、3、4 节）：signal 粒度是
   ``spirit_order.<pattern>``，blocking 与 advisory 分开记，机械不变量成立。
3. **声明一致性**（第 5 节）：pattern 用到什么字段就必须声明什么字段；
   声明 ``depth_l1`` 可用时**不得**被记成缺失。

"真验牙" vs "结构性"
--------------------
标记 **真行为级验牙** 的用例：只用修复前就已存在的符号，把
``src/arad/rules/spirit_order.py`` stash 回旧版后**仍能正常收集**，
失败必然落在 ``assert`` 上（``AssertionError``）—— 这才证明行为真的坏了。

标记 **结构性** 的用例：依赖本文件之外、修复才引入的符号或账本能力，
旧版上会以 ``ImportError``/``TypeError``/``AttributeError``/
``KeyError`` 收集失败。这类红证明的是"符号不存在"，**不能冒充验牙**。

基准算术（``float_cap=50`` 亿、``price=10`` 元 => 流通股数 5 亿股）::

    50 万股 = 5,000 手   机构买单/卖单绝对量门槛
    80 万股 = 8,000 手   有大买盘/大卖盘绝对量门槛
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fakes import make_quote, make_snapshot  # noqa: E402

from arad.capabilities import CAPABILITY_TABLE, RoundObservationSet  # noqa: E402
from arad.rules.base import RuleContext  # noqa: E402
from arad.rules.spirit_order import (  # noqa: E402
    DEFAULTS,
    PATTERNS,
    SpiritOrderRule,
    build,
)
from arad.session import SessionPhase  # noqa: E402

#: 本文件**刻意不在模块顶层** import 修复才引入的符号
#: （``BLOCKING_DEPS`` / ``CAPS_OF`` / ``check_capability_declaration``）。
#:
#: 为什么：这些符号在旧版 ``spirit_order.py`` 里不存在，顶层 import 会让
#: **整个模块收集失败**（``ImportError``）—— 那是**结构性**的红，
#: 证明不了任何行为问题。把它们改成在各自的测试函数**内部** import 之后，
#: 本文件在旧版上仍能正常收集，失败必然落在 ``assert`` 上
#: （``AssertionError``），"真验牙"才成立。
__all__ = ["NOW"]

NOW = datetime(2026, 9, 15, 10, 30, 0)          # 2026-09-15 是周二，交易日
FLOAT_CAP = 50.0                                # 流通市值（亿）=> 5 亿股 @10 元

#: 只开"机构买单/卖单"，把大墙类抬到天上，隔离 L1 单档判定。
WALL_OFF = dict(wall_shares=1e12, wall_float_pct=1e12)
#: 反过来：只开"有大买盘/大卖盘"。
ORDER_OFF = dict(institution_order_shares=1e12, institution_order_amount=1e12,
                 institution_order_float_pct=1e12)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
class FakeState:
    """最小 EngineState 替身：本规则只用到这三个查询方法。"""

    def __init__(self) -> None:
        self.quotes: dict = {}
        self.history: dict = {}
        self.last_price: dict = {}
        self.first_seen: dict = {}
        self.day_open: dict = {}

    def window(self, code: str, seconds: float, now_epoch: float):
        return []

    def price_change(self, code: str, seconds: float, now_epoch: float):
        return None

    def volume_delta(self, code: str, seconds: float, now_epoch: float) -> float:
        return 0.0


def cfg_of(**kw) -> dict:
    merged = dict(DEFAULTS)
    merged["enabled"] = True
    merged.update(kw)
    return merged


def ctx_with(source: str, obs, cfg: dict, *, now: datetime = NOW) -> RuleContext:
    """带**真实能力声明**的上下文：``ctx.provides`` 会按源表回答。"""
    caps = CAPABILITY_TABLE[source]
    return RuleContext(
        state=FakeState(), cfg=dict(cfg), now=now,
        session=SessionPhase.MORNING,
        elapsed_trading_seconds=1800.0, minutes_to_close=120.0,
        capabilities=caps, observation=obs,
    )


def obs_for(source: str, requested: int = 1) -> RoundObservationSet:
    return RoundObservationSet(
        requested=requested,
        capabilities=CAPABILITY_TABLE[source],
        source=source,
    )


def l1_quote(code: str = "600000", *, bid_vol: float = 100.0,
             ask_vol: float = 100.0, bid1: float = 10.00, ask1: float = 10.01,
             outer: float = 50_000.0, inner: float = 50_000.0,
             volume: float = 100_000.0, **kw) -> Quote:  # noqa: F821
    """**Sina 形态**的行情：只有买一/卖一价 + 真实挂单量，五档数组为空。

    ``bid_vol``/``ask_vol`` 单位是**手**（Sina 解析器把 f10/f20 的股数除了 100），
    这与 L5 数组的元素单位一致 —— 所以 L1 退路可以**原样复用**同一套阈值算术。
    """
    base = dict(
        code=code, name="测试股", price=10.00, prev_close=10.00,
        volume_lots=volume, turnover=0.0, float_cap=FLOAT_CAP,
        outer_vol=outer, inner_vol=inner,
        bid_prices=(), bid_vols=(), ask_prices=(), ask_vols=(),
        bid1=bid1, ask1=ask1, bid_vol=bid_vol, ask_vol=ask_vol,
        amount=volume * 100.0 * 10.00,
    )
    base.update(kw)
    return make_quote(**base)


def depth_quote(code: str = "600000", *, bids=(100.0,) * 5, asks=(100.0,) * 5,
                outer: float = 50_000.0, inner: float = 50_000.0,
                volume: float = 100_000.0, **kw):  # noqa: F821
    """**腾讯形态**：完整五档 + L1，两种数据都在。"""
    base = dict(
        name="测试股", price=10.00, prev_close=10.00, volume_lots=volume,
        turnover=2.0, float_cap=FLOAT_CAP, outer_vol=outer, inner_vol=inner,
        bid_prices=tuple(10.00 - 0.01 * i for i in range(5)), bid_vols=tuple(bids),
        ask_prices=tuple(10.01 + 0.01 * i for i in range(5)), ask_vols=tuple(asks),
        bid1=10.00, ask1=10.01, bid_vol=bids[0], ask_vol=asks[0],
        amount=volume * 100.0 * 10.00,
    )
    base.update(kw)
    return make_quote(code=code, **base)


def run(rule: SpiritOrderRule, quotes, ctx: RuleContext, *, prev=None,
        ts: datetime = NOW):
    """求值本轮（``prev`` 非 None 时先喂一轮上一轮快照）。

    **上一轮用独立的账本**：``RoundObservationSet`` 在真实引擎里是**每轮一份**
    （只保留最近一轮），不是跨轮累加。若测试图省事复用同一份，第二轮的
    ``mark_evaluated`` 会被第一轮已占用的终态桶挡掉，账面就反映不出本轮命中 ——
    那是测试用法错，不是规则错。断言对象始终是 ``ctx.observation``。
    """
    if prev is not None:
        src = getattr(ctx.capabilities, "source", "")
        ctx0 = ctx_with(src, obs_for(src), ctx.cfg,
                        now=ts - timedelta(seconds=6))
        rule.evaluate(make_snapshot([prev], ts=ts - timedelta(seconds=6)), ctx0)
    return rule.evaluate(make_snapshot(quotes, ts=ts), ctx)


def patterns_of(alerts) -> set[str]:
    return {a.metrics["pattern"] for a in alerts}


def stats_of(obs: RoundObservationSet, pattern: str):
    return obs.signal_evals.get(f"spirit_order.{pattern}")


def assert_invariants(obs: RoundObservationSet) -> None:
    """每个用例末尾都要检查：账面数字必须自洽。"""
    bad = obs.check_signal_invariants()
    assert bad == [], f"机械不变量被破坏：{bad}"


# ===========================================================================
# 1. L1 数量退路（真行为级验牙）
# ===========================================================================
def test_sina_l1_bid_volume_triggers_institution_buy():
    """**真行为级验牙**：Sina 形态下机构买单必须能用**买一挂单量**命中。

    修复前：``_best_order`` 只遍历空的 ``bid_vols``，调用点传的兜底值
    ``_num(q.bid1, 0.0)`` 是**价格 10.00**。循环一次都不执行 -> 返回 None
    -> ``patterns_of(...)`` 为空 -> 本断言 ``AssertionError``。

    修复后：回退路径用 ``q.bid_vol``（真实手数），6,000 手 = 60 万股 >
    50 万股门槛 -> 命中。
    """
    cfg = cfg_of(**WALL_OFF)
    obs = obs_for("sina")
    rule = build(cfg)
    alerts = run(rule, [l1_quote(bid_vol=6_000.0, ask_vol=1.0)],
                 ctx_with("sina", obs, cfg))
    assert "institution_buy" in patterns_of(alerts), (
        "Sina 只有买一量时，机构买单必须用真实买一挂单量判定")
    assert_invariants(obs)


def test_sina_l1_ask_volume_triggers_institution_sell():
    """**真行为级验牙**：卖一方向同理（修复前同样静默 0 告警）。"""
    cfg = cfg_of(**WALL_OFF)
    obs = obs_for("sina")
    rule = build(cfg)
    alerts = run(rule, [l1_quote(bid_vol=1.0, ask_vol=6_000.0)],
                 ctx_with("sina", obs, cfg))
    assert "institution_sell" in patterns_of(alerts), (
        "Sina 只有卖一量时，机构卖单必须用真实卖一挂单量判定")
    assert_invariants(obs)


def test_l1_fallback_never_invents_quantity_from_price():
    """**结构性**（旧版**同样通过**，是反向回归护栏）：没有数量时必须不报。

    回退实现后实测**通过** —— 旧代码 ``bid_vols`` 为空时循环一次都不执行，
    本来就不会命中。所以它**不是验牙**，价值在于把"绝不用价格冒充数量"
    这条纪律钉死：将来若有人写成"没有数量就用价格凑"，本用例立刻变红。

    为什么用 ``bid1=1e6``：把"价格被当手数"的后果放大到必然命中 ——
    若误当 1e6 手，则 1 亿股 = 10 亿元，绝对量/绝对额双双爆表。
    """
    cfg = cfg_of(**WALL_OFF)
    obs = obs_for("sina")
    rule = build(cfg)
    alerts = run(rule, [l1_quote(bid_vol=0.0, ask_vol=0.0, bid1=1e6, ask1=1e6)],
                 ctx_with("sina", obs, cfg))
    assert "institution_buy" not in patterns_of(alerts)
    assert "institution_sell" not in patterns_of(alerts)
    assert_invariants(obs)


def test_l1_quantity_is_floored_at_real_lot_units():
    """**真行为级验牙**：退路走的是**手**，与五档口径同一套算术。

    ``bid_vol=6,000`` 手、卖价为 10 元：市值 6,000×100×10 = 600 万元，
    既超 50 万股绝对量门槛，也超 100 万元绝对额门槛，命中说明里两项都要在。

    修复前：``patterns_of(...)`` 为空 -> 断言失败。修复后两项命中都成立。
    """
    cfg = cfg_of(**WALL_OFF, institution_order_shares=1e12,
                 institution_order_float_pct=0.0,
                 institution_order_amount=1_000_000.0)
    obs = obs_for("sina")
    rule = build(cfg)
    alerts = run(rule, [l1_quote(bid_vol=6_000.0, ask_vol=1.0)],
                 ctx_with("sina", obs, cfg))
    a = {x.metrics["pattern"]: x for x in alerts}.get("institution_buy")
    assert a is not None, "绝对额口径必须能用 L1 数量命中"
    assert a.metrics["bid_lots"] == 6_000.0
    assert a.metrics["bid_amount"] == 6_000.0 * 100.0 * 10.00
    assert_invariants(obs)


def test_l5_arrays_still_win_when_present():
    """**结构性**（旧版同样通过，是回归护栏）：有五档时 L1 数量被忽略。

    为什么必须钉住：若 L1 覆盖了五档，腾讯（两者都有）的行为就会静默改变。
    这里 ``bid_vol`` 极大（远超门槛）、五档每档仅 100 手（远低于门槛）——
    正确行为是**不报**，因为判定只认五档数组。
    """
    cfg = cfg_of(**WALL_OFF)
    obs = obs_for("tencent")
    rule = build(cfg)
    q = depth_quote(bids=(100.0,) * 5, bid_vol=99_999.0)
    alerts = run(rule, [q], ctx_with("tencent", obs, cfg))
    assert "institution_buy" not in patterns_of(alerts), (
        "有五档数组时必须以五档为准，L1 数量不得参与")


def test_l5_array_best_level_selection_unchanged():
    """**结构性**：数组路径仍取**量最大**的那一档作为代表。

    ``(100, 100, 7_000, 100, 100)`` 手 -> 命中档是第 3 档（7,000 手 = 70 万股），
    价格取该档自己的 ``bid_prices[2]``（而非买一价）。
    """
    cfg = cfg_of(**WALL_OFF)
    obs = obs_for("tencent")
    rule = build(cfg)
    px = tuple(10.00 - 0.01 * i for i in range(5))
    q = depth_quote(bids=(100.0, 100.0, 7_000.0, 100.0, 100.0), bid_prices=px)
    alerts = run(rule, [q], ctx_with("tencent", obs, cfg))
    a = {x.metrics["pattern"]: x for x in alerts}["institution_buy"]
    assert a.metrics["bid_lots"] == 7_000.0
    assert a.metrics["bid_price"] == px[2]


def test_l1_fallback_does_not_leak_into_wall_totals():
    """**结构性**（旧版**同样通过**，是回归护栏）：L1 退路只服务单档判定。

    回退实现后实测**通过** —— 旧 ``_check_orders`` 里 ``wall_ok = q.has_depth
    and check_wall``，而本用例的行情五档数组为空 -> ``has_depth is False``
    -> 大墙类本来就被跳过。所以它**不是验牙**，价值在于：新增的 L1 回退路径
    若被顺手接进 ``_depth_total``，本用例立刻变红。

    语义依据（修复前就已成立、且既有测试
    ``test_big_bid_wall_skipped_without_depth`` 明确钉住）：五档合计口径
    在没有五档时必须跳过 —— 拿买一量冒充五档合计会系统性误报。
    这里买一量 20,000 手 = 200 万股，远超 80 万股大墙门槛：
    若 L1 退路泄漏进大墙合计，就会报出 ``big_bid_wall``。

    注意本用例只开大墙类（``ORDER_OFF``），所以"机构买单"是否命中不影响断言。
    """
    cfg = cfg_of(**ORDER_OFF)
    obs = obs_for("sina")
    rule = build(cfg)
    q = l1_quote(bid_vol=20_000.0, ask_vol=20_000.0)
    assert q.has_depth is False
    alerts = run(rule, [q], ctx_with("sina", obs, cfg))
    got = patterns_of(alerts)
    assert "big_bid_wall" not in got and "big_ask_wall" not in got, (
        "L1 挂单量绝不能冒充五档合计")
    assert_invariants(obs)


# ===========================================================================
# 2. 逐 pattern 账本：Sina 下的 blocking（真行为级验牙）
# ===========================================================================
def test_sina_l5_patterns_recorded_as_blocking():
    """**真行为级验牙**：Sina 缺 ``depth_l5`` 的两个大墙 pattern 必须记 blocking。

    修复前本文件里 ``_mark_unavailable`` 一次都没被调用
    （``git grep -c`` 返回 0），``obs.signal_evals`` 恒为空 ->
    ``stats_of(...)`` 返回 None -> ``assert st is not None`` **AssertionError**。

    实测（回退 ``spirit_order.py`` 后）：本用例红在
    ``AssertionError: big_bid_wall 必须进账本（修复前完全没有账本）`` ——
    是**行为级**失败，符合验牙标准。
    """
    cfg = cfg_of(**ORDER_OFF)
    obs = obs_for("sina")
    rule = build(cfg)
    run(rule, [l1_quote()], ctx_with("sina", obs, cfg))

    for pat in ("big_bid_wall", "big_ask_wall"):
        st = stats_of(obs, pat)
        assert st is not None, f"{pat} 必须进账本（修复前完全没有账本）"
        assert st.blocked_capability == 1, (
            f"{pat} 缺 depth_l5 必须记成 blocking，而不是假装评估过")
        assert st.evaluable == 0
        assert st.evaluable_coverage == 0.0
        assert st.blocked_reasons.get("depth_l5_not_provided") == 1
    assert_invariants(obs)


def test_sina_order_patterns_stay_evaluable_via_l1():
    """**真行为级验牙**：同为 Sina，机构买单/卖单的可评估覆盖率必须是 **1.0**。

    这是整个缺陷的核心对照：同一源、同一轮，**按 pattern 分开看**才看得出
    "有的信号完全失效、有的完全正常"。若账本停在 rule 粒度（或压根没有），
    这个 0.0 / 1.0 的对比根本无法表达 —— 正是审计说的"静默"。
    """
    cfg = cfg_of(**WALL_OFF)
    obs = obs_for("sina")
    rule = build(cfg)
    run(rule, [l1_quote(bid_vol=6_000.0, ask_vol=6_000.0)],
        ctx_with("sina", obs, cfg))

    for pat in ("institution_buy", "institution_sell"):
        st = stats_of(obs, pat)
        assert st is not None, f"{pat} 必须进账本"
        assert st.evaluable == 1, f"{pat} 在 Sina 下靠 depth_l1 完全可评估"
        assert st.blocked_capability == 0
        assert st.evaluable_coverage == 1.0
    assert_invariants(obs)


def test_sina_outer_inner_patterns_recorded_as_blocking():
    """**真行为级验牙**：Sina 不提供内外盘 -> 4 个成交类 pattern 全部 blocking。

    依据：``sina.py`` 的 ``Quote`` 构造**从不写** ``outer_vol``/``inner_vol``
    （只有 ``bid1/ask1/bid_vol/ask_vol``），契约里它们保持默认 0.0。
    把 0.0 当真实业务零值会让"这条信号在这个源下根本没法判"完全隐形。
    """
    cfg = cfg_of(**ORDER_OFF, detect_order=False)
    obs = obs_for("sina")
    rule = build(cfg)
    prev = l1_quote()
    cur = l1_quote(outer=60_000.0, inner=50_000.0, volume=110_000.0)
    run(rule, [cur], ctx_with("sina", obs, cfg), prev=prev)

    for pat in ("big_buy", "big_sell", "institution_eat", "institution_vomit"):
        st = stats_of(obs, pat)
        assert st is not None, f"{pat} 必须进账本"
        assert st.blocked_capability == 1, f"{pat} 在 Sina 下缺 outer_inner"
        assert st.evaluable_coverage == 0.0
        assert st.blocked_reasons.get("outer_inner_not_provided") == 1
    assert_invariants(obs)


def test_capability_absence_is_visible_not_silent_zero_alerts():
    """**真行为级验牙**：缺能力时告警可以是 0 条，但**账本必须有话说**。

    这是缺陷的验收语义：Sina 下 8 个 pattern 里 6 个不可评估，
    用户看到的仍然是"0 条告警"，但 status 里必须能读到**为什么** ——
    修复前 ``unavailable_codes`` / ``signal_evals`` 都是空的，
    "0 条"与"根本没测"无法区分。

    断言刻意分成两半：告警数不强求（那是业务结果），
    但 ``unavailable_codes`` 与逐 pattern 的 ``blocked_reasons`` 必须非空。
    """
    cfg = cfg_of(**ORDER_OFF, detect_order=False)
    obs = obs_for("sina")
    rule = build(cfg)
    run(rule, [l1_quote(outer=60_000.0, inner=50_000.0, volume=110_000.0)],
        ctx_with("sina", obs, cfg), prev=l1_quote())

    assert obs.unavailable_capability >= 1, (
        "Sina 期间至少有 pattern 不可评估，必须记进 unavailable_codes")
    assert "600000" in obs.unavailable_codes
    reasons = {d.reason for d in obs.decisions}
    assert "outer_inner_not_provided" in reasons, (
        "缺失原因必须能在 decisions 里读到，而不是只有一句'没有信号'")
    assert any(d.status == "unavailable_capability" for d in obs.decisions)
    assert_invariants(obs)


def test_advisory_missing_is_not_capability_unavailable():
    """**真行为级验牙**：机构买单缺 ``float_cap`` 时只能记 advisory。

    语义依据（修复前就已成立、且 ``_best_order``/``_wall_hits`` 显式处理
    ``float_shares <= 0``）：比例口径失效后**绝对量口径照常工作**，
    该票依然可评估。把它记进 ``unavailable_codes`` 就是把"少判一项"
    夸大成"整类不可评估"（IT-P1-CAPABILITY-003 的同一类错误）。

    Sina 恰好同时缺 ``float_cap``（``float_cap=0.0``）—— 所以这是真实形态，
    不是人造场景。

    实测（回退后）：红在 ``AssertionError: 机构买单必须进账本`` —— 旧版
    完全没有账本，所以 ``advisory_missing == 1`` 这条语义根本没机会被检验。
    """
    cfg = cfg_of(**WALL_OFF)
    obs = obs_for("sina")
    rule = build(cfg)
    run(rule, [l1_quote(bid_vol=6_000.0, ask_vol=6_000.0)],
        ctx_with("sina", obs, cfg))

    st = stats_of(obs, "institution_buy")
    assert st is not None, "机构买单必须进账本"
    assert st.evaluable == 1, "缺 float_cap 只是少判一项，仍必须可评估"
    assert st.advisory_missing == 1
    assert st.blocked_capability == 0
    advisory = [d for d in obs.decisions if d.status == "advisory_missing"]
    assert advisory and advisory[0].reason == "float_cap_not_provided"
    assert_invariants(obs)


# ===========================================================================
# 3. 腾讯：全能力 -> 零 blocking（真行为级验牙）
# ===========================================================================
def test_tencent_full_capability_no_blocking_anywhere():
    """**真行为级验牙**：腾讯声明 6 项全 True -> 任何 pattern 都不得 blocked。

    为什么必须钉住：账本一旦"宁可多报 blocked"，就会把健康源误报成失效源
    （假警报），比沉默更难排查。腾讯是已知能力最全的源，它是这条的对照极。

    实测（回退后）：红在 ``AssertionError: 腾讯下 big_buy 必须进账本``。
    """
    cfg = cfg_of()
    obs = obs_for("tencent")
    rule = build(cfg)
    prev = depth_quote()
    cur = depth_quote(outer=60_000.0, inner=50_000.0, volume=110_000.0)
    run(rule, [cur], ctx_with("tencent", obs, cfg), prev=prev)

    assert obs.unavailable_capability == 0, (
        "腾讯声明 depth/outer_inner/float_cap 全支持，不该有 unavailable")
    for pat in PATTERNS:
        st = stats_of(obs, pat)
        assert st is not None, f"腾讯下 {pat} 必须进账本"
        assert st.blocked_capability == 0, f"腾讯下 {pat} 不得被记 blocked"
        assert st.evaluable == 1, f"腾讯下 {pat} 判据完整，必须可评估"
        assert st.evaluable_coverage == 1.0
    assert_invariants(obs)


def test_tencent_publishes_across_all_eight_patterns():
    """**真行为级验牙**：腾讯下 8 个 pattern 可以**同时**命中并发出。

    既验证 L1/L5 两条路径并存时不互相干扰，也验证 ``published`` 与
    ``hit_candidates`` 的对应关系（没有截断时两者相等）。
    """
    cfg = cfg_of(max_per_round=0)
    obs = obs_for("tencent")
    rule = build(cfg)
    prev = depth_quote(bids=(100.0,) * 5, asks=(100.0,) * 5)
    cur = depth_quote(
        bids=(6_000.0,) * 5, asks=(9_000.0,) * 5,
        outer=60_000.0, inner=60_000.0, volume=220_000.0,
    )
    alerts = run(rule, [cur], ctx_with("tencent", obs, cfg), prev=prev)
    got = patterns_of(alerts)
    missing = set(PATTERNS) - got
    assert not missing, f"腾讯全能力下这些 pattern 未命中：{sorted(missing)}"
    for pat in PATTERNS:
        st = stats_of(obs, pat)
        assert st is not None
        assert st.hit_candidates == 1, f"{pat} 命中必须记进 hit_candidates"
        assert st.published == 1, f"{pat} 未截断时必须记 published"
    assert_invariants(obs)


# ===========================================================================
# 4. 截断语义（真行为级验牙）与坏账本韧性（结构性）
# ===========================================================================
def test_published_only_counts_alerts_that_survive_truncation():
    """**真行为级验牙**：``max_per_round`` 截掉的命中**不算 published**。

    为什么单列：``published <= hit_candidates`` 是机械不变量，而
    "被截断"必须与"没命中"分开计数，否则运维会把限流读成"市场很安静"。
    这里 ``max_per_round=1`` 但 8 个 pattern 全命中：
    ``hit_candidates == 8``、``published == 1``。

    实测（回退后）：红在 ``AssertionError: 8 个 pattern 全部命中``
    （旧版无账本，``hit_candidates`` 合计为 0）。
    """
    cfg = cfg_of(max_per_round=1)
    obs = obs_for("tencent")
    rule = build(cfg)
    prev = depth_quote(bids=(100.0,) * 5, asks=(100.0,) * 5)
    cur = depth_quote(
        bids=(6_000.0,) * 5, asks=(9_000.0,) * 5,
        outer=60_000.0, inner=60_000.0, volume=220_000.0,
    )
    alerts = run(rule, [cur], ctx_with("tencent", obs, cfg), prev=prev)
    assert len(alerts) == 1
    total_hits = sum(st.hit_candidates for st in obs.signal_evals.values())
    total_pub = sum(st.published for st in obs.signal_evals.values())
    assert total_hits == len(PATTERNS), "8 个 pattern 全部命中"
    assert total_pub == 1, "只有真的发出去的那一条算 published"
    assert_invariants(obs)


def test_rule_survives_observation_none():
    """**结构性**：``ctx.observation is None`` 时规则必须照常出告警。

    可观测性坏掉绝不能把异常抛进信号路径 —— 这是 ``_obs_mark`` 存在的全部
    理由。既有测试从未覆盖这个分支（它们连 observation 都不传）。

    **为什么用腾讯五档而不是 Sina L1 构造**：本用例要验的是"防御式记账"
    这一条，与 L1 退路无关。旧版 ``_scan`` 从不读 ``ctx.observation``，
    所以它在旧版上**同样通过**（真正的结构性护栏）；若用 L1 构造，
    旧版会因为没有 L1 退路而失败 —— 那就变成在验 L1 而不是验韧性了。
    """
    cfg = cfg_of(**WALL_OFF)
    rule = build(cfg)
    ctx = RuleContext(
        state=FakeState(), cfg=dict(cfg), now=NOW,
        session=SessionPhase.MORNING,
        elapsed_trading_seconds=1800.0, minutes_to_close=120.0,
        capabilities=CAPABILITY_TABLE["tencent"], observation=None,
    )
    q = depth_quote(bids=(6_000.0, 1.0, 1.0, 1.0, 1.0))
    alerts = rule.evaluate(make_snapshot([q], ts=NOW), ctx)
    assert "institution_buy" in patterns_of(alerts)


def test_rule_survives_broken_observation_ledger():
    """**结构性**：账本对象**没有**任何记账方法时，规则不得抛异常。

    为什么必须有：真实链路上 ``observation`` 的类型由引擎决定，规则拿到的
    可能是老版本对象、半初始化对象或测试替身。任何一次 ``AttributeError``
    都会把**整轮所有规则的告警**一起炸掉（引擎不隔离单条规则的异常）。

    同上一用例：用腾讯五档构造，旧版上**同样通过** —— 它验的是
    ``_obs_mark`` 的 getattr/except 纪律，不是 L1 退路。
    """

    class Bad:
        """一个只会让记账炸掉的对象（连 unavailable_codes 都没有）。"""

        def __getattr__(self, name):        # noqa: D105
            raise RuntimeError(f"ledger is broken: {name}")

    cfg = cfg_of(**WALL_OFF)
    rule = build(cfg)
    ctx = RuleContext(
        state=FakeState(), cfg=dict(cfg), now=NOW,
        session=SessionPhase.MORNING,
        elapsed_trading_seconds=1800.0, minutes_to_close=120.0,
        capabilities=CAPABILITY_TABLE["tencent"], observation=Bad(),
    )
    q = depth_quote(bids=(6_000.0, 1.0, 1.0, 1.0, 1.0))
    alerts = rule.evaluate(make_snapshot([q], ts=NOW), ctx)
    assert "institution_buy" in patterns_of(alerts), (
        "账本坏掉不能让信号消失")


def test_rule_survives_observation_missing_methods():
    """**结构性**：账本只有部分方法时也不得炸（``getattr`` + callable 检查）。

    与上一个用例互补：上一个是"属性访问就炸"，这个是"属性在但不是方法"
    （真实场景：改版后某方法被重命名/改成属性）。

    同前两个用例：用腾讯五档构造，旧版上**同样通过**（验的是
    ``callable`` 检查纪律，不是 L1 退路）。
    """

    class Partial:
        unavailable_codes: set = set()
        decisions: list = []
        mark_considered = 1              # 存在但**不是**方法

    cfg = cfg_of(**WALL_OFF)
    rule = build(cfg)
    ctx = RuleContext(
        state=FakeState(), cfg=dict(cfg), now=NOW,
        session=SessionPhase.MORNING,
        elapsed_trading_seconds=1800.0, minutes_to_close=120.0,
        capabilities=CAPABILITY_TABLE["tencent"], observation=Partial(),
    )
    q = depth_quote(bids=(6_000.0, 1.0, 1.0, 1.0, 1.0))
    alerts = rule.evaluate(make_snapshot([q], ts=NOW), ctx)
    assert "institution_buy" in patterns_of(alerts)


# ===========================================================================
# 5. 声明一致性（结构性）
# ===========================================================================
def test_capability_declaration_is_consistent():
    """**结构性**：``check_capability_declaration()`` 必须返回空列表。

    审计缺陷的本体就是"依赖声明与真实用法不一致"：大墙类只跑五档合计
    （``_depth_total``）却漏声明 ``depth_l5``，于是缺五档时账本显示
    "评估过、没命中"，而不是"根本没法判"。这条自检把它变成会红的断言。
    """
    from arad.rules.spirit_order import CAPS_OF, check_capability_declaration

    assert check_capability_declaration() == []
    # 扁平视图必须真的覆盖 8 个 pattern（防止自检被写成空转）
    assert set(CAPS_OF) == set(PATTERNS)


def test_depth_l5_declared_for_wall_patterns():
    """**结构性**：大墙类必须声明 ``depth_l5``，且**不得**只声明 ``depth_l1``。

    反面例子（修复前）：``BLOCKING_DEPS`` 里没有 ``depth_l5``，
    而 ``_depth_total`` 只读五档 -> 缺五档时 pattern 仍被记成 evaluable。
    """
    from arad.rules.spirit_order import BLOCKING_DEPS

    for pat in ("big_bid_wall", "big_ask_wall"):
        flat = {f for group in BLOCKING_DEPS.get(pat, ()) for f in group}
        assert "depth_l5" in flat, f"{pat} 的判据读五档，必须声明 depth_l5"
        assert "depth_l1" not in flat, (
            f"{pat} 是五档合计口径，不得声明成只需 depth_l1")


def test_l1_present_never_recorded_as_missing():
    """**结构性**：声明 ``depth_l1`` 可用时，机构买单/卖单**不得**被记 blocked。

    这条直接对应审计的 (c) 项："``depth_l1`` present ⇒ 必须 NOT 被记 missing"。
    做法是让 ``depth_l1`` 与 ``depth_l5`` 同组（内层「或」）：
    任一可用即算满足硬依赖。若有人把内层 tuple 拆成外层（AND 语义），
    本断言与 ``test_sina_order_patterns_stay_evaluable_via_l1`` 会同时变红。
    """
    from arad.rules.spirit_order import BLOCKING_DEPS

    for pat in ("institution_buy", "institution_sell"):
        groups = BLOCKING_DEPS.get(pat, ())
        assert groups, f"{pat} 必须有硬依赖声明"
        assert any(set(g) == {"depth_l1", "depth_l5"} for g in groups), (
            f"{pat} 的 depth_l1/depth_l5 必须同组（任一可用即可判）")


def test_eastmoney_all_patterns_honestly_blocked():
    """**真行为级验牙**：东方财富缺 depth 与 outer_inner -> 8 个 pattern 全 blocked。

    这是"诚实记账"的边界压力测试：一个源可以差到**一个 pattern 都不可评估**。
    此时覆盖率必须是 ``0.0``（有分母、确实全废），而不是 ``None``
    （没有分母 = not measured）—— 两者在 health 上是完全不同的结论。

    实测（回退后）：红在 ``AssertionError: big_buy 必须进账本``。
    """
    cfg = cfg_of()
    obs = obs_for("eastmoney", requested=2)
    rule = build(cfg)
    prev = depth_quote()
    cur = depth_quote(outer=60_000.0, inner=50_000.0, volume=110_000.0)
    run(rule, [cur], ctx_with("eastmoney", obs, cfg), prev=prev)

    for pat in PATTERNS:
        st = stats_of(obs, pat)
        assert st is not None, f"{pat} 必须进账本"
        assert st.blocked_capability == 1, f"{pat} 在东方财富下不可评估"
        assert st.evaluable == 0
        assert st.evaluable_coverage == 0.0, (
            "有分母但全废 -> 0.0；不能返回 None 冒充 not measured")
    assert_invariants(obs)


def test_disabled_patterns_get_no_ledger_entry():
    """**真行为级验牙**（前半段结构性、后半段验牙）：关掉的 pattern 不进账本。

    为什么重要：``institution_order_shares=0`` 等阈值全 0 时该 pattern
    根本没有判据，把它算进分母会把覆盖率系统性做低 —— 那是"没开"，
    不是"测不了"。

    实测（回退后）：前两条 ``is None`` 断言在旧版**通过**（旧版压根没有
    账本，所以"没有条目"平凡成立 —— 这正是"结构性断言在旧版上无意义"的
    典型）；红的是末行 ``wall is not None`` —— 真正有牙的是"大墙类仍启用
    时**必须**有账本条目且记 blocked"这一半。
    """
    cfg = cfg_of(institution_order_shares=0.0, institution_order_amount=0.0,
                 institution_order_float_pct=0.0, detect_trade=False)
    obs = obs_for("sina")
    rule = build(cfg)
    run(rule, [l1_quote()], ctx_with("sina", obs, cfg))

    assert stats_of(obs, "institution_buy") is None, (
        "阈值全 0 -> 该 pattern 没被判据覆盖，不该有账本条目")
    assert stats_of(obs, "institution_sell") is None
    assert stats_of(obs, "big_buy") is None, "detect_trade=False -> 不进账本"
    # 大墙类仍然启用，且 Sina 下应当被记 blocked
    wall = stats_of(obs, "big_bid_wall")
    assert wall is not None and wall.blocked_capability == 1
    assert_invariants(obs)


def test_trade_patterns_not_measured_on_first_round():
    """**结构性**：首轮没有上轮快照 -> 成交类记 not measured，不是 no_hit。

    这是"not measured 与 evaluated_no_hit 必须分开"的核心：
    修复前成交类首轮直接 ``return []``，账本（若存在）会把它记成
    "评估过、没命中" —— 把"根本没测"夸大成结论。
    正确行为是**根本不落终态桶**：``considered == 0``。
    """
    cfg = cfg_of(detect_order=False)
    obs = obs_for("tencent")
    rule = build(cfg)
    run(rule, [depth_quote()], ctx_with("tencent", obs, cfg))

    for pat in ("big_buy", "big_sell", "institution_eat", "institution_vomit"):
        st = stats_of(obs, pat)
        assert st is None or (st.considered == 0 and st.evaluated_no_hit == 0), (
            f"{pat} 首轮没有可判区间，不得记成'评估过没命中'")
    assert_invariants(obs)


def test_trade_patterns_evaluated_on_second_round():
    """**真行为级验牙**：第二轮有上轮 + 内外盘增量 -> 成交类必须真的进 evaluable。

    与上一条互补，确保"not measured"没有被滥用成"永远不记账"。

    实测（回退后）：红在
    ``AssertionError: big_buy 第二轮区间可判，必须进 evaluable``。
    """
    cfg = cfg_of(detect_order=False)
    obs = obs_for("tencent")
    rule = build(cfg)
    prev = depth_quote(outer=50_000.0, inner=50_000.0)
    cur = depth_quote(outer=56_000.0, inner=50_000.0, volume=106_000.0)
    run(rule, [cur], ctx_with("tencent", obs, cfg), prev=prev)

    for pat in ("big_buy", "big_sell", "institution_eat", "institution_vomit"):
        st = stats_of(obs, pat)
        assert st is not None and st.evaluable == 1, (
            f"{pat} 第二轮区间可判，必须进 evaluable")
    assert_invariants(obs)


def test_ledger_invariants_hold_under_repeated_evaluate():
    """**真行为级验牙**：同一账本被 ``evaluate`` 调用两次，账面仍必须自洽。

    真实链路上同轮可能对同一 code 触发多次求值（多份快照/边沿检查）。
    ``blocked``/``evaluated`` 幂等，``advisory_missing`` 靠
    ``mark_considered`` 的返回值把关 —— 这一条把三者的配合钉死。
    若没有幂等保护，重复求值会把 ``considered`` 放大到 2，
    ``evaluable + blocked == considered`` 立刻破裂。

    实测（回退后）：红在 ``assert st is not None``（旧版没有账本条目）。
    """
    cfg = cfg_of()
    obs = obs_for("tencent", requested=1)
    rule = build(cfg)
    prev = depth_quote()
    cur = depth_quote(outer=60_000.0, inner=50_000.0, volume=110_000.0)
    ctx = ctx_with("tencent", obs, cfg)
    ctx0 = ctx_with("tencent", obs, cfg, now=NOW - timedelta(seconds=6))
    rule.evaluate(make_snapshot([prev], ts=NOW - timedelta(seconds=6)), ctx0)
    rule.evaluate(make_snapshot([cur], ts=NOW), ctx)
    rule.evaluate(make_snapshot([cur], ts=NOW), ctx)      # 再来一次

    for pat in PATTERNS:
        st = stats_of(obs, pat)
        assert st is not None
        assert st.considered == 1, f"{pat} 同一轮同一 code 只能算一次"
    assert_invariants(obs)
    assert sum(st.advisory_missing for st in obs.signal_evals.values()) == 0
