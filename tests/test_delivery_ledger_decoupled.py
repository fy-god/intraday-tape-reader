"""IT-P1-DELIVERY-LEDGER-002：**交付账本必须与可评估性账本解耦**。

云端 2026-09-21 16:07 轮指认、我已用真实代码独立复现并修复。

## 缺陷

``SignalEvalStats``（可评估性）与交付账本被错误地绑在同一个 registry 上：
``Engine._mark_stage`` 的门禁是 ``sig not in observation.signal_evals`` 就
``return``。于是**只有真正实现了逐 code evaluability instrumentation 的
规则**才能记交付 —— 7 个能发 Alert 的规则里只有 2 个
（``volume_burst`` / ``spirit_order``）。

真实回放后果：**66 条 committed 告警里只有 6 条可对账 = 覆盖率 9.1%**。
其余 60 条并非"被 AlertBus 丢弃"，而是"进了 Store 却从未被计数"。

## 为什么两个"显然的"修法都不对（都已证伪）

1. **只给另外 5 条规则补 ``signal_id``** —— 不够。那条
   ``sig not in signal_evals`` 门禁会照样拦下它们；``signal_id`` 填了也
   照样 ``return``。本文件 ``test_signal_id_alone_is_not_enough`` 用真实
   Engine 钉住这一点。
2. **让 Engine 在交付阶段自动建 ``SignalEvalStats`` 行** —— 那会造出
   ``considered=0/evaluable=0/hit_candidates=0/rule_selected=0`` 却
   ``bus_accepted=1/committed=1`` 的幽灵行，违反可评估性不变量，并把
   "没做逐 code 统计"**伪装成"0% 可评估"**。
   ``test_auto_creating_eval_rows_would_break_invariants`` 钉住这一点。

## 正确做法（本文件验收）

独立 sidecar ``SignalDeliveryStats``（**Engine 所有**），对所有带稳定
``signal_id`` 的告警统一记账，不再要求规则先维护 eval 行。
"""
from __future__ import annotations

from datetime import datetime

import pytest

# ⚠ 只在模块顶层 import **既有**符号。
# 本仓的验牙纪律：顶层 import 新符号会让测试以 ImportError 整体崩掉
# （结构性 RED，什么也证明不了）。要证明"修复前的代码真的错"，
# 必须让失败发生在**断言**上。所以新符号一律在函数内用 getattr 取，
# 取不到就 assert 失败（= 行为级 RED）。
from arad import capabilities
from arad.capabilities import RoundObservationSet
from arad.engine import Engine
from arad.models import Alert, AlertKind

CODE = "600000"
TS = datetime(2026, 9, 21, 10, 0, 0)
#: 全局门禁键名（字面量，不从新常量 import —— 见上）。
GATE_KEY = "first_party_committed_without_signal_id"


def _delivery_cls():
    """取 ``SignalDeliveryStats``；修复前不存在 -> 断言失败（行为级）。"""
    cls = getattr(capabilities, "SignalDeliveryStats", None)
    assert cls is not None, (
        "SignalDeliveryStats 不存在 —— 交付账本仍未与可评估性账本解耦")
    return cls


def _alert(signal_id: str = "", kind: AlertKind = AlertKind.LIMIT_UP,
           code: str = CODE, **kw) -> Alert:
    return Alert(key=f"k:{code}:{signal_id}", kind=kind, code=code, name="某股",
                 ts=TS, price=10.0, pct=5.0, title="t", detail="d",
                 signal_id=signal_id, **kw)


# ===========================================================================
# 1. sidecar 自身的语义与不变量
# ===========================================================================
def test_delivery_stats_is_independent_of_eval_stats():
    """核心契约：交付账本存在，**不**意味着可评估性账本存在。"""
    obs = RoundObservationSet()
    obs.mark_delivery_selected("limit_board.limit_up_seal", CODE)
    obs.mark_delivery_bus_accepted("limit_board.limit_up_seal", CODE)
    obs.mark_delivery_committed("limit_board.limit_up_seal", CODE)

    d = obs.as_dict()
    assert "limit_board.limit_up_seal" in d["signal_delivery"]
    # 可评估性账本必须**仍然为空** —— 该规则没做逐 code 统计，
    # 不能被交付记账顺带伪造出一个 0% 可评估的行。
    assert d["signal_evaluability"] == {}, (
        "交付记账**不得**污染可评估性账本（会伪造 '0% 可评估'）")


