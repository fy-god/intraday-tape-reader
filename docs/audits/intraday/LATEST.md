# 最新审计

**最新云端独立审计**：[`2026-09-23_04-05-42_JST.md`](./2026-09-23_04-05-42_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-23_04-05-42_JST_AGENT_TASK.md`](./2026-09-23_04-05-42_JST_AGENT_TASK.md)  
**最新本地 Agent 产品轮**：[`2026-09-22_21-00-00_JST.md`](./2026-09-22_21-00-00_JST.md)  
**最新产品提交 / reviewed_source_sha**：`bf0b83bf212f169c720ca8a1c5108405c18a9712`  
**本轮审计开始 docs HEAD**：`1d0ab19727c24dada1373931e79baa5712c99c75`  
**report_commit_sha**：`6afb3a6dcff3a6673f32ca50389361ccdc893ee0`  
**agent_task_commit_sha**：`ef4ea0b79345461eeeb0d830897eb19a6509bf7e`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/1d0ab19727c24dada1373931e79baa5712c99c75/docs/audits/intraday/LATEST.md

> `bf0b83b..1d0ab197` 只有 docs 变化；历史报告文件均保留。本文件只移动当前接续指针。

## 2026-09-23 04:05:42 JST

主实验：`EXP-IT-MEMBERSHIP-007`

### 本轮最高优先

`IT-P1-UNIVERSE-MEMBERSHIP-QUALITY-001`

仓库现成停牌 fixture：

```text
base members = 5913
new codes = 10
raw_unique / expected = 5923
invalid-price old members = 370
usable quotes = 5553
transport_complete = True
```

固定产品当前调用链仍是：

```text
Eastmoney universe -> usable Quote[]
Engine _record_universe(quotes) -> active codes
```

所以这个 fixture 中可以出现：

```text
10/10 new codes enter
但 370 old market members 离开 active scan
active = 5553 / 5923 = 93.75%
```

现有 `test_new_listings_enter_pool` 只断言新 code 进入，并未断言旧 suspended/raw members 留在 `_codes`。

### 主改造

**Universe Membership Truth Contract v1**

分开：

```text
transport membership
quote usability
active scan membership
current snapshot availability
```

禁止用假 zero-price Quote 保 membership。

### 新 P2

`IT-P2-EASTMONEY-UNKNOWN-TOTAL-USABLE-EMPTY-STOPS-PAGINATION-001`：无 numeric total 时分页当前以 `page.quotes` 空作为到底；raw members 存在但全不可用时可能提前停止。需真实 repo RED 后再决定是否升级。

### 下一轮

membership 独立 diff → unknown-total pagination → reconcile → SourceManager [] → Timezone → ObservationInterval → provenance → research。

模型训练：0；真实 Precision/Recall/漏事件率/收益 unavailable。