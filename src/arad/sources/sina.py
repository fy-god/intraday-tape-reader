"""新浪 hq.sinajs.cn 数据源 —— 交叉校验 / 兜底通道。

契约：``docs/DATA_CONTRACT.md`` 第 2.3 节。

实测要点（2026-09-15 验证）::

    GET https://hq.sinajs.cn/list=sh600000,sz000001
        Referer: https://finance.sina.com.cn   <- **必须带**，否则 403
    响应 GBK。每行: var hq_str_sh600000="浦发银行,9.280,9.260,9.400,...";
    实测 800 码 / 310ms 可用（``bulk_chunk`` 保守默认 600）

**字段顺位与腾讯不同**（契约 2.3 特别强调）::

    0 名称 | 1 今开 | 2 昨收 | 3 现价 | 4 最高 | 5 最低 | 6 买一 | 7 卖一
    8 成交量(**股**) | 9 成交额(元) | ... | 30 日期 | 31 时间
    腾讯是「现价,昨收,今开」，新浪是「今开,昨收,现价」——千万别抄错。

单位换算::

    成交量 8 是**股** -> volume_lots = 股数 / 100（契约第 0 节：成交量=手）

新浪不提供：涨停价/跌停价、量比、市值、换手率 -> 一律置 0，
涨跌停价由 ``Quote.limit_up_price`` / ``limit_down_price`` 按板块费率自行推算。

只用标准库；import 时不做任何 I/O。
"""
from __future__ import annotations

import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Callable, Iterable

from ..models import Board, Quote, board_of, guess_prefix
from .base import SourceError

__all__ = [
    "SinaSource",
    "parse_response",
    "parse_universe",
    "QUOTE_URL",
    "UNIVERSE_URL",
    "LINE_RE",
    "REFERER",
    "SINA_ENCODING",
]

QUOTE_URL = "https://hq.sinajs.cn/list="
REFERER = "https://finance.sina.com.cn"
SINA_ENCODING = "gbk"

#: 全市场股票池：新浪行情中心的「沪深A股」节点列表接口。
#: 实测（2026-09-17）：``num=100`` 单页可用，``node=hs_a`` 共 5563 只；
#: 返回 **UTF-8 JSON 数组**，字段 ``symbol``(带前缀) / ``code`` / ``name`` / ``trade``。
#: 与 ``hq.sinajs.cn`` 不同，这里**不需要 GBK 解码**。
UNIVERSE_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)
#: 股票池节点的股票总数（用于估算页数，失败也不致命）
UNIVERSE_COUNT_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeStockCount"
)
UNIVERSE_NODE = "hs_a"
UNIVERSE_PAGE_SIZE = 100        # 实测 100 稳定；调大会被截断
UNIVERSE_MAX_PAGES = 80         # 5563 / 100 ≈ 56 页，留余量

#: ``var hq_str_sh600000="...";`` —— 契约 2.3 给定的正则
LINE_RE = re.compile(r'var hq_str_([a-z]{2}\d{6})="(.*)";?')

#: 重试退避（秒），与契约 2.1 的 0.3/0.9/2.0 一致
BACKOFF: tuple[float, ...] = (0.3, 0.9, 2.0)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

#: 有效行的最少字段数（下标 0..31，故至少 32 个）
MIN_FIELDS = 32

_NULL_TOKENS = frozenset({"", "-", "--", "null", "none", "nan"})

Fetcher = Callable[[str, "dict[str, str]", float], bytes]


# --------------------------------------------------------------------------
# 纯函数解析（可离线单测）
# --------------------------------------------------------------------------
def _decode(payload: bytes | bytearray) -> str:
    """按契约用 GBK 解码；失败再退回 UTF-8（见 docs/NOTES_eastmoney_sina.md）。

    真实线路是 GBK。但 ``fixtures/raw/sina_bulk_5.txt`` 是探针脚本用
    ``encoding="utf-8"`` 落盘的，字节流并非 GBK —— 严格 GBK 会抛
    ``UnicodeDecodeError``（0xa1 非法续接字节），故加 UTF-8 回退，
    保证离线 fixture 与真实响应都能正确解出中文名称。
    """
    raw = bytes(payload)
    try:
        return raw.decode(SINA_ENCODING)
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(SINA_ENCODING, "replace")


