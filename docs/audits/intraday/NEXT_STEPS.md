# NEXT_STEPS — 2026-09-22 21:00 JST

> 排序原则：**先解锁一批缺陷的公共堵点**，再做单点修复。
> 每条都标了"为什么现在做这个"和"什么算做完"。
>
> 🔴 **21:00 轮结论（最重要的一句）**：
> **我 17:00 轮的 tri-state 修复解决了"假红"，但在"证据合并"处造出了两个新假绿。**
> 我在 17:00 报告里写下的规矩是「**session measured facts > t0 snapshot**」。
> **方向对，但不够** —— 它让**后来的好消息开始抹掉先前的坏消息**：
> ```text
> t0 transport_complete = False   <- provider 明确声明：本次股票池被截断
> session 30 轮全测、0 轮不完整    <- 会话里确实没再看到截断
> => universe_transport = ok       <- 硬事实被盖掉
> ```
> 第二个：**零证据被当成肯定证据** ——
> `_universe_session([]) -> measured=False, ratio=None, ever=False`
> 却输出「会话期间未降级为仅自选股（**全程全市场扫描**）」；
> `finalize_metrics([])`（**一轮都没跑**）也输出同一句。
> 第三个：**会话内活跃覆盖下降无消费者**（t0 98.33% 掩盖会话 75%）。
>
> ⚠ **我自己的负控制"以错误的理由通过"**：我在 17:00 轮把它当负控制写进报告，
> **但它的输入把被测分支绕过去了** ——
> `t0=False + measured=0 -> fail`（我走的路径），**`measured=1 -> ok`**。
> **有负控制 ≠ 有有效的负控制。**
>
> **✅ 21:00 轮修复 5 条（全部针对我 17:00 轮的代码）**
> | ID | 级别 | 修复 |
> |---|---|---|
> | `...-EMPTY-SCAN-ASSERTED-AS-FULL-MARKET-001` | P1 | scope 由 `bool` 升为**四态** + 肯定句需 `measured` 门控 |
> | `...-TRANSPORT-T0-SUPPRESSED-001` | P1 | t0 硬负面**吸收**会话正面 |
> | **我自己的负控制失效** | — | 参数化 `measured ∈ {0,1,30}` |
> | `...-ACTIVE-COVERAGE-SESSION-BLIND-001` | P1 | 每轮采**分子+分母**；会话取最坏优先于 t0 |
> | `...-DROP-CAUSE-COLLAPSED-001` | **P2** | `denominator_kind` 进会话聚合（病因字段仍待搬） |
>
> **深层修复 = Session Universe Evidence Contract v1**（WP01）：
> **根因不是阈值，是"证据合并"没有合同。**
> scope 四态 + transport join 语义（hard negative 吸收）
> + active coverage 数值证据（t0 与 session 取最坏）+ 旧报告兼容。
>
> **1802 passed**（+11）/ 8 gate 全绿 / selftest 68-6-8-8 /
> 回退验牙 **10 条行为级 RED / 0 结构性**。
>
> **⚠ 修法纪律**：ADDENDUM2 §1 **撤回了它自己**"`:1576` 永不可达"的表述 ——
> 实测 `data/live_session_*.json` **7/7** 无 `universe_session` 键，**全走那一支**。
> 修法是**给肯定分支加门控**，**不动** `:1576`（并加了回归护栏）。
>
> 🔴 **仍未闭合（连续四轮最高优先）**：
> `IT-P1-UNIVERSE-MEMBERSHIP-QUALITY-001` —— 20:04 WP02 仍标"【独立 diff】"。
> **下一轮应当专门做它。**
>
> **⬇ 本轮下调（我 17:00 轮的声称）**：
> `...-TRANSPORT-TRISTATE-R1` → **PARTIAL**（优先级定错）；
> `...-TRANSPORT-SESSION-BLIND-001` → **PARTIAL**（接上了一个会覆盖硬事实的读者）；
> `...-WATCHLIST-FALLBACK-SESSION-BLIND-001` → **PARTIAL**（零证据仍落进肯定句）；
> **"我加了负控制"这个动作本身** → **失败**。
> `...-FRESHNESS-*` 三条与 `...-ATTEMPT-EXCEPTION-EXIT-001` **保持成立**（无新反证）。
>
> **✅ 17:00 轮**：撤回 13:00 轮「3 项全部成立」→ 全部 **PARTIAL**
> （并行只读审计线 + 13:55 更正附刊确认，我采纳）。
>
> **✅ 13:00 轮**：修掉 3 条（截断盲区 / 刷新台账 / 会话级新鲜度）——
> **17:00 轮已把这三条改判 PARTIAL**。
>
> **✅ 2026-09-21 21:00 轮：修掉 17:40 本地轮与 20:10 云端轮各自独立指认的 7 条缺陷。**
> **归属**：1.1–1.5 由 `17-40-00_JST.md` 轮先发现（§3.5 / §4 / 附录 A.1 / B.1），
> 20:10 云端轮独立同结论；1.6–1.7 由 `17-40-00_JST.md` §6 / §3.6 发现。
> **我实现并验证 —— 发现功劳不归我。**
> （注：这是 **09-21** 的 21:00 轮；与本文件顶部 **09-22** 的 21:00 轮是两次不同的轮次。）
>
> | ID | 复核 | 修复 |
> |---|---|---|
> | `IT-P1-DELIVERY-FALSE-GREEN-001` | **成立** | 显式 `accounting_status`，不再 clamp |
> | `IT-P1-ACCEPTANCE-GATE-RINGBUFFER-001` | **成立（我自己的测试）** | 分母改 `store.alerts_total()` |
> | `IT-P1-SOAK-LEDGER-BLIND-001` | **成立** | `live_session` 白名单 + 会话累计层 |
> | `IT-P1-DELIVERY-GATE-PEROUND-001` | **成立** | 逐轮自报 `_scope` |
> | `web.max_alerts` 接线残留 | **成立** | 传入 `AlertStore` |
> | `IT-P1-WEB-SIGNAL-ID-LOSS-001` | **成立** | `web.py` 重建 Alert 补 `signal_id` |
> | `IT-P1-DELIVERY-INVARIANTS-UNCALLED-001` | **成立** | `poll_once` 真的调用不变量检查 |
>
> **1676 passed**（+16）/ 8 gate 全绿 / selftest 68-6-8-8 /
> 回退验牙 **12 条行为级 RED / 0 结构性** / 端到端 481 轮 **6/6 自洽**。
>
> **⬇ 本轮三处自我更正（都必须记下）**
> 1. **我建的门禁是假绿**：`max(total-named, 0)` 把负差夹成 0，而 0 恰好是"通过"。
>    实测 `total=0/named=66` → 假绿；`total=1/named=3` → `ratio=3.0`（数学不可能）仍 PASS。
>    **我 17:00 轮刚批评过"恒等式让人看不见缺陷"，转头在门禁上重犯了同一个错。**
> 2. **我宣称的"逐轮交付不变量违规 0"是测试口径，不是生产保证** ——
>    `check_delivery_invariants()` 在生产里**零调用**（全仓只有它自己的 def）。
>    数是真的，**但当时没有任何东西在生产里检查它**。
> 3. **我引用的 `R-12` 数字本身算错了**：`4090/5917 = 69.12%`（不是 73.0%）、
>    `4576/5917 = 77.34%`（不是 83.1%）；且分母 `5917` 在代码里不存在（真实是 5913）。
>    **这两个数无法从任何机器产物复现，已作废**；被指名的证据文件
>    `universe_coverage_reconcile.json` **不存在**。`R-12` 的严重性不依赖这两个数。
>
> **⬇ 据实下调 17:00 轮结论**：17:00 说"移除了回答准确率问题的最后一道障碍"，
> 20:10 轮指出**下得太早** —— `R-12` 未闭合前不能声称全市场 Recall。
> **下调为"部分可解锁：只能做已扫描集合内的相对评价"。**
>
> **✅ 17:00 轮：交付账本与可评估性账本解耦**（覆盖率 9.1% → 100%）。
> **⚠ 但门禁本身有假绿（本轮已修）** —— 见上。
>
> **⬇ 17:00 轮"逐轮交付不变量违规 0"下调**：那是**测试口径**；
> 生产零调用（本轮已接线，见 §D）。
>
> **用户真正要的东西（P0-0）**：用**事后收益**给告警打标签，
> 量化"报得准不准"。在此之前，所有修复都只回答"会不会报错"。
>
> 🔴 **当前唯一挡在准确率结论前面的东西 = P0-B（股票池覆盖率）。**
> 交付账本已闭合；现在卡点是"市场应有的代码里有多少进了 active scan"，
> 且它**连可观测性都没有**（`_universe_meta` 已算出却零出口）。

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

