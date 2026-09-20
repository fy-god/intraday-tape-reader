"""短线精灵 · 指数类信号（拉升指数 / 打压指数）。

官方定义（大智慧/同花顺 短线精灵帮助）
--------------------------------------
* **拉升指数**：5 分钟内对指数的**拉升值 > 0.5**（指数点），即指数被权重股
  快速拉起；
* **打压指数**：5 分钟内对指数的**打压值 > 0.5**（指数点），方向相反。

注意阈值单位是**指数点**（绝对值），不是百分比。同样 0.5 点，对上证指数
（约 3900 点）只是 0.013%，对创业板指（约 3300 点）是 0.015% —— 都是极小
的瞬时波动，所以官方口径里这个信号**非常频繁**。本模块因此默认把阈值换算成
**基点(bp)** 并按指数点位缩放：``0.5 点`` 在 3900 点的上证上等于 1.28bp。
直接照搬"0.5 点"会让大盘指数几乎每轮都报，信号等于噪声。

本模块的两个诚实前提
--------------------
1. **指数点位必须自带前缀**。``000001`` 既是上证指数（``sh000001``）也是
   平安银行（``sz000001``），6 位纯数字码无法区分。本模块要求传入的
   ``Quote.code`` 已经是消歧后的（见 ``sources/tencent.py`` 的前缀保留逻辑），
   并用 ``board_of`` + 名称双重确认这确实是指数；不是指数就跳过，绝不猜。
2. **指数不进入 ``snap.quotes``**。引擎只把指数行情放进 ``state.quotes``，
   规则从 ``ctx.state`` 里读。这样 ``tick_surge``/``limit_board`` 等个股规则
   不会对指数误报"火箭发射"。
"""
from __future__ import annotations

from datetime import datetime

from ..models import (Alert, AlertKind, Board, Quote, Snapshot,
                      looks_like_index)
from ..session import CONTINUOUS
from .base import RuleContext, bucket_of, fmt_pct

__all__ = [
    "SIGNALS", "SIGNAL_CN", "DEFAULTS", "SpiritIndexRule", "build", "RULE",
    "DEFAULT_INDEX_CODES", "is_index_quote",
]

#: 本模块产出的信号名（写进 ``Alert.metrics["pattern"]``）。
SIGNALS = ("index_pull", "index_press")

SIGNAL_CN = {
    "index_pull": "拉升指数",
    "index_press": "打压指数",
}

#: 常用指数及其"消歧后代码"。指数代码必须带交易所前缀，否则会与个股撞码。
DEFAULT_INDEX_CODES = (
    "sh000001",     # 上证指数
    "sz399001",     # 深证成指
    "sz399006",     # 创业板指
    "sh000300",     # 沪深300
    "sh000688",     # 科创50
    "sh000905",     # 中证500
    "sz399005",     # 中小100
)

DEFAULTS: dict = {
    # 默认**关闭**：需要指数行情管道（poll.index_codes），且对大盘指数
    # 极易刷屏。确认数据管道通后再打开。
    "enabled": False,
    "only_continuous": True,
    # --- 扫描窗口：官方口径是 5 分钟 ---
    "windows": [300],
    # --- 触发阈值 ---
    #: 官方口径："拉升值 > 0.5 点"。这里记为 0.5，同时用下面的 bp 阈值兜底：
    #: ``min_points`` 与 ``min_bp`` **任一**满足即触发（OR），这样小盘指数
    #: （点位低，0.5 点 = 更多 bp）和大盘指数（点位高，0.5 点 = 更少 bp）
    #: 都不会因为单一口径而失真。
    "min_points": 0.5,
    #: 相对幅度阈值（bp，万分之一）。1.28bp ≈ 上证 3900 点上的 0.5 点。
    #: 设 0 表示禁用该口径（只按点数判定）。
    "min_bp": 1.2,
    #: 指数点位低于此值时只按 bp 判定 —— 低价指数上 0.5 点就是巨大波动。
    "min_index_level": 100.0,
    #: 现价与窗口方向一致才算数（避免"跌了但相对起点仍是涨"这类歧义）
    "require_direction_match": True,
    #: 指数白名单（带交易所前缀）。留空/None = 不限，全靠 ``is_index_quote`` 判定。
    #: 写裸 6 位码无意义：``000001`` 无法区分上证指数与平安银行。
    "codes": (),
    # --- 通用 ---
    "cooldown_seconds": 300,
    "max_per_round": 10,
    "severity": 1,
    #: 达到阈值这么多倍 -> severity 3（指数异动是全局信号，值得提级）
    "urgent_multiple": 3.0,
}


