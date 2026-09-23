"""WP03 —— `SourceManager.call_detailed` 合同校验（云端 2026-09-24 §WP03）。

三条独立合同，每条都要 rollback tooth（回退到 `try` 外 -> 必须重新红）：

1. **P1 类型合同**：``snapshots_detailed`` 返回裸 ``list``（错类型）必须
   按 **source failure** 处理并尝试后备源。
   修前会在 ``out._replace(...)`` 抛 ``AttributeError``，备源**不调用**。
2. **P2 route guard**：caller ``route="stocks"`` + ``result.route="index"``
   不得**静默分叉**（Manager 记 stocks 而 result 写 index）。
3. **source 对照**：结果自填错误 ``source`` 时，仍绑定到**实际被调用对象**的名字。

以及 **WP05**：WP03 生产代码不得读取 generic ``coverage``（读 `raw_return_coverage`
/ `usable_coverage`）。
"""

from __future__ import annotations

import re
import pathlib

import pytest

from arad.engine import SourceManager
from arad.sources.outcome import (
    PROVENANCE_EXACT,
    SnapshotFetchResult,
    build_outcome,
)


def _good(route="stocks", source="primary", n=1):
    return build_outcome(
        route=route, source=source,
        normalized_request=tuple(f"60000{i}" for i in range(n)),
        raw_keys=tuple(f"60000{i}" for i in range(n)),
        quotes=(), raw_rows=(), raw_presence_known=True,
        provenance=PROVENANCE_EXACT)


class _Bare:
    """**错类型**出口：返回裸 list（模拟第三方/手写源）。"""

    def __init__(self):
        self.name = "bare"
        self.calls = 0

    def snapshots_detailed(self, codes, *, route="stocks"):
        self.calls += 1
        return []                                   # <- 裸 list，不是 outcome

    def snapshots(self, codes):
        return []

    def health(self):
        return {"name": self.name, "ok": True, "latency_ms": 0, "err": ""}


class _Good:
    def __init__(self, name="good", route="stocks", report_source=None):
        self.name = name
        self._route = route
        self._report = report_source
        self.calls = 0

    def snapshots_detailed(self, codes, *, route="stocks"):
        self.calls += 1
        return _good(route=self._route,
                     source=self._report or self.name, n=len(codes) or 1)

    def snapshots(self, codes):
        return []

    def health(self):
        return {"name": self.name, "ok": True, "latency_ms": 0, "err": ""}


# ===========================================================================
# 1) P1 类型合同
# ===========================================================================

def test_wrong_type_detailed_fails_over_to_backup():
    """`IT-P1-CALL-DETAILED-TYPE-CONTRACT-001`。

    修前：``out._replace`` 抛 ``AttributeError``，备源 ``calls == 0``，
    整条链路炸掉 —— 一个**第三方源写错返回类型**就能让盘中预警全线停摆。
    """
    primary, backup = _Bare(), _Good("backup")
    mgr = SourceManager([primary, backup], threshold=3)

    out = mgr.call_detailed(["600000"], route="stocks")

    assert isinstance(out, SnapshotFetchResult), (
        f"必须返回合同对象，实测 {type(out).__name__}")
    assert backup.calls == 1, (
        f"错类型必须按 source failure 处理并尝试后备源，"
        f"实测 backup.calls={backup.calls}")
    assert out.source == "backup", f"来源应绑到实际供数源，实测 {out.source}"


def test_wrong_type_error_is_not_swallowed_silently():
    """错类型的**所有**源都必须失败时，抛异常而不是返回 None。"""
    mgr = SourceManager([_Bare(), _Bare2()], threshold=3)
    with pytest.raises(Exception):
        mgr.call_detailed(["600000"], route="stocks")


class _Bare2:
    def __init__(self):
        self.name = "bare2"

    def snapshots_detailed(self, codes, *, route="stocks"):
        return {"not": "an outcome"}               # 错类型（dict）

    def snapshots(self, codes):
        return []

    def health(self):
        return {"name": self.name, "ok": True, "latency_ms": 0, "err": ""}


def test_good_primary_does_not_trigger_backup():
    """**阳性对照**：主源正常时备源**不得**被调用（防止"永远 failover"的假绿）。"""
    primary, backup = _Good("primary"), _Good("backup")
    mgr = SourceManager([primary, backup], threshold=3)
    out = mgr.call_detailed(["600000"], route="stocks")
    assert out.source == "primary"
    assert backup.calls == 0, "主源健康时不得无谓调用备源"


# ===========================================================================
# 2) P2 route guard
# ===========================================================================

