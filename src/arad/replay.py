"""离线回放引擎 —— 不依赖真实行情，用合成 tick 流驱动真实引擎。

用途
----
1. **演示**：休市日/无网络时也能看到系统实际报警效果；
2. **验证**：把一整天的交易时段压缩到几十毫秒跑完，端到端检查
   「数据源 → 状态 → 规则 → 去重 → 通知」整条链路；
3. **回归**：确定性种子 -> 确定性行情 -> 确定性告警，可写进测试。

设计要点
--------
* **时钟隔离**：所有时间都来自 ``SimClock``，真实墙钟只用于算耗时。
  行情时间轴从 ``session_start`` 开始按 ``tick_seconds`` 推进，
  只覆盖连续竞价（09:30-11:30、13:00-15:00）。
* **确定性**：随机数一律走 ``random.Random(seed)``，绝不碰全局 ``random``，
  因此同 seed + 同参数字节级可复现。
* **几何游走**：价格按**等比**变动。等价差路径的百分比涨幅会随基数增大而
  衰减（10.0→10.25 是 +2.5%，10.25→10.5 只有 +2.44%），会让"加速度"类
  规则产生非预期结果，故一律用等比。
* **刻意植入事件**：每只股票被指定一个 ``script``（剧本），明确包含
  急拉 / 急跌 / 涨停封板 / 炸板 等，保证规则**必然**被触发，
  而不是"随机跑跑看有没有告警"。
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from typing import Callable, Iterable, Iterator, Sequence

from .engine import Engine, EngineState, build_rules
from .models import INDEX_CODES, Alert, Board, Quote, Snapshot, board_of, limit_rate_of
from .session import SessionPhase, TradingCalendar

__all__ = [
    "MORNING_OPEN", "MORNING_CLOSE", "AFTERNOON_OPEN", "AFTERNOON_CLOSE",
    "SimClock", "ReplayQuoteSource", "ScriptedStock", "build_script",
    "generate_script", "default_universe", "ReplayResult", "Replay",
    "trading_timeline", "TICK_SECONDS",
]

MORNING_OPEN = dtime(9, 30)
MORNING_CLOSE = dtime(11, 30)
AFTERNOON_OPEN = dtime(13, 0)
AFTERNOON_CLOSE = dtime(15, 0)

TICK_SECONDS = 15.0          # 一个 tick 代表 15 秒真实行情

# 剧本类型
KIND_NORMAL = "normal"
KIND_SURGE = "surge"         # 急拉
KIND_PLUNGE = "plunge"       # 急跌
KIND_LIMIT_SEAL = "seal"     # 涨停封板
KIND_LIMIT_BREAK = "break"   # 涨停后炸板
KIND_LIMIT_DOWN = "limit_down"   # 跌停封板
KIND_VOLUME_BURST = "volume_burst"
KIND_HIGH_OPEN_FADE = "high_open_fade"    # 高开低走
KIND_LATE_SURGE = "late_surge"            # 尾盘急拉

ALL_KINDS = (
    KIND_SURGE, KIND_PLUNGE, KIND_LIMIT_SEAL, KIND_LIMIT_BREAK,
    KIND_LIMIT_DOWN, KIND_VOLUME_BURST, KIND_HIGH_OPEN_FADE,
    KIND_LATE_SURGE, KIND_NORMAL,
)

# 剧本 -> 中文名（报告里给用户看）
KIND_CN = {
    KIND_NORMAL: "普通震荡",
    KIND_SURGE: "急拉",
    KIND_PLUNGE: "急跌",
    KIND_LIMIT_SEAL: "涨停封板",
    KIND_LIMIT_BREAK: "涨停炸板",
    KIND_LIMIT_DOWN: "跌停封板",
    KIND_VOLUME_BURST: "放量异动",
    KIND_HIGH_OPEN_FADE: "高开低走",
    KIND_LATE_SURGE: "尾盘急拉",
}


# ==========================================================================
# 模拟时钟
# ==========================================================================
class SimClock:
    """可推进的假时钟。``__call__`` 让它能直接当作 ``Engine(now_fn=...)``。

    真实墙钟只用于统计回放耗时，绝不参与业务时间判定。
    """

    def __init__(self, start: datetime):
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    @property
    def now(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> None:
        self._now = when

    def advance(self, seconds: float) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now


def trading_timeline(day: datetime, tick_seconds: float = TICK_SECONDS,
                     minutes: float | None = None) -> list[datetime]:
    """生成一个交易日的连续竞价时间轴（跳过午休）。

    ``minutes`` 只取上午前 N 分钟，用于快速演示。
    """
    out: list[datetime] = []
    step = timedelta(seconds=tick_seconds)
    # 上午
    t = datetime.combine(day.date(), MORNING_OPEN)
    morning_end = datetime.combine(day.date(), MORNING_CLOSE)
    while t <= morning_end:
        out.append(t)
        t += step
    # 下午
    t = datetime.combine(day.date(), AFTERNOON_OPEN)
    close = datetime.combine(day.date(), AFTERNOON_CLOSE)
    while t <= close:
        out.append(t)
        t += step
    if minutes is not None:
        # 只保留上午前 minutes 分钟（保证演示时长短、且仍在早盘时段）
        cutoff = datetime.combine(day.date(), MORNING_OPEN) + timedelta(minutes=minutes)
        out = [x for x in out if x <= cutoff]
    return out


# ==========================================================================
# 剧本化行情生成
# ==========================================================================
@dataclass
class ScriptedStock:
    """一只股票的剧本：代码 + 剧本类型 + 起始价。"""

    code: str
    kind: str = KIND_NORMAL
    start_price: float = 10.0
    name: str = ""
    board: Board = Board.MAIN
    # 剧本触发的时间进度（0~1），用于错开各股异动时刻
    at: float = 0.3
    float_cap: float = 50.0        # 亿元，用于 filters 的市值过滤
    vol_base: float = 20000.0      # 起始累计成交量（手）

    def limit_rate(self, when: object = None) -> float:
        """按**代码**算涨跌停比例；``when`` 是交易日（影响 ST 制度 5%→10%）。

        必须传 ``self.code``：``limit_rate_of`` 内部靠代码前缀判板块，
        传 ``Board`` 枚举会被判成 ``Board.OTHER``（10%），
        导致创业板/科创板剧本的涨停价算错（如 300750 会算成 55.00 而非 60.00），
        整个离线校验的价值就没了。

        IT-P1-ST-REPLAY-BYPASS-001（**既有缺陷**，本轮由云端 11:31 审计指认、
        我已独立复现）：本方法原先**不传日期**，于是 ``limit_rate_of`` 落到
        "按现行制度"，回放任何历史交易日的主板 ST 都得到 10% —— 而 2026-07-06
        之前是 5%。因为 ``build_script`` 把这个值算成 ``limit_up``/``limit_down``
        塞进 ``Quote``（见下），而 ``Quote.limit_up_price`` 在 ``limit_up>0`` 时
        **提前返回**，``Quote`` 里那条日期感知分支**永远不会执行**：
        整条回放链路结构性地绕过了 ST 修复。

        实测（回放 2025-03-10 主板 ST ``600001``）：``limit_up_price=11.00``，
        而当时 5% 制度下正确值是 **10.50** —— 真实 5% 封板不会被识别为涨停。
        """
        return limit_rate_of(self.code, self.name, when)

    def listed_days(self) -> str:
        """上市日期占位，避免 ``filters.min_list_days`` 因缺字段而误杀。"""
        return "20200101"


def _f(name: str) -> float:
    try:
        return float(name)
    except (TypeError, ValueError):
        return 0.0


def default_universe(count: int = 20, seed: int = 42) -> list[ScriptedStock]:
    """构造一个覆盖全部剧本类型的默认股票池。

    前若干只被指定为明确的剧本（保证规则必然触发），其余为普通震荡股。
    板块覆盖主板/创业板/科创板/北交所与 ST。
    """
    rng = random.Random(seed)
    catalog = [
        # (code, kind, start_price, name)
        ("600519", KIND_SURGE, 100.0, "测试急拉"),
        ("000001", KIND_PLUNGE, 20.0, "测试急跌"),
        ("601318", KIND_LIMIT_SEAL, 30.0, "测试封板"),
        ("300750", KIND_LIMIT_BREAK, 50.0, "测试炸板"),
        ("688111", KIND_VOLUME_BURST, 80.0, "测试放量"),
        ("002594", KIND_HIGH_OPEN_FADE, 40.0, "测试高开低走"),
        ("600030", KIND_LATE_SURGE, 15.0, "测试尾盘拉"),
        ("000858", KIND_LIMIT_DOWN, 25.0, "测试跌停"),
        ("601899", KIND_SURGE, 12.0, "测试急拉2"),
        ("600000", KIND_NORMAL, 9.5, "测试震荡"),
    ]
    out: list[ScriptedStock] = []
    seen: set[str] = set()
    for i, (code, kind, px, name) in enumerate(catalog):
        if i >= count:
            break
        board = board_of(code, name)
        out.append(ScriptedStock(
            code=code, kind=kind, start_price=px, name=name, board=board,
            at=0.2 + 0.6 * (i / max(1, len(catalog) - 1)),
            float_cap=20.0 + 10.0 * i,
            vol_base=float(rng.randint(8000, 30000)),
        ))
        seen.add(code)

    # 补足到 count 只：用不同板块的合成代码，剧本轮转
    idx = 0
    while len(out) < count:
        # 600xxx / 000xxx / 300xxx / 688xxx 循环，覆盖各板块涨跌幅。
        # 注意科创板必须是 688xxx/689xxx（±20%），写成 680xxx 会被 board_of 判成
        # STAR 但 limit_rate_of 只给 10%，两个口径打架。
        bucket = idx % 4
        n = 700 + idx
        if bucket == 0:
            code, board = f"60{n:04d}", Board.MAIN
        elif bucket == 1:
            code, board = f"00{n:04d}", Board.MAIN
        elif bucket == 2:
            code, board = f"30{n:04d}", Board.GEM
        else:
            code, board = f"688{n:03d}", Board.STAR
        idx += 1
        if code in seen:
            continue
        seen.add(code)
        kind = ALL_KINDS[idx % len(ALL_KINDS)]
        out.append(ScriptedStock(
            code=code, kind=kind,
            start_price=round(rng.uniform(6.0, 60.0), 2),
            name=f"合成{code[-3:]}", board=board,
            at=rng.uniform(0.15, 0.85),
            float_cap=rng.uniform(20.0, 300.0),
            vol_base=float(rng.randint(5000, 40000)),
        ))
    return out


def build_script(stock: ScriptedStock, timeline: Sequence[datetime],
                 rng: random.Random) -> list[Quote]:
    """把一只股票的剧本展开成逐 tick 的 ``Quote`` 序列。

    全程**等比**变动价格；成交量单调递增（累计值），并在异动时放量。
    """
    n = len(timeline)
    if n == 0:
        return []
    # IT-P1-ST-REPLAY-BYPASS-001：必须把**回放当日**传给涨跌停判定，
    # 否则 ST 制度变更（2026-07-06 主板 5%→10%）在整条回放链路上被绕过。
    trade_day = timeline[0].date()
    rate = stock.limit_rate(trade_day)
    limit_up = round(stock.start_price * (1 + rate), 2)
    limit_down = round(stock.start_price * (1 - rate), 2)
    # 触发窗口（以 tick 数表示）
    at = min(max(stock.at, 0.0), 0.95)
    trig = int(at * n)
    # 异动持续约 4 分钟（16 个 tick），保证多窗口（60/180/300s）都能命中
    span = max(6, min(24, n // 8 or 6))
    end = min(n - 1, trig + span)

    base_vol = stock.vol_base
    quotes: list[Quote] = []
    price = stock.start_price
    cum = 0.0
    peak = stock.start_price
    trough = stock.start_price
    open_px = stock.start_price

    for i, ts in enumerate(timeline):
        prev_price = price
        if stock.kind == KIND_NORMAL:
            # 温和随机游走 ±0.12%
            price *= 1 + rng.uniform(-0.0012, 0.0012)
            step_vol = base_vol * rng.uniform(0.6, 1.4) / max(1, n)
        elif stock.kind == KIND_SURGE:
            if trig <= i <= end:
                # 凸函数：越涨越快（后段涨幅 > 前段涨幅），满足 accel 校验。
                # 总涨幅取涨跌停幅度的 ~55%，保证"急拉但不封板"，
                # 与 KIND_LIMIT_SEAL 有明确区分。
                frac = (i - trig) / max(1, end - trig)
                total = rate * 0.55
                price = stock.start_price * (1 + total * (frac ** 1.6))
            else:
                price *= 1 + rng.uniform(-0.0008, 0.0008)
            step_vol = base_vol / max(1, n) * (6.0 if trig <= i <= end else 1.0)
        elif stock.kind == KIND_PLUNGE:
            if trig <= i <= end:
                frac = (i - trig) / max(1, end - trig)
                total = rate * 0.55
                price = stock.start_price * (1 - total * (frac ** 1.6))
            else:
                price *= 1 + rng.uniform(-0.0008, 0.0008)
            step_vol = base_vol / max(1, n) * (6.0 if trig <= i <= end else 1.0)
        elif stock.kind == KIND_LIMIT_SEAL:
            if i < trig:
                price *= 1 + rng.uniform(-0.001, 0.001)
            elif price < limit_up:
                # 快速拉向涨停，之后一直封住
                price = min(limit_up, price * 1.02)
            step_vol = base_vol / max(1, n) * (8.0 if i >= trig else 1.0)
        elif stock.kind == KIND_LIMIT_BREAK:
            if i < trig:
                price *= 1 + rng.uniform(-0.001, 0.001)
            elif i <= trig + max(2, span // 2):
                price = min(limit_up, price * 1.03)      # 先封板
            else:
                price = min(price, limit_up * 0.97)      # 再炸板
            step_vol = base_vol / max(1, n) * (5.0 if i >= trig else 1.0)
        elif stock.kind == KIND_LIMIT_DOWN:
            if i < trig:
                price *= 1 + rng.uniform(-0.001, 0.001)
            elif price > limit_down:
                price = max(limit_down, price * 0.98)
            step_vol = base_vol / max(1, n) * (8.0 if i >= trig else 1.0)
        elif stock.kind == KIND_VOLUME_BURST:
            if trig <= i <= end:
                price *= 1 + 0.0025                       # 小幅上台阶
            else:
                price *= 1 + rng.uniform(-0.0008, 0.0008)
            step_vol = base_vol / max(1, n) * (12.0 if trig <= i <= end else 1.0)
        elif stock.kind == KIND_HIGH_OPEN_FADE:
            # 高开 +3.5%，然后一路走低到 -3.5%
            if i == 0:
                price = stock.start_price * 1.035
            elif i <= end:
                frac = (i - trig) / max(1, end - trig) if i >= trig else 0.0
                price = stock.start_price * (1.035 - 0.07 * frac)
            step_vol = base_vol / max(1, n) * (2.0 if i >= trig else 1.0)
        elif stock.kind == KIND_LATE_SURGE:
            # 尾盘最后 10% 时间急拉
            tail = int(n * 0.9)
            if i >= tail:
                price *= 1.006
            else:
                price *= 1 + rng.uniform(-0.001, 0.001)
            step_vol = base_vol / max(1, n) * (5.0 if i >= tail else 1.0)
        else:
            price *= 1 + rng.uniform(-0.001, 0.001)
            step_vol = base_vol / max(1, n)

        # 夹在涨跌停之间（A股硬约束）
        price = min(max(price, limit_down), limit_up)
        if i == 0:
            open_px = price
        peak = max(peak, price)
        trough = min(trough, price)
        cum += max(0.0, step_vol)

        # 成交量单位是"手"；成交额用均价近似（VWAP ≈ (high+low+price)/3）
        vwap_approx = (peak + trough + price) / 3.0
        quotes.append(Quote(
            code=stock.code, name=stock.name, board=stock.board,
            price=round(price, 3), prev_close=stock.start_price,
            open=round(open_px, 3), high=round(peak, 3), low=round(trough, 3),
            volume_lots=round(cum, 1), amount=round(cum * 100 * vwap_approx, 2),
            turnover=round(cum * 100 / max(1.0, stock.float_cap * 1e8 / stock.start_price) * 100, 4),
            volume_ratio=round(1.0 + 3.0 * (1.0 if trig <= i <= end else 0.0), 2),
            bid1=round(price, 3), ask1=round(price, 3),
            # 封板时给出巨额封单，让 limit_board 的封单额判定成立
            bid_vol=round(cum * 0.3, 1) if price >= limit_up - 1e-9 else 0.0,
            ask_vol=round(cum * 0.3, 1) if price <= limit_down + 1e-9 else 0.0,
            float_cap=stock.float_cap, total_cap=stock.float_cap * 1.5,
            limit_up=limit_up, limit_down=limit_down,
            ts=ts, seq=i,                     # 必须是 datetime，引擎会拿它比大小
            list_date=stock.listed_days(),
        ))
    return quotes


def _stable_code_seed(code: str) -> int:
    """把股票代码映射成**跨进程稳定**的整数种子。

    **为什么不能用内置 ``hash()``**（IT-P1-REPLAY-DETERMINISM-001）：
    CPython 对 ``str`` 的 ``hash()`` 默认按 ``PYTHONHASHSEED`` **每进程随机化**
    （抵御哈希碰撞 DoS）。于是 ``hash(s.code)`` 会让同 seed 在不同进程生成
    **不同**行情 —— 本模块文档承诺的"同 seed + 同参数字节级可复现"是假的。

    实测（三个独立子进程，seed=42）：数据指纹
    ``8ba5542d…`` / ``4d73762d…`` / ``95a79558…`` 三者互不相同；
    固定 ``PYTHONHASHSEED=0`` 后三者全部相同 —— 根因确认。
    后果：``selftest``/``replay`` 的告警数在 68~71 之间漂移，
    **不能作为回归判据**，历次报告里"selftest N 条告警"也都不是稳定不变量。

    这里改用 SHA-256 前 8 字节，它对同一字符串**永远**给出同一个值。
    """
    h = hashlib.sha256(str(code).encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big") % 2**31


def generate_script(stocks: Sequence[ScriptedStock], day: datetime,
                    *, seed: int = 42, tick_seconds: float = TICK_SECONDS,
                    minutes: float | None = None) -> dict[str, list[Quote]]:
    """生成 ``{code: [Quote, ...]}`` 的整段回放数据（确定性）。

    确定性由 :func:`_stable_code_seed` 保证：同 ``seed`` + 同股票列表，
    **在任何进程、任何时刻**都产出逐字相同的数据。请勿改回 ``hash()``。
    """
    timeline = trading_timeline(day, tick_seconds=tick_seconds, minutes=minutes)
    out: dict[str, list[Quote]] = {}
    for i, s in enumerate(stocks):
        rng = random.Random((seed * 1_000_003) ^ (i * 7919) ^ _stable_code_seed(s.code))
        out[s.code] = build_script(s, timeline, rng)
    return out


# ==========================================================================
# 回放数据源
# ==========================================================================
class ReplayQuoteSource:
    """把预生成的行情按 tick 逐帧喂给引擎，实现 ``Source`` 协议。

    引擎每次 ``snapshots()`` 取当前帧并推进指针，因此无需改引擎主循环。
    """

    name = "replay"

    def __init__(self, frames: dict[str, list[Quote]], *, loop: bool = False):
        self.frames = frames
        self.codes = list(frames)
        self.loop = loop
        self.cursor = 0
        self.length = max((len(v) for v in frames.values()), default=0)
        self.exhausted = False

    def at_end(self) -> bool:
        return self.cursor >= self.length

    def current_time(self) -> datetime | None:
        for series in self.frames.values():
            if series:
                idx = min(self.cursor, len(series) - 1)
                if series[idx].ts is not None:
                    return series[idx].ts
        return None

    def snapshots(self, codes: list[str]) -> list[Quote]:
        if self.at_end():
            if not self.loop:
                self.exhausted = True
                return []
            self.cursor = 0
        out: list[Quote] = []
        want = set(codes) if codes else set(self.frames)
        for code, series in self.frames.items():
            if code not in want or not series:
                continue
            out.append(series[min(self.cursor, len(series) - 1)])
        self.cursor += 1
        return out

    def universe(self) -> list[Quote]:
        return self.snapshots(list(self.frames))

    def health(self) -> dict:
        return {"name": self.name, "ok": True, "latency_ms": 0, "err": ""}


# ==========================================================================
# 回放器
# ==========================================================================
@dataclass
class ReplayResult:
    """回放结果汇总。"""

    alerts: list[Alert] = field(default_factory=list)
    rounds: int = 0
    ticks: int = 0
    elapsed_ms: float = 0.0
    stocks: list[ScriptedStock] = field(default_factory=list)
    by_kind: dict[str, int] = field(default_factory=dict)
    by_stock: dict[str, list[Alert]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.alerts)

    def kinds_expected(self) -> dict[str, list[str]]:
        """剧本里"应该"报警的股票：{剧本: [代码]}。"""
        out: dict[str, list[str]] = {}
        for s in self.stocks:
            if s.kind != KIND_NORMAL:
                out.setdefault(s.kind, []).append(s.code)
        return out

    def hit(self) -> dict[str, list[str]]:
        """剧本 -> 实际报出告警的代码列表。"""
        out: dict[str, list[str]] = {}
        for s in self.stocks:
            got = [a for a in self.by_stock.get(s.code, [])]
            if got:
                out.setdefault(s.kind, []).append(s.code)
        return out

    def missed(self) -> list[ScriptedStock]:
        """植入了剧本却一条告警都没出的股票。"""
        return [s for s in self.stocks
                if s.kind != KIND_NORMAL and not self.by_stock.get(s.code)]

    def summary_lines(self) -> list[str]:
        """人类可读的中文汇总（CLI 直接打印）。"""
        lines: list[str] = []
        lines.append(f"回放完成：{self.rounds} 轮 / {self.ticks} tick，"
                     f"耗时 {self.elapsed_ms:.0f} ms，共 {self.total} 条告警")
        if self.by_kind:
            parts = "，".join(f"{k} {v} 条" for k, v in sorted(self.by_kind.items()))
            lines.append(f"按类型：{parts}")
        hit = self.hit()
        exp = self.kinds_expected()
        lines.append("剧本命中情况：")
        for kind, codes in sorted(exp.items()):
            got = set(hit.get(kind, []))
            mark = "✓" if got else "✗"
            lines.append(f"  {mark} {KIND_CN.get(kind, kind):<8} "
                         f"应报 {len(codes)} 只 / 实报 {len(got)} 只")
        miss = self.missed()
        if miss:
            lines.append("未触发告警的剧本股：" +
                         "，".join(f"{s.code}({KIND_CN.get(s.kind, s.kind)})" for s in miss))
        return lines


class Replay:
    """驱动真实 ``Engine`` 跑完一段合成行情。"""

    def __init__(self, stocks: Sequence[ScriptedStock] | None = None, *,
                 seed: int = 42, tick_seconds: float = TICK_SECONDS,
                 minutes: float | None = None, day: datetime | None = None,
                 settings=None, rules=None, notifiers=None, store=None):
        self.seed = seed
        self.tick_seconds = tick_seconds
        self.stocks = list(stocks) if stocks is not None else default_universe(20, seed)
        self.day = day or self._pick_day()
        self.frames = generate_script(self.stocks, self.day, seed=seed,
                                      tick_seconds=tick_seconds, minutes=minutes)
        self.timeline = trading_timeline(self.day, tick_seconds=tick_seconds, minutes=minutes)
        self.source = ReplayQuoteSource(self.frames)
        self.clock = SimClock(self.timeline[0] if self.timeline else self.day)
        self.settings = settings
        self._rules = rules
        self._notifiers = notifiers
        self._store = store

    @staticmethod
    def _pick_day() -> datetime:
        """选一个最近的交易日（周三），避免落在周末/节假日。"""
        d = datetime(2026, 9, 16, 9, 30)      # 2026-09-16 是周三
        return d

    # ------------------------------------------------------------------
    def build_engine(self) -> Engine:
        from .config import load_settings
        st = self.settings or load_settings()
        rules = self._rules if self._rules is not None else build_rules(st)
        # 回放时把自选设成整个剧本池，让状态跟踪覆盖所有股票
        watch = [s.code for s in self.stocks]
        return Engine(
            self.source, settings=st, rules=rules,
            notifiers=self._notifiers if self._notifiers is not None else [],
            store=self._store, watchlist=watch,
            calendar=TradingCalendar(holidays=set()),
            now_fn=self.clock,
        )

    def run(self, *, progress: Callable[[int, list[Alert]], None] | None = None
            ) -> ReplayResult:
        """跑完整段回放，返回汇总结果。"""
        import time as _time
        t0 = _time.perf_counter()
        engine = self.build_engine()
        # 让引擎直接使用剧本代码，跳过全市场 universe 抓取
        engine._codes = [s.code for s in self.stocks]
        engine._universe_refreshed_at = float("inf")

        result = ReplayResult(stocks=self.stocks)
        rounds = 0
        while not self.source.at_end():
            nxt = self.source.current_time()
            if nxt is not None:
                self.clock.set(nxt)
            try:
                fresh = engine.poll_once(force=True)
            except Exception:      # noqa: BLE001  —— 单轮异常不应中断整段回放
                fresh = []
            rounds += 1
            for a in fresh:
                result.alerts.append(a)
                result.by_stock.setdefault(a.code, []).append(a)
            if progress is not None:
                try:
                    progress(rounds, fresh)
                except Exception:  # noqa: BLE001
                    pass
        result.rounds = rounds
        result.ticks = self.source.length
        result.elapsed_ms = (_time.perf_counter() - t0) * 1000.0
        for a in result.alerts:
            key = a.kind.value if hasattr(a.kind, "value") else str(a.kind)
            result.by_kind[key] = result.by_kind.get(key, 0) + 1
        return result
