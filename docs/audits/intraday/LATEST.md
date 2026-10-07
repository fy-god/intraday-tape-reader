# 最新审计

## 最新本地独立审计：2026-10-08 05:20:00 JST（第 18 轮）

**完整报告**：[2026-10-08_05-20-00_JST.md](./2026-10-08_05-20-00_JST.md)  
**本轮起始 HEAD**：`6f990ec`  
**本报告对应 HEAD**：`392253daf5468b5d0c17a6a6a92a86665a4284a5`  
**reviewed_source_sha**：`b9206193a1fa946ced9caaef11871c6cfbfb7b6c`  
**本轮待审提交数**：**1**（`6f990ec`，纯文档，非 `docs/` 路径 **0** 个）  
**测试数字**：独立副本复跑 **2216 passed / 0 failed，rc=0**（与本线第 17 轮声称一致）  
**三轴**：`execution_status=COMPLETED` / `research_verdict=NO_NEW_SOURCE_COMMIT` / `evidence_status=REAL_REPO_MEASURED`

本轮核心发现：

| ID | 摘要 | 状态 |
|---|---|---|
| `IT-P1-R17-REPORT-TIME-INCONSISTENT-WITH-ITS-OWN-FIX-051` | 第 17 轮自称写于 `2026-09-30 13:11 JST`，但其收录的修复 `050` 实际提交于 **`2026-10-08 02:29:52 +0800`**（晚 8 天）。同报告内 `047/048/049` 都在自称时刻附近，唯独 `050` 不然 → 不是整体时钟偏移。**报告自身提交时间亦为 `2026-10-08 02:34:33`。** | **已确认（时序不自洽）**；`050` 的"9-30 已验证"**降级**为"最早可证于 2026-10-08 02:29" |
| `IT-P3-UNIVERSE-TRUTH-ZERO-READERS-052` | 承接上轮：`universe_truth` 在 `src/` 命中 **10** 处，**消费方 0**（产出 `engine.py:1361`、装配 `store.py:377`），却随 2 秒 SSE tick 重发 | **待验证风险（结转，本轮未修）** |
| `IT-P3-ALERTS-TOTAL-DEGENERATE-PATH-053` | 承接上轮：`_alerts_total()` 退化路径（`server/web.py:595`，调用 `:589`），`src/` 命中 12 处 | **未复现 → 维持降级（结转）** |

⚠ **本轮无任何源码提交可审**（唯一提交 `6f990ec` 是纯文档），故 `research_verdict = NO_NEW_SOURCE_COMMIT`。
⚠ **模型真正增量提升 = 0**；真实 Precision/Recall/漏事件率/收益仍 `unavailable`（未编造）。
⚠ **本轮未改任何源码、未改测试、未动排程。**

---

## 最新本地独立审计：2026-09-30 13:11:00 JST（第 17 轮）

**完整报告**：[2026-09-30_13-11-00_JST.md](./2026-09-30_13-11-00_JST.md)  
**本轮起始 HEAD**：`21ed0ac`  
**本报告对应 HEAD**：`b9206193a1fa946ced9caaef11871c6cfbfb7b6c`  
**reviewed_source_sha**：`b9206193a1fa946ced9caaef11871c6cfbfb7b6c`  
**证据目录**：[evidence_2026-09-30_13-11-00_JST/](./evidence_2026-09-30_13-11-00_JST/)  
**测试数字**：2183 → **2216 passed / 0 failed**  
**门禁**：`check_*.py` 8 ok / 0 fail；`selftest` rc=0；`dash_render_check.js` rc=0；`check_bom.py` rc=0  
**回滚验牙**：047 = 4/4 RED；049 = 2/2 RED；050 = 2/2 RED（全部字节级还原）

本轮四个修复：

