"""IT-P0-002 回归：provider 事件时间必须**写前准入**。

缺陷
----
``EngineState.window()`` 的文档说是闭区间 ``[now-seconds, now]``，实现却只
过滤 ``p[0] >= cutoff``，没有上界。``update()`` 又先把 quote 写进
``quotes/history``，之后遇到 ``q.ts > now`` 只执行 ``pass`` —— 空操作，
数据其实已经污染了状态。

后果：provider 时间超前（或乱序）时，``price_change()`` 会算出真实世界还
没发生的涨幅，规则据此产出无中生有的告警。

修复
----
1. ``window()`` 补 ``<= now_epoch`` 上界，并按时间排序（防插入序倒挂）。
2. ``update()`` 写前判断：超前超过 ``FUTURE_TOLERANCE_SECONDS`` 整只丢弃；
   轻微超前夹到 now；迟到且比最后一点更旧的观测不顺延追加。
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta

import pytest

from fakes import make_quote

from arad.engine import FUTURE_TOLERANCE_SECONDS, EngineState

NOW = datetime(2026, 9, 15, 10, 30, 0)
EP = NOW.timestamp()


def _state_with(code: str, points) -> EngineState:
    st = EngineState()
    st.history[code] = deque(maxlen=4096)
    for p in points:
        st.history[code].append(tuple(p))
    return st


# ---------------------------------------------------------------------------
# window 上界
# ---------------------------------------------------------------------------
def test_window_excludes_future_points():
    """未来点不得进入 ``[now-seconds, now]`` 窗口。"""
    st = _state_with("600000", [(EP + 600.0, 10.0, 1000.0), (EP, 10.5, 1200.0)])
    w = st.window("600000", 3600.0, EP)
    assert [p[0] for p in w] == [EP]
    assert all(p[0] <= EP for p in w)


def test_window_still_honours_lower_bound():
    """下界不能被上界修复破坏：太旧的点仍要排除。"""
    st = _state_with("600000", [(EP - 7200.0, 9.0, 100.0), (EP, 10.0, 200.0)])
    w = st.window("600000", 3600.0, EP)
    assert [p[0] for p in w] == [EP]


def test_window_is_time_sorted_even_if_inserted_out_of_order():
    """窗口必须按时间升序返回，即使 deque 是乱序插入的。"""
    st = _state_with("600000", [
        (EP, 10.0, 1000.0),           # 先插"现在"
        (EP - 120.0, 8.0, 900.0),     # 再插"迟到两分钟"
    ])
    w = st.window("600000", 3600.0, EP)
    assert [p[0] for p in w] == [EP - 120.0, EP]
    # 首尾正确 => 涨跌幅方向正确（8.0 -> 10.0 = +25%）
    assert st.price_change("600000", 3600.0, EP) == pytest.approx(25.0)


def test_price_change_ignores_future_spike():
    """未来点造出的假涨幅不能再出现在 price_change 里。"""
    st = _state_with("600001", [(EP, 10.0, 1000.0), (EP + 300.0, 13.0, 5000.0)])
    assert st.price_change("600001", 3600.0, EP) is None   # 只剩 1 个有效点


def test_volume_delta_ignores_future_points():
    st = _state_with("600001", [(EP, 10.0, 1000.0), (EP + 300.0, 10.0, 9000.0)])
    assert st.volume_delta("600001", 3600.0, EP) == 0.0


def test_peak_and_trough_ignore_future():
    st = _state_with("600001", [(EP, 10.0, 1.0), (EP + 60.0, 99.0, 2.0)])
    assert st.peak("600001", 3600.0, EP) == pytest.approx(10.0)
    assert st.trough("600001", 3600.0, EP) == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# update 写前准入
# ---------------------------------------------------------------------------
def test_update_rejects_far_future_quote():
    """超前远超容差的观测：整只丢弃，绝不写入任何状态。"""
    st = EngineState()
    q = make_quote(code="600002", price=99.0,
                   ts=NOW + timedelta(seconds=FUTURE_TOLERANCE_SECONDS + 60))
    st.update([q], NOW)
    assert "600002" not in st.quotes
    assert "600002" not in st.history
    assert "600002" not in st.last_price
    assert st.stats.get("t_reject:future") == 1


def test_update_accepts_slightly_future_and_clamps_to_now():
    """轻微超前（时钟抖动内）保留数据，但时间戳夹到 now，不进未来窗口。"""
    st = EngineState()
    q = make_quote(code="600003", price=10.0,
                   ts=NOW + timedelta(seconds=5))
    st.update([q], NOW)
    assert "600003" in st.quotes
    h = st.history["600003"]
    assert len(h) == 1
    assert h[-1][0] == pytest.approx(EP)          # 夹到 now
    assert st.window("600003", 60.0, EP)          # 现在能查到


def test_update_without_ts_is_treated_as_on_time():
    """多数 source 不填 ts —— 必须按接收时间处理，行为与改动前一致。"""
    st = EngineState()
    q = make_quote(code="600004", price=10.0)     # ts 缺省 None
    st.update([q], NOW)
    assert "600004" in st.quotes
    assert st.history["600004"][-1][0] == pytest.approx(EP)


def test_update_does_not_append_out_of_order_point():
    """迟到且比最后一点更旧的观测不顺延追加（否则窗口首尾倒挂）。"""
    st = EngineState()
    st.update([make_quote(code="600005", price=10.0, ts=NOW)], NOW)
    later = NOW + timedelta(seconds=60)
    st.update([make_quote(code="600005", price=10.5, ts=later)], later)
    # 再补一个"迟到"的点（时间早于已记录的最后一点）
    st.update([make_quote(code="600005", price=8.0,
                          ts=NOW + timedelta(seconds=10))], later)
    h = st.history["600005"]
    assert [p[0] for p in h] == [EP, later.timestamp()]     # 没被插到尾部
    assert st.stats.get("t_reject:out_of_order") == 1
    # 且窗口内首尾按时间序
    w = st.window("600005", 3600.0, later.timestamp())
    assert [p[1] for p in w] == [10.0, 10.5]


def test_update_keeps_normal_progression():
    """正常时间推进必须照常入库，不被准入逻辑误杀。"""
    st = EngineState()
    for i in range(5):
        t = NOW + timedelta(seconds=i * 30)
        st.update([make_quote(code="600006", price=10.0 + i * 0.1, ts=t)], t)
    h = st.history["600006"]
    assert len(h) == 5
    times = [p[0] for p in h]
    assert times == sorted(times)


def test_update_rejects_future_but_keeps_good_ones():
    """一只出问题不能连累整轮其它标的。"""
    st = EngineState()
    bad = make_quote(code="600007", price=99.0,
                     ts=NOW + timedelta(seconds=9999))
    good = make_quote(code="600008", price=10.0, ts=NOW)
    st.update([bad, good], NOW)
    assert "600007" not in st.quotes
    assert "600008" in st.quotes


def test_future_quote_does_not_poison_price_change():
    """端到端：喂进超前数据后，price_change 不得出现虚假涨幅。"""
    st = EngineState()
    st.update([make_quote(code="600009", price=10.0, ts=NOW)], NOW)
    later = NOW + timedelta(seconds=60)
    st.update([make_quote(code="600009", price=10.1, ts=later)], later)
    # provider 突然给一个超前的暴涨点
    st.update([make_quote(code="600009", price=13.0,
                          ts=later + timedelta(seconds=9999))], later)
    pc = st.price_change("600009", 3600.0, later.timestamp())
    assert pc == pytest.approx(1.0, abs=0.01)      # 10.0 -> 10.1，不是 +30%


def test_tolerance_boundary():
    """容差边界：正好等于容差算可接受，超过一点就丢弃。"""
    st = EngineState()
    at_limit = make_quote(code="600010", price=10.0,
                          ts=NOW + timedelta(seconds=FUTURE_TOLERANCE_SECONDS))
    st.update([at_limit], NOW)
    assert "600010" in st.quotes                    # <= 容差 -> 留

    st2 = EngineState()
    over = make_quote(code="600011", price=10.0,
                      ts=NOW + timedelta(seconds=FUTURE_TOLERANCE_SECONDS + 0.001))
    st2.update([over], NOW)
    assert "600011" not in st2.quotes               # > 容差 -> 弃
