# 最新审计

最新完整报告：[`2026-09-20_16-25-44_JST.md`](./2026-09-20_16-25-44_JST.md)  
本轮云端审计：[`2026-09-20_16-17-25_JST.md`](./2026-09-20_16-17-25_JST.md)  
本轮云端 Agent 任务书：[`2026-09-20_16-17-25_JST_AGENT_TASK.md`](./2026-09-20_16-17-25_JST_AGENT_TASK.md)

## 版本与证据边界（2026-09-20 16:25 JST）

- 仓库与分支：`fy-god/intraday-tape-reader` / `main`。
- 本轮开始 HEAD：`020f2bb`；`reviewed_source_sha`（**产品**提交）：`020f2bb`。
- 本轮执行位置：**本机本地 checkout**（`D:\ccc\ashare-radar`）。
- **本轮实测全量**：`python -m pytest -o addopts="" -q` → **`1260 passed in 68.00s`，exit 0**。
  起始基线 `1241`（`020f2bb`），本轮新增 19 项。
- 云端 16:17 轮为隔离沙箱、无完整 checkout，其 `1241 passed` 是转引 13:39 的历史证据。
- 真实多日连续 A 股语料仍 `blocked_no_mounted_multiday_continuous_corpus`。

## 本轮起因：上游复核抓到了我两个错误 —— 两条都成立，我错了

上游 `2026-09-20_13-39-26_JST.md` 独立复核了我上一轮提交 `020f2bb`。它证实了主结论
（`IT-P0-001` 已修、危害真实），**同时指出我两处叙述错误**。我逐条复现：

### (a) `IT-P0-001` 的"分母稀释"因果链 —— **我的叙述错误**

我写过：把 14:57-15:00 计入分母会让 `volume_burst` 的
`avg_per_min = volume_lots / elapsed * 60` 把放量速率**稀释**。

**这是错的。** `volume_burst.py:119` 的 `only_continuous` 门控在 **`:123` 读取
`elapsed` 之前**就已 `return []`。本机四分组合实测：

```text
MORNING        elapsed=14280  告警=1
MORNING        elapsed=14220  告警=1
CLOSE_AUCTION  elapsed=14220  告警=0
CLOSE_AUCTION  elapsed=14400  告警=0     <- 与上行完全相同
```

**若"稀释"成立，后两行应有不同结果。** 真实影响是该时段 7 个
`only_continuous` 规则由"被评估"变为"被静音"。时钟修正本身仍正确必要，
但**因果解释是错的**。已改 `session.py` docstring。

### (b) `is_tradable_window()` 的"两个历史调用点" —— **我的陈述不实**

本机实测 `git grep is_tradable_window`：`src/` **1 处（就是定义行）**、
`tools/` **0 处**。它是**纯测试 API**，从来没有生产调用点。已改为如实陈述。

> 这也让我上轮"不过度修正"的论证依据需要更正：把 `CLOSE_AUCTION` 放进
> `OBSERVABLE` 依然是对的（引擎门控用的是这个集合），**但我用错了依据**。

`IT-P0-001` **本身仍是已修**（上游独立复核补上决定性危害证据：旧代码 14:58
`elapsed=14280 告警=1` / 新代码 `elapsed=14220 告警=0`）。

## 本轮修复

| 编号 | 级别 | 来源 | 验牙 |
|---|---|---|---|
| `IT-P1-OBS-006` `stale_rejected` 名实相反 | P1 | 上游 13:39 | 改名 `future_rejected` + 弃用别名；11 项红 |
| `IT-P1-OBS-007` 账本在 Store 边界丢明细 | P1 | 上游 13:39 | 有界样本 ≤50 + 聚合计数 + 截断标志 |
| `IT-P1-COMPLETE-001` `complete` 未与 `returned` 挂钩 | P1 | 上游 13:39 | 缩水轴修复；5 项红 |
| `IT-P1-INFO-001` docstring 陈述不实 | P2 | 上游 13:39 | 改为如实陈述 |

### `IT-P1-OBS-006` 关键佐证

消费方 `tools/live_session.py:163` **早就**把它输出成 `future_rejected` ——
说明"未来"才是真实语义。字段注释同时写明：**当前不存在"陈旧拒绝"路径**。

### `IT-P1-OBS-007` 为什么是"有界样本"

导出 `unavailable_sample`（**≤50 条**）+ `unavailable_by_reason`（全量聚合）+
`decisions_truncated`。既恢复"哪只票缺什么"的可诊断性，又不把全市场成千上万条
明细塞进 Store/SSE。

### `IT-P1-COMPLETE-001` 的上限与云端更正（**仍未闭环**）

本轮修法是 `complete` 增加 `shortfall == 0`（判据"不少于"）。云端 16:17 报告
指出**该修法口径不纯**：`out` 是 post-parse 的 usable Quote，而 API `total` 是
transport/universe 口径，两者混在一起会把"传输完整但有少数无效 Quote"误判成
transport incomplete。

