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
    "SignalEvalStats",
    "RoundObservationSet",
    # WP01 / IT-P1-CAPABILITY-003：缺失的三种语义
    "BLOCKING",
    "ADVISORY",
    "NOT_REQUIRED",
]

#: 缺失导致该 signal **不能评估**。必须进 blocked 分母。
BLOCKING = "blocking"
#: 当前规则语义**允许跳过**该条件，signal 仍可评估（只是判据少了一项）。
#: 绝不能与 BLOCKING 混为一谈 —— 这正是 IT-P1-CAPABILITY-003 的核心。
ADVISORY = "advisory"
#: 本 signal 根本不需要该 capability。既不进 evaluable 也不进 blocked。
NOT_REQUIRED = "not_required"

#: ``as_dict()`` 里每个 signal 最多导出多少条 blocked 样本（防止全市场载荷无界）。
_BLOCKED_SAMPLE_CAP = 20

#: 全局门禁：第一方告警里**不许有**缺 ``signal_id`` 的 committed 条目。
#:
#: IT-P1-DELIVERY-LEDGER-002 的验收条件。为什么它是"门禁"而不是"指标"：
#: 缺 ``signal_id`` 的告警进不了交付账本 -> 无法回答"它到底有没有交付" ->
#: T+5/T+30 标签无法引用它 -> **无法定量回答"报得准不准"**（用户的核心诉求）。
#: 所以它必须是 0，而不是"越低越好"。
FIRST_PARTY_UNSIGNED_GATE_REASON = "first_party_committed_without_signal_id"

