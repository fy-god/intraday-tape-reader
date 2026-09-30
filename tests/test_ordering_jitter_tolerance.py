"""`IT-P1-ORDERING-GATE-USES-UNTRUSTED-CLOCK-047`

## 缺陷（真实代码 + 真实网络复现）

``source_time_contract.json`` 对三家源都写 ``role=unknown`` —— 仓内**没有**任何
权威字段规范证明 provider ts 是 event time。合同据此把 ``freshness_allowed``
设为 ``false``（不用它判新鲜度）。**但 ``strict_ordering_allowed`` 仍是
``true``，引擎继续用同一个不可信时钟做乱序水位线。**

于是出现**自相矛盾**：一边承认"不知道这个 ts 是什么时间"，
一边拿它当严格排序依据。

### 实测证据（2026-09-30 真实抓取，非构造）

1. **字段 30 是服务端时钟，不是最后成交时刻**
   午休时最后成交定格在 ``11:30:00``，而 tencent 的 ``fields[30]``
   显示 ``11:39~11:40``，且跟着"现在"走。

2. **倒退是板块专属的**
   ```
   组      倒退次数   总比较   倒退率
   sh      0          33      0.0%
   sz      0          22      0.0%
   bj      27         55      49.1%     <- 北交所
   ```

3. **全市场 soak 的 910 条乱序拒绝 100% 是 920xxx（北交所）**
   逐轮 `[0, 0, 256]`，被拒的 code 全是 `920000/920002/920003/920005/...`。

4. **倒退幅度分布**（10 轮全市场，913 个样本）
   ``min=3s / p50=12s / p90=21s / p99=24s / max=24s`` ——
   取值全是 **3 秒的整数倍**，说明是服务端 3 秒级刷新节拍。

5. **用户影响（受控实验，非现场推断）**
   午休价格冻结，现场测不出是否真丢数据。于是照抄实测抖动模式
   （±8s > 轮间隔 2s）驱动**真引擎**，价格每轮真实上涨：
   ```
   轮  收到ts      OOO   源价      引擎价     状态
   3  10:00:00   3     10.60    10.40    ★ 用了旧价
   5  10:00:01   3     11.00    10.80    ★ 用了旧价
   ```
   6 轮里 2 轮引擎价**落后**源价。机制：被拒的 tick 进不了 ``history``
   （它只在价格/量变化时追加），于是**新的价格永远进不来**。

### 修法

``TIME_POLICY`` 每源新增 ``ordering_jitter_tolerance``（秒），
乱序判据从 ``q_ep < wm`` 放宽到 ``q_ep < wm - tol``。

**为什么按源给而不是全局给**：抖动是**实测过的 provider 属性**，
不能外推到没测过的源。``tencent=60.0``（> 实测 max 24s，留 2.5x 余量）；
``sina``/``eastmoney`` 与未登记来源均为 ``0.0`` —— **行为与修前逐字节一致**。

**为什么不干脆关掉乱序门**：``strict_ordering_allowed=true`` 是合同明确声明，
且 ``test_engine_watermark_r2.py`` 有一整套真实行为验收（平价推进水位线、
迟到点必拒、被拒点不得压低水位线）。只放宽到"实测抖动级"。

**没有放宽的部分**（本文件逐条钉住）：水位仍严格取 ``max``；
超过容差的迟到包照旧被拒并计入 ``t_reject:out_of_order``；
未登记来源没有任何容差。
"""
from __future__ import annotations

import ast
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fakes import make_quote  # noqa: E402

from arad.engine import (  # noqa: E402
    DEFAULT_TIME_POLICY,
    TIME_POLICY,
    EngineState,
    time_policy_for,
)

NOW = datetime(2026, 9, 30, 10, 31, 0)
TOL = float(TIME_POLICY["tencent"]["ordering_jitter_tolerance"])


def _st(source: str = "tencent") -> EngineState:
    st = EngineState(history_len=360)
    st.begin_source_epoch("stocks", f"{source}#0", source_name=source)
    return st


def _q(code: str, ts: datetime, price: float = 10.0, vol: float = 100.0):
    return make_quote(code=code, price=price, volume_lots=vol, ts=ts,
                      prev_close=10.0, open=10.0, high=10.0, low=10.0)


# ===========================================================================
# 1. 合同表本身：容差必须按源声明，未登记来源必须为 0（fail-closed）
# ===========================================================================