| ID | 摘要 | 提交 |
|---|---|---|
| `IT-P1-ORDERING-GATE-USES-UNTRUSTED-CLOCK-047` | 乱序门误用不可信时钟，误杀北交所行情。实测倒退是**板块专属**（北交所 49.1% / 沪深 0.0%），全市场 910 条乱序拒绝 100% 是 920xxx。修后**同一口径 0 条**。 | `6c9ef20` |
| `IT-P2-QUOTE-SPEED-CLOCK-MISMATCH-050` | 异动榜 `speed` 按**墙钟**切窗口，`replay` 下恒 0 → 榜单退化成插入序。修后 `speed_1m` 非零 17/20。**注意：这是 replay 专属，实盘可用（实测 speed_1m=2.5）。** | `b920619` |
| `IT-P2-ONCE-SCOPE-MISREPORT-048` | `once` 显示"扫描 10 只"实扫 5571 只。覆盖面本来是对的，错的是**显示口径**。 | `feabf93` |
| `IT-P2-ALERT-WINDOW-VS-TOTAL-049` | 看板把"有界窗口里没有"渲染成"今天没有"。**由子 agent 实现，父 agent 独立验牙后收录。** | `dc54922` |

生产复测 `live_session.py --rounds 6 --minutes 3`：**✓ 健康（exit 0）**，池 5571，覆盖率 100%，观测账本
**请求 33426 / 返回 33426 / 准入 33426（100%）**（修前同口径 43658/44568 = 98.0%，乱序拒绝 910），
证据等级 `exact`，API 非 200 = 0。

⚠ **必须如实说明**：本轮 6 轮 soak **全程午间休市**，价格冻结。故"乱序拒绝 0"成立，
但"盘中不再漏价格变化"**本轮无法直接证明**（那是受控实验的结论）。`alerts=0` 是休市应有的，不是检测能力证据。

⚠ **本轮四条修复都不等于用户现在能收到有用的盘中预警**（详见报告 §五逐条对齐）。
最关键的结构性发现是报告 §六想法 2：**`live_session.py` 的 18 项检查全部可在 `alerts=0` 时通过** ——
"✓ 健康"与"用户收到了有用预警"**没有任何蕴含关系**，我们一直在优化一个不衡量目标函数的指标。

**本轮已下调的结论**：子 agent 报的 `speed` 恒 0 **下调为 replay 专属**（实盘 live 语义实测可用）；
eastmoney `RemoteDisconnected` **不是我们的 bug**（独立子 agent 复现，本机 host 被阻断）；
"刷新失败把 2h 改写成 0s" **未复现**。

**新增待验证风险**（子 agent 提出，本轮未独立复现、未修）：`universe_truth` 占 `/api/status` 83.2% 字节却**零读者**，
且随 2 秒 SSE tick 重发；`_alerts_total()` 退化路径（当前不可达）。

---

## 上一版本地独立审计：2026-09-30 12:30:00 JST（第 16 轮）

**完整报告**：[2026-09-30_12-30-00_JST.md](./2026-09-30_12-30-00_JST.md)  
**reviewed_source_sha**：`c4d6b10`（本轮起始 `17aeae2` 的父提交）

处理云端 `2026-09-30_04-13-31_JST.md` 的 `044`（四态证据合同）、`043`（分母）、正文 §3 放大器。
提交 `21ed0ac`；全量 2183 passed；验牙 5/5 behavioral RED。

---

## 最新云端审计：2026-09-30 04:13:31 JST（本次为补传）

**完整报告**：[2026-09-30_04-13-31_JST.md](./2026-09-30_04-13-31_JST.md)  
**Agent 任务书**：[2026-09-30_04-13-31_JST_AGENT_TASK.md](./2026-09-30_04-13-31_JST_AGENT_TASK.md)  
**原审计时间**：`2026-09-30T04:13:31+09:00`  
**补传登记时间**：`2026-09-30T11:38:58+09:00`  
**reviewed_source_sha**：`55dcce889411af22acee906d4aac8ce08bd8a31b`  
**补传前 main HEAD**：`55dcce889411af22acee906d4aac8ce08bd8a31b`  
**candidate_diff_hash**：`efe165db969c04224aef162e7ae991c5b357707ced0b9971ee48d9f557c29cdb`  
**主实验**：`EXP-IT-RAW-PRESENCE-ROUTE-SCOPE-043`

