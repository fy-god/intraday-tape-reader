"""腾讯行情源 ``qt.gtimg.cn`` —— 主通道（全市场快照 + 自选股快照）。

契约：``docs/DATA_CONTRACT.md`` 第 2.1 节（字段下标，已 100% 算术验证）、
第 2.5 节（代码前缀规则）、第 1 节（Quote 字段）。

要点
----
* ``GET https://qt.gtimg.cn/q=sh600000,sz000001,...`` 带 ``Referer`` 与真实 UA；
  真实响应是 **GBK**（``decode_body`` 里 GBK 优先，失败回退 UTF-8，见 NOTES_tencent.md）。
* 每批最多 ``cfg["bulk_chunk"]``（默认 600），**硬上限 800**（超出立即夹紧并告警）。
* 并发用 ``concurrent.futures.ThreadPoolExecutor``，``cfg["workers"]`` 个线程。
* 每批失败重试 ``cfg["retries"]`` 次（默认 3），退避 0.3 / 0.9 / 2.0 秒；
  即总尝试次数 = 1 + retries。**全部尝试都失败**才抛 ``SourceError``。
* 只用标准库；import 时不做任何 I/O；不 ``raise SystemExit``。
* ``health()`` 与内部统计由 ``threading.Lock`` 保护。

``universe()``
--------------
股票池由 eastmoney 源提供，本源的 ``universe`` 返回快照缓存（``self._cache``）：
最近一次（或多次）``snapshots()`` 结果的并集；缓存为空时抛 ``SourceError``。
也可以显式传 ``codes`` 让本源自己去拉（``universe(codes)`` 等价 ``snapshots(codes)``）。

下标缺口（Quote 无对应字段，解析时忽略）
----------------------------------------
* idx29 最近逐笔、idx43 振幅、idx51 均价：``Quote`` 没有这三个字段
  （``amplitude`` / ``vwap`` 由 high/low 与 amount/volume 派生），故不映射。
"""
from __future__ import annotations

import concurrent.futures as cf
import logging
import os
import re
import ssl
import threading
import time
import urllib.request
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..models import Board, Quote, board_of, guess_prefix, looks_like_index
from .base import SourceError

if TYPE_CHECKING:                                      # pragma: no cover
    from .outcome import SnapshotFetchResult

__all__ = [
    "TencentSource",
    "build",
    "parse_response",
    "decode_body",
    "normalize_code",
    "default_fetcher",
    "URL_BASE",
    "DEFAULT_REFERER",
    "DEFAULT_USER_AGENT",
    "DEFAULT_CHUNK",
    "MAX_CHUNK",
    "BACKOFF_SECONDS",
    "MIN_FIELDS",
    "LINE_RE",
    "FIELD_INDEX",
]

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
URL_BASE = "https://qt.gtimg.cn/q="
DEFAULT_REFERER = "https://gu.qq.com/"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_CHUNK = 600
MAX_CHUNK = 800          # 腾讯硬上限（契约 2.1）
DEFAULT_WORKERS = 4
DEFAULT_TIMEOUT = 10.0
DEFAULT_RETRIES = 3
BACKOFF_SECONDS = (0.3, 0.9, 2.0)
MIN_FIELDS = 54          # payload split("~") 后至少 54 段才有效

#: 单行正则（契约 2.1）。``v_pv_none_match="1";`` 之类不会命中。
LINE_RE = re.compile(r'^v_([a-z]{2}\d{6})="(.*)";?$')

_TIMESTAMP_FMT = "%Y%m%d%H%M%S"
_WAN = 10_000.0          # idx37 是万元
_INF = float("inf")

#: 字段下标（docs/DATA_CONTRACT.md 2.1）。带 ``*_unused`` 的是 Quote 无对应字段的列。
FIELD_INDEX: dict[str, int] = {
    "name": 1,
    "code": 2,
    "price": 3,
    "prev_close": 4,
    "open": 5,
    "volume_lots": 6,
    "outer_vol": 7,           # 外盘（主动买，手）
    "inner_vol": 8,           # 内盘（主动卖，手）
    "bid1": 9,
    "bid_vol": 10,
    "ask1": 19,
    "ask_vol": 20,
    "last_tick": 29,          # Quote 无字段，忽略
    "timestamp": 30,
    "high": 33,
    "low": 34,
    "amount_wan": 37,         # 万元 -> 元 需 ×10000
    "turnover": 38,
    "amplitude": 43,          # Quote 无字段，忽略
    "float_cap": 44,
    "total_cap": 45,
    "limit_up": 47,
    "limit_down": 48,
    "volume_ratio": 49,
    "avg_price": 51,          # Quote 无字段，忽略
}

