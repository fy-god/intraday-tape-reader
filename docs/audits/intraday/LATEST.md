# 最新审计

**最新云端独立审计**：[`2026-09-23_12-37-02_JST.md`](./2026-09-23_12-37-02_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-23_12-37-02_JST_AGENT_TASK.md`](./2026-09-23_12-37-02_JST_AGENT_TASK.md)  
**最新独立实测审计**：[`2026-09-23_09-05-00_JST.md`](./2026-09-23_09-05-00_JST.md)  
**最新本地 Agent 产品轮**：[`2026-09-22_21-00-00_JST.md`](./2026-09-22_21-00-00_JST.md)  
**最新产品提交 / reviewed_source_sha**：`bf0b83bf212f169c720ca8a1c5108405c18a9712`  
**本轮审计开始 docs HEAD**：`fea5cb2efe2ce9a0749fb5e6aaec0603f91a3820`  
**report_commit_sha**：`d3de6e26a43c6cc66bf14a07de96c50b31151547`  
**agent_task_commit_sha**：`35aad023355b8d3a55fb59980b7a88d5786b4223`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/fea5cb2efe2ce9a0749fb5e6aaec0603f91a3820/docs/audits/intraday/LATEST.md

> `bf0b83b..fea5cb2` 后续仍仅为审计文档，没有产品源码变化。历史报告文件均保留；本文件只移动当前接续指针。

## 2026-09-23 12:37:02 JST

主实验：`EXP-IT-SNAPSHOT-CROSS-SOURCE-010`

### 本轮新增最高优先

`IT-P1-SNAPSHOT-OUTCOME-SOURCE-SEMANTICS-DRIFT-001`：默认 stock route 是 Tencent primary → Sina fallback。固定源码中 Tencent 对 `price<=0` 停牌行保留 Quote，Engine 因而能记 `rejected_quality`；Sina 对同类 raw row 在 parser 内 `continue`，Engine 只能记 `unknown_missing`。同一个 raw-present-but-unusable 市场事实会因 serving source 改变账本语义。

### 主裁决

12:11 的 Snapshot Raw Outcome 优先顺序保持正确，但**不能只改 Eastmoney**。Snapshot outcome 必须成为 Tencent/Sina/Eastmoney 三个内置 stock source 的统一合同，或者显式标记 `raw_presence_known=False`，不能把 quote projection 冒充 exact provider absence。

### 新 P2 护栏

`IT-P2-SNAPSHOT-RAW-SET-ALGEBRA-001`：detailed raw result 必须按 requested set 做交集并单独记录 unexpected / duplicate。`returned` 不能用 raw row count，也不能用未过滤 raw unique count，否则重复/额外返回会让 returned>requested 或掩盖 requested missing。

### 主改造

**Snapshot Outcome Contract v4**

```text
Requested Identity Set
        ↓
Source Raw Outcome
        ↓
Quote Usability
        ↓
Observation Ledger
        ↓
Membership / Failover / Quality Factors
```

下一轮：三源 detailed RED/GREEN/rollback → Engine ledger migration → Membership 5923/5553 → soft-empty → correctness benchmark → pagination/timezone/window → research。

模型训练：0；真实 Precision/Recall/漏事件率/收益 `unavailable`。
