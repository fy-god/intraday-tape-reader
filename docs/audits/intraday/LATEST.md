# 最新审计

最新完整报告：[`2026-09-20_13-39-26_JST.md`](./2026-09-20_13-39-26_JST.md)

## 版本与证据边界（2026-09-20 13:39 JST）

- 仓库与分支：`fy-god/intraday-tape-reader` / `main`。
- 本轮开始 HEAD：`020f2bb5f45a50bb3367ea631654702b37b3aab9`。
- `reviewed_source_sha`（**产品**提交）：`020f2bb`（等于本地 HEAD，也等于
  `origin/main`；`git status --porcelain` 为空）。
- 本轮执行位置：**本机本地 checkout**（`D:\ccc\ashare-radar`）。
- **本轮实测全量**：`python -m pytest -o addopts="" -p no:cacheprovider -q`
  → **`1241 passed in 66.92s`，exit 0**。起始基线 `1185`（`79af0dd`），
  本轮区间新增 56 项。
- 真实多日连续 A 股语料仍 `blocked_no_mounted_multiday_continuous_corpus`；
  本轮**无新研究实验、无新增实股结果**。

## 本轮：独立复核 `020f2bb`（该提交自带报告，此前无独立复核）

`020f2bb` 把报告与产品代码打包进**同一次提交**，因此仓库里不存在对它的独立复核。
本轮派 4 个只读子 agent（新提交审查 / 未修项回归 / 机制影响 / **证伪**），
主 agent 亲自复跑每一条关键数字。

| 编号 | 级别 | 状态 | 证据 |
|---|---|---|---|
| `IT-P0-001` | P0 | **已修** | 行为级 `5 failed, 8 passed`（旧源码）；**危害实测**：旧代码 14:58 发 1 条告警，新代码 0 |
| `IT-P1-006-R1` **窄义** | P1 | **已修** | 新代码 13 passed；旧代码 11 failed, 2 passed；`total=301/max_pages=3` → `complete=False` |
| `IT-P1-006-R1` **广义** | P1 | **证伪，仍是缺陷** | 见下 `IT-P1-COMPLETE-001` |

### `IT-P0-001` 危害首次被实测（本轮新增证据）

前几轮只证明"时段判定错"，**未证明真的会发告警**。本轮用仓库自带
`tests/fakes.py` 口径补上：

```text
时点 14:58（旧 phase=afternoon，新 phase=close_auction）
  旧代码: elapsed=14280  告警数=1
  新代码: elapsed=14220  告警数=0
```

### ⚠ 两处对 `020f2bb` 自述叙事的修正

1. **"分母稀释"因果链不成立（已确认错误）**。`volume_burst.py:119` 的
   `only_continuous` 门控在 `:123` 读取 `elapsed` **之前**就 `return []`；
   该时段 :123-191 整段不执行。真实影响是 **7 个 `only_continuous` 规则
   由"被评估"变"被静音"**，不是"速率被稀释"。P0 危害仍成立，但报告不得沿用
   这一叙述。
2. **门控改写确为等价**（比自述更强）：修复前 9 个枚举结果**全部相同**；
   两棵树各自源码按 wall-clock 逐秒比对 21601 秒**零差异**。

## 本轮新发现（5 条，均由主 agent 独立复现）

| 编号 | 级别 | 内容 |
|---|---|---|
| `IT-P1-COMPLETE-001` | P1 | `eastmoney.py:372` 的 `complete` **从不比较** `len(out)` 与 `expected_total`（`:383` 的 `returned` 无人消费）。实测每页只回 10 行 → `returned=600 / expected_total=5913`，仍 `complete=True`，引擎把 **10.1%** 当完整全市场 |
| `IT-P1-TIME-ROLE-002` | P1 | `engine.py:209-210` provider 轻微超前的 `ts` 被**静默改写成本地 now**（实测 `10:00:45` → 存成 `10:00:00`），history 时间轴不是 provider 时间 |
| `IT-P1-TIME-ROLE-003` | P1 | `engine.py:211` 是 `_admit_time` 末行，无陈旧下限；首见码 `ts = now - 3 天` 实测**被接受** |
| `IT-P1-OBS-006` | P1 | `capabilities.py:187` `stale_rejected` 的唯一数据来源是 `t_reject:future`（`engine.py:932/959`）—— **语义相反**，会让人误判"没有陈旧数据" |
| `IT-P1-OBS-007` | P1 | `capabilities.py:192-193` 的 `decisions`/`unavailable_codes` **未被 `as_dict()` 导出**，`store.py:143` 只存 `as_dict()` → 逐股明细到 Store 即消失，削弱 `IT-P1-CAPABILITY-001` 可验收性 |
| `IT-P1-INFO-001` | P2 | `session.py:176-177` docstring 称 `is_tradable_window()` 有"两个历史调用点"，全历史 grep **零生产调用点**（纯测试 API） |

