"""放量异动规则 —— ``rules/volume_burst.py``（AlertKind.VOLUME_BURST）。

识别「量能脉冲」：某只股票突然以远高于**自身当日节奏**的速度成交，并且价格
同步出现波动 —— 这类异动常常意味着资金抢筹 / 出货 / 消息刺激（而不是温吞的
自然换手）。

判定条件（**全部满足**才告警；某项配置为 0/None 视为关闭，跳过该项）：
  1. 量比 ``quote.volume_ratio >= volume_ratio_threshold``（默认 3.0）。
     数据源没给量比（``== 0``）时**跳过**该项，而不是判负。
  2. 速度倍数（本规则的核心）::

         recent_per_min = state.volume_delta(code, W, now) / W * 60
         avg_per_min    = quote.volume_lots / max(elapsed_trading_seconds, 1) * 60
         ratio          = recent_per_min / avg_per_min  >= speed_multiple（默认 4.0）

     即「近 W 秒的每分钟成交量」是「当日平均每分钟成交量」的多少倍。
  3. 换手率 ``quote.turnover >= min_turnover``（默认 0.5%，小盘股放量才有意义）。
  4. 成交额 ``quote.amount >= min_amount``（默认 3000 万元）。
  5. ``abs(quote.pct) >= min_abs_pct``（默认 0.5%）—— 纯放量不波动不报，
     避免横盘对倒诱多。

severity：默认取 ``cfg["severity"]``（默认 1）；当速度倍数达到门槛的 2 倍以上，
或量比达到门槛的 2 倍以上时升到 2（不会降级配置里更高的 severity）。

key：``f"{code}:volume_burst:{bucket}"``，``bucket = bucket_of(now_epoch, cooldown_seconds)``。

仅使用标准库；``evaluate`` 内不做任何 I/O。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Alert, AlertKind, Snapshot
from ..session import CONTINUOUS
from .base import RuleContext, bucket_of, fmt_pct

__all__ = ["DEFAULTS", "VolumeBurstRule", "build", "RULE"]

DEFAULTS: dict = {
    "enabled": True,
    # 量比门槛
    "volume_ratio_threshold": 3.0,
    # 近 speed_window_seconds 秒的每分钟量 / 当日每分钟均量 的倍数门槛
    "speed_multiple": 4.0,
    "speed_window_seconds": 60.0,
    # 换手率下限（%）
    "min_turnover": 0.5,
    # 当日成交额下限（元）
    "min_amount": 30_000_000.0,
    # 要求价格有波动（%），避免横盘对倒
    "min_abs_pct": 0.5,
    "cooldown_seconds": 600.0,
    "only_continuous": True,
    "severity": 1,
    "max_per_round": 20,
}

_KIND = AlertKind.VOLUME_BURST
# 升档倍数：速度 / 量比 达到门槛的这个倍数即 severity -> 2
_URGENT_MULTIPLE = 2.0


def _num(v, default: float = 0.0) -> float:
    """宽松转 float：None / 非法值 / 空串 -> default。"""
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class VolumeBurstRule:
    """放量异动。``build(cfg)`` 构造，``cfg`` 见本模块 ``DEFAULTS``。"""

    name: str = "volume_burst"
    cfg: dict = field(default_factory=dict)

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

        # 当日已交易秒数：<=0 时无法与「当日平均节奏」比较（盘前/休市/停牌日）
        elapsed = _num(getattr(ctx, "elapsed_trading_seconds", 0.0), 0.0)
        if elapsed <= 0.0:
            return []
        denom_seconds = max(elapsed, 1.0)   # 契约要求 max(elapsed, 1)

        vr_thr = _num(self._get(ctx, "volume_ratio_threshold", 3.0))
        speed_mult = _num(self._get(ctx, "speed_multiple", 4.0))
        win = _num(self._get(ctx, "speed_window_seconds", 60.0))
        min_turnover = _num(self._get(ctx, "min_turnover", 0.5))
        min_amount = _num(self._get(ctx, "min_amount", 30_000_000.0))
        min_abs_pct = _num(self._get(ctx, "min_abs_pct", 0.5))
        cooldown = _num(self._get(ctx, "cooldown_seconds", 600.0))
        base_sev = int(_num(self._get(ctx, "severity", 1), 1.0))
        max_per_round = int(_num(self._get(ctx, "max_per_round", 20), 20.0))

        state = ctx.state
        now_epoch = float(ctx.now_epoch)
        ts = ctx.now or snap.ts
        bucket = bucket_of(now_epoch, cooldown)

        picked: list[tuple[float, Alert]] = []
        for code, q in snap.quotes.items():
            if q is None:
                continue
            # --- 边界：停牌 / 昨收异常 / 无价 ---------------------------
            if q.price <= 0.0 or q.prev_close <= 0.0 or q.volume_lots <= 0.0:
                continue
            if q.is_suspended:
                continue

            pct = float(q.pct)
            if min_abs_pct > 0.0 and abs(pct) < min_abs_pct:
                continue                                    # 纯放量不波动 -> 不报
            if min_turnover > 0.0 and float(q.turnover) < min_turnover:
                continue
            if min_amount > 0.0 and float(q.amount) < min_amount:
                continue

            # --- 条件 1：量比（数据源缺失时跳过该项） --------------------
            vr = _num(q.volume_ratio, 0.0)
            if vr_thr > 0.0 and vr > 0.0 and vr < vr_thr:
                continue

            # --- 条件 2：速度倍数 ---------------------------------------
            avg_per_min = float(q.volume_lots) / denom_seconds * 60.0
            if avg_per_min <= 0.0:
                continue                                    # 无均量基准可算 -> 跳过
            delta = 0.0
            if win > 0.0:
                delta = max(_num(state.volume_delta(code, win, now_epoch), 0.0), 0.0)
            recent_per_min = (delta / win * 60.0) if win > 0.0 else 0.0
            ratio = recent_per_min / avg_per_min
            if speed_mult > 0.0 and ratio < speed_mult:
                continue

            # --- severity 升档 ------------------------------------------
            sev = base_sev
            if speed_mult > 0.0 and ratio >= _URGENT_MULTIPLE * speed_mult:
                sev = max(sev, 2)
            if vr_thr > 0.0 and vr >= _URGENT_MULTIPLE * vr_thr:
                sev = max(sev, 2)

            # --- 文案 ---------------------------------------------------
            vr_txt = f"{vr:.1f}" if vr > 0.0 else "-"
            title = f"放量异动 量比{vr_txt} 速度{ratio:.1f}倍"

            vwap = float(q.vwap)
            above = bool(q.above_vwap)
            dev = (float(q.price) / vwap - 1.0) * 100.0 if vwap > 0.0 else 0.0
            amount = float(q.amount)
            detail = "\n".join(
                [
                    f"现价 {q.price:.2f} 元  涨跌幅 {fmt_pct(pct)}",
                    f"量比 {vr_txt}  速度 {ratio:.1f} 倍"
                    f"（近 {win:.0f} 秒成交 {delta:,.0f} 手，当日均速 {avg_per_min:,.0f} 手/分）",
                    f"换手 {float(q.turnover):.2f}%  成交额 {amount / 1e8:.2f} 亿"
                    f"  振幅 {float(q.amplitude):.2f}%",
                    f"均价 {vwap:.2f} 元 · 现价{'站上' if above else '跌破'}均价 {dev:+.2f}%",
                ]
            )
            metrics: dict = {
                "volume_ratio": vr,
                "speed_ratio": ratio,
                "turnover": float(q.turnover),
                "amount": amount,
                "pct": pct,
                "price": float(q.price),
                "amplitude": float(q.amplitude),
                "above_vwap": 1.0 if above else 0.0,
            }
            alert = Alert(
                key=f"{code}:{_KIND.value}:{bucket}",
                kind=_KIND,
                code=code,
                name=q.name,
                ts=ts,
                price=float(q.price),
                pct=pct,
                title=title,
                detail=detail,
                severity=sev,
                metrics=metrics,
                # key 里的时间桶跨桶只差 1 秒，必须另加"距上次至少 N 秒"的约束
                cooldown_key=f"{code}:{_KIND.value}",
                cooldown_seconds=cooldown,
            )
            picked.append((ratio, alert))

        # 按速度倍数降序（同倍数用代码保证确定性）；max_per_round<=0 视为不限量
        picked.sort(key=lambda t: (-t[0], t[1].code))
        if max_per_round > 0:
            picked = picked[:max_per_round]
        return [a for _, a in picked]


def build(cfg: dict | None = None) -> VolumeBurstRule:
    """用配置构造规则（缺省项取 DEFAULTS）。"""
    merged = dict(DEFAULTS)
    for k, v in (cfg or {}).items():
        if v is not None:
            merged[k] = v
    return VolumeBurstRule(cfg=merged)


RULE = build({})
