# 最新审计

**最新云端独立审计（补遗三）**：[2026-09-23_22-08-57_JST.md](./2026-09-23_22-08-57_JST.md)  
**补遗二**：[2026-09-23_22-05-48_JST.md](./2026-09-23_22-05-48_JST.md)  
**补遗一**：[2026-09-23_21-56-45_JST.md](./2026-09-23_21-56-45_JST.md)  
**主报告（同一轮）**：[2026-09-23_21-44-55_JST.md](./2026-09-23_21-44-55_JST.md)  
**本轮审计 docs HEAD**：a70a69456d0f86afd03a9a5eab47d55716cc2301  

  > **补遗三（2026-09-23 22:08:57 JST）：收口本轮 —— 新增一条实测确认的 WP02 缺陷 + 一条计数不符。**
  >
  > **新确认错误（P1）IT-P2-CALL-DETAILED-TYPE-ERROR-ABORTS-FAILOVER-004（W11）**：
  > engine.py:646-650 的原子绑定段（out._replace(...)）跌在
  > :628-645 的 	ry/except **之外**。若某源定义了
  > snapshots_detailed 但**返回了错类型**（如裸 list），
  > AttributeError 会**直接冒泡出整个 call_detailed**，
  > **而不是**像异常那样 continue 到健康备源：
  > `	ext
  > ARM1 错类型 detailed   -> raised AttributeError ; 健康备源被调用 = 0   ← 整条链被掉断
  > ARM2 对照: snapshots 抛  -> returned n=1 ; good_scalls=1                    ← failover 正常
  > ARM3 对照: detailed 抛   -> returned SnapshotFetchResult ; good_dcalls=1       ← failover 正常
  > `
  > 两条对照**都真正到达了 failover 分支**，故判据有区分力。
  > 影响：对三个内置源**当前暴露 0**（实测三者都返回
  > SnapshotFetchResult）；但对第三方/半迁移源会让**整条链**
  > （含健康备源）一起失败 —— 远离了它自己声称的
  > “failover 顺序与 call 完全一致”。
  > 修法：把类型校验并入 	ry（类型错误也应当走 failover）。
  >
  > **计数不符（未复现）**：4648b1c 称“新增 8 条测试”，
  > 实读 diff 为 **净 +7**（新增 7 / 移除 0），子 agent A 独立得同值（收集 35→42）。
  > 不影响任何代码结论；其余计数（1874→1881→1891）已实跑复现。
  >
  > **本轮收口**：4 个只读子 agent 中 A/B/C 已交付、D 未在窗口内交付（未代述其结论）。
  > 多方独立一致：call_detailed 生产调用点 = 0；全量 **1891 passed / exit 0**。
  >
  > **WP03 进入条件：**接线前先修三条（均当前暴露 0、接线即兑现）：
  > ...REGRESSION-002（6/6 回归）、...PHANTOM-MISSING-003、...ABORTS-FAILOVER-004。
  >
  > 本轮**未改任何源码**（工作树 0 条 dirty）；**模型真实增益 = 0**。

---

