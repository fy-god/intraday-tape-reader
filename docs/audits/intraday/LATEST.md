# 最新审计

**最新本地 Agent 产品轮**：[`2026-09-22_21-00-00_JST.md`](./2026-09-22_21-00-00_JST.md)  
**最新云端独立审计**：[`2026-09-22_20-04-38_JST.md`](./2026-09-22_20-04-38_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-22_20-04-38_JST_AGENT_TASK.md`](./2026-09-22_20-04-38_JST_AGENT_TASK.md)  
**最新本地审计追加-2**：[`2026-09-22_16-20-44_JST_ADDENDUM2.md`](./2026-09-22_16-20-44_JST_ADDENDUM2.md)  
**最新产品提交 / reviewed_source_sha**：`6c8f11e363b3efcd21e410a5af7a718e08e8564d`  
**本轮审计开始 docs HEAD**：`0816294d6b38ff1a788acd262c3734ecace6ae71`  
**report_commit_sha**：`ba52e1080b9b0e6fc352ed9bf54c537dd9f396d4`  
**agent_task_commit_sha**：`359b781a5658590aba604302dbc185445e8314cb`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/7b0404a8e8d1a3ea56b1498ad8650b2bfa75acae/docs/audits/intraday/LATEST.md

> 历史报告文件没有删除或覆盖；上一版完整长索引保留在不可变 commit。
> 重要更正：上一版仍把 `c4f2ce1...` 写作最新产品；`6c8f11e...` 是其后的真实产品修复提交，
> 而 `6c8f11e...0816294` 只有 docs(audit) 变化。

---

## 2026-09-22 22:15 JST 追加 2（本线）：同一类假绿**不止一处** —— `universe_freshness` 同型；
且作者自提的 `_measured` 门禁**抓不到它**

[2026-09-22_22-15-00_JST_ADDENDUM2.md](./2026-09-22_22-15-00_JST_ADDENDUM2.md)

- `reviewed_source_sha` = `ba025ad3737ef97a9b0c9816b02b778e41f7b569`；父报告 = `99934d61`、`ba025ad3`。
- **升级为『一类缺陷』**：21:40 报的 `universe_scope`（`:1737`）**不是孤例**。
同型第二处在 `universe_freshness` `live_session.py:2049` + `:2073-2075`：
30 轮中**只有 1 轮**带新鲜度键时，实测仍输出 **ok /「会话期间股票池新鲜（{'fresh': 1}）」**
（15/30 -> `{'fresh': 15}`）。**修前修后输出完全相同** ⇒ **不是 `bf0b83b` 引入的回归**，
是更早就存在的同类假绿。
- **本追加最有价值的一点**：作者在 21:00 报告 `:353-359` 提出的补救是
「凡肯定句都必须有显式 `_measured` 前置条件」，并称『当场抓到 2 条』。
**该办法对本案无效** —— `universe_freshness`(`:2049`) 与 `universe_scope`(`:1737`)
**都已经有** `_measured` 门控；病根是**存在量词的门控守护了全称量词的文案**，
所以按『缺门控』去扫**必然系统性漏掉这一类**。建议门禁改为校验
「肯定句引用的计数，其**分母是否为会话总轮数** `len(rows)`」而非 `sum(counts)`。
- **我撤回/降级的部分**：`universe_coverage:1894` 的「active 0/0 = 100.00%」**可达性 0**
（真实生产者 10 组 meta 穷举，数值 coverage 且分母 0 的 **0** 组）⇒ 只作潜在风险；
「修复造出新假红」二次确认**不成立**（真实冷启动 30 轮是真故障被正确抓住）。
- 测试真数：上一轮纯净树 `bf0b83b` = **1802 passed**，退出码 **0**（本条未新增回归）。
- 本仓库**无** `docs/audits/validate_latest.py`，校验器步骤不适用，不声称 gate PASS。

## 2026-09-22 21:55 JST 追加（本线）：回归核对表 —— **3 条已修、4 条仍开、1 条不存在**，轮转基线已过期

[2026-09-22_21-55-00_JST_ADDENDUM.md](./2026-09-22_21-55-00_JST_ADDENDUM.md)

- `reviewed_source_sha` = `99934d619cfb30e0ac4aa90f59393c39ab62b4f6`；父报告 = `docs/audits/intraday/2026-09-22_21-40-00_JST.md`。
- **基线更正（8 条逐条重取真值并独立复核）**：
  - **已修、勿再回归（3）**：`IT-P0-001`（`session.py:46/87/156/159`，实测 14:57–15:00 = `close_auction` / `is_open=False` / `observable=True`）、`IT-P1-LIMIT-001`（`limit_board.py:210` 判定前移，实测 100 万→`at_limit_unqualified`、300 万→`sealed`）、`IT-P2-OBS-001`（`live_session.py:247/2883`，实测累计 5000 vs 本轮 450）。
  - **仍开放（4）**：`IT-P0-002` 时区（`session.py:100` `_now_fn` 死参数，全树 `ZoneInfo|pytz|utcnow` **0** 命中，裸 `datetime.now()` **6** 处）、`IT-P1-SOURCE-EMPTY-001`（`engine.py:547-582` 仍以『未抛异常』为成功判据，实测全空时备用源调用 **0** 次）、`IT-P1-WINDOW-001`（`engine.py:313` change-only 追加；`ObservationInterval` 全树 85 命中、`src/tests/tools` **0**）、`active_snapshot['epoch']` 占位（`engine.py:1049` 是**读者**，**写者 0**；对照 `_universe_refresh_id` 有写者 `:758`）。
  - **不存在**：`tests/test_full_day_simulation.py` 两树皆无（`git ls-files`/`git log --all` 无记录）—— 属**待新增**，**不得再当回归项**。
- **降级标注**：`epoch` 占位**并非本线首报** —— 生产者自己的报告/计划已写明（`13-00-00:204,297`、`17-00-00:398`、`21-00-00:398`、`NEXT_STEPS:363`），本线只做独立确认。
- 三条『已修』是**产品线自己的提交**修的，**不是本线**的代码贡献（本线只核对与更正）。
- 手册 §4 基线本次**未修改**（硬约束：不创建/修改任何定时任务与排程），更正结论只写入本文档供人工同步。
- 本轮 4 个只读子 agent 中 **2 个因 PowerShell 不支持 heredoc 失败**，已修正提示词重派；回归表结论**逐条经主 agent 复核**后才写入。

## 2026-09-22 21:40 JST 轮审（独立审计线）：`bf0b83b` 三项假绿修复**确认为真**，但肯定句留下同类残余假绿

[2026-09-22_21-40-00_JST.md](./2026-09-22_21-40-00_JST.md)

- `reviewed_source_sha`（产品源码）= **`bf0b83bf212f169c720ca8a1c5108405c18a9712`**；`main_head_at_audit_start` = `bf0b83bf212f169c720ca8a1c5108405c18a9712`。
- **独立确认**：修前 `7b0404a8e8d1a3ea56b1498ad8650b2bfa75acae` 与修后 `bf0b83bf212f169c720ca8a1c5108405c18a9712` **双树同输入对照**，四项全部可判别 —— 零证据、空扫描、会话内覆盖下降、t0 `transport_complete=False` 被会话盖掉，**均为真修**；并复现了作者自述的『一个单位就翻绿』（`rounds_transport_measured` 0→1 即 fail→ok）。
- **新发现 P1（未修）**：肯定分支的分母是**只含带证据轮次的子集**（`live_session.py:1737` 把 `full_market_rounds` 与 `scope_measured_rounds` 相比，而 `:609` 明确老轮样本无该键则**不计入**，`:663` 仅对带键轮次求和）。实测：30 轮中**只有 1 轮**有扫描范围证据时，修后仍输出 **「全程全市场扫描，1/1 轮有明确扫描范围证据」**（5/30→5/5、15/30→15/15 同理）。会话总数就在同一聚合里（`:687` `rounds=len(rows)`）却**从不被该分支读取**（全树 `_sess.get('rounds')` 出现 **0** 次）。
- 该实现**低于作者自己的合同**（其 21:00 报告 §1.1 写「每一轮都有明确扫描范围证据」才允许肯定句）。
- **新发现 P3**：`coverage_active_denominator_kinds`（`:679`）、`coverage_active_p05`（`:676`）、`coverage_active_last`（`:678`）三字段**写后零读者**。
- **我自己否证并撤回两条假设**：①『修复造出新假红』 —— 7 份真实归档休市段 `universe.min` 为 5563（非 0），且 `engine.py:1203` 明确失败时**保留**原池绝不清空，故空扫描硬 fail 打中的是真故障；②『会话覆盖聚合忽略分母种类』 —— 机制属实但**可达性 0**（45 组 meta 穷举中数值 coverage 18 组，分母非 provider 的 0 组），仅作死字段上报。
- 测试真数：`pytest -o addopts= -ra` 于纯净树 `bf0b83bf212f169c720ca8a1c5108405c18a9712` = **1802 passed in 120.05s**，退出码 **0**（独立子 agent 复跑 1802/121.29s）。
- 本仓库**无** `docs/audits/validate_latest.py`，校验器步骤不适用，不声称 gate PASS。

## 2026-09-22 21:00 JST 本地 Agent 产品轮（**我 17:00 轮的"会话优先"造出了两个新假绿**）

- 起点 HEAD `6c8f11e` → pull 到 `7b0404a`（只见审计文档）；
  `reviewed_source_sha` = **`6c8f11e`**，即**我 17:00 轮推的那个提交**。
- 归档机器证据：**1802 passed in 110.25s**（上轮 1791 → **+11**）；
  `check_*.py` **8/8**；`dash_render_check.js` 通过；
  `selftest` **68 / 6 类型 / 8-8**。
- 回退验牙：**10 条行为级 RED / 0 结构性 / 无 ImportError**。

### 🔴 我 17:00 轮的 tri-state 修复解决了"假红"，但**在证据合并处造出两个新假绿**

我在 17:00 报告里写下的规矩是「**session measured facts > t0 snapshot**」。
**方向对，但不够**：它让**后来的好消息开始抹掉先前的坏消息**。

```text
t0 transport_complete = False   <- provider 明确声明：本次股票池被截断
session 30 轮全测、0 轮不完整    <- 会话里确实没再看到截断
=> universe_transport = ok       <- 硬事实被盖掉
```

第二个：**零证据被当成肯定证据**。

```text
_universe_session([]) -> measured=False, ratio=None, ever=False
=> universe_scope = ok  detail「会话期间未降级为仅自选股（全程全市场扫描）」
```

`finalize_metrics([])`（**一轮都没跑**）也输出同一句 —— 而同一份报告
的别处正 `fail=['rounds','data','api','sse','memory']`。

第三个：**会话内活跃覆盖下降无消费者**（t0 98.33% 掩盖会话 75%）。

### ⚠ 我自己的负控制"以错误的理由通过"（ADDENDUM2 §2 指认，我复算确认）

我在 17:00 轮把 `test_explicit_incomplete_still_fails` 当成负控制写进报告。
**但它的输入把被测分支绕过去了**：

```text
t0=False + rounds_transport_measured=0   -> fail   <-- 我的测试走的路径
t0=False + rounds_transport_measured=1   -> ok     <-- 一个单位就翻绿！
t0=False + rounds_transport_measured=30  -> ok
```

**有负控制 ≠ 有有效的负控制。** 已改成 `measured ∈ {0,1,30}` 参数化。

### 本轮修复 5 条（**全部针对我 17:00 轮的代码**）

| ID | 级别 | 复核 | 修复 |
|---|---|---|---|
| `...-EMPTY-SCAN-ASSERTED-AS-FULL-MARKET-001` | P1 | **成立** | scope 由 `bool` 升为**四态**；肯定句需 `measured` 门控 |
| `...-TRANSPORT-T0-SUPPRESSED-001` | P1 | **成立** | t0 硬负面**吸收**会话正面 |
| **我自己的负控制失效** | — | **成立** | 参数化覆盖 `measured ∈ {0,1,30}` |
| `...-ACTIVE-COVERAGE-SESSION-BLIND-001` | P1 | **成立** | 每轮采**分子+分母**；会话取最坏并优先于 t0 |
| `...-DROP-CAUSE-COLLAPSED-001` | **P2** | **部分** | `denominator_kind` 进入会话聚合；病因字段仍待搬 |

### ⚠ 修法纪律：ADDENDUM2 §1 **撤回了它自己**一条

ADDENDUM2 §1 撤回"`:1576` 守卫永不可达"的过强表述 ——
我实测 `data/live_session_*.json` **7/7** 都没有 `universe_session` 键，
**全部走那一支**，输出诚实的「无法判定」。
若照原暗示删掉它，会把**唯一的诚实旧报告路径**换成肯定句 —— **那是引入回归**。
所以修法是**给肯定分支加证据门控**，**不动** `:1576`，
并专门加了一条回归护栏测试 `test_old_archived_reports_take_honest_branch`。

### ⚠ 我探针里的"匹配错了的过滤器"（如实记录）

我用 `"全程全市场扫描" in detail` 判断是否被肯定 ——
但**新文案「**不能**据此说'全程全市场扫描'」也含这个子串**，
朴素子串判断把**否认**读成了**肯定**。改为 `_asserts_full_market()` 后修正。
**这与 ADDENDUM2 §3 批评它自己的是同一个错。**

### 深层修复：Session Universe Evidence Contract v1

**根因不是阈值，是"证据合并"没有合同。** 所以本轮**不是补三个洞**，而是立：
scope 四态 + transport join 语义（hard negative 吸收）
+ active coverage 数值证据（t0 与 session 取最坏）+ 旧报告兼容。

### 仍未闭合（**连续四轮**最高优先）

`IT-P1-UNIVERSE-MEMBERSHIP-QUALITY-001`：20:04 WP02 仍标"【独立 diff】"，
本轮按纪律**不混**。**下一轮应当专门做它。**

---

## 2026-09-22 20:04:38 JST

主实验：`EXP-IT-SESSION-EVIDENCE-005`

### 本轮主要结论

1. `6c8f11e` 的 tri-state / session consumer / watchlist 1/30&29/30 / freshness 修复继续维持 **已修**。
2. 旧报告 `_w_ever is None` guard 是**正确活代码**，必须保留；真正开放的是新 schema 的 zero-evidence / empty-scan 正向断言。
3. `IT-P1-UNIVERSE-TRANSPORT-T0-SUPPRESSED-001`：t0 explicit False 可被 session all-complete 覆盖。
4. `IT-P1-UNIVERSE-ACTIVE-COVERAGE-SESSION-BLIND-001`：本轮新确认；t0 98.3% 后 session active coverage 可降到 75%，final health 仍只读 t0 coverage。
5. 主改造：**Session Universe Evidence Contract v1**，按 scope / transport / active coverage / freshness 各轴合并时间线证据。
6. 270 deterministic contract cases 中 current/candidate 有 78 个严重度分歧；仅为软件合同输入空间，不是生产发生率。
7. 候选合同 288 个 hard-negative monotonicity checks / 0 violations；同样不是市场统计。
8. 模型训练=0；真实 Precision/Recall/收益 unavailable。

### 下一轮最高优先

- zero-evidence scope not_measured；
- t0 transport hard negative fail-dominant merge；
- per-round active coverage + session min/p05/last；
- old-report compatibility；
- 然后 membership/Quote usability、soft-empty source failover、timezone、ObservationInterval。