#: 五档买卖价/量在字段串中的起始下标（买 9..18，卖 19..28，各 5 档、价量交替）。
I_BID_BOOK = 9
I_ASK_BOOK = 19
DEPTH_LEVELS = 5

I_NAME = FIELD_INDEX["name"]
I_CODE = FIELD_INDEX["code"]
I_PRICE = FIELD_INDEX["price"]
I_PREV_CLOSE = FIELD_INDEX["prev_close"]
I_OPEN = FIELD_INDEX["open"]
I_VOLUME_LOTS = FIELD_INDEX["volume_lots"]
I_OUTER_VOL = FIELD_INDEX["outer_vol"]
I_INNER_VOL = FIELD_INDEX["inner_vol"]
I_BID1 = FIELD_INDEX["bid1"]
I_BID_VOL = FIELD_INDEX["bid_vol"]
I_ASK1 = FIELD_INDEX["ask1"]
I_ASK_VOL = FIELD_INDEX["ask_vol"]
I_TIMESTAMP = FIELD_INDEX["timestamp"]
I_HIGH = FIELD_INDEX["high"]
I_LOW = FIELD_INDEX["low"]
I_AMOUNT_WAN = FIELD_INDEX["amount_wan"]
I_TURNOVER = FIELD_INDEX["turnover"]
I_FLOAT_CAP = FIELD_INDEX["float_cap"]
I_TOTAL_CAP = FIELD_INDEX["total_cap"]
I_LIMIT_UP = FIELD_INDEX["limit_up"]
I_LIMIT_DOWN = FIELD_INDEX["limit_down"]
I_VOLUME_RATIO = FIELD_INDEX["volume_ratio"]

#: 可被测试替换的 sleep（避免真的等 3 秒，同时能断言退避序列）
_SLEEP = time.sleep


# --------------------------------------------------------------------------
# 解码 / 取值 helper
# --------------------------------------------------------------------------
def decode_body(raw: bytes | str) -> str:
    """把腾讯原始响应解成 ``str``。

    真实接口是 GBK（契约 2.1）。``fixtures/raw/tencent_bulk_sample.txt`` 实际是
    UTF-8 重编码样本（见 ``docs/NOTES_tencent.md``），所以 GBK 失败时回退 UTF-8。
    """
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode("gbk")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", "replace")
        log.debug("tencent: 响应不是合法 GBK，已回退 UTF-8 解码")
        return text


def _num_or_none(fields: list[str], idx: int) -> float | None:
    """安全取数：越界 / 空串 / 非数字 / NaN / inf -> None（永不抛异常）。"""
    try:
        s = fields[idx]
    except IndexError:
        return None
    if not isinstance(s, str):
        s = str(s)
    s = s.strip()
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    if v != v or v in (_INF, -_INF):   # NaN / ±inf
        return None
    return v


def _fnum(fields: list[str], idx: int, default: float = 0.0) -> float:
    v = _num_or_none(fields, idx)
    return default if v is None else v


def _parse_ts(value: str) -> datetime | None:
    """``20260914161438`` -> datetime；任何异常返回 None（不丢整条）。"""
    s = (value or "").strip()
    if len(s) != 14 or not s.isdigit():
        return None
    try:
        return datetime.strptime(s, _TIMESTAMP_FMT)
    except ValueError:
        return None


def split_prefix(code: str) -> tuple[str, str]:
    """``'sh000001'`` -> ``('sh', '000001')``；``'000001'`` -> ``('', '000001')``。

    指数与前缀的关系不是"加上去更规范"，而是**语义必需**：``000001`` 既是
    上证指数（``sh000001``）也是平安银行（``sz000001``）。调用方显式给了前缀
    时，那才是唯一正确的请求符号，不能再拿 ``guess_prefix`` 猜。
    """
    s = str(code or "").strip().lower()
    if len(s) > 6 and s[:2] in ("sh", "sz", "bj"):
        return s[:2], s[2:]
    return "", s


def normalize_code(code: str) -> str | None:
    """``'600000'`` / ``'sh600000'`` / ``'SH600000'`` -> ``'600000'``，非法返回 None。"""
    s = str(code).strip().upper()
    if len(s) > 6 and s[:2] in ("SH", "SZ", "BJ"):
        s = s[2:].strip()
    if len(s) == 6 and s.isdigit():
        return s
    return None


