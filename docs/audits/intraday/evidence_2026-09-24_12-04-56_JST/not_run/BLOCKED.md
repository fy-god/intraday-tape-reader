# 未运行 / 阻塞项 — 2026-09-24 17:35:20 JST

任务书 §「必须回传」要求「所有未运行项必须 `not_run/blocked + 原因`」。
本文件逐条列出，**不省略**。

---

## 1. 完全未运行（本轮无对应产物）

### WP03 — Per-route RoundObservation 的 terminal buckets

**状态**：**未做**（部分前置已完成）
**原因**：本轮只完成了 reject 分账（`reject_by_route`）。任务书要求的
终态分桶 `admitted / missing / quality / future / ooo / stale / unattributed`
以及不变式 `Requested = 不相交并集(terminals)` 未实现。

**为什么没做**：见报告 §8 R2 —— 在写不变式之前必须先钉死
"桶是**代码集合**还是**条数**"。当前代码里
`time_rejected_codes`（按代码）与 `future_rejected`（按条数）口径**不同**，
直接相加会对不上账。这是**任务定义问题**，不是程序问题。

**证据**：本轮**未产出** `route_observation_red.log`（只有 green）。

### WP04 — Engine detailed 集成

**状态**：**未做**
**原因**：`call_detailed` / `snapshots_detailed` 生产调用点仍为 **0**
（实测：`src/` 中 `call_detailed` 只出现在 `engine.py:591` 的**定义本身**；
`poll_once` 走 `self.sources.call("snapshots", ...)`）。
**证据**：`engine_detailed_*` 日志本轮只覆盖了 `update_detailed`
（`EngineState` 那条线），**不是** `call_detailed`（`SourceManager` 那条线）。

### WP05 — live_session 的 coverage 分账

**状态**：**部分**。本轮采集了 `reject_by_route` / `admitted_by_route` /
`returned_by_route`，但任务书要求的 stock/index
`raw_return_coverage` / `usable_coverage` 分账**未做**。

### WP07 — Membership

**状态**：**未做**。transport membership 与 usable quote 仍未分离。

### WP08 — exact soft-empty

**状态**：**未做**。`IT-P1-SOURCE-EMPTY-001` 的成功判据仍是
"有没有抛异常"（`engine.py` 的 failover 条件）。

### WP09 — Timezone / ObservationInterval

**状态**：**未做**。`src/arad/session.py:100 _now_fn` 仍 **0 引用**（零覆盖）。

### WP10 — Research

**状态**：**blocked**
**原因**：无真实多日 manifest。

---

## 2. 任务书 §「必须回传」清单 vs 本轮实际

| 要求产物 | 状态 | 说明 |
|---|---|---|
| `git rev-parse HEAD` | ✅ | `5d9c946...`（本轮开始） |
| `git status --porcelain` | ✅ | 见提交信息 |
| `git diff` / `diff SHA256` | ✅ | 回退脚本内含 sha256 前后比对 |
| `route_time_diag_red.log` | ❌ | **未单独产出**（单元 RED 在 `route_reject_rollback.log` 里） |
| `route_time_diag_green.log` | ✅ | |
| `route_time_diag_rollback.log` | ✅ | = `route_reject_rollback.log` |
| `route_reject_red.log` | ❌ | 同 `route_time_diag_red.log`，**未单独产出** |
| `route_reject_green.log` | ✅ | = `route_time_diag_green.log` |
| `route_reject_rollback.log` | ✅ | |
| `route_observation_red.log` | ❌ | WP03 未做 |
| `route_observation_green.log` | ⚠ | 是 **WP01 接线**的 green，不是 WP03 的 |
| `route_observation_rollback.log` | ❌ | WP03 未做 |
| `engine_detailed_red.log` | ❌ | WP04 未做 |
| `engine_detailed_green.log` | ⚠ | 是 `update_detailed` 的 green，不是 `call_detailed` |
| `engine_detailed_rollback.log` | ⚠ | 同上，范围不同 |
| `pagination_red.log` | ✅ | |
| `pagination_green.log` | ✅ | |
| `pagination_rollback.log` | ✅ | |
| `route_time_cases.json` | ✅ | 从测试文件 AST 抽取，非手写 |
| `route_reject_cases.json` | ❌ | 与上合并为一份 |
| `route_observation_cases.json` | ❌ | WP03 未做 |
| `pagination_cases.json` | ❌ | 合并进 `route_time_cases.json` |
| `full_pytest.log` | ✅ | |
| `check_*.py logs` | ✅ | 见报告 §6 |
| `dash_render_check.log` | ✅ | 见报告 §6 |
| `dataset_manifest.json` … `latency_memory.csv` | ❌ | **blocked**：无真实多日 manifest（WP10） |
| `RUN_MANIFEST.json` | ✅ | 本目录 |
| `NEXT_STEPS.md` | ✅ | 本目录 |

**关于合并**：`route_time_diag_*` 与 `route_reject_*` 是**同一批测试**的
两条命名（任务书把它们当成两件事，实际是 WP01 的一个测试文件）。
我没有为了凑文件名而**复制**出内容相同的假产物 —— 而是如实说明它们合并了。

---

## 3. 环境限制

| 项 | 状态 |
|---|---|
| CI | **不存在**（`.github/workflows` 缺失） |
| `docs/audits/validate_latest.py` | **不存在** ⇒ 任务书"第 5 步 validator"**INAPPLICABLE** |
| 真实行情服务 | **未启动**（硬性边界） |
| 真实通知 / 交易 | **未做**（硬性边界） |
| Windows 计划任务 | **未动**（硬性边界） |

---

## 4. 用户核心诉求的诚实状态

**用户要的是"盘中盯盘"能跑起来。本轮结束时它仍然没有跑起来。**

- `spirit_*` 仍 `enabled: false`
- 真实 Precision / Recall / 漏事件率 / 交易收益仍 `unavailable`
- 本轮改的是**账本与准入的内部正确性**，不是"让用户收到有用预警"

这一点在报告 §7.0 与 `EVIDENCE.md` §4.1 都写明了。
