# 最新审计

**最新本地独立审计**：[`2026-09-24_18-55-39_JST.md`](./2026-09-24_18-55-39_JST.md)  
**上一版本地独立审计**：[`2026-09-24_18-16-37_JST.md`](./2026-09-24_18-16-37_JST.md)  
**最新云端独立审计**：[`2026-09-24_16-02-09_JST.md`](./2026-09-24_16-02-09_JST.md)
**最新云端 Agent 任务书**：[`2026-09-24_16-02-09_JST_AGENT_TASK.md`](./2026-09-24_16-02-09_JST_AGENT_TASK.md)
**上一版本地独立审计**：[`2026-09-24_17-35-20_JST.md`](./2026-09-24_17-35-20_JST.md)
**reviewed_source_sha / 本轮固定产品代码**：`7a4f549a15e78fe87db7de00796eff71b707ae9b`
**audit_start_head / 本轮开始 main**：`9308c0e98ed85b35ea756ca8662236c56cb0538d`
**本轮 report_commit_sha**：`9aa1ec164aafd013e738f47c126e24c86088d174`
**上一版完整 LATEST 历史索引（不可变快照）**：
https://github.com/fy-god/intraday-tape-reader/blob/9308c0e98ed85b35ea756ca8662236c56cb0538d/docs/audits/intraday/LATEST.md

**最新本地独立审计**：[`2026-09-24_18-55-39_JST.md`](./2026-09-24_18-55-39_JST.md)  
**上一版本地独立审计**：[`2026-09-24_18-16-37_JST.md`](./2026-09-24_18-16-37_JST.md)  
**最新云端独立审计**：[2026-09-24_16-02-09_JST.md](./2026-09-24_16-02-09_JST.md)  
**最新云端 Agent 任务书**：[2026-09-24_16-02-09_JST_AGENT_TASK.md](./2026-09-24_16-02-09_JST_AGENT_TASK.md)  
**上一版云端独立审计**：[2026-09-24_12-04-56_JST.md](./2026-09-24_12-04-56_JST.md)  
**上一版云端 Agent 任务书**：[2026-09-24_12-04-56_JST_AGENT_TASK.md](./2026-09-24_12-04-56_JST_AGENT_TASK.md)  
**上一版本地独立审计**：[2026-09-24_09-27-16_JST.md](./2026-09-24_09-27-16_JST.md)  
**reviewed_source_sha / 本轮固定产品代码**：`7a4f549a15e78fe87db7de00796eff71b707ae9b`  
**audit_start_head / 本轮开始 main**：`5d9c946ef8eb9bf9a855cb7d3cdcf5c2f2af5483`  
**云端 16:02 report_commit_sha**：`869de2008e85543eb44eafbb4c31aef0196471c5`  
**云端 16:02 agent_task_commit_sha**：`4f171fa1124a60447524e58b5a8ad69f60b0a5b5`  
**上一版云端 report_commit_sha**：`b54d6289b29b04bdc8b308448e938da3adda258c`  
**上一版云端 agent_task_commit_sha**：`916cd36c93b63779a601d52d544d8687a666af8b`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/5d9c946ef8eb9bf9a855cb7d3cdcf5c2f2af5483/docs/audits/intraday/LATEST.md

> 版本纪律：`7a4f549..5d9c946` 的 GitHub compare changed files 全部位于
> `docs/audits/intraday/`，故真正产品代码仍固定为 `7a4f549`。
>
> **云端已独立复核我的上一轮修复**（`2026-09-24_12-04-56_JST.md` §6
> 「已修项不重开」）：原文「产品 `7a4f549` 已经真正修复：Tencent
> caller-owned identity role；wrong-type detailed return 穿透 failover；
> detailed source/route binding」，并确认「最新本地独立审计全量：
> `1919 passed in 81.96s / exit 0`」。⇒ 外部验证收到，不再重开那三项。

## 2026-09-24 18:55:39 JST（本地 · Sina 指数身份）

对应云端任务书 `2026-09-24_16-02-09_JST_AGENT_TASK.md` §1。
**性质：修复（拿错证券）。**

`IT-P1-SINA-INDEX-PREFIX-LOSS-SILENT-WRONG-INSTRUMENT-012` —
**已修复，并且我独立复现确认了云端的主张成立**（云端 18:16 也独立复现了同一机制）。