范围：Engine 正常/空轮 raw-presence、route 请求范围、live_session/soak 证据等级及分母、相关测试和默认/live 配置。本文所称已读、运行及问题状态均属于原报告时点；补传不构成新的源码审计。

开放 ID：`IT-P1-RAW-PRESENCE-ROUTE-SCOPE-FALSE-GRADE-044`、`IT-P2-SOAK-RAW-PRESENCE-ROUND-DENOMINATOR-043`；原报告另保留 041/042/039/038/037 开放队列，未因本次文档提交而关闭。

> ✅ **2026-09-30 12:30 JST 本地更新**：**044 与 043 以及正文 §3 的放大器，均已确认成立并修复**
> （见下方本轮报告）。修复提交 `17aeae2`；验牙 5/5 behavioral RED；
> 全量 2183 passed。云端列出的"下一轮需核验"项中，
> `route_scope_cases.json` 与 RED/GREEN/ROLLBACK 日志已在
> `evidence_2026-09-30_12-25-16_JST/` 交付（文件名略有不同：
> `route_scope_cases.json`、`rollback_teeth_red.log`、`four_state_contract.log`）；
> `RUN_MANIFEST.json` 未生成；正文 §5.2/§5.3 仅**部分**落地（如实标注）。
> 041/042/039/038/037 **仍未动**。

优先顺序：`044+043 -> 041 -> 042 -> current-HEAD full pytest / rollback -> 039 -> corrected 038/037 -> source/current/SSE -> research`。

下一轮需核验：044/043 RED-GREEN-ROLLBACK 日志、`044_route_scope_cases.json`、`raw_presence_reconcile.json`、定向与全量 pytest/selftest 日志、候选 diff/hash、`RUN_MANIFEST.json`、`NEXT_STEPS.md`。

证据边界：原报告的 `12 passed in 0.06s` 仅为隔离候选测试；`2060 passed` 属于历史产品归档，未在上述 SHA 复验。本次仅补传 Markdown，没有产品修复、部署、训练、真实行情轮询或通知，也未启动用户本地 agent。报告第 10 节的发布失败记录保留为历史。实际报告提交 SHA 由 Git 提交历史提供，不预填未知值。

最新本地独立审计现为 [2026-09-30_12-30-00_JST.md](./2026-09-30_12-30-00_JST.md)；该轮**已处理 044/043**。以下完整保留补传前索引；其中“最新”“本轮”等词均为历史记录。

---

**最新本地独立审计**：[2026-09-30_12-30-00_JST.md](./2026-09-30_12-30-00_JST.md)  
**上一版本地独立审计**：[2026-09-29_22-45-00_JST.md](./2026-09-29_22-45-00_JST.md)  
**最新云端独立审计**：[2026-09-30_04-13-31_JST.md](./2026-09-30_04-13-31_JST.md)（补传，见顶部；上一份 [2026-09-24_20-08-07_JST.md](./2026-09-24_20-08-07_JST.md)）  
**最新云端 Agent 任务书**：[2026-09-30_04-13-31_JST_AGENT_TASK.md](./2026-09-30_04-13-31_JST_AGENT_TASK.md)  
**reviewed_source_sha / 本轮固定产品代码**：`c4d6b10d53b256594b6cfae14d683532db20bcf1`  
**audit_start_head / 本轮开始 main**：`c4d6b10d53b256594b6cfae14d683532db20bcf1`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/c4d6b10d53b256594b6cfae14d683532db20bcf1/docs/audits/intraday/LATEST.md

