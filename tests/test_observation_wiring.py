"""WP02 回归：RoundObservationSet 必须真正可观测（不再只活在 poll_once 局部）。

上游任务书（2026-09-20 04:05 JST）WP02 验收：

> 测试 Tencent→Sina：REST/SSE 服务仍可健康，但 `volume_burst` 必须明确显示
> 缺 turnover，而不是只有 0 alerts。

本文件验证这条：切源后 `/api/status` 能读出 source / capability /
unavailable_capability，让人一眼看出"规则不可评估"而不是"这段时间没放量"。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from fakes import make_quote

from arad.capabilities import CAPABILITY_TABLE
from arad.engine import Engine, EngineState
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


class _Source:
    """可切换成败的源，名字决定 capability。"""

    def __init__(self, name: str, ok: bool = True):
        self.name = name
        self.ok = ok

    def universe(self):
        return []

    def snapshots(self, codes):
        if not self.ok:
            raise RuntimeError(f"{self.name} 故意失败")
        return [make_quote(code=c, price=10.0, volume_lots=200_000.0)
                for c in codes]

    def health(self):
        return {"name": self.name, "ok": self.ok, "latency_ms": 0, "err": ""}


class _Cap:
    name = "capture"

    def __init__(self):
        self.obs = []

    def evaluate(self, snap, ctx):
        self.obs.append(getattr(ctx, "observation", None))
        return []


def _engine(sources):
    from arad.engine import SourceManager, ROUTE_STOCKS
    rule = _Cap()
    mgr = SourceManager(sources, threshold=1)
    store = AlertStore(_settings(), calendar=_cal())
    eng = Engine(source=mgr, settings=_settings(), rules=[rule], notifiers=[],
                 calendar=_cal(), now_fn=lambda: NOW, store=store)
    eng._codes = ["600000"]
    eng._codes_pinned = True
    eng.watchlist = []
    eng.refresh_universe = lambda *a, **k: 0
    return eng, rule, store


# ---------------------------------------------------------------------------
# 1. observation 进入 Store / status
# ---------------------------------------------------------------------------
def test_observation_reaches_store_status():
    eng, rule, store = _engine([_Source("tencent")])
    eng.poll_once(force=True)

    st = store.status()
    assert "observation" in st, "status() 必须暴露 observation"
    obs = st["observation"]
    assert obs["source"] == "tencent"
    assert obs["requested"] >= 1
    assert obs["admitted"] == 1


def test_capabilities_visible_in_status():
    """status 里必须能看出当前源具备哪些能力。"""
    eng, rule, store = _engine([_Source("tencent")])
    eng.poll_once(force=True)

    caps = store.status()["observation"]["capabilities"]
    assert caps["source"] == "tencent"
    assert caps["turnover"] is True
    assert caps["depth_l5"] is True


def test_store_observation_property():
    eng, rule, store = _engine([_Source("tencent")])
    eng.poll_once(force=True)
    assert store.observation["admitted"] == 1


def test_observation_is_bounded_summary_not_per_stock():
    """不持久化逐股明细 —— 只有汇总计数（内存有界）。"""
    eng, rule, store = _engine([_Source("tencent")])
    eng._codes = [f"60000{i}" for i in range(5)]
    eng.poll_once(force=True)

    obs = store.status()["observation"]
    # 有汇总字段
    for k in ("requested", "returned", "admitted", "capabilities",
              "unknown_missing", "stale_rejected", "out_of_order_rejected"):
        assert k in obs, f"缺少 {k}"
    # decisions 明细**不**进 store（那是无界逐股数据）
    assert "decisions" not in obs


# ---------------------------------------------------------------------------
# 2. 核心验收：Tencent→Sina 后 capability 必须可见地下降
# ---------------------------------------------------------------------------
def test_tencent_to_sina_capability_drop_is_visible():
    """WP02 核心验收：切到 Sina 后，status 必须显示 turnover 不可用。

    服务仍然"健康"（HTTP 200、admitted>0），但 capability 明确下降 ——
    这正是"不能只看到 alerts=0"的含义。
    """
    tencent = _Source("tencent", ok=True)
    sina = _Source("sina", ok=True)
    eng, rule, store = _engine([tencent, sina])

    eng.poll_once(force=True)
    before = store.status()["observation"]
    assert before["source"] == "tencent"
    assert before["capabilities"]["turnover"] is True

    # 主源失败 -> 热备到 Sina（threshold=1 会正式切换）
    tencent.ok = False
    eng.poll_once(force=True)
    after = store.status()["observation"]

    assert after["source"] == "sina", f"应显示实际供数源，实际 {after['source']}"
    assert after["capabilities"]["turnover"] is False, "必须显示 turnover 不可用"
    assert after["capabilities"]["volume_ratio"] is False
    # 服务本身仍然健康：数据有返回、有准入
    assert after["admitted"] >= 1
    assert after["requested"] >= 1


def test_restoring_tencent_restores_capability():
    """切回 Tencent 后 capability 恢复可见。

    用 ``threshold=3`` 让它停在**热备**（备用源供数但主源未被正式替换）。
    ``threshold=1`` 会立刻 ``_switch()`` 把 idx 永久挪到 Sina，
    ``current`` 再也不回 Tencent —— 那是 SourceManager 的正确行为，
    但测不到"恢复"这条路径。
    """
    tencent = _Source("tencent", ok=False)
    sina = _Source("sina", ok=True)
    rule = _Cap()
    from arad.engine import SourceManager
    store = AlertStore(_settings(), calendar=_cal())
    eng = Engine(source=SourceManager([tencent, sina], threshold=3),
                 settings=_settings(), rules=[rule], notifiers=[],
                 calendar=_cal(), now_fn=lambda: NOW, store=store)
    eng._codes = ["600000"]
    eng._codes_pinned = True
    eng.watchlist = []
    eng.refresh_universe = lambda *a, **k: 0

    eng.poll_once(force=True)
    assert store.status()["observation"]["capabilities"]["turnover"] is False

    tencent.ok = True
    eng.poll_once(force=True)
    assert store.status()["observation"]["capabilities"]["turnover"] is True


def test_unavailable_capability_counted_in_status():
    """Sina 期间 volume_burst 的 unavailable 记账必须真的发生。

    注意：规则在 ``min_abs_pct``（默认 0.5%）就会提前 ``continue`` ——
    "没波动"比"缺字段"更早判定，这是既有语义、本轮不改。所以这里必须
    构造一只**真的在波动**的票，才能走到换手率门槛那一步。
    """
    from arad.rules.volume_burst import VolumeBurstRule

    rule = VolumeBurstRule()
    cap = _Cap()

    class _SinaMoving(_Source):
        """有波动的 Sina 行情：涨幅够、量够，但 turnover=0.0（源不提供）。"""

        def snapshots(self, codes):
            return [make_quote(code=c, price=10.30, prev_close=10.00,
                               open=10.00, high=10.40, low=9.98,
                               volume_lots=200_000.0, turnover=0.0,
                               volume_ratio=0.0) for c in codes]

    from arad.engine import SourceManager
    eng = Engine(source=SourceManager([_SinaMoving("sina")], threshold=1),
                 settings=_settings(), rules=[cap, rule], notifiers=[],
                 calendar=_cal(), now_fn=lambda: NOW,
                 store=AlertStore(_settings(), calendar=_cal()))
    eng._codes = ["600000"]
    eng._codes_pinned = True
    eng.watchlist = []
    eng.refresh_universe = lambda *a, **k: 0

    eng.poll_once(force=True)

    obs = cap.obs[-1]
    assert obs.capabilities.supports("turnover") is False
    assert obs.unavailable_capability >= 1, (
        "Sina 期间 volume_burst 必须记下缺 turnover，而不是无声无息")
    assert "600000" in obs.unavailable_codes
    assert any(d.reason == "turnover_not_provided" for d in obs.decisions)


def test_time_rejects_visible_in_status():
    """future/out-of-order 拒绝数必须能在 status 里读到。"""
    class _Future(_Source):
        def snapshots(self, codes):
            return [make_quote(code=c, price=10.0, volume_lots=200_000.0,
                               ts=NOW + timedelta(seconds=9999)) for c in codes]

    eng, rule, store = _engine([_Future("tencent")])
    eng.poll_once(force=True)

    obs = store.status()["observation"]
    assert obs["stale_rejected"] == 1
    assert obs["admitted"] == 0


def test_observation_survives_multiple_rounds():
    """多轮后 observation 反映最近一轮，不累加。"""
    eng, rule, store = _engine([_Source("tencent")])
    eng.poll_once(force=True)
    first = store.status()["observation"]["admitted"]
    eng.poll_once(force=True)
    second = store.status()["observation"]["admitted"]
    assert first == second == 1


def test_status_json_safe():
    """status() 必须能 JSON 序列化（REST/SSE 要发出去）。"""
    import json
    eng, rule, store = _engine([_Source("sina")])
    eng.poll_once(force=True)
    json.dumps(store.status(), ensure_ascii=False)
