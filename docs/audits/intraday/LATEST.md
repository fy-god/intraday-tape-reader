# 最新审计

**最新本地 Agent 轮**：[`2026-09-21_13-00-00_JST.md`](./2026-09-21_13-00-00_JST.md)  
**最新云端独立审计**：[`2026-09-21_12-02-53_JST.md`](./2026-09-21_12-02-53_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-21_12-02-53_JST_AGENT_TASK.md`](./2026-09-21_12-02-53_JST_AGENT_TASK.md)  
**上一份本地 Agent 轮**：[`2026-09-21_11-31-34_JST.md`](./2026-09-21_11-31-34_JST.md)  
**上一份云端独立审计**：[`2026-09-21_08-04-12_JST.md`](./2026-09-21_08-04-12_JST.md)  
**上一份云端 Agent 任务书**：[`2026-09-21_08-04-12_JST_AGENT_TASK.md`](./2026-09-21_08-04-12_JST_AGENT_TASK.md)  
**更早本地 Agent 轮**：`2026-09-21_03-38-00_JST.md`（**文件名少算 1 小时**，见下方口径更正）  
**下一步计划**：[`NEXT_STEPS.md`](./NEXT_STEPS.md)  
**仓库执行清单**：[`RUN_MANIFEST.json`](./RUN_MANIFEST.json)

> 历史审计文件均保留在本目录；本索引只移动“最新/上一份”指针，不删除历史报告。
>
> **⚠ 与云端 `12-02-53_JST` 轮的关系（如实说明）**：该云端轮写于
> `reviewed_source_sha = 4e73ded`（即**早于**我这一轮 `13-00-00` 的产品提交）。
> 我的 `13-00-00` 轮已独立复现并修复了它指认的两条 P1
> （`IT-P1-UNKNOWN-DATE-FAILOPEN-001`、`IT-P1-ST-REPLAY-BYPASS-001`）
> 与死代码项 `IT-P2-RULE-VERSION-DEADCODE-001`。
> 因此**以 `13-00-00` 轮为最新产品状态**；`12-02-53` 轮的结论请对照下方
> `13-00-00` 摘要阅读（其中已被修复的条目不应再视为开放）。
> 合并时仅指针冲突，云端新增的两份文件已完整保留。

> **命名口径更正（记录在案）**：本仓约定 **JST = 本地 + 1 小时**。
> 上一份本地轮命名为 `03-38-00_JST`，但其提交发生在本地 **03:43**，
> 正确应为 **04:38 JST** —— **那个文件名少算了 1 小时**。
> 本轮起改用正确口径（本地 12:00 → `13-00-00_JST`）。

## 最新本地 Agent 轮结论（2026-09-21 13:00 JST，起点 `70af044`）

### 最重要的一条：`IT-P1-EVAL-PUBLISH-001` **已修复**（这是云端两轮都点名的高优先级项）

云端 `Alert Truth Contract v2` 要的阶段链，前四级**本轮已落地并有真实对账**：

```text
hit_candidate → rule_selected → bus_accepted → committed
                └────────── 本轮实现 ──────────┘
delivery_attempted / sent / skipped / failed   ← 仍开放（IT-P1-NOTIFY-RESULT-001/002）
```

1. **`IT-P1-EVAL-PUBLISH-001` 已修复。** 旧账本只有一个 `published`，且它在
   **规则内部**、`max_per_round` 截断后立刻写，那时告警**还没**过
   `AlertBus.accept()` 与 `store.add_alert()`。
   **机制反例（真实代码端到端，非推演）**：同一票每 5 秒持续放量、
   `cooldown=600s`、24 轮 → `rule_selected=24 / bus_accepted=1 / committed=1`，
   **overcount 24×**。危害：拿旧 `published` 当"已发布事件"分母做 T+5/T+30
   标签，23 条从未交付的候选会被当成真实事件，**标签集从源头错**。
   修复：`capabilities.py` 拆三字段 + `dropped_by_bus`/`committed_ratio`，
   `published` 降为兼容别名；`engine.py` 新增 `_mark_stage()` 在 bus/store
   **之后**记账；`Alert` 新增 `signal_id`（由规则填，不靠 title 猜 ——
   8 个 spirit pattern 共用同一 `AlertKind`）。
   **验收**：`tests/test_alert_delivery_stages.py` 20 条 +
   `test_live_session_observation.py` 聚合 3 条。

2. **`IT-P1-ALERT-IDENTITY-001` 部分修复。** `Alert.signal_id` 已落地并进入
   `to_dict()`，rule→bus→store→client 的 **signal 级**对账已可做。
   **`event_id` 仍未做** —— 该条只算部分完成，不下调为"已修"。

3. **新确认三处既有缺陷（全部由"真实看板端到端对账"暴露，无一是单元测试或离线 selftest 发现的）**：
   * **`-R1`**：`volume_burst` 在 `not provides("turnover")` 时**只记 blocked、没有 `continue`**，
     于是同一票**既被记成"判不了"又真的发出告警** →
     `considered=1 blocked=1 hit=0 sel=1`，机械不变量当场被打破。
     修复：记 blocked 即整只跳过（真实 Sina 送 `turnover=0.0` 占位，
     告警集合逐字不变）。
   * **`-R2`**：`min_turnover` 是**唯一**被拦下时不记 `_no_hit` 的门槛，
     导致"换手率不够"这一整类票**从账本彻底消失**（`considered` 都不涨）。
     修复：补 `_no_hit`，与其余门槛同口径。
   * **`-R3`（用户可见）**：`CAPABILITY_TABLE` 缺 `replay` 条目 →
     `capabilities_for("replay")` 落到"全 False 未知源" →
     `volume_burst` 把 turnover 当硬依赖 → **演练模式下放量告警一条都不出**
     （实测 selftest `volume_burst` 6 条 → **0 条**）。
     而看板 `serve --replay` 正是用户确认"功能到底有没有做"的地方。
     修复后恢复 **5 条**（与基线一致）。同类于 `IT-P1-CAPABILITY-001`：
     **把"来源未知"当成"能力缺失"**。

4. **独立复现并修复云端 11:31 轮针对我上一轮 ST 修复的两条 P1**（均**确认成立**）：
   * **`IT-P1-UNKNOWN-DATE-FAILOPEN-001`**：`_as_date` 只认 `date`/`datetime`，
     字符串/整数一律 `None`，而调用方把 `None` 当"按现行制度" →
     **静默 fail-open**。实测修复前 `st_limit_rate_on("2025-03-10") == 0.10`
     （应为 **0.05**）。可触达性已逐处核对（sina/eastmoney/tencent 的
     `_ts_of`、`web.coerce_ts` 确实产出字符串或 `None`）。
     **附带自我加严**：`strptime("2025031","%Y%m%d")` 会宽松解析成
     `2025-03-01`，故改用正则**显式钉住位数**。
   * **`IT-P1-ST-REPLAY-BYPASS-001`**：`ScriptedStock.limit_rate()` 不传日期 →
     回放任何历史日均按现行 10% 算；`build_script` 把它塞进 `Quote.limit_up`，
     而 `Quote.limit_up_price` 提前返回 → **整条回放链路结构性绕过 ST 修复**。
     实测回放 2025-03-10 主板 ST：`limit_up_price=11.00`（应 **10.50**），
     **真实 5% 封板不被识别**。修复后 10.50，非 ST/创业板/科创板未误伤。

5. **`IT-P2-RULE-VERSION-DEADCODE-001` 已修复（采纳云端建议的"接上，不要两不沾"）。**
   我独立复核确认 `market_rule_version` **零调用、零测试**（全仓仅
   `__all__` 与定义两处），即"制度版本可追溯"上一轮只是**声明**。
   已接进 `Alert.to_dict()["rule_version"]`（alert 是唯一会被落盘/推流的载体，
   正是"事后追溯"场景）；`ts=2025-03-10` → `"cn-2026-07-05"`，
   `ts=2026-09-21` → `"cn-2026-07-06"`。测试用 `inspect.getsource`
   断言存在真实调用点，**防止再退回死代码**。
   ⚠ 诚实限定：看板/soak **尚未读取**该字段，端到端可观测性仍未验证（列入 `R-14`）。

6. **真实盘中验证（2026-09-21 10:43 CST 早盘交易中，`python -m arad.cli once`）**：
   **35 条真实告警**，板块限制正确区分 —— 北交所 `920478 +29.91%`（30% 板）、
   创业板 `300110 +20.07%`、主板 `002589 +10.09%`、
   科创板 `688137 +19.38% 炸板`。
   逐 signal 账本：`volume_burst` **considered=5247 / evaluable=5247 / blocked=0**
   （修复前此处会是 5247 条全 blocked），`source=tencent`，不变量违规**无**。

7. **真实看板端到端对账（本轮最关键的一步，`serve --replay` 同链路）**：
   逐轮进程内捕获 **361/361 轮**，按 `signal_id` 精确分组：
   `Σhit=66 / Σselected=66 / Σbus_accepted=6 / Σcommitted=6`，
   与真实带 `signal_id` 告警 **6 == 6 精确对账**，逐轮交付阶段链**违规 0**；
   被 AlertBus 去重/冷却丢掉 **60 条** —— 这正是旧口径会算成"已发布"的量。
   看板 `/api/status` 每个 signal 都含三级键。

8. **回退验牙（证明测试真的会红）**：批次 A **29 条行为级 RED / 0 结构性**；
   批次 B（ST 日期感知）**13 条行为级 RED / 0 结构性**；两次 `stash pop` 后
   均核对 `git diff --stat HEAD` 非空。
   **诚实限定**：`-R1/-R2/-R3` 与批次 A 同文件，整体回退时一起变红 ——
   能证明"修复前确实错"，但**不能单独归因**到某一处修复；
   单独证据是各自的机制探针与 selftest 的 6→0→5 条曲线。

9. **测试与门禁**：全量 pytest **1642 passed**（上轮 1579 → **+63**）；
   `tools/check_*.py` **8/8**；`node tools/dash_render_check.js` **通过**；
   `python -m arad.cli selftest` **68 条 / 6 类型 / 8-8 剧本命中**（与基线一致）。

10. **上轮结论下调（明确）**：上一轮把 `IT-P1-MARKET-RULE-20260706-001` 标为
    "已修复"。**该结论不完整** —— `Quote` 路径确实修好，但**回放链路结构性绕过**
    （见第 4 条），故下调为 **"部分修复"**。

11. **本轮未做（不含糊）**：未修 eastmoney/sina 股票池截断（`R-12`，两次实测覆盖率
    仅 **73.0% / 83.1%**，源端不稳定，无法区分限流与我方 `max_pages`）；
    **未启用 `spirit_*`**（不擅自改产品默认行为）→ 故 schedule-5 期望的
    "盘中成交类 pattern 出现"**未被验证**，如实标注（`R-13`）；
    未把 `rule_version` 接进看板（`R-14`）；
    未改 `Alert.to_dict()` 对 `ts=None` 崩溃（`R-15`，既有契约，避免扩大变更面）。

## 当前版本与发布

- 本轮起点 HEAD：`70af044d7a9df9e122d8b8490e68fe667db85548`（local == remote）。
- 本轮产品改动：`capabilities.py` / `engine.py` / `models.py` / `replay.py` /
  `rules/volume_burst.py` / `rules/spirit_order.py` / `tools/live_session.py`
  + 3 个新测试文件。
- 归档机器证据：**1642 passed / 0 failed**（本轮本地真实执行，非继承）。
- 当前开放 PR：0。

## 本轮最高优先级结论

1. **`IT-P1-EVAL-PUBLISH-001` 已修**（见第 1 条），阶段链前四级落地并有真实对账。
2. **`IT-P1-NOTIFY-RESULT-001/002` 仍开放**（继续采纳云端结论）：
   `_dispatch_many()` 丢弃 bool；且 bool 本身不够（disabled / severity-skip
   可返回 True 但并未物理发送）。下一步需要 typed
   `sent / skipped_* / failed`，**不能把 True 等价为 sent**。
   → 这是 Alert Truth Contract v2 剩余的第五、六级。
3. **`IT-P1-ALERT-IDENTITY-001` 部分修复**：`signal_id` 已落地；**`event_id` 仍缺**。
4. **`R-12` 股票池覆盖率是本轮最严重的未修风险**：73%~83% 覆盖率下，
   任何"没报警"都可能是"没扫到"。**建议下一轮优先**。
5. `IT-P1-CAPABILITY-002`、`IT-P1-SOURCE-EMPTY-001`、`IT-P1-WINDOW-001`、
   `IT-P1-008/009/003`、`IT-P1-NEWLIST-002-R1`、`IT-P1-006-R1` 继续开放。

## 主改造方向：Alert Truth Contract v2（进度）

```text
hit_candidate      ← 已有 (hit_candidates)
rule_selected      ← ✅ 本轮实现
bus_accepted       ← ✅ 本轮实现
committed(event_id)← ✅ committed 已实现；event_id 未做
delivery_attempted(channel)                                    ← 未做
delivery_sent / delivery_skipped / delivery_failed             ← 未做
client_received / client_applied                               ← 未做
future_label_known                                             ← 未做
```

冻结原则（**前三条本轮已由 `check_invariants()` 机械断言**）：

- `rule_selected <= hit_candidate` ✅
- `bus_accepted <= rule_selected` ✅
- `committed <= bus_accepted` ✅
- 每个 `committed event × channel` 必须恰有 `sent / skipped / failed` 之一 ⬜
- T+5/T+30 标签只能引用 `committed event_id` ⬜（`committed` 已有，`event_id` 未做）

本轮全部是**软件合同与账本修复**，不是市场策略指标。
本轮没有重新训练模型，真实 Precision / Recall / 漏事件率 / 交易收益
仍为 **`unavailable`** —— 不编造任何命中率或收益率。

## 下一轮必须检查的实物

```text
alert_stage_red.log / green.log / rollback.log          ← 本轮已产出（见报告 §4）
st_date_red.log / rollback.log                          ← 本轮已产出（见报告 §4）
alert_delivery_reconcile.json                           ← 已产出（Σcommitted 6 == 6）
delivery_stages_e2e.log                                 ← 已产出（361/361 轮）
live_intraday_ledger.json                               ← 已产出（5247/5247/blocked 0）
notification_result_red.log / green.log / rollback.log  ← 仍未做
notification_delivery_reconcile.json                    ← 仍未做
signal_event_identity.json                              ← signal_id 已有；event_id 未做
post_alert_label_manifest.json                          ← 仍未做
per_code_provenance.json                                ← 仍未做
soft_partial_reconcile.json                             ← 仍未做
universe_coverage_reconcile.json                        ← 新增待做（R-12）
full pytest log
all check_*.py logs
dash_render_check log
RUN_MANIFEST.json
NEXT_STEPS.md
```

更早审计、产品修复及本地执行记录继续保留在本目录和 Git 历史中；本索引只保留当前接续所需的关键状态。
