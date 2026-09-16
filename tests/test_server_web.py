"""`arad.server.web` 离线测试：真实本地 HTTP 服务 + 假 AlertStore。

覆盖契约第 7 节全部路由、limit clamp、404/405、异常 -> 500 JSON、
SSE 事件投递 / 心跳 / 多客户端 / 断开后 unsubscribe 清理 / 端口回收，
以及短线精灵（/api/spirit + SSE spirit 事件 + to_feed_item 的 JSON 安全性）。

所有请求都带 5 秒 socket 超时，避免 CI 卡死。
"""
from __future__ import annotations

import http.client
import json
import re
import socket
import struct
import threading
import time

import pytest
from fakes import make_alert

from arad.server import web as webmod

TIMEOUT = 5.0
HOST = "127.0.0.1"


# ==========================================================================
# 假 store
# ==========================================================================
def _quote(i: int, code: str | None = None) -> dict:
    code = code or f"{600000 + i:06d}"
    return {
        "code": code, "name": f"测试{i}", "price": 10.0 + i, "pct": round(i * 0.31 - 3, 2),
        "speed_1m": round(i * 0.11, 2), "speed_5m": round(i * 0.23, 2),
        "volume_ratio": round(1.0 + i * 0.05, 2), "amount": 1.0e8 * (i + 1),
        "turnover": round(i * 0.2, 2), "board": "main", "vwap": 10.0,
        "above_vwap": True, "limit_up": 11.0, "amplitude": 3.5,
    }


def _alert(i: int, kind: str = "surge") -> dict:
    from datetime import datetime

    from arad.models import AlertKind

    return make_alert(
        code=f"{600000 + i:06d}", kind=AlertKind(kind), ts=datetime(2026, 9, 15, 10, i % 60, 0),
        bucket=i, title=f"测试告警 {i}",
    ).to_dict()


class FakeStore:
    """模拟引擎的 AlertStore，记录所有调用供断言。"""

    def __init__(self, n_quotes: int = 40, n_alerts: int = 25, watch: int = 6):
        self.quote_items = [_quote(i) for i in range(n_quotes)]
        self.alert_items = [_alert(i, kind=("surge" if i % 2 == 0 else "plunge")) for i in range(n_alerts)]
        self.watch_items = [_quote(i, code=f"{300000 + i:06d}") for i in range(watch)]
        self.series_items = [{"t": 1_700_000_000 + i * 60, "price": 10 + i * 0.01, "pct": i * 0.1}
                             for i in range(30)]

        self.ack_calls: list[str] = []
        self.top_calls: list[tuple[int, str]] = []
        self.alert_calls: list[tuple[int, str | None]] = []
        self.series_calls: list[tuple[str, int]] = []
        self.status_calls = 0
        self.sub_count = 0
        self.unsub_count = 0
        self.queues: list = []

    # --- AlertStore 协议 -------------------------------------------------
    def status(self) -> dict:
        self.status_calls += 1
        return {
            "phase": "morning", "session": "morning", "uptime_s": 12.5, "universe": 5913,
            "alerts_total": len(self.alert_items), "sources": [
                {"name": "tencent", "ok": True, "latency_ms": 210, "err": ""},
                {"name": "eastmoney", "ok": False, "latency_ms": 0, "err": "boom"},
            ],
            "last_poll_ms": 2130, "poll_count": 7, "watchlist": len(self.watch_items),
            "title": "A股盘中雷达 · 测试", "dry_run": True, "last_poll_ts": "2026-09-15 10:00:05",
        }

    def recent_alerts(self, limit: int = 100, kind: str | None = None) -> list[dict]:
        self.alert_calls.append((limit, kind))
        items = [a for a in self.alert_items if kind is None or a["kind"] == kind]
        return items[:max(0, limit)]

    def top_quotes(self, limit: int = 30, sort: str = "speed") -> list[dict]:
        self.top_calls.append((limit, sort))
        # 与真实 arad/store.py 的 _SORTS 保持一致（键名漂移必须让测试失败）
        if sort not in ("speed", "speed5", "pct", "up", "down", "amount", "volume_ratio"):
            raise ValueError(f"unsupported sort: {sort}")
        return self.quote_items[:max(0, limit)]

    def watchlist_quotes(self) -> list[dict]:
        return list(self.watch_items)

    def series(self, code: str, limit: int = 240) -> list[dict]:
        self.series_calls.append((code, limit))
        return self.series_items[:max(0, limit)]

    def subscribe(self):
        import queue

        q: queue.Queue = queue.Queue()
        self.sub_count += 1
        self.queues.append(q)
        return q

    def unsubscribe(self, q) -> None:
        self.unsub_count += 1
        try:
            self.queues.remove(q)
        except ValueError:
            pass

    def ack(self, key: str) -> bool:
        self.ack_calls.append(key)
        return True

    # --- 测试辅助 --------------------------------------------------------
    def push(self, event: str, data) -> None:
        for q in list(self.queues):
            q.put({"event": event, "data": data})


class FailingStore(FakeStore):
    """所有数据方法都抛异常，用来验证 handler 不崩、返回 500 JSON。"""

    def status(self) -> dict:
        raise RuntimeError("status 炸了")

    def recent_alerts(self, limit: int = 100, kind: str | None = None) -> list[dict]:
        raise RuntimeError("recent_alerts 炸了")

    def top_quotes(self, limit: int = 30, sort: str = "speed") -> list[dict]:
        raise RuntimeError("top_quotes 炸了")

    def watchlist_quotes(self) -> list[dict]:
        raise RuntimeError("watchlist 炸了")

    def series(self, code: str, limit: int = 240) -> list[dict]:
        raise RuntimeError("series 炸了")

    def ack(self, key: str) -> bool:
        raise RuntimeError("ack 炸了")


class BadSubscribeStore(FakeStore):
    def subscribe(self):
        raise RuntimeError("subscribe 炸了")


# ==========================================================================
# fixtures
# ==========================================================================
_OPEN: list = []          # 已建立的短连接，fixture teardown 统一关闭，避免 FD 泄漏


@pytest.fixture
def servers():
    """创建服务器并后台 serve；teardown 保证端口释放。"""
    made: list[tuple] = []
    _OPEN.clear()

    def make(store=None, **cfg):
        store = store if store is not None else FakeStore()
        cfg.setdefault("port", 0)
        srv = webmod.create_server(store, cfg, port=0)
        th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        th.start()
        made.append((srv, th))
        return srv, store

    yield make

    for c in _OPEN:
        try:
            c.close()
        except Exception:                       # noqa: BLE001
            pass
    _OPEN.clear()
    for srv, th in made:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:                       # noqa: BLE001
            pass
        th.join(timeout=TIMEOUT)


def conn_for(srv, timeout: float = TIMEOUT) -> http.client.HTTPConnection:
    _, port = srv.server_address[0], srv.server_address[1]
    c = http.client.HTTPConnection(HOST, port, timeout=timeout)
    _OPEN.append(c)
    return c


