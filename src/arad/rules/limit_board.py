"""涨停 / 跌停 识别：触板、封板、炸板。

判定口径
--------
* **封板**：现价 ≥ 涨停价（含容差）且封单额达标。
* **触板**：现价触及涨停价但未封住（仅 ``detect_touch`` 时告警）。
* **炸板**：近期曾封/触涨停，随后明显回落——用历史窗口与当日最高价双重判定。
* **一字板**：开盘即涨停价；**首板**：开盘未涨停（非一字）。

涨停价一律走 :attr:`Quote.limit_up_price`，它已按主板 10% / 双创 20% /
北交所 30% / ST 主板 5% 与数据源提供的真实涨停价处理，**不写死 1.1**。
"""
from __future__ import annotations

from datetime import datetime

from ..models import Alert, AlertKind, Quote, Snapshot
from ..session import CONTINUOUS
from .base import RuleContext, bucket_of, fmt_pct

__all__ = ["LimitBoardRule", "build", "RULE", "DEFAULT_CFG"]

DEFAULT_CFG: dict = {
    "enabled": True,
    "detect_touch": True,
    "detect_seal": True,
    "detect_break": True,
    "detect_limit_down": True,
    "touch_tolerance": 0.001,
    "min_seal_amount_wan": 200,
    "cooldown_seconds": 300,
    # 单轮最多推送条数（按 severity、|pct| 降序取前 N；<=0 表示不限量）
    "max_per_round": 20,
    "only_continuous": True,
    "severity": 2,
    # 内部常量（可覆盖）
    "break_lookback_seconds": 1800,
    "break_retreat_pct": 0.3,
    "break_bucket_seconds": 60,
}


