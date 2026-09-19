"""真实盘中 soak（浸泡）测试：让真引擎跑真行情 N 分钟，然后给一个诚实的结论。

与 ``tools/probe_live_ready.py`` 的分工
--------------------------------------
``probe_live_ready.py`` 回答的是"**能不能**盘中用"（单点能力探针：股票池、
五档、指数、单轮耗时）。本工具回答的是另一个问题："**连续跑一段**会不会烂掉"
—— 抓取失败、看板接口报错、SSE 断流、内存无界增长，这些只有跑一段时间才暴露。

三个刻意的设计决定
------------------
1. ``poll_once(force=True)``：不带 force 时引擎在非连续竞价时段直接 return ``[]``
   （见 engine.poll_once 的 idle_when_closed 分支），于是"休市时跑 soak"会变成
   什么都没测到 —— 正因为大多数人只有晚上/周末有空跑它，才必须带 force。
2. **0 条告警不算失败**。休市时行情源给的是"最后成交快照"，价格根本不动，
   没有告警才是正确行为。把它判成失败会训练使用者忽略这个工具。
3. 指标聚合与结论判定全部是**纯函数**（plain data in -> metrics/verdict out），
   因此可以在没有网络、没有服务端的条件下离线单测（见
   ``tests/test_live_session_tool.py``）。

跑法::

    python tools\\live_session.py --minutes 1
    python tools\\live_session.py --rounds 20 --browser

退出码：0 = 健康；1 = 有检查项不通过；2 = 连一轮都没跑完（harness 级失败）。
"""
from __future__ import annotations

# Windows 控制台 UTF-8（见 tools/_console.py）。
# 先正常导入；若失败说明本文件是被**按路径**加载的（例如测试用 importlib
# 从 tests/ 里 exec 它），此时 tools/ 不在 sys.path 上——把本文件所在目录
# 补进去再试一次，这样"直接跑"和"被当模块加载"两种场景都能用。
try:
    import _console  # noqa: F401,E402
except ImportError:  # pragma: no cover - 取决于调用方式
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    import _console  # noqa: F401,E402

import argparse
import enum
import json
import logging
import math
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
# 与仓库里其它 tools/*.py 一致：直接以源码树运行，不要求先 pip install -e .
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

UA = "arad-live-session/1.0"
REPORT_VERSION = 1
DEFAULT_MINUTES = 10
#: 允许的抓取失败比例。为什么不是 0：README §5.5 写得很清楚 —— 东财限流是常态，
#: 主源偶发失败会由 SourceManager 自动退到备用源，这是**正常降级路径**而不是故障。
DEFAULT_TOLERANCES: dict[str, Any] = {"max_error_ratio": 0.10, "min_rounds": 1}
#: 股票池规模允许的膨胀倍数（首末对比）。留足余量：新股上市、股票池 TTL 到期后
#: 从"自选股降级"恢复到全市场，都会让这个数字变大，那不是内存泄漏。
MEM_GROWTH_LIMIT = 2.0
MEM_GROWTH_SLACK = 500

EXIT_HEALTHY = 0
EXIT_UNHEALTHY = 1
EXIT_NO_DATA = 2


# ==========================================================================
# 纯函数区：不碰网络、不碰文件、不碰时钟 —— 离线可测
# ==========================================================================
def percentile(values: Sequence[float], p: float) -> float | None:
    """线性插值分位数（0<=p<=100）。空序列返回 None。

    为什么不用 ``statistics.quantiles``：它对 n<2 会抛异常，而 soak 测试很可能
    只跑到 1 轮（``--rounds 1`` 是排查问题的常用姿势），此时不能让统计先崩掉。
    """
    vals = sorted(float(v) for v in values if _finite(v))
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    frac = min(max(float(p), 0.0), 100.0) / 100.0 * (len(vals) - 1)
    lo = int(math.floor(frac))
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (frac - lo)


def latency_stats(values: Sequence[float]) -> dict:
    """单轮耗时统计：min / median / p95 / max / count（毫秒，保留 2 位）。

    p95 而不是平均值才是判断"5 秒轮询够不够"的依据：平均 400ms 但尾部 9s 的
    引擎在盘中会持续堆积，平均数是看不出来的。
    """
    vals = [float(v) for v in values if _finite(v)]
    if not vals:
        return {"count": 0, "min": None, "median": None, "p95": None, "max": None}
    return {
        "count": len(vals),
        "min": round(min(vals), 2),
        "median": round(float(percentile(vals, 50) or 0.0), 2),
        "p95": round(float(percentile(vals, 95) or 0.0), 2),
        "max": round(max(vals), 2),
    }


def make_round_sample(
    *,
    index: int,
    latency_ms: float,
    alerts: Iterable[Any],
    error: bool,
    quotes: int,
    universe: int,
    history_points: int,
    history_codes: int,
    max_deque: int,
    history_maxlen: int,
    watchlist_only: bool,
    observation: dict | None = None,
) -> dict:
    """把一轮的观测值收敛成一个 plain dict（纯函数，便于离线造样本）。

    ``alerts`` 接受 Alert 对象、dict 或裸 kind 字符串：真实运行给的是 Alert，
    测试里手写样本时不必构造一整个 Alert。

    ``observation`` 是本轮的 ``RoundObservationSet.as_dict()``（WP02）。
    以前这里只有 ``quotes=len(state.quotes)`` —— 那是**累计缓存**的 key 数，
    不是本轮真实观测数，休市/切源时会把旧值冒充成本轮覆盖（IT-P2-OBS-001）。
    """
    obs = dict(observation or {})
    out = {
        "index": int(index),
        "latency_ms": float(latency_ms),
        "kinds": [_kind_of(a) for a in (alerts or [])],
        "error": bool(error),
        "quotes": int(quotes),
        "universe": int(universe),
        "history_points": int(history_points),
        "history_codes": int(history_codes),
        "max_deque": int(max_deque),
        "history_maxlen": int(history_maxlen),
        "watchlist_only": bool(watchlist_only),
    }
    # 本轮真实观测账本（有则并入；离线造样本不传时保持旧形状）。
    # 全部走 _safe_int/_safe_float：可观测性字段脏了不能让 soak 崩，
    # 报告宁可少几项也不能因为一个 None/字符串把整场 soak 打断。
    if obs:
        out["requested"] = _safe_int(obs.get("requested"))
        out["returned"] = _safe_int(obs.get("returned"))
        out["admitted"] = _safe_int(obs.get("admitted"))
        out["coverage"] = _safe_float(obs.get("coverage"))
        out["source"] = str(obs.get("source") or "")
        out["future_rejected"] = _safe_int(obs.get("stale_rejected"))
        out["out_of_order_rejected"] = _safe_int(obs.get("out_of_order_rejected"))
        missing = obs.get("unknown_missing")
        out["unknown_missing"] = len(missing) if isinstance(missing, (list, tuple)) else 0
        out["unavailable_capability"] = _safe_int(obs.get("unavailable_capability"))
        caps = obs.get("capabilities")
        out["capabilities"] = dict(caps) if isinstance(caps, dict) else {}
    return out