def _num(v, default: float = 0.0) -> float:
    """宽松转 float：None / 非法 / 空串 -> default。"""
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def is_index_quote(q: Quote) -> bool:
    """判断这条行情是不是指数。

    **不能只看有没有前缀**：``sz000001`` 是平安银行（个股），``sh000001`` 才是
    上证指数。判据主体在 :func:`arad.models.looks_like_index`（数据源层共用同一
    份逻辑，避免两处判断漂移）；这里额外认 ``Quote.board``——行情已经带了板块
    结论时它就是权威，不必再靠代码/名称猜。

    判定顺序（强 -> 弱）：
    1. ``board`` 已判为 INDEX（数据源已确认）；
    2. 名称含"指数/成指"、``399xxx``、或"沪市 000 段白名单"——见
       :func:`arad.models.looks_like_index`。
    """
    code = str(getattr(q, "code", "") or "")
    name = str(getattr(q, "name", "") or "")

    board = getattr(q, "board", None)
    if board is Board.INDEX or getattr(board, "value", board) == Board.INDEX.value:
        return True
    return looks_like_index(code, name)


class SpiritIndexRule:
    """短线精灵指数类信号：拉升指数 / 打压指数。"""

    name = "spirit_index"
    #: 引擎据此决定是否为 ``poll.index_codes`` 发请求（本规则消费指数行情）。
    wants_indices = True

    def __init__(self, cfg: dict | None = None):
        merged = dict(DEFAULTS)
        merged.update(cfg or {})
        self.cfg = merged
        self.windows = self._norm_windows(merged.get("windows"))
        self.cooldown = float(merged.get("cooldown_seconds", 300) or 300)
        # 0 = 不限量（与 limit_board/volume_burst 约定一致；不能用 `or N`，
        # 那会把显式 0 悄悄改回默认值）。
        _mpr = merged.get("max_per_round", 10)
        self.max_per_round = int(10 if _mpr is None else _mpr)
        self.severity = int(merged.get("severity", 1) or 1)
        self.urgent_multiple = float(merged.get("urgent_multiple", 3.0) or 0.0)
        #: 允许监控的指数白名单。空 = 不限（只靠 is_index_quote 判定）。
        raw_codes = merged.get("codes")
        self.codes: tuple[str, ...] = tuple(
            str(c).strip().lower() for c in (raw_codes or ()) if str(c).strip()
        )
        self._n = {k: float(v) for k, v in merged.items()
                   if isinstance(v, (int, float)) and not isinstance(v, bool)}

    # ------------------------------------------------------------------
    @staticmethod
    def _norm_windows(raw) -> list[float]:
        out: list[float] = []
        for item in raw or []:
            try:
                sec = float(item)
            except (TypeError, ValueError):
                continue
            if sec > 0:
                out.append(sec)
        out.sort()
        return out or [300.0]

    def _g(self, key: str, default: float) -> float:
        return self._n.get(key, default)

    # ------------------------------------------------------------------
    def _covered(self, pts, seconds: float) -> bool:
        """窗口是否被历史真正覆盖。

        历史只有 30 秒却按"5 分钟"判定，会把开盘头 30 秒的剧烈波动
        当成 5 分钟级别的拉升 —— 这是本项目在 tick_surge 上踩过的坑。
        要求最早样本至少覆盖窗口的 80%。
        """
        if len(pts) < 2:
            return False
        span = pts[-1][0] - pts[0][0]
        return span >= seconds * 0.8

    # ------------------------------------------------------------------
    def _evaluate_one(self, q: Quote, now: datetime, now_ep: float,
                      ctx: RuleContext):
        """对单条指数求值，返回 ``[(强度, 顺序, Alert)]``。"""
        # 阈值优先取引擎下发的 ctx.cfg（热更新配置走这条路），
        # 其次构造时的实例配置。用 _num 兜底，坏配置不至于让规则崩掉。
        def g(key, default):
            if isinstance(ctx.cfg, dict) and ctx.cfg.get(key) is not None:
                return _num(ctx.cfg.get(key), default)
            return self._g(key, default)

        # 白名单：配了 codes 就只认这些
        if self.codes:
            key = str(q.code).strip().lower()
            bare = key[2:] if len(key) > 6 and key[:2] in ("sh", "sz", "bj") else key
            if key not in self.codes and bare not in self.codes:
                return []

        if q.price <= 0 or q.prev_close <= 0:
            return []

        # 指数类信号的点位阈值与指数本身量级强相关，故按窗口取点位数
        min_points = g("min_points", 0.5)
        min_bp = g("min_bp", 1.2)
        min_level = g("min_index_level", 100.0)

        out: list[tuple[float, int, Alert]] = []
        for seconds in self.windows:
            pts = ctx.state.window(q.code, seconds, now_ep)
            if not self._covered(pts, seconds):
                continue
            start = pts[0][1]
            if start <= 0:
                continue

            delta = q.price - start            # 指数点变化
            change_pct = (q.price / start - 1.0) * 100.0
            change_bp = change_pct * 100.0     # 1% = 100bp

            # 口径一：绝对点数。只在指数点位足够大时才有意义 ——
            # 点位 20 的指数上 0.5 点是 2.5%，那已经是暴涨。
            hit_points = (min_points > 0.0 and abs(delta) >= min_points
                          and start >= min_level)
            # 口径二：相对幅度 bp
            hit_bp = min_bp > 0.0 and abs(change_bp) >= min_bp
            if not (hit_points or hit_bp):
                continue

            # 方向：现价相对窗口起点。要求与"当日涨跌方向"一致可过滤
            # "盘中砸坑后回到起点上方"这类两边都像的形态。
            if delta > 0:
                pattern, cn, kind = "index_pull", SIGNAL_CN["index_pull"], AlertKind.SURGE
                strength = max(abs(delta) / min_points if min_points > 0 else 0.0,
                               abs(change_bp) / min_bp if min_bp > 0 else 0.0)
            elif delta < 0:
                pattern, cn, kind = "index_press", SIGNAL_CN["index_press"], AlertKind.PLUNGE
                strength = max(abs(delta) / min_points if min_points > 0 else 0.0,
                               abs(change_bp) / min_bp if min_bp > 0 else 0.0)
            else:
                continue

            if (self.cfg.get("require_direction_match", True)
                    and q.prev_close > 0):
                day_pct = (q.price / q.prev_close - 1.0) * 100.0
                # 拉升却在当日深跌、打压却在当日大涨 -> 语义含混，跳过
                if pattern == "index_pull" and day_pct < -1.0:
                    continue
                if pattern == "index_press" and day_pct > 1.0:
                    continue

            hits = []
            if hit_points:
                hits.append(f"{min_points:g}点")
            if hit_bp:
                hits.append(f"{min_bp:g}bp")
            out.append((strength, 0 if pattern == "index_pull" else 1,
                        self._mk(q, now, now_ep, pattern, cn, kind, seconds,
                                 delta, change_pct, change_bp, strength,
                                 " 或 ".join(hits))))
        return out
    # ------------------------------------------------------------------
    def _mk(self, q: Quote, now: datetime, now_ep: float, pattern: str,
            cn: str, kind: AlertKind, seconds: float, delta: float,
            change_pct: float, change_bp: float, strength: float,
            hit_desc: str) -> Alert:
        bucket = bucket_of(now_ep, self.cooldown)
        severity = self.severity
        if self.urgent_multiple > 0 and strength >= self.urgent_multiple:
            severity = 3
        direction_cn = "拉升" if pattern == "index_pull" else "打压"
        title = f"{cn} {delta:+.2f}点 ({fmt_pct(change_pct)})"
        detail = "\n".join([
            f"{q.name or q.code} · 现价 {q.price:,.2f}  {fmt_pct(change_pct)}"
            f"  窗口 {self._fmt_s(seconds)} {delta:+.2f}点",
            # ⚠ 窗口文案必须用 _fmt_s(seconds)，不能写死「5 分钟」。
            # 上一行已经用 _fmt_s 渲染了窗口，这里写死会出现**相邻两行
            # 对同一个窗口给出不同说法**：windows=[60] 时行1 说「窗口 1分钟」、
            # 行2 说「5 分钟拉升」，用户无法判断该信哪个。
            # 默认 windows=[300] 正好等于 5 分钟，所以这个硬编码长期没被发现 ——
            # 只有把窗口改成别的值才会暴露（已实测 60/120/600 三档都会矛盾）。
            f"{self._fmt_s(seconds)}{direction_cn} {abs(delta):.2f} 点（{abs(change_bp):.2f}bp），"
            f"命中阈值：{hit_desc}",
            f"当日 {fmt_pct((q.price / q.prev_close - 1.0) * 100.0 if q.prev_close else 0.0)}"
            f"  昨收 {q.prev_close:,.2f}  今开 {q.open:,.2f}",
            f"最高 {q.high:,.2f}  最低 {q.low:,.2f}",
        ])
        return Alert(
            key=f"{q.code}:{kind.value}:{pattern}:{bucket}",
            kind=kind, code=q.code, name=q.name or q.code, ts=now,
            price=q.price,
            pct=(q.price / q.prev_close - 1.0) * 100.0 if q.prev_close else 0.0,
            title=title, detail=detail, severity=severity,
            cooldown_key=f"{q.code}:{kind.value}:{pattern}",
            cooldown_seconds=self.cooldown,
            metrics={
                "pattern": pattern,
                "window_seconds": float(seconds),
                "delta_points": round(delta, 3),
                "window_pct": round(change_pct, 4),
                "window_bp": round(change_bp, 3),
                "strength": round(strength, 3),
                "index_level": round(q.price, 2),
                "prev_close": round(q.prev_close, 2),
            },
        )

    @staticmethod
    def _fmt_s(seconds: float) -> str:
        s = int(round(seconds))
        if s % 60 == 0:
            return f"{s // 60}分钟"
        return f"{s}秒"

    # ------------------------------------------------------------------
    def evaluate(self, snap: Snapshot, ctx: RuleContext) -> list[Alert]:
        cfg = ctx.cfg or {}
        if not bool(cfg.get("enabled", self.cfg.get("enabled", False))):
            return []
        if bool(cfg.get("only_continuous",
                        self.cfg.get("only_continuous", True))) \
                and ctx.session not in CONTINUOUS:
            return []

        # IT-P1-INDEX-CURRENT-001：指数候选必须来自**本轮 current view**。
        #
        # ``ctx.state.quotes`` 是**累计 latest 缓存**，不是 current：指数路由
        # 某轮整体失败时，它里面仍是上一轮的指数 —— 直接遍历会让规则拿着陈旧
        # 数据继续产告警，而观测账本显示 index_admitted=0，两边对不上。
        # 所以优先用 ``ctx.current_indices``（引擎每轮填的本轮准入集）。
        candidates: dict[str, Quote] = {}
        current = getattr(ctx, "current_indices", None)
        if current is not None:
            for code, q in current.items():
                if q is not None and is_index_quote(q):
                    candidates[code] = q
        else:
            # 兼容老调用方/单测：没有 current view 时回退到累计缓存
            # （这是**旧行为**，不代表本轮真的有指数）。
            for code, q in (getattr(ctx.state, "quotes", None) or {}).items():
                if q is not None and is_index_quote(q):
                    candidates[code] = q
        # 兼容：若调用方（测试/回放）确实把指数放进了快照，也认
        for code, q in (snap.quotes or {}).items():
            if q is not None and is_index_quote(q):
                candidates.setdefault(code, q)

        now = ctx.now or datetime.now()
        now_ep = ctx.now_epoch
        picked: list[tuple[float, int, Alert]] = []
        for code, q in candidates.items():
            try:
                picked += self._evaluate_one(q, now, now_ep, ctx)
            except Exception:       # noqa: BLE001 - 单个指数异常不影响其它
                continue

        # 同一只指数每轮只报一个方向（同时"拉升"和"打压"是自相矛盾的），
        # 且多个窗口可能同时命中同一方向 —— 合并成一条，保留强度最高的。
        # 排序键 ``(-strength, order)``：强度降序，同强度时"拉升"在前。
        best: dict[tuple[str, str], tuple[float, int, Alert]] = {}
        for strength, order, a in picked:
            k = (a.code, a.metrics["pattern"])
            cur = best.get(k)
            if cur is None or (strength, -order) > (cur[0], -cur[1]):
                best[k] = (strength, order, a)
        rows = sorted(best.values(), key=lambda t: (-t[0], t[1], t[2].code))
        out = [a for _, _, a in rows]
        if self.max_per_round > 0:
            out = out[:self.max_per_round]
        return out


def build(cfg: dict | None = None) -> SpiritIndexRule:
    """按配置构造规则（与其它规则一致的工厂入口）。"""
    return SpiritIndexRule(cfg)


#: 模块级单例，供 ``RULE_MODULES`` 自动发现使用。注意它的状态在用例间会共享；
#: 测试请用 ``build()`` 构造独立实例。
RULE = SpiritIndexRule()
