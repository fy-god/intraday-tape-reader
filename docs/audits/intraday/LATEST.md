# 最新审计

**最新云端独立审计**：[`2026-09-21_16-07-37_JST.md`](./2026-09-21_16-07-37_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-21_16-07-37_JST_AGENT_TASK.md`](./2026-09-21_16-07-37_JST_AGENT_TASK.md)  
**最新本地 Agent 轮**：[`2026-09-21_13-37-29_JST.md`](./2026-09-21_13-37-29_JST.md)  
**上一份云端独立审计**：[`2026-09-21_12-02-53_JST.md`](./2026-09-21_12-02-53_JST.md)  
**上一份云端 Agent 任务书**：[`2026-09-21_12-02-53_JST_AGENT_TASK.md`](./2026-09-21_12-02-53_JST_AGENT_TASK.md)  
**上一份本地 Agent 产品轮**：[`2026-09-21_13-00-00_JST.md`](./2026-09-21_13-00-00_JST.md)  
**更早本地 Agent 轮**：[`2026-09-21_11-31-34_JST.md`](./2026-09-21_11-31-34_JST.md)  
**下一步计划**：[`NEXT_STEPS.md`](./NEXT_STEPS.md)  
**仓库执行清单**：[`RUN_MANIFEST.json`](./RUN_MANIFEST.json)

> 历史审计文件均保留在本目录；本索引只移动当前接续指针，不删除历史报告。

## 2026-09-21 16:07:37 JST 云端审计接续

- 发布前 HEAD：`948e0d2aaa29f8b7246225f6e45fdba6496f8551`。
- 被审最新产品：`eba8c4af5ed943a023ab09ab9c30000c1ac58f65`；其后到发布前 HEAD 只有审计文档，无产品源码变化。
- 开放 PR：0。
- 最新本地 Agent 对 `eba8c4a` 的归档机器证据：`1642 passed in 108.85s`，另一次独立复跑 `1642 passed in 118.34s`；本云端轮未重复执行完整 pytest。

### 本轮最大更正：`signal_id-only` 不能修 9.1% delivery coverage

最新本地轮正确量化：replay 66 条 committed Alert，只有 6 条 tagged，60 条无 `signal_id`；只有 `volume_burst/spirit_order` 两条规则进入账本。

但它提出“给另 5 条规则各补一行 signal_id 就能让 committed 对账生效”。当前 Engine `_mark_stage()` 还要求：

```text
signal_id 已存在
AND
signal_id 已经在 observation.signal_evals
```

五条未覆盖规则没有 SignalEvalStats 行，因此只补 signal_id 仍会被 Engine 直接 return。

本轮机制反事实：

```text
当前：2/7 可记 delivery
只补 signal_id：仍 2/7
Engine 自动造 SignalEvalStats：5/5 新行破坏 bus<=rule_selected 不变量
独立 SignalDeliveryStats：7/7 机制闭合
120,000 软件事件 property stress：0 delivery invariant violation
```

因此新增稳定问题：`IT-P1-DELIVERY-LEDGER-002`（P1）。

### 下一步顺序

1. **Delivery Ledger v2 与 evaluability registry 分离**；Engine 统一记录所有第一方 Alert 的 `rule_selected/bus_accepted/committed`。
2. 5 条未覆盖规则补稳定 `signal_id`，并建立 100% first-party committed coverage 门禁。
3. 并行建立 Universe Coverage Gate（历史实测曾只有 73.0%/83.1%，本轮未重测）。
4. 再做 stable `event_id`、typed NotificationResult、committed-event T+5/T+30。
5. 然后才推进 per-code provenance → targeted fallback → ObservationInterval → SSE durable cursor/gap。
6. 真实多日语料与上述分母闭合前，不恢复模型大搜索。

### 当前主要开放项

`IT-P1-DELIVERY-LEDGER-002` / `IT-P1-DELIVERY-COVERAGE-001` / `IT-P1-NOTIFY-RESULT-001/002` / `IT-P1-ALERT-IDENTITY-001` / universe coverage gate / `IT-P1-CAPABILITY-002` / `IT-P1-SOURCE-EMPTY-001` / `IT-P1-WINDOW-001` / SSE `IT-P1-008/009/003`。

### 研究边界

本轮模型训练次数 0；真实 Precision / Recall / 漏事件率 / 交易收益仍 `unavailable`。本轮实验只证明 delivery/evaluability 账本的机械合同，不代表市场预测收益。