def summarize_rounds(rounds: Sequence[dict]) -> dict:
    """逐轮样本 -> 聚合指标（纯函数）。

    输出的字段名就是报告里 ``metrics`` 的形状，``evaluate_health`` 直接消费它，
    两者之间没有第二套命名，避免"报告里是这个名、判定时读那个名"的错位。
    """
    rows = [r for r in (rounds or []) if isinstance(r, dict)]
    by_kind: dict[str, int] = {}
    for r in rows:
        for k in r.get("kinds") or []:
            by_kind[str(k)] = by_kind.get(str(k), 0) + 1

    lat = [float(r.get("latency_ms") or 0.0) for r in rows]
    quotes = [int(r.get("quotes") or 0) for r in rows]
    universe = [int(r.get("universe") or 0) for r in rows]

    def _edge(seq: Sequence[int]) -> dict:
        if not seq:
            return {"first": 0, "last": 0, "min": 0, "max": 0}
        return {"first": seq[0], "last": seq[-1], "min": min(seq), "max": max(seq)}

    # 降级判据：股票池规模不比自己那几只自选股大，说明全市场扫描没起来
    # （engine.run_forever 用的是同一个判据）。
    watch_only_rounds = sum(1 for r in rows if r.get("watchlist_only"))

    return {
        "rounds": len(rows),
        "latency_ms": latency_stats(lat),
        "error_rounds": sum(1 for r in rows if r.get("error")),
        "no_data_rounds": sum(1 for r in rows if int(r.get("quotes") or 0) <= 0),
        "alerts_total": sum(len(r.get("kinds") or []) for r in rows),
        "alerts_by_kind": by_kind,
        "quotes": _edge(quotes),
        "universe": _edge(universe),
        "watchlist_only_rounds": watch_only_rounds,
        "fell_back_to_watchlist": bool(rows) and watch_only_rounds >= len(rows),
        "memory_samples": [
            {
                "quotes": int(r.get("quotes") or 0),
                "history_points": int(r.get("history_points") or 0),
                "history_codes": int(r.get("history_codes") or 0),
                "max_deque": int(r.get("max_deque") or 0),
                "history_maxlen": int(r.get("history_maxlen") or 0),
            }
            for r in rows
        ],
    }


def check_memory(samples: Sequence[dict], *, growth_limit: float = MEM_GROWTH_LIMIT,
                 slack: int = MEM_GROWTH_SLACK) -> dict:
    """内存代理指标的有界性判定（纯函数）。

    为什么不看"历史点数首末比值"：那是**预期增长**而不是泄漏 —— state.history
    每个 code 一个 deque，跑 1 分钟就从 1 个点涨到 12 个点（12 倍），可它由
    ``maxlen=history_len`` 硬性封顶，永远不会无界。所以判据是三条真正有意义的：

    1. ``max_deque <= maxlen``：deque 没超上限（这是"有界"的**充分**证据）；
    2. ``history_points <= history_codes * maxlen``：单只股票的点数不超过上限；
    3. 股票池规模首末对比不爆炸（唯一可能真正无界增长的是 key 的数量）。
    """
    rows = [s for s in (samples or []) if isinstance(s, dict)]
    if not rows:
        return {"bounded": False, "reason": "无样本，无法判定", "samples": 0,
                "deques_ok": False, "density_ok": False, "codes_ok": False}

    first, last = rows[0], rows[-1]
    maxlen = max(int(s.get("history_maxlen") or 0) for s in rows)
    limit = max(maxlen, 30)          # engine 里 deque 是 maxlen=max(history_len, 30)
    max_deque = max(int(s.get("max_deque") or 0) for s in rows)

    quotes_first = int(first.get("quotes") or 0)
    quotes_last = int(last.get("quotes") or 0)
    pts_last = int(last.get("history_points") or 0)
    codes_last = max(int(last.get("history_codes") or 0), 1)

    deques_ok = max_deque <= limit
    density_ok = pts_last <= codes_last * limit
    codes_ok = quotes_last <= max(quotes_first, 1) * float(growth_limit) + int(slack)

    reasons = []
    if not deques_ok:
        reasons.append(f"单只历史点数 {max_deque} 超过上限 {limit}")
    if not density_ok:
        reasons.append(f"历史点总数 {pts_last} > 代码数 {codes_last}×{limit}")
    if not codes_ok:
        reasons.append(f"股票池 {quotes_first} -> {quotes_last} 增长超 {growth_limit}×")
    return {
        "bounded": bool(deques_ok and density_ok and codes_ok),
        "reason": "；".join(reasons) if reasons else "历史窗口有界，股票池无爆炸",
        "samples": len(rows),
        "history_maxlen": limit,
        "max_deque": max_deque,
        "quotes_first": quotes_first,
        "quotes_last": quotes_last,
        "history_points_first": int(first.get("history_points") or 0),
        "history_points_last": pts_last,
        "history_codes_last": int(last.get("history_codes") or 0),
        "deques_ok": bool(deques_ok),
        "density_ok": bool(density_ok),
        "codes_ok": bool(codes_ok),
    }


