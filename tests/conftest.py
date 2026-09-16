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
