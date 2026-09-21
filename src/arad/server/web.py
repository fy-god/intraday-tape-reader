"""实时看板 HTTP 服务：纯标准库 http.server + SSE。

契约见 `docs/DATA_CONTRACT.md` 第 7 节。**唯一数据来源是引擎提供的 AlertStore**，
本模块不抓任何行情、不造任何数据。

AlertStore 接口（由引擎实现，本模块只消费）::

    status() -> dict
    recent_alerts(limit=100, kind=None) -> list[dict]     # 倒序
    top_quotes(limit=30, sort="speed") -> list[dict]
    watchlist_quotes() -> list[dict]
    series(code, limit=240) -> list[dict]                 # [{"t":epoch,"price":float,"pct":float}]
    subscribe() -> queue.Queue                            # {"event":"alert"|"tick","data":{...}}
    unsubscribe(q) -> None
    ack(key) -> bool

对外接口::

    create_server(store, cfg=None, host=None, port=None) -> ThreadingHTTPServer
    serve(store, cfg=None, block=True)
    NullStore                       # 无引擎时单机调试用的空实现

路由表
------
===  ======================  =======================================
GET  /                       内置 dashboard.html（自包含、无外链）
GET  /api/status              store.status()
GET  /api/quotes              {"items":[...],"ts":...}   limit<=200
GET  /api/alerts              {"items":[...],"total":n}  limit<=1000
GET  /api/spirit              {"items":[feed...],"groups":[...]}  limit<=1000
GET  /api/watchlist           {"items":[...]}
GET  /api/series              {"code":...,"items":[...]}
GET  /api/stream              SSE（alert / spirit / tick + 心跳）
POST /api/ack                 {"ok":true}   body(JSON/表单) 或 query 里的 key
GET  /api/health              {"ok":true,"ts":...}
===  ======================  =======================================

短线精灵（spirit）
------------------
``/api/spirit`` 与 SSE 的 ``spirit`` 事件返回的是 :func:`arad.spirit.to_feed_item`
的输出（中文名/方向/分组/中文释义都在 ``spirit.SIGNALS`` 里定义），前端只负责画，
**不允许在 JS 里重新实现信号名映射**——两处各写一份必然慢慢说两套话。

为什么在 ``alert`` 事件之外**额外**发一条 ``spirit``：``alert`` 是引擎契约的原始
形状（kind/metrics/detail），``spirit`` 是展示形状；分开之后前端拿到就能直接渲染，
而既有消费方（日志、其它客户端、44 个旧测试）看到的 ``alert`` 一个字节都没变。

已知缺口（不在本模块修）：``spirit.to_feed_item`` 自己**不清洗非有限浮点** ——
它直接透传 ``alert.price`` / ``alert.pct`` / ``metrics``。正常链路里
``Alert.to_dict()`` 已经把 NaN/Inf 转成 None，所以 store 出来的 dict 是干净的；
但拿一个手搓的 ``Alert(price=float('nan'))`` 直接调 ``to_feed_item`` 会拿到 NaN。
本模块因此在两处兜底：``feed_item_of`` 做 float 归一，``dumps_json`` 出网前再清一遍
（``allow_nan=False`` + 递归替换），保证写出去的一定是合法 JSON。
"""
from __future__ import annotations

import json
import logging
import queue
import re
import socket
import sys
import threading
import time
from datetime import datetime
from errno import ECONNABORTED, ECONNRESET, ENOTCONN, EPIPE
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, unquote_plus, urlparse

from .. import spirit as spirit_mod

__all__ = [
    "create_server",
    "serve",
    "NullStore",
    "DashboardHandler",
    "RadarHTTPServer",
    "WEB_DEFAULTS",
    "load_dashboard_html",
]

logger = logging.getLogger("arad.web")

HERE = Path(__file__).resolve().parent
DASHBOARD_PATH = HERE / "dashboard.html"

# --------------------------------------------------------------------------
# 默认配置（优先级：显式参数 > cfg > settings.yaml web 节 > 本表）
# --------------------------------------------------------------------------
WEB_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "host": "127.0.0.1",
    "port": 8899,
    "sse_interval": 2,       # SSE 心跳/榜单推送间隔（秒）
    "sse_timeout": 3600,     # 单条 SSE 连接最长存活（秒），0=不限制；到期后前端自动重连
    "sse_poll": 0.5,         # SSE 循环内部轮询粒度（秒）
    "top_n": 30,             # 榜单默认条数
    "max_alerts": 300,       # 前端保留告警条数（同时作为 /api/alerts 默认 limit）
    "max_spirit": 200,       # 前端短线精灵保留行数（同时作为 /api/spirit 默认 limit）
    "title": "A股盘中雷达 · 急拉急跌预警",
}