def empty_metrics() -> dict:
    """一份"健康骨架"指标。离线单测直接改其中一项即可模拟某种失败模式。"""
    return {
        "rounds": 0,
        "latency_ms": {"count": 0, "min": None, "median": None, "p95": None, "max": None},
        "error_rounds": 0,
        "no_data_rounds": 0,
        "alerts_total": 0,
        "alerts_by_kind": {},
        "quotes": {"first": 0, "last": 0, "min": 0, "max": 0},
        "universe": {"first": 0, "last": 0, "min": 0, "max": 0},
        "watchlist_only_rounds": 0,
        "fell_back_to_watchlist": False,
        "memory": {"bounded": True, "reason": "（未采样）"},
        "api": {"requests": 0, "non_200": 0, "malformed": 0, "unreachable": 0, "by_route": {}},
        "sse": {"connected": False, "stalled": False, "events_total": 0, "events_by_type": {},
                "status": None, "error": None},
        "logs": {"WARNING": 0, "ERROR": 0, "CRITICAL": 0},
        "browser": {"ran": False, "skipped": True, "errors": []},
        "setup": {"engine_build_s": None, "universe_refresh_s": None, "universe_size": 0},
    }


def finalize_metrics(
    rounds: Sequence[dict],
    *,
    api: dict | None = None,
    sse: dict | None = None,
    logs: dict | None = None,
    browser: dict | None = None,
    setup: dict | None = None,
) -> dict:
    """逐轮样本 + 看板/SSE/日志统计 -> 完整的 metrics（纯函数）。

    各子项用 ``empty_metrics()`` 的默认值兜底：某个探针没能启动时（例如端口被占），
    报告仍然是完整形状，不会因为缺 key 让下游判定 KeyError。
    """
    out = empty_metrics()
    out.update(summarize_rounds(rounds))
    out["memory"] = check_memory(out.pop("memory_samples", []))
    for key, val in (("api", api), ("sse", sse), ("logs", logs),
                     ("browser", browser), ("setup", setup)):
        if isinstance(val, dict):
            merged = dict(out[key])
            merged.update(val)
            out[key] = merged
    return out


def evaluate_health(metrics: dict, *, tolerances: dict | None = None) -> dict:
    """指标 -> 结论（纯函数）。返回 ``{"healthy", "exit_code", "checks"}``。

    判定项只有"真的出错"的那几类，**告警条数不参与判定**（休市 0 条是正常的）。
    每项都带 ``detail``，因为"不健康"这三个字对使用者毫无信息量，必须说清哪项、
    差多少 —— 尤其是失败轮数 3 轮、容差 1 轮这种"只差一点"的情况。
    """
    tol = dict(DEFAULT_TOLERANCES)
    tol.update(tolerances or {})
    m = metrics if isinstance(metrics, dict) else {}
    rounds = int(m.get("rounds") or 0)
    error_rounds = int(m.get("error_rounds") or 0)
    no_data_rounds = int(m.get("no_data_rounds") or 0)
    api = dict(m.get("api") or {})
    sse = dict(m.get("sse") or {})
    memory = dict(m.get("memory") or {})
    browser = dict(m.get("browser") or {})
    by_kind = dict(m.get("alerts_by_kind") or {})
    quotes_max = int((m.get("quotes") or {}).get("max") or 0)

    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    min_rounds = int(tol.get("min_rounds") or 1)
    add("rounds", rounds >= min_rounds, f"完成 {rounds} 轮（下限 {min_rounds}）")

    ratio = float(tol.get("max_error_ratio") or 0.0)
    budget = int(rounds * ratio)
    bad = error_rounds + no_data_rounds
    add("fetch", bad <= budget,
        f"抓取失败 {error_rounds} 轮 + 空数据 {no_data_rounds} 轮 = {bad}，"
        f"容差 {budget} 轮（{ratio:.0%}）")

    add("data", quotes_max > 0, f"单轮最多拿到 {quotes_max} 只行情")

    req = int(api.get("requests") or 0)
    non200 = int(api.get("non_200") or 0)
    bad_json = int(api.get("malformed") or 0)
    unreachable = int(api.get("unreachable") or 0)
    add("api", req > 0 and non200 == 0 and bad_json == 0 and unreachable == 0,
        f"请求 {req} 次：非 200 {non200}，JSON 解析失败 {bad_json}，连不上 {unreachable}")

    events = int(sse.get("events_total") or 0)
    add("sse", bool(sse.get("connected")) and not bool(sse.get("stalled")) and events > 0,
        f"连接 {'是' if sse.get('connected') else '否'}，事件 {events} 条，"
        f"断流 {'是' if sse.get('stalled') else '否'}")

    add("memory", bool(memory.get("bounded")), str(memory.get("reason") or ""))

    if browser.get("ran"):
        errs = list(browser.get("errors") or [])
        add("browser", not errs, f"pageerror/console.error {len(errs)} 条")
    else:
        add("browser", True, "未运行（跳过不算失败）")

    total = sum(int(v) for v in by_kind.values())
    add("alerts", True, f"共 {total} 条告警（0 条不算失败：休市时本就无告警）")

    healthy = all(c["ok"] for c in checks)
    if rounds < min_rounds:
        # 一轮都没跑完 = harness 级失败，与"跑起来了但有问题"要能区分开
        return {"healthy": False, "exit_code": EXIT_NO_DATA, "checks": checks}
    return {
        "healthy": healthy,
        "exit_code": EXIT_HEALTHY if healthy else EXIT_UNHEALTHY,
        "checks": checks,
    }


