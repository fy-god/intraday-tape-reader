"""WP02 —— `SourceManager.call_detailed`：outcome 与实际服务源**原子绑定**。

云端 2026-09-23_20-09-44_JST.md §6.1 / 任务书 WP02。

必须证明四件事：
1. 主源 detailed 抛异常 -> 备用源给出 exact outcome，且 **source 是备用源**；
2. **同一 raw fact 在 failover 前后 terminal 语义不变**（这是本轮的主命题）；
3. legacy 源 -> `raw_presence_known=False` + `PROVENANCE_LEGACY`，
   **不得**伪装成 exact；
4. `result.source` 与 `Manager.serving_of(route)` 一致（原子性），
   且**不存在**"先 call 再问 serving_of"那个窗口。
"""

from __future__ import annotations

import pytest

from arad.engine import SourceManager
from arad.sources.outcome import (
    PROVENANCE_EXACT,
    PROVENANCE_LEGACY,
    build_outcome,
)

OK = "600002"
BAD = "600001"


class _Detailed:
    """内置源形态：有 `snapshots_detailed`。"""

    def __init__(self, name, *, raw_keys=(), quotes=(), boom=False):
        self.name = name
        self._raw = list(raw_keys)
        self._quotes = list(quotes)
        self._boom = boom
        self.calls = 0

    def snapshots_detailed(self, codes, *, route="stocks"):
        self.calls += 1
        if self._boom:
            raise RuntimeError(f"{self.name} down")
        return build_outcome(
            route=route, source=self.name,
            normalized_request=tuple(str(c) for c in codes),
            raw_keys=[k for k in self._raw if k in set(map(str, codes))],
            quotes=[q for q in self._quotes if q.code in set(map(str, codes))])

    def health(self):
        return {"name": self.name, "ok": not self._boom, "latency_ms": 0}


class _Legacy:
    """legacy 源形态：**只有** `snapshots`（返回 list[Quote]）。"""

    def __init__(self, name, *, quotes=(), boom=False):
        self.name = name
        self._quotes = list(quotes)
        self._boom = boom
        self.calls = 0

    def snapshots(self, codes):
        self.calls += 1
        if self._boom:
            raise RuntimeError(f"{self.name} down")
        want = set(map(str, codes))
        return [q for q in self._quotes if q.code in want]

    def health(self):
        return {"name": self.name, "ok": not self._boom, "latency_ms": 0}


class _Q:
    """最小 Quote 替身（只需要 `code`）。"""

    def __init__(self, code):
        self.code = code


# ---------------------------------------------------------------------------
# 1) 主源异常 -> 备用源 exact，source 必须是备用源
# ---------------------------------------------------------------------------

def test_call_detailed_failover_binds_actual_source():
    """主源 detailed 抛异常 -> 备用源出数，`result.source` = **备用源**。

    这是 §6.1 的核心：不能先 `call()` 拿到结果、再另取
    `serving_of(route)` 去猜来源 —— 两次读取之间有窗口。
    """
    primary = _Detailed("primary", boom=True)
    backup = _Detailed("backup", raw_keys=[BAD, OK],
                       quotes=[_Q(OK)])
    m = SourceManager([primary, backup], threshold=1)
    r = m.call_detailed([BAD, OK], route="stocks")
    assert r.source == "backup", "必须绑定**实际服务源**"
    assert r.provenance == PROVENANCE_EXACT
    assert r.raw_presence_known is True
    # 原子性：返回值里的 source 与 Manager 的记账必须一致
    assert m.serving_of("stocks") == 1
    assert m.sources[m.serving_of("stocks")].name == r.source
    # 主源**试过**且失败，不是被跳过
    assert primary.calls == 1 and backup.calls == 1


def test_call_detailed_primary_success_binds_primary():
    """**阳性对照**：主源正常时不得误记成备用源。"""
    primary = _Detailed("primary", raw_keys=[OK], quotes=[_Q(OK)])
    backup = _Detailed("backup", raw_keys=[OK], quotes=[_Q(OK)])
    m = SourceManager([primary, backup], threshold=1)
    r = m.call_detailed([OK], route="stocks")
    assert r.source == "primary"
    assert m.serving_of("stocks") == 0
    assert backup.calls == 0, "主源成功就不该打备用源"


