"""`IT-P2-QUOTE-SPEED-CLOCK-MISMATCH-050`

## 缺陷（真实 replay 链路复现）

``AlertStore._quote_item`` 算 ``speed_1m``/``speed_5m`` 时用 **墙钟**
``time.time()``；而 ``EngineState.history`` 的 ``p[0]`` 是**事件 ts**
（provider ts，由 ``Engine`` 的**可注入时钟** clamp）。窗口判据是
``cutoff <= p[0] <= now_epoch`` —— **两个时点来自不同时间轴**。

这属于本仓库 bug 类 **(b) 同一语义实现两次**：
``Engine.now()`` 已经是"当前时刻"的唯一权威实现（且可注入），
store 却又自己调了一次墙钟。

### 实测（修前）

真实 replay 链路（``Replay.build_engine`` + 真 ``AlertStore``）::

    引擎时钟（SimClock）now = 2026-09-30 09:30:00
    真实墙钟          now = 2026-09-30 12:21:07     <- 差 2h51m
    top_quotes(30,'speed') -> speed_1m 非零 0/30，speed_5m 非零 0/30
    # 且 sort=speed 与 sort=volume_ratio 返回**完全相同且未排序**的序列

``speed`` 是**异动榜的默认排序键**，所以回放模式下榜单退化成插入序。

### 为什么生产（实盘）看不出来

真实盘中 provider 的服务端时钟**跟着"现在"走**，与墙钟近似同步，
所以窗口能取到点。实测 live 语义（ts == 墙钟，价格每轮真实上涨）::

    speed_1m = 2.5   speed_5m = 2.5      <- 可用

**所以这是 replay 专属缺陷**，但 replay 正是用户在休市时"先看看长什么样"
的唯一入口（见 ``cli.cmd_serve`` 的 docstring），退化的榜单会直接误导。

### 修法

``AlertStore._now_epoch()``：优先用 ``engine.now()``（可注入时钟），
取不到再回落到 ``time.time()``。回落而不是硬依赖，是因为 ``_state()``
在引擎未 attach 时返回 ``None``，且 ``now_fn`` 理论上可能抛异常 ——
那时宁可退回墙钟（与修前一致），也不要让看板整页 500。
"""

from __future__ import annotations

import ast
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

STORE = ROOT / "src" / "arad" / "store.py"


def _store_src() -> str:
    return STORE.read_text(encoding="utf-8")


# ===========================================================================
# 1. 结构：quote 时间必须来自引擎时钟
# ===========================================================================

class TestQuoteTimeUsesEngineClock:
    def test_store_has_now_epoch_helper(self):
        """必须有一个集中的"取当前时刻"入口，而不是散落的 time.time()。"""
        assert "_now_epoch" in _store_src(), (
            "缺少 _now_epoch —— 时刻取值散落会导致两轴不一致复发")

    def test_helper_prefers_engine_now(self):
        """该入口必须**优先**读 ``engine.now()``。"""
        tree = ast.parse(_store_src())
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)
                   and n.name == "_now_epoch"), None)
        assert fn is not None, "找不到 _now_epoch"
        seg = ast.get_source_segment(_store_src(), fn) or ""
        assert 'getattr(eng, "now"' in seg or "getattr(eng, 'now'" in seg, (
            "_now_epoch 必须优先用引擎的可注入时钟")
        assert "time.time()" in seg, (
            "_now_epoch 必须保留墙钟回落（引擎未 attach 时不能整页 500）")

    def test_quote_item_does_not_call_wall_clock_directly(self):
        """``_quote_item`` 不得直接调 ``time.time()``（那正是缺陷本体）。

        用 AST 找调用节点，**不用子串** —— 注释里提到 ``time.time()``
        不应让门禁变红（子串同义反复）。
        """
        tree = ast.parse(_store_src())
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)
                   and n.name == "_quote_item"), None)
        assert fn is not None, "找不到 _quote_item"
        bad = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "time"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "time"
        ]
        assert not bad, (
            f"_quote_item 第 {[n.lineno for n in bad]} 行仍在直接调 "
            f"time.time() —— 必须走 _now_epoch()")


# ===========================================================================
# 2. 行为：replay 下 speed 必须非零（真实链路）
# ===========================================================================

