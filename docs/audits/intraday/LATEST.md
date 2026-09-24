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

# 最新审计

**最新本地独立审计**：[2026-09-24_09-27-16_JST.md](./2026-09-24_09-27-16_JST.md)  
**上一版本地独立审计**：[2026-09-24_05-57-34_JST.md](./2026-09-24_05-57-34_JST.md)  
**上一版附录**：[2026-09-24_05-57-34_JST_APPENDIX_A.md](./2026-09-24_05-57-34_JST_APPENDIX_A.md)  
**reviewed_source_sha / 本轮固定产品代码**：`7a4f549a15e78fe87db7de00796eff71b707ae9b`  
**audit_start_head / 本轮开始 main**：`1a0bd7fa10e5847ac14d9262698e611636680536`  
**report_commit_sha**：`df49fe75f5279052dfa1918a08f6ab2a1ac2ff3c`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/1a0bd7fa10e5847ac14d9262698e611636680536/docs/audits/intraday/LATEST.md

> 版本纪律：`reviewed_source_sha` 固定为**最后一个触及非 docs 路径**的提交 `7a4f549`
> （`fix(sources,engine): WP01/WP02 身份单一 role + WP03 call_detailed 合同校验`），
> 而非本文件所在的 docs 提交。历史报告未删除。

## 2026-09-24 09:27:16 JST

### ⚠ 更正：上一轮附录 A 的「未推送」定性是错的

`2026-09-24_05-57-34_JST_APPENDIX_A.md` 声称修复提交 `24a982b9` **未推送**——**错误**。
实际：**同一修复内容已以 `7a4f549a15e78fe87db7de00796eff71b707ae9b` 的形式在远端**
（同作者 `arad-bot`、同时间戳、同标题，**5 个代码 blob 逐字节相同**，且直接坐在我的索引
提交 `1c69e5a` 之上），**早于我的附录 A**。
**根因**：我用**开工时的陈旧尖端 `1d438e50`** 做 `merge-base --is-ancestor` 的 base，
而当时远端已前进到 `a6512b4`。**教训：判「是否在远端」必须先取当下尖端，且要比内容而非只比 SHA**
（rebase 会改 SHA，这正是我漏掉孪生提交的原因）。附录结论（修复有效、但**未修** route isolation）不受影响。

### 本轮最高优先

1. `IT-P1-SOURCE-MANAGER-GLOBAL-IDX-BREAKS-ROUTE-ISOLATION-008` — **仍存在，且在已落地的修复之后依然复现**。
   当前尖端实跑：`threshold=2`、primary **仅对 `stocks` 失败**、`index` 从未失败 ⇒
   **index 由 backup 服务、primary 连一次都没被调用**（`primary_index_calls = 0`）。
   代码面：`self.idx` **11 命中**（仍是唯一全局首选源）、`self._fails.clear()` **3 命中**（仍清整张表）、
   `_preferred_by_route` **0 命中**。
   ⚠ 他人在我上一轮后推送的 `7a4f549` **标题含 `fix(sources,engine)`，但修的是另一个缺陷**
   （`call_detailed` 合同校验），**route isolation 完全没被触及** —— 不得读成已解决。
   合同冲突不变：`tests/test_engine.py:259` `assert primary.served == [1, 1]` 把耦合钉成期望值。

2. `IT-P2-CALL-DETAILED-TYPE-ERROR-ABORTS-FAILOVER-004` — **已在远端修复**（`7a4f549`）。
   实测：错类型源 + 健康备用 ⇒ 不抛异常、**failover 成功**。修复前（`1d438e50`）为
   `AttributeError`、备用源调用 **0** 次。**这是他人的修复，非本 agent。**

3. `IT-P2-DOCS-PYTEST-QQ-SUPPRESSES-SUMMARY-003` — **仍未修**。`README.md:265` 写 `1035 项`
   （当时真数 1891，**本轮尖端 1919**，少报 **884**），且文档命令 `python -m pytest -q`
   在 `pyproject.toml:59` `addopts="-q"` 下变成 `-qq`、**静默不打印汇总行**。

### 测试真数（我的测量，仓库外净副本内）

`1d438e50` → **1891 passed，exit 0**；`1a0bd7fa`（含 `7a4f549`）→ **1919 passed in 81.96s，exit 0**（净增 28）。

### 环境异常

机器休眠导致 **h=5、h=6、h=7 三个时段未执行**，不补跑、不伪造。

### 指标边界

本轮 **0 实股结果、0 联网取数、0 训练**；`evidence_type = SOFTWARE_SAMPLE`。
5 项历史回归本轮**未重测**，一律标 **未复现**，不声称已修。

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
