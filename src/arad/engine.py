"""引擎：轮询 → 状态维护 → 规则求值 → 去重 → 通知/入库。

线程模型
--------
* 主循环单线程调用 :meth:`Engine.poll_once`，规则因此无需考虑并发。
* Web 服务在独立线程读 ``AlertStore``；``EngineState`` 只被主循环写，
  读取方（store）容忍读到"稍微过时"的数据，不做加锁以免拖慢行情线程。
"""
from __future__ import annotations

import importlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Iterable

from .capabilities import RoundObservationSet, capabilities_for
from .config import PROJECT_ROOT, Settings, load_settings, load_watchlist
from .filters import Filters
from .models import Alert, Quote, Snapshot
from .rules.base import Rule, RuleContext
from .session import OBSERVABLE, SessionPhase, TradingCalendar
from .store import AlertStore

__all__ = ["EngineState", "AlertBus", "SourceManager", "Engine", "run_forever", "build_rules", "build_notifiers"]

log = logging.getLogger("arad.engine")

# 规则模块名（顺序即求值顺序）
RULE_MODULES = (
    "tick_surge", "limit_board", "volume_burst", "unusual",
    # ---- 短线精灵信号（默认 enabled=false，见各模块 DEFAULTS）----
    "spirit_price", "spirit_order", "spirit_index",
)
NOTIFIER_MODULES = ("console", "file_jsonl", "webhook", "serverchan", "dingtalk", "feishu", "windows_toast")

#: provider 事件时间超前多少秒算"不可信"（超过就整只丢弃，见 EngineState.update）。
#: 取 120s：足以容纳正常的 NTP 抖动与交易所时间戳精度，又能挡住真正跑飞的
#: 未来数据。IT-P0-002。
FUTURE_TOLERANCE_SECONDS = 120.0

#: IT-P1-TIME-ROLE-003：首见码的**保守**陈旧下限（秒）。
#:
#: 修复前 ``_admit_time()`` 对任何早于 now 的 ts 都无条件接受，首见码甚至接受
#: 数天前的时间戳 —— 跨源切换（新码首现）时可能把 3 天前的价格当新鲜行情。
#:
#: 取值必须**保守**：A 股冷门股可能几十分钟才成交一次，它的 ``ts`` 表示"最后
#: 成交时间"，本身就是旧的。若下限设成几分钟，会把正常冷门股的报价全部误杀。
#: 4 小时 = 一个交易日的上午+下午连续竞价时长，足以挡住"隔日/数日前"的陈旧
#: 数据，又不会碰到当日的最后成交时间。
#:
#: 依赖 provider 时间角色的真实语义（见 ``source_time_contract.json``）—— 若
#: 某源根本不是"事件时间"，应先在该合同里标 ``unknown``，而不是靠这个下限兜底。
STALE_TOLERANCE_SECONDS = 4 * 3600.0

#: 默认路由名。个股请求走它；``EngineState`` 的兼容视图也指向它。
DEFAULT_ROUTE = "stocks"

#: IT-P1-TIME-ROLE-003-R1 / WP03：各来源的 provider 时间语义策略。
#:
#: 由 ``docs/audits/intraday/source_time_contract.json`` 驱动 —— 那份合同对三家
#: 都写 ``role=unknown``、``freshness_allowed=false``，因为仓内**没有**权威字段
#: 规范能证明这些字段是 event time / publish time / last trade time。
#:
#: 于是这里必须与合同一致：``freshness_allowed=false`` 的来源**不做** hard stale
#: reject，只记 age 诊断。上一轮（``100e06a``）直接用 provider ts 做 4 小时硬拒绝
#: 是**与自己的合同矛盾**的 —— 本轮下调。
#:
#: 将来某来源拿到权威语义（例如确认某字段确为 snapshot publish time）时，
#: 把该来源的 ``freshness_allowed`` 打开，硬拒绝能力已就绪（见 ``_admit_time``
#: 的 ``freshness_allowed`` 参数），无需再改结构。
#:
#: ``tests/test_time_policy_matrix.py`` 把本表与合同 JSON 绑死，防止两者漂移。
TIME_POLICY: dict[str, dict[str, bool]] = {
    "tencent": {"freshness_allowed": False, "strict_ordering_allowed": True},
    "sina": {"freshness_allowed": False, "strict_ordering_allowed": True},
    "eastmoney": {"freshness_allowed": False, "strict_ordering_allowed": True},
}

#: 未知来源的兜底：**同样不允许**用 provider ts 判新鲜度。
#: 默认从严（不给未登记的来源任何"权威时间"假设）。
DEFAULT_TIME_POLICY: dict[str, bool] = {
    "freshness_allowed": False,
    "strict_ordering_allowed": True,
}


def time_policy_for(source_name: str) -> dict[str, bool]:
    """按来源名取时间策略；未登记的来源用 ``DEFAULT_TIME_POLICY``。"""
    return TIME_POLICY.get(str(source_name or "").strip().lower(),
                           DEFAULT_TIME_POLICY)


