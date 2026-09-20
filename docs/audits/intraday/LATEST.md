# 最新审计

最新完整报告：[`2026-09-20_12-19-36_JST.md`](./2026-09-20_12-19-36_JST.md)

## 版本与证据边界（2026-09-20 12:19 JST）

- 仓库与分支：`fy-god/intraday-tape-reader` / `main`。
- 本轮开始 HEAD：`57256e3`。
- `reviewed_source_sha`（**产品**提交）：`a88094e915340b4eaec534af2826b5ffef68fe0e`；
  `60fdee5`/`87b0abd`/`61ebc61`/`ac9011a`/`57256e3` 均为审计文档/索引提交，**不计产品升级**。
- 本轮执行位置：**本机本地 checkout**（`D:\ccc\ashare-radar`）。
- **本轮实测全量**：`python -m pytest -o addopts="" -q` → **`1185 passed in 78.02s`，exit 0**。
  起始基线 `1155`（`a88094e`），本轮新增 30 项。
- 真实多日连续 A 股语料仍 `blocked_no_mounted_multiday_continuous_corpus`；本轮**无新研究实验**。

## 本轮：修复上游 09:32 报告点名的四个账本/水位线缺陷

上游 `2026-09-20_09-32-43_JST.md` 在我推送 `a88094e` 后**独立复跑全量**
（`1155 passed`，与自报一致）并**逐个复现**了我没修到的缺陷。本轮不重复其
覆盖内容，直接进入修复。

| 编号 | 状态 | 证据 |
|---|---|---|
| `IT-P0-002-R2` 平价观测不推进水位线 | **已修** | 行为级 red 断言与上游逐字一致；12 项回归；回退 **10 failed** |
| `IT-P2-OBS-005` `price<=0` 被记成「没返回」 | **已修** | 三桶互斥 + 恢复 `rejected_quality` 发射点；10 项回归；回退 **10 failed** |
| `IT-P2-OBS-003` `admitted` 混算两类语义 | **已修** | 改纯时间准入口径 + 个股/指数分账 |
| `IT-P2-OBS-004` 失败轮沿用上轮 observation | **已修** | `observation_seq` 归属判定；8 项回归；回退 **4 failed** |
| 账本恒等式差额 5 | **本轮新发现** | `requested` 含指数而桶只覆盖个股 → 新增 `index_requested`/`stock_requested` 后对平 |

### `IT-P0-002-R2` 机制（P0）

水位线借用 `history[-1][0]`，而 `history` 只在**价格或累计量变化**时追加
（服务窗口采样）。于是**平价新鲜观测**（同价同量）虽推进 `quotes`/`last_price`，
却**不推进水位线** → 下一个真正迟到的点被放行，覆盖 latest、序列倒挂、
`out_of_order` 为 0。

修法：引入**独立显式** `accepted_watermark`，准入成功时**无条件推进**；
`history` 的"仅变化追加"语义保留不动；`prune()` 同步回收水位线。

**关键**：行为级验收（只用 `admitted`/`latest`/`ooo`，不碰新属性）在修复前
代码上实测红，断言消息与上游报告**逐字一致**：
`AssertionError: 迟到的 R3 必须被拒，实际被准入: ['600000']`。

### 上轮结论复核

- `IT-P0-002-R1` 主路径：**成立、已修、无回归**（上游 09:32 判定准确）。
- `IT-P1-006` 主体、`IT-P1-007`、`IT-P0-003`、`IT-P0-004/005`（BOM）：**确认已修、无回归**。
- `IT-P1-CAPABILITY-001`：仍为**阶段一部分修复**，降级口径**未决定**，故**不下调**。

## 本轮未做（明确不谎报）

- `IT-P0-001` 14:57–15:00 收盘集合竞价（**仍未修**）
- `IT-P1-TIME-ROLE-001` `source_epoch`/`time_role`（**仍未修**）
- `IT-P1-006-R1` Eastmoney 分页截断不判 incomplete（**仍未修**）
- `IT-P1-WINDOW-001`、`IT-P1-LIMIT-001`、`IT-P1-SOURCE-EMPTY-001`、
  `IT-P1-CAPABILITY-002`、`IT-P1-008/009/003`
- `tests/test_full_day_simulation.py`
- WP03/WP08 其余、真实多日语料（`blocked`）

理由：本轮选的是**根因集中、可在同一片代码内修透**的四条
（`EngineState.update()` 与 `RoundObservationSet`），且都能配可失败的行为级
回归。`IT-P0-001`（牵动 7 个规则的 session 语义）与 `IT-P1-TIME-ROLE-001`
（需先建 `source_epoch` 契约）是契约级重构，半做比不做更危险。

## 测试与门禁（真实执行）

```text
python -m pytest -o addopts="" -q   -> 1185 passed / 0 failed  (78s)
                                       基线 1155；本轮新增 30 项
tools/check_*.py                    -> 8 个全部 exit 0
node tools/dash_render_check.js     -> exit 0
```

工具提示：`pyproject.toml` 设 `addopts="-q"`，命令行再加 `-q` 会吞掉汇总行；
取真实计数须用 `-o addopts=""`。

## 下一轮必须核查

1. `IT-P0-001` 收盘集合竞价 —— 边界 14:56:59 / 14:57:00 / 14:59:59 / 15:00:00，
   同步 `elapsed_trading_seconds` 与 7 个规则的 `only_continuous`；
2. `IT-P1-TIME-ROLE-001` `source_epoch`/`time_role`：`provider_time=False` 时
   `ts` 只允许同源排序，跨源比较须**显式失败**；
3. `IT-P1-006-R1` Eastmoney `required_pages > max_pages` → `complete=false`；
4. WP03 targeted fallback 前先落 per-code provenance（本轮已铺分账）；
5. `tests/test_full_day_simulation.py`；
6. WP08/WP09（语料到位后）；
7. `tools/check_admission_bypass.py`。

## 前轮线索

上一份**本机**完整报告：[`2026-09-20_05-16-58_JST.md`](./2026-09-20_05-16-58_JST.md)（产品提交 `a88094e`）。

## 历史完整审计

- [2026-09-20 12:19 JST](2026-09-20_12-19-36_JST.md) — 本轮；R2 + 三账本桶修复
- [2026-09-20 12:07 JST](2026-09-20_12-07-11_JST.md) — 云端轮（产品提交同为 a88094e）
- [2026-09-20 09:32 JST](2026-09-20_09-32-43_JST.md) — 独立复跑 1155；R2 三度复现
- [2026-09-20 08:10 JST](2026-09-20_08-10-29_JST.md) — 云端轮（无 checkout）
- [2026-09-20 05:30 JST](2026-09-20_05-30-48_JST.md) — IT-P0-002-R2 首次发现
- [2026-09-20 05:16 JST](2026-09-20_05-16-58_JST.md)
- [2026-09-20 04:05 JST](2026-09-20_04-05-31_JST.md)
- [2026-09-20 02:41 JST](2026-09-20_02-41-38_JST.md)
