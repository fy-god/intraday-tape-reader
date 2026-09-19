# 最新审计

最新完整报告：[`2026-09-20_05-16-58_JST.md`](./2026-09-20_05-16-58_JST.md)

## 版本与证据边界

- 仓库与分支：`fy-god/intraday-tape-reader` / `main`。
- 审计时间：2026-09-20 05:16 JST（本地 04:16 CST）。
- 本轮开始 HEAD：`b1de0d83d820198f7230aa20d520a255ea3feb43`。
- 上游审计 `reviewed_source_sha`：`a3cfd567fb07e414c1d553e5fc478e50892abea8`。
- 本轮性质：**本地 checkout 真实执行**，全部结论在本机复现。
- 真实多日连续 A 股语料：仍 `blocked`。
- 未启动任何真实通知、长期采集、交易或付费训练。

## 本轮最高优先：`IT-P0-002-R1`（上游提出，已修）

上游指出的是**我上一轮修复的漏洞**，且**完全正确**：

`a3cfd56` 把时间准入修在 `EngineState` 内部，但 `poll_once()` 随后又从
**原始 `quotes`** 重建规则 Snapshot —— 等于只保护了 State、没保护规则。
`update()` 返回 `None`，调用方拿不到准入结果，只能自己重算，两边口径必然
分叉。同时乱序分支在判断**之前**已写 `state.quotes`，最后无条件写
`last_price`，迟到旧价会把最新缓存倒退。

修法：**单一 admitted-current 合同** —— `update()` 返回
`dict[str, Quote]`；乱序判定提到所有写入之前；`poll_once()` 消费返回值；
股票与指数共用同一准入。

红测 12 项失败 → 绿测 15 项通过。

### 一处必须记录的坑

第一版测试用固定 `NOW=2026-09-15`，但引擎用真实时钟（2026-09-20），
"未来 600 秒"其实是**过去**，测试假绿。改用注入的 `_Clock` 才真正测到。
否则本轮回归是一组**永远通过**的无效测试。

## `IT-P2-OBS-001` / WP02（已修）

`RoundObservationSet` 从 `poll_once()` 局部升格为 Store/status/
live_session 可读的事实源：

- `store.py`：`_observation` 有界汇总 + `observation` property +
  `status()["observation"]`；
- `engine.py`：`set_poll_stats(..., observation=...)`；
- `live_session.py`：新字段 `requested/returned/admitted/coverage/source/
  future_rejected/out_of_order_rejected/unknown_missing/
  unavailable_capability/capabilities`，并加 `_safe_int/_safe_float`
  保证脏值不打断长时间 soak。

修掉了"用累计 `len(state.quotes)` 冒充本轮行情规模"。

**WP02 核心验收通过**：Tencent→Sina 后服务仍健康（`admitted>=1`），
但 `capabilities.turnover=False` 明确可见，不是只看到 `alerts=0`。

回退验牙：8 项失败。

## 已修复并保留回归

`IT-P0-003`、`IT-P0-004/005`（BOM）、`IT-P1-CAPABILITY-001`（Sina 占位零，
按上游判定为**阶段一部分修复**，降级口径仍未决定）、`IT-P0-002`、
`IT-P1-006` 主体、`IT-P1-007`。

## 本轮未完成（明确不谎报）

本轮把 **WP01（最高优先）与 WP02** 做透并验牙，其余**未做**：

- WP03 soft-partial / 定向 fallback
- WP04 收盘集合竞价（`IT-P0-001`）
- WP05 ObservationInterval（`IT-P1-WINDOW-001`）
- WP06 limit qualified-state（`IT-P1-LIMIT-001`）
- WP07 Eastmoney truncation（`IT-P1-006-R1`）
- WP08 35 因子 capability mask
- WP09 真实 manifest 与 HGB 三模型对照
- WP10 shadow / SSE 对账
- `IT-P1-008/009/003/004`、per-route idx 分账

理由：WP01 是唯一 P0 且是"上游指出上轮没修好"的项，WP02 是其直接下游。
其余各项都涉及独立契约变更（session 语义牵动 7 个规则、Eastmoney 分页、
研究目录），半做会留下比不做更危险的中间态。

## `IT-P2-REPORT-001`（上游提出：报告数字不一致）

批评成立。`1103` 是修完 capability/IT-P0-002 后的中途数字，`1114` 是加了
卫生测试与时间准入测试后的终值；我引用中途数字时未标注，造成矛盾。
本轮起统一：报告、LATEST、commit message 只用**同一份终值**。
本轮终值 **1155 passed**（起始基线 1114）。

## 测试与门禁（真实执行）

```text
python -m pytest                 -> 1155 passed / 0 failed  (72s)
                                   基线 1114；本轮新增 41 项
tools/check_*.py                 -> 8 个全部 exit 0
node tools/dash_render_check.js  -> exit 0
python tools/check_bom.py        -> exit 0
```

## 下一轮优先核查

1. WP04 收盘集合竞价 —— 边界 14:56:59 / 14:57:00 / 14:59:59 / 15:00:00，
   并同步 `elapsed_trading_seconds` 与 7 个规则的 `only_continuous`；
2. WP03 soft-partial requested/returned/unknown_missing + 定向 fallback；
3. WP07 Eastmoney `required_pages > max_pages` → `complete=false`；
4. WP06 limit qualified-state；
5. WP05 ObservationInterval；
6. WP08/WP09（语料到位后）；
7. `tools/check_admission_bypass.py`（RF-01 可执行形态）。
