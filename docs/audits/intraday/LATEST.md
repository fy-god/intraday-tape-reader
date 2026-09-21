# 最新审计

**最新云端独立审计**：[`2026-09-21_12-02-53_JST.md`](./2026-09-21_12-02-53_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-21_12-02-53_JST_AGENT_TASK.md`](./2026-09-21_12-02-53_JST_AGENT_TASK.md)  
**最新本地 Agent 轮**：[`2026-09-21_11-31-34_JST.md`](./2026-09-21_11-31-34_JST.md)  
**上一份云端独立审计**：[`2026-09-21_08-04-12_JST.md`](./2026-09-21_08-04-12_JST.md)  
**上一份云端 Agent 任务书**：[`2026-09-21_08-04-12_JST_AGENT_TASK.md`](./2026-09-21_08-04-12_JST_AGENT_TASK.md)  
**上一份本地 Agent 轮**：[`2026-09-21_03-38-00_JST.md`](./2026-09-21_03-38-00_JST.md)  
**更早云端审计**：[`2026-09-21_04-10-59_JST.md`](./2026-09-21_04-10-59_JST.md)  
**下一步计划**：[`NEXT_STEPS.md`](./NEXT_STEPS.md)  
**仓库执行清单**：[`RUN_MANIFEST.json`](./RUN_MANIFEST.json)

> 历史审计文件均保留在本目录；本索引只移动“最新/上一份”指针，不删除历史报告。

## 本轮云端独立审计（2026-09-21 12:02:53 JST）

- `reviewed_source_sha` / 最新产品提交：`4e73ded6b1fe12e3d4eaa15fb8971098124bebe9`
- 发布前默认分支 HEAD：`70af044d7a9df9e122d8b8490e68fe667db85548`（产品提交之后均为 `docs/audits/intraday/` 文档）
- 开放 PR：0
- 本轮范围：市场制度日期/限价来源语义、replay、Alert Truth Contract、per-code provenance/soft-partial、window/SSE 续项；没有改远端产品代码。

### 本轮最高优先级结论

1. **新确认 `IT-P1-LIMIT-PROVENANCE-001`（P1）。** `Quote.limit_up/limit_down` 同一个字段同时承载“provider 明确给出的真实限价”和“replay 本地推导限价”。`Quote.limit_up_price` 对任何正值都无条件短路返回；而 replay 先用无日期的 `limit_rate_of(code,name)` 算出 10% 限价再塞入 `Quote.limit_up`，因此这份本地推导值被伪装成“显式权威限价”，结构性绕过日期感知 fallback。正确修法是给 limit evidence 增加来源/交易日语义，而不是只在 `_as_date` 上打补丁。
2. **更正 `IT-P1-UNKNOWN-DATE-FAILOPEN-001` 的影响边界。** `_as_date("2025-03-10")` / `20250310` 静默落 `None` 的函数行为为真，但当前内置 `Quote.ts` 生产路径（腾讯/新浪/东财）输出的是 `datetime | None`；`server/web.py::coerce_ts` 是 **Alert feed 重建**，不是 Quote/市场制度链，因此不能作为 ST 限价路径的 str/int 可达性证据。可达的 `None` 仍真实存在，应显式记“交易日期未知”，但不要把尚未证明可达的 str/int 放大成当前实盘故障。
3. **市场制度的 trade date 应与 provider timestamp 解耦。** 仓库自己的 `source_time_contract.json` 对三家 provider time role 都是 `unknown`；与此同时 Engine/RuleContext 已经有独立决策时钟 `now`，replay 也有 timeline。建议建立 `MarketRuleEvidence`：`decision_trade_date / market_rule_version / limit_source / date_known`，provider ts 只作为时间证据，不再单独决定市场制度版本。
4. **`market_rule_version` 仍是零消费者。** 若保留“制度版本可追溯”能力，应把它真正落到 committed event / future-label 数据中；否则删除能力声明。未来 T+5/T+30 数据必须带 `decision_trade_date + market_rule_version + limit_source`，避免跨 2026-07-06 制度样本静默混合。
5. **Alert Truth Contract 仍是研究前置。** `mark_published` 仍早于 `AlertBus.accept`，Notifier bool 仍被 Engine 丢弃，stable `signal_id/event_id` 仍缺失；真实 future labels 只能从 `committed event_id` 开始。

### 本轮优先实验

`EXP-IT-QUAL-002-alert-cluster-denominator`：只做软件统计机制检查，不是市场回测。构造 250 个独立事件簇、2671 条重复 `rule_selected` 行、每簇 1 条 `committed`。逐行 IID 口径把有效样本量放大约 **10.684×**，naive SE 约为 event-cluster SE 的 **29.8%**。结论：未来真实 T+5/T+30 认证必须以 `committed event_id / event cluster` 为基本单位并做 group/block bootstrap，不能按 rule-selected/tick 行做 IID 统计。

### 继续开放

- `IT-P1-ST-REPLAY-BYPASS-001`：纳入 `IT-P1-LIMIT-PROVENANCE-001` 的一个可达表现；修 replay 必须传交易日并避免本地推导值冒充 provider explicit。
- `IT-P1-EVAL-PUBLISH-001`
- `IT-P1-NOTIFY-RESULT-001/002`
- `IT-P1-ALERT-IDENTITY-001`
- `IT-P1-CAPABILITY-002`
- `IT-P1-SOURCE-EMPTY-001`
- `IT-P1-WINDOW-001`
- `IT-P1-008/009/003`（SSE durable delivery / 慢客户端 gap / series 有界）
- `IT-P1-NEWLIST-002-R1`

### 下一轮必须回传的实物

```text
market_rule_red.log / green.log / rollback.log
market_rule_replay_reconcile.json
market_rule_date_matrix.json
limit_provenance_cases.json
alert_stage_red.log / green.log / rollback.log
alert_delivery_reconcile.json
notification_delivery_reconcile.json
per_code_provenance.json
provider_coverage.csv
full pytest log
RUN_MANIFEST.json
NEXT_STEPS.md
```

本轮没有真实多日连续 A 股语料，也没有重新训练模型；真实 Precision / Recall / 漏事件率 / 交易收益继续 `unavailable`。
