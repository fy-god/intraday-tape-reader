"""全市场粗筛：在进入规则之前剔除不值得告警的标的，降低规则开销与噪声。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .models import Board, Quote

__all__ = ["Filters"]


@dataclass
class Filters:
    """按 config.settings 的 `filters:` 节构造。"""

    min_price: float = 1.5
    max_price: float = 2000.0
    min_amount: float = 8_000_000.0
    exclude_st: bool = False
    exclude_boards: set[str] = field(default_factory=lambda: {"index"})
    exclude_codes: set[str] = field(default_factory=set)
    min_list_days: int = 11
    # code -> 上市日 "YYYYMMDD"（可选，来自股票池源）
    list_dates: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_cfg(cls, cfg: dict | None) -> "Filters":
        cfg = cfg or {}
        return cls(
            min_price=float(cfg.get("min_price", 1.5) or 0),
            max_price=float(cfg.get("max_price", 2000.0) or 1e9),
            min_amount=float(cfg.get("min_amount", 8_000_000) or 0),
            exclude_st=bool(cfg.get("exclude_st", False)),
            exclude_boards=set(cfg.get("exclude_boards") or []),
            exclude_codes={str(c) for c in (cfg.get("exclude_codes") or [])},
            min_list_days=int(cfg.get("min_list_days", 0) or 0),
            list_dates={},
        )

    # ------------------------------------------------------------------
    def _too_new(self, code: str, today: date | None = None) -> bool:
        """上市天数不足则跳过（数据缺失时放行，不误杀）。"""
        if self.min_list_days <= 0:
            return False
        raw = self.list_dates.get(code)
        if not raw:
            return False
        s = str(raw).strip()
        if len(s) < 8 or not s.isdigit():
            return False
        try:
            d = date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except ValueError:
            return False
        today = today or date.today()
        return (today - d).days < self.min_list_days

    def accept(self, q: Quote, today: date | None = None) -> bool:
        """该股票是否值得进入规则。"""
        if q.is_suspended:
            return False
        if q.code in self.exclude_codes:
            return False
        if q.board in (Board.INDEX,) or q.board.value in self.exclude_boards:
            return False
        if q.price < self.min_price or q.price > self.max_price:
            return False
        if q.amount < self.min_amount:
            return False
        if self.exclude_st and "ST" in (q.name or "").upper():
            return False
        if self._too_new(q.code, today):
            return False
        return True

    def apply(self, quotes: dict[str, Quote], today: date | None = None) -> dict[str, Quote]:
        return {c: q for c, q in quotes.items() if self.accept(q, today)}
