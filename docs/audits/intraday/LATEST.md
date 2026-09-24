# 最新审计

**最新云端独立审计**：[2026-09-24_16-02-09_JST.md](./2026-09-24_16-02-09_JST.md)  
**最新云端 Agent 任务书**：[2026-09-24_16-02-09_JST_AGENT_TASK.md](./2026-09-24_16-02-09_JST_AGENT_TASK.md)  
**上一版本地独立审计**：[2026-09-24_09-27-16_JST.md](./2026-09-24_09-27-16_JST.md)  
**reviewed_source_sha / 本轮固定产品代码**：`7a4f549a15e78fe87db7de00796eff71b707ae9b`  
**audit_start_head / 本轮开始 main**：`5d9c946ef8eb9bf9a855cb7d3cdcf5c2f2af5483`  
**report_commit_sha**：`869de2008e85543eb44eafbb4c31aef0196471c5`  
**agent_task_commit_sha**：`4f171fa1124a60447524e58b5a8ad69f60b0a5b5`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/5d9c946ef8eb9bf9a855cb7d3cdcf5c2f2af5483/docs/audits/intraday/LATEST.md

> 版本纪律：`7a4f549..5d9c946` 的 GitHub compare changed files 全部位于 `docs/audits/intraday/`，故真正产品代码仍固定为 `7a4f549`。本轮只发布报告、任务书并移动索引，不删除历史报告。

## 2026-09-24 16:02:09 JST

主实验：`EXP-IT-SINA-INDEX-IDENTITY-017`

### 本轮最高优先新增

1. `IT-P1-SINA-INDEX-PREFIX-LOSS-SILENT-WRONG-INSTRUMENT-012`：Sina `snapshots()` / `snapshots_detailed()` 先通过 `_norm_code/_norm_codes` 丢掉显式指数前缀，再用只对股票可靠的 `guess_prefix()` 重建 wire symbol。默认 5 个 index codes 中，`sh000001→sz000001`、`sh000300→sz000300`、`sh000688→sz000688` 三个会被确定性改成错误交易所符号。
2. 这不是普通 missing：`sz000001` 是真实股票平安银行，legacy `wanted_set` 也是裸 `000001`，所以错误证券响应可以被当成成功行情收下。
3. detailed 路径使用同一 normalization，`R/P/Q` 甚至可能对错误证券机械自洽，产生“exact but wrong”假精确。因此必须先修 Source security identity，再继续 WP07。

### 主改造

**Sina Route Identity Contract v1**：

```text
显式 index prefix 保留到 HTTP wire
        ↓
指数 Quote.code 保留 exchange prefix
        ↓
index route detailed R/P/Q 使用同一 prefixed identity
        ↓
再复验 route-state collision
        ↓
StateUpdateResult / Per-route RoundObservation
```

普通股票现有 bare-code API 必须保持兼容。

### 当前主线状态

- `IT-P1-SNAPSHOT-RAW-LEDGER-COLLAPSE-001`：PARTIAL，Source / SourceManager exact truth 已有，生产 `poll_once` 仍未接 detailed。
- `IT-P1-INDEX-ROUTE-HEALTH-MASKED-BY-STOCK-AGGREGATE-001`：继续开放。
- `IT-P2-ROUND-ROUTE-PROVENANCE-COLLAPSE-001`：继续开放。
- `IT-P2-TIME-DIAGNOSTIC-ROUTE-COLLISION-010`：改为 Source identity 修复后的回归门；当前第一根因更早，是 Sina wire identity 已经拿错证券。
- `IT-P2-TIME-REJECT-ROUTE-ATTRIBUTION-011`：继续作为 WP07 前置。
- Eastmoney unknown-total pagination：已有真实 RED/GREEN，仍 ready-to-land。
- Membership / exact soft-empty / timezone / ObservationInterval：继续 downstream。

### 下一轮关键产物

- `sina_index_wire_red/green/rollback.log`
- `sina_index_quote_red/green/rollback.log`
- `sina_index_detailed_red/green/rollback.log`
- `default_index_matrix.json`
- `sina_index_cases.json`
- `route_state_recheck.json`
- `full_pytest.log`
- `RUN_MANIFEST.json`
- `NEXT_STEPS.md`

模型训练：0；真实 Precision/Recall/漏事件率/交易收益仍 `unavailable`。
