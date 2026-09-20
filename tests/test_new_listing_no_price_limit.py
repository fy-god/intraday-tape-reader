"""新上市股（N/C 状态）无涨跌幅限制 —— 回归测试。

真实缺陷（2026-09-21 实盘全市场 5564 只真实行情中发现）
------------------------------------------------------
``601091 C沈鼓`` 上市第 2~5 日（名称冠 ``C``），当天 **+177.74%**，
现价 57.77，而 ``limit_rate_of`` 按主板给 ±10%，算出
``limit_up_price = 22.88``、``limit_down_price = 18.72``。

后果：``limit_board`` 面对一个**根本不存在的限价**判定"价格贴板/炸板"，
产出「打开跌停 +208.60%」这种无意义告警 —— 实测该轮全市场 95 条告警里，
601091 一只就贡献 3 条互相矛盾的信号（同时出现"打开跌停"与"低开高走 +285%"）。

同轮另一只 ``688837 C信诺维``（+34.46%）同样落在板块限价区间之外。
全市场 5564 只里符合 N/C 形态的**恰好这 2 只**，无假阳性 ——
所以判据可以收紧到"首字母 N/C 且第二个字符为中文"。

修复约定（下游必须遵守）
------------------------
无涨跌幅限制时 ``limit_up_price``/``limit_down_price`` 返回 **0.0**，
含义是"没有这个约束"，不是"限价就是 0 元"。因此判断"是否贴板"必须走
``is_at_limit_up``/``is_at_limit_down``，**不能**直接写
``q.price >= q.limit_up_price`` —— 那在无约束股上恒真。

---
验牙说明（为什么本文件分成 A / B 两组）
--------------------------------------
回退 ``models.py`` 后，引用**新符号**的测试会以 ``AttributeError``/``ImportError``
失败 —— 那是**结构性**失败，只证明"符号不存在"，**证明不了任何行为**。
所以 A 组刻意只用修复前**已存在**的 API（``limit_up_price``、
``limit_down_price``、``limit_rate_of``、``board_of``）：回退后它们以
``AssertionError`` 失败，那才是真正的行为级验牙。
B 组专门钉新 API 的契约，如实标注为结构性。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from arad.models import Quote, Snapshot, board_of, limit_rate_of


def _q(code: str, name: str, price: float = 10.0, prev: float = 10.0, **kw) -> Quote:
    return Quote(
        code=code, name=name, board=board_of(code, name),
        price=price, prev_close=prev, open=prev, high=max(price, prev),
        low=min(price, prev), volume_lots=10000.0, amount=1e8, **kw,
    )


# ==========================================================================
# A 组 —— 行为验牙：只用修复前已存在的 API
# ==========================================================================
def test_c_new_listing_limit_up_is_unconstrained():
    """C 状态：涨停价必须是"无约束"。

    修复前 ``limit_up_price`` 返回 22.88（= 20.80 * 1.1）。
    断言只碰 ``limit_up_price`` —— 这是修复前就有的属性，所以回退时
    失败类型是 AssertionError（行为级），不是 AttributeError。
    """
    q = _q("601091", "C沈鼓", price=57.77, prev=20.80)
    assert q.limit_up_price == 0.0, (
        f"新股无涨跌幅限制，涨停价应为 0.0（无约束）；实际 {q.limit_up_price}")


def test_c_new_listing_limit_down_is_unconstrained():
    """C 状态：跌停价同样无约束。修复前返回 18.72（= 20.80 * 0.9）。

    正是这个假跌停价让现价 57.77 显得"远在跌停之上"，被误判成「打开跌停」。
    """
    q = _q("601091", "C沈鼓", price=57.77, prev=20.80)
    assert q.limit_down_price == 0.0, (
        f"新股无涨跌幅限制，跌停价应为 0.0；实际 {q.limit_down_price}")


def test_star_c_new_listing_limits_are_unconstrained():
    """科创板 C 股：688837 C信诺维 +34.46%，修复前 limit_up=49.62（20% 口径）。"""
    q = _q("688837", "C信诺维", price=55.60, prev=41.35)
    assert q.limit_up_price == 0.0
    assert q.limit_down_price == 0.0


def test_n_first_day_limits_are_unconstrained():
    """N 状态（上市首日）同样无涨跌幅限制。"""
    q = _q("301999", "N测试", price=88.88, prev=20.00)
    assert q.limit_up_price == 0.0
    assert q.limit_down_price == 0.0


@pytest.mark.parametrize("code,name,price,prev,exp_up,exp_dn", [
    ("600000", "浦发银行", 10.00, 10.00, 11.00, 9.00),      # 主板 10%
    ("300750", "宁德时代", 10.00, 10.00, 12.00, 8.00),      # 创业板 20%
    ("688111", "金山办公", 10.00, 10.00, 12.00, 8.00),      # 科创板 20%
    ("600519", "ST测试", 10.00, 10.00, 10.50, 9.50),        # 主板 ST 5%
])
def test_normal_stock_limits_unchanged(code, name, price, prev, exp_up, exp_dn):
    """正常股限价行为必须**逐字不变** —— 防止修复误伤。

    断言顺序很重要：先碰既有属性，回退时才会以 AssertionError 失败。
    """
    q = _q(code, name, price=price, prev=prev)
    assert q.limit_up_price == pytest.approx(exp_up)
    assert q.limit_down_price == pytest.approx(exp_dn)


def test_explicit_limit_from_source_wins_over_new_listing_heuristic():
    """数据源若给了真实涨跌停价，必须以它为准（新股也可能有明确价）。"""
    q = _q("601091", "C沈鼓", price=57.77, prev=20.80,
           limit_up=80.00, limit_down=12.00)
    assert q.limit_up_price == pytest.approx(80.00)
    assert q.limit_down_price == pytest.approx(12.00)


@pytest.mark.parametrize("name,expect_unconstrained", [
    # --- 真新股形态：必须无约束 ---
    ("N沈鼓", True),
    ("C沈鼓", True),
    ("C信诺维", True),
    ("N金山办公", True),
    # --- 以下**绝不能**被当成新股（否则正常股会被摘掉涨跌停约束）---
    ("TCL科技", False),        # 首字母非 N/C
    ("NVIDIA概念", False),     # N 后面是 ASCII（英文名）
    ("CPU概念", False),        # C 后面是 ASCII
    ("浦发银行", False),        # 普通名
    ("ST沈鼓", False),         # ST 不带 N/C 前缀
    ("*ST沈鼓", False),        # 退市风险警示
    ("CTest", False),          # C + ASCII
])
def test_limit_price_boundary_by_name(name, expect_unconstrained):
    """判据必须"宁缺毋滥"，且**通过既有属性**表达边界。

    漏判新股只是少过滤一次；误判会把正常股的涨跌停约束摘掉，后果更严重。
    这里只断言 ``limit_up_price``，所以回退时是真 AssertionError。

    期望值由 ``limit_rate_of`` 现算，而不是写死 22.88 —— 因为 ST 股是 5%
    （``limit_up_price`` 21.84），且 ``"CTest".upper()`` 含子串 ``"ST"``
    会命中 ST 分支（``limit_rate_of`` 的既有行为，本次不涉及）。
    写死数字会让测试钉住无关细节。
    """
    q = _q("600000", name, price=20.80, prev=20.80)
    if expect_unconstrained:
        assert q.limit_up_price == 0.0, f"{name!r} 应视为无涨跌幅限制"
    else:
        exp = round(20.80 * (1 + limit_rate_of("600000", name)), 2)
        assert q.limit_up_price == pytest.approx(exp), (
            f"{name!r} 是正常股，必须保留按板块/ST 算出的涨停约束 {exp}")


def test_limit_rate_of_keeps_pure_board_semantics():
    """``limit_rate_of`` 保持纯板块/ST 口径 —— 新股特判**不能**塞进来。

    原因：它返回的是**比例**，而"无限制"不是一个比例。硬塞 0.0 或 1.0
    都会被下游当成真阈值。无约束这件事由 ``limit_up_price == 0.0`` 表达。
    """
    assert limit_rate_of("601091", "C沈鼓") == pytest.approx(0.10)
    assert limit_rate_of("688837", "C信诺维") == pytest.approx(0.20)
    assert limit_rate_of("600519", "ST测试") == pytest.approx(0.05)


def test_limit_board_silent_on_new_listing():
    """端到端复现：C沈鼓 +177.74% 不得产出任何涨跌停告警。

    修复前该股产生「打开跌停 +208.60%」。反向断言正常股仍要报，
    证明不是把整条规则关掉。
    """
    from arad.capabilities import RoundObservationSet, capabilities_for
    from arad.rules.base import RuleContext
    from arad.rules.limit_board import build as build_lb
    from arad.session import SessionPhase

    rule = build_lb({"enabled": True})
    base = datetime(2026, 9, 21, 10, 0, 0)
    codes = ["601091", "688837", "600000"]
    out = []
    for i in range(3):
        ts = base + timedelta(seconds=5 * i)
        qs = {
            # 无约束新股：即便给出巨额"封单"也不得触发（它们没有涨停这回事）
            "601091": _q("601091", "C沈鼓", price=57.77, prev=20.80, bid_vol=999999.0),
            "688837": _q("688837", "C信诺维", price=55.60, prev=41.35, bid_vol=999999.0),
            # 正常股：真贴涨停 + 封单 5500 万 >> 门槛 200 万，必须报
            "600000": _q("600000", "浦发银行", price=11.00, prev=10.00, bid_vol=50000.0),
        }
        for q in qs.values():
            q.ts = ts
        snap = Snapshot(ts=ts, seq=i + 1, quotes=qs)
        ctx = RuleContext(
            state=type("S", (), {"history": {}, "universe": set(codes)})(),
            cfg={"enabled": True}, now=ts, session=SessionPhase.MORNING,
            elapsed_trading_seconds=1800.0, minutes_to_close=120.0,
            capabilities=capabilities_for("tencent"),
            observation=RoundObservationSet(
                requested=len(codes), source="tencent",
                capabilities=capabilities_for("tencent")),
        )
        out.extend(rule.evaluate(snap, ctx) or [])

    bad = [a for a in out if a.code in ("601091", "688837")]
    assert bad == [], (
        "无涨跌幅限制的新股不得触发涨跌停告警；实际: "
        + ", ".join(f"{a.code} {a.title}" for a in bad))
    good = [a for a in out if a.code == "600000"]
    assert good, "正常股贴涨停必须仍然告警 —— 否则修复等于关闭功能"


# ==========================================================================
# B 组 —— 新 API 契约（结构性：回退时为 AttributeError/ImportError）
# 这些测试**不构成本次修复的行为证据**，只钉住新接口的语义。
# ==========================================================================
def test_b_is_new_listing_predicate():
    from arad.models import is_new_listing
    assert is_new_listing("C沈鼓") is True
    assert is_new_listing("N沈鼓") is True
    assert is_new_listing("TCL科技") is False
    assert is_new_listing("C") is False
    assert is_new_listing("") is False
    assert is_new_listing("CTest") is False


def test_b_has_price_limit_semantics():
    assert _q("601091", "C沈鼓", price=57.77, prev=20.80).has_price_limit is False
    assert _q("600000", "浦发银行").has_price_limit is True


def test_b_is_new_listing_property_matches_function():
    from arad.models import is_new_listing
    q = _q("601091", "C沈鼓", price=57.77, prev=20.80)
    assert q.is_new_listing is is_new_listing("C沈鼓")


def test_b_is_at_limit_helpers_false_when_unconstrained():
    """贴板判定在无约束股上必须恒为 False。

    这是**最容易写错**的地方：``q.price >= q.limit_up_price`` 在
    ``limit_up_price == 0.0`` 时恒真，会把每只新股都报成"涨停"。
    """
    q = _q("601091", "C沈鼓", price=57.77, prev=20.80)
    assert q.is_at_limit_up() is False
    assert q.is_at_limit_down() is False


def test_b_is_at_limit_helpers_true_for_normal_stock():
    up = _q("600000", "浦发银行", price=11.00, prev=10.00)
    dn = _q("600000", "浦发银行", price=9.00, prev=10.00)
    mid = _q("600000", "浦发银行", price=10.30, prev=10.00)
    assert up.is_at_limit_up() is True and up.is_at_limit_down() is False
    assert dn.is_at_limit_down() is True and dn.is_at_limit_up() is False
    assert mid.is_at_limit_up() is False and mid.is_at_limit_down() is False
