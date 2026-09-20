"""IT-P0-001 的**行为级**验收（只用修复前就存在的 API）。

为什么单独一个文件
------------------
`test_session_close_auction.py` 导入了 `CLOSE_AUCTION`/`CALL_AUCTIONS` 等
**新符号**，所以在修复前的代码上会直接 `ImportError` —— 那是**结构性**红，
不能证明"行为真的变了"，只证明"符号不存在"。

本文件只使用 `session.py` 里**修复前就有**的 API：
`TradingCalendar.phase/is_open/elapsed_trading_seconds` 与
`SessionPhase.AFTERNOON`。因此它在旧代码上会**按断言失败**，这才是
能守住语义的回归 —— 将来即使换掉 `CLOSE_AUCTION` 的实现，
只要"14:57-15:00 不再算连续竞价、量能时钟不再走到 15:00"，
这些断言依然成立。

上游给的验收判据正是这个形态：
「断言 14:58 的 phase() 不是 MORNING/AFTERNOON（连续竞价），
 且 is_open(14:58) is False」。
"""
from __future__ import annotations

from datetime import datetime

from arad.session import SessionPhase, TradingCalendar

DAY = (2026, 9, 15)          # 周二，交易日


def at(h: int, m: int, s: int = 0) -> datetime:
    return datetime(*DAY, hour=h, minute=m, second=s)


def cal() -> TradingCalendar:
    return TradingCalendar(holidays=set())


# ---------------------------------------------------------------------------
# 1. 上游判据：14:58 不是连续竞价
# ---------------------------------------------------------------------------
def test_1458_is_not_continuous_session():
    """**不引用任何新枚举成员** —— 只断言"不是连续竞价那两个"。"""
    ph = cal().phase(at(14, 58))
    assert ph is not SessionPhase.MORNING, f"14:58 不应是早盘，实际 {ph}"
    assert ph is not SessionPhase.AFTERNOON, f"14:58 不应是午盘连续竞价，实际 {ph}"


def test_is_open_false_at_1458():
    """上游判据：``is_open(14:58) is False``。"""
    assert cal().is_open(at(14, 58)) is False


def test_is_open_true_just_before_boundary():
    """不得过度修正：14:56:59 仍必须是连续竞价。"""
    assert cal().is_open(at(14, 56, 59)) is True


def test_is_open_true_during_afternoon():
    assert cal().is_open(at(13, 30)) is True
    assert cal().is_open(at(14, 30)) is True


# ---------------------------------------------------------------------------
# 2. 量能时钟：分母不得走到 15:00
# ---------------------------------------------------------------------------
def test_elapsed_does_not_reach_14400():
    """旧实现 15:00 时 elapsed == 14400（4 小时整）。

    收盘集合竞价没有连续成交，把这段计入分母会让
    ``volume_lots / elapsed * 60`` 凭空变小 —— 放量速率被人为稀释。
    """
    v = cal().elapsed_trading_seconds(at(15, 0))
    assert v < 14400.0, f"15:00 的连续竞价时长应为 14220，实际 {v}"
    assert v == 14220.0


def test_elapsed_frozen_across_close_auction_window():
    """14:57-15:00 期间分母必须一动不动（不引用新符号）。"""
    c = cal()
    vals = [c.elapsed_trading_seconds(at(14, 57)),
            c.elapsed_trading_seconds(at(14, 57, 30)),
            c.elapsed_trading_seconds(at(14, 58)),
            c.elapsed_trading_seconds(at(14, 59))]
    assert len(set(vals)) == 1, f"收盘集合竞价期间分母不应增长：{vals}"


def test_elapsed_still_grows_before_boundary():
    """不得过度修正：连续竞价期间分母必须继续增长。"""
    c = cal()
    assert c.elapsed_trading_seconds(at(14, 0)) < c.elapsed_trading_seconds(at(14, 30))
    assert c.elapsed_trading_seconds(at(14, 30)) < c.elapsed_trading_seconds(at(14, 56))


def test_close_auction_not_silently_closed():
    """该时段**不是**休市/收盘：价格确实在动，看板要能显示"收盘集合竞价"。

    只断言它不是 POST、也不是 CLOSED —— 用修复前的枚举也能表达。
    """
    ph = cal().phase(at(14, 58))
    assert ph is not SessionPhase.POST, "14:58 尚未收盘"
    assert ph is not SessionPhase.CLOSED, "14:58 不是休市"


def test_1500_is_post():
    """15:00 整仍必须收口到 POST（左闭右开）。"""
    assert cal().phase(at(15, 0)) is SessionPhase.POST
    assert cal().is_open(at(15, 0)) is False


def test_describe_distinguishes_close_auction():
    """看板文案必须区分"连续竞价"与"集合竞价（无连续成交）"。"""
    c = cal()
    assert "交易中" in c.describe(at(10, 0))
    assert "无连续成交" in c.describe(at(14, 58))
    assert "无连续成交" in c.describe(at(9, 20))
    assert "无连续成交" not in c.describe(at(14, 0))


# ---------------------------------------------------------------------------
# 3. 引擎门控端到端（不过度修正 / 不漏抓）
# ---------------------------------------------------------------------------
def _engine_at(h, m, s=0):
    from arad.config import load_settings
    from arad.engine import Engine

    class _Counting:
        name = "counting"

        def __init__(self):
            self.calls = 0

        def snapshots(self, codes):
            self.calls += 1
            return []

        def health(self):
            return {"name": self.name, "ok": True, "latency_ms": 0, "err": ""}

    src = _Counting()
    moment = datetime(*DAY, hour=h, minute=m, second=s)
    eng = Engine(source=src, settings=load_settings(use_cache=False),
                 rules=[], notifiers=[], calendar=TradingCalendar(holidays=set()),
                 now_fn=lambda t=moment: t)
    eng._codes = ["600000"]
    return eng, src


def test_engine_still_fetches_during_close_auction():
    """14:58 必须**照常抓行情** —— 这是"不得过度修正"的端到端守卫。

    把收盘集合竞价一并排除出抓取窗口，就是错误方向相反的同一个 bug：
    该时段价格确实在动，只是没有连续成交。
    """
    eng, src = _engine_at(14, 58)
    eng.poll_once()                      # 不带 force，走真实时段判定
    assert src.calls > 0, "14:58 收盘集合竞价应当照常抓行情"


def test_engine_still_idles_after_close():
    """15:30 必须停抓（对照：别把门控改成永远放行）。"""
    eng, src = _engine_at(15, 30)
    eng.poll_once()
    assert src.calls == 0, "收盘后不应抓取"


def test_engine_still_idles_during_silence():
    """09:27 静默期仍必须停抓（原有语义不得被这次改动碰坏）。"""
    eng, src = _engine_at(9, 27)
    eng.poll_once()
    assert src.calls == 0, "静默期不应抓取"
