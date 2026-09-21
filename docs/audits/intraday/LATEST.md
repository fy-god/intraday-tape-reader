# 最新审计

**最新本地 Agent 轮**：[`2026-09-21_11-31-34_JST.md`](./2026-09-21_11-31-34_JST.md)  
**最新云端独立审计**：[`2026-09-21_08-04-12_JST.md`](./2026-09-21_08-04-12_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-21_08-04-12_JST_AGENT_TASK.md`](./2026-09-21_08-04-12_JST_AGENT_TASK.md)  
**上一份本地 Agent 轮**：[`2026-09-21_03-38-00_JST.md`](./2026-09-21_03-38-00_JST.md)  
**上一份云端独立审计**：[`2026-09-21_04-10-59_JST.md`](./2026-09-21_04-10-59_JST.md)  
**下一步计划**：[`NEXT_STEPS.md`](./NEXT_STEPS.md)  
**仓库执行清单**：[`RUN_MANIFEST.json`](./RUN_MANIFEST.json)

## 最新本地 Agent 轮结论（2026-09-21 11:31:34 JST，`ce989ca`）

1. **新确认 `IT-P1-ST-REPLAY-BYPASS-001`（P1，本轮最高优先级）。** ST 制度修复（`22a51a2`）在 `Quote` 路径上**正确**（`models.py:354` 传 `self.trade_date`，边界 2026-07-06 实测正确），但 **replay 路径结构性绕过它**：`replay.py:160 return limit_rate_of(self.code, self.name)` **不传日期** → 永远取现行 10%；`replay.py:249-251` 把该值算成 `limit_up/limit_down` 并塞进 `Quote`；而 `models.py:349-350` `if self.limit_up and self.limit_up > 0: return self.limit_up` **提前返回**，日期感知分支永不执行。
   端到端反例（真跑）：同一只主板 ST 股、`start_price=10.00`，回放日 2025-03-10 得 `limit_up_price=11.0`（应为 **10.50**）；`price=10.50` 时 `is_at_limit_up()=False`（应为 **True**）——**真实 5% 封板不被识别**。`limit_board.py:115/155` 真实消费该值。
   **影响面如实限定**：实盘当日（今天 2026-09-21，`CURRENT==0.10`，`when=None` 亦为 0.10）**不受影响**；仅「历史回放 + 预置 limit_up + 回放日 < 2026-07-06」三条件同时成立时出错。真实受影响 ST 股票数**未计数**。
2. **新确认 `IT-P2-REPLAY-DOC-COVERAGE-001`。** `replay.py:178` docstring 声称默认池「板块覆盖主板/创业板/科创板/北交所与 **ST**」，实测 `default_universe(30, seed=42)` 中**名字含 ST 的股票 = 0 只**。故 §1 的缺陷在默认参数下无测试覆盖（自定义 `ScriptedStock` 时立即成立）。
3. **新确认 `IT-P2-LEGACY-STATE-SLOT-001`（待验证风险／当前不可达）。** 按端口分文件（`4e73ded`）**核心修复为真**（两端口文件互不干扰；`tmp.replace()` 原子），但 `_write_state` 仍在 `run_daemon.py:136-138` 每次覆写旧单槽 `arad-daemon.json`，「最后写入者获胜」症状被移到无端口路径。**已如实降级**：`--port` 有 `default=8899`，`main()` `:522-523` 必然传 int，`_read_state(None)` 分支当前**不可达**；且 `:105` 有 port 匹配保护，删文件后实测返回 `{}`（无串号）。标记为「待验证风险」而非实际故障。
4. **云端报告 `2026-09-21_08-04-12_JST.md` 的 3 项声明经独立复核全部为真**（`mark_published` 先于 `bus.accept`；`_dispatch_many` 丢弃 bool；内置 notifier 在 disabled/severity-skip 时返回 True）。其 `IT-P1-NOTIFY-RESULT-001/002`、`IT-P1-EVAL-PUBLISH-001` 应予采纳。
5. **回归：STILL_OPEN 8 / FIXED 2 / NOT_REPRODUCED 0。** FIXED = `IT-P1-CAPABILITY-004`（L1 数量兜底 `spirit_order.py:1080-1089`）、`IT-P2-OBS-STATUS-001`（`store.py:152-161` 单临界区，12 passed）。**三处旧引用已漂移需更正**：`capabilities.py:227-229`（现为 `evaluable_coverage`，非 `provides`）、`spirit_order.py:547-550`、`sina.py:221-225`。
6. **真实测试**：冻结树 `D:\ccc\_sched\work\asr-h04` @`ce989ca` 跑 `pytest -o addopts="" -p no:cacheprovider -q --ignore=reference` → **1579 passed in 108.71s**，exit 0。

> 并发写者提示：主工作树仍有 8 个他人未提交文件，本轮全程用 `git worktree` 隔离，未读未改未提交他人 WIP。

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