class TestPolicyShape:
    def test_every_registered_source_declares_the_field(self):
        """每个登记源都必须显式声明容差 —— 不许靠默认值隐式补齐。"""
        for name, pol in TIME_POLICY.items():
            assert "ordering_jitter_tolerance" in pol, (
                f"{name} 未声明 ordering_jitter_tolerance")

    def test_tencent_has_a_positive_tolerance(self):
        """tencent 是**实测**有抖动的源，必须有正容差。"""
        assert TOL > 0, "tencent 实测抖动 max=24s，容差不能为 0"

    def test_tolerance_covers_measured_max_jitter(self):
        """容差必须 **> 实测最大抖动 24s**，否则仍会误拒。

        这是本缺陷的直接判据：实测 max=24s，容差 <=24 就还会丢包。
        """
        assert TOL > 24.0, (
            f"容差 {TOL}s 未覆盖实测最大抖动 24s —— 仍会误拒北交所行情")

    def test_tolerance_is_far_below_stale_threshold(self):
        """容差必须远小于陈旧阈值（4h）—— 不能把"真陈旧"也放进来。"""
        from arad.engine import STALE_TOLERANCE_SECONDS

        assert TOL < STALE_TOLERANCE_SECONDS / 100.0, (
            f"容差 {TOL}s 相对陈旧阈值 {STALE_TOLERANCE_SECONDS}s 过大，"
            f"会把分钟级真迟到当成抖动")

    def test_unregistered_source_gets_zero_tolerance(self):
        """**fail-closed**：没测过的来源不给任何容差。"""
        pol = time_policy_for("some_unknown_source_xyz")
        assert pol["ordering_jitter_tolerance"] == 0.0, (
            "未登记来源不得获得抖动容差 —— 那是凭空假设")
        assert DEFAULT_TIME_POLICY["ordering_jitter_tolerance"] == 0.0

    def test_sina_and_eastmoney_have_zero_tolerance(self):
        """未做过抖动实测的源保持 0.0 —— 行为与修前一致。"""
        for name in ("sina", "eastmoney"):
            assert TIME_POLICY[name]["ordering_jitter_tolerance"] == 0.0, (
                f"{name} 没有实测抖动数据，不得凭空给容差")


# ===========================================================================
# 2. 行为：容差内的倒退放行，容差外的照旧拒绝
# ===========================================================================

class TestJitterWithinTolerance:
    def test_small_backward_is_admitted(self):
        """**核心行为**：容差内的倒退必须被放行（这才是缺陷的修复）。"""
        st = _st("tencent")
        st.update([_q("920001", NOW)], NOW)
        wm = st.accepted_watermark["920001"]

        # 倒退 实测中位数 12s（容差内）
        back = NOW - timedelta(seconds=12)
        res = st.update_detailed([_q("920001", back, price=10.5)], NOW)

        assert res.rejected_out_of_order == 0, (
            "容差内的时钟抖动不得被判乱序")
        assert "920001" in res.admitted, "抖动包必须准入"
        assert st.quotes["920001"].price == 10.5, (
            "★ 关键：新的价格必须真的生效（这才是用户受影响的地方）")
        assert st.accepted_watermark["920001"] == wm, (
            "水位线不得因抖动包而倒退（仍严格取 max）")

    def test_measured_max_jitter_is_admitted(self):
        """实测 max=24s 的倒退必须放行 —— 这是 soak 里 910 条误拒的量级。"""
        st = _st("tencent")
        st.update([_q("920002", NOW)], NOW)
        back = NOW - timedelta(seconds=24)
        res = st.update_detailed([_q("920002", back, price=11.0)], NOW)
        assert res.rejected_out_of_order == 0
        assert st.quotes["920002"].price == 11.0

    def test_price_change_survives_jitter(self):
        """**用户可见后果**：抖动包的价格变化必须进入窗口。"""
        st = _st("tencent")
        st.update([_q("920003", NOW, price=10.0)], NOW)
        back = NOW - timedelta(seconds=12)
        st.update([_q("920003", back, price=10.5)], NOW)

        # 窗口基点取 NOW（10.0），抖动包虽然 ts 更早但价格是 10.5
        pts = st.window("920003", 300.0, NOW.timestamp())
        assert len(pts) == 2, f"两个点都该在窗口里，实测 {pts}"
        assert 10.5 in [p[1] for p in pts], (
            "抖动包的新价格必须进入历史窗口，否则急拉算不出来")