class Resp:
    """已读完的 HTTP 响应（连接交给 fixture 统一回收）。"""

    __slots__ = ("status", "headers", "body")

    def __init__(self, status: int, headers, body: bytes):
        self.status = status
        self.headers = headers
        self.body = body

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read(self) -> bytes:
        return self.body

    def json(self):
        return json.loads(self.body.decode("utf-8"))


def get(srv, path: str, timeout: float = TIMEOUT) -> Resp:
    c = conn_for(srv, timeout)
    c.request("GET", path)
    r = c.getresponse()
    return Resp(r.status, r.headers, r.read())


def get_raw(srv, path: str, timeout: float = TIMEOUT):
    """GET 但**不读 body**（SSE 等需要保留流的场景）。"""
    c = conn_for(srv, timeout)
    c.request("GET", path)
    return c, c.getresponse()


class SSE:
    """一条 SSE 长连接。

    http.client 在 getresponse() 后把 conn.sock 置空，而 HTTPResponse 内部的
    `sock.makefile()` 会让 socket._io_refs +1 —— 只调 sock.close() 并不会真正
    关闭 fd（对端什么都收不到）。所以关闭时必须连 response 一起关，
    并先设 SO_LINGER(1,0) 让它以 RST 而不是 FIN 结束。
    """

    __slots__ = ("sock", "resp")

    def __init__(self, sock, resp):
        self.sock = sock
        self.resp = resp

    def readline(self) -> bytes:
        return self.resp.readline()

    def close_hard(self) -> None:
        """RST 断开，等价于浏览器标签页被直接关掉。"""
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        except OSError:
            pass
        try:
            self.sock.close()                     # _io_refs > 0 -> 仅标记
        except OSError:
            pass
        try:
            self.resp.close()                     # 真正 close(fd) -> 发 RST
        except Exception:                         # noqa: BLE001
            pass

    def close_soft(self) -> None:
        """FIN 断开（只关写方向），模拟代理/网络层优雅断开。"""
        try:
            self.sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def sse_connect(srv, timeout: float = TIMEOUT) -> SSE:
    """建立 SSE 连接（不读 body，返回可 readline 的连接对象）。"""
    c = conn_for(srv, timeout)
    c.connect()
    sock = c.sock
    assert sock is not None
    c.request("GET", "/api/stream", headers={"Accept": "text/event-stream"})
    return SSE(sock, c.getresponse())


def read_json(resp):
    if isinstance(resp, Resp):
        return resp.json()
    return json.loads(resp.read().decode("utf-8"))


def wait_for(pred, timeout: float = 5.0, interval: float = 0.05) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(interval)
    return pred()


# ==========================================================================
# 1. 基础：/ 与 JSON 头
# ==========================================================================
def test_index_returns_selfcontained_html(servers):
    srv, _ = servers()
    r = get(srv, "/")
    assert r.status == 200
    assert r.getheader("Content-Type") == "text/html; charset=utf-8"
    html = r.read().decode("utf-8")

    assert "A股盘中雷达" in html
    assert "实时告警流" in html and "异动榜" in html and "自选股" in html
    assert "声音提醒" in html
    # 单文件自包含：无外链资源
    refs = re.findall(r"""(?:src|href)\s*=\s*["']([^"']*)["']""", html, re.I)
    bad = [u for u in refs if u.startswith("//") or "://" in u or u.lower().startswith("http")]
    assert bad == [], f"发现外链资源: {bad}"
    css_urls = re.findall(r"""url\(\s*["']?([^"')]+)""", html, re.I)
    assert [u for u in css_urls if not u.startswith("data:")] == []
    assert "@import" not in html


def test_index_no_plain_http_links(servers):
    srv, _ = servers()
    r = get(srv, "/")
    html = r.read().decode("utf-8")
    assert "http://" not in html
    assert "https://" not in html


def test_status_endpoint(servers):
    srv, store = servers()
    r = get(srv, "/api/status")
    assert r.status == 200
    assert r.getheader("Content-Type") == "application/json; charset=utf-8"
    assert r.getheader("Cache-Control") == "no-store"
    body = read_json(r)
    assert body["universe"] == 5913
    assert body["phase"] == "morning"
    assert body["poll_count"] == 7
    assert store.status_calls == 1


def test_health_endpoint(servers):
    srv, _ = servers()
    r = get(srv, "/api/health")
    assert r.status == 200
    body = read_json(r)
    assert body["ok"] is True
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", body["ts"])


# ==========================================================================
# 2. 榜单 / 告警 / 自选 / 分时
# ==========================================================================
def test_quotes_json_and_shape(servers):
    srv, store = servers()
    r = get(srv, "/api/quotes?limit=5&sort=pct")
    assert r.status == 200
    assert r.getheader("Content-Type") == "application/json; charset=utf-8"
    body = read_json(r)
    assert len(body["items"]) == 5
    assert "ts" in body
    assert store.top_calls[-1] == (5, "pct")
    assert set(body["items"][0]) >= {"code", "name", "price", "pct", "speed_1m", "volume_ratio"}


def test_quotes_limit_clamped_to_200(servers):
    srv, store = servers(FakeStore(n_quotes=500))
    r = get(srv, "/api/quotes?limit=9999")
    assert r.status == 200
    body = read_json(r)
    assert len(body["items"]) == 200, "limit 必须 clamp 到 200"
    assert store.top_calls[-1][0] == 200
    assert body["limit"] == 200

    body2 = read_json(get(srv, "/api/quotes?limit=-3"))
    assert len(body2["items"]) >= 1 and body2["limit"] == 1

    body3 = read_json(get(srv, "/api/quotes?limit=abc"))
    assert body3["limit"] == 30, "非法 limit 回落默认 top_n=30"


def test_quotes_unknown_sort_falls_back(servers):
    srv, store = servers()
    r = get(srv, "/api/quotes?sort=bogus")
    assert r.status == 200
    body = read_json(r)
    assert body["sort"] == "speed"
    assert store.top_calls[-1][1] == "speed"


def test_alerts_endpoint_and_filter(servers):
    srv, store = servers()
    r = get(srv, "/api/alerts?limit=10")
    assert r.status == 200
    body = read_json(r)
    assert len(body["items"]) == 10
    assert body["total"] == len(store.alert_items)
    assert "key" in body["items"][0] and "kind" in body["items"][0]

    body2 = read_json(get(srv, "/api/alerts?kind=plunge&limit=100"))
    assert body2["items"], "kind 过滤应返回数据"
    assert {a["kind"] for a in body2["items"]} == {"plunge"}
    assert body2["total"] == len([a for a in store.alert_items if a["kind"] == "plunge"])