### D. ✅（09-21 21:00）交付账本自洽性 + 不变量**接线到生产**
- **完成情况**（`IT-P1-DELIVERY-FALSE-GREEN-001` + `IT-P1-DELIVERY-INVARIANTS-UNCALLED-001`）
  * `capabilities.py`：新增 `accounting_errors` 计数、
    `delivery_accounting_status()`（`ok`/`not_measured`/`inconsistent`）、
    `check_delivery_accounting()`；`as_dict()` 导出三者。
  * 逐轮字典加 `_scope: "last_round"` + `_scope_note`（`IT-P1-DELIVERY-GATE-PEROUND-001`）。
  * `engine.py`：吞异常**留痕**（计入 `accounting_errors`）；
    **并在 `poll_once` 末尾真的调用 `check_delivery_invariants()`** ——
    此前它在生产里**零调用**，所以"违规 0"只是测试口径。
  * `engine.py`：`web.max_alerts` 接通 `AlertStore`。
  * `server/web.py`：重建 `Alert` 时补 `signal_id`
    （`IT-P1-WEB-SIGNAL-ID-LOSS-001`，17:40 轮 §6 发现）。
- **验收（已达成）**：`tests/test_delivery_false_green.py` **16 条**；
  实测两个假绿病例都变成 `inconsistent`；端到端 481 轮 **6/6 自洽**。
