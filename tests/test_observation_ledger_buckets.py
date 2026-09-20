"""IT-P2-OBS-003 / IT-P2-OBS-005 回归：账本三个桶必须互斥且可对账。

OBS-005：provider 返回了但 ``price<=0`` 被记成「没返回」
--------------------------------------------------------
旧代码 ``returned = dict(admitted)``，``unknown_missing`` 用 ``returned`` 做
补集。任何**已返回但被内部丢弃**的票（``price<=0``，以及被时间准入拒绝的）
都落进 ``unknown_missing``。于是这一个字段同时表达三种互斥语义，而
``capabilities.py`` 已定义的 ``rejected_quality`` 桶**零发射点**、实际不存在。

OBS-003：``admitted`` 混算两类语义
-----------------------------------
旧代码 ``admitted=len(eligible) + len(idx_admitted)``：``eligible`` 经**个股
业务粗筛**，``idx_admitted`` 是**指数纯时间准入**。两者相加使
``returned - admitted`` 无法解释为「被时间拒绝」。现在 ``admitted`` 是纯时间
准入口径，个股与指数同口径。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from fakes import make_quote

from arad.capabilities import RoundObservationSet, SourceCapabilities
from arad.engine import Engine, SourceManager
from arad.session import SessionPhase, TradingCalendar
from arad.store import AlertStore

NOW = datetime(2026, 9, 15, 10, 30, 0)


def _settings():
    from arad.config import load_settings
    return load_settings(use_cache=False)


def _cal():
    cal = TradingCalendar(holidays=set())
    cal.phase = lambda now=None: SessionPhase.MORNING      # type: ignore[method-assign]
    return cal


class _Src:
    def __init__(self, name: str, quotes):
        self.name = name
        self._quotes = quotes

    def universe(self):
        return []

    def snapshots(self, codes):
        return [q for q in self._quotes if q.code in set(codes)]

    def health(self):
        return {"name": self.name, "ok": True, "latency_ms": 0, "err": ""}


class _Cap:
    name = "capture"

    def __init__(self):
        self.obs = []

    def evaluate(self, snap, ctx):
        self.obs.append(getattr(ctx, "observation", None))
        return []


def _engine(quotes, codes):
    cap = _Cap()
    eng = Engine(source=SourceManager([_Src("tencent", quotes)], threshold=1),
                 settings=_settings(), rules=[cap], notifiers=[],
                 calendar=_cal(), now_fn=lambda: NOW,
                 store=AlertStore(_settings(), calendar=_cal()))
    eng._codes = list(codes)
    eng._codes_pinned = True
    eng.watchlist = []
    eng.refresh_universe = lambda *a, **k: 0
    return eng, cap


# ---------------------------------------------------------------------------
# OBS-005：price<=0 必须进 rejected_quality，不进 unknown_missing
# ---------------------------------------------------------------------------
def test_zero_price_goes_to_rejected_quality_not_missing():
    """provider 真返回了 600001（price=0），不得记成「没返回」。"""
    quotes = [make_quote(code="600000", price=10.0, volume_lots=100.0, ts=NOW),
              make_quote(code="600001", price=0.0, volume_lots=0.0, ts=NOW)]
    eng, cap = _engine(quotes, ["600000", "600001"])
    eng.poll_once(force=True)

    obs = cap.obs[-1]
    assert "600001" in obs.rejected_quality, "price<=0 必须进 rejected_quality"
    assert "600001" not in obs.unknown_missing, "返回了就不是 missing（OBS-005）"
    assert obs.returned == 2, "returned 是 provider 原始返回数"
    assert obs.admitted == 1


def test_truly_missing_code_still_reported():
    """provider 完全没返回的代码仍必须进 unknown_missing（不得修坏）。"""
    quotes = [make_quote(code="600000", price=10.0, volume_lots=100.0, ts=NOW)]
    eng, cap = _engine(quotes, ["600000", "600099"])
    eng.poll_once(force=True)

    obs = cap.obs[-1]
    assert "600099" in obs.unknown_missing, "没返回的必须报 missing"
    assert "600099" not in obs.rejected_quality
    assert obs.returned == 1


def test_time_rejected_code_is_not_double_counted_as_missing():
    """被时间准入拒绝的票不得**同时**出现在 unknown_missing（双重计数）。"""
    far = NOW + timedelta(seconds=9999)
    quotes = [make_quote(code="600000", price=10.0, volume_lots=100.0, ts=NOW),
              make_quote(code="600001", price=10.0, volume_lots=100.0, ts=far)]
    eng, cap = _engine(quotes, ["600000", "600001"])
    eng.poll_once(force=True)

    obs = cap.obs[-1]
    assert obs.stale_rejected == 1
    assert "600001" not in obs.unknown_missing, \
        "返回了但时间不合格，不该算 missing（OBS-005 双重计数）"
    assert "600001" not in obs.rejected_quality, "时间问题不是质量问题"


def test_three_buckets_are_mutually_exclusive():
    """missing / quality / 时间拒绝 三者互斥（同一 code 只能落一个桶）。"""
    far = NOW + timedelta(seconds=9999)
    quotes = [make_quote(code="600000", price=10.0, volume_lots=100.0, ts=NOW),
              make_quote(code="600001", price=0.0, volume_lots=0.0, ts=NOW),
              make_quote(code="600002", price=10.0, volume_lots=100.0, ts=far)]
    eng, cap = _engine(quotes, ["600000", "600001", "600002", "600099"])
    eng.poll_once(force=True)

    obs = cap.obs[-1]
    missing = set(obs.unknown_missing)
    quality = set(obs.rejected_quality)
    assert "600099" in missing and "600001" in quality
    assert not (missing & quality), "两桶不得相交"
    assert "600001" not in missing and "600099" not in quality


def test_ledger_identity_holds():
    """机械恒等式（个股口径）：

        requested_stock == admitted_stock + missing + quality + future + ooo

    ``requested`` 含指数，而 missing/quality 只覆盖个股，故必须用
    ``requested - index_requested`` 分账后才能对平。
    """
    far = NOW + timedelta(seconds=9999)
    quotes = [make_quote(code="600000", price=10.0, volume_lots=100.0, ts=NOW),
              make_quote(code="600001", price=0.0, volume_lots=0.0, ts=NOW),
              make_quote(code="600002", price=10.0, volume_lots=100.0, ts=far)]
    eng, cap = _engine(quotes, ["600000", "600001", "600002", "600099"])
    eng.poll_once(force=True)

    obs = cap.obs[-1]
    admitted_stock = obs.admitted - obs.index_admitted
    total = (admitted_stock + len(obs.unknown_missing) + len(obs.rejected_quality)
             + obs.stale_rejected + obs.out_of_order_rejected)
    assert total == obs.stock_requested, (
        f"个股账本对不上: {total} != stock_requested {obs.stock_requested}"
        f" (requested={obs.requested}, index_requested={obs.index_requested})")


def test_stock_and_index_are_separately_accounted():
    """个股与指数必须分账，否则机械恒等式永远差 index_requested。"""
    quotes = [make_quote(code="600000", price=10.0, volume_lots=100.0, ts=NOW)]
    eng, cap = _engine(quotes, ["600000"])
    eng.poll_once(force=True)

    obs = cap.obs[-1]
    assert obs.index_requested > 0, "poll 会请求指数，必须分账"
    assert obs.requested == obs.stock_requested + obs.index_requested
    assert obs.index_admitted <= obs.admitted


# ---------------------------------------------------------------------------
# OBS-003：admitted 是纯时间准入口径
# ---------------------------------------------------------------------------
def test_admitted_does_not_include_business_filtered():
    """被**个股业务粗筛**拦掉的票不得计入 admitted（那是业务过滤，不是准入）。"""
    # 688 是科创板，若被 exclude_boards 拦掉，它仍通过了时间准入
    quotes = [make_quote(code="600000", price=10.0, volume_lots=100.0, ts=NOW),
              make_quote(code="688001", price=10.0, volume_lots=100.0, ts=NOW)]
    eng, cap = _engine(quotes, ["600000", "688001"])
    eng.poll_once(force=True)

    obs = cap.obs[-1]
    # 两只都通过了时间准入（update 的返回值），所以 admitted 应为 2
    returned_by_update = eng.state.accepted_watermark
    assert "688001" in returned_by_update, "688001 应通过时间准入"
    assert obs.admitted == 2, (
        f"admitted 应为纯时间准入=2，实际 {obs.admitted}（OBS-003 混算业务粗筛）")


def test_returned_minus_admitted_is_time_rejection():
    """``returned - admitted`` 必须能解释为「被时间拒绝」的条数。"""
    far = NOW + timedelta(seconds=9999)
    quotes = [make_quote(code="600000", price=10.0, volume_lots=100.0, ts=NOW),
              make_quote(code="600001", price=10.0, volume_lots=100.0, ts=far)]
    eng, cap = _engine(quotes, ["600000", "600001"])
    eng.poll_once(force=True)

    obs = cap.obs[-1]
    time_rejected_expect = obs.stale_rejected + obs.out_of_order_rejected
    assert obs.returned - obs.admitted == time_rejected_expect, (
        f"returned({obs.returned}) - admitted({obs.admitted}) 应等于时间拒绝"
        f"({time_rejected_expect})")


# ---------------------------------------------------------------------------
# as_dict / status 层
# ---------------------------------------------------------------------------
def test_as_dict_exposes_rejected_quality():
    obs = RoundObservationSet(source="tencent", requested=2, returned=2,
                              admitted=1, rejected_quality=("600001",))
    d = obs.as_dict()
    assert d["rejected_quality"] == ["600001"]
    assert d["unknown_missing"] == []


def test_observation_status_json_safe_with_new_bucket():
    import json
    quotes = [make_quote(code="600001", price=0.0, volume_lots=0.0, ts=NOW)]
    eng, cap = _engine(quotes, ["600001"])
    eng.poll_once(force=True)
    st = eng.store.status()
    json.dumps(st, ensure_ascii=False)
    assert "rejected_quality" in st["observation"]
