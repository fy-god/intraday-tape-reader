# 证据索引 — 2026-09-24 17:35:20 JST

对应报告：`../2026-09-24_17-35-20_JST.md`
对应任务书：`../2026-09-24_12-04-56_JST_AGENT_TASK.md`
本轮开始 HEAD：`5d9c946ef8eb9bf9a855cb7d3cdcf5c2f2af5483`
固定产品基线（云端 12:04 认定）：`7a4f549a15e78fe87db7de00796eff71b707ae9b`

---

## 1. 复现命令（可逐条重跑）

```powershell
cd D:\ccc\ashare-radar
$env:PYTHONPATH='src'; $env:PYTHONIOENCODING='utf-8'

# WP01 单元（12 条）
python -m pytest tests/test_engine_route_time.py -o addopts="" -q -p no:cacheprovider -v

# WP01 生产接线（8 条，AST 判定）
python -m pytest tests/test_engine_route_ledger_wiring.py -o addopts="" -q -p no:cacheprovider -v

# WP06 无总数翻页（7 条）
python -m pytest tests/test_eastmoney_no_total_pagination.py -o addopts="" -q -p no:cacheprovider -v

# 全量
python -m pytest -o addopts="" -q -p no:cacheprovider -v

# 门禁
python tools/check_bom.py
Get-ChildItem tools\check_*.py | ForEach-Object { python $_.FullName; "exit=$LASTEXITCODE" }
node tools/dash_render_check.js
python -m arad.cli selftest
```

**注意**：`pyproject.toml` 的 `addopts = "-q"` 会抑制末尾汇总行，
必须用 `-o addopts=""` 覆盖，否则看不到 `N passed`。

---

## 2. 关键数字（全部来自本目录日志，非手写）

| 数字 | 值 | 来源文件 |
|---|---|---|
| 全量测试 | **1946 passed / 0 failed / exit 0** | `full_pytest.log` |
| `check_*.py` | 8/8 exit 0 | 见报告 §6 |
| BOM 检查 | exit 0 | 见报告 §6 |
| dash 渲染 | exit 0 | 见报告 §6 |
| CLI selftest | exit 0（68 告警 / 6 类型） | 见报告 §6 |

### 回退验牙总表（8 条，全部行为性，0 结构性）

| 回退 | 内容 | 结果 | 日志 |
|---|---|---|---|
| RB-A | 诊断映射去掉 by_route 写入 | 2 failed ✓ | `route_reject_rollback.log` |
| RB-B | future/ooo 退回 global-only | 3 failed ✓ | 同上 |
| RB-C | `route` 归属写死成默认值 | 1 failed ✓ | 同上 |
| RB-D | 旧形状（计数不传，0 默认值兜住） | 3 failed ✓ | 同上 |
| RB-E | `poll_once` 退回 `update()` | 2 failed ✓ | `engine_detailed_rollback.log` |
| RB-F | 退回差分全局 counters | 2 failed ✓ | 同上 |
| RB-G | 空轮手写第二套账 | 1 failed ✓ | 同上 |
| RB-P | 退回 `if not page.quotes: break` | 1 failed ✓ | `pagination_rollback.log` |

**判据升级记录（本轮）**：
- **结构性** = 在 **import / collection** 阶段就炸（只证明缺符号，无探测力）。
- **行为性** = 运行期在**目标测试路径上**抛错（真证据）。
- 每条回退必须命中**它自己的**目标测试（`TARGETS[name] & failed_names`）。

---

## 3. 两个缺陷的实测证据

### 3.1 `IT-P2-TIME-REJECT-ROUTE-ATTRIBUTION-011`

**Case A vs Case B**（`route_time_cases.json` 有全部用例名）：

```text
Case A: index future=1, stock ooo=1   -> 签名 (1, 0, 0, 1)
Case B: index ooo=1,   stock future=1 -> 签名 (0, 1, 1, 0)
旧行为（global 差分）: 两者都是 future=1 / ooo=1  -> 签名相同，不可区分
修复后:                签名不同 -> 可区分
```

**route code collision**（`000001` 既是指数又是平安银行）：

```text
index age = STALE_TOLERANCE_SECONDS + 600  (真陈旧)
stock age = 0                              (新鲜)
旧行为: stock 覆盖 index  -> time_age_seconds_by_route[("index","000001")] 丢失
修复后: 两条独立 -> index 仍 stale、stock 仍 fresh
```

### 3.2 `IT-P2-EASTMONEY-UNKNOWN-TOTAL-USABLE-EMPTY-STOPS-PAGINATION-001`

真行为 RED（不是构造断言）：见 `pagination_red.log`。

```text
页序列: p1=正常600011  p2=停牌600022  p3=正常600033  p4=真空
旧行为: requested pages = [1, 2]
        codes = ['600011']            <- 600033 静默丢失
正确:   requested pages = [1, 2, 3, 4]
        codes = ['600011', '600033']
```

`pagination_branch_diag.py` 是分支诊断脚本 —— 它证明了**我的第一版测试
根本没走进被测分支**（第 1 页在循环外，不受 break 管辖）。

---

## 4. 诚实的边界

### 4.1 用户要的"盯盘"仍然没有跑起来

- `spirit_*` 仍 `enabled: false`。
- 真实 Precision / Recall / 漏事件率 / 交易收益**仍然 `unavailable`**。
- 本轮改的全是**账本与准入的内部正确性**，不是让用户收到有用预警。
- **没有任何伪造的性能数字**。

### 4.2 本轮修复的代码在生产上的可达性

| 改动 | 生产可达性 |
|---|---|
| `poll_once` route 分账（WP01） | **可达** —— `poll_once` 是主循环 |
| 诊断映射 by_route（WP02） | **可达** —— `update_detailed` 由 `poll_once` 调用 |
| Eastmoney 无总数翻页（WP06） | **可达** —— 但仅在 `data.total` 为 null 时走到 |
| `live_session` 采集新字段 | **可达** —— 但只在 soak 运行时 |

**仍不可达**：`call_detailed` / `snapshots_detailed`（WP04 未做，生产调用点 = 0）。

### 4.3 三个自曝的测试错误

见报告 §4。三者都**如实留在测试文件 docstring 里**：
- §4.1 `test_engine_route_time.py`（WP01 rollback target 名单）
- §4.2 `test_engine_route_ledger_wiring.py`（重言式 gate → 改 AST + 自检锁）
- §4.3 `test_eastmoney_no_total_pagination.py`（RED 未走进被测分支）
- §4.4 `test_engine_route_time.py`（任务书 900s ≠ 仓库 14400s 阈值）

---

## 5. 未完成项

见 `not_run/BLOCKED.md`。