def sanitize(obj: Any) -> Any:
    """把任意对象转成"JSON 原生类型 + 有限数值"的结构（纯函数）。

    两个必须做的理由：

    * ``json.dumps`` 默认写出裸 ``NaN``/``Infinity``，那是**非法 JSON**，别的
      工具链（jq / 浏览器）读不了；真实行情确实会产出 inf（同
      ``models.Alert.to_dict`` 的处理），所以只靠"我传进去的都是好数"不成立。
    * tuple/set 转成 list：否则
      ``json.loads(json.dumps(report)) == report`` 这个往返断言会假失败。
    """
    if obj is None or isinstance(obj, bool) or isinstance(obj, str):
        return obj
    if isinstance(obj, enum.Enum):
        return sanitize(obj.value)
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        # set 无序，排序后才能保证同一份数据每次序列化结果一致
        return [sanitize(v) for v in sorted(obj, key=str)]
    if isinstance(obj, (list, tuple)):
        return [sanitize(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat(timespec="seconds")
    return str(obj)


def build_report(
    *,
    started_at: str,
    finished_at: str,
    duration_s: float,
    bounds: dict,
    session_info: dict,
    config_snapshot: dict,
    metrics: dict,
    verdict: dict,
    notes: Sequence[str] | None = None,
) -> dict:
    """组装报告（纯函数）。

    ``config_snapshot`` 是刻意留的：soak 结果只有配上"当时用的什么间隔、开了
    哪些规则"才可比较；否则两次运行的差异永远解释不清。
    """
    return sanitize({
        "tool": "tools/live_session.py",
        "report_version": REPORT_VERSION,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_s": round(float(duration_s), 2),
        "bounds": bounds,
        "session": session_info,
        "config": config_snapshot,
        "metrics": metrics,
        "verdict": verdict,
        "notes": list(notes or []),
    })


def _finite(v: Any) -> bool:
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def _kind_of(item: Any) -> str:
    """从 Alert / dict / 字符串里取出 kind（纯函数）。"""
    if item is None:
        return "unknown"
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        raw = item.get("kind")
    else:
        raw = getattr(item, "kind", None)
    if raw is None:
        return "unknown"
    return str(getattr(raw, "value", raw))


def _safe_int(v: Any) -> int:
    """可观测性计数转 int；None/脏值一律 0（**不抛**）。

    这些字段来自 Store 汇总，理论上都是干净的 int，但 soak 是要连续跑几小时
    的，任何一个脏值把整场 soak 打断都得不偿失 —— 报告少一项远好过全丢。
    """
    if isinstance(v, bool) or v is None:
        return 0
    if isinstance(v, int):
        return v
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _safe_float(v: Any) -> float:
    """可观测性比率转 float；None/脏值一律 0.0（**不抛**）。"""
    if isinstance(v, bool) or v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ==========================================================================
# 采集器：会碰网络/线程，但只负责计数，不做任何判断
# ==========================================================================
class StreamProbe(threading.Thread):
    """独占一条 ``/api/stream`` SSE 长连接，按事件类型计数。

    为什么必须"真的挂着"而不是只发一次请求：SSE 是这个看板唯一的长连接通道，
    断流只有在**持续订阅**时才会暴露（例如服务端忘了给订阅队列续命、生成器
    提前 return）。判断"断流"用 socket 读超时：服务端每 ``sse_interval`` 秒
    至少发一条 tick 心跳，超过 ``stall_after`` 没动静就是真断了。
    """

    def __init__(self, url: str, *, stall_after: float = 8.0) -> None:
        super().__init__(name="arad-live-sse", daemon=True)
        self.url = url
        self.stall_after = max(1.0, float(stall_after))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._connected = False
        self._status: int | None = None
        self._error: str | None = None
        self._stalled = False
        self._lines = 0
        self._events: dict[str, int] = {}
        self._last_event_at: float | None = None

    # -- 线程体 ---------------------------------------------------------
    def run(self) -> None:
        req = urllib.request.Request(
            self.url, headers={"Accept": "text/event-stream", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=self.stall_after) as resp:
                with self._lock:
                    self._status = int(getattr(resp, "status", 0) or 0)
                    self._connected = self._status == 200
                if self._status != 200:
                    return
                with self._lock:
                    self._last_event_at = time.time()
                while not self._stop.is_set():
                    try:
                        raw = resp.readline()
                    except (socket.timeout, TimeoutError):
                        # 心跳都没了 -> 判定断流并退出（再等下去也只会一直超时）
                        with self._lock:
                            self._stalled = True
                        return
                    except OSError as exc:
                        with self._lock:
                            self._error = f"{type(exc).__name__}: {exc}"
                        return
                    if not raw:
                        return                      # EOF：服务端关了连接
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    with self._lock:
                        self._lines += 1
                    if line.startswith("event:"):
                        name = line[6:].strip() or "message"
                        with self._lock:
                            self._events[name] = self._events.get(name, 0) + 1
                            self._last_event_at = time.time()
        except Exception as exc:                     # noqa: BLE001 —— 探针失败不能拖垮主流程
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"

    # -- 控制/读取 -------------------------------------------------------
    def stop_stream(self) -> None:
        """停止订阅（不阻塞：socket 会在服务端关连接后自然 EOF）。"""
        self._stop.set()

    def snapshot(self, *, final: bool = False) -> dict:
        with self._lock:
            events = dict(self._events)
            last = self._last_event_at
            stalled = self._stalled
            out = {
                "status": self._status,
                "connected": self._connected,
                "stalled": stalled,
                "lines": self._lines,
                "events_by_type": events,
                "events_total": sum(events.values()),
                "error": self._error,
            }
        if final and self._connected and last is not None and not stalled:
            # 收尾时再判一次：整个 run 期间没动静，等同于断流
            out["stalled"] = (time.time() - last) > self.stall_after
        return out


class ApiProbe:
    """轮询看板 REST 接口，统计非 200 / 连不上 / JSON 解析失败。

    只统计**不修复**：看板接口出问题时报出来就是本工具的价值，替它兜底会让
    真实故障在报告里消失。
    """

    def __init__(self, base: str, routes: Sequence[str], *, timeout: float = 5.0) -> None:
        self.base = base.rstrip("/")
        self.routes = list(routes)
        self.timeout = float(timeout)
        self.requests = 0
        self.non_200 = 0
        self.malformed = 0
        self.unreachable = 0
        self.by_route: dict[str, dict[str, int]] = {
            p: {"requests": 0, "non_200": 0, "malformed": 0, "unreachable": 0}
            for p in self.routes
        }
        self.samples: dict[str, Any] = {}

    def hit(self, path: str) -> tuple[int | None, Any]:
        """请求单个路径；返回 (状态码, 解析后的 JSON 或 None)。"""
        url = f"{self.base}{path}"
        self.requests += 1
        self.by_route.setdefault(path, {"requests": 0, "non_200": 0,
                                        "malformed": 0, "unreachable": 0})
        self.by_route[path]["requests"] += 1
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = int(getattr(resp, "status", 0) or 0)
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            self.non_200 += 1
            self.by_route[path]["non_200"] += 1
            return int(exc.code or 0), None
        except Exception:                            # noqa: BLE001 —— 连不上也是一种失败统计
            self.unreachable += 1
            self.by_route[path]["unreachable"] += 1
            return None, None
        if status != 200:
            self.non_200 += 1
            self.by_route[path]["non_200"] += 1
            return status, None
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except Exception:                            # noqa: BLE001
            self.malformed += 1
            self.by_route[path]["malformed"] += 1
            return status, None
        if not isinstance(data, (dict, list)):
            # 合法 JSON 但不是对象/数组 —— 对看板前端等价于坏数据
            self.malformed += 1
            self.by_route[path]["malformed"] += 1
            return status, None
        return status, data

    def poll_all(self) -> None:
        for path in self.routes:
            status, data = self.hit(path)
            if data is not None:
                self.samples[path] = _summarize_payload(path, data)

    def snapshot(self) -> dict:
        return {
            "requests": self.requests,
            "non_200": self.non_200,
            "malformed": self.malformed,
            "unreachable": self.unreachable,
            "by_route": {k: dict(v) for k, v in self.by_route.items()},
            "samples": dict(self.samples),
        }


class LogCounter(logging.Handler):
    """按级别统计日志条数（WARNING/ERROR/CRITICAL）。

    引擎的 ``_errors`` 只覆盖"抓取整轮失败"，而规则求值异常、通知器异常只出现在
    日志里。soak 的结论不该漏掉这些，所以把日志也变成可比的数字。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.counts: dict[str, int] = {"WARNING": 0, "ERROR": 0, "CRITICAL": 0}

    def emit(self, record: logging.LogRecord) -> None:
        name = record.levelname
        self.counts[name] = self.counts.get(name, 0) + 1

    def snapshot(self) -> dict:
        return dict(self.counts)


def _summarize_payload(path: str, data: Any) -> dict:
    """把接口返回压缩成几个可比数字（不整包存进报告，否则报告会变成日志）。"""
    if isinstance(data, dict):
        out: dict[str, Any] = {"type": "dict", "keys": len(data)}
        for key in ("count", "total", "universe", "alerts_total", "phase", "ok", "status"):
            if key in data:
                out[key] = data[key]
        return out
    return {"type": "list", "len": len(data)}


# ==========================================================================
# 运行期（有副作用）
# ==========================================================================
def _configure_stdout() -> None:
    """把 stdout 固定成 UTF-8。

    Windows 默认 cp936 下 ``print("✓")`` 会抛 UnicodeEncodeError —— 一个只在
    中文 Windows 上炸的崩溃，比任何检查失败都难排查。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:                                # noqa: BLE001
        pass


def _setup_logging(verbose: bool, counter: LogCounter) -> None:
    """引擎日志 -> stderr（WARNING 起）+ 计数 handler。

    默认不打印 INFO：soak 一跑几十轮，每轮一条"股票池已刷新"会把结论表冲没。
    """
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    stream = logging.StreamHandler(sys.stderr)
    stream.setLevel(logging.INFO if verbose else logging.WARNING)
    stream.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(stream)
    root.addHandler(counter)
    root.setLevel(logging.INFO)


def _free_port() -> int:
    """返回一个当前空闲的端口（仅用于探测；真正的绑定仍走 create_server）。

    注意这里必须 close 掉探测用的 socket，否则端口一直被占着 —— 这正是
    "找不到空闲端口"这类问题的经典成因。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _fmt_table(checks: Sequence[dict]) -> str:
    lines = []
    for c in checks:
        lines.append(f"  {'✓' if c.get('ok') else '✗'} {c.get('name', '?'):<9}"
                     f"{c.get('detail', '')}")
    return "\n".join(lines)


def _collect_config_snapshot(st: Any, engine: Any, watchlist: Sequence[str]) -> dict:
    """配置快照：让多次运行之间可比（尤其是轮询间隔与启用的规则）。"""
    rules_raw = st.section("rules") if hasattr(st, "section") else {}
    return {
        "app": {
            "dry_run": bool(st.get("app.dry_run", True)),
            "replay": bool(st.get("app.replay", False)),
            "log_level": st.get("app.log_level"),
        },
        "poll": {
            "universe_seconds": st.get("poll.universe_seconds"),
            "watchlist_seconds": st.get("poll.watchlist_seconds"),
            "universe_refresh_seconds": st.get("poll.universe_refresh_seconds"),
            "history_len": st.get("poll.history_len"),
            "batch_size": st.get("poll.batch_size"),
            "workers": st.get("poll.workers"),
            "http_timeout": st.get("poll.http_timeout"),
            "retries": st.get("poll.retries"),
            "idle_when_closed": st.get("poll.idle_when_closed"),
            "index_codes": list(st.get("poll.index_codes") or []),
        },
        "sources": {
            "primary": st.get("sources.primary"),
            "universe": st.get("sources.universe"),
            "fallback": list(st.get("sources.fallback") or []),
            "failover_threshold": st.get("sources.failover_threshold"),
        },
        "rules": {
            "effective": [str(getattr(r, "name", r.__class__.__name__)) for r in engine.rules],
            "enabled_in_config": sorted(
                [str(k) for k, v in (rules_raw or {}).items()
                 if isinstance(v, dict) and v.get("enabled")],
            ),
            "raw": rules_raw,
        },
        "notifiers": {
            "enabled_in_config": st.get("notify.enabled"),
            "effective": [str(getattr(n, "name", n.__class__.__name__)) for n in engine.notifiers],
        },
        "web": {
            "sse_interval": st.get("web.sse_interval"),
            "top_n": st.get("web.top_n"),
            "max_alerts": st.get("web.max_alerts"),
        },
        "watchlist_size": len(list(watchlist)),
        "filters": dict(st.section("filters") or {}) if hasattr(st, "section") else {},
    }


def _check_browser(url: str, *, wait_ms: int = 2500) -> dict:
    """可选的浏览器检查：抓 pageerror / console.error。任何问题都只"跳过"。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ~ 未安装 Playwright，跳过浏览器检查"
              "（pip install playwright && playwright install chromium）")
        return {"ran": False, "skipped": True, "errors": [],
                "reason": "playwright 未安装"}
    errors: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
                page.on("console",
                        lambda m: errors.append(f"console.error: {m.text}")
                        if m.type == "error" else None)
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(wait_ms)
            finally:
                browser.close()
    except Exception as exc:                         # noqa: BLE001 —— 浏览器不可用不算失败
        print(f"  ~ 浏览器检查跳过（{type(exc).__name__}: {exc}）")
        return {"ran": False, "skipped": True, "errors": errors,
                "reason": f"{type(exc).__name__}: {exc}"}
    print(f"  浏览器检查完成：pageerror/console.error {len(errors)} 条")
    return {"ran": True, "skipped": False, "errors": errors[:20], "url": url}


def _soak_loop(engine: Any, probe: ApiProbe, *, max_rounds: int | None,
               deadline: float, interval: float, verbose: bool) -> tuple[list[dict], dict]:
    """主 soak 循环：逐轮 poll_once(force=True) + 看板探活。"""
    rounds: list[dict] = []
    flags = {"deadline_hit": False, "interrupted": False, "exception": None}
    while True:
        if max_rounds is not None and len(rounds) >= max_rounds:
            break
        if time.monotonic() >= deadline:
            flags["deadline_hit"] = True
            break

        state = engine.state
        # _errors 是引擎内部计数器，也是"这一轮抓取失败了"的唯一真实来源
        # （poll_once 失败时直接 return []，外部看不出与"没告警"的区别）。
        errs_before = int(getattr(engine, "_errors", 0) or 0)
        t0 = time.perf_counter()
        fresh: list[Any] = []
        exc_note: str | None = None
        try:
            fresh = engine.poll_once(force=True) or []
        except KeyboardInterrupt:
            raise
        except Exception as exc:                     # noqa: BLE001 —— 单轮异常要留在报告里
            exc_note = f"{type(exc).__name__}: {exc}"
        latency_ms = (time.perf_counter() - t0) * 1000.0
        error = exc_note is not None or int(getattr(engine, "_errors", 0) or 0) > errs_before

        codes = list(getattr(engine, "_codes", []) or [])
        watch = list(getattr(engine, "watchlist", []) or [])
        history = getattr(state, "history", {}) or {}
        maxlen = max([getattr(h, "maxlen", 0) or 0 for h in history.values()]
                     + [int(getattr(state, "history_len", 0) or 0), 30])
        # 本轮真实观测账本（WP02 / IT-P2-OBS-001）。Store 里存的是**最近一轮**
        # 的有界汇总；拿不到就退化为不含 observation 的旧形状。
        obs_snapshot: dict = {}
        try:
            store = getattr(engine, "store", None)
            get_obs = getattr(store, "observation", None)
            if isinstance(get_obs, dict) and get_obs:
                obs_snapshot = get_obs
        except Exception:                            # noqa: BLE001
            obs_snapshot = {}

        sample = make_round_sample(
            index=len(rounds) + 1,
            latency_ms=latency_ms,
            alerts=fresh,
            error=error,
            quotes=len(getattr(state, "quotes", {}) or {}),
            universe=len(codes),
            history_points=sum(len(h) for h in history.values()),
            history_codes=len(history),
            max_deque=max((len(h) for h in history.values()), default=0),
            history_maxlen=maxlen,
            watchlist_only=bool(codes) and len(codes) <= len(watch),
            observation=obs_snapshot,
        )
        rounds.append(sample)

        probe.poll_all()                             # 每轮顺带探一次看板 REST

        note = f"  异常: {exc_note}" if exc_note else ""
        print(f"  第 {sample['index']:>3} 轮 | {latency_ms / 1000:6.2f}s | "
              f"池 {sample['universe']:>5} | 行情 {sample['quotes']:>5} | "
              f"告警 {len(fresh):>3} | 历史点 {sample['history_points']:>7}{note}",
              flush=True)
        if verbose and fresh:
            for a in fresh[:5]:
                print(f"        · {_kind_of(a)} {getattr(a, 'code', '?')} "
                      f"{getattr(a, 'name', '')}")

        if max_rounds is not None and len(rounds) >= max_rounds:
            break
        # 间隔补偿：poll_once 本身耗时算进间隔，避免实际轮询周期被拉长
        target = time.monotonic() + max(0.0, interval - (time.perf_counter() - t0))
        while time.monotonic() < target:
            if time.monotonic() >= deadline:
                flags["deadline_hit"] = True
                break
            time.sleep(min(0.2, max(0.0, target - time.monotonic())))
    return rounds, flags


def run(args: argparse.Namespace) -> int:
    """执行一次 soak；返回进程退出码。"""
    _configure_stdout()
    counter = LogCounter()
    _setup_logging(bool(args.verbose), counter)

    from arad.config import PROJECT_ROOT, load_settings
    from arad.engine import Engine, build_notifiers, build_rules
    from arad.server import web as webmod
    from arad.session import TradingCalendar

    print("=" * 66)
    print(" A股盘中雷达 · 真实 soak 测试（tools/live_session.py）")
    print("=" * 66)

    # --- 1) 配置与时段 ---------------------------------------------------
    st = load_settings(use_cache=False)              # 必须绕过缓存：下面会改配置
    calendar = TradingCalendar.load()
    now = datetime.now()
    phase = calendar.phase(now)
    is_open = bool(calendar.is_open(now))
    started_at = now.isoformat(timespec="seconds")

    print(f"[1/5] 时段：{calendar.describe(now)}")
    print(f"      市场状态：{'✓ 连续竞价中' if is_open else '✗ 未开市（非连续竞价时段）'}")
    if not is_open:
        print("      说明：仍用 force=True 跑完整数据链路；休市时的行情是"
              "“最后成交快照”，")
        print("            价格基本不动，因此 0 条告警是正常结果，不计入失败。")

    interval = float(st.get("poll.universe_seconds", 5) or 5)
    minutes = float(args.minutes)
    max_rounds = int(args.rounds) if args.rounds and int(args.rounds) > 0 else None
    bound_txt = f"轮数 {max_rounds}" if max_rounds else f"时长 {minutes} 分钟"
    print(f"      边界：{bound_txt}（间隔 {interval:.1f}s，"
          f"--minutes 作为兜底上限始终生效）")

    # --- 2) 引擎 ---------------------------------------------------------
    t0 = time.perf_counter()
    engine = Engine(source=None, settings=st, rules=build_rules(st),
                    notifiers=build_notifiers(st), calendar=calendar)
    build_s = time.perf_counter() - t0
    print(f"[2/5] 引擎就绪（{build_s:.2f}s）：规则 "
          f"{[str(getattr(r, 'name', '?')) for r in engine.rules]}")
    print(f"      通知器：{[str(getattr(n, 'name', '?')) for n in engine.notifiers]}"
          f"（dry_run={st.get('app.dry_run')}）")

    # 先把股票池预热好，不然第一轮的 latency 里会混进 20 秒的股票池刷新，
    # 后面所有 p95/max 都被这一个离群值污染。
    t0 = time.perf_counter()
    universe_size = 0
    try:
        universe_size = int(engine.refresh_universe() or 0)
    except Exception as exc:                         # noqa: BLE001
        print(f"      [!] 股票池刷新异常：{type(exc).__name__}: {exc}")
    refresh_s = time.perf_counter() - t0
    codes = list(getattr(engine, "_codes", []) or [])
    watch = list(getattr(engine, "watchlist", []) or [])
    fell_back = bool(codes) and len(codes) <= len(watch)
    print(f"      股票池：{len(codes)} 只（刷新 {refresh_s:.1f}s）"
          + ("  [!] 已降级为仅自选股" if fell_back else ""))
    if not codes:
        print("      [!] 股票池为空且自选股也没配 -> 本轮不会产生任何行情/告警")

    # --- 3) 看板 + SSE ---------------------------------------------------
    routes_wanted = ["/api/health", "/api/status", "/api/spirit", "/api/alerts"]
    route_table = getattr(webmod, "_GET_ROUTES", {}) or {}
    routes = [p for p in routes_wanted if p in route_table]
    missing = [p for p in routes_wanted if p not in route_table]
    print(f"[3/5] 看板接口探活：{' '.join(routes) if routes else '（无可用路由）'}")
    if missing:
        print(f"      [!] 路由表里不存在，已跳过：{' '.join(missing)}"
              f"（以 web.py 的 _GET_ROUTES 为准）")

    cfg = dict(st.web or {})
    cfg["host"] = "127.0.0.1"
    port_arg = int(args.port or 0)
    server = None
    stream: StreamProbe | None = None
    web_thread: threading.Thread | None = None
    base_url = ""
    rounds: list[dict] = []
    flags: dict = {"deadline_hit": False, "interrupted": False, "exception": None}
    browser_info: dict = {"ran": False, "skipped": True, "errors": [],
                          "reason": "未请求（--browser）"}
    probe: ApiProbe | None = None
    sse_info: dict = {}
    fatal: str | None = None
    t_start = time.monotonic()

    try:
        # port=0：由内核分配空闲端口，再读回真实端口 —— 比"先探测再绑定"少一次竞态
        server = webmod.create_server(engine.store, cfg, host="127.0.0.1", port=port_arg)
        real_port = int(server.server_address[1])
        if real_port <= 0:
            raise RuntimeError(f"看板端口异常：{server.server_address!r}")
        base_url = f"http://127.0.0.1:{real_port}"
        web_thread = threading.Thread(target=server.serve_forever,
                                      kwargs={"poll_interval": 0.2},
                                      name="arad-live-web", daemon=True)
        web_thread.start()
        print(f"      看板已启动：{base_url}/")

        stall_after = max(8.0, 3.0 * float(cfg.get("sse_interval") or 2.0))
        stream = StreamProbe(f"{base_url}/api/stream", stall_after=stall_after)
        stream.start()
        # 等 SSE 真正连上再开跑，否则首轮的 phase/alert 事件会漏掉
        deadline_conn = time.monotonic() + 5.0
        while time.monotonic() < deadline_conn:
            if stream.snapshot().get("connected") or stream.snapshot().get("error"):
                break
            time.sleep(0.05)
        print(f"      SSE 订阅：{stream.snapshot()}")

        probe = ApiProbe(base_url, routes)

        # --- 4) soak ----------------------------------------------------
        print(f"[4/5] 开始 soak …")
        deadline = time.monotonic() + minutes * 60.0
        rounds, flags = _soak_loop(engine, probe, max_rounds=max_rounds,
                                   deadline=deadline, interval=interval,
                                   verbose=bool(args.verbose))

        # --- 5) 可选浏览器 ----------------------------------------------
        print("[5/5] 收尾检查")
        if args.browser:
            browser_info = _check_browser(base_url + "/")
        else:
            print("  ~ 未指定 --browser，跳过浏览器检查")
    except KeyboardInterrupt:
        flags["interrupted"] = True
        print("\n[!] 收到 Ctrl+C：停止轮询，仍然生成本次报告与结论。")
    except Exception as exc:                         # noqa: BLE001 —— 也要写报告，方便事后定位
        fatal = f"{type(exc).__name__}: {exc}"
        print(f"\n[!] 运行异常：{fatal}")
    finally:
        # 收尾顺序有讲究：先让 SSE 自己退出（服务端 shutdown 会给它发 bye 并关闭
        # 连接，客户端读到 EOF 自然结束），再关监听套接字。任何一条都不能漏，
        # 否则端口会一直被占着，下一次运行直接 bind 失败。
        if stream is not None:
            stream.stop_stream()
        if server is not None:
            try:
                server.shutdown()
            except Exception:                        # noqa: BLE001
                pass
            try:
                server.server_close()
            except Exception:                        # noqa: BLE001
                pass
        if stream is not None:
            stream.join(timeout=3.0)
            sse_info = stream.snapshot(final=True)
            if stream.is_alive():
                print("  [!] SSE 线程未在 3s 内退出（daemon，不阻塞进程退出）")
        if web_thread is not None:
            web_thread.join(timeout=3.0)
        if server is not None:
            print(f"      看板已关闭，端口 {int(server.server_address[1])} 已释放")

    durations = time.monotonic() - t_start
    api_info = probe.snapshot() if probe is not None else empty_metrics()["api"]
    metrics = finalize_metrics(
        rounds,
        api=api_info,
        sse=sse_info or {"connected": False, "stalled": False, "events_total": 0},
        logs=counter.snapshot(),
        browser=browser_info,
        setup={"engine_build_s": round(build_s, 3),
               "universe_refresh_s": round(refresh_s, 3),
               "universe_size": universe_size,
               "fell_back_to_watchlist": fell_back,
               "sse_stall_after_s": max(8.0, 3.0 * float(cfg.get("sse_interval") or 2.0))
               if cfg else None},
    )
    verdict = evaluate_health(metrics)
    if fatal and verdict["exit_code"] == EXIT_HEALTHY:
        verdict = {"healthy": False, "exit_code": EXIT_UNHEALTHY,
                   "checks": verdict["checks"] + [
                       {"name": "fatal", "ok": False, "detail": fatal}]}

    notes = [
        "休市时行情为最后成交快照，0 条告警不等于故障",
        "force=True 已强制跑完整数据链路（否则休市时 poll_once 直接返回空）",
        f"抓取失败容差为 {DEFAULT_TOLERANCES['max_error_ratio']:.0%} 轮（东财限流是常态）",
    ]
    if flags.get("interrupted"):
        notes.append("本次运行被 Ctrl+C 中断，统计仅覆盖已完成的轮次")
    if flags.get("deadline_hit"):
        notes.append("本次运行到达 --minutes 兜底上限后停止")

    session_info = {
        "checked_at": now.isoformat(timespec="seconds"),
        "phase": str(getattr(phase, "value", phase)),
        "is_open": is_open,
        "describe": calendar.describe(now),
        "note": ("连续竞价中，行情为实时数据" if is_open
                 else "非连续竞价时段，行情为最后成交快照；0 条告警属正常"),
    }
    bounds = {
        "minutes": minutes,
        "rounds_requested": max_rounds,
        "interval_s": interval,
        "rounds_done": len(rounds),
        "duration_s": round(durations, 2),
        "deadline_hit": bool(flags.get("deadline_hit")),
        "interrupted": bool(flags.get("interrupted")),
        "dashboard_url": base_url,
        "routes_probed": routes,
        "routes_missing": missing,
    }
    report = build_report(
        started_at=started_at,
        finished_at=datetime.now().isoformat(timespec="seconds"),
        duration_s=durations,
        bounds=bounds,
        session_info=session_info,
        config_snapshot=_collect_config_snapshot(st, engine, watch),
        metrics=metrics,
        verdict=verdict,
        notes=notes,
    )

    report_path = PROJECT_ROOT / "data" / (
        "live_session_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".json")
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        wrote = True
    except Exception as exc:                         # noqa: BLE001 —— 写不了报告也要给结论
        wrote = False
        print(f"[!] 报告写入失败：{type(exc).__name__}: {exc}")

    _print_summary(metrics, verdict, bounds, report_path if wrote else None)
    return int(verdict["exit_code"])


def _print_summary(metrics: dict, verdict: dict, bounds: dict,
                   report_path: Path | None) -> None:
    """打印结论表。人只看这一段，所以关键数字必须都在这里。"""
    lat = metrics.get("latency_ms") or {}
    mem = metrics.get("memory") or {}
    api = metrics.get("api") or {}
    sse = metrics.get("sse") or {}
    logs = metrics.get("logs") or {}
    quotes = metrics.get("quotes") or {}
    universe = metrics.get("universe") or {}

    def _ms(key: str) -> str:
        v = lat.get(key)
        return "—" if v is None else f"{float(v):.0f}ms"

    print()
    print("=" * 66)
    print(" 观测结果")
    print("=" * 66)
    print(f"  轮次 {metrics.get('rounds')}（请求 {bounds.get('rounds_requested') or '—'}，"
          f"用时 {bounds.get('duration_s')}s，间隔 {bounds.get('interval_s')}s）")
    print(f"  单轮耗时 min/median/p95/max = {_ms('min')} / {_ms('median')} / "
          f"{_ms('p95')} / {_ms('max')}")
    print(f"  抓取失败 {metrics.get('error_rounds')} 轮，"
          f"空数据 {metrics.get('no_data_rounds')} 轮")
    print(f"  告警 {metrics.get('alerts_total')} 条："
          f"{metrics.get('alerts_by_kind') or '{}'}")
    print(f"  股票池 {universe.get('first')} -> {universe.get('last')}"
          f"（min {universe.get('min')} / max {universe.get('max')}），"
          f"行情 {quotes.get('first')} -> {quotes.get('last')}，"
          f"自选股降级 {metrics.get('watchlist_only_rounds')} 轮")
    print(f"  内存代理：quotes {mem.get('quotes_first')} -> {mem.get('quotes_last')}，"
          f"历史点 {mem.get('history_points_first')} -> {mem.get('history_points_last')}"
          f"（单只上限 {mem.get('history_maxlen')}）")
    print(f"  看板接口：请求 {api.get('requests')}，非 200 {api.get('non_200')}，"
          f"JSON 坏 {api.get('malformed')}，连不上 {api.get('unreachable')}")
    print(f"  SSE：连接 {'是' if sse.get('connected') else '否'}，"
          f"事件 {sse.get('events_total')} 条 {sse.get('events_by_type') or '{}'}，"
          f"断流 {'是' if sse.get('stalled') else '否'}")
    print(f"  日志：{logs}")

    print()
    print("=" * 66)
    print(" 结论")
    print("=" * 66)
    print(_fmt_table(verdict.get("checks") or []))
    print()
    if verdict.get("healthy"):
        print(f"✓ 健康（exit {verdict.get('exit_code')}）")
    else:
        print(f"✗ 不健康（exit {verdict.get('exit_code')}）"
              f"  —— 失败项："
              f"{[c['name'] for c in verdict.get('checks') or [] if not c.get('ok')]}")
    if report_path is not None:
        print(f"报告：{report_path}")
    else:
        print("报告：未写出（见上方错误）")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="真实盘中 soak 测试：真引擎跑真行情 N 分钟，输出诚实结论。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--minutes", type=float, default=DEFAULT_MINUTES,
                    help="浸泡时长（分钟）；始终作为兜底上限生效")
    ap.add_argument("--rounds", type=int, default=0,
                    help="按轮数设边界（>0 时优先于 --minutes，但仍受 --minutes 兜底）")
    ap.add_argument("--port", type=int, default=0,
                    help="看板端口；0 = 内核分配空闲端口")
    ap.add_argument("--browser", action="store_true",
                    help="额外用 Playwright 打开看板，收集 pageerror/console.error")
    ap.add_argument("--verbose", action="store_true",
                    help="打印引擎 INFO 日志与每轮告警明细")
    args = ap.parse_args(list(argv) if argv is not None else None)
    if args.minutes <= 0 and not args.rounds:
        ap.error("--minutes 必须 > 0，或者用 --rounds 指定轮数")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return run(args)


if __name__ == "__main__":                           # pragma: no cover - 入口
    raise SystemExit(main())