def test_alerts_limit_clamped(servers):
    srv, store = servers(FakeStore(n_alerts=1200))
    body = read_json(get(srv, "/api/alerts?limit=999999"))
    assert len(body["items"]) == 1000
    assert store.alert_calls[-1][0] == 1000


def test_watchlist_endpoint(servers):
    srv, store = servers()
    r = get(srv, "/api/watchlist")
    assert r.status == 200
    body = read_json(r)
    assert len(body["items"]) == len(store.watch_items)
    assert body["items"][0]["code"] == "300000"


def test_series_endpoint(servers):
    srv, store = servers()
    r = get(srv, "/api/series?code=sh600000&limit=10")
    assert r.status == 200
    body = read_json(r)
    assert body["code"] == "600000"
    assert len(body["items"]) == 10
    assert {"t", "price", "pct"} <= set(body["items"][0])
    assert store.series_calls[-1] == ("600000", 10)


def test_series_bad_code_400(servers):
    srv, _ = servers()
    for path in ("/api/series", "/api/series?code=", "/api/series?code=abc"):
        r = get(srv, path)
        assert r.status == 400, path
        assert "error" in read_json(r)


# ==========================================================================
# 3. ack
# ==========================================================================
def test_ack_json_body(servers):
    srv, store = servers()
    c = conn_for(srv)
    try:
        c.request("POST", "/api/ack", body=json.dumps({"key": "600000:surge:12"}),
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        assert r.status == 200
        body = read_json(r)
    finally:
        c.close()
    assert body == {"ok": True, "key": "600000:surge:12", "acked": True}
    assert store.ack_calls == ["600000:surge:12"]


def test_ack_query_string(servers):
    srv, store = servers()
    c = conn_for(srv)
    try:
        c.request("POST", "/api/ack?key=abc%3Adef")
        r = c.getresponse()
        assert r.status == 200
        body = read_json(r)
    finally:
        c.close()
    assert body["ok"] is True
    assert store.ack_calls == ["abc:def"]


def test_ack_form_body(servers):
    srv, store = servers()
    c = conn_for(srv)
    try:
        c.request("POST", "/api/ack", body="key=zzz", headers={"Content-Type": "application/x-www-form-urlencoded"})
        r = c.getresponse()
        assert r.status == 200
    finally:
        c.close()
    assert store.ack_calls == ["zzz"]


def test_ack_missing_key_400(servers):
    srv, store = servers()
    c = conn_for(srv)
    try:
        c.request("POST", "/api/ack", body="{}", headers={"Content-Type": "application/json"})
        r = c.getresponse()
        assert r.status == 400
        body = read_json(r)
    finally:
        c.close()
    assert body["ok"] is False
    assert store.ack_calls == []


# ==========================================================================
# 4. 404 / 405
# ==========================================================================
def test_404_json(servers):
    srv, _ = servers()
    for path in ("/api/nope", "/nope.html", "/api/quotes/extra"):
        r = get(srv, path)
        assert r.status == 404, path
        assert r.getheader("Content-Type") == "application/json; charset=utf-8"
        assert "error" in read_json(r)


def test_405_for_unsupported_methods(servers):
    srv, _ = servers()
    for method, path in (("POST", "/api/status"), ("POST", "/api/health"),
                         ("PUT", "/api/status"), ("DELETE", "/api/ack"), ("OPTIONS", "/")):
        c = conn_for(srv)
        c.request(method, path, body=b"{}")
        r = c.getresponse()
        assert r.status == 405, f"{method} {path}"
        assert "error" in read_json(r)


# ==========================================================================
# 5. 异常 -> 500 JSON（不崩）
# ==========================================================================
def test_store_exception_returns_500_json(servers):
    srv, _ = servers(FailingStore())
    for path in ("/api/status", "/api/quotes", "/api/alerts", "/api/watchlist", "/api/series?code=600000"):
        r = get(srv, path)
        assert r.status == 500, path
        assert r.getheader("Content-Type") == "application/json; charset=utf-8"
        body = read_json(r)
        assert "error" in body and "炸了" in body["error"]

    # 静态页与健康检查不受 store 影响
    assert get(srv, "/").status == 200

    # 服务仍然存活
    r = get(srv, "/api/health")
    assert r.status == 200 and read_json(r)["ok"] is True


def test_ack_exception_returns_500(servers):
    srv, _ = servers(FailingStore())
    c = conn_for(srv)
    c.request("POST", "/api/ack", body=json.dumps({"key": "k"}), headers={"Content-Type": "application/json"})
    r = c.getresponse()
    assert r.status == 500
    assert "error" in read_json(r)


def test_subscribe_exception_returns_500_json(servers):
    srv, _ = servers(BadSubscribeStore())
    r = get(srv, "/api/stream")
    assert r.status == 500
    assert r.getheader("Content-Type") == "application/json; charset=utf-8"
    assert "error" in read_json(r)


def test_log_message_silenced():
    from http.server import BaseHTTPRequestHandler

    assert webmod.DashboardHandler.log_message is not BaseHTTPRequestHandler.log_message
    assert webmod.DashboardHandler.log_request is not BaseHTTPRequestHandler.log_request


# ==========================================================================
# 6. SSE
# ==========================================================================
def read_event(resp, name: str, deadline: float = 5.0):
    """读到指定事件名，返回 (data_dict, 原始行列表)。"""
    end = time.monotonic() + deadline
    lines: list[str] = []
    while time.monotonic() < end:
        try:
            raw = resp.readline()
        except (TimeoutError, socket.timeout):
            break
        if not raw:
            break
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        lines.append(line)
        if line.startswith("event: ") and line[7:].strip() == name:
            data = []
            while True:
                raw2 = resp.readline()
                if not raw2:
                    break
                ln = raw2.decode("utf-8", "replace").rstrip("\r\n")
                lines.append(ln)
                if ln == "":
                    break
                if ln.startswith("data: "):
                    data.append(ln[6:])
            return json.loads("\n".join(data)), lines
    raise AssertionError(f"未在 {deadline}s 内收到 event: {name}；已读: {lines}")


def test_sse_heartbeat_at_configured_interval(servers):
    """sse_interval=0.3 时，3 秒内必须收到多次心跳（tick 事件或 `:` 注释行）。"""
    srv, _ = servers(sse_interval=0.3)
    conn = sse_connect(srv)
    ticks = pings = 0
    end = time.monotonic() + 3.0
    try:
        while time.monotonic() < end:
            try:
                raw = conn.readline()
            except (TimeoutError, socket.timeout):
                break
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("event: tick"):
                ticks += 1
            elif line.startswith(":"):
                pings += 1
    finally:
        conn.close_hard()
    assert ticks + pings >= 3, f"心跳不足：tick={ticks} ping={pings}"
    assert ticks >= 1, "必须推送 tick 事件（榜单+状态）"


def test_sse_headers(servers):
    srv, store = servers(sse_interval=0.2)
    conn = sse_connect(srv)
    try:
        assert conn.resp.status == 200
        assert conn.resp.getheader("Content-Type") == "text/event-stream; charset=utf-8"
        assert conn.resp.getheader("Cache-Control") == "no-cache"
        assert conn.resp.getheader("Connection") == "keep-alive"
        assert conn.resp.getheader("X-Accel-Buffering") == "no"
        first = conn.readline().decode("utf-8", "replace").strip()
        assert first.startswith("retry:"), first
        assert store.sub_count == 1
    finally:
        conn.close_hard()


def test_sse_delivers_alert_event(servers):
    srv, store = servers(sse_interval=0.2)
    conn = sse_connect(srv)
    try:
        alert = _alert(3)
        store.push("alert", alert)
        data, lines = read_event(conn.resp, "alert", deadline=5.0)
        assert data["key"] == alert["key"]
        assert data["kind"] == "surge"
        assert data["code"] == alert["code"]
        assert "event: alert" in lines
        assert any(ln.startswith("data: {") and ln.endswith("}") for ln in lines), \
            f"data 行必须是单行 JSON：{lines}"
    finally:
        conn.close_hard()


def test_sse_heartbeat_tick(servers):
    srv, store = servers(sse_interval=0.2)
    conn = sse_connect(srv)
    try:
        data, lines = read_event(conn.resp, "tick", deadline=5.0)
        assert "ts" in data
        assert "status" in data and data["status"]["universe"] == 5913
        assert "quotes" in data and 0 < len(data["quotes"]) <= 30
    finally:
        conn.close_hard()


def test_sse_unsubscribe_on_client_disconnect(servers):
    """客户端断开（RST）后必须 unsubscribe —— 不能泄漏订阅。"""
    srv, store = servers(sse_interval=0.1)
    conn = sse_connect(srv)
    assert conn.readline().startswith(b"retry:")
    assert wait_for(lambda: store.sub_count == 1, 2.0)
    assert store.unsub_count == 0
    assert len(store.queues) == 1

    conn.close_hard()                               # 浏览器关页 -> RST
    assert wait_for(lambda: store.unsub_count >= 1, 5.0), "客户端断开后必须 unsubscribe"
    assert store.queues == [], "订阅队列必须被回收"


def test_sse_unsubscribe_on_graceful_close(servers):
    """半关闭（FIN）也要被识别为断开并清理。"""
    srv, store = servers(sse_interval=0.1)
    conn = sse_connect(srv)
    assert conn.readline().startswith(b"retry:")
    assert wait_for(lambda: store.sub_count == 1, 2.0)

    try:
        conn.close_soft()
    except OSError:
        pass
    assert wait_for(lambda: store.unsub_count >= 1, 5.0), "对端 FIN 后必须 unsubscribe"
    conn.close_hard()


def test_sse_unsubscribe_on_timeout(servers):
    """服务端超时主动收尾，同样要 unsubscribe。"""
    srv, store = servers(sse_interval=0.1, sse_timeout=0.4)
    conn = sse_connect(srv)
    try:
        data, _ = read_event(conn.resp, "bye", deadline=5.0)
        assert data["reason"] == "timeout"
        assert conn.readline() == b""              # 服务端主动收尾，流结束
    finally:
        conn.close_hard()
    assert wait_for(lambda: store.unsub_count >= 1, 5.0)
    assert store.queues == []


def test_sse_multiple_clients(servers):
    """多客户端并发：各自独立收到同一事件，各自断开各自清理。"""
    srv, store = servers(sse_interval=0.2)
    c1 = sse_connect(srv)
    c2 = sse_connect(srv)
    try:
        assert wait_for(lambda: store.sub_count == 2, 3.0)
        assert len(store.queues) == 2
        store.push("alert", _alert(7))
        d1, _ = read_event(c1.resp, "alert", deadline=5.0)
        d2, _ = read_event(c2.resp, "alert", deadline=5.0)
        assert d1["key"] == d2["key"] == _alert(7)["key"]

        c1.close_hard()                             # 先断一个，另一个必须还活着
        assert wait_for(lambda: store.unsub_count >= 1, 5.0)
        store.push("alert", _alert(8))
        d3, _ = read_event(c2.resp, "alert", deadline=5.0)
        assert d3["key"] == _alert(8)["key"]
    finally:
        c1.close_hard()
        c2.close_hard()
    assert wait_for(lambda: store.unsub_count >= 2, 5.0), "两个客户端都要清理"
    assert store.queues == []


def test_sse_survives_store_exception_in_tick(servers):
    """tick 里 status/top_quotes 抛错也不能把 SSE 打断。"""
    srv, _ = servers(FailingStore(), sse_interval=0.15)
    conn = sse_connect(srv)
    try:
        data, _ = read_event(conn.resp, "tick", deadline=5.0)
        assert data["status"] == {}
        assert data["quotes"] == []
        data2, _ = read_event(conn.resp, "tick", deadline=5.0)   # 心跳继续
        assert "ts" in data2
    finally:
        conn.close_hard()


def test_server_stop_terminates_sse_threads(servers):
    """shutdown 时 SSE 线程要主动收尾（不靠 daemon 硬杀）。"""
    srv, store = servers(sse_interval=0.1, sse_timeout=30)
    conn = sse_connect(srv)
    try:
        assert conn.readline().startswith(b"retry:")
        assert wait_for(lambda: store.sub_count == 1, 2.0)
        srv.shutdown()
        srv.server_close()
        assert wait_for(lambda: store.unsub_count >= 1, 5.0), "shutdown 后必须 unsubscribe"
        assert store.queues == []
    finally:
        conn.close_hard()

# ==========================================================================
# 7. 端口回收 / NullStore / 配置
# ==========================================================================
def test_port_released_after_close():
    store = FakeStore()
    srv = webmod.create_server(store, {"port": 0}, port=0)
    host, port = srv.server_address[0], srv.server_address[1]
    assert host == HOST
    assert port > 0
    th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    th.start()
    try:
        assert get(srv, "/api/health").status == 200
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=TIMEOUT)
        assert not th.is_alive(), "serve_forever 线程必须退出"

    # 端口必须已释放：可以重新绑定
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(TIMEOUT)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, port))
        probe.listen(1)
    finally:
        probe.close()

    # 新连接应被拒绝
    c = http.client.HTTPConnection(host, port, timeout=2.0)
    try:
        with pytest.raises(OSError):
            c.request("GET", "/api/health")
            c.getresponse()
    finally:
        c.close()


