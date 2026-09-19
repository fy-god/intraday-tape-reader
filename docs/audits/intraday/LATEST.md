# 最新审计

最新完整报告：[`2026-09-20_04-05-31_JST.md`](./2026-09-20_04-05-31_JST.md)

本轮本地 Agent 任务书：[`2026-09-20_04-05-31_JST_AGENT_TASK.md`](./2026-09-20_04-05-31_JST_AGENT_TASK.md)

## 版本与证据边界

- 仓库与分支：`fy-god/intraday-tape-reader` / `main`。
- 审计时间：2026-09-20 04:05 JST。
- `reviewed_source_sha`：`a3cfd567fb07e414c1d553e5fc478e50892abea8`。
- 产品源码树：`ca2b165c0f5ae3c3b7e878eba930e11775161c58`。
- 本轮报告提交：`e175741c798a4d314beacb7747e70b1f4e7b3ee5`。
- 本轮 Agent 任务书提交：`1928c167cd103045c529e182539261cdcc4fcac4`。
- 本轮自动化沙箱 `git clone` 因 DNS 失败，因此**未独立重跑全仓 pytest**；产品提交中 `1114 passed` 仅作为历史机器记录引用。
- 真实多日连续 A 股训练语料仍未挂载；本轮研究结果为 synthetic capability robustness，不是实盘 Precision/Recall 或收益。

## 本轮最高优先发现

### `IT-P0-002-R1` — P0，时间准入没有闭合到规则 Snapshot

`a3cfd56` 已修 `EngineState.window()` 的未来点上界，并在 `EngineState.update()` 中拒绝远未来 provider 时间；但 `Engine.poll_once()` 在 `state.update(quotes, now)` 之后仍从**原始 `quotes`**重建：

```python
returned = {q.code: q for q in quotes if q.price > 0}
eligible = {c: q for c, q in returned.items() if ...}
```

所以已被 State 拒绝的 far-future quote 仍可能进入同轮规则 Snapshot，并被计入 `RoundObservationSet.admitted`。

同时，out-of-order quote 的 history 虽不追加，但当前 `EngineState.update()` 在乱序判断之前已经写 `state.quotes[q.code] = q`，最后仍写 `last_price[q.code] = q.price`。本轮机制复现：10.0 → 10.5 后迟到 8.0，history 仍 `[10.0,10.5]`，但 latest cache、last_price 与 rule Snapshot 均倒退到 8.0。

下一版必须建立单一 `admitted_current`，让 state mutation、rule Snapshot、index path、RoundObservationSet 与研究特征全部消费同一准入结果。

## Capability / observability 状态

`IT-P1-CAPABILITY-001` 应视为**阶段一部分修复**：`SourceCapabilities`、`ObservationDecision`、`RoundObservationSet` 和 `VolumeBurstRule` 的 unavailable 记账已落地，但 observation 仍是 `poll_once()` 局部对象，没有进入 Store/status/live_session。

`IT-P2-OBS-001` 因而继续开放：`tools/live_session.py` 仍以累计 `len(state.quotes)` 和 `len(engine._codes)` 代表行情/股票池规模，没有 requested/returned/admitted/time-reject/per-rule capability coverage。

## 继续开放

- `IT-P0-001`：14:57–15:00 仍归 `AFTERNOON` / continuous；SSE 与 SZSE 股票均为收盘集合竞价。
- `IT-P1-SOURCE-EMPTY-001`：指定代码 soft-partial 缺 requested-vs-returned + targeted fallback 合同。
- `IT-P1-WINDOW-001`：change-only history 仍删除横盘但新鲜的窗口覆盖证据。
- `IT-P1-LIMIT-001`：封单资格金额检查前先写 `sealed`，跨门槛首次合格事件仍可被吞。
- `IT-P1-006-R1`：Eastmoney `max_pages` 截断仍可 `complete=True`。
- `IT-P1-008/009/003/004`：SSE 补账、慢客户端 drop-oldest、series 容量、Tencent TLS fallback。
- `IT-P2-REPORT-001`：同一产品提交的完整报告写 `1103 passed`，commit message/LATEST 写 `1114 passed`；后续必须以 RUN_MANIFEST 统一机器计数。

已修并继续保留回归：`IT-P0-003`、`IT-P0-004/005`、`IT-P1-006` 主体、`IT-P1-007`。

## 本轮研究增量

主实验：`EXP-IT-CAP-002-explicit-mask`。

在新的 synthetic lockbox（30 个 synthetic symbol、19,222 candidates；日期仅为人工索引，不是未来市场数据）上，固定旧 HGB 参数，比较 base / capability-dropout / 35 factors + 8 family capability masks：

| 场景 | base PR | capdrop PR | capmask PR |
|---|---:|---:|---:|
| clean | **0.6782** | 0.6761 | 0.6778 |
| provider shift | 0.6716 | 0.6755 | **0.6778** |

provider 分层显示收益主要来自 Sina（0.6186 → 0.6366），Eastmoney capmask 没有提升（0.6054 → 0.5970），因此不能把 mask 当万能补丁。

预登记 capability abstention：all 覆盖100% PR 0.6778；l1_plus 覆盖91.07% PR 0.6841；full_microstructure 覆盖73.71% PR 0.6933。该结果只说明 synthetic coverage-quality tradeoff，不能冒充端到端 Recall 或发布阈值。

本轮没有证据支持继续扩大 TCN；真实数据阶段优先固定 HGB base/capdrop/capmask。

## 下一轮必须检查的产物

1. `IT-P0-002-R1` 真实 repo 修前红测 / 修后绿测 / 回退验牙与 diff hash；
2. RoundObservationSet 进入 Store/status/live_session，并有 current + capability coverage；
3. source soft-partial requested/returned/missing/provenance 与定向 fallback；
4. 14:57 close auction、ObservationInterval、limit qualified-state、Eastmoney max_pages 四项回归；
5. 真实 `dataset_manifest.json`；
6. 数据足够时固定 HGB base/capdrop/capmask 的真实 time-forward/provider 分层；
7. 报告、LATEST、commit 与 RUN_MANIFEST 的测试数量必须一致。

## 前轮线索

上一份完整报告：[`2026-09-20_02-41-38_JST.md`](./2026-09-20_02-41-38_JST.md)。该轮产品提交 `a3cfd567fb07e414c1d553e5fc478e50892abea8` 的 commit/LATEST 归档 `1114 passed`，而完整报告仍写 `1103 passed`；本轮已将这项记录为 `IT-P2-REPORT-001`，不擅自猜测哪一数字应被改写。
