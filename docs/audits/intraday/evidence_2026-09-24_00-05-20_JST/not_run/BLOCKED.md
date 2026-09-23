# 未产出 / blocked 清单 —— 2026-09-24 05:52:56 JST

任务书 §研究阶段物化要求下列文件。**本轮未产出，原因是 blocked，非"忘了"。**

## blocked：缺少真实多日盘中语料

| 文件 | 阻塞原因 |
|---|---|
| `dataset_manifest.json` | 需要**真实多日**盘中快照语料（含各源原始响应）。仓库内只有单测夹具与 bootstrap 期样本，不足以构成可训练/可评测数据集。 |
| `task_spec.json` | 依赖上一条。 |
| `split_manifest.json` | 依赖上一条；无数据则无法定义 train/valid/test 切分。 |
| `factor_catalog.csv` | 因子目录可**枚举**（35 因子 capability mask 已有雏形），但"每个因子的可用性"必须用真实数据验证，否则是编造。 |
| `factor_diagnostics.csv` | **blocked**：需要真实分布；凭空生成 = 编造数字，明令禁止。 |
| `trials.jsonl` | **blocked**：需要真实试验记录。 |
| `training_log.jsonl` | **blocked**：本轮未训练任何模型。 |
| `event_reconcile.json` | **blocked**：需要真实盘中行情 + 人工标注事件，才能核对"系统报了什么 / 实际发生了什么"。 |
| `latency_memory.csv` | **blocked**：需要真实运行会话的延迟序列。 |

## 为什么不能"先写个占位"就交

任务书与既有轮审规范都明令：
**绝不虚构或硬编码假错误**、**不编造指标数字**。
生成结构正确但内容虚构的 `factor_diagnostics.csv`
会污染后续所有依赖它的结论 —— 那正是本仓库主导 bug 类
"证据子集自称全程"的翻版。

## 解除阻塞需要什么

1. 在**真实交易时段**连续采集 ≥ 5 个交易日的完整盘中快照（含 raw 响应）；
2. 保留每次请求的 `route` / `source` / 时间戳，才能算 `latency_memory`；
3. 人工标注至少一批急拉/急跌事件，才能算 Precision/Recall/漏报率。

以上三步**均不在本轮硬性边界内**（不改部署配置、不启动行情服务），
需要用户明确授权才可进行。
