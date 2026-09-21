"""急拉 / 急跌 检测（系统核心规则）。

判定逻辑
--------
在滑动的短窗口内，价格相对窗口起点快速变动即视为异动：

1. **多窗口扫描**：对 ``windows`` 里每个 ``{seconds, pct}`` 组合计算窗口涨幅，
   任一命中即候选；``plunge_windows`` 同理（阈值为负数）。
2. **方向互斥**：同一只股票同一轮只出**一条**告警，取 ``|实际涨幅| / |阈值|``
   比值最大的窗口作为代表，避免同一波行情刷屏。
3. **量价确认**（可用配置关闭）：
   - 急拉要求站上分时均价，急跌要求跌破均价（过滤诱多/诱空）；
   - 窗口成交量需达到前一等长窗口的 ``volume_confirm_ratio`` 倍；
   - 急拉要求加速度：后半窗口涨幅 ≥ ``accel_ratio`` × 前半窗口涨幅。
4. **分级**：涨幅达到阈值的 ``urgent_multiple`` 倍时 severity 升为 3。
5. **幂等**：key 用时间分桶，配合 ``AlertBus`` 保证冷却期内不重复推送。
"""
from __future__ import annotations

from datetime import datetime

from ..models import Alert, AlertKind, Quote, Snapshot
from ..session import CONTINUOUS
from .base import RuleContext, bucket_of, fmt_pct

__all__ = ["TickSurgeRule", "build", "RULE", "DEFAULT_CFG"]

DEFAULT_CFG: dict = {
    "enabled": True,
    "windows": [
        {"seconds": 60, "pct": 2.0},
        {"seconds": 180, "pct": 3.0},
        {"seconds": 300, "pct": 4.0},
    ],
    "plunge_windows": [
        {"seconds": 60, "pct": -2.0},
        {"seconds": 180, "pct": -3.0},
        {"seconds": 300, "pct": -4.0},
    ],
    "require_above_vwap": True,
    "require_below_vwap": True,
    "volume_confirm_ratio": 1.2,
    "accel_ratio": 1.0,
    "cooldown_seconds": 300,
    "max_per_round": 15,
    "only_continuous": True,
    "severity": 2,
    "urgent_multiple": 2.0,
}