class LimitBoardRule:
    """涨跌停状态机。"""

    name = "limit_board"

    def __init__(self, cfg: dict | None = None):
        merged = dict(DEFAULT_CFG)
        merged.update(cfg or {})
        self.cfg = merged
        self.tol = float(merged.get("touch_tolerance", 0.001) or 0.001)
        self.min_seal_wan = float(merged.get("min_seal_amount_wan", 0) or 0)
        self.cooldown = float(merged.get("cooldown_seconds", 300) or 300)
        self.severity = int(merged.get("severity", 2) or 2)
        self.lookback = float(merged.get("break_lookback_seconds", 1800) or 1800)
        self.break_retreat = float(merged.get("break_retreat_pct", 0.3) or 0.3)
        self.break_bucket = float(merged.get("break_bucket_seconds", 60) or 60)
        self.max_per_round = int(merged.get("max_per_round", 20) or 0)
        # 状态机记忆：key = f"{code}:{'up'|'down'}" -> away | near | broken | sealed。
        # 只在**状态跃迁**时告警：封板/触板/炸板都是事件，不是可以每轮重播的状态。
        self._state: dict[str, str] = {}
        self._state_day: str = ""

    # ------------------------------------------------------------------
    def _reset_if_new_day(self, now: datetime) -> None:
        day = now.strftime("%Y%m%d")
        if day != self._state_day:
            self._state.clear()
            self._state_day = day

    # ------------------------------------------------------------------
    def evaluate(self, snap: Snapshot, ctx: RuleContext) -> list[Alert]:
        if self.cfg.get("only_continuous", True) and ctx.session not in CONTINUOUS:
            return []

        now = ctx.now or datetime.now()
        self._reset_if_new_day(now)
        now_ep = ctx.now_epoch
        out: list[Alert] = []

        for code, q in snap.quotes.items():
            if q is None or q.is_suspended or q.price <= 0 or q.prev_close <= 0:
                continue

            up = self._limit_up_price(q)
            down = self._limit_down_price(q)
            if up <= 0 and down <= 0:
                continue

            # 天地板：同一只股票可能同时命中"曾涨停后炸板"与"现封跌停"，
            # 两个方向是独立事件，都要报，不能先到先得互相遮蔽。
            up_alert = self._check_side(q, ctx, now, now_ep, up=up, down=down, rising=True)
            if up_alert is not None:
                out.append(up_alert)
            if self.cfg.get("detect_limit_down", True) and down > 0:
                down_alert = self._check_side(q, ctx, now, now_ep, up=up, down=down, rising=False)
                if down_alert is not None:
                    out.append(down_alert)

        out.sort(key=lambda a: (-a.severity, -abs(a.pct)))
        if self.max_per_round > 0:
            out = out[: self.max_per_round]
        return out

    # ------------------------------------------------------------------
    @staticmethod
    def _limit_up_price(q: Quote) -> float:
        try:
            return float(q.limit_up_price)
        except Exception:  # noqa: BLE001
            return 0.0

    @staticmethod
    def _limit_down_price(q: Quote) -> float:
        try:
            return float(q.limit_down_price)
        except Exception:  # noqa: BLE001
            return 0.0

    def _check_side(self, q: Quote, ctx: RuleContext, now: datetime, now_ep: float,
                    *, up: float, down: float, rising: bool) -> Alert | None:
        """判定单侧（涨停或跌停）。

        只在**状态跃迁**时告警——封板/触板/炸板都是事件，而不是可以每轮重播的状态。
        一只票连续封板 2 小时只会报一次；炸板后一直不回头也只报一次。

        状态机（每只股票每个方向独立）::

            away ──触限价──► sealed ──跌离──► broken ──回落到位──► broken(已报)
              ▲                 │                │
              └─────回落──── near ◄────────────┘
        """
        limit = up if rising else down
        if limit <= 0:
            return None
        kind = AlertKind.LIMIT_UP if rising else AlertKind.LIMIT_DOWN
        at_limit = (q.price >= limit - self.tol) if rising else (q.price <= limit + self.tol)

        tag = f"{q.code}:{'up' if rising else 'down'}"
        prev = self._state.get(tag, "away")

        if at_limit:
            # 重新封回 -> 状态置为 sealed，并重新武装（快速回封后再炸板要能再报）
            self._state[tag] = "sealed"
            if prev == "sealed":
                return None                      # 一直在封板：状态未变，不重复报
            return (self._make_seal(q, ctx, now, now_ep, limit, kind, rising)
                    if self.cfg.get("detect_seal", True) else None)

        # 未在限价：判断是否曾触及（触板 / 炸板）
        # 跌停侧只识别"封跌停"：撬板（跌停被打开）不属于本规则口径，直接放过，
        # 但状态必须复位为 away，否则"封跌停→撬开→再封跌停"不会再报（状态机卡住）。
        if not rising:
            self._state[tag] = "away"
            return None
        # detect_break / detect_touch 各自独立生效，不能要求两者同时为假才短路。
        want_break = bool(self.cfg.get("detect_break", True))
        want_touch = bool(self.cfg.get("detect_touch", True))
        if not want_break and not want_touch:
            self._state[tag] = "away"
            return None
        if not self._touched_recently(q, ctx, now_ep, limit, rising):
            self._state[tag] = "away"
            return None

        retreat = (limit - q.price) / limit * 100.0
        if want_break and retreat >= self.break_retreat:
            if prev == "broken":
                return None                      # 已报过炸板且仍在回落状态：不重播
            self._state[tag] = "broken"
            return self._check_break(q, ctx, now, now_ep, limit)

        # 轻度回落：触板
        if not want_touch:
            self._state[tag] = "near"
            return None
        if prev == "near":
            return None                          # 触板状态未变，不重复报
        self._state[tag] = "near"
        return self._make_touch(q, ctx, now, now_ep, limit, kind, rising)

    # ------------------------------------------------------------------
    def _seal_amount_wan(self, q: Quote, rising: bool) -> float:
        """封单额（万元）：涨停看买一量，跌停看卖一量。"""
        if rising:
            vol = q.bid_vol if q.bid_vol > 0 else 0.0
        else:
            vol = q.ask_vol if q.ask_vol > 0 else 0.0
        return vol * 100.0 * q.price / 10000.0

    def _is_one_word(self, q: Quote, limit: float, rising: bool) -> bool:
        if rising:
            return q.open > 0 and q.open >= limit - self.tol and abs(q.high - q.low) < 1e-9
        return False

    def _is_first_board(self, q: Quote, limit: float, rising: bool) -> bool:
        """首板 = 开盘未涨停（非一字板）。"""
        if not rising:
            return False
        return not (q.open > 0 and q.open >= limit - self.tol)

    def _make_seal(self, q: Quote, ctx: RuleContext, now: datetime, now_ep: float,
                   limit: float, kind: AlertKind, rising: bool) -> Alert | None:
        seal_wan = self._seal_amount_wan(q, rising)
        if self.min_seal_wan > 0 and seal_wan < self.min_seal_wan:
            return None

        noun = "封涨停" if rising else "封跌停"
        seal_txt = f"封单{seal_wan / 10000:.2f}亿" if seal_wan >= 10000 else f"封单{seal_wan:.0f}万"
        one_word = self._is_one_word(q, limit, rising)
        first_board = self._is_first_board(q, limit, rising)
        board_txt = "一字板" if one_word else ("首板" if first_board else "非一字")

        bucket = bucket_of(now_ep, self.cooldown)
        detail = "\n".join([
            f"现价 {q.price:.2f}  涨跌 {fmt_pct(q.pct)}  {'涨停价' if rising else '跌停价'} {limit:.2f}",
            f"{seal_txt}  {board_txt}  换手 {q.turnover:.2f}%  成交额 {q.amount / 1e8:.2f}亿",
            f"开盘 {q.open:.2f}（{fmt_pct(q.open_pct)}）  最高 {q.high:.2f}  最低 {q.low:.2f}  振幅 {q.amplitude:.2f}%",
        ])
        return Alert(
            key=f"{q.code}:{kind.value}:seal:{bucket}",
            kind=kind, code=q.code, name=q.name, ts=now, price=q.price, pct=q.pct,
            title=f"{noun} {seal_txt}", detail=detail, severity=3,
            metrics={
                "price": round(q.price, 3), "pct": round(q.pct, 3),
                "limit_up": round(self._limit_up_price(q), 3),
                "limit_down": round(self._limit_down_price(q), 3),
                "distance_to_limit_pct": round((limit / q.price - 1.0) * 100.0, 3) if rising else 0.0,
                "seal_amount_wan": round(seal_wan, 1),
                "one_word_board": 1.0 if one_word else 0.0,
                "is_first_board": 1.0 if first_board else 0.0,
                "touch_count": float(self._touch_count(q, ctx, now_ep, limit, rising)),
                "stage": 2.0,
                # 细分信号名：短线精灵播报按它区分「封涨停板 / 打开涨停板」等，
                # 缺了它下游只能看到笼统的 limit_up，+16% 的触板会被显示成"涨停"。
                "pattern": "limit_up_seal" if rising else "limit_down_seal",
                "rising": 1.0 if rising else 0.0,
            },
        )

    def _make_touch(self, q: Quote, ctx: RuleContext, now: datetime, now_ep: float,
                    limit: float, kind: AlertKind, rising: bool) -> Alert:
        noun = "触及涨停" if rising else "触及跌停"
        bucket = bucket_of(now_ep, self.cooldown)
        gap = abs(limit - q.price)
        detail = "\n".join([
            f"现价 {q.price:.2f}  涨跌 {fmt_pct(q.pct)}  限价 {limit:.2f}（差 {gap:.3f}）",
            f"换手 {q.turnover:.2f}%  成交额 {q.amount / 1e8:.2f}亿  振幅 {q.amplitude:.2f}%",
            f"最高 {q.high:.2f}  最低 {q.low:.2f}  开盘 {q.open:.2f}",
        ])
        return Alert(
            key=f"{q.code}:{kind.value}:touch:{bucket}",
            kind=kind, code=q.code, name=q.name, ts=now, price=q.price, pct=q.pct,
            title=f"{noun} {fmt_pct(q.pct)}", detail=detail, severity=self.severity,
            metrics={
                "price": round(q.price, 3), "pct": round(q.pct, 3),
                "limit_up": round(self._limit_up_price(q), 3),
                "limit_down": round(self._limit_down_price(q), 3),
                "distance_to_limit_pct": round(gap / max(q.price, 1e-9) * 100.0, 3),
                "seal_amount_wan": 0.0,
                "one_word_board": 0.0,
                "is_first_board": 1.0 if self._is_first_board(q, limit, rising) else 0.0,
                "touch_count": float(self._touch_count(q, ctx, now_ep, limit, rising)),
                "stage": 1.0,
                "pattern": "limit_up_touch" if rising else "limit_down_touch",
                "rising": 1.0 if rising else 0.0,
            },
        )

    # ------------------------------------------------------------------
    def _touched_recently(self, q: Quote, ctx: RuleContext, now_ep: float,
                          limit: float, rising: bool = True) -> bool:
        """近期是否曾触及限价（历史窗口 + 快照极值兜底）。"""
        state = ctx.state
        try:
            pts = state.window(q.code, self.lookback, now_ep)
        except Exception:  # noqa: BLE001
            pts = []
        for _, price, _vol in pts:
            if (price >= limit - self.tol) if rising else (price <= limit + self.tol):
                return True
        # 兜底：快照的当日最高/最低（无历史时也能识别炸板）
        return (q.high >= limit - self.tol) if rising else (q.low <= limit + self.tol)

    def _touch_count(self, q: Quote, ctx: RuleContext, now_ep: float, limit: float,
                     rising: bool = True) -> int:
        """统计历史窗口内"进入限价区"的段数（连续点只算一段）。

        ``rising`` 决定比较方向：涨停侧看 ``price >= limit - tol``，
        跌停侧看 ``price <= limit + tol``。方向搞反会让任何高于跌停价的点位
        都被误计入，数值完全没有意义。
        """
        state = ctx.state
        try:
            pts = state.window(q.code, self.lookback, now_ep)
        except Exception:  # noqa: BLE001
            pts = []
        cnt = 0
        prev_in = False
        for _, price, _vol in pts:
            inside = (price >= limit - self.tol) if rising else (price <= limit + self.tol)
            if inside and not prev_in:
                cnt += 1
            prev_in = inside
        return cnt

    def _check_break(self, q: Quote, ctx: RuleContext, now: datetime,
                     now_ep: float, limit: float) -> Alert | None:
        """炸板：曾涨停，现价回落超过阈值。

        幂等由 ``_check_side`` 的状态机保证（``broken`` 状态不重复报），
        本方法只负责判定与构造告警。
        """
        if not self._touched_recently(q, ctx, now_ep, limit):
            return None
        # 必须已明显跌离涨停价
        if q.price > limit * (1.0 - self.break_retreat / 100.0):
            return None
        # 一字板不开板不算炸板（价仍在涨停）
        if q.price >= limit - self.tol:
            return None

        retreat = (limit - q.price) / limit * 100.0
        kind = AlertKind.LIMIT_UP
        bucket = bucket_of(now_ep, self.break_bucket)
        seal_wan = self._seal_amount_wan(q, True)
        detail = "\n".join([
            f"现价 {q.price:.2f}  涨跌 {fmt_pct(q.pct)}  涨停价 {limit:.2f}  回落 {retreat:.2f}%",
            f"最高 {q.high:.2f}  最低 {q.low:.2f}  振幅 {q.amplitude:.2f}%",
            f"换手 {q.turnover:.2f}%  成交额 {q.amount / 1e8:.2f}亿  买一封单 {seal_wan:.0f}万",
        ])
        return Alert(
            key=f"{q.code}:{kind.value}:break:{bucket}",
            kind=kind, code=q.code, name=q.name, ts=now, price=q.price, pct=q.pct,
            title=f"炸板 -{retreat:.2f}%", detail=detail, severity=3,
            metrics={
                "price": round(q.price, 3), "pct": round(q.pct, 3),
                "limit_up": round(self._limit_up_price(q), 3),
                "limit_down": round(self._limit_down_price(q), 3),
                "distance_to_limit_pct": round((q.price / limit - 1.0) * 100.0, 3),
                "seal_amount_wan": round(seal_wan, 1),
                "one_word_board": 0.0,
                "is_first_board": 1.0 if self._is_first_board(q, limit, True) else 0.0,
                "touch_count": float(self._touch_count(q, ctx, now_ep, limit, True)),
                "retreat_pct": round(retreat, 3),
                "stage": 3.0,
                "pattern": "open_limit_up",
                "rising": 1.0,
            },
        )


def build(cfg: dict | None = None) -> LimitBoardRule:
    return LimitBoardRule(cfg)


RULE = build({})