> 版本纪律：本轮 `git fetch` 后 `origin/main == c4d6b10`（我自己上一轮的收尾提交）。
> **云端 `2026-09-30_04-13-31_JST.md` 审的是 `55dcce8`** —— 即我上一轮的
> **中间产物**，其开放 ID 044/043 **本轮已全部处理完毕**。

## 2026-09-30 12:30:00 JST（本地）

**本轮处理云端补传报告，修掉 3 个我上一轮漏掉的真缺陷：**

1. ✅ `IT-P1-RAW-PRESENCE-ROUTE-SCOPE-FALSE-GRADE-044` ——
   **一个 bool 同时承担四种状态**（有证据精确／legacy 投影／**根本没请求**／
   **请求了但失败**）。三种反例全部真实复现：
   - **false downgrade**：未请求 index 仍导出 `index=False`，
     让**没 dispatch** 的 route 反过来给 soak 制造黄色证据等级；
   - `index` 请求失败（`outcome=None`）被说成 projected；
   - **false upgrade（最危险）**：空轮分支硬编码 `{stocks:True,index:True}`，
     **完全不看 outcome** —— 指数失败被粉饰成 `exact`。
     （`stk_outcome`/`idx_outcome` **确实在作用域内**，所以这是**疏漏**，
     不是设计权衡。）
2. ✅ 云端 §3 **放大器**：route-ledger 为 schema 稳定保留零值键，
   消费端只 union keys，于是 `index:0` 被当成"出过数"，
   **伪造 route 活动并触发假 partial**（`partial` 是唯一判 fail 的分支）。
3. ✅ `IT-P2-SOAK-RAW-PRESENCE-ROUND-DENOMINATOR-043` ——
   `_safe_int(dict)` 恒得 **0**，文案出现 `投影 1/0 轮`；
   修后实测 **`1/1 轮`**。

**修法**：新增 `Engine._route_raw_presence`（`engine.py:1598`）作为
**唯一事实来源**，正常路径与空轮分支**共用**（bug 类 b）；
合同为「未请求／无 outcome → **省略键**」，由下游判 `not_measured`。
新增 `_positive_activity()`（**fail-closed**）：键存在 != 有活动。

- 全量 **2164 → 2183 passed**（+19），零回归
- 验牙 **5/5 behavioral RED**，全部**字节级还原**
  （`engine.py 9176467` / `live_session.py e48b30b`）
- 门禁：`check_*.py` 8 ok / 0 fail；`selftest` 0；`dash_render_check` 0
- `session.py` **字节等于 HEAD**（`fd74113269abd83e`，未触碰）
- 证据目录：`evidence_2026-09-30_12-25-16_JST/`
- **⚠ 本轮最该记录的事**：我**连续第三轮被同一类错误咬到**，
  而这次咬我的是**我自己上一轮的产出** ——
  R14 加字段没加读者，R15 加读者却没审**字段本身的表达能力**。
  本项目 4 次命中"数据算了但判决层零读者"，是同一根因的四个面：
  **观测字段的"消费者契约"从未被显式定义**。
- **诚实标注**：云端 §5.2（`index_requested` 带进轮样本）**未做**；
  §5.3 三集合分离**仅部分**；041/042/039/038/037 **未动**；
  **模型真正增量提升 = 0**；真实 Precision/Recall/漏事件率/收益 `unavailable`

## 2026-09-29 22:45:00 JST（本地）

**本轮完成 4 项 + 1 项诚实降级：**

1. ✅ `tests/test_full_day_simulation.py` —— **挂了 10+ 轮的未决项已交付**
   （**33 条**）。首次把**一整天**盯盘流程（时段序列含午休/收盘竞价、
   告警冷却 fire→suppress→refire、盘中 failover、确定性）钉成可回归测试。
2. ✅ `SourceManager` failover/记账**两份手写 → 单点 `_serve`**（bug 类 b）。
   **AST 独立核实**：记账语句只在 `_serve` 里存在一份，
   `call`/`call_detailed` 各 **0** 条，纯委派。
