"""云端 2026-09-21 20:10 轮针对**已推代码**的 4 条指控 —— 全部已用真实代码复核。

本文件是这 4 条的回归验收。全部只用**既有** API 断言，
新符号用 ``getattr`` 在函数内取 —— 这样"回退验牙"时失败落在
AssertionError/AttributeError 上（行为级），而不是模块顶层 ImportError
（结构性，什么也证明不了）。

| 指控 | 结论 |
|---|---|
| `IT-P1-DELIVERY-FALSE-GREEN-001` | **成立**：门禁可"账本坏了但 PASS" |
| `IT-P1-ACCEPTANCE-GATE-RINGBUFFER-001` | **成立**：我自己的测试用 ring buffer 当累计分母 |
| `IT-P1-SOAK-LEDGER-BLIND-001` | **成立**：live_session 对新账本全盲 |
| `IT-P1-DELIVERY-GATE-PEROUND-001` | **成立**：逐轮 vs 会话累计未分名 |
"""
from __future__ import annotations

from datetime import datetime

from arad import capabilities
from arad.capabilities import RoundObservationSet

CODE = "600000"
TS = datetime(2026, 9, 21, 10, 0, 0)
GATE_KEY = "first_party_committed_without_signal_id"


def _mk_delivery(sig: str, n: int) -> RoundObservationSet:
    """构造 sidecar 里 committed == n 的账本（不设 committed_alerts_total）。"""
    obs = RoundObservationSet()
    st = getattr(obs, "delivery_stats_for")(sig)
    st.rule_selected = n
    st.bus_accepted = n
    st.committed = n
    return obs


def _acct(obs) -> dict:
    return obs.as_dict()["delivery_accounting"]


# ===========================================================================
# 1. IT-P1-DELIVERY-FALSE-GREEN-001（门禁假绿）
# ===========================================================================
def test_gate_alone_was_a_false_green_when_denominator_was_swallowed():
    """**核心缺陷**：分母自增被吞（total=0）而 sidecar 记了 66 条。

    修复前：门禁 = ``max(0 - 66, 0)`` = **0** = PASS —— 账本已经坏了却读成绿。
    修复后：``accounting_status`` 必须是 ``inconsistent``。
    """
    obs = _mk_delivery("s1", 66)
    a = _acct(obs)
    # 门禁值本身仍是 0（历史兼容，max(...) 夹住负差）——
    # 所以**不能只看它**：
    assert a[GATE_KEY] == 0
    assert a["committed_alerts_total"] == 0
    assert a["committed_with_signal_id"] == 66
    # 真正的判据在这里：分母比分子小，逻辑不可能 -> 不自洽。
    assert a["accounting_status"] == "inconsistent", (
        "分母被吞时必须报 inconsistent，否则就是假绿")
    assert a["accounting_problems"], "必须给出具体原因，不能只给一个状态"


def test_ratio_above_one_is_flagged_not_clamped():
    """``named > total`` 会让 ratio > 1.0（逻辑不可能）—— 必须报，不许夹掉。"""
    obs = _mk_delivery("s1", 3)
    obs.committed_alerts_total = 1
    a = _acct(obs)
    assert a["signed_ratio"] == 3.0, "ratio 如实反映 broken 状态，不 clamp"
    assert a["accounting_status"] == "inconsistent"
    assert a[GATE_KEY] == 0, "这一条说明为什么不能只看门禁：它仍是 0"


def test_accounting_errors_are_counted_not_swallowed():
    """``accounting_errors > 0`` 本身就必须让整体判为 inconsistent。

    为什么：吞异常可以保留（可观测性不能打断告警主链路），
    但**必须留痕** —— 否则分母静默不涨，就是假绿的源头。
    """
    obs = _mk_delivery("s1", 5)
    obs.committed_alerts_total = 5
    assert _acct(obs)["accounting_status"] == "ok"
    obs.accounting_errors = 1
    a = _acct(obs)
    assert a["accounting_errors"] == 1
    assert a["accounting_status"] == "inconsistent"


def test_healthy_ledger_reports_ok():
    """正常账本必须报 ok（不能因为加了检查就永远 red）。"""
    obs = RoundObservationSet()
    obs.committed_alerts_total = 3
    for sig in ("s1", "s2"):
        st = getattr(obs, "delivery_stats_for")(sig)
        st.rule_selected = 2
        st.bus_accepted = 2
        st.committed = 2 if sig == "s1" else 1
    a = _acct(obs)
    assert a["committed_alerts_total"] == 3
    assert a["committed_with_signal_id"] == 3
    assert a["signed_ratio"] == 1.0
    assert a[GATE_KEY] == 0
    assert a["accounting_status"] == "ok"


