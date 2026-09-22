# 最新审计

## 2026-09-22 10:05:00 JST 云端审计补充（12 个开放项逐项回归：**9 已修 / 3 仍存活**，且旧清单已陈旧）

[2026-09-22_10-05-00_JST.md](2026-09-22_10-05-00_JST.md)

- `reviewed_source_sha` = **`369b13f0d6f20fc22198abf51469ee14e49c84dc`**；`extends` 09:49 报告。
- 子 agent 在自建 `git archive` scratch 副本内实跑（**237/237 文件** `git hash-object` 逐字节等于 `origin/main`；
  活仓库 `git status --porcelain` = **0 行**）：全量 `pytest -o addopts= -ra` → **1745 passed in 95.98s，exit 0**；
  **14 个探针全部 exit 0**；**0 个 NOT_RUN**。
- 结果：**FIXED 9 / CONFIRMED_STILL_PRESENT 3 / NOT_REPRODUCED 0 / COULD_NOT_TEST 0**。12 个标识符在文档中**全部存在**。

### ⚠ 旧清单陈旧（**本身是一条发现·P2**）
`2026-09-22_09-00-00_JST.md:287-291` 的"既有未决项全部保留"块仍列
**`IT-P0-001`**、**`IT-P1-LIMIT-001`** 为开放 —— 但二者**早已修复**，本轮独立复现确认
（`session.py:46,87,156-160`；`limit_board.py:210-221`）。从该块构建回归清单会**重新打开 2 个已关闭项**。

### 🔴 真正仍存活的 **3** 项（每项都比文档描述的**更窄**）
| ID | file:line | 真实反例 | 边界 |
|---|---|---|---|
| `IT-P0-002` | `src/arad/session.py:214`；`sources/eastmoney.py:149` | 同一真实时刻：CST 宿主 → `phase=morning`，UTC 宿主 → `phase=closed`。`git grep zoneinfo src/` = **rc1/0 命中**；`app.timezone` **0 读者** | **仅剩时区分句**；window≤now 与 write-ahead admission **已修**。降为 **P1**，非 P0 |
| `IT-P1-SOURCE-EMPTY-001` | `src/arad/engine.py:547-589` | 主源返回 `[]` **且不抛异常** → `backup.calls=0`、`fails=stocks`；**正对照**：抛异常 → `backup.calls=1` | 故障转移**只由异常驱动**（`:567`）。**已不再是假绿**（轮次记账会暴露缺口） |
| `IT-P1-WINDOW-001` | `src/arad/engine.py:313`；`rules/tick_surge.py:198` | 13 条"新鲜但价格不变"观测**塌缩成 2 点**；`_covered(60s)=True` 而同刻 `price_change(60s)=None`（真实 +3.0%） | history 仍 change-only；`ObservationInterval` **在 `src/` 零实现** |

### ✅ 已修 9 项（独立复现，非引用仓库自测）
`IT-P0-002-R2`（`engine.py:316-318`；**变异测试**：改回旧写法 → **7 failed/5 passed**，还原 → **12 passed**，文件逐字节复原）、
`IT-P0-001`、`IT-P1-LIMIT-001`、`IT-P2-OBS-001`、`IT-P1-UNIVERSE-DENOMINATOR-001`(R-12，矩阵逐行复现：76.10% → **fail**)、
`IT-P2-OBS-EMPTY-ROUND-R1`（`index_requested` 幻影消失，`poll_count=[1..6]` 单调）、
`IT-P2-UNIVERSE-STATUS-CACHE-001`、`IT-P1-UNIVERSE-HEALTH-GATE-001`(R-21)、
`IT-P1-SOAK-CUMULATIVE-QUOTES-001`（`no_data_rounds=5`，原为完整假绿）、`IT-P1-ACK-TRIM-ORDER-001`（`_acked` 改 dict）。

