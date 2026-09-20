# 最新审计

**最新云端独立审计**：[`2026-09-20_20-09-14_JST.md`](./2026-09-20_20-09-14_JST.md)  
**最新本地 Agent 任务书**：[`2026-09-20_20-09-14_JST_AGENT_TASK.md`](./2026-09-20_20-09-14_JST_AGENT_TASK.md)  
最新产品实现提交：[`100e06a`](https://github.com/fy-god/intraday-tape-reader/commit/100e06aeda7dc5605ac20b3f8511ec34ea40e9b6)  
上一份本地独立审计：[`2026-09-20_18-22-15_JST.md`](./2026-09-20_18-22-15_JST.md)  
上一份产品实现轮：[`2026-09-20_17-22-44_JST.md`](./2026-09-20_17-22-44_JST.md)  
时间合同：[`source_time_contract.json`](./source_time_contract.json)  
上一轮云端报告：[`2026-09-20_16-17-25_JST.md`](./2026-09-20_16-17-25_JST.md)

---

## 2026-09-20 20:09 JST 云端审计

- `reviewed_source_sha`：`100e06aeda7dc5605ac20b3f8511ec34ea40e9b6`。
- 审计开始 `main` HEAD：`aae2ac9ad7ef41a4bd50ed859801c30aa17fe0b5`；其相对 `100e06a` 仅增加 18:22 审计文档，无产品源码变化。
- 开放 PR：0。
- 本轮沙箱无法 clone GitHub（DNS），因此**没有**重新跑完整仓库 pytest；`100e06a` 的 `1308 passed / 0 failed` 仅作为本地 Agent 已归档历史结果，不冒充本轮执行。
- 真实多日连续 A 股语料仍未挂载；真实 Precision / Recall / 漏事件率 / 交易收益继续 `unavailable`。

### 本轮最高优先结论

1. **`IT-P1-COMPLETE-001-R1` 仍为已确认 P1**：Eastmoney `complete` 仍用 post-parse `len(out)` 对比 transport `total`。18:22 冻结树已经真实复现：正常不可用/停牌行即可让 `complete=False`，已有股票池拒绝刷新，新上市代码实测可出现 `0/10` 进入。下一轮优先改为 `transport_complete` 与 `usable_coverage` 双账。
2. **新 `IT-P1-TIME-ROLE-004`**：SourceManager 已按 stocks/index 路由分 serving source，但 EngineState 只有一个全局 `source_epoch`/watermark；`poll_once()` 只按 `ROUTE_STOCKS` 开 epoch。结果：index 自己切源时新源首包可能被旧 watermark 拒绝；stocks 切源时又会清掉 index watermark，使 index 同源旧包可能被误接纳。
3. **新 `IT-P1-TIME-ROLE-003-R1` / `IT-P1-TIME-POLICY-001`**：切 source epoch 只清 watermark、不清全局 `first_seen`，同码新源 3 天前首包可绕过 stale gate；同时合同三源均 `role=unknown`、`freshness_allowed=false`，Engine 却仍基于 provider ts 做 4 小时 hard stale reject，执行策略与自身合同冲突。
4. **新 `IT-P1-OBS-010`**：产品已经存在 `t_reject:stale`，但 RoundObservationSet 没有真实 stale bucket；旧 `stale_rejected` 仍错误 alias 到 `future_rejected`，陈旧拒绝无法在观测账本中对账。
5. **新 `IT-P1-OBS-011`**：`live_session.make_round_sample()` 已抓 requested/returned/admitted/coverage/capability，但 `summarize_rounds()` 又把这些字段全部丢弃；最终 health 仍无法用真实 observation coverage / rule evaluability 识别 soft-partial 或 capability degradation。
6. `IT-P1-LIMIT-001` 本轮回读产品代码后维持**已修**：先 `_seal_qualified()`，不足记 `at_limit_unqualified`，达标才 `sealed`。
7. `IT-P1-INDEX-CURRENT-001`、`IT-P1-SOURCE-EMPTY-001`、`IT-P1-CAPABILITY-002`、`IT-P1-WINDOW-001`、`IT-P2-OBS-008/009` 与 SSE/Store 可靠性项继续开放。

### 本轮研究 `EXP-IT-CAP-005-block-correlation-robustness`

没有重新训练。复用冻结的 4 个 synthetic lockbox / `77,271` 行逐样本预测，按 symbol-day 固定 `30/60/120/300s` 时间块聚合，并以 `lockbox|symbol|day` 为单位做 500 次 group bootstrap。

Provider-specialist 相对 global capmask 的 PR AUC 增量：

| block | Spec-Cap PR | group bootstrap 95% |
|---|---:|---:|
| 30s | +0.00604 | [+0.00319, +0.00887] |
| 60s | +0.00497 | [+0.00187, +0.00774] |
| 120s | +0.00268 | [+0.00025, +0.00517] |
| 300s | +0.00289 | [+0.00087, +0.00478] |

结论：synthetic specialist 增量不是完全由逐 tick 重复行制造，但事件块越粗收益越小。继续保留为**真实数据候选**，不升级 shadow 默认；真实认证主指标改用事件簇/时间块，增量 `<0.005` 或主要 folds/provider 方向不稳定即淘汰。TCN/Transformer 继续暂停。

### 下一轮必须拿到的实物

- Eastmoney transport/usable 双账：红测/绿测/回退验牙、`universe_transport_reconcile.json`、diff SHA256；
- stocks/index route-specific epoch：`source_epoch_route_cases.json` 与真实集成测试；
- time policy：`time_policy_matrix.json`，让 `source_time_contract.json` 真正约束 freshness/ordering；
- Observation Ledger：真实 stale bucket、route-level actual requested/code-level missing、`observation_ledger_cases.json`；
- live-session：coverage 分位、拒绝原因、source/capability mix 真正进入 summarize/health；
- index current-only：`index_current_reconcile.json`；
- 完整 `RUN_MANIFEST.json` 与 `NEXT_STEPS.md`。

完整证据、机制复现、研究表和十个工作包见本轮报告与任务书。
