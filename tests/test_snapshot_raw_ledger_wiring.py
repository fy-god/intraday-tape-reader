"""WP07：生产路径必须真正消费 detailed 出口。

`IT-P1-SNAPSHOT-RAW-LEDGER-COLLAPSE-001` + bug 类 (c)
「数据算了、判决层零读者」。

修前的实测事实（云端 20:08 §2 与我本地复核一致）：
``call_detailed`` 的生产调用点 = **0**（只有 ``engine.py:709`` 的定义）。
engine 的 `poll_once` / `_fetch_indices` 都还调 legacy ``call("snapshots")``。
于是 detailed 的全部精确 raw presence 在**生产上没有任何读者** ——
它只存在于测试里，"修好了"是测试里的修好。

⚠ 本文件的门禁**必须用 AST**，不能 `in` 子串匹配源码：
我上一轮就因为自己的**注释里**含 `update_detailed` 这个字串，
让一条本该转红的门禁保持绿色（自造的 tautology）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1] / "src" / "arad" / "engine.py"


def _engine_tree() -> ast.Module:
    return ast.parse(ENGINE.read_text(encoding="utf-8"))


def _call_names(node: ast.AST) -> list[str]:
    """收集一段 AST 里所有 ``X.Y(...)`` 形式的**被调属性名**。"""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            out.append(n.func.attr)
    return out


def _string_constants(node: ast.AST) -> list[str]:
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _method(name: str) -> ast.FunctionDef:
    for node in ast.walk(_engine_tree()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError(f"engine.py 里找不到方法 {name}")


# ===========================================================================
# 门禁：生产调用点必须存在（AST，非子串）
# ===========================================================================

def test_poll_once_calls_detailed_fetch_helpers():
    """`poll_once` 必须调 detailed 抓取助手（不是 legacy call("snapshots")）。"""
    fn = _method("poll_once")
    names = _call_names(fn)
    assert "_fetch_stocks_detailed" in names, (
        "poll_once 必须走 _fetch_stocks_detailed（AST 实测被调属性："
        f"{sorted(set(names))}）")


def test_poll_once_does_not_call_legacy_snapshots_directly():
    """`poll_once` 不得再直接调 legacy ``sources.call("snapshots")``。"""
    fn = _method("poll_once")
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "call":
            args = _string_constants(n)
            assert "snapshots" not in args, (
                "poll_once 不得直接调 legacy snapshots —— "
                "那会绕开精确 raw presence")


def test_fetch_indices_detailed_calls_call_detailed():
    """`_fetch_indices_detailed` 必须调 ``call_detailed``。"""
    fn = _method("_fetch_indices_detailed")
    assert "call_detailed" in _call_names(fn), (
        "_fetch_indices_detailed 必须调 call_detailed")
    assert "snapshots" not in _string_constants(fn), (
        "_fetch_indices_detailed 不得退回 legacy snapshots")


def test_fetch_stocks_detailed_calls_call_detailed():
    fn = _method("_fetch_stocks_detailed")
    assert "call_detailed" in _call_names(fn), (
        "_fetch_stocks_detailed 必须调 call_detailed")


def test_production_call_detailed_sites_are_nonzero():
    """**本条缺陷的直接否定**：生产 `call_detailed` 调用点 > 0。

    修前实测 = 0（只有定义，没有生产读者）。
    """
    tree = _engine_tree()
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for c in ast.walk(node):
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) \
                    and c.func.attr == "call_detailed":
                sites.append((node.name, c.lineno))
    assert sites, (
        "engine.py 里必须存在调用 call_detailed 的**方法**；"
        "实测 0 个 —— detailed 出口没有生产读者（bug 类 c）")
    names = {n for n, _ in sites}
    assert {"_fetch_stocks_detailed", "_fetch_indices_detailed"} <= names, (
        f"个股与指数**两条** route 都必须走 detailed，实测 {sorted(names)}")


def test_call_detailed_sites_are_in_production_reachable_methods():
    """调用点必须在**生产可达**的方法里（不是死代码/私有探针）。"""
    tree = _engine_tree()
    # `_fetch_stocks_detailed` / `_fetch_indices_detailed` 必须被 poll_once 家族调到
    poll = ast.unparse(_method("poll_once"))
    assert "_fetch_stocks_detailed" in poll
    assert "_fetch_indices_detailed" in poll, (
        "poll_once 必须同时驱动两条 route 的 detailed 抓取")


def test_ledger_records_raw_presence_evidence_level():
    """账本必须如实登记证据等级（`raw_presence_known_by_route`）。"""
    tree = _engine_tree()
    found = False
    for n in ast.walk(tree):
        if isinstance(n, ast.keyword) and \
                n.arg == "raw_presence_known_by_route":
            found = True
            break
    assert found, (
        "必须往 RoundObservationSet 传 raw_presence_known_by_route —— "
        "否则下游无法区分精确证据与投影反推")


def test_raw_keys_of_helper_exists_and_gates_on_known():
    """`_raw_keys_of` 必须在 `raw_presence_known=False` 时返回 None（降级）。"""
    fn = _method("_raw_keys_of")
    src = ast.unparse(fn)
    assert "raw_presence_known" in src, (
        "`_raw_keys_of` 必须检查 raw_presence_known，否则 legacy 源"
        "会被伪装成精确证据（假绿出口）")
    assert "None" in src, "必须能返回 None 表达'拿不到精确证据'"


# ===========================================================================
# 行为：证据等级在真实 poll 上如实反映
# ===========================================================================

def test_raw_keys_of_none_for_missing_outcome():
    from arad.engine import Engine

    assert Engine._raw_keys_of(None) is None


def test_raw_keys_of_none_when_presence_unknown():
    """`raw_presence_known=False` -> None（**不得**当成"没有行返回"）。"""
    from arad.engine import Engine

    class _Legacy:
        raw_presence_known = False
        raw_returned_requested_keys = frozenset({"600000"})

    assert Engine._raw_keys_of(_Legacy()) is None, (
        "legacy 出口必须降级成 None，不能伪装成精确")


def test_raw_keys_of_returns_keys_when_exact():
    from arad.engine import Engine

    class _Exact:
        raw_presence_known = True
        raw_returned_requested_keys = frozenset({"600000", "000001"})

    assert Engine._raw_keys_of(_Exact()) == {"600000", "000001"}


def test_raw_keys_of_fails_closed_without_attribute():
    """没有 `raw_returned_requested_keys` 属性时也必须降级（fail-closed）。"""
    from arad.engine import Engine

    class _NoAttr:
        raw_presence_known = True

    assert Engine._raw_keys_of(_NoAttr()) is None