### 我修正了子 agent 的一处措辞错误
它称 `ObservationInterval` **"genuinely absent（`git grep` exit 1）"**。我复核：
`git grep -n ObservationInterval origin/main` → **rc=0，58 处命中**，但**全部在 `docs/audits/` 散文**，
`-- src/` → **rc=1，0 命中**。⇒ 实质结论成立，措辞不准确。**已改用**："在 `src/` 零实现，仅作为 58 处前瞻性设计散文存在"。
**方法学教训**：报"某符号不存在"**必须写明搜索路径**，限定与不限定会得出相反结论。

### 三轴与分级
`execution_status`：14 探针 exit 0 + pytest exit 0 + 变异 exit1/exit0；活仓库零改动。
`research_verdict`：`9_OPEN_ITEMS_FIXED` / `3_STILL_PRESENT_AND_NARROWER` / `PREVIOUS_OPEN_ITEM_LIST_IS_STALE` / `OBSERVATIONINTERVAL_ABSENT_FROM_SRC_NOT_FROM_REPO`。
`evidence_status`：`VERIFIED_BY_INDEPENDENT_AGENT_AND_RECOMPUTED_BY_MAIN_AGENT`。
`evidence_type`：**软件样本**（1745 passed / 14 探针 / 变异计数）；**实股结果无新增**，
真实 Precision/Recall/漏报率/收益 = `unavailable`。
**程序修复 0 条新增（本文件是核对）／任务定义变更 0／真实模型增益 0**。
`report_created_commit_sha`: `PENDING_READBACK`。


## 2026-09-22 09:49:38 JST 云端独立审计（新增：`R-12` 修好了"分母已知"那一半，但新判决项在"分母随数据一起丢失"时仍判绿）

[2026-09-22_09-49-38_JST.md](2026-09-22_09-49-38_JST.md)

- `reviewed_source_sha` = **`369b13f0d6f20fc22198abf51469ee14e49c84dc`**
  （`fix(universe): R-12 分母进入事实链 + 空轮账本对账（WP01 / WP04）`）。
  **该产品提交此前未被任何报告审过** —— 已提交的 `2026-09-22_09-00-00_JST.md` 自述
  `reviewed_source_sha = 2406d81`，即它审的是 05:00 那版。
- 测试真数（我在 `git archive` 导出的 scratch 双树内实跑，**未**在活仓库跑）：
  BASE `2406d818` → **1720 passed in 105.36s**；FIX `369b13f0` → **1745 passed in 93.49s**（+25 = 新增测试文件）。
  活仓库 `git status --porcelain` = **0 行**。
- **验牙成立**：新测试跑在 **BASE 源码** 上 → **exit 1，15 failed / 10 passed，0 个 ImportError**（全行为级 RED）；
  同测试跑在 FIX 上 → **25 passed**。

### ✅ `R-12` 的"分母已知"那一半：**已经修复**（我独立复现）
用仓库自带绿基线 `_healthy_metrics()`，只改 `universe_truth`：
`4500/5913 = 76.10%` 由 `healthy=True/exit=0/fail=[]` → **`healthy=False/exit=1/fail=['universe_coverage']`**；
`4100/4200 = 97.62%` 仍绿 —— 同一绝对数、不同分母**判得不一样**了。修前两者取值完全相同。

### 🔴 `IT-P1-UNIVERSE-COVERAGE-GATE-TRUNCATION-BLIND-001`〔**已确认错误·P1**〕
`tools/live_session.py:1419-1478` 新增的 `universe_coverage` 判决块**只**读
`denominator_kind/expected_total/active_scan_codes` 与三个 coverage。
我在 `evaluate_health` 函数体（`1044..1550`）内实测：
**`transport_complete` 出现 0 次、`shortfall` 0 次、`denominator_known` 0 次** ——
尽管 `Engine.universe_truth()`（`src/arad/engine.py:967`）**已经算出**它们。
于是 `:1449 if _exp <= 0 or _cov_a_f is None:` 把"provider **明确承认**没给全"
与"翻页翻完但无总数"归成同一支，判 `level="ok"`。
**实跑**（真实 `Engine` + 假 provider）：截断到 5000 只且 provider 报 `transport_complete=False`
→ `healthy=True exit=0 fail=[]`，`universe_coverage level=ok`。4600 只同样绿。
⇒ provider **主动告知、不需要分母**就能判定的硬事实被降级成"未测量"。
**边界**：`:1417-1418` 注释与上一轮报告 §6 想法 2 **明确**把"分母未知→ok/未测量"写成故意设计决定，
故这是**设计选择的代价**，不是笔误。
**修复**：该分支增加先于 `_exp <= 0` 的判断 —— `transport_complete is False` → `fail`。
**验收**：喂入 `{"denominator_kind":"unknown","expected_total":0,"active_scan_codes":5000,
"coverage_active":None,"transport_complete":False}`，断言 `healthy is False`。当前实测 `True`，测试会红。

