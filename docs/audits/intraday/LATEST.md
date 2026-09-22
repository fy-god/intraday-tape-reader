# 最新审计

**最新本地 Agent 产品轮**：[`2026-09-22_17-00-00_JST.md`](./2026-09-22_17-00-00_JST.md)  
**最新云端独立审计**：[`2026-09-22_16-13-10_JST.md`](./2026-09-22_16-13-10_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-22_16-13-10_JST_AGENT_TASK.md`](./2026-09-22_16-13-10_JST_AGENT_TASK.md)  
**最新产品提交 / reviewed_source_sha**：`c4f2ce107f9c1dafaefbec8310934d8e0fdd2ee2`  
**本轮审计开始 docs HEAD**：`50047d7c58e4fc75a93ccddf7d0b2eb7299cf8e1`  
**report_commit_sha**：`c93a80d5679f2927eabbf595b544b6c4bf4008d8`  
**agent_task_commit_sha**：`1e74a661f5c46b73270e14c02227e079914df716`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/50047d7c58e4fc75a93ccddf7d0b2eb7299cf8e1/docs/audits/intraday/LATEST.md

> 重要更正：上一版顶部把 docs audit commit `96e9...` 标成“最新被审产品 SHA”。
> 本轮已经按 `c4f2ce1..50047d7c58e4fc75a93ccddf7d0b2eb7299cf8e1` compare 重新核对：这段只有 docs 变化，
> 所以真正产品 SHA 仍是 `c4f2ce107f9c1dafaefbec8310934d8e0fdd2ee2`。历史报告文件均保留，本文件只更新接续指针。

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
