"""运行期共享状态容器：告警仓库 + 订阅广播（供 Web/SSE 使用）。

设计要点
--------
* ``AlertStore`` 是引擎与 Web 层之间**唯一**的数据通道，避免两者直接耦合。
* 所有跨线程访问都用 ``threading.RLock`` 保护；订阅队列写满时丢弃最旧事件，
  绝不阻塞引擎（行情推送不能被慢客户端拖死）。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any

from .config import Settings, load_settings
from .models import Alert, AlertKind, Quote
from .session import CALL_AUCTIONS, CONTINUOUS, PHASE_CN, SessionPhase, TradingCalendar

__all__ = ["AlertStore"]

log = logging.getLogger("arad.store")

# 榜单排序键
_SORTS = {
    "speed": lambda it: -abs(it.get("speed_1m") or 0.0),
    "speed5": lambda it: -abs(it.get("speed_5m") or 0.0),
    "pct": lambda it: -abs(it.get("pct") or 0.0),
    "up": lambda it: -(it.get("pct") or 0.0),
    "down": lambda it: (it.get("pct") or 0.0),
    "amount": lambda it: -(it.get("amount") or 0.0),
    "volume_ratio": lambda it: -(it.get("volume_ratio") or 0.0),
}


class AlertStore:
    """引擎产出 → 前端消费的共享仓库。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        calendar: TradingCalendar | None = None,
        max_alerts: int = 300,
        series_len: int = 240,
    ):
        self.settings = settings or load_settings()
        self.calendar = calendar or TradingCalendar.load()
        self._lock = threading.RLock()
        self._engine: Any = None

        self._alerts: deque[dict] = deque(maxlen=max(int(max_alerts), 10))
        # IT-P1-ACK-TRIM-ORDER-001：**有序**容器（dict 保持插入序）。
        # 以前是 set —— `list(set)` 是哈希序，裁剪 `[-2000:]` 会丢掉刚 ack 的。
        # 值恒为 None，只借 key 的插入序当"最近 ack"时序。
        self._acked: dict[str, None] = {}
        self._total = 0
        self._by_kind: dict[str, int] = {}
        self._subs: set[queue.Queue] = set()

        self._series_len = max(int(series_len), 2)
        self._series: dict[str, deque[dict]] = {}
        self._series_codes: deque[str] = deque(maxlen=120)

        self._started = time.time()
        self._last_poll_ts: str = ""
        self._last_poll_ms: int = 0
        self._poll_count: int = 0
        self._source_health: list[dict] = []
        self._notes: list[str] = []
        #: 最近一轮的观测账本（有界汇总，见 set_poll_stats）。WP02。
        self._observation: dict = {}
        #: 观测账本的轮次序号，每写入一次 +1。消费方据此判断"这份账本
        #: 是不是本轮的"——防止失败轮沿用上一成功轮的数字（IT-P2-OBS-004）。
        self._observation_seq: int = 0
        #: 账本**自身**的写入时刻，与 ``_last_poll_ts`` **同源**
        #: （``set_poll_stats(now=...)``）。无账本时为 ``None``。
        #:
        #: 为什么单独存而不是复用 ``_last_poll_ts``：后者每次 poll 都刷新，
        #: **包括**没有账本的失败轮；用它当账本时刻就等于把旧账标成新鲜
        #: （IT-P2-OBS-STATUS-001）。格式取完整日期时间（与 status 的
        #: ``ts`` 同格式），因为只有时分秒无法跨零点算 age。
        self._observation_observed_at: str | None = None
        #: 账本写入时引擎的 poll 计数（``set_poll_stats(count=...)``）。
        #: 无账本时为 ``0``（与 ``_observation_seq`` 的 0 哨兵一致）。
        #: 注意它是**账本当时**的计数，不是"最近一次 poll"的计数。
        self._observation_poll_count: int = 0

    # ------------------------------------------------------------------
    # 引擎接入
    # ------------------------------------------------------------------
    def attach(self, engine: Any) -> None:
        """绑定引擎，之后 ``status()/top_quotes()`` 才能读到实时行情。"""
        with self._lock:
            self._engine = engine

    @property
    def engine(self) -> Any:
        return self._engine

    def _state(self):
        eng = self._engine
        return getattr(eng, "state", None) if eng is not None else None

    # ------------------------------------------------------------------
    # 写入侧（引擎调用）
    # ------------------------------------------------------------------
    def add_alert(self, alert: Alert) -> dict:
        """记录一条告警并广播。返回其 dict 形式。"""
        d = alert.to_dict()
        with self._lock:
            self._alerts.appendleft(d)
            self._total += 1
            k = d.get("kind", "")
            self._by_kind[k] = self._by_kind.get(k, 0) + 1
        self.broadcast("alert", d)
        return d

    def record_tick(self, quotes: list[Quote], now: datetime | None = None) -> None:
        """记录分时序列（供前端 mini 图）。"""
        ts = (now or datetime.now()).timestamp()
        with self._lock:
            for q in quotes:
                if q.price <= 0:
                    continue
                buf = self._series.get(q.code)
                if buf is None:
                    buf = deque(maxlen=self._series_len)
                    self._series[q.code] = buf
                    self._series_codes.append(q.code)
                buf.append({"t": round(ts, 1), "price": round(q.price, 3),
                            "pct": round(q.pct, 3)})

    def set_poll_stats(self, *, poll_ms: int, count: int,
                       health: list[dict] | None = None,
                       now: datetime | None = None,
                       observation: Any = None) -> None:
        """记录本轮统计。

        ``observation`` 是 ``RoundObservationSet``（IT-P0-002-R1 / WP02）。
        以前它只活在 ``poll_once()`` 局部，Store/status/live_session 都看不到，
        于是"Sina 期间放量规则不可评估"这类事实无人能查。这里只保留**有界的
        汇总**（计数 + 能力位 + 来源），不持久化逐股明细。
        """
        with self._lock:
            self._last_poll_ms = int(poll_ms)
            self._poll_count = int(count)
            ts = now or datetime.now()
            self._last_poll_ts = ts.strftime("%H:%M:%S")
            if health is not None:
                self._source_health = list(health)
            if observation is not None:
                as_dict = getattr(observation, "as_dict", None)
                if callable(as_dict):
                    try:
                        self._observation = as_dict()
                        self._observation_seq += 1
                        # 账本身份与其内容**同一时刻**落账：只有真的写入
                        # 才推进 seq/时刻/计数，三者永远同步（IT-P2-OBS-STATUS-001）。
                        self._observation_observed_at = ts.strftime("%Y-%m-%d %H:%M:%S")
                        self._observation_poll_count = int(count)
                    except Exception:  # noqa: BLE001  可观测性不得影响主流程
                        pass

    @property
    def observation(self) -> dict:
        """最近一轮的观测账本（没有则为空 dict）。"""
        with self._lock:
            return dict(self._observation)

    @property
    def observation_seq(self) -> int:
        """观测账本被写入的次数。

        消费方（如 ``live_session``）用它在采样前后比对：序号没变就说明
        本轮**没有**产生新账本，此时 ``observation`` 仍是上一轮的，不能
        冒充本轮数字（IT-P2-OBS-004）。
        """
        with self._lock:
            return int(self._observation_seq)

    @property
    def observation_observed_at(self) -> str | None:
        """账本自身的记录时刻（``YYYY-MM-DD HH:MM:SS``）。

        从未写入过账本时返回 ``None`` —— 消费方必须据此判定"无账本"，
        而不是拿 API 顶层 ``ts``（那是**请求**时刻）冒充账本时刻。
        """
        with self._lock:
            return None if self._observation_observed_at is None \
                else str(self._observation_observed_at)

    @property
    def observation_poll_count(self) -> int:
        """账本写入时引擎的 poll 计数；从未写入过账本时为 ``0``。"""
        with self._lock:
            return int(self._observation_poll_count)

    def add_note(self, msg: str) -> None:
        with self._lock:
            self._notes.append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")
            del self._notes[:-20]

    def broadcast(self, event: str, data: Any) -> None:
        """向所有订阅者推送事件；队列满则丢弃最旧的一条。"""
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait({"event": event, "data": data})
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait({"event": event, "data": data})
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # 订阅
    # ------------------------------------------------------------------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    # ------------------------------------------------------------------
    # 读取侧（Web 调用）
    # ------------------------------------------------------------------
    def _describe_phase(self, phase: SessionPhase) -> str:
        """按时段给人类可读描述（**只用 phase，不碰墙钟**）。

        ``TradingCalendar.describe()`` 会自己重新算 ``phase(now)``，
        所以它**不能**用来描述一个"引擎算出来的" phase —— 回放模式下墙钟是
        凌晨、引擎时钟是 09:40，两者会打架。这里按同一张 ``PHASE_CN`` 表
        和同样的措辞自己拼，保证与 ``phase`` 字段自洽。
        """
        label = PHASE_CN.get(phase, phase.value)
        if phase in CONTINUOUS:
            return f"{label} 交易中"
        if phase in CALL_AUCTIONS:
            return f"{label}（无连续成交）"
        if phase in (SessionPhase.CLOSED, SessionPhase.POST):
            try:
                nxt = self.calendar.next_open()
            except Exception:                      # noqa: BLE001
                return label
            return f"{label} · 下次开盘 {nxt.strftime('%m-%d %H:%M')}"
        return label

    def status(self) -> dict:
        state = self._state()
        # ⚠ 时段判定优先用**引擎实际评估过的** phase（`state.session`），
        # 而不是拿墙钟重算（IT-P1-SERVE-REPLAY-001）。
        #
        # 为什么：`serve --replay` 用合成行情 + 假时钟推进，墙钟仍是凌晨，
        # 于是 `calendar.phase()` 返回 closed。前端就会显示"休市"，
        # 而左栏同时滚动着 63 条 09:40 的告警 —— 自相矛盾，
        # 用户会以为数据是假的或坏了。
        #
        # `state.session` 由 `poll_once` 在**同一轮**用引擎自己的时钟写入
        # （engine.py:1049 `self.state.session = phase`），所以它才是
        # "这批数据属于哪个时段"的正确答案。
        # 回退到墙钟只为覆盖"引擎一轮都还没跑"的启动瞬间。
        session_phase = getattr(state, "session", None) if state is not None else None
        if session_phase is not None:
            phase = session_phase
        else:
            phase = self.calendar.phase()
        with self._lock:
            total = self._total
            by_kind = dict(self._by_kind)
            poll_ms, poll_count = self._last_poll_ms, self._poll_count
            health = list(self._source_health)
            notes = list(self._notes)
            last_poll_ts = self._last_poll_ts
        quotes = getattr(state, "quotes", {}) if state is not None else {}
        watch = list(getattr(self._engine, "watchlist", []) or []) if self._engine else []
        # IT-P2-UNIVERSE-STATUS-CACHE-001：真正的"本轮扫描池"是引擎的 `_codes`，
        # **不是** `state.quotes`（累计缓存，含历史准入过的代码与指数，从不裁剪）。
        # 取不到就不猜 —— 让消费方看到 None 而不是一个漂亮但错的数。
        active_universe: int | None = None
        if self._engine is not None:
            try:
                ac = getattr(self._engine, "_codes", None)
                if ac is not None:
                    active_universe = len(list(ac))
            except Exception:  # noqa: BLE001 —— 展示字段，取不到就退化
                active_universe = None
        with self._lock:
            observation = dict(self._observation)
            # 账本**自身**的身份（IT-P2-OBS-STATUS-001）。必须与
            # ``observation`` 在同一临界区读出，否则可能读到"新 seq 配旧账本"。
            obs_seq = int(self._observation_seq)
            obs_observed_at = (None if self._observation_observed_at is None
                               else str(self._observation_observed_at))
            obs_poll_count = int(self._observation_poll_count)
        return {
            "phase": phase.value,
            "session": PHASE_CN.get(phase, phase.value),
            "session_desc": self._describe_phase(phase),
            "is_open": phase in (SessionPhase.MORNING, SessionPhase.AFTERNOON),
            "uptime_s": round(time.time() - self._started, 1),
            # IT-P2-UNIVERSE-STATUS-CACHE-001：**不能**用 ``len(state.quotes)``。
            #
            # ``state.quotes`` 是**累计**最新报价缓存（``engine.py:302`` 是唯一
            # 写入，``prune()`` **不回收**它），所以它包含**历史上准入过但本轮已
            # 不在扫描池里**的代码与指数 —— 实测扫描范围缩 30% 后它**仍报旧规模**。
            #
            # 看板"股票池"芯片读的就是这个字段 -> 会**高报**扫描范围。
            # 真值是本轮实际扫描的代码数（``sample['universe']`` 同口径）。
            # 拿不到就把键省掉，让消费方知道"没测"，而不是给一个漂亮但错的数。
            "universe": (active_universe if active_universe is not None
                         else len(quotes)),
            # 同一字段的两个口径必须都能读到，否则没法判断上面那个数是哪个：
            #   universe        = 本轮实际扫描池（引擎 `_codes`），**递减会跟着变**
            #   universe_cached = 累计最新报价缓存规模（旧口径，只会涨）
            # 实测：扫描范围缩 30% 时二者会差出上千只。旧名保留兼容。
            "universe_cached": len(quotes),
            "alerts_total": total,
            "by_kind": by_kind,
            "sources": health,
            "last_poll_ms": poll_ms,
            "poll_count": poll_count,
            "last_poll_ts": last_poll_ts,
            "watchlist": len(watch),
            "title": str(self.settings.get("web.title", "A股盘中雷达")),
            "dry_run": self.settings.dry_run,
            "replay": self.settings.replay,
            "subscribers": self.subscriber_count,
            "notes": notes,
            # 本轮观测账本：requested/returned/admitted/future_rejected/… +
            # capability 集合。让"Sina 期间放量规则不可评估"能被查到，
            # 而不是只看到 alerts=0（IT-P0-002-R1 / WP02）。
            "observation": observation,
            # 账本**自己的**身份，而不是"这次请求"的。外部 API 消费者据此
            # 判断读到的是本轮账本还是上一成功轮的旧账：``observation_seq``
            # 没变 = 本轮没产生新账本，``observation_observed_at`` 让你能算
            # age（顶层 ``ts`` 是请求时刻，**不能**代替它）。
            # 无账本：seq=0 / observed_at=None / poll_count=0。
            "observation_seq": obs_seq,
            "observation_observed_at": obs_observed_at,
            "observation_poll_count": obs_poll_count,
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def recent_alerts(self, limit: int = 100, kind: str | None = None) -> list[dict]:
        try:
            n = max(1, min(int(limit), 1000))
        except (TypeError, ValueError):
            n = 100
        with self._lock:
            items = list(self._alerts)
            acked = set(self._acked)
        if kind:
            items = [a for a in items if a.get("kind") == kind]
        out = []
        for a in items[:n]:
            a = dict(a)
            a["acked"] = a.get("key") in acked
            out.append(a)
        return out

    def alerts_total(self) -> int:
        with self._lock:
            return self._total

    def ack(self, key: str) -> bool:
        if not key:
            return False
        with self._lock:
            # IT-P1-ACK-TRIM-ORDER-001：`_acked` 必须是**有序**容器。
            #
            # 以前是 `set`，裁剪写 `set(list(self._acked)[-2000:])` ——
            # 而 `list(set)` 的顺序是**哈希序**，不是插入序，
            # 所以 `[-2000:]` 取的是**任意** 2000 条，会
            # **系统性地丢掉刚 ack 的那条**（实测 `set(_acked) == 最新2000`
            # 在 5 个 PYTHONHASHSEED 下**全为 False**，seed=0 时连最新 ack
            # 都留不下）。
            #
            # 用户可见形式：某条告警仍在 300 条 ring buffer 内（看板可见）、
            # 用户已点过"确认"，但 `recent_alerts()` 报 `acked=False`，
            # 看板于是**把已确认的告警重新变回未确认**并重新显示 ack 按钮。
            #
            # `dict` 保持插入序，`pop` 再赋可以刷新"最近 ack"的时序。
            k = str(key)
            self._acked.pop(k, None)
            self._acked[k] = None
            if len(self._acked) > 5000:
                # 只保留**最新** 2000（按 ack 时序），不是任意 2000。
                keep = list(self._acked)[-2000:]
                self._acked = dict.fromkeys(keep)
        return True

    # --- 行情榜单 -------------------------------------------------------
    def _quote_item(self, q: Quote, state) -> dict:
        s1 = s5 = 0.0
        if state is not None:
            now_ep = time.time()
            try:
                v1 = state.price_change(q.code, 60, now_ep)
                v5 = state.price_change(q.code, 300, now_ep)
                s1 = float(v1) if v1 is not None else 0.0
                s5 = float(v5) if v5 is not None else 0.0
            except Exception:  # noqa: BLE001
                pass
        return {
            "code": q.code, "name": q.name, "price": round(q.price, 3),
            "pct": round(q.pct, 2), "speed_1m": round(s1, 2), "speed_5m": round(s5, 2),
            "volume_ratio": round(q.volume_ratio, 2), "amount": round(q.amount, 0),
            "turnover": round(q.turnover, 2), "board": q.board.value,
            "vwap": round(q.vwap, 3), "above_vwap": bool(q.above_vwap),
            "limit_up": round(q.limit_up_price, 3), "amplitude": round(q.amplitude, 2),
            "high": round(q.high, 3), "low": round(q.low, 3), "open": round(q.open, 3),
            "prev_close": round(q.prev_close, 3),
        }

    def top_quotes(self, limit: int = 30, sort: str = "speed") -> list[dict]:
        state = self._state()
        if state is None:
            return []
        try:
            n = max(1, min(int(limit), 200))
        except (TypeError, ValueError):
            n = 30
        key = sort if sort in _SORTS else "speed"
        quotes = list(getattr(state, "quotes", {}).values())
        items = [self._quote_item(q, state) for q in quotes if not q.is_suspended]
        items.sort(key=_SORTS[key])
        return items[:n]

    def watchlist_quotes(self) -> list[dict]:
        state = self._state()
        if state is None or self._engine is None:
            return []
        codes = list(getattr(self._engine, "watchlist", []) or [])
        qmap = getattr(state, "quotes", {})
        return [self._quote_item(qmap[c], state) for c in codes if c in qmap]

    def series(self, code: str, limit: int = 240) -> list[dict]:
        with self._lock:
            buf = self._series.get(code)
            items = list(buf) if buf else []
        if limit and limit > 0:
            items = items[-int(limit):]
        return items

    def series_codes(self) -> list[str]:
        with self._lock:
            return list(self._series_codes)

    # ------------------------------------------------------------------
    def snapshot_payload(self, top_n: int = 30) -> dict:
        """SSE tick 事件用的聚合负载。"""
        return {
            "status": self.status(),
            "quotes": self.top_quotes(top_n, "speed"),
            "watchlist": self.watchlist_quotes(),
            "ts": datetime.now().strftime("%H:%M:%S"),
        }
