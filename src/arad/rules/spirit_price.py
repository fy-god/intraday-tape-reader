"""短线精灵 · 价格类信号（火箭发射 / 快速反弹 / 高台跳水 / 加速下跌）。

与 ``tick_surge`` 的分工
-----------------------
``tick_surge`` 回答"**涨得快不快**"——纯幅度判定，60/180/300 秒涨 2/3/4% 就报。
本模块回答"**这波涨跌是什么性质**"，即同花顺短线精灵里那四个有**状态语义**的信号：

============  ============  ==========================================
信号          中文          与"单纯涨跌"的区别
============  ============  ==========================================
``rocket``    火箭发射      必须**创出当日新高**；只涨不创新高不算
``rebound``   快速反弹      必须**先处于下跌状态**再拉起
``dive``      高台跳水      必须**先处于上涨状态**再跳水
``accel_down`` 加速下跌     必须**延续原下跌**且**斜率在变陡**
============  ============  ==========================================

所以这四个信号不是"幅度更大"，而是**形态**不同：一只票全天阴跌中的一次反弹是
``rebound``，而它创新高后回落是 ``dive``。只看幅度的话两者完全一样。

设计取舍
--------
* **默认 ``enabled: false``**：``tick_surge`` 已覆盖"涨得快"的告警，本模块是
  语义增强。同时开启会让同一波行情出两条不同说法的告警，由用户自己权衡。
* **状态判定要证据**：``rebound`` 的"原来在跌"用**窗口内低点显著低于窗口起点**
  来证明，``dive`` 的"原来在涨"用**当日高点较昨收有明显涨幅**来证明。
  证据不足就不报（绝不猜）—— 这是短线精灵最容易变成噪声的地方。
* 四个信号都是**事件型**：每次发生报一次，配合 ``cooldown_key`` 做真实时间距离去重。
"""
from __future__ import annotations

from datetime import datetime

from ..models import Alert, AlertKind, Quote, Snapshot
from ..session import CONTINUOUS
from .base import RuleContext, bucket_of, fmt_pct

__all__ = ["SpiritPriceRule", "build", "RULE", "DEFAULTS", "SIGNALS"]

#: 本模块产出的信号名（会写进 ``Alert.metrics["pattern"]``）。
#:
#: **顺序即优先级**：一只票一轮只出一个价格信号，先命中的胜出。把
#: ``rocket``/``dive`` 排在前、``rebound``/``accel_down`` 排在后，是因为
#: 前两者证据更强（创当日新高 / 自峰值大跌），后两者是"次一级"的形态。
SIGNALS = ("rocket", "dive", "rebound", "accel_down")

DEFAULTS: dict = {
    "enabled": False,
    "only_continuous": True,
    # --- 扫描窗口（多个窗口任一命中即触发）---
    "windows": [60, 180],
    # --- 火箭发射：快速上涨且创当日新高 ---
    # 涨幅门槛（%）；窗口内涨幅达到它才算"快速"
    "rocket_pct": 2.0,
    # 现价距当日最高价多近才算"在新高上"（0.3% 以内）
    "rocket_near_high_pct": 0.3,
    # 当日最高价须高于昨收这么多，避免下跌股的小反弹被当成新高
    "rocket_min_high_pct": 0.0,
    # --- 快速反弹：先跌后拉 ---
    # 自窗口低点拉起的幅度门槛（%）。注意这**不是**窗口涨跌幅：
    # 10.2 -> 9.9 -> 10.1 的窗口涨跌幅是 -1.0%，但确实反弹了。
    "rebound_pct": 2.0,
    # 窗口内必须出现过这么深的跌幅（低点较窗口起点），才承认"原来在跌"
    "rebound_prior_drop_pct": 1.0,
    # 兼容旧配置名（等价于 rebound_pct）
    "rebound_off_low_pct": 2.0,
    # --- 高台跳水：先涨后跌 ---
    # 窗口内峰值较窗口起点须涨这么多，才承认"原来在涨"
    "dive_prior_rise_pct": 2.0,
    # 自峰值须回落这么多（%）
    "dive_off_high_pct": 2.0,
    # --- 加速下跌 ---
    "accel_down_pct": -2.0,
    # 后半段跌幅须达到前半段的这个倍数（斜率变陡）
    "accel_ratio": 1.5,
    # 总跌幅须超过当日振幅的这个比例，避免横盘小波动被当成加速下跌
    "accel_min_range_ratio": 0.4,
    # --- 通用 ---
    "cooldown_seconds": 300,
    "max_per_round": 15,
    "severity": 2,
    "urgent_multiple": 2.0,
}


