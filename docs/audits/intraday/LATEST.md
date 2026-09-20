# 最新审计

最新完整报告：[`2026-09-20_16-17-25_JST.md`](./2026-09-20_16-17-25_JST.md)  
本轮本地 Agent 任务书：[`2026-09-20_16-17-25_JST_AGENT_TASK.md`](./2026-09-20_16-17-25_JST_AGENT_TASK.md)

## 版本与证据边界（2026-09-20 16:17 JST）

- 仓库与分支：`fy-god/intraday-tape-reader` / `main`。
- 本轮开始 HEAD：`6ef855fa8227f0dc4bcb6863db846fee5f92363d`。
- `reviewed_source_sha`：`020f2bb5f45a50bb3367ea631654702b37b3aab9`。
- 从 `020f2bb` 到本轮开始 HEAD 只有上一轮报告和索引，无产品代码变化。
- 报告发布 commit：`01a294b670d14334c3a7d51f40078ba4522ec0f2`。
- Agent 任务书发布 commit：`06491bc7f53e68241276223cff7e2a8f830b8586`。
- PR：0。
- 本轮执行位置：隔离 Linux 沙箱；无完整 checkout，故本轮**没有重跑**全量 pytest。
- 上一 13:39 JST 独立本地 checkout 归档的 `1241 passed in 66.92s` 仅作为历史证据，本轮没有冒充重新执行。
- 真实多日连续 A 股语料仍未挂载；本轮研究为既有 77,271 行 synthetic row predictions 的二次诊断，不是实盘结果。

## 本轮新增/深化

### `IT-P1-TIME-ROLE-001` 继续最高优先

当前 `EngineState.accepted_watermark` 仍是 `code -> timestamp`，没有 `route/source_epoch/time_role`。本轮最小机制反例确认：A 源先接受 10:00:40 后，若实际供数切到 B 源且 B 的首包时间为 10:00:05，code-only watermark 会把 B 首包当乱序拒绝；按 source epoch 分段则可接受 B 首包，同时仍能拒绝 B epoch 内真正倒退的 10:00:01。

修复不能只把 source 名拼进 key；应在**实际 serving source** 每次变化时生成新的 route epoch，A→B→A 形成三个 epoch。

### `IT-P1-TIME-ROLE-002/003` 仍开放

- 轻微 future provider timestamp 当前被改写为 receive `now`，raw provider time 丢失；
- 首见 code 没有 stale 下限，`now-3天` 的 provider ts 仍能被接受；
- stale 阈值不能先拍脑袋设，必须先确认 provider 时间角色。

三家 parser 都填 `Quote.ts`，但 capability 表又把三家 `provider_time` 都声明为 false。当前只能从社区资料得到 Tencent 字段 30 常被称为“数据更新时间”、Sina 30/31 为日期/时间、Eastmoney f124 为时间戳；没有权威字段规范证明它们是 snapshot publish time 还是 last trade time，因此 `source_time_contract` 必须允许 `unknown`。

### `IT-P2-OBS-008`（本轮新确认）

默认配置有 5 个 `index_codes`，但 `spirit_index.enabled=false` 时 `_wants_indices=false`，引擎不会发 index 网络请求；Observation Ledger 却仍执行：

```python
requested = req_stocks + len(self.index_codes)
index_requested = len(self.index_codes)
```

因此“配置过”被记成“实际请求过”。机制例：实际 100/100 股票全部返回、0 次 index 请求，当前 ledger 会显示 100/105=95.24% coverage；候选正确值应为 100/100=100%。全市场 5563 只时偏差约 0.09 个百分点，但 watchlist-only 10 只时会被放大为 10/15=66.7%。

### `IT-P2-OBS-009`（本轮新确认）

index route 真正请求 5 个、只回 4 个时，当前 `unknown_missing` 只遍历股票 `_codes`，缺失指数没有 code-level 明细。下一版必须按 route 保存 `requested_codes/provider_object_codes/missing/rejected`。

### `IT-P1-INDEX-CURRENT-001` 本轮重新确认

`SpiritIndexRule.evaluate()` 仍从累计 `ctx.state.quotes` 构造指数 current candidates。若 R1 指数成功、R2 index route 失败，R1 的缓存指数仍可能被 R2 规则消费。默认 `spirit_index=false`，但启用后会重新出现“latest cache 冒充 current”的旧型风险。

