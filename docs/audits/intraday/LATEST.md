# 最新审计

**最新本地独立审计**：[2026-09-29_22-00-00_JST.md](./2026-09-29_22-00-00_JST.md)  
**上一版本地独立审计**：[2026-09-29_17-00-00_JST.md](./2026-09-29_17-00-00_JST.md)  
**最新云端独立审计**：[2026-09-24_20-08-07_JST.md](./2026-09-24_20-08-07_JST.md)  
**最新云端 Agent 任务书**：[2026-09-24_20-08-07_JST_AGENT_TASK.md](./2026-09-24_20-08-07_JST_AGENT_TASK.md)  
**reviewed_source_sha / 本轮固定产品代码**：`4e702b68066ac5187b1b298a52c132873672ee6d`  
**audit_start_head / 本轮开始 main**：`4e702b68066ac5187b1b298a52c132873672ee6d`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/4e702b68066ac5187b1b298a52c132873672ee6d/docs/audits/intraday/LATEST.md

> 版本纪律：本轮 `git fetch` 后 `origin/main == 4e702b6`，**无新云端报告**；最新云端审计仍是 09-24 20:08。本轮做的是既有任务书尾巴（WP08/未决项），修改了产品代码，故 reviewed_source_sha 指修改前的 HEAD。

## 2026-09-29 22:00:00 JST（本地）

**本轮结论**：挖出并修掉 **3 个真实缺陷**：

1. `IT-P1-ADMIT-REASON-STALE-SILENT-DROP-016` — `AdmitReason.stale` 拒绝
   **静默丢失 route 归属**（bug 类 d 假绿出口，**已在树里**，且被两个既有
   测试覆盖）。**更正云端任务书**："`stale` 不可达"是错的。
2. `IT-P1-SOAK-RAW-PRESENCE-GRADE-BLIND-001` — `live_session` 把**投影反推**
   的 missing/quality 当**精确事实**上报（上一轮我加了字段却零读者）。
   已改为三态 `exact`/`mixed`/`not_measured`，**键缺失不得当成 exact**。
3. `IT-P1-006-R1-R2` — Eastmoney 无 total 分支 `truncated` **恒 False**
   （诊断说谎；安全性未漏，恒 fail-closed）。

**独立复核并把一个"疑似缺陷"降级**：`call` 与 `call_detailed` 两份
failover 逻辑经 4 条路径探针实测**行为完全一致** —— 是潜在维护风险，
**不是**已确认缺陷。

- 全量测试 **2002 → 2060 passed**（+58），零回归
- 回滚牙齿：Eastmoney **2/2 behavioral RED**；AdmitReason 给了
  **structural + behavioral 双证**（诚实标注 structural 是较弱证据）
- 门禁：`check_*.py` 8 ok / 0 fail；`selftest` exit 0；`dash_render_check` exit 0
- 证据目录：`evidence_2026-09-29_21-59-53_JST/`
- **模型真正增量提升 = 0**；真实 Precision/Recall/漏事件率/收益仍 `unavailable`
- **诚实标记**：`stale_hard_rejected` 在生产**恒为 0**（三源 `freshness_allowed=false`），
  是 capability-ready 遥测，**不是会动的曲线**
- **用户获得感提示**：`spirit_index` 仍默认关闭，非交易时段，WP09 soak 未运行

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