云端说 Sina 把 `sh000001`（上证指数）发成 `sz000001`（平安银行）。
我用**真实调用链**（只替换 `_request`）复现：默认 5 个 `index_codes` 里
**3/5 抓的是错误证券**，且错误证券的响应**能通过裸码过滤被当成成功收下**。

旧代码两步都错且互相掩盖：`_norm_codes` 剥掉调用方给的前缀，
`guess_prefix` 再用"只对个股可靠"的规则重猜 —— 而 `guess_prefix`
的 docstring 自己就写着「指数必须由调用方显式给出前缀」。

修法：**前缀是调用方拥有的事实，只能搬运，不能重猜。**
新增 `_split_prefix` / `_wire_symbol` / `_wire_symbols`；
`_fetch_bulk` 按**带前缀的完整符号**过滤响应（第二道，旧代码这里也漏）；
detailed 路径新增 `wire_symbols` 参数做严格过滤。
账本轴 R/P/Q 保持裸码（与 `outcome._key_of` 同轴），过滤轴用完整符号。

实测 RED 为 **3 条行为性 + 3 条结构性**（如实分类，不是所有红都算证据）。
回退验牙 **3/3 行为性咬合，0 结构性**；`sina.py` sha256 前后一致。
全量 **1957 passed / 0 failed / exit 0**；`check_*.py` **8/8**；BOM exit 0；
`dash_render_check.js` exit 0；`arad.cli selftest` 68 告警 / 6 类型 / exit 0。

**附带修复**：发现 `.gitignore:25` 的 `*.log` 把任务书点名的
`*_red.log` / `*_green.log` / `*_rollback.log` **全部吞掉** ——
**我上一轮报告引用的 8 个证据日志从未真正上传**。已 `git add -f` 补入两轮共 11 个。

**未决**：`Quote.code` 对指数仍不带前缀（账本仍分不清 `sh000001`/`sz000001`）；
`call_detailed` 生产调用点仍 **0**（WP04 未做）；其他源是否有同类
"剥前缀再重猜"模式未逐一审计。**`spirit_*` 仍 `enabled: false`。**

## 2026-09-24 17:35:20 JST（本地 · WP01/WP02/WP06）

对应任务书 `2026-09-24_12-04-56_JST_AGENT_TASK.md`。**性质：修复 + 生产接线。**

1. `IT-P2-TIME-REJECT-ROUTE-ATTRIBUTION-011` — **已修复**
   （`engine.py` `EngineState.update` / `Engine.poll_once`）。
   旧代码取一次全局基线再差分 → `index future + stock ooo` 与
   `index ooo + stock future` **塌成同一个结果**（都是 future=1/ooo=1），
   无法恢复 route truth。新增 `StateUpdateResult` + `update_detailed()`；
   `poll_once` 直接消费 route-local 计数，**删除全局差分**；
   `RoundObservationSet` 增 `reject_by_route` / `admitted_by_route` /
   `returned_by_route` 并导出；两条分支共用 `_route_ledger()`。
2. `IT-P2-TIME-DIAGNOSTIC-ROUTE-COLLISION-010` — **已修复**：
   `time_age_seconds` 等四个诊断映射只按**裸 code** 存，`000001`
   既是指数又是平安银行 → stock 覆盖 index 的 age。
   新增四个 `*_by_route` 表（双写保留兼容 view）+ `prune` 同步回收；
   `poll_once` 的 `_stale_codes` 改读 by_route 表。
3. `IT-P2-EASTMONEY-UNKNOWN-TOTAL-USABLE-EMPTY-STOPS-PAGINATION-001` —
   **已修复**（`eastmoney.py` `universe()` 无 total 分支）：
   旧终止判据 `if not page.quotes: break` 把"该页全是停牌股"
   当成"翻完了"。**实测真数据丢失**：`[正常, 停牌, 正常, 空]` 序列下
   旧行为只请求 `[1,2]`，第 3 页的 `600033` **静默丢失**；
   改为 `raw_code_rows <= 0 and not codes` 后请求 `[1,2,3,4]`，
   两只都拿到。`transport_complete` 语义未变。
4. **WP01 生产接线**：`poll_once` 现在真的调 `update_detailed`，
   并用 **AST 层** gate 钉住（不是子串匹配）。

### 回退验牙

**8 条全部行为性、0 结构性**：RB-A(2)/RB-B(3)/RB-C(1)/RB-D(3)/
RB-E(2)/RB-F(2)/RB-G(1)/RB-P(1)。源码 sha256 恢复前后一致。

### 我自己犯的三个错（如实记录）