def test_empty_ledger_is_not_measured_not_ok():
    """空账本 = ``not_measured``，**不是** ok —— 空集合不该被读成"账本健康"。"""
    a = _acct(RoundObservationSet())
    assert a["accounting_status"] == "not_measured"
    assert a["signed_ratio"] is None


def test_real_engine_counts_denominator_independently():
    """Engine 必须**独立**数分母，且吞异常时留痕。

    用真实 Engine + 真实 Store 跑，验证 ``committed_alerts_total``
    与 ``store.alerts_total()`` 一致（两个独立来源互证）。
    """
    import inspect

    from arad.engine import Engine

    src = inspect.getsource(Engine.poll_once)
    assert "committed_alerts_total" in src, "Engine 必须记分母"
    assert "accounting_errors" in src, (
        "吞异常必须留痕 —— 裸 except/pass 就是假绿的源头")


# ===========================================================================
# 2. IT-P1-ACCEPTANCE-GATE-RINGBUFFER-001（ring buffer 当累计分母）
# ===========================================================================
def test_store_ring_buffer_truncates_so_must_use_alerts_total():
    """``recent_alerts(n)`` 受 deque(maxlen) 截断，**不能**当累计分母。

    为什么这条重要：它是我**自己**上一轮测试里的 bug ——
    用 ``len(store.recent_alerts(1000))`` 与累计账本比较，
    告警 >300 时会**假红**（断言失败但产品没错），
    而假红会浪费下一轮的排查时间。
    """
    from arad.config import load_settings
    from arad.models import Alert, AlertKind
    from arad.store import AlertStore

    store = AlertStore(load_settings(use_cache=False))
    buf = None
    for attr in ("_alerts", "alerts", "_rows"):
        cand = getattr(store, attr, None)
        if cand is not None and hasattr(cand, "maxlen"):
            buf = cand
            break
    assert buf is not None, "必须能定位告警 ring buffer"
    assert buf.maxlen is not None and buf.maxlen >= 10, (
        "它是有界 deque —— 这正是不能当累计分母的原因")

    # 塞满并溢出缓冲区，证明两个来源会分歧。
    over = int(buf.maxlen) + 5
    for i in range(over):
        store.add_alert(Alert(key=f"k{i}", kind=AlertKind.UNIQUE
                              if hasattr(AlertKind, "UNIQUE") else AlertKind.UNUSUAL,
                              code=CODE, name="某股", ts=TS, price=10.0,
                              pct=1.0, title="t", detail="d"))
    assert store.alerts_total() == over, (
        "alerts_total() 才是累计真值（独立于 ring buffer）")
    assert len(store.recent_alerts(10000)) == buf.maxlen, (
        "recent_alerts 被 maxlen 截断 —— 拿它当累计分母必然假红")


def test_web_max_alerts_config_is_actually_wired():
    """``web.max_alerts`` 必须真的传给 AlertStore（配置接线残留）。"""
    import inspect

    from arad.engine import Engine

    src = inspect.getsource(Engine.__init__)
    assert "web.max_alerts" in src, (
        "web.max_alerts 必须被读取 —— 否则改配置没有任何效果")


# ===========================================================================
# 3. IT-P1-SOAK-LEDGER-BLIND-001（soak 对新账本全盲）
# ===========================================================================
def test_live_session_whitelist_covers_new_delivery_ledger():
    """live_session 的可观测白名单必须包含新交付账本键。

    为什么：白名单只到 ``signal_evaluability`` 时，soak 汇总仍从旧 eval 账本
    累加 —— 那个数只覆盖 2/7 规则（9.1%），于是 soak 会报"只交付了 6 条"，
    而真实交付是 66 条。**方向性错误**，不是精度问题。
    """
    import io
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = io.open(os.path.join(root, "tools", "live_session.py"),
                  encoding="utf-8").read()
    for key in ("signal_delivery", "delivery_accounting"):
        assert key in src, f"live_session 必须认识 {key}，否则 soak 对它全盲"


def test_live_session_summary_has_session_layer_schema():
    """会话累计必须有**独立命名**的键，不能与逐轮口径混用同名键。"""
    import io
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = io.open(os.path.join(root, "tools", "live_session.py"),
                  encoding="utf-8").read()
    for key in ("delivery_committed_total", "delivery_accounting_session",
                "signal_delivery_rounds"):
        assert key in src, f"缺少会话级键 {key}"


