"""其他异动形态规则 —— ``rules/unusual.py``（AlertKind.UNUSUAL）。

一个规则内含 **5 个互相独立的子检测**，每条告警在 ``metrics["pattern"]`` 里标明
自己属于哪一种形态，并各自使用**独立的 key 命名空间**：

    key = f"{code}:unusual:{pattern}:{bucket}"
    bucket = bucket_of(now_epoch, cooldown_seconds)     # 默认 900

| pattern | 含义 | 触发条件 | severity |
|---|---|---|---|
| ``high_open_fade`` | 高开低走 | ``open_pct >= high_open_pct`` 且 ``(price-open)/open*100 <= -high_open_fade_pct`` | 2 |
| ``low_open_rise`` | 低开高走 | ``open_pct <= low_open_pct`` 且 ``(price-open)/open*100 >= low_open_rise_pct`` | 2 |
| ``wide_amplitude`` | 巨震 | ``amplitude >= wide_amplitude_pct`` 且 ``amount >= wide_amplitude_min_amount`` | 2 |
| ``late_surge`` | 尾盘异动 | ``minutes_to_close <= late_session_minutes`` 且 ``abs(price_change(300)) >= late_session_pct`` | 2 |
| ``reseal`` | 快速回封 | 近 ``reseal_seconds`` 内曾触及涨停 → 其后跌离涨停 >0.5% → **现在又回到涨停价** | 3 |

要点：
* 同一只股票同一轮**允许**同时出现多个不同 pattern 的告警；同一 pattern 每轮至多一条。
* ``reseal`` 完全依赖 ``state.history``：历史里找不到「曾涨停」的点就**不报**（绝不猜）。
* 排序：severity 降序 → ``|pct|`` 降序，取前 ``max_per_round``（默认 15）。
* 仅使用标准库；``evaluate`` 内不做任何 I/O。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Alert, AlertKind, Snapshot
from ..session import CONTINUOUS
from .base import RuleContext, bucket_of, fmt_pct

__all__ = [
    "DEFAULTS",
    "PATTERNS",
    "UnusualRule",
    "build",
    "RULE",
]

#: 五种子形态的稳定标识（写日志 / 存库 / 前端筛选用它，不要改）
PATTERNS = (
    "high_open_fade",
    "low_open_rise",
    "wide_amplitude",
    "late_surge",
    "reseal",
)

#: 中文名（用于 detail 首行，便于人工阅读与日报聚合）
PATTERN_CN: dict[str, str] = {
    "high_open_fade": "高开低走",
    "low_open_rise": "低开高走",
    "wide_amplitude": "巨震",
    "late_surge": "尾盘异动",
    "reseal": "快速回封",
}

DEFAULTS: dict = {
    "enabled": True,
    # 高开低走：开盘涨幅 >= 阈值 且 现价较开盘回落 >= 阈值
    "high_open_pct": 3.0,
    "high_open_fade_pct": 3.0,
    # 低开高走
    "low_open_pct": -3.0,
    "low_open_rise_pct": 3.0,
    # 巨震：振幅 >= 阈值 且 成交额达标（元）
    "wide_amplitude_pct": 9.0,
    "wide_amplitude_min_amount": 100_000_000.0,
    # 快速回封：炸板后 X 秒内重新封板
    "reseal_seconds": 600.0,
    # 判定「在涨停价上」的容差（元）
    "reseal_tolerance": 0.001,
    # 回封前必须跌离涨停的幅度（%）
    "reseal_pullback_pct": 0.5,
    # 尾盘异动
    "late_session_minutes": 30.0,
    "late_session_pct": 1.5,
    "late_session_window_seconds": 300.0,
    "cooldown_seconds": 900.0,
    "only_continuous": True,
    "severity": 1,
    "max_per_round": 15,
}

_KIND = AlertKind.UNUSUAL
_SEV_PATTERN = 2       # 形态类（高开低走/低开高走/巨震/尾盘异动）固定 severity
_SEV_RESEAL = 3        # 快速回封最紧急


def _num(v, default: float = 0.0) -> float:
    """宽松转 float：None / 非法值 / 空串 -> default。"""
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class UnusualRule:
    """形态类异动（高开低走/低开高走/巨震/尾盘异动/快速回封）。"""

    name: str = "unusual"
    cfg: dict = field(default_factory=dict)
    #: 状态型形态的边沿检测状态：``f"{code}:{pattern}"`` -> 当前是否处于该形态。
    #:
    #: 高开低走 / 低开高走 / 巨震这类条件一旦成立就会**持续成立**，
    #: 逐轮上报等于把同一件事反复播报：实测 4 小时里同一只票同一形态报 6 次、
    #: 标题完全相同（回放中 unusual 占到全部告警的 39%）。
    #: 真正的行情软件只在形态**成立的那一刻**提醒一次。
    _edge_state: dict = field(default_factory=dict, repr=False)
    _edge_day: str = field(default="", repr=False)
    #: 最近一次 evaluate 用的冷却秒数（_mk 需要它）
    _cooldown: float = field(default=900.0, repr=False)

    # ------------------------------------------------------------------
    def _edge(self, now, code: str, pattern: str, *, on: bool, off: bool) -> bool:
        """状态型形态的边沿检测：仅在 ``False -> True`` 时返回 True。

        ``on`` 是"进入该形态"的条件；``off`` 是"已明确恢复"的条件，
        两者之间留有迟滞，避免价格在阈值附近抖动导致反复上报。
        ``off`` 恒为 False 表示该形态当天不会恢复（例如振幅单调不减的巨震），
        因此一天只报一次。状态按自然日重置。
        """
        day = now.strftime("%Y-%m-%d") if now is not None else ""
        if day != self._edge_day:
            self._edge_day = day
            self._edge_state.clear()
        k = f"{code}:{pattern}"
        if not self._edge_state.get(k, False):
            if on:
                self._edge_state[k] = True
                return True
            return False
        if off:
            self._edge_state[k] = False
        return False

    # ------------------------------------------------------------------
    def _get(self, ctx: RuleContext, key: str, default):
        """取配置：ctx.cfg（引擎下发的分节配置）优先，其次自身 cfg，最后 default。"""
        for src in (getattr(ctx, "cfg", None), self.cfg):
            if isinstance(src, dict):
                v = src.get(key)
                if v is not None:
                    return v
        return default

    # ------------------------------------------------------------------
    def evaluate(self, snap: Snapshot, ctx: RuleContext) -> list[Alert]:
        if not bool(self._get(ctx, "enabled", True)):
            return []
        if bool(self._get(ctx, "only_continuous", True)) and ctx.session not in CONTINUOUS:
            return []

        cooldown = _num(self._get(ctx, "cooldown_seconds", 900.0))
        self._cooldown = cooldown          # 供 _mk 填 cooldown_seconds
        max_per_round = int(_num(self._get(ctx, "max_per_round", 15), 15.0))
        ts = ctx.now or snap.ts
        now_epoch = float(ctx.now_epoch)
        bucket = bucket_of(now_epoch, cooldown)

        # 纯配置项先取出来，避免在内层循环反复查表
        high_open_pct = _num(self._get(ctx, "high_open_pct", 3.0))
        fade_pct = _num(self._get(ctx, "high_open_fade_pct", 3.0))
        low_open_pct = _num(self._get(ctx, "low_open_pct", -3.0))
        rise_pct = _num(self._get(ctx, "low_open_rise_pct", 3.0))
        wide_amp = _num(self._get(ctx, "wide_amplitude_pct", 9.0))
        wide_amt = _num(self._get(ctx, "wide_amplitude_min_amount", 100_000_000.0))
        reseal_seconds = _num(self._get(ctx, "reseal_seconds", 600.0))
        reseal_tol = _num(self._get(ctx, "reseal_tolerance", 0.001))
        pullback = _num(self._get(ctx, "reseal_pullback_pct", 0.5))
        late_minutes = _num(self._get(ctx, "late_session_minutes", 30.0))
        late_pct = _num(self._get(ctx, "late_session_pct", 1.5))
        late_win = _num(self._get(ctx, "late_session_window_seconds", 300.0))
        minutes_to_close = _num(getattr(ctx, "minutes_to_close", 0.0), 0.0)

        picked: list[tuple[int, float, Alert]] = []
        for code, q in snap.quotes.items():
            if q is None:
                continue
            if q.is_suspended or q.price <= 0.0 or q.prev_close <= 0.0:
                continue

            pct = float(q.pct)
            open_ = float(q.open)
            open_ok = open_ > 0.0
            intraday = ((float(q.price) - open_) / open_ * 100.0) if open_ok else 0.0

            # --- 1. 高开低走 --------------------------------------------
            if high_open_pct > 0.0 and fade_pct > 0.0 and open_ok:
                if float(q.open_pct) >= high_open_pct and intraday <= -fade_pct:
                    # 状态型：只在"刚跌破"时报一次，回到半幅以上才算恢复
                    if self._edge(ts, code, "high_open_fade",
                                  on=True, off=intraday > -fade_pct * 0.5):
                        picked.append(self._mk(
                            q, ctx, ts, bucket, "high_open_fade", _SEV_PATTERN,
                            title=f"高开低走 {intraday:.1f}%",
                            extra=f"开盘 {open_:.2f} 元（{fmt_pct(q.open_pct)}）高开，"
                                  f"现价较开盘回落 {intraday:.2f}%",
                            metrics={"open_pct": float(q.open_pct),
                                     "from_open_pct": intraday},
                        ))
                else:
                    self._edge(ts, code, "high_open_fade", on=False, off=True)

            # --- 2. 低开高走 --------------------------------------------
            if low_open_pct < 0.0 and rise_pct > 0.0 and open_ok:
                if float(q.open_pct) <= low_open_pct and intraday >= rise_pct:
                    if self._edge(ts, code, "low_open_rise",
                                  on=True, off=intraday < rise_pct * 0.5):
                        picked.append(self._mk(
                            q, ctx, ts, bucket, "low_open_rise", _SEV_PATTERN,
                            title=f"低开高走 {intraday:+.1f}%",
                            extra=f"开盘 {open_:.2f} 元（{fmt_pct(q.open_pct)}）低开，"
                                  f"现价较开盘拉升 {intraday:+.2f}%",
                            metrics={"open_pct": float(q.open_pct),
                                     "from_open_pct": intraday},
                        ))
                else:
                    self._edge(ts, code, "low_open_rise", on=False, off=True)

            # --- 3. 巨震 -------------------------------------------------
            amp = float(q.amplitude)
            amount = float(q.amount)
            if wide_amp > 0.0 and amp >= wide_amp and amount >= wide_amt:
                # 振幅是"当日最高/最低"的函数，只会缓慢单调增长，
                # off 恒为 False -> 每股每天只报一次，不再刷屏。
                if self._edge(ts, code, "wide_amplitude", on=True, off=False):
                    picked.append(self._mk(
                        q, ctx, ts, bucket, "wide_amplitude", _SEV_PATTERN,
                        title=f"巨震 {amp:.1f}%",
                        extra=f"振幅 {amp:.2f}%（最高 {float(q.high):.2f} / "
                              f"最低 {float(q.low):.2f}），成交额 {amount / 1e8:.2f} 亿",
                        metrics={"amplitude": amp},
                    ))

            # --- 4. 尾盘异动 --------------------------------------------
            if late_win > 0.0 and late_pct > 0.0 and minutes_to_close <= late_minutes:
                chg = ctx.state.price_change(code, late_win, now_epoch)
                chg = None if chg is None else _num(chg, 0.0)
                if chg is not None and abs(chg) >= late_pct:
                    up = chg >= 0.0
                    picked.append(self._mk(
                        q, ctx, ts, bucket, "late_surge", _SEV_PATTERN,
                        title=f"尾盘{'急拉' if up else '跳水'} {chg:+.1f}%",
                        extra=f"距收盘 {minutes_to_close:.0f} 分钟，"
                              f"近 {late_win / 60.0:.0f} 分钟{'急拉' if up else '跳水'} {chg:+.2f}%",
                        metrics={"late_change_pct": chg,
                                 "minutes_to_close": minutes_to_close},
                    ))

            # --- 5. 快速回封（无历史则不报） -----------------------------
            alert = self._check_reseal(
                q, ctx, ts, bucket, now_epoch, reseal_seconds, reseal_tol, pullback,
            )
            if alert is not None:
                picked.append(alert)

        # severity 降序 -> |pct| 降序 -> code 升序（保证确定性）
        picked.sort(key=lambda t: (-t[0], -t[1], t[2].code))
        if max_per_round > 0:
            picked = picked[:max_per_round]
        return [a for _, _, a in picked]

    # ------------------------------------------------------------------
    def _check_reseal(
        self,
        q,
        ctx: RuleContext,
        ts,
        bucket: int,
        now_epoch: float,
        reseal_seconds: float,
        tol: float,
        pullback_pct: float,
    ) -> tuple[int, float, Alert] | None:
        """快速回封：曾涨停 → 打开 >0.5% → 现在又封回。历史不足则返回 None。"""
        if reseal_seconds <= 0.0:
            return None
        limit = float(q.limit_up_price)
        if limit <= 0.0:
            return None
        tol = max(tol, 1e-9)
        # 先判「现在是否在涨停价上」，绝大多数股票在这一步就被排除（性能关键）
        if float(q.price) < limit - tol:
            return None

        hist = getattr(ctx.state, "history", None)
        if not hist:
            return None
        rows = hist.get(q.code)
        if not rows:
            return None

        start = now_epoch - reseal_seconds
        # 状态机：未封板 -> 曾封板 -> 跌离涨停（炸板）。一旦有炸板证据即保留。
        sealed_at = None           # 最近一次「曾涨停」的时间
        pulled_back = False        # 该次封板之后是否跌离涨停超过阈值
        floor = limit * (1.0 - pullback_pct / 100.0)
        # 窗口内最低价。文案说的是「近 N 分钟内」，所以这里必须只统计窗口内的点 ——
        # 旧代码图省事直接用 q.low（**当日**最低），于是"近 10 分钟内最低 10.30"
        # 里的 10.30 可能出现在几小时前，回落幅度被夸大十几倍
        # （实测：窗口内真实最低 10.94 -> 0.55%，却印成当日口径的 6.36%）。
        win_low = 0.0
        for point in rows:
            try:
                t = float(point[0])
                p = float(point[1])
            except (TypeError, ValueError, IndexError):
                continue
            if t < start or t > now_epoch or p <= 0.0:
                continue
            if win_low <= 0.0 or p < win_low:
                win_low = p
            if sealed_at is None:
                if p >= limit - tol:
                    sealed_at = t       # 窗口内第一次触及涨停
                continue
            if t <= sealed_at:
                continue
            if p <= floor:
                pulled_back = True      # 曾跌离涨停 -> 有「炸板」证据（不再撤销）
            elif p >= limit - tol:
                sealed_at = t           # 封得更近的一次，仅刷新时间
        if sealed_at is None or not pulled_back:
            return None

        # 窗口内一个有效点都没有时（理论上不会走到这里，因为 pulled_back
        # 需要窗口内的点）退回当日最低，保证不会算出 0 元这种数。
        if win_low <= 0.0:
            win_low = float(q.low)

        gap = (limit - win_low) / limit * 100.0
        return self._mk(
            q, ctx, ts, bucket, "reseal", _SEV_RESEAL,
            title="快速回封",
            extra=f"近 {reseal_seconds / 60.0:.0f} 分钟内炸板回落（最低 {win_low:.2f} 元，"
                  f"较涨停价 {gap:.2f}%）后重新封上涨停 {limit:.2f} 元",
            # metrics 里同时保留两个口径，避免下游把"窗口最低"误当"当日最低"
            metrics={"limit_up_price": limit, "low": win_low,
                     "day_low": float(q.low),
                     "reseal_window_seconds": float(reseal_seconds)},
        )

    # ------------------------------------------------------------------
    def _mk(
        self,
        q,
        ctx: RuleContext,
        ts,
        bucket: int,
        pattern: str,
        severity: int,
        *,
        title: str,
        extra: str,
        metrics: dict,
    ) -> tuple[int, float, Alert]:
        """构造 (severity, |pct|, Alert) 三元组，供排序后截断。"""
        pct = float(q.pct)
        vwap = float(q.vwap)
        above = bool(q.above_vwap)
        dev = (float(q.price) / vwap - 1.0) * 100.0 if vwap > 0.0 else 0.0
        cn = PATTERN_CN.get(pattern, pattern)
        detail = "\n".join(
            [
                f"{cn} · 现价 {float(q.price):.2f} 元  涨跌幅 {fmt_pct(pct)}",
                extra,
                f"换手 {float(q.turnover):.2f}%  成交额 {float(q.amount) / 1e8:.2f} 亿",
                f"均价 {vwap:.2f} 元 · 现价{'站上' if above else '跌破'}均价 {dev:+.2f}%",
            ]
        )
        m = {
            "pattern": pattern,
            "pct": pct,
            "price": float(q.price),
            "amplitude": float(q.amplitude),
            "amount": float(q.amount),
            "turnover": float(q.turnover),
            "above_vwap": 1.0 if above else 0.0,
        }
        m.update(metrics or {})
        alert = Alert(
            key=f"{q.code}:{_KIND.value}:{pattern}:{bucket}",
            kind=_KIND,
            code=q.code,
            name=q.name,
            ts=ts,
            price=float(q.price),
            pct=pct,
            title=title,
            detail=detail,
            severity=severity,
            # IT-P1-DELIVERY-LEDGER-002：稳定 signal 身份。用**机器语义**的
            # pattern（宽幅震荡/尾盘异动/回封…）而不是 title 文案 ——
            # 文案会改，pattern 是判断依据本身。
            signal_id=f"unusual.{pattern}",
            metrics=m,
            # 尾盘异动/回封这类仍可能连续触发的形态，靠时间距离兜底；
            # 前面三种状态型形态已由 _edge 边沿检测保证只报一次。
            cooldown_key=f"{q.code}:{_KIND.value}:{pattern}",
            cooldown_seconds=self._cooldown,
        )
        return (severity, abs(pct), alert)


def build(cfg: dict | None = None) -> UnusualRule:
    """用配置构造规则（缺省项取 DEFAULTS）。"""
    merged = dict(DEFAULTS)
    for k, v in (cfg or {}).items():
        if v is not None:
            merged[k] = v
    return UnusualRule(cfg=merged)


RULE = build({})