# ==========================================================================
# 状态
# ==========================================================================
@dataclass
class EngineState:
    """行情历史与派生指标的共享容器（规则通过它读窗口数据）。"""

    quotes: dict[str, Quote] = field(default_factory=dict)
    history: dict[str, deque] = field(default_factory=dict)
    first_seen: dict[str, Quote] = field(default_factory=dict)
    last_price: dict[str, float] = field(default_factory=dict)
    #: ``(route, code) -> 已准入观测量的事件时间上界``（秒）。乱序判定的**显式**水位线。
    #: 必须与 ``history`` 解耦：``history`` 只在价格/量变化时追加（服务窗口
    #: 采样），若借用它的尾部时间当水位线，平价新鲜观测就不推进水位线，
    #: 迟到的旧点会被误准入（IT-P0-002-R2）。
    #:
    #: IT-P1-TIME-ROLE-004：键必须**带 route**。个股与指数是两个独立的数据流，
    #: 各有自己的 serving source 与切源时机。用全局 ``code -> ts`` 会有两个后果：
    #: ① 指数路由自己切源时不开 epoch，指数新源首包被旧水位线误杀；
    #: ② 个股切源会把**指数**的水位线一起清掉，指数那边真实的乱序就钻过去。
    #: 另外 ``000001`` 既是个股（平安银行）又是指数（上证指数），**会撞码** ——
    #: 所以带 route 不只是为了切源，key 本身就必须区分。
    accepted_watermark_by_route: dict[tuple[str, str], float] = field(
        default_factory=dict)
    #: IT-P1-TIME-ROLE-003-R1：``(route, code) -> 该 route 当前 epoch 内是否见过``。
    #:
    #: 陈旧下限只对"本 epoch 首见"生效。若沿用全局 ``first_seen``，切源后同一
    #: 代码在全局表里"已见过"，新 epoch 的首包就绕过了陈旧下限（实测 3 天前的
    #: 首包被接受）。按 route 分账后，新 epoch 的该码重新算"首见"。
    seen_in_epoch: dict[tuple[str, str], bool] = field(default_factory=dict)
    #: ``route -> 当前 epoch 标签``。A→B→A 是**三个** epoch，所以存标签而非布尔；
    #: 同一来源连续上报是幂等的。
    source_epochs: dict[str, str] = field(default_factory=dict)
    #: ``route -> 当前供数源的名字``（用于查 ``source_time_contract.json`` 的时间策略）。
    #: 与 ``source_epochs`` 的标签分开存：标签是"名字#下标"的复合标识，直接用标签
    #: 查策略会查不到（策略表按纯名字登记）。
    source_names: dict[str, str] = field(default_factory=dict)
    #: ``code -> 最近一次算出的 age 诊断``（**秒**，provider ts 相对接收时刻有多旧）。
    #:
    #: IT-P1-TIME-ROLE-003-R1：合同 ``freshness_allowed=false`` 时不做硬拒绝，
    #: 但**必须**留下这个诊断 —— 否则"该源时间是否可信"就完全无从观察，
    #: 将来某源拿到权威语义时也没有历史依据可查。
    time_age_seconds: dict[str, float] = field(default_factory=dict)
    #: 诊断计数：``route -> 陈旧包出现次数``（未被拦截的那些）。
    stale_diagnosed: dict[str, int] = field(default_factory=dict)
    #: IT-P1-TIME-ROLE-002：``code -> provider 原始 ts``（秒）。
    #:
    #: 轻微超前（时钟抖动内）的点会被夹到 ``now`` 入库 —— 那是既有且被测试
    #: 固定的契约（``test_engine_time_admission.py`` 断言 ``h[-1][0] == EP``），
    #: 保留它以免把未来点喂进窗口。但**原始值不能因此消失**：这里留痕，供
    #: 诊断"该源的时间是否可信"与跨源对账用。
    provider_ts_raw: dict[str, float] = field(default_factory=dict)
    #: IT-P1-TIME-ROLE-002：``code -> 实际用于入库的生效时间``（秒）。
    effective_event_time: dict[str, float] = field(default_factory=dict)
    #: IT-P1-TIME-ROLE-002：``code -> 本地接收时刻``（秒）。
    #: 与 ``effective_event_time`` 分开，跨源比较才有依据。
    received_at: dict[str, float] = field(default_factory=dict)
    day_open: dict[str, float] = field(default_factory=dict)
    last_alert: dict[str, float] = field(default_factory=dict)
    #: cooldown_key -> 上次放行时刻。用于"同类告警至少间隔 N 秒"的时间距离判断，
    #: 弥补 key 时间桶跨桶只需 1 秒的缺陷。
    last_cooldown: dict[str, float] = field(default_factory=dict)
    universe: dict[str, Quote] = field(default_factory=dict)
    session: SessionPhase = SessionPhase.CLOSED
    seq: int = 0
    stats: dict[str, int] = field(default_factory=dict)
    history_len: int = 360

    # ---- 窗口查询 -----------------------------------------------------
    def window(self, code: str, seconds: float, now_epoch: float) -> list[tuple[float, float, float]]:
        """返回 ``[now-seconds, now]`` 内的 (ts, price, cum_volume_lots) 列表（时间升序）。

        IT-P0-002：这里以前只过滤 ``p[0] >= cutoff``，**没有上界**。文档说
        是闭区间 ``[now-seconds, now]``，实现却允许 ``p[0] > now`` 的未来点
        进入窗口，于是 provider 时间超前时 ``price_change`` 会算出"还没发生
        的涨幅"。现在补上 ``<= now_epoch`` 上界，并把结果按时间排序 ——
        deque 是按**插入序**追加的，迟到的乱序点会排在尾部，导致
        ``pts[0]/pts[-1]`` 首尾倒挂、涨跌幅算反（见下面 update 的准入）。
        """
        h = self.history.get(code)
        if not h:
            return []
        cutoff = now_epoch - float(seconds)
        return sorted(
            (p for p in h if cutoff <= p[0] <= now_epoch),
            key=lambda p: p[0],
        )

    def price_change(self, code: str, seconds: float, now_epoch: float) -> float | None:
        """窗口内涨跌幅（%），以窗口内首个价格为基准。数据不足返回 None。"""
        pts = self.window(code, seconds, now_epoch)
        if len(pts) < 2:
            return None
        first, last = pts[0][1], pts[-1][1]
        if first <= 0:
            return None
        return (last / first - 1.0) * 100.0

    def volume_delta(self, code: str, seconds: float, now_epoch: float) -> float:
        """窗口内成交手数增量（负数截断为 0）。"""
        pts = self.window(code, seconds, now_epoch)
        if len(pts) < 2:
            return 0.0
        return max(0.0, pts[-1][2] - pts[0][2])

    def price_at(self, code: str, seconds: float, now_epoch: float) -> float | None:
        """窗口内最早的价格（用于自定义计算）。"""
        pts = self.window(code, seconds, now_epoch)
        return pts[0][1] if pts else None

    def peak(self, code: str, seconds: float, now_epoch: float) -> float | None:
        pts = self.window(code, seconds, now_epoch)
        return max((p[1] for p in pts), default=None)

    def trough(self, code: str, seconds: float, now_epoch: float) -> float | None:
        pts = self.window(code, seconds, now_epoch)
        return min((p[1] for p in pts), default=None)

    # ---- 写入 ---------------------------------------------------------
    def update(self, quotes: Iterable[Quote], now: datetime,
               *, route: str = DEFAULT_ROUTE) -> dict[str, Quote]:
        """把一轮快照并入状态，**返回本轮真正被准入的标的**。

        曾经有个 ``eligible=`` 关键字参数，但函数体从未读过它 —— 调用方以为
        "只更新合格标的"会生效，实际全量写入。与其留一个静默失效的承诺，
        不如删掉：粗筛由 ``poll_once`` 在读取侧做（见 ``eligible`` 字典）。

        IT-P0-002：provider 时间质量必须**写前准入**。旧代码先把 quote 写进
        ``quotes/history``，之后遇到 ``q.ts > now`` 只执行 ``pass`` —— 那是空
        操作，超前数据其实已经污染了状态（``price_change`` 会算出真实世界还
        没发生的涨幅）。现在改为写前判断：

        * ``q.ts > now + FUTURE_TOLERANCE``（远超时钟偏差）→ 整只丢弃；
        * 迟到且比已记录的最后一点还旧的观测 → 不覆盖 latest cache、
          不追加 history、不更新 ``last_price``，计入
          ``stats["t_reject:out_of_order"]``；
        * 轻微超前（时钟抖动范围内）→ 把时间戳夹到 ``now``，保留数据。

        IT-P0-002-R1：返回值是**准入结果的单一事实来源**。上一版修在 State
        内部，但 ``poll_once()`` 随后又从原始 ``quotes`` 重建 Snapshot ——
        被这里拒绝的 far-future 照样进了规则，迟到旧价照样覆盖了缓存。
        现在调用方必须消费本函数的返回值，而不是自己重算。

        IT-P0-002-R2：乱序水位线必须是**独立显式**的 ``accepted_watermark``。
        以前借用 ``history[-1][0]``，而 ``history`` 只在价格/累计量**变化**时
        追加（它是窗口采样，语义不该改）。于是"平价新鲜观测"（同价同量）虽
        推进了 ``quotes``/``last_price``，却**不推进水位线**；下一个真正迟到
        的点因 ``q_ep >= h[-1][0]`` 被放行，覆盖缓存并让时间序列尾部倒挂。
        现在水位线在**准入成功时无条件推进**，与 history 的追加策略解耦。

        IT-P1-TIME-ROLE-004：水位线与"本 epoch 是否见过"都按 **route** 分账。
        个股与指数是两个独立数据流，各有自己的 serving source 与切源时机。
        """
        ep = now.timestamp()
        route = str(route or DEFAULT_ROUTE)
        self.seq += 1
        admitted: dict[str, Quote] = {}
        # IT-P1-TIME-ROLE-003-R1：陈旧硬拒绝是否可用，由该 route 的供数源的
        # 时间语义合同决定（三家目前都是 freshness_allowed=false）。
        _policy = time_policy_for(self.source_names.get(route, ""))
        _fresh_ok = bool(_policy.get("freshness_allowed", False))
        for q in quotes:
            if q.price <= 0:
                continue

            wm_key = (route, q.code)
            # --- 写前时间准入 -------------------------------------------
            # "本 route 本 epoch 是否首见"在**准入之前**取：
            # 陈旧下限只对首见码生效（IT-P1-TIME-ROLE-003）。
            ts_ok, q_ep, why = self._admit_time(
                q, now, ep, first_seen=wm_key not in self.seen_in_epoch,
                freshness_allowed=_fresh_ok)
            if not ts_ok:
                self.stats[f"t_reject:{why}"] = self.stats.get(f"t_reject:{why}", 0) + 1
                continue

            # IT-P1-TIME-ROLE-002：留痕 provider 原始 ts，并区分"生效时间"与
            # "接收时间"。入库仍用夹过的 q_ep（既有契约），但原始值不丢。
            if q.ts is not None:
                try:
                    _raw = float(q.ts.timestamp())
                    self.provider_ts_raw[q.code] = _raw
                    # IT-P1-TIME-ROLE-003-R1：无论是否拦得住，都留下 age 诊断。
                    # 合同 freshness_allowed=false 时这是**唯一**可观察"该源时间
                    # 是否可信"的地方，不能因为"不拦"就不记。
                    _age = ep - _raw
                    self.time_age_seconds[q.code] = _age
                    if _age > STALE_TOLERANCE_SECONDS:
                        self.stale_diagnosed[route] = \
                            self.stale_diagnosed.get(route, 0) + 1
                except (AttributeError, OSError, ValueError):
                    pass
            self.effective_event_time[q.code] = q_ep
            self.received_at[q.code] = ep

            # --- 乱序准入：用显式水位线，必须在任何写入之前判定 ----------
            # 水位线在每次准入成功时无条件推进（见函数末尾），因此"平价新鲜
            # 观测"也会推高它，迟到的旧点就再也钻不过去（IT-P0-002-R2）。
            # IT-P1-TIME-ROLE-001/004：水位线**按 (route, source epoch) 分段**，
            # 跨 epoch 的旧时间戳不算"倒退"（见 begin_source_epoch）。
            wm = self.accepted_watermark_by_route.get(wm_key)
            if wm is not None and q_ep < wm:
                self.stats["t_reject:out_of_order"] = \
                    self.stats.get("t_reject:out_of_order", 0) + 1
                continue

            self.quotes[q.code] = q
            if q.code not in self.first_seen:
                self.first_seen[q.code] = q
            if q.open > 0 and q.code not in self.day_open:
                self.day_open[q.code] = q.open
            # 只在价格/成交量真正变化时追加，避免重复点污染窗口
            prev = self.last_price.get(q.code)
            h = self.history.get(q.code)
            if h is None:
                h = deque(maxlen=max(int(self.history_len), 30))
                self.history[q.code] = h
            if prev is None or prev != q.price or not h or h[-1][2] != q.volume_lots:
                h.append((q_ep, q.price, q.volume_lots))
            self.last_price[q.code] = q.price
            # 无条件推进水位线（与 history 是否追加无关）
            self.accepted_watermark_by_route[wm_key] = (
                q_ep if wm is None else max(wm, q_ep))
            self.seen_in_epoch[wm_key] = True
            admitted[q.code] = q
        return admitted

    def _admit_time(self, q: Quote, now: datetime, ep: float,
                    *, first_seen: bool = False,
                    freshness_allowed: bool = False) -> tuple[bool, float, str]:
        """写前时间准入。返回 ``(是否接纳, 用于入库的时间戳, 拒绝原因)``。

        事件时间缺失时按"接收时间"处理（视作准时），这是兼容既有行为：
        多数 source 不填 ``ts``。

        ``first_seen`` 为真时才施加**陈旧下限**（IT-P1-TIME-ROLE-003）：首见码
        如果带着数天前的 ``ts``，说明这个源的时间轴与本地时钟不同源（或数据
        本身陈旧），此时把它当新鲜行情接纳是错的。**已有水位线的码不走这条**：
        它的陈旧由 epoch 内乱序判定负责，重复加下限只会互相干扰。

        IT-P1-TIME-ROLE-003-R1 / WP03：``freshness_allowed`` 来自
        ``source_time_contract.json``（经 ``time_policy_for``）。合同对三家都写
        ``false``，所以**默认不对 provider ts 做硬陈旧拒绝** —— 只记 age 诊断。
        硬拒绝能力保留在代码里，等某个来源拿到权威时间语义后打开即可，
        不需要再改结构。
        """
        if q.ts is None:
            return True, ep, ""
        try:
            q_ep = float(q.ts.timestamp())
        except (AttributeError, OSError, ValueError):
            return True, ep, ""
        if q_ep > ep + FUTURE_TOLERANCE_SECONDS:
            return False, q_ep, "future"
        # 陈旧判定只在两个条件**同时**成立时才硬拒绝：
        #   ① 该来源的时间语义允许判新鲜度（合同 freshness_allowed）；
        #   ② 这是本 route 本 epoch 的首见包（已有水位线的码由乱序判定负责）。
        # 否则只记诊断，不拦截 —— 这与 source_time_contract.json 一致。
        if first_seen and freshness_allowed and q_ep < ep - STALE_TOLERANCE_SECONDS:
            return False, q_ep, "stale"
        if q_ep > ep:
            return True, ep, ""          # 轻微超前 -> 夹到 now，保留数据
        return True, q_ep, ""

    def begin_source_epoch(self, route: str, source: str | None = None, *,
                           source_name: str | None = None) -> bool:
        """IT-P1-TIME-ROLE-001/004：某条 **route** 的供数源变化时开一个新 epoch。

        签名是 ``(route, source)``。兼容旧的单参调用
        ``begin_source_epoch("sina")`` —— 那种写法会被当成
        ``route=DEFAULT_ROUTE, source="sina"``（见下）。

        返回是否真的开了新 epoch。同一来源重复上报是**幂等**的（返回 False）——
        否则每次轮询都把水位线清掉，epoch 内的真实乱序就再也拒不住了。

        由调用方传入**实际 serving source**（``SourceManager.serving_of(route)``），
        而不是 ``SourceManager.current``：热备期间由备用源实际供数，
        ``current`` 还是主源，按它分 epoch 会把两段混成一段。

        为什么按 route 分账（IT-P1-TIME-ROLE-004）：个股与指数是两个独立数据流，
        各有自己的切源时机。全局分账会有两个后果 —— 指数自己切源不开 epoch
        （新源首包被误杀），个股切源清掉指数水位线（指数真实乱序被放行）。
        """
        # 兼容旧签名 begin_source_epoch("sina")：单参时它是 source，route 取默认。
        if source is None:
            route, source = DEFAULT_ROUTE, route
        route = str(route or DEFAULT_ROUTE)
        tag = str(source or "")
        # 供数源的**纯名字**单独记一份，用于查 source_time_contract.json 的
        # 时间策略表（策略按纯名字登记，不是"名字#下标"的复合标签）。
        self.source_names[route] = str(source_name or tag.split("#")[0])
        if tag == self.source_epochs.get(route):
            return False
        self.source_epochs[route] = tag
        # 新 epoch：**该 route** 的水位线与"本 epoch 首见"标记都要重新起算。
        #
        # 清 first_seen 的分账表是关键（IT-P1-TIME-ROLE-003-R1）：只清水位线
        # 会让该码在新 epoch 被当成"全局非首见"，于是 3 天前的首包绕过陈旧下限。
        for key in [k for k in self.accepted_watermark_by_route if k[0] == route]:
            self.accepted_watermark_by_route.pop(key, None)
        for key in [k for k in self.seen_in_epoch if k[0] == route]:
            self.seen_in_epoch.pop(key, None)
        return True

    # ---- 向后兼容视图 ---------------------------------------------------
    @property
    def accepted_watermark(self) -> dict[str, float]:
        """**兼容视图**：默认 route（个股）的 ``code -> ts`` 快照。

        水位线的真实存储是 ``accepted_watermark_by_route[(route, code)]``
        （IT-P1-TIME-ROLE-004）。这里给旧调用方一个只读快照，语义等价于
        "个股路由的水位线"。新代码请直接用 by_route 表。
        """
        return {code: ts for (r, code), ts in self.accepted_watermark_by_route.items()
                if r == DEFAULT_ROUTE}

    @property
    def source_epoch(self) -> str:
        """**兼容视图**：默认 route（个股）的 epoch 标签。"""
        return self.source_epochs.get(DEFAULT_ROUTE, "")

    def prune(self, keep_codes: set[str] | None = None) -> int:
        """丢弃不再关注的股票历史，控制内存。返回清理条数。"""
        if keep_codes is None:
            return 0
        drop = [c for c in self.history if c not in keep_codes]
        for c in drop:
            self.history.pop(c, None)
            self.last_price.pop(c, None)
            # 水位线必须与 history 一起回收，否则 prune 后残留的水位线
            # 会让重新关注的代码被旧水位线误判为"迟到"。
            # IT-P1-TIME-ROLE-004：水位线/首见标记按 route 分账，**所有** route
            # 都要一起回收。
            for key in [k for k in self.accepted_watermark_by_route
                        if k[1] == c]:
                self.accepted_watermark_by_route.pop(key, None)
            for key in [k for k in self.seen_in_epoch if k[1] == c]:
                self.seen_in_epoch.pop(key, None)
            self.first_seen.pop(c, None)
            # IT-P1-TIME-ROLE-002 的留痕表必须与 history 一起回收，
            # 否则长时间运行会随着关注池轮换而无界增长。
            self.provider_ts_raw.pop(c, None)
            self.effective_event_time.pop(c, None)
            self.received_at.pop(c, None)
            self.time_age_seconds.pop(c, None)
        # 冷却表按时间过期
        cutoff = time.time() - 3600
        stale = [k for k, v in self.last_alert.items() if v < cutoff]
        for k in stale:
            self.last_alert.pop(k, None)
        return len(drop)