def _norm_codes(codes) -> list[str]:
    """6 位代码归一 + 去重（保持原顺序），非法代码直接丢弃。"""
    if isinstance(codes, str):
        codes = [codes]
    out: list[str] = []
    seen: set[str] = set()
    for raw in codes or []:
        c = normalize_code(raw)
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _norm_specs(codes) -> list[tuple[str, bool]]:
    """归一成 ``[(完整符号, 是否显式给过前缀)]``，按**解析后**的符号去重。

    - 裸 ``600000`` 用 ``guess_prefix`` 补成 ``sh600000``；显式 ``sh600000``
      保持原样 —— 两者解析结果相同，因此 ``["600000", "sh600000"]`` 只留一个。
    - 显式前缀**必须保留**：``sh000001`` 是上证指数，而裸 ``000001`` 会推成
      ``sz000001``（平安银行）。丢了前缀就会拿到一份"看起来正常"的错误数据。
    - ``bool`` 那一位记录"调用方是否显式写过前缀"，只有显式的才允许被判为指数
      （自选股里裸写 ``000001`` 时，用户要的是平安银行）。
    """
    if isinstance(codes, str):
        codes = [codes]
    out: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for raw in codes or []:
        prefix, _ = split_prefix(raw)
        c = normalize_code(raw)
        if not c:
            continue
        sym = (prefix or guess_prefix(c)) + c
        if sym not in seen:
            seen.add(sym)
            out.append((sym, bool(prefix)))
    return out


def _depth(fields: list[str], start: int) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """取五档买卖盘（价、量交替）。缺失/非法一律按 0 补齐 5 档。

    返回 ``(prices, vols)``，两元组长度恒为 :data:`DEPTH_LEVELS`。
    停牌股常给全 0，此时 ``Quote.has_depth`` 仍为 True 但量合计为 0，
    规则侧再按"量>0"判断，不会误报「有大买盘」。
    """
    prices: list[float] = []
    vols: list[float] = []
    for lv in range(DEPTH_LEVELS):
        pi = start + lv * 2
        vi = pi + 1
        prices.append(_fnum(fields, pi))
        vols.append(_fnum(fields, vi))
    return tuple(prices), tuple(vols)


