# 最新审计

**最新本地 Agent 轮**：[`2026-09-21_03-38-00_JST.md`](./2026-09-21_03-38-00_JST.md)  
**最新云端独立审计**：[`2026-09-21_04-10-59_JST.md`](./2026-09-21_04-10-59_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-21_04-10-59_JST_AGENT_TASK.md`](./2026-09-21_04-10-59_JST_AGENT_TASK.md)  
**上一份本地 Agent 轮**：[`2026-09-21_02-24-00_JST.md`](./2026-09-21_02-24-00_JST.md)  
**上一份云端独立审计**：[`2026-09-21_01-40-00_JST.md`](./2026-09-21_01-40-00_JST.md)  
**下一步计划**：[`NEXT_STEPS.md`](./NEXT_STEPS.md)  
**仓库执行清单**：[`RUN_MANIFEST.json`](./RUN_MANIFEST.json)

## 当前版本与发布

- 本轮本地轮时间：2026-09-21 03:38 JST。产出 HEAD：`22a51a2`。
- 当前产品提交归档：**1570 passed / 0 failed**、8/8 gate、selftest 稳定 68 条。
- 本轮产品提交：
  - `a415965` — `serve --replay`（休市也能看到短线精灵）+ `status` 时段自洽
  - `9c4e08d` — `run_daemon.py --detach` 真正脱离终端
  - `22a51a2` — 主板 ST 涨跌幅按交易日 5%→10% + 守护健康归属修复
- 本轮已消费云端 `2026-09-21_04-10-59_JST.md`（`reviewed_source_sha = 9c4e08d`）。

## 本轮已修复（均带证据）

1. **`IT-P1-MARKET-RULE-20260706-001` — 已修复。** 沪深主板 ST/*ST 涨跌幅
   自 **2026-07-06** 起由 ±5% 调为 ±10%（证监会上海监管局 / 上交所公告 /
   深交所指南多源核验）。原 `ST_LIMIT_RATE=0.05` 写死。
   **真实影响（腾讯 5564 只实测）**：主板 ST 144 只全部算错；
   12 只现价超出旧 ±5% 区间（新规下合法）；其中 **10 只被误判为「贴板」**。
   修复为**日期感知**（`st_limit_rate_on(when)`，日期取自 `Quote.ts`），
   因为历史回放 2026-07-05 前仍须 5%。修复后 144/144 正确、误判 **12→0**，
   且真正到 ±10% 的 2 只被正确识别。回退验证 **7/7 真 AssertionError**。
2. **`IT-P2-DAEMON-HEALTH-001` — 已修复。** 云端指出的是我上一提交
   `9c4e08d` 自身的缺陷：`--detach` 只轮询端口 `/api/status`，端口已有健康
   服务时会把**旧服务**当成新 PID 的启动成功并谎报"可以关闭窗口了"。
   修法：Popen 前 `_preflight()`；成功判据绑身份
   （状态文件 `pid == child pid` 且活着且端口健康）。
   端到端对照：旧实现谎报成功退出 0 → 新实现提示"已有服务在响应"退出 2。
   ⚠ **诚实标注**：新增 8 条测试回退后为 6 结构性 / 0 行为牙，
   决定性证据是端到端对照，不是单元测试。
3. **`IT-P1-SERVE-REPLAY-001` — 已修复。** 休市时用户看不到短线精灵，
   而原提示"去跑 `replay`"是纯命令行、**不经过看板**，等于没解决。
   新增 `serve --replay`（共用同一 store）；并修 `store.status()` 用墙钟
   重算 phase 导致"显示休市却滚动 63 条 09:40 告警"的自相矛盾。
   Chromium 实测：短线精灵 12 行可见、控制台零错误、180 条信号文本。
4. **`IT-P1-DAEMON-DETACH-001` — 已修复。** `GetConsoleWindow()` 实测
   `HAS_CONSOLE` 由 1 → 0。守护已 `--detach` 运行 62 分钟、PID 未变、
   **不属于任何 dsh 会话**。

## 仍未修（按优先级）

1. **`IT-P1-EVAL-PUBLISH-001`** — `published` 在 Rule 返回前写入，早于
   `AlertBus.accept`；机制反例 overcount **24×**。必须拆
   `rule_selected -> bus_accepted -> committed -> sent`。
   这是把真实告警接到 future-label 流水线的前置条件。
2. **`IT-P1-CAPABILITY-002-R1`** — capability 仍 round-global；
   source 声称 L5 可用但单条 Quote 缺深度时，该 code 从 considered 分母消失。
3. **`IT-P1-SOURCE-EMPTY-001`** — 仍是 exception-driven failover，
   soft-partial 不补洞。
4. **`IT-P1-NEWLIST-002-R1`** — N/C 只覆盖前 5 日，第 6~10 日无精确上市日。
5. **`IT-P1-WINDOW-001`**、**`IT-P1-008/009/003`**（SSE durable replay /
   drop-oldest / series 容量）、**`IT-P2-RELEASE-DOC-001`**（CHANGELOG 停 1.0.3）。

## 本轮新增待验证风险

- `R-07` ST 新规只到单元测试层，**未跑完整 replay** 对比 07-05/07-06
  （replay 剧本名不含 ST，回放端到端不触发该分支）。
- `R-08` Sina 主源切换后的 ST 实盘路径未验证。
- `R-09` `--detach` 的是结构性测试（6/0），行为证据仅来自端到端对照。
- `R-10` 回放模式**不重放历史告警到 SSE**（`/api/stream` 只推 `tick`）；
  刷新页面可从 `/api/spirit` 读到全部 63 条。

## 研究推进

优先实验仍为 **`EXP-IT-QUAL-001-post-alert-matched-control`**：把真实告警质量
代理的标签合同冻结为 T+5/T+30 signed return + matched controls +
event cluster + `known_at`/unknown。本轮**没有**推进模型，也没有新的
真实标签 —— 真实 Precision / Recall / 漏事件率 / 交易收益仍为 `unavailable`。

## 下一轮必须检查的实物

```text
market_rule_red.log / green.log / rollback.log
market_rule_matrix.json
market_rule_replay_0705_vs_0706.json      <- 新增（补 R-07）
st_limit_live_scan.json                    <- 新增（5564 只实测归档）
alert_stage_red.log / green.log / rollback.log
alert_delivery_reconcile.json
daemon_attribution_e2e.log                 <- 新增
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

