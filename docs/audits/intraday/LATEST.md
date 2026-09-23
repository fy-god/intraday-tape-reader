# 最新审计

**最新云端独立审计**：[2026-09-24_00-05-20_JST.md](./2026-09-24_00-05-20_JST.md)  
**最新云端 Agent 任务书**：[2026-09-24_00-05-20_JST_AGENT_TASK.md](./2026-09-24_00-05-20_JST_AGENT_TASK.md)  
**最新本地产品代码提交 / reviewed_source_sha**：`a0ca7e6caff972d22b903a80b1731e2b7d5a2f95`  
**本轮审计开始 docs HEAD**：`5d7254318d180bf8324fdf692667cd428d6df0b1`  
**report_commit_sha**：`4bdac46ab1bc46c1e51aa0c737861066b57b8f5c`  
**agent_task_commit_sha**：`6880dff4922ecdb8c513054b6fe1137e2ab19d11`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/5d7254318d180bf8324fdf692667cd428d6df0b1/docs/audits/intraday/LATEST.md

> `a0ca7e6..5d725431` 只有审计文档 / evidence / `LATEST.md`，没有产品代码变化。历史报告文件未删除；本文件只移动当前接续指针。

## 2026-09-24 00:05:20 JST

主实验：`EXP-IT-PRE-WP03-GATE-013`

### 当前最高优先 gate

1. `IT-P2-TENCENT-NAME-BLIND-REQUEST-KEY-REGRESSION-002` — **P1**：request 轴用空名称、raw/Quote 轴用 provider 真名称；7 个名称唯一定身份的指数已被独立 probe 确认回归。
2. `IT-P2-WP01-AXIS-SUITE-BLINDNESS-005` — **P1**：把两轴都改成名称盲的错误修法仍 `1891 passed`，现有测试无法区分“修对/改错”。
3. `IT-P2-CALL-DETAILED-TYPE-ERROR-ABORTS-FAILOVER-004` — **P1**：wrong-type detailed return 在 per-source `try` 外抛错，健康备源不被尝试。
4. `IT-P1-SNAPSHOT-RAW-LEDGER-COLLAPSE-001` — **PARTIAL**：source exact outcome + `call_detailed` 已有，但生产 `poll_once` 调用点仍为 0；在线 RoundObservation 仍 Quote-only。
5. `IT-P2-LEGACY-FALLBACK-PHANTOM-MISSING-003` — 接线前 P1 风险：legacy prefixed request 与裸 `Quote.code` 可能错轴。
6. `IT-P2-CALL-DETAILED-ROUTE-MISBIND-006` — **本轮新 P2**：Manager 原子修正 source，但不校验/修正 `SnapshotFetchResult.route`；第三方/半迁移源可让 outcome route 与 Manager route 账本分叉。

### 本轮机制结果

固定最新审计符号集 15 个：

```text
修前“显式前缀即保留”          13/15 同轴
当前“request 侧名称盲”          8/15 同轴
候选“caller/route typed role”  15/15 同轴
```

这不是线上正确率，只是确定性审计符号集合的机制对照。

### WP03 进入条件

在 Engine 接 `call_detailed` 前必须先完成：
- name-sensitive index + safe index + explicit stock 的表驱动测试；
- M6 mutation 必须行为性变红；
- request/raw/Quote 三轴消费同一身份 role；
- `call_detailed` type/source/route 校验全部位于 failover `try` 内；
- legacy prefixed identity 测试；
- WP03 新生产代码禁止 generic `coverage` 别名。

之后才允许：
`Engine unified Round Attribution -> Membership -> exact soft-empty -> timezone/window/provenance -> research`。

模型训练：0；真实 Precision/Recall/漏事件率/交易收益仍 `unavailable`。
