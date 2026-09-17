"""端到端验证「指数管道 -> 拉升指数信号 -> 精灵播报」这条链路。

回放剧本目前只造个股（``replay.py`` 不生成指数行情），所以这里手工灌入一段
上证指数走势，走真实的 ``Engine.poll_once`` + ``spirit_index`` 规则 + 展示层，
确认指数能从行情层一路走到中文播报。

跑法：``python tools\\shot_index.py``
"""
from __future__ import annotations

# Windows 控制台 UTF-8（见 tools/_console.py）。
# 先正常导入；若失败说明本文件是被**按路径**加载的（例如测试用 importlib
# 从 tests/ 里 exec 它），此时 tools/ 不在 sys.path 上——把本文件所在目录
# 补进去再试一次，这样"直接跑"和"被当模块加载"两种场景都能用。
try:
    import _console  # noqa: F401,E402
except ImportError:  # pragma: no cover - 取决于调用方式
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    import _console  # noqa: F401,E402

import io
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arad.config import load_settings                      # noqa: E402
from arad.engine import Engine                             # noqa: E402
from arad.models import Board, Quote                       # noqa: E402
from arad.notifiers.console import ConsoleNotifier          # noqa: E402
from arad.session import SessionPhase, TradingCalendar      # noqa: E402
import arad.notifiers.console as C                          # noqa: E402

C._VT_ENABLED = True

st = load_settings()
st.section("rules").setdefault("spirit_index", {})["enabled"] = True
st.section("poll")["index_codes"] = ["sh000001"]
# 只留指数规则，避免个股噪声干扰观察
for name in ("tick_surge", "limit_board", "volume_burst", "unusual",
             "spirit_price", "spirit_order"):
    st.section("rules").setdefault(name, {})["enabled"] = False

cal = TradingCalendar(holidays=set())
cal.phase = lambda now=None: SessionPhase.MORNING       # type: ignore[assignment]


class IndexOnlySource:
    """只喂指数：每轮推进 0.2 点，30 轮 = 6 点，远超 0.5 点阈值。"""

    def __init__(self, start: float = 3890.0):
        self.t = datetime(2026, 9, 15, 9, 30, 0)
        self.price = start
        self.n = 0

    def snapshots(self, codes):
        self.n += 1
        self.price += 0.2
        self.t += timedelta(seconds=10)
        return [
            Quote(code="sh000001", name="上证指数", board=Board.INDEX,
                  price=round(self.price, 2), prev_close=3864.28,
                  open=3866.0, high=round(self.price, 2), low=3865.0,
                  volume_lots=1e7, amount=1e11, ts=self.t)
            for _ in codes
        ]

    def health(self):
        return {"name": "index-only", "ok": True, "latency_ms": 1, "err": ""}


src = IndexOnlySource()
eng = Engine(source=src, settings=st, notifiers=[], calendar=cal)
eng.index_codes = ["sh000001"]
print(f"已启用规则：{[getattr(r, 'name', type(r).__name__) for r in eng.rules]}")
print(f"index_codes = {eng.index_codes}\n")

alerts = []
for i in range(40):
    eng.now = lambda s=src: s.t          # type: ignore[assignment]
    alerts.extend(eng.poll_once(force=True))

print(f"40 轮（每轮 +0.2 点，累计 +{src.price - 3890.0:.1f} 点）产生 {len(alerts)} 条告警\n")

buf = io.StringIO()
n = ConsoleNotifier(color=True, style="spirit", show_header=True, align_name=10,
                    stream=buf, enabled=True)
n._vt = True
for a in alerts:
    n.send(a)
sys.stdout.write(buf.getvalue())

pats = {(a.metrics or {}).get("pattern") for a in alerts}
print(f"\n信号种类：{pats}")
assert alerts, "指数持续拉升却没有产生告警！"
assert "index_pull" in pats, f"期望 index_pull，实际 {pats}"
print("✓ 指数管道 -> 拉升指数 -> 精灵播报 全链路正常")