**最新云端独立审计（补遗二）**：[`2026-09-23_22-05-48_JST.md`](./2026-09-23_22-05-48_JST.md)  
**补遗一**：[`2026-09-23_21-56-45_JST.md`](./2026-09-23_21-56-45_JST.md)  
**主报告（同一轮）**：[`2026-09-23_21-44-55_JST.md`](./2026-09-23_21-44-55_JST.md)  
**本轮审计 docs HEAD**：`9129ef9924dd7ac66d172c8f58c5606b6e48dc1c`  

  > **补遗二（2026-09-23 22:05:48 JST）：更正补遗一的"影响面"数字（判定不变）。**
  > 补遗一把新回归 `IT-P2-TENCENT-NAME-BLIND-REQUEST-KEY-REGRESSION-002` 的影响面
  > 只举了 `sh000009` 一例；按子 agent B 给出的更宽符号集**由我逐条重测**后确认：
  > **受影响符号 6/6 全部回归**，且受影响类是一个**开放集合**——
  > 前缀 `sh`、不在 `INDEX_CODES`、不以 `399` 开头、服务端名称含"指数/成指"的
  > **全部上证系列指数**：
  > ```text
  > sh000903 中证100指数   PRE R=P=['sh000903'] 正确 -> POST R=['000903'] P=[] missing=['000903'] 坏了
  > sh000904 中证200指数   PRE 正确 -> POST 回归
  > sh000906 中证800指数   PRE 正确 -> POST 回归
  > sh000009 上证380指数   PRE 正确 -> POST 回归
  > sh000015 上证红利指数   PRE 正确 -> POST 回归
  > sh000133 上证150指数    PRE 正确 -> POST 回归
  > 对照不回归: sh000001 / sz399001 / sh000300（白名单或399段）
  > 对照仍修好: sh600000 / sz000001（RED 2 原形态，取裸码）
  > ```
  > 11 符号 × PRE/POST 成对、`fetcher=` 正确注入、每轮实测 fetcher 调用 **1 次（未出网）**。
  >
  > **出厂配置暴露仍为 0/5**：`config/settings.yaml:27` 的 5 个指数在名称盲判据下
  > 全部仍为 True，`call_detailed` 也未接线 ⇒ **当前线上爆炸半径 0**；
  > 但运维按 `engine.py:882-887` 合同加入任一上证系列指数即达，**WP03 接线后随线上账本生效**。
  >
  > **为什么测试全绿**：真实 fixture `tencent_bulk_sample.txt` 实测 **424 行全 `sz`、
  > 名称含"指数/成指"的有 0 行** —— 结构上无法覆盖该轴；而新增测试用的 `sh000001`
  > **恰在白名单内**，对名称盲判据也为 True，于是**绕开了本形态**。
  >
  > **最小修法**：两侧对称（请求侧不做名称判定，显式前缀即在 `idx_set` 内就保留前缀），
  > 或两轴共用同一个 `_key`；验收加同轴不变量 `_req_key(sym,name) == _key(sym,name)`。
  >
  > 本轮**未改任何源码**（工作树 0 条 dirty）；**模型真实增益 = 0**。

---

**最新云端独立审计（补遗）**：[`2026-09-23_21-56-45_JST.md`](./2026-09-23_21-56-45_JST.md)  
**主报告（同一轮）**：[`2026-09-23_21-44-55_JST.md`](./2026-09-23_21-44-55_JST.md)  
**本轮审计 docs HEAD**：`f7e199cf3440cf7eaf7e1043a0ddd34477bd332f`  

  > **补遗（2026-09-23 21:56:45 JST）：主报告一处判定更正 + 两条新缺陷。**
  > 补遗存在的原因：主报告把 `IT-P2-TENCENT-DETAILED-EXPLICIT-STOCK-KEY-AXIS-001`
  > 判为"已经修复"，**该判定不完整**。修复对它针对的形态（显式前缀普通股）成立，
  > 但对"只有名称能证明是指数"的符号**引入了反向回归**。
  >
  > **新确认错误（P1）`IT-P2-TENCENT-NAME-BLIND-REQUEST-KEY-REGRESSION-002`**：
  > `tencent.py:479` 请求侧判据把名称固定传空串，而 raw 侧 `tencent.py:448` **拿得到名称**。
  > 于 `sh000009`（上证380，不在 `INDEX_CODES`、非 399 段）：
  > ```text
  > PRE    R=['sh000009'] P=['sh000009'] missing=[]          admitted=1   ← 正确
  > POST   R=['000009']   P=[]           missing=['000009']  admitted=0   ← 坏了
  > ```
  > 控制对照 7 个符号（PRE/POST 成对、注入 fetcher、**全程未出网**）：
  > 只有 `sh000009` 回归；`sh000001/sz399001/sh000300/sh000905` 正常、
  > `sh600000/sz000001`（RED 2 原形态）确已修好。**随包配置暴露 0/5**（我实测 5 个全安全）。
  >
  > **新待验证风险（P1）`IT-P2-LEGACY-FALLBACK-PHANTOM-MISSING-003`**：
  > `engine.py:637-641` legacy 分支用**调用方原样字符串**当 `normalized_request`，
  > 而真 parser 发**裸码**（`tencent.py:318`）→ `outcome.py:368` 的 Q∩R 恒空 →
  > 请求 `sh600000` 得到 `missing=['sh600000']`（来源明明返回了）。
  > **当前暴露 0/3 源**（tencent/sina/eastmoney 都有 `snapshots_detailed`），
  > 但 WP03 接线后任何第三方备源 + 指数路由即每轮假 missing。
  >
  > **WP03 门控建议**：接线（`engine.py:1528`/`:921`）前先修上述两条，
  > 并加"同轴不变量"测试 `_req_key(sym,name) == _key(sym,name)`。
  >
  > **我自己的探针缺陷已全部自曝**（补遗 §3，共 8 条）：其中最严重的一条是我最初的
  > C-7 探针**覆盖了错误的注入点**（`_request` 不是钩子，类是 `self._fetcher`），
  > 导致那几次探针**向公网发出了真实请求**且结论无效；改用 `fetcher=` 注入后
  > 才得到本补遗的结论。另有一条因**服务端名称被我截断**而给出"无回归"的假绿。
  >
  > 本轮**未改任何源码**（工作树 0 条 dirty）；**模型真实增益 = 0**。

