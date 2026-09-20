# 最新审计

**最新本地 Agent 轮（本轮）**：[`2026-09-21_00-52-00_JST.md`](./2026-09-21_00-52-00_JST.md)  
**最新云端独立审计**：[`2026-09-21_01-40-00_JST.md`](./2026-09-21_01-40-00_JST.md)（r2，792 行，被审 `e729c1f`）  
**最新云端 Agent 任务书**：[`2026-09-21_00-14-53_JST_AGENT_TASK.md`](./2026-09-21_00-14-53_JST_AGENT_TASK.md)  
**上一份本地 Agent 轮**：[`2026-09-20_21-49-29_JST.md`](./2026-09-20_21-49-29_JST.md)  
**可评估性证据**：[`signal_evaluability_cases.json`](./signal_evaluability_cases.json)  
**下一步计划**：[`NEXT_STEPS.md`](./NEXT_STEPS.md)  
**执行清单**：[`RUN_MANIFEST.json`](./RUN_MANIFEST.json)  
本轮起点产品提交：[`ffabc57`](https://github.com/fy-god/intraday-tape-reader/commit/ffabc5723846479fa5ec3fc1a9503ad646b3b6b1)  

---

## 2026-09-21 00:52 JST 本地 Agent 轮（本轮）

- `round_start_sha`：`ffabc5723846479fa5ec3fc1a9503ad646b3b6b1`（`git pull --ff-only` 后 clean worktree）。
- **真实 clean 基线（只读冻结树 `asr-r3` @`ffabc57`，`status --porcelain` 为空）**：
  `python -m pytest -o addopts="" -p no:cacheprovider -q` → **`1430 passed in 82.47s`，exit 0**。
  与云审计 r2 的独立复跑数字**一致**。
- **修复后回归**：全量 → **`1469 passed in 82.14s`，exit 0**（`1469 = 1430 + 39` 新测试）。
  **9/9 门禁全绿**（8 个 `tools/check_*.py` + `node tools/dash_render_check.js`）。
- **产品全链路自检**：`python -m arad.cli selftest` → 962 轮 / 962 tick /
  **69 条告警** / 6 种类型 / **8 个剧本全部命中** / exit 0。

### 本轮主结论

1. **`IT-P1-CAPABILITY-003` 已修复**（分母错 + 语义混淆）。
   修复前：20 轮 × 5000 code × 每轮 1 个 blocked → `capability_unavailable_ratio = 1.0`
   → `capability` **fail** → `healthy=False / exit 1`，文案「整类规则…一次都没被评估过」。
   修复后同一输入：逐 signal 可评估率 **99.98%**（99980/100000）→ **ok / healthy=True / exit 0**。
   反例由**真实产品函数**复跑（`summarize_rounds` → `finalize_metrics` → `evaluate_health`），
   不是手写复刻；证据留档 `signal_evaluability_cases.json`。
   - 新增 `SignalEvalStats`（`capabilities.py:172`）与三态语义
     `BLOCKING / ADVISORY / NOT_REQUIRED`；`evaluable_coverage` 在无分母时返回
     **`None`** 而非 `0.0`。
   - `volume_burst` 分账：缺 `turnover` = **blocking**；缺 `volume_ratio` = **advisory**
     （跳过量比门槛后**仍能命中**，实测产出 1 条 alert，故该票可评估）。
   - `max_per_round` 截断后单独 `mark_published` → **被截断的命中不会变成
     `evaluated_no_hit`**。
   - 健康判据改为**逐 signal**（`live_session.py:962-1020`），只认一种 fail 形状：
     **某 signal 的 `considered` 全部 blocked**；旧轮级比例降级为**诊断量且最高只到 warn**；
     样本不足（`< 200`）不判 fail。
2. **`IT-P2-OBS-STATUS-001` 已修复**：`/api/status` 现暴露账本**自身**的
   `observation_seq` / `observation_observed_at` / `observation_poll_count`，
   三者与账本内容**同临界区**读写；**刻意不复用** `_last_poll_ts`
   （后者每次 poll 都刷新，含无账本的失败轮，会"账本没更新但时刻变新"）。
   验收 12 条，其中**真行为级验牙只有 1 条**（其余为结构性/契约断言，已在报告 §2.2 分类）。
3. `IT-P1-CAPABILITY-004`（`spirit_order` L1 数量兜底 + 逐 pattern 账本）**进行中**，
   属另一工作包；截至本轮报告撰写时尚未产出测试。

### 必须如实说明的三件事

- **职责边界**：本轮只做（a）读 GitHub 审计报告、（b）改产品代码、（c）上传。
  **未**动部署配置、Actions、PR、行情服务、Windows 计划任务。
- **与"盯盘"的距离**：用户真正要的是明天开盘能看到急拉急跌预警。本轮**没有让它更准**；
  它的价值是让"某类信号其实一次都没被评估过"变得**可见**，而不是伪装成"这段时间没行情"。
  真实查准/查全/漏报率/收益**仍为 `unavailable`**，不给数字。
- **过程事故**：本轮有两个子 agent 中途失败（`be56eb24` / `cb2e52ca`）。
  `cb2e52ca` 死前已把**完整且正确**的 `store.py` 改动留在工作树，我核对后**保留并补写测试**
  → 该项代码归属那个失败的子 agent，测试与验证归属我。`be56eb24` 无产出，已重开。
  另有 **3 条我自写测试的缺陷**（非产品缺陷）与 **1 条真产品缺陷**（`mark_evaluated`
  幂等实现错误致不变量破裂），已在报告 §4.3 逐条分列。

---

## 2026-09-21 01:40 JST 云端独立审计 r2（保留要点）
- 被审源码 `e729c1f0bd3d6754e00dba0574a25b01a65b6c58`；报告发布 commit `ffabc57`。
- 我方独立复跑确认其 `1430 passed` 为真。
- 报告登记 **6 项**（5 项 P1 + 1 项新登记 P2），并做对抗性证伪：
  - `IT-P1-CAPABILITY-003`（P1）：正确指出 `live_session.py:419-423` 的
    `if n > 0` 与 `:502-503` 的 `unavailable_rounds / obs_rounds` 分母错误；
    §S1 记录其**自我证伪失败**（原声称成立）。
  - `IT-P1-CAPABILITY-004`（P1）：`spirit_order` 全文件 `provides(...)` 0 命中；
    Sina 声明 `depth_l1=True` 但 `_best_order()` 只遍历空的 `bids/asks`；
    三源矩阵 Tencent 8/8、**Sina 0/8**、Eastmoney 0/8，退化可静默表现为普通 0 alerts。
  - `IT-P1-CAPABILITY-002`（`provides()` 仍 round-global 单参）、
    `IT-P1-SOURCE-EMPTY-001`（`SourceManager.call()` 仍只在 exception 时 failover）、
    `IT-P1-WINDOW-001`（history 仍只在价/量变化时追加）、`IT-P1-008/009/003`（SSE）
    —— **均仍 OPEN**。
  - `IT-P2-OBS-STATUS-001`（P2，本轮新登记）。
- §4 已修项表确认我方上轮的 `IT-P1-COMPLETE-001-R1`、`IT-P1-TIME-ROLE-003-R1`、
  `IT-P1-TIME-ROLE-004`、`IT-P1-INDEX-CURRENT-001`、`IT-P1-LIMIT-001`、
  `IT-P2-LIMIT-FIRST-BOARD-MULTI` 均落地。
- 未复核项收敛为 `IT-P1-008/009/003` 与真实盘中触发率。

---

## 历史索引

**云端独立审计（上一版）**：[`2026-09-21_00-14-53_JST.md`](./2026-09-21_00-14-53_JST.md)  
**云端任务书（上一版）**：[`2026-09-21_00-14-53_JST_AGENT_TASK.md`](./2026-09-21_00-14-53_JST_AGENT_TASK.md)  
**本地 Agent 轮（上上版）**：[`2026-09-20_21-49-29_JST.md`](./2026-09-20_21-49-29_JST.md)  
**他方审计（已推送）**：[`2026-09-20_21-36-00_JST.md`](./2026-09-20_21-36-00_JST.md)  
**云端独立审计（更早）**：[`2026-09-20_20-09-14_JST.md`](./2026-09-20_20-09-14_JST.md)  
上一份产品实现提交：[`e729c1f`](https://github.com/fy-god/intraday-tape-reader/commit/e729c1f0bd3d6754e00dba0574a25b01a65b6c58)  
时间合同：[`source_time_contract.json`](./source_time_contract.json)  

### 2026-09-20 21:49 JST 本地 Agent 轮（保留要点）

- `reviewed_source_sha`：`32acc32462b94411bdaa83b1c13a02290486f4a0`。
- 全量 `1430 passed in 72.51s`，exit 0；同命令在该轮起点树 **`1308 passed`**（+122 用例）。
- 门禁 9/9 全绿。
- 该轮修复：`IT-P2-LIMIT-FIRST-BOARD-MULTI`、`IT-P2-OBS-008/009`、
  `IT-P1-TIME-POLICY-001` 收口及若干文档/合同漂移，并完成 WP01–WP06 主要实现。
- 当时仍 OPEN：`IT-P1-WINDOW-001`、`IT-P1-SOURCE-EMPTY-001`、`IT-P1-CAPABILITY-002`。
- 当时明确下调：provider time 角色未知时不做 provider-ts hard stale reject，只做陈旧诊断。
- 研究结论：真实多日语料仍未挂载；云端 synthetic 研究文件在本地不可回溯，标 `UNVERIFIABLE`。

完整细节以各份 `docs/audits/intraday/*.md` 报告为准。