# --------------------------------------------------------------------------
# 纯解析函数（离线单测入口）
# --------------------------------------------------------------------------
def parse_response(text: str, seq: int = 0,
                   index_codes: set[str] | None = None) -> list[Quote]:
    """把腾讯 ``v_xxx="..."`` 响应文本解析成 ``Quote`` 列表（纯函数，无副作用）。

    * 只认 ``^v_([a-z]{2}\\d{6})="(.*)";?$``；``v_pv_none_match`` / 无匹配行跳过。
    * ``payload.split("~")`` 后长度 < 54 跳过。
    * 数值字段空串/非数字 -> 走 helper 取默认 0.0，**不整条丢弃**；
      只有 ``price`` / ``prev_close`` 无法解析（空串或非数字）时才丢弃该行。
    * ``price <= 0`` 或 ``prev_close <= 0``（停牌）**保留**，``Quote.is_suspended`` 会处理。
    * ``limit_up`` / ``limit_down`` <= 0（停牌/PT 常为 -1）一律置 0，
      由 ``Quote.limit_up_price`` 按板块自行推算，绝不把 -1 当涨停价存进去。
    * idx37 是万元 -> ``amount`` = 值 × 10000（元）；idx6 是手，直接给 ``volume_lots``。

    ``index_codes``：调用方声明的指数符号集合（如 ``{"sh000001", "sz399001"}``）。
    命中时该行的 ``code`` 保留**带前缀**的形式并强制 ``board=INDEX`` —— 因为
    ``000001`` 无法区分上证指数与平安银行，裸码会在 ``EngineState.quotes``
    里互相覆盖。
    """
    out: list[Quote] = []
    idx_set = index_codes or set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or "pv_none_match" in line:
            continue
        m = LINE_RE.match(line)
        if not m:
            continue
        symbol = m.group(1).lower()      # sh600000
        code = symbol[2:]                # 600000
        fields = m.group(2).split("~")
        if len(fields) < MIN_FIELDS:
            continue

        price = _num_or_none(fields, I_PRICE)
        prev_close = _num_or_none(fields, I_PREV_CLOSE)
        if price is None or prev_close is None:
            continue                     # 价格/昨收无效 -> 该行不可用

        name = (fields[I_NAME] or "").strip() if len(fields) > I_NAME else ""

        # 指数：保留带前缀的 code，否则 000001 会与平安银行共用一个 key。
        # 双重确认：调用方声明过这个符号 **且** 这行自身看起来确实是指数 ——
        # 只信声明的话，配置里写错一个 sh600000 就会把个股标成指数。
        is_index = symbol in idx_set and looks_like_index(symbol, name, code)
        out_code = symbol if is_index else code
        board = Board.INDEX if is_index else board_of(code, name)

        # 科创板 688/689 的成交量字段单位是"股"，其余板块是"手"。
        # 不归一化会让 vwap 偏小 100 倍（如 688111 算出 2.30 而实际 230.06）。
        volume_lots = _fnum(fields, I_VOLUME_LOTS)
        bid_vol = _fnum(fields, I_BID_VOL)
        ask_vol = _fnum(fields, I_ASK_VOL)
        outer_vol = _fnum(fields, I_OUTER_VOL)
        inner_vol = _fnum(fields, I_INNER_VOL)

        # 五档（价、量交替）。量同样需要按板块归一化到"手"。
        bid_prices, bid_vols = _depth(fields, I_BID_BOOK)
        ask_prices, ask_vols = _depth(fields, I_ASK_BOOK)

        if _volume_is_shares(code):
            volume_lots /= 100.0
            bid_vol /= 100.0
            ask_vol /= 100.0
            outer_vol /= 100.0
            inner_vol /= 100.0
            bid_vols = tuple(v / 100.0 for v in bid_vols)
            ask_vols = tuple(v / 100.0 for v in ask_vols)

        # 外盘+内盘 应当等于总成交量；偏差过大说明字段口径异常，宁可不给
        # （主动买卖判定完全依赖这两个数，错了会产出方向相反的信号）。
        if outer_vol > 0 and inner_vol > 0 and volume_lots > 0:
            drift = abs((outer_vol + inner_vol) - volume_lots) / volume_lots
            if drift > 0.05:
                outer_vol = 0.0
                inner_vol = 0.0

        limit_up = _fnum(fields, I_LIMIT_UP)
        limit_down = _fnum(fields, I_LIMIT_DOWN)
        if limit_up <= 0:                # 停牌/PT 的 -1 不能当涨停价
            limit_up = 0.0
        if limit_down <= 0:
            limit_down = 0.0

        out.append(
            Quote(
                code=out_code,
                name=name,
                board=board,
                price=price,
                prev_close=prev_close,
                open=_fnum(fields, I_OPEN),
                high=_fnum(fields, I_HIGH),
                low=_fnum(fields, I_LOW),
                volume_lots=volume_lots,
                amount=_fnum(fields, I_AMOUNT_WAN) * _WAN,
                turnover=_fnum(fields, I_TURNOVER),
                volume_ratio=_fnum(fields, I_VOLUME_RATIO),
                bid1=_fnum(fields, I_BID1),
                ask1=_fnum(fields, I_ASK1),
                bid_vol=bid_vol,
                ask_vol=ask_vol,
                bid_prices=bid_prices,
                bid_vols=bid_vols,
                ask_prices=ask_prices,
                ask_vols=ask_vols,
                outer_vol=outer_vol,
                inner_vol=inner_vol,
                float_cap=_fnum(fields, I_FLOAT_CAP),
                total_cap=_fnum(fields, I_TOTAL_CAP),
                limit_up=limit_up,
                limit_down=limit_down,
                ts=_parse_ts(fields[I_TIMESTAMP]) if len(fields) > I_TIMESTAMP else None,
                seq=seq,
            )
        )
    return out


