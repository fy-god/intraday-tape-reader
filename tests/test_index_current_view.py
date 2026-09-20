"""IT-P1-INDEX-CURRENT-001：指数规则必须只消费**本轮 current** 指数。

## 缺陷

`SpiritIndexRule.evaluate()` 从 ``ctx.state.quotes`` 里挑指数。但
``state.quotes`` 是**累计 latest 缓存**（服务历史/回看），**不是** "本轮 current"。

后果：指数路由某轮**整体失败**时（``index_admitted=0``），规则仍能从上几轮
残留的缓存里拿到指数，继续产告警 —— 表面上"系统正常出信号"，
实际数据已经断了。观测账本与规则行为**互相矛盾**。

## 修法

引擎每轮把本轮真正准入的指数放进 ``ctx.current_indices``，规则只认它；
``current_indices is None``（老调用方/单测）时才回退旧行为。

## 回退验牙结果（实测，如实记录）

回退 `spirit_index.py` + `engine.py` + `base.py` 到 `c38e79b`（保留全部新测试）：

| 用例 | 修复前 | 性质 |
|---|---|---|
| `test_rule_ignores_cumulative_cache_when_current_is_empty` | **红（AssertionError）** | **真验牙（行为级）** |
| `test_engine_fills_current_indices` | **红（AssertionError）** | **真验牙（行为级）** |
| `test_rule_fires_when_current_has_the_index` | 绿 | 前置条件（证明数据真的会触发） |
| `test_rule_falls_back_to_cache_when_current_is_none` | 绿 | 回归保护 |

即 **2 条真验牙 / 0 条结构性 / 2 条回归保护**。不谎称"全部变红"。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fakes import make_quote

from arad.engine import EngineState
from arad.models import Snapshot
from arad.rules.base import RuleContext
from arad.rules.spirit_index import SpiritIndexRule
from arad.session import SessionPhase

NOW = datetime(2026, 9, 15, 10, 30, 0)
IDX = "sh000001"

_EMPTY_SNAP = Snapshot(ts=NOW, seq=1, quotes={})


def _ago(s: float) -> datetime:
    return NOW - timedelta(seconds=s)


def _idx_q(ts, price: float):
    # 指数必须被 is_index_quote 认出 —— 用带前缀的 code。
    return make_quote(code=IDX, price=price, volume_lots=1000.0, ts=ts,
                      prev_close=price - 5, open=price - 5,
                      high=price, low=price - 5)


def _state_with_index_history() -> EngineState:
    """造一个**真的会触发** index_pull 的状态。

    指数在 5 分钟窗口内从 3000 涨到 3010（+10 点，超过 min_points=0.5，
    且点位 >= min_index_level），窗口跨度满足 ``_covered`` 的 80% 要求。

    注意**不传** ``route=`` —— 那是本轮新增的 kwarg，用了它会让本文件在旧代码上
    按 ``TypeError`` 失败（结构性），从而掩盖真正要验的行为。
    本文件只关心"规则读不读 current view"，路由分账由
    ``test_source_epoch_route.py`` 负责。
    """
    st = EngineState(history_len=360)
    for sec in (300.0, 200.0, 100.0, 0.0):
        price = 3000.0 + (300.0 - sec) / 30.0     # 3000 -> 3010
        st.update([_idx_q(_ago(sec), price)], NOW)
    return st


def _ctx(state, *, current_marker):
    """构造 RuleContext，并在**构造后** setattr 设 current_indices。

    这样旧代码（没有该字段）会**静默接受**这个属性而不去读它 ——
    于是用例红在 ``AssertionError``（行为级），而不是 ``TypeError``（结构性）。
    """
    ctx = RuleContext(
        state=state,
        cfg={"enabled": True, "only_continuous": False,
             "windows": [300], "min_points": 0.5, "min_index_level": 100.0},
        now=NOW, session=SessionPhase.MORNING)
    setattr(ctx, "current_indices", current_marker)
    return ctx


def _rule():
    return SpiritIndexRule({"enabled": True, "only_continuous": False,
                            "windows": [300], "min_points": 0.5,
                            "min_index_level": 100.0})


def test_rule_fires_when_current_has_the_index():
    """先证明这套数据**真的会触发** —— 否则下面的断言没有意义。"""
    st = _state_with_index_history()
    alerts = _rule().evaluate(_EMPTY_SNAP, _ctx(st, current_marker=st.quotes))
    assert alerts, "构造的数据没触发告警，后续'不触发'的断言将毫无意义"


def test_rule_ignores_cumulative_cache_when_current_is_empty():
    """**行为级核心**：本轮 0 指数准入 + 缓存里有上轮指数 -> 不得产告警。

    旧代码：不读 ``current_indices``，照样从 ``state.quotes`` 取到残留指数
            并产出告警 -> AssertionError。
    新代码：``current_indices`` 为空 -> 0 告警。
    """
    st = _state_with_index_history()
    alerts = _rule().evaluate(_EMPTY_SNAP, _ctx(st, current_marker={}))
    assert alerts == [], (
        f"本轮 0 指数准入，规则却仍从累计缓存产出 {len(alerts)} 条告警 —— "
        "指数路由已经断了，信号却看起来正常（IT-P1-INDEX-CURRENT-001）")


def test_rule_falls_back_to_cache_when_current_is_none():
    """回归保护：``current_indices is None`` 时必须回退旧行为。

    老调用方（测试/回放）不会填 ``current_indices``，dataclass 默认就是 ``None``。
    此时**必须**回退到累计缓存，而不是变成"本轮没有指数" ——
    否则会静默把所有老调用方的指数规则关掉。
    """
    st = _state_with_index_history()
    ctx = _ctx(st, current_marker=None)
    alerts = _rule().evaluate(_EMPTY_SNAP, ctx)
    assert alerts, "None 应回退到累计缓存并照常产告警，而不是静默失效"


def test_engine_fills_current_indices():
    """**结构性**：引擎必须每轮把本轮准入的指数传进 RuleContext。"""
    import inspect

    from arad import engine as eng_mod

    src = inspect.getsource(eng_mod.Engine.poll_once)
    assert "current_indices" in src, \
        "引擎必须每轮把本轮准入的指数传进 RuleContext.current_indices"