# ==========================================================================
# 去重
# ==========================================================================
@dataclass
class AlertBus:
    """跨规则统一去重。

    两层判断，缺一不可：

    1. **同 key**：``key`` 内含时间桶，同桶内完全相同的事件只放行一次；
    2. **同 cooldown_key 的时间距离**：``key`` 的桶边界落在固定墙上时钟网格上，
       跨桶只需 1 秒，所以单靠 key 会让两条告警只隔几秒就都发出去。
       对声明了 ``cooldown_seconds`` 的告警，额外要求距上次同类告警至少间隔
       这么久。
    """

    state: EngineState
    window_seconds: float = 3600.0

    def accept(self, alert: Alert, now_epoch: float) -> bool:
        prev = self.state.last_alert.get(alert.key)
        if prev is not None and (now_epoch - prev) < self.window_seconds:
            return False

        # 时间距离约束（真正的"冷却 N 秒"）
        cd = float(getattr(alert, "cooldown_seconds", 0.0) or 0.0)
        ck = getattr(alert, "cooldown_key", "") or ""
        if cd > 0.0 and ck:
            last = self.state.last_cooldown.get(ck)
            if last is not None and (now_epoch - last) < cd:
                return False
            self.state.last_cooldown[ck] = now_epoch

        self.state.last_alert[alert.key] = now_epoch
        return True


# ==========================================================================
# 数据源故障转移
# ==========================================================================
#: 故障转移的记账路由。**必须分开**：个股请求有几千只、指数只有几只，
#: 共用一个失败计数器时，几乎不会失败的指数请求每轮都会把个股链路的失败
#: 计数清零，正式切换永远不会发生（IT-P1-007）。
ROUTE_STOCKS = "stocks"
ROUTE_INDEX = "index"
ROUTE_UNIVERSE = "universe"


class SourceManager:
    """主源 + 备用源，连续失败达阈值自动切换。

    失败计数按 **路由（route）** 分开记账（IT-P1-007）。原因：一轮
    ``poll_once`` 会发出两类 ``snapshots`` 请求 —— 全市场几千只的个股请求，
    和只含 ``index_codes`` 那几只的指数请求。指数请求几乎不会失败；若两条
    路由共用一个计数器，指数请求每轮成功都会把个股请求攒下的失败计数清零，
    后果是：个股数据一直由备用源提供、``health()`` 却始终显示主源 active，
    主源还每轮都被白试一次（延迟照付），正式切换永远不会发生。
    按路由分开后，「个股链路连续失败」才能如实累计到阈值并触发切换。
    """

    def __init__(self, sources: list[Any], *, threshold: int = 3):
        if not sources:
            raise ValueError("SourceManager 需要至少一个数据源")
        self.sources = sources
        self.threshold = max(int(threshold), 1)
        self.idx = 0
        self._fails: dict[str, int] = {}      # route -> 连续由备用源服务/全失败次数
        self._serving: dict[str, int] = {}    # route -> 最近一次实际供数的源下标
        self.history: list[dict] = []

    @property
    def current(self):
        return self.sources[self.idx]

    @property
    def fails(self) -> int:
        """所有路由的失败计数之和（保留旧接口，看板/测试仍可用）。"""
        return sum(self._fails.values())

    @property
    def fails_by_route(self) -> dict[str, int]:
        """各路由的连续失败计数快照。"""
        return dict(self._fails)

    def serving_of(self, route: str) -> int | None:
        """某路由最近一次实际供数的源下标（``None`` = 还没有成功过）。"""
        return self._serving.get(route)

    def _switch(self) -> None:
        if len(self.sources) > 1:
            self.idx = (self.idx + 1) % len(self.sources)
            self._fails.clear()
            log.warning("数据源切换 -> %s", getattr(self.current, "name", "?"))

    def _bump(self, route: str) -> int:
        self._fails[route] = self._fails.get(route, 0) + 1
        return self._fails[route]

    def call(self, method: str, *args, route: str | None = None):
        """调用当前源；失败则按顺序尝试备用源。

        语义：
        * 每个源自身已实现内部重试，本方法对每个源只尝试一次。
        * 主源失败、备用源成功 => 本次返回备用源数据（热备），并累加**该路由**的
          失败计数；连续失败达到 ``threshold`` 后把该备用源提升为主源（正式切换）。
        * 全部失败 => 抛出最后一个异常。

        ``route`` 把不同用途的调用分开记账（缺省按方法名）。个股请求与指数请求
        必须传不同的 route，否则小额指数请求的成功会把个股链路的失败计数清零，
        正式切换永远不会发生（IT-P1-007）。
        """
        route = route or method
        order = [self.idx] + [i for i in range(len(self.sources)) if i != self.idx]
        last_exc: Exception | None = None
        for i in order:
            src = self.sources[i]
            try:
                out = getattr(src, method)(*args)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                log.warning("数据源 %s.%s 失败: %s", getattr(src, "name", "?"), method, exc)
                continue
            self._serving[route] = i
            if i == self.idx:
                self._fails[route] = 0
            else:
                n = self._bump(route)
                log.info("路由 %s 已由备用源 %s 提供服务（连续 %d/%d）",
                         route, getattr(src, "name", "?"), n, self.threshold)
                if n >= self.threshold:
                    self.idx = i
                    self._fails.clear()
                    log.warning("数据源已正式切换 -> %s", getattr(src, "name", "?"))
            return out

        n = self._bump(route)
        if n >= self.threshold:
            self._switch()
        if last_exc:
            raise last_exc
        return None

    def health(self) -> list[dict]:
        out = []
        for pos, s in enumerate(self.sources):
            try:
                h = s.health()
            except Exception as exc:  # noqa: BLE001
                h = {"name": getattr(s, "name", "?"), "ok": False, "latency_ms": 0, "err": str(exc)}
            h = dict(h)
            h["active"] = (pos == self.idx)
            # 如实反映「谁在真正供数」：主源 active 而个股路由其实一直由备用源
            # 服务时，只看 active 会得出完全错误的结论（IT-P1-007）。
            h["serving_routes"] = sorted(r for r, i in self._serving.items() if i == pos)
            out.append(h)
        return out

    def route_health(self) -> dict:
        """按路由的故障转移状况（诊断用，不属数据源契约）。"""
        return {
            "threshold": self.threshold,
            "primary": getattr(self.current, "name", "?"),
            "fails_by_route": dict(self._fails),
            "serving": {r: getattr(self.sources[i], "name", "?")
                        for r, i in self._serving.items()},
        }


