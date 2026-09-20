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


# ===========================================================================
# WP05 / IT-P1-OBS-011：summarize_rounds 必须聚合观测账本，evaluate_health
# 必须能用它抓住 soft-partial 与 capability degradation。
#
# 修复前的行为（红测证据）：
#   summarize_rounds() 的返回键里**完全没有** coverage / * _total / source_mix /
#   capability_unavailable* / worst_rounds；于是 4 轮 coverage 全 0.93 的
#   soft-partial 得到 healthy=True / exit 0，逐项检查表里连一行都没有。
# ===========================================================================
def _healthy_metrics(rounds: list[dict], **over) -> dict:
    """给一组轮样本配上一份"其余项全绿"的 metrics，单独暴露观测判定。"""
    m = ls.finalize_metrics(
        rounds,
        api={"requests": 20, "non_200": 0, "malformed": 0, "unreachable": 0},
        sse={"connected": True, "stalled": False, "events_total": 15,
             "events_by_type": {"tick": 5, "bye": 1}},
        logs={"WARNING": 0, "ERROR": 0, "CRITICAL": 0},
        browser={"ran": False, "skipped": True, "errors": []},
    )
    m.update(over)
    return m


def _obs_round(i: int, *, coverage: float, requested: int = 100,
               source: str = "tencent", **over) -> dict:
    """一轮"带观测账本"的样本：returned 由 coverage 反推，保证自洽。

    注意 ``_obs()`` 的基线里 ``unavailable_capability=4`` —— 那是"典型 sina 轮"的
    样子。判定类测试要的是"其余项全绿"，所以这里把默认值压到 0，需要能力的
    测试显式传。不这么做，每个 coverage 测试都会被 capability 项污染。
    """
    base = dict(unavailable_capability=0, unknown_missing=[], rejected_quality=[],
                future_rejected=0, out_of_order_rejected=0)
    base.update(over)
    obs = _obs(source=source, requested=requested,
               returned=int(round(coverage * requested)),
               admitted=int(round(coverage * requested)), coverage=coverage)
    obs.update(base)
    return _sample(index=i, observation=obs)


def _check(verdict: dict, name: str) -> dict:
    hits = [c for c in verdict["checks"] if c["name"] == name]
    assert hits, f"verdict 里必须有 {name} 检查项，实际：{[c['name'] for c in verdict['checks']]}"
    return hits[0]


