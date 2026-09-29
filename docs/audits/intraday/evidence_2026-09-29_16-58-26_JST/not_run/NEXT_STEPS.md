# 下一步

1. **WP08 剩余**：把 `raw_presence_known_by_route` 接进
   `tools/live_session.py` 的健康/出处输出 —— 让'投影反推'在
   实盘观测里显式可见，而不是只存在于账本字段。
2. **WP09 soak**：交易日 09:30–15:00 连续会话，打开 `spirit_index`，
   记录指数与平安银行是否真的分账（本轮已给出单元级确定性证据，
   但需要真实 tick 佐证）。
3. **`_admit_time` 的字符串 `why` 协议**改成 `Literal`/enum ——
   现在是自由字符串，拼错不会被发现（bug 类 d 的温床）。
4. **`call` / `call_detailed` 仍是两份 failover+记账逻辑**
   （同一语义实现两次 = bug 类 b）。本轮已让生产走 detailed，
   但 `call` 的 try 边界从未审计过同一类缺陷。
5. `IT-P1-006-R1`：Eastmoney `max_pages` 截断的'已完成'标记
   需要独立复核 —— 上轮修复只覆盖 no-total 路径。
