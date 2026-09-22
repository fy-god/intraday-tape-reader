"""Health Consumer Contract + tri-state 回归测试。

来源：`2026-09-22_13-32-18_JST.md`、`2026-09-22_13-55-00_JST_ADDENDUM.md`
（更正附刊）、`2026-09-22_16-13-10_JST.md`（+ AGENT_TASK）。

**这轮的核心指控是针对我 13:00 轮（`c4f2ce1`）的**：
13:55 更正附刊撤回"3 项修复全部成立"，改为 **PARTIAL**，理由是
我只验了"机制是否存在"，**未验 ①边界可达性 ②出口完整性（含异常出口）
③新引入的镜像错误**。我逐条独立复现后确认**对方是对的**。

核心概念：**三态被压成两态**。
* 修"把不知道当没问题"（假绿）时，很容易引入
  "把不知道当成有罪并归罪于数据源"（假红）—— **镜像错误**。
* 两者同源，所以缺数据的语义必须端到端保留：
  `unknown != false` / `unknown != 0` / `unknown != fresh`。

设计纪律：新符号一律在函数体内 `getattr` 取，避免 ImportError 冒充行为级 RED。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tools"))


def _ls():
    import live_session
    return live_session


def _lvl(v, name):
    it = [c for c in v["checks"] if c["name"] == name]
    return it[0]["level"] if it else "MISSING"


def _detail(v, name):
    it = [c for c in v["checks"] if c["name"] == name]
    return it[0]["detail"] if it else ""


def _sig(v):
    """完整判决签名 —— consumer-effect 测试的判据。"""
    return tuple(sorted((c["name"], c["level"]) for c in v["checks"]))


def _base():
    m = _ls().empty_metrics()
    m.update({
        "rounds": 30, "alerts_total": 0, "alerts_by_kind": {},
        "quotes": {"min": 5000, "max": 5000, "last": 5000, "first": 5000},
        "universe": {"min": 5000, "max": 5000, "last": 5000, "first": 5000},
        "rounds_with_observation": 30, "coverage_rounds": 30,
        "coverage": 0.99, "coverage_p05": 0.99, "coverage_p50": 0.99,
        "coverage_min": 0.99, "coverage_max": 1.0,
        "requested_total": 5000 * 30, "returned_total": 5000 * 30,
        "admitted_total": 5000 * 30, "source_mix": {"eastmoney": 30},
        "source_mix_rounds": 30, "capability_unavailable_ratio": 0.0,
        "memory": {"bounded": True, "reason": "ok"},
        "api": {"requests": 300, "non_200": 0, "malformed": 0,
                "unreachable": 0, "by_route": {}},
        "sse": {"connected": True, "stalled": False, "events_total": 50,
                "events_by_type": {}, "status": None, "error": None},
    })
    return m


def _green_session(**over):
    s = {"measured": True, "worst_state": "fresh", "states": {"fresh": 30},
         "attempt_statuses": {"applied": 30}, "rounds_not_applied": 0,
         "rounds_transport_incomplete": 0, "rounds_transport_measured": 30,
         "max_age_s": 1.0, "last_age_s": 1.0,
         "watchlist_only_rounds": 0, "watchlist_only_ratio": 0.0,
         "ever_watchlist_only": False}
    s.update(over)
    return s


def _green_t0():
    return {"denominator_kind": "provider_declared_total",
            "expected_total": 6000, "active_scan_codes": 5900,
            "coverage_active": 5900 / 6000, "transport_complete": True,
            "active_age_s": 1.0, "full_market_age_s": 1.0,
            "attempt_status": "applied",
            "freshness": {"state": "fresh", "age_s": 1.0,
                          "warn_s": 1800.0, "fail_s": 3600.0}}


def _green_metrics():
    m = _base()
    m["setup"] = {"universe_size": 5900, "fell_back_to_watchlist": False,
                  "universe_truth": _green_t0()}
    m["universe_session"] = _green_session()
    return m


class _Src:
    name = "stub"

    def __init__(self, n=0, fail=False):
        self.n = n
        self.fail = fail

    def universe(self):
        if self.fail:
            raise RuntimeError("provider down")
        from datetime import datetime

        from arad.models import Board, Quote
        return [Quote(code=f"60{i:04d}", name="x", board=Board.MAIN,
                      price=10.0, prev_close=10.0, open=10.0, high=10.0,
                      low=10.0, volume_lots=100, amount=1000.0,
                      ts=datetime(2026, 9, 21, 10, 0, 0))
                for i in range(self.n)]

    def universe_info(self):
        return {"complete": True, "transport_complete": True,
                "transport_expected_total": self.n or 6000,
                "raw_unique_codes": self.n or 6000,
                "usable_quotes": self.n or 6000, "shortfall": 0}

    def snapshots(self, codes=None, **kw):
        return []

    def capabilities(self):
        from arad.capabilities import capabilities_for
        return capabilities_for("replay")

    def health(self):
        return {"name": "stub", "ok": True, "latency_ms": 1, "err": ""}


def _engine(src, watchlist=()):
    from arad.config import load_settings
    from arad.engine import Engine
    from arad.session import TradingCalendar
    eng = Engine(source=src, settings=load_settings(use_cache=False),
                 calendar=TradingCalendar(), watchlist=list(watchlist))
    # 禁止命中真实网络：`_universe_sources()` 会**按配置新建源**，
    # 不使用注入的 source=（13:00 轮就因此意外抓了一次真实东财股票池）。
    eng._universe_src = [src]
    return eng


# ===========================================================================
# WP01 / RED A：transport tri-state
# ===========================================================================
def test_cold_start_transport_is_none_not_false():
    """**核心 RED（镜像错误）**：冷启动 `transport_complete` 必须是 `None`。

    修前：`_universe_meta == {}` 时 `bool(None)` -> `False` ->
    判决层说 **"provider 声明本次股票池被截断"**，`exit=1`，把排障指向数据源。
    **provider 什么都没声明。** 这是"把不知道当成有罪"。
    """
    eng = _engine(_Src(0))
    ut = eng.universe_truth()
    assert eng._universe_meta == {}
    assert ut["transport_complete"] is None, (
        f"冷启动必须未测量，实得 {ut['transport_complete']!r}")
    assert ut["active_snapshot"]["transport_complete"] is None


def test_cold_start_verdict_does_not_accuse_provider():
    """冷启动判决必须是"未测量"，**不得**诬告 provider。"""
    eng = _engine(_Src(0))
    ut = eng.universe_truth()
    m = _base()
    m["setup"] = {"universe_size": 0, "universe_truth": ut}
    v = _ls().evaluate_health(m)
    det = _detail(v, "universe_transport")
    assert "截断" not in det, f"冷启动不得说 provider 截断：{det}"
    assert "未测量" in det, f"应报未测量：{det}"
    assert _lvl(v, "universe_transport") == "ok"


def test_explicit_incomplete_still_fails():
    """负控制：**真的** False 仍必须 fail（三态修完不能把真截断放走）。

    ⚠ **本测试曾"以错误的理由通过"**（20:04 §4 / ADDENDUM2 §2 指认，
    我复算确认）。修前它设 `rounds_transport_measured=0`，使会话肯定分支
    的条件为假，于是**落到 t0 分支**才判 fail ——
    它从未在 `rounds_transport_measured > 0` 时试过被测分支：

        t0=False + rounds_transport_measured=0  -> fail   <-- 旧测试走的路径
        t0=False + rounds_transport_measured=1  -> ok     <-- 一个单位就翻绿！
        t0=False + rounds_transport_measured=30 -> ok

    **这是"负控制的输入把被测分支绕过去了"的教科书案例。**
    所以现在参数化覆盖 `measured ∈ {0, 1, 30}`：
    **无论会话测了几轮，t0 的明确 False 都必须判 fail。**
    """
    for tm in (0, 1, 30):
        m = _base()
        t = _green_t0()
        t["transport_complete"] = False
        t["denominator_kind"] = "unknown"
        t["expected_total"] = 0
        t["coverage_active"] = None
        m["setup"] = {"universe_size": 5000, "universe_truth": t}
        m["universe_session"] = _green_session(
            rounds_transport_incomplete=0, rounds_transport_measured=tm)
        v = _ls().evaluate_health(m)
        assert _lvl(v, "universe_transport") == "fail", (
            f"t0 transport_complete=False 必须 fail，"
            f"但 rounds_transport_measured={tm} 时实得 "
            f"{_lvl(v, 'universe_transport')} —— 会话的『全完整』"
            f"不得抹掉 t0 的硬事实")


def test_t0_hard_negative_beats_session_all_complete():
    """`IT-P1-UNIVERSE-TRANSPORT-T0-SUPPRESSED-001`（20:04 §4）。

    **后来的 positive evidence 不能抹掉同一评估窗口内的 explicit hard
    negative。** 一个评估窗口里出现过"明确坏"，窗口结论就是坏。

    与 `worst_state` 的纪律完全一致（"中途曾坏过"不能被"最后又好了"抹掉），
    只是对象从新鲜度换成了传输完整性。
    """
    for t0_val, want in ((False, "fail"), (True, "ok"), (None, "ok")):
        m = _base()
        t = _green_t0()
        t["transport_complete"] = t0_val
        m["setup"] = {"universe_size": 5900, "universe_truth": t}
        m["universe_session"] = _green_session(
            rounds_transport_incomplete=0, rounds_transport_measured=30)
        v = _ls().evaluate_health(m)
        assert _lvl(v, "universe_transport") == want, (
            f"t0 transport_complete={t0_val!r} + 会话 30 轮全完整 "
            f"应判 {want}，实得 {_lvl(v, 'universe_transport')}")


def test_round_sample_preserves_none_tri_state():
    """`make_round_sample` **不得**把 None 重压成 False。

    16:13 §4：即便 Engine 修好了，这一层 `bool(...)` 又会把 None 压回 False，
    于是 session 聚合把"未测量"误计为"被截断" ——
    **修一个假绿会立刻造出一个新的假红**。
    """
    ls = _ls()
    s = ls.make_round_sample(
        index=1, latency_ms=1.0, alerts=[], error=False, quotes=10,
        universe=10, history_points=0, history_codes=0, max_deque=0,
        history_maxlen=0, watchlist_only=False, observation={},
        universe_truth={"transport_complete": None,
                        "freshness": {"state": "unknown", "age_s": None},
                        "latest_attempt": {}})
    assert s["universe_transport_complete"] is None
    assert s["universe_age_s"] is None, "年龄未知不得变成 0.0（那是「最年轻」）"


def test_optional_helpers_exist_and_are_tri_state():
    ls = _ls()
    ob = getattr(ls, "_optional_bool", None)
    of = getattr(ls, "_optional_float", None)
    assert ob is not None and of is not None, "必须有 _optional_bool/_optional_float"
    assert ob(None) is None and ob(True) is True and ob(False) is False
    assert of(None) is None and of("x") is None and of(1.5) == 1.5
    # 关键：未知 != 0
    assert of(None) != 0.0


def test_session_does_not_count_unmeasured_as_truncated():
    """聚合层：None（未测量）不得计入 `rounds_transport_incomplete`。"""
    fn = getattr(_ls(), "_universe_session")
    agg = fn([{"universe_fresh_state": "fresh", "universe_attempt_status": "applied",
               "universe_transport_complete": None} for _ in range(30)])
    assert agg["rounds_transport_incomplete"] == 0
    assert agg["rounds_transport_measured"] == 0


# ===========================================================================
# WP01 / RED B：session transport consumer（consumer-effect）
# ===========================================================================
def test_session_transport_consumer_changes_verdict():
    """**核心 RED（第 6 次"判决层零读者"）**：`_ti` 必须真的改变判决。

    13:32 用 AST/bytecode/1008 组签名证明修前
    `rounds_transport_incomplete = 0` 与 `= 12` 得到**相同 verdict**。
    """
    a = _green_metrics()
    b = _green_metrics()
    b["universe_session"] = _green_session(rounds_transport_incomplete=12)
    va, vb = _ls().evaluate_health(a), _ls().evaluate_health(b)
    assert _sig(va) != _sig(vb), "会话截断事实必须改变完整判决签名"
    assert _lvl(vb, "universe_transport") == "fail"
    assert _lvl(va, "universe_transport") == "ok"


def test_green_baseline_is_actually_green():
    """consumer-effect 测试的前提：基线必须真绿，否则"变化"没有意义。"""
    v = _ls().evaluate_health(_green_metrics())
    assert v["healthy"] is True, f"基线应绿，实得 fail={v['fail']}"


# ===========================================================================
# WP01 / RED C：session truth 不依赖 t0
# ===========================================================================
def test_session_facts_consumed_without_t0():
    """**核心 RED**：setup 缺 `universe_truth` 时必须仍判会话事实。

    修前整段 session 消费被嵌在 `if isinstance(_ut, dict) and _ut:` 里，
    实测 `worst_state=stale / rounds_transport_incomplete=99` 仍 `healthy=True`。
    """
    m = _base()
    m["setup"] = {"universe_size": 5000}       # 无 universe_truth
    m["universe_session"] = _green_session(
        worst_state="stale", states={"stale": 30},
        attempt_statuses={"all_failed": 30}, rounds_not_applied=30,
        rounds_transport_incomplete=99, max_age_s=9000.0, last_age_s=9000.0)
    v = _ls().evaluate_health(m)
    assert v["healthy"] is False, "无 t0 时会话事实不得被整体跳过"
    assert _lvl(v, "universe_freshness") == "fail"


# ===========================================================================
# WP01 / RED E：unknown 新鲜度语义
# ===========================================================================
def test_unknown_freshness_is_not_rendered_as_fresh():
    """`states={"unknown":30}` 必须说"未测量"，**不能说"新鲜"**。"""
    m = _green_metrics()
    m["universe_session"] = _green_session(
        worst_state="unknown", states={"unknown": 30}, max_age_s=None)
    v = _ls().evaluate_health(m)
    det = _detail(v, "universe_freshness")
    assert "未测量" in det, f"unknown 必须报未测量：{det}"
    assert "新鲜（" not in det, f"不得说成新鲜：{det}"


# ===========================================================================
# WP01 / RED D：watchlist ratio
# ===========================================================================
def _rows_with_watch(n_of_30):
    ls = _ls()
    return [ls.make_round_sample(
        index=i + 1, latency_ms=1.0, alerts=[], error=False,
        quotes=5000, universe=5000, history_points=0, history_codes=0,
        max_deque=0, history_maxlen=0, watchlist_only=(i < n_of_30),
        observation={},
        universe_truth={"attempt_status": "applied",
                        "transport_complete": True,
                        "freshness": {"state": "fresh", "age_s": 1.0}})
        for i in range(30)]


@pytest.mark.parametrize("n_watch,expect_fail", [
    (0, False),    # 全市场 soak 干净
    (1, True),     # **1/30 也必须可见**（修前只有 30/30 才 True）
    (29, True),    # 修前**漏掉**的那一档
    (30, True),
])
def test_watchlist_ratio_matrix(n_watch, expect_fail):
    """RED D 矩阵：不能只加 `setup OR 顶层 fell_back` 布尔。

    16:13 §2：那个布尔只有 30/30 才 True，**29/30 仍会漏**。
    必须保留 round count / ratio / ever 三个事实。
    """
    m = _base()
    m["setup"] = {"universe_size": 5000, "universe_truth": _green_t0()}
    m["universe_session"] = _ls()._universe_session(_rows_with_watch(n_watch))
    v = _ls().evaluate_health(m)
    assert ("universe_scope" in v["fail"]) is expect_fail, (
        f"{n_watch}/30 降级 期望 fail={expect_fail}，实得 {v['fail']}")


def test_explicit_watch_only_mode_is_exempt():
    """显式 `--watch-only` 是**用户意图**，不是故障 —— 不得误报。

    意图只能靠显式 run mode 识别，**绝不能**靠"股票数很少"猜。
    """
    m = _base()
    m["setup"] = {"universe_size": 10, "watch_only": True,
                  "universe_truth": _green_t0()}
    m["universe_session"] = _ls()._universe_session(_rows_with_watch(30))
    v = _ls().evaluate_health(m)
    assert _lvl(v, "universe_scope") == "ok", "显式 --watch-only 不得判失败"


def test_session_ratio_facts_present_even_without_freshness():
    """watchlist 事实是**独立轴**：无新鲜度样本时也要给出。"""
    agg = _ls()._universe_session([{"watchlist_only": True, "quotes": 5},
                                   {"watchlist_only": False, "quotes": 5000}])
    assert agg["measured"] is False
    assert agg["watchlist_only_rounds"] == 1
    assert agg["watchlist_only_ratio"] == 0.5
    assert agg["ever_watchlist_only"] is True


# ===========================================================================
# WP01（21:00 批）—— Session Universe Evidence Contract v1
# ===========================================================================
def _asserts_full_market(detail: str) -> bool:
    """这条 detail 是否**真的在肯定**"全程全市场扫描"。

    不能只用子串判断：新文案里"**不能**据此说'全程全市场扫描'"
    **也含这个子串**，朴素 `in` 会把**否认**读成**肯定**。
    （我在探针里就犯过这个错 —— 规则 mmm2：
    **匹配错了的过滤器什么也证明不了**。）
    """
    return ("全程全市场扫描" in detail
            and "不能" not in detail
            and "不足" not in detail)


def test_zero_evidence_scope_is_not_asserted_as_full_market():
    """**核心 RED（C1）**：零证据不得被肯定成"全程全市场扫描"。

    `IT-P2-UNIVERSE-EMPTY-SCAN-ASSERTED-AS-FULL-MARKET-001`（20:04 §3）：

    修前 `_universe_session([])` 给出
    `measured=False / ratio=None / ever=False`，consumer 的 `else` 分支
    输出「会话期间未降级为仅自选股（**全程全市场扫描**）」。
    **零证据被当成了肯定证据** —— 与"把不知道当没问题"完全同类。
    """
    m = _base()
    m["setup"] = {"universe_size": 5000, "universe_truth": _green_t0()}
    m["universe_session"] = _ls()._universe_session([])
    v = _ls().evaluate_health(m)
    det = _detail(v, "universe_scope")
    assert not _asserts_full_market(det), f"零证据不得肯定全市场扫描：{det}"
    assert "不足" in det, f"应说证据不足：{det}"


def test_zero_round_report_is_not_asserted_as_full_market():
    """**核心 RED（C1b）**：`finalize_metrics([])`（**一轮都没跑**）
    也曾输出"全程全市场扫描"。

    这是最刺眼的一种：报告自己 `fail=['rounds','data','api','sse','memory']`，
    却在同一份报告的 scope 轴说"全程全市场扫描"。
    """
    mm = _ls().finalize_metrics([])
    assert mm.get("rounds") == 0
    m = _base()
    m["setup"] = {"universe_size": 5000, "universe_truth": _green_t0()}
    m["universe_session"] = mm.get("universe_session") or {}
    det = _detail(_ls().evaluate_health(m), "universe_scope")
    assert not _asserts_full_market(det), (
        f"零轮报告不得肯定全市场扫描：{det}")


def test_empty_scan_is_distinguished_from_full_market():
    """**核心 RED（C1 变体）**：**空扫描**与"全市场"必须可区分。

    股票池被清空（`active_scan_codes == 0`）时，`state.quotes` 可能
    还留着旧缓存，所以 `quotes` 看起来正常。旧口径下两者**都是
    `watchlist_only=False`，不可区分** —— 这正是缺陷本体。
    """
    rows = [_row_scope(quotes=5000, active=0, expected=6000, universe=0)
            for _ in range(30)]
    agg = _ls()._universe_session(rows)
    assert agg["empty_scan_rounds"] == 30, f"应识别 30 轮空扫描：{agg}"
    assert agg["full_market_rounds"] == 0
    m = _base()
    m["setup"] = {"universe_size": 5000, "universe_truth": _green_t0()}
    m["universe_session"] = agg
    v = _ls().evaluate_health(m)
    assert _lvl(v, "universe_scope") == "fail", (
        "空扫描（一只票都没扫）必须判 fail，不能被读成'全市场'")


def _row_scope(*, quotes, active, expected, universe, watch=False):
    return _ls().make_round_sample(
        index=1, latency_ms=1.0, alerts=[], error=False, quotes=quotes,
        universe=universe, history_points=0, history_codes=0, max_deque=0,
        history_maxlen=0, watchlist_only=watch, observation={},
        universe_truth={"attempt_status": "applied",
                        "transport_complete": True,
                        "expected_total": expected,
                        "active_scan_codes": active,
                        "coverage_active": (active / expected
                                            if expected else None),
                        "denominator_kind": "provider_declared_total",
                        "freshness": {"state": "fresh", "age_s": 1.0}})


def test_scope_state_is_four_state_not_bool():
    """scope 必须是**四态**：`bool` 无法表达 `empty_scan` 与 `unknown`。"""
    ls = _ls()
    assert ls.SCOPE_FULL_MARKET != ls.SCOPE_EMPTY_SCAN
    assert ls.SCOPE_EMPTY_SCAN != ls.SCOPE_UNKNOWN
    assert ls.SCOPE_WATCHLIST_ONLY != ls.SCOPE_FULL_MARKET
    assert len({ls.SCOPE_FULL_MARKET, ls.SCOPE_WATCHLIST_ONLY,
                ls.SCOPE_EMPTY_SCAN, ls.SCOPE_UNKNOWN}) == 4
    full = _row_scope(quotes=5000, active=5900, expected=6000, universe=5900)
    empty = _row_scope(quotes=5000, active=0, expected=6000, universe=0)
    assert full["universe_scope_state"] == ls.SCOPE_FULL_MARKET
    assert empty["universe_scope_state"] == ls.SCOPE_EMPTY_SCAN


def test_full_market_positive_text_requires_measured_evidence():
    """**只有**测到了、且每轮都有明确扫描范围，才允许肯定句。"""
    rows = [_row_scope(quotes=5900, active=5900, expected=6000,
                       universe=5900) for _ in range(30)]
    agg = _ls()._universe_session(rows)
    assert agg["scope_measured_rounds"] == 30
    assert agg["full_market_rounds"] == 30
    m = _base()
    m["setup"] = {"universe_size": 5900, "universe_truth": _green_t0()}
    m["universe_session"] = agg
    det = _detail(_ls().evaluate_health(m), "universe_scope")
    assert _asserts_full_market(det), f"30/30 有证据时才可以肯定：{det}"


def test_mid_session_active_coverage_drop_is_consumed():
    """**核心 RED（C3）**：会话内活跃覆盖下降必须进判决。

    `IT-P1-UNIVERSE-ACTIVE-COVERAGE-SESSION-BLIND-001`（20:04 §5）：

    修前 per-round 样本**根本不采** `expected_total / active_scan_codes /
    coverage_active`，`universe_coverage` 只读 t0。于是：

        t0: 5900/6000 = 98.33%  -> ok
        会话: 4500/6000 = 75%   -> **被完全掩盖**

    实测修前 `universe_coverage = ok`，文案还是 t0 的 98.33%。
    """
    rows = [_row_scope(quotes=4500, active=4500, expected=6000,
                       universe=4500) for _ in range(30)]
    s = rows[0]
    assert s.get("universe_coverage_active") == 0.75, (
        f"轮样本必须采到覆盖率，实得 {s.get('universe_coverage_active')}")
    assert s.get("universe_expected_total") == 6000
    assert s.get("universe_active_scan_codes") == 4500
    agg = _ls()._universe_session(rows)
    assert agg["coverage_active_measured_rounds"] == 30
    assert agg["coverage_active_min"] == 0.75
    m = _base()
    m["setup"] = {"universe_size": 5900, "universe_truth": _green_t0()}
    m["universe_session"] = agg
    v = _ls().evaluate_health(m)
    assert _lvl(v, "universe_coverage") == "fail", (
        f"会话内 75% 覆盖必须判 fail（t0 98.33% 不得掩盖它），"
        f"实得 {_lvl(v, 'universe_coverage')}：{_detail(v, 'universe_coverage')}")


def test_session_coverage_can_judge_when_t0_denominator_unknown():
    """会话数值证据**可以**在 t0 分母未知时独立支撑结论。

    修前那种情形只会输出"分母未知 -> 无法计算"，即使会话里已经有
    30 轮真实覆盖率。**有证据却判未测量**是另一种误判。
    """
    rows = [_row_scope(quotes=4500, active=4500, expected=6000,
                       universe=4500) for _ in range(30)]
    t = _green_t0()
    t["denominator_kind"] = "unknown"
    t["expected_total"] = 0
    t["active_scan_codes"] = 0
    t["coverage_active"] = None
    m = _base()
    m["setup"] = {"universe_size": 4500, "universe_truth": t}
    m["universe_session"] = _ls()._universe_session(rows)
    v = _ls().evaluate_health(m)
    assert _lvl(v, "universe_coverage") == "fail", (
        f"t0 无分母但会话有 75% 覆盖时必须判 fail，"
        f"实得 {_lvl(v, 'universe_coverage')}")


def test_session_coverage_unknown_stays_unmeasured():
    """三态纪律：会话**没采到**覆盖率时不得凭空判红或判绿。"""
    m = _base()
    m["setup"] = {"universe_size": 5900, "universe_truth": _green_t0()}
    m["universe_session"] = _green_session()      # 无 coverage_* 键
    v = _ls().evaluate_health(m)
    assert _lvl(v, "universe_coverage") == "ok", (
        "旧报告/未采集 -> 维持 t0 判定，不得因缺数据判红")


def test_optional_int_is_tri_state():
    """`_optional_int`：**未知 != 0**（0 是"测出来是 0"，会变成 0% 覆盖）。"""
    ls = _ls()
    fn = getattr(ls, "_optional_int", None)
    assert fn is not None, "必须有 _optional_int"
    assert fn(None) is None
    assert fn(None) != 0
    assert fn(0) == 0
    assert fn(True) is None, "bool 必须先于 int 拦掉（isinstance(True,int) 为真）"
    assert fn("6000") == 6000
    assert fn("abc") is None
    assert fn(float("nan")) is None


def test_old_archived_reports_take_honest_branch():
    """**回归护栏（C4）**：`:1576` 的"无法判定"分支是**活代码**。

    ADDENDUM2 §1 撤回"永不可达"：实测真实归档
    `data/live_session_*.json` **7/7** 都没有 `universe_session` 键。

    **这条测试存在的意义是防止未来有人"清理死代码"时把它删掉** ——
    删掉它会把唯一的诚实旧报告路径换成肯定句，**那是引入回归**。
    """
    agg = _ls()._universe_session([])
    m = _base()
    m["setup"] = {"universe_size": 5000, "universe_truth": _green_t0()}
    # 模拟旧报告：有 setup.universe_size，但**没有** universe_session 键。
    m.pop("universe_session", None)
    _ = agg
    v = _ls().evaluate_health(m)
    det = _detail(v, "universe_scope")
    assert "无法判定" in det, (
        f"旧报告（无 universe_session）必须走诚实的'无法判定'分支：{det}")
    assert not _asserts_full_market(det)


# ===========================================================================
# WP02：freshness clocks / config / exception exit
# ===========================================================================
def test_fallback_does_not_reset_full_market_clock():
    """**核心 RED**：降级到自选股**不得**把"2h 没拿到全市场"改写成"0s"。

    实测修前：`freshness.state` 从 stale 变 fresh、`age 7200 -> 0`、
    fail 降级成 warn，且 detail 句子为假。
    """
    import time
    eng = _engine(_Src(0, fail=True), watchlist=["600000", "000001"])
    eng._universe_refreshed_at = time.time() - 7200.0
    eng._universe_full_market_at = time.time() - 7200.0
    eng._codes_raw = []
    eng._maybe_refresh_universe(force=True)
    ut = eng.universe_truth()
    assert ut["active_scan_codes"] == 2, "应已降级为自选股"
    assert ut["full_market_age_s"] > 3000, (
        f"全市场年龄不得被重置，实得 {ut['full_market_age_s']}")
    assert ut["freshness"]["state"] == "stale", "降级后新鲜度仍应 stale"


def test_fallback_keeps_verdict_fail():
    """经判决层：降级后新鲜度必须仍是 fail，不得降级为 warn。"""
    import time
    eng = _engine(_Src(0, fail=True), watchlist=["600000", "000001"])
    eng._universe_refreshed_at = time.time() - 7200.0
    eng._universe_full_market_at = time.time() - 7200.0
    eng._codes_raw = []
    eng._maybe_refresh_universe(force=True)
    m = _base()
    m["setup"] = {"universe_size": 2, "universe_truth": eng.universe_truth()}
    v = _ls().evaluate_health(m)
    assert _lvl(v, "universe_freshness") == "fail"


def test_freshness_reads_official_config_key(tmp_path):
    """**核心 RED**：新鲜度阈值必须来自 `poll.universe_refresh_seconds`。

    修前读 `universe.refresh_seconds` —— 该键**不存在**，`get()` 严格
    dotted lookup 永远返回 None，于是回硬编码。运维改真键
    **改变了真实 TTL 却改不了判决阈值**（行为时钟与健康时钟漂移）。
    """
    import time
    from arad.config import load_settings
    from arad.engine import Engine
    from arad.session import TradingCalendar

    cfg = tmp_path / "over.yaml"
    cfg.write_text("poll:\n  universe_refresh_seconds: 60\n",
                   encoding="utf-8")
    st = load_settings(cfg, use_cache=False)
    assert st.get("poll.universe_refresh_seconds") == 60
    assert st.get("universe.refresh_seconds") is None, "该键不存在（正是缺陷）"

    src = _Src(120)
    eng = Engine(source=src, settings=st, calendar=TradingCalendar(),
                 watchlist=[])
    eng._universe_src = [src]
    eng.refresh_universe()
    eng._universe_full_market_at = time.time() - 200.0
    ut = eng.universe_truth()
    assert ut["freshness"]["warn_s"] == 60.0, "阈值必须跟随正式配置键"
    assert ut["freshness"]["fail_s"] == 120.0
    # 端到端：200s > fail_s(120s) -> fail
    m = _base()
    m["setup"] = {"universe_size": 5000, "universe_truth": ut}
    v = _ls().evaluate_health(m)
    assert _lvl(v, "universe_freshness") == "fail", (
        "判决层必须消费 Engine 导出的阈值，否则改配置仍然无效")


def test_failed_refresh_ledger_stays_self_consistent():
    """三态失败出口：`all_failed` 必须落账，active pool 保留。"""
    src = _Src(120)
    eng = _engine(src)
    eng.refresh_universe()
    ut1 = eng.universe_truth()
    assert ut1["attempt_status"] == "applied"
    src.fail = True
    eng.refresh_universe()
    ut2 = eng.universe_truth()
    assert ut2["attempt_status"] == "all_failed"
    assert ut1 != ut2
    assert ut2["active_scan_codes"] == 120


def test_outer_exception_exit_is_recorded():
    """异常出口也要落账（`crashed`），且 refresh_id 与台账自洽，异常原样抛出。

    13:55 B2 / 16:13 §1.2。**严重度按 16:13 判 P2 robustness**
    （无生产可达触发证据），所以这里只验机制，不主张 P0。
    """
    class _Boom(_Src):
        def universe(self):
            raise ValueError("boom from outer")

    src = _Boom(120)
    eng = _engine(src)
    eng.refresh_universe()
    rid1 = eng._universe_refresh_id
    # 让 `_universe_sources()` 自身抛出 -> 走外层 finalizer
    def _bad():
        raise RuntimeError("sources chain exploded")
    eng._universe_sources = _bad
    with pytest.raises(RuntimeError):
        eng.refresh_universe()
    ut = eng.universe_truth()
    assert ut["attempt_status"] == "crashed", (
        f"异常出口必须落账，实得 {ut['attempt_status']!r}")
    assert eng._universe_refresh_id == rid1 + 1, "id 与台账必须自洽"


# ===========================================================================
# WP01 / GREEN：Consumer Registry
# ===========================================================================
@pytest.mark.parametrize("fact,item,mutate", [
    ("rounds_transport_incomplete", "universe_transport",
     {"rounds_transport_incomplete": 5}),
    ("worst_state", "universe_freshness", {"worst_state": "stale",
                                           "states": {"stale": 30}}),
    ("ever_watchlist_only", "universe_scope",
     {"ever_watchlist_only": True, "watchlist_only_rounds": 3}),
    ("rounds_not_applied", "universe_freshness",
     {"rounds_not_applied": 7, "attempt_statuses": {"all_failed": 7}}),
])
def test_every_session_fact_has_a_consumer(fact, item, mutate):
    """Consumer Registry（16:13 §9.1）：每个 session 事实都必须有消费者，
    且**修改它必须改变完整判决签名** —— 只断言"字段存在"不算完成。
    """
    a = _green_metrics()
    b = _green_metrics()
    b["universe_session"] = _green_session(**mutate)
    va, vb = _ls().evaluate_health(a), _ls().evaluate_health(b)
    assert _sig(va) != _sig(vb), (
        f"事实 {fact} 改了但判决签名不变 —— 它是死变量（零读者）")


# ===========================================================================
# `evaluability_fail_coverage`：说谎的旋钮
# ===========================================================================
def _eval_metrics(ev, tot):
    m = _green_metrics()
    m["signal_evaluability"] = {
        "volume_burst": {"considered": tot, "evaluable": ev,
                         "blocked_capability": tot - ev, "advisory_missing": 0,
                         "hit_candidates": 0, "published": 0,
                         "rule_selected": 0, "bus_accepted": 0,
                         "committed": 0, "dropped_by_bus": 0,
                         "blocked_reasons": {}},
    }
    m["evaluability_considered_total"] = tot
    m["evaluability_evaluable_total"] = ev
    m["evaluability_blocked_total"] = tot - ev
    return m


def test_evaluability_fail_coverage_knob_actually_works():
    """**核心 RED**：`evaluability_fail_coverage` 曾是**说谎的旋钮**。

    13:32 §1 / 16:13 §9.1：`ev_fail_cov = _num("evaluability_fail_coverage")`
    取出来**从未被读取** —— 运维把 `evaluability_fail_coverage: 0.5`
    写进配置**静默无效**。
    """
    m = _eval_metrics(ev=800, tot=1000)        # coverage = 0.80
    # 默认 0.0 -> 既有行为不变（0.80 > 0，不触发 fail）
    v_default = _ls().evaluate_health(m)
    assert _lvl(v_default, "capability") != "fail", "默认必须保持既有行为"
    # 显式设 0.90 -> 0.80 < 0.90 -> 必须 fail
    v_tight = _ls().evaluate_health(m, tolerances={
        "evaluability_fail_coverage": 0.90})
    assert _lvl(v_tight, "capability") == "fail", (
        "设了阈值就必须生效 —— 否则它是个说谎的旋钮")
    assert _sig(v_default) != _sig(v_tight)


def test_evaluability_default_preserves_full_block_only_fail():
    """默认值 0.0 必须**逐字节**保持"只有全部阻断才 fail"的既有语义。"""
    m = _eval_metrics(ev=1, tot=1000)          # 极低但非 0
    v = _ls().evaluate_health(m)
    assert _lvl(v, "capability") != "fail", "非 0 覆盖不应在默认档判 fail"
    m0 = _eval_metrics(ev=0, tot=1000)
    v0 = _ls().evaluate_health(m0)
    assert _lvl(v0, "capability") == "fail", "全部阻断仍必须 fail"
