# 最新审计

**最新云端独立审计**：[`2026-09-22_20-04-38_JST.md`](./2026-09-22_20-04-38_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-22_20-04-38_JST_AGENT_TASK.md`](./2026-09-22_20-04-38_JST_AGENT_TASK.md)  
**最新本地 Agent 产品轮**：[`2026-09-22_17-00-00_JST.md`](./2026-09-22_17-00-00_JST.md)  
**最新本地审计追加-2**：[`2026-09-22_16-20-44_JST_ADDENDUM2.md`](./2026-09-22_16-20-44_JST_ADDENDUM2.md)  
**最新产品提交 / reviewed_source_sha**：`6c8f11e363b3efcd21e410a5af7a718e08e8564d`  
**本轮审计开始 docs HEAD**：`0816294d6b38ff1a788acd262c3734ecace6ae71`  
**report_commit_sha**：`ba52e1080b9b0e6fc352ed9bf54c537dd9f396d4`  
**agent_task_commit_sha**：`359b781a5658590aba604302dbc185445e8314cb`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/0816294d6b38ff1a788acd262c3734ecace6ae71/docs/audits/intraday/LATEST.md

> 历史报告文件没有删除或覆盖；上一版完整长索引保留在不可变 commit。
> 重要更正：上一版仍把 `c4f2ce1...` 写作最新产品；`6c8f11e...` 是其后的真实产品修复提交，
> 而 `6c8f11e...0816294` 只有 docs(audit) 变化。

## 2026-09-22 20:04:38 JST

主实验：`EXP-IT-SESSION-EVIDENCE-005`

### 本轮主要结论

1. `6c8f11e` 的 tri-state / session consumer / watchlist 1/30&29/30 / freshness 修复继续维持 **已修**。
2. 旧报告 `_w_ever is None` guard 是**正确活代码**，必须保留；真正开放的是新 schema 的 zero-evidence / empty-scan 正向断言。
3. `IT-P1-UNIVERSE-TRANSPORT-T0-SUPPRESSED-001`：t0 explicit False 可被 session all-complete 覆盖。
4. `IT-P1-UNIVERSE-ACTIVE-COVERAGE-SESSION-BLIND-001`：本轮新确认；t0 98.3% 后 session active coverage 可降到 75%，final health 仍只读 t0 coverage。
5. 主改造：**Session Universe Evidence Contract v1**，按 scope / transport / active coverage / freshness 各轴合并时间线证据。
6. 270 deterministic contract cases 中 current/candidate 有 78 个严重度分歧；仅为软件合同输入空间，不是生产发生率。
7. 候选合同 288 个 hard-negative monotonicity checks / 0 violations；同样不是市场统计。
8. 模型训练=0；真实 Precision/Recall/收益 unavailable。

### 下一轮最高优先

- zero-evidence scope not_measured；
- t0 transport hard negative fail-dominant merge；
- per-round active coverage + session min/p05/last；
- old-report compatibility；
- 然后 membership/Quote usability、soft-empty source failover、timezone、ObservationInterval。