def test_null_store_defaults():
    ns = webmod.NullStore()
    st = ns.status()
    assert st["universe"] == 0 and st["alerts_total"] == 0 and st["poll_count"] == 0
    assert isinstance(st["sources"], list) and st["sources"] == []
    assert st["dry_run"] is True and isinstance(st["uptime_s"], float)
    assert ns.recent_alerts() == []
    assert ns.top_quotes() == []
    assert ns.watchlist_quotes() == []
    assert ns.series("600000") == []
    assert ns.ack("x") is False


def test_server_with_null_store(servers):
    srv, _ = servers(webmod.NullStore())
    for path in ("/api/status", "/api/quotes", "/api/alerts", "/api/watchlist", "/api/health"):
        r = get(srv, path)
        assert r.status == 200, path
        read_json(r)
    r = get(srv, "/api/series?code=600000")
    assert r.status == 200 and read_json(r)["items"] == []


def test_null_store_serves_and_publishes(servers):
    """NullStore 也能跑 SSE，手动 publish 的事件会送达（本地调 UI 用）。"""
    ns = webmod.NullStore()
    srv, _ = servers(ns, sse_interval=0.2)
    conn = sse_connect(srv)
    try:
        assert conn.readline().startswith(b"retry:")
        assert wait_for(lambda: ns.subscribers == 1, 3.0)
        ns.publish("alert", _alert(2))
        data, _ = read_event(conn.resp, "alert", deadline=5.0)
        assert data["kind"] == "surge"
    finally:
        conn.close_hard()
    assert wait_for(lambda: ns.subscribers == 0, 5.0)


