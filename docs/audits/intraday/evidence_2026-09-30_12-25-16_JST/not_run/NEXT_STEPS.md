# 下一步

1. **交易日真实 soak（最高优先）**：打开 `spirit_index` 跑连续会话，
   让 044 的四态合同在真实 dispatch 下被检验。这是最影响用户
   获得感的一步。
2. **`index_requested` 带进轮样本**（云端 §5.2 未做完的部分）：
   `RoundObservationSet` 已导出 `index_requested`，但
   `make_round_sample()` 未保留 —— 新样本仍靠 grade map 键
   推 expected routes，旧样本才需回退。
3. **三集合分离**（云端 §5.3）：expected / produced / measured
   目前 `_seen_here`（produced）与 grade 键（measured）已分开，
   但 expected 仍缺显式 dispatch 分母。
4. **引擎级收盘竞价兜底**：`engine.py` 门是 `OBSERVABLE`（含
   CLOSE_AUCTION），`tick_surge.py` 门是 `CONTINUOUS`（不含）——
   同一事实两个判据，靠 7 个规则各自声明 `only_continuous=True`
   才正确，无引擎级护栏。
5. **`reject_by_route` 收敛为三元组**：现为 (future, ooo)，
   与新 `stale_hard_rejected` 形状不一致，需版本化迁移。
6. **裸构造 `TradingCalendar()` 默认空假期表**：需先审计
   replay/bench 的 4 个刻意用法，分两步迁移。