MAX_QUOTES_LIMIT = 200        # /api/quotes limit 硬上限（契约要求 clamp 到 200）
MAX_ALERTS_LIMIT = 1000       # /api/alerts limit 硬上限
MAX_SPIRIT_LIMIT = 1000       # /api/spirit limit 硬上限
MAX_SERIES_LIMIT = 2000       # /api/series limit 硬上限
ALERT_TOTAL_PROBE = 1000      # 带 kind 过滤时统计 total 的最大探测条数

# 短线精灵分组的中文名与展示顺序。
# 顺序与 `arad.spirit.SIGNALS` 里注释的分组顺序一致；`other` 兜底放在最后。
# 分组名本身从注册表实时推导（见 spirit_groups()），这里只负责"怎么显示"，
# 免得以后新增一个分组时前端悄悄漏掉。
SPIRIT_GROUP_CN: dict[str, str] = {
    "price": "价格异动", "order": "盘口委托", "limit": "涨跌停",
    "index": "指数", "pattern": "形态", "other": "其它",
}
SPIRIT_GROUP_ORDER = ("price", "order", "limit", "index", "pattern", "other")

# top_quotes 的排序键。
# 引擎侧 `arad/store.py` 的 `_SORTS` 只认：speed / speed5 / pct / up / down /
# amount / volume_ratio。前端表头用的是列名，所以这里做一层显式别名映射，
# **绝不把引擎不认识的键透传下去**（真实 store 会静默退化成 speed，看板会看起来
# 「排了但没排」；有的实现则直接抛错）。
ENGINE_SORTS = frozenset({
    "speed", "speed5", "pct", "up", "down", "amount", "volume_ratio",
})
# 前端列名 -> 引擎排序键（表头 data-sort 与这里保持一致）
SORT_ALIASES = {
    "speed_1m": "speed", "speed_5m": "speed5",
    "turnover": "amount", "amplitude": "pct",
    "price": "pct", "code": "speed", "name": "speed",
}
ALLOWED_SORTS = ENGINE_SORTS | frozenset(SORT_ALIASES)


def resolve_sort(raw: Any) -> tuple[str, str]:
    """把前端传来的 sort 解析成 (requested, engine_key)。非法一律回落 speed。"""
    requested = str(raw or "speed").strip().lower() or "speed"
    if requested not in ALLOWED_SORTS:
        return "speed", "speed"
    return requested, SORT_ALIASES.get(requested, requested)

_CODE_RE = re.compile(r"^\d{6}$")
_MAX_BODY = 64 * 1024
# socket.recv 在 Windows 上抛的「对端已断开」errno
_DISCONNECT_ERRNOS = frozenset({ECONNRESET, ECONNABORTED, ENOTCONN, EPIPE})