def test_config_defaults_and_overrides():
    conf = webmod.build_config()
    assert conf["host"] == "127.0.0.1"
    assert conf["port"] == 8899
    assert conf["sse_interval"] == 2
    assert conf["title"]

    conf2 = webmod.build_config({"port": 9123, "sse_interval": 0.5})
    assert conf2["port"] == 9123 and conf2["sse_interval"] == 0.5

    # host/port 显式参数优先
    srv = webmod.create_server(FakeStore(), {"port": 9999, "host": "0.0.0.0"}, host="127.0.0.1", port=0)
    try:
        assert srv.server_address[0] == HOST
        assert srv.server_address[1] != 9999
        assert srv.store is not srv.cfg
    finally:
        srv.server_close()


def test_trailing_slash_and_index_alias(servers):
    srv, _ = servers()
    for path in ("/index.html", "/dashboard.html"):
        r = get(srv, path)
        assert r.status == 200
        assert "A股盘中雷达" in r.read().decode("utf-8")
    # 带尾斜杠的其他路径按原样 404（只有 / 有别名）
    assert get(srv, "/api/status/").status == 200


def test_json_is_ensure_ascii_false(servers):
    srv, _ = servers()
    raw = get(srv, "/api/status").read().decode("utf-8")
    assert "A股盘中雷达 · 测试" in raw, "JSON 必须是 ensure_ascii=False 的 UTF-8 原文"
    assert "\\u" not in raw


# ==========================================================================
# 8. 排序键映射（契约风险：引擎 _SORTS 的键与我们透传的键必须一致）
# ==========================================================================
def test_resolve_sort_never_passes_unknown_key_to_engine():
    """引擎的 AlertStore 只认 speed/speed5/pct/up/down/amount/volume_ratio。"""
    from arad.store import _SORTS as ENGINE_SORTS_REAL

    for raw in ("speed", "speed5", "pct", "up", "down", "amount", "volume_ratio"):
        requested, key = webmod.resolve_sort(raw)
        assert key in ENGINE_SORTS_REAL, f"{raw} 映射到了引擎不认识的 {key}"

    # 前端列名必须映射到引擎键，而不是把列名直接透传
    for alias, expect in (("speed_1m", "speed"), ("speed_5m", "speed5"),
                          ("turnover", "amount"), ("amplitude", "pct")):
        requested, key = webmod.resolve_sort(alias)
        assert requested == alias
        assert key == expect, f"{alias} 应映射为 {expect}，实际 {key}"
        assert key in ENGINE_SORTS_REAL

    # 任何输入都不允许产生引擎不认识的键
    for raw in ("", None, "bogus", "SPEED", "  pct  ", "DROP TABLE", "speed_9m"):
        requested, key = webmod.resolve_sort(raw)
        assert key in ENGINE_SORTS_REAL, f"sort={raw!r} -> {key} 不在引擎 _SORTS 中"
        assert requested in webmod.ALLOWED_SORTS


def test_quotes_sort_alias_hits_engine_key(servers):
    srv, store = servers()
    body = read_json(get(srv, "/api/quotes?sort=speed_5m"))
    assert body["sort"] == "speed_5m"
    assert body["sort_key"] == "speed5"
    assert store.top_calls[-1] == (30, "speed5"), "必须用引擎键调用 top_quotes"

    body2 = read_json(get(srv, "/api/quotes?sort=turnover"))
    assert body2["sort_key"] == "amount"


def test_real_alert_store_end_to_end(servers):
    """用真实 AlertStore 跑一遍全链路，防止假 store 与真实现漂移。"""
    from datetime import datetime

    from fakes import make_quote

    from arad.models import AlertKind
    from arad.store import AlertStore

    class _State:
        def __init__(self):
            self.quotes = {}

        def price_change(self, code, seconds, now_epoch):
            return 1.5

    class _Engine:
        def __init__(self, state):
            self.state = state
            self.watchlist = ["600000", "300001"]

    state = _State()
    engine = _Engine(state)
    store = AlertStore(max_alerts=50, series_len=20)
    store.attach(engine)
    quotes = [
        make_quote(code="600000", name="浦发银行", price=10.5, prev_close=10.0, volume_ratio=2.4),
        make_quote(code="300001", name="特锐德", price=20.0, prev_close=19.0, volume_ratio=1.8),
    ]
    for q in quotes:
        state.quotes[q.code] = q
    store.record_tick(quotes, datetime.now())
    store.set_poll_stats(poll_ms=2100, count=7,
                         health=[{"name": "tencent", "ok": True, "latency_ms": 210, "err": ""}])
    store.add_alert(make_alert(code="600000", kind=AlertKind.SURGE, bucket=1, title="真实急拉"))

    srv, _ = servers(store)

    st = read_json(get(srv, "/api/status"))
    assert st["universe"] == 2
    assert st["alerts_total"] == 1
    assert st["last_poll_ms"] == 2100
    assert st["sources"][0]["name"] == "tencent"

    q = read_json(get(srv, "/api/quotes?limit=10&sort=speed"))
    assert len(q["items"]) == 2
    assert set(q["items"][0]) >= {"code", "name", "price", "pct", "speed_1m", "volume_ratio",
                                  "amount", "turnover", "board", "vwap", "above_vwap"}

    al = read_json(get(srv, "/api/alerts?limit=10"))
    assert al["total"] == 1 and al["items"][0]["title"] == "真实急拉"
    assert al["items"][0]["acked"] is False

    wl = read_json(get(srv, "/api/watchlist"))
    assert [i["code"] for i in wl["items"]] == ["600000", "300001"]

    se = read_json(get(srv, "/api/series?code=600000"))
    assert len(se["items"]) == 1
    assert {"t", "price", "pct"} <= set(se["items"][0])

    # ack 走真实 store，之后 recent_alerts 的 acked 必须变 True
    c = conn_for(srv)
    c.request("POST", "/api/ack", body=json.dumps({"key": "600000:surge:1"}),
              headers={"Content-Type": "application/json"})
    r = c.getresponse()
    assert r.status == 200
    assert read_json(r)["acked"] is True
    al2 = read_json(get(srv, "/api/alerts?limit=10"))
    assert al2["items"][0]["acked"] is True


