
# 最新审计

**最新本地独立审计**：[2026-09-24_05-57-34_JST.md](./2026-09-24_05-57-34_JST.md)  
**上一版本地独立审计**：[2026-09-24_02-48-00_JST.md](./2026-09-24_02-48-00_JST.md)  
**上一版云端独立审计**：[2026-09-24_04-04-27_JST.md](./2026-09-24_04-04-27_JST.md)  
**上一版云端 Agent 任务书**：[2026-09-24_04-04-27_JST_AGENT_TASK.md](./2026-09-24_04-04-27_JST_AGENT_TASK.md)  
**reviewed_source_sha / 本轮固定产品代码**：`a0ca7e6caff972d22b903a80b1731e2b7d5a2f95`  
**audit_start_head / 本轮开始 main**：`1d438e503077541781f23da4ac68c18b3e0b1b8a`  
**report_commit_sha**：`26265ddf855d6316b5016b4c6a1cde5ea8792004`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/1d438e503077541781f23da4ac68c18b3e0b1b8a/docs/audits/intraday/LATEST.md

> 版本纪律：延续上一轮更正后的口径，明确分离**固定产品 SHA**、**审计开始 HEAD** 与**报告提交 SHA**。
> `a0ca7e6..1d438e5` 的 GitHub compare 非 docs 路径 **零变化**（本轮实测 `git diff --stat` 为空），
> 故 `reviewed_source_sha` 固定为 `a0ca7e6`。历史报告文件未删除。

## 2026-09-24 05:57:34 JST

主实验：`EXP-IT-ROUTE-ISOLATION-014`

### 本轮最高优先

1. `IT-P1-SOURCE-MANAGER-GLOBAL-IDX-BREAKS-ROUTE-ISOLATION-008` — **机制已用真类实跑确认，可达性非零**：
   `engine.py:514` 全局 `self.idx` 是唯一首选源槽位；`:540`/`:580`/`:660` 三处 `_fails.clear()`
   **清整张表**；只有 `:573`/`:653` 是路由作用域。实测 stocks 达阈值后 **index 由 backup 服务且
   primary 从未被调用**；镜像臂同样成立；4 个对照臂全过。生产 `.call` 调用点 **4 处**且共用同一实例
   （`:807` + `_wrap_sources`）。
   ⚠ **`tests/test_engine.py:259` `assert primary.served == [1, 1]` 把该耦合钉成了期望值**
   （注释 `:256-258` 还称其「恰好说明切换真的生效了」）⇒ 这不是漏测，是**产品当前投票支持全局切换**；
   要修必须**一并改写** `test_engine.py:251/255/259`、`:293`、`test_source_manager_detailed.py:235`
   （子 agent 变异实测 `4 failed, 75 passed`）。`_preferred_by_route` 在 `src/` **0 命中**（全在 docs 建议里）。

2. `IT-P2-DOCS-PYTEST-QQ-SUPPRESSES-SUMMARY-003` — **新，已确认**：`README.md:265` 与
   `docs/ARCHITECTURE.md:866` 给出的验证命令是 `python -m pytest -q`，而 `pyproject.toml:59` 已设
   `addopts = "-q"` ⇒ 实跑成为 `-qq`，**pytest 不打印任何汇总行**（exit 仍 0）。
   受控对：单 `-q` → `1891 passed`；`-qq` → 无汇总行。且同一行的测试数**已过期**
   （README 写 `1035 项`、ARCHITECTURE 写 `1064 项`，**实测 1891**）。
   全仓库计数互不一致（1026…1891）；该漂移 **4 天前已被记录但一直未修**。

3. `IT-P2-AUDIT-REVIEWED-SHA-DOC-COMMIT-003` — **已修复**，且**被更正对象正是本线 h=0 的索引**：
- **附录 A（本轮补发）**：[`2026-09-24_05-57-34_JST_APPENDIX_A.md`](./2026-09-24_05-57-34_JST_APPENDIX_A.md) —— 实测**并发写入者提交的未推送修复** `24a982b9`（父提交 = 本轮审计尖端）：它**确实修好**了 `call_detailed` 错类型穿透 failover（实测 failover 成功、全套 **1919 passed exit 0**，本轮尖端为 1891），但**没有**修 `IT-P1-...-ROUTE-ISOLATION-008`（`_preferred_by_route` 仍 **0 命中**、`self.idx` 仍 11 命中）。该提交**未推送**，我只读核验、未触碰其工作树。
   `d282d53` 把 `9c1b596`（文件列表仅 `['docs/audits/intraday/LATEST.md']`）写成
   `reviewed_source_sha / 最新本地产品代码提交`。**全历史扫 70/116 修订，仅此 1 次** ⇒ 本线引入、未扩散。
   本轮索引已按拆分口径固定 `a0ca7e6`。
   **验收**：断言 `reviewed_source_sha` 指向的提交至少有一个非 `docs/` 文件；阳性对照喂纯 docs 提交须失败。

