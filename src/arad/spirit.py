"""短线精灵信号注册表与播报格式化。

本模块是「短线精灵」效果的**展示层**：把各规则产出的告警翻译成
同花顺/大智慧短线精灵那样的逐条播报行。

设计要点
--------
* **一处定义，多处复用**：控制台播报、Web 看板滚动列表、JSON 导出都从这里
  取信号中文名与方向，避免三处各写一份映射而逐渐不一致。
* **方向决定配色**：A 股是**红涨绿跌**，所以 ``direction`` 为 ``up`` 的用红色、
  ``down`` 用绿色。注意「打开涨停板」是**利空**（封单被砸开）-> 绿色，
  「打开跌停板」是**利好**（跌停被撬开）-> 红色，这是最容易搞反的两个。
* **不猜**：注册表里没有的信号名原样显示英文名，并归为中性色，
  而不是硬塞进某个分类里。
"""
from __future__ import annotations

import math
from typing import Any

from .models import Alert, AlertKind

# 方向常量
UP = "up"          # 偏多（红）
DOWN = "down"      # 偏空（绿）
NEUTRAL = "flat"   # 中性（灰）


class Signal:
    """一个短线精灵信号的展示元数据。"""

    __slots__ = ("name", "cn", "direction", "group", "hint")

    def __init__(self, name: str, cn: str, direction: str, group: str, hint: str = ""):
        self.name = name            # 内部信号名（metrics["pattern"] 或 kind）
        self.cn = cn                # 中文短名（播报用，尽量 ≤4 字）
        self.direction = direction  # up / down / flat
        self.group = group          # price / order / limit / index / pattern
        self.hint = hint            # 悬停提示：这个信号到底什么意思