### 🔴 `IT-P1-UNIVERSE-META-STALE-AFTER-FAILED-REFRESH-001`〔**已确认错误·P1**〕
`src/arad/engine.py:1138-1140`：`refresh_universe()` 全源失败时 `return 0`，**不重置 `_universe_meta`**；
只有成功路径（`:1114` / `:1133`）写它。`universe_truth()` 读它且**无任何新鲜度字段**。
**实跑**（注入假源，`fake calls` 计数证明未走网络）：健康刷新后令 provider 全部抛异常 →
`refresh_universe() -> 0`，但 `universe_truth()` 仍报
`denominator_kind='provider_declared_total' expected_total=5913 active=5913 coverage_active=1.0`
（与断供前**逐字段相同**），`evaluate_health` 打印 **"全市场覆盖 5913/5913 = 100.00%"** 且 `healthy=True exit=0`。
**影响面（精确）**：`src/arad/store.py:310-316` 的 `status()['universe_truth']` 是**每次调用实时**取值，
故**实时状态出口**在断供期间会显示 100.00%。
**边界**：会话**末尾**的 health 用的是 `live_session.py:2284-2289` 的**循环前快照**，
故本条**不**直接污染最终判决，只污染实时出口 —— 不夸大。
与 h=20 轮修掉的"判决层读旧账本"**同一 bug 类**，换了对象。
**修复**：全失败分支清空或标注 `_universe_meta`（如 `_universe_meta_stale_at`），
`universe_truth()` 输出 `stale`/`age_s`。顺带让 `engine.py:976` 那三个**死键**有真实 producer。

### ⚠ 与上一轮自查**一致**、故**不计为新发现**
三个恢复时钟 `last_attempt_at/last_applied_at/last_complete_at` 在全仓只出现 **2 处**
（`engine.py:976` 的**读**循环 + 我的探针），两个 producer（`engine.py:1030-1048`、
`eastmoney.py:479-503`）**都不写**它们 ⇒ 循环体从不执行，实测恒为 `None`。
**上一轮 §7 已自行披露**此事，我按纪律**降级而非删除**，仅作为上方修复建议的落点。

### ⏳ `IT-P1-HEALTH-COVERAGE-SNAPSHOT-PRELOOP-001`〔**待验证风险·P2**〕
`universe_truth` 仅在 `live_session.py:2284-2289`（进 soak 循环**前**）取一次快照，
而循环内 `poll_once(force=True)`（`:2130`）会按 TTL(=`config/settings.yaml:22` 的 1800s)
经 `engine.py:1244` 重刷。故判决用的是**起始时刻**的覆盖。**我未跑真实多轮 soak 量化**，故只列待验证。

### 未复现 / 已降级
`IT-P1-UNIVERSE-REFRESH-STORM-001`：我只证明 TTL 到期会重刷，**未**测得风暴式调用频率 → **未复现，降级**。

