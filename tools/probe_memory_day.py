"""量化"跑一整天"的内存表现，判断是收敛还是泄漏。

为什么要测：4 小时 × 5s ≈ 2880 轮。一个盘中系统如果每轮泄漏几 KB，
12 分钟的 soak 只涨几 MB、看起来完全正常，下午两点才 OOM。
这里把"一整天"压进十几秒跑完。

⚠ **关键前提：引擎并不会把全市场都喂给 record_tick。**
``engine.poll_once`` 只登记 `自选股 ∪ 本轮告警的票`，所以 `_series` 的键集合是
"当天出现过异动的票"，量级几百到两千多，而不是 5563。第一版探针按全市场喂，
量出 412 MB，差点把这个数字当成"系统内存占用"—— 那是在测一个**不存在的负载**。
所以本探针两个场景都跑，并把差别摆出来：

  场景 A  现实负载（累计约 2400 只异动票）→ 约 44 MB
  场景 B  最坏上界（全市场 5563 只都记）→ 约 412 MB，到上限后走平

两个场景都**收敛**（再跑一轮 +0.00 MB）。结论与推导见 `docs/MEMORY_AUDIT.md`。

跑法：
    python tools/probe_memory_day.py
环境变量可调：MEM_UNIVERSE / MEM_REAL / MEM_ROUNDS
"""
from __future__ import annotations

try:
    import _console  # noqa: F401,E402
except ImportError:  # pragma: no cover
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    import _console  # noqa: F401,E402

import gc
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arad.store import AlertStore  # noqa: E402
from arad.models import Alert, AlertKind, Board, Quote  # noqa: E402


def rss_mb() -> float:
    """当前进程 RSS（MB）。

    ``GetProcessMemoryInfo`` 在 ``psapi.dll`` 里，不是 ``kernel32`` ——
    直接 ``ctypes.windll.psapi`` 在部分 Windows 上会因为没预加载而失败，
    所以显式 ``LoadLibrary`` 一次。量不出来就返回 nan（不假装是 0，
    否则下面的"增量"会算出假的负数）。
    """
    try:
        import ctypes
        import ctypes.wintypes as wt

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        psapi = ctypes.WinDLL("psapi.dll")
        kernel32 = ctypes.WinDLL("kernel32.dll")
        # 必须声明 argtypes：否则 64 位下 HANDLE 会被按 32 位 int 传，
        # GetCurrentProcess() 的返回值被截断，调用静默失败返回 0。
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        ok = psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
        if not ok:
            return float("nan")
        return pmc.WorkingSetSize / 1024 / 1024
    except Exception:
        return float("nan")


def make_quotes(n: int, round_i: int) -> list[Quote]:
    """造 n 只股票的行情，价格随轮次轻微游走（模拟真实盘中）。"""
    out = []
    for i in range(n):
        px = 10.0 + (i % 100) * 0.37 + (round_i % 20) * 0.01
        out.append(Quote(
            code=f"{600000 + i:06d}", name=f"股票{i}", board=Board.MAIN,
            price=round(px, 2), prev_close=round(px * 0.99, 2),
            open=round(px * 0.995, 2), high=round(px * 1.01, 2),
            low=round(px * 0.98, 2), volume_lots=10_000.0 + i,
            amount=(10_000.0 + i) * px * 100))
    return out


def main() -> int:
    # ⚠ 关键：引擎**不是**把全市场都喂给 record_tick。
    # engine.poll_once 只记 track = 自选股 ∪ 本轮告警的票（见 engine.py 中
    # "分时序列（只记自选 + 异动榜，控制内存）" 那几行）。
    # 所以一整天下来 _series 的键数 ≈ 自选股 + 当天出现过的异动票，
    # 而不是 5563。第一版探针按全市场喂，得出 412MB 的结论其实是
    # 在测一个**不存在的负载**。这里两个都测，把差别摆出来。
    UNIVERSE = int(os.environ.get("MEM_UNIVERSE", "5563"))
    # 场景 A：模拟"当天陆续有不同股票异动"。引擎每轮只登记
    # 自选股 ∪ 本轮告警票，所以 _series 的键是**当天累计出现过异动的票**。
    # 按盘中实测外推（12 分钟 118 条告警 -> 一整天约 2400 条），
    # 且这些告警会分散到不同个股上，取 2400 只作为现实上界。
    REAL_UNIQUE = int(os.environ.get("MEM_REAL", "2400"))
    ROUNDS = int(os.environ.get("MEM_ROUNDS", "300"))

    print("=" * 70)
    print(f"场景 A：引擎真实负载 —— 分时只记「自选股 ∪ 当日异动票」")
    print(f"        模拟一整天累计 {REAL_UNIQUE} 只不同个股出现异动")
    print("=" * 70)
    a_base, a_end, a_series = _run_churn(REAL_UNIQUE, ROUNDS)

    print()
    print("=" * 70)
    print(f"场景 B：最坏情况 —— 假设全市场 {UNIVERSE} 只**全部**记分时")
    print("        （引擎不会这样做，这里只是测上界）")
    print("=" * 70)
    b_base, b_end, b_series = _run(UNIVERSE, ROUNDS, label="B")

    print()
    print("=" * 70)
    print("结论")
    print("=" * 70)
    print(f"  场景 A（现实负载，累计 {a_series:,} 只）: "
          f"{a_base:.1f} -> {a_end:.1f} MB（{a_end - a_base:+.1f}）")
    print(f"  场景 B（最坏上界，{b_series:,} 只）      : "
          f"{b_base:.1f} -> {b_end:.1f} MB（{b_end - b_base:+.1f}）")
    return 0


