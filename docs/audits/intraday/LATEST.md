# 最新审计

**最新本地 Agent 产品轮**：[`2026-09-22_01-00-00_JST.md`](./2026-09-22_01-00-00_JST.md)  
**最新云端独立审计**：[`2026-09-22_00-08-36_JST.md`](./2026-09-22_00-08-36_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-22_00-08-36_JST_AGENT_TASK.md`](./2026-09-22_00-08-36_JST_AGENT_TASK.md)  
**上一份本地 Agent 产品轮**：[`2026-09-21_21-00-00_JST.md`](./2026-09-21_21-00-00_JST.md)  
**上一份云端独立审计**：[`2026-09-21_21-56-56_JST.md`](./2026-09-21_21-56-56_JST.md)  
**更早云端独立审计**：[`2026-09-21_21-43-27_JST.md`](./2026-09-21_21-43-27_JST.md)  
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
|---|---|---|---|
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
