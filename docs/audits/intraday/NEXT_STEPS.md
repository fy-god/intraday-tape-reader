# NEXT_STEPS — 2026-09-21 17:00 JST

> 排序原则：**先解锁一批缺陷的公共堵点**，再做单点修复。
> 每条都标了"为什么现在做这个"和"什么算做完"。
>
> **⚠ 17:40 独立审计轮的更正（不影响 17:00 修复本身，但推翻其两条"已解除"结论）：**
> * `IT-P1-DELIVERY-LEDGER-002` 修复**复核通过**（真实 361 轮回放：账本 committed 与 Store 实数**逐条相等**，修复前 `6/66` 也独立复现）。**但**上面写的 `signed_ratio = 1.0` 是**会话累计**；运营面 `status()["observation"]` 拿到的是**末轮**（`store.py:156` 覆盖写），实测 361 轮里末轮为 0 条 → 读 `None`。见 `IT-P1-DELIVERY-GATE-PERROUND-001`。
> * 因此下面第 30-32 行"两道阻塞均已解除 / 标签分母已有 100% 覆盖"**过于乐观**：分母的**语义**对了，但它的**可观测性**只存在于末轮。
> * 第 33 行"剩下的唯一结构卡点是 P0-B"**方向正确**，但需补一句：`R-12` 目前连**可观测性**都没有 —— `_universe_meta` 引擎已算出却**零出口**（5 个模块 0 命中）。补出口是**零新增采集**的纯管道工作。
> * 详见 `2026-09-21_17-40-00_JST.md`。
>
> **✅ 17:00 轮：`IT-P1-DELIVERY-LEDGER-002` 已修复 —— 交付覆盖率 9.1% → 100%。**
> 采纳云端 16:07 轮 v3 设计（evaluability 与 delivery **分账**）并端到端验证：
> 真实 Engine + Replay + Store，361 轮 → 真实告警 **66/66 全部带 `signal_id`**
> （此前 6/66），交付账本覆盖 **8 个 signal**（可评估性账本仍只有 1 个，
> **未被污染**），门禁 `first_party_committed_without_signal_id` = **0**，
> 逐轮交付不变量违规 **0**，`signed_ratio` = **1.0**。
> 全量 **1660 passed**（+18）/ 8 gate 全绿 / selftest 68-6-8-8。
> 回退验牙 **12 条行为级 RED / 0 结构性**。
>
> **⚠ 这只是"能开始测量"，不是"已经测出结果"** ——
> 真实 Precision / Recall / 漏报率 / 收益仍为 `unavailable`。
>
> **✅ 13:00 轮：P0-A 与 A2 均已完成。**
> * **P0-A**：`published` 已拆成 `rule_selected / bus_accepted / committed`。
>   机制反例端到端复现 **24× overcount**（真实 Engine + 真实 AlertBus）。
> * **A2**：ST 新规已推到回放链路（`IT-P1-ST-REPLAY-BYPASS-001` 修复），
>   回放 2025-03-10 主板 ST 的 `limit_up_price` 由 **11.00 → 10.50**。
> * 连带修掉三处由**真实看板对账**暴露的既有缺陷
>   （`-R1` blocked 未跳过、`-R2` 换手率门槛漏记、`-R3` replay 能力缺失
>   导致演练模式放量功能整类静默）。全量 **1642 passed** / 8 gate 全绿。
>
> **用户真正要的东西（P0-0）**：用**事后收益**给告警打标签，
> 量化"报得准不准"。在此之前，所有修复都只回答"会不会报错"。
>
> ⚠ **两道阻塞均已解除**：`IT-P1-EVAL-PUBLISH-001`（13:00 修）与
> `IT-P1-DELIVERY-LEDGER-002`（17:00 修）都已关闭。
> P0-0 的标签分母现在既有**正确语义**（`committed`）又有 **100% 覆盖**。
> **剩下的唯一结构卡点是 P0-B（股票池覆盖率），然后才是 P0-C（`event_id`）。**

## P0-A — 告警交付阶段账本 ✅ **已完成（13:00 + 17:00 两轮）**

