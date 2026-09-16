"""核心数据模型：Quote / Snapshot / Alert / 板块与告警枚举。

字段名与 docs/DATA_CONTRACT.md 第 1 节完全一致，禁止改名。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import math

__all__ = [
    "Board",
    "AlertKind",
    "Quote",
    "Snapshot",
    "Alert",
    "board_of",
    "guess_prefix",
    "limit_rate_of",
    "LIMIT_RATE",
]

# 各板块涨跌停比例
LIMIT_RATE: dict[str, float] = {
    "main": 0.10,
    "star": 0.20,
    "gem": 0.20,
    "bj": 0.30,
    "index": 0.0,
    "other": 0.10,
}
# ST 股主板 ±5%
ST_LIMIT_RATE = 0.05


class Board(str, Enum):
    MAIN = "main"
    STAR = "star"
    GEM = "gem"
    BJ = "bj"
    INDEX = "index"
    OTHER = "other"


class AlertKind(str, Enum):
    SURGE = "surge"
    PLUNGE = "plunge"
    LIMIT_UP = "limit_up"
    LIMIT_DOWN = "limit_down"
    VOLUME_BURST = "volume_burst"
    UNUSUAL = "unusual"


# 指数代码（深证成指系列 399xxx 单独判定）
INDEX_CODES = {"000001", "000300", "000905", "000016", "000688", "000852", "000010"}


def board_of(code: str, name: str = "") -> Board:
    """根据 6 位代码与名称判定板块。

    注意 000001 既是平安银行也是上证指数，用名称消歧：名称含"指数"或
    code 属于已知指数集合时判为 INDEX。**名称缺失时一律按股票处理**——
    数据源偶尔不返名称，若此时把 000001 当成指数，平安银行会被
    ``filters.exclude_boards=["index"]`` 静默剔除。
    """
    if isinstance(code, Board):          # 容错：误传枚举时直接用
        return code
    c = str(code or "").strip()          # 容错：容忍 int 代码（JSON 里常见）
    if not c.isdigit() or len(c) != 6:
        return Board.OTHER
    if c.startswith("399"):
        return Board.INDEX
    if c in INDEX_CODES:
        # 只有"明确像指数"时才判 INDEX；名称缺失或不像指数都按主板
        if name and "指数" in name:
            return Board.INDEX
        return Board.MAIN
    if c.startswith(("688", "689")):
        return Board.STAR
    if c.startswith(("300", "301")):
        return Board.GEM
    if c.startswith(("43", "83", "87", "88", "920")):
        return Board.BJ
    if c.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
        return Board.MAIN
    return Board.OTHER


def guess_prefix(code: str) -> str:
    """6 位代码 -> sh/sz/bj 前缀。

    B 股单独处理：沪B ``900xxx`` -> sh，深B ``200xxx`` -> sz（不能落进北交所）。

    ⚠ 本函数**只对个股可靠**。指数必须由调用方显式给出前缀：``000001`` 会被
    推成 ``sz000001``（平安银行），而 ``sh000001`` 才是上证指数。
    """
    if isinstance(code, Board):
        return "sh"
    c = str(code or "").strip()
    if c.startswith(("900", "200")):     # 沪B / 深B
        return "sh" if c.startswith("900") else "sz"
    if c.startswith("6"):
        return "sh"
    if c.startswith(("0", "3")):
        return "sz"
    if c.startswith(("4", "8", "9")):
        return "bj"
    return "sh"


def looks_like_index(symbol: str, name: str = "", bare: str = "") -> bool:
    """判断一个行情符号是否真的是指数（``models`` 层的唯一判据）。

    ``symbol`` 是带前缀的完整符号（``sh000001``），``bare`` 是裸 6 位码。
    单独看前缀不够：``sz000001`` 是平安银行、``sh000001`` 才是上证指数，
    两者前缀都合法。因此规则是"*沪市*的 000 段白名单 + 399 段无条件"：

    1. 名称含"指数/成指"——最直接的证据（数据源给了名称时）；
    2. ``399xxx`` 是深证系列指数，与个股不撞码；
    3. 裸码属于 :data:`INDEX_CODES` 时，**前缀必须是 sh**；
    4. 兜底交给 :func:`board_of`，但要求名称非空 —— 否则裸 ``000001``
       会被误判（它默认按主板处理正是为了这种歧义）。
    """
    sym = str(symbol or "").strip().lower()
    prefix, bare_from_sym = sym[:2], sym[2:]
    if prefix not in ("sh", "sz", "bj"):
        prefix, bare_from_sym = "", sym
    c = str(bare or bare_from_sym or "").strip()
    nm = str(name or "")

    if "指数" in nm or "成指" in nm:
        return True
    if c.startswith("399"):
        return True
    if c in INDEX_CODES:
        return prefix == "sh"
    return bool(nm) and board_of(c, nm) is Board.INDEX


def limit_rate_of(code: str, name: str = "") -> float:
    """该股票的涨跌停比例（ST 主板 5%，其余按板块）。

    只按**板块**决定比例：双创的 ST 股仍是 20%。
    """
    if isinstance(code, Board):
        b = code
    else:
        b = board_of(str(code or ""), name)
    if b in (Board.MAIN, Board.OTHER) and ("ST" in (name or "").upper()):
        return ST_LIMIT_RATE
    return LIMIT_RATE.get(b.value, 0.10)


@dataclass(slots=True)
class Quote:
    code: str
    name: str
    board: Board
    price: float
    prev_close: float
    open: float
    high: float
    low: float
    volume_lots: float
    amount: float
    turnover: float = 0.0
    volume_ratio: float = 0.0
    bid1: float = 0.0
    ask1: float = 0.0
    bid_vol: float = 0.0
    ask_vol: float = 0.0
    # ---- 五档盘口（短线精灵「有大买盘/大卖盘」「机构买单」的判定依据）----
    #: 买一~买五价（元）；长度不足 5 表示数据源未提供，按缺失处理。
    bid_prices: tuple[float, ...] = ()
    #: 买一~买五量（**手**，已与 volume_lots 同单位归一化）。
    bid_vols: tuple[float, ...] = ()
    ask_prices: tuple[float, ...] = ()
    ask_vols: tuple[float, ...] = ()
    # ---- 外盘 / 内盘（主动买 / 主动卖，**手**）----
    #: 外盘：以卖出价成交的量（主动买）。行情快照口径，非逐笔。
    outer_vol: float = 0.0
    #: 内盘：以买入价成交的量（主动卖）。
    inner_vol: float = 0.0
    float_cap: float = 0.0
    total_cap: float = 0.0
    limit_up: float = 0.0
    limit_down: float = 0.0
    ts: datetime | None = None
    seq: int = 0
    # 上市日期 "YYYYMMDD"（股票池源提供，用于新股过滤；缺失为空串）
    list_date: str = ""

    # ---- 派生量 -------------------------------------------------------
    @property
    def pct(self) -> float:
        if self.prev_close <= 0 or self.price <= 0:
            return 0.0
        return (self.price / self.prev_close - 1.0) * 100.0

    @property
    def change(self) -> float:
        if self.prev_close <= 0 or self.price <= 0:
            return 0.0
        return self.price - self.prev_close

    @property
    def amplitude(self) -> float:
        if self.prev_close <= 0:
            return 0.0
        return (self.high - self.low) / self.prev_close * 100.0

    @property
    def vwap(self) -> float:
        """分时均价（元）。"""
        vol_shares = self.volume_lots * 100.0
        if vol_shares <= 0 or self.amount <= 0:
            return self.price
        return self.amount / vol_shares

    @property
    def above_vwap(self) -> bool:
        v = self.vwap
        return v > 0 and self.price >= v

    @property
    def is_suspended(self) -> bool:
        return self.price <= 0 or self.prev_close <= 0 or self.volume_lots <= 0

    @property
    def limit_up_price(self) -> float:
        if self.limit_up and self.limit_up > 0:
            return self.limit_up
        return round(self.prev_close * (1 + limit_rate_of(self.code, self.name)), 2)

    @property
    def limit_down_price(self) -> float:
        if self.limit_down and self.limit_down > 0:
            return self.limit_down
        return round(self.prev_close * (1 - limit_rate_of(self.code, self.name)), 2)

    @property
    def open_pct(self) -> float:
        if self.prev_close <= 0 or self.open <= 0:
            return 0.0
        return (self.open / self.prev_close - 1.0) * 100.0

    # ---- 盘口派生量（短线精灵信号用）----------------------------------
    @property
    def bid_total_vol(self) -> float:
        """五档买盘合计（手）。无五档数据时退回买一量。"""
        if self.bid_vols:
            return float(sum(self.bid_vols))
        return self.bid_vol

    @property
    def ask_total_vol(self) -> float:
        """五档卖盘合计（手）。无五档数据时退回卖一量。"""
        if self.ask_vols:
            return float(sum(self.ask_vols))
        return self.ask_vol

    @property
    def has_depth(self) -> bool:
        """是否有真正的五档数据（而非只有买一/卖一）。"""
        return len(self.bid_vols) >= 5 and len(self.ask_vols) >= 5

    @property
    def float_shares(self) -> float:
        """流通股数（股）。由流通市值 / 现价推算，用于比例型阈值。"""
        if self.float_cap > 0 and self.price > 0:
            return self.float_cap * 1e8 / self.price
        return 0.0

    @property
    def net_outer_vol(self) -> float:
        """外盘 − 内盘（手）。正=主动买占优，负=主动卖占优。"""
        return self.outer_vol - self.inner_vol

    @property
    def outer_inner_ratio(self) -> float:
        """外盘 / 内盘。内盘为 0 时返回 0（数据不足，不猜测）。"""
        if self.inner_vol <= 0:
            return 0.0
        return self.outer_vol / self.inner_vol

    def copy_with(self, **kw) -> "Quote":
        d = {
            "code": self.code, "name": self.name, "board": self.board,
            "price": self.price, "prev_close": self.prev_close, "open": self.open,
            "high": self.high, "low": self.low, "volume_lots": self.volume_lots,
            "amount": self.amount, "turnover": self.turnover,
            "volume_ratio": self.volume_ratio, "bid1": self.bid1, "ask1": self.ask1,
            "bid_vol": self.bid_vol, "ask_vol": self.ask_vol,
            "bid_prices": self.bid_prices, "bid_vols": self.bid_vols,
            "ask_prices": self.ask_prices, "ask_vols": self.ask_vols,
            "outer_vol": self.outer_vol, "inner_vol": self.inner_vol,
            "float_cap": self.float_cap, "total_cap": self.total_cap,
            "limit_up": self.limit_up, "limit_down": self.limit_down,
            "ts": self.ts, "seq": self.seq, "list_date": self.list_date,
        }
        d.update(kw)
        return Quote(**d)


@dataclass(slots=True)
class Snapshot:
    ts: datetime
    seq: int
    quotes: dict[str, Quote] = field(default_factory=dict)

    def get(self, code: str) -> Quote | None:
        return self.quotes.get(code)

    def __len__(self) -> int:
        return len(self.quotes)

    def __iter__(self):
        return iter(self.quotes.values())


@dataclass(slots=True)
class Alert:
    key: str
    kind: AlertKind
    code: str
    name: str
    ts: datetime
    price: float
    pct: float
    title: str
    detail: str
    severity: int = 2
    metrics: dict[str, object] = field(default_factory=dict)
    #: 稳定身份（**不含时间桶**）。用于"距上次同类告警至少 N 秒"的冷却判断。
    #: 留空则退化为只用 ``key`` 去重。
    cooldown_key: str = ""
    #: 同类告警的最小时间间隔（秒）。<=0 表示不做时间距离约束，只按 ``key`` 去重。
    #:
    #: 为什么需要它：``key`` 里带的是**时间桶** ``now_ep // cooldown``，桶边界落在
    #: 固定墙上时钟网格上。于是"桶 5965080 的最后一秒"和"桶 5965081 的第一秒"
    #: 虽然只差 1 秒，key 却不同，去重完全失效——实测两条急拉告警只隔 **11 秒**
    #: （配置的 cooldown 是 300 秒）。加上这个字段后才是真正的"至少隔 300 秒"。
    cooldown_seconds: float = 0.0

    def to_dict(self) -> dict:
        def _num(v: object) -> object:
            """数值指标保留 4 位小数；字符串/列表等原样透传。

            规则会把 ``pattern``（字符串）或 ``hit_windows``（列表）放进 metrics，
            因此这里不能无条件 float()——否则 to_dict() 会抛 ValueError，
            进而炸掉 file/webhook/SSE 整条下游链路。

            **非有限浮点（NaN / ±Inf）一律转成 None**：``json.dumps`` 默认会输出
            裸的 ``NaN``/``Infinity``，那是**非法 JSON**，浏览器端的 ``JSON.parse``
            会直接抛错，整条 SSE 推送就废了。真实数据源可以产出它们
            （例如东财返回 ``f2="1e999"`` -> ``inf``）。
            """
            if isinstance(v, bool):
                return v
            try:
                f = float(v)
            except (TypeError, ValueError):
                return v
            if not math.isfinite(f):
                return None
            return round(f, 4)

        def _finite(v: float) -> float | None:
            """顶层数值字段的非有限值同样必须清掉。"""
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return round(f, 3) if math.isfinite(f) else None

        return {
            "key": self.key,
            "kind": self.kind.value if isinstance(self.kind, AlertKind) else str(self.kind),
            "code": self.code,
            "name": self.name,
            "ts": self.ts.strftime("%Y-%m-%d %H:%M:%S"),
            "price": _finite(self.price),
            "pct": _finite(self.pct),
            "title": self.title,
            "detail": self.detail,
            "severity": self.severity,
            "metrics": {k: _num(v) for k, v in self.metrics.items()},
        }

    def one_line(self) -> str:
        sign = "+" if self.pct >= 0 else ""
        return f"[{self.kind.value}] {self.code} {self.name} {self.price:.2f} ({sign}{self.pct:.2f}%) {self.title}"
