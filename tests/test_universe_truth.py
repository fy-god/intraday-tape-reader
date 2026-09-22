"""R-12 / WP01（universe 分母）+ WP04（空轮账本对账）回归测试。

来源：云端 `2026-09-22_08-08-19_JST.md`（reviewed `2406d81`）。

* §4  `R-12 / IT-P1-UNIVERSE-DENOMINATOR-001`：health 只有绝对阈值，
  **分母缺失** —— 实测 `4500/5913 = 76.10%` 被判 OK，
  与 `4100/4200 = 97.62%` **无法区分**。
* §7  `IT-P2-OBS-EMPTY-ROUND-R1`：我上一轮**自己新增**的空轮分支
  手写了一套简化 request 算术，产出**幻影** `index_requested`，
  且 early return 早于 `_poll_count += 1` -> **poll identity 断裂**。

设计纪律：
* 新符号一律用 ``getattr`` 在**函数体内**取，避免 ImportError 造成
  "结构性 ERROR" 冒充行为级 RED。
* 绝不把 `5913` 写成产品常量 —— 本文件里它只是 provider fixture。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tools"))


def _ls():
    """延迟导入 ``live_session`` —— 顶层导入会让回退验牙变成结构性 ERROR。"""
    import live_session
    return live_session


# ===========================================================================
# 工具
# ===========================================================================
def _setup(universe_truth=None, *, size=4500, fell=False):
    s = {"engine_build_s": 1.0, "universe_refresh_s": 1.0,
         "universe_size": size, "fell_back_to_watchlist": fell}
    if universe_truth is not None:
        s["universe_truth"] = universe_truth
    return s


def _ut(expected, active, *, kind="provider_declared_total", raw=None,
        usable=None, transport=None):
    """构造一个 provider 声明了分母的 universe_truth。"""
    return {
        "denominator_kind": kind,
        "expected_total": expected,
        "transport_expected_total": expected,
        "raw_unique_codes": expected if raw is None else raw,
        "usable_quotes": active if usable is None else usable,
        "active_scan_codes": active,
        "coverage_transport": 1.0 if transport is None else transport,
        "coverage_usable": (active / expected) if expected else None,
        "coverage_active": (active / expected) if expected else None,
        "denominator_known": bool(expected),
    }


def _base_metrics():
    """一份"除了被测项之外全都健康"的 metrics。

    必须完整 —— 缺项会让其它判决项转红，掩盖被测项。
    """
    m = _ls().empty_metrics()
    m.update({
        "rounds": 30, "alerts_total": 0, "alerts_by_kind": {},
        "quotes": {"min": 4500, "max": 4500, "last": 4500, "first": 4500},
        "universe": {"min": 4500, "max": 4500, "last": 4500, "first": 4500},
        "rounds_with_observation": 30, "coverage_rounds": 30,
        "coverage": 0.99, "coverage_p05": 0.99, "coverage_p50": 0.99,
        "coverage_min": 0.99, "coverage_max": 1.0,
        "requested_total": 4500 * 30, "returned_total": 4500 * 30,
        "admitted_total": 4500 * 30, "source_mix": {"eastmoney": 30},
        "source_mix_rounds": 30, "capability_unavailable_ratio": 0.0,
        "memory": {"bounded": True, "reason": "ok"},
        "api": {"requests": 300, "non_200": 0, "malformed": 0,
                "unreachable": 0, "by_route": {}},
        "sse": {"connected": True, "stalled": False, "events_total": 50,
                "events_by_type": {}, "status": None, "error": None},
    })
    return m


def _verdict(m):
    return _ls().evaluate_health(m)


def _cov_item(v):
    return next((c for c in v["checks"] if c["name"] == "universe_coverage"),
                None)


# ===========================================================================
# §1 R-12：分母必须进入事实链
# ===========================================================================
def test_universe_coverage_is_a_check_item():
    """`universe_coverage` 必须是一个真实判决项（R-12）。"""
    v = _verdict(_base_metrics())
    names = [c["name"] for c in v["checks"]]
    assert "universe_coverage" in names, (
        f"R-12：判决层必须读**相对覆盖**，当前项：{names}")


def test_engine_universe_truth_exists():
    """引擎必须导出股票池真相（`_universe_meta` 此前零出口）。"""
    from arad.engine import Engine
    assert hasattr(Engine, "universe_truth"), (
        "必须有 universe_truth() —— `_universe_meta` 不能继续零出口")


def test_universe_truth_exposes_four_coverages():
    """四个 coverage 必须**分开**，不得合并成一个数。"""
    from arad.engine import Engine
    eng = Engine.__new__(Engine)
    eng._universe_meta = {
        "transport_expected_total": 100, "raw_unique_codes": 100,
        "usable_quotes": 94, "transport_complete": True, "shortfall": 0,
    }
    eng._codes_raw = [f"c{i}" for i in range(100)]
    ut = eng.universe_truth()
    for k in ("coverage_transport", "coverage_usable", "coverage_active"):
        assert k in ut, f"缺 {k}"
    assert ut["coverage_transport"] == pytest.approx(1.0)
    assert ut["coverage_usable"] == pytest.approx(0.94)
    assert ut["coverage_active"] == pytest.approx(1.0)


def test_universe_truth_membership_not_usable_quotes():
    """**成员集不能等于可用 Quote 数**（云端 §8.2）。

    transport 拿到了 100 个代码，其中 94 个能构造 Quote ——
    active membership 必须仍是 100，不是 94。
    把二者静默等同，会让"市场里有停牌股"被读成"扫描范围缩了"。
    """
    from arad.engine import Engine
    eng = Engine.__new__(Engine)
    eng._universe_meta = {
        "transport_expected_total": 100, "raw_unique_codes": 100,
        "usable_quotes": 94, "transport_complete": True, "shortfall": 0,
    }
    eng._codes_raw = [f"c{i}" for i in range(100)]
    ut = eng.universe_truth()
    assert ut["active_scan_codes"] == 100, (
        "active membership 必须来自代码成员集，不是 usable_quotes")
    assert ut["usable_quotes"] == 94
    assert ut["coverage_usable"] == pytest.approx(0.94)
    assert ut["coverage_active"] == pytest.approx(1.0)


@pytest.mark.parametrize("expected,active,expect_fail", [
    (5913, 5850, False),   # 98.93% -> ok
    (4200, 4100, False),   # 97.62% -> ok
    (5913, 4600, True),    # 77.79% -> **fail**（修前是 OK）
    (5913, 4500, True),    # 76.10% -> **fail**（修前是 OK，云端关键行）
    (5913, 4100, True),    # 69.34% -> fail
])
def test_active_coverage_matrix(expected, active, expect_fail):
    """云端 EXP-IT-UNIVERSE-TRUTH-002 矩阵：分母已知时覆盖度必须可区分。

    核心断言：`4500/5913 = 76.10%` **不再**判 OK ——
    修前它与 `4100/4200 = 97.62%` 得到完全相同的结果。
    """
    m = _base_metrics()
    m["setup"] = _setup(_ut(expected, active))
    v = _verdict(m)
    assert ("universe_coverage" in v["fail"]) is expect_fail, (
        f"{active}/{expected} = {100.0 * active / expected:.2f}% "
        f"期望 fail={expect_fail}，实得 fail={v['fail']}")


def test_relative_coverage_distinguishes_same_absolute_count():
    """**同一个绝对只数**必须因分母不同而得到不同结论（R-12 的本质）。"""
    good, bad = _base_metrics(), _base_metrics()
    good["setup"] = _setup(_ut(4200, 4100))
    bad["setup"] = _setup(_ut(5913, 4100))
    vg, vb = _verdict(good), _verdict(bad)
    assert vg["healthy"] != vb["healthy"], (
        "active 都是 4100，但 97.62% 与 69.34% 必须判得不一样 —— "
        "这正是'分母缺失'导致旧绝对门禁失效的证据")


def test_denominator_unknown_is_not_measured_not_fail():
    """分母未知 -> **未测量判 ok**，不许假装知道，也不许判红（云端 §8.5）。

    新浪 clean pagination 能证明"翻页翻完了"，但没有 numeric total ——
    二者不能硬塞成同一种 coverage。
    """
    m = _base_metrics()
    m["setup"] = _setup(_ut(0, 5000, kind="pagination_exhausted_non_numeric"))
    v = _verdict(m)
    item = _cov_item(v)
    assert item is not None and item["level"] == "ok"
    assert v["healthy"] is True


def test_missing_universe_truth_charges_nothing():
    """旧报告没有 universe_truth -> 跳过，不算失败（否则所有旧绿测翻红）。"""
    m = _base_metrics()
    m["setup"] = _setup(None)
    v = _verdict(m)
    assert "universe_coverage" not in v["fail"]


@pytest.mark.parametrize("evil", [
    None, "boom", {}, {"expected_total": "x"},
    {"expected_total": 100, "active_scan_codes": "y"},
    {"coverage_active": "z", "expected_total": 100},
    {"expected_total": 100, "coverage_active": None},
])
def test_coverage_gate_tolerates_dirty_values(evil):
    """脏值**绝不许**让判决层抛异常。

    实测：`{'coverage_active': 'z', 'expected_total': 100}` 曾让
    `evaluate_health` 抛 `ValueError` —— 判决层崩溃比判错更糟：
    调用方拿到异常而不是"不健康"。
    """
    m = _base_metrics()
    m["setup"] = {"universe_size": 4500, "universe_truth": evil}
    v = _verdict(m)          # 不抛即通过
    assert "healthy" in v


def test_c_round_cannot_mask_c_active():
    """**C_round 永远不能替代 C_active**（云端 §8.3）。

    本轮每个请求都回来了（C_round=100%），但只扫描了全市场 76% ——
    判决必须仍然转红。否则"我要的拿到了"会掩盖"我要的不是全市场"。
    """
    m = _base_metrics()
    m["coverage"] = 1.0
    m["coverage_p05"] = 1.0
    m["coverage_min"] = 1.0
    m["setup"] = _setup(_ut(5913, 4500))
    v = _verdict(m)
    assert "universe_coverage" in v["fail"], (
        "C_round=100% 不得掩盖 C_active=76.10% 的缺口")


def test_store_status_exposes_universe_truth():
    """分母必须沿 status() 出去（`_universe_meta` 的第一个生产出口）。"""
    from arad.store import AlertStore
    st = AlertStore.__new__(AlertStore)
    import threading
    st._lock = threading.RLock()

    class _E:
        _codes = ["a", "b"]

        def universe_truth(self):
            return {"expected_total": 100, "active_scan_codes": 2}

    st._engine = _E()
    assert hasattr(st, "status")
    # 只断言 status 的源码里确实读了 universe_truth（避免构造整个 Store）
    import inspect
    src = inspect.getsource(AlertStore.status)
    assert "universe_truth" in src, (
        "status() 必须导出 universe_truth，否则分母到不了 health")


# ===========================================================================
# §2 WP04：空轮账本必须与正常路径对账
# ===========================================================================
def test_request_arithmetic_is_single_source_of_truth():
    """`_request_arithmetic` 必须是唯一事实来源，两条路径都调它。"""
    from arad.engine import Engine
    assert hasattr(Engine, "_request_arithmetic"), (
        "必须有 _request_arithmetic() —— 不许两条路各写一套")
    import inspect
    src = inspect.getsource(Engine.poll_once)
    assert src.count("_request_arithmetic()") >= 2, (
        "正常路径与空轮分支**都**必须调 _request_arithmetic()，"
        f"当前只出现 {src.count('_request_arithmetic()')} 次")


def test_empty_round_has_no_phantom_index_request():
    """**幻影 index_requested**：`_wants_indices=False` 时不许报指数请求。

    实测（500 只个股 + 5 个指数码 + spirit_index 关）：
    修前账本报 `requested=505 / index_requested=5`，而实际 dispatch 只有 500。
    这让观测层无法区分"指数抓了没回来"与"根本没抓"。

    ⚠ 用**子类**覆盖 property，不要去改真实的 ``Engine`` 类 ——
    我第一版写成 ``del eng.__class__._wants_indices``，把真实类的 property
    删掉了，直接污染同进程后续所有测试（这正是"测试自己制造缺陷"）。
    """
    from arad.engine import Engine

    class _Eng(Engine):
        def __init__(self, wants):
            self._wants = wants

        @property
        def _wants_indices(self):
            return self._wants

    eng = _Eng(False)
    eng._codes = [f"c{i}" for i in range(500)]
    eng.index_codes = ["000001", "399001"]
    st_req, idx_req, total = eng._request_arithmetic()
    assert idx_req == 0, f"未 dispatch 指数却报 index_requested={idx_req}"
    assert total == 500, f"requested 应为 500（实得 {total}）"

    eng2 = _Eng(True)
    eng2._codes = [f"c{i}" for i in range(500)]
    eng2.index_codes = ["000001", "399001"]
    _, idx_req2, total2 = eng2._request_arithmetic()
    assert idx_req2 == 2, f"dispatch 了 2 个指数却报 {idx_req2}"
    assert total2 == 502

    # 收尾自证：真实 Engine 的 property 必须**仍然健在**
    assert isinstance(Engine.__dict__["_wants_indices"], property), (
        "测试不得破坏真实 Engine 类")


def test_poll_count_advances_on_every_poll():
    """**poll identity**：空轮也是一次 poll，必须推进 poll 计数。

    实测修前：连续 6 个空轮得到
        observation_seq        = 1,2,3,4,5,6
        observation_poll_count = 1,1,1,1,1,1
    "第 N 轮观测"无法与"第几次 poll"对上。
    """
    from arad.engine import Engine
    assert hasattr(Engine, "_bump_poll_count"), (
        "必须有 _bump_poll_count() 作为唯一自增点")
    import inspect
    src = inspect.getsource(Engine.poll_once)
    assert "_bump_poll_count()" in src, "正常路径必须用 _bump_poll_count()"
    assert "self._poll_count += 1" not in src, (
        "poll 自增已收口到 _bump_poll_count()，poll_once 里不许再直接自增")


# ===========================================================================
# §3 端到端：真实 Engine 走空轮，账本必须自洽
# ===========================================================================
class _SilentSource:
    """第 1 轮给满量，之后返回空且**不抛异常**。"""

    name = "silent"

    def __init__(self, codes):
        self.codes = list(codes)
        self.round = 0

    def snapshots(self, codes=None, **kw):
        from arad.models import Board, Quote
        self.round += 1
        if self.round == 1:
            return [Quote(code=c, name="x", board=Board.MAIN, price=10.0,
                          prev_close=10.0, open=10.0, high=10.0, low=10.0,
                          volume_lots=100, amount=1000.0,
                          ts=datetime(2026, 9, 21, 10, 0, 0))
                    for c in self.codes]
        return []

    def universe(self, **kw):
        return list(self.codes)

    def capabilities(self):
        from arad.capabilities import capabilities_for
        return capabilities_for("replay")

    def health(self):
        return {"name": "silent", "ok": True, "latency_ms": 1, "err": ""}


def test_e2e_empty_round_ledger_is_reconcilable():
    """**端到端**：连续空轮时 seq / poll_count 必须同步推进，且无幻影请求。"""
    from arad.config import load_settings
    from arad.engine import Engine
    from arad.session import TradingCalendar

    codes = [f"60{i:04d}" for i in range(500)]
    eng = Engine(source=_SilentSource(codes), settings=load_settings(use_cache=False),
                 calendar=TradingCalendar(), watchlist=[])
    eng._codes = list(codes)
    eng._codes_pinned = True
    store = eng.store

    seqs, polls, idx_reqs, reqs = [], [], [], []
    for _ in range(5):
        eng.poll_once(force=True)
        seqs.append(int(getattr(store, "observation_seq", 0) or 0))
        polls.append(int(store.observation_poll_count))
        obs = store.observation or {}
        idx_reqs.append(int(obs.get("index_requested") or 0))
        reqs.append(int(obs.get("requested") or 0))

    assert len(set(seqs)) == len(seqs), f"seq 必须逐轮递增：{seqs}"
    assert polls == seqs, (
        f"poll identity 断裂：seq={seqs} 而 poll_count={polls} —— "
        f"空轮也必须在账本里有自己的 poll 序号")
    assert all(r == 0 for r in idx_reqs), (
        f"spirit_index 关闭时不许报指数请求（幻影）：{idx_reqs}")
    assert all(r == len(codes) for r in reqs), (
        f"requested 必须只有个股 {len(codes)}：{reqs}")