class TickSurgeRule:
    """急拉急跌检测规则。"""

    name = "tick_surge"

    def __init__(self, cfg: dict | None = None):
        merged = dict(DEFAULT_CFG)
        merged.update(cfg or {})
        self.cfg = merged
        self.windows = self._norm_windows(merged.get("windows"), positive=True)
        self.plunge_windows = self._norm_windows(merged.get("plunge_windows"), positive=False)
        self.cooldown = float(merged.get("cooldown_seconds", 300) or 300)
        # 0 = 不限量（与 limit_board 约定一致）。不能用 `or 15`：
        # 那会把用户显式配置的 0 悄悄改回 15，两个规则的语义就打架了。
        _mpr = merged.get("max_per_round", 15)
        self.max_per_round = int(15 if _mpr is None else _mpr)
        self.vol_ratio = float(merged.get("volume_confirm_ratio", 0) or 0)
        self.accel_ratio = float(merged.get("accel_ratio", 0) or 0)
        self.urgent_multiple = float(merged.get("urgent_multiple", 2.0) or 2.0)
        self.severity = int(merged.get("severity", 2) or 2)

    # ------------------------------------------------------------------
    @staticmethod
    def _norm_windows(raw, *, positive: bool) -> list[tuple[float, float]]:
        """把配置规整成 [(seconds, pct)]，并按窗口长度升序（短窗口优先）。"""
        out: list[tuple[float, float]] = []
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            try:
                sec = float(item.get("seconds", 0))
                pct = float(item.get("pct", 0))
            except (TypeError, ValueError):
                continue
            if sec <= 0:
                continue
            if positive:
                if pct <= 0:
                    continue
            else:
                if pct >= 0:
                    continue
            out.append((sec, pct))
        out.sort(key=lambda x: x[0])
        return out

    # ------------------------------------------------------------------
    def evaluate(self, snap: Snapshot, ctx: RuleContext) -> list[Alert]:
        cfg = self.cfg
        if cfg.get("only_continuous", True) and ctx.session not in CONTINUOUS:
            return []

        now = ctx.now or datetime.now()
        now_ep = ctx.now_epoch
        state = ctx.state

        surges: list[tuple[float, Alert]] = []
        plunges: list[tuple[float, Alert]] = []

        for code, q in snap.quotes.items():
            if q is None or q.is_suspended or q.price <= 0:
                continue
            hist = getattr(state, "history", {}).get(code)
            if not hist or len(hist) < 2:
                continue

            up = self._scan(q, ctx, self.windows, now_ep, rising=True)
            if up is not None:
                surges.append(up)
                continue
            down = self._scan(q, ctx, self.plunge_windows, now_ep, rising=False)
            if down is not None:
                plunges.append(down)

        surges.sort(key=lambda t: -t[0])
        plunges.sort(key=lambda t: -t[0])

        out: list[Alert] = []
        out.extend(a for _, a in surges[: max(self.max_per_round, 1)])
        out.extend(a for _, a in plunges[: max(self.max_per_round, 1)])
        return out

    # ------------------------------------------------------------------
    def _scan(self, q: Quote, ctx: RuleContext, windows, now_ep: float,
              *, rising: bool) -> tuple[float, Alert] | None:
        """在给定窗口集合里找最佳命中。返回 (强度比值, Alert) 或 None。"""
        state = ctx.state
        best: tuple[float, Alert] | None = None

        for seconds, threshold in windows:
            # 历史必须基本覆盖该窗口，否则会把"仅有的 1 分钟数据"当成 5 分钟急拉
            if not self._covered(ctx, q.code, seconds, now_ep):
                continue
            change = state.price_change(q.code, seconds, now_ep)
            if change is None:
                continue
            # 方向与阈值校验
            if rising:
                if change < threshold:
                    continue
            else:
                if change > threshold:
                    continue

            # --- 量价确认 ---------------------------------------------
            vol_ratio = self._volume_ratio(ctx, q.code, seconds, now_ep)
            if self.vol_ratio > 0 and vol_ratio is not None and vol_ratio < self.vol_ratio:
                continue
            if rising:
                if self.cfg.get("require_above_vwap", True) and not self._above_vwap(q):
                    continue
            else:
                if self.cfg.get("require_below_vwap", True) and self._above_vwap(q):
                    continue

            # --- 加速度（仅急拉） -------------------------------------
            if rising and self.accel_ratio > 0:
                half = state.price_change(q.code, seconds / 2.0, now_ep)
                first_half = self._first_half_change(ctx, q.code, seconds, now_ep)
                if first_half is not None and first_half > 0 and half is not None:
                    # half = 后半段涨幅；first_half = 前半段涨幅
                    if half < self.accel_ratio * first_half:
                        continue

            strength = abs(change) / abs(threshold) if threshold else 0.0
            if best is not None and strength <= best[0]:
                continue

            kind = AlertKind.SURGE if rising else AlertKind.PLUNGE
            best = (strength, self._make_alert(q, ctx, seconds, change, kind, vol_ratio))

        return best

    # ------------------------------------------------------------------
    @staticmethod
    def _covered(ctx: RuleContext, code: str, seconds: float, now_ep: float,
                 tolerance: float = 0.9) -> bool:
        """历史是否基本覆盖整个窗口。

        没有这个校验，开盘前几分钟只有 1 分钟数据时，会把这点涨幅当成
        "5 分钟急拉"——窗口越大越容易误报。允许 10% 的采样相位偏差。
        """
        hist = getattr(ctx.state, "history", {}).get(code)
        if not hist:
            return False
        first_ts = hist[0][0]
        return first_ts <= now_ep - seconds * tolerance

    def _above_vwap(self, q: Quote) -> bool:
        if q.volume_lots <= 0 or q.amount <= 0:
            return False
        return q.price >= q.vwap

    def _volume_ratio(self, ctx: RuleContext, code: str, seconds: float,
                      now_ep: float) -> float | None:
        """窗口量能 / 前一等长窗口量能。前一窗口为 0 时返回 None（放行）。"""
        state = ctx.state
        cur = state.volume_delta(code, seconds, now_ep)
        total = state.volume_delta(code, seconds * 2.0, now_ep)
        prev = max(0.0, total - cur)
        if prev <= 0:
            return None
        return cur / prev

    def _first_half_change(self, ctx: RuleContext, code: str, seconds: float,
                           now_ep: float) -> float | None:
        """前半段（更早的那一半）窗口的涨幅。"""
        state = ctx.state
        pts = state.window(code, seconds, now_ep)
        if len(pts) < 2:
            return None
        cutoff = now_ep - seconds / 2.0
        first_half = [p for p in pts if p[0] <= cutoff]
        if len(first_half) < 2:
            return None
        a, b = first_half[0][1], first_half[-1][1]
        if a <= 0:
            return None
        return (b / a - 1.0) * 100.0

    # ------------------------------------------------------------------
    def _make_alert(self, q: Quote, ctx: RuleContext, seconds: float,
                    change: float, kind: AlertKind, vol_ratio: float | None) -> Alert:
        now = ctx.now or datetime.now()
        now_ep = ctx.now_epoch
        bucket = bucket_of(now_ep, self.cooldown)
        win_txt = self._fmt_seconds(seconds)
        verb = "急拉" if kind is AlertKind.SURGE else "急跌"

        severity = self.severity
        threshold = next((p for s, p in
                          (self.windows if kind is AlertKind.SURGE else self.plunge_windows)
                          if abs(s - seconds) < 1e-6), None)
        if threshold and abs(change) >= abs(threshold) * self.urgent_multiple:
            severity = 3

        # 距限价：方向必须跟着**告警方向**走。
        # 急拉看"离涨停还有多远"（+ 号读作"还能涨多少"）；
        # 急跌看"离跌停还有多远"。旧代码只算涨停口径、且两个方向共用同一行
        # detail，于是急跌告警里印着「距涨停 +13.40%」——
        # 一条 kind=plunge、标题为负的告警带着一个「+」号，
        # 而用户真正需要的「距跌停」在整条告警里根本不存在。
        #
        # 仅在对应限价可用时才输出：拿不到真实限价（如指数、缺字段）时
        # **不打印**这个字段，而不是打一个 0.00% 让用户误以为"贴着了"。
        to_limit = None
        if q.price > 0:
            if kind is AlertKind.SURGE and q.limit_up_price > 0:
                to_limit = (q.limit_up_price / q.price - 1.0) * 100.0
            elif kind is AlertKind.PLUNGE and q.limit_down_price > 0:
                to_limit = (q.price / q.limit_down_price - 1.0) * 100.0
        limit_label = "距涨停" if kind is AlertKind.SURGE else "距跌停"

        amount_yi = q.amount / 1e8
        title = f"{verb} {fmt_pct(change)} / {win_txt}"
        vwap_txt = "上方" if self._above_vwap(q) else "下方"
        ratio_txt = "数据不足" if vol_ratio is None else f"{vol_ratio:.2f}倍"
        # 拿不到限价时整段省略（连同标签），免得出现「距跌停  最高 …」这种空值
        limit_txt = (f"{limit_label} {to_limit:+.2f}%  " if to_limit is not None
                     else "")
        detail = "\n".join([
            f"现价 {q.price:.2f}  涨跌 {fmt_pct(q.pct)}  窗口 {win_txt} {fmt_pct(change)}",
            f"分时均价 {q.vwap:.2f}（现价在其{vwap_txt}）  量能比 {ratio_txt}",
            f"振幅 {q.amplitude:.2f}%  换手 {q.turnover:.2f}%  成交额 {amount_yi:.2f}亿",
            f"{limit_txt}最高 {q.high:.2f}  最低 {q.low:.2f}  开盘 {q.open:.2f}",
        ])

        return Alert(
            key=f"{q.code}:{kind.value}:{bucket}",
            kind=kind, code=q.code, name=q.name, ts=now,
            price=q.price, pct=q.pct, title=title, detail=detail,
            severity=severity,
            # IT-P1-DELIVERY-LEDGER-002：稳定 signal 身份。急拉/急跌是本系统的
            # 核心诉求，必须分开记账 —— 二者预测力不同，混在一起算交付率
            # 与命中率都没有意义。kind 已是精确枚举，直接映射，不猜文案。
            signal_id=("tick_surge.surge" if kind is AlertKind.SURGE
                       else "tick_surge.plunge"),
            # 不只靠 key 里的时间桶：桶边界在固定墙上时钟网格上，跨桶只差 1 秒，
            # 实测会出现两条急拉只隔 11 秒。带上冷却键+秒数才是真的"隔 N 秒"。
            cooldown_key=f"{q.code}:{kind.value}",
            cooldown_seconds=self.cooldown,
            metrics={
                "window_pct": round(change, 3),
                "window_seconds": float(seconds),
                "pct": round(q.pct, 3),
                "price": round(q.price, 3),
                "vwap": round(q.vwap, 3),
                "vol_ratio": round(vol_ratio, 3) if vol_ratio is not None else -1.0,
                "amount": round(q.amount, 0),
                "turnover": round(q.turnover, 2),
                "amplitude": round(q.amplitude, 2),
                # to_limit_pct 现在是**跟随告警方向**的"距限价"：
                # 急拉 = 距涨停，急跌 = 距跌停。旧代码无论方向都只写涨停口径，
                # 于是急跌告警的 metrics 里塞着一个跌停方向根本用不到的涨停距离。
                # 沿用同一个键名（值是"距限价"而非"距涨停"），方向由 Alert.kind
                # 决定 —— 不再额外加一个 label 键，那只是把 kind 抄一遍。
                # 拿不到限价时为 -1.0（与 vol_ratio 的"数据不足"约定一致）。
                "to_limit_pct": round(to_limit, 2) if to_limit is not None else -1.0,
                "hits": 1.0,
            },
        )

    @staticmethod
    def _fmt_seconds(seconds: float) -> str:
        if seconds < 60:
            return f"{int(seconds)}秒"
        if seconds % 60 == 0:
            return f"{int(seconds // 60)}分钟"
        return f"{seconds / 60:.1f}分钟"

    # ------------------------------------------------------------------
    def evaluate_with_hits(self, snap: Snapshot, ctx: RuleContext) -> list[Alert]:
        """同 evaluate，但在 metrics 里附带全部命中窗口（供前端/复盘使用）。"""
        alerts = self.evaluate(snap, ctx)
        for a in alerts:
            if a.metrics.get("hits", 1) == 1:
                hits = self._collect_hits(a, snap, ctx)
                if hits:
                    a.metrics["hits"] = float(len(hits))
                    a.metrics["hit_windows"] = hits  # type: ignore[assignment]
        return alerts

    def _collect_hits(self, alert: Alert, snap: Snapshot, ctx: RuleContext) -> list[float]:
        q = snap.get(alert.code)
        if q is None:
            return []
        rising = alert.kind is AlertKind.SURGE
        wins = self.windows if rising else self.plunge_windows
        out: list[float] = []
        for seconds, threshold in wins:
            ch = ctx.state.price_change(q.code, seconds, ctx.now_epoch)
            if ch is None:
                continue
            if (rising and ch >= threshold) or ((not rising) and ch <= threshold):
                out.append(float(seconds))
        return out

    # ------------------------------------------------------------------
    def __call__(self, snap: Snapshot, ctx: RuleContext) -> list[Alert]:
        return self.evaluate(snap, ctx)


def build(cfg: dict | None = None) -> TickSurgeRule:
    return TickSurgeRule(cfg)


RULE = build({})
