# 最新审计

**最新云端独立审计**：[`2026-09-23_15-52-52_JST.md`](./2026-09-23_15-52-52_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-23_12-37-02_JST_AGENT_TASK.md`](./2026-09-23_12-37-02_JST_AGENT_TASK.md)  
**最新独立实测审计**：[`2026-09-23_15-52-52_JST.md`](./2026-09-23_15-52-52_JST.md)  
**最新本地 Agent 产品轮**：[`2026-09-22_21-00-00_JST.md`](./2026-09-22_21-00-00_JST.md)  
**最新产品提交 / reviewed_source_sha**：`bf0b83bf212f169c720ca8a1c5108405c18a9712`  
**本轮审计开始 docs HEAD**：`d86276339abd6a6904c45a52c6a4b489a0285060`  
**report_commit_sha**：`PENDING_BACKFILL`  
**agent_task_commit_sha**：`NA_THIS_ROUND`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/d86276339abd6a6904c45a52c6a4b489a0285060/docs/audits/intraday/LATEST.md

> `bf0b83b..d862763` 后续仍仅为审计文档，**没有产品源码变化**（本轮独立复核：`git diff --name-status` 的 6 条变更全部在 `docs/audits/intraday/` 下）。历史报告文件均保留；本文件只移动当前接续指针。

## 2026-09-23 12:37:02 JST

主实验：`EXP-IT-SNAPSHOT-CROSS-SOURCE-010`

### 本轮新增最高优先

`IT-P1-SNAPSHOT-OUTCOME-SOURCE-SEMANTICS-DRIFT-001`：默认 stock route 是 Tencent primary → Sina fallback。固定源码中 Tencent 对 `price<=0` 停牌行保留 Quote，Engine 因而能记 `rejected_quality`；Sina 对同类 raw row 在 parser 内 `continue`，Engine 只能记 `unknown_missing`。同一个 raw-present-but-unusable 市场事实会因 serving source 改变账本语义。

### 主裁决

12:11 的 Snapshot Raw Outcome 优先顺序保持正确，但**不能只改 Eastmoney**。Snapshot outcome 必须成为 Tencent/Sina/Eastmoney 三个内置 stock source 的统一合同，或者显式标记 `raw_presence_known=False`，不能把 quote projection 冒充 exact provider absence。

### 新 P2 护栏

`IT-P2-SNAPSHOT-RAW-SET-ALGEBRA-001`：detailed raw result 必须按 requested set 做交集并单独记录 unexpected / duplicate。`returned` 不能用 raw row count，也不能用未过滤 raw unique count，否则重复/额外返回会让 returned>requested 或掩盖 requested missing。

### 主改造

**Snapshot Outcome Contract v4**

```text
Requested Identity Set
        ↓
Source Raw Outcome
        ↓
Quote Usability
        ↓
Observation Ledger
        ↓
Membership / Failover / Quality Factors
```

下一轮：三源 detailed RED/GREEN/rollback → Engine ledger migration → Membership 5923/5553 → soft-empty → correctness benchmark → pagination/timezone/window → research。

模型训练：0；真实 Precision/Recall/漏事件率/收益 `unavailable`。

## 2026-09-23 15:52:52 JST

主实验：`EXP-IT` 运维门控与账本归因（**无产品源码变化**，故为纯审计轮）

### 本轮最高优先（新）

`IT-P1-SUITE-TIMING-ASSERTION-NONDETERMINISTIC-001`：**同一棵逐字节相同的源码树**，整套 pytest 既给出 `1 failed, 1801 passed`（exit 1）也给出 `1802 passed`（exit 0）。唯一失败点是墙钟断言 `tests/test_rule_limit_board.py:1212 assert elapsed < 0.2`（实测 214.3ms，超出阈值仅 7.2%）。**本仓库的红/绿不能作为正确性证据。**

`IT-P2-EMPTY-ROUND-LEDGER-IDENTITY-UNATTRIBUTED-001`（本轮新增，与已有空轮测试不相交）：`src/arad/engine.py:1515-1523` 的空轮分支只写 `returned=0/admitted=0`，**不设** `unknown_missing`，于是仓库自己文档化的恒等式 `stock_requested == admitted+missing+quality+future+ooo` 在空轮下变成 `5913 == 0`，**整轮请求集无人负责**。已有 4 个空轮测试分别断言 poll 恒等、幻影指数、请求数，**无一断言桶恒等式**。

### 主裁决

`IT-P2-EASTMONEY-UNKNOWN-TOTAL-USABLE-EMPTY-STOPS-PAGINATION-001` **本轮完成 RED/GREEN 对照**（阴性臂红 `pages=[1,2] raw=100`；阳性臂绿 `pages=[1,2,3,4] raw=300`；健全臂证明探针有效）。修复 = `src/arad/sources/eastmoney.py:434-437` **两行换序** + 判据由 `page.quotes` 改为 `page.raw_rows`（`git diff --stat`：2 insertions, 2 deletions）。

`IT-P1-SNAPSHOT-OUTCOME-SOURCE-SEMANTICS-DRIFT-001` **成立但机制需修正**：Tencent **推迟**到 `engine.py:258` 才丢（parser 保留 Quote，Engine 已看到 → `rejected_quality`）；Sina/Eastmoney **在 parser 内**就丢（Engine 只能记 `unknown_missing`）。分歧需**同时**满足「停牌码在请求集内」与「该源保留行」两条件，故在当前成员口径下不可见。

### 记账修正（我自己的）

上一份报告（`2026-09-23_09-05-00_JST.md`）的 `pytest=not_run` 是真实缺口，**本轮补齐真数**：整套 3 次（1 红 2 绿）、子集 `test_eastmoney_truncation.py` 29 passed。

### 下一轮

D6 在 `live_session` 侧定级 → D1 拆非门控性能套件 → D3 三臂固化为仓库内测试 → D4 以 Eastmoney 的 transport 账本为统一切入点 → 补测三处未计数频次。

模型训练：0；真实 Precision/Recall/漏事件率/收益 `unavailable`。本轮 `evidence_type`：软件样本 = 有，实股结果 = 0。