### `IT-P1-COMPLETE-001`：缺陷成立，但修法需要更正

当前 Eastmoney `complete` 仍不检查真实覆盖；13:39 报告发现的缩水页缺陷成立。但不能直接用 `len(out) >= expected_total` 修：`out` 是 post-parse usable Quote，而 API `total` 是 transport/universe 口径。正确方案应分别记录：

```text
raw_rows
raw_unique_codes
usable_quotes
expected_total
required_pages
pages_requested
pages_failed
truncated
```

`transport_complete` 由 raw unique code / page contract 判断；`usable_coverage` 单独报告。否则“传输完整但有少数无效 Quote”会被错误判成 transport incomplete。

## 研究：`EXP-IT-CAP-004-disagreement-and-blend`

本轮不重复 fit 模型，而用已有 77,271 行 synthetic row predictions 检查两个新点子。

总体：

```text
base PR       0.68531
capmask PR    0.69451
specialist PR 0.70022
universal PR  0.67305
```

### 否定 disagreement-abstention

只保留 `|specialist-capmask| <= 0.03`：coverage 62.54%，specialist PR 0.70308；为了约 +0.00286 PR 丢掉 37.46% coverage，不划算。

更关键：高分歧 `>0.08` 的 4,178 行里 specialist PR=0.70668，而 capmask PR=0.64747；大分歧并不等于 specialist 不可信，反而是 specialist 相对 capmask 更有价值的区域之一。

结论：**不实现“分歧大就 abstain”。**

### 否定 blend 复杂度

`0.75*specialist + 0.25*capmask` 在四个既有 synthetic lockbox 的平均 PR=0.7002487，纯 specialist=0.7002152，增量只有约 `+0.0000335`；specialist 的平均 ROC/Brier/LogLoss 还更好。

结论：**不增加 blend；provider-specialist 继续只作为真实数据候选。**

## 继续开放

- `IT-P1-WINDOW-001`
- `IT-P1-LIMIT-001`
- `IT-P1-SOURCE-EMPTY-001`
- `IT-P1-CAPABILITY-002`
- `IT-P1-OBS-006/007`
- `IT-P1-008/009/003`
- `IT-P1-INFO-001`

已修继续回归：`IT-P0-003`、`IT-P0-001`、`IT-P1-006-R1`（max_pages 截断窄义）、`IT-P1-007`。

## 下一轮必须检查的产物

1. `source_time_contract.json`：三源 provider time field / role / evidence / strict-ordering policy；
2. source epoch 的真实 repo 红测、绿测与回退验牙；
3. `observation_ledger_cases.json`：index disabled / 5回5 / 5回4 / exception / future / zero-price；
4. `index_current_reconcile.json`：R1成功、R2 index缺失时 current candidates 必须为空且 history 保留；
5. `universe_transport_reconcile.json`：raw_rows/raw_unique_codes/usable_quotes 双账；
6. `provider_coverage.csv`；
7. `git diff` + SHA256、`red.log`、`green.log`、`rollback-tooth.log`、`RUN_MANIFEST.json`、`NEXT_STEPS.md`。

真实多日数据到位以后只先比较冻结的 HGB base / capmask / provider-specialist；不扩 TCN/Transformer，不实现 disagreement-abstention 或 blend。

## 历史线索

- [2026-09-20 16:17 JST](2026-09-20_16-17-25_JST.md) — 本轮；source-epoch/time-role、index ledger/current、transport completeness 修法更正、模型分歧负实验
- [2026-09-20 13:39 JST](2026-09-20_13-39-26_JST.md) — 独立复核 `020f2bb`，1241 passed，COMPLETE/TIME/OBS 新问题
- [2026-09-20 13:16 JST](2026-09-20_13-16-06_JST.md) — 关闭 close-auction 与 max_pages 截断窄义
- [2026-09-20 12:07 JST](2026-09-20_12-07-11_JST.md) — Observation Contract / provider-specialist 研究
- [2026-09-20 08:10 JST](2026-09-20_08-10-29_JST.md)
- [2026-09-20 04:05 JST](2026-09-20_04-05-31_JST.md)

后续仍只在本目录新增审计 Markdown / Agent 任务书并更新本索引；文档提交不计作产品源码升级。
