"""全市场规模下的单轮耗时基准（回答"5 秒轮询够不够"）。

背景：``tools\\live_session.py`` 实测 5563 只时单轮 p95 = 5991ms、max = 6221ms，
**超过了配置的 5 秒轮询间隔**。README 里记录的 369ms 是 800 只的数字，不能直接
外推。本脚本把规模扫一遍，看成本是线性还是更差，并给出明确结论。

两种模式：
  --offline  合成行情，测**纯 CPU 成本**（规则+状态+去重+存储），无网络抖动
  --live     真抓行情（5563 只），测端到端

跑法：
  python tools\\bench_round.py --offline --sizes 500,1000,2000,4000,5563 --repeat 5
  python tools\\bench_round.py --live --repeat 5
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arad.config import load_settings                       # noqa: E402
from arad.engine import Engine, build_notifiers, build_rules  # noqa: E402
from arad.models import Board, Quote                        # noqa: E402
from arad.session import SessionPhase, TradingCalendar      # noqa: E402

DAY = datetime(2026, 9, 17)


def synth(code: str, price: float, i: int) -> Quote:
    """造一只有五档/内外盘/流通市值的行情，让所有规则都真的跑起来。

    注意 ``float_shares`` 是**派生属性**（由 ``float_cap`` 换算），不是构造参数，
    所以这里只给 ``float_cap``。
    """
    shares = 500_000_000.0
    return Quote(
        code=code, name=f"票{i:04d}", board=Board.MAIN,
        price=price, prev_close=price * 0.99, open=price * 0.995,
        high=price * 1.02, low=price * 0.98,
        volume_lots=120_000.0 + i, amount=(120_000.0 + i) * price * 100,
        turnover=1.5, volume_ratio=1.8,
        float_cap=shares * price / 1e8, total_cap=shares * price * 1.3 / 1e8,
        limit_up=round(price * 1.1, 2), limit_down=round(price * 0.9, 2),
        outer_vol=60_000.0 + i, inner_vol=60_000.0,
        bid_vols=(12_000.0, 8_000.0, 5_000.0, 3_000.0, 1_000.0),
        ask_vols=(9_000.0, 7_000.0, 4_000.0, 2_000.0, 900.0),
        bid_prices=tuple(round(price - 0.01 * k, 2) for k in range(1, 6)),
        ask_prices=tuple(round(price + 0.01 * k, 2) for k in range(1, 6)),
    )


def make_engine(st, moment: datetime) -> Engine:
    cal = TradingCalendar(holidays=set())
    class NoNet:
        name = "offline"
        def snapshots(self, codes): return []
        def health(self): return {"name": self.name, "ok": True, "latency_ms": 0, "err": ""}
    # 用**空源**构造，然后手工喂 state：这样测的是规则+状态+去重的纯 CPU 成本
    eng = Engine(source=NoNet(), settings=st, rules=build_rules(st),
                 notifiers=build_notifiers(st), calendar=cal,
                 now_fn=lambda: moment)
    return eng


def bench_offline(st, size: int, repeat: int) -> dict:
    """合成 N 只，跑 repeat 轮，返回每轮毫秒数。"""
    moment = DAY.replace(hour=10, minute=30, second=0)
    eng = make_engine(st, moment)

    codes = [f"{600000 + i:06d}" for i in range(size)]
    # 预热一轮，排除首次导入/懒初始化的开销
    quotes = [synth(c, 10.0 + (i % 50) * 0.1, i) for i, c in enumerate(codes)]
    eng.state.update(quotes, moment - timedelta(seconds=6))

    times: list[float] = []
    for r in range(repeat):
        # 轻微变动价格，让规则真的计算而不是全部命中缓存
        q2 = [synth(c, 10.0 + (i % 50) * 0.1 + 0.01 * (r + 1), i)
              for i, c in enumerate(codes)]
        t0 = time.perf_counter()
        eng.state.update(q2, moment + timedelta(seconds=6 * (r + 1)))
        snap = type("S", (), {"quotes": {q.code: q for q in q2},
                              "now": moment + timedelta(seconds=6 * (r + 1)),
                              "session": SessionPhase.MORNING})()
        for rule in eng.rules:
            ctx = type("C", (), {"cfg": st, "state": eng.state, "now": snap.now,
                                 "session": snap.session, "log": eng.log})()
            try:
                rule.evaluate(snap, ctx)
            except Exception:  # noqa: BLE001
                pass
        times.append((time.perf_counter() - t0) * 1000.0)
    return {"size": size, "times": times, "mode": "offline"}


def bench_live(st, repeat: int) -> dict:
    """真抓：直接计时 poll_once。"""
    moment = datetime.now()
    eng = Engine(source=None, settings=st)
    eng._maybe_refresh_universe(force=True)
    print(f"  股票池：{len(eng._codes)} 只")
    times: list[float] = []
    for i in range(repeat):
        t0 = time.perf_counter()
        eng.poll_once(force=True)
        ms = (time.perf_counter() - t0) * 1000.0
        times.append(ms)
        print(f"    第 {i+1} 轮：{ms:7.0f} ms  (行情 {len(eng.state.quotes)} 只)")
        time.sleep(0.5)
    return {"size": len(eng._codes), "times": times, "mode": "live"}


def summarize(res: dict, budget_ms: float) -> dict:
    t = sorted(res["times"])
    n = len(t)
    p = lambda q: t[min(n - 1, int(q * n))]           # noqa: E731
    med = statistics.median(t)
    return {
        "size": res["size"], "mode": res["mode"],
        "min": t[0], "p50": med, "p90": p(0.9), "p95": p(0.95), "max": t[-1],
        "ms_per_stock": med / max(1, res["size"]),
        "headroom": budget_ms / med if med > 0 else float("inf"),
        "fits": t[-1] < budget_ms,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="单轮耗时基准")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--sizes", default="500,1000,2000,4000,5563")
    ap.add_argument("--repeat", type=int, default=5)
    a = ap.parse_args()
    if not (a.offline or a.live):
        a.offline = True

    st = load_settings(use_cache=False)
    for name in ("spirit_price", "spirit_order", "spirit_index"):
        st.section("rules").setdefault(name, {})["enabled"] = True
    budget = float(st.get("poll.universe_seconds", 5) or 5) * 1000.0

    rows: list[dict] = []
    if a.offline:
        print("=== 离线（合成行情，纯 CPU 成本）===")
        for s in (int(x) for x in a.sizes.split(",")):
            r = bench_offline(st, s, a.repeat)
            rows.append(summarize(r, budget))
    if a.live:
        print("\n=== 联网（真实行情）===")
        rows.append(summarize(bench_live(st, a.repeat), budget))

    print(f"\n{'规模':>7}{'p50(ms)':>10}{'p90':>9}{'p95':>9}{'max':>9}"
          f"{'ms/只':>9}{'余量':>8}  5s 预算")
    print("-" * 74)
    for r in rows:
        print(f"{r['size']:>7}{r['p50']:>10.0f}{r['p90']:>9.0f}{r['p95']:>9.0f}"
              f"{r['max']:>9.0f}{r['ms_per_stock']:>9.3f}"
              f"{r['headroom']:>7.1f}x  {'✓' if r['fits'] else '✗ 超时'}")

    # 线性度：最大规模 vs 最小规模的 ms/只 之比
    if len(rows) >= 2:
        lo, hi = rows[0], rows[-1]
        ratio = hi["ms_per_stock"] / max(1e-9, lo["ms_per_stock"])
        print(f"\n线性度：{lo['size']} -> {hi['size']} 只，"
              f"每只成本变化 {ratio:.2f}x（≈1.0 = 线性，>1.5 = 超线性）")

    out = ROOT / "data" / f"bench_round_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"budget_ms": budget, "rows": rows},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告：{out}")

    bad = [r for r in rows if not r["fits"]]
    if bad:
        print(f"\n✗ {len(bad)} 个规模的最慢一轮超过 5 秒预算："
              f"{[r['size'] for r in bad]}")
        return 1
    print("\n✓ 所有测试规模都在 5 秒预算内")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