- **回退验牙**：**12 条行为级 RED / 0 结构性**。
- **诚实更正**：我 17:00 轮宣称的"逐轮交付不变量违规 0"是**测试口径**，
  **不是生产保证**。数是真的（独立重跑确实 0），但当时**没有任何东西
  在生产里检查它**。本轮已接线。

### E. ⚠（`R-17` / `R-20`）`signal_id` × `metrics["pattern"]` 一致性门禁 —— **先复核再实施**
- **现状（`R-17`）**：8 处 `signal_id` 是**手工**按 `pattern` 语义命名的，
  与 `metrics["pattern"]` 是**同一事实的两份拷贝** → 可能**静默漂移**。
- **⚠ 但 17:40 轮 §5 警告：原计划的门禁"`signal_id == <模块>.<pattern>`"
  会误报**（`metrics['pattern']` 并非在所有 Alert 上都等于 signal 后缀）。
  **我未独立复核该反例** → 本条**不能照原样实施**，必须先确认映射关系。
- **可证伪**：若真实运行中两者恒相等，门禁永不触发，说明过虑了。

### F. （`R-18`，**09-21 21:00 新增**）`accounting_status` 必须真的显示在用户眼前
- **现状**：`accounting_status` 目前只有 soak 与测试在读；
  `/api/status` 虽导出该字段，但**看板前端是否显示未核实**。
- **危害**：若前端不显示，用户界面上仍可能出现"门禁绿而账本坏"的观感 ——
  **修复只在数据层，未到用户眼前**。
- **做法**：核实看板是否渲染该字段；若无则加显著提示。
- **如实标注为未验证**，不声称端到端可见。

## P0-B — Universe Truth（**最高优先；先测量，再加门禁**）

### A. ⚠ **第一步不是加门禁，而是先重新测量并落盘真实覆盖率**（`R-19`）
- **为什么这是第一步**：我此前引用的 **73.0%（`4090/5917`）与
  83.1%（`4576/5917`）无法复现** —— 独立重算得 **69.12%** 与 **77.34%**，
  换成正确分母 5913 得 69.17% / 77.39%，**都对不上**；
  且分母 `5917` 在代码/fixture 里**不存在**（真实是 `5913`）。
  被指名的证据文件 `universe_coverage_reconcile.json` **不存在**。
- **做法**：重新测量，落盘一个**含源端原始回包 hash** 的真实覆盖率数字，
  并把 `73.0%/83.1%` 从全部文档中**替换或标注为未复现**。
- **验收**：`universe_coverage_reconcile.json` 存在且其分子分母可被独立重算复现。

### B. ✅（**09:00 主体已完成**）让"市场应有多少 → 实际扫了多少"成为一条单一事实链
- **09:00 轮已落地**（详见 D5 / `2026-09-22_09-00-00_JST.md` §1.1）：
  `Engine.universe_truth()` → `store.status()['universe_truth']` →
  `evaluate_health` 的 `universe_coverage` 项。**分母第一次进入事实链。**
- **原证据（保留）**
  * `poll_once()`：`req_stocks = len(self._codes); coverage = returned / requested`
    —— 回答"我决定扫的这些回来多少"，**不问**"市场应有的有多少进了 active scan"。
  * `_universe_meta` 引擎**已算出**却**零出口**（`git grep` 可查）——
    09:00 轮已加出口。
  * 17:40 轮用 `max_pages=41` **离线复现**：`4100/5913 = 69.3%`、
    `transport_complete=False`，但 `refresh_universe()` **仍返回 4100 并写入 `eng._codes`**；
    且 `evaluate_health` **完全没有 universe 项** ——
    健康判定对 `universe_size ∈ {0, 4100, 5000, 5563}` **逐字节相同**。
    （09:00 后 `universe_coverage` 项已能区分，见 D5 实测表。）
