# 最新审计

**最新云端独立审计**：[`2026-09-23_20-09-44_JST.md`](./2026-09-23_20-09-44_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-23_20-09-44_JST_AGENT_TASK.md`](./2026-09-23_20-09-44_JST_AGENT_TASK.md)  
**最新本地 Agent 产品轮**：[`2026-09-23_19-05-00_JST.md`](./2026-09-23_19-05-00_JST.md)  
**最新产品提交 / reviewed_source_sha**：`ff8fd8f125cd785340c6fd62f3d15db61b02f333`  
**本轮审计开始 docs HEAD**：`014435893a63d02b35ae48c34f3179c9a4fa8845`  
**report_commit_sha**：`a0535c494453f992d22e6f8d6d19e8787784abfd`  
**agent_task_commit_sha**：`59a646760949df09e034e5d1b60f97986b853b7a`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/014435893a63d02b35ae48c34f3179c9a4fa8845/docs/audits/intraday/LATEST.md

> `ff8fd8f..014435893` 只有 19:05 产品报告 / evidence / LATEST 三个 docs 路径；历史报告文件没有删除。本文件移动当前接续指针，上一版完整长索引固定保留在上面的不可变 commit。

## 2026-09-23 20:09:44 JST

主实验：`EXP-IT-OUTCOME-INTEGRATION-012`

### 接续

19:05 产品轮已把 Snapshot Outcome v4 做进 Tencent/Sina/Eastmoney，并归档 `1874 passed ×5`、12 条行为回退 RED；**但产品报告明确声明尚未接进 Engine RoundObservationSet**。因此 source-layer raw truth 已有，线上账本仍是 Quote-only 旧语义。

### 本轮新增

1. `IT-P2-SNAPSHOT-OUTCOME-COVERAGE-SEMANTIC-COLLISION-001`：source outcome 的 generic `coverage` 是 Q/R usable coverage，而现有 RoundObservation `coverage` 是 P/R raw-return coverage。WP02 接线前必须拆名。
2. `IT-P2-TENCENT-DETAILED-EXPLICIT-STOCK-KEY-AXIS-001`：显式前缀普通股票（如 `sh600000`）在 detailed request/raw identity 轴可能错位，产生 false missing/unexpected；现有测试未覆盖该形态。
3. `IT-P2-SNAPSHOT-MERGE-TERMINAL-OVERLAP-001`：同 code 在一个 part invalid、另一个 part valid 时，merge 对 per-part quality 直接并集，可让同一 code 同时 admitted+quality，破坏 terminal disjointness。

### 主改造

**Snapshot Outcome → Round Attribution Integration Contract v2**

顺序：`source-v4 self-consistency -> SourceManager detailed -> Engine unified attribution -> live-session unattributed -> Membership -> exact soft-empty`。

### 下一轮关键产物

- `coverage_cases.json`
- `snapshot_outcome_cases.json`
- `round_attribution_cases.json`
- `pagination_cases.json`
- `full_pytest.log`
- `check_*.py` logs
- `RUN_MANIFEST.json`
- `NEXT_STEPS.md`

模型训练：0；真实 Precision/Recall/漏事件率/交易收益仍 `unavailable`。
