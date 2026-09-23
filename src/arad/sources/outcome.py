"""Snapshot Outcome Contract v4 —— **源无关**的快照取数结果（纯数据 + 集合代数）。

为什么必须存在（云端 `2026-09-23_12-37-02_JST_AGENT_TASK.md` WP01 /
`2026-09-23_16-11-07_JST_AGENT_TASK.md` WP01）：

修前每个 source 的 ``snapshots()`` 只交出一个 ``list[Quote]``，
**"provider 没返回这个代码" 与 "返回了但数据不可用" 在出口处已经无法区分** ——
Sina / Eastmoney 的 parser 把不可用行 ``continue`` 掉了，
Engine 只能看到"这个 code 不在 Quote 列表里"，于是把它记成 ``unknown_missing``。

后果不是学术问题：**默认链路是 Tencent 主 + Sina 备**，
同一个 raw 事实（provider 明确返回了某代码、但价格不可用）
在 Tencent 上落进 ``rejected_quality``、在 Sina 上落进 ``unknown_missing``。
**一次 failover 就改变账本语义**，而账本是研究/模型的质量标签来源。

契约核心是**集合代数**（任务书 §44-57，逐字）：

```text
R = requested unique identities
P = raw observed identities ∩ R
Q = usable quote identities ∩ R

returned          = |P|
rejected_quality  = P - Q
unknown_missing   = R - P
unexpected        = raw - R
```

**`returned` 绝不等于 raw row 数量，也绝不等于未过滤的 raw unique 数。**
两种被点名的错误实现（审计 §6 §458-488）：

* 错误 A：``returned = 4`` 而 ``requested = 3`` —— 立刻越界；
* 错误 B：用未过滤的 raw unique 数当分子 ——
  表面 ``coverage = 100%``，但**没回来的 600003 被不相关的 600999 补齐了分子**。

``raw_presence_known`` 是这套代数的**诚实开关**：
只有它为 True 时，``unknown_missing`` 才允许被称作
"provider 确实没返回"。老/未迁移的 source 必须声明
``raw_presence_known=False`` + ``provenance=quote_projection_legacy``，
**不得把 parser 丢掉的行重新包装成"provider 完全没返回"**。
"""

from __future__ import annotations

from typing import Any, Iterable, NamedTuple

#: 老（未迁移）source 的 provenance 字面量 —— 审计指定的名字，**不可改**。
PROVENANCE_LEGACY = "quote_projection_legacy"
#: 已迁移 source 的 provenance 字面量（审计只钉了 legacy 那个，
#: 这个由我选定并在测试里断言）。
PROVENANCE_EXACT = "exact_raw_presence"