* 我 09-21 21:00 轮真实回放中 `eastmoney` 报 `RemoteDisconnected`（第 3 次野外观测）。
- **✅ 悬置疑问已关闭**（我独立复核，09-21 21:00）：`page_size=100`、`max_pages=80`、
  全市场 5913 → 需 `ceil(5913/100) = 60` 页；`60 > 80` 为 **False**。
  **默认配置下 `max_pages` 截断在算术上不可能** —— 短缺只能来自源端
  `pages_failed`。我此前反复写的"无法区分限流与我方 `max_pages`"
  **可从源码单独解决**，是我没去算。
- **危害**：分母被截断时**任何"没报警"都可能是"没扫到"**，
  且 **Recall 的上限未知**。这是唯一会让用户**系统性**误读结果的因素。
- **剩余（下一轮）**：`UniverseTruth` **落盘**成独立产物
  （`universe_coverage_reconcile.json`）与**真实 coverage reconcile** ——
  本轮只做到"进了运行时事实链 + 参与判决"，**没有**落盘产物，
  也**没有**真实网络测量去独立验证 provider 声明的分母。
  membership 来源分离见 **D6**。
- **可证伪**：若真实长时间运行中 `active_coverage` 恒 ≥ 95%，门禁永不触发，
  说明源端其实稳定、这个担心不成立。

### C. ✅（**05:00 已完成**）`IT-P2-UNIVERSE-STATUS-CACHE-001`：`status.universe` 不是 active universe
- **05:00 轮已修**：`status()['universe']` 改用引擎的真实扫描池 `len(_codes)`；
  同时导出 `universe_cached`（旧累计口径），**两个口径都能读到** ——
  否则消费方无法判断这个数是哪个。
- **原缺陷（证实）**：`AlertStore.status()` 用 `len(state.quotes)`，
  而 `state.quotes` 是**所有最近准入过的 latest cache**（个股 + 指数都写，
  且 `EngineState.prune()` **不删** `state.quotes`）。看板"股票池"芯片读它
  ⇒ 扫描范围缩 30% 后**仍显示旧规模**（云端实测高报 1813）。
- **05:00 实测**：扫描池 4100 / 缓存 5913 时，修复前只报 5913（错），
  修复后 `universe=4100`（对）且 `universe_cached=5913`。
- **保留在此仅为记录。**

### D0. ✅（**05:00 已完成**）`R-21`：给 universe 加判决项
- **05:00 轮已修**：新增 `universe` 判决项（降级为自选股 → fail；
  < 3000 只 → fail；< 4500 只 → warn；未测量 → ok）。
- **根因（云端精确化，我独立确认）**：不是"缺判决项"，而是
  **数据早已在生产路径传入 `metrics['setup']`，只差一个读者** ——
  `universe_size` / `fell_back_to_watchlist` 早就有了。
  这把 R-21 从"需要新采集的设计任务"降级为"一个小补丁即可闭合"。
- **保留在此仅为记录**；`R-12`（分母仍未落盘）见 **D5**，仍未闭合。

### D1. 判决项必须"带证据来源"（01:00 想法 1）
- **证据**：同一条线上已出现**至少 4 次**同一失败模式 ——
  "算了/放了字段"被当成"问题解决了"：
`_universe_meta` 零出口、`check_delivery_invariants()` 生产零调用（09-21 21:00 修）、
  `_scope_note` 死键（`R-22`）、`accounting_status` 判决层不读（01:00 修）。
- **做法**：门禁 —— 任何被写进 metrics 的
  `*_status` / `*_accounting` / `*_coverage` 类字段，
  **必须至少被一个判决项引用**，否则测试失败。
- **可证伪**：若不存在"算了没人读"的字段，门禁永不触发。
  —— 本轮已找到 4 个实例，**反证条件已被证伪**。

### D2. 健康判决必须**显式声明盲区**（01:00 想法 2）
- **证据**：`exit 0` 目前的真实含义是"我检查的 N 件事都过了"，
  **不是**"系统健康"。用户读到的是后者（`R-21`、`R-12` 都是盲区）。
- **做法**：判决输出加 `not_checked: [...]`，逐项给出"为什么现在测不了"。
- **可证伪**：若 `not_checked` 恒为空，说明系统确实全覆盖，改动无用。
  —— 已知至少 universe 一项为空，**反证不成立**。

