# 最新审计

**最新本地 Agent 产品轮**：[`2026-09-22_13-00-00_JST.md`](./2026-09-22_13-00-00_JST.md)  
**最新云端独立审计**：[`2026-09-22_12-03-20_JST.md`](./2026-09-22_12-03-20_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-22_12-03-20_JST_AGENT_TASK.md`](./2026-09-22_12-03-20_JST_AGENT_TASK.md)  
**最新被审产品 SHA**：`369b13f0d6f20fc22198abf51469ee14e49c84dc`  
**审计时间**：2026-09-22 12:03:20 JST  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/bba01b1751ca3c2214dcd09f8321578634bf56a3/docs/audits/intraday/LATEST.md

> 历史报告文件没有删除或覆盖；为避免把 30KB+ 历史索引每轮重复复制并引入冲突，旧索引正文固定保留在上面的不可变 commit 快照中。本文件只移动“当前接续”指针。

---

## 2026-09-22 13:00 JST 本地 Agent 产品轮（我新加的 gate 自己成了假绿 —— 第 5 次同类错误）

- 起点 HEAD：`369b13f0d6f20fc22198abf51469ee14e49c84dc`；
  `reviewed_source_sha` = `369b13f`（我 09:00 轮推的提交，正是本轮被审的那个）。
- 归档机器证据：**1764 passed in 61.67s**（上轮 1745 → +19）；
  `check_*.py` **8/8**；`check_bom` 通过；`dash_render_check.js` 通过；
  `selftest` **68 / 6 类型 / 8-8**。
- 回退验牙：**14 条行为级 RED / 0 结构性，无 ImportError**。

### 🔴 我 09:00 轮加的 `universe_coverage` 判决项**自己成了新的假绿来源**

我当时的约定是"分母未知 → 未测量 → ok"。约定本身没错（无 numeric total
确实算不出比例），但我**漏了一类"分母未知、事实却明确"的情况**：

```
provider 自己说 transport_complete = False   <- 本次股票池被截断
```

实测（云端 09:49 先发现、12:03 复核，我独立复现）：
分母未知 + `transport_complete=False` → 我的 gate 判 **`ok` / `healthy=True` / `fail=[]`**。
**这不是"无从判断"，是"可判而未判"** —— provider 已经把答案告诉我们了。

**这是同一类错误的第 5 次**：
| # | 轮次 | 形态 |
|---|---|---|
| 1 | 21:00 | 交付账本数据层有了、判决层零读者 |
| 2 | 05:00 | `len(state.quotes)` 累计缓存当逐轮量 → `no_data_rounds` 恒 0 |
| 3 | 05:00 | `_acked` 哈希序裁剪丢掉刚 ack 的 |
| 4 | 05:00 | `status()['universe']` 用累计缓存 → 看板高报 |
| **5** | **09:00（本轮被指认）** | **新 gate 把"provider 明说被截断"归入"未测量"** |

共同形态：**把"我不知道"当成了"没问题"**，而没有先把已知的坏情况逐个排除。

### 修复 1：`universe_transport` 独立判决项

拆出**独立**项，只读 `transport_complete`。**为什么不合并进 `universe_coverage`**：
两项证据来源不同（"provider 说全不全" vs "比例算不算得出"），
合并就会让一个被另一个的"未测量"掩盖 —— **正是本缺陷的成因**。

实测：`transport_complete=False` → **fail**；
`transport_complete=True` 且无分母（新浪 clean pagination）→ **仍 ok**（不误报）。

### 修复 2：`active_snapshot` / `latest_attempt` / `freshness` 三账分离

`IT-P1-UNIVERSE-META-STALE-AFTER-FAILED-REFRESH-001`：全源失败时
`refresh_universe()` 返回 0 但**不更新** `_universe_meta`。实测：

| 观测 | 修前 | 修后 |
|---|---|---|
| 全源失败后 `attempt_status` | **字段不存在** | **`all_failed`** |
| 两次 `universe_truth()` 逐字段相同 | **`True`** | **`False`** |
| active pool 保留 | 保留（对） | 保留（对） |
| `freshness.fresh` | 不存在 | 失败后 **`False`** |

保留 active pool 是**对的**，但"上次成功"不能被读成"现在健康"。
四条出口（`applied` / `applied_partial` / `rejected_smaller` / `all_failed` / `empty`）
**全部落账**。

### 修复 3：WP02 会话级聚合（t0 快照不能代表整场 soak）

`setup` 只在 soak **之前**取一次 `universe_truth`，中途的刷新失败
**结构上**进不了最终判决。新增每轮采样（只取标量）+ `_universe_session()`
聚合，**`worst_state` 取最坏**（中途坏过不能被"最后又好了"抹掉）。
实测：t0 健康 + 会话 stale → `healthy=True` → **`False`**。

### ⚠ 本轮我自己的两个坑（如实记录）

1. **探针打到了真实网络**：我用 `Engine(source=fake)` 以为 fake 就是 universe 源，
   但 `_universe_sources()` 会**按配置新建源**、**不使用注入的 source** ——
   失败注入根本没生效，反而**真实抓了一次东财股票池**
   （得到 `expected_total=5918 / active=5554`，即 `coverage_active=93.85%`）。
   这是探针缺陷，不是产品缺陷；副作用如实记录（无服务启动、无常驻进程）。
2. **一次 edit 把两行粘成一行** → `SyntaxError` / pytest collection error，已修。

### 仍未闭合（下一轮最高优先，连续两轮）

`IT-P1-UNIVERSE-MEMBERSHIP-QUALITY-001`：`_record_universe` 的 active
membership **来源未改**。改它会**改变实际扫描集合**，必须配真实 provider 回放。

---

## 本轮范围

- `reviewed_source_sha = 369b13f0d6f20fc22198abf51469ee14e49c84dc`
- 审计开始 `main = bba01b1751ca3c2214dcd09f8321578634bf56a3`；`369b13f0d6f20fc22198abf51469ee14e49c84dc..bba01b1751ca3c2214dcd09f8321578634bf56a3` 只有 `docs/audits/intraday/` 文档变更。
- 开放 PR：0。
- 已读：HEAD/提交差异/递归源码树/README/CHANGELOG/config/Engine/Store/Session/SourceManager/live-session/tests/LATEST/NEXT_STEPS。
- 固定产品树未发现 `.github/workflows` 目录；不据此推断仓库外自动化。
- 本轮未运行完整 repo pytest；最近独立机器证据继续采用 10:05 审计的 `1745 passed in 95.98s + 14 probes exit 0`。
- 真实 Precision/Recall/漏报率/收益：`unavailable`。

## 当前开放项

1. `IT-P1-UNIVERSE-COVERAGE-GATE-TRUNCATION-BLIND-001`
2. `IT-P1-UNIVERSE-META-STALE-AFTER-FAILED-REFRESH-001`
3. `IT-P1-HEALTH-COVERAGE-SNAPSHOT-PRELOOP-001`（静态/机制成立，真实 soak RED 待跑）
4. `IT-P0-002-TZ-R1`（P1）
5. `IT-P1-SOURCE-EMPTY-001`
6. `IT-P1-WINDOW-001`
7. `IT-P1-UNIVERSE-REFRESH-STORM-001`（机制风险，真实 import 频率验牙前不升级）

## 主实验

`EXP-IT-UNIVERSE-FAILURE-003`

核心结果（均为确定性软件机制，不是实盘）：
- unknown denominator + `transport_complete=false`：当前判 `OK/not_measured`，候选 v4 判 FAIL；
- retained full + recent partial reject：当前 OK，候选 WARN；
- retained full + stale failed/rejected refresh：当前 OK，候选 FAIL；
- primary snapshots 返回 `[]`：当前 backup 不调用；抛异常时 backup 正常调用；
- fresh-flat history：当前可出现 `_covered=True` 但 `price_change=None`。

## 主改造

**Universe Refresh Truth Contract v4**：
- `active_snapshot`
- `latest_attempt`
- `freshness`
- session-level universe truth aggregation

不采用“失败就清 `_universe_meta`”或“失败完全不动 `_universe_meta`”两个极端方案。

## 下一轮必核查产物

- `universe_refresh_truth_cases.json`
- `universe_session_snapshot_cases.json`
- `timezone_cases.json`
- `source_empty_cases.json`
- `observation_interval_cases.json`
- `partial_retry_cases.json`
- `full_pytest.log`
- `check_*.py` logs
- `RUN_MANIFEST.json`
- `NEXT_STEPS.md`

研究线继续：真实数据 manifest → TaskSpec/labels/splits → factor diagnostics → tabular → TCN。
