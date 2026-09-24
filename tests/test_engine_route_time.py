"""WP01 —— `StateUpdateResult` / route-local admission facts。

任务书 `2026-09-24_12-04-56_JST_AGENT_TASK.md` §WP01（第一优先）。

两条 RED：

* **RED 1（`IT-P2-TIME-DIAGNOSTIC-ROUTE-COLLISION-010`）**：
  硬准入水位线已按 `(route, code)` 分账，但时间**诊断**映射
  （`provider_ts_raw` / `effective_event_time` / `received_at` /
  `time_age_seconds`）仍只按**裸 code** 存。Sina 在 index route 上会把
  `sh000001` 解析成裸 `000001`，随后 stock `000001` 的更新**覆盖** index
  的 age/timestamp —— 两个不同标的抢一个 key。

* **RED 2（`IT-P2-TIME-REJECT-ROUTE-ATTRIBUTION-011`）**：
  `update()` 只返回 admitted，future/ooo 只写**全局** counters。
  `poll_once` 在两条 route 更新前取一次基线、之后算一个 aggregate delta：
  `index future + stock ooo` 与 `index ooo + stock future`
  会塌成同样的 `future=1 / ooo=1`，**无法恢复 route truth**。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from fakes import make_quote

from arad.engine import (
    STALE_TOLERANCE_SECONDS,
    EngineState,
    StateUpdateResult,
)

NOW = datetime(2026, 9, 24, 10, 30, 0)
EP = NOW.timestamp()


# ===========================================================================
# StateUpdateResult 形状
# ===========================================================================

def test_state_update_result_carries_route_local_facts():
    """合同：结果必须带 route + admitted + future + ooo + stale 诊断。"""
    r = StateUpdateResult(route="stocks")
    assert r.route == "stocks"
    assert r.admitted == {}
    assert r.rejected_future == 0
    assert r.rejected_out_of_order == 0
    assert r.stale_diagnosed == 0


# ===========================================================================
# RED 1：route code collision
# ===========================================================================

def test_route_code_collision_does_not_overwrite_index_age():
    """**RED 1**：index route 的 `000001` 与 stock route 的 `000001` 不共享诊断。

    任务书原文场景是 ``index age = 900s / stock age = 0s``。但那组数字在本仓库
    **判不出 stale**：``STALE_TOLERANCE_SECONDS = 4*3600 = 14400``（我第一版照抄
    了任务书的 900s，结果断言 `900 > 14400` 直接失败 —— 那是**我的测试写错**，
    不是产品错）。
    这里用 ``STALE_TOLERANCE_SECONDS + 600`` 构造**真陈旧**，
    同时保留"两 route 同裸码"这个真正的考点。
    """
    st = EngineState()
    stale_age = STALE_TOLERANCE_SECONDS + 600.0

    # index：裸 000001（Sina 在 index route 的解析结果），时间陈旧
    idx_ts = NOW - timedelta(seconds=stale_age)
    st.update([make_quote(code="000001", price=3900.0, ts=idx_ts)],
              NOW, route="index")

    # stock：同一个裸码 000001（平安银行），新鲜
    st.update([make_quote(code="000001", price=11.0, ts=NOW)],
              NOW, route="stocks")

    idx_age = st.time_age_seconds_by_route.get(("index", "000001"))
    stk_age = st.time_age_seconds_by_route.get(("stocks", "000001"))

    assert idx_age is not None, (
        "index route 的时间诊断必须存在（不能被 stock 覆盖掉）")
    assert stk_age is not None, "stock route 的时间诊断必须存在"
    assert idx_age == pytest.approx(stale_age, abs=1.0), (
        f"index age 应约 {stale_age}s，实测 {idx_age}")
    assert stk_age == pytest.approx(0.0, abs=1.0), (
        f"stock age 应约 0s，实测 {stk_age}")
    assert idx_age > STALE_TOLERANCE_SECONDS, (
        "index 这条必须仍被判为 stale（这正是被覆盖后会丢失的事实）")
    assert stk_age <= STALE_TOLERANCE_SECONDS, "stock 这条必须仍被判为 fresh"

    # 兼容 view 只反映**默认 route**（个股），所以裸码上是新鲜值 ——
    # 这正说明旧 view 无法表达 index 的陈旧：必须用 by_route 表。
    assert st.time_age_seconds.get("000001") == pytest.approx(0.0, abs=1.0), (
        "裸码兼容 view 是 stock-only，这里应显示 stock 的新鲜值")


def test_route_collision_keeps_both_provider_timestamps():
    """两 route 的 provider 原始 ts 必须各自可读，不得互相覆盖。"""
    st = EngineState()
    idx_ts = NOW - timedelta(seconds=STALE_TOLERANCE_SECONDS + 600.0)
    st.update([make_quote(code="000001", price=3900.0, ts=idx_ts)],
              NOW, route="index")
    st.update([make_quote(code="000001", price=11.0, ts=NOW)],
              NOW, route="stocks")

    a = st.provider_ts_raw_by_route.get(("index", "000001"))
    b = st.provider_ts_raw_by_route.get(("stocks", "000001"))
    assert a == pytest.approx(idx_ts.timestamp(), abs=0.5), f"index ts 实测 {a}"
    assert b == pytest.approx(NOW.timestamp(), abs=0.5), f"stock ts 实测 {b}"
    assert a != b, "两个 route 的同一裸码必须是两条独立事实"


def test_stale_diagnosed_is_counted_per_route():
    """陈旧诊断计数按 route 分账（index 陈旧不该算到 stocks 头上）。"""
    st = EngineState()
    st.update([make_quote(code="000001", price=3900.0,
                          ts=NOW - timedelta(
                              seconds=STALE_TOLERANCE_SECONDS + 600.0))],
              NOW, route="index")
    st.update([make_quote(code="600000", price=11.0, ts=NOW)],
              NOW, route="stocks")
    assert st.stale_diagnosed.get("index", 0) >= 1, (
        f"index 应有 stale 诊断，实测 {dict(st.stale_diagnosed)}")
    assert st.stale_diagnosed.get("stocks", 0) == 0, (
        "stocks 是新鲜的，不该被算进 stale")


def test_backward_compatible_stock_only_view_still_works():
    """旧 stock-only 兼容 view 必须保留（既有调用方/测试仍读它）。"""
    st = EngineState()
    st.update([make_quote(code="600000", price=11.0, ts=NOW)], NOW)
    # 兼容 view：裸 code -> 值
    assert st.time_age_seconds.get("600000") is not None
    assert st.provider_ts_raw.get("600000") is not None
    assert st.effective_event_time.get("600000") is not None


# ===========================================================================
# RED 2：reject reason route attribution
# ===========================================================================

def _run_case(order):
    """跑一个两种 reject 的排列，返回两条 route 各自的 detailed 结果。

    ``order`` = [("index", "future"), ("stocks", "ooo")] 之类。
    """
    st = EngineState()
    out = {}
    for route, kind in order:
        code = "000001" if route == "index" else "600000"
        if kind == "future":
            q = make_quote(code=code, price=99.0,
                           ts=NOW + timedelta(seconds=9999))
        else:  # ooo —— 先种一个未来水位，再喂一个更旧的
            st.update([make_quote(code=code, price=10.0, ts=NOW)], NOW,
                      route=route)
            q = make_quote(code=code, price=8.0,
                           ts=NOW - timedelta(seconds=5))
        out[route] = st.update_detailed([q], NOW, route=route)
    return out


def test_reject_reason_route_attribution_case_a():
    """**RED 2 Case A**：index future=1 / stock ooo=1。"""
    r = _run_case([("index", "future"), ("stocks", "ooo")])
    assert r["index"].rejected_future == 1, (
        f"index 该记 future，实测 {r['index']}")
    assert r["index"].rejected_out_of_order == 0
    assert r["stocks"].rejected_out_of_order == 1, (
        f"stocks 该记 ooo，实测 {r['stocks']}")
    assert r["stocks"].rejected_future == 0


def test_reject_reason_route_attribution_case_b():
    """**RED 2 Case B**：index ooo=1 / stock future=1。

    Case A 与 Case B 的 **aggregate 完全相同**（future=1 / ooo=1），
    但 per-route result 必须不同 —— 这正是"从 global stats 反推 route reason"
    永远做不到的事（任务书 §WP03「禁止从 global stats 反推 route reason」）。
    """
    r = _run_case([("index", "ooo"), ("stocks", "future")])
    assert r["index"].rejected_out_of_order == 1, (
        f"index 该记 ooo，实测 {r['index']}")
    assert r["index"].rejected_future == 0
    assert r["stocks"].rejected_future == 1, (
        f"stocks 该记 future，实测 {r['stocks']}")
    assert r["stocks"].rejected_out_of_order == 0


def test_case_a_and_b_are_distinguishable():
    """**RED 2 的核心**：A/B 的 per-route 结果必须**不同**。"""
    a = _run_case([("index", "future"), ("stocks", "ooo")])
    b = _run_case([("index", "ooo"), ("stocks", "future")])

    a_sig = (a["index"].rejected_future, a["index"].rejected_out_of_order,
             a["stocks"].rejected_future, a["stocks"].rejected_out_of_order)
    b_sig = (b["index"].rejected_future, b["index"].rejected_out_of_order,
             b["stocks"].rejected_future, b["stocks"].rejected_out_of_order)

    assert a_sig != b_sig, (
        f"Case A 与 Case B 必须可区分，实测都是 {a_sig} —— "
        f"说明 reject reason 没有按 route 归属")
    assert a_sig == (1, 0, 0, 1), f"Case A 签名错: {a_sig}"
    assert b_sig == (0, 1, 1, 0), f"Case B 签名错: {b_sig}"


def test_global_counters_still_updated_for_compatibility():
    """**阳性对照**：全局 counters 必须**照旧**累加（旧读者不能被弄坏）。"""
    st = EngineState()
    st.update([make_quote(code="600000", price=99.0,
                          ts=NOW + timedelta(seconds=9999))], NOW,
              route="stocks")
    assert st.stats.get("t_reject:future", 0) == 1, (
        f"全局 future counter 必须仍累加，实测 {dict(st.stats)}")


def test_update_detailed_admits_normally():
    """**阳性对照**：正常数据必须照常准入（不能修成永远拒绝）。"""
    st = EngineState()
    r = st.update_detailed([make_quote(code="600000", price=11.0, ts=NOW)],
                           NOW, route="stocks")
    assert "600000" in r.admitted
    assert r.rejected_future == 0 and r.rejected_out_of_order == 0
    assert r.route == "stocks"


def test_result_route_is_the_callers_route_not_a_default():
    """**独立牙**：`result.route` 必须是**调用方传进来的** route。

    这条专门抓"归属字段被写死成默认值"这种回退 ——
    它不能靠"计数是否为 0"间接发现：计数全对、只有 route 标签错时，
    下游会把这个结果记到**别人**头上，而 aggregate 数字却完全正常。
    传一个**非默认** route（index）才能把它逼出来。
    """
    st = EngineState()
    r = st.update_detailed([make_quote(code="000001", price=3900.0, ts=NOW)],
                           NOW, route="index")
    assert r.route == "index", (
        f"index route 的结果必须自报 index，实测 {r.route!r} —— "
        f"写死默认值会让归属错到别人头上")
    # 同样确认默认 route 也能正确自报（不能反过来写死 index）
    r2 = st.update_detailed([make_quote(code="600000", price=11.0, ts=NOW)],
                            NOW)
    assert r2.route == "stocks", f"默认 route 应自报 stocks，实测 {r2.route!r}"


def test_legacy_update_signature_unchanged():
    """**接口纪律**：`update()` 必须保留原签名与返回类型（admitted dict）。"""
    st = EngineState()
    got = st.update([make_quote(code="600000", price=11.0, ts=NOW)], NOW)
    assert isinstance(got, dict), f"update 必须仍返回 dict，实测 {type(got)}"
    assert "600000" in got
