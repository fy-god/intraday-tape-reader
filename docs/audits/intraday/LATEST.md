# 最新审计

最新完整报告：[`2026-09-20_13-16-06_JST.md`](./2026-09-20_13-16-06_JST.md)

## 版本与证据边界（2026-09-20 13:16 JST）

- 仓库与分支：`fy-god/intraday-tape-reader` / `main`。
- 本轮开始 HEAD：`79af0dd`。
- `reviewed_source_sha`（**产品**提交，本轮开始前）：`79af0dd`；
  其后到本轮开始 HEAD 无产品源码变化。
- 本轮执行位置：**本机本地 checkout**（`D:\ccc\ashare-radar`）。
- **本轮实测全量**：`python -m pytest -o addopts="" -q` → **`1241 passed in 89.07s`，exit 0**。
  起始基线 `1185`（`79af0dd`），本轮新增 56 项。
- 真实多日连续 A 股语料仍 `blocked_no_mounted_multiday_continuous_corpus`；本轮**无新研究实验**。

## 本轮：关闭挂了最久的 P0 `IT-P0-001`

`IT-P0-001` 自 **2026-09-18 16:58 报告首次提出**，此后 **9 份报告**反复确认
"仍未修"。本轮专攻它，并**并行**派子 agent 处理独立的 `IT-P1-006-R1`。

| 编号 | 级别 | 状态 | 验牙 |
|---|---|---|---|
| `IT-P0-001` 14:57-15:00 收盘集合竞价 | **P0** | **已修（首次实现）** | 5 项**行为级**红 |
| `IT-P1-006-R1` Eastmoney 截断报 complete | P1 | **已修** | 11 项红 |

### `IT-P0-001` 机制

`phase()` 把 13:00-15:00 **整体**返回 `AFTERNOON`，而 `AFTERNOON ∈ CONTINUOUS`。
于是收盘集合竞价在**三个层面**被当成连续竞价：

1. **规则层**：7 个 `only_continuous=True` 规则照常评估 —— 该段无连续成交，
   价格由 15:00 一次性撮合决定，触发出来的"急拉/放量"是撮合假象；
2. **量能时钟层**：`elapsed_trading_seconds()` 把该段计入分母，导致
   `volume_burst` 的 `avg_per_min = volume_lots / elapsed * 60` 出现
   **"分子不动、分母继续涨"**，放量速率被人为稀释；
3. **展示层**：看板显示"午盘交易中"。

**代码与自身文档矛盾**：`session.py:4` 从第一天起就声明了该时段。

修复前实测：

```text
14:58 的 phase()  = afternoon      <- 被判为连续竞价
is_open(14:58)    = True           <- 应为 False
elapsed(14:57)    = 14220.0
elapsed(14:57:30) = 14250.0        <- 分母在涨
elapsed(14:58)    = 14280.0
elapsed(14:59)    = 14340.0
elapsed(15:00)    = 14400.0        <- 旧"全天 4 小时"
```

这段输出正是 12:07 报告所说**"只增加枚举但量能时钟仍算到 15:00 不算完成"**
的实证。

### 修法（严格按 12:07 报告要求五处一起改）

1. 新增 `SessionPhase.CLOSE_AUCTION = "close_auction"` 与 `PHASE_CN` 标签；
2. `phase()` 拆出 14:57 边界；
3. `CONTINUOUS` 保持不变（`CLOSE_AUCTION` 本就不该在里面）；
4. 新增 `CALL_AUCTIONS` / `OBSERVABLE` / `is_call_auction()`；**不改**
   `is_open()` 语义，而是修正 `is_tradable_window()` 的成员列表；
5. `elapsed_trading_seconds()` 下午右边界收到 **14:57** → 全天 **14220** 秒；
6. 引擎门控改用具名集合 `OBSERVABLE`；
7. 看板补 `close_auction` 标签；`describe()` 对集合竞价标"（无连续成交）"；
8. 模块 docstring 修正（旧注释误标"深市"专属，实际沪深一致）；
9. 7 个规则**无需逐个改** —— 统一 `ctx.session not in CONTINUOUS`，
   新时段自动静音，这就是既有的"显式声明"形态。

