"""云端 h=0 轮（2026-09-22_00-43-31_JST）§4 的 3 个"同 bug 类"存活实例 —— 全部已复核并修复。

主线（cloud bug class）：**有界容器 / 累计缓存的长度被当作完整总量。**

| 实例 | 位置 | 我的复核 |
|---|---|---|
| §4.1 逐轮行情数用累计缓存 | `tools/live_session.py` 写入点 | **成立**，最严重 |
| §4.2 `_acked` 裁剪保留"任意"2000 | `src/arad/store.py` | **成立**，用户可见 |
| §4.3 `status()['universe']` 用累计缓存 | `src/arad/store.py` | **成立** |
| `R-21` universe 无判决项 | `tools/live_session.py` | **成立**，根因=数据早已传入只差读者 |

我 01:00 轮写"同一 bug 类我漏了第二个实例"——本轮证明**漏的不止第二个**。

本文件只用既有 API 断言，新符号一律 `getattr` 在函数内取，
确保回退验牙时失败落在**断言**上（行为级）而不是顶层 ImportError（结构性）。
"""
from __future__ import annotations

import copy
import os
import sys
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

TS = datetime(2026, 9, 21, 10, 0, 0)


def _ls():
    import live_session
    return live_session


def _base_metrics() -> dict:
    """逐项都过、判决必然 healthy 的 metrics（含 setup）。"""
    m = _ls().empty_metrics()
    m.update({
        "rounds": 30,
        "alerts_total": 0,
        "alerts_by_kind": {},
        "quotes": {"min": 5913, "max": 5913, "last": 5913, "first": 5913},
        "universe": {"min": 5913, "max": 5913, "last": 5913,
                     "first": 5913},
        "rounds_with_observation": 30,
        "coverage_rounds": 30,
        "coverage": 0.999,
        "coverage_p05": 0.99,
        "coverage_p50": 0.999,
        "coverage_min": 0.99,
        "coverage_max": 1.0,
        "requested_total": 5913 * 30,
        "returned_total": 5908 * 30,
        "admitted_total": 5908 * 30,
        "source_mix": {"tencent": 30},
        "source_mix_rounds": 30,
        "capability_unavailable_ratio": 0.0,
        "memory": {"bounded": True, "reason": "ok"},
        "api": {"requests": 300, "non_200": 0, "malformed": 0,
                "unreachable": 0, "by_route": {}},
        "sse": {"connected": True, "stalled": False, "events_total": 50,
                "events_by_type": {}, "status": None, "error": None},
        "setup": {"engine_build_s": 1.0, "universe_refresh_s": 2.0,
                  "universe_size": 5913, "fell_back_to_watchlist": False},
    })
    return m


# ===========================================================================
# §4.1 逐轮行情数不得用累计缓存
# ===========================================================================
def test_state_quotes_never_shrinks():
    """先把"累计缓存从不裁剪"这个**事实**钉住（这是根因，不是缺陷本身）。"""
    from arad.engine import EngineState
    from arad.models import Quote

    st = EngineState()
    for i in range(500):
        code = f"60{i:04d}"
        st.quotes[code] = Quote(code=code, name="x", board="SH", price=10.0,
                                prev_close=10.0, open=10.0, high=10.0,
                                low=10.0, volume_lots=100, amount=1000.0,
                                ts=TS)
    before = len(st.quotes)
    assert before == 500
    try:
        st.prune(keep_codes={"sh600001"})
    except TypeError:
        st.prune()
    assert len(st.quotes) == before, (
        "state.quotes 应**从不裁剪** —— 这正是不能拿它当逐轮行情数的原因")


def test_round_quote_count_prefers_per_round_observation():
    """**核心验收**：本轮行情数必须取 observation 的 per-round 计数。

    修复前写的是 ``quotes=len(state.quotes)`` —— provider 连续返回 0 条时
    它恒为累计值（实测 5913），于是 ``no_data_rounds``（判据
    ``int(r['quotes']) <= 0``）**结构上恒为 0**。
    """
    ls = _ls()
    fn = getattr(ls, "_round_quote_count", None)
    assert fn is not None, "必须提供 _round_quote_count（本轮新增）"

    class _State:
        quotes = {f"60{i:04d}": object() for i in range(5913)}

    # 本轮 observation 说返回 0 条 -> 必须报 0，**不能**报累计的 5913
    assert fn({"returned": 0, "admitted": 0}, _State()) == 0
    assert fn({"returned": 12, "admitted": 10}, _State()) == 12
    # returned 缺失时退回 admitted
    assert fn({"admitted": 7}, _State()) == 7
    # 完全拿不到观测 -> 退化到累计（明确是退化，不是正确值）
    assert fn({}, _State()) == 5913
    assert fn(None, _State()) == 5913


