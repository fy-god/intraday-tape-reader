# 最新审计

**最新云端独立审计**：[`2026-09-22_00-08-36_JST.md`](./2026-09-22_00-08-36_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-22_00-08-36_JST_AGENT_TASK.md`](./2026-09-22_00-08-36_JST_AGENT_TASK.md)  
**最新本地 Agent 产品轮**：[`2026-09-21_21-00-00_JST.md`](./2026-09-21_21-00-00_JST.md)  
**上一份云端独立审计**：[`2026-09-21_21-56-56_JST.md`](./2026-09-21_21-56-56_JST.md)  
**更早云端独立审计**：[`2026-09-21_21-43-27_JST.md`](./2026-09-21_21-43-27_JST.md)  
**下一步计划**：[`NEXT_STEPS.md`](./NEXT_STEPS.md)  
**仓库执行清单**：[`RUN_MANIFEST.json`](./RUN_MANIFEST.json)

> 历史审计文件全部保留在本目录；本索引仅移动接续指针，不删除或覆盖历史报告。

## 2026-09-22 00:08 JST 云端审计：Universe Truth 进一步收敛为 Recovery State

- `reviewed_source_sha`: `fd51674508cb237c844d6a4924b7e6a2f084c4b6`
- 审计开始 `main`: `f6efd5fc2e32a0be2a00c85fbada9251f0ac5078`
- `fd516745 -> f6efd5fc` 仅有 docs-only 提交；本轮固定产品版本不变。
- 当前开放 PR：0。
- 沙箱完整 checkout：`blocked`（容器 DNS 无法解析 `github.com`）；GitHub connector 固定 SHA 读取正常。
- 本轮模型训练：0；真实盘中网络、真实通知、交易：均未启动。

### 新确认 P1

1. **`IT-P1-UNIVERSE-WATCHLIST-PIN-001`**：normal 模式全市场源失败后，watchlist fallback 通过通用 `_codes` setter 写入，导致 `_codes_pinned=True`；后续 `_maybe_refresh_universe(force=False)` 永久短路。机制 fixture：源 60 秒后恢复完整 5913，当前控制流仍停在 10 只 watchlist；候选 recoverable fallback 可自动恢复 full。
2. **`IT-P1-UNIVERSE-REFRESH-STORM-001`**：已有 full pool 过 TTL 后，smaller partial candidate 被正确拒绝，但拒绝分支不推进任何 last-attempt/retry 时钟；每个 5 秒 poll 都再次刷新 universe。固定 10 分钟/120 轮机制 fixture：当前 120 次 refresh；候选 60 秒 degraded retry 为 10 次。60 秒仅是起始候选配置，不是实盘最优值。

两条问题共同说明：当前 `_codes_pinned` 混淆“显式固定”与“临时降级”，`_universe_refreshed_at` 又混淆“最后完整成功”与“最后尝试”。主改造升级为 **Universe Truth & Recovery Contract v2**：`unknown/full/partial_degraded/retained_previous/watchlist_fallback/explicit_pinned` 六状态，并拆 `last_attempt_at / last_applied_at / last_complete_at` 三个时钟。

### 继续开放

- **`IT-H20-DELIVERY-ACCOUNTING-GATE-MISSING`**：上一云端 addendum 已真实证明判决层假绿，并给出 scratch patch：原产品新测试 `2 failed / 3 passed`，补丁后 `5 passed`，全套 `1681 passed`。当前产品仍未接入；下一 Agent 轮应先以小提交落地并回退验牙。
- **`IT-P1-UNIVERSE-COVERAGE-001`**：round `returned/requested` 只度量 active pool 内返回率，不是市场覆盖率。
- **`IT-P2-UNIVERSE-STATUS-CACHE-001`**：`/api/status.universe = len(state.quotes)` 是 latest quote cache 口径，不是 active scan universe。
- `IT-P1-CAPABILITY-002`、`IT-P1-SOURCE-EMPTY-001`、`IT-P1-WINDOW-001`、SSE durable cursor/gap 与 bounded state 继续开放。

### 证据纪律

- 旧 `73.0% / 83.1%` 股票池覆盖率已被本地 Agent 自我更正为不可复算/INVALID，本轮没有引用为事实。
- 本轮 `5913 / 4100` 仅是 synthetic mechanism fixture，不是重新测得的真实市场覆盖率。
- 下次真实覆盖测量必须落 `source/raw hash/expected_total/raw_unique_codes/usable_quotes/active_scan_codes/timestamp/command`，可独立重算。

### 下一轮优先顺序

1. 落地已经证明可行的 H20 delivery verdict/print 小补丁；
2. normal watchlist fallback 不得 explicit pin，增加真实 Engine 红测；
3. smaller-partial reject 使用独立 degraded retry/backoff；
4. UniverseTruth schema 接 Store/status/live-session；
5. 本地授权环境重新测真实 universe coverage；
6. per-code provenance/capability → targeted soft-partial fallback → ObservationInterval → SSE durable delivery；
7. UniverseTruth 与真实多日标签未闭合前，不扩 TCN/Transformer。

### 下一轮必须核查的实物

`wp01_delivery_gate_{red,green,rollback}.log`、`universe_watchlist_recovery_{red,green,rollback}.log`、`universe_partial_backoff_{red,green,rollback}.log`、`universe_recovery_cases.json`、`universe_truth_cases.json`、真实测量时的 `universe_coverage_reconcile.json`、完整 pytest/check/dashboard 日志、`RUN_MANIFEST.json`、`NEXT_STEPS.md`。

## 关键历史接续

- [`2026-09-21_21-56-56_JST.md`](./2026-09-21_21-56-56_JST.md)：确认 Delivery accounting 在 `evaluate_health` 判决层假绿；scratch patch 已证明零回归。
- [`2026-09-21_21-43-27_JST.md`](./2026-09-21_21-43-27_JST.md)：发现新 Delivery Ledger producer 与 verdict/print consumer 断线。
- [`2026-09-21_21-00-00_JST.md`](./2026-09-21_21-00-00_JST.md)：产品轮 `fd516745...`，修 Delivery false-green 产品层、ring-buffer 分母、soak producer、signal_id roundtrip 等；归档 1676 passed。
- [`2026-09-21_20-10-37_JST.md`](./2026-09-21_20-10-37_JST.md)：把 Universe denominator 提升为研究主瓶颈；旧实盘覆盖百分比后续已作废。
- [`2026-09-21_17-40-00_JST.md`](./2026-09-21_17-40-00_JST.md)：独立复核 Delivery Ledger。
- [`2026-09-21_17-00-00_JST.md`](./2026-09-21_17-00-00_JST.md)：产品轮，Evaluability / Delivery 分账。
- 更早报告继续按本目录时间戳文件追溯，均未删除。
