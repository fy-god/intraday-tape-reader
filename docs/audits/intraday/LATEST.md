# 最新审计

**最新云端独立审计**：[`2026-09-23_21-44-55_JST.md`](./2026-09-23_21-44-55_JST.md)  
**上一轮云端独立审计**：[`2026-09-23_20-09-44_JST.md`](./2026-09-23_20-09-44_JST.md)  
**上一轮审计开始 docs HEAD**：`014435893a63d02b35ae48c34f3179c9a4fa8845`  
**本轮审计开始 / 结束 HEAD**：`540d4365b11e47d9dd6633f98f0d287e43863fec`（审计期间由 `a0ca7e6caff972d22b903a80b1731e2b7d5a2f95`
前进到 `540d4365b11e47d9dd6633f98f0d287e43863fec`，该提交只带 docs：`git diff --stat a0ca7e6 540d436 -- src/ tests/ tools/` 为空）  
**本轮报告文件**：[`2026-09-23_21-44-55_JST.md`](./2026-09-23_21-44-55_JST.md)  

  > **本轮云端独立审计（2026-09-23 21:44:55 JST）：基线后两个代码提交逐条实机复现 —— 4 条声明成立，1 条新发现（潜伏风险）。**
  > 提交 `4648b1c`（WP01 三条 RED）+ `a0ca7e6`（WP02 `call_detailed`）。
  >
  > **我用自写探针（每条配正/负对照）独立复现，全部成立：**
  > 1. `IT-P2-SNAPSHOT-OUTCOME-COVERAGE-SEMANTIC-COLLISION-001` —— 双轴已拆；
  >    `R=3,P=3,Q=1` 下 raw=`1.0` / usable=`0.3333333333333333`，两轴对照确有区别；
  >    legacy 退化得 `raw_return_coverage=None`（未测量），**没有**伪装成 `1.0`。
  > 2. `IT-P2-TENCENT-DETAILED-EXPLICIT-STOCK-KEY-AXIS-001` —— **走类级出口**实测：
  >    普通股 `sh600000` 落裸码 `600000`，真指数 `sh000001` 保留前缀，**两臂身份不同**
  >    （对照有判别力，故这是有效验证而非空转）。
  > 3. `IT-P2-SNAPSHOT-MERGE-TERMINAL-OVERLAP-001` —— merged terminal overlap = `[]`，
  >    `identity_holds()=True`；`unexpected`/`duplicate` 仍按并集（未被误伤）。
  > 4. WP02 四条声明全部实测成立：legacy 降级 `quote_projection_legacy`（不伪装 exact）、
  >    内置源 `exact_raw_presence`、来源与 outcome 原子绑定、全失败抛异常（非静默 `None`）。
  >
  > **全量测试真数：`1891 passed`，exit `0`**（两次独立跑，118.35 s / 131.5 s，均绿）
  > —— 与提交声称一致（1881 是 WP01 中间时点，WP02 再 +10 得 1891）。
  >
  > **新发现（P2，潜伏）`IT-P2-COVERAGE-ALIAS-REINTRODUCES-COLLISION-ACROSS-WP03-BOUNDARY-002`**：
  > WP01 保留的**废弃别名** `coverage` 指向**可用轴**，而线上账本
  > `RoundObservationSet.as_dict()['coverage']` 是**传输轴**（`engine.py:1693` 明写
  > `returned` = "provider 原始返回的条数"、`:1753` 用 `len(raw_stock)+len(raw_idx)`）。
  > 两者都是普通 dict，WP03 一合并即**同名静默覆盖**：`R=3,P=3,Q=1` 时
  > `merged['coverage']` = `0.3333333333333333`，而线上真值应为 `1.0`（误差 `0.666667`）。
  > **⚠ 别名方向与它自己引用的先例相反**：先例 `eastmoney.py:432`
  > `meta["complete"] = meta["transport_complete"]`（旧名钉**传输轴**），
  > WP01 却 `outcome.py:131 return self.usable_coverage`（旧名钉**可用轴**）。
  > **当前影响 = 0**（我实测 `git grep`：仓库内读该别名的消费者 = 0），
  > 故记为**待验证风险**而非已确认错误；但 WP03 接线时必然兑现。
  >
  > **⚠ 诚实边界**：`IT-P1-SNAPSHOT-RAW-LEDGER-COLLAPSE-001` **仍未修** ——
  > 我实测 `call_detailed` 的**生产调用点 = 0**（AST 调用图），
  > `poll_once` 走的仍是 `sources.call("snapshots")`（`engine.py:921/1528`），
  > 故两个提交**都不改变任何线上账本数字**（与其自述一致）。
  > `IT-P1-UNIVERSE-MEMBERSHIP-QUALITY-001` 连续第 **7** 轮未修。
  > **模型真实增益 = 0**；真实 Precision/Recall/漏报率/交易收益仍 `unavailable`。
  >
  > **我自曝两处自己的探针缺陷**：(a) 首跑 pytest 经 `Select-Object` 管道截断到 38%
  > 且 `$LASTEXITCODE=1`，我差点误报"测试失败"——完整捕获后真值为 `exit 0 / 1891 passed`；
  > (b) 我第一版别名探针用了错的 `build_outcome` 签名（缺 `raw_keys`），已按真实签名重测。
  > 另：审计期间有**并发写者**在写 `docs/audits/intraday/evidence_2026-09-23_21-00-00_JST/`
  > （0 B -> 15 文件 / 18,302 B），我**只读未动**。

---
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/540d4365b11e47d9dd6633f98f0d287e43863fec/docs/audits/intraday/LATEST.md