def test_caller_route_and_result_route_must_not_diverge():
    """`IT-P2-CALL-DETAILED-ROUTE-DIVERGENCE-001`（任务书 §WP03 新 P2）。

    构造：caller ``route="stocks"``，但源返回的 outcome ``route="index"``。

    修前：Manager 的 ``_serving``/``_fails`` 记在 **stocks**，
    返回的对象却写 **index** —— 两处对"这批数据属于哪条路由"给出不同答案。
    下游任何按 ``result.route`` 分账的代码都会把它算进 index，
    而失败计数在 stocks：**同一事实两个时点/两个轴分别推断**（本仓库主导 bug 类）。

    策略（本实现固定为**规范化**）：Manager 以 caller 的 route 为准，
    把 ``result.route`` 归一，保证"记账的路由 == 结果声明的路由"。
    """
    src = _Good("primary", route="index")        # 源自报 index
    mgr = SourceManager([src], threshold=3)

    out = mgr.call_detailed(["600000"], route="stocks")

    assert out.route == "stocks", (
        f"caller route 与 result.route 不得静默分叉："
        f"caller=stocks 而 result.route={out.route}")
    assert mgr.serving_of("stocks") == 0, "记账必须落在 caller route"
    assert mgr.serving_of("index") is None, (
        "不得在 index 路由留下记账痕迹（那正是分叉的证据）")


def test_route_normalization_preserves_other_fields():
    """规范化只改 ``route``，不得顺手抹掉 raw 证据（防止过度修复）。"""
    src = _Good("primary", route="index")
    mgr = SourceManager([src], threshold=3)
    out = mgr.call_detailed(["600000", "600001"], route="stocks")
    assert out.route == "stocks"
    assert out.requested_keys == frozenset({"600000", "600001"}), (
        "规范化不得破坏 requested_keys")
    assert out.provenance == PROVENANCE_EXACT, (
        "规范化不得降级 provenance")


# ===========================================================================
# 3) source 对照
# ===========================================================================

def test_result_source_binds_to_actually_called_object():
    """结果自填**错误** source 时，仍绑定到实际被调用对象的名字。"""
    src = _Good("real-name", report_source="someone-else")
    mgr = SourceManager([src], threshold=3)
    out = mgr.call_detailed(["600000"], route="stocks")
    assert out.source == "real-name", (
        f"必须绑到实际被调用对象，实测 {out.source}")


# ===========================================================================
# 4) WP05 —— coverage alias gate
# ===========================================================================

_CODE_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src"

#: WP05 的扫描范围 = **WP03 新增代码所在的模块**。
#:
#: ⚠ 我第一版把扫描放到整个 ``src/``，结果误报
#: ``capabilities.py:892 "coverage": round(self.coverage(), 4)`` ——
#: 那是**另一个类自己的** ``coverage()`` 方法（定义在同文件 :867），
#: 语义是"本轮能力覆盖率"，与 ``SnapshotFetchResult.coverage`` 这个
#: 废弃别名**毫无关系**。任务书 §WP05 的原文是「**WP03 新代码**读取
#: generic ``coverage`` → FAIL」，范围就是 WP03 的落点。
#:
#: 改成只扫 ``engine.py`` —— 但保留"扫描是可执行的、不是注释"这一点：
#: 一旦有人在 ``engine.py`` 里写 ``out.coverage``，这条立刻红。
_WP03_MODULES = ("engine.py",)


def test_wp03_production_code_never_reads_generic_coverage():
    """`IT-P2-COVERAGE-ALIAS-READ-001`（任务书 §WP05）。

    ``SnapshotFetchResult.coverage`` 已废弃（它是 ``usable_coverage`` 的别名）。
    WP03 新代码必须**显式**读 ``raw_return_coverage`` 或 ``usable_coverage``
    —— 否则又一次"同一语义两个名字"：读的人以为在问 raw 覆盖率，
    拿到的却是"能被解析成 Quote"的覆盖率。

    先证明这条 gate **有牙**（不是恒真）：对一个已知会读
    ``out.coverage`` 的样本必须报错。见 ``coverage_alias_gate.log``。
    """
    pat = re.compile(r"\.coverage\b")

    def offenders_in(text, label):
        out = []
        for ln, line in enumerate(text.splitlines(), 1):
            if not pat.search(line):
                continue
            if "raw_return_coverage" in line or "usable_coverage" in line:
                continue
            if line.lstrip().startswith("#") or "def coverage" in line:
                continue
            out.append(f"{label}:{ln}: {line.strip()}")
        return out

    offenders = []
    for mod in _WP03_MODULES:
        py = _CODE_ROOT / "arad" / mod
        offenders += offenders_in(py.read_text(encoding="utf-8"), mod)

    # ---- 自检：gate 必须能抓住已知违规样本（否则它是恒真式）----
    bait = "x = out.coverage\n" + "y = out.raw_return_coverage\n"
    assert offenders_in(bait, "BAIT"), (
        "gate 无牙：连 `out.coverage` 这种明显违规都抓不到")

    assert not offenders, (
        "WP03 生产代码不得读取 generic `.coverage`（废弃别名）：\n  "
        + "\n  ".join(offenders))