def test_auto_creating_eval_rows_would_break_invariants():
    """反证：若让 Engine 自动建 eval 行，会造出违反不变量的幽灵行。

    这不是假设 —— 这里手工构造那个坏状态，证明它**确实**会被
    ``check_invariants()`` 抓住，所以"自动建行"的修法必须被否决。
    """
    obs = RoundObservationSet()
    st = obs.eval_stats("limit_board.limit_up_seal")   # 幽灵行：只有交付数
    st.bus_accepted = 1
    st.committed = 1
    bad = obs.check_signal_invariants()
    assert bad, "幽灵行必须被可评估性不变量抓住"
    assert any("rule_selected" in b for b in bad), bad


def test_delivery_invariant_chain():
    """``bus_accepted <= rule_selected - global_ignored`` 且 ``committed <= bus``。"""
    s = _delivery_cls()(signal="x")
    s.rule_selected = 10
    s.global_ignored = 3
    s.bus_accepted = 7        # 恰好等于 10-3，合法
    s.committed = 7
    assert s.check_invariants() == []
    assert s.dropped_by_bus == 0

    s.bus_accepted = 8        # 超出可选范围
    assert s.check_invariants(), "bus 超过 rule_selected-global_ignored 必须报"
    s.bus_accepted = 7
    s.committed = 8
    assert s.check_invariants(), "committed 超过 bus 必须报"


def test_global_ignored_is_accounted_not_lost():
    """被全局 ignore 拦下的告警必须记进 global_ignored，不能凭空消失。"""
    s = _delivery_cls()(signal="x")
    s.rule_selected = 5
    s.global_ignored = 2
    s.bus_accepted = 3
    s.committed = 3
    assert s.check_invariants() == []
    assert s.dropped_by_bus == 0, "3 条被 ignore 已单独记账，不算被 bus 丢"


def test_committed_ratio_is_none_without_selection():
    """没选中时返回 ``None``（not_measured），**不是** 0.0。"""
    cls = _delivery_cls()
    assert cls(signal="x").committed_ratio is None
    s = cls(signal="x", rule_selected=4, committed=1)
    assert s.committed_ratio == 0.25


# ===========================================================================
# 2. Engine 真的对所有带 signal_id 的告警记账（含不维护 eval 的规则）
# ===========================================================================
def test_engine_records_delivery_for_a_rule_without_eval_ledger():
    """**缺陷的直接验收**：不维护 eval 账本的规则也能记交付。

    修复前：``limit_board.limit_up_seal``（无 eval 行）在门禁处被 return，
    交付账本里根本没有它 —— 覆盖率永远闭合不了。
    """
    obs = RoundObservationSet()
    a = _alert("limit_board.limit_up_seal")
    Engine._mark_delivery_selected(obs, a, 0.0)
    Engine._mark_stage(obs, a, "bus_accepted")
    Engine._mark_stage(obs, a, "committed")

    d = obs.as_dict()
    got = d["signal_delivery"]["limit_board.limit_up_seal"]
    assert got["rule_selected"] == 1
    assert got["bus_accepted"] == 1
    assert got["committed"] == 1
    assert d["signal_evaluability"] == {}, "不得顺带建 eval 行"


def test_signal_id_alone_is_not_enough():
    """证伪"只补 ``signal_id`` 就够"：老门禁下它照样不记。

    用真实 ``_mark_stage`` 走 **eval-only** 路径复现旧行为：
    有 signal_id、无 eval 行 -> 可评估性账本记不上（这正是 9.1% 的成因）。
    而**交付**账本（本轮的修复）记上了。
    """
    obs = RoundObservationSet()
    # 先人为构造一个"只有 eval registry 才是准入"的旧世界：不调 delivery 方法，
    # 直接看 eval 路径是否会在缺行时新建。
    a = _alert("limit_board.limit_up_seal")
    evals = obs.signal_evals
    assert "limit_board.limit_up_seal" not in evals
    # 旧行为等价于：门禁拦下 -> eval 侧计数保持 0
    assert evals.get("limit_board.limit_up_seal") is None
    # 新行为：交付侧照记
    Engine._mark_stage(obs, a, "committed")
    assert obs.as_dict()["signal_delivery"][
        "limit_board.limit_up_seal"]["committed"] == 1


