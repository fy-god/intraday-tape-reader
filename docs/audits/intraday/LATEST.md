# 最新审计

**最新云端独立审计**：[`2026-09-22_12-03-20_JST.md`](./2026-09-22_12-03-20_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-22_12-03-20_JST_AGENT_TASK.md`](./2026-09-22_12-03-20_JST_AGENT_TASK.md)  
**最新被审产品 SHA**：`369b13f0d6f20fc22198abf51469ee14e49c84dc`  
**审计时间**：2026-09-22 12:03:20 JST  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/bba01b1751ca3c2214dcd09f8321578634bf56a3/docs/audits/intraday/LATEST.md

> 历史报告文件没有删除或覆盖；为避免把 30KB+ 历史索引每轮重复复制并引入冲突，旧索引正文固定保留在上面的不可变 commit 快照中。本文件只移动“当前接续”指针。

## 本轮范围

- `reviewed_source_sha = 369b13f0d6f20fc22198abf51469ee14e49c84dc`
- 审计开始 `main = bba01b1751ca3c2214dcd09f8321578634bf56a3`；`369b13f0d6f20fc22198abf51469ee14e49c84dc..bba01b1751ca3c2214dcd09f8321578634bf56a3` 只有 `docs/audits/intraday/` 文档变更。
- 开放 PR：0。
- 已读：HEAD/提交差异/递归源码树/README/CHANGELOG/config/Engine/Store/Session/SourceManager/live-session/tests/LATEST/NEXT_STEPS。
- 固定产品树未发现 `.github/workflows` 目录；不据此推断仓库外自动化。
- 本轮未运行完整 repo pytest；最近独立机器证据继续采用 10:05 审计的 `1745 passed in 95.98s + 14 probes exit 0`。
- 真实 Precision/Recall/漏报率/收益：`unavailable`。

## 当前开放项

1. `IT-P1-UNIVERSE-COVERAGE-GATE-TRUNCATION-BLIND-001`
2. `IT-P1-UNIVERSE-META-STALE-AFTER-FAILED-REFRESH-001`
3. `IT-P1-HEALTH-COVERAGE-SNAPSHOT-PRELOOP-001`（静态/机制成立，真实 soak RED 待跑）
4. `IT-P0-002-TZ-R1`（P1）
5. `IT-P1-SOURCE-EMPTY-001`
6. `IT-P1-WINDOW-001`
7. `IT-P1-UNIVERSE-REFRESH-STORM-001`（机制风险，真实 import 频率验牙前不升级）

## 主实验

`EXP-IT-UNIVERSE-FAILURE-003`

核心结果（均为确定性软件机制，不是实盘）：
- unknown denominator + `transport_complete=false`：当前判 `OK/not_measured`，候选 v4 判 FAIL；
- retained full + recent partial reject：当前 OK，候选 WARN；
- retained full + stale failed/rejected refresh：当前 OK，候选 FAIL；
- primary snapshots 返回 `[]`：当前 backup 不调用；抛异常时 backup 正常调用；
- fresh-flat history：当前可出现 `_covered=True` 但 `price_change=None`。

## 主改造

**Universe Refresh Truth Contract v4**：
- `active_snapshot`
- `latest_attempt`
- `freshness`
- session-level universe truth aggregation

不采用“失败就清 `_universe_meta`”或“失败完全不动 `_universe_meta`”两个极端方案。

## 下一轮必核查产物

- `universe_refresh_truth_cases.json`
- `universe_session_snapshot_cases.json`
- `timezone_cases.json`
- `source_empty_cases.json`
- `observation_interval_cases.json`
- `partial_retry_cases.json`
- `full_pytest.log`
- `check_*.py` logs
- `RUN_MANIFEST.json`
- `NEXT_STEPS.md`

研究线继续：真实数据 manifest → TaskSpec/labels/splits → factor diagnostics → tabular → TCN。