class SpiritPriceRule:
    """短线精灵价格类信号。"""

    name = "spirit_price"

    def __init__(self, cfg: dict | None = None):
        merged = dict(DEFAULTS)
        merged.update(cfg or {})
        self.cfg = merged
        self.windows = self._norm_windows(merged.get("windows"))
        self.cooldown = float(merged.get("cooldown_seconds", 300) or 300)
        # 0 = 不限量（约定同 limit_board/volume_burst）。不能用 `or 15`，
        # 那会把显式配置的 0 悄悄变成 15，与其它规则行为不一致。
        self.max_per_round = int(merged.get("max_per_round", 15)
                                 if merged.get("max_per_round") is not None else 15)
        self.severity = int(merged.get("severity", 2) or 2)
        self.urgent_multiple = float(merged.get("urgent_multiple", 2.0) or 2.0)
        self._n = {k: float(v) for k, v in merged.items()
                   if isinstance(v, (int, float)) and not isinstance(v, bool)}

    # ------------------------------------------------------------------
    @staticmethod
    def _norm_windows(raw) -> list[float]:
        """窗口规整成升序正数列表；非法项丢弃。"""
        out: list[float] = []
        for item in raw or []:
            try:
                sec = float(item)
            except (TypeError, ValueError):
                continue
            if sec > 0:
                out.append(sec)
        out.sort()
        return out

    def _g(self, key: str, default: float) -> float:
        return self._n.get(key, default)

    # ------------------------------------------------------------------
    def evaluate(self, snap: Snapshot, ctx: RuleContext) -> list[Alert]:
        if not bool(self.cfg.get("enabled", False)):
            return []
        if bool(self.cfg.get("only_continuous", True)) and ctx.session not in CONTINUOUS:
            return []

        now = ctx.now or datetime.now()
        now_ep = ctx.now_epoch
        picked: list[tuple[float, Alert]] = []

        for code, q in snap.quotes.items():
            if q is None or q.is_suspended or q.price <= 0 or q.prev_close <= 0:
                continue
            # 指数不参与个股价格信号（指数代码也会走这条路，需排除）
            if self._is_index(q):
                continue

            for sig in SIGNALS:
                hit = self._check(sig, q, ctx, now, now_ep)
                if hit is not None:
                    picked.append(hit)
                    # 一只票一轮只出一个价格信号：火箭发射与快速反弹、
                    # 高台跳水与加速下跌语义互斥，同时报等于自己跟自己打架
                    break

        picked.sort(key=lambda t: (-t[1].severity, -abs(t[0])))
        if self.max_per_round > 0:
            picked = picked[: self.max_per_round]
        return [a for _, a in picked]

    # ------------------------------------------------------------------
    @staticmethod
    def _is_index(q: Quote) -> bool:
        from ..models import Board
        return q.board is Board.INDEX or q.code.startswith("399")

    def _check(self, sig: str, q: Quote, ctx: RuleContext, now: datetime,
               now_ep: float) -> tuple[float, Alert] | None:
        """按信号分派；返回 (强度, Alert) 或 None。"""
        fn = getattr(self, f"_sig_{sig}", None)
        if fn is None:
            return None
        for seconds in self.windows:
            if not self._covered(ctx, q.code, seconds, now_ep):
                continue
            hit = fn(q, ctx, now, now_ep, seconds)
            if hit is not None:
                return hit
        return None

    # ------------------------------------------------------------------
    # 火箭发射：快速上涨 + 创当日新高
    # ------------------------------------------------------------------
    def _sig_rocket(self, q: Quote, ctx: RuleContext, now: datetime,
                    now_ep: float, seconds: float):
        need = self._g("rocket_pct", 2.0)
        change = ctx.state.price_change(q.code, seconds, now_ep)
        if change is None or change < need:
            return None

        # 关键：必须是**当日新高**。只涨不创新高（还在下跌途中反弹）属于 rebound。
        if q.high <= 0:
            return None
        near = self._g("rocket_near_high_pct", 0.3) / 100.0
        if q.price < q.high * (1.0 - near):
            return None
        min_high = self._g("rocket_min_high_pct", 0.0)
        if min_high > 0 and (q.high / q.prev_close - 1.0) * 100.0 < min_high:
            return None

        return self._mk(q, ctx, now, now_ep, "rocket", "火箭发射",
                        AlertKind.SURGE, seconds, change,
                        f"窗口 {self._fmt_s(seconds)} 涨 {fmt_pct(change)}，"
                        f"创当日新高 {q.high:.2f}")

    # ------------------------------------------------------------------
    # 快速反弹：先跌后拉
    # ------------------------------------------------------------------
    def _sig_rebound(self, q: Quote, ctx: RuleContext, now: datetime,
                     now_ep: float, seconds: float):
        """先跌后拉。

        **不能用窗口涨跌幅判定**：一只票从 10.2 跌到 9.9 再拉回 10.1，
        窗口涨跌幅仍是 **-1.0%**（首点 10.2 -> 现价 10.1），但它确实是一次
        标准的快速反弹。所以这里以"**自窗口低点拉起的幅度**"为准。
        """
        pts = ctx.state.window(q.code, seconds, now_ep)
        if len(pts) < 3:
            return None
        start = pts[0][1]
        if start <= 0:
            return None
        low = min(p[1] for p in pts)
        if low <= 0:
            return None

        # 证据 1：窗口内确实先跌过
        prior_drop = (low / start - 1.0) * 100.0
        if prior_drop > -self._g("rebound_prior_drop_pct", 1.0):
            return None
        # 证据 2：现价确实从低点拉起来了
        off_low = (q.price / low - 1.0) * 100.0
        if off_low < self._g("rebound_pct", 2.0):
            return None

        # 已经创新高的反弹其实就是 rocket，让 rocket 去报，避免同一波两条
        if q.high > 0 and q.price >= q.high * 0.999:
            return None

        change = (q.price / start - 1.0) * 100.0
        return self._mk(q, ctx, now, now_ep, "rebound", "快速反弹",
                        AlertKind.SURGE, seconds, off_low,
                        f"窗口内先跌 {fmt_pct(prior_drop)}（低点 {low:.2f}）"
                        f"后自低点拉 {fmt_pct(off_low)}",
                        window_pct=change)

    # ------------------------------------------------------------------
    # 高台跳水：先涨后跌
    # ------------------------------------------------------------------
    def _sig_dive(self, q: Quote, ctx: RuleContext, now: datetime,
                  now_ep: float, seconds: float):
        """先涨后跌。

        判据锚在**窗口内的峰值**上，而不是窗口首点：高台跳水的本质是
        "从高处掉下来"，即使现价仍高于窗口起点（当天仍上涨）也算跳水。

        与 ``accel_down`` 的**互斥**由"峰值是否显著高于窗口起点"保证：
        - 峰值远高于起点（窗口内先涨）= 跳水；
        - 峰值就在起点（全程下跌）= 加速下跌，不会同时命中。
        """
        pts = ctx.state.window(q.code, seconds, now_ep)
        if len(pts) < 3:
            return None
        start = pts[0][1]
        if start <= 0 or q.price <= 0:
            return None
        peak = max(p[1] for p in pts)
        if peak <= 0:
            return None

        # 证据 1：窗口内确实先涨过
        rise = (peak / start - 1.0) * 100.0
        if rise < self._g("dive_prior_rise_pct", 2.0):
            return None
        # 证据 2：自峰值大幅回落
        fall = (peak / q.price - 1.0) * 100.0
        if fall < self._g("dive_off_high_pct", 2.0):
            return None

        change = (q.price / start - 1.0) * 100.0
        return self._mk(q, ctx, now, now_ep, "dive", "高台跳水",
                        AlertKind.PLUNGE, seconds, -fall,
                        f"窗口内先涨 {fmt_pct(rise)}（高点 {peak:.2f}）"
                        f"后跳水 {fmt_pct(-fall)}",
                        window_pct=change)

    # ------------------------------------------------------------------
    # 加速下跌：延续下跌且斜率变陡
    # ------------------------------------------------------------------
    def _sig_accel_down(self, q: Quote, ctx: RuleContext, now: datetime,
                        now_ep: float, seconds: float):
        need = self._g("accel_down_pct", -2.0)
        change = ctx.state.price_change(q.code, seconds, now_ep)
        if change is None or change > need:
            return None

        pts = ctx.state.window(q.code, seconds, now_ep)
        if len(pts) < 4:
            return None
        # 全程下跌：峰值必须就在起点附近，否则那是"先涨后跌"（跳水）
        start = pts[0][1]
        peak = max(p[1] for p in pts)
        if start <= 0 or (peak / start - 1.0) * 100.0 > 0.5:
            return None

        cutoff = now_ep - seconds / 2.0
        first = [p for p in pts if p[0] <= cutoff]
        if len(first) < 2:
            return None
        # 前半段已经是下跌（"延续原下跌状态"）
        a, b = first[0][1], first[-1][1]
        if a <= 0:
            return None
        first_chg = (b / a - 1.0) * 100.0
        if first_chg >= 0:
            return None
        # 后半段跌得更快（斜率变陡）
        second_chg = change - first_chg
        ratio = self._g("accel_ratio", 1.5)
        if first_chg == 0 or (second_chg / first_chg) < ratio:
            return None
        # 总跌幅在当日振幅里要占相当比重，否则是横盘噪声
        rng = q.high - q.low
        if rng > 0 and abs(q.price - start) / rng < self._g("accel_min_range_ratio", 0.4):
            return None

        return self._mk(q, ctx, now, now_ep, "accel_down", "加速下跌",
                        AlertKind.PLUNGE, seconds, change,
                        f"前半段 {fmt_pct(first_chg)} -> 后半段 {fmt_pct(second_chg)}，"
                        f"跌速加快 {second_chg / first_chg:.1f} 倍",
                        window_pct=change)

    # ------------------------------------------------------------------
    @staticmethod
    def _covered(ctx: RuleContext, code: str, seconds: float, now_ep: float,
                 tolerance: float = 0.9) -> bool:
        """历史是否覆盖整个窗口（否则会把 1 分钟数据当成 3 分钟行情）。"""
        hist = getattr(ctx.state, "history", {}).get(code)
        if not hist:
            return False
        return hist[0][0] <= now_ep - seconds * tolerance

    @staticmethod
    def _fmt_s(seconds: float) -> str:
        return f"{seconds / 60.0:.0f}分钟" if seconds >= 60 else f"{seconds:.0f}秒"

    def _mk(self, q: Quote, ctx: RuleContext, now: datetime, now_ep: float,
            pattern: str, cn: str, kind: AlertKind, seconds: float,
            change: float, extra: str,
            window_pct: float | None = None) -> tuple[float, Alert]:
        """构造告警。

        ``change`` 是**用于分级的强度指标**（火箭发射/加速下跌用窗口涨跌幅，
        反弹用自低点拉起的幅度、跳水用自峰值回落的幅度），因为这四者中
        后两者的窗口涨跌幅可能很小甚至反向，用它分级会永远评不上"紧急"。
        """
        bucket = bucket_of(now_ep, self.cooldown)
        severity = self.severity
        thr_key = "rocket_pct" if pattern == "rebound" else \
                  ("dive_off_high_pct" if pattern == "dive" else f"{pattern}_pct")
        thr = abs(self._g(thr_key, 2.0))
        if thr > 0 and abs(change) >= thr * self.urgent_multiple:
            severity = 3

        actual = change if window_pct is None else window_pct
        detail = "\n".join([
            f"{cn} · 现价 {q.price:.2f}  涨跌 {fmt_pct(q.pct)}  "
            f"窗口 {self._fmt_s(seconds)} {fmt_pct(actual)}",
            extra,
            f"均价 {q.vwap:.2f}  换手 {q.turnover:.2f}%  成交额 {q.amount / 1e8:.2f}亿",
            f"最高 {q.high:.2f}  最低 {q.low:.2f}  开盘 {q.open:.2f}  振幅 {q.amplitude:.2f}%",
        ])
        alert = Alert(
            key=f"{q.code}:{kind.value}:{pattern}:{bucket}",
            kind=kind, code=q.code, name=q.name, ts=now,
            price=q.price, pct=q.pct,
            title=f"{cn} {fmt_pct(actual)}", detail=detail, severity=severity,
            cooldown_key=f"{q.code}:{kind.value}:{pattern}",
            cooldown_seconds=self.cooldown,
            metrics={
                "pattern": pattern,
                "window_seconds": float(seconds),
                "window_pct": round(actual, 3),
                "signal_pct": round(change, 3),
                "pct": round(q.pct, 3),
                "price": round(q.price, 3),
                "high": round(q.high, 3),
                "low": round(q.low, 3),
                "vwap": round(q.vwap, 3),
                "amplitude": round(q.amplitude, 2),
                "turnover": round(q.turnover, 2),
                "amount": round(q.amount, 0),
            },
        )
        return (abs(change), alert)


def build(cfg: dict | None = None) -> SpiritPriceRule:
    return SpiritPriceRule(cfg)


RULE = SpiritPriceRule()