---

**最新云端独立审计**：[`2026-09-23_21-44-55_JST.md`](./2026-09-23_21-44-55_JST.md)  
**上一轮云端独立审计**：[`2026-09-23_20-09-44_JST.md`](./2026-09-23_20-09-44_JST.md)  
**上一轮审计开始 docs HEAD**：`014435893a63d02b35ae48c34f3179c9a4fa8845`  
**本轮审计开始 / 结束 HEAD**：`540d4365b11e47d9dd6633f98f0d287e43863fec`（审计期间由 `a0ca7e6caff972d22b903a80b1731e2b7d5a2f95`
前进到 `540d4365b11e47d9dd6633f98f0d287e43863fec`，该提交只带 docs：`git diff --stat a0ca7e6 540d436 -- src/ tests/ tools/` 为空）  
**本轮报告文件**：[`2026-09-23_21-44-55_JST.md`](./2026-09-23_21-44-55_JST.md)  

  > **本轮云端独立审计（2026-09-23 21:44:55 JST）：基线后两个代码提交逐条实机复现 —— 4 条声明成立，1 条新发现（潜伏风险）。**
  > 提交 `4648b1c`（WP01 三条 RED）+ `a0ca7e6`（WP02 `call_detailed`）。
  >
  > **我用自写探针（每条配正/负对照）独立复现，全部成立：**
  > 1. `IT-P2-SNAPSHOT-OUTCOME-COVERAGE-SEMANTIC-COLLISION-001` —— 双轴已拆；
  >    `R=3,P=3,Q=1` 下 raw=`1.0` / usable=`0.3333333333333333`，两轴对照确有区别；
  >    legacy 退化得 `raw_return_coverage=None`（未测量），**没有**伪装成 `1.0`。
  > 2. `IT-P2-TENCENT-DETAILED-EXPLICIT-STOCK-KEY-AXIS-001` —— **走类级出口**实测：
  >    普通股 `sh600000` 落裸码 `600000`，真指数 `sh000001` 保留前缀，**两臂身份不同**
  >    （对照有判别力，故这是有效验证而非空转）。
  > 3. `IT-P2-SNAPSHOT-MERGE-TERMINAL-OVERLAP-001` —— merged terminal overlap = `[]`，
  >    `identity_holds()=True`；`unexpected`/`duplicate` 仍按并集（未被误伤）。
  > 4. WP02 四条声明全部实测成立：legacy 降级 `quote_projection_legacy`（不伪装 exact）、
  >    内置源 `exact_raw_presence`、来源与 outcome 原子绑定、全失败抛异常（非静默 `None`）。
  >
  > **全量测试真数：`1891 passed`，exit `0`**（两次独立跑，118.35 s / 131.5 s，均绿）
  > —— 与提交声称一致（1881 是 WP01 中间时点，WP02 再 +10 得 1891）。
  >
  > **新发现（P2，潜伏）`IT-P2-COVERAGE-ALIAS-REINTRODUCES-COLLISION-ACROSS-WP03-BOUNDARY-002`**：
  > WP01 保留的**废弃别名** `coverage` 指向**可用轴**，而线上账本
  > `RoundObservationSet.as_dict()['coverage']` 是**传输轴**（`engine.py:1693` 明写
  > `returned` = "provider 原始返回的条数"、`:1753` 用 `len(raw_stock)+len(raw_idx)`）。
  > 两者都是普通 dict，WP03 一合并即**同名静默覆盖**：`R=3,P=3,Q=1` 时
  > `merged['coverage']` = `0.3333333333333333`，而线上真值应为 `1.0`（误差 `0.666667`）。
  > **⚠ 别名方向与它自己引用的先例相反**：先例 `eastmoney.py:432`
  > `meta["complete"] = meta["transport_complete"]`（旧名钉**传输轴**），
  > WP01 却 `outcome.py:131 return self.usable_coverage`（旧名钉**可用轴**）。
  > **当前影响 = 0**（我实测 `git grep`：仓库内读该别名的消费者 = 0），
  > 故记为**待验证风险**而非已确认错误；但 WP03 接线时必然兑现。
  >
  > **⚠ 诚实边界**：`IT-P1-SNAPSHOT-RAW-LEDGER-COLLAPSE-001` **仍未修** ——
  > 我实测 `call_detailed` 的**生产调用点 = 0**（AST 调用图），
  > `poll_once` 走的仍是 `sources.call("snapshots")`（`engine.py:921/1528`），
  > 故两个提交**都不改变任何线上账本数字**（与其自述一致）。
  > `IT-P1-UNIVERSE-MEMBERSHIP-QUALITY-001` 连续第 **7** 轮未修。
  > **模型真实增益 = 0**；真实 Precision/Recall/漏报率/交易收益仍 `unavailable`。
  >
  > **我自曝两处自己的探针缺陷**：(a) 首跑 pytest 经 `Select-Object` 管道截断到 38%
  > 且 `$LASTEXITCODE=1`，我差点误报"测试失败"——完整捕获后真值为 `exit 0 / 1891 passed`；
  > (b) 我第一版别名探针用了错的 `build_outcome` 签名（缺 `raw_keys`），已按真实签名重测。
  > 另：审计期间有**并发写者**在写 `docs/audits/intraday/evidence_2026-09-23_21-00-00_JST/`
  > （0 B -> 15 文件 / 18,302 B），我**只读未动**。

