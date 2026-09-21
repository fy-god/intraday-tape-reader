"""云端 2026-09-22 00:08 轮 + h=20 轮针对 `fd51674` 的指控 —— 全部已用真实代码复核。

| 指控 | 我的结论 |
|---|---|
| `IT-H20-DELIVERY-ACCOUNTING-GATE-MISSING` | **成立**：假绿从数据层**搬到了判决层** |
| `IT-P1-UNIVERSE-WATCHLIST-PIN-001` | **成立**：一次临时降级变**永久** |
| `IT-P1-ALERT-TOTAL-KIND-TRUNCATION-001` | **成立**（本轮**我自己**复核发现的，见 `web.py:603`）|

本文件只用**既有** API 断言；新符号一律 `getattr` 在函数内取 ——
这样回退验牙时失败落在断言上（行为级），而不是顶层 ImportError（结构性）。
"""
from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

CODE = "600000"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _ls():
    import live_session
    return live_session


def _healthy_metrics() -> dict:
    """一个逐项都过、判决必然 healthy 的 metrics。"""
    m = _ls().empty_metrics()
    m.update({
        "rounds": 30,
        "alerts_total": 57,
        "alerts_by_kind": {"limit_up": 20, "unusual": 15},
        "quotes": {"min": 5000, "max": 5913, "last": 5913},
        "universe": {"min": 5913, "max": 5913, "last": 5913},
        "rounds_with_observation": 30,
        "coverage_rounds": 30,
        "coverage": 0.999,
        "coverage_p05": 0.99,
        "coverage_p50": 0.999,
        "coverage_min": 0.99,
        "coverage_max": 1.0,
        "requested_total": 5913 * 30,
        "returned_total": 5908 * 30,
        "admitted_total": 5908 * 30,
        "source_mix": {"tencent": 30},
        "source_mix_rounds": 30,
        "capability_unavailable_ratio": 0.0,
        "memory": {"bounded": True, "reason": "ok"},
        "api": {"requests": 300, "non_200": 0, "malformed": 0,
                "unreachable": 0, "by_route": {}},
        "sse": {"connected": True, "stalled": False, "events_total": 50,
                "events_by_type": {}, "status": None, "error": None},
        "delivery_accounting_session": {
            "committed_alerts_total": 57,
            "committed_with_signal_id": 57,
            "signed_ratio": 1.0,
            "unsigned_total": 0,
            "accounting_errors": 0,
            "accounting_status_seen": {"ok": 30},
            "inconsistent_rounds": [],
            "delivery_accounting_rounds": 30,
            "accounting_status": "ok",
        },
    })
    return m


# ===========================================================================
# 1. IT-H20-DELIVERY-ACCOUNTING-GATE-MISSING（判决层假绿）
# ===========================================================================
def test_healthy_baseline_is_actually_healthy():
    """先把基线钉住：这个 helper 必须判 healthy，否则下面几条没有意义。"""
    v = _ls().evaluate_health(_healthy_metrics())
    assert v["healthy"] is True, v.get("fail")
    assert v["exit_code"] == 0


def test_delivery_accounting_is_a_real_check_item():
    """判决里必须真的有一项 ``delivery_accounting``。

    为什么：我上一轮把假绿修在**数据层**，但 ``evaluate_health``
    一项都没读它 —— 判决项只有 rounds/fetch/data/coverage/capability/
    api/sse/memory/browser/alerts 共 10 项，**一项都没提 delivery**。
    """
    v = _ls().evaluate_health(_healthy_metrics())
    names = [c["name"] for c in v["checks"]]
    assert "delivery_accounting" in names, (
        f"判决层必须真的读交付账本；当前项：{names}")


def test_broken_ledger_must_not_be_healthy():
    """**核心验收**：账本严重损坏时不许判健康。

    实测（修复前）：注入 ``accounting_status=inconsistent``、
    ``accounting_errors=7``、``committed_alerts_total=0``、
    ``committed_with_signal_id=66`` 后，
    ``healthy=True / exit=0``，且 ``checks`` 与健康基线**逐字节相同**。
    """
    m = copy.deepcopy(_healthy_metrics())
    m["delivery_accounting_session"].update({
        "committed_alerts_total": 0,        # 分母被吞
        "committed_with_signal_id": 66,     # 分子 > 分母（逻辑不可能）
        "signed_ratio": None,
        "accounting_errors": 7,
        "accounting_status_seen": {"inconsistent": 30},
        "inconsistent_rounds": list(range(30)),
        "accounting_status": "inconsistent",
    })
    v = _ls().evaluate_health(m)
    assert v["healthy"] is False, "账本坏了却仍判健康 = 假绿搬到了判决层"
    assert v["exit_code"] != 0
    assert "delivery_accounting" in v["fail"]


