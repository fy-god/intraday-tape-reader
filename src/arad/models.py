"""核心数据模型：Quote / Snapshot / Alert / 板块与告警枚举。

字段名与 docs/DATA_CONTRACT.md 第 1 节完全一致，禁止改名。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
import math
import re

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
    "ST_LIMIT_RATE_LEGACY",
    "ST_LIMIT_RATE_CURRENT",
    "ST_LIMIT_RATE_CHANGED_ON",
    "st_limit_rate_on",
    "market_rule_version",
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

# ---- 主板风险警示（ST/*ST）涨跌幅：**制度在 2026-07-06 变过**
#
# 背景（IT-P1-MARKET-RULE-20260706-001）：沪深主板风险警示股票涨跌幅限制
# 自 **2026-07-06** 起由 ±5% 调整为 ±10%（上交所《交易规则》2026 年修订
# 及官方修订说明；深交所风险警示板指南同日施行）。此前一直是 ±5%。
#
# 为什么不能只改一个常量：本项目有**历史回放**（replay 会指定任意交易日），
# 2026-07-06 之前的交易日仍必须按 5% 判定，否则会把那之前的行情
# 算成"没到板"从而漏报、或把 `limit_board` 状态机判错。
#
# 为什么用**日期边界 + 函数**而不是一个 dict：边界哪天再变都有可能，
# 集中在一处才好改；而且判定必须走"交易日"而不是"看到代码的那天"。
ST_LIMIT_RATE_LEGACY = 0.05          # 2026-07-06 之前
ST_LIMIT_RATE_CURRENT = 0.10         # 2026-07-06 起
ST_LIMIT_RATE_CHANGED_ON = date(2026, 7, 6)

#: 日期串的**严格**形态（YYYY-MM-DD / YYYYMMDD / YYYY/MM/DD）。
#: 必须显式钉位数：``strptime(..., "%Y%m%d")`` 会宽松接受 ``"2025031"``
#: 并解析成 2025-03-01，把 7 位乱码当成合法日期（实测）。
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$"
                      r"|^(\d{4})(\d{2})(\d{2})$"
                      r"|^(\d{4})/(\d{2})/(\d{2})$")


def market_rule_version(when: date | datetime | None = None) -> str:
    """该交易日适用的市场规则版本号（可写入告警/样本身份，便于事后追溯）。"""
    d = _as_date(when)
    if d is not None and d < ST_LIMIT_RATE_CHANGED_ON:
        return "cn-2026-07-05"
    return "cn-2026-07-06"


def _as_date(when: date | datetime | None) -> date | None:
    """把 ``date``/``datetime``/常见字符串与整数形式归一成 ``date``。

    IT-P1-UNKNOWN-DATE-FAILOPEN-001（**既有缺陷**，本轮由云端 11:31 审计指认、
    我已独立复现）：本函数原先只认 ``date``/``datetime`` 实例，**其它一律
    返回 ``None``**。而调用方把 ``None`` 解释成"拿不到交易日 -> 按现行制度算"，
    于是**一个完全可解析的历史日期字符串会静默套用现行制度**：

        st_limit_rate_on("2025-03-10") == 0.10   # 错！当时是 0.05
        st_limit_rate_on("20250310")   == 0.10   # 错！
        st_limit_rate_on(20250310)     == 0.10   # 错！

    这不是理论问题：``Quote.ts`` 由真实解析器填充，而它们**确实**会给出
    字符串或 ``None`` —— ``sources/sina.py``、``sources/eastmoney.py`` 的
    ``_ts_of``、``sources/tencent.py``（``len(fields) <= I_TIMESTAMP`` 时给
    ``None``）、``server/web.py:coerce_ts`` 都在这条路径上。
    当日无影响（今天现行制度恰是 10%），但在**历史回放**或**制度再次变更后**
    立刻变成真实故障：5% 制度下的封板会被算成"没到板"而漏报，或把
    ``limit_board`` 状态机判错。

    因此这里补齐真实出现过的输入形态：ISO 串 ``"2025-03-10"``、
    紧凑串 ``"20250310"``、斜杠串 ``"2025/03/10"``、以及等价的整数
    ``20250310``。**仍然无法解析的**（``None``/空串/乱码/epoch 秒）返回
    ``None``，保持既有"按现行制度"的降级语义不变 —— 本次只修"可解析却被
    当成不可解析"这一处静默错误，不改变真正的未知情形。
    """
    if when is None:
        return None
    if isinstance(when, datetime):
        # 注意 datetime 是 date 的子类，必须先判，否则会丢掉时间部分后又 .date()
        return when.date()
    if isinstance(when, date):
        return when
    # --- 真实解析器会给出的字符串/整数形态 -----------------------------
    if isinstance(when, str):
        s = when.strip()
        if not s:
            return None
        # ⚠ 不能只靠 strptime：``strptime("2025031", "%Y%m%d")`` 会**宽松地**
        # 解析成 2025-03-01（它允许个位数的月/日），于是 7 位乱码被当成合法
        # 日期。所以先用正则**钉住位数**，再交给 strptime 校验真实性。
        m = _DATE_RE.match(s)
        if m is None:
            return None
        # 三个分支各 3 组，未参与匹配的分支是 None —— 取非 None 的那三个。
        parts = [g for g in m.groups() if g is not None]
        if len(parts) != 3:
            return None
        y, mo, d = (int(g) for g in parts)
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    if isinstance(when, int) and not isinstance(when, bool):
        # 仅接受 8 位 YYYYMMDD；epoch 秒（10 位）等一律不在此列。
        s = str(when)
        if len(s) != 8 or not s.isdigit():
            return None
        return _as_date(s)          # 复用上面的位数校验 + 真实性校验
    return None


def st_limit_rate_on(when: date | datetime | None) -> float:
    """**指定交易日**沪深主板风险警示股的涨跌幅比例。

    ``when`` 为 ``None`` 时返回**现行**比例（调用方拿不到交易日时，
    只能按当前制度算；回放/实时链路都应传真实交易日）。
    """
    d = _as_date(when)
    if d is not None and d < ST_LIMIT_RATE_CHANGED_ON:
        return ST_LIMIT_RATE_LEGACY
    return ST_LIMIT_RATE_CURRENT


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


def is_new_listing(name: str) -> bool:
    """名称是否处于「无涨跌幅限制」的新股状态（上市首日 ``N`` / 第 2-5 日 ``C``）。

    **为什么必须识别**：2023 全面注册制后，新股上市**前 5 个交易日不设涨跌幅
    限制**。A 股行情源的命名约定是：首日冠 ``N``、第 2~5 日冠 ``C``（如
    ``N沈鼓`` / ``C沈鼓``）。若不识别，:func:`limit_rate_of` 会按板块给出
    ±10%/±20%，算出一个**远低于现价**的"涨停价"，于是：

    * ``limit_board`` 误报「打开跌停 +208%」（实测 601091 C沈鼓，现价 57.77
      而按主板算出的 limit_up 只有 22.88，差 152%）；
    * ``tick_surge`` / ``unusual`` 的"接近涨停"类判据同样失真。

    判据**收紧到"首字母 N/C 且第二个字符是中文"**：只认这个组合，
    避免误伤名称以 C/N 开头的正常股票（如 ``TCL`` 之类英文名）。
    实测全市场 5564 只真实行情里，符合该形态的恰好 2 只（C沈鼓 / C信诺维），
    且**没有**假阳性。
    """
    nm = str(name or "").strip()
    if len(nm) < 2:
        return False
    if nm[0] not in ("N", "C"):
        return False
    return not nm[1].isascii()


def limit_rate_of(code: str, name: str = "",
                  when: date | datetime | None = None) -> float:
    """该股票的涨跌停比例（主板 ST 按**交易日**取 5% 或 10%，其余按板块）。

    只按**板块**决定比例：双创的 ST 股仍是 20%。

    ``when`` = 该行情所属的**交易日**（``Quote.ts`` / 回放时钟 / ``ctx.now``）。
    主板 ST 的比例在 2026-07-06 变过（5% → 10%），所以必须按交易日取，
    不能写死（IT-P1-MARKET-RULE-20260706-001）。``when=None`` 时按现行制度。

    ⚠ **不适用于新股**：上市前 5 个交易日无涨跌幅限制，调用方应先看
    :meth:`Quote.has_price_limit`。这里**不**把新股特判成某个比例 ——
    因为"无限制"不是一个比例，硬塞一个 0.0 或 1.0 都会在下游被当成真阈值。
    """
    if isinstance(code, Board):
        b = code
    else:
        b = board_of(str(code or ""), name)
    if b in (Board.MAIN, Board.OTHER) and ("ST" in (name or "").upper()):
        return st_limit_rate_on(when)
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
    def is_new_listing(self) -> bool:
        """是否处于「无涨跌幅限制」的新股状态（上市首日 ``N`` / 第 2-5 日 ``C``）。"""
        return is_new_listing(self.name)

    @property
    def has_price_limit(self) -> bool:
        """本股当前是否**有**涨跌幅限制。

        新股（``N``/``C`` 状态）上市前 5 个交易日无限制；此外若数据源
        直接给了 ``limit_up``/``limit_down``（部分源会返真实值），也以它为准。
        """
        if self.limit_up and self.limit_up > 0 and self.limit_down and self.limit_down > 0:
            return True
        return not is_new_listing(self.name)

    @property
    def trade_date(self) -> date | None:
        """该行情所属**交易日**（来自 ``ts``）；无 ``ts`` 时返回 ``None``。

        涨跌停比例依赖交易日（主板 ST 在 2026-07-06 由 5% 变 10%），
        所以限价推算必须用**行情自己的**日期，而不是运行当天 ——
        否则历史回放会套用今天的制度（IT-P1-MARKET-RULE-20260706-001）。
        """
        return _as_date(self.ts)

    @property
    def limit_up_price(self) -> float:
        """涨停价；**无涨跌幅限制时返回 0.0**（表示"没有这个约束"）。

        为什么返回 0.0 而不是 ``prev_close * 1.1``：新股上市前 5 日真的没有
        涨停，给一个假的 ±10% 会让 ``limit_board`` 误报
        「打开跌停 +208%」（实测 601091 C沈鼓）。下游约定见
        :meth:`is_at_limit_up` —— 0.0 一律当"无此约束"处理。
        """
        if self.limit_up and self.limit_up > 0:
            return self.limit_up
        if not self.has_price_limit:
            return 0.0
        return round(self.prev_close
                     * (1 + limit_rate_of(self.code, self.name, self.trade_date)), 2)

    @property
    def limit_down_price(self) -> float:
        """跌停价；**无涨跌幅限制时返回 0.0**（同 :attr:`limit_up_price`）。"""
        if self.limit_down and self.limit_down > 0:
            return self.limit_down
        if not self.has_price_limit:
            return 0.0
        return round(self.prev_close
                     * (1 - limit_rate_of(self.code, self.name, self.trade_date)), 2)

    def is_at_limit_up(self, tol: float = 1e-6) -> bool:
        """现价是否**贴着**涨停（无涨跌幅限制时恒为 False）。

        这是"该股有涨停约束"的唯一正确入口：先看是否有约束，再比价格。
        直接写 ``q.price >= q.limit_up_price`` 在无约束股上会因
        ``limit_up_price == 0.0`` 而**恒真**。
        """
        lu = self.limit_up_price
        return bool(lu > 0.0 and self.price >= lu - tol)

    def is_at_limit_down(self, tol: float = 1e-6) -> bool:
        """现价是否**贴着**跌停（无涨跌幅限制时恒为 False）。"""
        ld = self.limit_down_price
        return bool(ld > 0.0 and self.price <= ld + tol)

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
    #: 稳定 signal 身份，取值与 ``SignalEvalStats`` 的账本键**完全一致**
    #: （如 ``"volume_burst"``、``"spirit_order.institution_buy"``）。
    #:
    #: 为什么必须有它（IT-P1-EVAL-PUBLISH-001）：Engine 要在
    #: ``AlertBus.accept`` 与 ``store.add_alert`` 之后**按 signal 记账**。
    #: 没有这个字段，Engine 只能用 ``title`` 文案或 ``cooldown_key`` 去猜
    #: 所属 signal —— 猜错就会把交付数记到别的信号头上，账本比不记还糟。
    #: 由**产生该告警的规则**填写（它本来就知道自己的 signal 名）；
    #: 留空表示该规则不参与 signal 账本（Engine 会跳过，不凭空建条目）。
    signal_id: str = ""

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
            # IT-P1-EVAL-PUBLISH-001：把稳定 signal 身份带给下游。
            # 没有它，消费方（看板/报告/事后标签）只能用 title 文案反推
            # 这条告警属于哪个 signal —— 8 个 spirit pattern 共用同一
            # AlertKind 且文案会改，反推必然错。留空表示该规则不参与
            # signal 账本（如实透出空串，不伪造成某个 signal）。
            "signal_id": self.signal_id,
            # IT-P2-RULE-VERSION-DEAD-001：把**该告警生成时适用的市场规则版本**
            # 写进载荷，让"这条是按 5% 还是 10% 制度算的"可事后追溯。
            #
            # 为什么必须在这里接上：``market_rule_version`` 此前**零调用、零测试**
            # （云端 11:31 审计指认，我已复核：全仓仅 ``models.py`` 的
            # ``__all__`` 与定义两处命中）—— 一个导出了、有 docstring、还被报告
            # 当作"便于事后追溯"来宣传的 API，实际没有任何消费者，
            # 属于**死代码**。接在 to_dict() 是成本最低的真实消费点：
            # alert 是唯一会被落盘/推流/长期保存的载体，正是"事后追溯"的场景。
            #
            # 用 ``self.ts`` 取交易日，所以历史回放的告警会如实带上当时的版本。
            "rule_version": market_rule_version(self.ts),
            "metrics": {k: _num(v) for k, v in self.metrics.items()},
        }

    def one_line(self) -> str:
        sign = "+" if self.pct >= 0 else ""
        return f"[{self.kind.value}] {self.code} {self.name} {self.price:.2f} ({sign}{self.pct:.2f}%) {self.title}"