### D3. 🔴 **可观测性修复必须配端到端验收**（05:00 新增，最高优先）
- **证据（本轮我自己就是反例）**：修空轮落账时我写了
  `self.sources.capabilities()`，而 **`SourceManager` 根本没有这个方法**。
  异常被我自己的 `except: self.log.debug(...)` 吞掉 → **账本一份都没发**，
  外表完全正常。**24 条单元测试全绿**（纯函数行为是对的，
  错的是**没人喂给它数据**）；只有跑真实 `_soak_loop` + 真实 Engine 才暴露
  （`obs.returned=None`、`observation_seq` 不推进）。
- **危害**：这正是我连续三轮批评的反模式（"可观测性失败被静默吞掉 → 假绿"），
  **我自己又犯了一次**。
- **做法**：
  1. 任何可观测性修复**必须**附一条跑真实 `_soak_loop`/真实 Engine 的用例，
     并断言**产出通道真的动了**（如 `observation_seq` **严格递增**）；
  2. `except` 里的日志**禁止只写 `debug`** —— 可观测性失败可以不断主链路，
     但**不许不留痕**（本轮已把该处提到 `error`）。
- **可证伪**：若端到端用例从不比单测多抓到东西，这条规矩无用。
  —— 本轮它**多抓到了 1 个**（我自己的实现 bug），**反证不成立**。
- **验收**：`test_e2e_empty_round_publishes_zero_return_observation`
  （已落地，断言 seq 严格递增）。

### D4. 同一 bug 类的**系统性收口**（05:00 新增，09:00 扩为两类，见下文合并版）
- 本节已与 09:00 新增的姊妹类合并到 **D4（合并版）**，见本文件后段。
  保留标题仅为历史可追溯；**请直接读 D4（合并版）**。

### D5. ✅（**09:00 已完成**）`R-12`：分母进入事实链 + 相对覆盖判决项
- **09:00 轮已修**，一条事实链三处出口（详见 `2026-09-22_09-00-00_JST.md` §1.1）：
  1. 新 `Engine.universe_truth()`：分母 + 四个分开的 coverage + `denominator_kind` 三态；
  2. `store.status()['universe_truth']`：`_universe_meta` 的**第一个生产出口**；
  3. `evaluate_health` 新增 `universe_coverage`（`<90%` fail / `90–95%` warn /
     `>=95%` ok / 分母未知 **未测量判 ok**）。
- **实测修前 → 修后**：`4500/5913 = 76.10%` **ok → fail**；
  `4100/5913` warn → fail；`4200/4100` warn → ok；
  同一绝对只数 4100 在 `/4200` 与 `/5913` 下**判得不一样**（修前相同）。
- **仍未闭合的那一半**：分母**未知**时仍只能判"未测量" ——
  这是诚实的（无 numeric total 时本就算不出比例），不是遗漏。
- **仍未做的事**：**没有**任何真实网络测量去独立验证 provider 声明的分母
  是否真等于全市场。本轮修的是**测量前提**，不是测量结果。

### D6. 🔴 **membership 与 quote usability 分离**（**09:00 新增；连续三轮最高优先未做**）
> **17:00 轮说明**：本条**连续三轮**（09:00 / 13:00 / 17:00）被列为最高优先而**未做**。
> 16:13 WP06 明确要求"**单独 diff**，不和 health consumer 混一起"——
> 13:00 与 17:00 两轮都是 health consumer 工作，按该纪律**不混**。
> 下一轮应**专门**做这一条。
- **证据（云端 §5 / §8.2，我独立复核成立）**：Eastmoney 已经在 metadata 里
  把 `transport_complete`（传输轴）与 `usable_coverage`(诊断轴) 拆开了，
  **但在 active membership 上又合并了**：
  ```
  Eastmoney universe()
  → 返回 parsed usable Quote 列表
  → Engine _record_universe(quotes)
  → _extract_codes(quotes)
  → active _codes
  ```
  代码自身允许
  `expected_total=5913 / raw_unique_codes=5913 / transport_complete=True /
   usable_quotes=4600` —— 此时 active membership 会变成 4600，
  **而实际上有 5913 个成员本该被扫描**。
- **危害**：停牌/无效价格行会**静默缩小扫描集合**，
  且缩小后的规模看起来"正常"。这正是 `R-12` 的兄弟缺陷：
  R-12 是"分母没进事实链"，这条是"分子被 usability 静默改小了"。