def test_real_store_sse_alert_and_tick(servers):
    """真实 store 的 broadcast()/snapshot_payload() 与 SSE 频道对齐。"""
    from datetime import datetime

    from fakes import make_quote

    from arad.models import AlertKind
    from arad.store import AlertStore

    class _State:
        def __init__(self):
            self.quotes = {}

        def price_change(self, code, seconds, now_epoch):
            return 0.5

    class _Engine:
        def __init__(self, state):
            self.state = state
            self.watchlist = ["600000"]

    state = _State()
    store = AlertStore(max_alerts=50, series_len=20)
    store.attach(_Engine(state))
    q = make_quote(code="600000", name="浦发银行", price=10.5, prev_close=10.0)
    state.quotes["600000"] = q
    store.record_tick([q], datetime.now())

    srv, _ = servers(store, sse_interval=0.2)
    conn = sse_connect(srv)
    try:
        assert conn.readline().startswith(b"retry:")
        assert wait_for(lambda: store.subscriber_count == 1, 3.0)

        tick, _ = read_event(conn.resp, "tick", deadline=5.0)
        assert tick["status"]["universe"] == 1
        assert len(tick["quotes"]) == 1
        assert [i["code"] for i in tick["watchlist"]] == ["600000"]

        store.add_alert(make_alert(code="600000", kind=AlertKind.VOLUME_BURST,
                                   bucket=9, title="真实放量"))
        alert, _ = read_event(conn.resp, "alert", deadline=5.0)
        assert alert["kind"] == "volume_burst"
        assert alert["title"] == "真实放量"
    finally:
        conn.close_hard()
    assert wait_for(lambda: store.subscriber_count == 0, 5.0), "真实 store 的订阅必须被回收"


def test_engine_style_phase_broadcast_is_tolerated(servers):
    """引擎还会 broadcast('phase', ...)：未知事件名不能被当成告警或崩溃。"""
    srv, store = servers(sse_interval=0.2)
    conn = sse_connect(srv)
    try:
        assert wait_for(lambda: store.sub_count == 1, 3.0)
        store.push("phase", {"phase": "morning"})
        data, lines = read_event(conn.resp, "phase", deadline=5.0)
        assert data == {"phase": "morning"}
        # 之后 tick 心跳必须继续
        tick, _ = read_event(conn.resp, "tick", deadline=5.0)
        assert "ts" in tick
    finally:
        conn.close_hard()


# ==========================================================================
# 9. 短线精灵：/api/spirit + SSE spirit 事件
# ==========================================================================
def _spirit_alert(i: int, pattern: str = "rocket", kind: str = "unusual") -> dict:
    """带 metrics['pattern'] 的告警（spirit.py 靠它决定中文名与方向）。"""
    from datetime import datetime

    from arad.models import AlertKind

    return make_alert(
        code=f"{600000 + i:06d}", kind=AlertKind(kind), bucket=i,
        ts=datetime(2026, 9, 16, 9, 30, i % 60), title=f"精灵 {i}",
        metrics={"pattern": pattern},
    ).to_dict()


class SpiritStore(FakeStore):
    """告警带 pattern，供短线精灵转换用。"""

    PATTERNS = ("rocket", "dive", "limit_up_seal", "open_limit_up", "big_buy")

    def __init__(self, n_alerts: int = 12):
        super().__init__(n_alerts=n_alerts)
        self.alert_items = [
            _spirit_alert(i, pattern=self.PATTERNS[i % len(self.PATTERNS)])
            for i in range(n_alerts)
        ]


def test_spirit_endpoint_happy_path(servers):
    """/api/spirit 返回 feed item，且字段齐全、中文名来自注册表。"""
    srv, store = servers(SpiritStore(n_alerts=12))
    r = get(srv, "/api/spirit?limit=10")
    assert r.status == 200
    assert r.getheader("Content-Type") == "application/json; charset=utf-8"
    body = read_json(r)
    assert len(body["items"]) == 10
    assert body["limit"] == 10 and body["count"] == 10
    assert body["groups"], "必须带上分组清单（前端筛选按钮靠它生成）"

    item = body["items"][0]
    assert set(item) >= {"key", "ts", "epoch", "code", "name", "signal",
                         "cn", "dir", "group", "hint", "price", "pct",
                         "severity", "title", "extra"}
    # 中文名必须来自 arad.spirit 注册表，而不是服务端另写一份
    from arad import spirit as sp

    for it in body["items"]:
        sig = sp.SIGNALS.get(it["signal"])
        assert sig is not None, f"{it['signal']} 不在注册表里"
        assert it["cn"] == sig.cn
        assert it["dir"] == sig.direction
        assert it["group"] == sig.group
        assert re.match(r"^\d{2}:\d{2}:\d{2}$", it["ts"]), it["ts"]
    # 顺序由 store 决定（AlertStore 契约：recent_alerts 倒序返回），
    # 服务端只做转换、**不重排**，所以这里断言的是"原样透传"而不是"服务端帮我排序"。
    assert [it["key"] for it in body["items"]] == \
        [a["key"] for a in store.recent_alerts(10, None)[:10]]


def test_spirit_direction_mapping_is_not_reinvented(servers):
    """打开涨停 = 绿(down)、打开跌停 = 红(up) —— 最容易搞反的两个。"""
    srv, _ = servers(SpiritStore(n_alerts=10))
    body = read_json(get(srv, "/api/spirit?limit=100"))
    by_signal = {it["signal"]: it for it in body["items"]}
    assert by_signal["limit_up_seal"]["dir"] == "up"
    assert by_signal["limit_up_seal"]["cn"] == "封涨停板"
    assert by_signal["open_limit_up"]["dir"] == "down", "打开涨停是利空 -> 绿色"
    assert by_signal["open_limit_up"]["cn"] == "打开涨停"
    assert by_signal["rocket"]["dir"] == "up"
    assert by_signal["dive"]["dir"] == "down"

    # 注册表里另两个方向陷阱也要对
    from arad import spirit as sp

    assert sp.SIGNALS["open_limit_down"].direction == sp.UP
    assert sp.SIGNALS["open_limit_up"].direction == sp.DOWN


