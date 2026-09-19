# 最新审计

最新完整报告：[`2026-09-19_20-10-46_JST.md`](./2026-09-19_20-10-46_JST.md)

本轮本地 Agent 任务书：[`2026-09-19_20-10-46_JST_AGENT_TASK.md`](./2026-09-19_20-10-46_JST_AGENT_TASK.md)

## 版本与本轮性质

- 仓库与分支：`fy-god/intraday-tape-reader` / `main`。
- 审计时间：2026-09-19 20:10 JST。
- 本轮开始 HEAD：`6d92f50e317de9b8ff4a34a84766347c3de3d041`。
- `reviewed_source_sha`：`34778204dee41575697357f5d2e20a40aad3bc9d`。
- 报告与本轮任务书发布提交：`e34ab87641ff738e35fe48f1698966feba04488f`。
- `3477820 -> 6d92f50` 经 compare 只有审计文档变化，所以本轮没有把文档提交当产品升级。
- 本轮无相关 PR。

本轮性质：**工程审计 + 隔离沙箱研发闭环**。没有修改远端产品代码、测试、配置、Actions 或 PR；没有真实通知、交易或真实行情常驻抓取。

## 已修复并保留回归

- `IT-P0-003`：`3477820` 已将个股规则 Snapshot 改为只消费本轮 `returned`；本轮不再把它重复列为未修。
- `IT-P1-006` 主体保护与 `IT-P1-007` 分路由故障转移记账继续保留。

## 本轮确认/推进的开放问题

- `IT-P0-002`：window 缺 `<= now` 上界，provider 时间没有写前质量门控；机制探针已复现 future point 进入当前窗口。
- `IT-P0-001`：`session.py` 仍把 14:57–15:00 归入连续竞价；SSE/SZSE 官方规则均为收盘集合竞价。
- `IT-P1-SOURCE-EMPTY-001`：Tencent/Sina/Eastmoney 指定代码抓取缺 requested-vs-returned 完整性合同；soft-partial 不会自动补洞。
- `IT-P1-WINDOW-001`：change-only history 压缩会删除“横盘但持续新鲜”的 anchor，横盘后跳变可算不出窗口涨幅。
- `IT-P1-LIMIT-001`：封单未达门槛也先写 `sealed`，后续 100万→300万首次合格封板可被吞。
- `IT-P1-006-R1`：Eastmoney `total` 需要页数超过 `max_pages` 时仍可能 `complete=True`；这是旧完整性修复的残留分支，不是否定主体修复。
- `IT-P2-OBS-001`：`tools/live_session.py` 仍用累计 cache 数和目标 universe 数，不能度量本轮 current coverage。
- `IT-P1-008/009/003/004` 继续开放：SSE补账、慢客户端静默 drop-oldest、series 主体容量、Tencent 默认不安全 TLS fallback。

## 本轮研发执行结果

真实多日连续训练数据没有挂载；仓库可读 raw fixture 只有单股单日分钟趋势等少量样本，因此真实模型训练仍为 `blocked_real_data`。为验证研发链实际可执行，本轮仅以 `synthetic_fixture` 跑通：

- 30 模拟股票 ×20 模拟交易日 ×220 个5秒 tick，共132,000行；仅为 pipeline execution。
- 35 个候选因子、8 个家族；做 50 次未来字段扰动因果性检查，当前时刻因子 0 项变化。
- Rule / Logistic / HGB / MLP / causal TCN 全部完成 fit→checkpoint→reload→predict→evaluate；checkpoint 重载预测差均为0。
- 负控制、6组消融、10组 tune-only 粗筛、3个 expanding walk-forward、seeds 17/29/43 已执行。
- synthetic 初始 HGB ROC/PR AUC = 0.6647/0.7076；TCN 只有213条 synthetic test sequence 且 ROC≈0.503，当前没有扩大神经网络的证据。所有数值都**不是实盘成绩**，真实 Precision/Recall 仍不可用。
- 成功训练脚本 wall 15.54s，Max RSS 821,516KB；粗筛/前推脚本 wall 19.22s，Max RSS 279,340KB。

本轮 `RUN_MANIFEST.json` SHA256：`f570d6a6e54ff21a4931bb0d8e6c77f8094ddb0ef600213bc1fd81900e17abe0`。

## 主实验与下一轮产物

主实验继续 `EXP-IT-OBS-001 v3`：先完成真实 `Round Observation Contract` 的 requested/returned/admitted/unknown_missing/stale/out_of_order/source_mix 影子统计，再把 35 因子迁移到真实字段和真实数据。模型默认只 shadow，不影响正式告警。

下一轮优先核查：

1. 真实 checkout 上 `IT-P0-002`、`IT-P1-WINDOW-001`、`IT-P1-LIMIT-001`、`IT-P1-006-R1`、soft-partial 的修前红/修后绿/回退验牙日志；
2. 真实 `dataset_manifest.json / task_spec.json / split_manifest.json`；
3. 真实字段因子目录、因果性测试、单因子/家族诊断和 trial 台账；
4. 真实数据就绪后同切分的 Rule/Logistic/HGB/MLP/TCN 训练、checkpoint hash、predictions、ablation、walk-forward、多种子；
5. `live_session.py` 的 current coverage 与 committed/sent/received/applied 事件对账。

不存在的 `tests/test_full_day_simulation.py` 已在本轮实际核对为 404；后续如果采用该路径必须标记“待新增”，不能继续写成“已有但 not_run”。

## 历史线索

上一份完整报告：[`2026-09-19_17-59-54_JST.md`](./2026-09-19_17-59-54_JST.md)，其产品修复提交为 `34778204dee41575697357f5d2e20a40aad3bc9d`。

上一次本地研发任务要求更新：[`2026-09-19_19-19-04_JST_AGENT_TASK.md`](./2026-09-19_19-19-04_JST_AGENT_TASK.md)，提交 `6d471270ccd2cce522f196d4d37092b6f1ae48fe`；该任务书更新不是产品修复。
