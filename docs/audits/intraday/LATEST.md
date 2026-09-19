# 最新审计

最新完整报告：[`2026-09-20_08-10-29_JST.md`](./2026-09-20_08-10-29_JST.md)

本轮 Agent 任务书：[`2026-09-20_08-10-29_JST_AGENT_TASK.md`](./2026-09-20_08-10-29_JST_AGENT_TASK.md)

## 版本与证据边界（2026-09-20 08:10 JST）

- 仓库与分支：`fy-god/intraday-tape-reader` / `main`。
- 审计开始 HEAD：`60fdee5332ee00fa4d7ec86055b5e61e7b753327`。
- `reviewed_source_sha`：`a88094e915340b4eaec534af2826b5ffef68fe0e`；`60fdee5` 只是 05:30 JST 审计文档提交，不计产品升级。
- 报告提交：`87b0abdedbac2f271c3b66dc401c150d8570774e`。
- Agent 任务书提交：`61ebc61fde496400cfa7e584620350c507912b4b`。
- 当前 PR：无。
- 本轮云端没有完整 repo checkout，因此没有重跑全量 pytest；05:30 JST 本地 Agent 归档的 `1155 passed / 0 failed` 只作为“本地 Agent 回传且 GitHub 已归档、云端本轮未复验”的软件证据。
- 真实多日连续 A 股语料仍 `blocked_no_mounted_multiday_continuous_corpus`，本轮模型实验全部为 synthetic robustness evidence，不是实盘成绩。

## 本轮最高优先：`IT-P0-002-R2` 继续开放

本轮独立机制探针再次复现“乱序水位线滞后”：`EngineState.update()` 用 `history[-1][0]` 判断 out-of-order，但 history 仅在价格/累计量变化时 append；同价同量的新鲜观测虽然已准入并推进 latest，却不推进水位线。固定场景 R1 `t0 10.0/100` → R2 `t+60 10.0/100` → R3 `t+30 9.8/110`，当前控制流仍把 R3 准入、latest 倒退到 9.8、`ooo=0`。独立 accepted watermark 候选则正确拒绝。

但本轮新增一项前置风险 `IT-P1-TIME-ROLE-001`：capability 表对 Tencent/Sina/Eastmoney 都声明 `provider_time=False`，三个 parser 却都填 `Quote.ts`，而 `_admit_time()` 会信任任何非空 `ts`。因此下一步水位线必须和 `source_epoch/time_role` 一起设计，不能跨来源盲用一个 provider-time watermark。

## Observation Ledger 新增/继续开放

- `IT-P2-OBS-002`：被时间准入拒绝的 provider-returned 票同时进入 `unknown_missing` 与 future/ooo reject，语义双计。
- `IT-P2-OBS-003`：`admitted` 混合个股业务 filter 与指数纯时间准入，`returned-admitted` 不能解释成时间拒绝。
- `IT-P2-OBS-004`：fetch-error 轮不覆盖 observation，status 会保留上一成功轮且没有 round identity。
- `IT-P2-OBS-005`（本轮新增）：provider 已返回 `price<=0` Quote 时，当前账本仍把该 code 当 `unknown_missing`；必须区分“provider object 已返回但质量/状态不可用”和“根本没返回”。

主改造收敛到 Observation Ledger v2：拆开 `provider_objects / returned_positive / admitted_time / rule_eligible / unknown_missing / explicit_unavailable / future / ooo / filter_rejected`，并增加 `round_id/status/observed_at`。

## targeted fallback 的架构前置

新增 `IT-P1-CAPABILITY-002`（设计阻断）：当前 `RuleContext` 一轮只有一个 `capabilities`。若下一步把主源 soft-partial 的缺失 code 用备用源定向补齐，同一 Snapshot 会混入多个 provider，单一 capability 必然错判至少一部分股票。实施 targeted fallback 前必须先有 per-code `source/source_epoch/capabilities` sidecar，再让规则按 code 查询字段能力。

## 其他继续开放

- `IT-P0-001`：13:00–15:00 仍整体归为 continuous；SSE/SZSE 官方当前股票时段为 13:00–14:57 连续竞价、14:57–15:00 收盘集合竞价。
- `IT-P1-SOURCE-EMPTY-001`：SourceManager 仍只按异常判断调用成功，没有 requested-vs-returned 完整性和定向 fallback。
- `IT-P1-WINDOW-001`：change-only history 仍丢“新鲜但值未变”的窗口覆盖证据。
- `IT-P1-LIMIT-001`：封单资格状态仍需拆 `at_limit_unqualified -> sealed_qualified`。
- `IT-P1-006-R1`：Eastmoney `required_pages > max_pages` 仍需显式判 incomplete。
- `IT-P1-008/009/003`：SSE 重连补账、慢客户端静默 drop-oldest、series 主体容量继续开放。

已修并继续保留：`IT-P0-003`、`IT-P0-002-R1` 主路径、`IT-P1-006` 主体、`IT-P1-007`。

## 本轮研发实验：`EXP-IT-CAP-003-synthetic-specialist-routing`

不重复大搜索；沿用旧 tune 固定 HGB 参数，用 provider capability 预先冻结特征 schema，比较 full base、global capability-mask、provider specialist、universal intersection。

主 fresh synthetic lockbox：30 个 synthetic symbols、5 个人工日期索引、19,248 candidates；PR AUC：base `0.6454`、capmask `0.6577`、specialist `0.6646`、universal `0.6373`。另外 3 个全新 synthetic lockbox 中 specialist 相对 capmask 的 PR AUC 增量分别 `+0.00615/+0.00793/+0.00986`，四组方向一致。

但是 provider-switch 邻域里 universal intersection PR `0.6637` 略高于 specialist `0.6592`；而 aggressive abstention 代价很高：去 Eastmoney coverage 降至 `93.68%` 只换约 `+0.0019` PR，Tencent-only coverage 降至 `75.68%` 只换约 `+0.0035` PR。因此只把 provider-specialist routing 保留为**真实数据待验证假设**，不部署、不扩大 TCN、不采用激进 abstention。

本轮研究脚本没有持久化 row-level predictions，`predictions.csv` 明确标 `not_saved`；下一关键/真实实验必须补齐。

## 下一轮必须核查

1. `IT-P0-002-R2` 的真实 repo 红/绿/回退验牙，以及 source-epoch/time-role-aware watermark；
2. `source_time_contract.json`，时间角色有证据而不是猜；
3. Observation Ledger v2 的机械对账和失败轮 identity；
4. targeted fallback 前是否已有 per-code provenance/capability；
5. 14:57 close auction、ObservationInterval、limit qualified state、Eastmoney truncation；
6. 真实多日数据若挂载，只跑固定三 HGB（base/capmask/provider-specialist）做 time-forward/provider/unseen-stock；没有真实数据就维持 blocked，不重复 synthetic 冒充实盘进展；
7. 研究运行必须保存 row-level `predictions.csv` 和完整 RUN_MANIFEST。

## 前轮线索

上一份完整报告：[`2026-09-20_05-30-48_JST.md`](./2026-09-20_05-30-48_JST.md)，审计产品提交同为 `a88094e915340b4eaec534af2826b5ffef68fe0e`；该轮本地 checkout 实际跑过 `1155 passed / 0 failed`，本轮云端未重复执行。