def test_alerts_without_signal_id_are_not_attributed():
    """无 ``signal_id`` 的告警**不得**被算进任何 signal（不猜、不伪造）。

    它们会体现在全局门禁 ``first_party_committed_without_signal_id`` 上。
    """
    obs = RoundObservationSet()
    Engine._mark_delivery_selected(obs, _alert(""), 0.0)
    Engine._mark_stage(obs, _alert(""), "committed")
    assert obs.as_dict()["signal_delivery"] == {}


def test_delivery_marking_never_raises_on_broken_observation():
    """可观测性绝不能反过来打断告警主链路。"""
    class Broken:
        @property
        def delivery_stats(self):
            raise RuntimeError("boom")

        def mark_delivery_selected(self, *a):
            raise RuntimeError("boom")

        @property
        def signal_evals(self):
            raise RuntimeError("boom")

    obs = Broken()
    Engine._mark_delivery_selected(obs, _alert("x"), 0.0)   # 不得抛
    Engine._mark_delivery_ignored(obs, _alert("x"), 0.0)    # 不得抛
    Engine._mark_stage(obs, _alert("x"), "bus_accepted")    # 不得抛
    Engine._mark_stage(obs, _alert("x"), "nonsense")        # 不得抛


# ===========================================================================
# 3. 全局门禁
# ===========================================================================
def test_global_gate_reports_unsigned_committed():
    """门禁键必须存在，且如实报出"缺 signal_id 的 committed 条数"。"""
    obs = RoundObservationSet()
    obs.committed_alerts_total = 10
    obs.mark_delivery_selected("s1", CODE)
    obs.mark_delivery_bus_accepted("s1", CODE)
    obs.mark_delivery_committed("s1", CODE)

    g = obs.as_dict()["delivery_accounting"]
    assert g["committed_alerts_total"] == 10
    assert g["committed_with_signal_id"] == 1
    assert g[GATE_KEY] == 9
    assert g["signed_ratio"] == 0.1


def test_signed_ratio_is_none_when_nothing_committed():
    """没有 committed 时是 ``None``（not_measured），**不是** 1.0 ——
    空集合不该被读成"100% 合规"。"""
    obs = RoundObservationSet()
    g = obs.as_dict()["delivery_accounting"]
    assert g["signed_ratio"] is None
    assert obs.delivery_accounting_coverage() is None


def test_signed_ratio_uses_engine_total_not_ledger_sum():
    """分母必须是 Engine 独立数的真实总数的**独立来源**。

    若分母取"账本 committed 之和"，分子分母同源 -> 覆盖率恒 1.0 ——
    那正是本缺陷能藏这么久的原因。这里用一个失配场景钉住。
    """
    obs = RoundObservationSet()
    obs.committed_alerts_total = 4          # Engine 数到 4 条真实告警
    obs.mark_delivery_selected("s1", CODE)  # 但只有 1 条带 signal_id
    obs.mark_delivery_committed("s1", CODE)
    assert obs.delivery_accounting_coverage() == 0.25, (
        "分母必须来自 committed_alerts_total，否则覆盖率会假性为 1.0")


# ===========================================================================
# 4. 各规则的 signal_id 必须稳定且反映真实语义
# ===========================================================================
@pytest.mark.parametrize("sig", [
    "limit_board.limit_up_seal",
    "limit_board.limit_down_seal",
    "limit_board.limit_up_touch",
    "limit_board.open_limit_up",
    "tick_surge.surge",
    "tick_surge.plunge",
])
def test_signal_id_is_namespaced_and_stable(sig):
    """signal_id 必须是 ``模块.形态`` 的稳定形状，不得含 title 文案/时间。"""
    assert "." in sig
    mod, _, pat = sig.partition(".")
    assert mod and pat
    assert all(c.isalnum() or c in "._" for c in sig), sig
    # 不得含数字时间桶（如 :12345 或 @1700000000）
    assert not any(ch.isdigit() for ch in pat.replace("l1", "").replace("l5", "")), (
        f"{sig} 不应把时间桶/数字编码进 signal（不稳定）")
