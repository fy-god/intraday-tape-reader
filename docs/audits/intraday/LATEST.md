
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

**最新云端独立审计**：[2026-09-24_04-04-27_JST.md](./2026-09-24_04-04-27_JST.md)  
**最新云端 Agent 任务书**：[2026-09-24_04-04-27_JST_AGENT_TASK.md](./2026-09-24_04-04-27_JST_AGENT_TASK.md)  
**上一版本地独立审计**：[2026-09-24_02-48-00_JST.md](./2026-09-24_02-48-00_JST.md)  
**reviewed_source_sha / 本轮固定产品代码**：`a0ca7e6caff972d22b903a80b1731e2b7d5a2f95`  
**audit_start_head / 本轮开始 main**：`bd711c4a5a5a2a636717c7f883d2f9c5976aeabc`  
**report_commit_sha**：`c9da70103aefdbcb3c93a0b9f9786470bdb95f9c`  
**agent_task_commit_sha**：`52d5b58f53bca3bc1eaa113cc97aa1a79a5baa67`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/bd711c4a5a5a2a636717c7f883d2f9c5976aeabc/docs/audits/intraday/LATEST.md

> 版本纪律更正：`a0ca7e6..bd711c4` 的 GitHub compare 只有 `docs/audits/intraday/` 变化，故 docs commit 不能写成产品 `reviewed_source_sha`。本文件从本轮起明确分离产品 SHA、审计开始 HEAD 与报告提交 SHA。历史报告文件未删除；上一版完整索引由上面的不可变快照承担。

## 2026-09-24 04:04:27 JST

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