- **接线测试曾是重言式**：用 `"update_detailed" in getsource(...)`，
  而我**自己的注释**里就有这个词 → 回退把真实调用全删掉后测试**照样绿**。
  改走 AST + 加自检锁。**牙被自己的散文缴了械。**
- **WP06 RED 没走进被测分支**（D15 再犯）：第 1 页在循环外，
  不受 break 管辖 → 第一版报 `6 passed`，看着像"产品已经对的"。
- **WP01 RB-C 的 target 名单写错**：它打红了，我却判"牙不咬" ——
  错在我的名单，不在 gate。另：任务书写 `age=900s`，
  仓库 `STALE_TOLERANCE_SECONDS=14400`，照抄会写出自相矛盾的断言。

### 测试真数

**1946 passed / 0 failed / exit 0**。`check_*.py` **8/8**；
BOM exit 0；`dash_render_check.js` exit 0；
`arad.cli selftest` 68 告警 / 6 类型 / exit 0。

### ⚠ 诚实交代

`spirit_*` 仍 `enabled: false`，**用户要的"盯盘"仍然没有跑起来**；
真实 Precision/Recall/漏事件率/交易收益仍 `unavailable`；
`call_detailed` 生产调用点仍 **0**（WP04 未做）。
本轮改的是**账本与准入的内部正确性**。

## 2026-09-24 16:02:09 JST（云端）

> 版本纪律：`7a4f549..5d9c946` 的 GitHub compare changed files 全部位于
> `docs/audits/intraday/`，故真正产品代码仍固定为 `7a4f549`。

主实验：`EXP-IT-SINA-INDEX-IDENTITY-017`

### 本轮最高优先新增

1. `IT-P1-SINA-INDEX-PREFIX-LOSS-SILENT-WRONG-INSTRUMENT-012`：Sina `snapshots()` / `snapshots_detailed()` 先通过 `_norm_code/_norm_codes` 丢掉显式指数前缀，再用只对股票可靠的 `guess_prefix()` 重建 wire symbol。默认 5 个 index codes 中，`sh000001→sz000001`、`sh000300→sz000300`、`sh000688→sz000688` 三个会被确定性改成错误交易所符号。
2. 这不是普通 missing：`sz000001` 是真实股票平安银行，legacy `wanted_set` 也是裸 `000001`，所以错误证券响应可以被当成成功行情收下。
3. detailed 路径使用同一 normalization，`R/P/Q` 甚至可能对错误证券机械自洽，产生“exact but wrong”假精确。因此必须先修 Source security identity，再继续 WP07。

### 主改造

**Sina Route Identity Contract v1**：

```text
显式 index prefix 保留到 HTTP wire
        ↓
指数 Quote.code 保留 exchange prefix
        ↓
index route detailed R/P/Q 使用同一 prefixed identity
        ↓
再复验 route-state collision
        ↓
StateUpdateResult / Per-route RoundObservation
```

普通股票现有 bare-code API 必须保持兼容。

### 当前主线状态

- `IT-P1-SNAPSHOT-RAW-LEDGER-COLLAPSE-001`：PARTIAL，Source / SourceManager exact truth 已有，生产 `poll_once` 仍未接 detailed。
- `IT-P1-INDEX-ROUTE-HEALTH-MASKED-BY-STOCK-AGGREGATE-001`：继续开放。
- `IT-P2-ROUND-ROUTE-PROVENANCE-COLLAPSE-001`：继续开放。
- `IT-P2-TIME-DIAGNOSTIC-ROUTE-COLLISION-010`：改为 Source identity 修复后的回归门；当前第一根因更早，是 Sina wire identity 已经拿错证券。
- `IT-P2-TIME-REJECT-ROUTE-ATTRIBUTION-011`：继续作为 WP07 前置。
- Eastmoney unknown-total pagination：已有真实 RED/GREEN，仍 ready-to-land。
- Membership / exact soft-empty / timezone / ObservationInterval：继续 downstream。

### 下一轮关键产物

- `sina_index_wire_red/green/rollback.log`
- `sina_index_quote_red/green/rollback.log`
- `sina_index_detailed_red/green/rollback.log`
- `default_index_matrix.json`
- `sina_index_cases.json`
- `route_state_recheck.json`
- `full_pytest.log`
- `RUN_MANIFEST.json`
- `NEXT_STEPS.md`

模型训练：0；真实 Precision/Recall/漏事件率/交易收益仍 `unavailable`。
