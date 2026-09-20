"""IT-P0-002-R2 / IT-P2-OBS-003 / IT-P2-OBS-005 回归。

R2：乱序水位线不得借用 ``history[-1][0]``
------------------------------------------
上游三次独立复现（05:30 / 08:10 / 09:32 JST）并指出关键事实：
现有测试**只覆盖 R2 变动价格**的路径，「平价空洞」无测试。

机制：``history`` 只在价格或累计量**变化**时追加（它服务窗口采样），
而旧代码把 ``history[-1][0]`` 当水位线。于是同价同量的新鲜观测虽推进了
``quotes``/``last_price``，却不推进水位线 → 下一个真正迟到的点被放行。

本文件把"平价"与"变动"两条路径并排锁定 —— 只测变动那条就是
"有测试但没测到"。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fakes import make_quote

from arad.engine import EngineState

T0 = datetime(2026, 9, 15, 10, 30, 0)


def _at(sec: float) -> datetime:
    return T0 + timedelta(seconds=sec)


def _feed(st: EngineState, price: float, lots: float, at: float,
          now: float | None = None):
    """喂一个观测点；``now`` 默认与事件时间一致。"""
    return st.update(
        [make_quote(code="600000", price=price, volume_lots=lots, ts=_at(at))],
        _at(now if now is not None else at),
    )


# ---------------------------------------------------------------------------
# 1. R2 核心：平价新鲜观测必须推进水位线
# ---------------------------------------------------------------------------
def test_flat_fresh_observation_advances_watermark():
    """上游点名的空洞：R2 平价（同价同量）也必须推进水位线。

    修复前：水位线停在 t+0，迟到的 R3(t+30) 被准入，latest 倒退到 9.8，
            out_of_order == 0。
    """
    st = EngineState()
    _feed(st, 10.0, 100.0, 0.0)           # R1 t0
    _feed(st, 10.0, 100.0, 60.0)          # R2 t+60，平价同量

    assert st.accepted_watermark["600000"] == _at(60.0).timestamp(), \
        "平价新鲜观测必须推进水位线（R2 空洞）"

    admitted = _feed(st, 9.8, 110.0, 30.0, now=120.0)   # R3 t+30，迟到

    assert admitted == {}, "迟到的 R3 必须被拒"
    assert st.quotes["600000"].price == 10.0, "latest 不得倒退"
    assert st.last_price["600000"] == 10.0
    assert st.stats.get("t_reject:out_of_order", 0) == 1


def test_flat_observation_does_not_append_history():
    """水位线与 history 解耦：平价观测推进水位线，但**不**追加 history 点。

    history 的"仅变化追加"是窗口采样语义，本轮**故意不改**。
    """
    st = EngineState()
    _feed(st, 10.0, 100.0, 0.0)
    _feed(st, 10.0, 100.0, 60.0)

    assert len(st.history["600000"]) == 1, "平价点不应污染窗口采样"
    assert st.accepted_watermark["600000"] == _at(60.0).timestamp()


def test_moved_fresh_observation_still_advances_watermark():
    """对照：变动价格路径（修复前后都正常）不得被改坏。"""
    st = EngineState()
    _feed(st, 10.0, 100.0, 0.0)
    _feed(st, 10.5, 100.0, 60.0)

    assert st.accepted_watermark["600000"] == _at(60.0).timestamp()
    admitted = _feed(st, 9.8, 110.0, 30.0, now=120.0)
    assert admitted == {}
    assert st.stats.get("t_reject:out_of_order", 0) == 1


def test_flat_then_flat_then_late_is_still_rejected():
    """连续多个平价观测：水位线必须一路推进到最后一次。"""
    st = EngineState()
    _feed(st, 10.0, 100.0, 0.0)
    for t in (30.0, 60.0, 90.0, 120.0):
        _feed(st, 10.0, 100.0, t)
    assert st.accepted_watermark["600000"] == _at(120.0).timestamp()

    admitted = _feed(st, 9.9, 120.0, 100.0, now=180.0)
    assert admitted == {}
    assert st.quotes["600000"].price == 10.0


def test_equal_timestamp_is_admitted_not_rejected():
    """同时间戳（q_ep == wm）是**平价重复**，不是乱序，必须放行。

    边界：判据是 ``q_ep < wm``（严格小于），不是 ``<=``。
    """
    st = EngineState()
    _feed(st, 10.0, 100.0, 0.0)
    admitted = _feed(st, 10.0, 100.0, 0.0)
    assert "600000" in admitted, "同一时间戳的重复观测应当准入"
    assert st.stats.get("t_reject:out_of_order", 0) == 0


def test_out_of_order_never_touches_watermark():
    """被拒的迟到点不得反过来压低水位线。"""
    st = EngineState()
    _feed(st, 10.0, 100.0, 0.0)
    _feed(st, 10.0, 100.0, 60.0)
    wm_before = st.accepted_watermark["600000"]

    _feed(st, 9.0, 50.0, 10.0, now=120.0)          # 迟到，被拒

    assert st.accepted_watermark["600000"] == wm_before


def test_flat_hole_purely_behavioral():
    """**行为级**验收（不依赖任何新属性），与上游的 red 用例同形。

    只断言三件外部可见的事：迟到点不被准入、latest 不倒退、
    out_of_order 计数为 1。这样即使将来水位线实现换了，
    这条测试仍然守得住语义。
    """
    st = EngineState()
    _feed(st, 10.0, 100.0, 0.0)          # R1 t0
    _feed(st, 10.0, 100.0, 60.0)         # R2 t+60 平价同量
    admitted = _feed(st, 9.8, 110.0, 30.0, now=120.0)   # R3 t+30 迟到

    assert sorted(admitted) == [], \
        f"迟到的 R3 必须被拒，实际被准入: {sorted(admitted)}"
    assert st.quotes["600000"].price == 10.0, "latest 不得被迟到价倒退"
    assert st.last_price["600000"] == 10.0, "last_price 不得被迟到价倒退"
    assert st.stats.get("t_reject:out_of_order", 0) == 1, "必须记一次乱序拒绝"


def test_window_does_not_regress_from_flat_hole():
    """影响面：窗口涨跌幅不得把迟到点当窗口末点（上游点名的下游后果）。"""
    st = EngineState()
    _feed(st, 10.0, 100.0, 0.0)
    _feed(st, 10.0, 100.0, 60.0)
    _feed(st, 9.8, 110.0, 30.0, now=120.0)         # 迟到，必须被拒

    pts = st.window("600000", 180.0, _at(120.0).timestamp())
    prices = [p[1] for p in pts]
    assert prices == [10.0], f"窗口不得含迟到点，实际 {prices}"
    assert all(p[0] <= _at(120.0).timestamp() for p in pts)
    # 迟到点已被准入拦住，所以 history 尾部没有 9.8
    assert 9.8 not in prices


def test_price_change_uses_admitted_points_only():
    """窗口涨跌幅只能由**已准入**的点算出，不得被迟到点翻转方向。"""
    st = EngineState()
    _feed(st, 10.0, 100.0, 0.0)
    _feed(st, 10.0, 100.0, 60.0)
    _feed(st, 9.8, 110.0, 30.0, now=120.0)

    # 若迟到点混进窗口，涨跌幅会变成 -2%；这里应算不出（只有 1 个点）
    assert st.price_change("600000", 180.0, _at(120.0).timestamp()) is None


def test_prune_clears_watermark():
    """prune 必须回收水位线，否则重新关注时会被旧水位线误判迟到。"""
    st = EngineState()
    _feed(st, 10.0, 100.0, 0.0)
    assert "600000" in st.accepted_watermark

    st.prune(keep_codes=set())
    assert "600000" not in st.accepted_watermark, "prune 后不得残留水位线"


def test_first_observation_has_no_watermark_penalty():
    """首见标的：水位线缺失时不得拒绝（不得因 None 误判）。"""
    st = EngineState()
    admitted = _feed(st, 10.0, 100.0, 500.0)
    assert "600000" in admitted
    assert st.accepted_watermark["600000"] == _at(500.0).timestamp()


def test_watermark_is_per_code():
    """水位线按标的独立：一只票被判乱序，不得连带影响另一只。

    600001 的水位线被推到 t+60，故 t+10 是乱序；
    600000 的水位线是 t+0，同样喂 t+10 应当正常准入。
    """
    st = EngineState()
    st.update([
        make_quote(code="600000", price=10.0, volume_lots=100.0, ts=_at(0.0)),
        make_quote(code="600001", price=20.0, volume_lots=100.0, ts=_at(60.0)),
    ], _at(60.0))
    assert st.accepted_watermark["600000"] == _at(0.0).timestamp()
    assert st.accepted_watermark["600001"] == _at(60.0).timestamp()

    admitted = st.update([
        make_quote(code="600000", price=10.5, volume_lots=100.0, ts=_at(10.0)),
        make_quote(code="600001", price=20.5, volume_lots=100.0, ts=_at(10.0)),
    ], _at(70.0))

    assert "600001" not in admitted, "600001 的水位线是 t+60，t+10 属乱序"
    assert "600000" in admitted, "600000 不得被 600001 的乱序牵连"
    assert st.stats.get("t_reject:out_of_order", 0) == 1