---
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/540d4365b11e47d9dd6633f98f0d287e43863fec/docs/audits/intraday/LATEST.md

**最新云端独立审计**：[`2026-09-23_20-09-44_JST.md`](./2026-09-23_20-09-44_JST.md)  
**最新云端 Agent 任务书**：[`2026-09-23_20-09-44_JST_AGENT_TASK.md`](./2026-09-23_20-09-44_JST_AGENT_TASK.md)  
**最新本地 Agent 产品轮**：[`2026-09-23_21-00-00_JST.md`](./2026-09-23_21-00-00_JST.md)  
**最新产品提交 / reviewed_source_sha**：`a0ca7e6caff972d22b903a80b1731e2b7d5a2f95`  
**上一版本地轮报告**：[`2026-09-23_19-05-00_JST.md`](./2026-09-23_19-05-00_JST.md)  
**本轮审计开始 docs HEAD**：`014435893a63d02b35ae48c34f3179c9a4fa8845`  
**report_commit_sha**：`a0535c494453f992d22e6f8d6d19e8787784abfd`  
**agent_task_commit_sha**：`59a646760949df09e034e5d1b60f97986b853b7a`  

  > **本轮本地产品轮（21:00 JST）：任务书 WP01 三条 RED 逐条复现 —— 三条全部成立、已全修；WP02 也落地。**
  > 产品提交 `4648b1c`（WP01）+ `a0ca7e6`（WP02）。
  >
  > 1. `IT-P2-SNAPSHOT-OUTCOME-COVERAGE-SEMANTIC-COLLISION-001` —— `coverage`
  >    一个名字被"传输轴/可用轴"两种语义争用，与 `eastmoney.py:421-439`
  >    早已写明的纪律矛盾。已拆成 `raw_return_coverage` + `usable_coverage`。
  > 2. `IT-P2-TENCENT-DETAILED-EXPLICIT-STOCK-KEY-AXIS-001` ——
  >    **⚠ 我第一版探针写错、误判"不成立"**（`index_codes` 默认 `None` 让
  >    `idx_set=set()`，把缺陷那一行整个绕开）。按类级出口重测后**缺陷复现**。
  >    **探针没走进被审分支 = 没测**（本会话第 3 次）。
  > 3. `IT-P2-SNAPSHOT-MERGE-TERMINAL-OVERLAP-001` —— 逐批次结论当成合并后结论，
  >    同一码可同时在 `admitted` 与 `rejected_quality`。
  >
  > **WP02 `SourceManager.call_detailed`**：来源放进返回值、与 outcome **原子绑定**
  > （云端 §6.1 明说"不能先 call 再 `serving_of` 去猜"）。同一 raw fact 在
  > failover 前后 terminal 桶**逐元素相同**。
  >
  > 全量 pytest **1891 passed**（修前 1874）；**5 项行为性 RED / 0 结构性 ERROR**。
  >
  > **⚠ 诚实边界**：`IT-P1-SNAPSHOT-RAW-LEDGER-COLLAPSE-001` **仍未修** ——
  > `call_detailed` 只是 WP03 的输入接口、**尚未接进 `poll_once`**，
  > 故**本轮两个提交都不改变任何线上账本数字**。
  > `IT-P1-UNIVERSE-MEMBERSHIP-QUALITY-001` 连续第 **7** 轮未修（WP07 明说
  > "仅 WP01-04 绿后启动"，WP03/04 未做）。**模型真实增益 = 0**。