class TestObservationAggregation:
    """6 组聚合：分位、总量、拒绝构成、source mix、能力缺失、最差 N 轮。"""

    def test_coverage_percentiles_not_just_mean(self):
        """要求 1：coverage p05 / p50 / min —— 均值会掩盖坏轮次。"""
        rows = [_obs_round(i + 1, coverage=c)
                for i, c in enumerate([0.99, 0.99, 0.99, 0.99, 0.99, 0.40])]
        got = ls.summarize_rounds(rows)
        assert got["coverage_rounds"] == 6
        assert got["coverage_min"] == pytest.approx(0.40)
        assert got["coverage_p50"] == pytest.approx(0.99)
        # p05 必须被那一轮塌方拖下去；均值(0.8917)反而看不出来
        assert got["coverage_p05"] < 0.95
        assert got["coverage_min"] < got["coverage_p50"]

    def test_all_93_percent_is_visible_not_silent(self):
        """要求 1 的核心场景：全是 93% 的 soft-partial 必须在分位里现形。"""
        rows = [_obs_round(i + 1, coverage=0.93) for i in range(6)]
        got = ls.summarize_rounds(rows)
        assert got["coverage_p05"] == pytest.approx(0.93)
        assert got["coverage_p50"] == pytest.approx(0.93)
        assert got["coverage_min"] == pytest.approx(0.93)

    def test_totals_summed(self):
        """要求 1：requested / returned / admitted 总计。"""
        rows = [_obs_round(i + 1, coverage=1.0, requested=100) for i in range(3)]
        got = ls.summarize_rounds(rows)
        assert got["requested_total"] == 300
        assert got["returned_total"] == 300
        assert got["admitted_total"] == 300

        mixed = [_obs_round(1, coverage=1.0, requested=100),
                 _obs_round(2, coverage=0.5, requested=100),
                 _obs_round(3, coverage=0.25, requested=200)]
        got2 = ls.summarize_rounds(mixed)
        assert got2["requested_total"] == 400
        assert got2["returned_total"] == 100 + 50 + 50
        assert got2["admitted_total"] == 100 + 50 + 50

    def test_rejection_totals(self):
        """要求 4：missing / quality / future / stale / ooo 拒绝数总计。"""
        rows = [
            _obs_round(1, coverage=0.9, unknown_missing=["a", "b"],
                       rejected_quality=["c"], future_rejected=1,
                       out_of_order_rejected=2),
            _obs_round(2, coverage=0.9, unknown_missing=["d"],
                       rejected_quality=[], future_rejected=3,
                       out_of_order_rejected=0),
            _obs_round(3, coverage=0.9, unknown_missing=[],
                       rejected_quality=["e", "f", "g"], future_rejected=0,
                       out_of_order_rejected=5),
        ]
        got = ls.summarize_rounds(rows)
        assert got["missing_total"] == 3
        assert got["quality_total"] == 4
        assert got["future_total"] == 4
        assert got["ooo_total"] == 7

    def test_legacy_stale_key_counted_separately(self):
        """旧样本只有 stale_rejected（旧名 = 未来拒绝）：只计一次，不双计。"""
        old = {"requested": 100, "returned": 90, "admitted": 90, "coverage": 0.9,
               "stale_rejected": 6, "out_of_order_rejected": 1,
               "unknown_missing": [], "rejected_quality": []}
        got = ls.summarize_rounds([_sample(index=1, observation=old)])
        assert got["future_total"] == 6      # 旧名当未来拒绝读（IT-P1-OBS-006）
        assert got["stale_total"] == 6       # 但也要能看出它来自旧的 stale 键
        assert got["ooo_total"] == 1

    def test_source_mix(self):
        """要求 5：source mix 正确（含未知来源单独记）。"""
        rows = [_obs_round(1, coverage=1.0, source="tencent"),
                _obs_round(2, coverage=1.0, source="tencent"),
                _obs_round(3, coverage=1.0, source="sina"),
                _obs_round(4, coverage=1.0, source="")]
        got = ls.summarize_rounds(rows)
        assert got["source_mix"] == {"tencent": 2, "sina": 1}
        assert got["source_mix_rounds"] == 3
        assert got["source_mix_unknown_rounds"] == 1

    def test_capability_unavailable_totals(self):
        """要求 1：capability-unavailable 总计 + 轮覆盖率。"""
        rows = [
            _obs_round(1, coverage=1.0, unavailable_capability=3,
                       unavailable_by_reason={"turnover_not_provided": 3}),
            _obs_round(2, coverage=1.0, unavailable_capability=4,
                       unavailable_by_reason={"turnover_not_provided": 2,
                                              "volume_ratio_not_provided": 2}),
            _obs_round(3, coverage=1.0, unavailable_capability=0),
        ]
        got = ls.summarize_rounds(rows)
        assert got["capability_unavailable_total"] == 7
        assert got["capability_unavailable_rounds"] == 2
        assert got["capability_unavailable_ratio"] == pytest.approx(2 / 3, abs=1e-4)
        assert got["unavailable_by_reason"] == {"turnover_not_provided": 5,
                                                "volume_ratio_not_provided": 2}
        # 能力缺失要能看出是"哪个能力"整类缺：sina 不提供 turnover/volume_ratio
        assert got["capability_missing_rounds"]["turnover"] == 3
        assert got["capability_missing_rounds"]["depth_l5"] == 3

    def test_worst_rounds_sorted_by_coverage_with_index(self):
        """要求 6：最差 N 轮按 coverage 升序，且轮号正确（便于回查原始样本）。"""
        rows = [_obs_round(1, coverage=0.99),
                _obs_round(2, coverage=0.93),
                _obs_round(3, coverage=0.80),
                _obs_round(4, coverage=0.97),
                _obs_round(5, coverage=0.60)]
        got = ls.summarize_rounds(rows, worst_n=3)
        assert [r["index"] for r in got["worst_rounds"]] == [5, 3, 2]
        assert [r["coverage"] for r in got["worst_rounds"]] == [0.60, 0.80, 0.93]
        assert got["worst_rounds"][0]["requested"] == 100
        assert got["worst_rounds"][0]["source"] == "tencent"

    def test_worst_rounds_includes_rejection_breakdown(self):
        """最差轮必须能直接看到那一轮为什么差（拒绝构成）。"""
        rows = [_obs_round(1, coverage=1.0),
                _obs_round(2, coverage=0.7, unknown_missing=["a", "b", "c"],
                           rejected_quality=["d"], future_rejected=2,
                           out_of_order_rejected=1, unavailable_capability=5)]
        got = ls.summarize_rounds(rows, worst_n=1)
        w = got["worst_rounds"][0]
        assert w["index"] == 2
        assert (w["missing"], w["quality"], w["future"], w["ooo"],
                w["unavailable_capability"]) == (3, 1, 2, 1, 5)

    def test_worst_n_default_is_bounded(self):
        """默认 N 有上限，否则最差轮列表会退化成逐轮日志。"""
        rows = [_obs_round(i + 1, coverage=0.5 + i * 0.01) for i in range(50)]
        got = ls.summarize_rounds(rows)
        assert len(got["worst_rounds"]) == ls.DEFAULT_WORST_ROUNDS
        assert got["rounds"] == 50


