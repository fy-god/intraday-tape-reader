# 最新审计

**最新本地 Agent 轮（本轮）**：[`2026-09-21_01-40-00_JST.md`](./2026-09-21_01-40-00_JST.md)  
**最新云端独立审计**：[`2026-09-21_00-14-53_JST.md`](./2026-09-21_00-14-53_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-21_00-14-53_JST_AGENT_TASK.md`](./2026-09-21_00-14-53_JST_AGENT_TASK.md)  
**上一份本地 Agent 轮**：[`2026-09-20_21-49-29_JST.md`](./2026-09-20_21-49-29_JST.md)  
**下一步计划**：[`NEXT_STEPS.md`](./NEXT_STEPS.md)  
**执行清单**：[`RUN_MANIFEST.json`](./RUN_MANIFEST.json)  
当前被审产品提交：[`e729c1f`](https://github.com/fy-god/intraday-tape-reader/commit/e729c1f0bd3d6754e00dba0574a25b01a65b6c58)  

---

## 2026-09-21 01:40 JST 本地 Agent 轮（本轮）

- 被审源码：`e729c1f0bd3d6754e00dba0574a25b01a65b6c58`；tree `0032d5d13505d148ebdab3bd4d52efd5924b7c66`。
  **这是上轮审计点 `32acc32` 之后唯一的源码提交**（19 个源码/测试文件，源码 +2975/−188）。
- **我方独立复跑的真数（只读冻结树 `asr-h00` @`e729c1f`）**：
  `python -m pytest -o addopts="" -p no:cacheprovider --ignore=reference -q`
  → **`1430 passed in 73.34s`，exit 0**；**9/9 门禁全绿**
  （`check_audit_shots/bom/colors/config_consumed/config_wiring/orphan_config/readme_tools/spirit_mapping`
  8 个 `.py` + `dash_render_check.js`，全部 exit 0）。
  → 上一份云端报告自陈「1430 passed / 9/9 门禁……**未独立复跑**」，我方复跑**确认这两个数字为真**。
- **本轮主结论：`00:14` 云端报告的 5 项新登记，我逐条独立复现，全部成立且行号精确。**
  - `IT-P1-CAPABILITY-003`（**仍 OPEN**）：`capabilities.py:227-229`
    `unavailable_capability = len(unavailable_codes)`；`live_session.py:419-423` 只要
    `n > 0` 就把整轮计入 `unavailable_rounds`；`:502-503` 用
    `unavailable_rounds / obs_rounds` 当比例；`:104` 阈值 `unavailable_fail_ratio = 1.0`；
    `:817/830/844-845` 判红并输出「整类规则在这整场 soak 里一次都没被评估过」。
    **我用真实 `summarize_rounds`→`finalize_metrics`→`evaluate_health` 复现**：
    20 轮 × 5000 code × 每轮 1 个 code 缺失 → `capability_unavailable_ratio = 1.0`
    → capability 项 **fail**，文案含「整类规则…一次都没被评估过」，
    而真实按 code 可评估比例是 **4999/5000 = 99.98%**。**分母错误已确认。**
  - `IT-P1-CAPABILITY-004`（**仍 OPEN**）：`spirit_order.py` 全文件
    **`ctx.provides(...)` / `ObservationDecision` 0 命中**；`:547/564` 只传
    `q.bid_vols/q.ask_vols`，`:643` 只遍历传入的 vols；`sina.py:224-225` 只填单档
    `bid_vol/ask_vol` 而**不填** `bid_vols/ask_vols`（`models.py:171-177`）。
    **我实测真实 `_best_order`**：同样 1000 万股挂单，五档形状命中、Sina 形状恒 `None`
    → Sina 下 `institution_buy/sell` 实际不可评估，且**不留 ledger 记录**。
  - `IT-P1-CAPABILITY-002`（**仍 OPEN**）：`provides(key)` 仍单参，capability 仍 round-global。
  - `IT-P1-SOURCE-EMPTY-001`（**仍 OPEN**）：`engine.py:563-571` `SourceManager.call()`
    仍只按「有没有抛异常」判成功，无 requested/returned 差集。
  - `IT-P1-WINDOW-001`（**仍 OPEN**）：`engine.py:313` history 仍只在
    「价变或量变」时 append（水位线已在 `:316` 解耦，那部分确实修好了）。
  - `IT-P2-OBS-STATUS-001`（**本轮新登记 · OPEN**）：`store.py:221-245` 的 `status()`
    返回 `observation` 但**不含** `observation_seq`（该属性在 `store.py:155` 存在却未暴露），
    且顶层 `ts` 是当前墙钟 → 外部消费者可能拿旧账当本轮。
- **已修项我也抽样独立复核，全部为真**：`IT-P1-TIME-ROLE-004`（`engine.py:118`
  已改为 route 键控 dict）、`IT-P1-TIME-ROLE-003-R1`（`:392-395` 按 route 清 first_seen 分账）、
  `IT-P1-COMPLETE-001-R1`（`eastmoney.py:375-391/459-472` 传输轴 `raw_unique_codes` vs
  可用轴 `usable_coverage` 诊断，双账确实分离）、`IT-P1-LIMIT-001`（`limit_board.py:210-219`）、
  `IT-P2-LIMIT-FIRST-BOARD-MULTI`（`:109/137-150` 平行 `meta` 回滚）。
  **我对 LIMIT-FIRST-BOARD-MULTI 做了端到端实测**：3 票同轮达标 + `max_per_round=2`
  → 第 1 轮报 2 条且第 3 只**状态被回滚**，第 2 轮**补报成功**
  （对照 `21:49` 报告记录的修复前「第 2 轮 0 条 = 永久丢失」）→ **修复确实有效**。
- **r2 追加（未经完成的子 agent D 的对抗性证伪工作，由主 agent 本人补做）**：
  - **4 个攻击点全部失败，原声称全部存活**：
    ① `IT-P1-CAPABILITY-003` 的「硬阻断 vs 可跳过」——我用**真实 `VolumeBurstRule`** 构造
    三组对照，实测 **缺 `turnover` → 0 条 Alert（硬阻断）**、
    **缺 `volume_ratio` → 1 条 Alert（仍命中）**，而两者记录的 status **同为
    `unavailable_capability`** → 同名承载相反语义**已确证**。
    ② 攻击「回滚是否让已发出的告警重复报」：真实 `LimitBoardRule(max_per_round=2)` +
    3 票连跑 5 轮 → **每只票恰好报 1 次，无重复、无丢失** → 修复**双向正确**。
    ③ 攻击「`qualified` 先判后写是否活锁」：封单 1000→1000→1500→3000 手
    → 不足期间停在 `at_limit_unqualified`，**首次达标恰好报 1 次**，之后幂等 → **无活锁**。
    ④ 攻击「up/down 同轮截断还原是否互相覆盖」：确认跌停封单看 `ask_vol`
    （`limit_board.py:271`），我的观察是**自己的构造错误**，非产品缺陷。
  - **补验证正文 §9 标为「未复核」的两项，均成立**：
    `IT-P0-001` 用真实 `TradingCalendar().phase()` 实跑 → `14:58` = `close_auction`、
    **不在** `CONTINUOUS`、**在** `CALL_AUCTIONS`（`session.py:67-72` 自陈故意单列）→ **已修**；
    `IT-P1-TIME-POLICY-001` 用真实 `time_policy_for()` 实跑 → 三家源**全部**
    `freshness_allowed=False`、**未注册源也取 False** → **不 hard reject，成立**。
  - **新登记 `IT-P1-CAPABILITY-004-PRECEDENT-AVAILABLE`（P1 · 加强项）**：
    对**同一个** Sina 形状 quote（单档 `bid_vol=30000` 有值、五档为空），
    `limit_board._seal_amount_wan` = **3,000 万元**且 `qualified=True`，
    而 `spirit_order._best_order` = **`None`** —— **`limit_board` 早就在消费单档量**
    （`limit_board.py:266-272`、`models.py:171`）。故 `spirit_order` 的修复
    **不需要等 `WP03`（per-code provenance）**，**修复代价比原报告估计更低，建议上调优先级**。
  - **我自己的 3 处探针 bug 已全部在正文披露**（`volume_delta` 过小致 (B) 误得 0 条；
    封单 5,000 手×10 元实已超门槛；跌停侧误用 `bid_vol`）——
    三处**都不改变结论**，保留在案以免误读为"一次成功"。
- **`00:14` 报告的诚实性**：`:10/:11/:12/:586-592` 明确标注了
  `blocked_no_checkout`、`not_run`、`unavailable`，并声明 1430/9 门禁**未独立复跑**；
  `:38` 主动停止叠加不可复现的 synthetic 数字。**未发现把要求冒充成绩。**
  其 `predictions_v6.csv`「不可回溯」的判定我也证实：
  `git log --all -S 'predictions_v6'` 的 7 处命中**全部只改 `docs/audits/` Markdown**，
  `--diff-filter=AMD -- '*predictions_v6.csv'` **0 命中** → 该文件从未被提交。
- **当前 OPEN 合计 7 条**：`CAPABILITY-002/003/004`、`SOURCE-EMPTY-001`、`WINDOW-001`、
  `008/009/003`（SSE durable replay 等）、`IT-P2-OBS-STATUS-001`。
- **三个只读子 agent 本轮全部未能完成**（A 误判「冻结树被并发写者改动」，
  实为**主工作树**被改动；B、D 未产出报告），故**全部不采信**；
  本报告每条结论均由主 agent 亲自用真实命令在冻结树内复现。
- **并发写者（只读观察，未触碰）**：主工作树 `D:\ccc\ashare-radar` 出现
  ` M src/arad/capabilities.py`、` M src/arad/rules/volume_burst.py`、` M src/arad/store.py`、
  ` M tests/test_capabilities.py`、`?? tests/test_signal_evaluability.py`、
  `?? tests/test_signal_evaluability_teeth.py`（约 +322/−5）。
  其新增用例名（如 `test_one_blocked_in_five_thousand_is_not_whole_class_zero`）
  显示 `00:14` 报告的 **WP01 正在实现中** —— **我方未评审、未改动、未提交**。
- 本轮**未**改任何产品源码/配置/权重/Actions/PR；**未**动任何定时任务。
  （以上计数与 SHA 属本次复核，**不**认证历史报告中未经我复跑的结论。）

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
