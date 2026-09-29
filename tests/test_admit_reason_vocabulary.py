"""`_admit_time()` 拒绝原因的**闭集**词表回归（bug 类 (d)「默认分支是假绿出口」）。

缺陷
----
修前 ``EngineState._admit_time(...)`` 返回的第三个元素是**自由字符串**
``why``。调用方（``update_detailed``）这样用：

    ts_ok, q_ep, why = self._admit_time(...)
    self.stats[f"t_reject:{why}"] = self.stats.get(f"t_reject:{why}", 0) + 1
    if why == "future":
        rejected_future += 1
    elif why == "out_of_order":
        rejected_out_of_order += 1

把 ``"future"`` 拼成 ``"furure"``、或写成 ``"out-of-order"``，**不报任何错**：
它会静默新建一个 ``t_reject:furure`` 桶，同时 ``if/elif`` 两条路由全部落空，
route-local 归属计数保持 0。观测面上多出一条谁都不认识的曲线，而本该被归类的
拒绝变成"没发生"。这就是本仓库 bug 类 (d)。

修复
----
``AdmitReason(str, Enum)`` 把合法原因**声明在一处**（``engine.py``）。
``AdmitReason("furure")`` 在**运行时**抛 ``ValueError`` —— 非法值构造不出来。

本文件守住四件事
----------------
1. **闭集**：enum 成员集合 == 实现真的会返回的原因集合（不是靠读源码猜，
   而是真的构造 quote 走 ``_admit_time`` / ``update_detailed``）。
2. **键的字节级同一性**：``t_reject:future`` / ``t_reject:out_of_order`` /
   ``t_reject:stale`` 与修前**逐字节相同**（dashboards 按字符串匹配）。
3. **fail-closed**：拼错的原因**不能**静默产生新桶。
4. **阳性对照**：真的 future / 真的 out_of_order 仍必须被正确分类。

设计说明（为什么本文件**不**用子串匹配源码）
------------------------------------------
本仓库已经因为"门禁匹配的是我自己的注释"翻过车（见
``tests/test_engine_route_ledger_wiring.py`` 的 docstring：一条本该转红的门禁
因为注释里含 ``update_detailed`` 而保持绿色）。所以这里的每条断言都建立在
**enum 成员**与**实际产生的 stats 键**之上，绝不建立在"源码文本里出现过某个词"。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from fakes import make_quote

from arad.engine import (
    ADMIT_REJECT_STATS_KEYS,
    FUTURE_TOLERANCE_SECONDS,
    STALE_TOLERANCE_SECONDS,
    AdmitReason,
    EngineState,
)

NOW = datetime(2026, 9, 15, 10, 30, 0)


# ===========================================================================
# 1. 闭集：声明的词表 == 实现能返回的原因集合
# ===========================================================================

#: **独立**于实现写下的期望（不是从 enum 反推，否则就是恒真式）。
EXPECTED_REJECT_REASONS = {"future", "out_of_order", "stale"}


def test_enum_members_are_exactly_the_reject_reasons_plus_admit():
    """enum 成员集合必须**恰好**是 {拒绝原因} ∪ {NO_REJECT}。

    与实现的返回路径对照：``_admit_time`` 有 4 个出口 ——
    ``NO_REJECT``（无 ts / ts 解析失败 / 轻微超前夹逼 / 准时）与三个拒绝。
    """
    members = {m.value for m in AdmitReason}
    assert members == EXPECTED_REJECT_REASONS | {AdmitReason.NO_REJECT.value}, (
        f"AdmitReason 成员与实现宣称的出口不一致：{sorted(members)}。"
        f"新增/删除原因时**必须**同步本断言 —— 它就是这个词表的护栏。")


def test_every_reject_reason_is_a_str_subclass():
    """必须继承 ``str``：既有 ``== "future"`` 比较与 JSON 序列化才不变。"""
    for m in AdmitReason:
        assert isinstance(m, str), f"{m!r} 不是 str 子类"
        assert m == m.value, f"{m!r} 不等于它自己的 value"


def test_implementation_can_only_return_declared_reasons():
    """**走真实代码路径**确认：返回值只能是 enum 成员。

    这不是读源码猜，而是真的构造各类 quote 调 ``_admit_time``，断言返回的
    第三个元素在闭集内。

    注意 ``OUT_OF_ORDER`` **不**由 ``_admit_time`` 返回 —— 乱序是在
    ``update_detailed`` 里用水位线单独判定的（见下一条测试）。所以这里
    期望的集合是 ``set(AdmitReason) - {OUT_OF_ORDER}``，而不是全集。
    这个切分本身就是要钉住的事实：**拒绝原因的产出点有两个**，
    词表却是同一个。
    """
    st = EngineState()
    ep = NOW.timestamp()
    cases = [
        # (描述, quote, first_seen, freshness_allowed)
        ("无 ts", make_quote(code="600000", price=10.0), False, False),
        ("准时", make_quote(code="600000", price=10.0, ts=NOW), False, False),
        ("轻微超前", make_quote(code="600000", price=10.0,
                             ts=NOW + timedelta(seconds=5)), False, False),
        ("远期未来", make_quote(code="600000", price=10.0,
                             ts=NOW + timedelta(
                                 seconds=FUTURE_TOLERANCE_SECONDS + 60)),
         False, False),
        ("陈旧（受信任源首见）", make_quote(code="600000", price=10.0,
                                    ts=NOW - timedelta(
                                        seconds=STALE_TOLERANCE_SECONDS + 60)),
         True, True),
        ("陈旧（合同不可信）", make_quote(code="600000", price=10.0,
                                   ts=NOW - timedelta(
                                       seconds=STALE_TOLERANCE_SECONDS + 60)),
         True, False),
    ]
    seen: set[AdmitReason] = set()
    for desc, q, first_seen, fresh_ok in cases:
        _, _, why = st._admit_time(q, NOW, ep, first_seen=first_seen,
                                   freshness_allowed=fresh_ok)
        assert isinstance(why, AdmitReason), (
            f"[{desc}] _admit_time 返回了非 AdmitReason 的 "
            f"{why!r}（类型 {type(why).__name__}）—— 词表被绕过了")
        assert why in AdmitReason, f"[{desc}] {why!r} 不在闭集内"
        seen.add(why)
    expected = set(AdmitReason) - {AdmitReason.OUT_OF_ORDER}
    assert seen == expected, (
        f"_admit_time 只观察到 {sorted(m.value for m in seen)}，"
        f"期望 {sorted(m.value for m in expected)} —— "
        f"有条出口没被测到，或词表里有死成员")


def test_out_of_order_is_produced_by_update_detailed_not_admit_time():
    """``OUT_OF_ORDER`` 的产出点是 ``update_detailed`` 的水位线判定。

    这是词表与产出点的**对齐证据**：词表里的每个拒绝原因都必须真的有人产生，
    且每个产生点产出的原因都必须在词表里。乱序若也从 ``_admit_time`` 冒出来，
    说明"同一语义两个产出点"；若这里测不出来，说明词表里有死成员。
    """
    st = EngineState()
    st.update([make_quote(code="600010", price=10.0, ts=NOW)], NOW)
    later = NOW + timedelta(seconds=60)
    st.update([make_quote(code="600010", price=10.5, ts=later)], later)
    res = st.update_detailed(
        [make_quote(code="600010", price=8.0,
                    ts=NOW + timedelta(seconds=10))], later)
    assert res.rejected_out_of_order == 1
    assert st.stats.get(AdmitReason.OUT_OF_ORDER.stats_key) == 1, (
        "乱序的 stats 键必须由 AdmitReason.OUT_OF_ORDER.stats_key 产生，"
        "而不是别处手写的字符串")


# ===========================================================================
# 2. 键的**字节级**同一性
# ===========================================================================

def test_declared_stats_keys_are_the_expected_exact_strings():
    """拒绝键的闭集必须**恰好**是这三个**精确字符串**。"""
    assert ADMIT_REJECT_STATS_KEYS == {
        "t_reject:future", "t_reject:out_of_order", "t_reject:stale"}, (
        f"键的闭集被改动了：{sorted(ADMIT_REJECT_STATS_KEYS)}。"
        f"这些字符串是 dashboards / tools/live_session.py 的匹配目标。")


@pytest.mark.parametrize("reason,expected", [
    (AdmitReason.FUTURE, "t_reject:future"),
    (AdmitReason.OUT_OF_ORDER, "t_reject:out_of_order"),
    (AdmitReason.STALE, "t_reject:stale"),
])
def test_stats_key_property_is_byte_identical(reason, expected):
    """``stats_key`` 必须与修前的 f-string 结果逐字节相同。"""
    assert reason.stats_key == expected
    # 字节级（防止某个 Unicode 同形字符混进来）
    assert reason.stats_key.encode("utf-8") == expected.encode("utf-8")


def test_fstring_and_str_agree_with_value():
    """``str()`` / f-string / ``+`` 三种写法必须都给出同一个字符串。

    这是最容易翻车的地方：``Enum.__str__`` 默认返回 ``"AdmitReason.FUTURE"``，
    而 ``str`` mixin **不**覆盖它 —— 于是 ``f"t_reject:{why}"`` 会静默变成
    ``t_reject:AdmitReason.FUTURE``，键被改掉。本类显式绑定
    ``__str__ = str.__str__`` / ``__format__ = str.__format__`` 修掉它。
    """
    for m in AdmitReason:
        assert str(m) == m.value
        assert f"{m}" == m.value
        assert f"t_reject:{m}" == f"t_reject:{m.value}"
        assert ("t_reject:" + m) == f"t_reject:{m.value}"
        assert "{!s}".format(m) == m.value


def test_no_reject_stats_key_is_not_emitted():
    """``NO_REJECT`` **不得**出现在拒绝键闭集里（否则会凭空造 ``t_reject:``）。"""
    assert AdmitReason.NO_REJECT.stats_key not in ADMIT_REJECT_STATS_KEYS
    assert AdmitReason.NO_REJECT.value == ""


# ===========================================================================
# 3. fail-closed：拼错不能静默产生新桶（**本文件最重要的一条**）
# ===========================================================================

@pytest.mark.parametrize("typo", [
    "furure",            # 常见手误
    "out-of-order",      # 连字符 vs 下划线
    "outoforder",
    "Future",            # 大小写
    "FUTURE",
    "stale_reject",
    " future",           # 前导空格
    "future ",           # 尾空格
    "no_reject",         # 名字而不是值
    "NO_REJECT",
])
def test_typo_cannot_construct_a_reason(typo):
    """非法原因**构造即失败** —— 不可能静默新建 ``t_reject:<typo>`` 桶。"""
    with pytest.raises(ValueError):
        AdmitReason(typo)


def test_empty_string_maps_to_no_reject_not_a_typo():
    """``""`` 是 ``NO_REJECT`` 的**合法值**，不是拼错。

    修前 ``_admit_time`` 用空串表示"接纳、无拒绝"。保留这个值让
    ``AdmitReason("")`` 幂等等价于 ``NO_REJECT`` —— 既有的"接纳"语义
    可以通过值直接还原，不需要一个额外的哨兵字符串。
    """
    assert AdmitReason("") is AdmitReason.NO_REJECT
    assert AdmitReason.NO_REJECT.value == ""
    # 且它**不是**一个拒绝键（否则会凭空造出 `t_reject:` 这个桶）
    assert AdmitReason.NO_REJECT.stats_key not in ADMIT_REJECT_STATS_KEYS


def test_typo_cannot_produce_a_new_stats_bucket():
    """**fail-closed 的端到端证据**：拼错的原因拿不到 stats 键。

    修前的写法 ``f"t_reject:{why}"`` 对一个拼错的 ``why`` 会**成功**返回
    ``"t_reject:furure"`` 并被写进 ``stats``。现在这个字符串**无法** 变成
    一个 ``AdmitReason``，因此也拿不到 ``stats_key`` —— 静默新桶在构造层
    就被堵死。
    """
    for typo in ("furure", "out-of-order", "Future"):
        with pytest.raises(ValueError):
            AdmitReason(typo)
        # 并且它确实**不是**一个已声明的键
        assert f"t_reject:{typo}" not in ADMIT_REJECT_STATS_KEYS


def test_stats_dict_never_contains_an_undeclared_reject_key():
    """跑真实的准入路径，断言 ``stats`` 里**没有**闭集之外的 ``t_reject:*``。

    这是"闭集外的新桶"的**生产级**守卫：未来有人往 ``_admit_time`` 里加一个
    手写字符串出口，只要它绕过了 enum，这里就会红。
    """
    st = EngineState()
    st.begin_source_epoch("stocks", "tencent#0", source_name="tencent")
    # 未来
    st.update([make_quote(code="600000", price=99.0,
                          ts=NOW + timedelta(
                              seconds=FUTURE_TOLERANCE_SECONDS + 60))], NOW)
    # 乱序
    st.update([make_quote(code="600001", price=10.0, ts=NOW)], NOW)
    later = NOW + timedelta(seconds=60)
    st.update([make_quote(code="600001", price=10.5, ts=later)], later)
    st.update([make_quote(code="600001", price=8.0,
                          ts=NOW + timedelta(seconds=10))], later)
    # 正常
    st.update([make_quote(code="600002", price=10.0, ts=NOW)], NOW)

    actual = {k for k in st.stats if k.startswith("t_reject:")}
    assert actual <= ADMIT_REJECT_STATS_KEYS, (
        f"出现了闭集之外的拒绝桶：{sorted(actual - ADMIT_REJECT_STATS_KEYS)}。"
        f"任意的自由字符串又回到了 stats 键里 —— bug 类 (d) 复发。")
    assert actual == {"t_reject:future", "t_reject:out_of_order"}, (
        f"本用例只应产生 future + out_of_order，实测 {sorted(actual)}")


# ===========================================================================
# 4. 阳性对照：真的 future / 真的 out_of_order 必须被正确分类
# ===========================================================================

def test_positive_control_future_is_classified_as_future():
    """阳性对照：真的未来包必须记 ``t_reject:future`` 且归属 future。"""
    st = EngineState()
    q = make_quote(code="600003", price=99.0,
                   ts=NOW + timedelta(seconds=FUTURE_TOLERANCE_SECONDS + 60))
    res = st.update_detailed([q], NOW)
    assert "600003" not in res.admitted
    assert st.stats.get("t_reject:future") == 1, \
        f"应记一次 future，实测 stats={ {k: v for k, v in st.stats.items() if 't_reject' in k} }"
    assert res.rejected_future == 1, "route-local 归属必须记到 future"
    assert res.rejected_out_of_order == 0
    assert res.rejected_stale == 0
    assert st.stats.get("t_reject:out_of_order", 0) == 0


def test_positive_control_out_of_order_is_classified_as_out_of_order():
    """阳性对照：真的乱序包必须记 ``t_reject:out_of_order`` 且归属 ooo。"""
    st = EngineState()
    st.update([make_quote(code="600004", price=10.0, ts=NOW)], NOW)
    later = NOW + timedelta(seconds=60)
    st.update([make_quote(code="600004", price=10.5, ts=later)], later)
    res = st.update_detailed(
        [make_quote(code="600004", price=8.0,
                    ts=NOW + timedelta(seconds=10))], later)
    assert st.stats.get("t_reject:out_of_order") == 1, \
        f"应记一次 ooo，实测 stats={ {k: v for k, v in st.stats.items() if 't_reject' in k} }"
    assert res.rejected_out_of_order == 1, "route-local 归属必须记到 ooo"
    assert res.rejected_future == 0
    assert res.rejected_stale == 0
    assert st.stats.get("t_reject:future", 0) == 0


def test_positive_control_stale_is_classified_as_stale_not_silently_dropped():
    """阳性对照：受信任源的陈旧首包必须记 ``t_reject:stale``。

    修前这条路径写全局 ``t_reject:stale``，但 ``why == "stale"`` 同时落空
    future/ooo 两支 —— route-local 归属**静默丢失**。现在它有自己的桶。
    """
    from arad import engine as eng_mod

    saved = dict(eng_mod.TIME_POLICY.get("tencent", {}))
    eng_mod.TIME_POLICY["tencent"] = {"freshness_allowed": True,
                                      "strict_ordering_allowed": True}
    try:
        st = EngineState()
        st.begin_source_epoch("stocks", "tencent#0", source_name="tencent")
        res = st.update_detailed(
            [make_quote(code="600005", price=99.0,
                        ts=NOW - timedelta(
                            seconds=STALE_TOLERANCE_SECONDS + 60))],
            NOW, route="stocks")
        assert "600005" not in res.admitted, "受信任源的陈旧首包应被拒"
        assert st.stats.get("t_reject:stale") == 1
        assert res.rejected_stale == 1, (
            "陈旧拒绝必须有自己的 route-local 桶 —— "
            "修前它同时落空 future/ooo，归属静默丢失")
        assert res.rejected_future == 0
        assert res.rejected_out_of_order == 0
    finally:
        eng_mod.TIME_POLICY["tencent"] = saved


def test_positive_control_fresh_packet_is_admitted_with_no_reject():
    """阴性对照：正常包必须被接纳，且**不**产生任何拒绝键。"""
    st = EngineState()
    res = st.update_detailed([make_quote(code="600006", price=10.0, ts=NOW)],
                             NOW)
    assert "600006" in res.admitted
    assert res.rejected_future == 0
    assert res.rejected_out_of_order == 0
    assert res.rejected_stale == 0
    assert not [k for k in st.stats if k.startswith("t_reject:")], \
        f"干净路径不该产生拒绝桶，实测 {sorted(st.stats)}"


# ===========================================================================
# 5. 消费者接线自查（bug 类 c「数据算了、判决层零读者」）
# ===========================================================================

def test_stale_hard_rejected_is_declared_on_the_observation_ledger():
    """``rejected_stale`` 必须真的**接**到 RoundObservationSet，否则零读者。

    本仓库反复出现"字段算了但没有消费者"。这里的守卫是：新桶必须出现在
    账本 dataclass 上，并且能经由 ``as_dict()`` 导出 —— 而不只是活在
    ``EngineState`` 内部。
    """
    from arad.capabilities import RoundObservationSet

    obs = RoundObservationSet()
    assert hasattr(obs, "stale_hard_rejected"), \
        "rejected_stale 没有接到 RoundObservationSet —— 零读者（bug 类 c）"
    d = obs.as_dict()
    assert "stale_hard_rejected" in d, "没有从 as_dict() 导出，下游读不到"
    assert d["stale_hard_rejected"] == 0
    # 两个语义不同的字段都必须在账本里、且**各是各的**（不是同一个存储格）。
    # 修前 stale_rejected 被 alias 成 future_rejected，两条互斥曲线同值 ——
    # 这里要求"陈旧硬拒"与"陈旧诊断"是**独立**的字段。
    assert getattr(type(obs), "stale_hard_rejected") is not \
        getattr(type(obs), "provider_stale_diagnosed"), \
        "stale_hard_rejected 不得 alias 到 provider_stale_diagnosed"
    # 用一个**非零**诊断值证明两者可分离（不是靠默认 0 恰好相等）。
    obs2 = RoundObservationSet(provider_stale_diagnosed=7,
                               stale_hard_rejected=0)
    d2 = obs2.as_dict()
    assert d2["provider_stale_diagnosed"] == 7
    assert d2["stale_hard_rejected"] == 0, \
        "陈旧诊断非零时硬拒计数不得跟着一起动（两条曲线必须可分离）"
    # 已弃用的 stale_rejected **属性**（Python property，不是 dict 键）必须仍
    # 指向**诊断**数，而不是新的硬拒数 —— 否则旧读者会被静默改语义。
    assert obs2.stale_rejected == obs2.provider_stale_diagnosed == 7, \
        "已弃用的 stale_rejected 属性必须仍指向诊断数"