### A. 把 `published` 拆成 `rule_selected / bus_accepted / committed` ✅
- **完成情况**：
  * `capabilities.py`：`SignalEvalStats` 新增 `rule_selected` / `bus_accepted` /
    `committed` 三字段，`dropped_by_bus` / `committed_ratio` 两属性；
    `published` 降级为 `rule_selected` 的**兼容别名**；
    `check_invariants()` 增加两条链式断言。
  * `engine.py`：新增 `_mark_stage(observation, alert, stage)`，
    在 `bus.accept()` **之后**记 `bus_accepted`、`store.add_alert()` **之后**记
    `committed`。
  * `models.py`：`Alert.signal_id`（**由规则填**，不靠 title 猜）。
  * `tools/live_session.py`：逐 signal slim + 全场三级总量；
    旧轮样本缺新键时**退回 `published`**，不退回 0。
- **验收（已达成）**：`tests/test_alert_delivery_stages.py` 20 条。
- **回退验牙**：29 条行为级 RED / 0 结构性（见轮报 §4）。

### B. 把交付账本从可评估性账本**解耦** ✅（17:00，`IT-P1-DELIVERY-LEDGER-002`）
- **完成情况**：
  * `capabilities.py`：新增独立 sidecar `SignalDeliveryStats`
    （`rule_selected / global_ignored / bus_accepted / committed`）+
    `RoundObservationSet.delivery_stats` registry +
    `mark_delivery_selected/ignored/bus_accepted/committed` +
    `check_delivery_invariants()` + `delivery_accounting_coverage()`；
    新增 `committed_alerts_total`（Engine 独立数的**真实**分母）。
  * `engine.py`：`rule_selected` 由 **Engine 统一记**（规则返回的 Alert
    即 rule-selected output，避免"某条规则忘了维护账本"）；
    `_mark_stage` 拆成"交付账本（只要有 signal_id 就记）"与
    "可评估性账本（只有已 instrument 的 signal 才记）"两条路径。
  * 5 条规则补稳定 `signal_id`：`limit_board.*`（4 pattern）、
    `tick_surge.surge/plunge`、`unusual.<pattern>`、
    `spirit_index.<pattern>`、`spirit_price.<pattern>`。
  * 全局门禁 `first_party_committed_without_signal_id == 0`。
- **验收（已达成）**：`tests/test_delivery_ledger_decoupled.py` 18 条；
  真实回放 66/66 带 signal_id、`signed_ratio` 1.0、违规 0。
- **回退验牙**：12 条行为级 RED / 0 结构性。

### C. 堵住两个**已被真实代码证伪**的修法（防止下一轮走回头路）✅
- **只补 `signal_id` 不够**：`sig not in signal_evals` 门禁照样 return。
  测试 `test_signal_id_alone_is_not_enough` 钉住。
- **Engine 自动建 eval 行不行**：会造幽灵行、破坏可评估性不变量、
  把"没做逐 code 统计"伪装成"0% 可评估"。
  测试 `test_auto_creating_eval_rows_would_break_invariants` 钉住。

### D. （新增 `R-17`）`signal_id` × `metrics["pattern"]` 一致性门禁
- **现状**：8 处 `signal_id` 是我**手工**按 `pattern` 语义命名的，
  而 `metrics["pattern"]` 在同处另写一次 —— **同一事实的两份拷贝**。
- **为什么现在做**：将来某规则 `pattern` 集合变化而 `signal_id` 忘记同步，
  两者会**静默漂移**：交付账本仍工作，但名字与新 pattern 不符，
  而"名字说谎"比"账本为空"更难发现。
- **做法**：加门禁"每条 Alert 的 `signal_id` 必须等于
  `<模块>.<metrics['pattern']>`（或显式豁免）"。
- **可证伪**：若真实运行中两者恒相等，门禁永不触发，说明过虑了。

## P0-B — 股票池覆盖率门禁（**仍是最严重的未修风险**）

