"""WP01 生产接线：`poll_once` 必须消费 route-local 归属，禁止 global 差分反推。

任务书 §WP01「WP07 生产路径必须用 detailed」+ §WP03
「禁止从 global stats 反推 route reason」。

`tests/test_engine_route_time.py` 验的是 `EngineState.update_detailed` 这个
**单元**。本文件验的是**生产装配**：`poll_once` 真的调了 detailed，
并且 `RoundObservationSet` 里真的出现了 route 分账的键。

为什么必须单独有这一层：本仓库反复出现
「数据算了、但判决层零读者」（bug 类 c）—— 单元测试全绿而生产代码
根本没接线。上一轮 `call_detailed` 就是这样：4 个 gate 全绿，
但 `src/` 里生产调用点 = 0。
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import datetime

import pytest

from arad.capabilities import RoundObservationSet
from arad.engine import ROUTE_INDEX, ROUTE_STOCKS, Engine

NOW = datetime(2026, 9, 24, 10, 30, 0)


def _poll_once_ast() -> ast.AST:
    """`poll_once` 的 **AST**（注释/docstring 不参与判定）。

    我第一版这里用 `inspect.getsource` + 子串匹配，结果**自己的注释**
    （"现在每个 route 的 update_detailed() 自己给出…"）里含该词，
    于是回退 RB-E 把真实调用全删掉后测试**照样绿** —— 牙被自己的散文缴了械。
    这正是本仓库反复出现的退化：
    **gate 变成"文本里出现过这个词"，而不是"真的调了"**。

    改成走 AST：只看真实节点。
    """
    src = textwrap.dedent(inspect.getsource(Engine.poll_once))
    return ast.parse(src)


def _called_attr_names(tree: ast.AST) -> set[str]:
    """源码里真实出现的被调用属性名（`self.x(...)` -> `"x"`）。"""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            out.add(node.func.attr)
    return out


def _string_constants(tree: ast.AST) -> set[str]:
    """AST 里**所有**字符串常量（含下标与调用实参）。

    用于查 `stats["t_reject:future"]` 与 `stats.get("t_reject:future", 0)`
    两种写法。
    """
    return {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


# ===========================================================================
# 结构：生产代码真的调了 detailed
# ===========================================================================

def test_poll_once_calls_update_detailed():
    """`poll_once` 必须**真的调用** `update_detailed`（AST 层，不看注释）。"""
    called = _called_attr_names(_poll_once_ast())
    assert "update_detailed" in called, (
        f"poll_once 必须真实调用 update_detailed，实测调用的相关方法只有 "
        f"{sorted(n for n in called if 'update' in n)}。"
        f"（注释里提到它**不算** —— 旧版测试就是被那句注释骗过的）")


def test_poll_once_no_longer_diffs_global_counters():
    """**回归牙**：`poll_once` 不得再出现 `t_reject:future/out_of_order`。

    这就是被修掉的机制 —— aggregate 差值丢 route 归属。
    """
    seen = _string_constants(_poll_once_ast())
    for banned in ("t_reject:future", "t_reject:out_of_order"):
        assert banned not in seen, (
            f"不得再读 {banned}（global 差分丢 route 归属）")


def test_route_ledger_helper_is_the_single_source():
    """正常路径与空轮分支必须**共用** `_route_ledger`（单一事实来源）。"""
    tree = _poll_once_ast()
    n = sum(1 for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_route_ledger")
    assert n >= 2, (
        f"正常路径与空轮分支都该调 _route_ledger，AST 里实测只出现 {n} 次 —— "
        f"少的那条就是手写的第二套账（假绿温床）")


def test_poll_once_docstring_has_no_update_detailed():
    """**自检锁**：`poll_once` 的 docstring 不得含 `update_detailed`。

    给上面 AST 测试上的锁：只要 docstring 里没这个词，
    "文本匹配会误绿"就不可能再发生。若有人把它写回注释里，这条会红 ——
    提醒他改的是注释、不是代码。
    """
    ds = ast.get_docstring(_poll_once_ast()) or ""
    assert "update_detailed" not in ds, (
        "poll_once 的 docstring 里出现了 update_detailed —— "
        "会重新让文本匹配型 gate 变得可被注释欺骗")


# ===========================================================================
# 行为：两条分支的账本形状一致
# ===========================================================================

def _ledger_shape(obs: RoundObservationSet):
    """账本里 route 分账键的形状（键集合），不含值。"""
    return (tuple(sorted(obs.reject_by_route)),
            tuple(sorted(obs.admitted_by_route)),
            tuple(sorted(obs.returned_by_route)))


def test_route_ledger_keys_are_stable_in_normal_round():
    """正常轮：两条 route 的分账键必须齐全。"""
    obs = RoundObservationSet(
        **Engine._route_ledger(
            stk_returned=3, stk_admitted=2, idx_returned=5, idx_admitted=5,
            stk_reject=(1, 0), idx_reject=(0, 2)))
    assert _ledger_shape(obs) == ((ROUTE_INDEX, ROUTE_STOCKS),
                                  (ROUTE_INDEX, ROUTE_STOCKS),
                                  (ROUTE_INDEX, ROUTE_STOCKS))
    assert obs.reject_by_route[ROUTE_STOCKS] == (1, 0)
    assert obs.reject_by_route[ROUTE_INDEX] == (0, 2)
    assert obs.admitted_by_route[ROUTE_INDEX] == 5
    assert obs.returned_by_route[ROUTE_STOCKS] == 3


def test_route_ledger_keys_exist_even_in_empty_round():
    """空轮：形状**仍**齐全（值为 0），不能"读不到键"。

    "读不到 index 键"与"index 键为 0"若混成同一种表现，
    下游就无法区分"这条分支没接好"和"这条 route 本轮真的是 0"。
    """
    obs = RoundObservationSet(
        **Engine._route_ledger(
            stk_returned=0, stk_admitted=0, idx_returned=0, idx_admitted=0,
            stk_reject=(0, 0), idx_reject=(0, 0)))
    assert _ledger_shape(obs) == ((ROUTE_INDEX, ROUTE_STOCKS),
                                  (ROUTE_INDEX, ROUTE_STOCKS),
                                  (ROUTE_INDEX, ROUTE_STOCKS))
    assert all(v == (0, 0) for v in obs.reject_by_route.values())
    assert all(v == 0 for v in obs.admitted_by_route.values())


# ===========================================================================
# 行为：合计值分不出的两种故障，分账后必须分得出
# ===========================================================================

def test_two_route_faults_with_identical_totals_are_distinguishable():
    """**核心**：同一组合计值下，两种完全不同的故障必须可区分。

    任务书原文：
    ```text
    Case A: index future=1, stock ooo=1
    Case B: index ooo=1, stock future=1
    aggregate 都 future=1/ooo=1，但 per-route result 必须不同。
    ```
    这是端到端的：从 `_route_ledger` 装配出的账本，读出来必须不同。
    """
    case_a = RoundObservationSet(
        **Engine._route_ledger(
            stk_returned=1, stk_admitted=0, idx_returned=1, idx_admitted=0,
            stk_reject=(0, 1), idx_reject=(1, 0)))
    case_b = RoundObservationSet(
        **Engine._route_ledger(
            stk_returned=1, stk_admitted=0, idx_returned=1, idx_admitted=0,
            stk_reject=(1, 0), idx_reject=(0, 1)))

    # 合计完全相同 —— 只看这个（旧行为的全部可见信息）无法区分
    assert case_a.future_rejected == case_b.future_rejected
    assert case_a.out_of_order_rejected == case_b.out_of_order_rejected

    # 分账后必须不同
    assert (case_a.reject_by_route[ROUTE_INDEX]
            != case_b.reject_by_route[ROUTE_INDEX]), (
        "index 侧的故障类型不同，分账必须体现")
    assert (case_a.reject_by_route[ROUTE_STOCKS]
            != case_b.reject_by_route[ROUTE_STOCKS])
    a_dict = case_a.as_dict()
    b_dict = case_b.as_dict()
    assert a_dict["reject_by_route"] != b_dict["reject_by_route"], (
        "as_dict 必须导出可区分的 route 归属")


def test_as_dict_exports_route_attribution():
    """`as_dict()`（消费方读的那一层）必须真的带出 route 归属。"""
    obs = RoundObservationSet(
        **Engine._route_ledger(
            stk_returned=2, stk_admitted=2, idx_returned=1, idx_admitted=0,
            stk_reject=(0, 0), idx_reject=(2, 0)))
    d = obs.as_dict()
    assert d["reject_by_route"][ROUTE_INDEX] == {"future": 2,
                                                "out_of_order": 0}
    assert d["reject_by_route"][ROUTE_STOCKS] == {"future": 0,
                                                  "out_of_order": 0}
    assert d["admitted_by_route"][ROUTE_STOCKS] == 2
    assert d["returned_by_route"][ROUTE_INDEX] == 1
    assert "reject_by_route" in d["_schema_note"], (
        "新字段必须进 schema note，否则消费方不知道该读它")
