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

WP04 / IT-P1-OBS-010：陈旧与超前必须分账
------------------------------------------
``stale_rejected`` 曾被 alias 成 ``future_rejected``，两个**互斥**的桶永远同值
（且唯一数据源是 ``t_reject:future``，名字与值相反）。WP03 让陈旧包不再被拦之后，
"陈旧"就只剩**诊断**可见 —— 新增 ``provider_stale_diagnosed`` 承载它，
别名指向该诊断，不再与 future 同值。
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
    # 该票是**超前**（ts=far），故计入 future_rejected —— WP04 之后
    # stale_rejected 不再 alias 到 future，必须用真正对应的桶。
    assert obs.future_rejected == 1
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
             + obs.future_rejected + obs.out_of_order_rejected)
    assert total == obs.stock_requested, (
        f"个股账本对不上: {total} != stock_requested {obs.stock_requested}"
        f" (requested={obs.requested}, index_requested={obs.index_requested})")


def test_stock_and_index_are_separately_accounted():
    """个股与指数必须分账，否则机械恒等式永远差 index_requested。

    IT-P2-OBS-008：分账的前提是**指数真的被请求了**。``_Cap`` 默认没有
    ``wants_indices``，引擎就**不发**指数请求（省流量），此时
    ``index_requested`` 必须是 0 —— 旧实现无条件写 ``len(index_codes)``，
    让"根本没抓"和"抓了没回来"取值相同，观测层无法分辨。
    这里显式把规则声明成需要指数，才是在测分账本身。
    """
    quotes = [make_quote(code="600000", price=10.0, volume_lots=100.0, ts=NOW)]
    eng, cap = _engine(quotes, ["600000"])
    cap.wants_indices = True                 # 规则声明需要指数 -> 引擎才会去抓
    eng.poll_once(force=True)

    obs = cap.obs[-1]
    assert obs.index_requested > 0, "规则声明需要指数时，poll 会请求指数，必须分账"
    assert obs.requested == obs.stock_requested + obs.index_requested
    assert obs.index_admitted <= obs.admitted


def test_index_not_requested_is_not_counted_as_requested():
    """**真验牙**（IT-P2-OBS-008）：没发出去的请求不得算"请求了"。

    规则不声明 ``wants_indices`` 时 ``_fetch_indices()`` 直接返回 []（故意不发
    请求）。旧实现仍把 5 个指数码算进 ``requested``/``index_requested``，
    于是账本里凭空多出 5 个"请求了但没回来"，coverage 被永久拉低 ——
    一个**正常配置**（不用指数规则）看起来像持续丢数据。
    """
    quotes = [make_quote(code="600000", price=10.0, volume_lots=100.0, ts=NOW)]
    eng, cap = _engine(quotes, ["600000"])
    assert not getattr(cap, "wants_indices", False), "本用例前提：规则不需要指数"
    assert eng.index_codes, "本用例前提：配置里确实有指数码"
    eng.poll_once(force=True)

    obs = cap.obs[-1]
    assert obs.index_requested == 0, (
        f"没发指数请求就不能记 index_requested，实际 {obs.index_requested}")
    assert obs.requested == len(eng._codes), (
        f"requested 只能是真发出去的个股数，实际 {obs.requested}")
    # 没抓的指数码也不该被当成"缺失" —— 那会把正常配置报成丢数
    _idx_in_missing = set(obs.unknown_missing) & set(eng.index_codes)
    assert not _idx_in_missing, (
        f"未请求的指数码不该进 missing: {sorted(_idx_in_missing)}")


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
    time_rejected_expect = obs.future_rejected + obs.out_of_order_rejected
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


# ---------------------------------------------------------------------------
# WP04 / IT-P1-OBS-010：陈旧诊断桶（与 future 分账）
# ---------------------------------------------------------------------------
def test_stale_diagnosed_is_recorded_and_separate_from_future():
    """陈旧包不再被拦（WP03），但**必须**进入诊断桶，且与 future 不同值。"""
    old = NOW - timedelta(days=3)
    quotes = [make_quote(code="600000", price=10.0, volume_lots=100.0, ts=old)]
    eng, cap = _engine(quotes, ["600000"])
    eng.poll_once(force=True)

    obs = cap.obs[-1]
    assert obs.provider_stale_diagnosed >= 1, "3 天前的包必须被诊断为陈旧"
    assert "600000" in obs.provider_stale_diagnosed_codes
    assert obs.provider_stale_by_route.get("stocks", 0) >= 1, "必须按 route 分账"
    assert obs.future_rejected == 0, "陈旧不是超前"
    assert obs.stale_rejected != obs.future_rejected, \
        "别名不得再让陈旧与超前同值（原缺陷）"


