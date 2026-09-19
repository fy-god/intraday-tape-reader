# 最新审计

最新完整报告：[`2026-09-20_05-30-48_JST.md`](./2026-09-20_05-30-48_JST.md)

## 版本与证据边界（2026-09-20 05:30 JST）

- 仓库与分支：`fy-god/intraday-tape-reader` / `main`。
- 审计时间：2026-09-20 05:30 JST（本地 04:30 CST）。
- 本轮开始 HEAD：`a88094e915340b4eaec534af2826b5ffef68fe0e`（04:18 CST，就在本轮触发前约 2 分钟前进）。
- 上游审计 `reviewed_source_sha`：`a3cfd567fb07e414c1d553e5fc478e50892abea8`。
- 本轮性质：**本地 checkout 真实执行**，全部结论在本机复现，含实验/对照。
- 真实多日连续 A 股语料：仍 `blocked`。
- `execution_status`: `COMPLETED`；`research_verdict`: `NOT_EVALUATED`；`evidence_status`: `VERIFIED`。
- 未启动任何真实通知、长期采集、交易或付费训练。

## 本轮最高优先：`IT-P0-002-R2`（同一缺陷类残留，未完全闭合）

`a88094e` 声称修复的 `IT-P0-002-R1`（迟到旧价不得倒退 `quotes`/`last_price`、不得进规则）**主路径确实修好**（我独立复现：R1 10.0 → R2 10.5 → R3 旧价 8.0 后 `last_price=10.5`、`ooo=1`、规则 Snapshot 空）。

**但同一缺陷类以"水位线滞后"形态残留**：乱序守卫用 `history[-1][0]`（最后一次 **append** 的点）作基准，而 `history` 仅在价格或量变化时才追加；`quotes`/`last_price` 却每次准入都推进。于是：

- **实验组**（R2 同价同量，history 不追加）：R3 送 t+30（旧于已接受的 t+60）→ `last_price` 从 10.0 **倒退到 9.8**、**进入规则 Snapshot**、`ooo=0`。
- **对照组**（仅把 R2 改成 10.5，history 推进到 t+60）：同一 R3 **被正确拒绝**，`last_price=10.5`、规则 Snapshot 空、`ooo=1`。

两组唯一差异是 history 是否推进 ⇒ 残留归因于水位线。修法：每票维护 `last_accepted_ep`，每次准入均更新，不复用 `history[-1][0]`。

## 本轮其余已确认

1. **`IT-P2-OBS-002`（本轮新引入的回归）**：`unknown_missing` 语义被 `returned = dict(admitted)`（`engine.py:858`）连带改变。旧版 `a3cfd567` 用**原始返回集**，同一场景得 `()`（正确）；新版得 `('600000',)`。被时间拒绝的票**同时**落入 `unknown_missing` 与 `stale_rejected`，恒等式 `1+1+1=3 ≠ 7` 不成立。`capabilities.py:135` 定义为"请求了但本轮没返回"，与事实矛盾。
2. **`IT-P2-OBS-003`**：`admitted` 混装两套口径（个股走 `filters.accept+ignore`，指数只走时间准入）。单只 `0.01` 价票得 `returned-admitted=1` 而时间拒绝为 0，直接反驳 `engine.py:893-894` 的注释。
3. **`IT-P2-OBS-004`**：失败轮（`engine.py:822-824` 不传 `observation`）不更新账本，`store.py:136` 只在非 `None` 时覆盖 ⇒ `/api/status` 持续展示上一轮数字，且 `as_dict()` **无任何轮次/时间戳 identity**，陈旧性下游不可检测。
4. **`IT-P0-001` 仍开放**：`session.py:39/58/65` 把 13:00–15:00 全判为 `CONTINUOUS`，而文件自身 L4 写明 14:57–15:00 是深市收盘集合竞价。

## 已修好并复核（不重复报错）

`IT-P0-002-R1` 主路径、远未来不进规则、写入前判定（三个 `continue` 均在全部写入之前）、规则不再从原始 `quotes` 重建 —— 四项均由我独立复现确认。

## 测试与门禁（本机真实执行）

```text
python -m pytest -p no:cacheprovider   -> 1155 passed / 0 failed  (50.06s)  exit 0
三个新测试文件                          -> 15 + 11 + 10 = 36 个 test 函数
tools/check_*.py（8 个）                -> 全部 exit 0
node tools/dash_render_check.js         -> exit 0
python tools/check_bom.py               -> exit 0
```

与提交信息核对：`1155 passed / 0 failed` 与"绿测 15 项"**均实测一致**。提交声称的"红测 12 项失败"与"WP02 回退 8 项失败"我**未在仓库内回退文件**（遵守只读纪律），**未独立复现 → 标未复现**。子 agent 报告的"自选股进 Snapshot 但不计数"在本机**未复现**，已降级不写入。

## 流程观察（非代码缺陷）

`a88094e` **把源码修复、自证报告与 `LATEST.md` 索引放进同一个提交**（该仓库惯例是文档另行单独提交并带 `[skip ci]`），导致索引里的 `reviewed_source_sha = a3cfd567` 落后于它自己所在提交的 HEAD。

## 真实模型增益

**零。** 真实多日语料仍 `blocked`；本轮 1155 与 8 个门禁是**软件回归证据**，不构成模型有效性证据。本轮未改源码、未动排程。

---

## 版本与证据边界（2026-09-20 05:16 JST，上一轮）

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
