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
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, NamedTuple

from ..models import Quote, board_of, guess_prefix
from .base import SourceError

if TYPE_CHECKING:                                      # pragma: no cover
    from .outcome import SnapshotFetchResult

__all__ = [
    "EastmoneySource",
    "ClistPage",
    "parse_clist",
    "parse_clist_page",
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

    只要可用 Quote，不要传输账本时用它；需要**双账**（IT-P1-COMPLETE-001-R1）
    请用 ``parse_clist_page()`` —— 本函数是它的 ``.quotes`` 投影。
    """
    return list(parse_clist_page(payload, seq).quotes)


class ClistPage(NamedTuple):
    """单页 clist 的**双账**（IT-P1-COMPLETE-001-R1）—— 传输层与解析层分账。

    为什么必须分开：``data.total`` 是 **transport/universe 口径**（含停牌、
    含字段缺失的原始行），而 ``_quote_of`` 会按契约丢弃 ``f2/f18`` 为 ``"-"``
    或 ``<= 0`` 的行。把两者塞进一个布尔，就会把「市场里有停牌股」误判成
    「服务端少给了数据」。

    字段::

        quotes          通过 parser 质量校验的 Quote（本页、未去重）
        raw_rows        ``data.diff`` 的原始行数（传输层收到多少行）
        raw_code_rows   其中能规范化出 6 位代码的行数
        codes           能规范化出的代码（保序，含重复；供 unique 统计）
        total           API 声称的总数（``data.total``；无则 0）
    """

    quotes: list[Quote]
    raw_rows: int
    raw_code_rows: int
    codes: tuple[str, ...]
    total: int

    @property
    def dropped_invalid(self) -> int:
        """被 parser 丢弃的行数（含无代码行与价格无效行）。"""
        return self.raw_rows - len(self.quotes)


def parse_clist_page(payload: str | dict, seq: int = 0) -> ClistPage:
    """解析 clist 单页并**同时**产出传输账本与可用 Quote（纯函数，无 I/O）。

    与 ``parse_clist`` 的差别只在返回值：这里额外报告 ``raw_rows`` /
    ``raw_code_rows`` / ``codes`` / ``total``，让 ``universe()`` 能把
    「传输完整性」与「解析可用率」分别记账。
    """
    obj = _as_dict(payload)
    if not obj:
        return ClistPage(quotes=[], raw_rows=0, raw_code_rows=0, codes=(), total=0)
    quotes: list[Quote] = []
    codes: list[str] = []
    raw_rows = 0
    for row in _rows_of(obj):
        raw_rows += 1
        code = _norm_code(row.get("f12")) if isinstance(row, Mapping) else None
        if code is not None:
            codes.append(code)
        q = _quote_of(row, seq)
        if q is not None:
            quotes.append(q)
    data = obj.get("data")
    total = 0
    if isinstance(data, Mapping):
        num = _num(data.get("total"), 0.0)
        total = int(num) if num and num > 0 else 0
    return ClistPage(quotes=quotes, raw_rows=raw_rows,
                     raw_code_rows=len(codes), codes=tuple(codes), total=total)


def parse_ulist(payload: str | dict, seq: int = 0) -> list[Quote]:
    """解析 ulist.np（批量快照）响应；字段与 clist 同源，规则一致。"""
    return parse_clist(payload, seq)


def parse_ulist_detailed(payload: str | dict, seq: int = 0,
                         requested: Iterable[str] | None = None,
                         route: str = "stocks") -> "SnapshotFetchResult":
    """``parse_ulist`` 的**精确 raw-presence** 版本（Snapshot Outcome v4）。

    `IT-P1-SNAPSHOT-OUTCOME-SOURCE-SEMANTICS-DRIFT-001`（12:37 §2）：
    东财的 ``_quote_of``（``:196-200``）在 ``f2`` / ``f18`` 为 ``"-"``
    或 ``<= 0`` 时 ``return None``，而 ``parse_clist_page`` 已经用
    ``:192 code = _norm_code(row.get("f12"))`` 拿到了身份。
    旧路径 ``parse_ulist`` 直接扔掉 ``codes``，于是这一行
    在下游变成 ``unknown_missing`` —— 而**同一事实**在腾讯上是
    ``rejected_quality``。

    任务书明确：东财 raw-f12-present / ``_quote_of``→None
    **必须落 quality，不得落 missing**。

    复用既有的双账 ``ClistPage``（``:248``）拿 raw codes，
    **不重复实现行循环**（本仓库反复出现的 bug 类"同一语义实现两次"）。

    ⚠ **键轴**：与 ``Quote.code`` 同轴 —— 东财的 ``_quote_of`` 用
    ``_norm_code`` 产出**裸 6 位码**，``parse_clist_page.codes`` 同样用
    ``_norm_code``，因此请求集也必须走 ``_norm_code`` 归一到裸码。
    """
    from .outcome import PROVENANCE_EXACT, build_outcome, normalize_request

    req = normalize_request(requested or (), normalize=_norm_code)
    page = parse_clist_page(payload, seq)
    return build_outcome(
        route=route,
        source=EastmoneySource.name,
        normalized_request=req,
        raw_keys=list(page.codes),
        quotes=list(page.quotes),
        raw_rows=list(range(page.raw_rows)),   # 原始行数（内容不透明，保留计数）
        raw_presence_known=True,
        provenance=PROVENANCE_EXACT,
    )


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

        完整性在这里是**两把独立的尺子**（IT-P1-COMPLETE-001-R1）：

        * **传输轴** ``transport_complete``：服务端有没有把池子给全。
          判据 = 有总数 + 无失败页 + 未被 ``max_pages`` 截断 +
          ``raw_unique_codes >= transport_expected_total``。
          只数**传输层收到的原始行/代码**，与 parser 丢不丢停牌行无关。
        * **可用轴** ``usable_coverage``：解析后真正能用的比例，
          纯诊断，**不参与** ``transport_complete``。

        为什么必须分开：``data.total`` 是 transport/universe 口径（含停牌股），
        而契约 2.2 明文要求 parser 丢弃 ``f2/f18`` 为 ``"-"`` 或 ``<= 0`` 的行。
        拿 ``len(usable_quotes)`` 去比 ``total``，等于把「市场里有停牌股」判成
        「服务端少给了数据」—— 实测 A 股停牌率约 6.06%（docs/NOTES_tencent.md：
        5908 只里 358 只停牌），于是每次刷新都假报 incomplete，``engine.py`` 的
        ``complete`` 门控拒绝覆盖现有池，**新上市代码永远进不来**
        （IT-P1-COMPLETE-001-R1，18:22 冻结树实测 0/10）。

        兼容：``meta["complete"] = meta["transport_complete"]``，既有
        ``engine.py`` 的 ``meta.get("complete", True)`` 语义继续成立。

        截断（IT-P1-006-R1）与失败页（IT-P1-006）语义不变，仍令 False。
        ``total`` 不可用（``data/diff`` 为 null）时只能顺序探测，没有基准就
        **无法证明**拿全了，也报 False。

        注意 ``usable_coverage`` 可能极低（极端时全市场停牌 -> 0.0）而
        ``transport_complete`` 仍为 True：传输确实完整，只是没有可用行情。
        ``engine.refresh_universe`` 对空 ``quotes`` 另有 ``if not quotes``
        保护，不会把空池写进股票池。
        """
        seq = self._next_seq()
        failed: list[int] = []
        pages_requested = 0
        expected_total = 0
        required_pages = 0    # 按总数算出的**需要**页数（截断前）
        truncated = False     # required_pages > max_pages
        reason = ""
        pages: list[ClistPage] = []
        first = parse_clist_page(self._request_json(self._clist_url(1)), seq)
        pages.append(first)
        total = first.total
        if total > 0:
            expected_total = total
            required_pages = -(-total // self.page_size)
            truncated = required_pages > self.max_pages
            pages_requested = min(self.max_pages, required_pages)
            if pages_requested > 1:
                more, failed = self._scan_pages(range(2, pages_requested + 1), seq)
                pages.extend(more)
            if truncated:
                reason = (f"翻页被 max_pages={self.max_pages} 截断："
                          f"总数 {expected_total} 只需 {required_pages} 页，"
                          f"仅请求了前 {pages_requested} 页")
                if failed:
                    reason += f"；另有 {len(failed)} 页失败 {failed[:5]}"
            elif failed:
                reason = f"第 {failed[0]} 页起失败，共 {len(failed)} 页"
        else:
            # total 不可用（data/diff 为 null）：顺序探测，遇空页即停，避免空转 80 页。
            # 「空页」判据沿用**可用行情为空**（而不是 raw 行为空），与修复前一致。
            for pn in range(2, self.max_pages + 1):
                page = parse_clist_page(self._request_json(self._clist_url(pn)), seq)
                if not page.quotes:
                    break
                pages.append(page)
                pages_requested = pn
            else:
                pages_requested = self.max_pages
            # 没有 total 就没有「应该有多少只」的基准，无法证明完整。
            reason = "接口未返回总数，只能顺序探测，无法确认是否翻完"

        # ---- 双账：传输层原始行 与 解析后可用行情 分开数 ------------------
        raw_rows = sum(p.raw_rows for p in pages)
        raw_code_rows = sum(p.raw_code_rows for p in pages)
        raw_unique_codes = len({c for p in pages for c in p.codes})
        duplicate_codes = raw_code_rows - raw_unique_codes
        parsed: list[Quote] = [q for p in pages for q in p.quotes]
        out = _dedupe(parsed)
        usable_quotes = len(out)
        dropped_invalid = raw_rows - len(parsed)

        # shortfall 是**传输轴**缺口：原始唯一代码没覆盖住服务端声明的总数。
        # 停牌行有代码、只是价格无效 —— 它计入 raw_unique_codes，**不产生**
        # shortfall（这正是本缺陷的修复点）。判据仍是「不少于」而非「等于」：
        # 翻页边界重叠会让 raw_unique_codes 略高于 expected_total（实测
        # total=5913 -> 6020），那是**正常的上界重叠**，不能判 incomplete。
        shortfall = 0
        if expected_total > 0 and raw_unique_codes < expected_total:
            shortfall = expected_total - raw_unique_codes
            cover = raw_unique_codes / expected_total
            shrink = (f"行数缩水：传输层只有 {raw_unique_codes} 个唯一代码"
                      f"（{raw_rows} 行，去重后 {usable_quotes} 条可用）"
                      f" < 总数 {expected_total}（缺 {shortfall}，覆盖 {cover:.1%}）")
            reason = f"{reason}；{shrink}" if reason else shrink
        err = f"clist {len(failed)} 页失败: {failed[:5]}" if failed else ""
        # IT-P1-006-R1：截断 (truncated) 与失败页一样，都不能算完整。
        # 没有 total 时 required_pages=0/truncated=False/expected_total=0，
        # shortfall 恒为 0，transport_complete 仍由 bool(total > 0) 决定为
        # False（没有基准即无法证明完整），既有语义不变。
        transport_complete = (bool(total > 0) and not failed and not truncated
                              and shortfall == 0)
        # 可用率只是**诊断**：不参与 transport_complete，否则又回到旧缺陷。
        usable_coverage = (usable_quotes / expected_total) if expected_total > 0 else 0.0
        with self._lock:
            self._last_universe = {
                # 兼容字段：既有调用方（engine.py 的 complete 门控）继续可用
                "complete": transport_complete,
                "transport_complete": transport_complete,
                "truncated": truncated,
                "pages_failed": len(failed),
                "pages_ok": max(pages_requested - len(failed), 0),
                "pages_requested": pages_requested,
                "required_pages": required_pages,
                "max_pages": self.max_pages,
                "expected_total": expected_total,
                "transport_expected_total": expected_total,
                "returned": usable_quotes,
                # 传输轴缺口（0 = 原始代码已覆盖总数）；> 0 时 transport_complete 恒 False
                "shortfall": shortfall,
                "reason": reason or "已按总数翻完",
                # ---- 双账明细 ----
                "raw_rows": raw_rows,                    # 收到的原始行数（去重前）
                "raw_code_rows": raw_code_rows,          # 其中能规范化出代码的行数
                "raw_unique_codes": raw_unique_codes,    # 唯一代码数（传输轴覆盖）
                # 重复出现的代码个数（== raw_code_rows - raw_unique_codes）；
                # 无代码的行不计入这里，它们在 raw_rows - raw_code_rows 里
                "duplicate_codes": duplicate_codes,
                "usable_rows": len(parsed),              # 能解析成 Quote 的行数
                "usable_quotes": usable_quotes,          # 去重后的 Quote 数（= returned）
                "dropped_invalid": dropped_invalid,      # 被 parser 丢弃的行数
                "usable_coverage": usable_coverage,      # 可用率（诊断，不参与判定）
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

    # --- 分页 / 分批 -----------------------------------------------------
    # 注意：不要恢复「只返回 list[Quote] 的单页抓取」辅助函数（旧 ``_fetch_one_page``
    # / ``_fetch_pages`` 已删除）。``universe()`` 必须拿得到**传输账本**
    # （raw_rows / codes），否则又会退化成拿 post-parse 条数去比 API total ——
    # 那正是 IT-P1-COMPLETE-001-R1 的根因。
    def _scan_one_page(self, pn: int, seq: int) -> ClistPage:
        """抓一页并保留**传输账本**（``universe()`` 要用 raw 行/代码数）。"""
        return parse_clist_page(self._request_json(self._clist_url(pn)), seq)

    def _scan_pages(self, pns: Iterable[int], seq: int) -> tuple[list[ClistPage], list[int]]:
        """并发抓多页；返回 (每页双账, 失败页号列表)。语义同旧 ``_fetch_pages``。

        返回的页按 ``pn`` 升序（并发完成顺序不确定，排序后才能让去重「先到先得」
        有确定的语义：靠前的页赢得重码）。
        """
        wanted = list(pns)
        if not wanted:
            return [], []
        got: dict[int, ClistPage] = {}
        failed: list[int] = []
        with ThreadPoolExecutor(max_workers=min(self.workers, len(wanted))) as ex:
            futs = {ex.submit(self._scan_one_page, pn, seq): pn for pn in wanted}
            for fut in as_completed(futs):
                pn = futs[fut]
                try:
                    got[pn] = fut.result()
                except SourceError as exc:
                    failed.append(pn)
                    with self._lock:
                        self._stats["last_err"] = f"page {pn} 失败: {exc}"[:200]
        if failed:
            failed.sort()
            with self._lock:
                self._stats["pages_failed"] += len(failed)
            if len(failed) == len(wanted):
                raise SourceError(f"eastmoney clist 全部 {len(wanted)} 页失败")
        return [got[pn] for pn in sorted(got)], failed

    def _fetch_ulist(self, codes: list[str], seq: int) -> list[Quote]:
        return parse_ulist(self._request_json(self._ulist_url(codes)), seq)

    def snapshots_detailed(self, codes: list[str], *,
                           route: str = "stocks") -> "SnapshotFetchResult":
        """Snapshot Outcome v4 出口（**精确 raw presence**）。

        东财的 ``_quote_of``（``:196-200``）在 ``f2``/``f18`` 为 ``"-"``
        或 ``<= 0`` 时 ``return None``，而这些行的 ``f12`` 身份
        在 ``parse_clist_page`` 里**已经**被 ``_norm_code`` 取出来了 ——
        旧 ``snapshots()`` 把它和"provider 没返回"一起压成"不在列表里"。

        与 ``snapshots()`` 抓**同一批**数据、同样的批次失败语义。
        """
        from .outcome import PROVENANCE_EXACT, build_outcome, merge_outcomes

        wanted = _norm_codes(codes)
        if not wanted:
            return build_outcome(route=route, source=self.name,
                                 normalized_request=(), raw_keys=(),
                                 quotes=(), raw_presence_known=True,
                                 provenance=PROVENANCE_EXACT)
        seq = self._next_seq()
        batches = [wanted[i:i + self.bulk_chunk]
                   for i in range(0, len(wanted), self.bulk_chunk)]
        parts: list[SnapshotFetchResult] = []
        errors: list[SourceError] = []
        if len(batches) == 1:
            parts.append(self._fetch_ulist_detailed(batches[0], seq, wanted, route))
        else:
            with ThreadPoolExecutor(max_workers=min(self.workers, len(batches))) as ex:
                futs = {ex.submit(self._fetch_ulist_detailed, b, seq, wanted, route): b
                        for b in batches}
                for fut in as_completed(futs):
                    try:
                        parts.append(fut.result())
                    except SourceError as exc:
                        errors.append(exc)
        if errors:
            with self._lock:
                self._stats["pages_failed"] += len(errors)
            self._finish(f"ulist {len(errors)}/{len(batches)} 批失败: {errors[0]}")
            raise SourceError(
                f"eastmoney ulist {len(errors)}/{len(batches)} 批失败: {errors[0]}")
        merged = merge_outcomes(parts)
        self._finish("", merged.quotes)
        return merged

    def _fetch_ulist_detailed(self, codes: list[str], seq: int,
                              requested: list[str],
                              route: str) -> "SnapshotFetchResult":
        payload = self._request_json(self._ulist_url(codes))
        return parse_ulist_detailed(payload, seq, requested=requested, route=route)


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