#: 信号表。键是信号名，值含中文名/方向/分组/释义。
#:
#: 分组说明：``price`` 价格异动、``order`` 盘口委托、``limit`` 涨跌停、
#: ``index`` 指数、``pattern`` 形态。
SIGNALS: dict[str, Signal] = {
    # ---- 价格类（火箭发射等）----------------------------------------
    "rocket": Signal("rocket", "火箭发射", UP, "price",
                     "快速上涨并创出当日新高"),
    "rebound": Signal("rebound", "快速反弹", UP, "price",
                      "由下跌状态转为快速上涨"),
    "dive": Signal("dive", "高台跳水", DOWN, "price",
                   "由上涨状态转为快速下跌"),
    "accel_down": Signal("accel_down", "加速下跌", DOWN, "price",
                         "延续下跌且跌速变陡"),
    "surge": Signal("surge", "急拉", UP, "price", "短时间快速拉升"),
    "plunge": Signal("plunge", "急跌", DOWN, "price", "短时间快速下跌"),
    # ---- 盘口/委托类（大笔买入等）----------------------------------
    "big_buy": Signal("big_buy", "大笔买入", UP, "order",
                      "主动买盘（外盘）成交占流通盘 ≥0.1%"),
    "big_sell": Signal("big_sell", "大笔卖出", DOWN, "order",
                       "主动卖盘（内盘）成交占流通盘 ≥0.1%"),
    "institution_buy": Signal("institution_buy", "机构买单", UP, "order",
                              "买队列出现大额挂单（≥50万股/100万元/0.25%流通盘）"),
    "institution_sell": Signal("institution_sell", "机构卖单", DOWN, "order",
                               "卖队列出现大额挂单"),
    "institution_eat": Signal("institution_eat", "机构吃货", UP, "order",
                              "主动买入成交单巨大（≥50万股/100万元/0.1%流通盘）"),
    "institution_vomit": Signal("institution_vomit", "机构吐货", DOWN, "order",
                                "主动卖出成交单巨大"),
    "big_bid_wall": Signal("big_bid_wall", "有大买盘", UP, "order",
                           "五档买盘合计 ≥80万股 或 流通盘 0.8%"),
    "big_ask_wall": Signal("big_ask_wall", "有大卖盘", DOWN, "order",
                           "五档卖盘合计 ≥80万股 或 流通盘 0.8%"),
    # ---- 涨跌停类 ---------------------------------------------------
    "limit_up_seal": Signal("limit_up_seal", "封涨停板", UP, "limit", "封住涨停"),
    "limit_down_seal": Signal("limit_down_seal", "封跌停板", DOWN, "limit",
                              "封住跌停"),
    "open_limit_up": Signal("open_limit_up", "打开涨停", DOWN, "limit",
                            "涨停封单被砸开（利空）"),
    "open_limit_down": Signal("open_limit_down", "打开跌停", UP, "limit",
                              "跌停封单被撬开（利好）"),
    # limit_board 规则实际产出的细分信号（触板很常见，必须与封板区分开，
    # 否则 +16% 的触板会被播报成"涨停"，严重误导）
    "limit_up_touch": Signal("limit_up_touch", "触及涨停", NEUTRAL, "limit",
                             "触及涨停价但未封住"),
    "limit_down_touch": Signal("limit_down_touch", "触及跌停", NEUTRAL, "limit",
                               "触及跌停价但未封住"),
    "limit_up": Signal("limit_up", "涨停", UP, "limit", "涨停相关异动"),
    "limit_down": Signal("limit_down", "跌停", DOWN, "limit", "跌停相关异动"),
    # ---- 指数类 -----------------------------------------------------
    "index_pull": Signal("index_pull", "拉升指数", UP, "index",
                         "5 分钟内指数拉升超阈值"),
    "index_press": Signal("index_press", "打压指数", DOWN, "index",
                          "5 分钟内指数打压超阈值"),
    # ---- 形态类 -----------------------------------------------------
    "high_open_fade": Signal("high_open_fade", "高开低走", DOWN, "pattern",
                             "高开后持续走低"),
    "low_open_rise": Signal("low_open_rise", "低开高走", UP, "pattern",
                            "低开后持续走高"),
    "wide_amplitude": Signal("wide_amplitude", "巨震", NEUTRAL, "pattern",
                             "当日振幅剧烈"),
    "late_surge": Signal("late_surge", "尾盘异动", NEUTRAL, "pattern",
                         "临近收盘的急拉或跳水"),
    "reseal": Signal("reseal", "快速回封", UP, "pattern",
                     "炸板后快速重新封上涨停"),
    "volume_burst": Signal("volume_burst", "放量", NEUTRAL, "pattern",
                           "成交量显著放大"),
    # ---- 注意：这里曾有三个"旧命名"信号 seal / break / touch --------------
    # 它们是早期笼统命名（封板/炸板/触板），已被 limit_board 实际使用的
    # 细分名取代：limit_up_seal / limit_down_seal / open_limit_up /
    # open_limit_down / limit_up_touch / limit_down_touch。
    #
    # 三个旧名**没有任何产出点**，也不在 KIND_FALLBACK 里，所以永远不会出现；
    # 但看板按 group 计数时会把它们算进「涨跌停 N 个信号」，虚报数量。
    # 用户悬停读到「封板」「触板」这些名字、又从没见过，会怀疑自己的配置。
    #
    # 已删除（保留这段注释说明去向，避免日后有人以为是漏注册）：
    #   seal  -> limit_up_seal / limit_down_seal
    #   break -> open_limit_up / open_limit_down
    #   touch -> limit_up_touch / limit_down_touch
    #
    # 而 ``limit_up`` / ``limit_down`` 保留：它们虽无 pattern 产出点，
    # 但是 KIND_FALLBACK 的兜底名 —— 没有 pattern 的涨跌停告警会退回它们，
    # 因此是**可达**的，不算虚报。
}

#: ``AlertKind`` -> 兜底信号名（告警没有 pattern 时用它）
KIND_FALLBACK: dict[AlertKind, str] = {
    AlertKind.SURGE: "surge",
    AlertKind.PLUNGE: "plunge",
    AlertKind.LIMIT_UP: "limit_up",
    AlertKind.LIMIT_DOWN: "limit_down",
    AlertKind.VOLUME_BURST: "volume_burst",
    AlertKind.UNUSUAL: "wide_amplitude",
}

#: 中文名 -> Signal 反查表（看板按中文筛选时用）
BY_CN: dict[str, Signal] = {s.cn: s for s in SIGNALS.values()}