def test_gate_zero_alone_cannot_prove_health():
    """**关键**：门禁 ``unsigned_total == 0`` 不能证明健康。

    这正是假绿的成因 —— 分母被吞时门禁也是 0。
    所以判据必须看 ``accounting_status`` / ``accounting_errors``，
    而不是只看那个被 ``max(...,0)`` 夹过的门禁值。
    """
    m = copy.deepcopy(_healthy_metrics())
    m["delivery_accounting_session"].update({
        "unsigned_total": 0,               # 门禁"通过"
        "committed_alerts_total": 0,       # 但分母被吞
        "committed_with_signal_id": 66,    # 分子远大于分母
        "accounting_errors": 7,
        "accounting_status": "inconsistent",
    })
    v = _ls().evaluate_health(m)
    assert v["healthy"] is False, (
        "门禁为 0 但记账不自洽 —— 必须判不健康（否则就是假绿）")


def test_not_measured_charges_nothing():
    """``not_measured`` 判 ok —— 否则所有现存绿色用例立刻转红。

    实测依据：仓库四个"应当判健康"的 helper
    （``empty_metrics()`` / ``finalize_metrics([])`` 等）产出的都是
    ``not_measured``。且这与 ``capability`` 项既有的
    "跳过不算失败"约定一致。
    """
    m = copy.deepcopy(_healthy_metrics())
    m["delivery_accounting_session"]["accounting_status"] = "not_measured"
    m["delivery_accounting_session"]["accounting_errors"] = 0
    m["delivery_accounting_session"]["committed_alerts_total"] = 0
    m["delivery_accounting_session"]["committed_with_signal_id"] = 0
    v = _ls().evaluate_health(m)
    assert v["healthy"] is True, (
        f"not_measured 不该判红：{v.get('fail')}")
    item = [c for c in v["checks"] if c["name"] == "delivery_accounting"]
    assert item and item[0]["ok"] is True


@pytest.mark.parametrize("evil", [
    None,
    "boom",
    {},
    {"accounting_status": None, "accounting_errors": "x",
     "inconsistent_rounds": "notalist", "delivery_accounting_rounds": None},
    {"accounting_status": "inconsistent", "accounting_errors": None,
     "inconsistent_rounds": None},
])
def test_missing_or_dirty_ledger_never_crashes(evil):
    """键缺失 / 非 dict / 脏值都**不许崩** —— ``evaluate_health``
    必须对脏 metrics 稳健（既有约束，见 test_live_session_observation）。"""
    m = copy.deepcopy(_healthy_metrics())
    m["delivery_accounting_session"] = evil
    v = _ls().evaluate_health(m)          # 不得抛
    assert isinstance(v, dict)
    assert "checks" in v


def test_ledger_key_absent_is_tolerated():
    """整段字段缺失（旧报告）也不能崩、不能误判成 fail。"""
    m = copy.deepcopy(_healthy_metrics())
    del m["delivery_accounting_session"]
    v = _ls().evaluate_health(m)
    assert v["healthy"] is True, (
        f"旧报告没有该字段，不该判红：{v.get('fail')}")


def test_real_metrics_pipeline_feeds_the_gate():
    """真实 ``finalize_metrics`` 产出的 metrics 必须带上该字段。

    否则判决项永远走"无交付账本"分支 = 等于没接。
    """
    m = _ls().finalize_metrics([])
    assert "delivery_accounting_session" in m, (
        "finalize_metrics 必须导出 delivery_accounting_session")


# ===========================================================================
# 2. IT-P1-UNIVERSE-WATCHLIST-PIN-001（临时降级变永久）
# ===========================================================================
def _engine():
    from arad.config import load_settings
    from arad.engine import Engine

    class _Cal:
        def phase(self, now=None):
            return "CLOSED"

    class _Src:
        name = "fake"

        def capabilities(self):
            from arad.capabilities import capabilities_for
            return capabilities_for("replay")

    return Engine(source=_Src(), settings=load_settings(use_cache=False),
                  calendar=_Cal(), watchlist=["000001", "000002"])


def test_codes_setter_pins_on_nonempty():
    """先把 setter 合同钉住（这条是**正确**的，不能改）。"""
    eng = _engine()
    eng._codes = ["600000", "600001"]
    assert eng._codes_pinned is True
    eng._codes = []
    assert eng._codes_pinned is False


def test_degraded_fallback_must_not_pin():
    """**核心验收**：降级到自选股**不得**置 pin。

    修复前：降级走 ``self._codes = list(self.watchlist)``
    -> setter 设 ``_codes_pinned = True``
    -> 下次 ``_maybe_refresh_universe`` 一进门 ``if self._codes_pinned: return``
    -> **源端恢复后也再不尝试全市场**。
    实测：降级后 pinned=True，TTL 过期且源端恢复时
    ``refresh_universe`` 调用次数 = **0**。
    """
    eng = _engine()
    eng.refresh_universe = lambda **kw: None       # 模拟全市场失败
    eng._codes = []
    eng._universe_refreshed_at = 0
    eng._maybe_refresh_universe(force=True)

    assert eng._codes == ["000001", "000002"], "应降级为自选股"
    assert eng._codes_pinned is False, (
        "降级是**临时**的，不得置 pin —— 否则永远回不到全市场")


