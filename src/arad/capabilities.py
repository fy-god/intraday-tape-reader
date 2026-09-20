"""Round Observation Contract —— 区分「数值零」与「provider 不提供该字段」。

要解决的问题（IT-P1-CAPABILITY-001）
-----------------------------------
``Quote`` 是冻结业务模型，``turnover: float = 0.0`` 同时承载两种完全不同的
语义：

* **真业务零**：源提供了换手率，值就是 0（比如一字板封死无成交）。
* **能力缺失**：源根本不输出这个字段，解析器只能填 ``0.0`` 占位。

``VolumeBurstRule`` 的量比门槛写成了 ``vr > 0.0 and vr < vr_thr``（懂"缺失"），
换手率门槛却写成 ``q.turnover < min_turnover``（不懂）。于是默认配置下
Tencent→Sina 热备时，``volume_burst`` 会在 Sina 服务期间对**所有**股票被
静默过滤 —— ``SourceManager`` 认为调用成功、``health()`` 一切正常，用户只
看到这类告警突然消失。

本模块提供 sidecar，把"这个字段这一轮到底可不可评估"显式化，**不改冻结的
Quote 字段**。规则可以据此区分：
``unavailable_capability``（能力缺失，不该当不达标）与
``evaluated_no_hit``（真的评估了，就是没到门槛）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CAPABILITY_KEYS",
    "SourceCapabilities",
    "CAPABILITY_TABLE",
    "capabilities_for",
    "ObservationDecision",
    "RoundObservationSet",
]

# 参与 capability 判定的字段名。顺序即展示顺序。
CAPABILITY_KEYS: tuple[str, ...] = (
    "turnover",
    "volume_ratio",
    "depth_l1",
    "depth_l5",
    "outer_inner",
    "float_cap",
    "provider_time",
)


@dataclass(slots=True)
class SourceCapabilities:
    """某个数据源**能提供**哪些字段。

    只表达能力，不表达本轮是否真的取到值。缺省全 False（最保守：
    未知来源不擅自假定它有字段，避免把 0.0 误当真实值）。
    """

    source: str = ""
    turnover: bool = False
    volume_ratio: bool = False
    depth_l1: bool = False
    depth_l5: bool = False
    outer_inner: bool = False
    float_cap: bool = False
    provider_time: bool = False

    def supports(self, key: str) -> bool:
        """该源是否提供 ``key``。未知 key 一律按"不提供"处理。"""
        return bool(getattr(self, key, False)) if key in CAPABILITY_KEYS else False

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"source": self.source}
        d.update({k: bool(getattr(self, k)) for k in CAPABILITY_KEYS})
        return d

    def missing(self, keys: tuple[str, ...] = CAPABILITY_KEYS) -> list[str]:
        """在 ``keys`` 里，该源**不提供**的那些。"""
        return [k for k in keys if not self.supports(k)]


# 各源的真实能力（依据解析器实际赋了什么字段，逐行核对得来）。
#
# Tencent : 换手率/量比/五档/内外盘/流通市值全都有。
# Sina    : 明确 turnover=0.0 / volume_ratio=0.0 / float_cap=0.0 占位；
#           只有买一卖一（depth_l1），没有五档 tuples、没有内外盘。
# Eastmoney: clist 快照给换手率/量比/流通市值，但**没有盘口**
#           （bid_prices/ask_prices 与内外盘均缺失）。
#
# 这张表是"声明"，最终以解析器赋值为准（见 tests/test_capabilities.py
# 里逐源核对，防止表与实现漂移）。
CAPABILITY_TABLE: dict[str, SourceCapabilities] = {
    "tencent": SourceCapabilities(
        source="tencent", turnover=True, volume_ratio=True,
        depth_l1=True, depth_l5=True, outer_inner=True,
        float_cap=True, provider_time=False,
    ),
    "sina": SourceCapabilities(
        source="sina", turnover=False, volume_ratio=False,
        depth_l1=True, depth_l5=False, outer_inner=False,
        float_cap=False, provider_time=False,
    ),
    "eastmoney": SourceCapabilities(
        source="eastmoney", turnover=True, volume_ratio=True,
        depth_l1=False, depth_l5=False, outer_inner=False,
        float_cap=True, provider_time=False,
    ),
}

# 能力未知的来源：全部按"不提供"处理，宁可判 unavailable 也不误判不达标。
UNKNOWN_CAPABILITIES = SourceCapabilities(source="unknown")


def capabilities_for(source: Any) -> SourceCapabilities:
    """取某个 source 实例/名字的能力声明。

    优先用 source 自己声明的 ``capabilities``（允许第三方源覆盖），
    否则查 ``CAPABILITY_TABLE``；都不认识就返回全 False 的保守值。
    """
    if isinstance(source, SourceCapabilities):
        return source
    name = source if isinstance(source, str) else getattr(source, "name", "")
    own = getattr(source, "capabilities", None)
    if isinstance(own, SourceCapabilities):
        return own
    return CAPABILITY_TABLE.get(str(name or "").lower(), UNKNOWN_CAPABILITIES)


@dataclass(slots=True)
class ObservationDecision:
    """单只股票在本轮的去向 —— 为可观测性而生（WP08）。

    ``status`` 取值：
    * ``hit``                  命中并产出告警
    * ``evaluated_no_hit``     真的评估了，没到门槛
    * ``unavailable_capability`` 该规则依赖的字段本来源不提供
    * ``rejected_quality``     数据本身不合格（停牌/无价/陈旧/乱序）
    * ``unknown_missing``      请求了但本轮没返回
    """

    code: str
    status: str
    rule: str = ""
    reason: str = ""
    missing: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        d = {"code": self.code, "status": self.status}
        if self.rule:
            d["rule"] = self.rule
        if self.reason:
            d["reason"] = self.reason
        if self.missing:
            d["missing"] = list(self.missing)
        return d


@dataclass(slots=True)
class RoundObservationSet:
    """一轮观测的完整账本（requested / returned / admitted / 各拒绝原因）。

    以前这些数字散落在各处且互相矛盾（``live_session.py`` 拿累计 cache 的
    key 数冒充"本轮覆盖"，见 IT-P2-OBS-001）。这里给出单一事实来源。
    """

    source: str = ""
    # 注意 default_factory 必须是**可调用**，不能直接给实例。
    capabilities: SourceCapabilities = field(
        default_factory=lambda: UNKNOWN_CAPABILITIES)
    #: 本轮**请求**的代码数（个股 + 指数）。
    requested: int = 0
    #: 其中**指数**部分。单独分账是必需的：``requested`` 含指数，而
    #: ``unknown_missing``/``rejected_quality`` 只覆盖个股，不拆开就永远
    #: 对不上账（机械恒等式差 5，正好是 ``poll.index_codes`` 的长度）。
    index_requested: int = 0
    #: provider **原始返回**的条数（含 price<=0 这类质量不可用的）。
    #: 必须与 ``admitted`` 分开：以前 returned 被定义成"准入后"集合，
    #: 于是明明返回了却被内部丢弃的票被记成"没返回"（IT-P2-OBS-005）。
    returned: int = 0
    #: 通过**时间准入**的条数（个股 + 指数，均为纯时间口径）。个股与指数
    #: 同为纯时间准入，不得把"个股业务粗筛"的结果混进来（IT-P2-OBS-003）。
    admitted: int = 0
    #: 其中**指数**部分（``admitted`` 的子集）。
    index_admitted: int = 0
    #: 请求了但 provider 本轮**完全没返回**的代码。
    unknown_missing: tuple[str, ...] = ()
    #: provider 返回了、但因数据质量不可用而被丢弃的代码
    #: （如 ``price<=0``）。与 unknown_missing / 时间拒绝三者互斥。
    rejected_quality: tuple[str, ...] = ()
    #: 因**时间戳过于超前**（超过 FUTURE_TOLERANCE_SECONDS）而被拒的条数。
    #:
    #: ⚠ 改名（IT-P1-OBS-006）：本字段原名 ``stale_rejected``（陈旧），但唯一
    #: 数据来源是 ``engine.py`` 的 ``t_reject:future`` 差分 —— **名实相反**。
    #: 消费方 ``tools/live_session.py`` 早就把它输出成 ``future_rejected``，
    #: 说明"未来"才是真实语义。现按实际语义改名。
    #:
    #: 注意：**当前不存在"陈旧拒绝"路径**。``_admit_time()`` 对任何早于 now
    #: 的 ``ts`` 都无条件接受（首见码甚至接受数天前的 ``ts``，见
    #: IT-P1-TIME-ROLE-003），所以不要指望这里能反映"数据太旧"。
    future_rejected: int = 0
    out_of_order_rejected: int = 0
    # 因能力缺失而无法评估的**代码集合**（不是次数）。同一只票可能同时缺
    # turnover 与 volume_ratio，按次数记会把"1 只票不可评估"夸大成 2，
    # 覆盖率数字就失真了。decisions 里仍逐 (code, field) 留明细。
    unavailable_codes: set[str] = field(default_factory=set)
    decisions: list[ObservationDecision] = field(default_factory=list)

    @property
    def unavailable_capability(self) -> int:
        """本规则因 provider 不提供字段而无法评估的标的**数量**。"""
        return len(self.unavailable_codes)

    @property
    def missing_count(self) -> int:
        return len(self.unknown_missing)

    @property
    def stock_requested(self) -> int:
        """个股部分请求数（``requested`` 去掉指数）。"""
        return max(self.requested - self.index_requested, 0)

    @property
    def stale_rejected(self) -> int:
        """**已弃用别名** —— 请改用 :attr:`future_rejected`。

        保留它只为不打断既有调用方（含 SSE 载荷与旧测试）。名字是错的
        （详见 :attr:`future_rejected` 的说明），新代码不要使用。
        """
        return self.future_rejected

    def coverage(self) -> float:
        """本轮返回率（requested 为 0 时返回 0，不伪造 1.0）。"""
        return (self.returned / self.requested) if self.requested > 0 else 0.0

    def as_dict(self) -> dict[str, Any]:
        """有界汇总。

        ``decisions`` 是逐 (code, field, reason) 的明细，**不能**整份导出 ——
        全市场一轮可达成千上万条，Store 只保留最近一轮，无界数据会把内存和
        SSE 载荷都撑坏（IT-P1-OBS-007 的反面）。这里导出的是**有界样本**
        加**按原因聚合的计数**，足以回答"是哪只票、缺哪个字段"，又不无界。
        """
        # 按 reason 聚合：{reason: 条数}
        by_reason: dict[str, int] = {}
        for d in self.decisions:
            r = str(getattr(d, "reason", "") or "unknown")
            by_reason[r] = by_reason.get(r, 0) + 1
        return {
            "source": self.source,
            "capabilities": self.capabilities.as_dict(),
            "requested": self.requested,
            "index_requested": self.index_requested,
            "returned": self.returned,
            "admitted": self.admitted,
            "index_admitted": self.index_admitted,
            "coverage": round(self.coverage(), 4),
            "unknown_missing": list(self.unknown_missing),
            "rejected_quality": list(self.rejected_quality),
            "future_rejected": self.future_rejected,
            "out_of_order_rejected": self.out_of_order_rejected,
            "unavailable_capability": self.unavailable_capability,
            # --- IT-P1-OBS-007：把"是哪只票、缺什么"带出 poll_once -------------
            # 有界样本：最多 50 条明细，避免无界载荷
            "unavailable_sample": [
                {"code": str(getattr(d, "code", "")),
                 "missing": list(getattr(d, "missing", ()) or ()),
                 "reason": str(getattr(d, "reason", ""))}
                for d in self.decisions[:50]
            ],
            "unavailable_by_reason": by_reason,
            "decisions_truncated": len(self.decisions) > 50,
        }
