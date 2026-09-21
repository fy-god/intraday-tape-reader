# 最新审计

**最新云端独立审计**：[`2026-09-22_08-08-19_JST.md`](./2026-09-22_08-08-19_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-22_08-08-19_JST_AGENT_TASK.md`](./2026-09-22_08-08-19_JST_AGENT_TASK.md)  
**最新本地 Agent 产品轮**：[`2026-09-22_05-00-00_JST.md`](./2026-09-22_05-00-00_JST.md)  
**上一份云端独立审计**：[`2026-09-22_04-05-26_JST.md`](./2026-09-22_04-05-26_JST.md)  
**上一份云端 Agent 任务书**：[`2026-09-22_04-05-26_JST_AGENT_TASK.md`](./2026-09-22_04-05-26_JST_AGENT_TASK.md)  

## 2026-09-22 08:08:19 JST 云端审计

- `reviewed_source_sha` = `2406d818168da9e642e73b73b254762050e229e5`
- 范围：Universe denominator / membership / retry、zero-return reconciliation、研究因子资产。
- 已回归确认：`2406d818` 已修 silent-empty 假绿、ACK recency、status universe cache、绝对 universe health gate。
- **最高优先开放**：`R-12 / IT-P1-UNIVERSE-DENOMINATOR-001`。
- **本轮新确认 P1**：`IT-P1-UNIVERSE-MEMBERSHIP-QUALITY-001`。
- **本轮新确认 P2**：`IT-P2-OBS-EMPTY-ROUND-R1`。
- 继续开放：`IT-P1-UNIVERSE-REFRESH-STORM-001`、per-code provenance、targeted fallback、ObservationInterval、SSE durable。
- 主实验：`EXP-IT-UNIVERSE-TRUTH-002`。
- 本轮模型训练：0；新增 32-factor 定义 catalog，**无性能成绩声明**。
- 下一产物：`universe_truth_cases.json`、`universe_coverage_reconcile.json`、`empty_round_reconcile.json`、per-code provenance、真实 dataset manifest（若本地存在授权数据）。

### 主实验摘要

| fixture | active coverage | 当前 gate |
|---|---:|---|
| 4100/4200 | 97.62% | WARN |
| 4100/5913 | 69.34% | WARN |
| 4500/5913 | 76.10% | **OK** |
| 4600/5913 | 77.79% | **OK** |

这些均为明确 synthetic denominator fixture，**不是本轮真实市场 coverage**。

---

## 历史索引原文（完整保留）

# 最新审计

**最新本地 Agent 产品轮**：[`2026-09-22_05-00-00_JST.md`](./2026-09-22_05-00-00_JST.md)  
**最新云端独立审计**：[`2026-09-22_04-05-26_JST.md`](./2026-09-22_04-05-26_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-22_04-05-26_JST_AGENT_TASK.md`](./2026-09-22_04-05-26_JST_AGENT_TASK.md)  
**上一份本地 Agent 产品轮**：[`2026-09-22_01-00-00_JST.md`](./2026-09-22_01-00-00_JST.md)  
**上一份云端独立审计**：[`2026-09-22_00-43-31_JST.md`](./2026-09-22_00-43-31_JST.md)  
**上一份云端 Agent 任务书**：[`2026-09-22_00-08-36_JST_AGENT_TASK.md`](./2026-09-22_00-08-36_JST_AGENT_TASK.md)  

## 2026-09-22 05:00 JST 本地 Agent 产品轮（静默断供的完整假绿 + 同一 bug 类再 +3）

- 起点 HEAD：`44b80c221667c39389606b5bcc40a02db0ec723a`；`reviewed_source_sha` = `e57bb3b87baacefd4e8abd908a5bc17877179ebf`（我 01:00 轮推的提交，正是云端 h=0 轮与 04:05 轮审的那个）。
- 归档机器证据：**1720 passed in 89.03s**（上轮 1694 → +26）；`check_*.py` **8/8**；`check_bom` 通过；`dash_render_check.js` 通过；`selftest` **68 / 6 类型 / 8-8**。
- 回退验牙：**14 条行为级 RED / 0 结构性，无 ImportError**。

