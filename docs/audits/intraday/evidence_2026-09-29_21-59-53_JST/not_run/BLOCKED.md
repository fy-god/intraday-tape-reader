# 未运行项与原因

## WP09 实盘 soak（真实盘中观测）
**未运行**。本会话为非交易时段（2026-09-29，A 股休市），且
`spirit_index.enabled=false`。合成 tick 会污染 Precision/Recall 样本，
**拒绝伪造**。需要交易日 09:30–11:30 / 13:00–15:00 的连续会话。

## `stale_hard_rejected` 的真实数据
**在生产上恒为 0**。三个生产源的 `freshness_allowed` 都是 false，
故 `AdmitReason.STALE` 在生产**不可达**。该字段是 capability-ready
遥测（合同打开后才会有值），不是会动的曲线。诚实标记。

## `reject_by_route` 仍是 (future, ooo) 二元组
**未扩展**。改成三元组会破坏已冻结的 JSON schema、`live_session.py`
读者与多个测试。本轮以**新增兄弟字段** `stale_hard_rejected` 的方式
additive 解决，未动既有形状。

## WP10 研究增量
**未运行**。需要真实多交易日 manifest。当前 unavailable。
