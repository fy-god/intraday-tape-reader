# 最新审计

**最新本地 Agent 产品轮**：[`2026-09-22_17-00-00_JST.md`](./2026-09-22_17-00-00_JST.md)  
**最新云端独立审计**：[`2026-09-22_16-13-10_JST.md`](./2026-09-22_16-13-10_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-22_16-13-10_JST_AGENT_TASK.md`](./2026-09-22_16-13-10_JST_AGENT_TASK.md)  
**最新盘中预警本地审计**：[`2026-09-22_16-20-44_JST.md`](./2026-09-22_16-20-44_JST.md)  
**最新盘中预警本地审计追加**：[`2026-09-22_16-20-44_JST_ADDENDUM.md`](./2026-09-22_16-20-44_JST_ADDENDUM.md)  
**最新盘中预警本地审计追加-2**：[`2026-09-22_16-20-44_JST_ADDENDUM2.md`](./2026-09-22_16-20-44_JST_ADDENDUM2.md)  
**最新产品提交 / reviewed_source_sha**：`c4f2ce107f9c1dafaefbec8310934d8e0fdd2ee2`  
**本轮审计开始 docs HEAD**：`50047d7c58e4fc75a93ccddf7d0b2eb7299cf8e1`  
**report_commit_sha**：`c93a80d5679f2927eabbf595b544b6c4bf4008d8`  
**agent_task_commit_sha**：`1e74a661f5c46b73270e14c02227e079914df716`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/6c8f11e363b3efcd21e410a5af7a718e08e8564d/docs/audits/intraday/LATEST.md

> 重要更正：上一版顶部把 docs audit commit `96e9...` 标成“最新被审产品 SHA”。
> 本轮已经按 `c4f2ce1..50047d7c58e4fc75a93ccddf7d0b2eb7299cf8e1` compare 重新核对：这段只有 docs 变化，
> 所以真正产品 SHA 仍是 `c4f2ce107f9c1dafaefbec8310934d8e0fdd2ee2`。历史报告文件均保留，本文件只更新接续指针。

---

## 2026-09-22 16:20:44 JST 追加-2（**更正我自己**的一条过强表述）

**追加-2 报告**：[`2026-09-22_16-20-44_JST_ADDENDUM2.md`](./2026-09-22_16-20-44_JST_ADDENDUM2.md)　
**被测产品提交**：`6c8f11e363b3efcd21e410a5af7a718e08e8564d`

三个**只读**子 agent 各自复核；我逐条自己重算。**一条推翻了我的表述（我改口）**，一条确认新缺陷，一条推翻回归子 agent 对第 8 项的定性。

- **🔻 撤回**：我上一份追加写 `:1576` 的「无法判定」守卫「**永不成立**」——**作为一般断言是错的**。
  实测**真实归档** `data/live_session_*.json` **7 个文件全部没有 `universe_session` 键**
  ⇒ `_w_ever is None` ⇒ **走 `:1576`，输出诚实的「无法判定」**。
  该分支是**活代码**，是唯一的旧报告正确处理；按我原暗示去动它**就是引入回归**。
  真正该修的仍是 `:1590` 那个肯定分支 —— **修法必须加证据门控，而不是删守卫**。
- **🔴 新缺陷（已确认）**：会话报「30 轮全完整」会**盖掉 t0 的硬事实**
  `transport_complete=False`，判决变 `universe_transport=ok`（`healthy` 不变）。
  **作者自己的负控制测错了**：`test_health_consumer_contract.py:180-192` 设
  `rounds_transport_measured=0`，**恰好绕过了它要测的分支**；
  实测 `measured=0 -> fail`、`measured=1 -> ok` ——**一个单位的改变就翻绿**。
- **🔻 更正回归子 agent**：第 8 项**不是**「零消费者」。我第一版矩阵把 `universe_truth` 放在顶层，
  而真实消费者读 `setup["universe_truth"]`（`:1642`）——**过滤器什么都没匹配上**（规则 mmm2）。
  改正后：`4/6 -> universe_coverage=fail`，`5900/6000 -> ok`，**丢掉的 2 只确实被消费**。
  真正的缺口是**语义**：停牌（良性）/ 传输少给（缺口）/ 只取到部分页（缺陷）**塌缩成同一个比率**。
- **旁证**：`TradingCalendar._now_fn` 全文件**只出现一次**（声明处）⇒ **死参数**；
  同一真实瞬间 `2026-09-22T10:00+08` 在 NY / UTC / Shanghai 三主机得到
  `POST / CLOSED / MORNING` **三种时段**；`src/` 内时区设施命中数 **0**。

`程序修复` 0 / `任务定义变更` 0 / `真实模型增益` 0；**未跑**真实 soak；归档交叉核对仍不可得 ⇒ **不给真实发生率**。

---
## 2026-09-22 16:20:44 JST 追加（零轮路径的**产物级**证据）

**追加报告**：[`2026-09-22_16-20-44_JST_ADDENDUM.md`](./2026-09-22_16-20-44_JST_ADDENDUM.md)　
**被测产品提交**：`6c8f11e363b3efcd21e410a5af7a718e08e8564d`

- 一个**只读**子 agent 独立复核本轮 P1 并给出**更强**的触发路径；我按规则 (vvv) 在**提交版**上逐条复算（它测的是提交前副本 3,041 行，提交版 3,051 行，**行号已全部改取提交版**）。
- **产物级证据（新增）**：`finalize_metrics([])`（**一轮都没跑成**）→
  `universe_session = {measured: False, watchlist_only_ratio: None, ever_watchlist_only: False}`
  → `universe_scope = ok`，文案「会话期间未降级为仅自选股（**全程全市场扫描**）」。
  经真实 `build_report` 序列化后，**该肯定句确实出现在报告 JSON 里**，而同一份判决是
  `rounds=0`、`healthy=False`、`fail=['rounds','data','api','sse','memory']`。
  **一份承认自己什么都没跑成的报告，不允许声称自己全程扫了全市场。**
  触发只需 `create_server` 抛异常或第 1 轮前 Ctrl+C —— 比原报告「真实 Engine 池被清空」更常见。
- **自相矛盾（两个都是 `ok`，故在 healthy/退出码里不可见）**：同一判决同时给出
  `universe: ok「无股票池规模记录…无法判定」` 与 `universe_scope: ok「全程全市场扫描」`。
- **死存储确认（函数归属 AST，提交版）**：`_sess_measured` 在 `L1555` 赋值、`L1804` 覆盖，
  最早读取在 `L1810`；`universe_scope` 块（`:1568-1592`）**从不读取** `measured`。
- **对照（未发现同类缺陷）**：零证据输入下 `universe_transport` / `universe_freshness` / `universe_coverage`
  **都正确地说「未测量 / 无法判定」**；`:1819` 的肯定分支被 `_tmeas > 0` 门控，**不可能**从零证据发出。
  ⇒ `universe_scope` 缺的正是这个模式。
- **未采纳/降级**：子 agent 标 `NOT_PROVEN` 的 truthiness 分类问题（`_optional_bool` 已缓解、生产可达未证明）
  与「t0 截断被会话全完整抑制」**仅记录、不列为缺陷**。
- `程序修复` 0 / `任务定义变更` 0 / `真实模型增益` 0；**未跑**真实 soak、**未做**训练。
  归档交叉核对仍不可得（`docs/audits/intraday/` 全部 JSON 均无 `universe_session`）⇒ **不给真实发生率**。

---
## 2026-09-22 16:20:44 JST 本地审计轮（Health Consumer Contract 复核）

**报告**：[`2026-09-22_16-20-44_JST.md`](./2026-09-22_16-20-44_JST.md)　**审计起点 HEAD**：`1b73270f83476ad24d09cc9f21d0ff44b2482f00`
　**审计终点 HEAD**：`6c8f11e363b3efcd21e410a5af7a718e08e8564d`

**上一版完整 LATEST 历史索引（不可变快照，本轮接续前的版本）**：
https://github.com/fy-god/intraday-tape-reader/blob/50047d7c58e4fc75a93ccddf7d0b2eb7299cf8e1/docs/audits/intraday/LATEST.md

- `execution_status`: `COMPLETED`　`evidence_status`: `VERIFIED`　`evidence_type`: `SOFTWARE_SAMPLE`。
- **测试真数**：`1b73270` → **1764 passed**；`6c8f11e` → **1791 passed**（+27），均 `0 failed / 0 skipped`、exit 0。
- **并发写入如实记录**：我以「未提交工作区改动」为对象完成取证后，作者在审计中途把它提交成 `6c8f11e`，
  且 `tools/live_session.py` 与 `tests/test_health_consumer_contract.py` **内容又变了**（155,261 B vs 154,570 B；
  22,847 B vs 20,595 B）⇒ **全部测量已在提交版上重做**，行号均取自 `6c8f11e`。

### ✅ 我在提交版上**独立验证成立**的修复（带负控制）

| 项 | 我的验证 |
|---|---|
| `IT-P1-UNIVERSE-TRANSPORT-TRISTATE-R1` | 冷启动 `transport_complete` = **`None`**（修前 `False`）；`make_round_sample` 保住 `None`；`_universe_session` 不计入截断；判决输出「**未测量**」而非「provider 声明截断」；**负控制**：显式 `False` 仍 `FAIL`、`healthy=False` |
| `IT-P1-UNIVERSE-TRANSPORT-SESSION-BLIND-001` | **变异测试**：`rounds_transport_incomplete` 0→1 使**完整判决签名改变**、`universe_transport` `ok→fail` |
| `IT-P1-HEALTH-SESSION-WITHOUT-T0-001` | setup 缺 `universe_truth` 时 `healthy=False`、`universe_freshness=fail`（不再整段跳过） |
| `IT-P1-UNIVERSE-WATCHLIST-FALLBACK-SESSION-BLIND-001` | 矩阵实测 0/30 `ok`、**1/30 `fail`**、29/30 `fail`、30/30 `fail` —— 29/30 那一档确实不再漏 |

### 🔴 本轮**新增**发现（尚未修复）

- **`IT-P2-UNIVERSE-EMPTY-SCAN-ASSERTED-AS-FULL-MARKET-001`（P1，已确认错误）**：
  `tools/live_session.py:1591-1592`。代理 `:2644` `watchlist_only = bool(codes) and len(codes) <= len(watch)`
  把**空扫描集**（`codes=0`）折叠成「非降级」，消费者于是输出**肯定句**「会话期间未降级为仅自选股（**全程全市场扫描**）」。
  **真实 Engine 端到端复现**：刷到 5000 只后清空 `_codes`、`state.quotes` 仍留 5000 → 轮样本 `universe=0 / quotes=5000` →
  `healthy=True`、`fail=[]`。「扫描集整个空了」与「正常全市场」在判决上**不可区分**。
  这是本轮主题的**第三形态**：修「不知道→有罪」的同时留下「**空→全市场**」。
- **`IT-P2-UNIVERSE-SCOPE-UNMEASURED-DEAD-GUARD-001`（P2，已确认错误）**：
  `:552-554` 的 `ever_watchlist_only = bool(watch_only)` **恒为 `bool`**，故 `:1576` 的
  `elif _w_ever is None:`（「无法判定」）**结构上不可达**；生产者已经算对的 `watchlist_only_ratio is None`
  （「未测到」）**有出口、零消费者**，被 `ever=False` 顶掉后走 `else` 输出肯定句。
  这与本仓库反复出现的「判决层零读者」是同一形态的新一例。
- **`IT-P3-UNIVERSE-WATCHLIST-PROXY-COUNT-001`（P3，待验证风险／`未复现`）**：
  `:2644` 用**数量比较**猜意图（`len(codes) <= len(watch)`），而作者自己在 `:1570` 写下
  「绝不能靠"股票数很少"猜意图」。反例实测：`codes=8 / watch=20` → 被判「降级为仅自选股」。
  **降级理由**：同一输入下 `universe` 项在**修前就**判红、`healthy` 两版皆 `False`，
  故不是新放行，只是**新增一条措辞错误**的失败原因；且未找到真实生产证据。

### 未做到的取证

`docs/audits/intraday/` 下**全部 11 个已提交 JSON 均不含 `universe_session` 键**、也无可用的
`rounds` 数组 ⇒ 上述缺陷**无法**在归档真实运行上交叉核对，**不给真实发生率**。
未跑真实 soak／训练／评估。

---
## 2026-09-22 17:00 JST 本地 Agent 产品轮（**撤回我 13:00 轮的"3 项全部成立"**）

- 起点 HEAD `c4f2ce1` → pull 到 `1b73270`（只见审计文档）；
  `reviewed_source_sha` = **`c4f2ce1`**，即**我 13:00 轮推的那个提交**。
- 归档机器证据：**1791 passed in 84.10s**（上轮 1764 → **+27**）；
  `check_*.py` **8/8**；`dash_render_check.js` 通过；
  `selftest` **68 / 6 类型 / 8-8**。
- 回退验牙：**20 条行为级 RED / 0 结构性 / 无 ImportError**。

### 🔴 我 13:00 轮声称"3 项修复全部成立"是**错的 —— 我撤回**

一条**并行只读审计线**（`2026-09-22_13-32-18_JST.md`）对**同一个 commit**
给出相反裁决。`2026-09-22_13-55-00_JST_ADDENDUM.md` 逐条复现后
**确认对方是对的**，把三项从"已修复"改为 **PARTIAL**。我本轮独立复现，
**确认更正附刊是对的，我原来错了**。

**我错在方法论**：只验了"**机制是否存在**"（三条机制**都是真的**），
**没验** ①边界可达性 ②出口完整性（**异常出口不是 `return`**）
③**新引入的镜像错误**。

### 根因：**三态被压成两态**

```python
# 我的代码（engine.py:975，修前）
"transport_complete": bool(transport_complete)
#   None（我没测）  ->  False（provider 说被截断了）
```

冷启动 `_universe_meta == {}` → `bool(None)` = `False` → 判决层输出
**"provider 声明本次股票池被截断"**，`exit=1`，
**把排障指向数据源**。**provider 什么都没声明。**
这是"把不知道当成**有罪**"—— 与我一直在修的
"把不知道当**没问题**"（假绿）**同源**，都是三态压成两态。

### 本轮修复 9 条（**全部针对我 13:00 轮的代码**）

| ID | 级别 | 复核 | 修复 |
|---|---|---|---|
| `IT-P1-UNIVERSE-TRANSPORT-TRISTATE-R1` | P1 | **成立** | tri-state 端到端（尖层 + 轮样本 + 聚合层，禁 `bool()`） |
| `IT-P1-UNIVERSE-TRANSPORT-SESSION-BLIND-001` | P1 | **成立** | 真正消费 `_ti`（第 **6** 次"判决层零读者"） |
| `IT-P1-HEALTH-SESSION-WITHOUT-T0-001` | P1 | **成立** | session 消费**独立于** t0 |
| `IT-P1-UNIVERSE-WATCHLIST-FALLBACK-SESSION-BLIND-001` | P1 | **成立** | round count/ratio/ever + 显式 `--watch-only` |
| `IT-P2-UNIVERSE-FALLBACK-FRESHNESS-RESET-001` | P2 | **成立** | 拆两个时钟，降级不得重置"全市场" |
| `IT-P2-UNIVERSE-FRESHNESS-CONFIG-KEY-001` | P2 | **成立** | 读正式键 + **判决层消费 Engine 导出的阈值** |
| `IT-P2-UNIVERSE-FRESHNESS-UNKNOWN-SEMANTIC-001` | P2 | **成立** | `unknown` 单列，说"未测量" |
| `IT-P2-UNIVERSE-ATTEMPT-EXCEPTION-EXIT-001` | **P2** | **成立** | 外层 finalizer 落 `crashed` 后原样抛出 |
| `IT-P1-EVAL-FAIL-COVERAGE-SILENT-001` | P1 | **成立** | **说谎的旋钮**接上消费者 |

### ⚠ 严重度纪律：我**下调**了 13:55 附刊的一条分级

13:55 附刊把 `engine.py:1180` 的异常出口列成 **P0**。
**16:13 §1.2 指出当时没有生产可达触发证据**（`build_source` 异常 /
普通网络异常 / `Settings.get` 都已各自被 catch）。
**我采纳：按生产可达性只算 P2 robustness**，我保留 finalizer
（机制成立、成本极低）但**不**写成 P0。

### ⚠ 修 B5 时我又发现一个零读者（本轮新增）

改完配置键之后**判决仍然不跟随** —— Engine 已导出
`freshness.warn_s`/`fail_s`，但 `evaluate_health` **只用自己的硬编码常量**。
**生产者的出口没有消费者。** 已一并修复（判决优先读 Engine 导出值）。

### ⚠ 我自己的两个坑（如实记录）

1. **我在修 tri-state 时自己制造了一个死变量**（`_t_known` 失去读者）。
   写 AST 脚本自查"赋值后从未读取"时，同时抓到**既有的 `ev_fail_cov`**
   —— 正是云端点名的说谎旋钮。**本轮主题恰恰是"赋值 ≠ 消费"，
   我差点自己又留一个。**
2. **`pull` 被两个未跟踪文件挡住**（远端**也**提交了同名文件）。
   我先备份到 `$env:TEMP`，再用 **`git hash-object` 权威比对 blob** ——
   **不是字节数比较**（`git show > file` 在 PowerShell 下写 UTF-16，
   我第一次因此得到"内容不同"的**假结论**），确认**逐字节相同**后才移除。

### 仍未闭合（**连续三轮**最高优先）

`IT-P1-UNIVERSE-MEMBERSHIP-QUALITY-001`：`_record_universe` 的
active membership **来源未改**。16:13 WP06 明确要求"**单独 diff**，
不和 health consumer 混一起"—— 本轮正是 health consumer，按纪律**不混**。

---

## 2026-09-22 16:13:10 JST

**范围**：Universe v4 判决消费层、missingness/tri-state、会话 watchlist/transport、refresh 异常可达性、SourceManager 空返回、宿主时区、fresh-flat window，以及研究链前置条件。  
**主实验**：`EXP-IT-HEALTH-CONSUMER-004`

当前最高优先：
1. `IT-P1-UNIVERSE-WATCHLIST-FALLBACK-SESSION-BLIND-001`
2. `IT-P1-UNIVERSE-TRANSPORT-SESSION-BLIND-001`
3. `IT-P1-UNIVERSE-TRANSPORT-TRISTATE-R1`
4. `IT-P1-HEALTH-SESSION-WITHOUT-T0-001`
5. `IT-P1-SOURCE-EMPTY-001`
6. `IT-P0-002-TZ-R1`（当前严重度 P1）
7. `IT-P1-WINDOW-001`
8. `IT-P1-UNIVERSE-MEMBERSHIP-QUALITY-001`

审计更正：
- 13:55 附刊把 `_universe_sources()` 外层异常出口升成 P0；本轮生产可达性审计后下调为
  `IT-P2-UNIVERSE-ATTEMPT-EXCEPTION-EXIT-001`。普通 provider、build_source、Settings.get 路径均已有保护。
- 13:32 建议 `setup OR 顶层 fell_back` 仍不足：顶层 bool 只有 **100% 轮次** watchlist-only 才 True。
  本轮 29/30 机制反例仍为 False，需保留 `watchlist_only_rounds/ratio/ever`。
- transport missingness 必须全链 tri-state；当前 Engine 和 `make_round_sample` 都会 `None -> False`。

**主改造**：Health Consumer Contract v1 + Universe Truth v4.1。  
**模型训练**：0；真实 Precision/Recall/收益仍 unavailable。

**下一轮关键产物**：
- `health_consumer_cases.json`
- `universe_refresh_cases.json`
- `source_empty_cases.json`
- `timezone_cases.json`
- `observation_interval_cases.json`
- `per_code_provenance.json`
- `full_pytest.log`
- `check_*.py` logs
- `RUN_MANIFEST.json`
- `NEXT_STEPS.md`