- **我 09:00 轮为什么没改**：改 `_record_universe` 的 membership 来源
  **会改变实际扫描集合**（真的会去请求更多代码），风险远大于加一个出口。
  必须有**真实 provider 回放**才能安全验证，列为下一轮最高优先。
- **可证伪**：若真实东财 universe 的 `usable_quotes == raw_unique_codes`
  （即市场无停牌），则本缺陷永不触发。
- **验收**：`expected_total=100 / raw_unique_codes=100 / usable_quotes=94 /
  transport_complete=True` 时，**active membership 必须仍有 100 个 code**，
  `usable_coverage=94%`。

### D7. 恢复时钟（`IT-P1-UNIVERSE-REFRESH-STORM-001`，09:00 新增）
- **证据（云端 §8.4，我**未**独立复现调用次数曲线）**：smaller partial 被拒后
  **没有独立 retry clock**，可能每个 5s poll 重刷一次 universe。
- **做法**：冻结三个语义不同的时刻 ——
  `last_attempt_at`（前进）/ `last_applied_at`（被拒时不变）/
  `last_complete_at`（被拒时不变）。`universe_truth()` 里**已预留透传**
  （`if key in meta`），但 provider 侧还没写这三个时刻。
- **可证伪**：若真实长跑下 `refresh_universe` 调用次数远小于 poll 次数，
  本担心不成立。**我尚未测量这个次数** → 列为待验证。

### D8. ✅（**13:00 已完成**）截断盲区 + 刷新尝试台账 + 会话级新鲜度
- **13:00 轮已修**（详见 `2026-09-22_13-00-00_JST.md` §1）：
  1. 拆出独立 `universe_transport` 判决项，只读 `transport_complete`
     —— **不与 `universe_coverage` 合并**（证据来源不同，合并会互相掩盖）；
  2. `active_snapshot` / `latest_attempt` / `freshness` 三账分离，
     四条出口（`applied`/`applied_partial`/`rejected_smaller`/`all_failed`/`empty`）全落账；
  3. 每轮采股票池新鲜度（只取标量）+ `_universe_session()` 聚合取**最坏**，
     `evaluate_health` 优先读会话级而非 t0 快照。
- **实测**：`transport_complete=False` → `ok/healthy=True` → **fail**；
  全源失败后 `attempt_status` **不存在 → `all_failed`**，
  两次 `universe_truth()` 逐字段相同 **`True → False`**；
  t0 健康 + 会话 stale → `healthy=True` → **`False`**。
- **仍未做的部分**：未加 partial refresh retry/backoff（WP06）；
  `active_snapshot["epoch"]` 是**占位字段**（`_universe_epoch` 不存在，恒 0）。

### D9. 🔴 **"未测量"必须有穷尽的前置条件**（**13:00 新增，最高优先**）
- **证据（我本人是反例，且是同一错误的第 5 次）**：
  | # | 轮次 | 形态 |
  |---|---|---|
  | 1 | 21:00 | 交付账本数据层有了、判决层零读者 |
  | 2 | 05:00 | `len(state.quotes)` 累计缓存当逐轮量 → `no_data_rounds` 恒 0 |
  | 3 | 05:00 | `_acked` 哈希序裁剪丢掉刚 ack 的 |
  | 4 | 05:00 | `status()['universe']` 用累计缓存 → 看板高报 |
  | **5** | **09:00（13:00 被指认）** | **新 gate 把"provider 明说被截断"归入"未测量"** |
  | **6** | **13:00（17:00 被指认）** | **`bool(None)` 把"未测量"渲染成"provider 声明被截断"——假绿的镜像（假红 + 归罪数据源）** |
  共同形态：**三态被压成两态** —— 要么把"我不知道"当成"没问题"，**
  要么当成"有罪"。两者同源。
- **做法**：规定每个判 `not_measured` 的分支**必须逐条枚举**它排除了哪些
  已知坏情况，并各配一条测试；可做成门禁扫描。
- **可证伪**：若穷举后从未发现遗漏，这条规矩无用。
  —— 本轮它**当场抓到 1 条**（`transport_complete=False`），**反证不成立**。
- **验收**：`test_transport_and_coverage_are_separate_evidence`
  （断言同一输入下两项结论**不同**）已落地。

### D10. 健康判决必须能回答"**这份结论有多旧**"（**13:00 新增**）
- **证据**：本轮 RED 2 实测 —— 全源失败后 `universe_truth()` **逐字段不变**，
  消费者看到 `coverage_active=93.85%` 却**不知道**它是刚测的还是两小时前的。
  更普遍：`exit 0` 的含义是"我检查的 N 件事**此刻**都过了"，
  **不是**"系统在过去一小时内健康"；而 soak 是**数小时**连续运行，
  用户读到的显然是后者。
