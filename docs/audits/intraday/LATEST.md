# 最新审计

**最新本地 Agent 轮**：[`2026-09-20_21-49-29_JST.md`](./2026-09-20_21-49-29_JST.md)  
**上一份他方审计（已推送）**：[`2026-09-20_21-36-00_JST.md`](./2026-09-20_21-36-00_JST.md)  
**最新云端独立审计**：[`2026-09-20_20-09-14_JST.md`](./2026-09-20_20-09-14_JST.md)  
**最新本地 Agent 任务书**：[`2026-09-20_20-09-14_JST_AGENT_TASK.md`](./2026-09-20_20-09-14_JST_AGENT_TASK.md)  
**下一步计划**：[`NEXT_STEPS.md`](./NEXT_STEPS.md)  
**执行清单**：[`RUN_MANIFEST.json`](./RUN_MANIFEST.json)  
上一份产品实现提交：[`100e06a`](https://github.com/fy-god/intraday-tape-reader/commit/100e06aeda7dc5605ac20b3f8511ec34ea40e9b6)  
时间合同：[`source_time_contract.json`](./source_time_contract.json)  

---

## 2026-09-20 21:49 JST 本地 Agent 轮（代码 + 上传）

- `reviewed_source_sha`：`32acc32462b94411bdaa83b1c13a02290486f4a0`（本轮起点 `main` HEAD）。
- **真实测试**：全量 `python -m pytest -o addopts="" -q` → **`1430 passed in 72.51s`，退出码 0**。
  同命令在起点树（改动全部 stash）为 **`1308 passed`** → 本轮 **+122 用例，0 失败**。
- **门禁 9/9 全绿**：8 个 `tools/check_*.py` + `node tools/dash_render_check.js`，全部 exit 0。
- **审计输入**：他方 `2026-09-20_21-36-00_JST.md`（reviewed `c38e79b`，已推送 `7a21339`+`32acc32`）。
- 真实多日连续 A 股语料仍未挂载；真实 Precision / Recall / 漏事件率 / 交易收益继续 `unavailable`。

### 本轮最高优先结论

1. **`IT-P2-LIMIT-FIRST-BOARD-MULTI` 已确认并修复（P1，涨停告警永久丢失）**。
   `limit_board.evaluate()` 的 `_check_side` **顺带写状态**，`max_per_round` 截断却在其**之后**：
   被截掉的告警状态已记 `sealed`，下一轮 `prev == "sealed"` 直接 `return None`。
   **实测（3 只同轮首达 + `max_per_round=2`）**：第 2 轮报 `600001,600002`，
   第 3 轮 `[]` —— **`600003` 永久丢失**。修复：记每条告警写入前的状态，
   `out` 与 `meta` 一起重排，**只回滚被截掉的那些**。修复后第 3 轮补报 `600003`、第 4 轮静默（幂等保留）。
   **验牙**：`2 failed / 81 passed`，两条均为 `AssertionError`（语义级真验牙）。
   *这是既有遗留形态（`max_per_round=0` 时单只路径本来就对），非 `IT-P1-LIMIT-001` 引入。*
2. **`IT-P2-OBS-008` 已确认并修复**：`_fetch_indices()` 在 `wants_indices=False` 时
   **故意不发**请求，但 `requested`/`index_requested` **无条件**加 `len(index_codes)` ——
   使"根本没抓"与"抓了没回来"**取值完全相同**（`requested=5/index_requested=3/coverage=0.40`）。
   一个**完全正常的配置**（不用指数规则）看起来像持续丢数据，直接毒化 `OBS-011` 刚修好的覆盖率聚合。
3. **`IT-P2-OBS-009` 已确认并修复**：`unknown_missing` 只遍历**个股**，
   请求了却没返回的**指数码在整个账本里出现 0 次** —— 个股丢失有 missing 兜底，**指数丢失没有任何桶**。
   修复后每个 `requested` 码都必须能被某个桶解释。
4. **`IT-P1-TIME-POLICY-001` 收口**：合同三源 `freshness_allowed=false` 却做 4h 硬拒绝。
   他方判断准确 ——"不是闸坏了，是**执行策略与自身书面合同分叉**"。
   **行为对齐由 WP03 完成**（实测：3 天前首见包 `admitted=1 / future=0 / stale_diag=1`，**不**硬拒绝但**被诊断**）；
   本轮补上他方建议 (a)：合同里把 `STALE_TOLERANCE_SECONDS` 显式标为
   `freshness_judgement_semantics = "not_provider_semantics"`。
5. **他方 3 条文档/注释漂移已修**：`IT-P2-CONTRACT-SHA-TYPO`（合同 `_reviewed_source_sha` 末位
   `c` → `f`；`…d47c` 实测 `git cat-file` **exit 128**，`…d47f` 存在）、
   `IT-P2-STALE-COMMENT-STALE`（`capabilities.py` 注释自相矛盾）、
   `IT-P2-ENGINE-DOCSTRING-DRIFT`（`refresh_universe` docstring 漏了 WP01 的"行数缩水"轴）。
6. **他方 §4 的 13 条 → 本轮后计数：已修 10 / 仍 OPEN 3 / 未复现 0 / 与描述不符 0**。
   仍 OPEN：`IT-P1-WINDOW-001`、`IT-P1-SOURCE-EMPTY-001`、`IT-P1-CAPABILITY-002`。
7. **必须声明的"下调"（不是悄悄丢掉）**：上一轮我为 `IT-P1-TIME-ROLE-003` 声称的 3 条**行为级**验牙，
   本轮**主动降级** —— 它们断言"3 天前的包**必须被拒绝**"，而合同明文**禁止**基于 provider ts
   判新鲜度；那是**要求代码违反自己的合同**。已改为合同驱动（"被准入 + 被诊断为陈旧"）并在 docstring 写明。
   **代价必须说清**：`STALE_TOLERANCE_SECONDS` 现在是**纯诊断参考线**，
   **当前没有任何 provider-ts 驱动的陈旧拦截**，陈旧防护完全依赖 epoch 内乱序判定。
   这是 `role=unknown` 的**必然代价**；硬拒绝能力保留在 `TIME_POLICY` 里，拿到权威语义后置 `true` 即可启用。
8. **两条既有测试本身编码了旧缺陷的假设**（`test_stock_and_index_are_separately_accounted`、
   `test_far_future_not_counted_as_admitted` 无条件用 `len(index_codes)`）。
   我按修正后的语义改写，并在 docstring 写明"**分账的前提是指数真的被请求了**" —— 这是**语义修正**，
   不是为了让红变绿而放宽断言。
9. **并发事故继承记录**：他方报告（21:36 轮）如实记录本工作树有**并发写者实时编辑**，
   20:39:24 一次 `reset: moving to HEAD` 让报告文件一度消失、20:40:32 恢复，**净损失为零**。
   本轮我全程只用 `git stash push -- <单个 src 文件>` + `pop`（不留残留），并逐次确认 `git stash list` 为空。
   **本轮全部改动与全部验收数据均已在同一棵树上实测。**

### 本轮产物（`docs/audits/intraday/`）

```text
2026-09-20_21-49-29_JST.md          本轮报告
LATEST.md / NEXT_STEPS.md / RUN_MANIFEST.json
universe_transport_reconcile.json   WP01 双轴对账（3 案例 × 18 字段，双恒等式全对平）
source_epoch_route_cases.json       WP02 route 级 epoch 台账 + 验牙分类
time_policy_matrix.json             WP03 策略投影
time_policy_matrix_check.json       WP03 合同↔矩阵↔engine 三方绑定
observation_ledger_cases.json       WP04 桶互斥台账
index_current_reconcile.json        WP06 指数 current view 对账
```

**未产出（不填假 0）**：`provider_coverage.csv`（需真实多日 soak，
本轮只有单元/集成级）；`red.log`/`green.log`/`rollback-tooth.log` 未另存为独立文件 ——
分类结论（真验牙 vs 结构性）已逐条写进报告与 `RUN_MANIFEST.json`。

### 本轮研究段：无新增实股结果

`research_verdict = NO_NEW_REAL_MARKET_RESULT`。本轮全部条目是**离线可复现反例（`SOFTWARE_SAMPLE`）**，
**一律未计数**真实标的或频次；`REAL_MARKET` 计数 = **0**。

他方审计 §5 的研究段判定我**继承并同意**：`EXP-IT-CAPABILITY-005` 的 `predictions_v6.csv`
在整个 D 盘 0 命中、扫全部 71 个提交历史**从未被提交**，`77,271` 与 sha256 `a1838e97…`
的唯一出处是审计文档自身 → **`UNVERIFIABLE`（证据链断裂，非造假）**，
**影响面超出本轮**：从 `12-07-11` 报告起整条研究线的数值都不可回溯。

### 下一轮必须拿到的实物

见 [`NEXT_STEPS.md`](./NEXT_STEPS.md)。P0 是两条**产品语义决策**（`max_per_round` 是限流还是不许丢、
封单缩水是否算独立事件）；P1 是 per-code provenance（解锁 `CAPABILITY-002` 与 WP07）。

---

## 上一轮（他方审计，已推送，保留要点）

`2026-09-20_21-36-00_JST.md`，reviewed `c38e79b`，全量 `1308 passed in 52.69s` exit 0，
8 个门禁全 exit 0。该轮**独立复现**了后来由我修复的 `COMPLETE-001-R1`、`TIME-ROLE-003-R1`、
`TIME-ROLE-004`、`TIME-POLICY-001`、`OBS-010/011`、`INDEX-CURRENT-001`、`OBS-008/009`
与 `LIMIT-FIRST-BOARD-MULTI`；并**独立确认** `LIMIT-001` 已修（三路验证一致）。
其 §5 单列 7 条**无法核验项**（含"东财 clist 路径停牌行比例未直接观测"、"真实频次未计数"、
"GitHub API 403 故 OPEN PR 无法确认"），我全部继承，未用任何数字填补。

完整历史见 `docs/audits/intraday/` 下各轮报告。
