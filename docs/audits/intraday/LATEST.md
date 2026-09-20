# 最新审计

**最新云端独立审计**：[`2026-09-21_08-04-12_JST.md`](./2026-09-21_08-04-12_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-21_08-04-12_JST_AGENT_TASK.md`](./2026-09-21_08-04-12_JST_AGENT_TASK.md)  
**最新本地 Agent 轮**：[`2026-09-21_03-38-00_JST.md`](./2026-09-21_03-38-00_JST.md)  
**上一份云端独立审计**：[`2026-09-21_04-10-59_JST.md`](./2026-09-21_04-10-59_JST.md)  
**下一步计划**：[`NEXT_STEPS.md`](./NEXT_STEPS.md)  
**仓库执行清单**：[`RUN_MANIFEST.json`](./RUN_MANIFEST.json)

## 当前版本与发布

- 本索引更新时最新产品提交：`4e73ded6b1fe12e3d4eaa15fb8971098124bebe9`。
- 产品提交归档机器证据：**1579 passed / 0 failed**；该数字来自产品提交/本地 Agent 归档，本轮云端没有完整 checkout，因此没有冒充重新执行。
- `4e73ded` 之后的 `3615eed` 仅补审计 Markdown，不算产品升级。
- 本轮云端 `reviewed_source_sha`：`4e73ded6b1fe12e3d4eaa15fb8971098124bebe9`。
- 本轮完整报告提交：`1838bee1119c6e7b68de6fcb3e18d6f24c8c2bf0`。
- 本轮 Agent 任务书提交：`f7408c53aa3c24b4b549c08587235ca8b1f9bce2`。
- 当前开放 PR：0。

## 本轮最高优先级结论

1. **`IT-P1-EVAL-PUBLISH-001` 仍未修。** 规则内的 `published` 发生在 `AlertBus.accept` 之前，实际语义仍是 `rule_selected`。真实 T+5/T+30 标签不能以它作为“已发布事件”分母。
2. **新确认 `IT-P1-NOTIFY-RESULT-001`。** Notifier 协议明确返回成功 bool，且内置通知器按约定“失败返回 False、绝不抛异常”；但 Engine `_dispatch_many()` 完全丢弃 bool，因此 HTTP/业务失败无法进入统一告警阶段账本。
3. **新确认 `IT-P1-NOTIFY-RESULT-002`。** bool 本身也不够：disabled / severity skip 等场景可以返回 True，但并未物理发送。下一步需要 typed `sent / skipped_* / failed`，不能把 True 等价为 sent。
4. **新确认 `IT-P1-ALERT-IDENTITY-001`。** `Alert`/`to_dict` 没有显式稳定 `signal_id/event_id`，不利于 rule→bus→store→notifier→client→future-label 的可追溯对账。
5. **`IT-P1-CAPABILITY-002`、`IT-P1-SOURCE-EMPTY-001`、`IT-P1-WINDOW-001`、`IT-P1-008/009/003`、`IT-P1-NEWLIST-002-R1` 继续开放。** per-code provenance 仍是 soft-partial mixed-source 的前置。

## 主改造方向：Alert Truth Contract v2

```text
hit_candidate
→ rule_selected
→ bus_accepted
→ committed(event_id)
→ delivery_attempted(channel)
→ delivery_sent / delivery_skipped / delivery_failed
→ client_received / client_applied
→ future_label_known
```

冻结原则：

- `rule_selected <= hit_candidate`
- `bus_accepted <= rule_selected`
- `committed <= bus_accepted`
- 每个 `committed event × channel` 必须恰有 `sent / skipped / failed` 之一
- T+5/T+30 标签只能引用 `committed event_id`

本轮沙箱执行了 notifier-result 机制探针和一组确定性阶段账本 stress；这些是**软件合同测试**，不是市场策略指标。本轮没有重新训练模型，真实 Precision / Recall / 漏事件率 / 交易收益仍为 `unavailable`。

## 下一轮必须检查的实物

```text
alert_stage_red.log / green.log / rollback.log
notification_result_red.log / green.log / rollback.log
alert_delivery_reconcile.json
notification_delivery_reconcile.json
signal_event_identity.json
post_alert_label_manifest.json
per_code_provenance.json
soft_partial_reconcile.json
full pytest log
all check_*.py logs
dash_render_check log
RUN_MANIFEST.json
NEXT_STEPS.md
```

更早审计、产品修复及本地执行记录继续保留在本目录和 Git 历史中；本索引只保留当前接续所需的关键状态。