### 云端按我上一轮的教训又找到 3 个存活实例 —— 我全部复核成立并修复

我 01:00 轮写下教训"修一个可观测性缺陷必须走到有人改变结论为止"，并自述"同一 bug 类我漏了第二个实例"。
云端 h=0 轮按此教训在全仓扫描「有界容器被当作完整总量」，**又找到 3 个**。
**本轮证明：漏的不止第二个 —— 该 bug 类在本仓共 5 个实例。**

| # | 实例 | 位置（修复前） | 危害 |
|---|---|---|---|
| 1 | ring buffer 当累计分母 | 测试 | 假红（21:00 修）|
| 2 | 按 kind 的 total 被截断 | `web.py:603` | 展示错（01:00 修）|
| **3** | **累计行情缓存当逐轮数** | **`live_session.py:1971`** | **完整假绿（本轮）** |
| **4** | **`_acked` 裁剪保留"任意"2000** | **`store.py:359`** | **用户可见（本轮）** |
| **5** | **`status()['universe']` 用累计缓存** | **`store.py:304`** | **看板高报（本轮）** |

### 🔴 第 3 个实例：静默断供拿到 `healthy=True / exit=0 / fail=[]`

`state.quotes` 是**累计**最新报价缓存，`prune()` **不回收**它（实测灌 5913 只后
`prune(keep_codes={'sh600001'})` 仍是 5913）。把它当逐轮行情数写进
`metrics['quotes']` 后，`no_data_rounds`（判据 `int(r['quotes']) <= 0`）
**结构上恒为 0**，`add("data", quotes_max > 0)` 也恒为旧值 ——
**两个专门盯行情数的判决项同时失效**。

**真实 `_soak_loop` 端到端（不是纯函数构造）**：

```
修复前：quotes = [500, 500, 500, 500, 500, 500]   no_data_rounds = 0   healthy=True
修复后：quotes = [500,   0,   0,   0,   0,   0]   no_data_rounds = 5   healthy=False
```

诚实边界：断供要求 provider **返回空且不抛异常**；抛异常时 `error_rounds>0` 会让 `fetch` 正确转红。
所以**不是**判决层全瞎，而是"**恰好静默的那种**断供全瞎"。

旁证：`tests/test_live_session_observation.py:5` 的 docstring **自己就写着这个坑** ——
认识已存在，但修复只落在 docstring/`finalize_metrics`，**没有落到写入点**。

修复两处：写入点改用本轮 `observation['returned']`；**空轮也必须落一份 `returned=0` 的账**
（此前 `poll_once` 直接 `return []`，断供根本不进观测账本）。

### 第 4 个实例：用户会看到"自己刚确认过的告警重新变成未确认"

`list(set)` 是**哈希序**，`[-2000:]` 取的是**任意** 2000 条：

| 判别式 | 修复前 | 修复后 |
|---|---|---|
| `set(_acked) == 最新 2000` | **False** | **True** |
| 最新那条 key 存活 | **False** | **True** |

看板 `if(a.acked)` 会给它加 `acked` class（变暗 + **隐藏 ack 按钮**）⇒ 用户可见。

### `R-21` 升级为已确认并修复：扫描范围从不参与判决

云端把根因从"缺判决项"精确到"**数据早已传入 `setup`、只差一个读者**"——
我独立确认 `finalize_metrics` 的 `setup` 里 `universe_size` / `fell_back_to_watchlist`
**早就有了**，而判决项名单里 `universe` 一个 token 都不出现。
这把 R-21 从"需要新采集的设计任务"降级为"一个小补丁即可闭合"。已加判决项：
降级为自选股 → **fail**；< 3000 只 → **fail**；< 4500 只 → **warn**；未测量 → **ok**。

### ⚠ 我在实现本轮修复时，**又踩了同一个坑一次**

