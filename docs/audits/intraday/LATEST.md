# 最新审计

**最新本地 Agent 审计**：[`2026-09-20_21-36-00_JST.md`](./2026-09-20_21-36-00_JST.md)  
**最新云端独立审计**：[`2026-09-20_20-09-14_JST.md`](./2026-09-20_20-09-14_JST.md)  
**最新本地 Agent 任务书**：[`2026-09-20_20-09-14_JST_AGENT_TASK.md`](./2026-09-20_20-09-14_JST_AGENT_TASK.md)  
最新产品实现提交：[`100e06a`](https://github.com/fy-god/intraday-tape-reader/commit/100e06aeda7dc5605ac20b3f8511ec34ea40e9b6)  
上一份本地独立审计：[`2026-09-20_18-22-15_JST.md`](./2026-09-20_18-22-15_JST.md)  
上一份产品实现轮：[`2026-09-20_17-22-44_JST.md`](./2026-09-20_17-22-44_JST.md)  
时间合同：[`source_time_contract.json`](./source_time_contract.json)  
上一轮云端报告：[`2026-09-20_16-17-25_JST.md`](./2026-09-20_16-17-25_JST.md)

---

## 2026-09-20 21:36 JST 本地 Agent 审计

- `reviewed_source_sha`：`c38e79ba299d0f965ab1435afe92f6f8314f4474`（= 审计开始时的 `main` HEAD；全部读/跑在**只读冻结 worktree** `D:\ccc\_sched\work\asr-h20` 上进行）。
- **真实测试**：全量 `pytest -o addopts="" -p no:cacheprovider -q` → **`1308 passed in 52.69s`，退出码 0**（本线独立复跑，另有 3 次子 agent 复跑 47.46s / 48.43s / 96.74s 均为 `1308 passed` exit 0）。8 个 `tools/check_*.py` **全部 exit 0**。
- **`1308` 等级上调**：20:09 报告谨慎地把该数字降级为"历史归档、未复跑"，本轮**独立复现得到同一数字**，归档出处可定位到 `2026-09-20_17-22-44_JST.md:227`（由 `100e06a` 引入）。**可直接采信。**
- 未修项回归计数：**13 条编号 → 仍 OPEN 12 / 已经修复 1 / 未复现 0 / 与描述不符 0**。
- 真实多日连续 A 股语料仍未挂载；真实 Precision / Recall / 漏事件率 / 交易收益继续 `unavailable`。

### 本轮最高优先结论

1. **`IT-P1-COMPLETE-001-R1` 复核为已确认 P1，并量化了触发门槛**：`eastmoney.py:344/346` 的 `expected_total` 取自 transport `data.total`，`:374` 的 `len(out)` 是 parser 去重**之后**，`:383-396` 相减得 `shortfall`。**我实测：无重叠缓冲时停牌 1 行即翻 `complete=False`；有 87 行上界重叠缓冲时第 88 行触发。** 引擎 `:818` 在 partial 且更小时 `return 0` 拒绝覆盖。**两处必须同时说明的克制**：(a) 这是**棘轮**不是"永久"——实测 +50 仍拒、**+361 起自愈**；(b) `config/settings.yaml:50` 是 `["eastmoney","sina"]`，新浪列表接口**保留**停牌行且用独立判据（`sina.py:252-253`、`:443`、**`:451 expected_total:0`**），**只有东财单源或两源同时失效时才出现拒绝刷新**。准确叙事是"契约强制的可用性过滤被当成传输完整性信号"。
2. **新 `IT-P1-TIME-ROLE-003-R1`（本提交新引入 P1）**：`begin_source_epoch`（`engine.py:291`）只清 `accepted_watermark`、**不清 `first_seen`** → 切源后 `:268` 陈旧闸（因 `first_seen=False`）与 `:224` 乱序闸（因水位线刚清空）**同时让开**。**我实测：同 epoch 内 3 天前 ts 被拒（ooo=1，缓存 10.0）；切源后同一包被接受，缓存 10.0→12.0。** 真正首见码的陈旧闸**工作正常**（`stale=1`）——即闸本身对的，边界处失效。
3. **`IT-P1-TIME-ROLE-004` 双向确认**：`:88 source_epoch: str` 是**单个全局字符串**（`dataclasses.fields` 实测），`:291 clear()` 清所有码，`:959-965` 只用 `ROUTE_STOCKS` 触发。**我实测：仅股票源切换 → 指数水位线被清空 → 指数时间倒退 600s 的点被放行（ooo=0，缓存 2900.0）**；反向：指数自己切源时 epoch 标签仍由股票路由决定，新源首包被误杀。
4. **`IT-P1-TIME-POLICY-001`**：合同三源 `role=unknown`、`freshness_allowed=false`、明文"禁止基于 provider ts 判定新鲜度"，而 `engine.py:56/268` 仍做 4 小时 hard stale reject。守卫测试只查合同**内部一致性**、不查 Engine 是否遵守，**分叉不会被 CI 拦下**。
5. **`IT-P1-OBS-010` / `IT-P1-OBS-011`**：`:269` 已产生 `"stale"` 拒绝原因，但 `RoundObservationSet` **无任何 stale 字段**、`:220-226 stale_rejected` 仍 `return self.future_rejected`（自称"已弃用别名"），stale 从不进 Ledger；`live_session.py:158-182` 抓的 **11 个** observation 字段在 `:212-233 summarize_rounds()` 里**全部被丢弃**。
6. **`IT-P1-LIMIT-001` 三路独立验证 = 已经修复**：`limit_board.py:178` 门槛前置于 `:187` 写 `sealed`。**我行为验证**：第 1 轮封单 110 万（门槛 200 万）→ 0 告警且状态 `at_limit_unqualified`，第 2 轮 330 万 → **1 告警**；幂等保持；回落再达标可重报。子 agent 把新增 9 用例跑在**修复前**源码上 → `7 failed, 2 passed`（核心 `assert 0 == 1`），旧代码仍绿的 2 个恰是自标【回归保护】的——**没有冒功**。
7. **新 `IT-P2-CONTRACT-SHA-TYPO`**：`source_time_contract.json:4` 的 `_reviewed_source_sha` 末位 `f` 误写成 `c`，**该对象不存在**（`git cat-file -t` → `fatal: could not get object info`，**exit 128**；`--is-shallow-repository=false`、71 commits，已排除浅克隆）；改成 `…d47f` → `commit`（exit 0），是 `100e06a` 的父提交。用修正 SHA 核对，合同引用的 9 处行号在 `100e06a` 上**逐条命中**，故仅一字符错。但 `test_source_time_contract.py:42-44` **只校验 `文件:行号` 正则、不校验该 SHA**，故永不报警。
8. **其余 OPEN**：`IT-P1-INDEX-CURRENT-001`（`spirit_index.py:325` 从累积 `state.quotes` 取 current，某轮指数返回空仍用上轮值）、`IT-P1-WINDOW-001`（`engine.py:240` history 为 change-only，10 个平价点仅存 1，60s 窗口反例 `price_change=None` 而真实约 +3.0%）、`IT-P1-SOURCE-EMPTY-001`（真实 parser 返回 1/2 码**不抛异常** → 备用源调用 **0 次**）、`IT-P1-CAPABILITY-002`、`IT-P2-OBS-008`（`wants_indices=False` 时指数请求**从未发出**，但 `requested/index_requested/coverage` 与 `True` **取值相同**）、`IT-P2-OBS-009`（指数码在账本中出现 **0 次**）。
9. **新 `IT-P2-LIMIT-FIRST-BOARD-MULTI`**（既有遗留，**非**本提交引入）：门槛 200 万 + `max_per_round=2` 且 3 只票同轮首达时，第 3 只被 `prev=="sealed" and qualified` 永久吞掉（post 2/3 vs pre 0/3，修复后是改善），建议单开缺陷。
10. **并发事故如实记录**：本工作树有**并发写者全程实时编辑**（`12 modified + 2 untracked` → `14 modified + 9 untracked`）。我的报告刚写完时，写者在 **20:39:24 执行 `reset: moving to HEAD`**，报告文件**一度消失**；20:40:32 写者的 stash/pop 类操作把它**完整恢复**（38,449 B / 365 行）。**净损失为零，但建议后续审计先在冻结树写报告再落盘。**

### 本轮研究段：无新增实股结果

`research_verdict = NO_NEW_REAL_MARKET_RESULT`。本轮**没有**新增任何实股收益/Precision/Recall 证据；12 条 OPEN 与 1 条 FIXED 全部是**离线可复现反例（`SOFTWARE_SAMPLE`）**，**一律未计数**真实标的或频次。`REAL_MARKET` 计数 = **0**。三个状态轴（`execution_status` / `research_verdict` / `evidence_status`）与证据分池在报告 §7 中严格分列，**不合并计数**。

上一份云端报告的研究段 `EXP-IT-CAPABILITY-005`（`77,271` 行 / 4 组 PR AUC 增量）经子 agent 全盘检索判定为 **`UNVERIFIABLE`（证据链断裂，非造假）**：`predictions_v6.csv` 在整个 D 盘 0 命中、扫全部 71 个提交历史**从未被提交**，`77,271` 与 sha256 `a1838e97…` 的唯一出处是审计文档自身。**影响面超出本轮：从 `12-07-11` 报告起整条研究线的数值都不可回溯。**

### 下一轮必须拿到的实物

- **Eastmoney 三层账**：`transport_complete` / `raw_unique_codes` / `usable_coverage`，引擎门控绑定 transport；补"**解析丢弃**"模式的测试（现有测试只覆盖"服务端每页只回 10 行"，该模式**零覆盖**）。
- **route-specific epoch**：`source_epoch`/`watermark`/`first_seen` 改为按 `stocks`/`index` 分表；补"股票切源不影响指数"与"指数切源首包不误杀"两个集成测试。
- **修跨 epoch 陈旧旁路**：补"**同一用例内** `begin_source_epoch` + 3 天前 ts → 必须拒绝"的测试（现两个 API 从未出现在同一用例）。
- **修合同 SHA** `…d47c` → `…d47f`，并加"合同引用的 SHA 必须存在"的断言。
- 同步 `capabilities.py:194-197` 的过期注释（仍称"当前不存在陈旧拒绝路径"），给 `RoundObservationSet` 加独立 stale 字段。
- `summarize_rounds` 消费 observation 字段，让 health 能识别 soft-partial / capability degradation。
- 指数 current 只取本轮；`window()` 平价采样语义；source-empty 触发 fallback。
- 研究段产物补交（`predictions_v6.csv` 等）或重跑。

完整证据、机制复现与逐条 file:line 见本轮报告。

---

## 上一研究轮审（保留）

以下为上一份审计（`2026-09-20 20:09 JST 云端审计`）原文，保留以便对照。

- `reviewed_source_sha`：`100e06aeda7dc5605ac20b3f8511ec34ea40e9b6`。
- 审计开始 `main` HEAD：`aae2ac9ad7ef41a4bd50ed859801c30aa17fe0b5`；其相对 `100e06a` 仅增加 18:22 审计文档，无产品源码变化。（**本轮已用 blob 逐一核对确认**：`100e06a`、`aae2ac9`、`c38e79b` 下 `engine.py`/`eastmoney.py`/`capabilities.py` 的 blob 哈希完全相同。）
- 开放 PR：0。
- 本轮沙箱无法 clone GitHub（DNS），因此**没有**重新跑完整仓库 pytest；`100e06a` 的 `1308 passed / 0 failed` 仅作为本地 Agent 已归档历史结果，不冒充本轮执行。（**本轮已独立复跑并复现该数字，见上。**）
- 真实多日连续 A 股语料仍未挂载；真实 Precision / Recall / 漏事件率 / 交易收益继续 `unavailable`。

### 该轮最高优先结论

1. **`IT-P1-COMPLETE-001-R1` 仍为已确认 P1**：Eastmoney `complete` 仍用 post-parse `len(out)` 对比 transport `total`。18:22 冻结树已经真实复现：正常不可用/停牌行即可让 `complete=False`，已有股票池拒绝刷新，新上市代码实测可出现 `0/10` 进入。下一轮优先改为 `transport_complete` 与 `usable_coverage` 双账。
2. **新 `IT-P1-TIME-ROLE-004`**：SourceManager 已按 stocks/index 路由分 serving source，但 EngineState 只有一个全局 `source_epoch`/watermark；`poll_once()` 只按 `ROUTE_STOCKS` 开 epoch。结果：index 自己切源时新源首包可能被旧 watermark 拒绝；stocks 切源时又会清掉 index watermark，使 index 同源旧包可能被误接纳。
3. **新 `IT-P1-TIME-ROLE-003-R1` / `IT-P1-TIME-POLICY-001`**：切 source epoch 只清 watermark、不清全局 `first_seen`，同码新源 3 天前首包可绕过 stale gate；同时合同三源均 `role=unknown`、`freshness_allowed=false`，Engine 却仍基于 provider ts 做 4 小时 hard stale reject，执行策略与自身合同冲突。
4. **新 `IT-P1-OBS-010`**：产品已经存在 `t_reject:stale`，但 RoundObservationSet 没有真实 stale bucket；旧 `stale_rejected` 仍错误 alias 到 `future_rejected`，陈旧拒绝无法在观测账本中对账。
5. **新 `IT-P1-OBS-011`**：`live_session.make_round_sample()` 已抓 requested/returned/admitted/coverage/capability，但 `summarize_rounds()` 又把这些字段全部丢弃；最终 health 仍无法用真实 observation coverage / rule evaluability 识别 soft-partial 或 capability degradation。
6. `IT-P1-LIMIT-001` 本轮回读产品代码后维持**已修**：先 `_seal_qualified()`，不足记 `at_limit_unqualified`，达标才 `sealed`。
7. `IT-P1-INDEX-CURRENT-001`、`IT-P1-SOURCE-EMPTY-001`、`IT-P1-CAPABILITY-002`、`IT-P1-WINDOW-001`、`IT-P2-OBS-008/009` 与 SSE/Store 可靠性项继续开放。

### 该轮研究 `EXP-IT-CAP-005-block-correlation-robustness`

没有重新训练。复用冻结的 4 个 synthetic lockbox / `77,271` 行逐样本预测，按 symbol-day 固定 `30/60/120/300s` 时间块聚合，并以 `lockbox|symbol|day` 为单位做 500 次 group bootstrap。

Provider-specialist 相对 global capmask 的 PR AUC 增量：

| block | Spec-Cap PR | group bootstrap 95% |
|---|---:|---:|
| 30s | +0.00604 | [+0.00319, +0.00887] |
| 60s | +0.00497 | [+0.00187, +0.00774] |
| 120s | +0.00268 | [+0.00025, +0.00517] |
| 300s | +0.00289 | [+0.00087, +0.00478] |

结论：synthetic specialist 增量不是完全由逐 tick 重复行制造，但事件块越粗收益越小。继续保留为**真实数据候选**，不升级 shadow 默认；真实认证主指标改用事件簇/时间块，增量 `<0.005` 或主要 folds/provider 方向不稳定即淘汰。TCN/Transformer 继续暂停。

**⚠ 本轮附加裁定（证据等级下调）**：上述 4 组 PR AUC 增量**无从核对** —— `predictions_v6.csv` 在整个 D 盘 0 命中，且扫全部 71 个提交历史确认**从未被提交**；`77,271` 与 sha256 `a1838e97…` 的唯一出处是审计文档自身；`tools/` 里无对应脚本。判 **`UNVERIFIABLE`（证据链断裂）而非造假**。

### 该轮"下一轮必须拿到的实物"及本轮完成情况

- Eastmoney transport/usable 双账：`universe_transport_reconcile.json` **已由并发写者产出（4,020 B，工作树未提交）**，三层账代码**在途**；红测/绿测/回退验牙**未见**。
- stocks/index route-specific epoch：`source_epoch_route_cases.json` **已产出（1,744 B）**，route 分表代码**在途**。
- time policy：`time_policy_matrix.json`（2,966 B）+ `time_policy_matrix_check.json`（953 B）**已产出**。
- Observation Ledger：`observation_ledger_cases.json` **已产出（1,944 B）**，真实 stale bucket **仍缺**（`IT-P1-OBS-010` 仍 OPEN）。
- live-session：coverage 分位 / 拒绝原因 / source-capability mix 进入 summarize/health **仍缺**（`IT-P1-OBS-011` 仍 OPEN）。
- index current-only：`index_current_reconcile.json` **已产出（1,388 B）**，规则侧仍读累积缓存（`IT-P1-INDEX-CURRENT-001` 仍 OPEN）。
- `RUN_MANIFEST.json` **已产出（8,851 B）**；`NEXT_STEPS.md` **仍不存在**。

> 上述 6 个 JSON 与 `RUN_MANIFEST.json` 均为**并发写者当前未提交的工作树文件**，**不在 `c38e79b` 中**，故本轮审计**不以其为已交付证据**；其内容需在写者提交后另立一轮核验。

完整历史见 `docs/audits/intraday/` 下各轮报告。