- **做法**：给每个判决项附 `observed_at`；要求最终结论基于
  **会话级最坏**而非**起点快照**（本轮已在 universe 轴落地，应推广到全部判决项）。
- **可证伪**：若真实长跑里 t0 快照与会话最坏**从不**不同，这条规矩无用。
  —— 本轮已构造出二者不同的情形，**反证不成立**。

### D11. 🔴 **health fact 必须有 consumer-effect test**（**17:00 新增，最高优先**）
- **证据**：17:00 轮的**全部 9 条缺陷**都是同一形态 ——
  **数据算了 / 字段进了报告 / 测试只断言字段名存在 / 最终判决没有消费者**：
  | # | 事实 | 形态 |
  |---|---|---|
  | 1 | `rounds_transport_incomplete` | 第 **6** 次"判决层零读者" |
  | 2 | `ev_fail_cov` | **说谎的旋钮**（配置静默无效） |
  | 3 | `freshness.warn_s`/`fail_s` | **修 B5 时新发现**的零读者 |
  | 4 | `watchlist_only_rounds` | 只有 30/30 才 True，29/30 漏 |
  | 5 | `active_age_s` vs `full_market_age_s` | 两个时钟被合并 |
- **做法**：任何被宣称为 health fact 的字段，必须配一条：
  ```
  先造 fully-green baseline
  → 只修改那一个事实
  → 断言**完整 verdict signature** 按设计变化
  ```
  不能只做 `assert "field_name" in source` 或"字段在报告里存在"。
- **已落地**：`test_every_session_fact_has_a_consumer`（参数化 4 个事实）
  + `test_green_baseline_is_actually_green`。
- **可证伪**：若穷举所有 health fact 后从未发现新的零读者，这条规矩无用。
  —— **反证不成立**：本轮**当场抓到 2 个**。
- **验收**：上列两条测试；推广到**全部** health fact（目前只覆盖 session 轴）。

### D12. 🔴 **修复声明必须验三条，不止"机制存在"**（**17:00 新增**）
- **证据**：我 13:00 轮的失败**不是**机制写错了 —— 三条机制**都对**。
  失败在于验收只覆盖了：
  * ①**边界可达性** —— "字段缺失就跳过"那条分支在真实引擎上**不可达**
    （顶层恒 `bool`），而**触发的是诬告分支**；
  * ②**出口完整性** —— 数 `refresh_universe` 的 **4 个 `return`** 不够，
    **异常出口不是 `return`**（循环头在 try 之外）；
  * ③**新引入的镜像错误** —— 修"把不知道当没问题"时，
    亲手造出"**把不知道当成有罪并归罪于数据源**"。
- **做法**：每个修复声明附三问 —— 这条分支**真实可达吗**？
  **所有**出口（含异常）都处理了吗？我**新造出**什么反向错误？
- **可证伪**：若按三问复核后从未发现遗漏，规矩无用。
  —— **反证不成立**：本轮三问**各抓到至少一条**。
- **验收**：本轮每条修复的 docstring 都写明了它的边界与出口；
  可进一步做成 PR 模板/门禁。

### D13. ✅（**17:00 已完成**）tri-state 端到端 + session 消费独立化
- **17:00 轮已修**（详见 `2026-09-22_17-00-00_JST.md` §1）：
  `True`/`False`/`None` 三态在 Engine → `make_round_sample` → 聚合 → 判决
  **全链保留**，任一层禁用 `bool(x)`；`_optional_bool`/`_optional_float`；
  session 事实**独立于** t0；`universe_scope` 项（count/ratio/ever）；
  两个时钟拆分；正式配置键 + 判决消费 Engine 阈值；
  `unknown` → "未测量"；外层 finalizer（**P2**，非 P0）。
- **仍未做**：`rounds_transport_incomplete` 之外的 session 事实
  （如 `rounds_transport_measured`）尚未各自配 consumer-effect test。
- **⚠ 21:00 轮更正**：本条的「session 事实独立于 t0」**方向对但不够** ——
  我把合并语义写成了「**session measured facts > t0 snapshot**」，
  于是**后来的好消息开始抹掉先前的坏消息**（见 D14）。已改为 join/吸收语义。

