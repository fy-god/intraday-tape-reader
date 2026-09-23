# 最新审计

**最新本地独立审计**：[2026-09-24_02-48-00_JST.md](./2026-09-24_02-48-00_JST.md)  
**上一版云端独立审计**：[2026-09-24_00-05-20_JST.md](./2026-09-24_00-05-20_JST.md)  
**上一版云端 Agent 任务书**：[2026-09-24_00-05-20_JST_AGENT_TASK.md](./2026-09-24_00-05-20_JST_AGENT_TASK.md)  
**reviewed_source_sha / 最新本地产品代码提交**：`9c1b596331a3f531b2365d12c35491827d59eccc`  
**上一轮审计的产品代码 basline**：`a0ca7e6caff972d22b903a80b1731e2b7d5a2f95`  
**report_commit_sha**：`cc29d8c995b1f4a76b57cb2ba5b7459aabb77259`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/9c1b596331a3f531b2365d12c35491827d59eccc/docs/audits/intraday/LATEST.md

> `a0ca7e6..9c1b596` 非 docs 路径 **零变化**（`git diff --stat` 为空，三种 base 各测一次）。
> 历史报告文件**未删除**；本文件按本仓库既定语义只移动**当前接续指针**，
> 历史索引由上面的不可变快照 URL 承担（本轮实测：近 26 个版本 **26/26** 都带该指针）。

## 2026-09-24 02:48 JST（本地 h=0）

`reviewed_source_sha = 9c1b596331a3f531b2365d12c35491827d59eccc`　产品代码零变化。

### 本轮把「机制」升级为「可执行证明」

1. `IT-P2-CALL-DETAILED-TYPE-ERROR-ABORTS-FAILOVER-004` — **已确认错误（本轮可控实验证明）**：
   `engine.py:650` 的 `out = out._replace(source=name)` 位于逐源 `try`（`:628-645`）**之外**，
   源**返回错误类型**（非抛异常）时 `AttributeError` 穿透 failover。
   阳性臂：`AttributeError: 'dict' object has no attribute '_replace'`、`backup.calls=0`；
   阴性对照（源抛异常）：正常 failover、`backup.calls=1`。**对照组成立**。
2. `IT-P2-TENCENT-NAME-BLIND-REQUEST-KEY-REGRESSION-002` — **已确认 7/7，但当前暴露 = 0**：
   实跑真实 `models.looks_like_index`，判别式
   `looks_like_index(sym, 真名, code) != looks_like_index(sym, "", code)`
   在 `sh000922/sh000015/sh000903/sh000904/sh000906/sh000009/sh000133` 上**全为真**；
   6 个对照（浦发/平安/上证指数/深证成指/沪深300/上证180）**全不 diverge**。
   ⚠ 但出厂 `config/settings.yaml:27` 的 5 个 `index_codes` 实测 **0/5** 落入该类
   ⇒ **潜伏缺陷，不是当前线上错误**。
3. `IT-P2-CALL-DETAILED-ROUTE-MISBIND-006` — **降档为「待验证风险」**：
   `out.route` / `result.route` / `_replace(route` 全仓库 **0 命中**；三个内置源都传自己的
   `route=route`，树内**不存在**可触发的第三方源，且 `call_detailed` 生产调用点 = 0
   ⇒ **可达性 0**。建议与第 1 条**同一处**修复。
4. `IT-P2-WP01-AXIS-SUITE-BLINDNESS-005` — **未复现**：本轮**没有**安全构造突变实验，
   `1891 passed` 只能说明现状绿、**不能**证明测试有判别力。按手册降级，**不沿用**云端结论。
5. `IT-P1-SNAPSHOT-RAW-LEDGER-COLLAPSE-001` — **仍未修（PARTIAL 属实）**：
   `call_detailed` 全仓库 84 次出现，**src 侧仅 1 次且是其定义**（`engine.py:591`）
   ⇒ **生产调用点 = 0**，在线 RoundObservation 仍 Quote-only。

### 口径修正（重要）

云端最新报告（`2026-09-24_00-05-20_JST.md`）正文的固定 15 符号矩阵
`13/15 · 8/15 · 15/15` 与其自称的 8 个「主要沙箱产物」文件名，
在 tip 的 301 个跟踪文件中 **0 个存在**；已提交的
`evidence_2026-09-23_21-00-00_JST/tencent_detailed_key_axis_matrix.csv` 只有 **8 行数据**。
`candidate_diff_hash = 7815f739…` 也**不是**仓库 git object（`cat-file` 返回 NO）。
⇒ 这些分数**只能在聊天沙箱复现，不是仓库事实**；本轮**不**计入独立证据
（不指称云端造假 —— 报告自己已标 `NOT APPLIED / NOT IMPORT-TESTED`）。

### 测试真数（三法交叉）

仓库 `pyproject.toml` 的 `addopts = "-q"` **抑制末尾汇总行**，单看 `-q` 看不到 passed 数。
三种独立方法：`--collect-only -q` 逐文件求和 **1891**；进度字符计数 **1891**；
`-v` 汇总行 `===== 1891 passed in 200.38s (0:03:20) =====`。
⇒ **1891 passed，exit 0**，跑在副本 `it_h0_copy`；原仓库 493 个文件的大小+mtime_ns
前后 `added=0 removed=0 changed=0`。

### 仍未修的完整清单

`IT-P1-SOURCE-EMPTY-001`、`IT-P1-WINDOW-001`、`IT-P1-UNIVERSE-MEMBERSHIP-QUALITY-001`、
`IT-P0-002-TZ-R1`、`IT-P2-LEGACY-FALLBACK-PHANTOM-MISSING-003`、
`IT-P2-COVERAGE-ALIAS-REINTRODUCES-COLLISION-ACROSS-WP03-BOUNDARY-002`
—— 本轮**未独立复核**（两个子 agent 双双失败且磁盘**零产出**，目录根本未创建），
故**不**写成已确认。轮次容量用于把第 1/2 条做成可执行证明。

模型训练：0；真实 Precision/Recall/漏事件率/交易收益仍 `unavailable`。