# dashboard.html 读取失败时的兜底页（保证 / 永远不 500）
_FALLBACK_HTML = (
    "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
    "<title>A股盘中雷达</title></head><body>"
    "<h1>A股盘中雷达</h1><p>dashboard.html 读取失败，请检查部署文件。</p>"
    "</body></html>"
)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def now_iso() -> str:
    """北京时间 naive 字符串（契约第 0 节：全部用北京时间）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clamp_int(raw: Any, default: int, lo: int, hi: int) -> int:
    """把 query 参数夹到 [lo, hi]；非法值用 default（同样夹紧）。"""
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        val = default
    return max(lo, min(hi, val))


def _first(query: dict[str, list[str]], key: str, default: str = "") -> str:
    vals = query.get(key)
    if not vals:
        return default
    return (vals[0] or "").strip()


def normalize_code(raw: Any) -> str:
    """'sh600000' / 'SH600000' / ' 600000 ' -> '600000'；非法返回 ''。"""
    s = str(raw or "").strip().upper()
    if s[:2] in ("SH", "SZ", "BJ"):
        s = s[2:]
    return s if _CODE_RE.match(s) else ""


def _as_list(value: Any) -> list:
    """把 store 的返回值安全转成 list（None -> []）。"""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set)):
        return list(value)
    return [value]


def load_dashboard_html(refresh: bool = False) -> bytes:
    """读取内置看板 HTML（按 mtime 缓存，开发时可热改）。"""
    try:
        stat = DASHBOARD_PATH.stat()
    except OSError:
        return _FALLBACK_HTML.encode("utf-8")
    cache = load_dashboard_html._cache  # type: ignore[attr-defined]
    mtime = stat.st_mtime
    if not refresh and cache and cache[0] == mtime:
        return cache[1]
    try:
        data = DASHBOARD_PATH.read_bytes()
    except OSError:
        return _FALLBACK_HTML.encode("utf-8")
    load_dashboard_html._cache = (mtime, data)  # type: ignore[attr-defined]
    return data


load_dashboard_html._cache = None  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# 短线精灵：把 store 里的原始告警转成前端滚动列表用的 feed item
# --------------------------------------------------------------------------
def spirit_groups() -> list[dict]:
    """短线精灵的分组清单（前端筛选按钮用）。

    **分组名从 ``arad.spirit.SIGNALS`` 实时推导**，不在前端硬编码：
    以后加一个 ``group`` 只会出现在注册表里，前端自动多一个按钮。
    展示顺序走 ``SPIRIT_GROUP_ORDER``，没列到的分组排在后面（名字兜底）。
    """
    seen: dict[str, int] = {}
    try:
        for sig in spirit_mod.SIGNALS.values():
            g = str(getattr(sig, "group", "") or "other")
            seen[g] = seen.get(g, 0) + 1
    except Exception as exc:                         # noqa: BLE001 —— 注册表坏了也要能起页
        logger.warning("spirit.SIGNALS 读取失败: %r", exc)
    order = {g: i for i, g in enumerate(SPIRIT_GROUP_ORDER)}
    groups = sorted(seen, key=lambda g: (order.get(g, len(order)), g))
    return [{"group": g, "cn": SPIRIT_GROUP_CN.get(g, g), "count": seen[g]}
            for g in groups]


def feed_item_of(raw: Any) -> dict | None:
    """把一条**已存成 dict** 的告警转成 feed item；转不了就返回 None。

    为什么需要它：``spirit.to_feed_item`` 收的是 :class:`arad.models.Alert`，
    而 ``store.recent_alerts()`` / SSE 推的是 ``Alert.to_dict()`` 的 dict。
    这里把 dict 还原成 Alert 再走**同一套**注册表逻辑，绝不另写一份映射。

    脏数据一律返回 None（调用方跳过该行）：
    真实 store 可能塞进 None、缺字段、ts 不是时间、metrics 不是 dict 的条目，
    一条坏数据不能让整页 ``/api/spirit`` 变 500，也不能毒死整条 SSE 连接。
    """
    if not isinstance(raw, dict):
        return None
    if not str(raw.get("key") or "").strip():
        # 没有 key 的行既不能去重也不能标记已读（AlertStore 契约里 key 就是身份），
        # 前端 pushAlert 本来就丢弃它，这里提前挡掉，省得 SSE 重连时刷出一堆"幽灵行"。
        logger.debug("告警缺少 key，跳过: %r", raw)
        return None
    try:
        from ..models import Alert, AlertKind

        kind_raw = raw.get("kind")
        try:
            kind = AlertKind(kind_raw)
        except (ValueError, TypeError):
            kind = kind_raw                        # 认不出就原样塞，spirit 会退回 signal_of
        alert = Alert(
            key=str(raw.get("key") or ""),
            kind=kind,                             # type: ignore[arg-type]
            code=str(raw.get("code") or ""),
            name=str(raw.get("name") or ""),
            ts=coerce_ts(raw.get("ts")),
            price=coerce_float(raw.get("price")),
            pct=coerce_float(raw.get("pct")),
            title=str(raw.get("title") or ""),
            detail=str(raw.get("detail") or ""),
            severity=coerce_int(raw.get("severity"), 2),
            metrics=raw.get("metrics") if isinstance(raw.get("metrics"), dict) else {},
            # IT-P1-WEB-SIGNAL-ID-LOSS-001：**必须**把 signal_id 带过去。
            # ``Alert.to_dict()`` 会导出它，这里重建时若丢掉，
            # 则任何走"落盘 -> 重建 -> 再导出"的路径（SSE 重放、快照恢复、
            # 前端二次处理）都会把交付账本的身份抹掉 —— 表现为这些告警
            # 在账本里"缺 signal_id"，从而被全局门禁判为不合规。
            # 这是**信息在往返中丢失**，不是规则没填。
            signal_id=str(raw.get("signal_id") or ""),
        )
        return spirit_mod.to_feed_item(alert)
    except Exception as exc:                         # noqa: BLE001 —— 单条坏数据只丢它自己
        logger.debug("to_feed_item 失败，跳过该条: %r", exc)
        return None


def coerce_ts(value: Any) -> datetime | None:
    """尽量把 ``to_dict()`` 的 ``"YYYY-mm-dd HH:MM:SS"`` 还原成 datetime。"""
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt == "%H:%M:%S":                      # 只有时间：补上今天
            return dt.replace(year=datetime.now().year, month=datetime.now().month,
                              day=datetime.now().day)
        return dt
    try:
        return datetime.fromisoformat(text)        # 带 T / 带毫秒的 ISO 串
    except ValueError:
        return None


def coerce_float(value: Any) -> float:
    """非有限值（NaN/Inf）一律归零 —— 它们会变成非法 JSON 打死 SSE 流。"""
    import math

    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) else 0.0


def coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def scrub_non_finite(obj: Any) -> Any:
    """递归把 NaN / ±Inf 换成 None。

    ``json.dumps`` 默认会输出**裸** ``NaN``/``Infinity`` —— 那是非法 JSON，
    浏览器 ``JSON.parse`` 直接抛错，整条 SSE 推送就废了（本项目踩过这个坑）。
    这里作为 ``allow_nan=False`` 失败后的兜底，保证出网的一定是合法 JSON。
    """
    import math

    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: scrub_non_finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub_non_finite(v) for v in obj]
    return obj


def dumps_json(obj: Any) -> str:
    """出网统一的 JSON 序列化：先严格模式，失败再清洗非有限浮点。"""
    try:
        return json.dumps(obj, ensure_ascii=False, default=str, allow_nan=False)
    except (ValueError, TypeError):
        return json.dumps(scrub_non_finite(obj), ensure_ascii=False, default=str,
                          allow_nan=False)


# --------------------------------------------------------------------------
# NullStore：无引擎时单独起服务调试
# --------------------------------------------------------------------------
class NullStore:
    """所有方法返回空/合理默认值的 AlertStore 替身。

    不产生任何数据、不联网，仅用于「没有引擎时把服务先跑起来」看 UI。
    """

    def __init__(self, title: str = WEB_DEFAULTS["title"], dry_run: bool = True) -> None:
        self.title = title
        self.dry_run = dry_run
        self._t0 = time.time()
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    # --- 读接口 --------------------------------------------------------
    def status(self) -> dict:
        return {
            "phase": "closed",
            "session": "closed",
            "uptime_s": round(max(0.0, time.time() - self._t0), 1),
            "universe": 0,
            "alerts_total": 0,
            "sources": [],
            "last_poll_ms": 0,
            "poll_count": 0,
            "watchlist": 0,
            "title": self.title,
            "dry_run": self.dry_run,
            "last_poll_ts": "",
        }

    def recent_alerts(self, limit: int = 100, kind: str | None = None) -> list[dict]:
        return []

    def top_quotes(self, limit: int = 30, sort: str = "speed") -> list[dict]:
        return []

    def watchlist_quotes(self) -> list[dict]:
        return []

    def series(self, code: str, limit: int = 240) -> list[dict]:
        return []

    # --- 订阅 ----------------------------------------------------------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def ack(self, key: str) -> bool:
        return False

    # --- 调试辅助 ------------------------------------------------------
    @property
    def subscribers(self) -> int:
        with self._lock:
            return len(self._subs)

    def publish(self, event: str, data: Any) -> int:
        """手动往所有订阅者推事件（本地调试 UI 用）。"""
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait({"event": event, "data": data})
            except queue.Full:
                pass
        return len(subs)


# --------------------------------------------------------------------------
# RequestHandler
# --------------------------------------------------------------------------
class DashboardHandler(BaseHTTPRequestHandler):
    """看板 HTTP 处理器。store / cfg 由 create_server 绑到子类上。"""

    store: Any = None
    cfg: dict[str, Any] = dict(WEB_DEFAULTS)
    server_version = "arad-dashboard/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"     # 支持 keep-alive，SSE 才能长连接

    # --- 日志降噪：默认静默，需要时打开 logging -------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D102
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def log_error(self, fmt: str, *args: Any) -> None:  # noqa: D102
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def log_request(self, code: Any = "-", size: Any = "-") -> None:  # noqa: D102
        logger.debug("%s %s -> %s", self.command, self.path, code)

    # --- HTTP 方法 ------------------------------------------------------
    def do_GET(self) -> None:            # noqa: N802
        self._safe_handle()

    def do_POST(self) -> None:           # noqa: N802
        self._safe_handle()

    def do_HEAD(self) -> None:           # noqa: N802
        self._method_not_allowed()

    def do_PUT(self) -> None:            # noqa: N802
        self._method_not_allowed()

    def do_DELETE(self) -> None:         # noqa: N802
        self._method_not_allowed()

    def do_PATCH(self) -> None:          # noqa: N802
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:        # noqa: N802
        self._method_not_allowed()

    # --- 发送工具 -------------------------------------------------------
    def _send_bytes(self, status: int, body: bytes, content_type: str,
                    extra: dict[str, str] | None = None,
                    cache_control: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self._responded = True
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _send_json(self, status: int, obj: Any) -> None:
        body = dumps_json(obj).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_text(self, status: int, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
        self._send_bytes(status, text.encode("utf-8"), content_type)

    def _method_not_allowed(self) -> None:
        self._responded = False
        try:
            self._send_json(405, {"error": "method not allowed", "method": self.command})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    # --- 总调度 ---------------------------------------------------------
    def _safe_handle(self) -> None:
        self._responded = False
        try:
            self._dispatch()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception as exc:                     # noqa: BLE001 —— 任何异常都不许打崩服务
            logger.warning("handler error %s %s: %r", self.command, self.path, exc)
            if not self._responded:
                try:
                    self._send_json(500, {"error": str(exc), "type": type(exc).__name__})
                except Exception:                    # noqa: BLE001
                    self.close_connection = True
            else:
                self.close_connection = True

    def _dispatch(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path or "/"
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/") or "/"
        query = parse_qs(parsed.query, keep_blank_values=True)
        method = self.command

        known = path in _KNOWN_PATHS
        if method == "GET":
            handler = _GET_ROUTES.get(path)
            if handler is not None:
                return handler(self, query)
        elif method == "POST":
            handler = _POST_ROUTES.get(path)
            if handler is not None:
                return handler(self, query)

        # 路径对但方法不对 -> 405；路径不存在 -> 404
        if known:
            return self._method_not_allowed()
        return self._send_json(404, {"error": "not found", "path": parsed.path})

    # --- 路由实现 -------------------------------------------------------
    def _route_index(self) -> None:
        html = load_dashboard_html()
        self._send_bytes(200, html, "text/html; charset=utf-8",
                         extra={"X-Content-Type-Options": "nosniff"})

    def _route_status(self) -> None:
        st = self.store.status()
        if not isinstance(st, dict):
            raise TypeError(f"store.status() 必须返回 dict，实际 {type(st).__name__}")
        self._send_json(200, st)

    def _route_quotes(self, query: dict[str, list[str]]) -> None:
        default_limit = _clamp_int(self.cfg.get("top_n", 30), 30, 1, MAX_QUOTES_LIMIT)
        limit = _clamp_int(_first(query, "limit", ""), default_limit, 1, MAX_QUOTES_LIMIT)
        requested, sort = resolve_sort(_first(query, "sort", "speed"))
        items = self._safe_top_quotes(limit, sort)
        self._send_json(200, {
            "items": items, "ts": now_iso(), "limit": limit,
            "sort": requested, "sort_key": sort,
        })

    def _route_alerts(self, query: dict[str, list[str]]) -> None:
        default_limit = _clamp_int(self.cfg.get("max_alerts", 100), 100, 1, MAX_ALERTS_LIMIT)
        limit = _clamp_int(_first(query, "limit", ""), default_limit, 1, MAX_ALERTS_LIMIT)
        kind = _first(query, "kind", "")
        kind = None if kind.lower() in ("", "all", "全部") else kind
        items = _as_list(self.store.recent_alerts(limit, kind))
        self._send_json(200, {
            "items": items,
            "total": self._alerts_total(items, kind),
            "count": len(items),
            "limit": limit,
            "kind": kind,
        })

    def _alerts_total(self, items: list, kind: str | None) -> int:
        """total = 与过滤条件匹配的告警总数（不因 limit 截断而变）。"""
        try:
            st = self.store.status()
            if isinstance(st, dict):
                if kind is None:
                    if st.get("alerts_total") is not None:
                        return int(st["alerts_total"])
                else:
                    # IT-P1-ALERT-TOTAL-KIND-TRUNCATION-001：**不能用
                    # ``len(recent_alerts(probe, kind))``** —— Store 的告警缓冲是
                    # ``deque(maxlen=web.max_alerts)``（默认 300），
                    # 探测条数再大也只能拿到 maxlen 条。实测灌入 450 条
                    # ``limit_up`` 后：``alerts_total()`` = 450（真值）、
                    # ``len(recent_alerts(1000, 'limit_up'))`` = **300**。
                    # 于是 ``/api/alerts?kind=limit_up`` 的 total 永远 ≤ 300，
                    # 而**不带** kind 的同一字段走 ``status()['alerts_total']``
                    # 却是正确的 450 —— 同一个展示字段两条路径语义不一致。
                    #
                    # 正确来源是 ``store`` 的**累计** ``_by_kind`` 计数
                    # （``status()['by_kind']``，它不随 ring buffer 驱逐而减少）。
                    by_kind = st.get("by_kind")
                    if isinstance(by_kind, dict) and kind in by_kind:
                        return int(by_kind[kind])
                    # 退化路径：store 没导出 by_kind（老实现 / 测试替身）时，
                    # 仍按旧口径探测。**它会被 ring buffer 截断** ——
                    # 所以只是"比 len(items) 好一点"，不是正确来源。
                    return len(_as_list(
                        self.store.recent_alerts(ALERT_TOTAL_PROBE, kind)))
        except Exception as exc:                     # noqa: BLE001 —— total 只是展示字段，失败就退化
            logger.debug("alerts total probe failed: %r", exc)
        return len(items)

    # --- 短线精灵 -------------------------------------------------------
    def _spirit_items(self, limit: int) -> list[dict]:
        """取最近 limit 条告警并转成 feed item；坏行跳过、异常退化空列表。

        ``limit`` 是**转换后**要的条数。存储里可能混着转不了的脏数据，
        所以先多要一点（1.5 倍 + 20 条）再截断，避免脏数据把列表挤空；
        多要失败（有的 store 会对大 limit 报错）就退回按 limit 取。
        """
        probe = min(MAX_SPIRIT_LIMIT, limit + max(20, limit // 2))
        try:
            raw = _as_list(self.store.recent_alerts(probe, None))
        except Exception as exc:                     # noqa: BLE001
            logger.debug("recent_alerts(%s) failed, retry with %s: %r", probe, limit, exc)
            raw = _as_list(self.store.recent_alerts(limit, None))
        out: list[dict] = []
        for item in raw:
            feed = feed_item_of(item)
            if feed is None:
                continue                             # 坏行只丢它自己，不影响其它行
            out.append(feed)
            if len(out) >= limit:
                break
        return out

    def _route_spirit(self, query: dict[str, list[str]]) -> None:
        default_limit = _clamp_int(self.cfg.get("max_spirit", 200), 200, 1, MAX_SPIRIT_LIMIT)
        limit = _clamp_int(_first(query, "limit", ""), default_limit, 1, MAX_SPIRIT_LIMIT)
        items = self._spirit_items(limit)
        self._send_json(200, {
            "items": items,
            "count": len(items),
            "limit": limit,
            "groups": spirit_groups(),
            "ts": now_iso(),
        })

    def _route_watchlist(self) -> None:
        self._send_json(200, {"items": _as_list(self.store.watchlist_quotes()), "ts": now_iso()})

    def _route_series(self, query: dict[str, list[str]]) -> None:
        code = normalize_code(_first(query, "code", ""))
        if not code:
            return self._send_json(400, {"error": "invalid or missing code", "code": _first(query, "code", "")})
        limit = _clamp_int(_first(query, "limit", ""), 240, 1, MAX_SERIES_LIMIT)
        items = _as_list(self.store.series(code, limit))
        self._send_json(200, {"code": code, "items": items, "limit": limit, "ts": now_iso()})

    def _route_health(self) -> None:
        self._send_json(200, {"ok": True, "ts": now_iso(), "service": "arad-dashboard"})

    def _route_ack(self, query: dict[str, list[str]]) -> None:
        payload = self._read_body_params()
        key = (payload.get("key") or _first(query, "key", "") or "").strip()
        if not key:
            return self._send_json(400, {"ok": False, "error": "missing key"})
        acked = bool(self.store.ack(key))
        self._send_json(200, {"ok": True, "key": key, "acked": acked})

    # --- SSE ------------------------------------------------------------
    def _route_stream(self) -> None:
        """SSE：每连接一个生成器循环，断开/超时后必须 unsubscribe。"""
        store = self.store
        q = store.subscribe()                        # 失败则抛到 _safe_handle -> 500 JSON
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self._responded = True
            self.close_connection = True             # 流结束即关闭连接，前端 EventSource 自动重连
            self.wfile.write(b"retry: 3000\n\n")
            for chunk in self._sse_events(q):
                self.wfile.write(chunk)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            pass                                     # 客户端断开是常态
        except OSError as exc:                       # socket 级错误同样按断开处理
            logger.debug("sse socket closed: %r", exc)
        finally:
            try:
                store.unsubscribe(q)
            except Exception as exc:                 # noqa: BLE001 —— 清理失败也不能让线程挂掉
                logger.warning("unsubscribe failed: %r", exc)
            self.close_connection = True

    def _sse_events(self, q: queue.Queue) -> Iterator[bytes]:
        """按 sse_interval 产出事件块；调用方负责写 socket 与清理。"""
        interval = float(self.cfg.get("sse_interval") or 2.0) or 2.0
        if interval < 0.05:
            interval = 0.05
        max_seconds = float(self.cfg.get("sse_timeout") or 0.0) or 0.0
        poll = float(self.cfg.get("sse_poll") or 0.5) or 0.5
        if poll > interval:
            poll = interval
        started = time.monotonic()
        last_tick = 0.0                              # 0 = 立刻发首帧
        while True:
            if self._stopping():
                yield self._sse_event("bye", {"ts": now_iso(), "reason": "server_shutdown"})
                return
            if max_seconds and (time.monotonic() - started) >= max_seconds:
                yield self._sse_event("bye", {"ts": now_iso(), "reason": "timeout"})
                return
            if self._peer_gone():
                # 客户端已断开：必须结束循环，让 finally 里的 unsubscribe 立刻执行。
                # （小 chunk 写入会落进 socket 缓冲，单靠 BrokenPipeError 察觉不到）
                logger.debug("sse peer gone, closing stream")
                return

            drained = 0
            while drained < 200:                     # 排空积压事件，避免落后
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    break
                except Exception as exc:             # noqa: BLE001
                    logger.debug("sse queue error: %r", exc)
                    break
                drained += 1
                for chunk in self._expand_queue_item(item):
                    yield chunk

            now = time.monotonic()
            if (now - last_tick) >= interval:        # 心跳 + 榜单
                last_tick = now
                yield self._sse_event("tick", self._tick_payload())

            wait = min(poll, max(0.02, interval - (time.monotonic() - last_tick)))
            try:
                item = q.get(timeout=wait)
            except queue.Empty:
                continue
            except Exception as exc:                 # noqa: BLE001
                logger.debug("sse queue error: %r", exc)
                continue
            for chunk in self._expand_queue_item(item):
                yield chunk

    def _expand_queue_item(self, item: Any) -> Iterator[bytes]:
        """把订阅队列里的一项展开成 1~2 个 SSE 事件块。

        ``alert`` 事件**原样**照发（既有消费方依赖它的原始形状），
        后面再补一条 ``spirit`` 事件（``to_feed_item`` 的展示形状），
        让短线精灵面板拿到就能直接画，不用在 JS 里重做一遍信号名映射。

        补发失败绝不影响主事件：单条脏告警不能让整个 SSE 流断掉。
        """
        if not isinstance(item, dict):
            yield self._sse_event("message", item)
            return
        name = item.get("event") or "message"
        data = item.get("data")
        yield self._sse_event(name, data)
        if str(name) != "alert":
            return
        try:
            feed = feed_item_of(data)
        except Exception as exc:                     # noqa: BLE001
            logger.debug("spirit feed item 生成失败: %r", exc)
            return
        if feed is not None:
            yield self._sse_event("spirit", feed)

    def _peer_gone(self) -> bool:
        """socket 层探测客户端是否已断开（非阻塞 peek）。

        浏览器关闭页面 / 刷新会发 RST，peek 立刻返回 ECONNRESET → 判定断开；
        正常 keep-alive 连接返回 EWOULDBLOCK/EAGAIN → 判定存活。
        取不到 socket 时返回 False（退化为依赖写入时的 BrokenPipeError）。
        """
        sock = getattr(self, "connection", None)
        if sock is None:
            return False
        try:
            sock.setblocking(False)
        except OSError:
            return False
        try:
            data = sock.recv(1, socket.MSG_PEEK)
        except (BlockingIOError, InterruptedError):
            return False                             # 无数据可读 = 连接仍然活着
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            return True
        except OSError as exc:
            return exc.errno in _DISCONNECT_ERRNOS
        finally:
            try:
                sock.setblocking(True)
            except OSError:
                pass
        return not data                              # b"" = 对端已优雅关闭

    def _stopping(self) -> bool:
        ev = getattr(self.server, "stop_event", None)
        return bool(ev is not None and ev.is_set())

    def _tick_payload(self) -> dict:
        """心跳里的榜单+状态；任何 store 故障都退化为空值，绝不让心跳断掉。"""
        return {
            "ts": now_iso(),
            "status": self._safe_status(),
            "quotes": self._safe_quotes_for_tick(self._top_n()),
            "watchlist": self._safe_watchlist(),
        }

    def _top_n(self) -> int:
        return _clamp_int(self.cfg.get("top_n", 30), 30, 1, MAX_QUOTES_LIMIT)

    def _safe_status(self) -> dict:
        try:
            st = self.store.status()
            return st if isinstance(st, dict) else {}
        except Exception as exc:                     # noqa: BLE001
            logger.debug("status() failed in sse tick: %r", exc)
            return {}

    def _safe_watchlist(self) -> list:
        """心跳里的自选股；异常退化为空列表，前端会保留上一次渲染。"""
        try:
            return _as_list(self.store.watchlist_quotes())
        except Exception as exc:                     # noqa: BLE001
            logger.debug("watchlist_quotes() failed in sse tick: %r", exc)
            return []

    def _safe_top_quotes(self, limit: int, sort: str) -> list:
        """榜单读取；未知 sort 导致引擎报错时回落到 speed（仅此一处容错）。"""
        try:
            return _as_list(self.store.top_quotes(limit, sort))
        except Exception:
            if sort != "speed":
                logger.debug("top_quotes(sort=%s) failed, fallback to speed", sort)
                return _as_list(self.store.top_quotes(limit, "speed"))
            raise

    def _safe_quotes_for_tick(self, limit: int) -> list:
        """SSE 心跳里的榜单：任何异常都退化成空列表（连接优先于数据）。"""
        try:
            return _as_list(self.store.top_quotes(limit, "speed"))
        except Exception as exc:                     # noqa: BLE001
            logger.debug("top_quotes() failed in sse tick: %r", exc)
            return []

    @staticmethod
    def _sse_event(name: Any, data: Any) -> bytes:
        payload = dumps_json(data)
        safe_name = str(name or "message").splitlines()[0].strip() or "message"
        out = [f"event: {safe_name}"]
        for line in payload.split("\n"):
            out.append(f"data: {line}")
        return ("\n".join(out) + "\n\n").encode("utf-8")

    # --- 请求体 ---------------------------------------------------------
    def _read_body_params(self) -> dict[str, str]:
        """读 body：JSON 对象 或 application/x-www-form-urlencoded。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, _MAX_BODY))
        if not raw:
            return {}
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            return {}
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "json" in ctype or text[:1] in "{[":
            try:
                obj = json.loads(text)
            except (ValueError, TypeError):
                return {}
            if isinstance(obj, dict):
                return {str(k): ("" if v is None else str(v)) for k, v in obj.items()}
            return {}
        out: dict[str, str] = {}
        for pair in text.split("&"):
            if not pair:
                continue
            k, _, v = pair.partition("=")
            out[unquote_plus(k)] = unquote_plus(v)
        return out


