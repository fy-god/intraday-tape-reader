"""`SourceManager` failover/accounting is implemented in EXACTLY ONE place.

缺陷类 (b)「same semantic implemented twice」的回归闸门。

## 背景

修前 ``SourceManager.call`` 与 ``SourceManager.call_detailed`` **各自手写**
了一份逐字相同的 failover + 失败记账循环：

* 热备成功分支 ``self._serving[route] = i`` / ``if i == self.idx: _fails=0``
  / ``else: n=_bump(route) ... if n >= threshold: idx=i; _fails.clear()``
* 全失败出口 ``n=_bump(route) ... if n>=threshold: _switch() ; raise last_exc``

两份实现**当前**行为一致（这是**潜在**可维护性缺陷，不是已确认的行为 bug），
所以本次重构**必须严格保行为**。两份拷贝一旦飘移，后果不对称且很难发现：
``call`` 用于股票池（``route="universe"``）与 CLI 扫描，``call_detailed``
用于盘中个股/指数主链路 —— 后者读的是 ``SnapshotFetchResult.source``，
前者读的是裸返回值。

## 本文件的四类断言

1. **行为等价**：同一场景下 ``call`` 与 ``call_detailed`` 的
   ``_serving`` / ``_fails`` / ``idx`` 演化逐个时点相同（用**同一个脚本化
   双源**驱动两条路径，逐轮对比状态）。
2. **单点真相**（AST）：``call`` 与 ``call_detailed`` 的方法体里**不得**
   再出现记账语句（``_serving[...] =``、``_fails[...] =``、``_fails.clear()``、
   ``self.idx =``、``self._switch()``、``self._bump(``）；它们必须只出现在
   ``_serve`` 里。用 AST 而不是子串匹配 —— 子串闸门可以被注释满足（本仓库
   被这个自证式咬过）。
3. **关键既有性质**：WP03（错类型必须 failover）、IT-P1-007（按路由分账）、
   全失败抛**最后一个**异常、``threshold`` 晋升时点 —— 对**两条路径**都钉住。
4. **闸门有牙**：把修前的 ``call`` 原文喂给 AST 检查器必须报错（否则是恒真式）。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from arad.engine import ROUTE_INDEX, ROUTE_STOCKS, SourceManager
from arad.sources.base import SourceError
from arad.sources.outcome import (
    PROVENANCE_EXACT,
    SnapshotFetchResult,
    build_outcome,
)

_ENGINE = pathlib.Path(__file__).resolve().parents[1] / "src" / "arad" / "engine.py"


# ===========================================================================
# 脚手架
# ===========================================================================
class _Quote:
    def __init__(self, code):
        self.code = code


class _Scripted:
    """脚本化数据源：``plain_script`` / ``detailed_script`` 每项要么是异常
    （抛出），要么是要返回的值。两个脚本分别喂 ``snapshots`` 与
    ``snapshots_detailed``，因此 ``call`` 与 ``call_detailed`` 可以共享
    同一个对象而互不干扰。"""

    def __init__(self, name, *, plain=None, detailed=None, wrong_type=False,
                 legacy_only=False):
        self.name = name
        self.plain = list(plain or [])
        self.detailed = list(detailed or [])
        self.wrong_type = wrong_type
        self.legacy_only = not detailed is None and legacy_only
        self.plain_calls = 0
        self.detailed_calls = 0

    @staticmethod
    def _take(script, default):
        if not script:
            return default
        item = script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def snapshots(self, codes):
        self.plain_calls += 1
        return self._take(self.plain, [])

    def universe(self):
        self.plain_calls += 1
        return self._take(self.plain, [])

    def snapshots_detailed(self, codes, *, route="stocks"):
        self.detailed_calls += 1
        if self.wrong_type and not self.detailed:
            return {"definitely": "not an outcome"}
        return self._take(
            self.detailed,
            build_outcome(
                route=route, source=self.name,
                normalized_request=tuple(str(c) for c in codes),
                raw_keys=tuple(str(c) for c in codes),
                quotes=tuple(_Quote(c) for c in codes),
                raw_rows=(), raw_presence_known=True,
                provenance=PROVENANCE_EXACT))

    def health(self):
        return {"name": self.name, "ok": True, "latency_ms": 0, "err": ""}


class _NoDetailed(_Scripted):
    """legacy 源：没有 ``snapshots_detailed``。"""

    def __init__(self, name, *, plain=None):
        super().__init__(name, plain=plain)

    def __getattribute__(self, item):
        if item == "snapshots_detailed":
            raise AttributeError(item)
        return object.__getattribute__(self, item)


def _state(mgr):
    return {
        "idx": mgr.idx,
        "fails": dict(mgr.fails_by_route),
        "serving": dict(mgr._serving),
    }


def _call_snapshots(mgr, route):
    return mgr.call("snapshots", ["600000"], route=route)


def _call_detailed(mgr, route):
    return mgr.call_detailed(["600000"], route=route)


# ===========================================================================
# 1) 行为等价：两条路径的状态演化必须逐时点相同
# ===========================================================================
PATHS = [
    pytest.param(_call_snapshots, id="call"),
    pytest.param(_call_detailed, id="call_detailed"),
]


def _driven(scenario):
    """按 ``scenario`` 构造主/备源 + threshold，返回 (mgr, primary, backup)。"""
    return scenario()


def _scenario_primary_down(long_run=False):
    n = 8 if long_run else 6
    primary = _Scripted("primary", plain=[SourceError("p")] * n,
                        detailed=[SourceError("p")] * n)
    backup = _Scripted("backup")
    return SourceManager([primary, backup], threshold=3), primary, backup


def _scenario_primary_recovers():
    primary = _Scripted("primary", plain=[SourceError("x"), None],
                        detailed=[SourceError("x")])
    backup = _Scripted("backup")
    return SourceManager([primary, backup], threshold=5), primary, backup


@pytest.mark.parametrize("invoke", PATHS)
def test_primary_down_backup_serves_then_promotes(invoke):
    """主源挂 -> 备用源顶上；第 ``threshold`` 次**正式晋升**。

    晋升时点必须**恰好**是第 ``threshold`` 次，早一轮/晚一轮都算错。
    """
    mgr, primary, backup = _scenario_primary_down()
    seen = []
    for _ in range(4):
        invoke(mgr, ROUTE_STOCKS)
        seen.append(_state(mgr))

    # 第 1、2 次：热备（idx 不动），fails 累加 1、2
    assert seen[0] == {"idx": 0, "fails": {ROUTE_STOCKS: 1},
                       "serving": {ROUTE_STOCKS: 1}}
    assert seen[1] == {"idx": 0, "fails": {ROUTE_STOCKS: 2},
                       "serving": {ROUTE_STOCKS: 1}}
    # 第 3 次 = threshold：晋升 + 计数清零
    assert seen[2] == {"idx": 1, "fails": {}, "serving": {ROUTE_STOCKS: 1}}
    assert seen[3] == {"idx": 1, "fails": {ROUTE_STOCKS: 0},
                       "serving": {ROUTE_STOCKS: 1}}
    assert mgr.current is backup


@pytest.mark.parametrize("invoke", PATHS)
def test_promotion_does_not_happen_before_threshold(invoke):
    """**阴性对照**：``threshold - 1`` 次之后**不得**晋升。"""
    primary = _Scripted("primary", plain=[SourceError("p")] * 5,
                        detailed=[SourceError("p")] * 5)
    backup = _Scripted("backup")
    mgr = SourceManager([primary, backup], threshold=3)

    for _ in range(2):
        invoke(mgr, ROUTE_STOCKS)
    assert mgr.idx == 0, "差一次就不该晋升"
    assert mgr.current is primary
    assert mgr.fails_by_route.get(ROUTE_STOCKS) == 2

    invoke(mgr, ROUTE_STOCKS)
    assert mgr.idx == 1, "第 threshold 次必须晋升"
    assert mgr.current is backup


@pytest.mark.parametrize("invoke", PATHS)
def test_primary_recovery_clears_its_route_counter(invoke):
    """主源恢复成功 -> 该路由计数清零，且**不**晋升。"""
    mgr, primary, backup = _scenario_primary_recovers()
    invoke(mgr, ROUTE_STOCKS)
    assert mgr.fails_by_route.get(ROUTE_STOCKS) == 1
    assert mgr.serving_of(ROUTE_STOCKS) == 1

    invoke(mgr, ROUTE_STOCKS)
    assert mgr.idx == 0, "主源恢复后不该晋升"
    assert mgr.fails_by_route.get(ROUTE_STOCKS) == 0
    assert mgr.serving_of(ROUTE_STOCKS) == 0


@pytest.mark.parametrize("invoke", PATHS)
def test_route_isolation_index_success_does_not_clear_stocks(invoke):
    """**IT-P1-007 本体**：``index`` 路由的成功不得清零 ``stocks`` 的计数。

    共用一个计数器时，几乎不失败的指数请求每轮都会把个股链路的失败清零，
    正式切换永远不发生 —— 个股一直由备用源供数，``health()`` 却显示主源
    active。
    """
    primary = _Scripted("primary", plain=[SourceError("big")] * 8,
                        detailed=[SourceError("big")] * 8)
    backup = _Scripted("backup")
    mgr = SourceManager([primary, backup], threshold=5)   # 阈值拉高，只看累计

    for _ in range(3):
        invoke(mgr, ROUTE_STOCKS)
    assert mgr.fails_by_route.get(ROUTE_STOCKS) == 3

    # 指数路由：备用源服务成功（主源在 index 上也挂）
    invoke(mgr, ROUTE_INDEX)
    assert mgr.fails_by_route.get(ROUTE_STOCKS) == 3, (
        "指数路由的调用把个股路由的失败计数改了 —— IT-P1-007 回归")
    assert mgr.fails_by_route.get(ROUTE_INDEX) == 1
    assert mgr.serving_of(ROUTE_STOCKS) == 1
    assert mgr.serving_of(ROUTE_INDEX) == 1
    assert mgr.idx == 0, "阈值未到，仍不该晋升"


@pytest.mark.parametrize("invoke", PATHS)
def test_route_isolation_when_primary_serves_index(invoke):
    """更贴近生产的一种：主源**对指数成功**、对个股失败。"""
    class _SizeSensitive(_Scripted):
        def snapshots(self, codes):
            self.plain_calls += 1
            if not codes or codes[0].startswith("sh"):
                return [_Quote("sh000001")]
            raise SourceError("大请求被限流")

        def snapshots_detailed(self, codes, *, route="stocks"):
            self.detailed_calls += 1
            if not codes or str(codes[0]).startswith("sh"):
                return build_outcome(
                    route=route, source=self.name,
                    normalized_request=tuple(str(c) for c in codes),
                    raw_keys=tuple(str(c) for c in codes),
                    quotes=tuple(_Quote(c) for c in codes),
                    raw_rows=(), raw_presence_known=True,
                    provenance=PROVENANCE_EXACT)
            raise SourceError("大请求被限流")

    primary, backup = _SizeSensitive("primary"), _Scripted("backup")
    mgr = SourceManager([primary, backup], threshold=3)

    for _ in range(3):
        invoke(mgr, ROUTE_STOCKS)
        mgr.call("snapshots", ["sh000001"], route=ROUTE_INDEX) \
            if invoke is _call_snapshots else \
            mgr.call_detailed(["sh000001"], route=ROUTE_INDEX)

    assert mgr.current is backup, (
        "个股连续失败 3 次后必须正式切换；仍指向 primary 说明计数被指数清零")
    assert mgr.fails_by_route.get(ROUTE_STOCKS, 0) == 0   # 晋升后清零
    assert mgr.serving_of(ROUTE_STOCKS) == 1


@pytest.mark.parametrize("invoke", PATHS)
def test_all_sources_fail_raises_last_exception_and_bumps(invoke):
    """全失败 -> 抛**最后**一个异常（不是第一个、不是包装异常），
    ``_bump`` 与 ``_switch`` 兜底记账一致。"""
    first_boom = ValueError("first")
    last_boom = KeyError("last")
    a = _Scripted("a", plain=[first_boom] * 2, detailed=[first_boom] * 2)
    b = _Scripted("b", plain=[last_boom] * 2, detailed=[last_boom] * 2)
    mgr = SourceManager([a, b], threshold=2)

    with pytest.raises(KeyError) as ei:
        invoke(mgr, ROUTE_STOCKS)
    assert ei.value is last_boom, "必须原样重抛最后一个异常对象"
    assert mgr.fails_by_route == {ROUTE_STOCKS: 1}, "全失败必须 _bump 一次"
    assert mgr.idx == 0, "未到阈值不切"
    assert ROUTE_STOCKS not in mgr._serving, "全失败不得记 serving"

    # 第 2 次 -> 达阈值：经 ``_switch`` 兜底切换 + 计数清零。
    #
    # ⚠ 顺序细节（值得钉住）：``_switch`` 在 ``raise`` **之前**执行，但本轮
    # 的 failover ``order`` 早在 `_switch` 之前就算好了，所以同一轮里仍然是
    # [a, b] —— "最后一个异常"依旧是 b 的 ``KeyError``。下一轮才会体现
    # 新顺序。
    with pytest.raises(KeyError) as ei2:
        invoke(mgr, ROUTE_STOCKS)
    assert ei2.value is last_boom
    assert mgr.idx == 1, "全失败累计到 threshold 也必须切换"
    assert mgr.fails_by_route == {}, "切换必须 clear"
    assert mgr.serving_of(ROUTE_STOCKS) is None, "全失败从未记过 serving"


@pytest.mark.parametrize("invoke", PATHS)
def test_single_source_all_fail_does_not_loop_forever(invoke):
    """只有一个源且全失败：抛异常，``_switch`` 不动 idx（不得死循环/回绕）。"""
    a = _Scripted("a", plain=[SourceError("only")] * 4,
                  detailed=[SourceError("only")] * 4)
    mgr = SourceManager([a], threshold=1)
    for _ in range(3):
        with pytest.raises(SourceError):
            invoke(mgr, ROUTE_STOCKS)
    assert mgr.idx == 0
    assert mgr.current is a


@pytest.mark.parametrize("invoke", PATHS)
def test_third_source_ordering_is_primary_then_remaining(invoke):
    """failover 顺序 = 主源优先，其余按下标递增（两条路径必须一致）。"""
    a = _Scripted("a", plain=[SourceError("a")], detailed=[SourceError("a")])
    b = _Scripted("b", plain=[SourceError("b")], detailed=[SourceError("b")])
    c = _Scripted("c")
    mgr = SourceManager([a, b, c], threshold=99)   # 只看这次谁供数

    invoke(mgr, ROUTE_STOCKS)
    assert mgr.serving_of(ROUTE_STOCKS) == 2, "应落到最后一个（第 3 个）源"
    assert a.plain_calls + a.detailed_calls == 1, "第 1 个源必须被**试过**一次"
    assert b.plain_calls + b.detailed_calls == 1, "第 2 个源必须被**试过**一次"
    assert c.plain_calls + c.detailed_calls == 1, "只有第 3 个源成功"
    assert mgr.fails_by_route.get(ROUTE_STOCKS) == 1


# ===========================================================================
# 2) 两条路径的**状态演化逐时点相同**（等价性的直接证据）
# ===========================================================================
def test_call_and_call_detailed_evolve_identically():
    """同一 failover 时间线下，``call`` 与 ``call_detailed`` 的
    ``_serving``/``_fails``/``idx`` 轨迹必须**逐时点相同**。

    这是"两份实现行为一致"这件事本身的可执行证据 —— 修前它也是绿的，
    所以它是**保行为**的监督者，不是新行为的主张。
    """
    def trace(invoke):
        primary = _Scripted("primary", plain=[SourceError("p")] * 7,
                            detailed=[SourceError("p")] * 7)
        backup = _Scripted("backup")
        mgr = SourceManager([primary, backup], threshold=3)
        out = []
        for _ in range(6):
            try:
                invoke(mgr, ROUTE_STOCKS)
            except Exception as exc:  # noqa: BLE001
                out.append(("exc", type(exc).__name__))
            else:
                out.append(("ok",))
            out.append((_state(mgr),))
        return out

    assert trace(_call_snapshots) == trace(_call_detailed)


# ===========================================================================
# 3) WP03：``call_detailed`` 的错类型必须 failover（唯一**只**属于它的性质）
# ===========================================================================
def test_detailed_wrong_type_fails_over_to_backup():
    """`IT-P1-CALL-DETAILED-TYPE-CONTRACT-001`（原 WP03）。

    修前 ``out._replace`` 在 ``try`` **外**抛 ``AttributeError``，直接冒出
    failover 循环 —— 备用源 ``calls == 0``。任何第三方源写错返回类型就能让
    盘中预警全线停摆。

    本断言在重构后**必须仍然绿**：合同校验（类型验证 / source 绑定 /
    route 规范化）整体仍在 ``try`` 内，由 ``_serve`` 的
    ``except Exception`` 接住。
    """
    bad = _Scripted("bad", wrong_type=True)
    good = _Scripted("good")
    mgr = SourceManager([bad, good], threshold=3)

    out = mgr.call_detailed(["600000"], route=ROUTE_STOCKS)

    assert isinstance(out, SnapshotFetchResult)
    assert good.detailed_calls == 1, (
        f"错类型必须按 source failure 处理并尝试备源，实测 good.detailed_calls="
        f"{good.detailed_calls}")
    assert bad.detailed_calls == 1
    assert out.source == "good", "source 必须绑到**实际供数**的源"
    assert out.route == ROUTE_STOCKS, "route 必须规范化为 caller 的 route"
    assert mgr.serving_of(ROUTE_STOCKS) == 1
    assert mgr.fails_by_route.get(ROUTE_STOCKS) == 1, "错类型计一次失败"


def test_detailed_wrong_type_on_every_source_still_raises():
    a = _Scripted("a", wrong_type=True)
    b = _Scripted("b", wrong_type=True)
    mgr = SourceManager([a, b], threshold=3)
    with pytest.raises(TypeError):
        mgr.call_detailed(["600000"], route=ROUTE_STOCKS)


def test_detailed_route_normalization_and_actual_source_binding():
    """route 必须规范化为 caller 的 route；source 必须绑**实际被调用对象**。"""
    class _Liar(_Scripted):
        def snapshots_detailed(self, codes, *, route="stocks"):
            self.detailed_calls += 1
            return build_outcome(
                route="index", source="someone-else",
                normalized_request=tuple(str(c) for c in codes),
                raw_keys=tuple(str(c) for c in codes),
                quotes=tuple(_Quote(c) for c in codes),
                raw_rows=(), raw_presence_known=True,
                provenance=PROVENANCE_EXACT)

    mgr = SourceManager([_Liar("real-name")], threshold=3)
    out = mgr.call_detailed(["600000"], route=ROUTE_STOCKS)
    assert out.route == ROUTE_STOCKS
    assert out.source == "real-name"
    assert mgr.serving_of(ROUTE_STOCKS) == 0
    assert mgr.serving_of("index") is None, "不得在别的路由留账"


def test_call_does_not_validate_return_type():
    """``call`` **不做**返回值合同校验：它调任意 ``method`` 并原样返回。

    这是 ``call`` 与 ``call_detailed`` 刻意保持的差异；统一记账时必须保留。
    """
    class _Anything:
        name = "anything"

        def snapshots(self, codes):
            return "definitely not a list of quotes"

        def health(self):
            return {"name": self.name, "ok": True, "latency_ms": 0, "err": ""}

    mgr = SourceManager([_Anything()], threshold=3)
    assert mgr.call("snapshots", ["600000"]) == "definitely not a list of quotes"


def test_call_returns_none_when_source_legitimately_returns_none():
    """``call`` 的 ``None`` 出口：源**合法**返回 ``None`` 时不得抛异常。

    这是 ``_serve`` 返回 ``ok=True`` 的路径（而不是全失败路径）。
    """
    class _NoneSource:
        name = "none-source"

        def snapshots(self, codes):
            return None

        def health(self):
            return {"name": self.name, "ok": True, "latency_ms": 0, "err": ""}

    mgr = SourceManager([_NoneSource()], threshold=1)
    assert mgr.call("snapshots", ["600000"]) is None
    assert mgr.serving_of("snapshots") == 0, "合法返回 None 也算成功供数"


def test_call_default_route_is_method_name():
    """缺省 route = 方法名（向后兼容老调用点）。"""
    bad = _Scripted("bad", plain=[SourceError("x")])
    good = _Scripted("good")
    mgr = SourceManager([bad, good], threshold=9)
    mgr.call("universe")
    assert mgr.fails_by_route == {"universe": 1}
    assert mgr.fails == 1


def test_call_legacy_source_still_works_through_call_detailed_path():
    """legacy 源（无 ``snapshots_detailed``）在重构后仍走投影降级路径。"""
    legacy = _NoDetailed("legacy", plain=[[_Quote("600000")]])
    mgr = SourceManager([legacy], threshold=3)
    out = mgr.call_detailed(["600000"], route=ROUTE_STOCKS)
    assert out.source == "legacy"
    assert out.raw_presence_known is False
    assert mgr.serving_of(ROUTE_STOCKS) == 0


# ===========================================================================
# 4) 单点真相（AST，不是子串）
# ===========================================================================
#: 记账语句的"指纹"。任何一条出现在 ``call`` / ``call_detailed`` 方法体内，
#: 都说明记账逻辑又有了第二份手写拷贝。
_MUTATORS = {
    "serving_assign",      # self._serving[route] = i
    "fails_assign",        # self._fails[route] = n
    "fails_clear",         # self._fails.clear()
    "idx_assign",          # self.idx = i
    "switch_call",         # self._switch()
    "bump_call",           # self._bump(route)
}


def _classify(node):
    """把一条语句归类到 ``_MUTATORS`` 的指纹（不认识 -> None）。"""
    # self._serving[...] = ... / self._fails[...] = ... / self.idx = ...
    if isinstance(node, ast.Assign):
        for tgt in node.targets:
            base = tgt.value if isinstance(tgt, ast.Subscript) else tgt
            if not isinstance(base, ast.Attribute):
                continue
            if not (isinstance(base.value, ast.Name) and base.value.id == "self"):
                continue
            if isinstance(tgt, ast.Subscript):
                if base.attr == "_serving":
                    return "serving_assign"
                if base.attr == "_fails":
                    return "fails_assign"
            elif base.attr == "idx":
                return "idx_assign"
        return None

    # self._fails.clear() / self._switch()
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        call = node.value
        fn = call.func
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Attribute):
            inner = fn.value
            if isinstance(inner.value, ast.Name) and inner.value.id == "self" \
                    and inner.attr == "_fails" and fn.attr == "clear" \
                    and not call.args and not call.keywords:
                return "fails_clear"
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) \
                and fn.value.id == "self" and fn.attr == "_switch" \
                and not call.args and not call.keywords:
            return "switch_call"
        return None

    # self._bump(route) —— 作为表达式或赋值右值
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) \
                    and fn.value.id == "self" and fn.attr == "_bump":
                return "bump_call"
    return None


#: ``_bump`` 的定位必须**排除**已归类的 ``serving/fails`` 赋值节点，
#: 否则 ``self._serving[route] = self._bump(...)`` 会被算成两个指纹。
_ORDERED = ("serving_assign", "fails_assign", "idx_assign",
            "fails_clear", "switch_call", "bump_call")


def _fingerprints(fn):
    """返回方法体内命中的指纹集合（每条语句只归一类，按 ``_ORDERED`` 优先）。"""
    hits = set()
    for stmt in ast.walk(fn):
        kind = _classify(stmt)
        if kind:
            hits.add(kind)
    return hits


def _findings(tree, class_name, wanted):
    """返回 ``{method_name: set(fingerprints)}``，只看指定类里指定名字的方法。"""
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for fn in node.body:
            if not isinstance(fn, ast.FunctionDef) or fn.name not in wanted:
                continue
            hits = _fingerprints(fn)
            out[fn.name] = hits
    return out


def _module_tree():
    return ast.parse(_ENGINE.read_text(encoding="utf-8"))


def test_bookkeeping_lives_only_in_serve():
    """**单点真相**：记账语句只能出现在 ``_serve`` 里。

    用 AST：``call`` / ``call_detailed`` 体内出现任何 ``_MUTATORS`` 指纹
    （``self._serving[...] =`` / ``self._fails[...] =`` / ``_fails.clear()``
    / ``self.idx =`` / ``self._switch()`` / ``self._bump(...)``）= 又一次
    "同一语义实现两次"。

    ⚠ 必须是 AST 而不是 ``"self._fails.clear()" in text``：后者可以被
    **注释里提到这句话**满足 —— 本仓库被这种自证式闸门咬过。
    """
    wanted = {"call", "call_detailed", "_serve"}
    found = _findings(_module_tree(), "SourceManager", wanted)

    assert set(found) == wanted, (
        f"没找到预期的三个方法，实测 {sorted(found)}")

    for name in ("call", "call_detailed"):
        assert not found[name], (
            f"`SourceManager.{name}` 里仍有手写的记账语句 {sorted(found[name])} "
            f"—— 记账必须只住在 `_serve`")

    assert found["_serve"], (
        "`_serve` 里一条记账语句都没有？那说明记账被搬到了别处（或闸门失配）")


def test_bookkeeping_gate_has_teeth():
    """**闸门自检**：把**修前**的 ``call`` 原文喂进去必须报错。

    否则上面的断言是恒真式（本仓库被"闸门无牙"咬过）。
    """
    pre_refactor_call = '''
class SourceManager:
    def call(self, method, *args, route=None):
        route = route or method
        order = [self.idx] + [i for i in range(len(self.sources)) if i != self.idx]
        last_exc = None
        for i in order:
            src = self.sources[i]
            try:
                out = getattr(src, method)(*args)
            except Exception as exc:
                last_exc = exc
                continue
            self._serving[route] = i
            if i == self.idx:
                self._fails[route] = 0
            else:
                n = self._bump(route)
                if n >= self.threshold:
                    self.idx = i
                    self._fails.clear()
            return out
        n = self._bump(route)
        if n >= self.threshold:
            self._switch()
        if last_exc:
            raise last_exc
        return None
'''
    bait = _findings(ast.parse(pre_refactor_call), "SourceManager", {"call"})
    assert bait["call"] == _MUTATORS, (
        f"闸门无牙：修前的 call 应当命中全部 {sorted(_MUTATORS)}，"
        f"实测只命中 {sorted(bait['call'])}")


def test_gate_fingerprints_cover_every_real_mutation():
    """自检：``_MUTATORS`` 必须**恰好**覆盖 ``_serve`` 里出现的全部指纹。

    如果有人在 ``_serve`` 里加了一种新的记账语句而忘了把它加进 ``_MUTATORS``，
    闸门就有了盲区 —— 这条断言负责发现那种漂移。
    """
    found = _findings(_module_tree(), "SourceManager", {"_serve"})
    assert found["_serve"] == _MUTATORS, (
        f"指纹表与 `_serve` 实际语句不一致："
        f"缺 {sorted(_MUTATORS - found['_serve'])}，"
        f"多 {sorted(found['_serve'] - _MUTATORS)}")


def test_both_public_methods_delegate_to_serve():
    """两条公开方法都必须真的**调用** ``_serve``（不是各写一份）。"""
    tree = _module_tree()
    calls = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "SourceManager":
            for fn in node.body:
                if not isinstance(fn, ast.FunctionDef) or fn.name not in (
                        "call", "call_detailed"):
                    continue
                called = {
                    sub.func.attr
                    for sub in ast.walk(fn)
                    if isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and isinstance(sub.func.value, ast.Name)
                    and sub.func.value.id == "self"
                }
                calls[fn.name] = called

    assert set(calls) == {"call", "call_detailed"}, sorted(calls)
    for name, called in calls.items():
        assert "_serve" in called, f"`{name}` 没有委托给 `_serve`：{sorted(called)}"