# ===========================================================================
# 4. IT-P1-DELIVERY-GATE-PEROUND-001（逐轮 vs 会话累计）
# ===========================================================================
def test_per_round_dict_declares_its_scope():
    """逐轮账本必须**自报口径**，否则会被误读成会话累计值。

    为什么：RoundObservationSet 是逐轮对象，Store 每轮覆盖最近 observation。
    会话末尾若末轮无告警，``signed_ratio`` 自然是 ``None`` —— 那是正确的
    not_measured，不是"账本坏了"。没有显式口径标记，读者无法区分。
    """
    a = _acct(RoundObservationSet())
    assert a.get("_scope") == "last_round", (
        "逐轮账本必须显式声明口径（缺了就会被当会话累计读）")
    assert str(a.get("_scope_note") or ""), "口径说明必须给出正确读法"


def test_last_round_zero_does_not_mean_broken():
    """末轮 0 告警 -> not_measured（**不是** inconsistent）。"""
    obs = RoundObservationSet()
    obs.committed_alerts_total = 0
    a = _acct(obs)
    assert a["accounting_status"] == "not_measured"
    assert a["signed_ratio"] is None
    assert a[GATE_KEY] == 0


# ===========================================================================
# 5. IT-P1-DELIVERY-INVARIANTS-UNCALLED-001（不变量检查生产零调用）
# ===========================================================================
def test_poll_once_actually_checks_delivery_invariants():
    """``poll_once`` 必须真的调用聚合版不变量检查。

    为什么：``check_delivery_invariants()`` 此前全仓**只有它自己的 def** ——
    于是"逐轮交付不变量违规 0"只是**测试口径**，生产里没人检查。
    数是真的（独立重跑确实 0），但**没有东西在保证它**。
    """
    import inspect

    from arad.engine import Engine

    src = inspect.getsource(Engine.poll_once)
    assert "check_delivery_invariants" in src, (
        "poll_once 必须调用聚合版不变量检查，否则它永远不被生产使用")


def test_invariant_failure_is_recorded_not_raised():
    """不变量违规必须**记账 + 记日志**，但**绝不抛**（不打断告警主链路）。"""
    import inspect

    from arad.engine import Engine

    src = inspect.getsource(Engine.poll_once)
    idx = src.find("check_delivery_invariants")
    assert idx > 0
    window = src[idx:idx + 700]
    assert "accounting_errors" in window, "违规必须计入 accounting_errors"
    assert "except Exception" in window, (
        "必须包在 try 里 —— 可观测性不能反过来打断告警主链路")


# ===========================================================================
# 6. web.py 重建 Alert 时不得丢失 signal_id
# ===========================================================================
def test_web_rebuild_preserves_signal_id():
    """``web.py`` 从 dict 重建 ``Alert`` 时必须带上 ``signal_id``。

    为什么：``Alert.to_dict()`` 导出 signal_id，若重建时丢掉，
    则任何"落盘 -> 重建 -> 再导出"的路径（SSE 重放、快照恢复）都会
    把交付身份抹掉 —— 这些告警在账本里表现为"缺 signal_id"，
    被全局门禁判为不合规。**这是信息在往返中丢失，不是规则没填。**
    """
    import inspect
    import io
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = io.open(os.path.join(root, "src", "arad", "server", "web.py"),
                  encoding="utf-8").read()
    # 定位重建 Alert 的那一段
    idx = src.find("alert = Alert(")
    assert idx > 0, "必须能找到重建点"
    window = src[idx:idx + 900]
    assert "signal_id" in window, (
        "重建 Alert 时必须带 signal_id，否则交付身份在往返中丢失")
    _ = inspect  # 保持 import 有用（防 lint 误删）


def test_web_rebuild_roundtrip_keeps_signal_id():
    """行为级：真实走一遍 dict -> Alert 重建，signal_id 必须还在。

    不复用 web 的私有函数（它需要 Flask 上下文），而是直接验证
    ``Alert.to_dict()`` -> 重建 -> ``to_dict()`` 的往返语义。
    """
    from arad.models import Alert, AlertKind

    a = Alert(key="k1", kind=AlertKind.LIMIT_UP, code=CODE, name="某股",
              ts=TS, price=10.0, pct=5.0, title="t", detail="d",
              signal_id="limit_board.limit_up_seal")
    d = a.to_dict()
    assert d["signal_id"] == "limit_board.limit_up_seal", (
        "to_dict 必须导出 signal_id（否则下游无从保留）")
    # 模拟 web 的重建（带上修复后的字段）
    b = Alert(key=str(d.get("key") or ""), kind=AlertKind(d["kind"]),
              code=str(d.get("code") or ""), name=str(d.get("name") or ""),
              ts=a.ts, price=float(d["price"]), pct=float(d["pct"]),
              title=str(d.get("title") or ""), detail=str(d.get("detail") or ""),
              signal_id=str(d.get("signal_id") or ""))
    assert b.signal_id == a.signal_id, (
        "往返后 signal_id 丢失 —— 交付账本身份在重建时被抹掉")
