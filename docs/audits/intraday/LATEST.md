# 最新审计

**最新云端独立审计**：[`2026-09-23_08-05-58_JST.md`](./2026-09-23_08-05-58_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-23_08-05-58_JST_AGENT_TASK.md`](./2026-09-23_08-05-58_JST_AGENT_TASK.md)  
**最新独立实测审计**：[`2026-09-23_05-35-00_JST.md`](./2026-09-23_05-35-00_JST.md)  
**最新本地 Agent 产品轮**：[`2026-09-22_21-00-00_JST.md`](./2026-09-22_21-00-00_JST.md)  
**最新产品提交 / reviewed_source_sha**：`bf0b83bf212f169c720ca8a1c5108405c18a9712`  
**本轮审计开始 docs HEAD**：`55ee49a222e1e598e9845d98e4e5f472d894e174`  
**report_commit_sha**：`022d322efe120ffff7c69ea28330bbc77b397e22`  
**agent_task_commit_sha**：`57839464acd5707277984047913b0a620630451b`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/55ee49a222e1e598e9845d98e4e5f472d894e174/docs/audits/intraday/LATEST.md

> `bf0b83b..55ee49a222e1e598e9845d98e4e5f472d894e174` 只有 docs 变化；历史报告文件均保留。本文件只移动当前接续指针。

## 2026-09-23 08:05:58 JST

主实验：`EXP-IT-MEMBERSHIP-FIX-008`

### 当前最高优先

`IT-P1-UNIVERSE-MEMBERSHIP-QUALITY-001`

05:35 独立 checkout 已真实确认：

```text
before active = 5913
raw/expected  = 5923
usable Quote  = 5553
after active  = 5553
370 old members lost
10/10 new members enter
full suite = 1802 passed
```

本轮新增结论：两个直觉修法都不能作为最终实现：
- complete-path shrink guard 会保旧池但挡住10个新成员；
- union(prev, usable) 会无法删除真实退市/移除成员。

最终合同必须：
**transport membership replacement**，且 Member 必须带 `code/name/board/list_date/source`；不能只存 code，也不能造 fake zero-price Quote。

### Pagination P2

05:35 已真实确认：unknown-total 分支 `break` 在 `pages.append` 前；一页 raw code 存在但 usable Quote=0 时：
- 终止页 raw members 自己被丢；
- 后续页不请求。

只移动 append 不够；终止语义必须基于 raw transport emptiness。

### 历史真实数据上下文

05:35 本地 Agent 的历史面板测量（本轮未重跑）：p50 0.81%、p95 11.61%、p99 17.58%、max 50.15%，956/3157=30.3% 交易日停牌率 >= synthetic 6.26%。它只支持优先级，不是当前实时停牌率、Alert Recall 或漏报率。

下一轮：Membership RED/GREEN/rollback → member metadata → pagination → reconcile → SourceManager [] → Timezone → ObservationInterval → provenance/research。

模型训练：0；真实 Precision/Recall/漏事件率/收益 unavailable。

## 历史接续

上一版完整索引与全部历史报告仍保存在不可变 commit `55ee49a222e1e598e9845d98e4e5f472d894e174` 以及本目录历史文件中；本轮不删除历史报告。
