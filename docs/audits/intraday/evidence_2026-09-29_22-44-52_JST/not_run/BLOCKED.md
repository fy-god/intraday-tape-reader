# 未运行项与原因

## 真实盘中 soak
**未运行**。非交易时段（A 股休市）+ `spirit_index` 默认关闭。
合成 tick 会污染 Precision/Recall 样本，**拒绝伪造**。

## 真实 Precision / Recall / 漏事件率 / 交易收益
**unavailable**。需要多交易日真实 manifest。

## `stale_hard_rejected` 的真实数据
**生产恒为 0**（三源 `freshness_allowed=false`，STALE 生产不可达）。
capability-ready 遥测，不是会动的曲线。

## 假期表陷阱的"修复"
**未修产品语义**。`TradingCalendar()` 裸构造仍返回空表 ——
改它会波及 replay/bench 等**刻意**用法（replay.py:572、
bench_round.py:73、probe_session_boundaries.py:91、shot_index.py:49）。
本轮改为**钉住陷阱**（测试 + AST 门禁），不改行为。