def test_round_quote_count_tolerates_dirty_values():
    """脏观测值不许让 soak 崩。"""
    ls = _ls()
    fn = getattr(ls, "_round_quote_count", None)
    assert fn is not None

    class _State:
        quotes = {}

    for evil in ({"returned": "x"}, {"returned": None}, {"returned": -1},
                 {"returned": []}, {"admitted": "boom"}, "notadict"):
        assert fn(evil, _State()) == 0 or isinstance(fn(evil, _State()), int)


def test_soak_loop_does_not_use_state_quotes_as_round_count():
    """**源码级**验收：写入点不得再出现 ``quotes=len(state.quotes)``。

    这是防回归的硬钉子 —— 只要有人改回去，这条立刻红。
    """
    src = open(os.path.join(ROOT, "tools/live_session.py"),
               encoding="utf-8").read()
    assert "quotes=len(getattr(state, \"quotes\"" not in src, (
        "写入点不得把累计缓存当逐轮行情数（IT-P1-OBS-EMPTY-ROUND-001）")
    assert "_round_quote_count(" in src, "必须通过 _round_quote_count 取数"


def test_silent_outage_must_not_be_healthy():
    """**最重要的验收**：静默断供（provider 返回空、**不抛异常**）不许判健康。

    修复前实测：``no_data_rounds=0``（因为 quotes 谎报 5913）、
    ``error_rounds=0`` -> ``healthy=True / exit=0 / fail=[]``，
    与健康基线**无法区分**。
    对照：同一断供但 quotes 诚实（0）时正确转红 ``['fetch','data']``。
    """
    ls = _ls()
    base = _base_metrics()
    v_ok = ls.evaluate_health(copy.deepcopy(base))
    assert v_ok["healthy"] is True, v_ok.get("fail")

    m = copy.deepcopy(base)
    m["no_data_rounds"] = 6
    m["error_rounds"] = 0
    m["quotes"] = {"min": 0, "max": 0, "last": 0, "first": 0}
    v = ls.evaluate_health(m)
    assert v["healthy"] is False, (
        "静默断供必须判不健康 —— 否则就是完整假绿")
    assert "fetch" in v["fail"] or "data" in v["fail"], v["fail"]


def test_empty_round_publishes_observation():
    """空轮必须也落一份 returned=0 的账（否则断供不进观测账本）。

    修复前 ``poll_once`` 在 ``not quotes and not idx_quotes`` 处直接
    ``return []``，Store 里留的是**上一成功轮**的 observation。
    源码级 + 行为级双钉。
    """
    src = open(os.path.join(ROOT, "src/arad/engine.py"),
               encoding="utf-8").read()
    assert "IT-P1-OBS-EMPTY-ROUND-001" in src, "空轮落账必须被标记"
    # 空轮分支里必须调 set_poll_stats 且 returned=0
    idx = src.find("IT-P1-OBS-EMPTY-ROUND-001")
    window = src[idx:idx + 2600]
    assert "set_poll_stats" in window, "空轮必须发布观测"
    assert "returned=0" in window, "空轮的 returned 必须是 0"


# ===========================================================================
# §4.2 _acked 必须保留"最新"而非"任意" 2000
# ===========================================================================
def _store_with_acks(n: int, keys: list[str]):
    from arad.config import load_settings
    from arad.models import Alert, AlertKind
    from arad.store import AlertStore

    store = AlertStore(load_settings(use_cache=False))
    for k in keys:
        store.add_alert(Alert(key=k, kind=AlertKind.SURGE, code="600000",
                              name="某股", ts=TS, price=10.0, pct=1.0,
                              title="t", detail="d"))
    for k in keys[:n]:
        store.ack(k)
    return store


