# 下一步

1. **交易日真实 soak**：打开 `spirit_index` 跑连续会话，让指数分账
   从单元级证据变成盘中证据。这是**最影响用户获得感**的一步。
2. **引擎级收盘竞价兜底**：`engine.py:1910` 门是 `OBSERVABLE`
   （含 CLOSE_AUCTION），`tick_surge.py:101` 门是 `CONTINUOUS`
   （不含）—— 同一事实两个判据。今天靠 7 个规则各自声明
   `only_continuous=True` 才正确，无引擎级兜底。
3. **`reject_by_route` 收敛为三元组**：现为 (future, ooo)，
   而 `stale_hard_rejected` 是兄弟字段，需版本化 schema 迁移。
4. **裸构造改成默认 load()**：需先审计 replay/bench 的刻意用法，
   分两步（先加显式 `TradingCalendar.none()` 工厂，再改默认）。
5. **`stale_hard_rejected` 接进 live_session**：目前只有
   capabilities 导出，观测工具还没读。