class SnapshotFetchResult(NamedTuple):
    """一次快照取数的**完整**结果（源无关）。

    字段与审计给定的 11 项接口逐字对应（参见模块 docstring）。

    ::

        route                        SourceManager 路由（"stocks"/"index"/"universe"）
        source                       实际服务源名（"tencent"/"sina"/"eastmoney"）
        requested_keys               R：调用方请求的**规范化唯一**身份集
        raw_presence_known           raw 身份是否**真的**被观察到
        raw_returned_requested_keys  P = raw ∩ R
        quotes                       Q：可用 Quote（已与 R 相交）
        rejected_quality_keys        P - Q
        unexpected_raw_keys          raw - R
        duplicate_raw_keys           raw 中重复出现的键
        raw_rows                     raw 原始行对象（本批次）
        provenance                   ``PROVENANCE_LEGACY`` 或 ``PROVENANCE_EXACT``
    """

    route: str
    source: str
    requested_keys: frozenset[str]
    raw_presence_known: bool
    raw_returned_requested_keys: frozenset[str]
    quotes: tuple[Any, ...]
    rejected_quality_keys: frozenset[str]
    unexpected_raw_keys: frozenset[str]
    duplicate_raw_keys: frozenset[str]
    raw_rows: tuple[Any, ...]
    provenance: str

    # --- 派生量：**只有**这一份实现，不许各处重算 --------------------------
    @property
    def requested(self) -> int:
        return len(self.requested_keys)

    @property
    def returned(self) -> int:
        """``|P|`` —— 这是**唯一**合法的 returned 定义。

        ``raw_presence_known=False`` 时它退化为 ``|Q|``：
        没有 raw 证据时只能从可用 Quote 反推，此时 returned 与 admitted
        必然相等，**消费者必须看 ``raw_presence_known`` 才知道能不能信**。
        """
        if self.raw_presence_known:
            return len(self.raw_returned_requested_keys)
        return len(self.quotes)

    @property
    def admitted(self) -> int:
        return len(self.quotes)

    @property
    def unknown_missing_keys(self) -> frozenset[str]:
        """``R - P``。

        ``raw_presence_known=False`` 时这个集合**不是**"provider 没返回"，
        只是"没在 Quote 投影里出现" —— 调用方必须据此降级措辞。
        """
        return self.requested_keys - self.raw_returned_requested_keys

    @property
    def is_exact(self) -> bool:
        return self.raw_presence_known

    @property
    def coverage(self) -> float | None:
        """``|Q| / |R|``；``R`` 为空时返回 ``None``（不是 0.0，也不是 1.0）。"""
        if not self.requested_keys:
            return None
        return len(self.quotes) / len(self.requested_keys)

    def identity_holds(self) -> bool:
        """机械恒等：``R = 互斥并集(admitted, rejected_quality, unknown_missing)``。

        这是本契约**唯一**的自洽性断言。任何一处集合算错都会让它为 False
        —— 包括"把 unexpected/duplicate 混进分子"的错误 B。
        """
        q = frozenset(_key_of(x) for x in self.quotes)
        req = self.requested_keys
        p = self.raw_returned_requested_keys
        admit = q & req
        qual = (p - q) & req
        missing = req - p
        return (admit | qual | missing) == req and not (
            admit & qual or admit & missing or qual & missing)

    def as_dict(self) -> dict[str, Any]:
        """可 JSON 化的产物（键集与审计回传要求一致）。"""
        return {
            "route": self.route,
            "source": self.source,
            "provenance": self.provenance,
            "raw_presence_known": self.raw_presence_known,
            "requested": self.requested,
            "returned": self.returned,
            "admitted": self.admitted,
            "coverage": self.coverage,
            "requested_keys": sorted(self.requested_keys),
            "raw_returned_requested_keys":
                sorted(self.raw_returned_requested_keys),
            "unknown_missing": sorted(self.unknown_missing_keys),
            "rejected_quality": sorted(self.rejected_quality_keys),
            "unexpected_raw_keys": sorted(self.unexpected_raw_keys),
            "duplicate_raw_keys": sorted(self.duplicate_raw_keys),
            "raw_rows": len(self.raw_rows),
            "identity_holds": self.identity_holds(),
        }


def _key_of(quote: Any) -> str:
    """从 Quote 取身份键 —— 与 ``normalize_request`` 同轴。"""
    code = getattr(quote, "code", None)
    return str(code) if code is not None else ""


def normalize_request(codes: Iterable[str], *,
                      normalize: Any = None) -> tuple[str, ...]:
    """请求集 **R** 的规范化：**保序去重**（不是排序）。

    保序很重要：``P``/``Q`` 的排序若与请求顺序无关，
    报错时就无法指回"调用方第几个请求丢了"。
    """
    out: list[str] = []
    seen: set[str] = set()
    for c in codes:
        k = normalize(c) if normalize is not None else c
        if k is None:
            continue
        k = str(k)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(k)
    return tuple(out)


