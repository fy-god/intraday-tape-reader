# 最新审计

**最新本地 Agent 轮（本轮）**：[`2026-09-21_02-24-00_JST.md`](./2026-09-21_02-24-00_JST.md)  
**上一份本地 Agent 轮**：[`2026-09-21_00-52-00_JST.md`](./2026-09-21_00-52-00_JST.md)  
**最新云端独立审计**：[`2026-09-21_01-40-00_JST.md`](./2026-09-21_01-40-00_JST.md)（r2，792 行，被审 `e729c1f`）  
**最新云端 Agent 任务书**：[`2026-09-21_00-14-53_JST_AGENT_TASK.md`](./2026-09-21_00-14-53_JST_AGENT_TASK.md)  
**可评估性证据**：[`signal_evaluability_cases.json`](./signal_evaluability_cases.json)  
**下一步计划**：[`NEXT_STEPS.md`](./NEXT_STEPS.md)  
**执行清单**：[`RUN_MANIFEST.json`](./RUN_MANIFEST.json)  
本轮起点产品提交：[`c4254dc`](https://github.com/fy-god/intraday-tape-reader/commit/c4254dc)  
本轮产出提交：[`1713f4f`](https://github.com/fy-god/intraday-tape-reader/commit/1713f4f)、[`3decac9`](https://github.com/fy-god/intraday-tape-reader/commit/3decac9)  

---

## 2026-09-21 02:24 JST 本地 Agent 轮（本轮）

> **本轮方法论的转变**：不再读代码找 bug、不再只跑回放 ——
> **用真实网络行情跑全市场（腾讯，5564 只）**，让缺陷自己暴露。
> 结果：一次就抓到 **3 个**此前所有单测/回放都没发现的真实缺陷。

### 本轮主结论

1. **`IT-P1-NEWLIST-001` 新股无涨跌幅限制却被套 ±10%（已修）**。
   `601091 C沈鼓` 当日 **+177.74%**、现价 57.77，却算出 `limit_up=22.88`
   → `limit_board` 误报「**打开跌停 +208.60%**」，同轮还报「低开高走 +285.1%」，
   **自相矛盾**。修复：无约束时限价返回 **`0.0`**（哨兵值＝"无此约束"），
   并新增 `has_price_limit` / `is_at_limit_up` / `is_at_limit_down`
   （**直接用 `q.price >= q.limit_up_price` 在无约束股上恒真**）。
2. **`IT-P1-NEWLIST-002` 新股过滤第二层缺失（已修）**。
   `filters.min_list_days = 11` **完全不生效** —— `list_dates` 只有东财提供，
   而东财在本机被 IP 封禁，实际走腾讯（`list_date` 为空串），
   `_too_new` 按"数据缺失放行"直接返回 False。第二层防线不存在。
   修复：无精确上市日时退回**名称前缀**（`N`＝首日、`C`＝第 2~5 日，
   判据＝"首字母 N/C 且第二字符为**中文**"）。
   实测 5564 只中符合者**恰好 2 只、零假阳性**。
3. **`IT-P1-REPLAY-DETERMINISM-001` 回放确定性承诺是假的（已修）**。
   `replay.py:374` 用 `hash(s.code)` 播种，而 CPython 对 `str` 的 `hash()`
   按 `PYTHONHASHSEED` **每进程随机化**。三进程实测指纹互不相同；
   固定 `PYTHONHASHSEED=0` 后一致 → **根因确证**。
   后果：`selftest` 告警数在 **68/69/71 漂移，不能当回归判据**；
   历次报告里"selftest N 条告警"**全都不是稳定不变量**。
   修复：改用 SHA-256 派生稳定种子。修复后 6 次连跑**全部 68 条**。

### 回归与验牙（我自己复跑）

- 全量 **`1545 passed`**（改动前 1495）；**8/8 gate 全绿**；
  `selftest` **68 条**（6 次连跑一致）。
- 回退验牙（逐条单独运行分类）：`models.py` → **9 条 AssertionError 真验牙**
  （+3 AttributeError / 2 ImportError 结构性）；`filters.py` → **7 条，零结构性**；
  `replay.py` → **3 条，零结构性**。

### 真实行情端到端证据（本轮核心）

- 链路打通：默认 4 规则 / 打开 spirit 后 7 规则；`run_daemon.py` 常驻看板，
  `/api/status`、`/api/spirit`、`/api/alerts`、`/api/health` 均 200；
  SSE `/api/stream` 持续推出 `tick` / `phase` 事件。
- 真实全市场 4 轮：55 / 20 / 20 / 20 条告警；`institution_buy` 11 条、
  `institution_sell` 9 条 —— 证明 `IT-P1-CAPABILITY-004` 修复在真实数据上生效。
- 4 个**成交类** pattern 休市时缺席**是正确行为**（`d_volume == 0` → 不可判），
  已用真实 Quote + 人为推进内外盘证明 8/8 pattern 均可产出。

### 必须如实说明的三件事

- **修好 bug ≠ 用户明天能收到有用告警**。本轮修的是"会不会报错"，
  **不是"报得准不准"**。真实 Precision / Recall / 漏报率 / 交易收益
  **依然是 `unavailable`**，不给任何编造数字。
- **`spirit_*` 三条规则默认仍是 `enabled: false`**（`settings.yaml:188/223/257`）。
  配置注释要求"先在真实行情里观察命中质量再打开"——**我还没有足够证据建议打开**。
  连续竞价时段的实测（09:30 后）是下一步（已挂 `schedule-5` 于 09:32 CST）。
- **已知残留缺口**：次新股上市第 6 日起名称前缀脱落，只能靠依赖东财的
  `min_list_days` 第一层，而腾讯链路上该层仍缺失 → 第 6 日起的次新股**无过滤**。
  另有北交所是否用 N/C 前缀未核实、N/C 判据样本极小（仅一次快照 2 只）。

### 职责边界

本轮只做（a）读 GitHub 审计报告、（b）改产品代码、（c）上传。
**未**动部署配置、Actions、PR、行情服务、Windows 计划任务。
临时探针一律放 `$env:TEMP`，**未**污染 `tools/`（否则触发 `check_readme_tools.py`）。

---

## 2026-09-21 00:52 JST 本地 Agent 轮（保留要点）

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
   → **已于 02:24 JST 轮完成并发布 `1713f4f`**：`_best_order` 增加 `l1_lots` 参数，
   `not vols` 时用**真实的** `q.bid_vol`/`q.ask_vol`（手）当唯一一档
   （此前传的是 `_num(q.bid1, 0.0)` —— 那是**价格**）；逐 pattern 账本
   `spirit_order.<pattern>`；新增 `check_capability_declaration()`。
   我在**真实行情**上独立复跑验证：Sina 链路告警 **0 → 2**，
   账本 `institution_buy/sell ev=1 cov=1.0`；回退验牙 **15 条 AssertionError
   真验牙 + 3 条 ImportError 结构性**（新符号 import 已移入测试函数内部，
   否则旧版整体收集失败、红得没有行为意义）。

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
