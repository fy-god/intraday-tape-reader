# 最新审计

**最新本地 Agent 轮**：[`2026-09-21_17-00-00_JST.md`](./2026-09-21_17-00-00_JST.md)  
**最新云端独立审计**：[`2026-09-21_16-07-37_JST.md`](./2026-09-21_16-07-37_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-21_16-07-37_JST_AGENT_TASK.md`](./2026-09-21_16-07-37_JST_AGENT_TASK.md)  
**上一份本地 Agent 轮**：[`2026-09-21_13-37-29_JST.md`](./2026-09-21_13-37-29_JST.md)  
**上一份本地 Agent 产品轮**：[`2026-09-21_13-00-00_JST.md`](./2026-09-21_13-00-00_JST.md)  
**上一份云端独立审计**：[`2026-09-21_12-02-53_JST.md`](./2026-09-21_12-02-53_JST.md)  
**上一份云端 Agent 任务书**：[`2026-09-21_12-02-53_JST_AGENT_TASK.md`](./2026-09-21_12-02-53_JST_AGENT_TASK.md)  
**下一步计划**：[`NEXT_STEPS.md`](./NEXT_STEPS.md)  
**仓库执行清单**：[`RUN_MANIFEST.json`](./RUN_MANIFEST.json)

> 历史审计文件均保留在本目录；本索引只移动当前接续指针，不删除历史报告。

## 2026-09-21 17:00 JST 本地 Agent 轮（产品改动：交付账本解耦）

- 起点 HEAD：`948e0d2aaa29f8b7246225f6e45fdba6496f8551`；`reviewed_source_sha` = `eba8c4af5ed943a023ab09ab9c30000c1ac58f65`。
- 归档机器证据：**1660 passed in 110.07s**（本轮本地真实执行）；`tools/check_*.py` **8/8**；`dash_render_check.js` 通过；`selftest` **68 条 / 6 类型 / 8-8 剧本命中**。
- 回退验牙：**12 条行为级 RED / 0 结构性**（详见报告 §3）。

### ✅ `IT-P1-DELIVERY-LEDGER-002` 已修复 —— 交付覆盖率 9.1% → **100%**

采纳云端 16:07 轮的 v3 设计（evaluability 与 delivery **分账**），由我实现并端到端验证：

* 新增独立 sidecar `SignalDeliveryStats`（`rule_selected / global_ignored / bus_accepted / committed`），与 `SignalEvalStats` **彻底分家**。
* `rule_selected` 改由 **Engine 统一记**（规则返回的 Alert 即 rule-selected output），不再要求规则先维护 eval 行。
* 新增 `committed_alerts_total`（Engine 独立数的真实 committed 总数）作门禁分母。**分母不得取"账本 committed 之和"** —— 否则分子分母同源、覆盖率恒 1.0，那正是本缺陷能长期隐藏的原因。
* 5 条规则补稳定 `signal_id`：`limit_board.*`（4 个 pattern）、`tick_surge.surge/plunge`、`unusual.<pattern>`、`spirit_index.<pattern>`、`spirit_price.<pattern>`。
* 全局门禁 `first_party_committed_without_signal_id` 要求 **0**。

实测（真实 Engine + Replay + Store，361 轮，`seed=42`）：

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 交付账本覆盖 signal 数 | 1 | **8** |
| 真实告警带 `signal_id` | 6 / 66 | **66 / 66** |
| 无 `signal_id` | 60 | **0** |
| `signed_ratio` | 0.0909 | **1.0** |
| 逐轮交付不变量违规 | — | **0** |
| 可评估性账本 signal 数 | 1 | 1（**未被污染**）|

**意义**：这是解锁 T+5/T+30 打标（= 回答"报得准不准"）的**最后一道结构障碍**。
但必须说清：这是"**能开始测量**"，不是"**已经测出结果**"。

### 本轮两项更正（都对自己）

1. **我更正了云端对我的一处归属错误**：云端称我 13:00 轮主张"只补 `signal_id` 即可"，**该主张不存在** —— 出自 **13:37 轮**（另一 agent）`:183-187`。我原话只说"**必须由产生它的规则填写**"、"无 signal_id = 如实为空"。云端的**技术结论正确**，归属有误。
2. **我更正了自己 13:00 轮的一处因果错误**：我写"被 AlertBus 去重/冷却丢掉 60 条"，与同段 `alerts_total=66` **自相矛盾** —— 它们都在 Store 里。正确语义是"**已 committed 但规则不维护账本、从未被计数**"。数字没错，**归因错了**。已在原报告加更正块（保留原文）。

### 上轮被固化成契约的缺陷（已反转）

13:37 轮当时断言 `assert untagged > 0`（"不维护账本的规则应为空 signal_id"）—— 把 60/66 不可对账当**预期行为**。本轮已反转为 `assert untagged == 0`，并在测试中注明原因。

### 仍未闭合 / 最高优先未修

* `R-12` **股票池覆盖率**（13:00 轮两次实测仅 **73.0% / 83.1%**）—— 仍是**最严重的未修风险**：覆盖率不足时"没报警"可能是"没扫到"。**本轮未推进**。
* `R-13` `spirit_*` 默认关闭 → 交付账本里**未出现** spirit 类 signal，**不能**据此说它们已对账通过。
* `R-14` `rule_version` 已写进载荷，但**看板/soak 都未读取** —— "制度版本可追溯"仍无可观测性。
* `R-17`（**本轮新增**）8 处 `signal_id` 是我**手工**按 `pattern` 语义命名的，与 `metrics["pattern"]` 是同一事实的两份拷贝 → 将来 pattern 改名而 signal_id 未同步会**静默漂移**。建议加一致性门禁（本轮未实施）。
* `IT-P1-NOTIFY-RESULT-001/002`（交付链第五、六级）、`event_id` 仍未做。

### 2026-09-21 16:07:37 JST 云端审计接续（保留）

- 发布前 HEAD：`948e0d2aaa29f8b7246225f6e45fdba6496f8551`。
- 被审最新产品：`eba8c4af5ed943a023ab09ab9c30000c1ac58f65`；其后到发布前 HEAD 只有审计文档，无产品源码变化。
- 开放 PR：0。
- 最新本地 Agent 对 `eba8c4a` 的归档机器证据：`1642 passed in 108.85s`，另一次独立复跑 `1642 passed in 118.34s`；该云端轮未重复执行完整 pytest。

### 云端本轮最大更正：`signal_id-only` 不能修 9.1% delivery coverage

云端正确指出：`_mark_stage()` 还要求 `signal_id 已经在 observation.signal_evals`；
五条未覆盖规则没有 `SignalEvalStats` 行，因此只补 `signal_id` 仍会被直接 return。

**⚠ 归属更正（本轮）**：提出"给另 5 条规则各补一行 `signal_id`"的是
**本地 13:37 轮**报告（`:183-187`），**不是** 13:00 轮。云端的**技术结论成立**，
本轮已按正确方案（独立 delivery registry）修复。

最新本地轮正确量化：replay 66 条 committed Alert，只有 6 条 tagged，60 条无 `signal_id`；只有 `volume_burst/spirit_order` 两条规则进入账本。

但它提出“给另 5 条规则各补一行 signal_id 就能让 committed 对账生效”。当前 Engine `_mark_stage()` 还要求：

```text
signal_id 已存在
AND
signal_id 已经在 observation.signal_evals
```

五条未覆盖规则没有 SignalEvalStats 行，因此只补 signal_id 仍会被 Engine 直接 return。

本轮机制反事实：

```text
当前：2/7 可记 delivery
只补 signal_id：仍 2/7
Engine 自动造 SignalEvalStats：5/5 新行破坏 bus<=rule_selected 不变量
独立 SignalDeliveryStats：7/7 机制闭合
120,000 软件事件 property stress：0 delivery invariant violation
```

因此新增稳定问题：`IT-P1-DELIVERY-LEDGER-002`（P1）。
**→ 已在 17:00 本地轮修复**（独立 `SignalDeliveryStats`，7/7 机制闭合，实测 66/66 = 100%）。

### 云端当时的下一步顺序（对照本轮完成情况）

1. —— ✅ **已完成**：Delivery Ledger 与 evaluability registry 已分离；Engine 统一记所有第一方 Alert 的 `rule_selected/bus_accepted/committed`。
2. —— ✅ **已完成**：5 条未覆盖规则已补稳定 `signal_id`；100% first-party committed coverage 门禁已建立（`first_party_committed_without_signal_id == 0`，实测 0）。
3. ⬜ **仍未做**：Universe Coverage Gate（历史实测仅 73.0%/83.1%，本轮未重测）—— **仍是最严重未修项**。
4. ⬜ **仍未做**：stable `event_id`、typed NotificationResult、committed-event T+5/T+30。
5. ⬜ 未做：per-code provenance → targeted fallback → ObservationInterval → SSE durable cursor/gap。
6. ⬜ 未做：真实多日语料与分母闭合前，不恢复模型大搜索。

### 当前主要开放项

`IT-P1-NOTIFY-RESULT-001/002` / `IT-P1-ALERT-IDENTITY-001`（`signal_id` 已落地、`event_id` 未做）/ universe coverage gate / `IT-P1-CAPABILITY-002` / `IT-P1-SOURCE-EMPTY-001` / `IT-P1-WINDOW-001` / SSE `IT-P1-008/009/003`。

`IT-P1-DELIVERY-LEDGER-002` 与 `IT-P1-DELIVERY-COVERAGE-001` **已从开放项移出**（17:00 轮修复，实测 100%）。

### 研究边界

本轮模型训练次数 0；真实 Precision / Recall / 漏事件率 / 交易收益仍 `unavailable`。本轮实验只证明 delivery/evaluability 账本的机械合同，不代表市场预测收益。
