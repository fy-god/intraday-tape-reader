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


def at_limit_price(q: Quote, limit: float, rising: bool, tol: float) -> bool:
    """现价是否还在限价上（含容差）。方向按涨/跌停镜像。

    抽成模块级函数是因为 ``_check_side`` 和 ``_check_break`` 都要用，
    而两处各写一遍方向判断极易把 ``>=`` / ``<=`` 写反 ——
    方向写反会让跌停侧拿"高于跌停价"当"在跌停价上"，判据完全失效。
    """
    if limit <= 0 or q.price <= 0:
        return False
    return q.price >= limit - tol if rising else q.price <= limit + tol

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
        # 状态机记忆：key = f"{code}:{'up'|'down'}"
        #   -> away | at_limit_unqualified | sealed | broken | near。
        # 只在**状态跃迁**时告警：封板/触板/炸板都是事件，不是可以每轮重播的状态。
        # 注意 ``at_limit_unqualified``（价格贴限价但封单不足）必须与 ``sealed``
        # 分开：两者都"在限价上"，但只有后者才是"已封板"。
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
        #: 与 ``out`` 平行：每条告警对应 (tag, 调用前的状态)。
        #:
        #: IT-P2-LIMIT-FIRST-BOARD-MULTI：``_check_side`` 会**顺带写状态**，
        #: 而 ``max_per_round`` 的截断发生在**之后**。于是被截掉的那条告警
        #: 状态已经写成 ``sealed``，下一轮命中 ``prev == "sealed"`` 直接
        #: ``return None`` —— 告警**永久丢失**，不是"下轮再报"。
        #: 实测：3 只票同轮首达、``max_per_round=2`` 时第 3 只
        #: （``600003``）第 2/3 轮都不报，永久消失。
        #:
        #: 修法：记下每条告警写入前的状态，**被截断的那些回滚状态** ——
        #: 下一轮它们仍是"未报过的跃迁"，于是能正常补报。
        #: 只能对"本轮真正没发出去"的告警回滚：发出去了就必须保留状态，
        #: 否则下一轮会重复报同一条。
        meta: list[tuple[str, str]] = []

        for code, q in snap.quotes.items():
            if q is None or q.is_suspended or q.price <= 0 or q.prev_close <= 0:
                continue

            up = self._limit_up_price(q)
            down = self._limit_down_price(q)
            if up <= 0 and down <= 0:
                continue

            # 天地板：同一只股票可能同时命中"曾涨停后炸板"与"现封跌停"，
            # 两个方向是独立事件，都要报，不能先到先得互相遮蔽。
            tag_up = f"{code}:up"
            prev_up = self._state.get(tag_up, "away")
            up_alert = self._check_side(q, ctx, now, now_ep, up=up, down=down, rising=True)
            if up_alert is not None:
                out.append(up_alert)
                meta.append((tag_up, prev_up))
            if self.cfg.get("detect_limit_down", True) and down > 0:
                tag_dn = f"{code}:down"
                prev_dn = self._state.get(tag_dn, "away")
                down_alert = self._check_side(q, ctx, now, now_ep, up=up, down=down, rising=False)
                if down_alert is not None:
                    out.append(down_alert)
                    meta.append((tag_dn, prev_dn))

        # 排序 + 截断时必须让 meta 跟着一起走，否则回滚会张冠李戴。
        order = sorted(range(len(out)),
                       key=lambda i: (-out[i].severity, -abs(out[i].pct)))
        out = [out[i] for i in order]
        meta = [meta[i] for i in order]

        if self.max_per_round > 0 and len(out) > self.max_per_round:
            keep = self.max_per_round
            # 回滚**被截掉**那些的状态：它们本轮没发出去，不该留下"已报过"的记忆。
            for tag, prev in meta[keep:]:
                if prev == "away":
                    self._state.pop(tag, None)
                else:
                    self._state[tag] = prev
            out = out[:keep]
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

            away ──触限价──► at_limit_unqualified ──封单达标──► sealed ──跌离──► broken
              ▲                      │                          │              │
              │                      └──────回落──────┐         │              │
              └──────────回落─────────── near ◄────────┴─────────┘──────────────┘

        「在限价上」与「封单达标」是两件事：``at_limit_unqualified`` 表示价格已经
        贴在限价上，但封单额还没到 ``min_seal_amount_wan`` 门槛 —— 此时**不能**
        记成 ``sealed``。否则封单首次跨过门槛那一轮会因为 ``prev == "sealed"``
        直接 ``return None``，首次达标被永久吞掉（IT-P1-LIMIT-001）。
        封单回落跌破门槛会退回 ``at_limit_unqualified``，再次达标即可重新告警。
        """
        limit = up if rising else down
        if limit <= 0:
            return None
        kind = AlertKind.LIMIT_UP if rising else AlertKind.LIMIT_DOWN
        at_limit = at_limit_price(q, limit, rising, self.tol)

        tag = f"{q.code}:{'up' if rising else 'down'}"
        prev = self._state.get(tag, "away")

        if at_limit:
            # ⚠ 顺序至关重要：**先**判封单是否达标，**再**决定写哪个状态。
            #
            # 过去这里无条件写 ``self._state[tag] = "sealed"``，而封单门槛是在
            # ``_make_seal`` 里才检查的。于是"价格贴板但封单只有 100 万（门槛
            # 200 万）"的那一轮也留下了 ``sealed`` 记忆，下一轮封单涨到 300 万
            # 时命中 ``prev == "sealed"`` 直接 ``return None`` ——
            # **首次达标被永久吞掉，永远不发告警**（IT-P1-LIMIT-001，实测两轮
            # 均为 ``[None, None]``，应为 ``[None, Alert]``）。
            #
            # 现在两种情形分开记：封单不足记 ``at_limit_unqualified``（不是
            # "已封板"），达标才记 ``sealed``。这样既保住了"连续封板只报一次"
            # 的幂等语义，又让"首次跨过门槛"成为一次真正的状态跃迁。
            qualified = self._seal_qualified(q, rising)
            if prev == "sealed" and qualified:
                return None                      # 一直在有效封板：状态未变，不重复报
            if not qualified:
                # 价格在限价上但封单不足：既不是 sealed 也不重复报触板，
                # 但状态必须从 sealed/broken/near 退回，封单补上来时才有跃迁可报。
                # 注意这**不是**"已封板"，所以不能写 sealed —— 那正是本缺陷的根因。
                self._state[tag] = "at_limit_unqualified"
                return None
            self._state[tag] = "sealed"
            return (self._make_seal(q, ctx, now, now_ep, limit, kind, rising)
                    if self.cfg.get("detect_seal", True) else None)

        # 未在限价：判断是否曾触及（触板 / 炸板）
        #
        # 两个方向都要走完整状态机。跌停侧此前在这里直接 ``return None``，
        # 于是 ``limit_down_touch``（触及跌停）与 ``open_limit_down``（打开跌停）
        # 这两个已在注册表里、且带 hint 的信号**永远不可能出现**，而看板
        # 仍宣称「涨跌停 11 个信号」。用户排查"为什么从没见过『打开跌停』"
        # 会怀疑自己的配置或数据源 —— 实际是这里被短路了。
        # 而 ``_make_touch`` 本来就已经支持 rising=False（会用 limit_down_touch
        # 和"触及跌停"文案），所以这更像是漏做而非有意不做。
        #
        # 注：跌停侧"打开"的语义与涨停侧相反 —— 涨停打开是利空（封单被砸开），
        # 跌停打开是利好（封单被撬开，有资金接）。方向色由 spirit.py 的
        # direction 字段负责，这里只管识别。
        want_break = bool(self.cfg.get("detect_break", True))
        want_touch = bool(self.cfg.get("detect_touch", True))
        if not want_break and not want_touch:
            self._state[tag] = "away"
            return None
        if not self._touched_recently(q, ctx, now_ep, limit, rising):
            self._state[tag] = "away"
            return None

        # 离开限价的幅度：涨停侧看"回落多少"，跌停侧看"回升多少"。
        # 走到这里必然已离开限价（上面的 at_limit 分支不成立），
        # 所以这个值一定 > 0，不需要再夹紧。
        retreat = (limit - q.price) / limit * 100.0 if rising \
            else (q.price - limit) / limit * 100.0
        if want_break and retreat >= self.break_retreat:
            if prev == "broken":
                return None                      # 已报过炸板且仍在回落状态：不重播
            self._state[tag] = "broken"
            return self._check_break(q, ctx, now, now_ep, limit, rising)

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

    def _seal_qualified(self, q: Quote, rising: bool, seal_wan: float | None = None) -> bool:
        """封单额是否达到 ``min_seal_amount_wan`` 门槛（0 表示不设门槛）。

        抽成独立方法是因为 ``_check_side`` 必须在**写状态之前**知道封单是否达标，
        而 ``_make_seal`` 内部也判同一个门槛。两处各写一遍极易判据漂移
        （例如一边用 ``<=`` 一边用 ``<``），所以门槛只有一个来源。

        判据与 ``_make_seal`` 完全一致：**小于**门槛才算不达标，等于门槛放行。
        ``seal_wan`` 可传入已算好的封单额，避免重复计算。
        """
        if self.min_seal_wan <= 0:
            return True
        amount = self._seal_amount_wan(q, rising) if seal_wan is None else seal_wan
        return amount >= self.min_seal_wan

    def _is_one_word(self, q: Quote, limit: float, rising: bool) -> bool:
        """一字板：开盘即限价，且全天没有价格波动（high == low）。

        两个方向都要判。跌停侧此前直接 ``return False``，于是 ``_make_seal``
        必然把一字跌停印成「非一字」—— 而同一条 detail 的下一行就写着
        「开盘 9.00（-10.00%）最高 9.00 最低 9.00 振幅 0.00%」，
        用户在两行之间读到的是自相矛盾的话。
        对做短线的人来说「一字跌停」（想卖卖不掉）与「盘中跌停」差别很大，
        不该因为"跌停侧懒得判"就把这个信息丢掉。
        """
        if q.open <= 0 or abs(q.high - q.low) >= 1e-9:
            return False
        if rising:
            return q.open >= limit - self.tol
        return q.open <= limit + self.tol

    def _is_first_board(self, q: Quote, limit: float, rising: bool) -> bool:
        """首板 = 开盘未涨停（非一字板）。

        只对涨停侧有意义：「首板」指连续涨停序列里的第一个板，
        跌停侧没有对应概念，因此仍然恒为 ``False``（调用方据此不输出该文案）。
        """
        if not rising:
            return False
        return not (q.open > 0 and q.open >= limit - self.tol)

    def _make_seal(self, q: Quote, ctx: RuleContext, now: datetime, now_ep: float,
                   limit: float, kind: AlertKind, rising: bool) -> Alert | None:
        seal_wan = self._seal_amount_wan(q, rising)
        # 门槛的唯一来源是 ``_seal_qualified``。这里保留同一道校验作为兜底：
        # ``_make_seal`` 也可能被直接调用，绝不能凭封单不足的行情造出封板事件。
        if not self._seal_qualified(q, rising, seal_wan):
            return None

        noun = "封涨停" if rising else "封跌停"
        seal_txt = f"封单{seal_wan / 10000:.2f}亿" if seal_wan >= 10000 else f"封单{seal_wan:.0f}万"
        one_word = self._is_one_word(q, limit, rising)
        first_board = self._is_first_board(q, limit, rising)
        # ⚠ 标签必须**只在真的判过**时才输出。跌停侧不判首板（没有这个概念），
        # 所以它既不能显示"首板"，也不能显示"非一字"—— 后者是在断言一件
        # 根本没检查过的事（一字跌停曾被印成「非一字」）。
        # 对做短线的人来说「一字跌停」（想卖卖不掉）与「盘中跌停」差别很大，
        # 宁可少一个标签，也不要一个错的标签。
        if rising:
            board_txt = "一字板" if one_word else ("首板" if first_board else "非一字")
        else:
            board_txt = "一字跌停" if one_word else ""

        bucket = bucket_of(now_ep, self.cooldown)
        # board_txt 可能为空（跌停侧非一字时不输出标签），拼进去会留下两个
        # 连续空格。用 filter 去掉空段，保证不会出现「封单2700万    换手」这种。
        parts = [seal_txt, board_txt, f"换手 {q.turnover:.2f}%",
                 f"成交额 {q.amount / 1e8:.2f}亿"]
        detail = "\n".join([
            f"现价 {q.price:.2f}  涨跌 {fmt_pct(q.pct)}  {'涨停价' if rising else '跌停价'} {limit:.2f}",
            "  ".join(p for p in parts if p),
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
                     now_ep: float, limit: float, rising: bool = True) -> Alert | None:
        """开板：曾封/触限价，现已明显离开。

        涨停侧叫「炸板」（封单被砸开，利空），跌停侧叫「撬板/打开跌停」
        （封单被撬开，有资金接，利好）。两侧的判据是镜像的：

        * 涨停侧：现价须**跌离**涨停价超过 ``break_retreat_pct``；
        * 跌停侧：现价须**涨离**跌停价超过 ``break_retreat_pct``。

        幂等由 ``_check_side`` 的状态机保证（``broken`` 状态不重复报），
        本方法只负责判定与构造告警。
        """
        if not self._touched_recently(q, ctx, now_ep, limit, rising):
            return None
        # 必须已明显离开限价（方向按涨/跌停镜像）
        moved = (limit - q.price) if rising else (q.price - limit)
        if moved < limit * (self.break_retreat / 100.0):
            return None
        # 一字板不开板不算开板（价仍在限价）
        if at_limit_price(q, limit, rising, self.tol):
            return None

        retreat = moved / limit * 100.0
        kind = AlertKind.LIMIT_UP if rising else AlertKind.LIMIT_DOWN
        bucket = bucket_of(now_ep, self.break_bucket)
        # 板已经开了，此时**不存在封单**：留在买一/卖一上的只是普通挂单。
        # 旧文案一律写「买一封单」，在炸板场景下是错的（板都开了哪来的封单）。
        # 按方向取正确的一侧，并改称"买一挂单/卖一挂单"。
        if rising:
            side_txt = "买一挂单"
            bid1_wan = q.bid_vol * 100.0 * q.price / 10000.0
            limit_txt = f"涨停价 {limit:.2f}"
            extra = f"回落 {retreat:.2f}%"
            title = f"炸板 -{retreat:.2f}%"
        else:
            side_txt = "卖一挂单"
            bid1_wan = q.ask_vol * 100.0 * q.price / 10000.0
            limit_txt = f"跌停价 {limit:.2f}"
            extra = f"回升 {retreat:.2f}%"
            title = f"打开跌停 +{retreat:.2f}%"
        detail = "\n".join([
            f"现价 {q.price:.2f}  涨跌 {fmt_pct(q.pct)}  {limit_txt}  {extra}",
            f"最高 {q.high:.2f}  最低 {q.low:.2f}  振幅 {q.amplitude:.2f}%",
            f"换手 {q.turnover:.2f}%  成交额 {q.amount / 1e8:.2f}亿  {side_txt} {bid1_wan:.0f}万",
        ])
        return Alert(
            key=f"{q.code}:{kind.value}:break:{bucket}",
            kind=kind, code=q.code, name=q.name, ts=now, price=q.price, pct=q.pct,
            title=title, detail=detail, severity=3,
            metrics={
                "price": round(q.price, 3), "pct": round(q.pct, 3),
                "limit_up": round(self._limit_up_price(q), 3),
                "limit_down": round(self._limit_down_price(q), 3),
                # 带符号的"距限价"：正 = 现价在限价之上，负 = 在限价之下。
                # 同一个式子对两个方向都成立（涨停侧炸板必为负，跌停侧撬板必为正），
                # 所以不要改成 abs —— 那会丢掉"在上还是在下"这个信息，
                # 而且和既有断言（-4.545）冲突。
                "distance_to_limit_pct": round((q.price / limit - 1.0) * 100.0, 3),
                # ⚠ 这里**不能**写 seal_amount_wan：板已经开了，根本不存在封单，
                # 买一/卖一上那点量只是普通挂单。旧代码复用 _seal_amount_wan()
                # 并把同一个数同时写进 metrics 和文案，于是字面上说"封单"、
                # 数值上也在 metrics 里冒充封单 —— 而 seal_amount_wan 在
                # spirit.to_feed_item 的 extra 白名单里，看板悬停会照原样展示，
                # 用户就被明确告知"这是封单"。故改用语义正确的 bid1_amount_wan。
                # 真封板路径（_make_seal）仍然写 seal_amount_wan，那里措辞是对的。
                "bid1_amount_wan": round(bid1_wan, 1),
                "one_word_board": 0.0,
                "is_first_board": 1.0 if self._is_first_board(q, limit, rising) else 0.0,
                "touch_count": float(self._touch_count(q, ctx, now_ep, limit, rising)),
                "retreat_pct": round(retreat, 3),
                "stage": 3.0,
                "pattern": "open_limit_up" if rising else "open_limit_down",
                "rising": 1.0 if rising else 0.0,
            },
        )


def build(cfg: dict | None = None) -> LimitBoardRule:
    return LimitBoardRule(cfg)


RULE = build({})
