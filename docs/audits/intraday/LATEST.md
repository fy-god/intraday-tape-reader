# 最新审计

**⚠ 新增回归 P0（2026-09-23 18:17:39 JST）**：[`2026-09-23_18-17-39_JST.md`](./2026-09-23_18-17-39_JST.md)  
**发现**：`IT-P1-REFRESH-FAILED-POOL-RETAINED-READ-AS-MEASURED-ZERO-001`（**假红回归**）  
**审计时远端 main**：`788810084cd7ca4853707c02be4bc3058e2e390a`  
**report_commit_sha**：`c06b21bd9a921caebdb5eb1ab6147122eb6b9f97`  

  > `tools/live_session.py:3263` `int(engine.refresh_universe() or 0)`：**AST 实测生产者 `engine.py:1177-1334` 共 4 个 `return`、无 `return None`**，其中 `:1302`（`rejected_smaller`）与 `:1326`（`all_failed`/`empty`）**均保留现有池子后 `return 0`**。
  > **双臂端到端对照**（同一构造：30 轮、每轮扫 5563 只、`universe_size=0`）：
  > - OLD `d8627633`（blob `eb12ef35…`）→ `ok`、**`healthy=True`、`exit=0`、`fail=[]`**
  > - NEW `9ec40c67`（blob `b2f791f0…`）→ **`fail`、`healthy=False`、`exit=1`**，打印「扫描池为 **0 只**（明确测到，非未采集）—— 本场**一只票都没扫**」
  > 而会话实测 `universe_abs_min=5563`、`universe_abs_measured_rounds=30` —— **有 30 轮真实证据却判「一只票都没扫」**。
  > **三态对照**证明消费者能区分：同一会话 `universe_size=None` → **`ok`/`exit=0`**（诚实）；无会话证据 + `0` → 判红（**正确**）。**坏在生产者把信息丢了。**
  > **无测试覆盖**：`:3263` 在 `def run`（`:3204-3485`）内，全 `tests/` 对 live_session `run` 入口点命中 **0** → 在 `1830 passed / exit 0` 下存活。
  > §1 **交叉裁定**：子 agent C 与 A 对「5 个死字段接线」互相矛盾，我实测 5×扰动**均不改变任何 level / healthy / exit** → **A 对**，C 把 detail 文本变化当成了判决变化。
  > 详见 [`2026-09-23_18-17-39_JST.md`](./2026-09-23_18-17-39_JST.md)。

---

**修复后回归验证（2026-09-23 18:13:57 JST）**：[`2026-09-23_18-13-57_JST.md`](./2026-09-23_18-13-57_JST.md)  
**验证对象**：并发 agent 在工作树里（未提交）对 `2026-09-23_17-58-08_JST.md` §2.1/§2.2 的修复  
**本轮新增产品提交**：`9ed75efb50c41f9756c3fab2a0255685aea73868`（WP01 Snapshot Outcome Contract v4）  
**验证时远端 main**：`a644a7c2d400af824e4545d7b52e2fc3439d3953`  
**report_commit_sha**：`48a34d7803e0322807f87c1637eebe5494f7059a`  

  > **三重对照实测**（把我 §2.1 的建议变成可证伪的检查）：旧「单次采样」形式 + 注入 1 次 250ms 停顿 → **失败**（实测 250.5ms）；新「中位」形式 + **同一次**停顿 → **通过**（实测 0.1ms）；新「中位」形式 + **本来就慢**的 300ms 函数 → **仍然失败**（实测 300.3ms）→ 证明**断言未被解除武装**。
  > **我新测出的边界**：`median_elapsed(repeat=7)` 容忍 **≤3 次**停顿、**≥4 次**失败（它是「多数样本正常」的保证，不是「任意扰动都不翻转」）。
  > **§2**：我更正里的**两条假绿在新提交 `9ed75ef` 上逐字存活**（payload 逐字匹配 7 处；新增的 `tests/test_snapshot_outcome_contract.py` 对它们**零覆盖**）。
  > **§4**：并发 agent 对 §2.2 的修法（`dir(ls)` 动态枚举 `SCOPE_*` 并断言两两互异）**优于**我的原建议（只把 4 改成 5 会把同一个硬编码问题推后一轮）。

---