### C. 覆盖率过低时必须拒绝出结论
- **现状/证据**：13:00 轮两次真实全市场扫描，覆盖率分别只有
  **73.0%**（`4090/5917`，东财"第 2 页起失败"）与
  **83.1%**（`4576/5917`，"第 24 页起失败"）；`sina` 一次
  `HTTP Error 456`、一次 `2800 只（第 29 页起失败）`。
  `once` 输出里 `universe` 甚至是 `0`。**17:00 轮未重测**。
- **危害**：覆盖率 73% 时，**任何"没报警"都可能是"没扫到"**。
  用户会把"扫描器瞎了一半"读成"市场很平静"。这是**唯一一个会让用户
  系统性误读结果**的结构性因素，比统计噪声严重。
- **为什么优先级最高**：交付分母已在 17:00 轮补齐，**卡点就是它**。
- **做法**：`once` / `soak` / 看板在覆盖率低于阈值（建议 90%）时，
  把本轮标记为 `degraded` 并在结果顶部显著提示实际覆盖率。
- **为什么现在做**：它挡在所有"命中质量"结论前面 —— 分母都不全，
  Precision/Recall 无从谈起。
- **可证伪**：若真实运行覆盖率恒 ≥ 95%，本门禁永不触发，
  说明源端其实稳定、这个担心不成立。
- **证据文件**：`universe_coverage_reconcile.json`。

### A2. 把 ST 新规从"单元"推到"回放链路" ✅ **主体已完成**
- **完成情况**：`IT-P1-ST-REPLAY-BYPASS-001` 已修 ——
  `ScriptedStock.limit_rate(when=None)` 接受交易日，
  `build_script()` 传 `timeline[0].date()`。
  实测回放 2025-03-10 主板 ST：`limit_up_price` **11.00 → 10.50**、
  `limit_down_price` **9.00 → 9.50**；同一日期非 ST 仍 11.00、
  创业板/科创板仍 20%（未误伤）。
- **同时修正根因**：`IT-P1-UNKNOWN-DATE-FAILOPEN-001` ——
  `_as_date` 现在认得 ISO/紧凑/斜杠串与等价位整数
  （修复前 `"2025-03-10"` 给 **0.10**，应 0.05），
  并用正则**钉住位数**拒绝宽松形态（`"2025031"` 曾被
  `strptime` 解析成 2025-03-01）。
- **A2 剩余**：`default_universe` 里**仍没有 ST 股票**（云端
  `IT-P2-REPLAY-DOC-COVERAGE-001` 指出的问题）。所以上面的验证
  是用自定义 `ScriptedStock` 完成的，**默认参数下 CI 仍不会覆盖**。
  建议加一只主板 ST + 一只 `*ST` 并加自检断言 ——
  不改业务语义，却能把一个 P1 从"无人知晓"变成"CI 红灯"。
- **证据文件**：`market_rule_replay_0705_vs_0706.json`（**仍未产出**，
  现有证据是 `tests/test_st_date_awareness_end_to_end.py` 33 条 +
  `st_date_red.log`）。

## P0-0 — 从"会不会报错"转向"报得准不准"（**阻塞已解除，可开始**）

### 0. 用事后收益自动标注告警，产出 Precision 代理指标
- **现状**：本项目所有验证都只证明"链路通、不报错"。
  **真实 Precision / Recall / 漏报率 / 交易收益全部 `unavailable`** ——
  因为没有任何标注数据（"这只票当时到底该不该报"）。
- **做法**：记录每条**真正 committed** 告警的 `(code, ts, price)`
  （依赖 P0-A 的阶段账本），T+5/T+30 分钟后取真实价格，
  统计"告警后是否继续同向移动"。**无需人工标注**，可自动产出代理指标。
- **为什么现在做**：`spirit_*` 三条规则默认关闭，配置注释要求
  「先在真实行情里观察命中质量再打开」。**没有这个指标就永远无法决定**，
  只能一直抱着"默认关闭"或凭感觉打开。
- **造假条件（falsify）**：若告警后 T+5 收益分布与**随机抽样时点**的收益分布
  **无显著差异**，则该信号无预测价值 → 应保持关闭或重调阈值。
  更强条件：若真实事件在 ≥3 个独立交易日/时间块上，T+5/T+30 signed-return
  相对 matched controls 无稳定正增量，或只来自单日/单票，
  或控制市场方向后消失 —— 该 signal **不应**因"告警很多"而放开。