def test_acked_trim_keeps_latest_not_arbitrary():
    """**核心验收**：裁剪必须保留**最新** 2000，不是哈希序的任意 2000。

    修复前 ``set(list(self._acked)[-2000:])`` —— ``list(set)`` 是哈希序，
    实测 ``set(_acked) == 最新2000`` 在 5 个 ``PYTHONHASHSEED`` 下**全为 False**。
    """
    n = 5001
    keys = [f"k{i}" for i in range(n)]
    store = _store_with_acks(n, keys)

    acked = getattr(store, "_acked", None)
    assert acked is not None
    assert len(acked) == 2000
    assert set(acked) == set(keys[-2000:]), (
        "裁剪必须保留**最新** 2000 —— 哈希序会系统性丢掉刚 ack 的那条")
    assert keys[-1] in acked, "最新 ack 的那条必须存活"
    assert keys[0] not in acked, "最旧那条应被丢弃（证明裁剪确实运行了）"


def test_acked_recent_alert_stays_acked():
    """**用户可见**验收：ring buffer 内可见、用户已 ack 的告警，
    ``recent_alerts()`` 必须仍然报 ``acked=True``。

    修复前：看板 ``if(a.acked)`` 会给它加 ``acked`` class（变暗 + 隐藏 ack
    按钮），用户会看到**自己刚确认过的告警重新变成未确认**。
    """
    n = 5001
    keys = [f"k{i}" for i in range(n)]
    store = _store_with_acks(n, keys)
    # 最新那条一定在 ring buffer 内（maxlen=300）
    items = store.recent_alerts(300)
    assert items, "ring buffer 应有内容"
    newest = items[0]["key"]
    assert newest in getattr(store, "_acked", {}), (
        f"ring buffer 内可见的最新告警 {newest} 已 ack，账本里必须还在")
    assert items[0].get("acked") is True, (
        "已 ack 且仍可见的告警不得被报成未确认")


def test_acked_reack_refreshes_recency():
    """重复 ack 同一条必须刷新它的"最近"时序，否则它会被当旧的裁掉。"""
    keys = [f"k{i}" for i in range(5001)]
    store = _store_with_acks(5001, keys)
    # 再 ack 一条很旧的（它此刻不在 _acked 里会被重新记入并排到最新）
    store.ack("k0")
    assert "k0" in getattr(store, "_acked", {})


# ===========================================================================
# §4.3 status()['universe'] 不得用累计缓存
# ===========================================================================
def test_status_universe_is_active_scan_not_cache():
    """**核心验收**：``status()['universe']`` 必须是本轮扫描池，不是累计缓存。

    修复前 ``"universe": len(quotes)``（``state.quotes`` 累计、从不裁剪），
    实测扫描范围缩 30% 后**仍报旧规模**（云端实测高报 1813）。
    """
    from arad.config import load_settings
    from arad.store import AlertStore

    class _Engine:
        _codes = [f"60{i:04d}" for i in range(4100)]     # 真实扫描池
        # store._state() 走 `self._engine.state`
        state = type("_S", (), {
            "quotes": {f"60{i:04d}": object() for i in range(5913)},
            "session": None,
        })()

    store = AlertStore(load_settings(use_cache=False))
    store._engine = _Engine()
    st = store.status()
    uni = st.get("universe")
    assert uni == 4100, (
        f"status()['universe'] 必须是**本轮扫描池** 4100，而不是累计缓存 "
        f"5913（实得 {uni}）—— 否则看板会高报扫描范围")
    # 两个口径都要能读到，否则没法判断上面那个数是哪个
    assert st.get("universe_cached") == 5913


def test_status_universe_tolerates_missing_engine():
    """没有引擎时不许崩，退化但不静默给错数。"""
    from arad.config import load_settings
    from arad.store import AlertStore

    store = AlertStore(load_settings(use_cache=False))
    st = store.status()
    assert "universe" in st


# ===========================================================================
# R-21 universe 必须参与判决
# ===========================================================================
def test_universe_is_a_check_item():
    """**核心验收**：判决里必须真的有一项 ``universe``。

    修复前 ``evaluate_health`` 的 11 项里 ``universe/setup/universe_size/
    fell_back`` **一个 token 都不出现** —— 数据早已传入 setup，只差一个读者。
    """
    v = _ls().evaluate_health(_base_metrics())
    names = [c["name"] for c in v["checks"]]
    assert "universe" in names, (
        f"扫描范围必须参与判决；当前项：{names}")


def test_fell_back_to_watchlist_must_fail():
    """降级为仅自选股 = 全市场扫描不可用 -> 必须判 fail。

    这是用户最需要知道的：此期间"没有告警"**不能**解释为"没有异动"。
    """
    m = _base_metrics()
    m["setup"]["fell_back_to_watchlist"] = True
    m["setup"]["universe_size"] = 2
    v = _ls().evaluate_health(m)
    assert v["healthy"] is False, "降级为自选股却判健康 = 假绿"
    assert "universe" in v["fail"]