class TestObservationHealth:
    """要求 2：可配置阈值；93% soft-partial 与整类不可评估必须显式变黄/红。"""

    def test_soft_partial_93_is_not_silent(self):
        """红测核心 1：coverage 全 93% 时健康判定不得静默通过。

        修复前：``metrics`` 里连 ``coverage_p05`` 都不存在，verdict 里也没有
        ``coverage`` 这一项 —— healthy=True / exit 0，93% 完全隐形。
        """
        rows = [_obs_round(i + 1, coverage=0.93) for i in range(6)]
        v = ls.evaluate_health(_healthy_metrics(rows))
        cov = _check(v, "coverage")
        # 93% 落在 warn 区间（低于 warn 阈值 95%/97%），必须显式黄
        assert cov["level"] == "warn", cov
        assert cov["ok"] is True, "warn 不判死，但必须出现"
        assert "coverage" in v["warn"]
        assert "93.0%" in cov["detail"]

    def test_soft_partial_93_escalates_to_fail_when_strict(self):
        """同一份 93% 数据，严格档必须能升级成 FAIL —— 阈值真的可配置。"""
        rows = [_obs_round(i + 1, coverage=0.93) for i in range(6)]
        v = ls.evaluate_health(_healthy_metrics(rows), tolerances={
            "coverage_warn_p05": 0.99, "coverage_fail_p05": 0.99,
            "coverage_warn_p50": 0.99, "coverage_fail_p50": 0.99,
            "coverage_warn_min": 0.99, "coverage_fail_min": 0.99,
        })
        assert _check(v, "coverage")["level"] == "fail"
        assert v["healthy"] is False and v["exit_code"] == 1

    def test_coverage_100_is_healthy(self):
        """红测核心 2（回归保护）：全 100% 必须 PASS。"""
        rows = [_obs_round(i + 1, coverage=1.0) for i in range(5)]
        v = ls.evaluate_health(_healthy_metrics(rows))
        cov = _check(v, "coverage")
        assert cov["ok"] is True and cov["level"] == "ok"
        assert v["healthy"] is True and v["exit_code"] == 0

    def test_normal_jitter_stays_green(self):
        """回归保护：真实抖动不该被判黄。实测 tencent 健康轮 = 99.91%。"""
        rows = [_obs_round(i + 1, coverage=c)
                for i, c in enumerate([0.9991, 0.995, 0.99, 0.985, 0.99, 0.998])]
        v = ls.evaluate_health(_healthy_metrics(rows))
        assert _check(v, "coverage")["level"] == "ok"
        assert v["healthy"] is True

    def test_measured_real_coverage_is_green(self):
        """真实锚点回归：全市场实测 requested=5569/returned=5564 -> 0.9991。"""
        rows = [_obs_round(1, coverage=5564 / 5569, requested=5569),
                _obs_round(2, coverage=5564 / 5569, requested=5569),
                _obs_round(3, coverage=5564 / 5569, requested=5569)]
        got = ls.summarize_rounds(rows)
        assert got["coverage_p50"] == pytest.approx(0.9991, abs=1e-4)
        v = ls.evaluate_health(_healthy_metrics(rows))
        assert _check(v, "coverage")["level"] == "ok"
        assert v["healthy"] is True

    def test_coverage_collapse_is_fail(self):
        """单轮塌方（min 40%）必须红：均值抓不住它，min 能。"""
        rows = [_obs_round(i + 1, coverage=c)
                for i, c in enumerate([0.99] * 5 + [0.40])]
        v = ls.evaluate_health(_healthy_metrics(rows))
        cov = _check(v, "coverage")
        assert cov["level"] == "fail"
        assert cov["ok"] is False
        assert v["healthy"] is False and v["exit_code"] == 1
        assert "coverage" in v["fail"]

    def test_capability_whole_class_unavailable_is_not_green(self):
        """红测核心 3：所有轮 unavailable_capability > 0 -> 显式变黄/红。"""
        rows = [_obs_round(i + 1, coverage=1.0, unavailable_capability=7)
                for i in range(4)]
        v = ls.evaluate_health(_healthy_metrics(rows))
        cap = _check(v, "capability")
        # 每一轮都不可评估 = 整类规则一次都没被验证过 -> fail
        assert cap["level"] == "fail"
        assert cap["ok"] is False
        assert v["healthy"] is False and v["exit_code"] == 1
        assert "能力缺失" in cap["detail"]

    def test_capability_partial_is_warn_not_fail(self):
        """部分轮不可评估：变黄但不判死（退出码仍 0）。"""
        rows = [_obs_round(i + 1, coverage=1.0) for i in range(3)]
        rows.append(_obs_round(4, coverage=1.0, unavailable_capability=2))
        v = ls.evaluate_health(_healthy_metrics(rows))
        cap = _check(v, "capability")
        assert cap["level"] == "warn"
        assert cap["ok"] is True
        assert "capability" in v["warn"]
        assert v["healthy"] is True and v["exit_code"] == 0

    def test_capability_zero_is_green(self):
        """回归保护：能力齐全时不得因为这一项变黄。"""
        rows = [_obs_round(i + 1, coverage=1.0, unavailable_capability=0)
                for i in range(3)]
        v = ls.evaluate_health(_healthy_metrics(rows))
        cap = _check(v, "capability")
        assert cap["level"] == "ok" and cap["ok"] is True
        assert v["healthy"] is True

    def test_thresholds_are_configurable(self):
        """要求 2：阈值必须可配置 —— 同一份数据，松档绿、默认黄、严档红。"""
        rows = [_obs_round(i + 1, coverage=0.93) for i in range(6)]
        m = _healthy_metrics(rows)

        def level(tol):
            return _check(ls.evaluate_health(m, tolerances=tol), "coverage")["level"]

        assert level({"coverage_warn_p05": 0.99, "coverage_fail_p05": 0.99,
                      "coverage_warn_p50": 0.99, "coverage_fail_p50": 0.99,
                      "coverage_warn_min": 0.99, "coverage_fail_min": 0.99}) == "fail"
        assert level({}) == "warn"                    # 默认档
        assert level({"coverage_warn_p05": 0.5, "coverage_fail_p05": 0.4,
                      "coverage_warn_p50": 0.5, "coverage_fail_p50": 0.4,
                      "coverage_warn_min": 0.5, "coverage_fail_min": 0.4}) == "ok"

    def test_default_thresholds_documented_and_not_100_percent(self):
        """要求 2：默认阈值必须显式存在，且**没有任何一项要求 100%**。"""
        tol = ls.DEFAULT_OBSERVATION_TOLERANCES
        for key in ("coverage_warn_p05", "coverage_fail_p05",
                    "coverage_warn_p50", "coverage_fail_p50",
                    "coverage_warn_min", "coverage_fail_min",
                    "unavailable_warn_ratio", "unavailable_fail_ratio"):
            assert key in tol, f"缺少可配置阈值 {key}"
        for key in ("coverage_warn_p05", "coverage_fail_p05",
                    "coverage_warn_p50", "coverage_fail_p50",
                    "coverage_warn_min", "coverage_fail_min"):
            assert 0.0 < tol[key] < 1.0, f"{key} 不应要求 100% 覆盖：{tol[key]}"
        # warn 阈值必须比 fail 阈值严格（否则 warn 永远不会先于 fail 触发）
        for warn, fail in (("coverage_warn_p05", "coverage_fail_p05"),
                           ("coverage_warn_p50", "coverage_fail_p50"),
                           ("coverage_warn_min", "coverage_fail_min")):
            assert tol[warn] >= tol[fail], f"{warn} 必须 >= {fail}"

    def test_not_100_percent_required(self):
        """要求 2：**不得**要求绝对 100% 覆盖 —— 真实行情下不可能。

        实测健康轮 = 99.91%（5564/5569），98.5% 也远在 warn 门槛 95%/97% 之上。
        """
        rows = [_obs_round(i + 1, coverage=0.985) for i in range(6)]
        v = ls.evaluate_health(_healthy_metrics(rows))
        assert _check(v, "coverage")["level"] == "ok"
        assert v["healthy"] is True, "98.5% 是正常抖动，不能被 100% 门槛打死"

    def test_unavailable_thresholds_configurable(self):
        rows = [_obs_round(i + 1, coverage=1.0, unavailable_capability=1)
                for i in range(4)]
        m = _healthy_metrics(rows)
        assert _check(ls.evaluate_health(m), "capability")["level"] == "fail"
        loose = ls.evaluate_health(m, tolerances={
            "unavailable_warn_ratio": 1.5, "unavailable_fail_ratio": 2.0})
        assert _check(loose, "capability")["level"] == "ok"

    def test_missing_threshold_key_falls_back_to_default(self):
        """脏阈值（None/字符串）不得让判定崩，也不得静默放行。"""
        rows = [_obs_round(i + 1, coverage=0.5) for i in range(3)]
        m = _healthy_metrics(rows)
        v = ls.evaluate_health(m, tolerances={
            "coverage_fail_p50": None, "coverage_warn_p50": "x",
            "coverage_fail_p05": None, "coverage_warn_p05": None,
            "coverage_fail_min": None, "coverage_warn_min": None})
        assert _check(v, "coverage")["level"] == "fail"

    def test_legacy_metrics_without_observation_do_not_fail(self):
        """旧版本报告（没有观测键）按"跳过"处理，不能被打成不健康。"""
        legacy = ls.finalize_metrics(
            [_sample(index=1), _sample(index=2)],     # 无 observation
            api={"requests": 20, "non_200": 0, "malformed": 0, "unreachable": 0},
            sse={"connected": True, "stalled": False, "events_total": 9},
            browser={"ran": False, "skipped": True, "errors": []},
        )
        v = ls.evaluate_health(legacy)
        assert _check(v, "coverage")["ok"] is True
        assert _check(v, "capability")["ok"] is True
        assert v["healthy"] is True

    def test_require_observation_can_be_turned_on(self):
        legacy = ls.finalize_metrics(
            [_sample(index=1)],
            api={"requests": 20, "non_200": 0, "malformed": 0, "unreachable": 0},
            sse={"connected": True, "stalled": False, "events_total": 9},
            browser={"ran": False, "skipped": True, "errors": []},
        )
        v = ls.evaluate_health(legacy, tolerances={"require_observation": True})
        assert _check(v, "coverage")["ok"] is False
        assert v["healthy"] is False

    def test_verdict_keeps_warn_and_fail_lists(self):
        rows = [_obs_round(i + 1, coverage=1.0) for i in range(3)]
        rows.append(_obs_round(4, coverage=1.0, unavailable_capability=1))
        v = ls.evaluate_health(_healthy_metrics(rows))
        assert v["warn"] == ["capability"]
        assert v["fail"] == []
        assert all("level" in c for c in v["checks"])