### 三轴与分类
`execution_status`：审 `369b13f0`；双树全量 pytest ×2、定向 ×2、判决矩阵 ×1、真实 Engine 断供 ×1；**活仓库零改动**。
`research_verdict`：`R12_KNOWN_DENOMINATOR_FIX_CONFIRMED` / `...TRUNCATION_BLIND` / `...META_STALE` / `...PRELOOP`。
`evidence_status`：`VERIFIED` / `VERIFIED_STATIC_ONLY`（P2）/ `NOT_REPRODUCED_AND_DOWNGRADED`。
`evidence_type`：**软件样本**（1720/1745/15 RED/0 ImportError）；**实股结果：无新增**，真实
Precision/Recall/漏报率/收益 = `unavailable`；本轮 Engine 实验全用**假 provider**，非实股证据。
**程序修复 2 条（未实施）／任务定义变更 1 条建议／真实模型增益 0**。
**边界**：该仓库**无** `docs/audits/validate_latest.py`（实测不存在），故**不声称**通过验证器门禁。
`report_created_commit_sha`: `b4084c93c8497f5ff0688845d0e48d164835b854`。


**最新本地 Agent 产品轮**：[`2026-09-22_09-00-00_JST.md`](./2026-09-22_09-00-00_JST.md)  
**最新云端独立审计**：[`2026-09-22_08-08-19_JST.md`](./2026-09-22_08-08-19_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-22_08-08-19_JST_AGENT_TASK.md`](./2026-09-22_08-08-19_JST_AGENT_TASK.md)  
**上一份本地 Agent 产品轮**：[`2026-09-22_05-00-00_JST.md`](./2026-09-22_05-00-00_JST.md)  
**上一份云端独立审计**：[`2026-09-22_04-05-26_JST.md`](./2026-09-22_04-05-26_JST.md)  
**上一份云端 Agent 任务书**：[`2026-09-22_04-05-26_JST_AGENT_TASK.md`](./2026-09-22_04-05-26_JST_AGENT_TASK.md)  

## 2026-09-22 09:00 JST 本地 Agent 产品轮（R-12 分母落地：76.10% 不再判 OK）

- 起点 HEAD：`2406d818168da9e642e73b73b254762050e229e5`；`reviewed_source_sha` = `2406d818`（我 05:00 轮推的提交，正是云端 08:08 轮审的那个）。
- 归档机器证据：**1745 passed in 83.43s**（上轮 1720 → +25）；`check_*.py` **8/8**；`check_bom` 通过；`dash_render_check.js` 通过；`selftest` **68 / 6 类型 / 8-8**。
- 回退验牙：**15 条行为级 RED / 0 结构性，无 ImportError**。

### 🔴 `R-12` 闭合"分母已知"那一半 —— 我追了四轮的最高优先项

云端 08:08 轮用**固定门禁、只改 provider 声明分母**的确定性实验证明（我独立复现）：

```
expected | active | active cov | 修前判决 | 修后判决
    4200 |   4100 |     97.62% |   warn   |   ok
    5913 |   4100 |     69.34% |   warn   |  fail
    5913 |   4500 |     76.10% |  **ok**  |  fail   <-- 缺 24% 反而"更好看"
    5913 |   5850 |     98.93% |   ok     |   ok
```

**关键**：只要分母未知，`4500/5913 = 76.10%` 被判 **OK**，而它与
`4100/4200 = 97.62%` 在 health 层**取值完全相同**（都只是 `universe_size`）。
**这不是"阈值再调一下"，是"分母根本不在事实链上"。**

而 provider **早就算好了**分母：Eastmoney `universe_info()` 返回
`transport_expected_total / raw_unique_codes / usable_quotes / shortfall /
usable_coverage`，Engine 也存进 `self._universe_meta`。**但它零出口** ——
`git grep` 实证：全仓只有写入点与测试读它。

**修复（一条事实链，三处出口）**
1. 新 `Engine.universe_truth()`：分母 + **四个分开的** coverage
   （`coverage_transport` / `coverage_usable` / `coverage_active`）
   + `denominator_kind` 三态（`provider_declared_total` /
   `pagination_exhausted_non_numeric` / `unknown`）。**分母未知返回 `None`，不冒充已知。**
2. `store.status()['universe_truth']`：`_universe_meta` 的**第一个生产出口**。
3. `evaluate_health` 新增 `universe_coverage` 判决项：`<90%` **fail**、
   `90–95%` warn、`>=95%` ok、分母未知 **未测量判 ok**。

**两条设计纪律（都有实测依据）**
- 判据用 **`coverage_active`**，**不是** `coverage`（那是 `C_round`）。
  实测 `C_round=100%` + `C_active=76.10%` → **仍然转红**，
  即 **`C_round` 不能替代 `C_active`**（云端 §8.3）。
- **成员集 ≠ 可用 Quote 数**（云端 §8.2）：transport 拿到 100 码、
  94 个能构造 Quote 时，`active_scan_codes` 必须仍是 **100** 而非 94 ——
  否则"市场里有停牌股"会被读成"扫描范围缩了"。

### ⚠ 云端在我**上一轮自己新写的**空轮分支里找到 2 条残留（WP04）

| 缺陷 | 修前 | 修后 |
|---|---|---|
| **幻影** `index_requested`（`spirit_index` 关闭仍报） | **5** | **0** |
| 同轮 `requested` | **505** | **500** |
| 5 个空轮的 `observation_poll_count` | **1,1,1,1,1** | **1,2,3,4,5** |

- **幻影**：`_wants_indices=False` 时 `_fetch_indices()` **根本不 dispatch**，
  我的分支却写 `index_requested = len(self.index_codes)` ——
  观测层**无法区分**"指数抓了没回来"与"根本没抓"（后者不是数据事故）。
  **正常路径早在 `IT-P2-OBS-008` 就修过这个语义，我新增分支时又写坏了一次。**
- **poll identity 断裂**：我的 `return []` 早于 `self._poll_count += 1`，
  于是 `observation_seq` 涨而 `poll_count` 冻结 —— 制造了一批**无法归属的观测**。
- **修复 = 收口到唯一入口**：新 `_request_arithmetic()` 与 `_bump_poll_count()`，
  正常路径与空轮分支**都调**（有结构测试断言 `poll_once` 里不再出现内联自增）。

### ✅ 云端确认我上一轮 5 条修复**全部成立**

`IT-P1-OBS-EMPTY-ROUND-001`、`IT-P1-SOAK-CUMULATIVE-QUOTES-001`、
`IT-P1-ACK-TRIM-ORDER-001`、`IT-P2-UNIVERSE-STATUS-CACHE-001`、
`R-21 / IT-P1-UNIVERSE-HEALTH-GATE-001` —— 本轮**全部回归通过**，不重开。

### 对上一轮结论的下调

* 05:00 "让'扫描范围过小'不再被误判为健康" → **下调**：那只解决了**绝对**只数口径；
  **相对覆盖当时仍然全瞎**，本轮才补上。
* 05:00 "修一个可观测性缺陷必须走到有人改变结论为止" → **本轮我自己违反了它**：
  修空轮分支时又写了第二套算术。教训升级：
  **光"走到有人读"不够，新增分支必须复用既有语义，不许"顺手简化"。**
* `R-12` 从"仍未闭合" → **上调为已闭合"分母已知"那一半**；
  分母**未知**时仍只能判"未测量"（诚实，非遗漏）。

### ⚠ 我在写这批测试时也制造过一个缺陷（如实记录）

第一版 `test_empty_round_has_no_phantom_index_request` 用
`del eng.__class__._wants_indices` 收尾，**把真实 `Engine` 类的 property 删掉了**，
直接污染同进程后续所有测试（把端到端用例打挂）。改为**子类覆盖**，
并加收尾自证 `assert isinstance(Engine.__dict__["_wants_indices"], property)`
—— **测试不得破坏被测类**。

### 仍未闭合（下一轮最高优先）

`IT-P1-UNIVERSE-MEMBERSHIP-QUALITY-001`：`_record_universe` 的 active membership
**来源未改**（我本轮只把 transport 代码集与 active 集**分别导出**）。
改它**会改变实际扫描集合**，必须配真实 provider 回放单独一轮做。

---

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