- **验收**：对每条已启用规则给出"告警后 T+5 同向比例 vs 随机基线"，
  并明确标注这是**代理指标**，不是真实交易收益。
- **注意**：这是**任务定义调整**，不是模型增量提升。别把它包装成"模型变强了"。

## P0 — 需要**产品确认**才能继续（阻塞中，**未变**）

这两条不是技术问题，是**语义决策**。不定下来，改任何一边都可能是错的。

### 1. `max_per_round` 是"硬限流"还是"不许丢"？
- **现状**：截断 + 下一轮补报（不丢，但跨轮积压逐轮释放）。
  本轮 `limit_board` 与 `volume_burst` 都做了"**被截断的命中不算 `evaluated_no_hit`**"
  的账本区分，但**产品语义本身仍未定**。
- **02:24 新增实测证据**：真实全市场扫描中，`limit_board` **每轮恰好卡在 20 条**
  （其 `max_per_round=20`），而**同一轮** `unusual` 报了 35 条 —— 说明确有人在被截断。
  更值得警惕的是截断键是 `(-severity, -|pct|)`：
  「封单 1.00 亿」与「封单 1734 万」在排序上**被同等对待**。
- **如果产品意图是硬限流 20 条/轮**：超出的应**不写状态**、下一轮重新竞争（可能永远报不出）。
- **如果产品意图是不许丢告警**：当前实现正确，但排序键应当把**封单额**纳入。
- **为什么重要**：涨停封板是最高优先级信号，且"多股同时涨停"正是最容易触发截断、
  也最该看到的行情。
- **验收**：定下语义后，`test_truncated_alerts_are_reported_next_round_not_lost`
  要么保留、要么改成断言"超额被丢弃"；并把排序键的选择写成断言。

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
- **02:24 补充**：本轮已证明**单次**实时全市场抓取可行（5564 只、腾讯链路、
  4 轮 115 条告警），但**单点快照 ≠ 多日连续语料**。
  要做到第 0 条（事后收益标注），需要**持续采集**告警 + 价格序列。
- **验收**：能跑出真实逐轮 coverage 序列并落成 `provider_coverage.csv`。

### 8. 三源 `spirit_order` pattern 矩阵的真实触发率
- 云端 r2 §3.2 给出**离线**矩阵：Tencent 8/8、**Sina 0/8**、Eastmoney 0/8。
  本轮 `IT-P1-CAPABILITY-004` 正是按这个矩阵修的，
  并已在**真实行情**上验证腾讯链路 `institution_buy/sell` 真的会触发（11 / 9 条）。
- **仍缺**：真实盘中各 pattern 的触发次数，尤其 4 个**成交类**
  （`big_buy`/`big_sell`/`institution_eat`/`institution_vomit`）。
  休市时它们因 `d_volume == 0` **正确**缺席（已查证不是缺陷）；
  目前只有"人为推进内外盘"的离线证据，**真实增量下的行为未验证**。
- **解锁条件**：09:30 后连续竞价时段实测（已挂 `schedule-5` 于 09:32 CST）。

### 8b. 新股/次新股过滤的残留缺口（**02:24 轮新增**）
- **已修**：`N`/`C` 前缀识别（上市首日 + 第 2~5 日）。真实全市场实测
  5564 只中符合者恰好 2 只、零假阳性。
- **仍缺**：上市第 **6 日**起名称前缀脱落，只能靠 `min_list_days` 第一层，
  而该层依赖**东财** `list_date`，东财在本机被 IP 封禁、实际走腾讯
  → `list_dates` 为空 → **第 6 日起的次新股在腾讯链路上无过滤**。
- **注意**：N/C 判据只在**一次**快照上统计过（2 只）。
  **不能**声称长期假阳性率为 0。北交所是否也用 N/C 前缀**未核实**。
- **做法**：需要不依赖东财的上市日来源（或本地缓存自建）。
- **验收**：给出"第 6~20 日次新股被正确过滤"的真实用例。

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
