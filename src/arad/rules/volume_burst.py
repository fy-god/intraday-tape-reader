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

from ..capabilities import ObservationDecision
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
    def _mark_unavailable(self, ctx: RuleContext, code: str, field: str) -> None:
        """记一次「能力缺失导致该项**无法评估**」（**硬阻断**）。

        只做记账，供 live_session / 看板显示"放量规则在 Sina 期间不可评估"，
        而不是让用户以为"这段时间没放量"。没有挂 observation 时静默跳过。

        **WP01 / IT-P1-CAPABILITY-003：这里只记真正阻断的缺失。**
        可以跳过的缺失走 :meth:`_mark_advisory` —— 以前两者共用这一个方法，
        于是"少判一项但仍能命中"被记成"整类不可评估"，污染 health。
        """
        obs = getattr(ctx, "observation", None)
        if obs is None:
            return
        try:
            codes = getattr(obs, "unavailable_codes", None)
            if isinstance(codes, set):
                codes.add(code)          # 按标的去重，不按字段次数
            decisions = getattr(obs, "decisions", None)
            if isinstance(decisions, list):
                decisions.append(ObservationDecision(
                    code=code, status="unavailable_capability",
                    rule=self.name, reason=f"{field}_not_provided",
                    missing=(field,),
                ))
        except Exception:  # noqa: BLE001  可观测性绝不能影响主流程
            pass

    def _mark_advisory(self, ctx: RuleContext, code: str, field: str) -> None:
        """记一次「缺了这一项，但当前语义**允许跳过**」。

        **绝不能**写进 ``unavailable_codes`` —— 那会让
        ``unavailable_capability`` 同时表达两种互斥语义。该票仍可评估，
        只是判据少了一项；明细以 ``advisory_missing`` 状态单独留存。
        """
        obs = getattr(ctx, "observation", None)
        if obs is None:
            return
        try:
            decisions = getattr(obs, "decisions", None)
            if isinstance(decisions, list):
                decisions.append(ObservationDecision(
                    code=code, status="advisory_missing",
                    rule=self.name, reason=f"{field}_not_provided",
                    missing=(field,),
                ))
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    @staticmethod
    def _obs_mark(obs: object, method: str, *args: object) -> None:
        """防御式调用 ``RoundObservationSet`` 的 WP01 记账方法。

        **可观测性绝不能影响主流程** —— 这是本仓既有纪律（见
        ``test_broken_observation_does_not_break_rule``）。账本对象可能是
        旧版本、被替换成坏桩、或永远没挂上，任何一种都只能静默降级，
        不允许把异常抛进信号主链路。
        """
        if obs is None:
            return
        try:
            fn = getattr(obs, method, None)
            if callable(fn):
                fn(*args)
        except Exception:  # noqa: BLE001
            pass

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
        # WP01 / IT-P1-CAPABILITY-003：逐 signal 的可评估性账本。
        #
        # 旧口径只有一个全局 `unavailable_capability`（所有规则写入的 code
        # 并集），消费方拿"本轮有没有任意一个缺失"算轮比例，于是
        # **5000 码里每轮只坏 1 个**也会得到 ratio=1.0 并被解释成
        # "整类规则整场没被评估过"（真实可评估率 99.98%）。分母错了。
        #
        # 更重要的是：本规则对两种缺失的语义**本来就不一样** ——
        #   * 缺 `turnover`：下面 `q.turnover < min_turnover` 会把该票拦掉，
        #     是**硬阻断**（blocking）；
        #   * 缺 `volume_ratio`：`vr > 0.0` 为假 -> 跳过量比门槛，规则
        #     **仍可能命中**，是**可跳过的缺失**（advisory）。
        # 两者共用一个状态名必然污染 health，故在此拆开记账。
        obs = getattr(ctx, "observation", None)
        signal = self.name          # "volume_burst"：到 signal 粒度才有意义
        _mark = self._obs_mark      # 防御式调用：账本坏了绝不能影响规则

        def _no_hit(c: str) -> None:
            """该票判据完整、真的评估过但没到门槛。

            幂等：若同一 (signal, code) 已被记成 blocked，这里不会重复计数
            （``mark_evaluated`` 内部先 ``mark_considered``）。
            """
            _mark(obs, "mark_evaluated", signal, c, False)

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
                _no_hit(code)                               # 纯放量不波动 -> 不报
                continue

            # --- 换手率门槛 -------------------------------------------------
            # IT-P1-CAPABILITY-001：Sina 解析器用 turnover=0.0 表示"本源不提供
            # 该字段"。原来的 `q.turnover < min_turnover` 会把它当真实业务零值，
            # 于是 Sina 服务期间**所有**股票都在这里被判不达标：SourceManager
            # 认为调用成功、health 也正常，用户却只看到放量告警整类消失。
            #
            # 第一阶段只**记账**，不擅自改成"缺字段就跳过门槛"——那会改变误报率
            # （见本轮审计报告 §3.4）。所以下面的门槛判定与改动前逐字一致；
            # 新增的只是把这种情况记成 **blocking** 缺失，使"Sina 期间放量规则
            # 不可评估"从静默变为可见。降级口径等真实数据。
            #
            # IT-P1-EVAL-PUBLISH-001-R1：**记 blocked 就必须真的跳过该票**。
            # 旧代码在这里只记账、**没有 continue**，于是同一票可以既被记成
            # "判不了"(blocked) 又继续往下判并真的发出告警：
            #     considered=1 blocked=1 evaluated_no_hit=0 hit_candidates=0
            #     rule_selected=1            <-- 机械不变量当场被打破
            # 这在**本轮改动前的 HEAD 上同样复现**（既有缺陷，非本次拆分引入），
            # 由真实看板端到端对账暴露：volume_burst 出现 hit=0 而 sel=1 的轮次。
            #
            # 语义上正确的是"跳过"而不是"记账后照报"：turnover 是本规则的
            # **硬依赖**（`BLOCKING_DEPS`），且 Sina 的非零换手率同样是**不可信
            # 占位值**（见 tests/test_capabilities.py::test_capability_is_source_
            # level_not_value_level：同源下改数值不改变可评估性判定）。既然判据
            # 不可信，该票就该整只跳过 —— 这正是 blocked 的含义。
            #
            # 兼容性：真实 Sina 场景本来就送 turnover=0.0 占位，旧代码也会在下面
            # 的数值门槛处 `continue`，所以**告警集合逐字不变**；变的只是
            # "不再出现 blocked 却又发了告警"这种自相矛盾的记账。
            if min_turnover > 0.0 and not ctx.provides("turnover"):
                self._mark_unavailable(ctx, code, "turnover")
                # 缺 turnover 且 min_turnover>0 -> 该票**真的不能判**，整只跳过
                _mark(obs, "mark_blocked", signal, code,
                      "turnover_not_provided", "turnover")
                continue
            if min_turnover > 0.0 and float(q.turnover) < min_turnover:
                # IT-P1-EVAL-PUBLISH-001-R2：真实的业务门槛不达标，该票是
                # **判据完整、评估过但没到门槛** —— 必须与上面 `min_abs_pct` /
                # 下面的 `min_amount`、量比、速度**同口径**记 `_no_hit`。
                # 旧代码在这里直接 `continue`，于是"换手率不够"这一类的票
                # 从账本里**彻底消失**：considered 都不涨（实测 Tencent 真实零
                # 换手率下 considered=0），谁也无法从账本看出它们存在。
                # 其它门槛都记、唯独这一条不记，是单纯的漏记而非设计。
                _no_hit(code)
                continue
            if min_amount > 0.0 and float(q.amount) < min_amount:
                _no_hit(code)
                continue

            # --- 条件 1：量比（数据源缺失时跳过该项） --------------------
            # 同样只加记账：源不提供量比时标记 **advisory**（不是 blocking）——
            # `vr > 0.0` 为假即跳过门槛，规则**仍可能靠速度/金额命中**，
            # 所以该票依旧是可评估的。把这种情况算成"不可评估"正是
            # IT-P1-CAPABILITY-003 指出的语义混淆。
            vr = _num(q.volume_ratio, 0.0)
            if vr_thr > 0.0 and not ctx.provides("volume_ratio"):
                # 注意这里用 _mark_advisory 而不是 _mark_unavailable：
                # 缺量比只是**跳过一个判据**，规则仍可能靠速度/金额命中，
                # 该票依然可评估。记进 unavailable_codes 会把它误报成
                # "整类不可评估"（IT-P1-CAPABILITY-003）。
                self._mark_advisory(ctx, code, "volume_ratio")
                _mark(obs, "mark_advisory_missing", signal, code,
                      "vr_threshold_skipped", "volume_ratio")
            if vr_thr > 0.0 and vr > 0.0 and vr < vr_thr:
                _no_hit(code)
                continue

            # --- 条件 2：速度倍数 ---------------------------------------
            avg_per_min = float(q.volume_lots) / denom_seconds * 60.0
            if avg_per_min <= 0.0:
                _no_hit(code)
                continue                                    # 无均量基准可算 -> 跳过
            delta = 0.0
            if win > 0.0:
                delta = max(_num(state.volume_delta(code, win, now_epoch), 0.0), 0.0)
            recent_per_min = (delta / win * 60.0) if win > 0.0 else 0.0
            ratio = recent_per_min / avg_per_min
            if speed_mult > 0.0 and ratio < speed_mult:
                _no_hit(code)
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
                # 稳定 signal 身份：Engine 在 bus.accept / store 之后按它记账
                # （IT-P1-EVAL-PUBLISH-001）。不填则 Engine 只能猜，会污染账本。
                signal_id=signal,
            )
            picked.append((ratio, alert))
            # 到门槛了 = 候选命中。注意这时还**没有**发出去 ——
            # 下面 max_per_round 会截断，被截掉的绝不能算 published
            # （否则"被限流吞掉"会被读成"已经告警过"）。
            _mark(obs, "mark_evaluated", signal, code, True)

        # 按速度倍数降序（同倍数用代码保证确定性）；max_per_round<=0 视为不限量
        picked.sort(key=lambda t: (-t[0], t[1].code))
        if max_per_round > 0:
            picked = picked[:max_per_round]
        # 只有**真的进了返回列表**的才算 published；被截掉的那些保持
        # hit_candidate 身份，机械不变量 published <= hit_candidates 成立。
        for _ratio, a in picked:
            _mark(obs, "mark_published", signal, a.code)
        return [a for _, a in picked]


def build(cfg: dict | None = None) -> VolumeBurstRule:
    """用配置构造规则（缺省项取 DEFAULTS）。"""
    merged = dict(DEFAULTS)
    for k, v in (cfg or {}).items():
        if v is not None:
            merged[k] = v
    return VolumeBurstRule(cfg=merged)


RULE = build({})
