# 最新审计

最新完整报告：[`2026-09-20_00-09-21_JST.md`](./2026-09-20_00-09-21_JST.md)

本轮本地 Agent 任务书：[`2026-09-20_00-09-21_JST_AGENT_TASK.md`](./2026-09-20_00-09-21_JST_AGENT_TASK.md)

## 版本与本轮性质

- 仓库与分支：`fy-god/intraday-tape-reader` / `main`。
- 审计时间：2026-09-20 00:09 JST。
- 本轮开始 HEAD：`88a5b9fd262704646f0cfb00fe86e680ae731b37`。
- `reviewed_source_sha`：`34778204dee41575697357f5d2e20a40aad3bc9d`。
- 完整报告发布提交：`73b7d3df1719c81948e8f8f5779927cde8d66cd0`。
- Agent 任务书发布提交：`4bba8e76ff6922c6277dd6fb486489d0e49d8afa`。
- `3477820 -> 88a5b9f` 经 compare 只有 `docs/audits/intraday/` 文档变化，因此本轮开始时没有新的产品源码提交。
- 本轮无相关 PR；当前 HEAD 下未发现 `.github/workflows` 目录。

本轮性质：**工程审计 + 隔离沙箱研发闭环**。远端只新增审计 Markdown 与本索引，没有修改产品代码、测试、配置、Actions 或 PR；没有真实通知、交易、持续行情抓取或远程付费训练。

## 已修复并保留回归

- `IT-P0-003`：产品提交 `3477820` 已将个股规则 Snapshot 改为只消费本轮 `returned`；本轮继续视为已修，不重复报错。
- `IT-P1-006` 主体完整性保护与 `IT-P1-007` 分路由故障转移记账继续保留。

## 本轮新增确认

- `IT-P1-CAPABILITY-001`：配置默认 `Tencent -> Sina` failover 时，Sina 不提供 `turnover`，解析器用 `0.0` 表示不可用；默认启用的 `volume_burst` 又把 `turnover < 0.5` 当真实不达标，因此 source 调用可以健康成功、但整类放量规则静默失去可评估能力。根因是业务零值与 provider capability 缺失没有 sidecar 语义。下一步引入 `SourceCapabilities / BatchResult / ObservationDecision / RoundObservationSet`，先把 `evaluable / unavailable_capability / evaluated_no_hit / hit` 显式化，再决定降级策略。

## 继续开放

- `IT-P0-002`：window 缺 `<= now` 上界，provider future/out-of-order 时间没有写前准入。
- `IT-P0-001`：`session.py` 仍把 14:57–15:00 归入连续竞价；上交所/深交所当前规则均为收盘集合竞价。
- `IT-P1-SOURCE-EMPTY-001`：指定代码抓取仍缺 requested-vs-returned / explicit_unavailable / unknown_missing 合同。
- `IT-P1-WINDOW-001`：change-only history 会删除“横盘但持续新鲜”的窗口 anchor。
- `IT-P1-LIMIT-001`：封单从未达门槛到首次达标时仍有状态机吞告警风险。
- `IT-P1-006-R1`：Eastmoney 所需页数超过 `max_pages` 时仍可能错误标 `complete=True`。
- `IT-P2-OBS-001`：`live_session.py` 仍缺本轮 current/capability coverage。
- `IT-P1-008/009/003/004` 继续开放：SSE 补账、慢客户端 drop-oldest、series 主体容量、Tencent TLS fallback。

## 本轮研发推进

真实多日连续 A 股训练数据仍未挂载，真实 Precision/Recall 继续 `unavailable`；本轮不重复上一轮旧 synthetic TCN/粗筛，而是新增 `EXP-IT-CAP-001` 来源能力漂移锁箱实验：

- 新生成此前未参与选择的 30 synthetic symbols × 5 天锁箱，19,458 个候选，专用于 pipeline/robustness 证据。
- 建立 35 因子到 Tencent/Sina/Eastmoney 的真实源码 capability 映射；Sina 只有 L1 盘口子集，Eastmoney 快照无盘口，H 类质量因子必须来自 Round Observation metadata。
- 固定上一轮已选 HGB 参数，对比 base 与 train-only capability-dropout。clean PR AUC：base 0.6790、capdrop 0.6758；provider-shift PR AUC：base 0.6479、capdrop 0.6632。说明 capability-dropout 在合成来源漂移下回收部分退化，但 clean 略降，只能作为真实数据候选。
- Platt / Isotonic 在新锁箱上同时恶化 LogLoss/Brier/ECE，暂不采用默认校准。
- 旧 tune 选出的约 0.407 事件阈值在新锁箱没有带来有意义 Precision 增益，已淘汰，不接 shadow 发布政策。
- family permutation 中 synthetic E 五档压力、A 价格动力贡献最大；与 provider shift 删除盘口能力的退化机制一致。
- HGB CPU 研究微基准 batch1 p50 约 1ms；这些不是服务端端到端数字。

本轮 `RUN_MANIFEST.json` SHA256：`6a93a77f65fe782651a04198fb56b21cfaaa86d6674041f57bc1e8a0e8b9ea37`。

## 主实验与下一轮产物

主实验更新为 `EXP-IT-CAP-001`：优先把 source capability failover 从机制证据迁入真实 checkout，并让工程 observability 与研究 feature mask 共享同一 capability contract。

下一轮优先核查：

1. `IT-P1-CAPABILITY-001` 修前红、修后绿、回退验牙日志与候选 diff；
2. `SourceCapabilities / BatchResult / ObservationDecision / RoundObservationSet` schema 与真实 `requested/returned/missing/capabilities` 影子日志；
3. `live_session.py` 每轮 current coverage 和每规则 `evaluable/unavailable_capability/rejected_quality/evaluated_no_hit/hit`；
4. 真实 `dataset_manifest.json / task_spec.json / split_manifest.json`，量化真实股票数、交易日、采样间隔和字段能力，即使不足 30×20 也要交付；
5. 35 因子的真实 capability mask 与因果性/覆盖诊断；
6. 真实数据足够时只先跑 HGB base vs capability-dropout，同切分输出 checkpoint hash、predictions、provider 分组、ablation；TCN 暂不扩网；
7. `IT-P0-002`、window interval、qualified seal、Eastmoney truncation 与 SSE committed/sent/received/applied 继续推进。

## 历史线索

上一份完整报告：[`2026-09-19_20-10-46_JST.md`](./2026-09-19_20-10-46_JST.md)，其报告/任务书提交为 `e34ab87641ff738e35fe48f1698966feba04488f`，索引提交 `88a5b9fd262704646f0cfb00fe86e680ae731b37`。

此前产品修复报告：[`2026-09-19_17-59-54_JST.md`](./2026-09-19_17-59-54_JST.md)，对应产品修复提交 `34778204dee41575697357f5d2e20a40aad3bc9d`。
