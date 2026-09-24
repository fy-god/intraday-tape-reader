# 最新审计

**最新云端独立审计**：[2026-09-24_20-08-07_JST.md](./2026-09-24_20-08-07_JST.md)  
**最新云端 Agent 任务书**：[2026-09-24_20-08-07_JST_AGENT_TASK.md](./2026-09-24_20-08-07_JST_AGENT_TASK.md)  
**上一版本地独立审计**：[2026-09-24_18-55-39_JST.md](./2026-09-24_18-55-39_JST.md)  
**reviewed_source_sha / 本轮固定产品代码**：`9670dd97169a4d9c4e5021d5fbfc4f1a4804cfdb`  
**audit_start_head / 本轮开始 main**：`89eec3e89305037874afebbdabdeb96cb2543ce6`  
**report_commit_sha**：`28fcaac72c73e0724bbf43c67634243394b5f92c`  
**agent_task_commit_sha**：`d6f9c8b919808d53e8466d9e68184c082a327188`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/89eec3e89305037874afebbdabdeb96cb2543ce6/docs/audits/intraday/LATEST.md

> 版本纪律：`9670dd9..89eec3e` 只有 18:55 审计 docs/evidence 提交，因此本轮真正被审产品代码是 `9670dd9`。本轮远端写入仅限报告、任务书和本文件。

## 2026-09-24 20:08:07 JST

主实验：`EXP-IT-SINA-INDEX-END2END-018`

### 本轮最高优先新发现

1. `IT-P1-SINA-INDEX-QUOTE-ID-COLLIDES-WITH-STOCK-013`：上一轮已经把 `sh000001` 的 HTTP wire 修正确，但 Sina parser 仍把 index `Quote.code` 压成裸 `000001`。因此上证指数仍可与平安银行共享 `EngineState.quotes/history/first_seen/last_price/day_open` 的键；`SpiritIndex` 又按 `q.code` 读窗口，存在继承股票历史后构造巨大假指数拉升/打压的确定性机制反例。默认 `spirit_index=false`，故当前标 latent P1。
2. `IT-P2-SINA-INDEX-OBSERVATION-PHANTOM-MISSING-014`：当前 RoundObservation 请求轴使用配置中的 `sh000001`，而 Sina raw/admitted Quote 轴是 `000001`，所以一轮可以同时出现 `index returned/admitted=1` 与 `unknown_missing=sh000001`。
3. `IT-P2-SINA-DETAILED-PREFIX-TEST-BLIND-015`：现有测试名为 `test_detailed_identity_axis_keeps_index_prefix`，但只断言 requested/returned/admitted 数量与无 missing，没有断言 `requested_keys/raw_returned_requested_keys/admitted_keys/Quote.code` 真正保留 `sh000001`；当前 bare R/P/Q 实现因此可以通过。

### 已修项不重开

- `9670dd9`：Sina 显式 index prefix 的 HTTP wire/filter 错证券问题已修；本地产品报告归档 `1957 passed`。
- `3885ebb`：route-local `StateUpdateResult`、route-key 时间诊断 map、Eastmoney no-total pagination 已修。

### 下一轮依赖

`Sina index Quote/detailed canonical identity -> Tencent/Sina cross-source identity -> EngineState/SpiritIndex collision regression -> RoundObservation identity -> production call_detailed -> per-route health/provenance -> Membership/soft-empty -> live soak -> research`。

模型训练：0；真实 Precision/Recall/漏事件率/交易收益仍 unavailable。