def test_spirit_limit_clamped(servers):
    srv, store = servers(SpiritStore(n_alerts=1200))
    body = read_json(get(srv, "/api/spirit?limit=999999"))
    assert len(body["items"]) == 1000, "limit 必须 clamp 到 1000"
    assert body["limit"] == 1000
    assert store.alert_calls, "必须真的去 store 取数"

    body2 = read_json(get(srv, "/api/spirit?limit=-3"))
    assert body2["limit"] == 1 and len(body2["items"]) == 1

    body3 = read_json(get(srv, "/api/spirit?limit=abc"))
    assert body3["limit"] == 200, "非法 limit 回落默认 max_spirit=200"


def test_spirit_tolerates_malformed_alerts(servers):
    """脏数据行必须被跳过，绝不 500。"""
    store = SpiritStore(n_alerts=6)
    store.alert_items = [
        _spirit_alert(0),
        None,                                            # 整条是 None
        "not-a-dict",                                    # 根本不是 dict
        {},                                              # 空 dict：没有 key
        {"key": "no-kind:1", "ts": "2026-09-16 09:31:00"},   # 缺 kind/name/price
        {"key": "bad-ts:1", "kind": "surge", "ts": "昨天下午", "price": "abc",
         "pct": None, "metrics": ["不是 dict"]},
        {"key": "nan:1", "kind": "surge", "ts": "2026-09-16 09:32:00",
         "price": float("nan"), "pct": float("inf"), "metrics": {"amount": float("nan")}},
        _spirit_alert(1, pattern="totally_unknown_signal"),
    ]
    srv, _ = servers(store)
    r = get(srv, "/api/spirit?limit=100")
    assert r.status == 200, "脏数据不能让 /api/spirit 变 500"
    body = read_json(r)

    keys = [it["key"] for it in body["items"]]
    assert "no-kind:1" in keys and "bad-ts:1" in keys, "缺字段的行要尽力还原而不是丢弃"
    assert None not in keys and "" not in keys, "没有 key 的行必须跳过"
    assert len(body["items"]) == len(keys)

    # 未知信号名不硬分类：原名 + 中性色 + other 分组
    unknown = [it for it in body["items"] if it["signal"] == "totally_unknown_signal"]
    assert len(unknown) == 1
    assert unknown[0]["cn"] == "totally_unknown_signal"
    assert unknown[0]["dir"] == "flat" and unknown[0]["group"] == "other"

    # ts 解析不了 -> 空串 + epoch 0（前端显示 --:--:--），而不是异常
    bad_ts = [it for it in body["items"] if it["key"] == "bad-ts:1"][0]
    assert bad_ts["ts"] == "" and bad_ts["epoch"] == 0.0
    assert bad_ts["price"] == 0.0 and bad_ts["pct"] == 0.0

    # 非有限浮点绝不能出现在响应体里（裸 NaN 是非法 JSON）
    raw = r.read().decode("utf-8")
    assert "NaN" not in raw and "Infinity" not in raw


def test_spirit_empty_store(servers):
    """/api/spirit 在空 store 上必须返回空列表 + 分组清单，不能 404/500。"""
    srv, _ = servers(FakeStore(n_alerts=0))
    r = get(srv, "/api/spirit")
    assert r.status == 200
    body = read_json(r)
    assert body["items"] == [] and body["count"] == 0
    assert body["groups"], "分组清单来自代码里的注册表，与 store 是否有数据无关"


def test_spirit_works_with_null_store(servers):
    """NullStore（无引擎）下 /api/spirit 也要能正常响应。"""
    srv, _ = servers(webmod.NullStore())
    r = get(srv, "/api/spirit?limit=50")
    assert r.status == 200
    assert read_json(r)["items"] == []


def test_spirit_groups_come_from_registry():
    """分组清单必须由 spirit.SIGNALS 推导，不能是前端/服务端另抄的一份。"""
    from arad import spirit as sp

    groups = webmod.spirit_groups()
    names = [g["group"] for g in groups]
    assert set(names) == {s.group for s in sp.SIGNALS.values()}
    assert all(g["cn"] for g in groups), "每个分组都要有中文名"
    assert len(names) == len(set(names)), "分组不能重复"
    # 已知分组按固定顺序在前
    assert names[:5] == ["price", "order", "limit", "index", "pattern"]
    counts = {g["group"]: g["count"] for g in groups}
    assert counts["limit"] == len([s for s in sp.SIGNALS.values() if s.group == "limit"])


def test_feed_item_is_json_safe_for_hostile_numbers():
    """出网的 feed item 必须是**合法 JSON**：不许出现裸 NaN/Infinity。

    真实事故：``json.dumps`` 默认会写裸 ``NaN``，浏览器 ``JSON.parse`` 直接抛错，
    整条 SSE 推送就废了。这里用 NaN/Inf 喂满每个数值字段来钉住这个行为。

    分工（三道护栏，逐层收紧）：
      1. ``arad.spirit.to_feed_item`` —— 展示层出口就过 ``_finite``，非有限值归 0；
      2. ``web.feed_item_of``       —— dict -> feed 时再做一次 float 归一；
      3. ``web.dumps_json``         —— 出网前兜底，任何漏网的都洗成 null。

    第 1 道是后加的：早先 ``to_feed_item`` 不清洗，只靠 2/3 兜底。结果任何人
    直接消费 ``to_feed_item``（比如 CLI 或测试）都会拿到裸 NaN。清洗下沉到
    出口更稳，也让"一定能 JSON 序列化"成为这个函数的自身保证。
    """
    from datetime import datetime

    from arad.models import Alert, AlertKind

    from arad import spirit as sp

    alert = Alert(
        key="600000:surge:1", kind=AlertKind.SURGE, code="600000", name="浦发银行",
        ts=datetime(2026, 9, 16, 9, 41, 7), price=float("nan"), pct=float("inf"),
        title="坏数值", detail="d", severity=3,
        metrics={"pattern": "rocket", "amount": float("nan"),
                 "seal_amount_wan": float("-inf"), "note": "保留字符串"},
    )

    # 第一道：从 store 的 dict 形状进来（这就是 store 里真实的样子）
    served = webmod.feed_item_of({**alert.to_dict(), "price": float("nan"),
                                  "pct": float("inf")})
    assert served is not None
    assert served["price"] == 0.0 and served["pct"] == 0.0, "非有限值必须归零"
    assert served["cn"] == "火箭发射", "归一化不能把信号映射弄丢"
    text = webmod.dumps_json(served)
    json.loads(text)                                  # 不能抛
    assert "NaN" not in text and "Infinity" not in text

    # 第二道：直接消费 to_feed_item 的输出（绕过 web 层）也必须是干净数值。
    # 用 allow_nan=False 严格序列化：只要还剩一个 NaN/Inf 就会抛 ValueError。
    direct = sp.to_feed_item(alert)
    json.dumps(direct, allow_nan=False)               # 不能抛 = 出口已清洗
    assert direct["price"] == 0.0, "to_feed_item 出口就该把 NaN 归零"
    assert direct["pct"] == 0.0, "inf 同样归零"
    assert direct["extra"]["amount"] == 0.0
    assert direct["extra"]["seal_amount_wan"] == 0.0
    assert "NaN" not in webmod.dumps_json(direct)
    assert "Infinity" not in webmod.dumps_json(direct)