3. ✅ `IT-P1-SOAK-RAW-PRESENCE-GRADE-GATE-002` —— 证据等级**算了但判决层零读者**
   （bug 类 c，**我上一轮自己犯的**）。已接入 `evaluate_health`。
4. ✅ `IT-P2-CALENDAR-BARE-CONSTRUCT-SILENT-EMPTY-HOLIDAYS-003` ——
   **裸构造 `TradingCalendar()` 静默得到空假期表**，把国庆当交易日。
   **潜在陷阱非生产缺陷**（生产全用 `load()`）。已钉住 + AST 门禁。
5. ⬇️ **腾讯指数角色修复诚实降级为"结构对齐，非行为修复"** ——
   回滚牙齿有 **2 颗没转红**，证明 `idx_set` 只是预过滤、成员仍须过
   `looks_like_index`。我把结论写进测试文件头，并加测试防后人误以为
   它修好了 `sh000922` 那一类符号。

**⚠ 本轮最重要的事件**：我**拦下了一次自己人的回归** ——
一个子 agent 把 `session.py` 的 `LUNCH`/`CLOSE_AUCTION` 两个时段**删掉了**
（会让已修的 `IT-P0-001` 重新打开）。我立即中断并要求还原，
**独立核实**还原后字节等于 HEAD，并用隔离验证证明
那些 session 失败来自**另一个** agent 在写的 `engine.py`，不是它。

- 全量测试 **2002 → 2164 passed**（+162），零回归
- 回滚牙齿：腾讯 **4/4 behavioral**；failover **3 形状 structural + 3 定向变异 behavioral**；
  全日仿真 **2/2 behavioral**；全部字节级还原
- 门禁：`check_*.py` 8 ok / 0 fail；`selftest` exit 0；`dash_render_check` exit 0
- 证据目录：`evidence_2026-09-29_22-44-52_JST/`
- **模型真正增量提升 = 0**；真实 Precision/Recall/漏事件率/收益仍 `unavailable`
- **诚实标记**：`stale_hard_rejected` 生产恒 0；
  **收盘竞价静默靠规则层无引擎级兜底**（`engine.py:1910` vs `tick_surge.py:101`
  同一事实两个判据）；`spirit_index` 仍默认关闭，真实 soak 未运行

## 2026-09-29 22:00:00 JST（本地）

**本轮结论**：挖出并修掉 **3 个真实缺陷**：

1. `IT-P1-ADMIT-REASON-STALE-SILENT-DROP-016` — `AdmitReason.stale` 拒绝
   **静默丢失 route 归属**（bug 类 d 假绿出口，**已在树里**，且被两个既有
   测试覆盖）。**更正云端任务书**："`stale` 不可达"是错的。
2. `IT-P1-SOAK-RAW-PRESENCE-GRADE-BLIND-001` — `live_session` 把**投影反推**
   的 missing/quality 当**精确事实**上报（上一轮我加了字段却零读者）。
   已改为三态 `exact`/`mixed`/`not_measured`，**键缺失不得当成 exact**。
3. `IT-P1-006-R1-R2` — Eastmoney 无 total 分支 `truncated` **恒 False**
   （诊断说谎；安全性未漏，恒 fail-closed）。

**独立复核并把一个"疑似缺陷"降级**：`call` 与 `call_detailed` 两份
failover 逻辑经 4 条路径探针实测**行为完全一致** —— 是潜在维护风险，
**不是**已确认缺陷。

- 全量测试 **2002 → 2060 passed**（+58），零回归
- 回滚牙齿：Eastmoney **2/2 behavioral RED**；AdmitReason 给了
  **structural + behavioral 双证**（诚实标注 structural 是较弱证据）