修复后：14:56:59 仍 `is_open=True`（未过度修正）；14:57-14:59:59 为
`close_auction`、`is_open=False`；量能分母 14220 全程冻结；引擎 14:58 仍抓
行情、15:30 与 09:27 不抓。

### 为什么值得注意

`test_session_close_auction.py` 导入了**新符号**，在修复前代码上会
`ImportError` —— 那是**结构性**红，不证明行为变了。因此另建
`test_session_close_auction_behavioral.py`，**只用修复前就存在的 API**，
它在旧代码上按断言失败。验牙两次均为纯 `AssertionError`，其中最硬一条：

```text
E  AssertionError: 收盘集合竞价期间分母不应增长：[14220.0, 14250.0, 14280.0, 14340.0]
```

## 上轮结论复核

- `IT-P0-002-R2`、`IT-P2-OBS-003/004/005`：**确认已修、无回归**；
- `IT-P0-002-R1`、`IT-P0-003`、`IT-P1-006` 主体、`IT-P1-007`、
  `IT-P0-004/005`（BOM）：**确认已修、无回归**；
- `IT-P1-CAPABILITY-001`：仍为**阶段一部分修复**，降级口径**未决定**，不下调。

## 本轮未做（明确不谎报）

- `IT-P1-TIME-ROLE-001` `source_epoch`/`time_role`（**仍未修**，剩下的最高风险项）
- `IT-P1-WINDOW-001`、`IT-P1-LIMIT-001`、`IT-P1-SOURCE-EMPTY-001`、
  `IT-P1-CAPABILITY-002`、`IT-P1-008/009/003`
- `tests/test_full_day_simulation.py`（**欠了多轮**）
- WP03/WP08 其余、真实多日语料（`blocked`）

## 测试与门禁（真实执行）

```text
python -m pytest -o addopts="" -q   -> 1241 passed / 0 failed  (89s)
                                       基线 1185；本轮新增 56 项
tools/check_*.py                    -> 8 个全部 exit 0
node tools/dash_render_check.js     -> exit 0
python tools/check_bom.py           -> exit 0
```

## 下一轮必须核查

1. `IT-P1-TIME-ROLE-001` `source_epoch`/`time_role`：`provider_time=False` 时
   `ts` 只允许同源排序，跨源比较须**显式失败**；
2. `IT-P1-LIMIT-001` limit qualified-state；
3. `IT-P1-WINDOW-001` `window()` 只读 change-only history；
4. `IT-P1-SOURCE-EMPTY-001` requested-vs-returned 完整性判定；
5. `tests/test_full_day_simulation.py`；
6. `tools/check_phase_sets.py`（RF-01 可执行形态）；
7. WP03 targeted fallback / WP08（语料到位后）。

## 历史完整审计

- [2026-09-20 13:16 JST](2026-09-20_13-16-06_JST.md) — 本轮；关闭 IT-P0-001 + IT-P1-006-R1
- [2026-09-20 12:19 JST](2026-09-20_12-19-36_JST.md) — IT-P0-002-R2 + 三账本桶
- [2026-09-20 12:07 JST](2026-09-20_12-07-11_JST.md) — 云端轮（产品提交同为 a88094e）
- [2026-09-20 09:32 JST](2026-09-20_09-32-43_JST.md) — 独立复跑 1155；R2 三度复现
- [2026-09-20 08:10 JST](2026-09-20_08-10-29_JST.md) — 云端轮（无 checkout）
- [2026-09-20 05:30 JST](2026-09-20_05-30-48_JST.md) — IT-P0-002-R2 首次发现
- [2026-09-20 05:16 JST](2026-09-20_05-16-58_JST.md)
- [2026-09-20 04:05 JST](2026-09-20_04-05-31_JST.md)
- [2026-09-20 02:41 JST](2026-09-20_02-41-38_JST.md)
