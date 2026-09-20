"""东方财富 push2 数据源 —— 股票池主力(universe) + 备用批量快照(ulist)。

契约：``docs/DATA_CONTRACT.md`` 第 2.2 节。

实测要点（2026-09-15 验证）::

    GET https://push2.eastmoney.com/api/qt/clist/get   -> UTF-8 JSON
        total=5913 ；``pz`` 被服务端**硬限制为 100**（给 1000 仍只回 100 行）
        -> 必须翻页 pn=1..60，8 线程并发约 1.42s
    GET https://push2.eastmoney.com/api/qt/ulist.np/get
        secids=1.600000,0.000001 ；**每批最多 50 码**（800 码 URL 过长 -> HTTP 502）

单位换算（契约第 0 节：市值一律「亿元」）::

    f20(总市值,元) / 1e8 -> total_cap(亿)
    f21(流通市值,元) / 1e8 -> float_cap(亿)
    f5 已是「手」，f6 已是「元」，均不再换算

只用标准库；import 时不做任何 I/O。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping

from ..models import Quote, board_of, guess_prefix
from .base import SourceError

__all__ = [
    "EastmoneySource",
    "parse_clist",
    "parse_ulist",
    "secid_of",
    "CLIST_URL",
    "ULIST_URL",
    "FS_ALL",
    "CLIST_FIELDS",
    "ULIST_FIELDS",
]

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
ULIST_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"

# 沪深京全市场：深主板+创业板 / 沪主板 / 沪科创 / 北交所
FS_ALL = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
CLIST_FIELDS = "f2,f3,f5,f6,f8,f10,f11,f12,f13,f14,f15,f16,f17,f18,f20,f21,f22,f26,f124"
ULIST_FIELDS = "f2,f3,f5,f6,f8,f12,f14,f15,f16,f17,f18,f20,f21,f22,f124"
UT = "bd1d9ddb04089700cf9c27f6f7426281"

#: 服务端把 clist 的 pz 硬限制在 100，请求再大也只回 100 行
PAGE_LIMIT = 100
#: ulist.np 单次请求的 secids 上限（800 码会 502）
BULK_LIMIT = 50

#: 重试退避（秒），与契约 2.1 的 0.3/0.9/2.0 一致
BACKOFF: tuple[float, ...] = (0.3, 0.9, 2.0)

CAP_YI = 1e8  # 元 -> 亿元
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REFERER = "https://quote.eastmoney.com/"

#: 无效数值占位（"-" 等）→ 该条行情视为无效
_NULL_TOKENS = frozenset({"", "-", "--", "---", "null", "none", "nan"})

Fetcher = Callable[[str, "dict[str, str]", float], bytes]


# --------------------------------------------------------------------------
# 纯函数解析（可离线单测）
# --------------------------------------------------------------------------
def _as_dict(payload: str | bytes | Mapping[str, Any] | None) -> dict[str, Any]:
    """str/bytes/dict -> dict；不可解析时返回 {}。"""
    if payload is None:
        return {}
    if isinstance(payload, Mapping):
        return dict(payload)
    if isinstance(payload, (bytes, bytearray)):
        payload = bytes(payload).decode("utf-8", "replace")
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return {}
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            return {}
        return dict(obj) if isinstance(obj, Mapping) else {}
    return {}


def _num(value: Any, default: float | None = None) -> float | None:
    """宽松数值转换；``"-"``/``None``/空串/非法 -> default。"""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "")
    if s.lower() in _NULL_TOKENS:
        return default
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def _pos(value: Any, default: float) -> float:
    """取正数，否则用 default。"""
    v = _num(value)
    return v if (v is not None and v > 0) else default


def _norm_code(value: Any) -> str | None:
    """``301132`` / ``"301132"`` / ``"sz301132"`` -> ``"301132"``。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    s = str(value).strip().upper()
    if s[:2] in ("SH", "SZ", "BJ"):
        s = s[2:]
    if not s.isdigit() or len(s) > 6:
        return None
    s = s.zfill(6)
    return s