**最新云端独立审计**：[`2026-09-23_20-09-44_JST.md`](./2026-09-23_20-09-44_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-23_20-09-44_JST_AGENT_TASK.md`](./2026-09-23_20-09-44_JST_AGENT_TASK.md)  
**最新本地 Agent 产品轮**：[`2026-09-23_21-00-00_JST.md`](./2026-09-23_21-00-00_JST.md)  
**最新产品提交 / reviewed_source_sha**：`a0ca7e6caff972d22b903a80b1731e2b7d5a2f95`  
**上一版本地轮报告**：[`2026-09-23_19-05-00_JST.md`](./2026-09-23_19-05-00_JST.md)  
**本轮审计开始 docs HEAD**：`014435893a63d02b35ae48c34f3179c9a4fa8845`  
**report_commit_sha**：`a0535c494453f992d22e6f8d6d19e8787784abfd`  
**agent_task_commit_sha**：`59a646760949df09e034e5d1b60f97986b853b7a`  

  > **本轮本地产品轮（21:00 JST）：任务书 WP01 三条 RED 逐条复现 —— 三条全部成立、已全修；WP02 也落地。**
  > 产品提交 `4648b1c`（WP01）+ `a0ca7e6`（WP02）。
  >
  > 1. `IT-P2-SNAPSHOT-OUTCOME-COVERAGE-SEMANTIC-COLLISION-001` —— `coverage`
  >    一个名字被"传输轴/可用轴"两种语义争用，与 `eastmoney.py:421-439`
  >    早已写明的纪律矛盾。已拆成 `raw_return_coverage` + `usable_coverage`。
  > 2. `IT-P2-TENCENT-DETAILED-EXPLICIT-STOCK-KEY-AXIS-001` ——
  >    **⚠ 我第一版探针写错、误判"不成立"**（`index_codes` 默认 `None` 让
  >    `idx_set=set()`，把缺陷那一行整个绕开）。按类级出口重测后**缺陷复现**。
  >    **探针没走进被审分支 = 没测**（本会话第 3 次）。
  > 3. `IT-P2-SNAPSHOT-MERGE-TERMINAL-OVERLAP-001` —— 逐批次结论当成合并后结论，
  >    同一码可同时在 `admitted` 与 `rejected_quality`。
  >
  > **WP02 `SourceManager.call_detailed`**：来源放进返回值、与 outcome **原子绑定**
  > （云端 §6.1 明说"不能先 call 再 `serving_of` 去猜"）。同一 raw fact 在
  > failover 前后 terminal 桶**逐元素相同**。
  >
  > 全量 pytest **1891 passed**（修前 1874）；**5 项行为性 RED / 0 结构性 ERROR**。
  >
  > **⚠ 诚实边界**：`IT-P1-SNAPSHOT-RAW-LEDGER-COLLAPSE-001` **仍未修** ——
  > `call_detailed` 只是 WP03 的输入接口、**尚未接进 `poll_once`**，
  > 故**本轮两个提交都不改变任何线上账本数字**。
  > `IT-P1-UNIVERSE-MEMBERSHIP-QUALITY-001` 连续第 **7** 轮未修（WP07 明说
  > "仅 WP01-04 绿后启动"，WP03/04 未做）。**模型真实增益 = 0**。

---
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/014435893a63d02b35ae48c34f3179c9a4fa8845/docs/audits/intraday/LATEST.md

> `ff8fd8f..014435893` 只有 19:05 产品报告 / evidence / LATEST 三个 docs 路径；历史报告文件没有删除。本文件移动当前接续指针，上一版完整长索引固定保留在上面的不可变 commit。

## 2026-09-23 20:09:44 JST

主实验：`EXP-IT-OUTCOME-INTEGRATION-012`

### 接续

19:05 产品轮已把 Snapshot Outcome v4 做进 Tencent/Sina/Eastmoney，并归档 `1874 passed ×5`、12 条行为回退 RED；**但产品报告明确声明尚未接进 Engine RoundObservationSet**。因此 source-layer raw truth 已有，线上账本仍是 Quote-only 旧语义。

### 本轮新增

1. `IT-P2-SNAPSHOT-OUTCOME-COVERAGE-SEMANTIC-COLLISION-001`：source outcome 的 generic `coverage` 是 Q/R usable coverage，而现有 RoundObservation `coverage` 是 P/R raw-return coverage。WP02 接线前必须拆名。
2. `IT-P2-TENCENT-DETAILED-EXPLICIT-STOCK-KEY-AXIS-001`：显式前缀普通股票（如 `sh600000`）在 detailed request/raw identity 轴可能错位，产生 false missing/unexpected；现有测试未覆盖该形态。
3. `IT-P2-SNAPSHOT-MERGE-TERMINAL-OVERLAP-001`：同 code 在一个 part invalid、另一个 part valid 时，merge 对 per-part quality 直接并集，可让同一 code 同时 admitted+quality，破坏 terminal disjointness。

### 主改造

**Snapshot Outcome → Round Attribution Integration Contract v2**

顺序：`source-v4 self-consistency -> SourceManager detailed -> Engine unified attribution -> live-session unattributed -> Membership -> exact soft-empty`。

### 下一轮关键产物

- `coverage_cases.json`
- `snapshot_outcome_cases.json`
- `round_attribution_cases.json`
- `pagination_cases.json`
- `full_pytest.log`
- `check_*.py` logs
- `RUN_MANIFEST.json`
- `NEXT_STEPS.md`

模型训练：0；真实 Precision/Recall/漏事件率/交易收益仍 `unavailable`。
