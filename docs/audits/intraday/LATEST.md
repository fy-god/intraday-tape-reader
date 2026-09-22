# 最新审计

**最新本地 Agent 产品轮**：[`2026-09-22_13-00-00_JST.md`](./2026-09-22_13-00-00_JST.md)  
**最新独立审计**：[`2026-09-22_13-32-18_JST.md`](./2026-09-22_13-32-18_JST.md)  
**最新云端独立审计**：[`2026-09-22_12-03-20_JST.md`](./2026-09-22_12-03-20_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-22_12-03-20_JST_AGENT_TASK.md`](./2026-09-22_12-03-20_JST_AGENT_TASK.md)  
**最新被审产品 SHA**：`c4f2ce107f9c1dafaefbec8310934d8e0fdd2ee2`  
**审计时间**：2026-09-22 13:32:18 JST  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/c4f2ce107f9c1dafaefbec8310934d8e0fdd2ee2/docs/audits/intraday/LATEST.md

> 历史报告文件没有删除或覆盖；为避免把 30KB+ 历史索引每轮重复复制并引入冲突，旧索引正文固定保留在上面的不可变 commit 快照中。本文件只移动“当前接续”指针。

---
## 2026-09-22 13:32 JST 独立审计（判决层仍在读 t0 快照 —— 第 6 次"数据层有了、判决层零读者"）

- 审计 HEAD = `reviewed_source_sha = c4f2ce107f9c1dafaefbec8310934d8e0fdd2ee2`；
  `git status --porcelain` 空、`stash` 空（审计前后一致）。
- 独立实测：**1764 passed in 74.31s**（`exit=0`）；`check_*.py` **8/8 `exit=0`**；
  `selftest` **`exit=0`**（962 条 / 68 告警 / 6 类型 / 类型级 8-8）。
- 本仓库**没有** `docs/audits/validate_latest.py`（也无其测试）⇒ 手册 §2 step 5 的
  gate **N/A**，本轮**不声明任何 gate pass**。

### 🔴 两处会话级事实已算出、判决层零读者

`c4f2ce1` 为**新鲜度**接通了会话级聚合（`worst_state`，真进步），
但同轮的另两个会话级事实仍是"数据层有了、判决层零读者"：

| # | 会话级事实 | 判决实际读的 | 后果 |
|---|---|---|---|
| **1.1** | `fell_back_to_watchlist`（`live_session.py:859`，=`watch_only_rounds >= len(rows)`） | 只读 **`setup` t0 快照**（`:1495`） | **全场退化成仅自选股 = `healthy=True/exit=0`** |
| **1.2** | `rounds_transport_incomplete`（`:526`） | 只读 t0 的 `transport_complete` | 中途被截断 = 绿灯 |

**1.1 更严重**（实测）：`setup=False` + 顶层 `True`（3/3 轮退化，真实 `make_round_sample`→
`summarize_rounds` 产出）→ `healthy=True exit=0`，`universe` 还显示"扫描池 5000 只"（t0 数字）；
对照组让 `setup=True` → `healthy=False exit=1 fail=['universe']`。
**扫描集合整体消失**被读成健康。

**1.2**：`_ti = _safe_int(_sess.get("rounds_transport_incomplete"))`（`:1669`）在 **2837 行**里
出现 **1 次**，是 `Store` 不是 `Load`；子 agent 的 `dis()` 反汇编只得一条 `STORE_FAST _ti`，
**无 `LOAD_FAST`**；**1008 组配置**逐一比较完整判决签名 → **0 组有差异**。
对照实验（基线先绿）：只改 `rounds_transport_incomplete` 0→12 ⇒ 判决与基线**逐项相同**；
只改 `worst_state` fresh→stale ⇒ 立刻判红。

### 附带洞：t0 缺 `universe_truth` 时会话事实被整体跳过

`_sess` 消费块嵌在 `if isinstance(_ut, dict) and _ut:`（`:1543`）**内部** ⇒
`worst_state='stale'`+`ti=99`+`na=30` 的会话被**整体忽略**（实测 `healthy=True exit=0`；
同一会话在 t0 有快照时 → `healthy=False exit=1`）。

### 系统性清点（解释了为什么修一次还会再长）

AST 全量清点 `evaluate_health` 实际消费了多少"已算出的东西"：
`summarize_rounds` **22/52**、`_universe_session` **7/11**、`empty_metrics` **26/64**。

- **`evaluability_fail_coverage` 是说谎的旋钮**：`live_session.py:1365` 取出后**从未读取**，
  同行的 `ev_warn_cov`/`ev_min_samples` **都在用** ⇒ 运维配了也静默无效。
- **整块会话级交付账本零读者**（`:966-973` 六个 `delivery_*_total` + `:950` `signal_delivery`）：
  全仓唯一提到它们的是**字符串存在性测试**（`test_delivery_false_green.py:206` 只断言名字**出现**），
  判决只读自洽性子字典 —— 维护者记的第 1 次错误**原样复发**。
- `_universe_session` 的 `last_state`/`last_age_s`/`refresh_ids_seen` 只被测试读过。

**根因**：现有测试钉的是"**键名在源码文本里出现过**"，不是"**该键被判决消费**"
（`test_delivery_false_green.py:194-195` 的失败信息甚至写着"否则 soak 对它全盲"，
断言却写成字符串存在性；`test_universe_refresh_truth_v4.py:361` 把该键写死为 `0` 且从不断言其效果）。

### 撤回

本轮我自己提的"age 检查在会话已测量时不可达"候选**被代码否掉**：
`src/arad/engine.py:1036-1044` **直接由 `age` 派生 `state`**，阈值一致 ⇒
`age=99999` 必然 `state='stale'`。我的反例是自相矛盾的输入。**无发现，撤回。**

### 上一轮 3 项修复独立复核：**全部成立**

`universe_transport` 拆分 ✓；`refresh_universe` **4 个 return 全部经 `_finish_attempt` 落账**
（`:1172-1178`，5 状态全覆盖）✓；会话级 `worst_state` 确实进判决 ✓。
`rounds_transport_incomplete` 是 `c4f2ce1` **本轮新引入**的字段 ⇒ 上述是新洞，不是旧账。

### 开放项回归（在 `c4f2ce1` 上逐一复核，**无一项已修**）

`IT-P1-UNIVERSE-MEMBERSHIP-QUALITY-001`（`engine.py:1261-1279`）、
`IT-P1-WINDOW-001`（`src/` 零实现，`git grep` rc=1）、`IT-P0-002-TZ-R1`
（`session.py:214`，`zoneinfo/tzinfo/utcnow` 全仓零命中，同一 epoch 三种时区三种 phase）、
R-14 `rule_version` 只写不读、R-15 `to_dict()` `ts=None` **已复现 AttributeError**、
R-22 `_scope_note` 死键+悬空指称、R-23 **无任何 CI**、R-13 `spirit_*` 默认关（非缺陷）。

**1 项框架被反驳**：`IT-P1-SOURCE-EMPTY-001` 原表述"`[]` 与异常不可区分所以误判"**不准确** ——
实测两者**可区分**（异常→切备源 `fails={'R':1}`；`[]`→`fails={'R':0}`，连续 5 次空也不切）。
真实缺陷是"**空被当作确定性成功、永不 failover**"，应据此重述。

### 验收测试（已写，对当前 HEAD 实测为 RED）

`2 failed, 2 passed` —— 基线绿 ✓、干净会话不误报 ✓，而"中途截断必须判红"**当前失败**。

### 仍未闭合（下一轮最高优先）

1. `IT-P1-UNIVERSE-WATCHLIST-FALLBACK-SESSION-BLIND-001`：`:1495` 改为
   `setup or 顶层` 取或（与 `:1664-1687` 处理 universe 会话口径的做法一致）。
2. `IT-P1-UNIVERSE-TRANSPORT-SESSION-BLIND-001`：让 `:1669` 的 `_ti` 真正进判决，
   并把 `_sess` 块提到 `if _ut:` 之外。**两条改动是同一处。**



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