def _ts_of(value: Any) -> datetime | None:
    """f124 = 行情时间戳（秒）-> naive 北京时间 datetime；失败 None。"""
    ts = _num(value)
    if ts is None or ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts)
    except (OSError, OverflowError, ValueError):
        return None


def _list_date_of(value: Any) -> str:
    """f26 = 上市日期 ``YYYYMMDD`` 整数 -> 字符串 ``"YYYYMMDD"``。

    解析失败返回 ``""``（``models.Quote.list_date`` 的约定是「缺失为空串」，
    ``arad.filters`` 见到空串会放行而不是误杀，见 docs/NOTES_eastmoney_sina.md）。
    """
    n = _num(value)
    if n is None or n <= 0:
        return ""
    s = str(int(n))
    if len(s) != 8 or not s.isdigit():
        return ""
    try:
        datetime.strptime(s, "%Y%m%d")
    except ValueError:
        return ""
    return s


def _rows_of(obj: Mapping[str, Any]) -> list[Any]:
    """取出 data.diff（list 或 dict）；data/diff 为 null -> []。"""
    data = obj.get("data")
    if not isinstance(data, Mapping):
        return []
    rows = data.get("diff")
    if isinstance(rows, Mapping):
        # 个别接口用 {"0": {...}} 形式返回
        rows = list(rows.values())
    if not isinstance(rows, Iterable) or isinstance(rows, (str, bytes)):
        return []
    return list(rows)


def _quote_of(row: Any, seq: int) -> Quote | None:
    """东财单行 -> Quote；无效行（缺代码/缺现价/缺昨收/"-" 占位）返回 None。"""
    if not isinstance(row, Mapping):
        return None
    code = _norm_code(row.get("f12"))
    if code is None:
        return None
    name = str(row.get("f14") or "").strip()

    price = _num(row.get("f2"))
    prev_close = _num(row.get("f18"))
    # 契约 2.2：f2/f18 可能是 "-" 字符串 -> 该条无效，**丢弃**（不置 0）
    if price is None or prev_close is None or price <= 0 or prev_close <= 0:
        return None

    open_ = _pos(row.get("f17"), prev_close)
    high = _pos(row.get("f15"), max(price, open_))
    low = _pos(row.get("f16"), min(price, open_))

    return Quote(
        code=code,
        name=name,
        board=board_of(code, name),
        price=price,
        prev_close=prev_close,
        open=open_,
        high=high,
        low=low,
        volume_lots=_num(row.get("f5"), 0.0) or 0.0,   # 手
        amount=_num(row.get("f6"), 0.0) or 0.0,        # 元
        turnover=_num(row.get("f8"), 0.0) or 0.0,      # %
        volume_ratio=_num(row.get("f10"), 0.0) or 0.0,  # 量比
        # 东财 clist 无委买委卖价；f11 是「5分钟涨跌幅」，不是买一价
        bid1=0.0,
        ask1=0.0,
        bid_vol=0.0,
        ask_vol=0.0,
        float_cap=(_num(row.get("f21"), 0.0) or 0.0) / CAP_YI,  # 元 -> 亿元
        total_cap=(_num(row.get("f20"), 0.0) or 0.0) / CAP_YI,
        # 东财不提供涨跌停价：保持 0.0，由 Quote.limit_up_price 按板块费率推算
        limit_up=0.0,
        limit_down=0.0,
        ts=_ts_of(row.get("f124")),
        seq=seq,
        # f26=上市日期 YYYYMMDD：arad.filters 用它过滤次新股，缺失为空串
        list_date=_list_date_of(row.get("f26")),
    )