def test_fresh_quote_is_not_stale_diagnosed():
    """回归保护：新鲜包不得被诊断成陈旧。"""
    quotes = [make_quote(code="600000", price=10.0, volume_lots=100.0,
                         ts=NOW - timedelta(seconds=5))]
    eng, cap = _engine(quotes, ["600000"])
    eng.poll_once(force=True)
    obs = cap.obs[-1]
    assert obs.provider_stale_diagnosed == 0
    assert not obs.provider_stale_diagnosed_codes


def test_stale_diagnosis_is_per_round_not_cumulative():
    """诊断计数必须是**本轮**增量，不能把累计值当本轮（否则曲线单调上升）。"""
    old = NOW - timedelta(days=3)
    quotes = [make_quote(code="600000", price=10.0, volume_lots=100.0, ts=old)]
    eng, cap = _engine(quotes, ["600000"])
    eng.poll_once(force=True)
    first = cap.obs[-1].provider_stale_diagnosed
    assert first >= 1

    # 再跑一轮，**换成**新鲜 ts：本轮没有陈旧包，诊断增量必须为 0。
    # 注意要改**源里的**报价列表（_Src.snapshots 直接读它），
    # 而不是引擎上的某个副本。
    eng.sources.sources[0]._quotes = [
        make_quote(code="600000", price=10.0, volume_lots=100.0,
                   ts=NOW - timedelta(seconds=5))
    ]
    eng.poll_once(force=True)
    assert cap.obs[-1].provider_stale_diagnosed == 0, \
        "本轮没有陈旧包，诊断数应为 0（不得把累计值当本轮）"


def test_stale_bucket_is_json_safe():
    import json
    old = NOW - timedelta(days=3)
    quotes = [make_quote(code="600000", price=10.0, volume_lots=100.0, ts=old)]
    eng, cap = _engine(quotes, ["600000"])
    eng.poll_once(force=True)
    d = eng.store.status()["observation"]
    json.dumps(d, ensure_ascii=False)
    assert "provider_stale_diagnosed" in d
    assert isinstance(d["provider_stale_diagnosed_codes"], list), \
        "set 必须被转成 list，否则 JSON 序列化会失败"


# ---------------------------------------------------------------------------
# IT-P2-OBS-009：请求了但没回来的**指数**必须能被观测到
# ---------------------------------------------------------------------------
def test_requested_index_that_never_returns_lands_in_missing():
    """**真验牙**（IT-P2-OBS-009）：指数请求发了、provider 返回空，
    指数码必须出现在 ``unknown_missing`` 里。

    修复前 ``unknown_missing`` 只遍历 ``self._codes``（个股），指数码在整个
    账本里出现 **0 次** —— ``index_admitted=0`` 而 missing 为空，
    指数的丢失完全不可观测（个股丢失有 missing 兜底，指数没有）。
    """
    quotes = [make_quote(code="600000", price=10.0, volume_lots=100.0, ts=NOW)]
    eng, cap = _engine(quotes, ["600000"])
    cap.wants_indices = True                 # 必须真的发请求，否则本用例前提不成立
    # _Src 只认识 quotes 里的代码 -> 指数码一个都返回不了
    eng.poll_once(force=True)

    obs = cap.obs[-1]
    assert obs.index_requested > 0, "前提：指数请求确实发出去了"
    assert obs.index_admitted == 0, "前提：没有指数回来"
    _idx = set(eng.index_codes)
    _missing = set(obs.unknown_missing)
    assert _idx <= _missing, (
        f"请求了却没返回的指数必须进 unknown_missing；"
        f"缺失={sorted(_idx - _missing)}")


def test_index_requested_and_missing_stay_reconciled():
    """指数侧的机械恒等式也要能对平（与个股同样的口径）。"""
    quotes = [make_quote(code="600000", price=10.0, volume_lots=100.0, ts=NOW)]
    eng, cap = _engine(quotes, ["600000"])
    cap.wants_indices = True
    eng.poll_once(force=True)

    obs = cap.obs[-1]
    idx_missing = len(set(obs.unknown_missing) & set(eng.index_codes))
    idx_quality = len(set(obs.rejected_quality) & set(eng.index_codes))
    total = obs.index_admitted + idx_missing + idx_quality
    assert total == obs.index_requested, (
        f"指数账本对不上: admitted {obs.index_admitted} + missing {idx_missing} "
        f"+ quality {idx_quality} != index_requested {obs.index_requested}")