**我认可这条。** 正确方案是分开记录（`raw_rows` / `raw_unique_codes` /
`usable_quotes` / `expected_total` / `required_pages` / `pages_requested` /
`pages_failed` / `truncated`），把 `transport_complete` 与 `usable_coverage`
拆开。本轮修法**比修复前严格更安全**（不再静默报 complete），但**不是终态** ——
已列入下一轮。另需补"raw 全但部分 Quote 不可用"与"raw row 数够但 code 重复"
两轴测试。

## 上轮结论复核

- `IT-P0-001`（修复与危害）：**确认成立、无回归**；
- `IT-P0-002-R1/R2`、`IT-P2-OBS-003/004/005`、`IT-P0-003`、`IT-P1-006` 主体、
  `IT-P1-007`、`IT-P0-004/005`（BOM）：**确认已修、无回归**；
- `IT-P1-CAPABILITY-001`：仍为**阶段一部分修复**，**不下调**。

## 云端 16:17 轮新确认（本轮未做，如实登记）

- `IT-P1-TIME-ROLE-001/002/003` —— **最高优先，仍未修**。`accepted_watermark`
  仍是 `code -> timestamp`，无 `route/source_epoch/time_role`；轻微 future `ts`
  被改写为本地 now；首见码无 stale 下限；
- `IT-P2-OBS-008` —— 未实际发 index 请求，却把配置里的 index 计入 `requested`；
- `IT-P2-OBS-009` —— index 缺失没有 code-level `unknown_missing`；
- `IT-P1-INDEX-CURRENT-001` —— index 规则仍从累计 `state.quotes` 取 current。

## 测试与门禁（真实执行）

```text
python -m pytest -o addopts="" -q   -> 1260 passed / 0 failed  (68s)
                                       基线 1241；本轮新增 19 项
tools/check_*.py                    -> 8 个全部 exit 0
node tools/dash_render_check.js     -> exit 0
python tools/check_bom.py           -> exit 0
```

合并回退验牙（回退 `capabilities.py`/`engine.py`/`live_session.py` 到 `020f2bb`）：
**11 failed, 1 passed**；恢复后 **12 passed**。

## 下一轮必须核查（按云端任务书优先级）

1. **`IT-P1-TIME-ROLE-001/002/003`**（P1 最高）：建立 source-epoch/time-role
   时间合同。先写红测：跨源首包、同 epoch 真乱序、A→B→A 三段 epoch、
   flat observation 仍推进 ordering state、`source_time_contract.json`
   （证据不足写 `unknown`）、`provider_ts_raw` 不因 clamp 消失。
   新增 `tests/test_source_epoch_time_contract.py`；
2. **`IT-P2-OBS-008/009`**：`requested` 从真实 dispatch 产生，stock/index
   分 route 记录，缺失 code-level，coverage 分母只含真实请求；
3. **`IT-P1-INDEX-CURRENT-001`**：传本轮 current index view；
4. **`IT-P1-COMPLETE-001` 终态**：拆 `transport_complete` / `usable_coverage`；
5. 其后：`IT-P1-SOURCE-EMPTY-001`、per-code provenance、`IT-P1-WINDOW-001`、
   `IT-P1-LIMIT-001`、`IT-P1-008/009/003`。

### 欠账（措辞更正）

- `tests/test_full_day_simulation.py` —— **从未入库**，应称"待新增测试"，不是"回归缺陷"；
- `tools/check_phase_sets.py` —— **尚未创建**（`tools/check_*.py` 实有 8 个）。

## 历史线索

- [2026-09-20 16:25 JST](2026-09-20_16-25-44_JST.md) — 本轮；改我自己的两处错误 + OBS-006/007 + COMPLETE-001 缩水轴
- [2026-09-20 16:17 JST](2026-09-20_16-17-25_JST.md) — 云端；source-epoch/time-role、index ledger/current、transport 修法更正、模型分歧负实验
- [2026-09-20 13:39 JST](2026-09-20_13-39-26_JST.md) — 独立复核 `020f2bb`，指出我的叙述错误
- [2026-09-20 13:16 JST](2026-09-20_13-16-06_JST.md) — 关闭 close-auction 与 max_pages 截断窄义
- [2026-09-20 12:19 JST](2026-09-20_12-19-36_JST.md) — IT-P0-002-R2 + 三账本桶
- [2026-09-20 12:07 JST](2026-09-20_12-07-11_JST.md)
- [2026-09-20 09:32 JST](2026-09-20_09-32-43_JST.md)
- [2026-09-20 08:10 JST](2026-09-20_08-10-29_JST.md)
- [2026-09-20 05:30 JST](2026-09-20_05-30-48_JST.md)

## 研究约束（沿用云端结论，不重复造 synthetic）

- **不实现** disagreement-abstention（高分歧区 specialist PR 反而更高）；
- **不实现** blend（+0.0000335 增量不划算）；
- provider-specialist 仍只是待真实数据验证候选。真实数据到位前**不继续造新
  synthetic 模型刷指标**，继续完成工程合同、数据 manifest、真实数据扫描器。

后续仍只在本目录新增审计 Markdown / Agent 任务书并更新本索引；文档提交不计作产品源码升级。