def test_source_recovery_returns_to_full_market():
    """**行为级验收**：降级后源端恢复，必须重新尝试全市场。"""
    eng = _engine()
    eng.refresh_universe = lambda **kw: None
    eng._codes = []
    eng._universe_refreshed_at = 0
    eng._maybe_refresh_universe(force=True)
    assert eng._codes == ["000001", "000002"]

    calls = {"n": 0}

    def _recovered(**kw):
        calls["n"] += 1
        eng._codes_raw = [f"60{i:04d}" for i in range(3000)]
        eng._universe_refreshed_at = 9e18

    eng.refresh_universe = _recovered
    eng._universe_refreshed_at = 0                 # TTL 过期
    eng._maybe_refresh_universe()

    assert calls["n"] >= 1, (
        "源端恢复后必须重新尝试全市场 —— 调用 0 次说明被**永久**钉死在自选股")
    assert len(eng._codes) == 3000


def test_watch_only_still_pins():
    """对照：``--watch-only`` 与 replay **必须**仍然 pin。

    修复不能把这条既有语义改坏（``tests/test_cli_watch_only.py``
    明确断言 watch-only 必须 pin）。
    """
    eng = _engine()
    eng._codes = list(eng.watchlist)
    assert eng._codes_pinned is True, (
        "显式注入仍必须 pin（replay / watch-only 依赖它）")


# ===========================================================================
# 3. IT-P1-ALERT-TOTAL-KIND-TRUNCATION-001（按 kind 的 total 被截断）
# ===========================================================================
def test_per_kind_total_is_not_capped_by_ring_buffer():
    """**核心验收**：``/api/alerts?kind=...`` 的 total 不得被 ring buffer 截断。

    修复前 ``web.py`` 用 ``len(store.recent_alerts(1000, kind))``，
    而 Store 是 ``deque(maxlen=300)`` -> 灌入 450 条 limit_up 时
    total 报 **300**，而真实累计是 **450**。
    同一个展示字段：不带 kind 走 ``status()['alerts_total']``（正确），
    带 kind 走被截断的探测（错误）—— 两条路径语义不一致。
    """
    from datetime import datetime

    from arad.config import load_settings
    from arad.models import Alert, AlertKind
    from arad.store import AlertStore

    store = AlertStore(load_settings(use_cache=False))
    maxlen = getattr(store._alerts, "maxlen", None)
    assert maxlen is not None
    over = int(maxlen) + 150                       # 明确超过 ring buffer
    for i in range(over):
        store.add_alert(Alert(key=f"k{i}", kind=AlertKind.LIMIT_UP, code=CODE,
                              name="某股", ts=datetime(2026, 9, 21, 10, 0, 0),
                              price=10.0, pct=10.0, title="t", detail="d"))

    # 真正的累计真值在 store 里
    assert store.alerts_total() == over
    st = store.status()
    assert st["by_kind"][AlertKind.LIMIT_UP.value] == over, (
        "store 必须导出不随驱逐减少的 by_kind 累计")

    # 旧口径（被截断）
    naive = len(store.recent_alerts(1000, AlertKind.LIMIT_UP))
    assert naive == maxlen, "旧口径确实被截断 —— 这正是缺陷"

    # 新口径：``DashboardHandler._alerts_total`` 必须给出真值。
    # 该方法是普通函数（不碰 self 的其它属性），直接以替身作 self 调用。
    import arad.server.web as web_mod

    class _FakeAPI:
        def __init__(self, s):
            self.store = s

    _FakeAPI._alerts_total = web_mod.DashboardHandler._alerts_total
    got = _FakeAPI(store)._alerts_total([], AlertKind.LIMIT_UP.value)
    assert got == over, (
        f"按 kind 的 total 必须用累计 by_kind（真值 {over}），"
        f"而不是被截断的 ring-buffer 长度（{naive}）")


def test_per_kind_total_falls_back_when_by_kind_absent():
    """store 没导出 ``by_kind`` 时必须优雅退化，不能崩、不能报 0 给真数据。"""
    import arad.server.web as web_mod

    class _Store:
        def status(self):
            return {"alerts_total": 5}          # 没有 by_kind

        def recent_alerts(self, limit=100, kind=None):
            return [{"kind": kind}] * min(3, limit)

    class _FakeAPI:
        def __init__(self, s):
            self.store = s

    _FakeAPI._alerts_total = web_mod.DashboardHandler._alerts_total
    got = _FakeAPI(_Store())._alerts_total([], "limit_up")
    assert got == 3, "退化路径应回落到探测长度"