def parse_response_detailed(text: str, seq: int = 0,
                            index_codes: set[str] | None = None,
                            requested: list[str] | None = None,
                            route: str = "stocks") -> "SnapshotFetchResult":
    """``parse_response`` 的**精确 raw-presence** 版本（Snapshot Outcome v4）。

    `IT-P1-SNAPSHOT-OUTCOME-SOURCE-SEMANTICS-DRIFT-001`（12:37 §2）：
    腾讯**已经**保留 ``price<=0`` 的停牌行（见 ``:294`` 的契约注释），
    所以对腾讯而言引擎侧的 ``rejected_quality`` 本来就对。

    但 parser 里还有**两处 ``continue`` 会丢掉 raw 行**：

    * ``:316`` 字段数 < :data:`MIN_FIELDS`（54）；
    * ``:321`` ``price`` / ``prev_close`` 无法解析（空串 / 非数字）。

    这两处丢掉的行，在**只交出 ``list[Quote]``** 的旧接口下
    与"provider 根本没返回这个代码"**不可区分** —— 引擎一律记
    ``unknown_missing``。**同一个 raw 事实**（provider 明确返回了该行、
    只是数值不可用）在腾讯与新浪/东财上因此落进**不同的账本桶**。

    本函数在**数值质量校验之前**捕获身份（``:313-314`` 已经解析出
    ``symbol``），因此**不需要任何额外 HTTP** 就能给出精确的
    ``raw_present`` / ``rejected_quality`` / ``unknown_missing`` 三分。

    **键轴**：与 ``Quote.code`` **逐字同轴** ——
    个股用 6 位裸码；被识别为指数的行用带前缀符号
    （``000001`` 无法区分上证指数与平安银行，见 ``parse_response`` 的说明）。

    ⚠ 这个"同轴"要求是**易错点**：`_norm_specs` 一律返回带前缀符号，
    而 `Quote.code` 只对**指数**带前缀。若请求集直接用 `_norm_specs` 的
    ``sym``，则 R 里是 ``sh600000`` 而 raw 键是 ``600000``，
    交集恒为空 —— 会把**每一次正常取数**报成"全部 missing"。
    """
    from .outcome import PROVENANCE_EXACT, build_outcome, normalize_request

    idx_set = index_codes or set()

    def _key(symbol: str, name: str) -> str:
        """符号 -> 与 ``Quote.code`` 同轴的键（复刻 parse_response 的指数判定）。"""
        code = symbol[2:]
        if symbol in idx_set and looks_like_index(symbol, name, code):
            return symbol
        return code

    def _req_key(raw: Any) -> str | None:
        """请求项 -> 与 `_key` 同轴的键（**必须**与 `_key` 的判定一致）。"""
        prefix, _ = split_prefix(raw)
        c = normalize_code(raw)
        if not c:
            return None
        sym = (prefix or guess_prefix(c)) + c
        # 只有**显式**写过前缀、且该符号确实在指数集合里 == 指数；
        # 其余（含裸 000001 想拉平安银行）都是个股 -> 裸码。
        if prefix and sym in idx_set:
            return sym
        return c

    req = normalize_request(requested or (), normalize=_req_key)

    raw_keys: list[str] = []
    raw_rows: list[Any] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or "pv_none_match" in line:
            continue
        m = LINE_RE.match(line)
        if not m:
            continue
        symbol = m.group(1).lower()
        fields = m.group(2).split("~")
        name = (fields[I_NAME] or "").strip() if len(fields) > I_NAME else ""
        # **身份在数值校验之前捕获** —— 这是本函数存在的全部理由。
        raw_keys.append(_key(symbol, name))
        raw_rows.append(line)

    return build_outcome(
        route=route,
        source=TencentSource.name,
        normalized_request=req,
        raw_keys=raw_keys,
        quotes=parse_response(text, seq, index_codes),
        raw_rows=raw_rows,
        raw_presence_known=True,
        provenance=PROVENANCE_EXACT,
    )


# --------------------------------------------------------------------------
# 网络：默认 fetcher（可注入替换）
# --------------------------------------------------------------------------
def _volume_is_shares(code: str) -> bool:
    """该代码在腾讯接口里成交量单位是否为"股"（而非"手"）。

    实测（2026-09，用 ``换手率`` 与 ``流通市值/价格`` 反推成交股数交叉验证）：

    ===========  ==========  ============  ==============
    板块         代码前缀     实测 vol/实际股数   单位
    ===========  ==========  ============  ==============
    科创板       688/689     1.00          **股**
    沪市主板     600/601/603  0.01          手
    深市主板     000/001     0.01          手
    创业板       300/301     0.01          手
    北交所       920         0.01          手
    ===========  ==========  ============  ==============

    即只有科创板 688/689 需要 /100。佐证：688111 金山办公 idx6=3506475、
    成交额 8.067 亿元 -> 均价 230.06 元；若按"手"算会得到 2.30 元，
    与 228.59 的现价相差 100 倍，明显错误。
    ``idx7``(外盘)+``idx8``(内盘) 与 idx6 完全相等，进一步确认 idx6 就是股数。
    """
    return code.startswith(("688", "689"))


_CTX_LOCK = threading.Lock()
_SSL_CTX: dict[str, ssl.SSLContext] = {}

#: 是否允许在证书校验失败时回退到**不校验证书**的连接。
#:
#: 默认 True，因为部分 Windows Python 环境的根证书链不全，严格校验会直接
#: ``UNEXPECTED_EOF_WHILE_READING`` / ``CERTIFICATE_VERIFY_FAILED``，导致完全取不到
#: 行情。但这等于放弃中间人攻击防护——在不可信网络（公共 WiFi、公司代理）下
#: 应当关掉：设环境变量 ``ARAD_ALLOW_INSECURE_TLS=0``。
#: 关闭后证书问题会直接抛错，由 ``sources.fallback`` 切到备用源，而不是静默降级。
_INSECURE_OFF = ("0", "false", "no", "")


def _env_allows_insecure_tls() -> bool:
    """读环境变量决定是否允许不校验 TLS 的兜底重试（默认允许）。"""
    return os.environ.get(
        "ARAD_ALLOW_INSECURE_TLS", "1").strip().lower() not in _INSECURE_OFF


ALLOW_INSECURE_TLS: bool = _env_allows_insecure_tls()


