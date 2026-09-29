# 未运行项与原因

## WP09 实盘 soak（5 分钟真实盘中观测）
**未运行**。原因：本会话为**非交易时段**（当前 2026-09-29，A 股休市），
且 `spirit_index.enabled=false` 默认关闭。真实盘中 tick 无法在桌面上
合成 —— 伪造 tick 会污染 Precision/Recall 的样本。
需要：一个**交易日 09:30–11:30 / 13:00–15:00** 的连续会话，
且显式打开 `spirit_index`。

## WP08 per-route Health/Provenance 端到端
**部分未运行**。`raw_presence_known_by_route` 已进账本并被单元测试
覆盖（AST + 行为），但**尚未**接进 `tools/live_session.py` 的
健康判定输出。原因：改动 `live_session.py` 属部署/观测工具层，
超出本会话'只读审计报告 + 改产品代码 + 上传'的边界。
已在 `NEXT_STEPS.md` 记为待办。

## WP10 研究增量
**未运行**。需要一个真实多交易日 manifest 才能计算
Precision/Recall/漏事件率。当前 `unavailable`，本轮**不编造数字**。
