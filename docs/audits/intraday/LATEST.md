# 最新审计

**最新本地 Agent 轮**：[`2026-09-21_17-40-00_JST.md`](./2026-09-21_17-40-00_JST.md)  
**上一份本地 Agent 产品轮**：[`2026-09-21_17-00-00_JST.md`](./2026-09-21_17-00-00_JST.md)  
**最新云端独立审计**：[`2026-09-21_16-07-37_JST.md`](./2026-09-21_16-07-37_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-21_16-07-37_JST_AGENT_TASK.md`](./2026-09-21_16-07-37_JST_AGENT_TASK.md)  
**上一份本地 Agent 轮**：[`2026-09-21_13-37-29_JST.md`](./2026-09-21_13-37-29_JST.md)  
**上一份云端独立审计**：[`2026-09-21_12-02-53_JST.md`](./2026-09-21_12-02-53_JST.md)  
**下一步计划**：[`NEXT_STEPS.md`](./NEXT_STEPS.md)  
**仓库执行清单**：[`RUN_MANIFEST.json`](./RUN_MANIFEST.json)

> 历史审计文件均保留在本目录；本索引只移动当前接续指针，不删除历史报告。

## 2026-09-21 17:40 JST 独立审计轮（**复核 6997487：修复通过；但新门禁在运营面上只读"末轮"，且 R-12 仍不可观测**）

- 被审：`6997487f0ec5d92f81299edbb8c68845e9e056a8`（上一被审基线 `eba8c4af5ed943a023ab09ab9c30000c1ac58f65`）。
- 取证：全量 pytest **1660 passed in 120.52s（exit 0）**；`tools/check_*.py` **8/8 exit 0**。

### ✅ `IT-P1-DELIVERY-LEDGER-002` 修复**复核通过、并独立复现**

真实 `Replay + Engine + Store`（361 轮 / 30 只 / `seed=42`）：Store 里 **66/66** 告警带 `signal_id`；**逐 signal 账本 `committed` 与 Store 实数逐条相等**（`limit_board.*` / `tick_surge.*` / `unusual.*` / `volume_burst` 共 8 个 signal）。**修复前的 `6/66 = 0.0909` 我也独立复现**（用 `eba8c4a` 的旧门禁在同一批告警上做反事实）。

**MUTATION 测试确认新门禁不是恒真式**（这是必须做的）：1/3 未打标 → `0.6667`；全未打标 → `0.0`；分母 0 → `None`（不是伪造的 1.0）。提交作者显式避开了"分子分母同源 → 覆盖率恒 1.0"的陷阱，**这一点做对了**。

**⚠ 但必须限定**：9 个 `Alert(...)` 站点**全部**赋非空 `signal_id`（4 处三元表达式**两个分支都是非空字面量**），`RULE_MODULES` 恰好是这 7 个模块，`entry_point`/`pkgutil`/`plugin` 在 `src/` **0 命中** —— **不存在新增规则的第三方路径**。故 **`coverage == 1.0` 在当前规则集上是恒真式**：`9.1% → 100%` 作为**历史差量**为真，但 `100%` 是**这次编辑的性质**，不是**持续被验证的状态**；其唯一剩余价值是**未来某条规则忘填 `signal_id` 时的回归绊线**。

### ⚠ `IT-P1-DELIVERY-FALSE-GREEN-001`（**本轮新增 · P1 · 已确认 · 最危险**）**账本永久损坏却报 PASS**

`engine.py:1322-1325` 用**裸 `try/except Exception: pass`** 包住**分母自增本身**；`capabilities.py:831-833` 又用 `max(total - named, 0)` **夹逼**门禁。我构造并运行了反例（`D:\ccc\_sched\work\asr_h16_falsegreen.py`）：

- **假绿**：分母自增抛异常 → 被吞掉 → `committed_alerts_total` 停在 0 → 门禁读 **0（PASS）**、`signed_ratio` 读 `None`。**一个彻底坏掉的账本与一个完美的账本，在运营面上完全无法区分。**
- **夹逼把"少算"变成 PASS**：分母 1 / 分子 3 → `max(-2,0) = 0`（PASS），且 `signed_ratio = 3.0`（数学上不可能）而不触发任何告警。
- **假红**：分子侧异常 → 门禁报 2 违规，而**那 2 条告警全部带 `signal_id`**（冤枉无辜规则）。

全仓库**没有任何吞异常计数器**（`git grep -E 'swallow|_mark_errors|mark_failures|observability_errors' -- src` → 0 命中）；同文件 `_dispatch_many`（`:1462-1463`）**是有日志的** —— **内部标准不一致**。**修复：`except` 里计数并 `log.exception`；去掉或替换 `max(...,0)` 夹逼。**

### ✅ `check_delivery_invariants()` 在生产里**零调用**（证伪成功）

`git grep -n check_delivery_invariants` → **只有它自己的定义**（`capabilities.py:711`）+ 2 行散文。**连新测试都没调用**（新测试只测 `SignalDeliveryStats.check_invariants()` 的**单实例**版）。故 `NEXT_STEPS.md:11` 的"**逐轮交付不变量违规 0**"是**测试口径**，**不是生产保证**。

### ⚠ `IT-P1-SOAK-LEDGER-BLIND-001`（**新增 · P1 · 我独立复现**）soak 长跑对交付账本**完全盲**

用 `tools/live_session.py` **自己的常量**做端到端复现：`_OBSERVATION_MARKER_KEYS`（15 键）/ `_OBSERVATION_VALUE_FIELDS`（14 键）**都以旧账本 `signal_evaluability` 结尾**，真实 `as_dict()` 里的 `signal_delivery` 与 `delivery_accounting` **两个白名单都不含**。故 soak 的采样产物与阈值判定**永远看不到新门禁**。

**汇总**：新门禁的执法者**只有单元测试** —— `tools/check_*.py` **8/8 零覆盖**（§2.5）、`check_delivery_invariants()` **零生产调用**（上条）、soak **白名单不含**（本条）、`dashboard.html` 零渲染。**"算得出、进 JSON、没人看"。**

### ❌ 推翻子 agent C 的一处推理：`global_ignored` **不是** vestigial

C 据 `engine.py:1144` 排除 ignore 码推断 `:1295` 分支"近似不可达"。**该推理漏看了 `:1148-1159` 的 watchlist 回填**：规则实际看到的 `snap.quotes = watch_cur ∪ eligible`，而 `watch_cur` **只按 `self.watchlist` 过滤、不做 ignore 判定**。故只要某代码**同时在 `watchlist` 与 `ignore`**，规则仍能为它产出 `Alert`，`:1298` 即被执行。**我记为"可达性与真实触发频率均未复现"，只推翻其过强表述**（不指控死、不声称活）。

### ⚠ 不采信 C 的 D3 计数（定性成立、数字未复现）

C 称"28 个 `signal_id` 仅 1 个可直接索引 `SIGNALS`"。**定性我确认**（账本键是 `模块.形态`，`SIGNALS` 用裸名，需自行 `rpartition('.')`）。但**"28"我没能复现** —— 静态可确定的 `signal_id` 字面量只有 **9** 个（其余是 f-string，需展开运行时注册表；`SIGNALS` 自身 30 条）。**故只采信定性，不引用该计数及其"无对应 signal_id 的展示名"清单**（在不完整枚举下该清单必然偏大，属枚举不全，非缺陷证据）。

### ⚠ `IT-P1-DELIVERY-GATE-PERROUND-001`（**本轮新增 · P1 · 已确认**）新门禁是"逐轮"的，运营面读到的是**最后一轮**

`store.observation` 只保留最近一轮的 `as_dict()`（`store.py:156` 每轮**覆盖**）。实测 361 轮：**累计** 66/66 = **1.0**，但**末轮** `committed_alerts_total = 0` → 运营面 `signed_ratio = None`、`first_party_committed_without_signal_id = 0`。**305/361 轮是 0 条告警**，故该门禁绝大多数时候读 `None`。仓库内 `committed_alerts_total` **只有 `engine.py:1323` 一个写入点**，`store.py` / `web.py` / `cli.py` 对它**0 命中** —— **没有任何累计计数器可达运营面**。

### ⚠ `R-12`（**仍是最严重未修项 · 且本轮确认它连可观测性都没有 · 头号数字还系算错**）

新门禁对扫描范围**完全不敏感**：实测 100% 与 69% 扫描 `coverage` **都读 1.0** —— 它是**告警**的账，不是**标的**的账。引擎**已经知道**池子被截断（`engine.py:938-940/964-966` 打警告；`meta` 存于 `engine.py:953/972`，含 `eastmoney.py:474` 的 `usable_coverage`），但**零出口**：`store.py` / `web.py` / `cli.py` / `replay.py` / `session.py` 对 `universe_meta` **全部 0 命中**，唯一读取者是 `tests/test_engine.py:929-930` 戳私有属性。运营面唯一可见的是 `store.py:304` 的 `"universe": len(quotes)` —— **被截断后**的数量。**"没报警"与"没扫到"在产品上依旧等价。**

**❗ 并且线内引用的头号数字本身算错了**（5 处文档：`13-00-00_JST.md:390/:403`、`17-00-00_JST.md:194`、`LATEST.md:56/:107`、`NEXT_STEPS.md:93-94`）：写的是 `4090/5917 = 73.0%`、`4576/5917 = 83.1%`，**实际是 `69.1229%` 与 `77.3365%`**；且分母 `5917` **不在任何机器产物中**（`git grep 5917 -- src tests fixtures tools` 仅命中 `fixtures/` 的字节巧合；代码口径是 **`5913`**）。**标为 `未复现`。修 `R-12` 的第一步不是加门禁，而是先落盘一个真实覆盖率数字。**

**离线复现（我采信子 agent）**：`EastmoneySource(max_pages=41)` → `4100/5913 = 69.3%`、`transport_complete=False`，但 `refresh_universe()` **仍返回 4100 并写入 `eng._codes`**（部分池被静默接受，`engine.py:968-975`）；`tools/live_session.py:814-1139` 的 `evaluate_health` **完全无 universe 项**，对 `universe_size ∈ {0, 4100, 5000, 5563}` 给出**逐字节相同**的 `healthy=True exit=0`。**附带澄清**：默认 `page_size=100` / `max_pages=80` 时 `ceil(5913/100)=60 ≤ 80`，**`max_pages` 截断在算术上不可能** —— 任何短缺都是源端侧。

### 其余本轮发现

- `R-15`（**已复现崩溃**）：`Alert(ts=None).to_dict()` → `AttributeError: 'NoneType' object has no attribute 'strftime'`（`models.py:590`；`ts` 在 `:526` 是**非可选**，对比 `Quote.ts` 在 `:332` 是 `datetime | None`）。**单元层 100% 复现**；端到端可达性 `未复现`（引擎自身总传 `now`）。影响面大：`to_dict()` 是所有出站必经点。**一行守卫即可修。**
- `IT-P1-NOTIFY-RESULT-001/002`（**仍未修**）：`NotificationResult` 与 `event_id` 在 `src/ tools/ tests/ fixtures/` **全 0 命中**；引擎**丢弃**通知返回值（`engine.py:1443/:1458/:1461`）；7 个通知器在"禁用/跳过"时**全部 `return True`** → **`skipped` 与 `sent` 不可区分**。
- `R-14`（**仍未修 · 装饰性**）：`rule_version` 确为 write-only —— `src/`+`tools/` 穷举只命中 `models.py:27/64/605/613`；`tools/` **0 次**、`web.py`/`store.py`/`dashboard.html` **各 0 次**。
- `R-13`（**有意默认，非缺陷**）：我实测 `build_rules(默认配置)` 只产 **4/7** 规则 `['limit_board','tick_surge','unusual','volume_burst']`，三个 `spirit_*` 在 `settings.yaml:188/223/257` 为 `enabled: false`。
- `R-17`（**收窄**）：真正会漂移的是 **4 处字面量站点**（`limit_board.py:354/390/496`、`tick_surge.py:287`），它们与同 dict 里的 `metrics["pattern"]` 是同一事实的两份拷贝；另 3 个规则是 `f"...{pattern}"` 派生（不漂移）。`tools/` 8 个检查器**没有一个覆盖 `signal_id`**。**无一致性门禁。**
  - **⚠ 警告**：`NEXT_STEPS.md:85-86` 提议的门禁 `signal_id == f"{module}.{pattern}"` **会对 3/7 规则误报** —— `tick_surge` 与 `volume_burst` 的 `metrics` **根本没有 `pattern` 键**，且 `volume_burst` 的 `signal_id` 是**无点裸串** `"volume_burst"`。必须带豁免表。
- `IT-P2-ALERT-DICT-ROUNDTRIP-001`（**新增**）：`web.py:272-284` 从 dict 重建 `Alert` 时读 11 字段、**丢掉 `signal_id`**，而 `Alert.to_dict()` **是写出它的** —— 有损往返，静默（`signal_id` 默认 `""`）。
- `web.py` 的 `spirit.signal_of` 取值链是 `metrics["pattern"] → metrics["signal"] → AlertKind`，**不含 `signal_id`** —— 看板信号名与账本 `signal_id` 无强制一致关系。
- 一处**恒真式测试**：`tests/test_delivery_ledger_decoupled.py:162-180` 的 `test_signal_id_alone_is_not_enough`，其 L174/L176 对任何实现都成立（新建对象 `signal_evals` 本为空），**从未构造过旧门禁**。声称的结论**我独立确认成立**，但那条测试没有证明它。

**本轮结论**：修复**真实且可复现**；但 §2.4 / §3 / §3.5 / §4 几条新的 P1 说明 —— **"交付账已闭合"不等于"这一轮扫描可信"**，也不等于"账本坏了会被发现"。后两者才是决定"没报警"能否被解释的。**

**⚠ 更正上一轮（17:00）的两条结论**：① "`signed_ratio` = 1.0"是**会话累计**，运营面实际只读**末轮**（本 361 轮末轮为 0 条 → `None`）；② "逐轮交付不变量违规 0"是**测试口径**，生产零调用。

---

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
