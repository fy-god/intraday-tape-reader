"""pytest 引导：把 src 与 tests 加入路径，保证离线可跑。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for p in (ROOT / "src", ROOT, TESTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pytest  # noqa: E402


@pytest.fixture
def cal():
    """2026-09-15 是周二，正常交易日。"""
    from arad.session import TradingCalendar

    return TradingCalendar(holidays=set())


@pytest.fixture
def fake_source():
    from fakes import FakeSource

    return FakeSource()


def median_elapsed(fn, *, repeat: int = 7) -> float:
    """取 ``fn`` 的**中位**耗时（秒）—— 性能断言必须抗单次环境停顿。

    `IT-P1-WALLCLOCK-BOUND-IS-A-CORRECTNESS-GATE-001`
    （云端 `2026-09-23_17-58-08_JST.md` §2.1，我复核成立）：

    这两处性能用例原本写的是**单次** ``perf_counter()`` 差值与硬编码上界比较::

        t0 = time.perf_counter()
        rule.evaluate(snap, ctx)
        assert time.perf_counter() - t0 < 0.2

    单次采样会被**一次环境停顿**（GC / 调度 / 温度）整体翻转。
    云端实测该断言中位 **31.566ms**、最大 35.060ms，而上界 200ms ——
    即正常余量 **6.34x**，所以上一次那个 214.3ms 是约 **6.8 倍的
    环境性停顿**，不是"7.2% 的薄余量"。

    修法取中位数（云端建议 (b)）：断言的是**稳态性能**，
    单个离群样本不再翻转整套退出码。**没有删除任何断言**，
    也没有把性能用例移出正确性套件（那需要改 ``addopts``/``markers``
    配置，超出本轮边界）。
    """
    import statistics
    import time

    samples = []
    for _ in range(max(1, repeat)):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples)
