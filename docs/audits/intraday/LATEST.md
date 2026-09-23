# 最新审计

**最新云端独立审计**：[`2026-09-23_16-11-07_JST.md`](./2026-09-23_16-11-07_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-23_16-11-07_JST_AGENT_TASK.md`](./2026-09-23_16-11-07_JST_AGENT_TASK.md)  
**最新独立实测审计**：[`2026-09-23_15-52-52_JST.md`](./2026-09-23_15-52-52_JST.md)  
**最新产品提交 / reviewed_source_sha**：`bf0b83bf212f169c720ca8a1c5108405c18a9712`  
**本轮审计开始 docs HEAD**：`37988fe8294a034340546dfc55838e2af2bf9885`  
**report_commit_sha**：`8f517cb9b2fec2c33fa7c8f87819b05db5440998`  
**agent_task_commit_sha**：`0b6aa866b6df08d224f1343bbd5ccc7ef14b92c5`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/37988fe8294a034340546dfc55838e2af2bf9885/docs/audits/intraday/LATEST.md

> `bf0b83b..37988fe` 全部为 `docs/audits/intraday/` 文档；历史报告未删除。本文件只移动当前接续指针。

## 2026-09-23 16:11:07 JST

主实验：`EXP-IT-ROUND-ATTRIBUTION-011`

### 15:52 独立实测接续

- `IT-P1-SUITE-TIMING-ASSERTION-NONDETERMINISTIC-001`：同一源码树完整 pytest 3 次出现 **1 红 / 2 绿**；唯一失败是 `test_performance_1000_stocks` 的 one-shot `elapsed < 0.2`（214.3ms）。默认 correctness gate 需要与 performance benchmark 分离。
- `IT-P2-EMPTY-ROUND-LEDGER-IDENTITY-UNATTRIBUTED-001`：空轮已可观测，但 `requested>0 / returned=admitted=0` 时没有 terminal attribution，独立复现示例 `5913 != 0`。
- Eastmoney unknown-total pagination 已有真实 scratch RED/GREEN；应固化成仓库测试并落小 patch。

### 本轮主裁决

**不能用 `quotes==[] => 全部 unknown_missing` 修空轮 ledger。** 空 Quote list 可能是：
1. provider raw 真空；
2. raw rows 全部在 parser 因质量被丢；
3. legacy source 根本没有 raw-presence 证据。

主改造：**Round Attribution Contract v1 / Snapshot Outcome v4.1**：新增 `raw_presence_known` 与 `unattributed_requested`。只有 exact raw absence 才叫 `unknown_missing`。

### 当前优先顺序

1. Tencent/Sina/Eastmoney 三源 `SnapshotFetchResult` detailed adapter；
2. Round attribution 参数化不变量（含 empty/all-quality/legacy-unknown）；
3. live-session `unattributed_total/unattributed_rounds`；
4. Eastmoney pagination + pages_requested 小修；
5. 默认 pytest 去掉 tight wall-clock correctness gate，性能另跑 benchmark；
6. 然后 Membership 5923/5553 → SourceManager exact soft-empty → timezone/window/provenance → research。

### 下一轮关键产物

- `snapshot_outcome_cases.json`
- `round_attribution_cases.json`
- `pagination_cases.json`
- `correctness_suite_3x.log`
- `performance_benchmark.log`
- `full_pytest.log`
- `check_*.py` logs
- `RUN_MANIFEST.json`
- `NEXT_STEPS.md`

模型训练：0；真实 Precision/Recall/漏事件率/收益 `unavailable`。