# --------------------------------------------------------------------------
# 路由表：路径 -> handler(self, query)
# --------------------------------------------------------------------------
_GET_ROUTES: dict[str, Any] = {
    "/": lambda h, q: h._route_index(),                       # noqa: SLF001
    "/index.html": lambda h, q: h._route_index(),             # noqa: SLF001
    "/dashboard.html": lambda h, q: h._route_index(),         # noqa: SLF001
    "/api/status": lambda h, q: h._route_status(),            # noqa: SLF001
    "/api/quotes": lambda h, q: h._route_quotes(q),           # noqa: SLF001
    "/api/alerts": lambda h, q: h._route_alerts(q),           # noqa: SLF001
    "/api/spirit": lambda h, q: h._route_spirit(q),           # noqa: SLF001
    "/api/watchlist": lambda h, q: h._route_watchlist(),      # noqa: SLF001
    "/api/series": lambda h, q: h._route_series(q),           # noqa: SLF001
    "/api/stream": lambda h, q: h._route_stream(),            # noqa: SLF001
    "/api/health": lambda h, q: h._route_health(),            # noqa: SLF001
}
_POST_ROUTES: dict[str, Any] = {
    "/api/ack": lambda h, q: h._route_ack(q),                 # noqa: SLF001
}
_KNOWN_PATHS = frozenset(_GET_ROUTES) | frozenset(_POST_ROUTES)


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------
class RadarHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer + store/cfg + 停止信号（用于干净地终结 SSE 线程）。"""

    daemon_threads = True          # 请求线程不阻塞进程退出
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, server_address: tuple[str, int], handler_class: type,
                 store: Any = None, cfg: dict[str, Any] | None = None) -> None:
        self.store = store
        self.cfg = dict(cfg or {})
        self.stop_event = threading.Event()
        super().__init__(server_address, handler_class)

    def shutdown(self) -> None:                      # noqa: D102
        self.stop_event.set()                        # 先通知 SSE 线程收尾
        super().shutdown()

    def server_close(self) -> None:                  # noqa: D102
        self.stop_event.set()
        super().server_close()

    def handle_error(self, request, client_address) -> None:  # noqa: D102
        """客户端 RST/断开不是服务端故障，不要往 stderr 打 traceback。"""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, TimeoutError)):
            logger.debug("client %s disconnected: %r", client_address, exc)
            return
        logger.error("request from %s failed: %r", client_address, exc, exc_info=True)

    def verify_request(self, request, client_address) -> bool:  # noqa: D102
        return True

    @property
    def url(self) -> str:
        host, port = self.server_address[0], self.server_address[1]
        return f"http://{host}:{port}/"


def build_config(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """默认值 <- settings.yaml 的 web 节 <- 显式 cfg。"""
    out = dict(WEB_DEFAULTS)
    try:
        from ..config import load_settings

        web = load_settings().web or {}
        out.update({k: v for k, v in web.items() if v is not None})
    except Exception as exc:                         # noqa: BLE001 —— 配置文件坏了也要能起服务
        logger.debug("load_settings() failed, using defaults: %r", exc)
    if cfg:
        out.update({k: v for k, v in cfg.items() if v is not None})
    if not out.get("host"):
        out["host"] = WEB_DEFAULTS["host"]
    return out


def create_server(store: Any, cfg: dict[str, Any] | None = None,
                  host: str | None = None, port: int | None = None) -> RadarHTTPServer:
    """建好（已 bind）的 ThreadingHTTPServer；`port=0` 时由内核分配随机端口。

    测试用：`srv.server_address` 拿到真实 (host, port)。
    """
    conf = build_config(cfg)
    bind_host = host or str(conf.get("host") or WEB_DEFAULTS["host"])
    bind_port = int(conf.get("port", WEB_DEFAULTS["port"])) if port is None else int(port)
    handler = type("BoundDashboardHandler", (DashboardHandler,), {"store": store, "cfg": conf})
    srv = RadarHTTPServer((bind_host, bind_port), handler, store=store, cfg=conf)
    logger.debug("dashboard server bound on %s", srv.server_address)
    return srv


def serve(store: Any, cfg: dict[str, Any] | None = None, block: bool = True) -> RadarHTTPServer:
    """便捷入口：起服务；block=False 时后台线程跑并立刻返回 server。"""
    srv = create_server(store, cfg)
    print(f"[arad.web] 看板已启动 -> {srv.url}  (Ctrl+C 停止)", flush=True)
    if not block:
        threading.Thread(target=srv.serve_forever, name="arad-web", daemon=True).start()
        return srv
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\n[arad.web] 收到中断，正在关闭…", flush=True)
    finally:
        srv.shutdown()
        srv.server_close()
    return srv


if __name__ == "__main__":                            # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    serve(NullStore())
