"""`tools/live_session.py` 里纯函数的离线单测。

为什么这些测试**永远不联网、不起服务**：soak 工具的价值恰恰在于"真跑一次能给出
可信结论"，而结论的对错完全取决于 ``summarize_rounds`` / ``check_memory`` /
``evaluate_health`` / ``build_report`` 这几个纯函数。把判定逻辑绑在网络上测，
等于把"结论算得对不对"和"今天网通不通"混在一起 —— 前者必须每天都能验。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "live_session.py"
# `pyproject.toml` 的 pythonpath 只加了 src，tools/ 不是包，因此按文件路径加载。
# 用 spec 加载而不是改 sys.path：避免给整个测试会话塞进一个 tools 目录。
_SPEC = importlib.util.spec_from_file_location("live_session_tool", TOOL)
assert _SPEC and _SPEC.loader
ls = importlib.util.module_from_spec(_SPEC)
sys.modules["live_session_tool"] = ls
_SPEC.loader.exec_module(ls)


# ==========================================================================
# 夹具：手工构造一轮样本（不经过引擎，不碰网络）
# ==========================================================================
def sample(index: int = 1, *, latency_ms: float = 400.0, kinds=(), error: bool = False,
           quotes: int = 5000, universe: int = 5000, history_points: int = 5000,
           history_codes: int = 5000, max_deque: int = 1, history_maxlen: int = 360,
           watchlist_only: bool = False) -> dict:
    """一轮观测样本。默认值即"健康一轮"：全市场 5000 只、无错、1 个历史点。"""
    return ls.make_round_sample(
        index=index, latency_ms=latency_ms, alerts=kinds, error=error, quotes=quotes,
        universe=universe, history_points=history_points, history_codes=history_codes,
        max_deque=max_deque, history_maxlen=history_maxlen, watchlist_only=watchlist_only)


def healthy_rounds(n: int = 5) -> list[dict]:
    return [sample(i + 1) for i in range(n)]


def healthy_metrics(**over) -> dict:
    """一份"应当判健康"的完整指标；逐项覆盖即可模拟某种失败模式。"""
    metrics = ls.finalize_metrics(
        healthy_rounds(),
        api={"requests": 20, "non_200": 0, "malformed": 0, "unreachable": 0},
        sse={"connected": True, "stalled": False, "events_total": 15,
             "events_by_type": {"tick": 5, "phase": 5, "alert": 2, "spirit": 2, "bye": 1}},
        logs={"WARNING": 3, "ERROR": 0, "CRITICAL": 0},
        browser={"ran": False, "skipped": True, "errors": []},
    )
    metrics.update(over)
    return metrics


# ==========================================================================
# 1) 延迟分位数
# ==========================================================================
class TestLatency:
    def test_single_value(self):
        """只跑了一轮也必须能算 —— statistics.quantiles 在 n<2 时会直接抛异常。"""
        s = ls.latency_stats([123.456])
        assert s["count"] == 1
        assert s["min"] == s["median"] == s["p95"] == s["max"] == 123.46

    def test_percentile_interpolates(self):
        # 1..5：p50 落在下标 2.0 -> 精确命中 3；p95 落在下标 3.8 -> 4 + (5-4)*0.8
        assert ls.percentile([1, 2, 3, 4, 5], 50) == 3
        assert ls.percentile([1, 2, 3, 4, 5], 95) == pytest.approx(4.8)
        assert ls.percentile([1, 2, 3, 4, 5], 0) == 1
        assert ls.percentile([1, 2, 3, 4, 5], 100) == 5
        # p95 必须 >= p50：p95 是判定"5 秒轮询够不够"的依据，不能算反
        assert ls.percentile([1, 2, 3, 4, 5], 95) > ls.percentile([1, 2, 3, 4, 5], 50)

    def test_unsorted_input_is_sorted_first(self):
        s = ls.latency_stats([500, 100, 300, 200, 400])
        assert (s["min"], s["median"], s["max"]) == (100.0, 300.0, 500.0)

    def test_empty_is_all_none(self):
        assert ls.latency_stats([]) == {"count": 0, "min": None, "median": None,
                                        "p95": None, "max": None}

    def test_non_finite_ignored(self):
        """真实行情会产出 inf/NaN；它们不能把分位数变成 nan。"""
        s = ls.latency_stats([float("inf"), 100.0, float("nan"), 200.0])
        assert s["count"] == 2
        assert s["max"] == 200.0

    def test_none_values_ignored(self):
        assert ls.latency_stats([None, 50.0])["count"] == 1


# ==========================================================================
# 2) 逐轮聚合
# ==========================================================================
class TestSummarize:
    def test_counts_alerts_by_kind(self):
        rows = [sample(1, kinds=["surge", "surge", "limit_up"]),
                sample(2, kinds=["plunge"])]
        got = ls.summarize_rounds(rows)
        assert got["rounds"] == 2
        assert got["alerts_total"] == 4
        assert got["alerts_by_kind"] == {"surge": 2, "limit_up": 1, "plunge": 1}

    def test_accepts_alert_like_objects(self):
        """真实运行给的是 Alert 对象；kind 是枚举时要取 .value 而不是 'AlertKind.SURGE'。"""
        class _Kind:
            value = "surge"

        class _Alert:
            kind = _Kind()

        got = ls.summarize_rounds([sample(1, kinds=[_Alert(), {"kind": "plunge"}, "unusual"])])
        assert got["alerts_by_kind"] == {"surge": 1, "plunge": 1, "unusual": 1}

    def test_quotes_and_universe_first_last(self):
        rows = [sample(1, quotes=100, universe=100), sample(2, quotes=5000, universe=5000),
                sample(3, quotes=4800, universe=5000)]
        got = ls.summarize_rounds(rows)
        assert got["quotes"] == {"first": 100, "last": 4800, "min": 100, "max": 5000}
        assert got["universe"] == {"first": 100, "last": 5000, "min": 100, "max": 5000}

    def test_watchlist_fallback_detection(self):
        """每轮都只有自选股那 10 只 = 全市场扫描没起来，必须显式报出来。"""
        rows = [sample(i + 1, quotes=10, universe=10, watchlist_only=True) for i in range(3)]
        assert ls.summarize_rounds(rows)["fell_back_to_watchlist"] is True
        mixed = [sample(1, quotes=10, universe=10, watchlist_only=True), sample(2)]
        assert ls.summarize_rounds(mixed)["fell_back_to_watchlist"] is False

    def test_error_and_no_data_rounds(self):
        rows = [sample(1), sample(2, error=True, quotes=0), sample(3, error=True)]
        got = ls.summarize_rounds(rows)
        assert got["error_rounds"] == 2
        assert got["no_data_rounds"] == 1

    def test_empty_input_is_safe(self):
        got = ls.summarize_rounds([])
        assert got["rounds"] == 0 and got["alerts_by_kind"] == {}
        assert got["latency_ms"]["median"] is None
        assert got["fell_back_to_watchlist"] is False


# ==========================================================================
# 3) 内存代理
# ==========================================================================
class TestMemory:
    def test_bounded_when_deque_respects_maxlen(self):
        rows = [sample(1, history_points=100, history_codes=100, max_deque=1),
                sample(2, history_points=1200, history_codes=5000, max_deque=12)]
        got = ls.check_memory([r for r in rows])  # 直接喂样本
        assert got["bounded"] is True
        assert got["max_deque"] == 12

    def test_growth_within_maxlen_is_not_a_leak(self):
        """跑 1 分钟历史点从 5000 涨到 60000（12 倍）是**预期增长**，不是泄漏。"""
        samples = [
            {"quotes": 5000, "history_points": 5000, "history_codes": 5000,
             "max_deque": 1, "history_maxlen": 360},
            {"quotes": 5000, "history_points": 60000, "history_codes": 5000,
             "max_deque": 12, "history_maxlen": 360},
        ]
        assert ls.check_memory(samples)["bounded"] is True

    def test_deque_over_maxlen_is_unbounded(self):
        samples = [{"quotes": 10, "history_points": 5000, "history_codes": 10,
                    "max_deque": 500, "history_maxlen": 360}]
        got = ls.check_memory(samples)
        assert got["bounded"] is False
        assert "超过上限" in got["reason"]

    def test_quotes_explosion_is_unbounded(self):
        samples = [{"quotes": 100, "history_points": 100, "history_codes": 100,
                    "max_deque": 1, "history_maxlen": 360},
                   {"quotes": 90000, "history_points": 90000, "history_codes": 90000,
                    "max_deque": 1, "history_maxlen": 360}]
        assert ls.check_memory(samples)["bounded"] is False

    def test_slack_allows_small_growth_from_zero(self):
        """从 0 起来（首轮没数据）不算爆炸，slack 是给这种情况留的。"""
        samples = [{"quotes": 0, "history_points": 0, "history_codes": 0,
                    "max_deque": 0, "history_maxlen": 360},
                   {"quotes": 300, "history_points": 300, "history_codes": 300,
                    "max_deque": 1, "history_maxlen": 360}]
        assert ls.check_memory(samples)["bounded"] is True

    def test_no_samples_is_unbounded(self):
        assert ls.check_memory([])["bounded"] is False


# ==========================================================================
# 4) 结论与退出码
# ==========================================================================
def failing(verdict: dict) -> list[str]:
    return [c["name"] for c in verdict["checks"] if not c["ok"]]


class TestVerdict:
    def test_healthy_exit_zero(self):
        v = ls.evaluate_health(healthy_metrics())
        assert v["healthy"] is True
        assert v["exit_code"] == 0
        assert failing(v) == []
        assert all(c["detail"] for c in v["checks"])   # 每项都要有人能看懂的理由

    def test_zero_alerts_is_healthy(self):
        """核心约定：休市时 0 条告警是正常的，绝不能判失败。"""
        m = healthy_metrics(alerts_total=0, alerts_by_kind={})
        v = ls.evaluate_health(m)
        assert v["healthy"] is True and v["exit_code"] == 0
        alerts_check = [c for c in v["checks"] if c["name"] == "alerts"][0]
        assert alerts_check["ok"] is True
        assert "0 条不算失败" in alerts_check["detail"]

    def test_fetch_failures_beyond_tolerance_fail(self):
        m = healthy_metrics(error_rounds=3)            # 5 轮里错 3 轮 >> 10% 容差
        v = ls.evaluate_health(m)
        assert v["healthy"] is False and v["exit_code"] == 1
        assert "fetch" in failing(v)

    def test_small_fetch_failure_within_tolerance_passes(self):
        """东财限流是常态，偶发失败由 SourceManager 兜底，不该判失败。"""
        m = healthy_metrics(error_rounds=0, no_data_rounds=0)
        m["rounds"] = 100
        m["error_rounds"] = 8                          # 8% < 10%
        assert ls.evaluate_health(m)["healthy"] is True

    def test_error_and_empty_data_both_count(self):
        m = healthy_metrics(error_rounds=1, no_data_rounds=1)
        assert ls.evaluate_health(m)["healthy"] is False

    def test_no_data_at_all_fails(self):
        m = healthy_metrics(quotes={"first": 0, "last": 0, "min": 0, "max": 0})
        v = ls.evaluate_health(m)
        assert v["healthy"] is False and "data" in failing(v)

    @pytest.mark.parametrize("field,value", [
        ("non_200", 1), ("malformed", 1), ("unreachable", 1),
    ])
    def test_api_failures_fail(self, field, value):
        api = {"requests": 20, "non_200": 0, "malformed": 0, "unreachable": 0}
        api[field] = value
        m = healthy_metrics(api=api)
        m["api"] = api
        v = ls.evaluate_health(m)
        assert v["healthy"] is False and "api" in failing(v)

    def test_no_api_requests_fails(self):
        """一次接口都没探到 = 看板链路根本没验证，不能算健康。"""
        m = healthy_metrics()
        m["api"] = {"requests": 0, "non_200": 0, "malformed": 0, "unreachable": 0}
        v = ls.evaluate_health(m)
        assert v["healthy"] is False and "api" in failing(v)

    def test_malformed_json_fails(self):
        m = healthy_metrics()
        m["api"] = {"requests": 8, "non_200": 0, "malformed": 2, "unreachable": 0}
        v = ls.evaluate_health(m)
        assert v["healthy"] is False and "api" in failing(v)

    def test_stalled_stream_fails(self):
        m = healthy_metrics(sse={"connected": True, "stalled": True, "events_total": 9})
        v = ls.evaluate_health(m)
        assert v["healthy"] is False and "sse" in failing(v)

    def test_disconnected_stream_fails(self):
        m = healthy_metrics(sse={"connected": False, "stalled": False, "events_total": 0})
        assert "sse" in failing(ls.evaluate_health(m))

    def test_unbounded_memory_fails(self):
        m = healthy_metrics(memory={"bounded": False, "reason": "单只历史点数 5000 超过上限 360"})
        v = ls.evaluate_health(m)
        assert v["healthy"] is False and "memory" in failing(v)

    def test_browser_errors_fail_only_when_it_ran(self):
        ran_bad = healthy_metrics(browser={"ran": True, "skipped": False,
                                           "errors": ["pageerror: TypeError"]})
        assert "browser" in failing(ls.evaluate_health(ran_bad))
        skipped = healthy_metrics(browser={"ran": False, "skipped": True,
                                           "errors": ["（历史残留）"]})
        assert ls.evaluate_health(skipped)["healthy"] is True

    def test_zero_rounds_is_exit_two(self):
        """一轮都没跑完是 harness 级失败，与"跑起来了但有问题"区分开。"""
        v = ls.evaluate_health(ls.finalize_metrics([]))
        assert v["exit_code"] == 2 and v["healthy"] is False

    def test_custom_tolerance_zero_means_strict(self):
        m = healthy_metrics()
        m["rounds"] = 10
        m["error_rounds"] = 1
        assert ls.evaluate_health(m)["healthy"] is True
        strict = ls.evaluate_health(m, tolerances={"max_error_ratio": 0.0})
        assert strict["healthy"] is False

    def test_garbage_input_does_not_raise(self):
        v = ls.evaluate_health({})
        assert v["exit_code"] == 2 and isinstance(v["checks"], list)


# ==========================================================================
# 5) 报告 JSON 往返
# ==========================================================================
class TestReport:
    def _report(self, **over) -> dict:
        kw = dict(
            started_at="2026-01-05T21:00:00",
            finished_at="2026-01-05T21:01:00",
            duration_s=60.0,
            bounds={"minutes": 1.0, "rounds_requested": None, "interval_s": 5.0,
                    "rounds_done": 5, "routes_probed": ["/api/health"],
                    "routes_missing": []},
            session_info={"phase": "closed", "is_open": False, "describe": "休市"},
            config_snapshot={"poll": {"universe_seconds": 5},
                             "rules": {"effective": ["tick_surge", "limit_board"]}},
            metrics=healthy_metrics(),
            verdict=ls.evaluate_health(healthy_metrics()),
            notes=["休市时 0 条告警不等于故障"],
        )
        kw.update(over)
        return ls.build_report(**kw)

    def test_json_round_trip_is_identity(self):
        report = self._report()
        text = json.dumps(report, ensure_ascii=False)
        assert json.loads(text) == report

    def test_config_snapshot_is_kept(self):
        """没有配置快照，两次运行的结果就没法比较。"""
        report = self._report()
        assert report["config"]["poll"]["universe_seconds"] == 5
        assert report["config"]["rules"]["effective"] == ["tick_surge", "limit_board"]

    def test_nan_and_inf_become_null(self):
        """裸 NaN/Infinity 是非法 JSON，json.loads 都读不回来。"""
        report = ls.build_report(
            started_at="t", finished_at="t", duration_s=1.0, bounds={},
            session_info={}, config_snapshot={},
            metrics={"latency_ms": {"p95": float("nan"), "max": float("inf")}},
            verdict={"healthy": True, "exit_code": 0, "checks": []})
        assert report["metrics"]["latency_ms"] == {"p95": None, "max": None}
        assert json.loads(json.dumps(report)) == report
        assert "NaN" not in json.dumps(report) and "Infinity" not in json.dumps(report)

    def test_tuples_and_sets_are_json_native(self):
        """tuple/set 会让 round-trip 断言假失败（json 转回 list），必须统一。"""
        report = ls.build_report(
            started_at="t", finished_at="t", duration_s=1.0,
            bounds={"routes": ("/api/health", "/api/status")},
            session_info={}, config_snapshot={"codes": {"600519", "000001"}},
            metrics={}, verdict={"checks": []})
        assert report["bounds"]["routes"] == ["/api/health", "/api/status"]
        assert report["config"]["codes"] == ["000001", "600519"]   # 排序保证可复现
        assert json.loads(json.dumps(report)) == report

    def test_jsonl_friendly_ascii_escape(self):
        """报告用 ensure_ascii=False 写盘，中文必须能原样读回来。"""
        report = self._report()
        raw = json.dumps(report, ensure_ascii=False, indent=2)
        assert "休市" in raw
        assert json.loads(raw)["notes"][0].startswith("休市")

    def test_verdict_survives_round_trip(self):
        report = self._report()
        again = json.loads(json.dumps(report, ensure_ascii=False))
        assert again["verdict"]["exit_code"] == 0
        assert again["verdict"]["healthy"] is True
        assert [c["name"] for c in again["verdict"]["checks"]] == \
               [c["name"] for c in report["verdict"]["checks"]]

    def test_unserializable_object_is_stringified(self):
        class _Weird:
            def __repr__(self):  # pragma: no cover - 只用于确认不抛异常
                return "<weird>"

        report = ls.build_report(
            started_at="t", finished_at="t", duration_s=1.0, bounds={},
            session_info={"obj": _Weird()}, config_snapshot={}, metrics={},
            verdict={"checks": []})
        assert isinstance(report["session"]["obj"], str)
        assert json.loads(json.dumps(report)) == report


# ==========================================================================
# 6) 工具本身的约束（无导入副作用）
# ==========================================================================
class TestToolSurface:
    def test_pure_api_is_importable(self):
        for name in ("percentile", "latency_stats", "make_round_sample",
                     "summarize_rounds", "check_memory", "finalize_metrics",
                     "evaluate_health", "build_report", "sanitize",
                     "parse_args", "main", "run"):
            assert callable(getattr(ls, name)), name

    def test_import_has_no_side_effects(self):
        """import 这个模块不能有任何可见副作用（打印、建目录、起线程/服务）。

        用一个干净的子进程跑：本测试进程早就 import 过它了，在这里断言等于什么都没验。
        """
        import os
        import subprocess

        script = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('ls_probe', r'{TOOL}')\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "print('IMPORTED')\n"
        )
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True,
                              text=True, cwd=str(ROOT), timeout=60,
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "IMPORTED"     # 除自己那行外不许有输出
        assert proc.stderr.strip() == ""             # 也不许有日志/告警

    def test_main_is_guarded(self):
        """必须由 ``if __name__ == "__main__":`` 守着，不能 import 即跑。"""
        src = TOOL.read_text(encoding="utf-8")
        assert 'if __name__ == "__main__":' in src

    def test_parse_args_bounds(self):
        args = ls.parse_args(["--minutes", "1"])
        assert args.minutes == 1.0 and args.rounds == 0 and args.port == 0
        assert ls.parse_args(["--rounds", "3", "--minutes", "1"]).rounds == 3
        assert ls.parse_args(["--browser", "--verbose"]).browser is True
        assert ls.parse_args([]).minutes == 10.0      # 默认 10 分钟

    def test_parse_args_rejects_zero_bounds(self):
        with pytest.raises(SystemExit):
            ls.parse_args(["--minutes", "0"])