# ==========================================================================
# 规则 / 通知器装载
# ==========================================================================
def build_rules(settings: Settings) -> list[Rule]:
    """按配置装载启用的规则；缺失/损坏的规则模块只告警不致命。"""
    rules: list[Rule] = []
    for name in RULE_MODULES:
        cfg = settings.rule(name)
        if not cfg.get("enabled", True):
            log.info("规则 %s 已禁用", name)
            continue
        try:
            mod = importlib.import_module(f".rules.{name}", package="arad")
        except Exception as exc:  # noqa: BLE001
            log.error("规则模块 %s 载入失败: %s", name, exc)
            continue
        try:
            rule = mod.build(cfg) if hasattr(mod, "build") else getattr(mod, "RULE")
        except Exception as exc:  # noqa: BLE001
            log.error("规则 %s 构造失败: %s", name, exc)
            continue
        rules.append(rule)
        log.info("规则已启用: %s", getattr(rule, "name", name))
    return rules


def build_notifiers(settings: Settings) -> list[Any]:
    """按 ``notify.enabled`` 装载通知器。"""
    out: list[Any] = []
    enabled = settings.get("notify.enabled") or []
    if isinstance(enabled, str):
        enabled = [enabled]
    for name in enabled:
        module = NOTIFIER_MAP.get(name, name)
        try:
            mod = importlib.import_module(f".notifiers.{module}", package="arad")
        except Exception as exc:  # noqa: BLE001
            log.error("通知器 %s 载入失败: %s", name, exc)
            continue
        cfg = dict(settings.section("notify").get(name) or {})
        # 子节里可能有同名 dict（webhook/dingtalk 等）
        cfg.setdefault("dry_run", settings.dry_run)
        try:
            notifier = mod.build(cfg)
        except Exception as exc:  # noqa: BLE001
            log.error("通知器 %s 构造失败: %s", name, exc)
            continue
        out.append(notifier)
        log.info("通知器已启用: %s", name)
    return out


NOTIFIER_MAP = {
    "file": "file_jsonl",
    "console": "console",
    "webhook": "webhook",
    "serverchan": "serverchan",
    "dingtalk": "dingtalk",
    "feishu": "feishu",
    "windows_toast": "windows_toast",
}


