# 最新审计

最新完整报告：[`2026-09-20_17-22-44_JST.md`](./2026-09-20_17-22-44_JST.md)  
时间合同产物：[`source_time_contract.json`](./source_time_contract.json)  
上一轮云端审计：[`2026-09-20_16-17-25_JST.md`](./2026-09-20_16-17-25_JST.md)  
上一轮 Agent 任务书：[`2026-09-20_16-17-25_JST_AGENT_TASK.md`](./2026-09-20_16-17-25_JST_AGENT_TASK.md)

## 版本与证据边界（2026-09-20 17:22 JST）

- 仓库与分支：`fy-god/intraday-tape-reader` / `main`。
- 本轮开始 HEAD：`f6a064b`；`reviewed_source_sha`（**产品**提交）：`020f2bb`
  （`f6a064b` 相对它只多审计文档，**无产品源码变化**）。
- 本轮执行位置：**本机本地 checkout**（`D:\ccc\ashare-radar`）。
- **本轮实测全量**：`python -m pytest -o addopts="" -q` → **`1308 passed in 67.71s`，exit 0**。
  起始基线 `1260`（`f6a064b`），本轮新增 48 项。
- 真实多日连续 A 股语料仍 `blocked_no_mounted_multiday_continuous_corpus`。

## 本轮：任务书 P1-最高 —— source-epoch / time-role 时间合同

云端 16:17 任务书把 `IT-P1-TIME-ROLE-001/002/003` 列为 P1-最高并要求先写红测。
本轮照做，并并行派子 agent 处理独立的 `IT-P1-LIMIT-001`。

| 编号 | 级别 | 状态 | 验牙 |
|---|---|---|---|
| `IT-P1-TIME-ROLE-001` 跨源水位线误杀 | **P1** | **已修** | 端到端 `admitted=0→1`；4 条**行为级**红 |
| `IT-P1-TIME-ROLE-002` 原始 ts 丢失 | P1 | 已修 | **仅结构性牙**（见下） |
| `IT-P1-TIME-ROLE-003` 首见码无陈旧下限 | P1 | **已修** | 3 天前首见码由"接受"变"拒绝" |
| `IT-P1-LIMIT-001` 首次达标封板被吞 | P1 | **已修**（子 agent） | 4 条真验牙 `assert 0 == 1` |
| `source_time_contract.json` | 产物 | 新增 | 与真实 parser 的一致性守卫 |

### 001 机制

`accepted_watermark` 是 `code -> timestamp`，**不含来源**。A 源先接受 `10:30:40`；
供数切到 B 源、B 首包 `10:30:05` —— 按事件时间比水位线旧，被当"乱序"拒绝。
但 B 的时间轴是**另一个 epoch 的起点**，不是倒退。

修复前实测：

```text
A 源 ts=10:30:40 -> admitted=['600000']  watermark==10:30:40 True
B 源 ts=10:30:05 -> admitted=[]          stats={'t_reject:out_of_order': 1}
                    quotes.price 仍是 10.0（B 的 10.5 丢了）
首见码 ts=3天前  -> admitted=['999999']  【被接受】
轻微超前 ts=now+2s -> 入库 history 时间戳 == now（原始 ts 丢失）
```

> **构造用例的陷阱**：`ts` 必须是**已发生**的时间。用 `now+40s` 会被夹到 `now`，
> 水位线停在 `now`，B 的 `now+5s` 也被夹到 `now` → 相等而放行，**缺陷被掩盖**。
> 我第一版探针正是这么错的。

### 修法要点

- 水位线按**实际 serving source** 分段（`begin_source_epoch`）。用
  `serving_of(ROUTE_STOCKS)` 而**不是** `current` —— 热备期间由备用源实际供数。
- epoch 标签用 **「名字#下标」**：两个源重名时只用名字会判为幂等、不重置水位线，
  跨源误杀原样回来（已单独加测试与验牙）。
- A→B→A 是**三个** epoch；同一来源重复上报**幂等**。
- **保留 provider 原始 ts**（`provider_ts_raw`）：入库仍用夹过的值 ——
  那是**既有且被测试固定**的契约（`test_engine_time_admission.py:113`），
  本轮不擅自改动，只额外留痕。
- `effective_event_time` 与 `received_at` 分开。
- 首见码加**保守**陈旧下限 4 小时（冷门股最后成交时间本身就可能很旧，
  下限设成几分钟会误杀）。

### 002 **没有**行为级牙（如实交代）

`provider_ts_raw` 是新符号，在旧代码上只能 `AttributeError` —— 那是**结构性**红。
因为修复**不改变**"夹到 now"这个已被测试固定的契约，只是额外留痕。
谁若要求 002 也有行为牙，正确做法是先推翻该契约；本轮**不擅自**做。
这条写进了测试文件 docstring，不假装有牙。

### `source_time_contract.json`：三源全部 `role=unknown`

| 源 | provider_field | role | 依据 |
|---|---|---|---|
| tencent | `fields[30]` | `unknown` | `tencent.py:133/397` |
| sina | `fields[30]`+`[31]` | `unknown` | `sina.py:_ts_of` |
| eastmoney | `f124` | `unknown` | `eastmoney.py:55/142/227` |

**不猜**：仓内没有权威字段规范能证明这些字段是 event time / publish time /
last trade time。社区称 tencent 字段 30 为"数据更新时间"，但该说法**在仓内
无可核查来源**。合同明确禁止**跨源比较 ts** 与**基于 provider ts 判新鲜度**。

## `IT-P1-LIMIT-001`（并行子 agent）