def test_trimmed_universe_must_fail():
    """扫描范围缩 30%（5913 -> 4100）必须被判决看见。

    云端 h=0 轮实测：真实运行 5913->4100 得 ``healthy=True / exit=0``。
    """
    m = _base_metrics()
    m["setup"]["universe_size"] = 1500          # 远低于下限
    v = _ls().evaluate_health(m)
    assert v["healthy"] is False, "扫描范围过小却判健康"
    assert "universe" in v["fail"]


def test_moderately_small_universe_warns_without_changing_exit():
    """偏小但未触下限 -> warn，**不改退出码**（避免真实可接受降级把 CI 打红）。"""
    m = _base_metrics()
    m["setup"]["universe_size"] = 4000          # 下限 3000 / 警戒 4500 之间
    v = _ls().evaluate_health(m)
    assert v["healthy"] is True, (
        f"warn 不该改退出码：{v.get('fail')}")
    assert "universe" in v["warn"]


def test_universe_not_measured_charges_nothing():
    """旧报告没有 setup / **真的没测到** -> 跳过，不算失败。

    ⚠ 本用例在 2026-09-23 被**任务定义调整**修正过（不是产品 bug）：

    它原先把 `{"universe_size": 0}` 也归入"未测量"。但
    `IT-P1-UNIVERSE-T0-MEASURED-ZERO-READ-AS-MISSING-001`
    （09-23 00:12 §2）指出 **`None` 与 `0` 是两种语义**：

    ```text
    None = 没有测量到股票池大小      -> NOT_MEASURED -> 跳过不算失败
    0    = 明确测到 active scan = 0  -> FAIL（一只票都没扫）
    ```

    实测（以真实归档绿报告为基线，只替换 t0 快照）：
    `universe_size=0 + active_scan_codes=0 + 分母未知`
    修前 -> `healthy=True / exit_code=0 / fails=[]`（**整场全绿**）。

    "测到 0 只票"是**明确坏**，把它当成"没记录"正是缺陷本体，
    所以 `0` 从这个"不该判红"的集合里移出，改由
    `test_universe_measured_zero_is_red` 覆盖。
    """
    for setup in (None, {}, {"universe_size": None}):
        m = _base_metrics()
        if setup is None:
            m.pop("setup", None)
        else:
            m["setup"] = setup
        v = _ls().evaluate_health(m)
        assert v["healthy"] is True, (
            f"未测量不该判红 setup={setup!r} fail={v.get('fail')}")


def test_universe_measured_zero_is_red():
    """**明确测到 0 只**必须判红 —— 与"没记录"**分开**。

    `IT-P1-UNIVERSE-T0-MEASURED-ZERO-READ-AS-MISSING-001`（09-23 00:12 §2）。
    三态：`None -> NOT_MEASURED`；`0 -> FAIL`；`>0 -> 正常阈值`。
    """
    m = _base_metrics()
    m["setup"] = {"universe_size": 0, "fell_back_to_watchlist": False}
    v = _ls().evaluate_health(m)
    assert "universe" in v["fail"], (
        f"明确测到 0 只票必须进 fail：{v.get('fail')}")
    det = [c for c in v["checks"] if c["name"] == "universe"][0]["detail"]
    assert "0 只" in det, f"必须点名 0 只，不得说'无记录'：{det}"
    assert "无股票池规模记录" not in det, f"不得渲染成没记录：{det}"


def test_universe_check_tolerates_dirty_setup():
    """脏 setup 不许崩。"""
    for evil in ("boom", 42, [], {"universe_size": "x"},
                 {"fell_back_to_watchlist": "yes"}):
        m = _base_metrics()
        m["setup"] = evil
        v = _ls().evaluate_health(m)          # 不得抛
        assert isinstance(v, dict)


@pytest.mark.parametrize("size,expect_fail", [
    (5913, False), (5000, False), (4500, False),
    (4499, False),      # warn 档，不算 fail
    (3000, False),      # 恰好下限，不算 fail
    (2999, True),
    (1, True),
])
def test_universe_thresholds(size, expect_fail):
    """阈值边界必须精确（含恰好等于下限的边界）。"""
    m = _base_metrics()
    m["setup"]["universe_size"] = size
    v = _ls().evaluate_health(m)
    assert ("universe" in v["fail"]) is expect_fail, (
        f"size={size} 期望 fail={expect_fail}，实得 fail={v['fail']}")