class TestObservationDirtyData:
    """要求 3：可观测性字段脏了不能让 soak 汇总崩，也不能伪造成绩。"""

    def test_missing_observation_is_not_counted_as_zero(self):
        """区分"确实是 0"与"没采集到"：旧样本不进分位、不进带账本轮数。"""
        rows = [_obs_round(1, coverage=1.0), _sample(index=2), _sample(index=3)]
        got = ls.summarize_rounds(rows)
        assert got["rounds"] == 3
        assert got["rounds_with_observation"] == 1
        assert got["coverage_rounds"] == 1
        # 旧轮不得被当成 0% 参与分位
        assert got["coverage_p05"] == pytest.approx(1.0)
        assert got["coverage_min"] == pytest.approx(1.0)
        assert [r["index"] for r in got["worst_rounds"]] == [1]

    def test_no_readable_coverage_is_none_not_zero(self):
        """全是旧样本时 coverage 必须是 None（"没读到"），不是 0.0（"真的是 0"）。"""
        got = ls.summarize_rounds([_sample(index=1), _sample(index=2)])
        assert got["coverage"] is None
        assert got["coverage_p05"] is None
        assert got["coverage_p50"] is None
        assert got["coverage_min"] is None
        assert got["coverage_rounds"] == 0
        assert got["worst_rounds"] == []

    def test_real_zero_coverage_is_recorded_as_zero(self):
        """确实是 0（provider 一只都没返回）必须老实记成 0，并进分位。"""
        rows = [_obs_round(1, coverage=0.0, requested=100)]
        got = ls.summarize_rounds(rows)
        assert got["coverage_rounds"] == 1
        assert got["coverage_min"] == 0.0
        assert got["returned_total"] == 0
        assert got["requested_total"] == 100
        assert [r["index"] for r in got["worst_rounds"]] == [1]
        # 0% 覆盖率必须判 FAIL（不是跳过、不是 0 分静默）
        v = ls.evaluate_health(_healthy_metrics(rows))
        assert _check(v, "coverage")["level"] == "fail"

    def test_unreadable_coverage_value_is_tracked(self):
        """采到了字段但值是坏的（None/字符串/NaN）单独记账，不混进 0 也不混进 100。"""
        rows = [_obs_round(1, coverage=1.0)]
        bad = _obs_round(2, coverage=1.0)
        bad["coverage"] = None
        bad2 = _obs_round(3, coverage=1.0)
        bad2["coverage"] = "not-a-number"
        bad3 = _obs_round(4, coverage=1.0)
        bad3["coverage"] = float("nan")
        got = ls.summarize_rounds(rows + [bad, bad2, bad3])
        assert got["coverage_rounds"] == 1
        assert got["coverage_p50"] == pytest.approx(1.0)
        assert got["coverage_unreadable_rounds"] == [2, 3, 4]

    @pytest.mark.parametrize("bad", [
        {"coverage": None}, {"coverage": "x"}, {"coverage": float("nan")},
        {"requested": None, "returned": None, "admitted": None},
        {"requested": "x", "returned": "y", "admitted": "z"},
        {"source": None}, {"source": 12345},
        {"unknown_missing": None}, {"rejected_quality": "oops"},
        {"future_rejected": None}, {"out_of_order_rejected": object()},
        {"unavailable_capability": None}, {"unavailable_by_reason": "nope"},
        {"capabilities": None}, {"capabilities": ["not", "a", "dict"]},
    ])
    def test_dirty_observation_does_not_break_summary(self, bad):
        """红测要求 3：脏字段下 summarize + evaluate 全程不抛。"""
        obs = _obs()
        obs.update(bad)
        rows = [_sample(index=1, observation=obs),
                _obs_round(2, coverage=1.0)]
        got = ls.summarize_rounds(rows)          # 不抛即通过
        assert got["rounds"] == 2
        v = ls.evaluate_health(_healthy_metrics(rows))
        assert isinstance(v["healthy"], bool)

    def test_round_sample_without_index_still_summarizes(self):
        """轮样本缺 index 也要能出最差轮摘要（退回 1 基位置，不抛）。"""
        rows = [dict(_obs_round(1, coverage=0.5)), dict(_obs_round(2, coverage=0.9))]
        for r in rows:
            r.pop("index", None)
        got = ls.summarize_rounds(rows, worst_n=2)
        assert [r["index"] for r in got["worst_rounds"]] == [1, 2]

    def test_non_dict_rows_are_ignored(self):
        rows = [_obs_round(1, coverage=0.9), None, "junk", 42]
        got = ls.summarize_rounds(rows)
        assert got["rounds"] == 1
        assert got["coverage_p50"] == pytest.approx(0.9)

    def test_summary_output_is_json_safe(self):
        import json
        rows = [_obs_round(i + 1, coverage=0.9, unavailable_capability=1)
                for i in range(3)]
        m = _healthy_metrics(rows)
        json.dumps({"metrics": ls.sanitize(m),
                    "verdict": ls.sanitize(ls.evaluate_health(m))},
                   ensure_ascii=False)
        assert ls.sanitize(m)["coverage_p50"] == pytest.approx(0.9)

    def test_observation_fields_marker_distinguishes_zero_from_absent(self):
        """标记本身：有 observation 就记采集到的字段，没采到就空集。"""
        with_obs = _obs_round(1, coverage=0.0)
        assert "coverage" in with_obs["observation_fields"]
        assert with_obs["coverage"] == 0.0
        legacy = _sample(index=2)
        assert "observation_fields" not in legacy
        assert ls._round_observation_fields(legacy) == set()
        assert "coverage" in ls._round_observation_fields(with_obs)

    @pytest.mark.parametrize("good", [0, 0.0, 0.5, 0.93, 1.0, 1, "0.93"])
    def test_in_range_coverage_survives(self, good):
        """区间内的值是**真成绩**，一个都不能被"容错"吃掉（含 0 和 1 两个端点）。

        数字字符串 ``"0.93"`` 也在内：仓库既有的 ``_safe_float`` 就是刻意容忍
        这种输入的（见其 docstring），这里保持一致，不额外发明一套更严的规矩。
        """
        s = _sample(index=1, observation=_obs(coverage=good))
        assert s["coverage"] == pytest.approx(0.93 if good == "0.93" else float(good))

    @pytest.mark.parametrize("bad", [
        True, False, -1, -0.5, 1.5, 2, 100, "-1", "1.5",
        "x", None, object(), [], {}, float("nan"), float("inf"),
        float("-inf"),
    ])
    def test_out_of_range_or_unreadable_coverage_becomes_none(self, bad):
        """越界/坏类型不得被当成真成绩回报。

        ``True`` 尤其危险：bool 是 int 的子类，不拦会被读成 1.0（"完美覆盖"）。
        ``-1``/``1.5`` 会被原样带进分位，于是报告里出现"覆盖率 -100%"。
        """
        s = _sample(index=1, observation=_obs(coverage=bad))
        assert s["coverage"] is None, f"{bad!r} 应被记为读不出"
        assert "coverage" in s["observation_fields"]   # 但"采过这个字段"仍是事实


