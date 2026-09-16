"""测试替身：FakeSource / make_quote / RecordingNotifier / load_raw。

所有模块的测试都必须基于本文件，保证离线可跑（见 docs/DATA_CONTRACT.md 第 9 节）。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from arad.models import Alert, AlertKind, Board, Quote, Snapshot, board_of  # noqa: E402

RAW_DIR = ROOT / "fixtures" / "raw"
__all__ = [
    "ROOT", "SRC", "RAW_DIR", "load_raw", "load_text", "load_json",
    "make_quote", "FakeSource", "RecordingNotifier", "make_alert",
    "make_snapshot", "FakeClock",
]


def load_raw(name: str) -> bytes:
    """读 fixtures/raw/<name> 原始字节。"""
    return (RAW_DIR / name).read_bytes()


def load_text(name: str, encoding: str = "utf-8") -> str:
    return load_raw(name).decode(encoding, "replace")


def load_json(name: str):
    import json

    return json.loads(load_text(name))


def make_quote(
    code: str = "600000",
    name: str = "测试股",
    price: float = 10.0,
    prev_close: float = 10.0,
    **kw,
) -> Quote:
    """构造一个合理的 Quote；未给的字段按 prev_close 推导。"""
    board = kw.pop("board", None) or board_of(code, name)
    open_ = kw.pop("open", prev_close)
    high = kw.pop("high", max(price, open_, prev_close))
    low = kw.pop("low", min(price, open_, prev_close))
    volume_lots = kw.pop("volume_lots", 10000.0)
    amount = kw.pop("amount", volume_lots * 100.0 * price)
    return Quote(
        code=code, name=name, board=board, price=price, prev_close=prev_close,
        open=open_, high=high, low=low, volume_lots=volume_lots, amount=amount,
        **kw,
    )


def make_snapshot(quotes: Iterable[Quote], ts: datetime | None = None, seq: int = 1) -> Snapshot:
    ts = ts or datetime(2026, 9, 15, 10, 0, 0)
    return Snapshot(ts=ts, seq=seq, quotes={q.code: q for q in quotes})


def make_alert(
    code: str = "600000",
    kind: AlertKind = AlertKind.SURGE,
    name: str = "测试股",
    price: float = 10.5,
    pct: float = 5.0,
    ts: datetime | None = None,
    bucket: int = 0,
    **kw,
) -> Alert:
    ts = ts or datetime(2026, 9, 15, 10, 0, 0)
    return Alert(
        key=kw.pop("key", f"{code}:{kind.value}:{bucket}"),
        kind=kind, code=code, name=name, ts=ts, price=price, pct=pct,
        title=kw.pop("title", "测试告警"),
        detail=kw.pop("detail", "detail"),
        **kw,
    )


class FakeClock:
    """可手动推进的时钟，替换 datetime.now。"""

    def __init__(self, start: datetime | None = None):
        self.now = start or datetime(2026, 9, 15, 9, 30, 0)

    def __call__(self) -> datetime:
        return self.now

    def tick(self, seconds: float) -> datetime:
        from datetime import timedelta

        self.now = self.now + timedelta(seconds=seconds)
        return self.now

    def set(self, dt: datetime) -> datetime:
        self.now = dt
        return self.now


class FakeSource:
    """实现 Source 协议的测试数据源。

    用法::

        src = FakeSource([make_quote(price=10.0)])
        src.push([make_quote(price=10.5)])   # 推进一步行情
        src.universe()                        # 全市场
        src.snapshots(["600000"])             # 指定代码
    """

    name = "fake"

    def __init__(self, quotes: list[Quote] | None = None, *, fail_times: int = 0):
        self._quotes: dict[str, Quote] = {q.code: q for q in (quotes or [])}
        self.fail_times = fail_times
        self.calls: list[list[str]] = []

    # --- Source 协议 ---------------------------------------------------
    def universe(self) -> list[Quote]:
        self._maybe_fail()
        return list(self._quotes.values())

    def snapshots(self, codes: list[str]) -> list[Quote]:
        self._maybe_fail()
        self.calls.append(list(codes))
        return [self._quotes[c] for c in codes if c in self._quotes]

    def health(self) -> dict:
        return {"name": self.name, "ok": True, "latency_ms": 1, "err": ""}

    # --- 测试辅助 ------------------------------------------------------
    def push(self, quotes: list[Quote]) -> None:
        for q in quotes:
            self._quotes[q.code] = q

    def set(self, quote: Quote) -> None:
        self._quotes[quote.code] = quote

    def remove(self, code: str) -> None:
        self._quotes.pop(code, None)

    def _maybe_fail(self) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            from arad.sources.base import SourceError

            raise SourceError("fake source failure")


class RecordingNotifier:
    """记录所有收到的告警，供断言。

    ``alerts`` = **实际投递出去的**告警（无论引擎走 ``send`` 还是 ``send_digest``），
    ``digests`` 另外记录批量调用的分批情况。
    """

    name = "recording"

    def __init__(self):
        self.alerts: list[Alert] = []
        self.digests: list[list[Alert]] = []
        self.send_calls = 0

    def send(self, alert: Alert) -> bool:
        self.send_calls += 1
        self.alerts.append(alert)
        return True

    def send_digest(self, alerts: list[Alert]) -> bool:
        batch = list(alerts)
        self.digests.append(batch)
        self.alerts.extend(batch)          # 批量投递同样算"已送达"
        return True

    @property
    def codes(self) -> list[str]:
        return [a.code for a in self.alerts]

    @property
    def kinds(self) -> list[str]:
        return [a.kind.value for a in self.alerts]


class LegacyNotifier:
    """只实现 ``send`` 的旧式通知器，用于验证引擎的逐条兜底路径。"""

    name = "legacy"

    def __init__(self):
        self.alerts: list[Alert] = []

    def send(self, alert: Alert) -> bool:
        self.alerts.append(alert)
        return True

    @property
    def codes(self) -> list[str]:
        return [a.code for a in self.alerts]