def _as_text(payload: str | bytes | None) -> str:
    """bytes 按 GBK（回退 UTF-8）解码，str 原样返回。"""
    if payload is None:
        return ""
    if isinstance(payload, (bytes, bytearray)):
        return _decode(payload)
    return str(payload)


def _num(value: Any, default: float = 0.0) -> float:
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
    v = _num(value, 0.0)
    return v if v > 0 else default


def _to_int(value: Any, default: int) -> int:
    """宽松取整：坏配置一律退回默认值，绝不抛异常。

    为什么不用裸 ``int()``：配置里写错一个值（``universe_max_pages: many``）
    会让 ``SinaSource.__init__`` 抛 ValueError -> 引擎在 ``_universe_sources()``
    里只打一条 warning 就跳过这个源 -> **兜底能力静默消失**。等东财真被限流
    那天才会发现没得退，而那时已经开盘了。腾讯源一直用同款兜底，这里对齐。
    """
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ts_of(fields: list[str]) -> datetime | None:
    """``30`` 日期 + ``31`` 时间 -> naive datetime；失败 None。"""
    if len(fields) <= 31:
        return None
    date_s = (fields[30] or "").strip()
    time_s = (fields[31] or "").strip()
    if not date_s or not time_s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(f"{date_s} {time_s}", fmt)
        except ValueError:
            continue
    return None


def parse_response(text: str | bytes, seq: int = 0) -> list[Quote]:
    """解析新浪批量行情响应；纯函数，无 I/O。

    - 空 payload（停牌/退市/无此代码，``hq_str_xxx=""``）整行跳过；
    - 现价或昨收 <= 0 的行跳过（停牌股价格全 0，进规则只会制造噪声）；
    - ``volume_lots`` = 字段 8 的股数 / 100。
    """
    out: list[Quote] = []
    s = _as_text(text)
    if not s.strip():
        return out
    for m in LINE_RE.finditer(s):
        code = m.group(1)[2:]  # 正则已保证 [a-z]{2}\d{6}，去掉 sh/sz/bj 前缀
        if not code.isdigit():
            continue
        code = code.zfill(6)
        fields = m.group(2).split(",")
        if len(fields) < MIN_FIELDS:
            continue  # 空 payload / 字段不足 -> 跳过
        name = (fields[0] or "").strip()
        open_ = _num(fields[1])
        prev_close = _num(fields[2])
        price = _num(fields[3])
        if price <= 0 or prev_close <= 0:
            continue  # 停牌 / 退市 / 无行情
        high = _pos(fields[4], max(price, open_))
        low = _pos(fields[5], min(price, open_))
        # 买一/卖一：停牌时可能是 0.000，用现价兜底避免 is_suspended 误判
        bid1 = _pos(fields[6], price)
        ask1 = _pos(fields[7], price)
        volume_shares = _num(fields[8])          # 股
        amount = _num(fields[9])                 # 元
        out.append(
            Quote(
                code=code,
                name=name,
                board=board_of(code, name),
                price=price,
                prev_close=prev_close,
                open=_pos(fields[1], prev_close),
                high=high,
                low=low,
                volume_lots=volume_shares / 100.0,   # 股 -> 手
                amount=amount,
                # 新浪没有换手率 / 量比：保持 0（契约 2.3）
                turnover=0.0,
                volume_ratio=0.0,
                bid1=bid1,
                ask1=ask1,
                # 新浪 f10/f20 是「买一量股数 / 卖一量股数」，不是市值；持仓单位是手
                bid_vol=_num(fields[10]) / 100.0,
                ask_vol=_num(fields[20]) / 100.0,
                # 新浪不提供市值
                float_cap=0.0,
                total_cap=0.0,
                # 新浪不提供涨跌停价 -> 0.0，由 Quote.limit_up_price 推算
                limit_up=0.0,
                limit_down=0.0,
                ts=_ts_of(fields),
                seq=seq,
            )
        )
    return out