**⚠ 更正（2026-09-23 18:07:42 JST）**：[`2026-09-23_18-07-42_JST.md`](./2026-09-23_18-07-42_JST.md)  
**更正对象**：[`2026-09-23_17-58-08_JST.md`](./2026-09-23_17-58-08_JST.md)（同一轮 h=16）  
**更正时远端 main**：`9ed75efb50c41f9756c3fab2a0255685aea73868`  
**report_commit_sha**：`3cdb63658797f11443ef1e87794b915b6427b206`  

  > **我上一份报告把 scope 轴的成立外推成了整条合同类的闭合，现撤回该外推。** v2 合同在**它点名的 scope 轴为真**（20 组输入实测：分母未知时零组判 `full_market`），但**同一量词/分母缺陷类在两条相邻轴存活**，两者均 `healthy=True`/`exit_code=0`/`fail=[]`：
  > - `tools/live_session.py:2298-2300` `universe_transport`：门是**存在量词**（`_tmeas>0`）、分母是**已测子集**，29 轮无证据 + 1 轮完整 → 实际打印「**会话期间 1 轮 transport 均为完整（无截断）**」。
  > - `tools/live_session.py:2355-2358` `universe_freshness`：`:737 freshness_measured_rounds = sum(states.values())` **数记录不数证据**，且 `:625 _order` 让 `fresh(1)` 压过 `unknown(0)` → 29 unknown + 1 fresh → 实际打印「**会话期间股票池新鲜（30/30 轮，{'unknown': 29, 'fresh': 1}）**」。
  > - §2 **收紧**上一份报告的「死字段 5 → 0」：点名的 5 个字段确已接线，但**模块级死字段不为 0**（我实测 v1 14 → HEAD 10，delta 4；零字段失去读者）。
  > - §3 `cov_ok` **从未被任何调用方传**，全市场线恒为 `0.95`（P2）。
  > 复现用**仓库自己的** `_base()`/`_row_scope()` 夹具（基线已断言 `healthy=True`），阳性/阴性对照齐备：见 [`2026-09-23_18-07-42_JST.md`](./2026-09-23_18-07-42_JST.md)。

---

**本轮（2026-09-23 17:58:08 JST）盘中预警独立审计**：[`2026-09-23_17-58-08_JST.md`](./2026-09-23_17-58-08_JST.md)  
**本轮 reviewed_source_sha（产品提交）**：`9ec40c6740abdf9f480f4219490a891dcf7c0a11`  
**本轮审计开始 docs HEAD**：`44876c1e2824c131b89f1b0575ebb84008de60b3`  
**report_commit_sha**：`527f48d716a2bffd5d7023e29d4893906cdc0808`  

  > 本轮要点：**在上一轮自己指的提交上重跑 5 次全量 → `1802 passed` ×5（exit 0），该「随机门」本轮未复现，予降级**；但实测其**机制说错**（断言余量实为 **6.34x**，非「7.2% 薄余量」），且**墙钟上界经 AST 枚举实为 3 处**（非 1 处）。宇宙证据合同 v2 的核心修复**经 20 组输入独立验证为真**（分母未知时**零组**判 `full_market`）。`IT-P0-002` **须拆分**：核心已修，**时区半仍为死字段且零覆盖**。
  > 详见 [`2026-09-23_17-58-08_JST.md`](./2026-09-23_17-58-08_JST.md)；`bf0b83bf..9ec40c67` 的历史索引指针全部保留于本页下方。

---

