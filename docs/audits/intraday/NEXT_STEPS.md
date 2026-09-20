# NEXT_STEPS — 2026-09-21 00:52 JST

> 排序原则：**先解锁一批缺陷的公共堵点**，再做单点修复。
> 每条都标了"为什么现在做这个"和"什么算做完"。
>
> **本轮（00:52）进展**：`IT-P1-CAPABILITY-003` 与 `IT-P2-OBS-STATUS-001` 已修复；
> `IT-P1-CAPABILITY-004` 进行中（另一工作包）。下方 P1/P2 条目按新状态重排。

## P0 — 需要**产品确认**才能继续（阻塞中，**未变**）

这两条不是技术问题，是**语义决策**。不定下来，改任何一边都可能是错的。

### 1. `max_per_round` 是"硬限流"还是"不许丢"？
- **现状**：截断 + 下一轮补报（不丢，但跨轮积压逐轮释放）。
  本轮 `limit_board` 与 `volume_burst` 都做了"**被截断的命中不算 `evaluated_no_hit`**"
  的账本区分，但**产品语义本身仍未定**。
- **如果产品意图是硬限流 20 条/轮**：超出的应**不写状态**、下一轮重新竞争（可能永远报不出）。
- **如果产品意图是不许丢告警**：当前实现正确。
- **为什么重要**：涨停封板是最高优先级信号，且"多股同时涨停"正是最容易触发截断、
  也最该看到的行情。
- **验收**：定下语义后，`test_truncated_alerts_are_reported_next_round_not_lost`
  要么保留、要么改成断言"超额被丢弃"。

### 2. `limit_board` 封单缩水（300万 → 100万 → 300万）算不算独立事件？
- **现状**：会**重新告警**（`sealed` → `at_limit_unqualified` → 再达标即跃迁）。
- **替代定义**：只在"炸板"（价格离开限价）时重新报，封单缩水不算。
- **为什么重要**：影响用户对"这只票是不是刚又封了"的判断。
- **验收**：定下语义后补一条跨轮用例钉住。

## P1 — 解锁一批缺陷的公共前置

### 3. per-code provenance（code → source / source_epoch / capabilities）· **最高优先**
- **解锁**：`IT-P1-CAPABILITY-002`（规则侧只有轮级能力声明）、
  soft-partial targeted fallback。
- **做法**：`Quote` 或 `EngineState` 上记录每个码**本轮由哪个源、哪个 epoch 提供**；
  规则据此判断"这个码的数据源不支持换手率"，而不是整轮降级。
- **为什么现在做**：本轮把 route 级 epoch、账本桶、**逐 signal 可评估性**都做完了，
  per-code 是自然下一步；不做它，`IT-P1-CAPABILITY-002` 与 targeted fallback 一直无法启动。
- **验收**：一条真验牙用例 —— 同一轮里两个码来自不同源，规则能**分别**判定能力。
- **注意**：`000001` 同时是平安银行（个股）与上证指数，provenance 的键必须**带 route**，
  否则撞码。已在 epoch 侧踩过这个坑。

### 4. `IT-P1-SOURCE-EMPTY-001` 空结果 vs 失败
- **位置**：`SourceManager.call()` + `eastmoney.snapshots`
- **问题**：provider 返回**空列表**（明确说"没有"）与**抛异常**（不知道）当前不可区分，
  故障转移会误判。
- **做法**：source 协议层区分 `[]`（成功但空）与异常（失败），failover 只在**失败**时切源。
- **验收**：空结果不得触发切源；异常必须触发。

### 5. `IT-P1-WINDOW-001` 窗口跨 epoch 语义
- **位置**：`engine.py:234-241`
- **问题**：窗口（如 300 秒）跨越 source epoch 边界时两侧数据不可比，当前没有处理。
- **做法**：定下"窗口遇到 epoch 边界就截断"还是"标脏"，然后实现 + 用例。
- **验收**：构造跨 epoch 窗口，断言不会把两段不可比数据拼成一个信号。

### 6. `IT-P1-008/009/003` SSE 持久化投递
- SSE durable replay、慢客户端静默 drop-oldest、series 容量。本轮未触碰 SSE 层。
  云端 r2 将其列为**仍未复核**的两组之一。

## P2 — 堵住"无法量化"的根

### 7. 挂载真实多日连续 A 股语料 · **决定"能不能给数字"的唯一路径**
- **解锁**：一批"影响面无法计数"的条目；真实模型认证；`provider_coverage.csv`。
- **现状**：`blocked_no_mounted_multiday_continuous_corpus`。
- **为什么最重要**：不挂载，Precision / Recall / 漏事件率 / 收益**永远 `unavailable`**。
  本轮同样**没有**编造这些数字。
- **验收**：能跑出真实逐轮 coverage 序列并落成 `provider_coverage.csv`。

### 8. 三源 `spirit_order` pattern 矩阵的真实触发率
- 云端 r2 §3.2 给出**离线**矩阵：Tencent 8/8、**Sina 0/8**、Eastmoney 0/8。
  本轮 `IT-P1-CAPABILITY-004` 正是按这个矩阵修的。
- **仍缺**：真实盘中各 pattern 的触发次数。修好 `depth_l1` 兜底后，
  Sina 下 `institution_buy/sell` **能否真的触发**只有真实语料能回答。

### 9. 东财 clist 路径上"停牌行"比例的直接观测
- **现状**：仓内唯一 clist 原始抓包 `fixtures/raw/eastmoney_clist_p1.txt`（100 行）
  实测 `f2="-"` 为 **0 行**。
- **为什么**：机制**不依赖**比例（1 行即触发），但真实比例决定影响面大小。
  `fixtures/raw/universe_sample.json` 5913 条样本里 `price=="-"` 恰 361 条（6.11%），
  但那是**另一条路径**的样本，不能替代 clist 路径的直接观测。
- **验收**：多个交易日的 clist 抓包，统计 `f2="-"` 行数分布。

## P3 — 未启动但已明确

### 10. ObservationInterval
给 history 加 `first_observed_at` / `last_observed_at` / `source_epoch` 区间结构。
独立数据模型改动。（与 #5 相关，可合并评估。）

## 明确**不做**的（他方结论，不重新讨论）

- **不实现** disagreement-abstention —— 高分歧行的 specialist PR **更高**
  （0.70668 vs 0.64747），弃权会损失精度。
- **不实现** blend（`0.75*specialist + 0.25*capmask`）—— 只增益 `+0.0000335` PR。
- **不伪造** synthetic 模型去追指标。
- **不**把 `unavailable_capability` 重新当作 whole-class health 真值 ——
  它已降级为诊断量（本轮 `IT-P1-CAPABILITY-003`）。