def parse_universe(text: str | bytes, seq: int = 0) -> list[Quote]:
    """解析新浪行情中心的列表接口（**UTF-8 JSON**，不是 GBK 行情格式）。

    真实响应形如::

        [{"symbol":"sh600000","code":"600000","name":"浦发银行",
          "trade":"9.280","settlement":"9.260","open":"9.270",
          "high":"9.400","low":"9.200","volume":12345600,"amount":1.1e8, ...}]

    只取股票池需要的字段。契约要求 ``universe()`` 返回 ``Quote``，但列表接口
    给的字段比行情接口少（没有五档、没有内外盘），因此这些一律留 0 —— 它们会
    在下一轮 ``snapshots()`` 里被真实值覆盖。**唯一用途是拿到代码清单。**

    停牌/无成交的行（``trade`` 为 0）也保留：它们的代码仍需参与后续快照轮询，
    否则一只票停牌一天就会从监控里消失、复牌当天完全收不到信号。
    """
    import json

    s = text.decode("utf-8", "replace") if isinstance(text, (bytes, bytearray)) else str(text)
    s = s.strip()
    if not s or s in ("null", "[]"):
        return []
    try:
        rows = json.loads(s)
    except (ValueError, TypeError):
        return []
    if not isinstance(rows, list):
        return []

    out: list[Quote] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_code = str(row.get("code") or "").strip()
        if not (len(raw_code) == 6 and raw_code.isdigit()):
            # code 缺失时退回 symbol（形如 "sh600000"）取后 6 位。
            # 为什么必须兜底：只带 symbol 的行若被整行丢掉，第一页就会"解析为空"
            # -> universe() 立刻 break -> 股票池变空 -> 引擎静默降级成只盯自选股
            # 那 10 只。这是"看起来一切正常但没有告警"的最坏故障形态。
            sym = str(row.get("symbol") or "").strip().lower()
            cand = sym[2:] if len(sym) >= 2 and sym[:2].isalpha() else sym
            if len(cand) == 6 and cand.isdigit():
                raw_code = cand
            else:
                continue
        name = str(row.get("name") or "").strip()
        board = board_of(raw_code, name)
        if board is Board.INDEX:         # 双重保险：指数不进股票池
            continue
        price = _num(row.get("trade"))
        prev_close = _num(row.get("settlement"))
        if prev_close <= 0:
            # 列表接口偶发 settlement 为 0；用开盘价/现价兜底，避免涨跌幅算成 inf
            prev_close = _num(row.get("open")) or price
        out.append(
            Quote(
                code=raw_code,
                name=name,
                board=board,
                price=price,
                prev_close=prev_close if prev_close > 0 else price,
                open=_num(row.get("open")),
                high=_num(row.get("high")),
                low=_num(row.get("low")),
                volume_lots=_num(row.get("volume")) / 100.0,   # 股 -> 手
                amount=_num(row.get("amount")),
                # 列表接口不提供：换手率/量比/五档/内外盘/市值/涨跌停价
                turnover=0.0,
                volume_ratio=0.0,
                float_cap=0.0,
                total_cap=0.0,
                limit_up=0.0,
                limit_down=0.0,
                seq=seq,
            )
        )
    return out


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def default_fetcher(url: str, headers: dict[str, str], timeout: float) -> bytes:
    """标准库 GET（GBK 原始字节原样返回，解析时再解码）。"""
    req = urllib.request.Request(url, headers=dict(headers or {}), method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


# --------------------------------------------------------------------------
# Source
# --------------------------------------------------------------------------
class SinaSource:
    """新浪数据源：``universe()`` 与 ``snapshots()`` 走同一 bulk 接口。

    参数::

        cfg = {"bulk_chunk": 600, "timeout": 10, "retries": 3,
               "workers": 4, "referer": "https://finance.sina.com.cn"}
    """

    name = "sina"

    def __init__(self, cfg: dict[str, Any] | None = None, fetcher: Fetcher | None = None):
        c = dict(cfg or {})
        # 实测 800 码可用，保守默认 600
        self.bulk_chunk = max(1, int(c.get("bulk_chunk", 600) or 600))
        self.timeout = float(c.get("timeout", 10) or 10)
        self.retries = max(1, int(c.get("retries", 3) or 3))
        self.workers = max(1, _to_int(c.get("workers", 4), 4))
        self.referer = str(c.get("referer") or REFERER)
        #: 股票池最多翻多少页（100/页）。5563 只约 56 页，80 页留足余量。
        #:
        #: 非正数一律退回**默认值**，而不是夹到下限 1：只翻 1 页 = 100 只的
        #: 股票池，和空池几乎一样糟（全市场扫描名存实亡），而且不会报错。
        #: 配置写坏了就该用能覆盖全市场的安全默认值。
        _pages = _to_int(c.get("universe_max_pages", UNIVERSE_MAX_PAGES),
                         UNIVERSE_MAX_PAGES)
        self.universe_max_pages = _pages if _pages > 0 else UNIVERSE_MAX_PAGES

        self._fetcher: Fetcher = fetcher or default_fetcher
        self._lock = threading.Lock()
        self._seq = 0
        self._stats: dict[str, Any] = {
            "calls": 0,
            "errors": 0,
            "retries": 0,
            "pages_failed": 0,
            "quotes": 0,
            "last_ms": 0,
            "last_err": "",
        }
        #: 最近一次 ``universe()`` 的完整性元数据（IT-P1-006）。
        self._last_universe: dict[str, Any] = {}

    # --- 对外接口（Source 协议） ---------------------------------------
    def universe(self, max_pages: int | None = None) -> list[Quote]:
        """全市场 A 股股票池（沪深A股节点，分页拉取）。

        为什么新浪也能做股票池：``hq.sinajs.cn`` 只支持按代码查询，但新浪
        **行情中心**另有一套列表接口（``Market_Center.getHQNodeData``），实测能
        列出全部 5563 只。这条路径的价值在于**东财被限流/IP 封时仍有全市场扫描**
        —— 否则系统只能退化成盯自选股那 10 只，盘中基本没用。

        返回的 ``Quote`` 只保证 ``code``/``name`` 可靠；五档、内外盘、市值等
        字段留 0，会在下一轮 ``snapshots()`` 被真实值覆盖。**本方法的唯一用途
        是拿代码清单。**
        """
        pages = int(max_pages or self.universe_max_pages)
        seq = self._next_seq()
        out: list[Quote] = []
        seen: set[str] = set()
        failed_pages: list[int] = []
        pages_ok = 0
        # 循环是「正常翻到底」还是「页数用尽被截断」，决定本次结果完不完整。
        ended_clean = False

        for page in range(1, pages + 1):
            url = (
                f"{UNIVERSE_URL}?page={page}&num={UNIVERSE_PAGE_SIZE}"
                f"&sort=symbol&asc=1&node={UNIVERSE_NODE}"
            )
            try:
                raw = self._request(url)
            except SourceError:
                if page == 1:
                    with self._lock:
                        self._last_universe = {
                            "complete": False, "pages_failed": 1,
                            "pages_ok": 0, "pages_requested": pages,
                            "expected_total": 0, "returned": 0,
                            "reason": "第 1 页即失败",
                        }
                    raise
                # 中途某页失败：保留已拿到的部分，别让整轮股票池刷新报废
                failed_pages.append(page)
                with self._lock:
                    self._stats["pages_failed"] += 1
                    self._stats["last_err"] = f"universe 第 {page} 页失败"
                break

            batch = parse_universe(raw, seq)
            if not batch:
                ended_clean = True       # 翻到空页 = 到底了
                break
            # 逐个判重并**立即写入 seen**：先整体过滤再统一更新 seen 的话，
            # 同一页内部的重复代码会一起通过（节点数据抖动时真的出现过重复）。
            fresh: list[Quote] = []
            for q in batch:
                if q.code in seen:
                    continue
                seen.add(q.code)
                fresh.append(q)
            if not fresh:
                ended_clean = True       # 整页都是重复 -> 数据源在回绕，停
                break
            out.extend(fresh)
            pages_ok += 1
        else:
            # for 正常跑完 = 一直没遇到空页/重复页，页数上限把结果截断了。
            # 这种「不是失败的失败」最危险：返回了几千只、看着很成功，
            # 其实只是全市场的一个前缀。必须如实标成不完整（IT-P1-006）。
            ended_clean = False

        complete = ended_clean and not failed_pages
        with self._lock:
            self._stats["calls"] += 1
            self._last_universe = {
                "complete": complete,
                "pages_failed": len(failed_pages),
                "pages_ok": pages_ok,
                "pages_requested": pages,
                "expected_total": 0,      # 新浪列表接口不返回总数，只能靠翻到底判断
                "returned": len(out),
                "reason": ("已翻到底" if ended_clean else
                           (f"第 {failed_pages[0]} 页起失败" if failed_pages
                            else f"翻满 {pages} 页仍未到底，可能被截断")),
            }
        return out

    def universe_info(self) -> dict:
        """最近一次 ``universe()`` 的完整性元数据（IT-P1-006）。

        契约之外的诊断接口：``complete=False`` 表示这次只拿到全市场的一部分，
        调用方（``Engine.refresh_universe``）据此拒绝用小块覆盖更大的完整股票池。
        """
        with self._lock:
            return dict(getattr(self, "_last_universe", {}) or {})

    def snapshots(self, codes: list[str]) -> list[Quote]:
        """指定代码的最新快照（每批 ``bulk_chunk``，并发）。批次失败即抛 SourceError。"""
        wanted = _norm_codes(codes)
        if not wanted:
            return []
        seq = self._next_seq()
        batches = [wanted[i:i + self.bulk_chunk] for i in range(0, len(wanted), self.bulk_chunk)]
        quotes: list[Quote] = []
        errors: list[SourceError] = []
        if len(batches) == 1:
            quotes = self._fetch_bulk(batches[0], seq)
        else:
            with ThreadPoolExecutor(max_workers=min(self.workers, len(batches))) as ex:
                futs = {ex.submit(self._fetch_bulk, b, seq): b for b in batches}
                for fut in as_completed(futs):
                    try:
                        quotes.extend(fut.result())
                    except SourceError as exc:
                        errors.append(exc)
            if errors:
                with self._lock:
                    self._stats["pages_failed"] += len(errors)
                    self._stats["last_err"] = str(errors[0])[:200]
                raise SourceError(f"sina {len(errors)}/{len(batches)} 批失败: {errors[0]}")
        # 只返回被请求的代码：新浪可能对同一次 list= 查询附带别的代码，
        # 调用方按 code 取值，多返回会造成意外覆盖。
        wanted_set = set(wanted)
        out = _dedupe(q for q in quotes if q.code in wanted_set)
        with self._lock:
            self._stats["quotes"] = len(out)
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
        with self._lock:
            return dict(self._stats)

    # --- 统计 -----------------------------------------------------------
    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    # --- URL / 请求 ------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        """契约 2.3：**必须带 Referer**，否则新浪返回 403。"""
        return {
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": self.referer,
        }

    def _bulk_url(self, prefixed: list[str]) -> str:
        return QUOTE_URL + ",".join(prefixed)

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
                if isinstance(body, str):
                    body = body.encode(SINA_ENCODING, "replace")
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
            f"sina 请求失败（{self.retries} 次尝试后）: {type(last_err).__name__}: {last_err}"
        )

    def _fetch_bulk(self, codes: list[str], seq: int) -> list[Quote]:
        prefixed = [f"{guess_prefix(c)}{c}" for c in codes]
        url = self._bulk_url(prefixed)
        return parse_response(self._request(url), seq)


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _norm_code(value: Any) -> str | None:
    """``600000`` / ``"sh600000"`` -> ``"600000"``。"""
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip().upper()
    if s[:2] in ("SH", "SZ", "BJ"):
        s = s[2:]
    if not s.isdigit() or len(s) > 6:
        return None
    return s.zfill(6)


def _norm_codes(codes: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in codes or []:
        c = _norm_code(raw)
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _dedupe(quotes: Iterable[Quote]) -> list[Quote]:
    out: list[Quote] = []
    seen: set[str] = set()
    for q in quotes:
        if q.code not in seen:
            seen.add(q.code)
            out.append(q)
    return out
