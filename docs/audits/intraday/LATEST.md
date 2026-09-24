# 最新审计

**最新云端独立审计**：[2026-09-24_12-04-56_JST.md](./2026-09-24_12-04-56_JST.md)  
**最新云端 Agent 任务书**：[2026-09-24_12-04-56_JST_AGENT_TASK.md](./2026-09-24_12-04-56_JST_AGENT_TASK.md)  
**上一版本地独立审计**：[2026-09-24_09-27-16_JST.md](./2026-09-24_09-27-16_JST.md)  
**reviewed_source_sha / 本轮固定产品代码**：`7a4f549a15e78fe87db7de00796eff71b707ae9b`  
**audit_start_head / 本轮开始 main**：`ba8c3029cb15b96a34892da9972a943c699f1b08`  
**report_commit_sha**：`b54d6289b29b04bdc8b308448e938da3adda258c`  
**agent_task_commit_sha**：`916cd36c93b63779a601d52d544d8687a666af8b`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/ba8c3029cb15b96a34892da9972a943c699f1b08/docs/audits/intraday/LATEST.md

> 版本纪律：`7a4f549..ba8c302` 只有 `docs/audits/intraday/` / evidence 文档变化，故真正产品代码仍固定为 `7a4f549`。本轮只新增审计报告/任务书并移动索引，不删除历史报告。

## 2026-09-24 12:04:56 JST

主实验：`EXP-IT-ROUTE-TIME-ATTRIBUTION-016`

### 本轮新增

1. `IT-P2-TIME-DIAGNOSTIC-ROUTE-COLLISION-010`：hard admission watermark 已按 `(route,code)` 分账，但 `provider_ts_raw / effective_event_time / received_at / time_age_seconds` 仍只按裸 code 存。Sina 在 index route 可把 `sh000001` 解析成裸 `000001`，随后 stock `000001` 更新可覆盖 index 的 age/timestamp 诊断；当前影响主要是 stale/age/provenance/研究诊断，不把它夸成当前硬准入跨 route 失效。
2. `IT-P2-TIME-REJECT-ROUTE-ATTRIBUTION-011`：`EngineState.update()` 只返回 admitted，future/ooo 只写全局 counters。`poll_once` 在两条 route 更新前取一次基线、之后算一个 aggregate delta；`index future + stock ooo` 与 `index ooo + stock future` 会塌成同样的 `future=1 / ooo=1`，无法恢复 route truth。
3. 上一轮 `IT-P1-SOURCE-MANAGER-GLOBAL-IDX-BREAKS-ROUTE-ISOLATION-008` 重新定性：跨 route global promotion 的代码机制真实存在，但当前 `tests/test_engine.py` 明确把它钉成产品期望行为；在产品策略不变时不继续当隐藏 P1。真正剩余的是 `IT-P2-SOURCE-PROMOTION-POLICY-DOC-CONFLICT-009`：EngineState 注释的“各 route 各有切源时机”与 executable policy 需要统一。

### 主改造

**Route Admission Result Contract v1**：

```text
SnapshotFetchResult
        ↓
Route-local StateUpdateResult
        ↓
Per-Route RoundObservation
        ↓
Per-Route Health / Provenance
        ↓
Compatibility Aggregate
```

`StateUpdateResult` 需提供 route-local admitted / future / out-of-order / stale identities，并把 provider/effective/received/age 诊断真实存储改为 `(route,code)`。WP07 不能继续从 global stats 反推 route 时间拒绝原因。

### 当前主线状态

- `IT-P1-SNAPSHOT-RAW-LEDGER-COLLAPSE-001`：PARTIAL，Source/SourceManager exact truth 已有，生产 `poll_once` 仍未接 detailed。
- `IT-P1-INDEX-ROUTE-HEALTH-MASKED-BY-STOCK-AGGREGATE-001`：继续开放，stocks 5563/5563 + indices 0/5 时 aggregate 仍约 99.91%。
- `IT-P2-ROUND-ROUTE-PROVENANCE-COLLAPSE-001`：继续开放，一轮 stocks/index 可来自不同 source，而 RoundObservation 仍只有一个 stock-derived source/capabilities。
- Eastmoney unknown-total pagination：已有真实 RED/GREEN，仍 ready-to-land。
- Membership / exact soft-empty / timezone / ObservationInterval：继续 downstream。

### 下一轮关键产物

- `route_time_cases.json`
- `route_reject_cases.json`
- `route_observation_cases.json`
- `pagination_cases.json`
- `full_pytest.log`
- `check_*.py` logs
- `RUN_MANIFEST.json`
- `NEXT_STEPS.md`

模型训练：0；真实 Precision/Recall/漏事件率/交易收益仍 `unavailable`。
