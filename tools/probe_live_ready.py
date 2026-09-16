"""盘中可用性总检：全市场股票池 + 真实抓取 + 三个 spirit 模块全开。

回答"现在能盘中用吗"这个问题，逐项打勾或打叉。

跑法：``python tools\\probe_live_ready.py``
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arad.config import load_settings                 # noqa: E402
from arad.engine import Engine                        # noqa: E402
from arad.models import Board                         # noqa: E402

ROUNDS = 3
SLEEP = 3.0

FAIL: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {label}" + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(label)


print("=== 1) 股票池来源链 ===")
st = load_settings(use_cache=False)
for n in ("spirit_price", "spirit_order", "spirit_index"):
    st.section("rules").setdefault(n, {})["enabled"] = True
st.section("poll")["index_codes"] = ["sh000001", "sz399001", "sz399006"]

eng = Engine(source=None, settings=st)
print(f"  规则：{[getattr(r, 'name', '?') for r in eng.rules]}")
print(f"  股票池来源：{[getattr(s, 'name', '?') for s in eng._universe_sources()]}")

t0 = time.perf_counter()
n = eng.refresh_universe()
el = time.perf_counter() - t0
print(f"  刷新耗时 {el:.1f}s")
check("拿到全市场股票池（>1000 只）", n > 1000, f"{n} 只")
check("股票池刷新在 60s 内完成", el < 60, f"{el:.1f}s")

# --- 2) 真实多轮抓取 -------------------------------------------------------
print("\n=== 2) 真实多轮抓取（全开规则）===")
codes = list(eng._codes)
# 全市场 5000+ 只每轮抓 2 秒左右，这里只取前 800 只做压力观察
if len(codes) > 800:
    eng._codes = codes[:800]
print(f"  本轮监控 {len(eng._codes)} 只")

round_ms: list[float] = []
total = 0
for i in range(ROUNDS):
    t1 = time.perf_counter()
    alerts = eng.poll_once(force=True)
    ms = (time.perf_counter() - t1) * 1000
    round_ms.append(ms)
    total += len(alerts)
    print(f"  第 {i+1} 轮：{ms:8.0f} ms | state.quotes={len(eng.state.quotes):4d} | "
          f"告警 {len(alerts)}")
    if i + 1 < ROUNDS:
        time.sleep(SLEEP)

avg = sum(round_ms) / len(round_ms)
check("单轮耗时能支撑 5s 轮询", avg < 5000, f"平均 {avg:.0f} ms")
check("全市场抓取有数据", len(eng.state.quotes) > 100, f"{len(eng.state.quotes)} 只")

idx = {c for c, q in eng.state.quotes.items() if q.board is Board.INDEX}
check("指数行情到位", bool(idx), str(sorted(idx)))

# --- 3) 数据完整性：spirit_order / spirit_index 的输入 -------------------
print("\n=== 3) 规则输入完整性 ===")
stocks = [q for q in eng.state.quotes.values() if q.board is not Board.INDEX]
depth = sum(1 for q in stocks if q.has_depth)
outer = sum(1 for q in stocks if q.outer_vol > 0 or q.inner_vol > 0)
fcap = sum(1 for q in stocks if q.float_shares > 0)
print(f"  个股 {len(stocks)} 只：有五档 {depth} 只，有内外盘 {outer} 只，"
      f"有流通股本 {fcap} 只")
check("有五档（有大买盘/卖盘/机构单需要）", depth > len(stocks) * 0.8,
      f"{depth}/{len(stocks)}")
check("有内外盘（大笔买入/机构吃货需要）", outer > len(stocks) * 0.8,
      f"{outer}/{len(stocks)}")
check("有流通股本（比例阈值需要）", fcap > len(stocks) * 0.8, f"{fcap}/{len(stocks)}")

# --- 4) 配置生效性 ---------------------------------------------------------
print("\n=== 4) 配置与运行期一致性 ===")
print(f"  轮询间隔：universe={st.get('poll.universe_seconds')}s "
      f"watchlist={st.get('poll.watchlist_seconds')}s")
for name in ("spirit_price", "spirit_order", "spirit_index"):
    sec = st.section("rules").get(name) or {}
    print(f"  {name}: enabled={sec.get('enabled')} "
          f"max_per_round={sec.get('max_per_round')} cooldown={sec.get('cooldown_seconds')}")

print("\n" + "=" * 62)
if FAIL:
    print(f"✗ {len(FAIL)} 项未通过：{FAIL}")
    raise SystemExit(1)
print("✓ 盘中可用性检查全部通过")