# ---------------------------------------------------------------------------
# 2) 主命题：同一 raw fact，failover 前后 terminal 语义不变
# ---------------------------------------------------------------------------

def test_same_raw_fact_has_same_terminals_across_failover():
    """**本轮主命题**：同一个 raw 事实在 failover 前后落**同一个桶**。

    构造：raw 回来了 2 条（一条可用、一条不可用），第 3 条真缺席。
    A 臂 = 主源直接服务；B 臂 = 主源挂掉、备用源服务**同样的 raw fact**。
    两臂的 admitted / rejected_quality / unknown_missing **必须逐元素相同**，
    否则"一次 failover 就改变账本语义"的缺陷仍然存在
    （这正是 WP01 v4 存在的理由）。
    """
    absent = "600003"
    req = [BAD, OK, absent]

    def terminals(r):
        return (set(r.admitted_keys), set(r.rejected_quality_keys),
                set(r.unknown_missing_keys))

    # A 臂：主源直接就服务
    a_src = _Detailed("A", raw_keys=[BAD, OK], quotes=[_Q(OK)])
    a = SourceManager([a_src], threshold=1).call_detailed(req, route="stocks")

    # B 臂：主源挂 -> 备用源服务**同一个 raw fact**
    dead = _Detailed("dead", boom=True)
    b_src = _Detailed("B", raw_keys=[BAD, OK], quotes=[_Q(OK)])
    b = SourceManager([dead, b_src], threshold=1).call_detailed(
        req, route="stocks")

    assert a.source == "A" and b.source == "B", "两臂确实来自不同源"
    assert terminals(a) == terminals(b), (
        f"failover 改变了账本语义：\n  A={terminals(a)}\n  B={terminals(b)}")
    # 具体桶也要对（防止两臂一起错）
    assert set(b.admitted_keys) == {OK}
    assert set(b.rejected_quality_keys) == {BAD}
    assert set(b.unknown_missing_keys) == {absent}
    assert a.raw_return_coverage == b.raw_return_coverage == pytest.approx(2 / 3)
    assert a.usable_coverage == b.usable_coverage == pytest.approx(1 / 3)


# ---------------------------------------------------------------------------
# 3) legacy 源不得伪装 exact
# ---------------------------------------------------------------------------

def test_call_detailed_legacy_source_is_not_dressed_as_exact():
    """legacy 源（只有 `snapshots`）必须标 `raw_presence_known=False`。

    它没有 raw 证据，只能从 Quote 投影反推 —— 若伪装成 exact，
    下游会把"没在投影里出现"读成"provider 真没返回"（云端 FAIL 条件之一）。
    """
    legacy = _Legacy("legacy", quotes=[_Q(OK)])
    m = SourceManager([legacy], threshold=1)
    r = m.call_detailed([BAD, OK], route="stocks")
    assert r.source == "legacy"
    assert r.raw_presence_known is False
    assert r.provenance == PROVENANCE_LEGACY
    # 没有 raw 证据 -> raw 覆盖率必须是**未测量**，不是 100%
    assert r.raw_return_coverage is None, (
        "legacy 不得声称 raw 覆盖率（那会把'没证据'伪装成'全回来了'）")
    assert r.usable_coverage == pytest.approx(1 / 2)


def test_call_detailed_legacy_behind_detailed_primary_stays_legacy():
    """主源 detailed 挂 -> 备用是 legacy：整体必须是 legacy 语义。

    这是**混合链路的单调性**：加进一个没有 raw 证据的源之后，
    整体证据只能变弱，不能因为"前面主源是 exact 实现"就整体声称 exact。
    """
    dead = _Detailed("dead", boom=True)
    legacy = _Legacy("legacy", quotes=[_Q(OK)])
    m = SourceManager([dead, legacy], threshold=1)
    r = m.call_detailed([BAD, OK], route="stocks")
    assert r.source == "legacy"
    assert r.raw_presence_known is False
    assert r.provenance == PROVENANCE_LEGACY
    assert r.raw_return_coverage is None