class TestObservationReachesReport:
    """聚合必须真的到达报告与终端摘要 —— 只在纯函数里算是没用的。"""

    def test_report_carries_observation_aggregates(self):
        rows = [_obs_round(i + 1, coverage=0.93, unavailable_capability=2)
                for i in range(4)]
        m = _healthy_metrics(rows)
        v = ls.evaluate_health(m)
        report = ls.build_report(
            started_at="2026-09-20T20:00:00", finished_at="2026-09-20T20:01:00",
            duration_s=60.0,
            bounds={"minutes": 1.0, "rounds_requested": 4, "interval_s": 5.0,
                    "rounds_done": 4, "duration_s": 60.0, "deadline_hit": False,
                    "interrupted": False, "dashboard_url": "", "routes_probed": [],
                    "routes_missing": []},
            session_info={"is_open": False, "describe": "x", "note": "y"},
            config_snapshot={"rules_enabled": []},
            metrics=m, verdict=v, notes=["n"])
        got = report["metrics"]
        assert got["coverage_p50"] == pytest.approx(0.93)
        assert got["coverage_p05"] == pytest.approx(0.93)
        assert got["worst_rounds"][0]["index"] == 1
        assert got["capability_unavailable_total"] == 8
        assert report["verdict"]["warn"], "警告项必须进报告，不能只活在内存里"
        assert ls.sanitize(report)["metrics"]["coverage_p50"] == pytest.approx(0.93)

    def test_console_summary_prints_observation_line(self, capsys):
        rows = [_obs_round(i + 1, coverage=0.93) for i in range(3)]
        m = _healthy_metrics(rows)
        v = ls.evaluate_health(m)
        ls._print_summary(m, v, {"rounds_requested": 3, "duration_s": 15.0,
                                 "interval_s": 5.0}, None)
        out = capsys.readouterr().out
        assert "观测覆盖" in out, "终端摘要必须打印覆盖率分位，否则人看不到"
        assert "93.0%" in out
        assert "最差轮次" in out
        assert "警告项" in out, "黄色项必须在终端点名"