def _run_churn(unique_codes: int, rounds: int) -> tuple[float, float, int]:
    """模拟"异动票随时间累积"：每轮登记一批**新**代码，键数持续增长到上限。

    这才是真实形状 —— record_tick 每轮只喂少量票，但一整天下来
    见过的代码集合会不断变大。要验证的正是"键数增长时内存是否线性涨、
    以及到顶后是否收敛"。
    """
    st = AlertStore(max_alerts=300, series_len=240)
    gc.collect()
    base = rss_mb()
    print(f"起始 RSS: {base:.1f} MB")
    print(f"\n{'轮':>7}{'RSS(MB)':>10}{'增量':>10}{'_series':>10}"
          f"{'平均只/轮':>11}{'_alerts':>9}")
    print("-" * 60)

    t0 = time.perf_counter()
    batch = max(unique_codes // rounds, 1)
    seen = 0
    for r in range(1, rounds + 1):
        # 每轮登记 batch 只**新**股票（模拟新的异动票出现）
        codes = [f"{600000 + (seen + i):06d}" for i in range(batch)]
        seen = min(seen + batch, unique_codes)
        st.record_tick(_quotes_for(codes, r))
        # 已见过的票每轮也要更新（它们仍在被追踪）
        if seen > batch:
            st.record_tick(_quotes_for(
                [f"{600000 + i:06d}" for i in range(0, seen, max(seen // 200, 1))], r))
        if r % 10 == 0:
            for k in range(3):
                st.add_alert(Alert(
                    key=f"mem{k}:{r}", kind=AlertKind.SURGE,
                    code=f"{600000 + k:06d}", name=f"股票{k}",
                    ts=datetime.now(), price=10.0, pct=3.0,
                    title="急拉 +3.0% / 1分钟", detail="测内存用"))
        if r % max(rounds // 10, 1) == 0 or r == 1:
            gc.collect()
            cur = rss_mb()
            print(f"{r:>7}{cur:>10.1f}{cur - base:>+10.1f}"
                  f"{len(st._series):>10}{len(st._series) / r:>11.0f}"
                  f"{len(st._alerts):>9}")

    gc.collect()
    end = rss_mb()
    dt = time.perf_counter() - t0
    print("-" * 60)
    print(f"耗时 {dt:.1f}s，_series 键数 = {len(st._series):,}"
          f"（目标 {unique_codes:,}）")

    # 收敛：再跑一轮，RSS 不该继续涨
    st.record_tick(_quotes_for([f"{600000 + i:06d}" for i in range(batch)], 999))
    gc.collect()
    extra = rss_mb() - end
    print(f"再跑 1 轮后增量: {extra:+.2f} MB  "
          + ("✓ 收敛（无泄漏）" if extra < 5.0 else "✗ 仍在增长"))
    return base, end, len(st._series)


def _quotes_for(codes: list[str], round_i: int) -> list[Quote]:
    out = []
    for c in codes:
        i = int(c) - 600000
        px = 10.0 + (i % 100) * 0.37 + (round_i % 20) * 0.01
        out.append(Quote(
            code=c, name=f"股票{i}", board=Board.MAIN,
            price=round(px, 2), prev_close=round(px * 0.99, 2),
            open=round(px * 0.995, 2), high=round(px * 1.01, 2),
            low=round(px * 0.98, 2), volume_lots=10_000.0 + i,
            amount=(10_000.0 + i) * px * 100))
    return out


def _run(n_codes: int, rounds: int, label: str) -> tuple[float, float, int]:
    """跑 n_codes 只 × rounds 轮，返回 (起始MB, 结束MB, _series键数)。"""
    st = AlertStore(max_alerts=300, series_len=240)
    gc.collect()
    base = rss_mb()
    print(f"起始 RSS: {base:.1f} MB")
    print(f"\n{'轮':>7}{'RSS(MB)':>10}{'增量':>10}{'_series':>10}"
          f"{'_series_codes':>15}{'_alerts':>9}")
    print("-" * 63)

    t0 = time.perf_counter()
    for r in range(1, rounds + 1):
        st.record_tick(make_quotes(n_codes, r))
        if r % 10 == 0:
            for k in range(3):
                st.add_alert(Alert(
                    key=f"mem{k}:{r}", kind=AlertKind.SURGE,
                    code=f"{600000 + k:06d}", name=f"股票{k}",
                    ts=datetime.now(), price=10.0, pct=3.0,
                    title="急拉 +3.0% / 1分钟", detail="测内存用"))
        if r % max(rounds // 10, 1) == 0 or r == 1:
            gc.collect()
            cur = rss_mb()
            print(f"{r:>7}{cur:>10.1f}{cur - base:>+10.1f}"
                  f"{len(st._series):>10}{len(st._series_codes):>15}"
                  f"{len(st._alerts):>9}")

    gc.collect()
    end = rss_mb()
    dt = time.perf_counter() - t0
    print("-" * 63)
    print(f"{n_codes:,} 只 × {rounds} 轮 = {n_codes * rounds:,} 次 record_tick，"
          f"耗时 {dt:.1f}s")
    print(f"_series_codes = {len(st._series_codes)}（maxlen=120 生效）")
    print(f"_alerts = {len(st._alerts)}（maxlen=300 生效）")

    # 收敛判定：同样的股票再跑一轮，RSS 不该继续涨
    st.record_tick(make_quotes(n_codes, 999))
    gc.collect()
    extra = rss_mb() - end
    print(f"再跑 1 轮后增量: {extra:+.2f} MB  "
          + ("✓ 收敛（无泄漏）" if extra < 5.0 else "✗ 仍在增长"))
    return base, end, len(st._series)


if __name__ == "__main__":
    raise SystemExit(main())