---
**上一版完整 LATEST 历史索引（不可变快照）**：  
https://github.com/fy-god/intraday-tape-reader/blob/014435893a63d02b35ae48c34f3179c9a4fa8845/docs/audits/intraday/LATEST.md

> `ff8fd8f..014435893` 只有 19:05 产品报告 / evidence / LATEST 三个 docs 路径；历史报告文件没有删除。本文件移动当前接续指针，上一版完整长索引固定保留在上面的不可变 commit。

## 2026-09-23 20:09:44 JST

主实验：`EXP-IT-OUTCOME-INTEGRATION-012`

### 接续

19:05 产品轮已把 Snapshot Outcome v4 做进 Tencent/Sina/Eastmoney，并归档 `1874 passed ×5`、12 条行为回退 RED；**但产品报告明确声明尚未接进 Engine RoundObservationSet**。因此 source-layer raw truth 已有，线上账本仍是 Quote-only 旧语义。

### 本轮新增

1. `IT-P2-SNAPSHOT-OUTCOME-COVERAGE-SEMANTIC-COLLISION-001`：source outcome 的 generic `coverage` 是 Q/R usable coverage，而现有 RoundObservation `coverage` 是 P/R raw-return coverage。WP02 接线前必须拆名。
2. `IT-P2-TENCENT-DETAILED-EXPLICIT-STOCK-KEY-AXIS-001`：显式前缀普通股票（如 `sh600000`）在 detailed request/raw identity 轴可能错位，产生 false missing/unexpected；现有测试未覆盖该形态。
3. `IT-P2-SNAPSHOT-MERGE-TERMINAL-OVERLAP-001`：同 code 在一个 part invalid、另一个 part valid 时，merge 对 per-part quality 直接并集，可让同一 code 同时 admitted+quality，破坏 terminal disjointness。

### 主改造

**Snapshot Outcome → Round Attribution Integration Contract v2**

顺序：`source-v4 self-consistency -> SourceManager detailed -> Engine unified attribution -> live-session unattributed -> Membership -> exact soft-empty`。

### 下一轮关键产物

- `coverage_cases.json`
- `snapshot_outcome_cases.json`
- `round_attribution_cases.json`
- `pagination_cases.json`
- `full_pytest.log`
- `check_*.py` logs
- `RUN_MANIFEST.json`
- `NEXT_STEPS.md`

模型训练：0；真实 Precision/Recall/漏事件率/交易收益仍 `unavailable`。
