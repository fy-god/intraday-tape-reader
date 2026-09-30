"""`IT-P1-RAW-PRESENCE-ROUTE-SCOPE-FALSE-GRADE-044`
`IT-P2-SOAK-RAW-PRESENCE-ROUND-DENOMINATOR-043`

云端 `2026-09-30_04-13-31_JST.md` 审计了**我上一轮的中间产物**（`55dcce8`），
指出一个 bool 同时承担**四种**本应分开的状态：

```text
True  = 有 outcome 且 raw evidence exact
False = 有 outcome 但 legacy/projected
?     = route 根本没请求                    <- 曾压成 False
?     = route 请求了但 fetch 失败/无 outcome  <- 曾压成 False
```

两个方向的错误都真实存在：

* **false downgrade**（§2.1/§2.2）：没 dispatch 或 fetch 失败的 route
  被写成 `False` = "投影"，于是**根本没测**的 route 反过来给 soak
  制造黄色证据等级；
* **false upgrade**（§2.3，云端认定最危险）：空轮分支**硬编码
  `{stocks: True, index: True}`**，**完全不看** outcome ——
  指数请求**失败**时被粉饰成 `exact`。

还有 §3 的**放大器**：route-ledger 为 schema 稳定每轮保留零值键，
消费端只 union **keys**，于是 `index: 0` 被当成"这条 route 出过数"，
伪造 route 活动并触发**假 partial**。

以及 §4（043）：`_safe_int(dict)` 恒得 0，导致文案
"投影 1/0 轮"、"精确 0 轮"这种**荒谬分母**。

## 冻结合同（§5.1）

```text
outcome + raw_presence_known=True  -> True   (exact)
outcome + raw_presence_known=False -> False  (projected/legacy)
route 未请求                        -> **省略键**
route 请求了但 outcome=None         -> **省略键**（下游 not_measured）
```

正常路径与空轮分支**必须共用同一个 helper**，禁止再手写两套。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arad.engine import ROUTE_INDEX, ROUTE_STOCKS, Engine  # noqa: E402


class _Outcome:
    """最小 outcome 桩：只带 helper 真正读的两个属性。"""

    def __init__(self, known: bool, keys=()):
        self.raw_presence_known = known
        self.raw_returned_requested_keys = list(keys)


HELPER = Engine._route_raw_presence


def call(*, stk_req, idx_req, stk_out, idx_out):
    return HELPER(stocks_requested=stk_req, index_requested=idx_req,
                  stk_outcome=stk_out, idx_outcome=idx_out)


# ===========================================================================
# 1) 合同四态：不能再压成一个 bool
# ===========================================================================

class TestContractFourStates:
    def test_both_requested_with_outcomes_both_keys_present(self):
        got = call(stk_req=True, idx_req=True,
                   stk_out=_Outcome(True), idx_out=_Outcome(True))
        assert got == {ROUTE_STOCKS: True, ROUTE_INDEX: True}

    def test_legacy_outcome_is_false_not_missing(self):
        """**有** outcome 但 legacy -> `False`（projected），键必须在。"""
        got = call(stk_req=True, idx_req=True,
                   stk_out=_Outcome(False), idx_out=_Outcome(False))
        assert got == {ROUTE_STOCKS: False, ROUTE_INDEX: False}, (
            "有 outcome 的 legacy route 必须记 False（不是省略键）")

    def test_unrequested_route_key_omitted(self):
        """**反例 1**：未 dispatch 的 route 必须**省略键**，不得记 False。"""
        got = call(stk_req=True, idx_req=False,
                   stk_out=_Outcome(True), idx_out=None)
        assert got == {ROUTE_STOCKS: True}, f"实测 {got}"
        assert ROUTE_INDEX not in got, (
            "未请求的 route 不得出现 —— 否则是 false downgrade")

    def test_requested_but_none_outcome_key_omitted(self):
        """**反例 2**：请求了但 fetch 失败（outcome=None）-> 省略键。

        必须是 not_measured，**不能**冒充 projected(False)。
        """
        got = call(stk_req=True, idx_req=True,
                   stk_out=_Outcome(True), idx_out=None)
        assert got == {ROUTE_STOCKS: True}, f"实测 {got}"
        assert ROUTE_INDEX not in got, (
            "请求了但没 outcome 的 route 不得写成 False（那是假装知道）")

    def test_empty_round_with_failed_index_is_not_exact(self):
        """**反例 3（最危险）**：空轮 + 指数失败，**不得**写成 exact。

        旧代码空轮硬编码 `{stocks: True, index: True}` —— false upgrade。
        """
        got = call(stk_req=True, idx_req=True,
                   stk_out=None, idx_out=None)
        assert got == {}, f"两者都无 outcome 时必须全空，实测 {got}"
        assert ROUTE_INDEX not in got, (
            "指数请求失败不得被粉饰成 exact=True（false upgrade）")

    def test_nothing_requested_yields_empty_map(self):
        got = call(stk_req=False, idx_req=False,
                   stk_out=None, idx_out=None)
        assert got == {}

    def test_requested_route_with_known_false_is_false(self):
        """回归：有 outcome 且 raw_presence_known=False 仍须 False。"""
        got = call(stk_req=True, idx_req=False,
                   stk_out=_Outcome(False), idx_out=None)
        assert got == {ROUTE_STOCKS: False}

    def test_partial_request_only_stock(self):
        got = call(stk_req=True, idx_req=False,
                   stk_out=_Outcome(True), idx_out=_Outcome(True))
        assert got == {ROUTE_STOCKS: True}, (
            "index 没请求 -> 即使给了 outcome 也不得出现")


# ===========================================================================
# 2) 空轮分支不得再硬编码（结构门禁，AST）
# ===========================================================================

class TestNoHardcodedEmptyRound:
    def test_engine_has_no_hardcoded_true_true_map(self):
        """engine.py 里**不得**再有 `{STOCKS: True, INDEX: True}` 硬编码。

        用 AST 找 dict 字面量，**不用子串匹配**（注释会满足子串）。
        """
        import ast

        src = (ROOT / "src" / "arad" / "engine.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        hits = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            vals = [v for v in node.values
                    if isinstance(v, ast.Constant) and v.value is True]
            if len(vals) >= 2 and len(node.keys) == len(vals) == 2:
                hits.append(node.lineno)
        assert not hits, (
            f"engine.py 行 {hits} 仍有 {{x: True, y: True}} 硬编码 —— "
            f"空轮分支必须走 `_route_raw_presence`，不得无视 outcome")

    def test_both_branches_call_the_shared_helper(self):
        """正常路径与空轮分支**都必须**调用 `_route_raw_presence`。

        这正是云端 §5.1 的"禁止再手写两套"（bug 类 b）。
        """
        import ast

        src = (ROOT / "src" / "arad" / "engine.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "_route_raw_presence"]
        assert len(calls) >= 2, (
            f"应有 ≥2 处调用（正常 + 空轮），实测 {len(calls)}")

    def test_helper_declared_once(self):
        import ast

        src = (ROOT / "src" / "arad" / "engine.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        defs = [n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef)
                and n.name == "_route_raw_presence"]
        assert len(defs) == 1, f"helper 必须只声明一次，实测 {len(defs)}"


# ===========================================================================
# 3) 放大器：键存在 != 有活动
# ===========================================================================

class TestPositiveActivity:
    def _f(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "ls_pa", ROOT / "tools" / "live_session.py")
        m = importlib.util.module_from_spec(spec)
        sys.modules["ls_pa"] = m
        spec.loader.exec_module(m)
        return m._positive_activity

    def test_zero_is_no_activity(self):
        """**放大器本体**：零值键不算"出过数"。"""
        f = self._f()
        assert f(0) is False, "0 = 没出数"
        assert f(0.0) is False

    def test_positive_is_activity(self):
        f = self._f()
        assert f(1) is True
        assert f(5) is True

    def test_reject_tuple_shape(self):
        """reject_by_route 的 `(future, ooo)` 元组形状。"""
        f = self._f()
        assert f((0, 0)) is False, "两个都是 0 -> 没拒绝 = 没活动"
        assert f((0, 1)) is True
        assert f((1, 0)) is True

    def test_reject_dict_shape(self):
        """reject_by_route 的 dict 形状（新版）。"""
        f = self._f()
        assert f({"future": 0, "out_of_order": 0}) is False
        assert f({"future": 0, "out_of_order": 2}) is True

    def test_dirty_values_fail_closed(self):
        """脏值一律算**无活动**（fail-closed：不伪造 route 活动）。"""
        f = self._f()
        assert f(None) is False
        assert f("garbage") is False
        assert f(True) is False, "bool 不是计数"

    def test_zero_key_does_not_enter_seen_here(self):
        """端到端：route-ledger 零值键不得让 partial 误报。

        构造一轮：index 有键但全 0；grade map 只有 stocks。
        旧行为 -> `_seen_here` 含 index -> `stocks` 有键而 index 被算"出过数"
        但无等级 -> **假 partial**。新行为 -> index 不进 seen -> 无 partial。
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "ls_amp", ROOT / "tools" / "live_session.py")
        ls = importlib.util.module_from_spec(spec)
        sys.modules["ls_amp"] = ls
        spec.loader.exec_module(ls)

        obs = {
            "source": "sina",
            "capabilities": {"source": "sina"},
            "requested": 100, "returned": 100, "admitted": 100,
            "coverage": 1.0,
            "unknown_missing": [], "rejected_quality": [],
            "future_rejected": 0, "out_of_order_rejected": 0,
            "unavailable_capability": 0,
            # index 从未 dispatch -> 等级只有 stocks
            "raw_presence_known_by_route": {"stocks": True},
            # 但 route-ledger 为 schema 稳定保留了零值 index 键
            "returned_by_route": {"stocks": 100, "index": 0},
            "admitted_by_route": {"stocks": 100, "index": 0},
            "reject_by_route": {"stocks": {"future": 0, "out_of_order": 0},
                                "index": {"future": 0, "out_of_order": 0}},
        }
        row = ls.make_round_sample(
            index=1, latency_ms=400.0, alerts=(), error=False,
            quotes=5000, universe=5000, history_points=10, history_codes=10,
            max_deque=1, history_maxlen=360, watchlist_only=False,
            observation=obs)
        m = ls.summarize_rounds([row])
        g = m.get("raw_presence_grade") or {}
        assert g.get("rounds_partial") == 0, (
            f"零值 index 键不得触发假 partial，实测 "
            f"rounds_partial={g.get('rounds_partial')} "
            f"ungraded={g.get('ungraded_routes')}")


