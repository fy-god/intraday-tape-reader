"""Universe Refresh Truth v4 回归测试（云端 2026-09-22 12:03 WP01/WP02）。

来源：`2026-09-22_12-03-20_JST.md`（reviewed `369b13f`）。

三条指控**全部针对我 09:00 轮新写的代码**：

* `IT-P1-UNIVERSE-COVERAGE-GATE-TRUNCATION-BLIND-001`
  我加 `universe_coverage` 时把"分母未知"一律归入**未测量 -> ok**，
  漏了"分母未知但 provider 明说 `transport_complete=False`（被截断）"——
  实测判 `ok / healthy=True`。**可判而未判**，又一种假绿。
* `IT-P1-UNIVERSE-META-STALE-AFTER-FAILED-REFRESH-001`
  全源失败时 `refresh_universe()` 返回 0 但**不更新** `_universe_meta`，
  实测 `universe_truth()` 断供前后**逐字段相同**，消费者无从知道
  那份覆盖度是多久以前的。
* `IT-P1-HEALTH-COVERAGE-SNAPSHOT-PRELOOP-001`（WP02）
  `setup['universe_truth']` 只在 soak **开始前**取一次，
  中途的刷新失败**结构上**进不了最终判决。

设计纪律：新符号一律 ``getattr`` 在函数体内取，避免 ImportError 冒充行为级 RED。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tools"))


def _ls():
    import live_session
    return live_session


def _lvl(v, name):
    it = [c for c in v["checks"] if c["name"] == name]
    return it[0]["level"] if it else "MISSING"


def _base_metrics():
    m = _ls().empty_metrics()
    m.update({
        "rounds": 30, "alerts_total": 0, "alerts_by_kind": {},
        "quotes": {"min": 5000, "max": 5000, "last": 5000, "first": 5000},
        "universe": {"min": 5000, "max": 5000, "last": 5000, "first": 5000},
        "rounds_with_observation": 30, "coverage_rounds": 30,
        "coverage": 0.99, "coverage_p05": 0.99, "coverage_p50": 0.99,
        "coverage_min": 0.99, "coverage_max": 1.0,
        "requested_total": 5000 * 30, "returned_total": 5000 * 30,
        "admitted_total": 5000 * 30, "source_mix": {"eastmoney": 30},
        "source_mix_rounds": 30, "capability_unavailable_ratio": 0.0,
        "memory": {"bounded": True, "reason": "ok"},
        "api": {"requests": 300, "non_200": 0, "malformed": 0,
                "unreachable": 0, "by_route": {}},
        "sse": {"connected": True, "stalled": False, "events_total": 50,
                "events_by_type": {}, "status": None, "error": None},
    })
    return m


def _with_truth(truth, **setup_extra):
    m = _base_metrics()
    s = {"engine_build_s": 1.0, "universe_refresh_s": 1.0,
         "universe_size": 5000, "fell_back_to_watchlist": False,
         "universe_truth": truth}
    s.update(setup_extra)
    m["setup"] = s
    return m


# ===========================================================================
# WP01 RED 1：unknown denominator + 显式截断必须 FAIL
# ===========================================================================
def test_truncation_blind_gate_is_now_a_check_item():
    """必须有独立项读 `transport_complete`（与 coverage 分开）。"""
    v = _ls().evaluate_health(_base_metrics())
    names = [c["name"] for c in v["checks"]]
    assert "universe_transport" in names, (
        f"provider 明说被截断时必须能判；当前项：{names}")


def test_explicit_truncation_without_denominator_must_fail():
    """**本轮核心 RED**：分母未知 + `transport_complete=False` -> **fail**。

    修前实测：`universe_coverage=ok / healthy=True / exit=0 / fail=[]`。
    这**不是**"分母未知所以无从判断" —— provider 已经明确告诉我们
    结果不完整，把它归入"未测量"就是**可判而未判**。
    """
    m = _with_truth({
        "denominator_kind": "unknown", "expected_total": 0,
        "coverage_active": None, "coverage_transport": None,
        "coverage_usable": None, "active_scan_codes": 5000,
        "raw_unique_codes": 5000, "usable_quotes": 5000,
        "transport_complete": False, "shortfall": 0,
        "denominator_known": False,
    })
    v = _ls().evaluate_health(m)
    assert "universe_transport" in v["fail"], (
        f"provider 声明被截断却判健康：fail={v['fail']}")
    assert v["healthy"] is False
    # coverage 项仍应保持"未测量"（它确实算不出比例）—— 两者证据来源不同
    assert _lvl(v, "universe_coverage") == "ok"


def test_clean_pagination_without_denominator_stays_ok():
    """反面对照：翻页翻完但无 numeric total **不该**判红（避免误报）。

    新浪 clean pagination 能证明"翻页翻完了"，只是给不出总数。
    把它判红会把正常配置误报成事故。
    """
    m = _with_truth({
        "denominator_kind": "pagination_exhausted_non_numeric",
        "expected_total": 0, "coverage_active": None,
        "transport_complete": True, "active_scan_codes": 5000,
    })
    v = _ls().evaluate_health(m)
    assert _lvl(v, "universe_transport") == "ok"
    assert "universe_transport" not in v["fail"]


def test_transport_and_coverage_are_separate_evidence():
    """两项证据来源不同，**不能合并** —— 合并会互相掩盖。"""
    trunc = _with_truth({
        "denominator_kind": "unknown", "expected_total": 0,
        "coverage_active": None, "transport_complete": False,
        "active_scan_codes": 5000})
    v = _ls().evaluate_health(trunc)
    assert _lvl(v, "universe_coverage") == "ok"      # 比例确实算不出
    assert _lvl(v, "universe_transport") == "fail"   # 但截断是硬事实


# ===========================================================================
# WP01 RED 2：active snapshot 与 latest attempt 必须分离
# ===========================================================================
class _UniSrc:
    """universe 源：可切换为"全抛异常"。"""

    name = "flat"

    def __init__(self, n):
        self.n = n
        self.fail = False

    def universe(self):
        if self.fail:
            raise RuntimeError("provider down")
        from datetime import datetime

        from arad.models import Board, Quote
        return [Quote(code=f"60{i:04d}", name="x", board=Board.MAIN,
                      price=10.0, prev_close=10.0, open=10.0, high=10.0,
                      low=10.0, volume_lots=100, amount=1000.0,
                      ts=datetime(2026, 9, 21, 10, 0, 0))
                for i in range(self.n)]

    def universe_info(self):
        return {"complete": True, "transport_complete": True,
                "transport_expected_total": self.n,
                "raw_unique_codes": self.n, "usable_quotes": self.n,
                "shortfall": 0}

    def snapshots(self, codes=None, **kw):
        return []

    def capabilities(self):
        from arad.capabilities import capabilities_for
        return capabilities_for("replay")

    def health(self):
        return {"name": "flat", "ok": True, "latency_ms": 1, "err": ""}


def _engine_with(src):
    from arad.config import load_settings
    from arad.engine import Engine
    from arad.session import TradingCalendar
    eng = Engine(source=src, settings=load_settings(use_cache=False),
                 calendar=TradingCalendar(), watchlist=[])
    # `_universe_sources()` 按配置**新建**源，不使用注入的 source= ——
    # 必须显式钉住，否则会打到真实网络（我第一次跑就拿到了真实市场数据）。
    eng._universe_src = [src]
    return eng


def test_universe_truth_has_active_snapshot_and_latest_attempt():
    """v4 必须把两张账**分开**：当前生效的池 vs 最近一次尝试。"""
    eng = _engine_with(_UniSrc(120))
    eng.refresh_universe()
    ut = eng.universe_truth()
    for k in ("active_snapshot", "latest_attempt", "freshness"):
        assert k in ut, f"缺 {k}（v4 要求两张账分离）"
    assert ut["latest_attempt"].get("status") == "applied"
    assert ut["freshness"].get("fresh") is True
    assert ut["active_snapshot"]["active_scan_codes"] == 120


def test_failed_refresh_is_recorded_not_silently_identical():
    """**本轮核心 RED**：全源失败必须落账，不能与断供前逐字段相同。

    修前实测：两次 `universe_truth()` **逐字段相同**（`ut1 == ut2` 为 True），
    且没有 `attempt_status` / `last_attempt_at` 字段 ——
    消费者无从知道那份 coverage 是多久以前的。
    """
    src = _UniSrc(120)
    eng = _engine_with(src)
    eng.refresh_universe()
    ut1 = eng.universe_truth()
    assert ut1["attempt_status"] == "applied"

    src.fail = True
    eng.refresh_universe()
    ut2 = eng.universe_truth()

    assert ut2["attempt_status"] == "all_failed", (
        f"全源失败必须记 all_failed，实得 {ut2['attempt_status']!r}")
    assert ut1 != ut2, "断供前后不该逐字段相同（attempt 变了）"
    # active pool 必须**保留** —— 不能因为一次刷新失败就把扫描集抹掉
    assert ut2["active_scan_codes"] == 120
    # 但 freshness 必须说出"最近一次没生效"
    assert ut2["freshness"]["fresh"] is False


def test_rejected_smaller_partial_is_recorded():
    """更小的 partial 被拒也要落账（保留 active pool 是对的，但要有痕迹）。"""
    class _Partial(_UniSrc):
        def __init__(self, n):
            super().__init__(n)
            self.mode = "full"

        def universe(self):
            if self.mode == "partial":
                from datetime import datetime

                from arad.models import Board, Quote
                return [Quote(code=f"60{i:04d}", name="x", board=Board.MAIN,
                              price=10.0, prev_close=10.0, open=10.0,
                              high=10.0, low=10.0, volume_lots=100,
                              amount=1000.0,
                              ts=datetime(2026, 9, 21, 10, 0, 0))
                        for i in range(self.n)]
            return super().universe()

        def universe_info(self):
            if self.mode == "partial":
                return {"complete": False, "transport_complete": False,
                        "transport_expected_total": 5000,
                        "raw_unique_codes": self.n, "usable_quotes": self.n,
                        "shortfall": 5000 - self.n, "reason": "截断"}
            return super().universe_info()

    src = _Partial(120)
    eng = _engine_with(src)
    eng.refresh_universe()                  # 先拿到 120 只
    src.mode = "partial"
    src.n = 50                              # 更小的 partial
    eng.refresh_universe()
    ut = eng.universe_truth()
    assert ut["attempt_status"] == "rejected_smaller", (
        f"被拒的部分池必须落账，实得 {ut['attempt_status']!r}")
    assert ut["active_scan_codes"] == 120, "被拒后 active pool 必须保留"


# ===========================================================================
# WP01 RED 3：freshness 阈值
# ===========================================================================
@pytest.mark.parametrize("age,expect_fail", [
    (60, False),      # 新鲜
    (600, False),     # 刷新失败但池子还新 -> WARN（云端 RED 3）
    (1799, False),    # 仍在警戒线内 -> WARN
    (2000, True),     # 超过一个刷新周期(1800s) -> FAIL（云端 RED 3）
    (7200, True),     # 远超上限 -> FAIL
])
def test_freshness_thresholds(age, expect_fail):
    """陈旧度阈值（云端 12:03 WP01 RED 3）。

    语义：最近一次尝试**未生效**时，
    `age < 1800s` -> warn；`age >= 1800s` -> **fail**。
    即"刷新失败"叠加"池子已经超过一个刷新周期没更新"才是硬失败。
    """
    m = _with_truth({
        "denominator_kind": "provider_declared_total",
        "expected_total": 5913, "active_scan_codes": 5900,
        "coverage_active": 5900 / 5913, "transport_complete": True,
        "active_age_s": age, "attempt_status": "all_failed"})
    v = _ls().evaluate_health(m)
    assert ("universe_freshness" in v["fail"]) is expect_fail, (
        f"age={age}s 期望 fail={expect_fail}，实得 {v['fail']}")


def test_failed_attempt_enters_verdict():
    """刷新未生效必须进入判决（至少 warn）—— 不能只有数据层知道。"""
    m = _with_truth({
        "denominator_kind": "provider_declared_total",
        "expected_total": 6000, "active_scan_codes": 5900,
        "coverage_active": 5900 / 6000, "transport_complete": True,
        "active_age_s": 10, "attempt_status": "all_failed"})
    v = _ls().evaluate_health(m)
    assert _lvl(v, "universe_freshness") != "ok"


def test_freshness_not_measured_charges_nothing():
    """旧报告没有这些字段 -> 跳过，不算失败。"""
    v = _ls().evaluate_health(_with_truth({"expected_total": 5913,
                                           "active_scan_codes": 5913,
                                           "transport_complete": True}))
    assert "universe_freshness" not in v["fail"]


# ===========================================================================
# WP02：会话级聚合（t0 快照不能代表整场 soak）
# ===========================================================================
def test_universe_session_aggregates_worst_state():
    """会话聚合必须取**最坏** —— "中途坏过"不能被"最后又好了"抹掉。"""
    ls = _ls()
    fn = getattr(ls, "_universe_session", None)
    assert fn is not None, "必须有 _universe_session（WP02 会话聚合）"
    rows = [
        {"universe_fresh_state": "fresh",
         "universe_attempt_status": "applied", "universe_age_s": 1.0},
        {"universe_fresh_state": "stale",
         "universe_attempt_status": "all_failed", "universe_age_s": 4000.0},
        {"universe_fresh_state": "fresh",
         "universe_attempt_status": "applied", "universe_age_s": 2.0},
    ]
    agg = fn(rows)
    assert agg["measured"] is True
    assert agg["worst_state"] == "stale"
    assert agg["last_state"] == "fresh"      # 最新是 fresh，但最坏是 stale
    assert agg["rounds_not_applied"] == 1
    assert agg["max_age_s"] == 4000.0


def test_universe_session_skips_old_samples():
    """老轮样本没有这些键 -> measured=False，消费方跳过（不判红）。

    `measured` 之外的 watchlist 事实是**独立轴**（16:13 §2），
    即使没有任何 universe 新鲜度样本也要给出，所以这里只断言 `measured`。
    """
    fn = getattr(_ls(), "_universe_session")
    assert fn([])["measured"] is False
    assert fn([{"quotes": 1}, {"quotes": 2}])["measured"] is False


def test_session_stale_overrides_healthy_t0_snapshot():
    """**WP02 核心 RED**：t0 快照健康，但 soak 中途 stale -> 判决必须 fail。

    `setup['universe_truth']` 只是 t0 那一刻。若只看它，
    "跑着跑着股票池全刷不出来了"会**结构上**拿到绿灯。
    """
    m = _with_truth({
        "denominator_kind": "provider_declared_total",
        "expected_total": 5913, "active_scan_codes": 5913,
        "coverage_active": 1.0, "transport_complete": True,
        "active_age_s": 5, "attempt_status": "applied"})
    # t0 快照是健康绿灯
    v0 = _ls().evaluate_health(m)
    assert v0["healthy"] is True
    # 但会话中途变 stale
    m["universe_session"] = {
        "measured": True, "worst_state": "stale", "last_state": "stale",
        "states": {"stale": 10}, "attempt_statuses": {"all_failed": 10},
        "rounds_not_applied": 10, "rounds_transport_incomplete": 0,
        "max_age_s": 7200.0, "last_age_s": 7200.0, "refresh_ids_seen": 1}
    v1 = _ls().evaluate_health(m)
    assert v1["healthy"] is False, (
        "会话中途股票池 stale，最终判决不得仍是绿灯")
    assert "universe_freshness" in v1["fail"]


def test_round_sample_carries_universe_freshness():
    """`make_round_sample` 必须把股票池新鲜度写进轮样本（否则聚不了）。"""
    ls = _ls()
    s = ls.make_round_sample(
        index=1, latency_ms=1.0, alerts=[], error=False, quotes=100,
        universe=100, history_points=0, history_codes=0, max_deque=0,
        history_maxlen=0, watchlist_only=False, observation={},
        universe_truth={"attempt_status": "all_failed",
                        "transport_complete": False,
                        "freshness": {"state": "stale", "age_s": 4000.0},
                        "latest_attempt": {"refresh_id": 7}})
    assert s["universe_attempt_status"] == "all_failed"
    assert s["universe_fresh_state"] == "stale"
    assert s["universe_age_s"] == 4000.0
    assert s["universe_transport_complete"] is False
    assert s["universe_refresh_id"] == 7


def test_summarize_rounds_exposes_universe_session():
    """`summarize_rounds` 必须产出 `universe_session`（供判决读）。"""
    ls = _ls()
    rows = [ls.make_round_sample(
        index=i, latency_ms=1.0, alerts=[], error=False, quotes=10,
        universe=10, history_points=0, history_codes=0, max_deque=0,
        history_maxlen=0, watchlist_only=False, observation={},
        universe_truth={"attempt_status": "all_failed",
                        "freshness": {"state": "stale", "age_s": 5000.0}})
        for i in (1, 2)]
    m = ls.summarize_rounds(rows)
    assert "universe_session" in m
    us = m["universe_session"]
    assert us["measured"] is True
    assert us["worst_state"] == "stale"
    assert us["rounds_not_applied"] == 2
