# 最新审计

**最新云端独立审计**：[2026-09-24_08-10-43_JST.md](./2026-09-24_08-10-43_JST.md)  
**最新云端 Agent 任务书**：[2026-09-24_08-10-43_JST_AGENT_TASK.md](./2026-09-24_08-10-43_JST_AGENT_TASK.md)  
**上一版本地独立审计**：[2026-09-24_05-57-34_JST.md](./2026-09-24_05-57-34_JST.md)  
**reviewed_source_sha / 本轮固定产品代码**：`7a4f549a15e78fe87db7de00796eff71b707ae9b`  
**audit_start_head / 本轮开始 main**：`fd403c377e835bb7816045177f2e239910222cba`  
**report_commit_sha**：`497c44011fbc3a00e11b412060cd86837b5fd850`  
**agent_task_commit_sha**：`6f02102523061c4fbcf066f034c5c90358a76e89`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/fd403c377e835bb7816045177f2e239910222cba/docs/audits/intraday/LATEST.md

> 版本纪律：`7a4f549..fd403c3` 的 compare 只有 `docs/audits/intraday/` 与 evidence 文档，故当前真正产品代码固定为 `7a4f549`。历史报告文件没有删除；上一版完整长索引固定保留在上面的不可变 commit。

## 2026-09-24 08:10:43 JST

主实验：`EXP-IT-PER-ROUTE-OBSERVATION-015`

### 本轮最高优先

1. `IT-P1-INDEX-ROUTE-HEALTH-MASKED-BY-STOCK-AGGREGATE-001` — **新 P1 latent**：当前 `RoundObservationSet.coverage` 把股票与指数加总，`live_session` 也只消费 aggregate coverage；5563 只股票全返回、5 个指数 0 返回时，aggregate 仍为 **99.9102%**，而 index route 实际为 **0%**。`spirit_index` 默认关闭，所以这是功能启用后的漏报/不可评估盲区；current_indices 已保护 stale-alert，不把本条夸成陈旧误报。
2. `IT-P2-ROUND-ROUTE-PROVENANCE-COLLAPSE-001` — **新 P2**：股票和指数同轮可以由不同 provider 服务，但 `RoundObservationSet` 只有一个 `source/capabilities`，且该值来自 stock route；研究 / soak 不能恢复真实 index provider。
3. `IT-P1-SNAPSHOT-RAW-LEDGER-COLLAPSE-001` — **PARTIAL**：Source exact outcome 与 `call_detailed` 已修并有产品测试，但 `poll_once` 仍走 bare `call("snapshots")`；线上 RoundObservation 尚未消费 exact raw truth。
4. `IT-P1-SOURCE-MANAGER-GLOBAL-IDX-BREAKS-ROUTE-ISOLATION-008` — **本轮更正为当前产品政策，不再按 P1 bug 重报**。现有 `tests/test_engine.py` 明确要求 global promotion 跨 route 生效；真正剩余问题降为 `IT-P2-SOURCE-PROMOTION-POLICY-DOC-CONFLICT-009`：EngineState 注释的“各 route 各有切源时机”与测试固定的 global promotion 口径需要统一。

### 本轮主改造

**Per-Route Round Observation Contract v1**：

```text
Exact Source Outcome
        ↓
RouteObservation[stocks] / RouteObservation[index]
        ↓
Per-route raw/usable coverage + source/capabilities + terminal buckets
        ↓
Compatibility aggregate RoundObservation
```

WP07 不能只把 `call()` 换成 `call_detailed()`；必须保留 route-level truth，避免 exact source truth 在最后一层再次被 stocks/index 总和压扁。

### 下一轮关键产物

- `route_observation_cases.json`
- `mixed_source_cases.json`
- `index_health_cases.json`
- `pagination_cases.json`
- `full_pytest.log`
- `check_*.py` logs
- `RUN_MANIFEST.json`
- `NEXT_STEPS.md`

模型训练：0；真实 Precision/Recall/漏事件率/交易收益仍 `unavailable`。
