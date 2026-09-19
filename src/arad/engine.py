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
from .session import CONTINUOUS, SessionPhase, TradingCalendar
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
    def update(self, quotes: Iterable[Quote], now: datetime) -> dict[str, Quote]:
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
        """
        ep = now.timestamp()
        self.seq += 1
        admitted: dict[str, Quote] = {}
        for q in quotes:
            if q.price <= 0:
                continue

            # --- 写前时间准入 -------------------------------------------
            ts_ok, q_ep, why = self._admit_time(q, now, ep)
            if not ts_ok:
                self.stats[f"t_reject:{why}"] = self.stats.get(f"t_reject:{why}", 0) + 1
                continue

            # --- 乱序准入：必须在任何写入之前判定 ------------------------
            # 旧代码先写 state.quotes，之后才判乱序，于是"不顺延 history"
            # 只保护了 history，latest cache 与 last_price 仍被迟到旧价倒退。
            h = self.history.get(q.code)
            if h and q_ep < h[-1][0]:
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
            if h is None:
                h = deque(maxlen=max(int(self.history_len), 30))
                self.history[q.code] = h
            if prev is None or prev != q.price or not h or h[-1][2] != q.volume_lots:
                h.append((q_ep, q.price, q.volume_lots))
            self.last_price[q.code] = q.price
            admitted[q.code] = q
        return admitted

    def _admit_time(self, q: Quote, now: datetime,
                    ep: float) -> tuple[bool, float, str]:
        """写前时间准入。返回 ``(是否接纳, 用于入库的时间戳, 拒绝原因)``。

        事件时间缺失时按"接收时间"处理（视作准时），这是兼容既有行为：
        多数 source 不填 ``ts``。
        """
        if q.ts is None:
            return True, ep, ""
        try:
            q_ep = float(q.ts.timestamp())
        except (AttributeError, OSError, ValueError):
            return True, ep, ""
        if q_ep > ep + FUTURE_TOLERANCE_SECONDS:
            return False, q_ep, "future"
        if q_ep > ep:
            return True, ep, ""          # 轻微超前 -> 夹到 now，保留数据
        return True, q_ep, ""

    def prune(self, keep_codes: set[str] | None = None) -> int:
        """丢弃不再关注的股票历史，控制内存。返回清理条数。"""
        if keep_codes is None:
            return 0
        drop = [c for c in self.history if c not in keep_codes]
        for c in drop:
            self.history.pop(c, None)
            self.last_price.pop(c, None)
            self.first_seen.pop(c, None)
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
        self.store = store if store is not None else AlertStore(
            self.settings, calendar=self.calendar,
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
        * 不完整结果（``complete=False``，即分页中途失败/翻页被截断）只作为
          **兜底候选**，继续往后试，只有所有来源都没给出完整结果时才采用其中
          最大的那个 —— 且**绝不用它覆盖更大的已有股票池**（IT-P1-006）。

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
            if phase not in CONTINUOUS and phase is not SessionPhase.PRE_OPEN:
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
        # 先记准入计数基线，用来算"本轮"拒绝数（stats 是累计值）。
        _t0_future = int(self.state.stats.get("t_reject:future", 0))
        _t0_ooo = int(self.state.stats.get("t_reject:out_of_order", 0))
        idx_admitted = self.state.update(idx_quotes, now) if idx_quotes else {}
        admitted = self.state.update(quotes, now)

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
        # 账本口径（IT-P0-002-R1 / WP02）：
        #   requested = 本轮请求的代码数（个股 + 指数）
        #   returned  = provider 原始返回且 price>0 的（未经时间准入）
        #   admitted  = 通过时间准入后真正进入规则的
        # returned 与 admitted 的差额就是被时间准入拒掉的（future / out_of_order），
        # 这两个 rejection 计数直接取自 state.stats 的差分，不另起一套口径。
        req_stocks = len(self._codes)
        raw_returned = sum(1 for q in quotes if q.price > 0)
        future_rej = int(self.state.stats.get("t_reject:future", 0)) - _t0_future
        ooo_rej = int(self.state.stats.get("t_reject:out_of_order", 0)) - _t0_ooo
        observation = RoundObservationSet(
            source=str(getattr(serving_src, "name", "")),
            capabilities=caps,
            requested=req_stocks + len(self.index_codes),
            returned=raw_returned + sum(1 for q in idx_quotes if q.price > 0),
            admitted=len(eligible) + len(idx_admitted),
            unknown_missing=tuple(
                c for c in self._codes if c not in returned
            ),
            stale_rejected=max(future_rej, 0),
            out_of_order_rejected=max(ooo_rej, 0),
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
                if self.ignore and a.code in self.ignore:
                    continue
                if not self.bus.accept(a, now_ep):
                    continue
                if a.code in self.focus and a.severity < 3:
                    a.severity += 1
                fresh.append(a)

        # 紧急优先，同级按幅度
        fresh.sort(key=lambda a: (-a.severity, -abs(a.pct)))

        for a in fresh:
            self.store.add_alert(a)
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