`limit_board.py` 在**门槛检查之前**无条件写 `"sealed"`，紧接
`if prev == "sealed": return None`，于是首次达标被永久吞掉（实测两轮 `[None, None]`，
应为 `[None, Alert]`）。修法：先判资格再写状态，不达标记 `at_limit_unqualified`
（**贴板 ≠ 已封板**），新增 `_seal_qualified()` 作门槛唯一来源。

子 agent **主动如实分类**验牙：4 条真验牙（红在告警本身）、3 条结构性
（红在状态名/新符号）、2 条回归保护（修前也绿）—— 不谎称全红，我认可并要求保留。

**一处产品口径**：封单"300万→100万→300万"允许**重新告警**（有意选择，与既有
"价格离开再封回可重报"一致）。已写进 `docs/ARCHITECTURE.md` 状态机小节并钉在
`test_seal_refires_after_amount_falls_back_below_threshold`。若业务要求一天一次，
需改该处判据。

## 上轮修法的自我下调：`IT-P1-COMPLETE-001`

上轮用 `complete = ... and shortfall == 0` 修了"缩水页"轴。云端 16:17 指出该修法
**口径不纯**：`out` 是 post-parse usable Quote，API `total` 是 transport 口径，
混用会把"传输完整但有少数无效 Quote"误判成 transport incomplete。**我认可。**
该修法比修复前严格更安全，但**不是终态**，已列入下一轮。

## 一次操作事故（如实报告）

用 PowerShell `Set-Content` 做验牙时**把 `src/arad/engine.py` 写坏**（编码改变、
内容截断，`UnicodeDecodeError`）。已用事前备份完整恢复并逐项自检（可 UTF-8
编译、1299 行、五处新符号齐备），随后改用 **Python 读写**做同一验牙。
教训：**验牙不要用 `Set-Content`**。全量与门禁都在恢复之后跑。

## 测试与门禁（真实执行）

```text
python -m pytest -o addopts="" -q   -> 1308 passed / 0 failed  (67.71s)
                                       基线 1260；本轮新增 48 项
tools/check_*.py  (8 个)            -> 全部 exit 0
node tools/dash_render_check.js     -> exit 0
python tools/check_bom.py           -> exit 0
```

回退验牙（`engine.py` 回退到 `f6a064b`，保留新测试）：**16 failed, 7 passed**，
其中行为级 4 条全为 `AssertionError`；恢复后 39 passed。

## 本轮未做（明确不谎报）

- `IT-P2-OBS-008/009`（index requested 未按真实 dispatch / 缺失无 code-level 明细）
- `IT-P1-INDEX-CURRENT-001`（index 规则仍从累计 `state.quotes` 取 current）
- `IT-P1-COMPLETE-001` 终态（拆 `transport_complete` / `usable_coverage`）
- `IT-P1-SOURCE-EMPTY-001`、`IT-P1-WINDOW-001`、`IT-P1-CAPABILITY-002`、
  `IT-P1-008/009/003`
- `tests/test_full_day_simulation.py`（**从未入库**，应称"待新增测试"）
- 真实多日语料（`blocked`）

## 下一轮必须核查

1. `IT-P2-OBS-008/009`：`requested` 从真实 dispatch 产生，stock/index 分 route
   记录 actual requested codes，缺失 code-level，coverage 分母只含真实请求；
2. `IT-P1-INDEX-CURRENT-001`：传本轮 current index view；
3. `IT-P1-COMPLETE-001` 终态：拆 `transport_complete` / `usable_coverage`，
   补"raw 全但部分 Quote 不可用"与"raw row 数够但 code 重复"两轴；
4. `IT-P1-SOURCE-EMPTY-001`、`IT-P1-WINDOW-001`、`IT-P1-CAPABILITY-002`、
   `IT-P1-008/009/003`；
5. `tests/test_full_day_simulation.py`（新增，非"回归"）。

## 研究约束（沿用云端结论，不重复造 synthetic）

- **不实现** disagreement-abstention（高分歧区 specialist PR 反而更高）；
- **不实现** blend（+0.0000335 增量不划算）；
- provider-specialist 仍只是待真实数据验证候选。真实数据到位前**不继续造新
  synthetic 模型刷指标**。

## 历史线索

- [2026-09-20 17:22 JST](2026-09-20_17-22-44_JST.md) — 本轮；source-epoch/time-role 时间合同 + LIMIT-001
- [2026-09-20 16:25 JST](2026-09-20_16-25-44_JST.md) — 改我自己的两处错误 + OBS-006/007 + COMPLETE-001 缩水轴
- [2026-09-20 16:17 JST](2026-09-20_16-17-25_JST.md) — 云端；source-epoch 任务书、index ledger/current、transport 修法更正
- [2026-09-20 13:39 JST](2026-09-20_13-39-26_JST.md) — 独立复核 `020f2bb`，指出我的叙述错误
- [2026-09-20 13:16 JST](2026-09-20_13-16-06_JST.md) — 关闭 close-auction 与 max_pages 截断窄义
- [2026-09-20 12:19 JST](2026-09-20_12-19-36_JST.md) — IT-P0-002-R2 + 三账本桶
- [2026-09-20 12:07 JST](2026-09-20_12-07-11_JST.md)
- [2026-09-20 09:32 JST](2026-09-20_09-32-43_JST.md)
- [2026-09-20 05:30 JST](2026-09-20_05-30-48_JST.md)

后续仍只在本目录新增审计 Markdown / Agent 任务书并更新本索引；文档提交不计作产品源码升级。