### 回归（本轮逐项重测真实文件）

- `IT-P1-SOURCE-EMPTY-001` **仍存在**；`IT-P1-WINDOW-001` **仍存在**（水位线子项已修）；
  `IT-P1-SNAPSHOT-RAW-LEDGER-COLLAPSE-001` **复现确认**（`call_detailed` 生产调用点 = 0）；
  `IT-P2-LEGACY-FALLBACK-PHANTOM-MISSING-003` **仍未修**（机制行 `:636`/`:639`，暴露 0/3）；
  `IT-P0-002-TZ-R1` **仍存在**（`session.py:100` 死字段）；
  `IT-P2-OBS-001` **主案例已修**（仅静态核查，未跑 soak）。
  行号纠正：旧记 `engine.py:703`/`:1418` → 实际 `:777`/`:783`/`:1498`。

### 测试真数（我的测量，仓库外净副本内）

`python -m pytest . -p no:cacheprovider` → **1891 passed，exit 0**（91.4 / 102.5 / 112.4 s 三次一致）；
重跑后树 **added=0 removed=0 modified=0** ⇒ 该套件 **hermetic**。尖端**无 `.github/`** ⇒ 无 CI。

### 指标边界

本轮 **0 实股结果、0 联网取数、0 训练**；`evidence_type = SOFTWARE_SAMPLE`。
未跑 mutation testing ⇒ **不声称测试有判别力**。

# 最新审计

**最新本地独立审计**：[2026-09-24_05-52-56_JST.md](./2026-09-24_05-52-56_JST.md)  
**最新云端独立审计**：[2026-09-24_04-04-27_JST.md](./2026-09-24_04-04-27_JST.md)  
**最新云端 Agent 任务书**：[2026-09-24_04-04-27_JST_AGENT_TASK.md](./2026-09-24_04-04-27_JST_AGENT_TASK.md)  
**上一版本地独立审计**：[2026-09-24_02-48-00_JST.md](./2026-09-24_02-48-00_JST.md)  
**上一版云端独立审计**：[2026-09-24_00-05-20_JST.md](./2026-09-24_00-05-20_JST.md)  
**上一版云端 Agent 任务书**：[2026-09-24_00-05-20_JST_AGENT_TASK.md](./2026-09-24_00-05-20_JST_AGENT_TASK.md)  
**reviewed_source_sha**：`bd711c4a5a5a2a636717c7f883d2f9c5976aeabc`（本轮开始 HEAD）  
**audit_start_head / 本轮开始 main**：`bd711c4a5a5a2a636717c7f883d2f9c5976aeabc`  
**上一版云端固定产品代码**：`a0ca7e6caff972d22b903a80b1731e2b7d5a2f95`  
**上一版云端 report_commit_sha**：`c9da70103aefdbcb3c93a0b9f9786470bdb95f9c`  
**上一版云端 agent_task_commit_sha**：`52d5b58f53bca3bc1eaa113cc97aa1a79a5baa67`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/bd711c4a5a5a2a636717c7f883d2f9c5976aeabc/docs/audits/intraday/LATEST.md

> 版本纪律（沿用云端 04:04 更正）：docs commit **不能**写成产品 `reviewed_source_sha`。
> 本文件明确分离产品 SHA、审计开始 HEAD 与报告提交 SHA。
> 历史报告文件未删除；上一版完整索引由上面的不可变快照承担。

## 2026-09-24 05:52:56 JST（本地 · WP01/WP02/WP03/WP05 修复 + 回退验牙）

对应任务书 `2026-09-24_00-05-20_JST_AGENT_TASK.md`。
**性质：修复**（上一份 02:48 本地报告与 04:04 云端报告都是**诊断**）。

1. `IT-P2-TENCENT-NAME-BLIND-REQUEST-KEY-REGRESSION-002` — **已修复**
   （**是我自己 `4648b1c` 引入的回归**）。新增模块级 `is_index_role(symbol, *,
   index_role, idx_set)` 作**唯一**判据，`request`/`raw`/`Quote` 三处同调；
   角色由**调用方 route** 拥有；`name` **不参与**身份判定；
   `parse_response` 增加 `route` 参数。
   修前 `11 failed`（7 个名称唯一指数全部 phantom missing）→ 修后 `21 passed`。
   纯函数层 **15/15 三轴一致**；**类级生产路径 15/15**；默认 5 指数 `coverage=1.0`。
2. `IT-P2-CALL-DETAILED-TYPE-ERROR-ABORTS-FAILOVER-004` — **已修复**
   （呼应 02:48 / 04:04 的诊断）。四件事（取结果 / `isinstance` / source 绑定 /
   caller route 绑定）**全部移进 per-source `try`**；错类型按 source failure
   failover（修前 `backup.calls == 0`）。修前 `3 failed` → 修后 `7 passed`。
3. `IT-P2-CALL-DETAILED-ROUTE-MISBIND-006` — **已修复**：固定策略 = **规范化**
   （`_replace(source=name, route=route)`，一次绑定）。
