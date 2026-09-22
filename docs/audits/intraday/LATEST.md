# 最新审计

**最新云端独立审计**：[`2026-09-23_00-12-42_JST.md`](./2026-09-23_00-12-42_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-23_00-12-42_JST_AGENT_TASK.md`](./2026-09-23_00-12-42_JST_AGENT_TASK.md)  
**最新本地 Agent 产品轮**：[`2026-09-22_21-00-00_JST.md`](./2026-09-22_21-00-00_JST.md)  
**最新产品提交 / reviewed_source_sha**：`bf0b83bf212f169c720ca8a1c5108405c18a9712`  
**本轮审计开始 docs HEAD**：`04abd419e74fb5c641f9800886fcdfa58b326f62`  
**report_commit_sha**：`83841da3e92080d0504177605d758b06d9319568`  
**agent_task_commit_sha**：`4176f5c86f800afcf7bbfcf6345535ed27bfc3d8`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/04abd419e74fb5c641f9800886fcdfa58b326f62/docs/audits/intraday/LATEST.md

> 产品版本更正：上一版 LATEST 仍写 `6c8f11e...`；当前最新真实产品提交已经是
> `bf0b83bf212f169c720ca8a1c5108405c18a9712`。`bf0b83b..04abd419` 只有 docs 变化。
> 历史报告文件均保留；本文件只移动接续指针，上一版完整长索引固定在上面的不可变 commit。

## 2026-09-23 00:12:42 JST

主实验：`EXP-IT-EVIDENCE-COMPLETENESS-006`

当前最高优先：
1. `IT-P1-UNIVERSE-T0-MEASURED-ZERO-READ-AS-MISSING-001`
2. `IT-P1-UNIVERSE-ABS-SIZE-SESSION-BLIND-001`（本轮新）
3. `IT-P1-UNIVERSE-COVERAGE-SESSION-WITHOUT-T0-001`（本轮新）
4. `IT-P2-UNIVERSE-FRESHNESS-POSITIVE-USES-EVIDENCED-SUBSET-001`（按 P1 执行）
5. `IT-P2-UNIVERSE-FULL-MARKET-IS-MAGNITUDE-BLIND-001`
6. `IT-P1-UNIVERSE-MEMBERSHIP-QUALITY-001`
7. `IT-P1-SOURCE-EMPTY-001`
8. `IT-P0-002-TZ-R1`（当前严重度 P1）
9. `IT-P1-WINDOW-001`

### 本轮新增机制证据

- `summarize_rounds()` 已经有 `metrics['universe'].min`，但绝对 `universe` health 只读 t0 `setup.universe_size`：
  t0=5000、session min=2000、denominator unknown 时，session 绝对缩池缺少 consumer。
- session active coverage 的 merge 仍位于 `if setup.universe_truth` 分支内：
  t0 truth 缺失但 session 已测到 75% coverage 时，当前 consumer 仍可能按“未测量”跳过。
- 22:45 已验证：`universe_size=0` + denominator unknown 可全绿；`active>0` 被 scope 直接命名为 `full_market`，不看 magnitude。
- 22:15 已验证：1/30 fresh evidence 仍可产生整个 session 的 fresh 肯定句。

### 主改造

**Universe Evidence Completeness Contract v2**：
- `None != 0`；
- universal positive wording 必须 `measured_rounds == total_rounds`；
- `full_market` 必须有 magnitude/coverage 证据；
- t0 与 session 同轴证据独立解析后取最坏显式证据；
- session absolute min 与 session numeric coverage 都必须有 consumer。

### 本轮本地软件实验

枚举 `scope evidence × freshness evidence × session min active size × denominator` 共 **128** 个 deterministic contract cases；候选合同在其中 62 个格子更严格。**62/128 不是 bug 率、线上故障率或发生概率**，只表示人工枚举状态空间中的判决差异。

### 下一轮关键产物

- `universe_evidence_cases.json`
- `membership_cases.json`
- `source_empty_cases.json`
- `timezone_cases.json`
- `observation_interval_cases.json`
- `per_code_provenance.json`
- `full_pytest.log`
- `check_*.py` logs
- `RUN_MANIFEST.json`
- `NEXT_STEPS.md`

模型训练：0；真实 Precision/Recall/漏事件率/交易收益仍 `unavailable`。