def parse_clist(payload: str | dict, seq: int = 0) -> list[Quote]:
    """解析 clist（全市场股票池）响应；纯函数，无 I/O。

    ``data`` 为 null 或 ``data.diff`` 为 null -> 返回 ``[]``（该页无数据，不算失败）。
    字段映射见契约 2.2；f2/f18 为 ``"-"`` 的行被丢弃。
    """
    obj = _as_dict(payload)
    if not obj:
        return []
    out: list[Quote] = []
    for row in _rows_of(obj):
        q = _quote_of(row, seq)
        if q is not None:
            out.append(q)
    return out


def parse_ulist(payload: str | dict, seq: int = 0) -> list[Quote]:
    """解析 ulist.np（批量快照）响应；字段与 clist 同源，规则一致。"""
    return parse_clist(payload, seq)


def secid_of(code: str) -> str:
    """6 位代码 -> 东财 secid（沪=1.，深/北=0.）。"""
    c = _norm_code(code) or ""
    return ("1." if guess_prefix(c) == "sh" else "0.") + c


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def default_fetcher(url: str, headers: dict[str, str], timeout: float) -> bytes:
    """标准库 GET；测试里通过 ``fetcher`` 注入替身。"""
    req = urllib.request.Request(url, headers=dict(headers or {}), method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


# --------------------------------------------------------------------------
# Source
# --------------------------------------------------------------------------
class EastmoneySource:
    """东财数据源：``universe()`` 拉全市场，``snapshots()`` 拉指定代码。

    参数::

        cfg = {"page_size": 100, "max_pages": 80, "workers": 8,
               "timeout": 15, "retries": 3, "bulk_chunk": 50}
    """

    name = "eastmoney"

    def __init__(self, cfg: dict[str, Any] | None = None, fetcher: Fetcher | None = None):
        c = dict(cfg or {})
        # 实测：pz 被服务端硬限制为 100，配置更大也压回 100
        self.page_size = max(1, min(int(c.get("page_size", PAGE_LIMIT) or PAGE_LIMIT), PAGE_LIMIT))
        self.max_pages = max(1, int(c.get("max_pages", 80) or 80))
        self.workers = max(1, int(c.get("workers", 8) or 8))
        self.timeout = float(c.get("timeout", 15) or 15)
        self.retries = max(1, int(c.get("retries", 3) or 3))
        # 契约 2.2：ulist 每批最多 50 码
        self.bulk_chunk = max(1, min(int(c.get("bulk_chunk", BULK_LIMIT) or BULK_LIMIT), BULK_LIMIT))
        self.referer = str(c.get("referer") or REFERER)

        self._fetcher: Fetcher = fetcher or default_fetcher
        self._lock = threading.Lock()
        self._seq = 0
        self._stats: dict[str, Any] = {
            "calls": 0,        # 成功请求数
            "errors": 0,       # 失败尝试数（含重试）
            "retries": 0,      # 重试次数
            "pages_failed": 0,  # 最终失败的分页/分批数
            "quotes": 0,       # 最近一次返回的行情条数
            "last_ms": 0,
            "last_err": "",
        }
        #: 最近一次 ``universe()`` 的完整性元数据（IT-P1-006）。
        self._last_universe: dict[str, Any] = {}

    # --- 对外接口（Source 协议） ---------------------------------------
    def universe(self) -> list[Quote]:
        """全市场股票池快照（翻页 + 并发）。

        完整性（IT-P1-006）：``total`` 可用时，期望页数由总数算出，
        ``pages_failed`` 为空**且未被 ``max_pages`` 截断**才算完整；
        ``total`` 不可用（``data/diff`` 为 null）时只能顺序探测，遇空页即停，
        此时**无法证明**拿全了，故标为不完整。

        截断（IT-P1-006-R1）：``required_pages > max_pages`` 时只取前
        ``max_pages`` 页，此时**必须**报 ``complete=False`` —— 否则 300 只的
        部分池会被当成全市场 5913 只。截断不抛异常（调用方依赖部分数据继续
        跑），但事实通过 ``truncated`` / ``required_pages`` / ``max_pages``
        显式暴露在 ``universe_info()`` 里。

        行数缩水（IT-P1-COMPLETE-001）：仅看页数**不够** —— 页数够但每页内容
        缩水（服务端限流/降级、边界重叠去重）时，拿到的 ``len(out)`` 会远低于
        ``expected_total``。故 ``complete`` 还要求 ``len(out) >= expected_total``。
        判据是「不少于」而非「等于」：翻页边界重叠会让 ``len(out)`` 略高于
        ``expected_total``（实测 5913 只 -> 6000 行），这是**正常**的上界重叠，
        不能因此判 incomplete。
        """
        seq = self._next_seq()
        failed: list[int] = []
        pages_requested = 0
        expected_total = 0
        required_pages = 0    # 按总数算出的**需要**页数（截断前）
        truncated = False     # required_pages > max_pages
        reason = ""
        first = self._request_json(self._clist_url(1))
        quotes = parse_clist(first, seq)
        total = self._total_of(first)
        if total > 0:
            expected_total = total
            required_pages = -(-total // self.page_size)
            truncated = required_pages > self.max_pages
            pages = min(self.max_pages, required_pages)
            pages_requested = pages
            if pages > 1:
                more, failed = self._fetch_pages(range(2, pages + 1), seq)
                quotes.extend(more)
            if truncated:
                reason = (f"翻页被 max_pages={self.max_pages} 截断："
                          f"总数 {expected_total} 只需 {required_pages} 页，"
                          f"仅请求了前 {pages} 页")
                if failed:
                    reason += f"；另有 {len(failed)} 页失败 {failed[:5]}"
            elif failed:
                reason = f"第 {failed[0]} 页起失败，共 {len(failed)} 页"
        else:
            # total 不可用（data/diff 为 null）：顺序探测，遇空页即停，避免空转 80 页
            for pn in range(2, self.max_pages + 1):
                rows = parse_clist(self._request_json(self._clist_url(pn)), seq)
                if not rows:
                    break
                quotes.extend(rows)
                pages_requested = pn
            else:
                pages_requested = self.max_pages
            # 没有 total 就没有「应该有多少只」的基准，无法证明完整。
            reason = "接口未返回总数，只能顺序探测，无法确认是否翻完"
        out = _dedupe(quotes)
        # IT-P1-COMPLETE-001：页数够 **不代表** 行数够。服务端限流/降级时每页
        # 只回几行，或边界大量重叠被去重后，len(out) 会远低于 expected_total，
        # 而此前 complete 只看「有总数 + 无失败页 + 未截断」，于是 600 行
        # （全市场的 10.1%）被当成完整全市场采用（engine.py 的 complete 门控）。
        #
        # 判据用「不少于」而非「等于」：翻页边界重叠会让 len(out) 略高于
        # expected_total（实测 total=5913、60 页 x 100 行 -> 6000 行），这是
        # **正常的上界重叠**，绝不能因此判 incomplete。
        shortfall = 0
        if expected_total > 0 and len(out) < expected_total:
            shortfall = expected_total - len(out)
            cover = len(out) / expected_total
            shrink = (f"行数缩水：实际只拿到 {len(out)} 行 < 总数 {expected_total} "
                      f"（缺 {shortfall} 行，覆盖 {cover:.1%}）")
            reason = f"{reason}；{shrink}" if reason else shrink
        err = f"clist {len(failed)} 页失败: {failed[:5]}" if failed else ""
        # IT-P1-006-R1：截断 (truncated) 与失败页一样，都不能算完整。
        # 没有 total 时 required_pages=0/truncated=False/expected_total=0，
        # shortfall 恒为 0，complete 仍由 bool(total > 0) 决定为 False
        # （没有基准即无法证明完整），既有语义不变。
        complete = (bool(total > 0) and not failed and not truncated
                    and shortfall == 0)
        with self._lock:
            self._last_universe = {
                "complete": complete,
                "truncated": truncated,
                "pages_failed": len(failed),
                "pages_ok": max(pages_requested - len(failed), 0),
                "pages_requested": pages_requested,
                "required_pages": required_pages,
                "max_pages": self.max_pages,
                "expected_total": expected_total,
                "returned": len(out),
                # 少于 expected_total 的行数（0 = 未缩水）；> 0 时 complete 恒为 False
                "shortfall": shortfall,
                "reason": reason or "已按总数翻完",
            }
        self._finish(err, out)
        return out

    def universe_info(self) -> dict:
        """最近一次 ``universe()`` 的完整性元数据（IT-P1-006）。"""
        with self._lock:
            return dict(getattr(self, "_last_universe", {}) or {})

    def snapshots(self, codes: list[str]) -> list[Quote]:
        """指定 6 位代码的最新快照（每批 ≤50 码，并发）。批次失败即抛 SourceError。"""
        wanted = _norm_codes(codes)
        if not wanted:
            return []
        seq = self._next_seq()
        batches = [wanted[i:i + self.bulk_chunk] for i in range(0, len(wanted), self.bulk_chunk)]
        quotes: list[Quote] = []
        errors: list[SourceError] = []
        if len(batches) == 1:
            quotes = self._fetch_ulist(batches[0], seq)
        else:
            with ThreadPoolExecutor(max_workers=min(self.workers, len(batches))) as ex:
                futs = {ex.submit(self._fetch_ulist, b, seq): b for b in batches}
                for fut in as_completed(futs):
                    try:
                        quotes.extend(fut.result())
                    except SourceError as exc:
                        errors.append(exc)
        if errors:
            # 目标是「这批指定代码」：缺任何一批都视为失败，交由引擎故障转移
            with self._lock:
                self._stats["pages_failed"] += len(errors)
            self._finish(f"ulist {len(errors)}/{len(batches)} 批失败: {errors[0]}")
            raise SourceError(f"eastmoney ulist {len(errors)}/{len(batches)} 批失败: {errors[0]}")
        # 只返回被请求的代码（服务端可能回带未请求的行）
        wanted_set = set(wanted)
        out = _dedupe(q for q in quotes if q.code in wanted_set)
        self._finish("", out)
        return out

    def health(self) -> dict:
        """{'name','ok','latency_ms','err'} —— 契约第 1 节格式。"""
        with self._lock:
            s = dict(self._stats)
        err = str(s.get("last_err") or "")
        return {
            "name": self.name,
            "ok": not err,
            "latency_ms": int(s.get("last_ms") or 0),
            "err": err,
        }

    def stats(self) -> dict:
        """统计快照（调试/看板用，非契约字段）。"""
        with self._lock:
            return dict(self._stats)

    # --- 统计 -----------------------------------------------------------
    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def _finish(self, err: str, quotes: list[Quote] | None = None) -> None:
        """一次 ``universe``/``snapshots`` 收尾：刷新条数统计与错误标记。

        ``last_err`` 只在**本次调用**有失败时保留，不会被后续成功页清掉 ——
        否则「少数页丢失」会被 health() 报成完全健康，故障转移永不触发。
        """
        with self._lock:
            self._stats["last_err"] = (err or "")[:200]
            if quotes is not None:
                self._stats["quotes"] = len(quotes)

    # --- URL ------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": self.referer,
        }

    def _clist_url(self, pn: int) -> str:
        q = urllib.parse.urlencode(
            {
                "pn": pn,
                "pz": self.page_size,
                "po": 0,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "fid": "f12",
                "fs": FS_ALL,
                "fields": CLIST_FIELDS,
                "ut": UT,
            }
        )
        return f"{CLIST_URL}?{q}"

    def _ulist_url(self, codes: list[str]) -> str:
        q = urllib.parse.urlencode(
            {
                "fltt": 2,
                "invt": 2,
                "secids": ",".join(secid_of(c) for c in codes),
                "fields": ULIST_FIELDS,
                "ut": UT,
            }
        )
        return f"{ULIST_URL}?{q}"

    # --- 请求 -----------------------------------------------------------
    def _request(self, url: str) -> bytes:
        """带重试的 GET；重试耗尽抛 SourceError。"""
        last_err: Exception | None = None
        for attempt in range(self.retries):
            t0 = time.perf_counter()
            try:
                body = self._fetcher(url, self._headers(), self.timeout)
            except Exception as exc:  # noqa: BLE001 - 注入的 fetcher 可能抛任何异常
                last_err = exc
                with self._lock:
                    self._stats["errors"] += 1
                    self._stats["last_err"] = f"{type(exc).__name__}: {exc}"[:200]
            else:
                if isinstance(body, str):  # 注入的替身可能直接给文本
                    body = body.encode("utf-8")
                if body:
                    with self._lock:
                        self._stats["calls"] += 1
                        self._stats["last_ms"] = int((time.perf_counter() - t0) * 1000)
                        self._stats["last_err"] = ""
                    return bytes(body)
                last_err = SourceError("空响应")
                with self._lock:
                    self._stats["errors"] += 1
                    self._stats["last_err"] = "空响应"
            if attempt + 1 < self.retries:
                with self._lock:
                    self._stats["retries"] += 1
                time.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
        raise SourceError(
            f"eastmoney 请求失败（{self.retries} 次尝试后）: "
            f"{type(last_err).__name__}: {last_err}"
        )

    def _request_json(self, url: str) -> dict[str, Any]:
        body = self._request(url)
        try:
            obj = json.loads(body.decode("utf-8", "replace"))
        except (ValueError, TypeError) as exc:
            raise SourceError(f"eastmoney 响应非 JSON: {exc}") from exc
        if not isinstance(obj, Mapping):
            raise SourceError("eastmoney 响应结构异常（非对象）")
        return dict(obj)

    def _total_of(self, payload: Mapping[str, Any]) -> int:
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return 0
        total = _num(data.get("total"), 0.0)
        return int(total) if total and total > 0 else 0

    # --- 分页 / 分批 -----------------------------------------------------
    def _fetch_one_page(self, pn: int, seq: int) -> list[Quote]:
        return parse_clist(self._request_json(self._clist_url(pn)), seq)

    def _fetch_pages(self, pns: Iterable[int], seq: int) -> tuple[list[Quote], list[int]]:
        """并发抓多页；返回 (行情, 失败页号列表)。

        单页失败不抛异常 —— 全市场 5913 只里丢一页（100 只）仍可用，
        整体崩掉反而会让引擎无谓地故障转移。失败页号由调用方记入 health。
        """
        pages = list(pns)
        if not pages:
            return [], []
        out: list[Quote] = []
        failed: list[int] = []
        with ThreadPoolExecutor(max_workers=min(self.workers, len(pages))) as ex:
            futs = {ex.submit(self._fetch_one_page, pn, seq): pn for pn in pages}
            for fut in as_completed(futs):
                pn = futs[fut]
                try:
                    out.extend(fut.result())
                except SourceError as exc:
                    failed.append(pn)
                    with self._lock:
                        self._stats["last_err"] = f"page {pn} 失败: {exc}"[:200]
        if failed:
            failed.sort()
            with self._lock:
                self._stats["pages_failed"] += len(failed)
            if len(failed) == len(pages):
                raise SourceError(f"eastmoney clist 全部 {len(pages)} 页失败")
        return out, failed

    def _fetch_ulist(self, codes: list[str], seq: int) -> list[Quote]:
        return parse_ulist(self._request_json(self._ulist_url(codes)), seq)


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _norm_codes(codes: Iterable[str]) -> list[str]:
    """去重、保序、过滤非法代码。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in codes or []:
        c = _norm_code(raw)
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _dedupe(quotes: Iterable[Quote]) -> list[Quote]:
    """按代码去重（保序，先到先得）。"""
    out: list[Quote] = []
    seen: set[str] = set()
    for q in quotes:
        if q.code not in seen:
            seen.add(q.code)
            out.append(q)
    return out
