# 最新审计

**最新云端独立审计**：[`2026-09-23_12-11-56_JST.md`](./2026-09-23_12-11-56_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-23_12-11-56_JST_AGENT_TASK.md`](./2026-09-23_12-11-56_JST_AGENT_TASK.md)  
**最新独立实测审计**：[`2026-09-23_09-05-00_JST.md`](./2026-09-23_09-05-00_JST.md)  
**最新本地 Agent 产品轮**：[`2026-09-22_21-00-00_JST.md`](./2026-09-22_21-00-00_JST.md)  
**最新产品提交 / reviewed_source_sha**：`bf0b83bf212f169c720ca8a1c5108405c18a9712`  
**本轮审计开始 docs HEAD**：`bd5528485e045bb6fadadecb8fbcc0764baf3824`  
**report_commit_sha**：`c22008756257febfdb2d466142fde2dc79ce6383`  
**agent_task_commit_sha**：`6fe2f3cd9f821971938c305f51f8702d862d0d93`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/bd5528485e045bb6fadadecb8fbcc0764baf3824/docs/audits/intraday/LATEST.md

> `bf0b83b..bd552848` 后续全是审计文档，没有产品源码变化。历史报告文件不删除；本文件只移动当前接续指针，上一版完整长索引固定在上面的不可变 commit。

## 2026-09-23 12:11:56 JST

主实验：`EXP-IT-SNAPSHOT-OUTCOME-009`

### 本轮最高优先

1. `IT-P1-SNAPSHOT-RAW-LEDGER-COLLAPSE-001`：Eastmoney UList 在 source 内先投影为 `list[Quote]`，导致“raw row 已返回但行情值不可用”与“provider 完全没返回”在进入 Engine 前合并；现有 `unknown_missing / rejected_quality` 合同因此失真。
2. `IT-P1-UNIVERSE-MEMBERSHIP-QUALITY-001`：Membership P1 已由 09:05 真实 Engine 实验确认，但不能只改 `_codes`；必须先让 Snapshot Outcome 能区分 raw-present-invalid 与 raw-absent。
3. `IT-P1-SOURCE-EMPTY-001`：detailed snapshot outcome 完成后，可用 `raw_returned_codes==0` 更安全地定义 soft-empty failover，避免把“全部 Quote 质量不可用”误当源断供。
4. `IT-P2-EASTMONEY-UNKNOWN-TOTAL-USABLE-EMPTY-STOPS-PAGINATION-001`、`IT-H08-PAGES-REQUESTED-UNDERCOUNTS-IN-NO-TOTAL-BRANCH-001`：09:05 已真实离线执行确认。
5. `IT-P2-UNIVERSE-COVERAGE-OVERLAP-OUT-OF-RANGE-001`：正常翻页重叠可使 ratio>1；live-session 将其静默视为未测量，保持 P2。
6. `IT-H08-NEWLIST-DAY6-10-UNPROTECTED-WHEN-NEVER-USABLE-001`：宽泛 list_date 丢失已被 09:05 反证；只保留 never-ever-usable 新股 day6–10 窄风险待产品 RED。

### 本轮裁决

- `C_active=1.0` 在“全部 market members 被主动安排扫描”的定义下是合法结果；不可用性应由 `C_usable / rejected_quality / provider_missing / C_round` 表达，不应为了保留旧的 93.75% 数值继续把真实成员排除出 active scan。
- 暂不新增 `membership_no_quote` 桶；现有 `rejected_quality` 已表达“provider 返回但质量不可用”，真正缺的是 source-level raw snapshot outcome。
- 目前没有证据证明 370 个停牌成员在真实 Eastmoney raw HTTP 响应里一定完全无 row，也不能把额外轮询直接称为“纯开销”。先修正确性，再做真实 batch/latency/恢复延迟 benchmark。

### 主改造

**Membership + Snapshot Outcome Contract v3**：

```text
Universe Transport Membership
        ↓
Active Scan Membership
        ↓
Snapshot Raw Outcome
        ↓
Snapshot Quote Usability
        ↓
Observation Ledger
        ↓
Rule Evaluability
```

source detailed result 至少保留：`requested_codes / raw_returned_codes / quotes / rejected_quality_codes / provenance`；Engine 中 `unknown_missing` 只表示请求 code 在 raw response 中确实缺席。

### 下一轮

Snapshot Outcome 真实 RED/GREEN/rollback → Observation Ledger migration → Membership 5923/5553 fixture → Source soft-empty → 成本 benchmark → Pagination/overlap P2 → Timezone/ObservationInterval → provenance/research。

模型训练：0；真实 Precision/Recall/漏事件率/交易收益仍 `unavailable`。
