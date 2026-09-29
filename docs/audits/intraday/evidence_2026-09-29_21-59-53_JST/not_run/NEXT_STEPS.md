# 下一步

1. **`reject_by_route` 收敛为三元组**：现在 `stale_hard_rejected` 是
   兄弟字段，而 `reject_by_route` 只有 (future, ooo)。两个
   语义相邻的账在两个形状里 —— 需要在一次**版本化的 schema
   迁移**里合并，不能就地改。
2. **WP09 soak**：交易日打开 `spirit_index` 跑连续会话，让指数分账
   从单元级证据变成盘中证据。
3. **`call` / `call_detailed` 合并为一份 failover+记账**：本轮已用
   真实代码证明两者**行为一致**（4 条路径探针），所以是
   bug 类 (b) 的**潜在**风险而非已确认缺陷。但两份手写逻辑
   迟早会漂移 —— 合并成一个私有实现，两者都调它。
4. **`tencent.snapshots()` 的 `idx_set` 推导**：仍由"调用方是否写了
   显式前缀"推导（v1 根因）。Sina 现在有 caller-owned
   `index_codes` 属性 + 引擎注入，腾讯应对齐同一机制。
5. `tests/test_full_day_simulation.py` 仍未编写。