def test_dumps_json_scrubs_non_finite_nested():
    text = webmod.dumps_json({"a": float("nan"), "b": [1, float("inf")],
                              "c": {"d": float("-inf")}, "e": "ok"})
    obj = json.loads(text)
    assert obj["a"] is None and obj["b"][1] is None and obj["c"]["d"] is None
    assert obj["e"] == "ok"
    assert "NaN" not in text and "Infinity" not in text


def test_spirit_sse_event_delivered_alongside_alert(servers):
    """SSE 在 alert 之外补发同一条 spirit 事件；alert 原样不变。"""
    srv, store = servers(sse_interval=0.2)
    conn = sse_connect(srv)
    try:
        assert wait_for(lambda: store.sub_count == 1, 3.0)
        raw = _spirit_alert(4, pattern="limit_up_seal", kind="limit_up")
        store.push("alert", raw)

        alert, _ = read_event(conn.resp, "alert", deadline=5.0)
        assert alert["key"] == raw["key"] and alert["kind"] == "limit_up", \
            "alert 事件必须是引擎原始形状，不能被换成 feed item"

        feed, _ = read_event(conn.resp, "spirit", deadline=5.0)
        assert feed["key"] == raw["key"]
        assert feed["cn"] == "封涨停板"
        assert feed["dir"] == "up" and feed["group"] == "limit"
        assert feed["code"] == raw["code"]
    finally:
        conn.close_hard()


def test_spirit_sse_survives_malformed_alert(servers):
    """坏告警只在 spirit 侧被跳过，SSE 流必须继续跑。"""
    srv, store = servers(sse_interval=0.2)
    conn = sse_connect(srv)
    try:
        assert wait_for(lambda: store.sub_count == 1, 3.0)
        store.push("alert", None)
        store.push("alert", {"key": "nan:1", "kind": "surge",
                             "ts": "2026-09-16 09:40:00", "price": float("nan")})
        store.push("alert", _spirit_alert(2, pattern="dive"))

        # 能读到后面那条好告警，就说明坏告警没把流打断
        data, lines = read_event(conn.resp, "spirit", deadline=5.0)
        assert data["key"] == "nan:1", "NaN 价格要被清洗成 0，而不是让流崩掉"
        assert data["dir"] == "up"

        feed, _ = read_event(conn.resp, "spirit", deadline=5.0)
        assert feed["signal"] == "dive" and feed["dir"] == "down"

        tick, _ = read_event(conn.resp, "tick", deadline=5.0)
        assert "ts" in tick
    finally:
        conn.close_hard()


def test_spirit_sse_none_alert_never_breaks_stream(servers):
    """store.push('alert', None)：alert 事件照发，spirit 侧静默跳过。"""
    srv, store = servers(sse_interval=0.2)
    conn = sse_connect(srv)
    try:
        assert wait_for(lambda: store.sub_count == 1, 3.0)
        store.push("alert", None)
        alert, _ = read_event(conn.resp, "alert", deadline=5.0)
        assert alert is None, "None 告警原样透传（前端 pushAlert 自己会丢弃）"
        # 下一条好告警必须还能正常到达（说明流没被打断）
        store.push("alert", _spirit_alert(5, pattern="big_buy"))
        feed, _ = read_event(conn.resp, "spirit", deadline=5.0)
        assert feed["signal"] == "big_buy" and feed["cn"] == "大笔买入"
        assert feed["group"] == "order" and feed["dir"] == "up"
    finally:
        conn.close_hard()


def test_spirit_sse_uses_same_shape_as_rest_endpoint(servers):
    """SSE 推的 feed item 与 /api/spirit 返回的必须是同一形状（同一个来源）。"""
    srv, store = servers(sse_interval=0.2)
    raw = _spirit_alert(0, pattern="rocket", kind="surge")

    sse_item = None
    conn = sse_connect(srv)
    try:
        assert wait_for(lambda: store.sub_count == 1, 3.0)
        store.push("alert", raw)
        sse_item, _ = read_event(conn.resp, "spirit", deadline=5.0)
    finally:
        conn.close_hard()

    assert webmod.feed_item_of(raw) == sse_item, "两条路径必须产出完全一致的 feed item"
    assert set(sse_item) == {"key", "ts", "epoch", "code", "name", "signal", "cn",
                             "dir", "group", "hint", "price", "pct", "severity",
                             "title", "extra"}, set(sse_item)


def test_spirit_end_to_end_with_real_store(servers):
    """真实 AlertStore + 真实规则产出的告警，走一遍 /api/spirit。"""
    from datetime import datetime

    from fakes import make_quote

    from arad.models import AlertKind
    from arad.store import AlertStore

    class _State:
        def __init__(self):
            self.quotes = {}

        def price_change(self, code, seconds, now_epoch):
            return 1.5

    class _Engine:
        def __init__(self, state):
            self.state = state
            self.watchlist = ["600000"]

    state = _State()
    store = AlertStore(max_alerts=50, series_len=20)
    store.attach(_Engine(state))
    q = make_quote(code="600000", name="浦发银行", price=10.5, prev_close=10.0)
    state.quotes["600000"] = q
    store.record_tick([q], datetime.now())
    store.add_alert(make_alert(code="600000", kind=AlertKind.SURGE, bucket=1,
                               name="浦发银行", title="真实急拉",
                               metrics={"pattern": "rocket"}))

    srv, _ = servers(store)
    body = read_json(get(srv, "/api/spirit?limit=10"))
    assert body["count"] == 1
    item = body["items"][0]
    assert item["signal"] == "rocket" and item["cn"] == "火箭发射"
    assert item["dir"] == "up" and item["group"] == "price"
    assert item["code"] == "600000" and item["name"] == "浦发银行"
    assert item["hint"] == "快速上涨并创出当日新高", "hint 必须来自 spirit.SIGNALS"
    assert re.match(r"^\d{2}:\d{2}:\d{2}$", item["ts"]), item["ts"]


def test_dashboard_has_spirit_panel(servers):
    """看板里真的内置了短线精灵面板（前端不是只有接口没有 UI）。"""
    srv, _ = servers()
    html = get(srv, "/").read().decode("utf-8")
    assert "短线精灵" in html
    for needle in ('id="spiritPanel"', 'id="spiritList"', 'id="spFilters"',
                   "/api/spirit", '"spirit"', "等待信号"):
        assert needle in html, f"看板缺少 {needle}"
    # 信号中文名映射不许在前端复制一份
    assert "火箭发射" not in html, "信号中文名必须由服务端给，不能在 JS 里硬编码"
    assert "封涨停板" not in html
    assert "打开涨停" not in html
