"""WP02：``live_session.py`` 必须暴露**本轮**观测，而不是累计缓存规模。

缺陷（IT-P2-OBS-001）
--------------------
`_soak_loop()` 以前把 `len(state.quotes)` 当"行情数"写进报告。那是**累计
缓存**的 key 数：provider 本轮一只都没返回时它照样是几千，
`len(engine._codes)` 也只是"配置里想抓多少"。于是报告看不出：

- 本轮请求了多少、真的返回了多少（requested / returned / coverage）；
- 有多少被时间准入拒掉（future / out-of-order）；
- 当前是哪家源在供数、它具备哪些能力；
- 规则因**能力缺失**而无法评估的有多少（而不是"没放量"）。

本文件锁定新字段的语义与向后兼容性。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "live_session.py"

_SPEC = importlib.util.spec_from_file_location("live_session_tool_obs", TOOL)
assert _SPEC and _SPEC.loader
ls = importlib.util.module_from_spec(_SPEC)
sys.modules["live_session_tool_obs"] = ls
_SPEC.loader.exec_module(ls)


def _obs(**over) -> dict:
    """一份典型的 RoundObservationSet.as_dict()。"""
    base = {
        "source": "sina",
        "capabilities": {"source": "sina", "turnover": False,
                         "volume_ratio": False, "depth_l1": True,
                         "depth_l5": False, "outer_inner": False,
                         "float_cap": False, "provider_time": False},
        "requested": 100,
        "returned": 97,
        "admitted": 95,
        "coverage": 0.97,
        "unknown_missing": ["000002", "600000", "600001"],
        "stale_rejected": 1,
        "out_of_order_rejected": 1,
        "unavailable_capability": 4,
    }
    base.update(over)
    return base


def _sample(**over) -> dict:
    kw = dict(index=1, latency_ms=400.0, alerts=(), error=False, quotes=5000,
              universe=100, history_points=10, history_codes=10, max_deque=1,
              history_maxlen=360, watchlist_only=False)
    kw.update(over)
    return ls.make_round_sample(**kw)


# ---------------------------------------------------------------------------
# 1. 有 observation 时必须并入真实计数
# ---------------------------------------------------------------------------
def test_observation_fields_present():
    s = _sample(observation=_obs())
    assert s["requested"] == 100
    assert s["returned"] == 97
    assert s["admitted"] == 95
    assert s["coverage"] == pytest.approx(0.97)
    assert s["source"] == "sina"


def test_capability_set_exposed():
    s = _sample(observation=_obs())
    caps = s["capabilities"]
    assert caps["source"] == "sina"
    assert caps["turnover"] is False
    assert caps["depth_l1"] is True


def test_time_rejects_exposed():
    s = _sample(observation=_obs(stale_rejected=3, out_of_order_rejected=2))
    assert s["future_rejected"] == 3
    assert s["out_of_order_rejected"] == 2


def test_unknown_missing_counted_not_listed():
    """只有计数进报告 —— 逐股明细不进（内存有界）。"""
    s = _sample(observation=_obs())
    assert s["unknown_missing"] == 3
    assert not isinstance(s["unknown_missing"], list)


def test_unavailable_capability_exposed():
    s = _sample(observation=_obs(unavailable_capability=7))
    assert s["unavailable_capability"] == 7


# ---------------------------------------------------------------------------
# 2. 向后兼容：没有 observation 时保持旧形状
# ---------------------------------------------------------------------------
def test_no_observation_keeps_legacy_shape():
    """离线造样本不传 observation 时，不得凭空多出字段。"""
    s = _sample()
    for k in ("requested", "returned", "admitted", "coverage", "source",
              "future_rejected", "out_of_order_rejected", "capabilities"):
        assert k not in s, f"没有 observation 时不应有 {k}"
    # 旧字段仍在
    assert s["quotes"] == 5000
    assert s["universe"] == 100


def test_empty_observation_dict_is_treated_as_absent():
    s = _sample(observation={})
    assert "requested" not in s
    assert s["quotes"] == 5000


def test_none_observation_is_safe():
    s = _sample(observation=None)
    assert s["quotes"] == 5000


# ---------------------------------------------------------------------------
# 3. 容错：脏数据不得炸掉 soak
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [
    {"requested": None, "returned": None, "admitted": None},
    {"requested": "x", "returned": "y", "admitted": "z"},
    {"coverage": None, "source": None},
    {"stale_rejected": None, "out_of_order_rejected": None},
    {"unknown_missing": None, "unavailable_capability": None},
    {"capabilities": None},
])
def test_dirty_observation_does_not_crash(bad):
    """可观测性字段脏了也不能让 soak 崩 —— 报告宁可少几项。"""
    obs = _obs()
    obs.update(bad)
    s = _sample(observation=obs)          # 不抛即通过
    assert s["index"] == 1


def test_observation_does_not_break_existing_consumers():
    """新字段不得破坏 finalize_metrics / evaluate_health。"""
    rounds = [_sample(index=i + 1, observation=_obs()) for i in range(3)]
    metrics = ls.finalize_metrics(
        rounds,
        api={"requests": 5, "non_200": 0, "malformed": 0, "unreachable": 0},
        sse={"connected": True, "stalled": False, "events_total": 3,
             "events_by_type": {"bye": 1}},
        logs={"WARNING": 0, "ERROR": 0, "CRITICAL": 0},
        browser={"ran": False, "skipped": True, "errors": []},
    )
    assert isinstance(metrics, dict)
    ls.evaluate_health(metrics)           # 不抛即通过


def test_round_with_observation_is_json_safe():
    import json
    s = _sample(observation=_obs())
    json.dumps(s, ensure_ascii=False)