# ===========================================================================
# 端到端：真实 _soak_loop + 真实 Engine 走静默断供
# ===========================================================================
class _SilentSource:
    """第 1 轮给满量，之后返回空且**不抛异常**（静默断供）。

    ``health()`` 必须返回 **dict** —— SourceManager 对每个源调它并 ``dict(h)``。
    我第一版写成返回 list，导致 ``poll_once`` 抛 ValueError，
    整条链路根本没跑起来（是探针的错，不是产品缺陷）。
    """

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


class _Probe:
    def __getattr__(self, name):
        def _f(*a, **kw):
            return {}
        return _f


def test_e2e_silent_outage_round_counts_are_honest():
    """**端到端核心验收**：真实 ``_soak_loop`` 走静默断供时逐轮行情数必须诚实。

    修复前：``quotes`` 恒为累计缓存值（第 1 轮灌了多少就一直报多少），
    于是 ``no_data_rounds`` 恒为 0、``add('data', quotes_max>0)`` 恒绿 ——
    **两个专门盯行情数的判决项同时失效**，静默断供拿到完整假绿。

    修复后实测：``quotes`` 序列 = ``[500, 0, 0, 0, 0, 0]``、
    ``no_data_rounds = 5``，且判决转红。
    """
    import time

    from arad.config import load_settings
    from arad.engine import Engine
    from arad.session import TradingCalendar

    ls = _ls()
    codes = [f"60{i:04d}" for i in range(500)]
    eng = Engine(source=_SilentSource(codes),
                 settings=load_settings(use_cache=False),
                 calendar=TradingCalendar(), watchlist=[])
    eng._codes = list(codes)
    eng._codes_pinned = True

    rounds, _flags = ls._soak_loop(
        eng, _Probe(), max_rounds=6,
        deadline=time.monotonic() + 60, interval=0.0, verbose=False)

    quotes = [int(r["quotes"]) for r in rounds]
    assert len(quotes) == 6, quotes
    assert quotes[0] == 500, f"第 1 轮应有 500 条，实得 {quotes[0]}"
    assert all(q == 0 for q in quotes[1:]), (
        f"断供轮必须报 0，实际序列 {quotes} —— "
        f"报累计缓存值会让 no_data_rounds 结构上恒为 0")

    m = ls.finalize_metrics(rounds)
    assert int(m.get("no_data_rounds") or 0) == 5, m.get("no_data_rounds")
    v = ls.evaluate_health(m)
    assert v["healthy"] is False, (
        "静默断供在真实链路上必须转红 —— 否则就是完整假绿")
    assert "fetch" in v["fail"] or "data" in v["fail"], v["fail"]


def test_e2e_empty_round_publishes_zero_return_observation():
    """空轮必须真的把 ``returned=0`` 发到 Store（``observation_seq`` 要推进）。

    这一条专门钉住我在实现时踩的坑：第一版调用
    ``self.sources.capabilities()``（SourceManager **没有**这个方法），
    异常被 ``except`` 吞掉 -> **账本一份都没发**，而外表看起来完全正常。
    所以断言 ``observation_seq`` 必须**严格递增**。
    """
    from arad.config import load_settings
    from arad.engine import Engine
    from arad.session import TradingCalendar

    codes = [f"60{i:04d}" for i in range(50)]
    src = _SilentSource(codes)
    eng = Engine(source=src, settings=load_settings(use_cache=False),
                 calendar=TradingCalendar(), watchlist=[])
    eng._codes = list(codes)
    eng._codes_pinned = True
    store = eng.store

    eng.poll_once(force=True)                     # 第 1 轮：有数据
    seq1 = int(getattr(store, "observation_seq", 0) or 0)
    assert seq1 >= 1, "第 1 轮必须发布观测"
    assert int((store.observation or {}).get("returned") or 0) == 50

    eng.poll_once(force=True)                     # 第 2 轮：静默断供
    seq2 = int(getattr(store, "observation_seq", 0) or 0)
    obs = store.observation or {}
    assert seq2 > seq1, (
        "空轮必须**也**发布观测 —— seq 不推进说明发布被异常吞掉了")
    assert int(obs.get("returned") or 0) == 0, (
        f"空轮的 returned 必须是 0，实得 {obs.get('returned')}")
    assert int(obs.get("admitted") or 0) == 0