修空轮分支时我写了 `self.sources.capabilities()` —— **`SourceManager` 根本没有这个方法**。
调用抛异常，被我自己写的 `except: self.log.debug(...)` **吞掉**，于是**账本一份都没发**，
而外表完全正常。**这正是我最近三轮一直在批评的反模式。**

**是端到端探针抓住的，不是单元测试**（24 条单测全绿，因为纯函数行为是对的，
错的是**没人喂给它数据**）。已改用与正常路径一致的取数方式，
日志从 `debug` 提到 **`error`**，并加回归测试断言 `observation_seq` **必须严格递增**。

> 教训升级：**纯函数测试通过 ≠ 这条链路上真的有人在喂数据。**

### 对上一轮结论的下调

* 01:00 轮"同一 bug 类我漏了第二个实例" → **下调**：漏的不止第二个，共 5 个。
* 01:00 轮把 `R-21` 列为"待验证风险" → **升级为已确认错误**（云端证明生产可达），
  同时根因比我写的更浅、修复成本比我估的更低。

---

## 2026-09-22 04:05:26 JST 云端审计：Round Truth 成为最高优先

- `reviewed_source_sha` = `e57bb3b87baacefd4e8abd908a5bc17877179ebf`
- 审计开始 HEAD = `904f67443bc84deab15acca2b384563abe395feb`
- `e57bb3b -> 904f674` 只有 `docs/audits/intraday/` 文档更新；当前开放 PR = 0。
- 最新独立机器证据继续采用上一云端实跑：`1694 passed in 109.69s`、新测试 18 passed、父提交 7 failed/11 passed、`check_*.py` 8/8、dash render exit 0、selftest 68/6/8-8；**本轮沙箱未重新跑完整 pytest**。

### 本轮新确认

1. **`IT-P1-OBS-EMPTY-ROUND-001`（P1）**：`Engine.poll_once()` 在 stock/index 都返回空且不抛异常时，于 `RoundObservationSet`、`_poll_count += 1`、`set_poll_stats(... observation=...)` 之前直接 `return []`。因此请求真实发生但本轮 observation 不存在，`observation_seq` 不增长。
2. **`IT-P1-SOAK-CUMULATIVE-QUOTES-001`（P1）**：`live_session._soak_loop()` 仍把 `len(state.quotes)`（latest cumulative cache）写成逐轮 `quotes`。结合上条，连续 silent-empty round 可以让 `fetch/data/coverage` 同时假绿。
3. **`R-21 / IT-P1-UNIVERSE-HEALTH-GATE-001`（P1）**：沿用上一云端真实证据，setup 已有 `universe_size/fell_back`，只缺 health consumer；scratch patch 已验证。
4. **`IT-P1-UNIVERSE-REFRESH-STORM-001`（P1）**：smaller partial candidate 被拒后不推进 retry/attempt 时钟；TTL 过期后每个 5 秒 poll 都可能重复 universe refresh。
5. `IT-P2-ACK-RECENCY-001`、`IT-P2-UNIVERSE-STATUS-CACHE-001`、Delivery D2/D1 继续开放。

### 本轮机制实验

silent provider `[]`、不抛异常、latest cache 保留旧值：

| 方案 | fetch | data | coverage | healthy |
|---|---|---|---|---|
| 当前 | PASS | PASS | PASS/skip | **True** |
| 只补 zero-return observation | PASS | PASS | FAIL | False |
| 完整 Round Truth（observation + per-round quotes） | FAIL | FAIL | FAIL | False |

因此主改造冻结为 **Round Truth Contract v1**：真正 dispatch 的 poll attempt 必须有明确终态；zero-return 必须产生 `requested>0 / returned=0 / coverage=0` 的 observation，latest cache 不能充当 current-round 数据。

### 下一步顺序

1. zero-return Round Observation 红/绿/回退验牙；
2. live-session 本轮 `quotes` 改为 observation returned/admitted；
3. 落地已经 scratch 证明的 Universe health gate；
4. partial-universe retry/backoff；
5. ACK 有序裁剪与 Delivery 脏输入硬化；
6. UniverseTruth → per-code provenance → targeted fallback → ObservationInterval/SSE；
7. Truth contracts 与真实多日标签齐全后再恢复模型。