# ===========================================================================
# 4) 043：分母不得因 dict 变 0
# ===========================================================================

class TestRoundDenominator:
    def test_health_text_does_not_show_one_over_zero(self):
        """**043 本体**：`rounds_seen_by_route` 是 dict，文案不得出现 `1/0`。

        `_safe_int(dict)` 恒得 0 -> 旧文案 "投影 1/0 轮"。
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "ls_den", ROOT / "tools" / "live_session.py")
        ls = importlib.util.module_from_spec(spec)
        sys.modules["ls_den"] = ls
        spec.loader.exec_module(ls)

        row = ls.make_round_sample(
            index=1, latency_ms=400.0, alerts=(), error=False,
            quotes=5000, universe=5000, history_points=10, history_codes=10,
            max_deque=1, history_maxlen=360, watchlist_only=False,
            observation={
                "source": "sina", "capabilities": {"source": "sina"},
                "requested": 100, "returned": 97, "admitted": 95,
                "coverage": 0.97, "unknown_missing": ["000002"],
                "rejected_quality": [], "future_rejected": 0,
                "out_of_order_rejected": 0, "unavailable_capability": 0,
                # stocks 投影、覆盖 1 轮
                "raw_presence_known_by_route": {"stocks": False},
                "returned_by_route": {"stocks": 97},
                "admitted_by_route": {"stocks": 95},
                "reject_by_route": {"stocks": {"future": 0,
                                               "out_of_order": 0}},
            })
        m = ls.summarize_rounds([row])
        v = ls.evaluate_health(m)
        item = next(c for c in v["checks"]
                    if c["name"] == "raw_presence_evidence")
        txt = str(item.get("msg") or item.get("detail") or "")
        assert "1/0" not in txt, (
            f"分母不得因 dict 变 0 —— 出现 '1/0'：{txt!r}")
        assert "/1" in txt or "1/" in txt, (
            f"分母应为 1（真实覆盖轮数）：{txt!r}")

    def test_helper_sums_dict_not_zero(self):
        """直接钉住 `_safe_int(dict)` 的坑：门禁里必须**显式**处理 dict。"""
        import ast

        src = (ROOT / "tools" / "live_session.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "evaluate_health")
        seg = ast.get_source_segment(src, fn) or ""
        assert "isinstance(_gsr, dict)" in seg, (
            "`rounds_seen_by_route` 是 dict，门禁必须显式判类型后求和，"
            "不能直接 `_safe_int(dict)`（恒得 0）")