# ---------------------------------------------------------------------------
# 4) 记账必须按路由分开（IT-P1-007 不许被新方法破坏）
# ---------------------------------------------------------------------------

def test_call_detailed_keeps_per_route_accounting():
    """新方法**必须**沿用 `call` 的按路由分账，否则 IT-P1-007 回归。

    IT-P1-007：指数请求几乎不失败，若与个股共用一个计数器，指数每轮成功
    都会把个股的失败计数清零 -> 正式切换永远不发生。

    ⚠ 阈值 5 是**故意的**：用 threshold=3 时第 3 次就正式切换了，
    而 `_switch`/切换分支会 `self._fails.clear()` —— 计数器归 0 是**正确的**，
    我第一版按 threshold=3 断言 `== 3` 于是误判"分账坏了"。
    这里要测的是**累计过程**，故把阈值调高让计数停在中间态。
    """
    primary = _Detailed("primary", boom=True)
    backup = _Detailed("backup", raw_keys=[OK], quotes=[_Q(OK)])
    m = SourceManager([primary, backup], threshold=5)
    for _ in range(3):
        m.call_detailed([OK], route="stocks")
    assert m.fails_by_route.get("stocks") == 3, "个股失败计数必须累计"
    assert m.fails_by_route.get("index", 0) == 0, "指数路由不得被牵连"
    assert m.serving_of("stocks") == 1, "实际服务源已是备用"
    assert m.serving_of("index") is None, "指数路由没请求过，不得有记账"

    # 关键对照：**指数路由成功一次，不得清零个股的失败计数**（IT-P1-007 本体）
    m.call_detailed([OK], route="index")
    assert m.fails_by_route.get("stocks") == 3, (
        "指数成功把个股失败计数清零了 —— IT-P1-007 回归")
    assert m.serving_of("index") == 1


def test_call_detailed_reaches_threshold_and_switches():
    """累计到阈值必须**正式切换**（阈值语义与 `call` 一致）。"""
    primary = _Detailed("primary", boom=True)
    backup = _Detailed("backup", raw_keys=[OK], quotes=[_Q(OK)])
    m = SourceManager([primary, backup], threshold=2)
    m.call_detailed([OK], route="stocks")
    assert m.idx == 0, "1 次还不够"
    m.call_detailed([OK], route="stocks")
    assert m.idx == 1, "到阈值必须切换主源"
    assert m.current.name == "backup"


def test_call_detailed_binds_source_even_if_source_misreports():
    """源**自己填错** `source` 时，Manager 必须以**实际调用对象**为准。

    这是 §6.1 "原子绑定"里真正有牙的那一半：多数源会老实填自己的 `name`，
    但一旦某个源填成别的名字（复制粘贴、包装层、复用别源 outcome），
    下游就会把账记到**没提供数据的源**头上 —— 这正是
    "用两个时点的观测拼一个事实" 的另一种形态。
    """
    class _Liar(_Detailed):
        def snapshots_detailed(self, codes, *, route="stocks"):
            out = super().snapshots_detailed(codes, route=route)
            return out._replace(source="totally-wrong-name")

    liar = _Liar("real-name", raw_keys=[OK], quotes=[_Q(OK)])
    m = SourceManager([liar], threshold=1)
    r = m.call_detailed([OK], route="stocks")
    assert r.source == "real-name", (
        f"必须以实际服务源为准，源自填的 {r.source!r} 不可信")


def test_call_detailed_all_sources_failed_raises():
    """全部失败 -> 抛最后一个异常（与 `call` 同语义），不得静默返回 None。"""
    a = _Detailed("a", boom=True)
    b = _Detailed("b", boom=True)
    m = SourceManager([a, b], threshold=1)
    with pytest.raises(RuntimeError):
        m.call_detailed([OK], route="stocks")


def test_call_detailed_empty_request_is_not_an_error():
    """空请求：不抛异常，且来源仍被正确绑定（边界）。"""
    src = _Detailed("s", raw_keys=[], quotes=[])
    m = SourceManager([src], threshold=1)
    r = m.call_detailed([], route="stocks")
    assert r.source == "s"
    assert r.requested == 0
    assert r.raw_return_coverage is None
