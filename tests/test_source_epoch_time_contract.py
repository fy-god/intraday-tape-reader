"""IT-P1-TIME-ROLE-001/002/003：source-epoch / time-role 时间合同。

## 为什么分两个文件

本文件的 epoch 机制需要**新 API**（``begin_source_epoch``）—— 因为"来源 epoch"
这个概念在修复前**根本不存在**。涉及它的用例在旧代码上按 ``AttributeError``
失败，那是**结构性**红，只证明"符号不存在"，**不证明行为变了**。

真正有牙的是本文件末尾的 ``test_engine_*`` 端到端用例：它们只用修复前就存在的
API（``Engine`` / ``SourceManager`` / ``poll_once`` / ``RoundObservationSet``），
**按真实触发条件**（切源）驱动，因此在旧代码上按 ``AssertionError`` 失败。
另见 ``tests/test_source_epoch_behavioral.py``，那里全是行为级用例。

## 三个缺陷（均已用真实代码复现）

**001 跨源共享水位线（最高优先）**
``accepted_watermark`` 是 ``code -> timestamp``，**不含来源**。A 源先接受
``10:30:40``；供数切到 B 源，B 首包 ``10:30:05`` —— 比 A 的水位线旧，
被当"乱序"**静默丢弃**。但 B 的时间轴是**另一个 epoch 的起点**，不是倒退。

实测（修复前）::

    A 源 ts=10:30:40 -> admitted=['600000']  watermark==10:30:40 True
    B 源 ts=10:30:05 -> admitted=[]          stats={'t_reject:out_of_order': 1}
                        quotes.price 仍是 10.0（B 的 10.5 丢了）

> 陷阱：构造用例时 ``ts`` 必须是**已发生**的时间。若用 ``now+40s``，它会被
> ``_admit_time`` 夹到 ``now``，水位线停在 ``now``，B 的 ``now+5s`` 也被夹到
> ``now`` —— 两者相等而放行，缺陷就被掩盖。我第一版探针正是这么错的。

**002 轻微超前被静默改写成本地 now**
实测：provider ``ts=now+2s`` → history 入库时间戳 ``== now``，原值丢失，
且当时没有 ``provider_ts_raw``。

**003 首见码无陈旧下限**
实测：首见码 ``ts=now-3天`` → ``admitted=['999999']``（被接受）。

## 修法

- 水位线按**实际 serving source** 分段：source 变化即开新 epoch，
  epoch **内**仍拒绝真实乱序，epoch **之间**不互相否决；
- 保留 provider 原始 ts（``provider_ts_raw``），并把
  ``effective_event_time`` 与 ``received_at`` 分开；
- 首见码加**保守**陈旧下限（只挡小时/天级陈旧，不误杀冷门股的最后成交时间）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fakes import make_quote

from arad.engine import Engine, EngineState, SourceManager
from arad.session import SessionPhase, TradingCalendar

# now = 10:31:00。所有 provider ts 都取**已发生**的时间（见模块 docstring 的陷阱）。
NOW = datetime(2026, 9, 15, 10, 31, 0)


class _ScriptedSource:
    """按预设脚本返回 quote，用于精确控制"哪个源、哪个 ts"。"""

    def __init__(self, name: str, script: dict[str, list] | None = None,
                 *, fail: bool = False):
        self.name = name
        self.script = script or {}
        self.fail = fail
        self.calls = 0

    def universe(self):
        return []

    def snapshots(self, codes):
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"{self.name} 故意失败")
        out = []
        for c in codes:
            rows = self.script.get(c)
            if rows:
                out.append(rows.pop(0))
        return out

    def health(self):
        return {"name": self.name, "ok": not self.fail,
                "latency_ms": 0, "err": ""}


def _q(code: str, ts: datetime, price: float = 10.0, vol: float = 100.0):
    return make_quote(code=code, price=price, volume_lots=vol, ts=ts,
                      prev_close=10.0, open=10.0, high=10.0, low=10.0)


def _cal():
    cal = TradingCalendar(holidays=set())
    cal.phase = lambda now=None: SessionPhase.MORNING      # type: ignore[method-assign]
    return cal


def _state():
    return EngineState(history_len=360)


def _ago(sec: float) -> datetime:
    return NOW - timedelta(seconds=sec)


# ===========================================================================
# 001 —— source epoch：跨源首包不得被旧 epoch 的水位线误杀
# ===========================================================================
def test_cross_source_first_packet_is_admitted():
    """A 接受 10:30:40 后切到 B，B 首包 10:30:05 必须被接受。"""
    st = _state()
    admitted_a = st.update([_q("600000", _ago(20))], NOW)
    assert "600000" in admitted_a, "A 源首包应被接受"

    st.begin_source_epoch("sina")                     # 供数切到 B
    admitted_b = st.update([_q("600000", _ago(55), price=10.5)], NOW)
    assert "600000" in admitted_b, (
        "跨源首包被旧 epoch 的水位线误杀：B 的时间轴是另一个 epoch 的起点，"
        "不是'倒退'")
    assert st.quotes["600000"].price == 10.5


def test_same_epoch_real_out_of_order_still_rejected():
    """同一 epoch 内真正倒退的点**仍然必须拒绝** —— 别把闸门一起拆了。"""
    st = _state()
    st.update([_q("600000", _ago(20))], NOW)
    st.begin_source_epoch("sina")
    st.update([_q("600000", _ago(55))], NOW)

    admitted = st.update([_q("600000", _ago(59))], NOW)      # 同 epoch 内更旧
    assert "600000" not in admitted, "同一 epoch 内的真实乱序必须仍被拒绝"
    assert st.stats.get("t_reject:out_of_order", 0) == 1


def test_a_to_b_to_a_forms_three_epochs():
    """A→B→A 必须形成**三个** epoch，不能复用最早 A 的硬水位线。"""
    st = _state()
    st.update([_q("600000", _ago(20))], NOW)
    st.begin_source_epoch("sina")
    st.update([_q("600000", _ago(55))], NOW)
    st.begin_source_epoch("tencent")                          # 回到 A
    admitted = st.update([_q("600000", _ago(10))], NOW)
    assert "600000" in admitted, "回到 A 后是新 epoch，10:30:50 应被接受"


def test_new_epoch_watermark_restarts_not_reuses_old():
    """新 epoch 的水位线必须从该 epoch 自己的数据重新起算。"""
    st = _state()
    st.update([_q("600000", _ago(20))], NOW)
    before = st.accepted_watermark["600000"]
    st.begin_source_epoch("sina")
    st.update([_q("600000", _ago(55))], NOW)
    after = st.accepted_watermark["600000"]
    assert after < before, (
        "新 epoch 的水位线仍在沿用旧 epoch 的硬水位线"
        f"（before={before}, after={after}）")


def test_same_source_rebegin_is_idempotent():
    """同一来源重复上报不该把该 epoch 的水位线清掉。"""
    st = _state()
    st.update([_q("600000", _ago(20))], NOW)
    st.begin_source_epoch("tencent")
    st.update([_q("600000", _ago(55))], NOW)
    st.begin_source_epoch("tencent")                          # 同源，幂等
    admitted = st.update([_q("600000", _ago(59))], NOW)       # epoch 内更旧
    assert "600000" not in admitted, (
        "同一来源重复 begin 把水位线清掉了，epoch 内乱序不再被拒")
    assert st.stats.get("t_reject:out_of_order", 0) == 1


# ===========================================================================
# 002 —— provider 原始 ts 不得因 clamp 消失
# ===========================================================================
def test_slightly_future_provider_ts_is_preserved_raw():
    """轻微超前的 provider ts 必须留痕，不能只写本地 now。"""
    st = _state()
    raw_ts = NOW + timedelta(seconds=2)
    st.update([_q("600000", raw_ts)], NOW)
    raw = st.provider_ts_raw
    assert "600000" in raw, "provider_ts_raw 没有记录该码的原始时间戳"
    assert abs(raw["600000"] - raw_ts.timestamp()) < 1e-6, (
        "provider_ts_raw 必须保存 provider 的原始时间戳，而不是夹过的值")


def test_effective_time_and_received_time_are_separate():
    """生效时间与接收时间必须是两个可分别读取的值。"""
    st = _state()
    st.update([_q("600000", NOW + timedelta(seconds=2))], NOW)
    eff = st.effective_event_time["600000"]
    recv = st.received_at["600000"]
    assert eff <= recv, "生效时间不应晚于接收时间"
    assert abs(recv - NOW.timestamp()) < 1e-6, "received_at 应是本地接收时刻"


def test_normal_past_ts_raw_equals_effective():
    """回归保护：不超前时原始 ts 与生效时间应一致。"""
    st = _state()
    ts = _ago(20)
    st.update([_q("600000", ts)], NOW)
    assert abs(st.provider_ts_raw["600000"] - ts.timestamp()) < 1e-6
    assert abs(st.effective_event_time["600000"] - ts.timestamp()) < 1e-6


# ===========================================================================
# 003 —— 陈旧判定（**本轮下调**：改为合同驱动）
# ===========================================================================
def test_stale_packet_admitted_when_contract_says_unknown():
    """WP03：合同三源 `freshness_allowed=false` -> 不得用 provider ts 硬拒绝。

    这是本轮对上一轮 `100e06a` 的**明确下调**：上一轮断言"3 天前首见码必须被拒"，
    但那与 `source_time_contract.json`（三源 role=unknown）自相矛盾。
    现在只有**受信任来源**才硬拒绝。
    """
    st = _state()
    st.begin_source_epoch("stocks", "tencent#0", source_name="tencent")
    st.update([_q("999999", NOW - timedelta(days=3))], NOW, route="stocks")
    assert "999999" in st.quotes, (
        "合同 freshness_allowed=false 时硬拒绝了 provider ts —— 与合同矛盾")
    assert st.stats.get("t_reject:stale", 0) == 0, "不该计入 stale 硬拒绝"


def test_stale_packet_rejected_when_source_is_trusted():
    """受信任来源（freshness_allowed=true）仍必须硬拒绝 3 天前的首包。

    这条保证"下调"没有把能力删掉 —— 只是把它置于合同开关之下。
    """
    from arad import engine as eng_mod

    saved = dict(eng_mod.TIME_POLICY.get("tencent", {}))
    eng_mod.TIME_POLICY["tencent"] = {"freshness_allowed": True,
                                      "strict_ordering_allowed": True}
    try:
        st = _state()
        st.begin_source_epoch("stocks", "tencent#0", source_name="tencent")
        admitted = st.update([_q("999999", NOW - timedelta(days=3))], NOW,
                             route="stocks")
        assert "999999" not in admitted, "受信任来源的陈旧首包应被拒"
        assert st.stats.get("t_reject:stale", 0) >= 1, "应计入 t_reject:stale"
    finally:
        eng_mod.TIME_POLICY["tencent"] = saved


def test_stale_is_diagnosed_under_unknown_contract():
    """不拦也要留痕：age 诊断必须写进 time_age_seconds。"""
    st = _state()
    st.begin_source_epoch("stocks", "tencent#0", source_name="tencent")
    st.update([_q("999999", NOW - timedelta(days=3))], NOW, route="stocks")
    assert st.time_age_seconds.get("999999", 0) > 4 * 3600
    assert st.stale_diagnosed.get("stocks", 0) >= 1


def test_first_seen_fresh_packet_is_admitted():
    """回归保护：正常新鲜首见码必须照常接受。"""
    st = _state()
    assert "600000" in st.update([_q("600000", _ago(1))], NOW)


def test_illiquid_last_trade_time_is_not_stale():
    """回归保护：冷门股"最后成交时间"可能较旧，但不该被当陈旧丢弃。

    陈旧下限必须**保守** —— 只挡小时/天级，不挡分钟级。
    """
    st = _state()
    assert "600000" in st.update([_q("600000", _ago(1800))], NOW), (
        "30 分钟前的最后成交时间被误判为陈旧 —— 下限过于激进")


def test_stale_limit_does_not_reject_no_ts():
    """回归保护：无 ts 的源（多数 source 不填）不得被陈旧下限误杀。"""
    st = _state()
    q = make_quote(code="600000", price=10.0, volume_lots=100.0, ts=None,
                   prev_close=10.0, open=10.0, high=10.0, low=10.0)
    assert "600000" in st.update([q], NOW)


# ===========================================================================
# 端到端（**行为级**：只用修复前就存在的 API）
# ===========================================================================
def _engine_with(t0, a, b, seen):
    from arad.config import load_settings
    from arad.store import AlertStore

    class _Cap:
        name = "capture"

        def evaluate(self, snap, ctx):
            seen.append(getattr(ctx, "observation", None))
            return []

    settings = load_settings(use_cache=False)
    cal = _cal()
    eng = Engine(source=SourceManager([a, b], threshold=1), settings=settings,
                 rules=[_Cap()], notifiers=[], calendar=cal, now_fn=lambda: t0,
                 store=AlertStore(settings, calendar=cal))
    eng._codes = ["600000"]
    eng._codes_pinned = True
    eng.watchlist = []
    eng.refresh_universe = lambda *x, **k: 0
    return eng


def test_engine_admits_fresh_data_after_source_switch():
    """端到端：切源后该轮必须仍有 admitted（修复前是 0）。

    只用修复前就存在的 API —— 失败是 ``AssertionError``，不是 ``ImportError``。
    """
    a = _ScriptedSource("tencent", {"600000": [_q("600000", _ago(20))]})
    b = _ScriptedSource("sina", {"600000": [_q("600000", _ago(55), price=10.5)]})
    seen = []
    eng = _engine_with(NOW, a, b, seen)

    eng.poll_once(force=True)
    assert seen[-1].admitted == 1, "R1：A 源数据应被接受"

    a.fail = True                                   # 触发切源 -> B 供数
    eng.poll_once(force=True)
    obs = seen[-1]
    assert obs.returned == 1, "R2：B 源确实返回了数据"
    assert obs.admitted == 1, (
        "切源后 B 的新鲜数据被丢弃"
        f"（admitted={obs.admitted}, out_of_order={obs.out_of_order_rejected}）")
    assert eng.state.quotes["600000"].price == 10.5


def test_engine_serving_source_change_is_the_epoch_trigger():
    """epoch 必须由**实际 serving source** 变化触发，而不是"本轮谁被调用"。"""
    a = _ScriptedSource("tencent", {"600000": [_q("600000", _ago(20))]})
    b = _ScriptedSource("sina", {"600000": [_q("600000", _ago(55), price=10.5)]})
    seen = []
    eng = _engine_with(NOW, a, b, seen)

    eng.poll_once(force=True)
    first = eng.state.source_epoch
    a.fail = True
    eng.poll_once(force=True)
    second = eng.state.source_epoch
    assert first != second, (
        f"serving source 改变后 epoch 未推进（仍是 {first}）")
    assert "sina" in str(second), f"epoch 应包含实际供数源，实际为 {second!r}"


def test_epoch_distinguishes_same_named_sources():
    """两个源**重名**时 epoch 仍必须能区分 —— 否则跨源误杀原样回来。

    只按名字分 epoch 的话，``sina -> sina`` 会被判为幂等而不重置水位线。
    实测构造：两个都叫 ``sina`` 的源，前者先喂旧时间戳，切换后时间戳更旧，
    若 epoch 未推进则会被误杀。
    """
    a = _ScriptedSource("sina", {"600000": [_q("600000", _ago(20))]})
    b = _ScriptedSource("sina", {"600000": [_q("600000", _ago(55), price=10.5)]})
    seen = []
    eng = _engine_with(NOW, a, b, seen)

    eng.poll_once(force=True)
    first = eng.state.source_epoch
    a.fail = True
    eng.poll_once(force=True)
    second = eng.state.source_epoch

    assert first != second, (
        f"两个同名源的 epoch 标签相同（{first!r}）—— 重名时跨源误杀会回来")
    assert seen[-1].admitted == 1, "重名源切换后数据被丢"
