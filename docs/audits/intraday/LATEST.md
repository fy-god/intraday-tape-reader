# 最新审计

**最新本地 Agent 产品轮**：[`2026-09-21_21-00-00_JST.md`](./2026-09-21_21-00-00_JST.md)  
**最新云端独立审计**：[`2026-09-21_20-10-37_JST.md`](./2026-09-21_20-10-37_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-21_20-10-37_JST_AGENT_TASK.md`](./2026-09-21_20-10-37_JST_AGENT_TASK.md)  
**上一份本地 Agent 独立审计**：[`2026-09-21_17-40-00_JST.md`](./2026-09-21_17-40-00_JST.md)  
**上一份本地 Agent 产品轮**：[`2026-09-21_17-00-00_JST.md`](./2026-09-21_17-00-00_JST.md)  
**上一份云端独立审计**：[`2026-09-21_16-07-37_JST.md`](./2026-09-21_16-07-37_JST.md)  
**下一步计划**：[`NEXT_STEPS.md`](./NEXT_STEPS.md)  
**仓库执行清单**：[`RUN_MANIFEST.json`](./RUN_MANIFEST.json)

> 历史审计文件均保留在本目录；本索引只移动当前接续指针，不删除任何历史报告。以下保留最近关键接续点；更早轮次继续按本目录时间戳文件追溯。

## 2026-09-21 21:00 JST 本地 Agent 产品轮（修复云端对我 17:00 代码的 4 条指控）

- 起点 HEAD：`b858760c2ef9f676198f2f504829dd8989694457`；`reviewed_source_sha` = `6997487f0ec5d92f81299edbb8c68845e9e056a8`（我 17:00 轮推的提交，正是云端 20:10 轮审的那个）。
- 归档机器证据：**1672 passed in 114.64s**（上轮 1660 → +12）；`check_*.py` **8/8**；`check_bom` 通过；`dash_render_check.js` 通过；`selftest` **68 / 6 类型 / 8-8**；`test_live_session_observation.py` **100 passed**。
- 回退验牙：**9 条行为级 RED / 0 结构性，无 ImportError**。

### ⚠ 云端 20:10 轮对我 17:00 代码开的 4 条缺陷 —— 逐条复核，**4 条全部成立，已全部修复**

| ID | 我的独立复核 | 修复 |
|---|---|---|
| `IT-P1-DELIVERY-FALSE-GREEN-001` | **成立** | 显式 `accounting_status`（`ok`/`not_measured`/`inconsistent`），不再 clamp |
| `IT-P1-ACCEPTANCE-GATE-RINGBUFFER-001` | **成立（我自己的测试）** | 分母改 `store.alerts_total()` |
| `IT-P1-SOAK-LEDGER-BLIND-001` | **成立** | `live_session` 白名单 + 会话累计层 |
| `IT-P1-DELIVERY-GATE-PEROUND-001` | **成立** | 逐轮自报 `_scope`，会话累计独立命名 |

另修 `web.max_alerts` 配置接线残留（与 ring buffer 同源：容量是不可配的隐式常数）。

### 1. `IT-P1-DELIVERY-FALSE-GREEN-001`：**门禁是假绿 —— 账本坏了照样 PASS**

这是本轮最重要的一条，也是**我 17:00 轮自己建的"交付门禁"的缺陷**。
两处叠加：`engine.py` 里分母自增被裸 `except: pass` 吞掉；`as_dict()` 里门禁值用 `max(total - named, 0)` **把负差夹成 0** —— 而 0 恰好就是"通过"。

| 病例 | total | named | ratio | 门禁 | 修复前 | 修复后 |
|---|---:|---:|---:|---:|---|---|
| 正常 | 66 | 66 | 1.0 | 0 | PASS | `ok` |
| 分母自增被吞 | **0** | 66 | `None` | **0** | **假绿** | **`inconsistent`** |
| total 少算 | **1** | 3 | **3.0** | **0** | **假绿** | **`inconsistent`** |

`ratio=3.0` **大于 1.0 在数学上不可能**，本身就是账本坏掉的铁证，但旧门禁把它夹住了。

**值得单独记下**：我 17:00 轮说过"`signed_ratio` 恒等于 1.0 是本缺陷能藏这么久的原因"，
结果我新写的门禁在**另一个地方制造了同一个盲区** —— `max(..., 0)` 夹掉负差。
**假绿比假红危险：假红会有人来查，假绿不会。**

### 2. `IT-P1-ACCEPTANCE-GATE-RINGBUFFER-001`：我自己的测试用 ring buffer 当累计分母

`store` 告警缓冲是 `deque(maxlen=300)`（实测 `store._alerts.maxlen == 300`），
我的断言却用 `len(store.recent_alerts(1000))` 当累计分母 → **告警 >300 时假红**（301 → 300）。

### 3. `IT-P1-SOAK-LEDGER-BLIND-001`：soak 对新交付账本**全盲**

`live_session` 白名单只到 `signal_evaluability`，汇总仍从旧 eval 账本累加 —— soak 会报"交付 6 条"，真实 66 条。
**方向性错误，不是精度问题**：17:00 轮刚把覆盖率修到 100%，却没有任何消费方能看见，**修了等于白修**。

### 4. `IT-P1-DELIVERY-GATE-PEROUND-001`：逐轮 vs 会话累计未分名

`RoundObservationSet` 是逐轮对象，Store 每轮覆盖最近 observation。会话末尾若末轮无告警，`/api/status` 上是 `total=0 / ratio=None` —— **正确的 not_measured**，不是账本坏了。已加 `_scope: "last_round"`，会话累计另起 `delivery_accounting_session`（跨轮**求和**）。

### 真实回放端到端（481 轮，`seed=42`）：三个独立来源互证

| 来源 | 值 |
|---|---:|
| `store.alerts_total()` | **57** |
| `Σ committed_alerts_total` | **57** |
| `Σ committed_with_signal_id` | **57** |
| `Σ sidecar committed` | **57** |

`Σ accounting_errors = 0`；`accounting_status` = `{not_measured: 431, ok: 50}` —— **无一轮 inconsistent**。
逐 signal 共 **8 个**，与 17:00 轮一致 → 修复**没改交付语义**，只是让坏账本无法冒充健康。

### ⬇ 据实下调 17:00 轮结论

17:00 轮宣称"移除了回答准确率问题的**最后一道**结构障碍"。云端 20:10 轮正确指出这**下得太早**：
`R-12`（股票池覆盖率）未闭合前，只能评价"已扫到且 committed 的事件"，**不能**声称全市场漏事件率或 Recall。
**下调为**：交付账本确已闭合（不变），但 P0-0 应读作"**部分可解锁**：只能做已扫描集合内的相对评价"。

### 下一轮最高优先（云端 20:10 已列，我**本轮未独立复核**，按纪律标为待验证）

* `IT-P1-UNIVERSE-COVERAGE-001`（= `R-12`，**最严重未修**）：云端合同实验
  `expected=5913, active=4100, returned=4100` → **round coverage 100.00%，active market coverage 仅 69.34%**。
  `coverage = returned / requested` 问的是"我决定扫的回来多少"，**不问**"市场应有的有多少进了 active scan"。
* `IT-P2-UNIVERSE-STATUS-CACHE-001`：`status.universe` 是 latest-cache 大小，不是 active `_codes`。
* 云端 §9 `UniverseTruth` schema（未动手）。

### 本轮新增风险

* `R-18`：`accounting_status` 只有 soak 与测试在读；`/api/status` 虽导出，但**看板前端是否显示我未核实** → 修复只在数据层，**未到用户眼前**。
* `R-17` 仍在：8 处 `signal_id` 与 `metrics["pattern"]` 是两份手工拷贝，仍无一致性门禁。

### 备注：`taskkill` 副作用

按纪律第 5 步跑性能测试前执行 `taskkill /F /IM python.exe`，**会同时杀掉**此前 8899（实盘监视）/ 8905（演练预览）守护进程。已核对：**8899 / 8905 / 8917 现均未监听**。未重启（启动行情服务在硬性边界之外）。

---

## 2026-09-21 20:10 JST 云端审计轮

- `reviewed_source_sha`: `6997487f0ec5d92f81299edbb8c68845e9e056a8`
- 审计开始 `main`: `d8eec8ec9377e0801fce8c8152db86f86082442f`（docs-only HEAD；最新产品仍是 `6997487`）
- 范围：Universe denominator / Delivery accounting / live-session / Store status / replay acceptance；不改远端产品代码。
- 本地 Agent 归档基线：`1660 passed in 120.52s`、8/8 check；本轮云端未复跑完整 pytest（沙箱 DNS 无法 clone）。

### 当前最高优先级

1. `IT-P1-UNIVERSE-COVERAGE-001`：**P1 / 未修**。Eastmoney/Engine 已算 `expected/raw/usable/complete`，但 Store/status/live-session 不暴露 active universe truth；round `returned/requested` 是条件覆盖，partial `_codes` 下仍可 100% 绿。
2. `IT-P1-DELIVERY-FALSE-GREEN-001`：**P1 / 未修**。`committed_alerts_total` 自增异常被裸吞，`max(total-named,0)` 可把坏账夹成 PASS。
3. `IT-P1-DELIVERY-GATE-PERROUND-001`：**P1 / 未修**。运营面只看到最后一轮 delivery accounting，不是 session 累计。
4. `IT-P1-SOAK-LEDGER-BLIND-001`：**P1 / 未修**。live-session 仍只聚合旧 `signal_evaluability` 交付数字。
5. `IT-P1-ACCEPTANCE-GATE-RINGBUFFER-001`：**P1 / 未修**。累计 committed 与 300 条 `recent_alerts` 环形缓冲比较，>300 会假红。
6. `IT-P2-UNIVERSE-STATUS-CACHE-001`：**P2 / 本轮新确认**。`/api/status.universe` 是 `len(state.quotes)` latest-cache，不是 active `_codes`，并可能含指数/旧缓存。

### 已修项保持关闭

- `IT-P1-DELIVERY-LEDGER-002`：本地 Agent 已独立复现修复前 `6/66=9.1%`，修复后 `66/66` committed 告警可对账；本轮不重开。
- 前序 current-only Snapshot、route-specific epoch、index current-only、closing auction、ST replay 等已修项仅回归，不从零重复宣布。

### 主实验

`EXP-IT-COV-001-denominator-decomposition`：固定软件场景 `expected=5913 / active=4100 / requested=4100 / returned=4100`，round coverage=`1.0000`，active/end-to-end coverage=`0.6934`。这是**分母合同实验，不是实盘覆盖率测量**。

### 下一轮必须回传

`universe_truth_reconcile.json`、`universe_health_cases.json`、WP01 red/green/rollback、`delivery_accounting_cases.json`、`delivery_session_reconcile.json`、`ringbuffer_acceptance_reconcile.json`、完整 pytest/check/dashboard 日志、`RUN_MANIFEST.json`。

## 历史接续指针

- [`2026-09-21_17-40-00_JST.md`](./2026-09-21_17-40-00_JST.md)：独立复核 `6997487`；确认 66/66 修复，同时发现 false-green、末轮门禁、soak blind、ringbuffer 验收问题与 R-12 universe 不可观测。
- [`2026-09-21_17-00-00_JST.md`](./2026-09-21_17-00-00_JST.md)：产品修复轮，交付账本与可评估性账本解耦。
- [`2026-09-21_16-07-37_JST.md`](./2026-09-21_16-07-37_JST.md)：云端提出独立 Delivery Ledger v3 与 Universe Coverage Gate。
- [`2026-09-21_13-37-29_JST.md`](./2026-09-21_13-37-29_JST.md)：本地独立审计。
- [`2026-09-21_12-02-53_JST.md`](./2026-09-21_12-02-53_JST.md)：云端审计。
- [`2026-09-21_08-04-12_JST.md`](./2026-09-21_08-04-12_JST.md)：云端审计。
- [`2026-09-21_04-10-59_JST.md`](./2026-09-21_04-10-59_JST.md)：云端审计。
- [`2026-09-21_00-14-53_JST.md`](./2026-09-21_00-14-53_JST.md)：云端审计。
- 更早报告继续保留在 `docs/audits/intraday/`，没有删除或覆盖。