def _ssl_context(*, insecure: bool = False) -> ssl.SSLContext:
    """惰性构造 SSLContext（import 时不做任何事）。"""
    key = "insecure" if insecure else "verified"
    with _CTX_LOCK:
        ctx = _SSL_CTX.get(key)
        if ctx is None:
            ctx = ssl.create_default_context()
            if insecure:                 # 仅在本机证书链不全时兜底，见 ALLOW_INSECURE_TLS
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            _SSL_CTX[key] = ctx
        return ctx


def _is_ssl_error(exc: BaseException) -> bool:
    """判断异常链里是否有证书/TLS 错误（URLError 会把 SSLError 包在 reason 里）。"""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLError):
            return True
        txt = str(cur).upper()
        if "CERTIFICATE" in txt or "SSL" in txt or "TLS" in txt:
            return True
        reason = getattr(cur, "reason", None)
        if isinstance(reason, BaseException) and reason is not cur:
            if _is_ssl_error(reason):
                return True
        nxt = cur.__cause__ or cur.__context__
        cur = nxt if nxt is not cur else None
    return False


def default_fetcher(url: str, headers: dict, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    """urllib 默认抓取器：``fetcher(url, headers, timeout) -> bytes``。

    先用 ``ssl.create_default_context()`` 严格校验。若因证书链失败，是否回退到
    **不校验** 的上下文由 :data:`ALLOW_INSECURE_TLS` 决定（见该开关的说明）；
    关闭时证书问题会直接抛出，绝不静默降级。
    """
    req = urllib.request.Request(url, headers=dict(headers or {}), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            return resp.read()
    except Exception as exc:  # noqa: BLE001 - 仅对 SSL 类错误兜底，其余原样抛
        if not _is_ssl_error(exc):
            raise
        if not ALLOW_INSECURE_TLS:
            raise
        log.warning(
            "tencent: TLS 证书校验失败，回退到**不校验证书**的连接重试一次: %s "
            "（这会失去中间人攻击防护；确认网络可信后再用，或用 "
            "ARAD_ALLOW_INSECURE_TLS=0 关掉该回退）", exc)
    with urllib.request.urlopen(
        req, timeout=timeout, context=_ssl_context(insecure=True)
    ) as resp:
        return resp.read()


# --------------------------------------------------------------------------
# 配置 helper
# --------------------------------------------------------------------------
def _to_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _resolve_chunk(cfg: dict) -> int:
    """``bulk_chunk``（回退 ``batch_size``），夹到 [1, MAX_CHUNK] 并 assert。"""
    raw = cfg.get("bulk_chunk")
    if raw is None:
        raw = cfg.get("batch_size")
    chunk = _to_int(raw, DEFAULT_CHUNK)
    if chunk < 1:
        log.warning("tencent: bulk_chunk=%r 非法，回退默认 %d", raw, DEFAULT_CHUNK)
        chunk = DEFAULT_CHUNK
    if chunk > MAX_CHUNK:
        log.warning("tencent: bulk_chunk=%d 超过硬上限 %d，已夹到上限", chunk, MAX_CHUNK)
        chunk = MAX_CHUNK
    assert 0 < chunk <= MAX_CHUNK, f"bulk_chunk 越界: {chunk}"
    return chunk


# --------------------------------------------------------------------------
# 数据源
# --------------------------------------------------------------------------
class TencentSource:
    """腾讯行情源（实现 ``arad.sources.base.Source`` 协议）。

    用法::

        src = TencentSource(source_cfg("tencent"))
        quotes = src.snapshots(["600000", "000001"])   # 6 位代码
        src.universe()                                  # 最近快照的并集缓存
        src.health()                                    # {'name','ok','latency_ms','err'}

    ``fetcher`` 可注入以便离线测试：``fetcher(url, headers, timeout) -> bytes``。
    """

    name = "tencent"

    def __init__(self, cfg: dict | None = None, fetcher=None) -> None:
        cfg = dict(cfg or {})
        self.cfg = cfg
        self.url_base = str(cfg.get("base_url") or URL_BASE)
        self.referer = str(cfg.get("referer") or DEFAULT_REFERER)
        self.user_agent = str(cfg.get("user_agent") or DEFAULT_USER_AGENT)
        self.timeout = _to_float(cfg.get("timeout"), DEFAULT_TIMEOUT)
        self.retries = max(0, _to_int(cfg.get("retries"), DEFAULT_RETRIES))
        self.chunk = _resolve_chunk(cfg)
        self.workers = max(1, _to_int(cfg.get("workers"), DEFAULT_WORKERS))
        self._fetcher = fetcher if fetcher is not None else default_fetcher

        self._lock = threading.Lock()
        self._cache: dict[str, Quote] = {}          # code -> 最近一次快照（并集）
        self._seq = 0
        self._stats: dict[str, int] = {
            "requests": 0, "errors": 0, "retries": 0, "chunks": 0, "quotes": 0,
        }
        self._last_latency_ms = 0
        self._last_ok = True
        self._last_err = ""

    # ---- Source 协议 ----------------------------------------------------
    def universe(self, codes: list[str] | None = None) -> list[Quote]:
        """全市场快照。

        **股票池由 eastmoney 源提供，本源的 universe 返回快照缓存**：
        最近一次（或多次）``snapshots()`` 结果的并集（``self._cache``）；
        缓存为空时抛 ``SourceError``。显式传入 ``codes`` 时等价于 ``snapshots(codes)``
        （代码列表由外部注入，本源不依赖 eastmoney）。
        """
        if codes is not None:
            return self.snapshots(codes)
        with self._lock:
            quotes = list(self._cache.values())
        if not quotes:
            raise SourceError(
                "tencent: universe 缓存为空，先调用 snapshots(codes) 注入代码；"
                "全市场股票池请使用 eastmoney 源"
            )
        return quotes

    def snapshots(self, codes: list[str]) -> list[Quote]:
        """指定 6 位代码列表的最新快照（内部转 ``sh600000`` 形式并按 chunk 并发）。

        任一批次在耗尽重试后仍失败 -> 抛 ``SourceError``（快照原子，不污染缓存）。
        """
        norm = _norm_specs(codes)
        if not norm:
            return []
        prefixed = [sym for sym, _ in norm]
        # 只有调用方**显式**写过前缀的符号才可能是指数：自选股里裸写 000001
        # 时用户要的是平安银行，不能因为它在 INDEX_CODES 里就标成上证指数。
        idx_set = {sym for sym, explicit in norm if explicit}
        size = self.chunk
        assert 0 < size <= MAX_CHUNK, f"bulk_chunk 越界: {size}"
        chunks = [prefixed[i:i + size] for i in range(0, len(prefixed), size)]

        seq = self._next_seq()
        headers = self._headers()
        with self._lock:
            self._stats["chunks"] += len(chunks)

        results: list[list[Quote]] = []
        failures: list[Exception] = []
        workers = max(1, min(self.workers, len(chunks)))
        with cf.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="arad-tencent"
        ) as ex:
            futures = [ex.submit(self._fetch_chunk, ch, seq, headers, idx_set)
                       for ch in chunks]
            for fut in futures:          # 按提交顺序取回，保证输出顺序稳定
                try:
                    results.append(fut.result())
                except Exception as exc:  # noqa: BLE001 - 汇总所有批次错误后统一抛
                    failures.append(exc)

        if failures:
            raise SourceError(
                f"tencent: {len(failures)}/{len(chunks)} 批次失败（首错: {failures[0]}）"
            ) from failures[0]

        merged: dict[str, Quote] = {}
        for batch in results:
            for q in batch:
                merged[q.code] = q
        with self._lock:
            self._cache.update(merged)
            self._stats["quotes"] += len(merged)
        return list(merged.values())

    def health(self) -> dict:
        """``{'name':str,'ok':bool,'latency_ms':int,'err':str}``（最近一次请求）。"""
        with self._lock:
            return {
                "name": self.name,
                "ok": bool(self._last_ok),
                "latency_ms": int(self._last_latency_ms),
                "err": str(self._last_err),
            }

    # ---- 额外（非协议）--------------------------------------------------
    def stats(self) -> dict:
        """内部计数器副本（requests / errors / retries / chunks / quotes）。"""
        with self._lock:
            return dict(self._stats)

    @property
    def cache_size(self) -> int:
        with self._lock:
            return len(self._cache)

    # ---- 内部 -----------------------------------------------------------
    def _headers(self) -> dict:
        return {
            "User-Agent": self.user_agent,
            "Referer": self.referer,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }

    def _build_url(self, prefixed_codes: list[str]) -> str:
        return self.url_base + ",".join(prefixed_codes)

    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def _record(self, started: float, ok: bool, err: str) -> None:
        ms = int(round((time.perf_counter() - started) * 1000.0))
        with self._lock:
            self._last_latency_ms = ms
            self._last_ok = bool(ok)
            self._last_err = "" if ok else str(err)
            self._stats["requests"] += 1
            if not ok:
                self._stats["errors"] += 1

    def snapshots_detailed(self, codes: list[str], *,
                           route: str = "stocks") -> "SnapshotFetchResult":
        """Snapshot Outcome v4 出口（**精确 raw presence**）。

        与 ``snapshots()`` 抓**同一批**数据（同样按 chunk 并发、
        同样在批次全败时抛 ``SourceError``），但额外把
        "provider 返回了这一行、只是 ``price``/``prev_close`` 不可用"
        与 "provider 根本没返回" 分开。

        腾讯的 ``parse_response`` **保留** ``price<=0`` 的停牌行，
        但仍会在 ``len(fields) < MIN_FIELDS``（``:316``）和
        ``price/prev_close is None``（``:321``）两处 ``continue`` ——
        这两处在旧接口下与"真缺席"不可区分。
        """
        from .outcome import PROVENANCE_EXACT, build_outcome, merge_outcomes

        norm = _norm_specs(codes)
        if not norm:
            return build_outcome(route=route, source=self.name,
                                 normalized_request=(), raw_keys=(),
                                 quotes=(), raw_presence_known=True,
                                 provenance=PROVENANCE_EXACT)
        prefixed = [sym for sym, _ in norm]
        idx_set = {sym for sym, explicit in norm if explicit}
        size = self.chunk
        assert 0 < size <= MAX_CHUNK, f"bulk_chunk 越界: {size}"
        chunks = [prefixed[i:i + size] for i in range(0, len(prefixed), size)]

        seq = self._next_seq()
        headers = self._headers()
        with self._lock:
            self._stats["chunks"] += len(chunks)

        parts: list[SnapshotFetchResult] = []
        failures: list[Exception] = []
        workers = max(1, min(self.workers, len(chunks)))
        with cf.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="arad-tencent"
        ) as ex:
            futs = [ex.submit(self._fetch_chunk_detailed, ch, seq, headers,
                              idx_set, codes, route) for ch in chunks]
            for fut in futs:
                try:
                    parts.append(fut.result())
                except Exception as exc:  # noqa: BLE001 - 汇总后统一抛
                    failures.append(exc)

        if failures:
            raise SourceError(
                f"tencent: {len(failures)}/{len(chunks)} 批次失败"
                f"（首错: {failures[0]}）"
            ) from failures[0]

        merged = merge_outcomes(parts)
        with self._lock:
            for q in merged.quotes:
                self._cache[q.code] = q
            self._stats["quotes"] += len(merged.quotes)
        return merged

    def _fetch_chunk_detailed(self, chunk: list[str], seq: int, headers: dict,
                              idx_set: set[str], requested: list[str],
                              route: str) -> "SnapshotFetchResult":
        """``_fetch_chunk`` 的 v4 版（同样的重试/退避，多交回 raw 身份）。"""
        url = self._build_url(chunk)
        attempts = self.retries + 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            if attempt:
                delay = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
                with self._lock:
                    self._stats["retries"] += 1
                _SLEEP(delay)
            started = time.perf_counter()
            try:
                raw = self._fetcher(url, headers, self.timeout)
                out = parse_response_detailed(decode_body(raw), seq, idx_set,
                                              requested=requested, route=route)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                self._record(started, False, f"{type(exc).__name__}: {exc}")
                continue
            self._record(started, True, "")
            return out
        raise SourceError(
            f"tencent: 批次 {len(chunk)} 码在 {attempts} 次尝试后仍失败: {last_exc}"
        ) from last_exc

    def _fetch_chunk(self, codes: list[str], seq: int, headers: dict,
                     index_codes: set[str] | None = None) -> list[Quote]:
        """抓一批：失败重试 ``self.retries`` 次（退避 0.3/0.9/2.0s），全败抛 SourceError。"""
        url = self._build_url(codes)
        attempts = self.retries + 1        # 首次 + retries 次重试
        last_exc: Exception | None = None
        for attempt in range(attempts):
            if attempt:
                delay = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
                with self._lock:
                    self._stats["retries"] += 1
                log.debug(
                    "tencent: 重试 %d/%d（%d 码）退避 %.1fs",
                    attempt, self.retries, len(codes), delay,
                )
                _SLEEP(delay)
            started = time.perf_counter()
            try:
                raw = self._fetcher(url, headers, self.timeout)
                quotes = parse_response(decode_body(raw), seq, index_codes)
            except Exception as exc:  # noqa: BLE001 - 网络/解析任何异常都重试
                last_exc = exc
                self._record(started, False, f"{type(exc).__name__}: {exc}")
                continue
            self._record(started, True, "")
            return quotes
        raise SourceError(
            f"tencent: {len(codes)} 码批次在 {attempts} 次尝试后仍失败: {last_exc}"
        ) from last_exc


def build(cfg: dict | None = None, fetcher=None) -> TencentSource:
    """工厂函数（与 notifiers/rules 的 ``build(cfg)`` 风格保持一致）。"""
    return TencentSource(cfg, fetcher=fetcher)
