"""短线精灵 · 盘口/委托类信号 —— ``rules/spirit_order.py``（AlertKind.UNUSUAL）。

模仿同花顺 / 大智慧「短线精灵」里与**盘口、委托、大单**有关的那 8 个信号。
本规则只产出 ``AlertKind.UNUSUAL``（不新增枚举成员，避免动冻结的 ``models.py``），
靠 ``metrics["pattern"]`` 与 ``key`` 里的**信号名分段**区分具体是哪一个。

| pattern（信号名） | 中文 | 官方判定条件 | 类型 |
|---|---|---|---|
| ``big_buy`` | 大笔买入 | 外盘成交的换手率 > 0.1% | 事件型 |
| ``big_sell`` | 大笔卖出 | 内盘成交的换手率 > 0.1% | 事件型 |
| ``institution_buy`` | 机构买单 | 买队列出现单档 > 50 万股 / 100 万元 / 流通盘 0.25% | 状态型（边沿） |
| ``institution_sell`` | 机构卖单 | 卖队列出现单档 > 50 万股 / 100 万元 / 流通盘 0.25% | 状态型（边沿） |
| ``institution_eat`` | 机构吃货 | 主动买入成交 > 50 万股 / 100 万元 / 流通盘 0.1% | 事件型 |
| ``institution_vomit`` | 机构吐货 | 主动卖出成交 > 50 万股 / 100 万元 / 流通盘 0.1% | 事件型 |
| ``big_bid_wall`` | 有大买盘 | 五档买盘合计 > 80 万股 / 流通盘 0.8% | 状态型（边沿） |
| ``big_ask_wall`` | 有大卖盘 | 五档卖盘合计 > 80 万股 / 流通盘 0.8% | 状态型（边沿） |

**最关键的诚实说明：本项目只有行情快照，没有逐笔成交明细。**
「大笔买入 / 机构吃货」这类信号在官方口径里是**逐笔**判定的，我们用**相邻两轮快照
的增量**去近似那一个区间的主动买卖，因此：

* 得到的粒度是「**一轮快照区间内的主动买/卖**」，不是严格意义的单笔；
* 区间越长越容易把多笔中小单合并误判成一大笔。所以有 ``max_gap_seconds``
  上限（默认 120 秒）：超过就**整轮跳过**。跨午休（11:30→13:00 之间没有任何快照）
  会把整个下午第一轮前的累积量算成"一笔"，是最典型的误报来源。
* 首轮（没有上一轮快照）**不报**；成交量回退（数据源重置 / 跨日）**不报**。
  宁可漏报，不可猜测。

阈值一律是「**或**」关系（任一满足即触发），满足的是哪一条写进
``metrics["hit_*"]``，便于排查误报。

比例型阈值全部用 ``q.float_shares``（流通股数）；``float_shares <= 0`` 时
**只用绝对量阈值**，不整体跳过 —— 否则自选股降级模式（数据源不给流通市值）
下这 8 个信号会全部失效。

key：``f"{code}:{kind.value}:{pattern}:{bucket}"``，``bucket = bucket_of(now_epoch, cooldown)``。
**``pattern`` 分段不可省**：8 个信号共用一个 AlertKind，缺少分段时同桶内
「大笔买入」会把「机构吃货」等别的信号吞掉（本项目刚修过同类缺陷）。

仅在连续竞价时段工作（``only_continuous``）。``DEFAULTS["enabled"]`` 是
**False**（与 ``engine.RULE_MODULES`` 里 spirit_* 那行的约定一致）：这 8 个信号
依赖快照增量近似单笔，噪声高于既有 4 条规则，建议先在真实行情里观察命中质量；
要启用就在 ``config/settings.yaml`` 的 ``rules.spirit_order`` 节写
``enabled: true``，或 ``build({"enabled": True})``。

仅使用标准库；``evaluate`` 内不做任何 I/O。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..models import Alert, AlertKind, Quote, Snapshot
from ..session import CONTINUOUS
from .base import RuleContext, bucket_of, fmt_pct

__all__ = [
    "DEFAULTS",
    "PATTERNS",
    "PATTERN_CN",
    "RULE",
    "SIGNALS",
    "SpiritOrderRule",
    "build",
]

#: 信号名（稳定标识，写日志/存库/前端筛选用它，不要改）
PATTERNS = (
    "big_buy",
    "big_sell",
    "institution_buy",
    "institution_sell",
    "institution_eat",
    "institution_vomit",
    "big_bid_wall",
    "big_ask_wall",
)

#: 与 ``spirit_price`` / ``spirit_index`` 统一的接口名（``PATTERNS`` 是本模块
#: 的原始命名，两者等价）。工具与测试按 ``SIGNALS`` 遍历三个模块，靠这个别名
#: 才能一视同仁；否则每加一个模块就要在调用处写 if。
SIGNALS = PATTERNS

#: 中文名（用于标题/明细首行，便于人工阅读与日报聚合）
PATTERN_CN: dict[str, str] = {
    "big_buy": "大笔买入",
    "big_sell": "大笔卖出",
    "institution_buy": "机构买单",
    "institution_sell": "机构卖单",
    "institution_eat": "机构吃货",
    "institution_vomit": "机构吐货",
    "big_bid_wall": "有大买盘",
    "big_ask_wall": "有大卖盘",
}

DEFAULTS: dict = {
    # 默认**关闭**（与 engine.RULE_MODULES 中 spirit_* 那行注释一致）。
    # 为什么：这 8 个信号依赖「相邻两轮快照增量」近似单笔成交，噪声天然高于
    # 既有 4 条规则 —— 而要触发就得是 60 万股/6,000 手这种量级，误报代价大。
    # 上线前先在真实行情里观察几天的命中质量再打开：
    # ``build({"enabled": True})``，或在 config/settings.yaml 的
    # ``rules.spirit_order`` 节里写 ``enabled: true``。
    "enabled": False,
    # --- 大笔买入 / 大笔卖出（事件型）------------------------------------
    # 外盘（内盘）增量占流通盘的比例门槛（%）
    "big_turnover_pct": 0.1,
    # --- 机构吃货 / 机构吐货（事件型，主动成交金额/股数）-----------------
    # 绝对量门槛：50 万股
    "institution_eat_shares": 500_000.0,
    # 绝对额门槛：100 万元
    "institution_eat_amount": 1_000_000.0,
    # 占流通盘比例门槛（%）
    "institution_eat_float_pct": 0.1,
    # --- 机构买单 / 机构卖单（状态型，单档挂单）--------------------------
    "institution_order_shares": 500_000.0,
    "institution_order_amount": 1_000_000.0,
    "institution_order_float_pct": 0.25,
    # --- 有大买盘 / 有大卖盘（状态型，五档合计）--------------------------
    # 80 万股
    "wall_shares": 800_000.0,
    "wall_float_pct": 0.8,
    # --- 快照增量近似单笔的相关控制 --------------------------------------
    # 相邻两轮快照的最大间隔（秒），超过则跳过本轮信号。
    # 为什么必须有：数据源断线/午休会让"上一轮"与"这一轮"之间隔着几十分钟，
    # 期间累积的成交量会被当成一笔巨单 —— 这是本规则最大的假信号来源。
    "max_gap_seconds": 120.0,
    # 区间主动成交额归因时用的最小总增量，防止除零/噪声
    "min_delta_lots": 0.0,
    # --- 分组开关（8 个信号可分别关掉，便于按需降噪）----------------------
    #: 成交类：大笔买入/大笔卖出/机构吃货/机构吐货（依赖上一轮快照）
    "detect_trade": True,
    #: 盘口类：机构买单/机构卖单/有大买盘/有大卖盘（只看当前快照）
    "detect_order": True,
    # --- 通用 -----------------------------------------------------------
    "cooldown_seconds": 300.0,
    "only_continuous": True,
    "severity": 2,
    "max_per_round": 20,
}

_KIND = AlertKind.UNUSUAL

#: severity 分配（信号紧急程度不同，按短线精灵的实际含义分档）
_SEV_BIG = 2            # 大笔买入 / 大笔卖出：普通但重要
_SEV_EAT = 3            # 机构吃货 / 机构吐货：立即成交，最紧急
_SEV_ORDER = 2          # 机构买单 / 机构卖单：只是挂单，可能撤
_SEV_WALL = 1           # 大买盘 / 大卖盘：存在感提示，噪声最多

_SEV_OF: dict[str, int] = {
    "big_buy": _SEV_BIG,
    "big_sell": _SEV_BIG,
    "institution_eat": _SEV_EAT,
    "institution_vomit": _SEV_EAT,
    "institution_buy": _SEV_ORDER,
    "institution_sell": _SEV_ORDER,
    "big_bid_wall": _SEV_WALL,
    "big_ask_wall": _SEV_WALL,
}

#: 排序用优先级（数值越小越靠前；同 severity 时先报成交、后报挂单）
_RANK_OF: dict[str, int] = {
    "institution_eat": 0,
    "institution_vomit": 0,
    "big_buy": 1,
    "big_sell": 1,
    "institution_buy": 2,
    "institution_sell": 2,
    "big_bid_wall": 3,
    "big_ask_wall": 3,
}

#: 手 -> 股
_LOT_SHARES = 100.0


def _num(v, default: float = 0.0) -> float:
    """宽松转 float：None / 非法值 / 空串 -> default。"""
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class SpiritOrderRule:
    """短线精灵 · 盘口/委托类信号。``build(cfg)`` 构造，``cfg`` 见 ``DEFAULTS``。"""

    name: str = "spirit_order"
    cfg: dict = field(default_factory=dict)
    #: 上一轮快照缓存：``code -> (outer_vol, inner_vol, amount, volume_lots, epoch)``。
    #: 单位为契约单位（外/内盘=手、amount=元、volume_lots=手）。
    _prev: dict = field(default_factory=dict, repr=False)
    #: 上一轮缓存所属自然日（``YYYY-MM-DD``），跨日整体清空（见 ``_reset_day``）。
    _day: str = field(default="", repr=False)
    #: 状态型信号的边沿检测状态：``f"{code}:{pattern}"`` -> 当前是否成立。
    _edge_state: dict = field(default_factory=dict, repr=False)
    #: 最近一次 evaluate 用的冷却秒数（``_mk`` 需要它填 cooldown_seconds）
    _cooldown: float = field(default=300.0, repr=False)
    #: 最近一次 evaluate 的告警时间戳 / 时间桶，供 ``_materialize`` 使用
    #: （延迟构造的代价：把这两个值挂在实例上，省掉逐候选传参）
    _ts: datetime | None = field(default=None, repr=False)
    _bucket: int = field(default=0, repr=False)

    # ------------------------------------------------------------------
    def _reset_day(self, now: datetime | None) -> None:
        """跨自然日重置全部状态。

        为什么必须重置：
        * ``outer_vol``/``inner_vol``/``amount`` 都是**当日累计**，次日从 0 重新开始，
          拿昨天的缓存做差会得到负数，或者（更糟）把昨天的巨量当成今天的增量；
        * 边沿状态不重置的话，昨天"有买盘"的股票今天第一轮就不报了。
        """
        day = now.strftime("%Y-%m-%d") if now is not None else ""
        if day == self._day:
            return
        self._day = day
        self._prev.clear()
        self._edge_state.clear()

    # ------------------------------------------------------------------
    def _edge(self, now: datetime | None, code: str, pattern: str, *, on: bool, off: bool) -> bool:
        """状态型信号的边沿检测：仅在 ``False -> True`` 时返回 True。

        ``on`` 是"进入该状态"的条件；``off`` 是"已明确恢复"的条件，两者之间
        留迟滞，避免挂单量在阈值附近抖动导致反复上报。状态与快照缓存同一天重置。
        """
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

        ts = ctx.now or snap.ts
        now_epoch = float(ctx.now_epoch)
        cooldown = _num(self._get(ctx, "cooldown_seconds", 300.0))
        self._cooldown = cooldown
        bucket = bucket_of(now_epoch, cooldown)
        # 延迟构造 Alert 需要这两个值（见 _materialize）
        self._ts = ts
        self._bucket = bucket

        max_per_round = int(_num(self._get(ctx, "max_per_round", 20), 20.0))
        max_gap = _num(self._get(ctx, "max_gap_seconds", 120.0))
        min_delta = _num(self._get(ctx, "min_delta_lots", 0.0))
        detect_order = bool(self._get(ctx, "detect_order", True))
        detect_trade = bool(self._get(ctx, "detect_trade", True))

        # 配置项先取出来，避免在内层循环反复查表
        big_turnover_pct = _num(self._get(ctx, "big_turnover_pct", 0.1))
        eat_shares = _num(self._get(ctx, "institution_eat_shares", 500_000.0))
        eat_amount = _num(self._get(ctx, "institution_eat_amount", 1_000_000.0))
        eat_float_pct = _num(self._get(ctx, "institution_eat_float_pct", 0.1))
        order_shares = _num(self._get(ctx, "institution_order_shares", 500_000.0))
        order_amount = _num(self._get(ctx, "institution_order_amount", 1_000_000.0))
        order_float_pct = _num(self._get(ctx, "institution_order_float_pct", 0.25))
        wall_shares = _num(self._get(ctx, "wall_shares", 800_000.0))
        wall_float_pct = _num(self._get(ctx, "wall_float_pct", 0.8))

        # 跨自然日：清空上一轮快照与边沿状态（必须在读取 _prev 之前）
        self._reset_day(ts)
        self._drop_stale(snap)
        return self._scan(snap, ctx, now_epoch, max_per_round,
                          max_gap, min_delta, big_turnover_pct,
                          eat_shares, eat_amount, eat_float_pct,
                          order_shares, order_amount, order_float_pct,
                          wall_shares, wall_float_pct,
                          detect_order, detect_trade)

    # ------------------------------------------------------------------
    def _drop_stale(self, snap: Snapshot) -> None:
        """丢弃本轮快照里已不存在的代码缓存。

        为什么必须丢：某只票从池子里消失若干轮再回来后，旧缓存会让"消失期间
        累积的量"被当成一轮的增量 —— 与"间隔过长"是同一类假信号。
        """
        if not self._prev:
            return
        for c in [c for c in self._prev if c not in snap.quotes]:
            self._prev.pop(c, None)

    # ------------------------------------------------------------------
    def _scan(
        self,
        snap: Snapshot,
        ctx: RuleContext,
        now_epoch: float,
        max_per_round: int,
        max_gap: float,
        min_delta: float,
        big_turnover_pct: float,
        eat_shares: float,
        eat_amount: float,
        eat_float_pct: float,
        order_shares: float,
        order_amount: float,
        order_float_pct: float,
        wall_shares: float,
        wall_float_pct: float,
        detect_order: bool,
        detect_trade: bool,
    ) -> list[Alert]:
        """逐票求值 + 排序 + 截断。"""
        picked: list[tuple] = []
        for code, q in snap.quotes.items():
            if q is None:
                continue
            # --- 边界：停牌 / 昨收异常 / 无价 ---------------------------
            # 显式条件 + is_suspended 双保险：is_suspended 的口径是
            # price<=0 / prev_close<=0 / volume_lots<=0，前两项单独再判一次是为了
            # 让"昨收异常"这条路径即使将来口径变化也不会漏拦。
            if q.price <= 0.0 or q.prev_close <= 0.0 or q.is_suspended:
                continue

            # --- 盘口类：机构买单/卖单、大买盘/大卖盘（不依赖上一轮快照）--
            if detect_order:
                picked += self._check_orders(
                    q, code,
                    order_shares, order_amount, order_float_pct, wall_shares, wall_float_pct,
                )

            # --- 成交类：大笔买入/卖出、机构吃货/吐货（需要上一轮快照）----
            if detect_trade:
                picked += self._check_trades(
                    q, code, now_epoch,
                    max_gap, min_delta, big_turnover_pct,
                    eat_shares, eat_amount, eat_float_pct,
                )

        # 确定性排序：severity 降序 -> 信号优先级 -> |pct| 降序 -> code 升序。
        # 每只票每轮每个信号至多一条，所以这里只会砍掉排名靠后的信号。
        #
        # 关键性能点：`_mk` 返回的是**轻量候选元组**，Alert（含 4 行 detail 文本）
        # 只在**截断之后**才构造。全市场 5000 只票、每只命中 8 个信号时，
        # 先建 40000 个 Alert 再扔掉 39980 个要 ~250ms（超过 200ms 预算）；
        # 延迟构造后只剩几十毫秒。
        picked.sort(key=lambda p: (-p[0], p[1], -p[2], p[3]))
        if max_per_round > 0:
            picked = picked[:max_per_round]
        return [self._materialize(p) for p in picked]

    # ------------------------------------------------------------------
    def _check_trades(
        self,
        q: Quote,
        code: str,
        now_epoch: float,
        max_gap: float,
        min_delta: float,
        big_turnover_pct: float,
        eat_shares: float,
        eat_amount: float,
        eat_float_pct: float,
    ) -> list[tuple]:
        """成交类 4 个信号：用**相邻两轮快照的增量**近似本区间的主动买卖。

        返回轻量候选列表（0~4 条，见 ``_mk``）。

        任何一条"不可信"的路径都只更新缓存、不产告警：
        首轮无上轮、间隔过长、成交量回退、主动量为 0。
        """
        outer = _num(q.outer_vol, 0.0)
        inner = _num(q.inner_vol, 0.0)
        amount = _num(q.amount, 0.0)
        volume = _num(q.volume_lots, 0.0)

        prev = self._prev.get(code)
        # 无论本轮是否产生告警，都要把最新值写回缓存（下一轮要拿它做差）
        self._prev[code] = (outer, inner, amount, volume, now_epoch)

        if prev is None:
            return []                       # 首轮：没有上轮，绝不猜
        p_outer, p_inner, p_amount, p_volume, p_epoch = prev

        # 间隔过长（数据源断线 / 跨午休）：整段累积量会被误判成一笔巨单 -> 跳过
        if max_gap > 0.0 and (now_epoch - float(p_epoch)) > max_gap:
            return []

        # 成交量回退（数据源重置 / 跨日）：增量是负的，任何比例都无意义 -> 跳过
        if volume < float(p_volume) or outer < float(p_outer) or inner < float(p_inner):
            return []

        d_outer = outer - float(p_outer)
        d_inner = inner - float(p_inner)
        d_volume = volume - float(p_volume)
        d_amount = amount - float(p_amount)
        d_act = d_outer + d_inner
        if d_volume <= 0.0 or d_act <= 0.0:
            return []                       # 本轮没有成交（或内外盘没更新）
        if min_delta > 0.0 and d_act < min_delta:
            return []

        # 区间成交额按内外盘比例归因：主动买占 d_outer/d_act，主动卖占 d_inner/d_act。
        # 注意这是**近似**：真实资金流还受撤单、大单拆分影响，见模块文档。
        d_amount = max(d_amount, 0.0)
        buy_amount = d_amount * d_outer / d_act
        sell_amount = d_amount * d_inner / d_act

        float_shares = float(q.float_shares)
        out: list[tuple] = []

        # --- 大笔买入 / 大笔卖出：区间主动量占流通盘比例 ------------------
        if big_turnover_pct > 0.0 and float_shares > 0.0:
            if d_outer > 0.0:
                ratio_buy = d_outer * _LOT_SHARES / float_shares * 100.0
                if ratio_buy >= big_turnover_pct:
                    out.append(self._mk(
                        q, "big_buy",
                        title=f"大笔买入 {d_outer:,.0f}手",
                        extra=f"本区间主动买入 {d_outer:,.0f} 手（{d_outer * _LOT_SHARES:,.0f} 股），"
                              f"占流通盘 {ratio_buy:.3f}% > {big_turnover_pct:.2f}%",
                        metrics={"d_outer_lots": d_outer, "float_pct": ratio_buy},
                    ))
            if d_inner > 0.0:
                ratio_sell = d_inner * _LOT_SHARES / float_shares * 100.0
                if ratio_sell >= big_turnover_pct:
                    out.append(self._mk(
                        q, "big_sell",
                        title=f"大笔卖出 {d_inner:,.0f}手",
                        extra=f"本区间主动卖出 {d_inner:,.0f} 手（{d_inner * _LOT_SHARES:,.0f} 股），"
                              f"占流通盘 {ratio_sell:.3f}% > {big_turnover_pct:.2f}%",
                        metrics={"d_inner_lots": d_inner, "float_pct": ratio_sell},
                    ))

        # --- 机构吃货 / 机构吐货：主动成交股数 或 金额 或 流通盘比例 ------
        # 阈值是「或」：任一满足即触发，命中项写进 metrics 便于排查
        if d_outer > 0.0:
            hit = self._eat_hits(d_outer * _LOT_SHARES, buy_amount, d_outer, float_shares,
                                 eat_shares, eat_amount, eat_float_pct)
            if hit is not None:
                out.append(self._mk(
                    q, "institution_eat",
                    title=f"机构吃货 {hit[0] / 1e4:,.0f}股",
                    extra=f"本区间主动买入成交 {hit[0]:,.0f} 股 / {buy_amount / 1e4:,.1f} 万元"
                          f"（约 {d_outer:,.0f} 手），命中：{hit[3]}",
                    metrics={"buy_shares": hit[0], "buy_amount": buy_amount,
                             "hit_shares": 1.0 if hit[1] else 0.0,
                             "hit_amount": 1.0 if hit[2] else 0.0,
                             "hit_float_pct": 1.0 if hit[4] else 0.0},
                ))
        if d_inner > 0.0:
            hit = self._eat_hits(d_inner * _LOT_SHARES, sell_amount, d_inner, float_shares,
                                 eat_shares, eat_amount, eat_float_pct)
            if hit is not None:
                out.append(self._mk(
                    q, "institution_vomit",
                    title=f"机构吐货 {hit[0] / 1e4:,.0f}股",
                    extra=f"本区间主动卖出成交 {hit[0]:,.0f} 股 / {sell_amount / 1e4:,.1f} 万元"
                          f"（约 {d_inner:,.0f} 手），命中：{hit[3]}",
                    metrics={"sell_shares": hit[0], "sell_amount": sell_amount,
                             "hit_shares": 1.0 if hit[1] else 0.0,
                             "hit_amount": 1.0 if hit[2] else 0.0,
                             "hit_float_pct": 1.0 if hit[4] else 0.0},
                ))

        # 每类信号各留一条（同一只票同一轮最多同时出现这 4 条）
        return out

    # ------------------------------------------------------------------
    @staticmethod
    def _eat_hits(
        shares: float,
        amount: float,
        lots: float,
        float_shares: float,
        thr_shares: float,
        thr_amount: float,
        thr_float_pct: float,
    ) -> tuple[float, bool, bool, str, bool] | None:
        """机构吃货/吐货的「或」判定。返回 ``(股数, 命中股数项, 命中金额项, 说明, 命中比例项)``。

        ``float_shares <= 0`` 时**只**用绝对量阈值 —— 数据源不给流通市值
        （自选股降级模式）不能导致信号整体失效。
        """
        if shares < thr_shares and amount < thr_amount and thr_float_pct <= 0.0:
            return None                      # 快速路径：明显不够
        hits: list[str] = []
        hit_shares = thr_shares > 0.0 and shares > thr_shares
        hit_amount = thr_amount > 0.0 and amount > thr_amount
        if hit_shares:
            hits.append(f"{thr_shares / 1e4:.0f}万股")
        if hit_amount:
            hits.append(f"{thr_amount / 1e4:.0f}万元")
        hit_float = False
        if thr_float_pct > 0.0 and float_shares > 0.0:
            float_pct = lots * _LOT_SHARES / float_shares * 100.0
            hit_float = float_pct > thr_float_pct
            if hit_float:
                hits.append(f"流通盘{thr_float_pct:.2f}%")
        if not (hit_shares or hit_amount or hit_float):
            return None
        return (shares, hit_shares, hit_amount, " 或 ".join(hits), hit_float)

    # ------------------------------------------------------------------
    def _check_orders(
        self,
        q: Quote,
        code: str,
        order_shares: float,
        order_amount: float,
        order_float_pct: float,
        wall_shares: float,
        wall_float_pct: float,
    ) -> list[tuple]:
        """盘口类 4 个信号：机构买单/卖单（单档）、大买盘/大卖盘（五档合计）。

        ``ts`` 不在这里传：边沿状态按自然日重置，日期取自 ``self._ts``
        （``evaluate`` 每轮填好），而 ``_mk`` 造的候选要到 ``_materialize``
        才需要时间戳。
        """
        ts = self._ts
        out: list[tuple] = []
        float_shares = float(q.float_shares)

        # --- 机构买单 / 机构卖单：买/卖队列中**存在单档**满足「或」条件 ---
        # 用"存在"而不是"合计"：官方口径就是某一档挂出巨单。
        check_order = order_shares > 0.0 or order_amount > 0.0 or order_float_pct > 0.0
        bid_hit = (self._best_order(q.bid_vols, q.bid_prices, float_shares,
                                    order_shares, order_amount, order_float_pct,
                                    _num(q.bid1, 0.0))
                   if check_order else None)
        # on = 条件成立；off = 条件已消失 -> 解除武装，下次再出现才能重报。
        # 传 ``off=hit is None`` 而不是恒 True：恒 True 会在"持续成立"的每一轮
        # 把状态清掉，导致下一轮又被当成 False->True 重复上报。
        if self._edge(ts, code, "institution_buy", on=bid_hit is not None, off=bid_hit is None):
            lots, px, hits, shares, amt = bid_hit
            out.append(self._mk(
                q, "institution_buy",
                title=f"机构买单 {lots:,.0f}手",
                extra=f"买盘挂出 {lots:,.0f} 手（{shares:,.0f} 股 / {amt / 1e4:,.1f} 万元"
                      f" @ {px:.2f} 元），命中：{hits}",
                metrics={"bid_lots": lots, "bid_price": px,
                         "bid_shares": shares, "bid_amount": amt},
            ))
        ask_hit = (self._best_order(q.ask_vols, q.ask_prices, float_shares,
                                    order_shares, order_amount, order_float_pct,
                                    _num(q.ask1, 0.0))
                   if check_order else None)
        if self._edge(ts, code, "institution_sell", on=ask_hit is not None, off=ask_hit is None):
            lots, px, hits, shares, amt = ask_hit
            out.append(self._mk(
                q, "institution_sell",
                title=f"机构卖单 {lots:,.0f}手",
                extra=f"卖盘挂出 {lots:,.0f} 手（{shares:,.0f} 股 / {amt / 1e4:,.1f} 万元"
                      f" @ {px:.2f} 元），命中：{hits}",
                metrics={"ask_lots": lots, "ask_price": px,
                         "ask_shares": shares, "ask_amount": amt},
            ))

        # --- 有大买盘 / 有大卖盘：五档合计；**没有五档必须跳过** ----------
        # 为什么必须跳过：bid_total_vol 在没有五档时会退回买一量，
        # 那是"只有一档"的量，拿它冒充五档合计会系统性低估/误判。
        check_wall = wall_shares > 0.0 or wall_float_pct > 0.0
        wall_ok = q.has_depth and check_wall
        bid_lots = self._depth_total(q, "bid") if wall_ok else 0.0
        bid_wall = self._wall_hits(bid_lots, float_shares, wall_shares, wall_float_pct) \
            if wall_ok else None
        if self._edge(ts, code, "big_bid_wall", on=bid_wall is not None, off=bid_wall is None):
            shares, hits = bid_wall
            out.append(self._mk(
                q, "big_bid_wall",
                title=f"有大买盘 {bid_lots:,.0f}手",
                extra=f"五档买盘合计 {bid_lots:,.0f} 手（{shares:,.0f} 股），命中：{hits}",
                metrics={"bid_total_lots": bid_lots, "bid_shares": shares,
                         "bid1": _num(q.bid1, 0.0), "bid_vol": _num(q.bid_vol, 0.0)},
            ))
        ask_lots = self._depth_total(q, "ask") if wall_ok else 0.0
        ask_wall = self._wall_hits(ask_lots, float_shares, wall_shares, wall_float_pct) \
            if wall_ok else None
        if self._edge(ts, code, "big_ask_wall", on=ask_wall is not None, off=ask_wall is None):
            shares, hits = ask_wall
            out.append(self._mk(
                q, "big_ask_wall",
                title=f"有大卖盘 {ask_lots:,.0f}手",
                extra=f"五档卖盘合计 {ask_lots:,.0f} 手（{shares:,.0f} 股），命中：{hits}",
                metrics={"ask_total_lots": ask_lots, "ask_shares": shares,
                         "ask1": _num(q.ask1, 0.0), "ask_vol": _num(q.ask_vol, 0.0)},
            ))
        return out

    # ------------------------------------------------------------------
    @staticmethod
    def _depth_total(q: Quote, side: str) -> float:
        """五档合计（手）。

        为什么不用 ``q.bid_total_vol``：那是 ``sum(self.bid_vols)`` 的直接求和，
        某一档是 ``None``/非数字（数据源脏数据）时会**抛 TypeError**，
        整条规则连同引擎这一轮全部告警一起炸掉。这里逐档宽松转 float。
        """
        vols = q.bid_vols if side == "bid" else q.ask_vols
        return sum(_num(v, 0.0) for v in vols)

    # ------------------------------------------------------------------
    @staticmethod
    def _best_order(
        vols,
        prices,
        float_shares: float,
        thr_shares: float,
        thr_amount: float,
        thr_float_pct: float,
        fallback_px: float = 0.0,
    ) -> tuple[float, float, str, float, float] | None:
        """在某一侧的挂单里找**满足「或」条件的最佳单档**。

        返回 ``(手, 价, 命中说明, 股, 金额)``；没有任何一档达标返回 None。
        * 只遍历 ``vols``，价格缺失/为 0 时用 ``fallback_px``（调用方传买一/卖一价）
          兜底，仍然没有就只能按 0 金额判 —— 那样只有绝对量口径能触发；
        * 金额 = 手 × 100 × 价格（契约单位：量=手、价=元）；
        * ``float_shares <= 0`` 时比例项自动失效，只比绝对量/绝对额。
        """
        best = None
        best_lots = 0.0
        for i, raw in enumerate(vols):
            lots = _num(raw, 0.0)
            if lots <= 0.0:
                continue
            px = _num(prices[i], 0.0) if i < len(prices) else 0.0
            if px <= 0.0:
                px = fallback_px
            shares = lots * _LOT_SHARES
            amount = shares * px
            hits: list[str] = []
            if thr_shares > 0.0 and shares > thr_shares:
                hits.append(f"{thr_shares / 1e4:.0f}万股")
            if thr_amount > 0.0 and amount > thr_amount:
                hits.append(f"{thr_amount / 1e4:.0f}万元")
            if thr_float_pct > 0.0 and float_shares > 0.0:
                if shares / float_shares * 100.0 > thr_float_pct:
                    hits.append(f"流通盘{thr_float_pct:.2f}%")
            if not hits:
                continue
            if lots > best_lots:            # 取量最大的那一档作为代表
                best_lots = lots
                best = (lots, px, " 或 ".join(hits), shares, amount)
        return best

    # ------------------------------------------------------------------
    @staticmethod
    def _wall_hits(
        lots: float,
        float_shares: float,
        thr_shares: float,
        thr_float_pct: float,
    ) -> tuple[float, str] | None:
        """大买盘/大卖盘的「或」判定（合计口径）。返回 ``(股数, 命中说明)``。"""
        if lots <= 0.0:
            return None
        shares = lots * _LOT_SHARES
        hits: list[str] = []
        if thr_shares > 0.0 and shares > thr_shares:
            hits.append(f"{thr_shares / 1e4:.0f}万股")
        if thr_float_pct > 0.0 and float_shares > 0.0:
            if shares / float_shares * 100.0 > thr_float_pct:
                hits.append(f"流通盘{thr_float_pct:.2f}%")
        if not hits:
            return None
        return (shares, " 或 ".join(hits))

    # ------------------------------------------------------------------
    def _mk(
        self,
        q: Quote,
        pattern: str,
        *,
        title: str,
        extra: str,
        metrics: dict,
    ) -> tuple:
        """构造**轻量候选** ``(severity, rank, |pct|, code, q, pattern, title, extra, metrics)``。

        刻意不在这里造 ``Alert``：全市场 5000 只票 × 8 个信号 = 40000 个候选，
        而 ``max_per_round`` 通常只留 20 条。先造 Alert（含 4 行 detail、
        ``vwap``/``above_vwap`` 两次 property 计算）再丢掉 99.95% 要 ~250ms，
        超过单轮 200ms 预算；延迟到截断之后再构造（见 ``_materialize``）只需几十毫秒。
        """
        return (
            _SEV_OF.get(pattern, 2),
            _RANK_OF.get(pattern, 9),
            abs(_num(q.pct, 0.0)),          # 排序用 |pct|，正负不影响紧急度
            q.code,
            q,
            pattern,
            title,
            extra,
            metrics,
        )

    # ------------------------------------------------------------------
    def _materialize(self, cand: tuple) -> Alert:
        """把候选元组展开成完整 ``Alert``（只在截断后对少数几条调用）。"""
        sev, _rank, _abs_pct, code, q, pattern, title, extra, metrics = cand
        pct = _num(q.pct, 0.0)
        vwap = float(q.vwap)
        above = bool(q.above_vwap)
        dev = (float(q.price) / vwap - 1.0) * 100.0 if vwap > 0.0 else 0.0
        cn = PATTERN_CN.get(pattern, pattern)
        detail = "\n".join(
            [
                f"{cn} · 现价 {float(q.price):.2f} 元  涨跌幅 {fmt_pct(pct)}",
                extra,
                f"换手 {float(q.turnover):.2f}%  成交额 {float(q.amount) / 1e8:.2f} 亿"
                f"  外盘 {float(q.outer_vol):,.0f} 手 / 内盘 {float(q.inner_vol):,.0f} 手",
                f"均价 {vwap:.2f} 元 · 现价{'站上' if above else '跌破'}均价 {dev:+.2f}%",
            ]
        )
        m = {
            "pattern": pattern,
            "pct": pct,
            "price": float(q.price),
            "amount": float(q.amount),
            "turnover": float(q.turnover),
            "outer_vol": float(q.outer_vol),
            "inner_vol": float(q.inner_vol),
            "float_shares": float(q.float_shares),
            "above_vwap": 1.0 if above else 0.0,
        }
        m.update(metrics or {})
        return Alert(
            # key 必须带 pattern 分段：8 个信号共用一个 AlertKind，
            # 少了分段时同桶内不同信号会撞 key 互相吞掉。
            key=f"{code}:{_KIND.value}:{pattern}:{self._bucket}",
            kind=_KIND,
            code=code,
            name=q.name,
            ts=self._ts,
            price=float(q.price),
            pct=pct,
            title=title,
            detail=detail,
            severity=sev,
            metrics=m,
            cooldown_key=f"{code}:{_KIND.value}:{pattern}",
            cooldown_seconds=self._cooldown,
        )


def build(cfg: dict | None = None) -> SpiritOrderRule:
    """用配置构造规则（缺省项取 DEFAULTS）。"""
    merged = dict(DEFAULTS)
    for k, v in (cfg or {}).items():
        if v is not None:
            merged[k] = v
    return SpiritOrderRule(cfg=merged)


RULE = build({})