- 门禁：`check_*.py` 8 ok / 0 fail；`selftest` exit 0；`dash_render_check` exit 0
- 证据目录：`evidence_2026-09-29_21-59-53_JST/`
- **模型真正增量提升 = 0**；真实 Precision/Recall/漏事件率/收益仍 `unavailable`
- **诚实标记**：`stale_hard_rejected` 在生产**恒为 0**（三源 `freshness_allowed=false`），
  是 capability-ready 遥测，**不是会动的曲线**
- **用户获得感提示**：`spirit_index` 仍默认关闭，非交易时段，WP09 soak 未运行

## 2026-09-29 17:00:00 JST（本地）

**本轮结论**：云端 20:08 的 `IT-P1-SINA-INDEX-QUOTE-ID-COLLIDES-WITH-STOCK-013`
**本地真实代码复现确认成立，已修复**。指数 `Quote.code` 由 `000001` → `sh000001`，
`board` 由 `Board.MAIN` → `Board.INDEX`。连带修掉云端同轮 #2 幻影 missing、
#3 盲测试；完成任务书 WP01–WP07（含生产 `call_detailed` 调用点 **0 → 2**）。

- 全量测试 **1957 → 2002 passed**（新增 45 条 / 4 个测试文件）
- 回滚牙齿 **7/7 behavioral RED，0 structural**，3 个产品文件 sha256 字节级还原
- 门禁：`check_*.py` 8 ok / 0 fail；`selftest` exit 0（68 告警 / 6 类型）；`dash_render_check` exit 0
- 证据目录：`evidence_2026-09-29_16-58-26_JST/`
- **模型真正增量提升 = 0**；真实 Precision/Recall/漏事件率/收益仍 `unavailable`（未编造）
- **用户获得感提示**：`spirit_index` 仍默认关闭，且本轮为**非交易时段**，
  WP09 实盘 soak 未运行 —— 单元级证据 ≠ 盘中真实预警。

## 2026-09-24 20:08:07 JST

主实验：`EXP-IT-SINA-INDEX-END2END-018`

### 本轮最高优先新发现

1. `IT-P1-SINA-INDEX-QUOTE-ID-COLLIDES-WITH-STOCK-013`：上一轮已经把 `sh000001` 的 HTTP wire 修正确，但 Sina parser 仍把 index `Quote.code` 压成裸 `000001`。因此上证指数仍可与平安银行共享 `EngineState.quotes/history/first_seen/last_price/day_open` 的键；`SpiritIndex` 又按 `q.code` 读窗口，存在继承股票历史后构造巨大假指数拉升/打压的确定性机制反例。默认 `spirit_index=false`，故当前标 latent P1。
2. `IT-P2-SINA-INDEX-OBSERVATION-PHANTOM-MISSING-014`：当前 RoundObservation 请求轴使用配置中的 `sh000001`，而 Sina raw/admitted Quote 轴是 `000001`，所以一轮可以同时出现 `index returned/admitted=1` 与 `unknown_missing=sh000001`。
3. `IT-P2-SINA-DETAILED-PREFIX-TEST-BLIND-015`：现有测试名为 `test_detailed_identity_axis_keeps_index_prefix`，但只断言 requested/returned/admitted 数量与无 missing，没有断言 `requested_keys/raw_returned_requested_keys/admitted_keys/Quote.code` 真正保留 `sh000001`；当前 bare R/P/Q 实现因此可以通过。

### 已修项不重开

- `9670dd9`：Sina 显式 index prefix 的 HTTP wire/filter 错证券问题已修；本地产品报告归档 `1957 passed`。
- `3885ebb`：route-local `StateUpdateResult`、route-key 时间诊断 map、Eastmoney no-total pagination 已修。

### 下一轮依赖

`Sina index Quote/detailed canonical identity -> Tencent/Sina cross-source identity -> EngineState/SpiritIndex collision regression -> RoundObservation identity -> production call_detailed -> per-route health/provenance -> Membership/soft-empty -> live soak -> research`。

模型训练：0；真实 Precision/Recall/漏事件率/交易收益仍 unavailable。
