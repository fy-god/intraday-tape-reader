"""IT-P1-TIME-ROLE-001/002/003 **行为级**回归。

## 为什么单独一个文件

``test_source_epoch_time_contract.py`` 里 epoch 相关用例需要新 API
（``begin_source_epoch`` / ``source_epoch`` / ``provider_ts_raw``），在修复前按
``AttributeError`` 失败 —— 那是**结构性**红，只证明"符号不存在"，
**不证明行为变了**。

本文件只用**修复前就存在**的公开 API，按**真实触发路径**驱动，因此在旧代码上
按 ``AssertionError`` 失败。这是"行为真的变了"的唯一依据。

## 各条的能力边界（如实交代，不含糊）

| 缺陷 | 本文件能不能给行为级牙 | 依据 |
|---|---|---|
| 001 跨源水位线误杀 | **能** | ``test_engine_end_to_end_switch_keeps_data``：走真实 ``poll_once`` 切源路径，旧代码 ``admitted=0`` |
| 002 原始 ts 丢失 | **不能** | 夹到 now 是**既有且被测试固定**的契约（``test_engine_time_admission.py`` 断言 ``h[-1][0] == EP``）。本次修复**不改变**该契约，只是**额外留痕** ``provider_ts_raw`` —— "多了一个属性"只能靠新符号验证。故 002 的牙在合同文件里（结构性），此处**不假装**有行为牙 |
| 003 首见陈旧 | **能** | ``test_stale_first_seen_does_not_pollute_state``：只看旧 API 的 ``quotes``/``history`` 有没有被污染 |

> 直说：002 我没有拿到行为级验牙。谁若要求"002 也有行为牙"，正确做法是先推翻
> "夹到 now"这个既有契约 —— 但那会改变已被测试固定的语义，本轮**不擅自**做。

## 关于"直接调 update()"的边界

跨源场景**无法**靠裸调 ``EngineState.update()`` 复现修复后的行为：``update()``
拿不到"这一包来自哪个源"的信息，epoch 必须由调用方（引擎）显式开启。所以本文件
不写"裸 update 断言跨源放行"的用例 —— 那种用例在修复后仍会红，是我自己写错，
不是产品错。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fakes import make_quote

from arad.engine import Engine, EngineState, SourceManager
from arad.session import SessionPhase, TradingCalendar

# 注意：所有 provider ts 必须是**已发生**时间。用 now+Ns 会被 _admit_time
# 夹到 now，令水位线停在 now，从而掩盖跨源误杀（我第一版探针就栽在这里）。
NOW = datetime(2026, 9, 15, 10, 31, 0)


def _ago(sec: float) -> datetime:
    return NOW - timedelta(seconds=sec)


def _q(code: str, ts, price: float = 10.0, vol: float = 100.0):
    return make_quote(code=code, price=price, volume_lots=vol, ts=ts,
                      prev_close=10.0, open=10.0, high=10.0, low=10.0)


def _cal():
    cal = TradingCalendar(holidays=set())
    cal.phase = lambda now=None: SessionPhase.MORNING      # type: ignore[method-assign]
    return cal


class _Src:
    def __init__(self, name, script, fail=False):
        self.name, self.script, self.fail = name, script, fail
        self.calls = 0

    def universe(self):
        return []

    def snapshots(self, codes):
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"{self.name} fail")
        out = []
        for c in codes:
            rows = self.script.get(c)
            if rows:
                out.append(rows.pop(0))
        return out

    def health(self):
        return {"name": self.name, "ok": not self.fail,
                "latency_ms": 0, "err": ""}


def _engine(a, b, seen):
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
                 rules=[_Cap()], notifiers=[], calendar=cal, now_fn=lambda: NOW,
                 store=AlertStore(settings, calendar=cal))
    eng._codes = ["600000"]
    eng._codes_pinned = True
    eng.watchlist = []
    eng.refresh_universe = lambda *x, **k: 0
    return eng


# ===========================================================================
# 001 —— 跨源水位线误杀（**行为级**，走真实 poll_once 切源路径）
# ===========================================================================
def test_engine_end_to_end_switch_keeps_data():
    """切源后该轮 admitted 不得为 0（旧代码：admitted=0, ooo=1）。"""
    a = _Src("tencent", {"600000": [_q("600000", _ago(20))]})
    b = _Src("sina", {"600000": [_q("600000", _ago(55), price=10.5)]})
    seen = []
    eng = _engine(a, b, seen)

    eng.poll_once(force=True)
    assert seen[-1].admitted == 1, "R1：主源数据应被接受"

    a.fail = True                                   # 触发切源 -> B 供数
    eng.poll_once(force=True)
    obs = seen[-1]
    assert obs.returned == 1, "R2：备用源确实返回了数据"
    assert obs.admitted == 1, (
        f"切源后数据被丢（admitted={obs.admitted}, "
        f"ooo={obs.out_of_order_rejected}）")


def test_engine_switch_updates_price_cache():
    """切源后缓存价格必须更新为新源的值（旧代码停在旧源的 10.0）。"""
    a = _Src("tencent", {"600000": [_q("600000", _ago(20), price=10.0)]})
    b = _Src("sina", {"600000": [_q("600000", _ago(55), price=10.5)]})
    seen = []
    eng = _engine(a, b, seen)

    eng.poll_once(force=True)
    a.fail = True
    eng.poll_once(force=True)
    assert eng.state.quotes["600000"].price == 10.5


# ===========================================================================
# 003 —— 首见陈旧（**行为级**：只看旧 API 的 quotes/history）
# ===========================================================================
def test_stale_first_seen_does_not_pollute_state():
    st = EngineState(history_len=360)
    st.update([_q("999999", NOW - timedelta(days=3))], NOW)
    assert "999999" not in st.quotes, "3 天前的首见报价污染了缓存"
    assert "999999" not in st.history, "3 天前的首见报价污染了 history"


def test_stale_first_seen_is_counted():
    st = EngineState(history_len=360)
    st.update([_q("999999", NOW - timedelta(days=3))], NOW)
    assert st.stats.get("t_reject:stale", 0) >= 1, "陈旧拒绝应计入 stats"


# ===========================================================================
# 回归保护（这些在修复前也应是绿的 —— 如实标注，不当验牙用例）
# ===========================================================================
def test_illiquid_30min_old_is_still_fine():
    """下限必须保守：冷门股最后成交时间可能较旧，不该被误杀。"""
    st = EngineState(history_len=360)
    st.update([_q("600000", _ago(1800))], NOW)
    assert "600000" in st.quotes


def test_fresh_first_seen_is_admitted():
    st = EngineState(history_len=360)
    st.update([_q("600000", _ago(1))], NOW)
    assert "600000" in st.quotes


def test_no_ts_is_not_stale_rejected():
    """多数 source 不填 ts —— 不得被陈旧下限误杀。"""
    st = EngineState(history_len=360)
    q = make_quote(code="600000", price=10.0, volume_lots=100.0, ts=None,
                   prev_close=10.0, open=10.0, high=10.0, low=10.0)
    st.update([q], NOW)
    assert "600000" in st.quotes


def test_same_epoch_out_of_order_still_rejected_end_to_end():
    """回归保护：同 epoch 内真实乱序**仍然**要被拒 —— 别把闸门拆了。

    引擎在两轮之间不切源时，第二轮的旧 ts 必须被 ooo 拒绝。
    """
    a = _Src("tencent", {"600000": [_q("600000", _ago(20)),
                                    _q("600000", _ago(90), price=9.9)]})
    b = _Src("sina", {})
    seen = []
    eng = _engine(a, b, seen)

    eng.poll_once(force=True)
    assert seen[-1].admitted == 1
    eng.poll_once(force=True)          # 同一源，ts 倒退
    obs = seen[-1]
    assert obs.admitted == 0, (
        f"同一 epoch 内的真实乱序没被拒（admitted={obs.admitted}）")
    assert obs.out_of_order_rejected == 1