4. WP05 coverage alias gate：收窄到 `engine.py`（第一版扫全 `src/` 误报
   `capabilities.py:892` —— 那是**另一个类自己的** `coverage()`），并加自检。
5. **【下调】任务书 §WP01 的 M6 定义**：修好后"强制 `name=""`"是**恒等变换
   （no-op）**，`is_index_role` 代码路径里 `name` 出现 **0** 次。
   未粉饰，换成 5 个能真正攻击新形状的 mutation（全部行为性变红、对照绿）。
6. ⚠ **最重要诚实交代**：`call_detailed` 生产调用点仍 **= 0**（`poll_once` →
   `_fetch_indices` → `sources.call("snapshots")`，`:948` 走 legacy 链）。
   本轮是"把新链修成**可接线**的正确形状并装牙"，**不是**修好线上问题。
   **下一轮首选 = WP07**（把 `call_detailed` 接进 `poll_once`，两条 route 都换）。

测试真数：**1919 passed / 0 failed**；`check_*.py` **8/8**；BOM exit 0；
`dash_render_check.js` exit 0；`arad.cli selftest` 68 告警 / 6 类型 / exit 0。
回退验牙 **7 条全行为性、0 结构性**（含一次判据升级：运行期在**目标测试路径上**
抛的 `AttributeError` 属行为性证据，不算结构性）。

## 2026-09-24 04:04:27 JST（云端）

主实验：`EXP-IT-ROUTE-ISOLATION-014`

### 本轮最高优先

1. `IT-P2-CALL-DETAILED-TYPE-ERROR-ABORTS-FAILOVER-004` — **P1 已确认**：wrong-type detailed return 在 per-source `try` 外触发 `_replace` 异常，健康 backup 不会被尝试；当前生产 `call_detailed` 调用点仍为 0，所以属于 WP03 接线前硬门。
2. `IT-P2-TENCENT-NAME-BLIND-REQUEST-KEY-REGRESSION-002` — **P1 latent**：7 个名称敏感指数 request/raw identity 分叉；出厂 5 个 index_codes 当前 0/5 暴露。
3. `IT-P1-SNAPSHOT-RAW-LEDGER-COLLAPSE-001` — **PARTIAL**：Source v4 + `call_detailed` 已存在，但 `poll_once` 仍未消费 exact detailed outcome。
4. `IT-P1-SOURCE-MANAGER-GLOBAL-IDX-BREAKS-ROUTE-ISOLATION-008` — **新 P1 candidate**：失败计数/serving 按 route 分账，但正式 preferred source 仍是一份全局 `idx`；一个 route 达阈值会改变另一个零失败 route 的下一首选源并清空全部 route fail counters。需先用仓库 RED 冻结“route-specific promotion vs global promotion”产品策略。
5. `IT-P2-CALL-DETAILED-ROUTE-MISBIND-006` — P2：Manager 绑定 `source`，但未验证 outcome `route`；当前 built-in/生产调用点暴露为 0，建议与 wrong-type 同处收口。
6. `IT-P2-EASTMONEY-UNKNOWN-TOTAL-USABLE-EMPTY-STOPS-PAGINATION-001` — P2，已有独立 scratch RED/GREEN，ready-to-land。
7. `IT-P1-UNIVERSE-MEMBERSHIP-QUALITY-001` / `IT-P1-SOURCE-EMPTY-001` / `IT-P0-002-TZ-R1` / `IT-P1-WINDOW-001` — 历史开放，本轮未独立重跑。

### 主改造

**Pre-WP03 Failover + Identity Gate v2**：

```text
identity truth
+ detailed result type/source/route validation
+ explicit route-promotion policy
        ↓
Engine unified Round Attribution
        ↓
Membership / exact soft-empty
        ↓
research
```

### 本轮机制结果

固定 `threshold=2`：primary 仅 stocks 失败、index 健康；backup 两路健康。stocks 连续两次由 backup 服务后，全局 `idx` 切到 backup；下一次 index 请求即使自己从未失败，也直接由 backup 服务，原 healthy primary index 不再尝试。镜像 index→stocks 同样成立。该结果是 fixed-SHA control-flow 的 deterministic mechanism，不是线上事故率；下一本地轮应先写 route-policy RED 再决定是否改为 `_preferred_by_route`。

### 下一轮关键产物

- `call_detailed_type_red/green/rollback.log`
- `identity_matrix_red/green.log` + mutation tooth
- `route_isolation_red/green/rollback.log`
- `legacy_axis_red/green.log`
- `pagination_red/green/rollback.log`
- `source_manager_cases.json`
- `identity_cases.json`
- `route_isolation_cases.json`
- `full_pytest.log`
- `RUN_MANIFEST.json`
- `NEXT_STEPS.md`

模型训练：0；真实 Precision/Recall/漏事件率/交易收益仍 `unavailable`。
`spirit_*` 仍 `enabled: false`。