## 仍开着的项（本轮逐条复现，全部仍存在）

- `IT-P1-TIME-ROLE-001`（**最高风险**）：跨源共享水位线，实测新鲜报价被静默丢弃；
  跨源同码 `ts` 差实测 2328 s / 2400 s
- `IT-P1-WINDOW-001`：`engine.py:186` change-only append，横盘越久越算不出跳变
- `IT-P1-LIMIT-001`：`limit_board.py:157` 在 `:243` 门槛判定**之前**写 `"sealed"`，
  首次达标封板被永久吞掉（实测 `[None, None]`，应为 `[None, Alert]`）
- `IT-P1-SOURCE-EMPTY-001`：`engine.py:349-354` 只按异常判失败；soft-partial 四轮
  不触发备用源、health 全绿
- `IT-P1-CAPABILITY-002`：`rules/base.py:43` 能力位是"一轮一个"，无 per-code 归属
- `IT-P1-008`：SSE 无 `id:`/无 `Last-Event-ID`，重连不补拉
- `IT-P1-009`：队列满静默丢最旧（实测**发 250 收 200**），无丢失计数
- `IT-P1-003`：`_series` 主 dict 无驱逐，单调增长

### 清单更正

`tests/test_full_day_simulation.py` **在全部 git 历史中从未入库**
（`git log --all --diff-filter=A` 空输出）——应改称**待新增测试**，不与既有缺陷同列。
`tools/check_phase_sets.py` 同样**尚未创建**（`tools/check_*.py` 实有 8 个，无此项）。

## 测试与门禁（真实执行）

```text
python -m pytest -o addopts="" -p no:cacheprovider -q
    -> 1241 passed in 66.92s          [exit code 0]   基线 1185
020f2bb 新增/改动 8 个测试文件
    -> 160 passed in 1.35s            [exit code 0]
tools/check_*.py  (8 个)              -> 全部 exit 0
node tools/dash_render_check.js       -> exit 0
```

## 下一轮必须核查

1. `IT-P1-COMPLETE-001`（新）：`complete` 须与 `returned` 挂钩；
2. `IT-P1-TIME-ROLE-002/003`（新）：`ts` 改写与陈旧下限；
3. `IT-P1-OBS-006/007`（新）：`stale_rejected` 语义、`decisions` 导出；
4. `IT-P1-TIME-ROLE-001`：跨源水位线（**最高风险**）；
5. `IT-P1-LIMIT-001`、`IT-P1-WINDOW-001`、`IT-P1-SOURCE-EMPTY-001`；
6. `IT-P1-008/009/003`；
7. `tests/test_full_day_simulation.py`（待新增，非回归）；
8. `tools/check_phase_sets.py`（待创建）；
9. WP03 targeted fallback / WP08（语料到位后）。

## 本轮未做（明确不谎报）

- 未启动 web 服务或浏览器，`IT-P1-008` 端到端漏事件率**未测**（仅静态确认）；
- 真实多日连续 A 股语料仍 `blocked`；
- 未跑任何真实通知、交易或付费训练。

## 历史完整审计

- [2026-09-20 13:39 JST](2026-09-20_13-39-26_JST.md) — 本轮；独立复核 020f2bb，证伪 IT-P1-006-R1 广义声称
- [2026-09-20 13:16 JST](2026-09-20_13-16-06_JST.md) — 关闭 IT-P0-001 + IT-P1-006-R1（窄义）
- [2026-09-20 12:19 JST](2026-09-20_12-19-36_JST.md) — IT-P0-002-R2 + 三账本桶
- [2026-09-20 12:07 JST](2026-09-20_12-07-11_JST.md) — 云端轮（产品提交同为 a88094e）
- [2026-09-20 09:32 JST](2026-09-20_09-32-43_JST.md) — 独立复跑 1155；R2 三度复现
- [2026-09-20 08:10 JST](2026-09-20_08-10-29_JST.md) — 云端轮（无 checkout）
- [2026-09-20 05:30 JST](2026-09-20_05-30-48_JST.md) — IT-P0-002-R2 首次发现
- [2026-09-20 05:16 JST](2026-09-20_05-16-58_JST.md)
- [2026-09-20 04:05 JST](2026-09-20_04-05-31_JST.md)
- [2026-09-20 02:41 JST](2026-09-20_02-41-38_JST.md)