### D14. 🔴 **证据合并必须有 join 语义，不能只说"谁优先"**（**21:00 新增，最高优先**）
- **证据**：我 17:00 轮立的规矩「session measured facts > t0 snapshot」
  **方向对但不够**。实测两个反例：
  ```
  ① t0 transport_complete=False + session 30 轮全完整  -> ok（错，应 fail）
     硬事实被后来的好消息抹掉
  ② _universe_session([]) -> measured=False/ratio=None/ever=False
     -> 「会话期间未降级为仅自选股（全程全市场扫描）」  （零证据 = 肯定证据）
  ```
  两条**同源**：合并函数不是**单调**的 —— 加入"明确坏"的证据**可以改善**结论。
- **做法**：把合并规则从"会话优先"改成**格（lattice）上的 join**：
  ```
  fail ⊔ anything  = fail        # explicit hard negative 是**吸收元**
  ok   只在**所有有证据的来源都 ok** 时成立
  unknown 是单位元
  ```
  **单调性要求**：加入明确坏证据**永远不能**改善结论
  （20:04 §7 的 288 个 monotonicity property checks 就是这个意思）。
- **可证伪预测**：若穷举 t0 × session 所有组合后发现"会话优先"
  与 join 语义**结论一致**，这条规矩无用。
  —— **反证不成立**：本轮实测 `t0=False + session all-true` 的**全部分支**
  都被判得更轻。
- **验收**：`test_t0_hard_negative_beats_session_all_complete`（三档）、
  `test_zero_evidence_scope_is_not_asserted_as_full_market`。
  应推广到**所有** t0 × session 合并点（目前只覆盖 transport 与 scope）。

### D15. 🔴 **有负控制 ≠ 有有效的负控制**（**21:00 新增**）
- **证据**：我在 17:00 轮把 `test_explicit_incomplete_still_fails` 写成负控制
  并写进报告。**但它的输入恰好把被测分支绕过去了**：
  ```
  t0=False + rounds_transport_measured=0   -> fail   <-- 我的测试走的路径
  t0=False + rounds_transport_measured=1   -> ok     <-- 一个单位就翻绿！
  t0=False + rounds_transport_measured=30  -> ok
  ```
  **它从未在 `measured > 0` 时试过被测分支。**
- **做法**：负控制必须**显式走过被测分支**。可操作判据：
  ① 打印/断言被测分支的**入口条件**在本用例下为真；
  ② 对会"绕过"分支的**哨兵参数**做参数化（本例 `measured ∈ {0,1,30}`）；
  ③ 配对正控制（本例 `t0=True -> ok` 证明分支真的活着）。
- **可证伪预测**：若按此三问复查所有负控制后从未发现绕过，规矩无用。
  —— **反证不成立**：本轮**当场抓到 1 条（我自己的）**。
- **验收**：`test_explicit_incomplete_still_fails` 已参数化；
  应复查 **全部** 已有负控制测试。

### D4（合并版）. 三个 bug 类的**系统性收口**（05:00 起，09:00 扩两类，13:00 +1，17:00 +1）
- **现状**：该 bug 类（**有界容器/累计缓存的长度被当作完整总量**）已确认 **5 个实例**：
  | # | 位置 | 状态 |
  |---|---|---|
  | 1 | 测试用 ring buffer 当累计分母 | 21:00 修 |
  | 2 | `web.py:603` 按 kind 的 total 被截断 | 01:00 修 |
  | 3 | `live_session.py:1971` 累计行情缓存当逐轮数 | 05:00 修 |
  | 4 | `store.py:359` `_acked` 裁剪保留"任意"2000 | 05:00 修 |
  | 5 | `store.py:304` `status()['universe']` 用累计缓存 | 05:00 修 |
- **09:00 新增的姊妹类**（**"同一语义写了两套实现"**）：
  | # | 位置 | 状态 |
  |---|---|---|
  | 6 | 空轮分支手写 request 算术 → **幻影** `index_requested=5` | 09:00 修 |
  | 7 | 空轮分支 early return 早于 poll 自增 → **poll identity 断裂** | 09:00 修 |
  | 8 | 我**更早一版**调用不存在的 `self.sources.capabilities()` 被 `except` 吞掉 | 05:00 修 |
- **为什么还要做**：8 个都是**逐个被抓出来的**，不是**系统性找出来的**。
  每轮靠外部审计发现同一个类的新实例，本身就是流程缺陷。
- **做法**：把两个类都做成门禁 ——
  (a) 枚举仓内 `len(...)` 被用作"总量/分母"的位置，要求声明读的是**累计**还是**逐轮**；
  (b) 扫描 `poll_once` 内是否出现**重复**的 request/poll 算术（必须走唯一 helper）。
- **可证伪**：若穷举后没有第 9 个实例，说明该类已收口。

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