def merge_outcomes(parts: Iterable[SnapshotFetchResult]) -> SnapshotFetchResult:
    """把同一路由的多个批次/分页结果**并**成一个（腾讯按 chunk、东财按 page）。

    合并规则必须与"一次请求"语义一致：

    * ``requested_keys`` = **并集**（每个批次问的是同一个 R 的不同子集，
      或者同一 R —— 两种情况下并集都是正确的总请求集）；
    * ``raw_returned_requested_keys`` / ``rejected_quality_keys`` = 并集
      （一个键只要在**任一**批次里 raw 出现过，就整体算 raw present）；
    * ``duplicate_raw_keys`` = 并集（跨批次的重复同样是异常）；
    * ``quotes`` = 按 code **去重后**保留**首个**出现的（与各源
      ``snapshots()`` 里 ``_dedupe`` / ``merged[code] = q`` 的既有口径一致）；
    * ``raw_presence_known`` = **全为** True 才为 True
      （任何一个批次没有 raw 证据，整体就不能声称 exact ——
       这正是 D14 的"unknown 是单位元、合并必须单调"）；
    * ``provenance`` = 只要有一个 legacy，整体就是 legacy。

    空列表 -> 返回一个空 outcome（``R`` 为空、``coverage`` 为 ``None``）。
    """
    parts = list(parts)
    if not parts:
        return build_outcome(route="", source="", normalized_request=(),
                             raw_keys=(), quotes=(), raw_presence_known=False,
                             provenance=PROVENANCE_LEGACY)

    req: set[str] = set()
    raw_present: set[str] = set()
    quality: set[str] = set()
    unexpected: set[str] = set()
    dups: set[str] = set()
    raw_rows: list[Any] = []
    seen_q: set[str] = set()
    quotes: list[Any] = []
    known = True
    prov = PROVENANCE_EXACT
    # 跨批次出现次数：`duplicate_raw_keys` 必须**跨批次**也算数
    # （只并各批次的内部重复会漏掉"同一个码被两个批次都返回"）。
    # 每批次的 raw 集合 = P ∪ unexpected（因为 raw ∩ R = P，raw − R = unexpected）。
    occ: dict[str, int] = {}

    for p in parts:
        req |= p.requested_keys
        raw_present |= p.raw_returned_requested_keys
        quality |= p.rejected_quality_keys
        unexpected |= p.unexpected_raw_keys
        dups |= p.duplicate_raw_keys
        raw_rows.extend(p.raw_rows)
        known = known and p.raw_presence_known
        if p.provenance != PROVENANCE_EXACT:
            prov = PROVENANCE_LEGACY
        for k in (p.raw_returned_requested_keys | p.unexpected_raw_keys):
            occ[k] = occ.get(k, 0) + 1
        for q in p.quotes:
            k = _key_of(q)
            if k not in seen_q:
                seen_q.add(k)
                quotes.append(q)

    dups |= {k for k, c in occ.items() if c > 1}

    if not known:
        # 有任一批次没有 raw 证据 -> 整体降级为 legacy，且不得声称 P。
        return build_outcome(
            route=parts[0].route, source=parts[0].source,
            normalized_request=tuple(sorted(req)), raw_keys=(), quotes=quotes,
            raw_rows=raw_rows, raw_presence_known=False,
            provenance=PROVENANCE_LEGACY)

    out = build_outcome(
        route=parts[0].route, source=parts[0].source,
        normalized_request=tuple(sorted(req)), raw_keys=sorted(raw_present),
        quotes=quotes, raw_rows=raw_rows, raw_presence_known=True,
        provenance=prov)
    # 诊断集按并集直接给出（不重新推导 —— 重新推导会把
    # "跨批次重复" 这类信息丢掉）。
    return out._replace(
        rejected_quality_keys=frozenset(quality),
        unexpected_raw_keys=frozenset(unexpected),
        duplicate_raw_keys=frozenset(dups),
    )


def build_outcome(
    *,
    route: str,
    source: str,
    normalized_request: tuple[str, ...],
    raw_keys: Iterable[str],
    quotes: Iterable[Any],
    raw_rows: Iterable[Any] = (),
    raw_presence_known: bool = True,
    provenance: str = PROVENANCE_EXACT,
) -> SnapshotFetchResult:
    """把 (raw 身份, 可用 Quote) 组装成 v4 结果 —— **集合代数只在这里算一次**。

    修前每一种 source 各自在 ``snapshots()`` 里写一遍"过滤到请求集"，
    同一语义实现多份，于是任何一处写错都只在某一个源上暴露
    （这正是本仓库反复出现的 bug 类"同一语义实现两次"）。
    """
    req = frozenset(normalized_request)
    raw_seq = [str(k) for k in raw_keys if k]
    raw_set = frozenset(raw_seq)
    # duplicate = 出现次数 > 1 的键（**不**限请求集内：一个没请求过的代码
    # 被返回两次，同样是 provider 侧的异常，不该被静默吞掉）。
    seen: set[str] = set()
    dups: set[str] = set()
    for k in raw_seq:
        if k in seen:
            dups.add(k)
        seen.add(k)

    q_list = list(quotes)
    q_keys = frozenset(_key_of(x) for x in q_list)
    # **Q 必须与 R 相交** —— "只返回被请求的代码"是调用方契约，
    # 不能靠 caller 事后过滤（那正是 sina/eastmoney parser 注释里写的
    # "服务端可能回带未请求的行"）。
    q_in_req = [x for x in q_list if _key_of(x) in req]
    p = raw_set & req

    if not raw_presence_known:
        # 老路径：没有 raw 证据，**不得**凭空宣称 P。
        # 用 Quote 投影当 P 的**下界**，并把这一点编码进 provenance。
        p = frozenset(_key_of(x) for x in q_in_req)
        provenance = PROVENANCE_LEGACY

    return SnapshotFetchResult(
        route=route,
        source=source,
        requested_keys=req,
        raw_presence_known=bool(raw_presence_known),
        raw_returned_requested_keys=p,
        quotes=tuple(q_in_req),
        rejected_quality_keys=(p - q_keys) & req,
        unexpected_raw_keys=raw_set - req,
        duplicate_raw_keys=frozenset(dups),
        raw_rows=tuple(raw_rows),
        provenance=provenance,
    )
