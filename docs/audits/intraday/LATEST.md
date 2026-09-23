# 最新审计

**最新云端独立审计**：[`2026-09-23_09-05-00_JST.md`](./2026-09-23_09-05-00_JST.md)  
**上一轮云端独立审计**：[`2026-09-23_08-05-58_JST.md`](./2026-09-23_08-05-58_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-23_08-05-58_JST_AGENT_TASK.md`](./2026-09-23_08-05-58_JST_AGENT_TASK.md)  
**最新独立实测审计**：[`2026-09-23_05-35-00_JST.md`](./2026-09-23_05-35-00_JST.md)  
**最新本地 Agent 产品轮**：[`2026-09-22_21-00-00_JST.md`](./2026-09-22_21-00-00_JST.md)  
**最新产品提交 / reviewed_source_sha**：`bf0b83bf212f169c720ca8a1c5108405c18a9712`  
**本轮审计开始 docs HEAD**：`ec9aa358fa420c4d49d2d5d0ad5b5c0bee451b97`  
**report_commit_sha**：`022d322efe120ffff7c69ea28330bbc77b397e22`  
**agent_task_commit_sha**：`57839464acd5707277984047913b0a620630451b`  
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/ec9aa358fa420c4d49d2d5d0ad5b5c0bee451b97/docs/audits/intraday/LATEST.md

> `bf0b83b..55ee49a222e1e598e9845d98e4e5f472d894e174` 只有 docs 变化；历史报告文件均保留。本文件只移动当前接续指针。

## 2026-09-23 09:05:00 JST（盘中预警 · h=8）
- **补遗 `413fc785b54bfec6fc7a3137cae05757d758c918`**：§5.2 的索引统计初版沿用上一轮未重数，已在更正版按实测重算（tip 带日期报告 **96→68**、被链命名 **44→29**、从未被命名 **53→39**、单节点最强 **18→15**）。方案方向未变，量级全部下修。  

[2026-09-23_09-05-00_JST.md](./2026-09-23_09-05-00_JST.md)

- `reviewed_source_sha` = `bf0b83bf212f169c720ca8a1c5108405c18a9712`；审计开始远端 tip `ec9aa358fa420c4d49d2d5d0ad5b5c0bee451b97`。自 `bf0b83b` 到 tip 的 21 个提交中 **20 个 docs-only、0 个产品提交**；自本 Agent 上次审计（`55ee49a`）起只有 **3 个 docs-only 提交**。

### 【P0 已确认】对端推荐的 membership 修法会砸坏两个现役指标

`08-05-58` 报告 §7.1 把「active membership = raw transport membership」定为最终合同。方向成立，但它只在**没有 checkout** 的沙箱里检查了 membership 集合本身（其 §0 自述 `full pytest: not_run`），未检查该改动会同时移动哪些别的字段。

机制：`engine.py:1666` `requested_codes = list(self._codes) + index`；`engine.py:1676` `unknown_missing` = 请求了但没返回的代码；`tests/test_observation_ledger_buckets.py:170` 断言`admitted+missing+quality+future+ooo == stock_requested`（每个被请求的代码必须落桶）。停牌成员在 `snapshots()` 里不返回任何行（`eastmoney.py:199-200` 提前 `return None`），因此把 370 个停牌成员放进 `_codes` 后它们**全部落进 `unknown_missing`**。

真实 `Engine.poll_once()` 三臂对照（`DISCRIMINATING=True`）：

```text
arm                     codes  req  ret  adm  missing  active  cov_active
ARM1 基线（可用行情投影）    8    8    8    8        0       8    0.6667
ARM2 对端修法（+4 停牌）   12   12    8    8        4      12    1.0000
ARM3 阴性对照（+4 会返回） 12   12   12   12        0      12    1.0000
ARM2-ARM1 == 新增停牌数 -> True ; ARM3-ARM1 == 0 -> True ; ARM2!=ARM3 -> True
```

真实东财尺度（`expected_total=5923` / usable `5553` / 停牌 `370`）：
`coverage_active` 由 **0.937532 变成 1.000000**，与 `coverage_transport` **按构造恒等**，二者之间那 6.2% 的缺口（正是「市场里多少成员拿不到可用行情」的度量）**永远归零**。而 `engine.py:908-920` 明确写着「`C_round` 永远不能替代 `C_active`」——该改动把一个 coverage 变成另一个 coverage 的复述，任何盯它的健康判决会**永远读到 100%**。同时 `missing_total` 被抬高约 **370**（数据质量毫无变化），`missing/requested` 约 0% → **6.2%**；每轮多发 **7 个 ulist 批次**（`BULK_LIMIT=50`：`ceil(5553/50)=112` → `ceil(5923/50)=119`），这 370 个 code 保证不返回任何行，是纯开销。

⇒ 修复必须**带配套改动**：把「在 active membership 中但本轮无可评价行情」与「provider 真的没返回」分开计桶，否则是用一个新假绿换掉一个真缺陷。

### 【P1】对端 §6 的机制被判错：`list_date` 不会因为 membership 修复而丢失

对端引用的源码事实**全部属实**（`CLIST_FIELDS` 含 `f26` 而 `ULIST_FIELDS` 不含；`_quote_of` 在价格无效时提前返回；`ClistPage.codes` 只存 code）。但结论不成立：`state.universe` 全仓**只有 `engine.py:1347` 一个写入点，只增不清**，而 `filters.list_dates`（`:1349-1353`）遍历的是这个**累计**映射。⇒ 只要一只股票曾被发现可用，其 `list_date` 就一直保留，**第 1 层判据照常命中**。

阳性/阴性对照（`DISCRIMINATING=True`，N=1..10 两臂取值不同）：
`ARM A`（对端假设：无条目）`_too_new` 全为 `False`；`ARM B`（真实代码：条目保留）上市 1/2/5/6/8/10 天均为 `True`，11 天起为 `False`。

真正未被保护的窗口更窄也更准：触发条件是「**从未被观测到可用行情**」——第 1 层无条目、第 2 层名称已失去 `C` 前缀（实测 `is_new_listing('沈鼓股份')=False`、`is_new_listing('C沈鼓')=True`）⇒ day 6–10 通过 11 天过滤。边界：窗口仅 5 天宽、必须是新股、且 `filters.py:76-77` 的 `is_suspended` 仍会挡住停牌股本身，故定为 **P1 待验证风险**而非 P0。ID：`IT-H08-NEWLIST-DAY6-10-UNPROTECTED-WHEN-NEVER-USABLE-001`。

### 【P2】对端 §5 的 `10000/10000` 是重言式，零判别力

独立复算（同 seed=230923、同状态空间）量级与排序复现：`current=477 / shrink=350 / union=614 / transport=10000`（对端报 468/330/600/10000；逐位不等属预期，对端未公布 `random` 调用顺序，故只声称量级复现）。但把目标定义为 `(prev − removals) ∪ new`、又把 `transport` 按同式构造 ⇒ **`target == transport` 在 10000/10000 个 case 上成立**，「strategy4 == target」是代数恒等，不是被测出的性质（方法学 kkk2）。

让 feed 不完整（对端 §9 自己证明会发生）后：

```text
feed 截断  0%: current=477 shrink=350 union=614 transport=10000
feed 截断  1%: current=  0 shrink= 84 union=614 transport=    0
```

⇒ `10000/10000` 是「假设 feed 完美」的产物；该报告内部自相矛盾——§9 的失败模式被排除在 §5 的状态空间之外。这不否定其结论，只否定该证据的强度。ID：`IT-H08-MEMBERSHIP-PROPERTY-TEST-IS-TAUTOLOGICAL-001`。

### 【P2】对端引用的三个本地产物在任何地方都不存在

`membership_strategy_properties.json` / `membership_metadata_guard.json` /`universe_membership_truth_contract_v2.json`：穷尽检索磁盘三棵树 +`git log --all --full-history`（特意加 `--full-history`，默认历史简化会静默漏掉被合并丢掉的路径）⇒ **磁盘 0 · 全历史 0 · tip 树 0**。而同目录本就有 11 个 `.json` 产物，说明是**真的没上传**，不是「目录不存 json」。§5 的 10,000 case 与 §6 的元数据合同因此**不可独立复核**。ID：`IT-H08-PEER-LOCAL-ARTIFACTS-NOT-PUBLISHED-001`。

### 【P2 待验证风险】索引快照链覆盖不全 —— 但我**不**把它当成回归

我最初怀疑索引被腰斩（tip 2,826 B/61 行 vs 工作树 6,853 B/125 行、历史最大 42,922 B/570 行），但 tip 版本**自己写明**这是指针式设计（指向 `55ee49a` 的不可变快照），且 tip 索引 4 个链接**全部解析成功、悬空 0**。⇒ **这是假阳性，我不报它**；它与 EventNet 线那个「净少 14 行、多 0 行」的纯倒退形态不同，不可混为一谈。

但沿快照链回溯：11 个节点后断链，并集只命名 **44/96** 份带日期报告，**53 份从未被链上任何节点提到**；改用与格式无关的 basename 子串普查复核（只看 markdown 链接语法会漏 —— 匹配不到东西的过滤器是无效探针）：最大节点与历史最大版本都只命名 **18/96**，**不存在**覆盖全部 96 份的「完整索引」。
我**没有**找到任何文字声称该链必须覆盖全部 96 份（「完整」修饰的是「上一版」），故定为**待验证风险**而非已确认缺陷。ID：`IT-H08-INDEX-SNAPSHOT-CHAIN-INCOMPLETE-001`。

### Pagination P2：我用**真实类**离线复现，并定位到分支

对端 §9 与 05:35 轮都声称：无 numeric total 时，终止页「raw 有码、usable 为空」会让 `break` 发生在 `pages.append` **之前**（`eastmoney.py:432-437`）。**我没有引用它，我用真实 `EastmoneySource` + 注入 fetcher 实测了它**（3 页 × 100 行，不走网络）：ARM A（无 total，page2 raw 有码/usable=0）`pages requested=[1,2]`、`usable=100`、`raw_unique_codes=100`（page2 的 100 个 raw code 丢失）；ARM B（阴性对照，page2 usable）`pages requested=[1,2,3,4]`、`raw_unique_codes=300`；ARM C（有 numeric total）`pages requested=[1,2,3]`、`raw_unique_codes=300`、`transport_complete=True`。⇒ 两条 CLAIM 均为真，且缺陷**特定于无 total 分支**（有 total 时走 `:413-428` 的 `_scan_pages`，不测试可用性）。`DISCRIMINATING=True`。

附带发现（P2，`:404`/`:437`/`:439`/`:482`）：同一分支的台账会把**实际抓取的页数**报少 —— 实测 A 实际抓 2 页而 `pages_requested=0`（page 1 在 `:410` 就请求了，该分支从未计入它）；**消费面为零**（引擎门禁只读 `complete`/`transport_complete`，`engine.py:1157-1175`），故定 P2。ID：`IT-H08-PAGES-REQUESTED-UNDERCOUNTS-IN-NO-TOTAL-BRANCH-001`。

### 【P2 已确认】正常的翻页重叠会把整条会话覆盖率轴静默记成「未测量」

`eastmoney.py:453-457` 明确记载翻页边界重叠是**正常上界重叠**（实测 5913 → 6020），`tests/test_eastmoney_truncation.py:604` **正是断言** `6020 > 5913`。但 `engine.py:1001` `coverage_active = _ratio(active_n, exp_n)` 无上界钳位，而 `tools/live_session.py:486-491`（及 `:616-617` 的会话聚合）把 `[0,1]` 之外一律记 None。

**我用真实 `Engine` 独立复现**（未直接引用子 agent）：`active_n=5913 -> 1.0`；`5914 -> 1.0001691188905801`；`6020 -> 1.0180957212920683`（越界）。钳位边界精确落在 **5913/5914** 之间。会话聚合 30 轮：无重叠 → 30 个样本（`min=0.939`）；全重叠 → **0 个样本**。**阴性对照**（`0.939`、`0.761035` 均在区间内且正常参与 min）证明这**不是**聚合器坏了，而是**专门针对 >1 的静默关闭**。`DISCRIMINATING=True`。

⇒ 「未知 != 0」的钳位**用意是好的**，但它假定了「合法值必在 [0,1]」——而重叠态下合法值**可以合法地 > 1**，于是**正常态被当成脏值**；真实退化（池子被砍）与「会话没跑」变得**不可区分**。定 P2：重叠是间歇的、且它不产生错误告警，只丢失诊断能力；真实轮次占比**未测**。修法：按**语义**而非值域判断（保留原值 + 记录 `overhang` 原因），**不可**一律 clamp 到 1.0（那是同一类错误的镜像）。ID：`IT-P2-UNIVERSE-COVERAGE-OVERHANG-UNMEASURED-001`。

### 仓库更名（运维事实，必须记录）

GitHub 远端已由 `fy-god/ashare-radar` 更名为 **`fy-god/intraday-tape-reader`**：旧 URL 实测 `rc=128 remote: Repository not found.`，新 URL `rc=0`。本地 `origin` 已指向新名，**无需改动**。

### Pagination P2（对端 §9，我复核其机制描述一致）

`break` 发生在 `pages.append` 之前 → 终止页自身的 raw code 也丢失，且后续页不请求。对端已由 05:35 的真实离线 arm 确认；本轮**未重跑**该 arm。

### 事实/推理/推测 与三轴

- **事实**（源码实测，可复核）：上述所有 `file:line`、三臂实验数值、属性测试复算、三处穷尽检索结果。
- **推理**：由 `state.universe` 只增不清推出 `list_date` 不丢；由 `coverage_active` 定义推出其将恒等于 `coverage_transport`。
- **推测**（未在真实行情复现）：day 6–10 窗口实际是否产生误报。已按手册降级为「待验证风险」。
- `execution_status` = 真实 checkout 读取 + 真实 `Engine.poll_once()` 三臂 + 两处对照实验 + 属性测试复算；真实行情网络 / 通知器 / 交易 / 模型训练 = `not_run`（列明原因）。
- `research_verdict` = `MEMBERSHIP_FIX_REQUIRES_COMPANION_LEDGER_CHANGE` / `PEER_LISTDATE_LOSS_MECHANISM_REFUTED` / `PEER_PROPERTY_TEST_IS_TAUTOLOGICAL` / `PEER_LOCAL_ARTIFACTS_ABSENT` / `INDEX_CHAIN_INCOMPLETE_NOT_A_REGRESSION`。
- `evidence_status` = `VERIFIED_BY_INDEPENDENT_REPRODUCTION_AND_CONTROL_PAIRS`。
- `evidence_type` = **软件样本**；**实股结果无新增**，真实 Precision/Recall/漏报率/收益 = `unavailable`。
- **程序修复 0 条新增（本轮未改任何源码）/ 任务定义变更 0 条 / 真实模型增益 0**。

### 子 agent（全部只读；我逐条独立复测后才采信）

A（源码事实 7 项）：6 CONFIRMED / 1 REFUTED —— 第 4 项确认 `state.universe` **永不收缩**（故对端把回归范围**写宽了**），第 7 项**独立**用 133,966 文件全盘 + 全历史 + stash 否证该产物存在，且枚举 **12 种抽样约定无一**复现对端那四个 tally。D（对抗性证伪）：C1 **REFUTED**（用「定义不变、只让输入有缺陷」的决定性控制臂，得分 10000 → 0/10000）；C2/C4/C5 SURVIVED；C3 **PARTIALLY_REFUTED**（实测 **370/370** 个停牌码**全部保留**精确 `list_date`，独立复现我 §2 的收窄）。D 还**更正了我提示词的一处错误前提**（该测试不是安全语义）并**撤回**了对应攻击路径。C（机制追踪）：指出修复必须搬 `(code, name, list_date)` 三元组而**不能只搬 code**（否则 `list_dates` 对**全部**成员失效、第 2 层名称判据也瞎）；实测「进了 active universe 却没 Quote 的代码**什么都不发生**」，且全仓**无** active 数 vs 传输总数的对账断言（85 处消费者全查）。**B（全量 pytest + 8 个 `check_*.py` + 12 项回归）截稿前未返回 ⇒ 本节相关项一律 `not_run`，本报告不声称任何通过数或退出码。**

### 三条研究想法（均可本地离线验证）

1. 给 `LATEST.md` 加一条可执行索引门禁：断言每份 `*_JST.md` 至少被 tip 索引或链上某快照命名，且所有链接可解析（EventNet 线已证明索引会静默退化，本仓无任何机制检查）。
2. 把「无行情的 active 成员」做成显式一等公民：注入 N 只停牌成员，断言 `missing_total` 不随 N 增长（引入新桶之后）——可离线判真伪的不变量。
3. 补「feed 不完整下 membership 语义」的不变量：正确的问题不是「哪种策略在完美 feed 下对」，而是「feed 截断 k% 时 active membership 应收缩还是保持、依据什么证据」。

`report_created_commit_sha`: `PENDING_BACKFILL`

---
## 2026-09-23 08:05:58 JST

主实验：`EXP-IT-MEMBERSHIP-FIX-008`

### 当前最高优先

`IT-P1-UNIVERSE-MEMBERSHIP-QUALITY-001`

05:35 独立 checkout 已真实确认：

```text
before active = 5913
raw/expected  = 5923
usable Quote  = 5553
after active  = 5553
370 old members lost
10/10 new members enter
full suite = 1802 passed
```

本轮新增结论：两个直觉修法都不能作为最终实现：
- complete-path shrink guard 会保旧池但挡住10个新成员；
- union(prev, usable) 会无法删除真实退市/移除成员。

最终合同必须：
**transport membership replacement**，且 Member 必须带 `code/name/board/list_date/source`；不能只存 code，也不能造 fake zero-price Quote。

### Pagination P2

05:35 已真实确认：unknown-total 分支 `break` 在 `pages.append` 前；一页 raw code 存在但 usable Quote=0 时：
- 终止页 raw members 自己被丢；
- 后续页不请求。

只移动 append 不够；终止语义必须基于 raw transport emptiness。

### 历史真实数据上下文

05:35 本地 Agent 的历史面板测量（本轮未重跑）：p50 0.81%、p95 11.61%、p99 17.58%、max 50.15%，956/3157=30.3% 交易日停牌率 >= synthetic 6.26%。它只支持优先级，不是当前实时停牌率、Alert Recall 或漏报率。

下一轮：Membership RED/GREEN/rollback → member metadata → pagination → reconcile → SourceManager [] → Timezone → ObservationInterval → provenance/research。

模型训练：0；真实 Precision/Recall/漏事件率/收益 unavailable。

## 历史接续

上一版完整索引与全部历史报告仍保存在不可变 commit `55ee49a222e1e598e9845d98e4e5f472d894e174` 以及本目录历史文件中；本轮不删除历史报告。