**最新本地 Agent 产品轮**：[`2026-09-23_16-25-42_JST.md`](./2026-09-23_16-25-42_JST.md)  
**最新云端独立审计**：[`2026-09-23_16-11-07_JST.md`](./2026-09-23_16-11-07_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-23_16-11-07_JST_AGENT_TASK.md`](./2026-09-23_16-11-07_JST_AGENT_TASK.md)  
**最新独立实测审计**：[`2026-09-23_15-52-52_JST.md`](./2026-09-23_15-52-52_JST.md)  
**最新产品提交 / 新的被审候选**：`9ec40c6740abdf9f480f4219490a891dcf7c0a11`  
**上一轮被审产品 SHA（reviewed_source_sha）**：`bf0b83bf212f169c720ca8a1c5108405c18a9712`  
**本轮审计开始 docs HEAD**：`37988fe8294a034340546dfc55838e2af2bf9885`  
**report_commit_sha**：`8f517cb9b2fec2c33fa7c8f87819b05db5440998`  
**agent_task_commit_sha**：`0b6aa866b6df08d224f1343bbd5ccc7ef14b92c5`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/37988fe8294a034340546dfc55838e2af2bf9885/docs/audits/intraday/LATEST.md

> `bf0b83b..37988fe` 全部为 `docs/audits/intraday/` 文档；历史报告未删除。本文件只移动当前接续指针。

## 2026-09-23 16:25:42 JST — 本地 Agent 产品轮

**产品提交 `9ec40c6`**：Universe Evidence Completeness Contract v2
（`tools/live_session.py` +474/−98；测试 38 → 65）。修掉 09-23 审计轮指认的 **7 项**，
**其中 1 项是我上一轮 `bf0b83b` 自己留下的残余假绿**。

根因（00:12 §7）：**肯定结论的量词和实际证据覆盖范围不一致。**
我 21:00 提的"肯定句加 `_measured` 前置条件"**对本案无效**（22:15 ADDENDUM2 §2 已指出，我复核成立）：
门控是**存在量词**，守护的文案是**全称量词** —— 加了也还是假绿。
正确门是**肯定句里那个计数的分母必须是会话总轮数**。

| 项 | 结果 |
|---|---|
| `IT-P2-UNIVERSE-SCOPE-POSITIVE-USES-EVIDENCED-SUBSET-AS-DENOMINATOR-001` | ✅ 实测「全程全市场扫描，**1/1** 轮」（30 轮会话）→ 修 |
| `IT-P2-UNIVERSE-FRESHNESS-POSITIVE-USES-EVIDENCED-SUBSET-001` | ✅ 实测 1/30 fresh 说成整场新鲜 → 修 |
| `IT-P1-UNIVERSE-T0-MEASURED-ZERO-READ-AS-MISSING-001` | ✅ 实测 `None`/`0` 都判 ok 全绿 → 三态拆分（**生产者源头一并修**） |
| `IT-P1-UNIVERSE-ABS-SIZE-SESSION-BLIND-001` | ✅ 会话 min=2000 零消费者 → `min(t0, session)` |
| `IT-P1-UNIVERSE-COVERAGE-SESSION-WITHOUT-T0-001` | ✅ 整块搬出 t0 条件 |
| `IT-P2-UNIVERSE-FULL-MARKET-IS-MAGNITUDE-BLIND-001` | ✅ 新增第五态 `broad_scan_unquantified` |
| 死字段 5 个（21:40 说 3，22:45 更正为 5） | ✅ 接进判决（AST 复查 **5 → 0**） |

**证据**：全量 pytest **1830 passed**（修前 1802）；`check_*.py` 8/8；
`dash_render_check` exit 0；`selftest` exit 0；
**行为性 RED 20 项 / 结构性 ERROR 0**（pre-fix blob hash 逐字节核实 `eb12ef35…`）；
rollback 后重新 65 passed。
日志：`evidence_2026-09-23_16-19-24_JST/`。

**任务定义调整 4 项**（夹具/骨架沿用旧两态约定，**不是产品缺陷**）：
骨架 `universe_size: 0` → `None`；`test_universe_not_measured_charges_nothing` 拆出
`test_universe_measured_zero_is_red`；`_sample` 的 `universe=100` 桩值 → `5000`。

**模型真正增量提升 = 0**。真实 Precision / Recall / 漏报率 / 交易收益仍为 `unavailable`，
本轮**不编造任何这类数字**。真实盘中 soak、浏览器核对、模型训练：全部 `not_run`。

**`IT-P1-UNIVERSE-MEMBERSHIP-QUALITY-001` 连续第 5 轮未修** —— 云端明确
「Snapshot Outcome 必须跨 source 一致以后，Membership 才允许进入产品」；
Quote-only 路径下提前改 membership 会让账本桶**随服务源变化**（用一个真缺陷换一个新的 source-dependent 假绿）。
**下一轮第一优先 = WP01 SnapshotFetchResult v4.1（三源同时实现）。**

## 2026-09-23 16:11:07 JST

主实验：`EXP-IT-ROUND-ATTRIBUTION-011`

### 15:52 独立实测接续

- `IT-P1-SUITE-TIMING-ASSERTION-NONDETERMINISTIC-001`：同一源码树完整 pytest 3 次出现 **1 红 / 2 绿**；唯一失败是 `test_performance_1000_stocks` 的 one-shot `elapsed < 0.2`（214.3ms）。默认 correctness gate 需要与 performance benchmark 分离。
- `IT-P2-EMPTY-ROUND-LEDGER-IDENTITY-UNATTRIBUTED-001`：空轮已可观测，但 `requested>0 / returned=admitted=0` 时没有 terminal attribution，独立复现示例 `5913 != 0`。
- Eastmoney unknown-total pagination 已有真实 scratch RED/GREEN；应固化成仓库测试并落小 patch。

### 本轮主裁决

**不能用 `quotes==[] => 全部 unknown_missing` 修空轮 ledger。** 空 Quote list 可能是：
1. provider raw 真空；
2. raw rows 全部在 parser 因质量被丢；
3. legacy source 根本没有 raw-presence 证据。

主改造：**Round Attribution Contract v1 / Snapshot Outcome v4.1**：新增 `raw_presence_known` 与 `unattributed_requested`。只有 exact raw absence 才叫 `unknown_missing`。

### 当前优先顺序

1. Tencent/Sina/Eastmoney 三源 `SnapshotFetchResult` detailed adapter；
2. Round attribution 参数化不变量（含 empty/all-quality/legacy-unknown）；
3. live-session `unattributed_total/unattributed_rounds`；
4. Eastmoney pagination + pages_requested 小修；
5. 默认 pytest 去掉 tight wall-clock correctness gate，性能另跑 benchmark；
6. 然后 Membership 5923/5553 → SourceManager exact soft-empty → timezone/window/provenance → research。

### 下一轮关键产物

- `snapshot_outcome_cases.json`
- `round_attribution_cases.json`
- `pagination_cases.json`
- `correctness_suite_3x.log`
- `performance_benchmark.log`
- `full_pytest.log`
- `check_*.py` logs
- `RUN_MANIFEST.json`
- `NEXT_STEPS.md`

模型训练：0；真实 Precision/Recall/漏事件率/收益 `unavailable`。