#: 交付账本自身的**记账自洽性**状态（IT-P1-DELIVERY-FALSE-GREEN-001）。
#:
#: 为什么需要它：只看 ``first_party_committed_without_signal_id == 0``
#: **不足以**证明账本健康 —— 该门禁用 ``max(total - named, 0)`` 算，
#: 会把**负差夹成 0**。于是下面两种"账本已经坏了"的情形都会显示 PASS：
#:
#: * 分母自增被异常吞掉：``total=0`` 而 ``named=66`` -> 门禁 0（假绿）
#: * 分母少算：``total=1`` 而 ``named=3`` -> ``ratio=3.0``（逻辑不可能）仍 PASS
#:
#: 正确合同是**显式检查**而不是继续 clamp::
#:
#:     0 <= named <= total   且   accounting_errors == 0
#:
#: 违反即 ``accounting_status = inconsistent``（**不是** PASS）。
ACCOUNTING_OK = "ok"
ACCOUNTING_NOT_MEASURED = "not_measured"
ACCOUNTING_INCONSISTENT = "inconsistent"

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
    # ``replay``：**合成**行情源（`ReplayQuoteSource`）。它由 build_script()
    # 直接构造 Quote，turnover / volume_ratio / 五档 / 内外盘 / 流通盘**全都有
    # 真实数值**（实测样本 600519 turnover=0.0401）。
    #
    # IT-P1-EVAL-PUBLISH-001-R3：**此前这里没有 replay 条目**，于是
    # ``capabilities_for("replay")`` 落到 ``UNKNOWN_CAPABILITIES``（全 False）。
    # 后果不是"少报一个字段"，而是**规则整类静默**：
    #   * volume_burst 把 turnover 当硬依赖 -> 每只票都 blocked -> 放量告警
    #     一条都不出（实测本仓 selftest/演练模式下 volume_burst 从 6 条变 0 条）；
    #   * spirit_order 的成交类 pattern 同样被 BLOCKING_DEPS 拦掉。
    # 也就是说：**看板"演练模式"下演示的功能比真实源还少**，而用户看它正是
    # 为了确认功能存在。这与 IT-P1-CAPABILITY-001（Sina 占位零）是同一类
    # 错误——把"来源未知"当成"能力缺失"。
    #
    # 这张表的纪律是"以解析器/构造器实际赋了什么为准"：ReplayQuoteSource 喂的
    # Quote 字段齐全，故如实声明全 True。注意这只是**声明**，真实值仍由数据决定
    # （合成数据里 turnover 可能很小，那是数值不达标，走业务门槛而非能力缺失）。
    "replay": SourceCapabilities(
        source="replay", turnover=True, volume_ratio=True,
        depth_l1=True, depth_l5=True, outer_inner=True,
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
class SignalEvalStats:
    """单条 signal 的**逐 code 可评估性**账本（WP01 / IT-P1-CAPABILITY-003）。

    为什么必须有它 —— 旧口径 ``unavailable_capability = len(unavailable_codes)``
    是**所有规则写入的 code 集合并集**，随后 ``live_session`` 用
    "本轮是否有任意一个 unavailable code" 算 round ratio，并在 ratio=1 时
    解释成"整类规则整场都没被评估"。这在语义上不成立：

    * 5000 码里每轮只坏 1 个 → 真实可评估率 99.98%，旧口径却得 ratio=1.0
      并报 **whole-class FAIL**。分母错了，不是阈值高低的问题。
    * ``unavailable_capability`` 同时承载**硬阻断**（缺 turnover 且
      ``min_turnover>0``，该票真的不能判）与**可跳过的缺失**（缺
      ``volume_ratio`` 时 ``vr>0`` 为假即跳过门槛，规则**仍可能命中**）。
      一个状态名两种语义，必然污染 health。

    因此这里按 **signal**（不是 rule，也不是 round）建立账本，并把缺失拆成
    三种：:data:`BLOCKING` / :data:`ADVISORY` / :data:`NOT_REQUIRED`。

    **机械不变量**（由 ``check_invariants()`` 断言，测试逐条钉住）::

        evaluable + blocked_capability == considered
        evaluated_no_hit + hit_candidates == evaluable
        published <= hit_candidates
    """

    signal: str = ""
    #: 进入该 signal 判据的标的数（已排除停牌/无价这类前置不合格的）。
    considered: int = 0
    #: 判据完整、真的评估过的。
    evaluable: int = 0
    #: 因 capability 缺失而**无法**评估的。
    blocked_capability: int = 0
    #: 缺失了某项、但当前语义允许跳过，**仍在 evaluable 里**。
    #: 这是 ``blocked_capability`` 的补集的一部分，不是它的子集。
    advisory_missing: int = 0
    #: 评估了、没到门槛。
    evaluated_no_hit: int = 0
    #: 评估了、到门槛了（候选命中）。
    hit_candidates: int = 0
    #: 规则**选中**了多少条（= 规则内部 ``max_per_round`` 截断**之后**剩下的）。
    #:
    #: ⚠ 这个字段原名 ``published``，语义改了（IT-P1-EVAL-PUBLISH-001）。
    #: 旧名叫 "published" 是**名实不符**：它是在 Rule 返回**之前**写的，
    #: 而规则返回值之后还要经过 :meth:`AlertBus.accept` 的 key 去重与
    #: cooldown，再经 ``store.add_alert``。被 cooldown 丢掉的那些从未
    #: 交付给任何人，却已经计进 "published"。
    #:
    #: 云端 04:10 的机制反例：同一只票每 5 秒都满足 ``volume_burst``、
    #: cooldown=600 秒、跑 24 轮 ->
    #: ``rule_selected=24`` 而 ``AlertBus 真正接受 = 1``，**overcount 24×**。
    #: 若拿它当"事后收益标签"的分母，会把从未交付的候选算成已发布事件。
    rule_selected: int = 0
    #: 通过 ``AlertBus.accept``（key 去重 + cooldown）的条数。
    bus_accepted: int = 0
    #: 真正写进 Store、会被看板/通知器看到的条数。
    committed: int = 0
    #: {原因: 次数}，例如 ``{"turnover_not_provided": 3}``。
    blocked_reasons: dict[str, int] = field(default_factory=dict)
    #: 有界样本（最多 :data:`_BLOCKED_SAMPLE_CAP` 条）。
    blocked_sample: list[dict] = field(default_factory=list)

    @property
    def published(self) -> int:
        """**兼容别名** —— 等价于 :attr:`rule_selected`。

        保留它只为不打断既有调用方（``live_session`` 的 ``published`` 键、
        旧测试）。**新代码请用 ``rule_selected`` / ``committed``**，
        并清楚二者差在哪里。真正的"交付成功"看 :attr:`committed`。
        """
        return self.rule_selected

    @property
    def dropped_by_bus(self) -> int:
        """规则选中了、却被 AlertBus 去重/冷却丢掉的条数。"""
        return max(self.rule_selected - self.bus_accepted, 0)

    @property
    def committed_ratio(self) -> float | None:
        """``committed / hit_candidates``；没有命中时返回 ``None``。

        这是"选中的告警里到底有多少真的交付了"的交付率。用 ``None``
        而不是 ``1.0``/``0.0`` 表示 not_measured —— 与
        :attr:`evaluable_coverage` 同一纪律。
        """
        if self.hit_candidates <= 0:
            return None
        return self.committed / self.hit_candidates

    @property
    def evaluable_coverage(self) -> float | None:
        """``evaluable / considered``；**没有分母时返回 None，不返回 0.0**。

        这是刻意的：``not_measured`` 与 "0% 可评估" 是完全不同的结论，
        填 0 会把"没测"误报成"整类失效"。
        """
        if self.considered <= 0:
            return None
        return self.evaluable / self.considered

    def check_invariants(self) -> list[str]:
        """返回违反的机械不变量列表（空 = 全部成立）。

        交付阶段链（IT-P1-EVAL-PUBLISH-001）::

            hit_candidates >= rule_selected >= bus_accepted >= committed

        每一级都是上一级的**子集**：截断砍掉一部分、去重/冷却再砍一部分。
        这条链若被破坏，说明有人在错误的阶段记账 —— 正是旧 ``published``
        的问题（它在链的**第一**级就被写上，却被当成最后一级用）。
        """
        bad: list[str] = []
        if self.evaluable + self.blocked_capability != self.considered:
            bad.append(
                f"{self.signal}: evaluable({self.evaluable}) + "
                f"blocked_capability({self.blocked_capability}) != "
                f"considered({self.considered})")
        if self.evaluated_no_hit + self.hit_candidates != self.evaluable:
            bad.append(
                f"{self.signal}: evaluated_no_hit({self.evaluated_no_hit}) + "
                f"hit_candidates({self.hit_candidates}) != "
                f"evaluable({self.evaluable})")
        if self.rule_selected > self.hit_candidates:
            bad.append(
                f"{self.signal}: rule_selected({self.rule_selected}) > "
                f"hit_candidates({self.hit_candidates})")
        if self.bus_accepted > self.rule_selected:
            bad.append(
                f"{self.signal}: bus_accepted({self.bus_accepted}) > "
                f"rule_selected({self.rule_selected})")
        if self.committed > self.bus_accepted:
            bad.append(
                f"{self.signal}: committed({self.committed}) > "
                f"bus_accepted({self.bus_accepted})")
        if self.advisory_missing > self.evaluable:
            bad.append(
                f"{self.signal}: advisory_missing({self.advisory_missing}) > "
                f"evaluable({self.evaluable})")
        return bad

    def as_dict(self) -> dict[str, Any]:
        cov = self.evaluable_coverage
        return {
            "signal": self.signal,
            "considered": self.considered,
            "evaluable": self.evaluable,
            "blocked_capability": self.blocked_capability,
            "advisory_missing": self.advisory_missing,
            "evaluated_no_hit": self.evaluated_no_hit,
            "hit_candidates": self.hit_candidates,
            # 交付阶段链（IT-P1-EVAL-PUBLISH-001）——
            # 三级分开记，消费方才能分辨"被截断"与"被冷却"。
            "rule_selected": self.rule_selected,
            "bus_accepted": self.bus_accepted,
            "committed": self.committed,
            "dropped_by_bus": self.dropped_by_bus,
            "committed_ratio": (round(self.committed_ratio, 4)
                                if self.committed_ratio is not None else None),
            # 兼容键：等价于 rule_selected。**不要**再用它当"已发布"。
            "published": self.rule_selected,
            # None 表示 not_measured；不要用 0.0 冒充"整类失效"。
            "evaluable_coverage": (round(cov, 4) if cov is not None else None),
            "blocked_reasons": dict(self.blocked_reasons),
            "blocked_sample": list(self.blocked_sample[:_BLOCKED_SAMPLE_CAP]),
            "blocked_sample_truncated":
                len(self.blocked_sample) > _BLOCKED_SAMPLE_CAP,
        }


@dataclass(slots=True)
class SignalDeliveryStats:
    """单条 signal 的**交付**账本 —— 与可评估性账本**彻底分家**。

    IT-P1-DELIVERY-LEDGER-002（云端 2026-09-21 16:07 轮指认，我已独立复现）。

    **为什么必须分家**：``SignalEvalStats`` 回答的是"这条 signal 的判据
    有没有被逐 code 完整评估过"（``considered/evaluable/blocked/...``），
    只有**真正实现了逐 code evaluability instrumentation** 的规则才写得出。
    而交付账本回答的是完全不同的问题："规则选中的告警，有几条真的
    过了 AlertBus、真的写进了 Store"。**后者对任何会发 Alert 的规则都成立**，
    根本不需要该规则先做逐 code 统计。

    此前两者被错误地绑在同一个 registry 上：``Engine._mark_stage`` 的门禁是
    ``sig not in observation.signal_evals`` 就 return，于是**只有 2/7 个规则
    模块**（``volume_burst`` / ``spirit_order``）能记交付，
    真实回放 66 条 committed 告警里只有 6 条可对账 —— **覆盖率 9.1%**。
    其余 60 条并非"被 AlertBus 丢弃"，而是"进了 Store 却从未被计数"。

    **为什么不能靠给另外 5 条规则补 ``signal_id`` 解决**（已用真实代码证伪）：
    补 ``signal_id`` 只是**必要**条件，那条 ``sig not in signal_evals`` 门禁
    依然会拦下它们 —— ``signal_id`` 填了也照样 return，交付覆盖率纹丝不动。

    **为什么也不能让 Engine 在交付阶段自动建 ``SignalEvalStats`` 行**：
    那会造出 ``considered=0/evaluable=0/hit_candidates=0/rule_selected=0``
    却 ``bus_accepted=1/committed=1`` 的幽灵行，直接违反可评估性不变量，
    并把"没做逐 code 统计"**伪装成"0% 可评估"**。

    所以这里给出独立 sidecar：Engine 对所有带稳定 ``signal_id`` 的告警
    统一记账，**不再需要规则先维护 eval 行**。

    机械不变量::

        bus_accepted <= rule_selected - global_ignored
        committed    <= bus_accepted
    """

    signal: str = ""
    #: 规则返回的有效 Alert 条数。由 **Engine** 统一记，不靠每条规则自己写 ——
    #: 规则自己写就会再次出现"某条规则忘了维护交付账本"。
    rule_selected: int = 0
    #: 被全局 ignore 规则（如 filters/黑白名单）拦下的条数。
    global_ignored: int = 0
    #: 通过 ``AlertBus.accept``（key 去重 + cooldown）的条数。
    bus_accepted: int = 0
    #: 真正写进 Store、会被看板/通知器看到的条数。
    committed: int = 0

    @property
    def dropped_by_bus(self) -> int:
        """规则选中了、却没过 AlertBus（去重/冷却）的条数。"""
        return max(self.rule_selected - self.global_ignored
                   - self.bus_accepted, 0)

    @property
    def committed_ratio(self) -> float | None:
        """``committed / rule_selected``；没有选中时返回 ``None``（不返回 0）。"""
        if self.rule_selected <= 0:
            return None
        return self.committed / self.rule_selected

    def check_invariants(self) -> list[str]:
        bad: list[str] = []
        if self.bus_accepted > self.rule_selected - self.global_ignored:
            bad.append(
                f"{self.signal}: bus_accepted({self.bus_accepted}) > "
                f"rule_selected({self.rule_selected}) - "
                f"global_ignored({self.global_ignored})")
        if self.committed > self.bus_accepted:
            bad.append(
                f"{self.signal}: committed({self.committed}) > "
                f"bus_accepted({self.bus_accepted})")
        return bad

    def as_dict(self) -> dict[str, Any]:
        return {
            "signal": self.signal,
            "rule_selected": self.rule_selected,
            "global_ignored": self.global_ignored,
            "bus_accepted": self.bus_accepted,
            "committed": self.committed,
            "dropped_by_bus": self.dropped_by_bus,
            "committed_ratio": (None if self.committed_ratio is None
                                else round(self.committed_ratio, 4)),
        }


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
    #: 注意：WP03 之后**仍不存在**由 provider ts 驱动的陈旧**拒绝**路径 ——
    #: 合同三源 ``freshness_allowed=false``，``_admit_time()`` 对任何早于 now 的
    #: ``ts`` 都接受（只记 age 诊断），所以不要指望这里能反映"数据太旧"。
    #: **真正的陈旧观测在** :attr:`provider_stale_diagnosed` —— 那是诊断计数，
    #: 不是拒绝计数，两者不可混为一条曲线。
    #:
    #: （更正 IT-P2-STALE-COMMENT-STALE：上一版注释一边写"当前不存在陈旧拒绝
    #: 路径"，一边又描述 ``t_reject:stale``，与自身语义自相矛盾。准确说法是
    #: **诊断存在、拒绝不存在** —— 除非某源将来打开
    #: ``TIME_POLICY[*].freshness_allowed``。）
    future_rejected: int = 0
    out_of_order_rejected: int = 0
    #: WP04 / IT-P1-OBS-010：provider ts 被判定"过旧"的**诊断**条数
    #: （``age > STALE_TOLERANCE_SECONDS``），**不是**拒绝计数。
    #:
    #: 为什么必须有它：WP03 之后陈旧包**不再被拦**，陈旧这件事就只剩诊断
    #: 可见。若无此桶，"某源时间轴整体偏移"在观测里完全隐形。
    #:
    #: 与 :attr:`future_rejected` **必须分开** —— 上一轮的缺陷正是
    #: ``stale_rejected`` 被 alias 成 ``future_rejected``，两条互斥的曲线
    #: 同值，谁也无法分辨"数据太旧"还是"数据来自未来"。
    provider_stale_diagnosed: int = 0
    #: 其中被诊断的代码集合（不是次数）。
    provider_stale_diagnosed_codes: set[str] = field(default_factory=set)
    #: 各 route 的陈旧诊断次数（``route -> 次数``）。
    provider_stale_by_route: dict[str, int] = field(default_factory=dict)
    # 因能力缺失而无法评估的**代码集合**（不是次数）。同一只票可能同时缺
    # turnover 与 volume_ratio，按次数记会把"1 只票不可评估"夸大成 2，
    # 覆盖率数字就失真了。decisions 里仍逐 (code, field) 留明细。
    unavailable_codes: set[str] = field(default_factory=set)
    decisions: list[ObservationDecision] = field(default_factory=list)
    #: WP01 / IT-P1-CAPABILITY-003：逐 **signal** 的可评估性账本。
    #:
    #: 键的形状是 ``signal_key``（如 ``volume_burst``、
    #: ``spirit_order:institution_buy``），不是 rule 名也不是轮号 ——
    #: 只有到 signal 粒度才可能回答"这条规则到底有没有被验证过"。
    signal_evals: dict[str, SignalEvalStats] = field(default_factory=dict)
    #: IT-P1-DELIVERY-LEDGER-002：逐 signal 的**交付**账本（独立 registry）。
    #:
    #: 与 ``signal_evals`` **必须分开**：
    #: * ``signal_evals`` = 规则所有，回答"逐 code 判据是否被完整评估"，
    #:   只有实现了 instrumentation 的规则才写；
    #: * ``delivery_stats`` = **Engine 所有**，回答"选中的告警有没有真交付"，
    #:   对任何会发 Alert 的规则都成立。
    #:
    #: 绑在一起时只有 2/7 规则能对账（覆盖率 9.1%）；分开后 7/7 闭合，
    #: 且不会在 ``signal_evals`` 里造出会污染 ``evaluable_coverage`` 分母的
    #: 幽灵行。
    delivery_stats: dict[str, SignalDeliveryStats] = field(default_factory=dict)
    #: 逐 signal 的"已考虑" code 集合，用于**去重** counting。
    #:
    #: 为什么需要：``evaluate()`` 可能对同一 code 在一轮里被调用多次（多份
    #: snapshot/多次边沿检查），重复 mark 会把 ``considered`` 放大到超过真实
    #: 标的数，机械不变量就假成立了。这里只保留**本轮内**的成员，随
    #: RoundObservationSet 一起被替换，故不会无界增长。
    _seen_codes: dict[str, set[str]] = field(default_factory=dict)
    #: 逐 signal 的"已落桶" code 集合（blocked 或 evaluated，二者互斥）。
    #:
    #: 与 ``_seen_codes`` **必须分开**：先 ``mark_considered`` 再
    #: ``mark_evaluated`` 是完全合法的调用序列（规则先登记、再判定），
    #: 若让 ``mark_evaluated`` 依赖 ``mark_considered`` 的返回值提前返回，
    #: 该票就永远进不了 evaluable —— ``considered`` 与
    #: ``evaluable+blocked`` 的不变量随即破裂。
    _final_codes: dict[str, set[str]] = field(default_factory=dict)
    #: 本 Engine 本轮**真正写进 Store 的第一方告警总数**（分母）。
    #:
    #: 为什么要 Engine 自己数：``delivery_accounting_coverage`` 的分母必须是
    #: "真实 committed 总数"，而它**不等于**任何账本里的 committed 之和 ——
    #: 账本只统计带 ``signal_id`` 的告警。用账本之和当分母会让覆盖率
    #: 恒等于 1.0（分子分母同源），那正是本缺陷能藏这么久的原因。
    committed_alerts_total: int = 0
    #: 交付账本更新过程中被吞掉的异常次数（IT-P1-DELIVERY-FALSE-GREEN-001）。
    #:
    #: 为什么必须单独计数：旧代码里分母自增包在
    #: ``try: ... except Exception: pass`` 中 —— 一旦抛异常，分母**静默不涨**，
    #: 而门禁 ``max(total - named, 0)`` 会把负差夹成 0，
    #: 于是"账本已经坏了"被读成 PASS（实测 total=0/named=66 -> 门禁 0）。
    #: 吞异常本身**可以**保留（可观测性不能打断告警主链路），
    #: 但**必须留下痕迹**，否则就是假绿。
    accounting_errors: int = 0

    # ------------------------------------------------------------------
    # WP01 记账 API（有界：只存计数 + 少量样本）
    # ------------------------------------------------------------------
    def eval_stats(self, signal: str) -> SignalEvalStats:
        """取（必要时新建）某 signal 的账本。"""
        key = str(signal or "")
        st = self.signal_evals.get(key)
        if st is None:
            st = SignalEvalStats(signal=key)
            self.signal_evals[key] = st
            self._seen_codes[key] = set()
            self._final_codes[key] = set()
        return st

    def mark_considered(self, signal: str, code: str) -> bool:
        """登记一只标的进入某 signal 的判据。**同一轮同一 code 只记一次**。

        返回是否为首次登记。
        """
        self.eval_stats(signal)
        seen = self._seen_codes.setdefault(str(signal or ""), set())
        key = str(code or "")
        if key in seen:
            return False
        seen.add(key)
        self.signal_evals[str(signal or "")].considered += 1
        return True

    def _claim_final(self, signal: str, code: str) -> bool:
        """该 code 在本轮是否**首次**落进终态桶（blocked/evaluated）。

        返回 False 表示它已经被记过（blocked 或 evaluated），调用方必须
        直接返回，不得重复计数。blocked 与 evaluated 互斥且先到先得。
        """
        self.eval_stats(signal)
        fin = self._final_codes.setdefault(str(signal or ""), set())
        key = str(code or "")
        if key in fin:
            return False
        fin.add(key)
        self.mark_considered(signal, code)      # 保证 considered 已计入
        return True

    def mark_blocked(self, signal: str, code: str, reason: str,
                     capability: str = "") -> None:
        """该 code 因 capability 缺失**不能评估**（进 blocked 分母）。"""
        if not self._claim_final(signal, code):
            return
        st = self.eval_stats(signal)
        st.blocked_capability += 1
        r = str(reason or "capability_missing")
        st.blocked_reasons[r] = st.blocked_reasons.get(r, 0) + 1
        if len(st.blocked_sample) <= _BLOCKED_SAMPLE_CAP:
            st.blocked_sample.append({
                "code": str(code or ""), "reason": r,
                "capability": str(capability or ""),
            })

    def mark_advisory_missing(self, signal: str, code: str, reason: str,
                              capability: str = "") -> None:
        """该 code 缺了某项，但当前语义**允许跳过**，仍可评估。

        注意它**不**把 code 记成 blocked，也**不**落终态桶 —— 调用方随后
        仍应 ``mark_evaluated``，该票才进 evaluable。
        """
        st = self.eval_stats(signal)
        st.advisory_missing += 1
        if not capability:
            return
        r = f"advisory/{reason or 'advisory'}:{capability}"
        # advisory 的明细与 blocked 分开记，避免污染 blocked_reasons。
        st.blocked_reasons[r] = st.blocked_reasons.get(r, 0) + 1

    def mark_evaluated(self, signal: str, code: str, hit: bool) -> None:
        """该 code 判据完整、真的评估过了。``hit`` 表示是否到门槛。"""
        if not self._claim_final(signal, code):
            return
        st = self.eval_stats(signal)
        st.evaluable += 1
        if hit:
            st.hit_candidates += 1
        else:
            st.evaluated_no_hit += 1

    def mark_published(self, signal: str, code: str) -> None:
        """规则**选中**了该 code 的命中并让它进入返回值。

        ⚠ 语义已改（IT-P1-EVAL-PUBLISH-001）：这里记的是
        :attr:`SignalEvalStats.rule_selected`，**不是**"已发布"。
        真正的交付由 :meth:`mark_bus_accepted` / :meth:`mark_committed`
        在 Engine 走完 ``AlertBus.accept`` 与 ``store.add_alert`` 后记。

        保留方法名 ``mark_published`` 是为了不打断既有规则调用点
        （``volume_burst`` / ``spirit_order`` 各一处）；它们记的本来
        就只是"规则选中"，所以调用点无需改动、语义反而更正。
        """
        st = self.eval_stats(signal)
        st.rule_selected += 1

    def mark_bus_accepted(self, signal: str, code: str) -> None:
        """该 code 的告警通过了 ``AlertBus.accept``（key 去重 + cooldown）。"""
        st = self.eval_stats(signal)
        st.bus_accepted += 1

    def mark_committed(self, signal: str, code: str) -> None:
        """该 code 的告警真正写进了 Store（会被看板/通知器看到）。"""
        st = self.eval_stats(signal)
        st.committed += 1

    # ------------------------------------------------------------------
    # IT-P1-DELIVERY-LEDGER-002：独立交付账本（Engine 所有）
    # ------------------------------------------------------------------
    def delivery_stats_for(self, signal: str) -> SignalDeliveryStats:
        """取（必要时新建）某 signal 的**交付**账本。

        与 :meth:`eval_stats` 的关键差别：这里新建条目**不会**污染
        ``signal_evals``，因此 ``evaluable_coverage`` 的分母不受影响 ——
        一个没做逐 code 统计的规则照样能记交付，而它的可评估性如实保持
        ``not_measured``（``None``），不会被伪造成 "0% 可评估"。
        """
        key = str(signal or "")
        st = self.delivery_stats.get(key)
        if st is None:
            st = SignalDeliveryStats(signal=key)
            self.delivery_stats[key] = st
        return st

    def mark_delivery_selected(self, signal: str, code: str, n: int = 1) -> None:
        """规则返回了一条有效 Alert（**Engine** 统一记，不靠规则自己写）。"""
        self.delivery_stats_for(signal).rule_selected += int(n)

    def mark_delivery_ignored(self, signal: str, code: str, n: int = 1) -> None:
        """该告警被全局 ignore 规则拦下（没进 AlertBus）。"""
        self.delivery_stats_for(signal).global_ignored += int(n)

    def mark_delivery_bus_accepted(self, signal: str, code: str) -> None:
        """该告警通过了 ``AlertBus.accept``。"""
        self.delivery_stats_for(signal).bus_accepted += 1

    def mark_delivery_committed(self, signal: str, code: str) -> None:
        """该告警真正写进了 Store。"""
        self.delivery_stats_for(signal).committed += 1

    def check_delivery_invariants(self) -> list[str]:
        """全部 signal 的交付不变量自检，返回违规列表（空 = 全成立）。"""
        out: list[str] = []
        for st in self.delivery_stats.values():
            out.extend(st.check_invariants())
        return out

    def _delivery_accounting_dict(self) -> dict[str, Any]:
        """交付账本的对外视图（含自洽性判定）。

        ``first_party_committed_without_signal_id`` 仍保留（消费方已在用），
        但它**单独不足以判定健康** —— 必须同时看 ``accounting_status``。
        门禁值用 ``max(..., 0)`` 是历史兼容行为；负差的情形由
        ``accounting_status=inconsistent`` 明确报出，不再被悄悄夹掉。
        """
        named = sum(st.committed for st in self.delivery_stats.values())
        total = self.committed_alerts_total
        cov = self.delivery_accounting_coverage()
        return {
            # ⚠ IT-P1-DELIVERY-GATE-PEROUND-001：下面这些数是**本轮**（逐轮）
            # 口径，因为 RoundObservationSet 是逐轮对象，Store 每轮用新的
            # as_dict() 覆盖最近 observation。会话末尾若末轮无告警，
            # /api/status 上会看到 committed_alerts_total=0 / signed_ratio=None
            # —— 那是**正确的 not_measured**，不是"账本坏了"。
            # 读取方必须用 `_scope` 判断口径，不要把它当会话累计值。
            "_scope": "last_round",
            "_scope_note": (
                "逐轮口径。会话累计请读 delivery_session（由 live_session/"
                "Store 维护），或对逐轮值跨轮求和。"),
            "committed_alerts_total": total,
            "committed_with_signal_id": named,
            "signed_ratio": None if cov is None else round(cov, 4),
            FIRST_PARTY_UNSIGNED_GATE_REASON: max(total - named, 0),
            # --- IT-P1-DELIVERY-FALSE-GREEN-001 ---------------------------
            "accounting_status": self.delivery_accounting_status(),
            "accounting_errors": self.accounting_errors,
            "accounting_problems": self.check_delivery_accounting(),
        }

    def delivery_accounting_coverage(self) -> float | None:
        """``带 signal_id 的真实 committed / 全部 committed``（**全局门禁**）。

        见 :data:`FIRST_PARTY_UNSIGNED_GATE_REASON`。没有 committed 时返回
        ``None``（not_measured），**不是** 1.0 —— 空集合不该被读成"100% 合规"。

        ⚠ **不要**只用这个值判定账本健康：当分母被少算时它会 > 1.0
        （逻辑不可能），必须配合 :meth:`delivery_accounting_status`。
        """
        named = sum(st.committed for st in self.delivery_stats.values())
        total = self.committed_alerts_total
        if total <= 0:
            return None
        return named / total

    def delivery_accounting_status(self) -> str:
        """交付账本的自洽性判定（IT-P1-DELIVERY-FALSE-GREEN-001）。

        返回 :data:`ACCOUNTING_OK` / :data:`ACCOUNTING_NOT_MEASURED` /
        :data:`ACCOUNTING_INCONSISTENT` 之一。

        判据（**显式检查，不 clamp**）::

            accounting_errors == 0
            0 <= named <= total（total 为 0 时要求 named 也为 0）

        为什么不能只看"门禁为 0"：门禁用 ``max(total - named, 0)``，
        负差被夹成 0，于是"分母被吞掉"（total=0/named=66）会显示 PASS。
        这是**假绿**，比假红危险得多 —— 假红会有人来查，假绿不会。
        """
        named = sum(st.committed for st in self.delivery_stats.values())
        total = self.committed_alerts_total
        if self.accounting_errors > 0:
            return ACCOUNTING_INCONSISTENT
        if total == 0 and named == 0:
            return ACCOUNTING_NOT_MEASURED
        if named > total:
            # 分母比分子还小 —— 只能是分母少算或被吞，逻辑上不可能。
            return ACCOUNTING_INCONSISTENT
        return ACCOUNTING_OK

    def check_delivery_accounting(self) -> list[str]:
        """返回记账不自洽的具体原因（空 = 自洽）。"""
        bad: list[str] = []
        named = sum(st.committed for st in self.delivery_stats.values())
        total = self.committed_alerts_total
        if self.accounting_errors > 0:
            bad.append(
                f"账本更新吞掉 {self.accounting_errors} 次异常 —— 分母可能少算")
        if named > total:
            bad.append(
                f"named_committed({named}) > committed_alerts_total({total}) —— "
                f"分母被少算或自增被吞（ratio 会 >1.0，逻辑不可能）")
        return bad

    def check_signal_invariants(self) -> list[str]:
        """全部 signal 的机械不变量自检，返回违规列表（空 = 全成立）。"""
        out: list[str] = []
        for st in self.signal_evals.values():
            out.extend(st.check_invariants())
        return out

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
        """**已弃用** —— 新代码用 :attr:`future_rejected`（超前）或
        :attr:`provider_stale_diagnosed`（陈旧诊断）。

        WP04 / IT-P1-OBS-010：上一轮这里直接 ``return self.future_rejected``，
        于是"陈旧"与"未来"两个**互斥**的桶永远同值 —— 消费方无法分辨。
        现在返回**陈旧诊断数**（该名字真正想表达的量），并在 ``as_dict()``
        里标注两个键的来源，避免继续把两条曲线混为一谈。

        注意它**不是**拒绝计数：WP03 之后陈旧包不再被拦。
        """
        return self.provider_stale_diagnosed

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
            # --- WP04 / IT-P1-OBS-010：陈旧诊断与"未来"必须是两条曲线 ---------
            # 上一轮 stale_rejected 被 alias 成 future_rejected，两个互斥的桶
            # 永远同值。现在分别导出，并把陈旧说清是**诊断**不是拒绝。
            "provider_stale_diagnosed": self.provider_stale_diagnosed,
            "provider_stale_diagnosed_codes": sorted(
                self.provider_stale_diagnosed_codes)[:50],
            "provider_stale_by_route": dict(self.provider_stale_by_route),
            "_schema_note": {
                "provider_stale_diagnosed": "诊断计数（age 超线），**不是**拒绝",
                "future_rejected": "超前拒绝计数；与陈旧互斥",
                "stale_rejected": "已弃用的别名，指向 provider_stale_diagnosed",
            },
            "unavailable_capability": self.unavailable_capability,
            # --- WP01 / IT-P1-CAPABILITY-003：逐 signal 可评估性 ---------------
            # 旧口径（unavailable_capability + round ratio）**不能**回答
            # "这条 signal 到底被验证过没有"。这里给出真正的分母与分子。
            "signal_evaluability": {
                k: v.as_dict() for k, v in sorted(self.signal_evals.items())
            },
            # --- IT-P1-DELIVERY-LEDGER-002：独立的交付账本 ----------------------
            # 与 signal_evaluability **分开导出**，消费方不得再把两者混为一谈。
            # 前者回答"判据有没有被逐 code 评估"，后者回答"选中的告警有没有
            # 真交付"；只有 2/7 规则能做前者，但 7/7 都能做后者。
            "signal_delivery": {
                k: v.as_dict() for k, v in sorted(self.delivery_stats.items())
            },
            # 全局门禁：真实 committed 总数、带 signal_id 的数、以及覆盖率。
            # ⚠ 分母 ``committed_alerts_total`` 由 Engine 独立数，**不是**
            # 账本 committed 之和 —— 否则分子分母同源，覆盖率恒 1.0。
            #
            # IT-P1-DELIVERY-FALSE-GREEN-001：光有 gate=0 **证明不了**账本健康。
            # 这里额外给出 ``accounting_status`` 与 ``accounting_problems``，
            # 让"账本坏了但门禁仍是 0"（分母被吞 / 少算）无法再冒充 PASS。
            "delivery_accounting": self._delivery_accounting_dict(),
            "_deprecated": {
                "unavailable_capability":
                    "IT-P1-CAPABILITY-003：这是所有规则写入的 code 并集大小，"
                    "**不是**任何 signal 的可评估性。仍然导出以兼容旧消费方，"
                    "但**不得**再作为 whole-class health 的真值 —— "
                    "请读 signal_evaluability[*].evaluable_coverage。",
            },
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
