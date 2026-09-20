# 最新审计

**最新云端独立审计**：[`2026-09-21_00-14-53_JST.md`](./2026-09-21_00-14-53_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-21_00-14-53_JST_AGENT_TASK.md`](./2026-09-21_00-14-53_JST_AGENT_TASK.md)  
**最新本地 Agent 轮**：[`2026-09-20_21-49-29_JST.md`](./2026-09-20_21-49-29_JST.md)  
**下一步计划**：[`NEXT_STEPS.md`](./NEXT_STEPS.md)  
**执行清单**：[`RUN_MANIFEST.json`](./RUN_MANIFEST.json)  
当前被审产品提交：[`e729c1f`](https://github.com/fy-god/intraday-tape-reader/commit/e729c1f0bd3d6754e00dba0574a25b01a65b6c58)  

---

## 2026-09-21 00:14 JST 云端独立审计

- `reviewed_source_sha`：`e729c1f0bd3d6754e00dba0574a25b01a65b6c58`。
- 报告发布 commit：`69d8e170171798a7236441b8eea31bbf6855f3f9`。
- Agent 任务书发布 commit：`c4db85e1aa55b7343167770c466bf1f271fc09b5`。
- 开放 PR：GitHub API 实际读取 `[]`。
- 本轮没有完整 checkout；`1430 passed / 0 failed` 是当前产品提交的本地 Agent 归档证据，本轮未独立复跑。
- 真实多日连续 A 股语料仍未挂载；真实 Precision / Recall / 漏事件率 / 收益继续 `unavailable`。

### 本轮最高优先结论

1. **`IT-P1-CAPABILITY-003` 新确认**：`RoundObservationSet.unavailable_capability` 是跨规则 code 并集；`live_session` 却用“每轮有没有任意 unavailable code”的 round ratio 解释“整类规则是否被评估”。固定反例中每轮 5000 code 只坏 1 code，按 code 可评估比例仍 99.98%，当前 ratio 却为 1.0 并进入 FAIL。更关键的是 `volume_burst` 缺 `volume_ratio` 会记 unavailable，但当前语义允许跳过该门槛并继续命中，所以 `unavailable_capability` 本身也不等价于“不可评估”。下一步必须改为 per-signal `considered/evaluable/blocked/advisory/hit/published` 真值。
2. **`IT-P1-CAPABILITY-004` 新确认**：`spirit_order` 基本没有 capability ledger。Sina parser 提供 `bid1/ask1 + bid_vol/ask_vol`，capability table 声明 `depth_l1=True`，但 `_best_order()` 只遍历空的 `bid_vols/ask_vols`，所以 `institution_buy/sell` 无法利用 L1；其余 6 pattern 又分别依赖 `outer_inner` 或 L5。按当前实现 capability 维度：Tencent 8/8、Sina **0/8**、Eastmoney 0/8；Sina/Eastmoney 下这种退化目前可以静默表现为普通 0 alerts。
3. **`IT-P1-CAPABILITY-002` 与 `IT-P1-SOURCE-EMPTY-001` 仍 OPEN**：`RuleContext.capabilities` 仍是 round-global；`SourceManager.call()` 仍只在 exception 时 fallback。必须先有 `code -> source/source_epoch/capabilities`，再做 soft-partial targeted fallback，不能反过来。
4. **`IT-P1-WINDOW-001` 仍 OPEN**：accepted watermark 已修 ordering，但 history 仍只存价/量变化，不能表达“平价但新鲜”的 observation coverage；仍需 ObservationInterval。
5. **`IT-P2-OBS-STATUS-001` 新确认**：Store 内部已有 `observation_seq`，live-session 也正确防旧账冒充；但 `/api/status` 不暴露 observation 自己的 seq/observed_at/poll_count，外部 API 消费者仍可能把上一成功轮 ledger 当当前轮。
6. 当前 `e729c1f` 的 Eastmoney 双账、route-specific epoch/time policy、index current-only、closing auction、limit-board 等既有修复均回读存在，本轮不重复重报。

### 本轮研发段

实验 ID：`EXP-IT-CAP-006-evaluability-contract`。本轮**没有重新训练模型**。最新本地 Agent 已将此前云端 `predictions_v6.csv` / 77,271 行 synthetic 链标为 `UNVERIFIABLE`（仓库证据链断裂，非造假），真实多日语料又未挂载；因此本轮停止继续叠加不可复现的 synthetic AUC/PR 数字，改做固定 SHA 可复核的 capability/evaluability 机制实验。

本轮云端产物包括 capability health 分母反例、optional-vs-blocking missing 反例、三源 `spirit_order` pattern 矩阵、soft-partial 控制流探针和 status observation identity 审计。下一轮本地 Agent 的第一主线是 WP01 Signal Evaluability Contract + WP02 SpiritOrder capability closure，再做 per-code provenance 与 targeted fallback。

---

## 历史索引（上一版 LATEST 内容保留）

**最新本地 Agent 轮（上一版主指针）**：[`2026-09-20_21-49-29_JST.md`](./2026-09-20_21-49-29_JST.md)  
**上一份他方审计（已推送）**：[`2026-09-20_21-36-00_JST.md`](./2026-09-20_21-36-00_JST.md)  
**上一份云端独立审计**：[`2026-09-20_20-09-14_JST.md`](./2026-09-20_20-09-14_JST.md)  
**上一份云端任务书**：[`2026-09-20_20-09-14_JST_AGENT_TASK.md`](./2026-09-20_20-09-14_JST_AGENT_TASK.md)  
上一份产品实现提交：[`100e06a`](https://github.com/fy-god/intraday-tape-reader/commit/100e06aeda7dc5605ac20b3f8511ec34ea40e9b6)  
时间合同：[`source_time_contract.json`](./source_time_contract.json)  

### 2026-09-20 21:49 JST 本地 Agent 轮（保留要点）

- `reviewed_source_sha`：`32acc32462b94411bdaa83b1c13a02290486f4a0`（该轮起点 `main` HEAD）。
- **真实测试**：全量 `python -m pytest -o addopts="" -q` → **`1430 passed in 72.51s`，退出码 0**；同命令在该轮起点树为 **`1308 passed`** → +122 用例，0 失败。
- **门禁 9/9 全绿**：8 个 `tools/check_*.py` + `node tools/dash_render_check.js`。
- 该轮修复：`IT-P2-LIMIT-FIRST-BOARD-MULTI`、`IT-P2-OBS-008/009`、`IT-P1-TIME-POLICY-001` 收口及若干文档/合同漂移；并完成 WP01–WP06 的主要实现。
- 当时仍 OPEN 的三项：`IT-P1-WINDOW-001`、`IT-P1-SOURCE-EMPTY-001`、`IT-P1-CAPABILITY-002`。
- 当时明确下调：provider time 角色未知时不做 provider-ts hard stale reject，只做陈旧诊断；硬拒绝能力保留待权威时间语义后启用。
- 该轮研究结论：真实多日语料仍未挂载；此前云端 synthetic 研究文件在本地仓库/磁盘不可回溯，标为 `UNVERIFIABLE`，不作为新的真实模型证据。

完整细节继续以 [`2026-09-20_21-49-29_JST.md`](./2026-09-20_21-49-29_JST.md) 与历史 `docs/audits/intraday/` 报告为准。