# ==========================================================================
# 引擎
# ==========================================================================
class Engine:
    """盘中雷达主引擎。"""

    def __init__(
        self,
        source,
        *,
        settings: Settings | None = None,
        rules: list[Rule] | None = None,
        notifiers: list[Any] | None = None,
        store: AlertStore | None = None,
        watchlist: list[str] | None = None,
        calendar: TradingCalendar | None = None,
        logger: logging.Logger | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self.settings = settings or load_settings()
        self.calendar = calendar or TradingCalendar.load()
        # 可注入时钟：回放/测试用它把"现在"固定在模拟时刻，
        # 避免真实墙钟（例如休市日）污染交易时段判定。
        self._now_fn = now_fn or datetime.now
        self.state = EngineState(
            history_len=int(self.settings.get("poll.history_len", 360) or 360))
        self.bus = AlertBus(self.state)
        # ⚠ ``series_len`` 必须显式传进去：``AlertStore.__init__`` 的默认值是
        # 硬编码的 240，它**不会**自己去读 settings。早先这里只写了
        # ``AlertStore(self.settings, ...)``，于是 ``storage.series_len``
        # 这个配置键被静默忽略 —— 改了配置没有任何效果，也没有任何报错。
        # ``or 240`` 是为了让 YAML 里的 ``series_len:``（空值 -> None）回落到默认，
        # 注意 0 会被 ``or`` 吃掉，但 0 不是合法值（``max(0, 2)`` 也不合理）。
        #
        # IT-P1-ACCEPTANCE-GATE-RINGBUFFER-001：``web.max_alerts`` 同样是配置接线
        # 残留 —— YAML 里有 ``web.max_alerts: 300``，但这里从没传给 AlertStore，
        # 于是它永远用 ``__init__`` 里的硬编码默认 300。改配置没有任何效果。
        # 这条**不只是洁癖**：``recent_alerts(n)`` 被测试当累计分母用过，
        # 而它是 deque(maxlen=max_alerts) —— 分母被 ring buffer 悄悄截断，
        # 告警 >300 时断言会**假红**。累积真值必须用 ``alerts_total()``。
        self.store = store if store is not None else AlertStore(
            self.settings, calendar=self.calendar,
            max_alerts=int(self.settings.get("web.max_alerts", 300) or 300),
            series_len=int(self.settings.get("storage.series_len", 240) or 240))
        self.store.attach(self)
        self.log = logger or log

        self.sources = self._wrap_sources(source)
        self.source = self.sources.current
        self.rules = rules if rules is not None else build_rules(self.settings)
        self.notifiers = notifiers if notifiers is not None else build_notifiers(self.settings)
        self.filters = Filters.from_cfg(self.settings.filters)
        self.watchlist = list(watchlist if watchlist is not None else load_watchlist())
        self.focus = tuple(self._load_focus())
        self.ignore = set(self._load_ignore())

        self._codes_raw: list[str] = []
        # 外部（回放/测试/自选降级）直接赋值 _codes 后置 True，表示"股票池已定，
        # 不要再自己去联网刷新"。否则 poll_once -> _maybe_refresh_universe 会因为
        # _universe_refreshed_at=0.0 判定过期，发起真实网络请求并**覆盖**注入的代码，
        # 让离线测试变成依赖网络、且结果随机。
        self._codes_pinned = False
        self._universe_refreshed_at: float = 0.0
        #: 最近一次成功刷新股票池的完整性元数据（IT-P1-006）。
        self._universe_meta: dict[str, Any] = {}
        self._poll_count = 0
        self._errors = 0
        self._stop = False
        self.running = False
        self._universe_src = None          # 股票池专用源（懒构造）

        # 指数符号：必须带 sh/sz 前缀（sh000001 才是上证指数，裸 000001 是
        # 平安银行）。单独抓、不混进 _codes —— 指数走 filters.accept 会被
        # exclude_boards=["index"] 拦掉，混进去只会白白占用配额。
        self.index_codes: list[str] = self._load_index_codes()

    # ------------------------------------------------------------------
    @property
    def _codes(self) -> list[str]:
        return self._codes_raw

    @_codes.setter
    def _codes(self, codes: list[str]) -> None:
        """赋值即视为"股票池已确定"（pin），后续不再自动联网刷新。"""
        self._codes_raw = list(codes)
        self._codes_pinned = bool(codes)

    # ------------------------------------------------------------------
    def _wrap_sources(self, source) -> SourceManager:
        threshold = int(self.settings.get("sources.failover_threshold", 3) or 3)
        if isinstance(source, SourceManager):
            return source
        if source is None:
            # 用完整故障转移链（主源 + sources.fallback），否则配了 fallback 也不生效
            return SourceManager(build_source_chain(self.settings), threshold=threshold)
        if isinstance(source, (list, tuple)):
            return SourceManager(list(source), threshold=threshold)
        return SourceManager([source], threshold=threshold)

    @property
    def _wants_indices(self) -> bool:
        """是否有已启用的规则需要指数行情。

        只看规则名是否含 ``index`` 太脆；直接问规则自身声明的
        ``wants_indices``，没有该属性的规则一律视为不需要。
        """
        return any(getattr(r, "wants_indices", False) for r in self.rules)

    def _load_index_codes(self) -> list[str]:
        """读 ``poll.index_codes``，只保留**带前缀**的合法符号。

        配置里写了裸 ``000001`` 会被丢掉并告警：它到底是上证指数还是平安银行
        无法从 6 位数字判断，猜错的代价是"拿到一份看起来正常的错误数据"，
        比直接不监控更糟。
        """
        raw = self.settings.get("poll.index_codes", None)
        if not raw:
            return []
        if isinstance(raw, str):
            raw = [raw]
        out: list[str] = []
        seen: set[str] = set()
        for item in raw:
            s = str(item or "").strip().lower()
            prefix, bare = (s[:2], s[2:]) if len(s) > 6 else ("", s)
            if prefix not in ("sh", "sz", "bj") or not (len(bare) == 6 and bare.isdigit()):
                self.log.warning(
                    "poll.index_codes 忽略 %r：指数必须写成带前缀的形式"
                    "（如 sh000001 上证指数、sz399001 深证成指）", item)
                continue
            sym = prefix + bare
            if sym not in seen:
                seen.add(sym)
                out.append(sym)
        return out

    def _fetch_indices(self) -> list[Quote]:
        """抓指数行情。失败只记日志，绝不影响个股主链路。

        只有**真的有规则要指数**时才发请求：``spirit_index`` 默认关闭，
        若不管规则就无脑抓，等于每轮白白多发 5 个代码的流量。
        """
        if not self.index_codes or not self._wants_indices:
            return []
        try:
            # route 必须与个股请求分开：指数只有几只、几乎不会失败，若共用计数器
            # 就会每轮把个股链路的失败计数清零（IT-P1-007）。
            return self.sources.call("snapshots", list(self.index_codes),
                                     route=ROUTE_INDEX)
        except Exception as exc:  # noqa: BLE001 - 指数是加分项，不能拖垮主循环
            self.log.warning("指数行情抓取失败（不影响个股）: %s", exc)
            return []

    def _load_focus(self) -> list[str]:
        try:
            from .config import load_focus

            return load_focus()
        except Exception:  # noqa: BLE001
            return []

    def _load_ignore(self) -> list[str]:
        try:
            from .config import load_ignore

            return load_ignore()
        except Exception:  # noqa: BLE001
            return []

    # ------------------------------------------------------------------
    # 股票池
    # ------------------------------------------------------------------
    def _universe_sources(self) -> list[Any]:
        """股票池来源链（按配置顺序）。

        ``sources.universe`` 可以是单个名字，也可以是**列表**。列表的意义：
        东财对部分网络会 RemoteDisconnected 限流，只配一个的话整轮股票池刷新
        就报废、系统退化成只盯自选股那 10 只 —— 盘中基本没用。配成
        ``["eastmoney", "sina"]`` 就能自动退到新浪行情中心（实测可列全 5563 只）。

        专用源不可用时退回主源链，保证至少还能用自选股跑起来。
        """
        raw = self.settings.get("sources.universe", "")
        names = [str(n).strip() for n in (raw if isinstance(raw, (list, tuple)) else [raw])
                 if str(n).strip()]
        if not names:
            return [self.sources]
        if getattr(self, "_universe_src", None) is None:
            chain: list[Any] = []
            for name in names:
                try:
                    chain.append(build_source(self.settings, name))
                except Exception as exc:  # noqa: BLE001
                    self.log.warning("股票池数据源 %s 构造失败，跳过: %s", name, exc)
            self._universe_src = chain or [self.sources]
        return self._universe_src

    def _universe_meta_of(self, src: Any, quotes: list[Any]) -> dict:
        """取某来源最近一次 ``universe()`` 的完整性元数据（IT-P1-006）。

        来源没实现 ``universe_info`` 时退化为「按条数判断」，绝不能因为拿不到
        元数据就把部分结果当完整结果用。
        """
        info: dict = {}
        fn = getattr(src, "universe_info", None)
        if callable(fn):
            try:
                got = fn()
                if isinstance(got, dict):
                    info = dict(got)
            except Exception as exc:  # noqa: BLE001 - 元数据拿不到不能拖垮刷新
                self.log.warning("读取 %s 的股票池元数据失败: %s",
                                 getattr(src, "name", src), exc)
        info.setdefault("returned", len(quotes))
        info.setdefault("complete", True)
        return info

    def refresh_universe(self) -> int:
        """刷新股票池代码列表（默认每 30 分钟一次）。

        按 ``sources.universe`` 的顺序依次尝试，采用第一个**可用**结果：

        * 完整结果（``complete=True``）**立即采用**，后续来源不再尝试；
        * 不完整结果（``complete=False``）只作为**兜底候选**，继续往后试，
          只有所有来源都没给出完整结果时才采用其中最大的那个 —— 且**绝不用它
          覆盖更大的已有股票池**（IT-P1-006）。

        ``complete`` 的语义是**传输口径**（服务端有没有把池子给全），具体三种
        不完整原因（见 ``sources/eastmoney.py``，WP01 已拆轴）：

        * 分页中途有失败页（``pages_failed > 0``）；
        * 翻页被 ``max_pages`` 截断（``truncated``）；
        * 行数缩水：``raw_unique_codes < transport_expected_total``。

        **停牌/无效行不再算不完整。** parser 按契约丢弃 ``price<=0`` 的行是
        **正常**的，它只影响 ``usable_coverage``（诊断轴），不影响
        ``transport_complete``。把两者混用会让"市场里有停牌股"被误判成
        "服务端少给了数据"，进而让引擎拒绝刷新已有股票池
        （IT-P1-COMPLETE-001-R1）。

        为什么必须这样：新浪 ``universe()`` 中途某页失败会返回前缀，东财被限流
        时也会少几页。没有完整性判断时，「5563 只的完整池」会被「3000 只的部分
        池」静默覆盖 —— 丢掉 2500 多只票却记为「刷新成功」，而且不报任何错。
        全失败才告警并保留原股票池（绝不清空 —— 清空会让引擎彻底停摆）。
        """
        last_exc: Exception | None = None
        partial: tuple[int, list[Any], dict, Any] | None = None
        for src in self._universe_sources():
            name = getattr(src, "name", src)
            try:
                quotes = (src.call("universe", route=ROUTE_UNIVERSE)
                          if isinstance(src, SourceManager)
                          else src.universe())
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                self.log.warning("股票池来源 %s 失败: %s", name, exc)
                continue
            if not quotes:
                continue
            meta = self._universe_meta_of(src, quotes)
            if not meta.get("complete", True):
                # 候选比较阶段用**纯函数**，不写 state.universe —— 部分结果
                # 可能因「比现有池更小」而被拒绝，拒绝后不该留下任何痕迹。
                cand = self._extract_codes(quotes)
                if not cand:
                    continue
                self.log.warning(
                    "股票池来源 %s 只拿到部分数据（%d 只，%s），继续尝试后续来源",
                    name, len(cand), meta.get("reason") or "未知原因")
                # 兜底候选取最大者，避免被更小的部分结果挤掉。
                if partial is None or len(cand) > partial[0]:
                    partial = (len(cand), quotes, meta, src)
                continue
            codes = self._record_universe(quotes)
            if not codes:
                continue
            # 直接写底层字段：这里拿到的是**真·全市场**，必须保持"未 pin"，
            # 否则 TTL 到期后不会再有下一次刷新（_codes 的 setter 会 pin）。
            self._codes_raw = codes
            self._codes_pinned = False
            self._universe_refreshed_at = time.time()
            self._universe_meta = meta
            self.log.info("股票池已刷新: %d 只（来源 %s）", len(codes), name)
            return len(codes)

        if partial is not None:
            n, pquotes, meta, src = partial
            name = getattr(src, "name", src)
            prev = len(self._codes_raw)
            if prev and n < prev:
                # 关键保护：不完整的部分池比现有池更小 -> 拒绝覆盖，保留现有池。
                # 否则 5563 只会被 3000 只静默替换，且记为「刷新成功」。
                self.log.warning(
                    "所有来源都只给出部分股票池；%s 仅 %d 只 < 现有 %d 只，"
                    "保留现有股票池以免缩小扫描范围", name, n, prev)
                return 0
            codes = self._record_universe(pquotes)
            self._codes_raw = codes
            self._codes_pinned = False
            self._universe_refreshed_at = time.time()
            self._universe_meta = meta
            self.log.warning("股票池已用**部分**结果刷新: %d 只（来源 %s，%s）",
                             len(codes), name, meta.get("reason") or "未知原因")
            return len(codes)

        if last_exc is not None:
            self.log.warning("所有股票池来源都失败，保留原股票池: %s", last_exc)
        return 0

    def _record_universe(self, quotes: list[Any]) -> list[str]:
        """记录股票池：填 ``state.universe`` / ``filters.list_dates``，返回代码列表。

        ``list_dates`` 用于新股过滤（``filters.min_list_days``）—— 只有东财提供
        ``list_date``，新浪这条链路拿不到，此时该字段为空、新股过滤自动失效
        （规则里对空值一律放行，不会误杀）。
        """
        codes = self._extract_codes(quotes)
        for q in quotes:
            code = getattr(q, "code", None) or (q.get("code") if isinstance(q, dict) else None)
            if code and not isinstance(q, dict):
                self.state.universe[str(code).strip()] = q
        if codes:
            self.filters.list_dates = {
                q.code: str(q.list_date or "")
                for q in self.state.universe.values()
                if getattr(q, "list_date", "")
            }
        return codes

    @staticmethod
    def _extract_codes(quotes: list[Any]) -> list[str]:
        """纯函数：从 quotes 里提取去重后的合法 6 位代码。

        与 ``_record_universe`` 分开是为了让「只做候选比较、尚未决定是否采用」
        的路径没有副作用 —— 被拒绝的部分结果不该污染 ``state.universe``。
        """
        codes: list[str] = []
        seen: set[str] = set()
        for q in quotes:
            code = getattr(q, "code", None) or (q.get("code") if isinstance(q, dict) else None)
            if not code:
                continue
            c = str(code).strip()
            if not (len(c) == 6 and c.isdigit()) or c in seen:
                continue
            seen.add(c)
            codes.append(c)
        return codes

    def _maybe_refresh_universe(self, *, force: bool = False) -> None:
        ttl = float(self.settings.get("poll.universe_refresh_seconds", 1800) or 1800)
        # 股票池被显式注入（回放/测试）时完全跳过联网刷新：
        # 既保证离线可跑，也避免真实行情把注入的剧本代码覆盖掉。
        if self._codes_pinned and not force:
            return
        if force or not self._codes or (time.time() - self._universe_refreshed_at) > ttl:
            self.refresh_universe()
        # 全市场股票池拿不到时（东财被限流/断网），退回自选股，
        # 否则 _codes 为空 -> poll_once 直接 return，系统会静默地什么都不做。
        if not self._codes and self.watchlist:
            self.log.warning(
                "股票池为空，降级为仅监控自选股 %d 只（全市场扫描暂不可用）",
                len(self.watchlist))
            self._codes = list(self.watchlist)
            self._universe_refreshed_at = time.time()

    # ------------------------------------------------------------------
    # 单轮
    # ------------------------------------------------------------------
    def now(self) -> datetime:
        """当前时间（可被注入时钟覆盖，回放模式下即模拟时刻）。"""
        return self._now_fn()

    def poll_once(self, *, force: bool = False) -> list[Alert]:
        """抓一次快照 → 更新状态 → 跑规则 → 去重 → 通知。返回本轮新告警。"""
        now = self.now()
        phase = self.calendar.phase(now)
        self.state.session = phase
        self.store.broadcast("phase", {"phase": phase.value})

        if self.settings.get("poll.idle_when_closed", True) and not force:
            # 可抓取窗口 = 连续竞价 + 两个集合竞价（OBSERVABLE）。
            # 静默期（09:25-09:30）与休市不抓：前者可撤单不可成交、价格冻结，
            # 抓了只是重复数据并稀释"急拉"速度。
            #
            # IT-P0-001：收盘集合竞价（14:57-15:00）**必须**留在这里 ——
            # 它虽是集合竞价、不再是连续竞价（故不在 CONTINUOUS），但价格
            # 确实在动，把它一并排除就是过度修正。以前这里写的是
            # `phase not in CONTINUOUS and phase is not PRE_OPEN`，语义相同
            # 只是没把新时段列进来。
            if phase not in OBSERVABLE:
                if not self._codes:
                    self._maybe_refresh_universe(force=True)
                return []

        t0 = time.perf_counter()
        self._maybe_refresh_universe()

        quotes: list[Quote] = []
        if self._codes:
            try:
                quotes = self.sources.call("snapshots", list(self._codes),
                                           route=ROUTE_STOCKS)
            except Exception as exc:  # noqa: BLE001
                self._errors += 1
                self.log.warning("行情抓取失败: %s", exc)
                self.store.set_poll_stats(
                    poll_ms=int((time.perf_counter() - t0) * 1000),
                    count=self._poll_count, health=self.sources.health(), now=now)
                # 抓取失败就整轮作废：state.quotes 里还留着上一轮的价格，
                # 继续跑规则等于拿旧价配新时间戳，可能产出无中生有的告警。
                return []
        elif not self.index_codes:
            return []

        # 指数单独抓（同一轮、同一个源）。放在个股之后：指数抓不到时个股照常跑。
        idx_quotes = self._fetch_indices()

        if not quotes and not idx_quotes:
            return []

        # 指数与个股共用同一准入合同（IT-P0-002-R1：股票和指数不得两套口径）。
        #
        # IT-P1-TIME-ROLE-001/004：准入前按**每条 route 各自的**实际供数源开 epoch。
        # 用 serving_of 而不是 current —— 热备期间由备用源实际供数，current
        # 还是主源，按它分段会把两个 epoch 混成一段，跨源首包照样被误杀。
        #
        # 必须**两条 route 都开**：个股与指数是两个独立数据流，各有自己的切源
        # 时机。只开个股会有两个后果 —— 指数自己切源不开 epoch（新源首包被
        # 误杀），个股切源清掉指数水位线（指数真实乱序被放行）。
        #
        # epoch 标签用「下标 + 名字」而不是只用名字：若配置里两个源重名
        # （例如都叫 sina），只用名字会让切换前后标签相同，begin_source_epoch
        # 判为幂等而不重置水位线 —— 跨源误杀就原样回来了。
        for _route in (ROUTE_STOCKS, ROUTE_INDEX):
            _serving_idx = self.sources.serving_of(_route)
            if _serving_idx is None:
                continue
            try:
                _serving_name = self.sources.sources[_serving_idx].name
            except (IndexError, AttributeError):
                _serving_name = "?"
            self.state.begin_source_epoch(
                _route, f"{_serving_name}#{_serving_idx}",
                source_name=_serving_name)

        # 先记准入计数基线，用来算"本轮"拒绝数（stats 是累计值）。
        _t0_future = int(self.state.stats.get("t_reject:future", 0))
        _t0_ooo = int(self.state.stats.get("t_reject:out_of_order", 0))
        # WP04 / IT-P1-OBS-010：陈旧**诊断**也要按轮差分 —— 它是累计值。
        _t0_stale_diag = dict(self.state.stale_diagnosed)
        idx_admitted = (self.state.update(idx_quotes, now, route=ROUTE_INDEX)
                        if idx_quotes else {})
        admitted = self.state.update(quotes, now, route=ROUTE_STOCKS)

        # 粗筛：只有**本轮真正被准入**且合格的标的进入规则。
        #
        # IT-P0-003：这里以前遍历 ``self.state.quotes``（累计"最近已知值"缓存），
        # 于是 provider 本轮少返回的代码会带着旧价、旧量冒充"本轮观测"进入规则。
        # 后果不只是看板显示旧价：``SpiritOrderRule._drop_stale()`` 靠"本轮
        # Snapshot 里不存在"来清理缓存，而缓存代码每轮都进 Snapshot，缺席永远
        # 看不见；``_check_trades()`` 又先刷新 ``_prev[code]`` 的时间戳再判
        # gap，于是 180 秒的真实缺口会被洗成 5 秒，长缺口安全阀失效。
        #
        # IT-P0-002-R1：从``quotes``（provider 原始输出）改为消费
        # ``state.update()`` 的返回值。旧写法自己重算 `price > 0`，把
        # far-future / out-of-order 这些**已被准入拒绝**的点又捞回 Snapshot，
        # 等于准入只保护了 State、没保护规则。现在准入结果是单一事实来源。
        # 注意：``admitted`` 只含通过时间准入的，`price > 0` 已在 update 内判过。
        returned = dict(admitted)
        eligible = {
            c: q for c, q in returned.items()
            if self.filters.accept(q, now.date()) and c not in self.ignore
        }
        # watchlist 里的代码即使被粗筛拦掉也要跑规则（这是既有语义），
        # 但同样必须是本轮真的返回了，否则就是在拿旧数据喂规则。
        watch_cur = {c: returned[c] for c in self.watchlist if c in returned}

        keep = set(eligible)
        keep.update(self.watchlist)
        # 指数不进 eligible（被 exclude_boards 拦掉），但必须保住它们的历史，
        # 否则 state.prune 会把刚攒起来的指数窗口清空，拉升指数永远等不到样本。
        keep.update(self.index_codes)
        if len(self.state.history) > len(keep) * 2:
            self.state.prune(keep)

        snap = Snapshot(ts=now, seq=self.state.seq, quotes=dict(watch_cur))
        snap.quotes.update(eligible)
        # 指数**不进 snap.quotes**（它是"个股快照"），spirit_index 从
        # ctx.state.quotes 自取，见其模块文档。

        # 本轮是谁在供数？能力声明跟着走（IT-P1-CAPABILITY-001）。
        # 用 serving_of 而不是 current：热备期间由备用源实际供数，
        # current 仍是主源，用错就会把 Sina 的数据按 Tencent 的能力判定。
        serving_idx = self.sources.serving_of(ROUTE_STOCKS)
        if serving_idx is None:
            serving_src = self.sources.current
        else:
            serving_src = self.sources.sources[serving_idx]
        caps = capabilities_for(serving_src)
        # 账本口径（IT-P0-002-R1 / IT-P2-OBS-003 / IT-P2-OBS-005）：
        #
        #   requested    = 本轮请求的代码数（个股 + 指数）
        #   returned     = provider **原始返回**的条数（含 price<=0）
        #   admitted     = 通过**时间准入**的条数（个股与指数同口径，纯时间）
        #   unknown_missing    = 请求了但**完全没返回**的（provider 侧缺失）
        #   rejected_quality   = 返回了但质量不可用（price<=0）
        #   stale/ooo_rejected = 返回了但时间不合格
        #
        # 以前 `returned = dict(admitted)`，于是"返回了但被内部丢弃"的票会被
        # 记成"没返回"（unknown_missing 同时表达三种互斥语义）；`admitted`
        # 又把"个股业务粗筛后"与"指数纯时间准入"相加，使 returned-admitted
        # 无法解释为"被时间拒绝"（IT-P2-OBS-003）。现在三者互斥且可对账。
        req_stocks = len(self._codes)
        raw_stock = list(quotes)
        raw_idx = list(idx_quotes)
        # IT-P2-OBS-008：**没发出去的请求不能算"请求了"**。
        #
        # ``_fetch_indices()`` 在 ``spirit_index`` 关闭时会直接返回 []（故意不发
        # 请求，省流量）。以前 ``index_requested`` 无条件写 ``len(self.index_codes)``，
        # 于是 ``wants_indices=False`` 与 ``True`` 的账本**取值完全相同**
        # （requested=5 / index_requested=3 / coverage=0.40）—— 观测层无法区分
        # "指数抓了但没回来"和"根本没抓"。后者不是数据质量事故，
        # 按前者记账会让 coverage 恒低、把正常配置误报成丢数。
        _idx_dispatched = bool(self.index_codes) and self._wants_indices
        index_requested = len(self.index_codes) if _idx_dispatched else 0
        future_rej = int(self.state.stats.get("t_reject:future", 0)) - _t0_future
        ooo_rej = int(self.state.stats.get("t_reject:out_of_order", 0)) - _t0_ooo
        # WP04：陈旧诊断的**本轮增量**，按 route 分账后再合计。
        stale_diag_by_route = {
            r: int(n) - int(_t0_stale_diag.get(r, 0))
            for r, n in self.state.stale_diagnosed.items()
            if int(n) - int(_t0_stale_diag.get(r, 0)) > 0
        }
        stale_diag = sum(stale_diag_by_route.values())
        # 被诊断的**代码集合**：age 表超线的那些（本轮原始返回中出现过）。
        _stale_codes = {
            q.code for q in (raw_stock + raw_idx)
            if self.state.time_age_seconds.get(q.code, 0.0) > STALE_TOLERANCE_SECONDS
        }

        # 质量不可用：provider 返回了但 price<=0（update 内部第 145 行丢弃）
        quality_bad = tuple(
            q.code for q in (raw_stock + raw_idx) if q.price <= 0
        )
        # 完全没返回：既不在原始返回里，也不是质量/时间问题
        #
        # IT-P2-OBS-009：**指数也要进 missing**。以前这里只遍历 ``self._codes``
        # （个股），于是"指数请求发了、provider 返回空"时 ``index_admitted=0``
        # 而 ``unknown_missing=()`` —— 指数码在整个账本里出现 **0 次**，
        # 指数丢失完全不可观测（个股丢失有 missing 兜底，指数没有）。
        # 现在 requested 里的每个码都必须能被某个桶解释。
        raw_codes = {q.code for q in raw_stock if q.price > 0}
        idx_raw_ok = {q.code for q in raw_idx if q.price > 0}
        time_rejected_codes = {
            q.code for q in raw_stock
            if q.price > 0 and q.code not in admitted
        }
        idx_time_rejected = {
            q.code for q in raw_idx
            if q.price > 0 and q.code not in idx_admitted
        }
        # 只有**真的发出去**的请求才进 missing 候选（与 index_requested 同口径）。
        requested_codes = list(self._codes) + (
            list(self.index_codes) if index_requested else [])
        observation = RoundObservationSet(
            source=str(getattr(serving_src, "name", "")),
            capabilities=caps,
            requested=req_stocks + index_requested,
            index_requested=index_requested,
            returned=len(raw_stock) + len(raw_idx),
            admitted=len(admitted) + len(idx_admitted),
            index_admitted=len(idx_admitted),
            unknown_missing=tuple(
                c for c in requested_codes
                if c not in raw_codes and c not in idx_raw_ok
                and c not in time_rejected_codes and c not in idx_time_rejected
                and c not in quality_bad
            ),
            rejected_quality=quality_bad,
            future_rejected=max(future_rej, 0),
            out_of_order_rejected=max(ooo_rej, 0),
            # WP04 / IT-P1-OBS-010：陈旧是**诊断**，与 future 分账。
            provider_stale_diagnosed=max(stale_diag, 0),
            provider_stale_diagnosed_codes=_stale_codes,
            provider_stale_by_route=stale_diag_by_route,
        )

        ctx = RuleContext(
            state=self.state,
            cfg={},
            now=now,
            session=phase,
            elapsed_trading_seconds=self.calendar.elapsed_trading_seconds(now),
            minutes_to_close=self.calendar.minutes_to_close(now),
            watchlist=tuple(self.watchlist),
            focus=self.focus,
            capabilities=caps,
            observation=observation,
            # IT-P1-INDEX-CURRENT-001：本轮**真正准入**的指数。
            # 规则不得再从累计 state.quotes 猜 current —— 指数路由整体失败时
            # 那里面是上一轮的数据，会让规则拿陈旧指数继续报。
            current_indices=idx_admitted,
        )

        now_ep = now.timestamp()
        fresh: list[Alert] = []
        for rule in self.rules:
            name = getattr(rule, "name", rule.__class__.__name__)
            try:
                alerts = rule.evaluate(snap, ctx) or []
            except Exception as exc:  # noqa: BLE001
                self.state.stats[f"err:{name}"] = self.state.stats.get(f"err:{name}", 0) + 1
                self.log.exception("规则 %s 求值异常: %s", name, exc)
                continue
            for a in alerts:
                if not isinstance(a, Alert):
                    continue
                # IT-P1-DELIVERY-LEDGER-002：对 Engine 而言有一个**统一且不用猜**
                # 的边界 —— 规则真正返回的 Alert 就是 rule-selected output。
                # 由 Engine 统一记，避免每条规则自己重复写同一阶段、又再次
                # 出现"某条规则忘了维护交付账本"（这正是 9.1% 覆盖率的成因）。
                self._mark_delivery_selected(observation, a, now_ep)
                if self.ignore and a.code in self.ignore:
                    # 被全局 ignore 拦下：如实记进 global_ignored，这样交付链
                    # 的不变量仍是 bus_accepted <= rule_selected - global_ignored。
                    self._mark_delivery_ignored(observation, a, now_ep)
                    continue
                if not self.bus.accept(a, now_ep):
                    continue
                # IT-P1-EVAL-PUBLISH-001：规则返回后**才**知道它真的过了
                # bus 去重/冷却。这里按 signal 记 bus_accepted —— 旧账本
                # 在规则内部就把这步算成 "published"，导致被冷却丢掉的
                # 候选也被当成已发布（机制反例 overcount 24×）。
                self._mark_stage(observation, a, "bus_accepted")
                if a.code in self.focus and a.severity < 3:
                    a.severity += 1
                fresh.append(a)

        # 紧急优先，同级按幅度
        fresh.sort(key=lambda a: (-a.severity, -abs(a.pct)))

        for a in fresh:
            self.store.add_alert(a)
            # 真正写进 Store 才叫 committed —— 这是"用户看得到"的那一级。
            self._mark_stage(observation, a, "committed")
        # 全局门禁的分母：**真实**写进 Store 的第一方告警总数。
        # 必须由 Engine 独立数，不能拿账本 committed 之和充当分母 ——
        # 那样分子分母同源，delivery_accounting_coverage 会恒等于 1.0，
        # 这正是本缺陷能长期隐藏的原因。
        #
        # IT-P1-DELIVERY-FALSE-GREEN-001：吞异常本身**可以**保留
        # （可观测性不能反过来打断告警主链路），但**必须留下痕迹**。
        # 旧代码是裸 `except Exception: pass` —— 分母静默不涨，而门禁
        # `max(total - named, 0)` 把负差夹成 0，于是"账本坏了"被读成 PASS
        # （实测 total=0/named=66 -> 门禁 0）。现在把失败计入
        # `accounting_errors`，使 `accounting_status` 变成 inconsistent。
        try:
            observation.committed_alerts_total += len(fresh)
        except Exception:  # noqa: BLE001
            try:
                observation.accounting_errors += 1
            except Exception:  # noqa: BLE001
                # 连错误计数都记不上：只能放弃，但绝不能影响告警主链路。
                pass
        # IT-P1-DELIVERY-INVARIANTS-UNCALLED-001：把自洽性检查从**读时**
        # 前移到**写时**。
        #
        # 为什么必须做：``check_delivery_invariants()`` 此前在**生产里零调用**
        # （全仓只有它自己的 def）。也就是说"逐轮交付不变量违规 0"是
        # **测试口径**，不是生产保证 —— 数是真的（361 轮独立重跑确实 0），
        # 但**生产里没有任何东西在检查它**。
        #
        # 与上面的纪律一致：检查失败**只记账 + 记日志，绝不抛** ——
        # 可观测性不能反过来打断告警主链路。
        try:
            bad = observation.check_delivery_invariants()
            if bad:
                observation.accounting_errors += len(bad)
                self.log.warning("交付账本不变量违规 %d 条: %s",
                                 len(bad), "; ".join(str(x) for x in bad[:3]))
        except Exception:  # noqa: BLE001
            try:
                observation.accounting_errors += 1
            except Exception:  # noqa: BLE001
                pass
        # 本轮所有告警一次性交给通知器；由通知器自己决定逐条还是聚合
        self._dispatch_many(fresh)

        self._poll_count += 1
        self.state.stats["polls"] = self._poll_count
        self.state.stats["alerts"] = self.state.stats.get("alerts", 0) + len(fresh)

        # 分时序列（只记自选 + 异动榜，控制内存）
        try:
            track = {c: self.state.quotes[c] for c in self.watchlist if c in self.state.quotes}
            for a in fresh:
                if a.code in self.state.quotes:
                    track[a.code] = self.state.quotes[a.code]
            if track:
                self.store.record_tick(list(track.values()), now)
        except Exception:  # noqa: BLE001
            pass

        self.store.set_poll_stats(
            poll_ms=int((time.perf_counter() - t0) * 1000), count=self._poll_count,
            health=self.sources.health(), now=now, observation=observation)
        return fresh

    @staticmethod
    def _mark_delivery_selected(observation: object, alert: Alert,
                                now_ep: float) -> None:
        """记一条"规则选中"进**独立交付账本**（IT-P1-DELIVERY-LEDGER-002）。

        ⚠ 没有 ``signal_id`` 的告警**无法归属**，这里不记 —— 它会体现在
        全局门禁 ``first_party_committed_without_signal_id`` 上，
        而不是被悄悄算进某个 signal 的 ``rule_selected``。

        **绝不抛异常**：可观测性不能打断告警主链路。
        """
        try:
            sig = str(getattr(alert, "signal_id", "") or "")
            if not sig:
                return
            fn = getattr(observation, "mark_delivery_selected", None)
            if callable(fn):
                fn(sig, str(getattr(alert, "code", "") or ""))
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _mark_delivery_ignored(observation: object, alert: Alert,
                               now_ep: float) -> None:
        """记一条"被全局 ignore 拦下"进交付账本。**绝不抛异常**。"""
        try:
            sig = str(getattr(alert, "signal_id", "") or "")
            if not sig:
                return
            fn = getattr(observation, "mark_delivery_ignored", None)
            if callable(fn):
                fn(sig, str(getattr(alert, "code", "") or ""))
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _mark_stage(observation: object, alert: Alert, stage: str) -> None:
        """把一条告警记进该轮账本的交付阶段。

        两条路径（IT-P1-EVAL-PUBLISH-001 + IT-P1-DELIVERY-LEDGER-002）：

        1. **可评估性账本**（``SignalEvalStats``，规则所有）：只在该 signal
           已被**规则自己**创建了 eval 行时才记。``mark_bus_accepted`` /
           ``mark_committed`` 会经 ``eval_stats()`` **新建**条目，所以这里
           必须先用 ``sig not in evals`` 把没有 instrumentation 的规则挡在
           外面 —— 否则会冒出 ``considered=0/evaluable=0/hit=0/sel=0`` 却
           ``bus_accepted=1/committed=1`` 的幽灵行，既违反可评估性不变量，
           又把"没做逐 code 统计"伪装成"0% 可评估"。
        2. **交付账本**（``SignalDeliveryStats``，**Engine 所有**）：只要告警
           带稳定 ``signal_id`` 就记，**不看**该 signal 有没有 eval 行。

        为什么必须有第 2 条（云端 16:07 轮的 IT-P1-DELIVERY-LEDGER-002，
        我已用真实代码复现）：7 个能发 Alert 的规则里只有 2 个维护 eval 账本，
        于是真实回放 66 条 committed 告警只有 6 条可对账 —— **覆盖率 9.1%**。
        修法**不能**只是"给另外 5 条规则补 ``signal_id``"：那条
        ``sig not in signal_evals`` 门禁会照样拦下它们（已实测证伪）。
        也不能让 Engine 自动补 eval 行（见上，会破坏不变量）。
        把交付账本独立出来，7/7 规则才全部闭合。

        **绝不抛异常**：可观测性不能反过来打断告警主链路（本仓既有纪律）。
        注意 ``getattr`` 本身也必须包在 ``try`` 里 —— 账本对象可能是被替换
        的坏桩，属性访问（property）随时可能抛；放在 ``try`` 外面就等于
        让"记账"能炸掉告警。
        """
        method = {
            "bus_accepted": "mark_bus_accepted",
            "committed": "mark_committed",
        }.get(stage)
        delivery_method = {
            "bus_accepted": "mark_delivery_bus_accepted",
            "committed": "mark_delivery_committed",
        }.get(stage)
        if method is None and delivery_method is None:
            return
        try:
            sig = str(getattr(alert, "signal_id", "") or "")
            if not sig:
                return
            code = str(getattr(alert, "code", "") or "")
            # --- 路径 2：独立交付账本（只要有 signal_id 就记）----------------
            if delivery_method:
                dfn = getattr(observation, delivery_method, None)
                if callable(dfn):
                    dfn(sig, code)
            # --- 路径 1：可评估性账本（只有已 instrument 的 signal 才记）------
            if method:
                evals = getattr(observation, "signal_evals", None)
                if isinstance(evals, dict) and sig in evals:
                    fn = getattr(observation, method, None)
                    if callable(fn):
                        fn(sig, code)
        except Exception:  # noqa: BLE001
            pass

    def _dispatch_many(self, alerts: list[Alert]) -> None:
        """把本轮全部告警交给每个通知器。

        优先调用 ``send_digest(alerts)`` —— 这样 ``console.mode=digest`` 之类
        的聚合模式才有意义（否则该模式永远收不到数据）。若通知器没有实现
        ``send_digest``（老的自定义通知器只实现了 ``send``），退化为逐条 ``send``，
        保证告警不会静默丢失。
        """
        if not alerts:
            return
        for n in self.notifiers:
            name = getattr(n, "name", "?")
            digest = getattr(n, "send_digest", None)
            try:
                if callable(digest):
                    digest(alerts)
                else:
                    for a in alerts:
                        n.send(a)
            except Exception as exc:  # noqa: BLE001
                self.log.error("通知器 %s 异常: %s", name, exc)
                # 聚合失败不能让告警消失：退回逐条推送
                for a in alerts:
                    try:
                        n.send(a)
                    except Exception as exc2:  # noqa: BLE001
                        self.log.error("通知器 %s 逐条兜底也失败: %s", name, exc2)
                        break

    def _dispatch(self, alert: Alert) -> None:
        """单条推送（保留给外部调用方；引擎内部走 ``_dispatch_many``）。"""
        self._dispatch_many([alert])

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def run_forever(self, *, max_rounds: int | None = None,
                    on_round: Callable[[int, list[Alert]], None] | None = None,
                    watch_only: bool = False) -> None:
        """按配置间隔轮询，直到 Ctrl+C 或达到 max_rounds。

        ``watch_only=True``：只盯自选股，**不拉全市场股票池**。适合"我就想看
        自己那几只"的用法——启动即用，不必等 20 秒的全市场刷新，也不会因为
        数据源限流而告警。自选股间隔走 ``poll.watchlist_seconds``（更密）。
        """
        self.running = True
        self._stop = False
        interval = float(self.settings.get("poll.universe_seconds", 5) or 5)
        if watch_only:
            if not self.watchlist:
                self.log.error("--watch-only 但 config/watchlist.yaml 是空的 -> 无事可做")
                self.running = False
                return
            self._codes = list(self.watchlist)      # 赋值即 pin，不会再联网刷新
            interval = float(self.settings.get("poll.watchlist_seconds", 3) or 3)
            self.log.info("自选股模式：仅监控 %d 只，间隔 %.1fs",
                          len(self.watchlist), interval)
        self.log.info("引擎启动：间隔 %.1fs，规则 %d 个，通知器 %d 个，自选 %d 只",
                      interval, len(self.rules), len(self.notifiers), len(self.watchlist))
        # 开跑前先确保股票池可用，并把降级情况明确告诉用户（否则会静默无告警）
        if not watch_only:
            try:
                self._maybe_refresh_universe(force=not self._codes)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("启动时刷新股票池失败: %s", exc)
        if not self._codes:
            self.log.error(
                "股票池为空且自选股也没配 -> 不会产生任何告警。"
                "请检查 sources.universe（%s）连通性，或在 config/watchlist.yaml 配置自选股。",
                self.settings.get("sources.universe", "eastmoney"))
        elif len(self._codes) <= len(self.watchlist) and not watch_only:
            self.log.warning("当前仅监控 %d 只（自选股降级模式），未在全市场扫描", len(self._codes))
        rounds = 0
        try:
            while not self._stop:
                t0 = time.perf_counter()
                try:
                    fresh = self.poll_once()
                except KeyboardInterrupt:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self._errors += 1
                    self.log.exception("轮询异常: %s", exc)
                    fresh = []
                rounds += 1
                if on_round:
                    try:
                        on_round(rounds, fresh)
                    except Exception:  # noqa: BLE001
                        pass
                if max_rounds is not None and rounds >= max_rounds:
                    break
                sleep_for = interval - (time.perf_counter() - t0)
                if sleep_for > 0:
                    end = time.time() + sleep_for
                    while not self._stop and time.time() < end:
                        time.sleep(min(0.2, end - time.time()))
        except KeyboardInterrupt:
            self.log.info("收到中断，正在退出…")
        finally:
            self.running = False
            self.log.info("引擎已停止（%d 轮，%d 条错误）", rounds, self._errors)

    def stop(self) -> None:
        self._stop = True


# ==========================================================================
# 数据源工厂
# ==========================================================================
SOURCE_MODULES = {"tencent": "tencent", "eastmoney": "eastmoney", "sina": "sina"}


def build_source(settings: Settings | None = None, name: str | None = None):
    """按名字构造数据源（延迟 import，便于单测替换）。"""
    st = settings or load_settings()
    wanted = name or str(st.get("sources.primary", "tencent"))
    module = SOURCE_MODULES.get(wanted, wanted)
    mod = importlib.import_module(f".sources.{module}", package="arad")
    cfg = dict(st.section("sources").get(module) or {})
    cfg.setdefault("batch_size", st.get("poll.batch_size", 600))
    cfg.setdefault("workers", st.get("poll.workers", 4))
    cfg.setdefault("timeout", st.get("poll.http_timeout", 10))
    cfg.setdefault("retries", st.get("poll.retries", 3))
    if hasattr(mod, "build"):
        return mod.build(cfg)
    cls = getattr(mod, "SOURCE", None) or getattr(
        mod, {"tencent": "TencentSource", "eastmoney": "EastmoneySource",
              "sina": "SinaSource"}.get(module, ""), None)
    if cls is None:
        raise RuntimeError(f"数据源 {wanted} 缺少 build() 或 Source 类")
    return cls(cfg)


def build_source_chain(settings: Settings) -> list[Any]:
    """主源 + 备用源（用于故障转移）。"""
    chain: list[Any] = []
    for name in [str(settings.get("sources.primary", "tencent"))] + list(
            settings.get("sources.fallback") or []):
        try:
            chain.append(build_source(settings, name))
        except Exception as exc:  # noqa: BLE001
            log.warning("数据源 %s 构造失败: %s", name, exc)
    if not chain:
        raise RuntimeError("没有任何可用数据源")
    return chain


# ==========================================================================
# 便捷入口
# ==========================================================================
def run_forever(*, settings: Settings | None = None, source=None,
                use_web: bool = True, watch_only: bool = False) -> None:
    """启动引擎（可选同时启动 Web 看板），阻塞直到 Ctrl+C。"""
    st = settings or load_settings()
    if source is None:
        source = build_source_chain(st)
    engine = Engine(source, settings=st)
    server = None
    if use_web and st.get("web.enabled", True):
        try:
            from .server.web import serve

            server = serve(engine.store, st.web, block=False)
            port = st.get("web.port", 8899)
            log.info("看板已启动: http://127.0.0.1:%s", port)
        except Exception as exc:  # noqa: BLE001
            log.error("看板启动失败: %s", exc)
    try:
        engine.run_forever(watch_only=watch_only)
    finally:
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:  # noqa: BLE001
                pass
