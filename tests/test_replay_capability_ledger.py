"""IT-P1-EVAL-PUBLISH-001 的**用户可见**回归：演练模式下功能不能比真实源还少。

背景（IT-P1-EVAL-PUBLISH-001-R3）：``CAPABILITY_TABLE`` 缺 ``replay`` 条目时，
``capabilities_for("replay")`` 回落到"全 False 未知源"，于是 ``volume_burst``
把 turnover 当硬依赖 -> 每只票都 blocked -> **放量告警一条都不出**。

危害是产品性的：看板 ``serve --replay``（演练模式）正是用户确认"短线精灵/
放量功能到底有没有"的地方。功能在演练里静默消失，用户会以为功能没做。

本文件钉住三件事：
1. ``replay`` 声明提供了 volume_burst 的硬依赖（turnover）；
2. 演练回放**真的产出**放量告警（不是 0 条）；
3. 交付账本里的 signal_id 与真实告警对得上（不靠 title 猜）。
"""
from __future__ import annotations

import pytest

from arad.capabilities import capabilities_for
from arad.config import load_settings
from arad.engine import build_rules
from arad.replay import Replay, default_universe
from arad.session import SessionPhase, TradingCalendar
from arad.store import AlertStore


def _run_replay(stocks: int = 30, minutes: int = 90):
    """跑完整场合成回放，返回 (store, engine, 录到的告警 dict 列表)。"""
    st = load_settings(use_cache=False)
    cal = TradingCalendar(holidays=set())
    cal.phase = lambda now=None: SessionPhase.MORNING      # type: ignore[method-assign]
    store = AlertStore(st, calendar=cal)
    rp = Replay(default_universe(stocks), seed=42, minutes=minutes, settings=st,
                rules=build_rules(st), notifiers=[], store=store)
    eng = rp.build_engine()
    eng._codes = [s.code for s in default_universe(stocks)]
    for ts in rp.timeline:
        rp.clock.set(ts)
        eng.poll_once(force=True)
    return store, eng, list(store.recent_alerts(1000))


def test_replay_provides_volume_burst_hard_dependency():
    """顺序前提：replay 必须被认成"提供 turnover"，否则规则整类静默。"""
    caps = capabilities_for("replay")
    assert caps.supports("turnover") is True
    assert caps.supports("volume_ratio") is True


def test_replay_mode_actually_emits_volume_burst_alerts():
    """**用户可见的行为级验牙**：演练模式必须真的报出放量告警。

    修复前这里是 0 条 —— 而 0 条恰好是"功能没做"的表现，用户无从分辨。
    """
    store, eng, rows = _run_replay()
    kinds = {}
    for r in rows:
        kinds[r.get("kind")] = kinds.get(r.get("kind"), 0) + 1
    assert rows, "演练回放必须产出告警"
    assert kinds.get("volume_burst", 0) > 0, (
        f"演练模式放量告警为 0 —— 功能在演练里静默消失（实际 kinds={kinds}）")
    assert kinds.get("surge", 0) > 0, "急拉必须有"


def test_replay_alerts_carry_signal_id_matching_real_counts():
    """交付账本与真实告警按 ``signal_id`` 精确对账（不靠 title 猜）。

    只有维护账本的规则才带 ``signal_id``；其余如实为空，不得伪造。
    逐轮账本的 ``committed`` 合计必须等于带该 ``signal_id`` 的真实告警条数。
    """
    st = load_settings(use_cache=False)
    cal = TradingCalendar(holidays=set())
    cal.phase = lambda now=None: SessionPhase.MORNING      # type: ignore[method-assign]
    store = AlertStore(st, calendar=cal)

    captured = []
    real = store.set_poll_stats

    def spy(*a, **kw):
        obs = kw.get("observation")
        if obs is not None:
            fn = getattr(obs, "as_dict", None)
            if callable(fn):
                captured.append(fn())
        return real(*a, **kw)

    store.set_poll_stats = spy                            # type: ignore[method-assign]
    rp = Replay(default_universe(30), seed=42, minutes=90, settings=st,
                rules=build_rules(st), notifiers=[], store=store)
    eng = rp.build_engine()
    assert eng.store is store, "引擎必须写同一个 store，否则对账无意义"
    eng._codes = [s.code for s in default_universe(30)]
    for ts in rp.timeline:
        rp.clock.set(ts)
        eng.poll_once(force=True)

    assert captured, "必须捕获到逐轮账本"

    # 逐轮交付阶段链
    chain_bad, committed_total = [], 0
    for obs in captured:
        for sig, c in (obs.get("signal_evaluability") or {}).items():
            c = c or {}
            hit = int(c.get("hit_candidates") or 0)
            sel = int(c.get("rule_selected") or 0)
            bus = int(c.get("bus_accepted") or 0)
            cmt = int(c.get("committed") or 0)
            committed_total += cmt
            if not (hit >= sel >= bus >= cmt):
                chain_bad.append((sig, hit, sel, bus, cmt))
    assert chain_bad == [], f"交付阶段链被打破：{chain_bad[:5]}"

    rows = list(store.recent_alerts(1000))
    tagged = {}
    untagged = 0
    for r in rows:
        sid = str(r.get("signal_id") or "")
        if sid:
            tagged[sid] = tagged.get(sid, 0) + 1
        else:
            untagged += 1

    assert sum(tagged.values()) == committed_total, (
        f"账本 committed={committed_total} 与真实带 signal_id 告警"
        f"{sum(tagged.values())} 不符（tagged={tagged}）")
    # 不维护账本的规则必须如实为空，不得伪造 signal_id
    assert untagged > 0, "limit_board/tick_surge 等不维护账本，应为空 signal_id"
    assert all(r.get("signal_id") is not None for r in rows), (
        "signal_id 字段必须存在（空串表示不参与账本），不能缺键")


def test_alert_signal_id_is_empty_by_default_not_fabricated():
    """默认必须是空串 —— 不得猜一个 signal 名填上。"""
    from fakes import make_alert

    assert make_alert().signal_id == ""
