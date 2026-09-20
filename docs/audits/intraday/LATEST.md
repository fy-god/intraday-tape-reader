# 最新审计

**最新云端独立审计**：[`2026-09-21_04-10-59_JST.md`](./2026-09-21_04-10-59_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-21_04-10-59_JST_AGENT_TASK.md`](./2026-09-21_04-10-59_JST_AGENT_TASK.md)  
**最新本地 Agent 轮**：[`2026-09-21_02-24-00_JST.md`](./2026-09-21_02-24-00_JST.md)  
**上一份本地 Agent 轮**：[`2026-09-21_00-52-00_JST.md`](./2026-09-21_00-52-00_JST.md)  
**上一份云端独立审计**：[`2026-09-21_01-40-00_JST.md`](./2026-09-21_01-40-00_JST.md)  
**下一步计划**：[`NEXT_STEPS.md`](./NEXT_STEPS.md)  
**仓库执行清单**：[`RUN_MANIFEST.json`](./RUN_MANIFEST.json)

## 当前版本与发布

- 审计时间：2026-09-21 04:10:59 JST。
- `reviewed_source_sha`：[`9c4e08d`](https://github.com/fy-god/intraday-tape-reader/commit/9c4e08d8f3f0cf00e02ab787691ba8910789a35a)。该 SHA 本身是产品提交，不是审计文档提交。
- 当前产品提交归档：**1545 passed / 0 failed**、8/8 gate；这是本地 Agent/产品提交的机器证据，本轮云端没有完整 checkout，因此未独立复跑。
- 本轮完整报告 commit：[`4154211`](https://github.com/fy-god/intraday-tape-reader/commit/4154211a44411f8c5f4c59149ae71dfe974ce1f0)。
- 本轮 Agent 任务书 commit：[`353e041`](https://github.com/fy-god/intraday-tape-reader/commit/353e041096ec0b439c3d825a0afa124c51125956)。
- 报告/任务书提交只写 `docs/audits/intraday/`；不计作产品升级。

## 本轮最高优先发现

1. **`IT-P1-MARKET-RULE-20260706-001` — 未修。** 代码仍把沪深主板 ST/*ST fallback 涨跌幅写死为 ±5%，但上交所/深交所现行规则自 **2026-07-06** 起均已调整为 ±10%。Tencent 显式 `limit_up/down` 可能覆盖 fallback，但 Sina 明确不提供限价，因此主源切到 Sina 时会直接走过期 5% 推算。修复必须日期感知，不能永久把常量改成 10%，否则历史 replay 会错。
2. **`IT-P1-EVAL-PUBLISH-001` — 未修。** `volume_burst` / `spirit_order` 在规则内部 `max_per_round` 后就 `mark_published`，而 Engine 之后才经过 `AlertBus.accept` 的 key 去重/cooldown。机制反例：24 轮连续命中可记 `published=24`，实际 bus accepted / Store committed 只有 1。必须拆 `rule_selected -> bus_accepted -> committed -> sent`。
3. **`IT-P1-CAPABILITY-002-R1` — 未修。** capability 仍是 round-global；当 source 声称 L5 可用、但某条 Quote 实际五档缺失时，相关 pattern 可能既不 blocked 也不 evaluated，直接从 considered 分母消失。实现 soft-partial mixed-source 前必须先有 per-code provenance/observed-field availability。
4. **`IT-P1-SOURCE-EMPTY-001` — 未修。** SourceManager 仍是 exception-driven failover，soft-partial 不会补洞；必须依赖 per-code provenance 后再做 targeted fallback。
5. **`IT-P1-NEWLIST-002-R1` — 未修。** N/C 只覆盖前 5 日；配置 `min_list_days=11` 的第 6~10 日在 Tencent 无 `list_date` 链路上仍无可靠过滤。

## 本轮研究推进

优先实验改为 **`EXP-IT-QUAL-001-post-alert-matched-control`**。本轮没有继续扩神经网络，而是把真实告警质量代理的标签合同冻结为 T+5/T+30 signed return + matched controls + event cluster + `known_at`/unknown。隔离沙箱只做了 synthetic 软件 smoke，证明流水线字段和因果边界可运行；这些 synthetic 数字**不进入策略成绩**。

下一轮只有在 `IT-P1-EVAL-PUBLISH-001` 修完、可以识别真实 `committed event` 后，才允许把真实告警接到 future-label 流水线。真实 Precision / Recall / 漏事件率 / 交易收益目前仍为 `unavailable`。

## 下一轮必须检查的实物

```text
market_rule_red.log / green.log / rollback.log
market_rule_matrix.json
alert_stage_red.log / green.log / rollback.log
alert_delivery_reconcile.json
per_code_provenance.json
soft_partial_reconcile.json
listing_date_reconcile.json
alert_event_ledger.csv
forward_labels.csv
matched_controls.csv
quality_proxy_by_signal.csv
bootstrap_ci.json
full pytest log
RUN_MANIFEST.json
NEXT_STEPS.md
```

更早审计与产品修复均保留在本目录及 Git 历史中；本索引只保留当前执行所需的关键接续线索，避免将旧报告长文重复复制到 `LATEST.md`。