class TestReplaySpeedIsUsable:
    @staticmethod
    def _run_replay():
        from arad.engine import AlertStore
        from arad.replay import Replay, default_universe

        store = AlertStore()
        rep = Replay(stocks=default_universe(), day=datetime(2026, 9, 30),
                     store=store, minutes=2.0, tick_seconds=5.0)
        eng = rep.build_engine()
        rep.run()
        return store, eng

    def test_speed_is_not_always_zero_in_replay(self):
        """**核心行为**：回放模式下 ``speed_1m``/``speed_5m`` 必须有用。

        修前实测 0/30 全零 —— 这是"数据算了但判决层零读者"的反面：
        榜单**在算**，但算出来恒 0。
        """
        store, _eng = self._run_replay()
        items = store.top_quotes(30, "speed")
        assert items, "回放应产出榜单"
        nz1 = sum(1 for it in items if (it.get("speed_1m") or 0) != 0)
        nz5 = sum(1 for it in items if (it.get("speed_5m") or 0) != 0)
        assert nz1 > 0, (
            f"replay 下 speed_1m 仍全零（{nz1}/{len(items)}）—— "
            f"窗口按墙钟切、历史点按模拟 ts 存，两轴不一致")
        assert nz5 > 0, f"replay 下 speed_5m 仍全零（{nz5}/{len(items)}）"

    def test_sort_keys_do_not_collapse_to_same_order(self):
        """``sort=speed`` 与 ``sort=volume_ratio`` 不得返回同一序。

        修前两者完全相同 —— 说明 speed 恒 0 让排序键失效，
        榜单退化成插入序（**假绿出口**：看起来排了，其实没排）。
        """
        store, _eng = self._run_replay()
        sp = [it["code"] for it in store.top_quotes(30, "speed")]
        vr = [it["code"] for it in store.top_quotes(30, "volume_ratio")]
        assert sp != vr, (
            "两个排序键返回完全相同且未排序的序列 —— speed 排序键失效")

    def test_speed_sort_is_actually_sorted(self):
        """``sort=speed`` 的结果必须**真的**按 speed 排序（不是插入序）。

        ⚠ 排序键是 ``-abs(speed_1m)`` —— **按绝对值降序**，不是按带符号值。
        这是对的：异动榜要"波动最大"的，急拉和急跌同样重要
        （``_SORTS["speed"] = lambda it: -abs(it.get("speed_1m") or 0.0)``）。
        我第一版断言写成"带符号降序"，被 ``-10.12`` 排在 ``5.68`` 前判红 ——
        **错的是我的断言，不是产品**。已按真实语义（绝对值降序）重写。
        """
        store, _eng = self._run_replay()
        items = store.top_quotes(30, "speed")
        vals = [abs(it.get("speed_1m") or 0) for it in items]
        assert vals == sorted(vals, reverse=True), (
            f"sort=speed 未真正按 |speed_1m| 降序排序：{vals[:8]}")


# ===========================================================================
# 3. 阳性对照：live 语义（ts == 墙钟）本来就能用，不得被改坏
# ===========================================================================

class TestLiveSemanticsUnchanged:
    def test_live_clock_still_produces_speed(self):
        """引擎时钟与事件 ts 同轴时，speed 必须可用（回归保护）。"""
        from datetime import datetime as _dt

        from fakes import make_quote

        from arad.engine import AlertStore, Engine
        from arad.session import SessionPhase, TradingCalendar

        class _Src:
            name = "tencent"

            def __init__(self):
                self.i = 0

            def universe(self, codes=None):
                return []

            def snapshots(self, codes):
                return {}

            def health(self):
                return {}

            def snapshots_detailed(self, codes, *, route="stocks"):
                from arad.sources.outcome import SnapshotFetchResult

                t = _dt.fromtimestamp(fixed[0] + self.i * 10)
                qs = [make_quote(code="600000", price=10.0 + 0.05 * self.i,
                                 ts=t, prev_close=10.0, open=10.0,
                                 high=10.0, low=10.0)]
                self.i += 1
                return SnapshotFetchResult(
                    route=route, source="tencent",
                    requested_keys=list(codes), raw_presence_known=True,
                    raw_returned_requested_keys=set(codes), quotes=qs,
                    rejected_quality_keys=[], unexpected_raw_keys=set(),
                    duplicate_raw_keys=[], raw_rows={},
                    provenance="exact_raw_presence")

        fixed = [1_700_000_000.0]
        cal = TradingCalendar(holidays=set())
        cal.phase = lambda now=None: SessionPhase.MORNING  # type: ignore
        store = AlertStore()
        eng = Engine(source=_Src(), calendar=cal, store=store,
                     now_fn=lambda: _dt.fromtimestamp(fixed[0]))
        eng._codes = ["600000"]
        eng._codes_pinned = True
        for k in range(6):
            fixed[0] += 10.0
            eng.poll_once(force=True)

        items = store.top_quotes(10, "speed")
        assert items, "live 语义下应有榜单"
        assert (items[0].get("speed_1m") or 0) != 0, (
            "live 语义（ts 与引擎时钟同轴）下 speed_1m 必须非零 —— 修 050 不得破坏它")