---

## 历史索引原文（完整保留）

**最新云端独立审计**：[`2026-09-22_00-43-31_JST.md`](./2026-09-22_00-43-31_JST.md)  
**最新本地 Agent 产品轮**：[`2026-09-22_01-00-00_JST.md`](./2026-09-22_01-00-00_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-22_00-08-36_JST_AGENT_TASK.md`](./2026-09-22_00-08-36_JST_AGENT_TASK.md)  
**上一份云端独立审计**：[`2026-09-22_00-08-36_JST.md`](./2026-09-22_00-08-36_JST.md)  
**上一份本地 Agent 产品轮**：[`2026-09-21_21-00-00_JST.md`](./2026-09-21_21-00-00_JST.md)  
**更早云端独立审计**：[`2026-09-21_21-56-56_JST.md`](./2026-09-21_21-56-56_JST.md)  
**更早云端独立审计**：[`2026-09-21_21-43-27_JST.md`](./2026-09-21_21-43-27_JST.md)  
## 2026-09-22 00:43:31 JST 云端独立审计：`e57bb3b` 三条修复全部成立，但同一 bug 类仍有存活实例

- `reviewed_source_sha` = `e57bb3b87baacefd4e8abd908a5bc17877179ebf`（= 当时 HEAD，实际 `git rev-parse` 读出）；父提交 `eca980ec6f1c8c01d6857842f9b6f084ffb12e7d`
- 待审范围 `fd516745..origin/main` 共 4 提交，**唯一含产品源码的是 `e57bb3b`**
- 本轮真实命令：全量 `1694 passed in 109.69s`；新测试 18 passed；**新测试跑在父提交未打补丁源码上 = 7 failed / 11 passed（全 AssertionError，0 结构性，无 ImportError）**；`check_*.py` 8/8；`node dash_render_check.js` exit 0；`selftest 68/6 类型/8-8`

### 三条修复独立复现（**[`2026-09-22_00-43-31_JST.md`](./2026-09-22_00-43-31_JST.md)** §2）

| ID | 结论 | 关键实测 |
|---|---|---|
| `IT-H20-DELIVERY-ACCOUNTING-GATE-MISSING` | **已经修复**（真修，非空转） | 父提交注入坏账本 → 10 项**逐字节不变** `healthy=True/exit 0`；HEAD → 11 项、`healthy=False/exit 1/fail=['delivery_accounting']`。新测试在未打补丁源码上 **7 failed** ⇒ 检测力成立 |
| `IT-P1-UNIVERSE-WATCHLIST-PIN-001` | **已经修复** | 降级后 `_codes_pinned` `True→False`；源恢复 + TTL 过期后 `refresh_universe` 调用 **0 → 1**，`_codes` 回到 3000。正当 pin（`--watch-only`/replay）未被破坏 |
| `IT-P1-ALERT-TOTAL-KIND-TRUNCATION-001` | **核心已修 + 退化分支回归** | 450 条 `limit_up`：真值 450 / 旧口径 300 → 修复后 450。但 `status()` 抛异常时 NEW=**100** 而 OLD=300（§2.3） |

### 🔴 本轮最高价值：同一 bug 类**仍有存活实例**（产品轮只修到第 2 个）

| 位置 | 机制 | 影响 |
|---|---|---|
| `tools/live_session.py:1971` | `quotes=len(state.quotes)` —— `state.quotes` 是**从不裁剪**的累计缓存（唯一写入 `engine.py:302`；`prune() :417-446` **不清它**） | 实测 5 轮全断供后 `sample['quotes']` **恒为 5913**、`no_data_rounds=0`、`data` 项仍报"最多拿到 5913 只" ⇒ **两个断供探测器同时失效** |
| `src/arad/store.py:359` | `set(list(self._acked)[-2000:])` —— `list(set)` 是**哈希序**，保留**任意** 2000 而非**最新** 2000 | 用户刚 ack 的告警仍留在 ring buffer 内却报 `acked=False`（看板 `dashboard.html:474` 变暗并隐藏 ack 按钮）。5 个哈希种子下全部失败；全仓**无测试**覆盖该阈值 |
| `src/arad/store.py:304` | `"universe": len(quotes)` 同样用累计缓存 | 看板"股票池"芯片在扫描范围缩 30% 时**高报 1813** |

### `R-21`（universe 无判决项）→ **已确认错误**，根因比原报告更精确，且**生产可达**

- 根因不是"缺判决项"，而是 **`run()` 已在 `:2056-2058/:2064` 采到 `universe_size`/`fell_back`，并在 `:2174-2179` 装进 `setup` 传给 `finalize_metrics` —— 但 `evaluate_health` 从不读 `setup`**。即"**缺一个消费者，不缺生产者**"，无需新增采集/改引擎/联网。
- **可达性（用真实 `Engine.refresh_universe`，未 monkeypatch 逻辑）**：`engine.py:968-969` 的守卫 `prev = len(self._codes_raw); if prev and n < prev` 在**会话首次刷新 `prev==0`** 时为假 ⇒ 冷启动 + `complete=False` 的 4100/5913 部分池**被采纳**（`_universe_meta` 如实记 `shortfall=1813` 却**零生产读者**）；热池 5913 时同一输入**被正确拒绝**。
- **可落地补丁（scratch 验证，未写入仓库）**：加一个 `universe` 判决项，基准用**本会话自身观测上限** `quotes.max`（`DEFAULT_TOLERANCES` 里**没有** `min_universe`，故不发明绝对阈值）。5900→4100 判失败；边界 `0.70 x 5913 = 4139.1` 精确（4139 失败 / 4140 通过）；`universe_size=0` 视为未测得而跳过；无 `setup`/`setup` 非 dict 容错。**补丁后 34 passed（18+16）零回归。**
- 阈值只能经 `evaluate_health(metrics, tolerances={...})` **参数**注入；写成 `metrics["tolerance"]` 会被**静默忽略**。

### 新加判决块自身的两处残留缺陷（§2.4）

- **D2（`修复后回归`，P2）**：`:1323 _dl_bad = _dl.get("inconsistent_rounds") or []` → `:1329 list(_dl_bad)[:5]`。走到失败分支且该值为真值标量（`int`/`float`/`bool`）时 `list()` 抛 `TypeError`。实测 **4 个用例 HEAD 崩溃而父提交 `healthy=True/exit 0`** ⇒ **对父提交的回归**，且违反该块自己 `:1310-1311` 写下的"不崩"契约。仓库自带脏输入用例只覆盖 `{}`/`None`/`str`，**独缺标量** ⇒ 18 条仍全绿。shipped producer 恒发 `list`，故仅手写/外部 metrics 触发。
- **D1（`待验证风险`，P2，纵深防御）**：`:1326` 判据仅 `status=="inconsistent" or errors>0`；`:1324-1325` 的 `_dl_total`/`_dl_named` **只用于 detail 文案**，**从不复算 `named <= total`**。实测 `total=66/named=1000` 且 `status="ok"` ⇒ `healthy=True`。当前不可达（生产者 `capabilities.py:813-815` 确实显式判 `named > total`），但这是 §2.1 那条认识没有贯彻到底 —— 生产者 label 一旦退化，判决层会同时失效。

### 其余复核

- `R-22` `_scope_note` 死键：**成立**（唯一定义 `capabilities.py:764`，唯一读者是一个测试；且它指向的 `delivery_session` **全仓不存在**）。
- `R-23` 无任何 CI：**成立**（228 tracked 文件；`.github/`、任何 CI yml、Jenkinsfile/Makefile/tox 均无）。`docs/audits/validate_latest.py` **不存在** ⇒ 手册步骤 5 为条件动作，本轮**不伪造 PASS**。
- `R-12` `_universe_meta` 零出口：**成立**（写入 `engine.py:961/:980`，读取仅同模块 `:877` + 一个测试）。

### 口径

真实实股结果：**本轮没有新增实股结果**。全部证据为软件样本（合成夹具 + 真实代码路径）；`5913/4100` 是机制夹具，**不是**真实市场覆盖率测量。
`程序修复` 与 `任务定义变更` 分开写在报告 §8；`真实模型增益：无`。

---

**下一步计划**：[`NEXT_STEPS.md`](./NEXT_STEPS.md)  
**仓库执行清单**：[`RUN_MANIFEST.json`](./RUN_MANIFEST.json)

> 历史审计文件全部保留在本目录；本索引仅移动接续指针，不删除或覆盖历史报告。

## 2026-09-22 01:00 JST 本地 Agent 产品轮（假绿从数据层搬到判决层）

- 起点 HEAD：`eca980ec6f1c8c01d6857842f9b6f084ffb12e7d`；`reviewed_source_sha` = `fd51674508cb237c844d6a4924b7e6a2f084c4b6`（我 21:00 轮推的提交，正是云端 00:08 轮审的那个）。
- 归档机器证据：**1694 passed in 98.02s**（上轮 1676 → +18）；`check_*.py` **8/8**；`check_bom` 通过；`dash_render_check.js` 通过；`selftest` **68 / 6 类型 / 8-8**。
- 回退验牙：**7 条行为级 RED / 0 结构性，无 ImportError**。

### ⚠ 核心：我上一轮修的"假绿"**没有被消除 —— 它从数据层搬到了判决层**

云端 00:08 轮 + addendum `21-56-56` 用真实实验证明，我独立复现确认：

| 场景 | healthy | exit_code | 10 个判决项与基线逐字节相同？ |
|---|---:|---:|---|
| 健康基线（账本 `ok`） | `True` | `0` | — |
| 注入 `inconsistent`/`errors=7`/`total=0`/`named=66` | **`True`** | **`0`** | **是** |

**根因**：`evaluate_health` 的 10 个判决项是
`rounds / fetch / data / coverage / capability / api / sse / memory / browser / alerts`
—— **没有任何一项读 `delivery_accounting_session`**。数据层修好了（`accounting_status` 如实报出损坏），**但没人读**。

**这是同一个缺陷的第二次搬家**：上一轮我把它从"规则计数"搬到"数据层"，这一轮它从"数据层"搬到"判决层"。
教训：**修一个可观测性缺陷，必须一路走到"有人会因此改变结论"为止；停在"字段有了"就是没修。**

修复后同一注入：`healthy=False` / `exit_code=1` / `fail=['delivery_accounting']` / 判决项 11 项。

三个设计决定（我逐条复核后采纳）：`not_measured` 判 **ok**（实测仓库四个"应当判健康"的 helper 产出的都是 `not_measured`，判红会让所有绿色用例转红）；判据**不看门禁**（门禁被 `max(...,0)` 夹过，分母被吞时它也是 0 —— 这正是假绿成因），必须看 `accounting_status`/`accounting_errors`；脏值不崩。

### `IT-P1-UNIVERSE-WATCHLIST-PIN-001`：一次**临时**降级变**永久**

`engine.py:1043` 的降级分支复用 `_codes` setter（`_codes_pinned = bool(codes)`），于是：

| 步骤 | `_codes` | `_codes_pinned` | `refresh_universe` 调用次数 |
|---|---|---:|---:|
| 源端失败 → 降级 | `['000001','000002']` | **`True`** | — |
| 源端恢复 + TTL 过期 | 仍 2 只 | `True` | **0** |

**用户会在毫无后续提示的情况下永久只盯自选股那几只票**（降级警告只出现一次，之后系统一直"正常运行"）。
修复：降级走 `_codes_raw` 直赋（**不 pin**）。对照：`--watch-only` 与 replay 用 setter 是**对的**，已加回归测试钉住。

### `IT-P1-ALERT-TOTAL-KIND-TRUNCATION-001`（**本轮我自己复核发现**，云端未提）

`web.py:603` 带 `kind` 过滤时用 `len(recent_alerts(1000, kind))`，而 Store 是 `deque(maxlen=300)`：

| 口径 | 值 |
|---|---:|
| 灌入 `limit_up` | 450 |
| `alerts_total()`（真值） | **450** |
| `len(recent_alerts(1000,'limit_up'))`（旧口径） | **300** |

→ `/api/alerts?kind=...` 的 total **永远 ≤ 300**；而**不带** kind 走 `status()['alerts_total']` 却正确。
**同一个展示字段两条路径语义不一致。** 与 21:00 修的 `RINGBUFFER-001` **同根因**，是同一 bug 类的第二个实例。
修复：改用 `status()['by_kind'][kind]`（累计，不随驱逐减少）。

### ⬇ 本轮对上一轮结论的两处下调

1. 21:00 轮宣称"假绿已修"—— **下调**：当时只修到数据层，判决层完全不读，用户可见的 `healthy/exit_code` 逐字节不变。**真正的修复是本轮。**
2. 21:00 轮把 `RINGBUFFER-001` 记为"已修"—— **下调为"修了我发现的那一处"**；同根因的 `web.py:603` 当时没查到。**同一个 bug 类我漏了第二个实例。**

### 🔴 本轮新增最高优先：`R-21` —— **universe 仍然没有判决项**

§1.1 只给 **delivery** 加了判决项，**universe 仍然没有**。
也就是说：**扫描范围从 5913 掉到 4100（缺 30%）时，`healthy` 照样是 `True` / `exit 0`。**
这与我本轮修好的是**同一类假绿**，只是对象不同 —— 而且我**已经知道它存在**。
如实列为下一轮最高优先，**不假装它不存在**。

### 其余新增风险

* `R-22`：`_scope_note` 是**死键**（全仓只有定义处 + 我自己的测试读它，无生产消费方）—— 与 17:40 轮对 `check_delivery_invariants` 的指控同类。
* `R-23`：仓库**没有任何 CI**（无 `.github/` 等，已实测确认）。所有测试/门禁全靠人工纪律。**在硬性边界内我不能改，只如实报告。**

### `taskkill` 副作用（沿用上轮）

按纪律第 5 步跑性能测试前执行 `taskkill /F /IM python.exe`，会同时杀掉 8899/8905 守护进程。已核对均未监听，未重启。

---

## 2026-09-22 00:08 JST 云端审计：Universe Truth 进一步收敛为 Recovery State

- `reviewed_source_sha`: `fd51674508cb237c844d6a4924b7e6a2f084c4b6`
- 审计开始 `main`: `f6efd5fc2e32a0be2a00c85fbada9251f0ac5078`
- `fd516745 -> f6efd5fc` 仅有 docs-only 提交；本轮固定产品版本不变。
- 当前开放 PR：0。
- 沙箱完整 checkout：`blocked`（容器 DNS 无法解析 `github.com`）；GitHub connector 固定 SHA 读取正常。
- 本轮模型训练：0；真实盘中网络、真实通知、交易：均未启动。

### 新确认 P1

1. **`IT-P1-UNIVERSE-WATCHLIST-PIN-001`**：normal 模式全市场源失败后，watchlist fallback 通过通用 `_codes` setter 写入，导致 `_codes_pinned=True`；后续 `_maybe_refresh_universe(force=False)` 永久短路。机制 fixture：源 60 秒后恢复完整 5913，当前控制流仍停在 10 只 watchlist；候选 recoverable fallback 可自动恢复 full。
2. **`IT-P1-UNIVERSE-REFRESH-STORM-001`**：已有 full pool 过 TTL 后，smaller partial candidate 被正确拒绝，但拒绝分支不推进任何 last-attempt/retry 时钟；每个 5 秒 poll 都再次刷新 universe。固定 10 分钟/120 轮机制 fixture：当前 120 次 refresh；候选 60 秒 degraded retry 为 10 次。60 秒仅是起始候选配置，不是实盘最优值。

两条问题共同说明：当前 `_codes_pinned` 混淆“显式固定”与“临时降级”，`_universe_refreshed_at` 又混淆“最后完整成功”与“最后尝试”。主改造升级为 **Universe Truth & Recovery Contract v2**：`unknown/full/partial_degraded/retained_previous/watchlist_fallback/explicit_pinned` 六状态，并拆 `last_attempt_at / last_applied_at / last_complete_at` 三个时钟。

### 继续开放

- **`IT-H20-DELIVERY-ACCOUNTING-GATE-MISSING`**：上一云端 addendum 已真实证明判决层假绿，并给出 scratch patch：原产品新测试 `2 failed / 3 passed`，补丁后 `5 passed`，全套 `1681 passed`。当前产品仍未接入；下一 Agent 轮应先以小提交落地并回退验牙。
- **`IT-P1-UNIVERSE-COVERAGE-001`**：round `returned/requested` 只度量 active pool 内返回率，不是市场覆盖率。
- **`IT-P2-UNIVERSE-STATUS-CACHE-001`**：`/api/status.universe = len(state.quotes)` 是 latest quote cache 口径，不是 active scan universe。
- `IT-P1-CAPABILITY-002`、`IT-P1-SOURCE-EMPTY-001`、`IT-P1-WINDOW-001`、SSE durable cursor/gap 与 bounded state 继续开放。

### 证据纪律

- 旧 `73.0% / 83.1%` 股票池覆盖率已被本地 Agent 自我更正为不可复算/INVALID，本轮没有引用为事实。
- 本轮 `5913 / 4100` 仅是 synthetic mechanism fixture，不是重新测得的真实市场覆盖率。
- 下次真实覆盖测量必须落 `source/raw hash/expected_total/raw_unique_codes/usable_quotes/active_scan_codes/timestamp/command`，可独立重算。

### 下一轮优先顺序

1. 落地已经证明可行的 H20 delivery verdict/print 小补丁；
2. normal watchlist fallback 不得 explicit pin，增加真实 Engine 红测；
3. smaller-partial reject 使用独立 degraded retry/backoff；
4. UniverseTruth schema 接 Store/status/live-session；
5. 本地授权环境重新测真实 universe coverage；
6. per-code provenance/capability → targeted soft-partial fallback → ObservationInterval → SSE durable delivery；
7. UniverseTruth 与真实多日标签未闭合前，不扩 TCN/Transformer。

### 下一轮必须核查的实物

`wp01_delivery_gate_{red,green,rollback}.log`、`universe_watchlist_recovery_{red,green,rollback}.log`、`universe_partial_backoff_{red,green,rollback}.log`、`universe_recovery_cases.json`、`universe_truth_cases.json`、真实测量时的 `universe_coverage_reconcile.json`、完整 pytest/check/dashboard 日志、`RUN_MANIFEST.json`、`NEXT_STEPS.md`。

## 关键历史接续

- [`2026-09-21_21-56-56_JST.md`](./2026-09-21_21-56-56_JST.md)：确认 Delivery accounting 在 `evaluate_health` 判决层假绿；scratch patch 已证明零回归。
- [`2026-09-21_21-43-27_JST.md`](./2026-09-21_21-43-27_JST.md)：发现新 Delivery Ledger producer 与 verdict/print consumer 断线。
- [`2026-09-21_21-00-00_JST.md`](./2026-09-21_21-00-00_JST.md)：产品轮 `fd516745...`，修 Delivery false-green 产品层、ring-buffer 分母、soak producer、signal_id roundtrip 等；归档 1676 passed。
- [`2026-09-21_20-10-37_JST.md`](./2026-09-21_20-10-37_JST.md)：把 Universe denominator 提升为研究主瓶颈；旧实盘覆盖百分比后续已作废。
- [`2026-09-21_17-40-00_JST.md`](./2026-09-21_17-40-00_JST.md)：独立复核 Delivery Ledger。
- [`2026-09-21_17-00-00_JST.md`](./2026-09-21_17-00-00_JST.md)：产品轮，Evaluability / Delivery 分账。
- 更早报告继续按本目录时间戳文件追溯，均未删除。