class TestBeyondToleranceStillRejected:
    def test_beyond_tolerance_is_rejected(self):
        """**没有放宽的部分**：超过容差的真迟到照旧被拒。"""
        st = _st("tencent")
        st.update([_q("600000", NOW)], NOW)
        late = NOW - timedelta(seconds=TOL + 60)
        res = st.update_detailed([_q("600000", late, price=8.0)], NOW)

        assert res.rejected_out_of_order == 1, (
            f"超过容差 {TOL}s 的迟到包必须被拒")
        assert "600000" not in res.admitted
        assert st.quotes["600000"].price == 10.0, "latest 不得倒退"

    def test_minutes_late_is_still_rejected(self):
        """分钟级迟到必须仍被拒 —— 容差不能变成"关掉乱序门"。"""
        st = _st("tencent")
        st.update([_q("600001", NOW)], NOW)
        res = st.update_detailed(
            [_q("600001", NOW - timedelta(seconds=600), price=9.0)], NOW)
        assert res.rejected_out_of_order == 1

    def test_unknown_source_has_no_tolerance(self):
        """未登记来源：**同样的倒退量**必须仍被拒（fail-closed 行为验证）。"""
        st = _st("mystery_src")
        st.update([_q("600002", NOW)], NOW)
        back = NOW - timedelta(seconds=12)      # tencent 会放行的量
        res = st.update_detailed([_q("600002", back, price=10.5)], NOW)

        assert res.rejected_out_of_order == 1, (
            "未登记来源不得因 tencent 的容差而放行 —— 容差必须按源隔离")
        assert st.quotes["600002"].price == 10.0

    def test_watermark_still_strictly_advances(self):
        """水位线语义没变：仍是无条件 ``max``。"""
        st = _st("tencent")
        st.update([_q("600003", NOW)], NOW)
        # 一个容差内倒退的包
        st.update([_q("600003", NOW - timedelta(seconds=5), price=10.1)], NOW)
        assert st.accepted_watermark["600003"] == NOW.timestamp(), (
            "抖动包不得把水位线拉回去")

    def test_rejected_packet_never_lowers_watermark(self):
        """被拒的包不得反过来压低水位线（既有性质，容差不得破坏）。"""
        st = _st("tencent")
        st.update([_q("600004", NOW)], NOW)
        wm = st.accepted_watermark["600004"]
        st.update([_q("600004", NOW - timedelta(seconds=600), price=1.0)], NOW)
        assert st.accepted_watermark["600004"] == wm


# ===========================================================================
# 3. 结构门禁：必须 AST 读策略，不得硬编码一个全局常数
# ===========================================================================

class TestStructuralGate:
    def test_gate_reads_tolerance_from_policy(self):
        """乱序判据必须**从策略读**容差，不得写死全局常数。

        用 AST 找 `update_detailed` 函数体，断言它读
        ``ordering_jitter_tolerance``（不是硬编码秒数）。
        """
        src = (ROOT / "src" / "arad" / "engine.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)
                   and n.name == "update_detailed"), None)
        assert fn is not None, "找不到 update_detailed"
        seg = ast.get_source_segment(src, fn) or ""
        assert "ordering_jitter_tolerance" in seg, (
            "update_detailed 必须从 TIME_POLICY 读容差（按源），"
            "而不是用全局常数 —— 否则无法按 provider 隔离")
        assert "_order_tol" in seg

    def test_no_orphan_global_tolerance_constant(self):
        """不得留下一个全局的 ORDER_JITTER_TOLERANCE_SECONDS 常数。

        那会让容差变成"所有源共用"，违背"容差是实测 provider 属性"。
        """
        src = (ROOT / "src" / "arad" / "engine.py").read_text(encoding="utf-8")
        assert "ORDER_JITTER_TOLERANCE_SECONDS" not in src, (
            "存在全局容差常数 —— 容差必须按源声明在 TIME_POLICY 里")

    def test_docstring_cites_the_measurement(self):
        """合同注释必须留下**实测依据**，否则后人无从复核取值。"""
        src = (ROOT / "src" / "arad" / "engine.py").read_text(encoding="utf-8")
        assert "920" in src, "注释应点明北交所（920xxx）是唯一受影响的板块"
