# 最新审计

最新完整报告：[`2026-09-20_12-07-11_JST.md`](./2026-09-20_12-07-11_JST.md)

本轮 Agent 任务书：[`2026-09-20_12-07-11_JST_AGENT_TASK.md`](./2026-09-20_12-07-11_JST_AGENT_TASK.md)

## 版本与证据边界（2026-09-20 12:07 JST）

- 仓库与分支：`fy-god/intraday-tape-reader` / `main`。
- 本轮开始 HEAD：`57256e323c181ebf009cc5fcf54dd74ea1d91f46`（09:32 JST 审计文档提交）。
- `reviewed_source_sha`（产品）：`a88094e915340b4eaec534af2826b5ffef68fe0e`；之后到本轮开始 HEAD 没有产品源码变化。
- 本轮完整报告提交：`a43f762770915a2ca97e80361d0cc1a57ecb8f5e`。
- 本轮 Agent 任务书提交：`05dde17e32072f48e7facf6fb2901a165ea02528`。
- 本云端轮没有完整 checkout，未重跑全量 pytest；09:32 本地 checkout 审计已独立得到 `1155 passed in 95.74s`、exit 0，本轮只引用该已归档机器记录，不冒充云端复验。
- 真实多日连续 A 股 corpus 仍未挂载；本轮研究结果均为 synthetic mechanism/robustness evidence，不是实盘 Precision/Recall 或交易收益。

## 本轮新增进展

1. **`IT-P0-002-R2` 方案收敛**：确定性 60,000-object property stress 支持“accepted watermark 与 change-only history 分离，并按 `(code, source_epoch, time_role)` 分段”。设计压力场景中当前 history-tail 机制出现 276 个 flat-hole 错误接受、642 个跨 source epoch 错误拒绝；这些只表示人工压力机制计数，不是市场发生率。
2. **`IT-P1-TIME-ROLE-001` 新 fixture 证据**：同一批响应里的 per-symbol 时间并不一致；Tencent 活跃样本跨度 3s、Sina 61s、Eastmoney `f124` 2256s。因此它们不是整批 receive timestamp；精确 role 仍未知，不能擅自命名为 snapshot/last-trade time，receive time 必须另记。
3. **新增 `IT-P1-INDEX-CURRENT-001`**：`spirit_index.evaluate()` 仍扫描累计 `ctx.state.quotes`。指数本轮缺失时旧 index cache 仍可成为 candidate；本轮机制 probe 构造的旧指数 + 290s 历史仍满足当前点数/bp 触发谓词。需下一轮本地真实 Engine 集成红测。
4. **Observation Ledger v1 的 `IT-P2-OBS-002/003/004/005` 继续开放**：missing/time reject 双计、stock/index admitted 口径混合、失败轮复用旧 observation、zero-price provider object 被误标 missing。
5. **`IT-P0-001` 仍未修**：官方 SSE/SZSE 当前时段均为 13:00–14:57 连续竞价、14:57–15:00 收盘集合竞价；产品仍把整个 13:00–15:00 作为 `AFTERNOON ∈ CONTINUOUS`。
6. **研究负结论**：`EXP-IT-CAP-004-source-epoch-warmup-routing` 在 dedicated synthetic dev 上 K={0,1,3,6,12} 中 **K=0 最优**，因此淘汰 source-switch 后 universal warm-up，不新增该状态机。4 个 fresh synthetic lockbox 上 provider-specialist 相对 capmask 平均约 +0.00575 PR AUC（总体）/+0.01763（切源邻域）；仍仅作为真实数据待验证候选。
7. **研究交付修复**：本轮保存 77,271 行 row-level `predictions_v6.csv`；前轮缺失逐样本预测的问题不再延续。

## 当前开放问题优先级

- P0：`IT-P0-002-R2`、`IT-P0-001`。
- P1：`IT-P1-TIME-ROLE-001`、`IT-P1-INDEX-CURRENT-001`、`IT-P1-SOURCE-EMPTY-001`、`IT-P1-CAPABILITY-002`、`IT-P1-WINDOW-001`、`IT-P1-LIMIT-001`、`IT-P1-006-R1`、`IT-P1-008/009/003`。
- P2：`IT-P2-OBS-002/003/004/005`（但它们是下一阶段 targeted fallback 和真实 coverage 的前置账本问题，实施优先级高于一般 P2）。
- 已修且本轮无新回归证据：`IT-P0-003`、`IT-P0-002-R1` 主路径、`IT-P1-006` 主体、`IT-P1-007`。

## 下一轮必须核查的实际产物

1. `IT-P0-002-R2` 的真实 repo red/green/rollback-tooth：flat admitted 必须推进独立 watermark；不要向 history 无脑追加 flat ticks。
2. `source_time_contract.json`：provider field、time_role、timezone、receive time、source_epoch；证据不足保持 `unknown`。
3. `IT-P1-INDEX-CURRENT-001` 的 Engine→SpiritIndexRule 集成红测：本轮无 current index 时不能用 cache 产新告警，历史仍保留。
4. Observation Ledger v2：round_id/observed_at/status、provider_objects、admitted_time、rule_eligible、mutually-exclusive missing/future/ooo/quality/filter buckets，并有机械恒等式测试；失败轮不能复用上一 observation。
5. per-code source/source_epoch/capabilities 完成后，才允许 soft-partial targeted fallback。
6. 14:57 closing call auction、ObservationInterval、limit qualified state、Eastmoney truncation、SSE cursor/gap 按依赖推进。
7. 若真实多日数据挂载，只跑冻结的 HGB base / capmask / provider-specialist 做 time-forward/provider/unseen-stock；`EXP-IT-CAP-004` 已淘汰 universal warm-up，不再实现；TCN 继续暂停扩张。
8. 回传 row-level predictions、RUN_MANIFEST、数据/切分/因子/config hash、事件对账与同环境成本表。

## 前轮线索

- [2026-09-20 09:32 JST](2026-09-20_09-32-43_JST.md) — 本地 checkout 独立全量 `1155 passed`，R2 红测及 observation 口径复核。
- [2026-09-20 08:10 JST](2026-09-20_08-10-29_JST.md) — provider-specialist synthetic 研究与 Observation Contract 审计。
- [2026-09-20 05:30 JST](2026-09-20_05-30-48_JST.md) — IT-P0-002-R2 首次发现。
- [2026-09-20 04:05 JST](2026-09-20_04-05-31_JST.md)
- [2026-09-20 02:41 JST](2026-09-20_02-41-38_JST.md)