def signal_of(alert: Alert) -> str:
    """从告警里取出信号名。

    优先 ``metrics["pattern"]``（各规则都往这里放细分信号），
    其次 ``metrics["signal"]``，最后退回 ``AlertKind``。
    """
    m = getattr(alert, "metrics", None) or {}
    for k in ("pattern", "signal"):
        v = m.get(k)
        if isinstance(v, str) and v:
            return v
    kind = alert.kind
    if isinstance(kind, AlertKind):
        return KIND_FALLBACK.get(kind, kind.value)
    return str(kind)


def describe(alert: Alert) -> Signal:
    """取信号的展示元数据；未注册的信号名原样返回、中性配色。"""
    name = signal_of(alert)
    s = SIGNALS.get(name)
    if s is not None:
        return s
    # 没注册：不硬分类，用原名 + 中性色，保证看板不会崩也不会误导
    return Signal(name, name, NEUTRAL, "other")


def direction_of(alert: Alert) -> str:
    return describe(alert).direction


# ==========================================================================
# 播报行格式化
# ==========================================================================
def fmt_line(alert: Alert, *, width_name: int = 0, show_code: bool = True) -> str:
    """单条播报（短线精灵风格，纯文本、控制台用）。

    形如::

        09:41:07  600000 浦发银行  火箭发射   9.10  +3.21%

    ``width_name`` > 0 时按该宽度对齐股票名，便于多行竖向对齐。
    """
    s = describe(alert)
    ts = alert.ts.strftime("%H:%M:%S") if alert.ts is not None else "--:--:--"
    code = f"{alert.code} " if show_code else ""
    name = alert.name or ""
    name = name.ljust(width_name) if width_name > 0 else name
    pct = f"{alert.pct:+.2f}%"
    return f"{ts}  {code}{name}  {s.cn:<8s} {alert.price:>8.2f}  {pct:>8s}"


def _finite(v: Any, default: float = 0.0) -> float:
    """把非有限浮点（nan/inf）换成安全值。

    为什么必须做：``JSON.parse`` 不接受裸的 ``NaN``/``Infinity``，一旦漏进
    SSE 或 ``/api/spirit`` 的响应体，**整个事件流会解析失败并断掉** —— 看板上
    表现为"短线精灵突然不动了"，而且没有任何报错。历史上真出过这个问题。

    数据源给 ``1e999`` 这类值（东财见过）或做除法时分母为 0 都会产生 inf，
    所以清洗要在**出口统一做**，而不是指望每个调用方自己小心。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def to_feed_item(alert: Alert) -> dict:
    """把告警转成前端滚动列表用的精简结构。

    只带看板真正要显示的字段：短线精灵一屏几十条，字段多了传输和渲染都吃不消。
    所有数值字段都过一遍 ``_finite``，保证结果一定能被 ``JSON.parse`` 接受。
    """
    s = describe(alert)
    m = getattr(alert, "metrics", None) or {}
    return {
        "key": alert.key,
        "ts": alert.ts.strftime("%H:%M:%S") if alert.ts is not None else "",
        "epoch": _finite(alert.ts.timestamp() if alert.ts is not None else 0.0),
        "code": alert.code,
        "name": alert.name,
        "signal": s.name,
        "cn": s.cn,
        "dir": s.direction,
        "group": s.group,
        "hint": s.hint,
        "price": _finite(alert.price),
        "pct": _finite(alert.pct),
        "severity": int(alert.severity) if isinstance(alert.severity, (int, float))
                    and math.isfinite(float(alert.severity)) else 0,
        "title": alert.title,
        # 盘口类信号的关键数字，鼠标悬停时展示
        "extra": {k: _finite(m[k]) for k in
                  ("amount", "volume_ratio", "turnover", "seal_amount_wan",
                   "window_pct", "amplitude", "ratio_vs_float")
                  if k in m},
    }


def group_of(alert: Alert) -> str:
    return describe(alert).group


__all__ = [
    "UP", "DOWN", "NEUTRAL", "Signal", "SIGNALS", "BY_CN", "KIND_FALLBACK",
    "signal_of", "describe", "direction_of", "group_of", "fmt_line",
    "to_feed_item",
]
