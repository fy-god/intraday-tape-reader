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
