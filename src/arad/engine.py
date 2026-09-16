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
        """返回 ``[now-seconds, now]`` 内的 (ts, price, cum_volume_lots) 列表（时间升序）。"""
        h = self.history.get(code)
        if not h:
            return []
        cutoff = now_epoch - float(seconds)
        # deque 有序，从右往左找即可；样本量小，直接过滤更简单可靠
        return [p for p in h if p[0] >= cutoff]

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
    def update(self, quotes: Iterable[Quote], now: datetime) -> None:
        """把一轮快照并入状态。

        曾经有个 ``eligible=`` 关键字参数，但函数体从未读过它 —— 调用方以为
        "只更新合格标的"会生效，实际全量写入。与其留一个静默失效的承诺，
        不如删掉：粗筛由 ``poll_once`` 在读取侧做（见 ``eligible`` 字典）。
        """
        ep = now.timestamp()
        self.seq += 1
        for q in quotes:
            if q.price <= 0:
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
                h.append((ep, q.price, q.volume_lots))
            self.last_price[q.code] = q.price
            if q.ts is not None and q.ts > now:
                pass  # 行情时间戳超前（时钟偏差），忽略

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
class SourceManager:
    """主源 + 备用源，连续失败达阈值自动切换。"""

    def __init__(self, sources: list[Any], *, threshold: int = 3):
        if not sources:
            raise ValueError("SourceManager 需要至少一个数据源")
        self.sources = sources
        self.threshold = max(int(threshold), 1)
        self.idx = 0
        self.fails = 0
        self.history: list[dict] = []

    @property
    def current(self):
        return self.sources[self.idx]

    def _switch(self) -> None:
        if len(self.sources) > 1:
            self.idx = (self.idx + 1) % len(self.sources)
            self.fails = 0
            log.warning("数据源切换 -> %s", getattr(self.current, "name", "?"))

    def call(self, method: str, *args):
        """调用当前源；失败则按顺序尝试备用源。

        语义：
        * 每个源自身已实现内部重试，本方法对每个源只尝试一次。
        * 主源失败、备用源成功 => 本次返回备用源数据（热备），并累加失败计数；
          连续失败达到 ``threshold`` 后把该备用源提升为主源（正式切换）。
        * 全部失败 => 抛出最后一个异常。
        """
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
            if i == self.idx:
                self.fails = 0
            else:
                self.fails += 1
                log.info("已由备用源 %s 提供服务（连续 %d/%d）",
                         getattr(src, "name", "?"), self.fails, self.threshold)
                if self.fails >= self.threshold:
                    self.idx = i
                    self.fails = 0
                    log.warning("数据源已正式切换 -> %s", getattr(src, "name", "?"))
            return out

        self.fails += 1
        if self.fails >= self.threshold:
            self._switch()
        if last_exc:
            raise last_exc
        return None

    def health(self) -> list[dict]:
        out = []
        for s in self.sources:
            try:
                h = s.health()
            except Exception as exc:  # noqa: BLE001
                h = {"name": getattr(s, "name", "?"), "ok": False, "latency_ms": 0, "err": str(exc)}
            h = dict(h)
            h["active"] = (s is self.current)
            out.append(h)
        return out


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
        self.store = store if store is not None else AlertStore(self.settings, calendar=self.calendar)
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
            return self.sources.call("snapshots", list(self.index_codes))
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

    def refresh_universe(self) -> int:
        """刷新股票池代码列表（默认每 30 分钟一次）。

        按 ``sources.universe`` 的顺序依次尝试，**任一来源返回非空就采用**；
        全失败才告警并保留原股票池（绝不清空 —— 清空会让引擎彻底停摆）。
        """
        last_exc: Exception | None = None
        for src in self._universe_sources():
            try:
                quotes = (src.call("universe") if isinstance(src, SourceManager)
                          else src.universe())
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                self.log.warning("股票池来源 %s 失败: %s",
                                 getattr(src, "name", src), exc)
                continue
            if not quotes:
                continue
            codes = self._record_universe(quotes)
            if codes:
                # 直接写底层字段：这里拿到的是**真·全市场**，必须保持"未 pin"，
                # 否则 TTL 到期后不会再有下一次刷新（_codes 的 setter 会 pin）。
                self._codes_raw = codes
                self._codes_pinned = False
                self._universe_refreshed_at = time.time()
                self.log.info("股票池已刷新: %d 只（来源 %s）",
                              len(codes), getattr(src, "name", src))
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
            if not isinstance(q, dict):
                self.state.universe[c] = q
            codes.append(c)
        if codes:
            self.filters.list_dates = {
                q.code: str(q.list_date or "")
                for q in self.state.universe.values()
                if getattr(q, "list_date", "")
            }
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
                quotes = self.sources.call("snapshots", list(self._codes))
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

        if idx_quotes:
            self.state.update(idx_quotes, now)
        self.state.update(quotes, now)

        # 粗筛：只有合格标的进入规则
        eligible = {
            c: q for c, q in self.state.quotes.items()
            if self.filters.accept(q, now.date()) and c not in self.ignore
        }
        keep = set(eligible)
        keep.update(self.watchlist)
        # 指数不进 eligible（被 exclude_boards 拦掉），但必须保住它们的历史，
        # 否则 state.prune 会把刚攒起来的指数窗口清空，拉升指数永远等不到样本。
        keep.update(self.index_codes)
        if len(self.state.history) > len(keep) * 2:
            self.state.prune(keep)

        snap = Snapshot(ts=now, seq=self.state.seq,
                        quotes={c: self.state.quotes[c] for c in self.watchlist if c in self.state.quotes})
        snap.quotes.update(eligible)
        # 指数**不进 snap.quotes**（它是"个股快照"），spirit_index 从
        # ctx.state.quotes 自取，见其模块文档。

        ctx = RuleContext(
            state=self.state,
            cfg={},
            now=now,
            session=phase,
            elapsed_trading_seconds=self.calendar.elapsed_trading_seconds(now),
            minutes_to_close=self.calendar.minutes_to_close(now),
            watchlist=tuple(self.watchlist),
            focus=self.focus,
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
            health=self.sources.health(), now=now)
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
