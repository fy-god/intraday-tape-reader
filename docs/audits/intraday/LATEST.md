# 最新审计

**最新本地独立审计**：[2026-09-29_17-00-00_JST.md](./2026-09-29_17-00-00_JST.md)  
**最新云端独立审计**：[2026-09-24_20-08-07_JST.md](./2026-09-24_20-08-07_JST.md)  
**最新云端 Agent 任务书**：[2026-09-24_20-08-07_JST_AGENT_TASK.md](./2026-09-24_20-08-07_JST_AGENT_TASK.md)  
**上一版本地独立审计**：[2026-09-24_18-55-39_JST.md](./2026-09-24_18-55-39_JST.md)  
**reviewed_source_sha / 本轮固定产品代码**：`221146d2efc8eb052a77b8032a3e540513e8df50`  
**audit_start_head / 本轮开始 main**：`221146d2efc8eb052a77b8032a3e540513e8df50`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/221146d2efc8eb052a77b8032a3e540513e8df50/docs/audits/intraday/LATEST.md

> 版本纪律：`9670dd9..221146d` 之间只有审计 docs/evidence 提交，因此本轮真正被审产品代码是 `221146d`（其树内产品代码 == `9670dd9`）。本轮**修改了产品代码**（Sina 指数身份 + 生产 detailed 接线），故 reviewed_source_sha 指修改前的 HEAD。

## 2026-09-29 17:00:00 JST（本地）

**本轮结论**：云端 20:08 的 `IT-P1-SINA-INDEX-QUOTE-ID-COLLIDES-WITH-STOCK-013`
**本地真实代码复现确认成立，已修复**。指数 `Quote.code` 由 `000001` → `sh000001`，
`board` 由 `Board.MAIN` → `Board.INDEX`。连带修掉云端同轮 #2 幻影 missing、
#3 盲测试；完成任务书 WP01–WP07（含生产 `call_detailed` 调用点 **0 → 2**）。

- 全量测试 **1957 → 2002 passed**（新增 45 条 / 4 个测试文件）
- 回滚牙齿 **7/7 behavioral RED，0 structural**，3 个产品文件 sha256 字节级还原
- 门禁：`check_*.py` 8 ok / 0 fail；`selftest` exit 0（68 告警 / 6 类型）；`dash_render_check` exit 0
- 证据目录：`evidence_2026-09-29_16-58-26_JST/`
- **模型真正增量提升 = 0**；真实 Precision/Recall/漏事件率/收益仍 `unavailable`（未编造）
- **用户获得感提示**：`spirit_index` 仍默认关闭，且本轮为**非交易时段**，
  WP09 实盘 soak 未运行 —— 单元级证据 ≠ 盘中真实预警。

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
