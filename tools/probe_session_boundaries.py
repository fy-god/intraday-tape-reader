"""验证「开盘那一刻」会发生什么：用假时钟把引擎推过 09:25 / 09:30 边界。

盘中能不能用，真正的风险不在实时代码，而在**开盘前后那几个状态转换**：
09:25 集合竞价结束、09:30 连续竞价开始、11:30 午休、15:00 收盘。
这些边界错了会表现为"开盘十分钟没有任何告警"或"午休时突然刷一堆"。

本脚本用可注入时钟（``Engine(now_fn=...)``）走一遍这些时刻，断言：
1. 开盘前（09:20）静默；
2. 09:30 一过立刻开始轮询；
3. 11:30:00 整点归入午休（右开区间，不能算还在早盘）；
4. 13:00 恢复；
5. 15:00:00 整点归入收盘。

跑法：``python tools\\probe_session_boundaries.py``
"""
from __future__ import annotations

import _console  # noqa: F401,E402  —— Windows 控制台 UTF-8（见 tools/_console.py）

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arad.config import load_settings               # noqa: E402
from arad.engine import Engine                      # noqa: E402
from arad.session import SessionPhase, TradingCalendar  # noqa: E402

DAY = datetime(2026, 9, 17)          # 周四，交易日

# (时刻, 期望时段, 期望 poll_once 是否真的抓行情)
#
# 时段语义（见 session.SessionPhase 的注释）：
#   CLOSED   非交易日，或交易日 09:15 之前
#   PRE_OPEN 09:15-09:25 开盘集合竞价（撮合中，价格会动 -> 要抓）
#   AUCTION  09:25-09:30 **静默**：可撤单不可成交，价格不再变化 -> 抓了也是白抓
#   MORNING  09:30-11:30（11:30 整点已归午休，右开区间）
#   LUNCH    11:30-13:00
#   AFTERNOON 13:00-15:00（15:00 整点已收盘）
#   POST     交易日 15:00 之后 = 已收盘；注意它**不等于** CLOSED（休市）
CASES = [
    ("09:00:00", SessionPhase.CLOSED,    False),
    ("09:14:59", SessionPhase.CLOSED,    False),
    ("09:15:00", SessionPhase.PRE_OPEN,  True),
    ("09:24:59", SessionPhase.PRE_OPEN,  True),
    ("09:25:00", SessionPhase.AUCTION,   False),
    ("09:29:59", SessionPhase.AUCTION,   False),
    ("09:30:00", SessionPhase.MORNING,   True),
    ("10:30:00", SessionPhase.MORNING,   True),
    ("11:29:59", SessionPhase.MORNING,   True),
    ("11:30:00", SessionPhase.LUNCH,     False),
    ("12:00:00", SessionPhase.LUNCH,     False),
    ("12:59:59", SessionPhase.LUNCH,     False),
    ("13:00:00", SessionPhase.AFTERNOON, True),
    ("14:59:59", SessionPhase.AFTERNOON, True),
    ("15:00:00", SessionPhase.POST,      False),
    ("20:00:00", SessionPhase.POST,      False),
]


class CountingSource:
    """记录被调用次数的假行情源（不联网）。"""

    name = "counting"

    def __init__(self):
        self.calls = 0

    def snapshots(self, codes):
        self.calls += 1
        return []

    def health(self):
        return {"name": self.name, "ok": True, "latency_ms": 0, "err": ""}


fails: list[str] = []
cal = TradingCalendar(holidays=set())
st = load_settings(use_cache=False)


def name_of(phase) -> str:
    return getattr(phase, "value", str(phase))


print(f"{'时刻':<10}{'期望时段':<12}{'实际时段':<12}{'抓行情':<8}判定")
print("-" * 62)
for hhmmss, want_phase, want_fetch in CASES:
    h, m, s = (int(x) for x in hhmmss.split(":"))
    moment = DAY.replace(hour=h, minute=m, second=s)

    src = CountingSource()
    eng = Engine(source=src, settings=st, rules=[], notifiers=[],
                 calendar=cal, now_fn=lambda t=moment: t)
    eng._codes = ["600000"]                      # pin 住，避免联网刷新

    phase = eng.calendar.phase(moment)
    eng.poll_once()                              # 不带 force：走真实时段判定
    fetched = src.calls > 0

    ok = (name_of(phase) == name_of(want_phase)) and (fetched == want_fetch)
    if not ok:
        fails.append(hhmmss)
    print(f"{hhmmss:<10}{name_of(want_phase):<12}{name_of(phase):<12}"
          f"{'是' if fetched else '否':<8}{'✓' if ok else '✗ 不符'}")

print("\n" + "=" * 62)
if fails:
    print(f"✗ 边界时刻判定不符：{fails}")
    raise SystemExit(1)
print("✓ 开盘/午休/收盘边界全部正确")
