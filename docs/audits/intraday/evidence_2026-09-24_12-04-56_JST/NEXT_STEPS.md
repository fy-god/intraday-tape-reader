# NEXT_STEPS — 2026-09-24 17:35:20 JST

按价值排序。每条都写明**前置条件**与**验收测试**，避免下一轮又在小修小补。

---

## 0. 用户真正的诉求（必须优先记住）

用户要的是**盘中实盘"急拉急跌"预警能跑起来**，不是审计报告。
本轮结束时 `spirit_*` 仍 `enabled: false`。

**最长杠杆的一步**：把 WP04 做完（`call_detailed` 接进 `poll_once`），
这才是"账本真的在线"的前提。做完 WP04 + WP03 之后，
才谈得上打开 `spirit_*` 并用真实盘中数据检验。

---

## 1. WP04 — `call_detailed` 接进 `poll_once`【最高优先】

**现状**（本轮实测）：
```text
src/arad/engine.py 里 call_detailed 只出现 1 次 —— :591 的**定义本身**
poll_once -> _fetch_indices -> self.sources.call("snapshots", ...)  <- legacy 链
```

**前置条件**：WP01/WP02/WP03/WP05 的门已满足（云端 12:04 已认定
`7a4f549` 修复了 wrong-type failover 与 source/route binding）。

**做法**：
1. `_fetch_indices()` 与个股取数改调 `self.sources.call_detailed(...)`。
2. **先冻结 RED**：现在 `call_detailed` 的返回是 `SnapshotFetchResult`，
   而 `state.update_detailed` 吃的是 `list[Quote]` —— 中间必须有一个
   明确的**转换点**。在改之前先写测试钉住"转换后 identity 保持"
   （`Quote.code` 仍是裸码、index 仍是 prefixed）。
3. **回退验牙**：把 `call_detailed` 换回 `call`，接线测试必须红。

**验收**：`grep call_detailed src/` 出现**非定义**的调用点；
`live_session` 的 `raw_return_coverage` 能非零。

---

## 2. WP03 — Per-route terminal buckets + 不相交并集不变式

**前置**：先解决 R2（见报告 §8）：**桶是代码集合还是条数？**

当前代码里 `time_rejected_codes`（按**代码**）与 `future_rejected`
（按**条数**）口径不同 —— 直接相加对不上账。

**做法**：
1. 先用一个 scratch 脚本在**当前**代码上算
   `len(requested_codes)` vs `sum(len(bucket))`，把差额写下来（预期非零）。
2. 统一定义：建议全部改成**代码集合**（因为 "Requested = 不相交并集"
   这句话只有在集合语义下才成立）。
3. 立不变式 + 加断言：`Requested = disjoint_union(admitted, missing,
   quality, future, ooo, stale, unattributed)`。
4. `unattributed` 是**兜底桶**，其值必须被监控 —— 长期非零说明还有
   没识别的终态，是发现新 bug 的入口。

**验收**：不变式测试上线；注入一个"未知拒绝原因"能让
`unattributed` 非零并使测试红。

---

## 3. WP06 的姊妹缺陷 — `IT-P1-006-R1`（Eastmoney max_pages 截断仍标 complete）

**现状**：`eastmoney.py` 有 `truncated = required_pages > self.max_pages`，
且 `transport_complete` 已把它纳入（`and not truncated`）。
**待验证**：`IT-P1-006-R1` 声称"截断仍标 complete" —— 本轮**未独立复核**它。
先跑一个 scratch 验证：
```text
total 极大（如 100000）使 required_pages > max_pages
-> 检查 meta["transport_complete"] 是否真的 False
```
若已 False，则 `IT-P1-006-R1` 应**下调为已修**；若仍 True，则它是真缺陷。

**注意**：本轮改的 `universe()` 分支只影响 **no-total** 路径，
**不影响** max_pages 截断（那条走 `if total > 0` 分支）。两者不要混为一谈。

---

## 4. WP07 — Membership 分离

transport membership（停牌也算）与 usable quote（要有价）分离。
本轮 WP06 的修复**正好是它的前置知识**：我已经证明
`raw_code_rows > 0` 而 `quotes == []` 是**真实存在**的状态
（停牌页），所以两层必须分开数。

---

## 5. WP08 — exact soft-empty

`engine.py` 的成功判据仍是"有没有抛异常"。软部分返回
（返回了但全是停牌/空）与完全空返回，当前都让 backup 调用 **0** 次。

**前置**：WP03 的 terminal buckets 做完，才有"soft-empty"的精确定义。

---

## 6. WP09 — Timezone / ObservationInterval

`src/arad/session.py:100 _now_fn` 仍 **0 引用**。零覆盖。
`ObservationInterval` 在文档里 85 次命中，`src/`/`tests/`/`tools/` 里 **0** 次。

**做法**：先写一个测试让 `_now_fn` 可注入（否则它永远测不到）。

---

## 7. 技术债（低优先，但迟早要还）

### 7.1 `call` / `call_detailed` 是两份拷贝
failover + 计账逻辑重复。做完 WP04 后应把 `call` 变成薄包装。

**风险**：`call` 的 `try` 边界**从未**被检查过是否有 WP03 同类缺陷
（错类型返回值会让绑定代码抛异常）。**建议顺手审一遍。**

### 7.2 `_admit_time` 的 `why` 用字符串协议（报告 §8 R1）
改成 `Literal` 或枚举，避免拼错时静默漏计。

### 7.3 `tencent.py` 类级出口的 `idx_set` 推导
`snapshots()`（`:757`）与 `snapshots_detailed()`（`:889`）仍从
"调用方写了显式前缀"推 `idx_set` —— 是 v1 根因。
`is_index_role` 的 `idx_set` 兜底分支因生产恒发统一 route 而覆盖薄弱。
**建议**：把 `idx_set` 参数直接删掉（若确认无调用方需要），
或补一条"混合前缀"的直测。

---

## 8. WP10 — Research（blocked）

无可动的部分。需要真实多日 manifest。
**不要**在没有真实数据时编造 `dataset_manifest.json` / `trials.jsonl` ——
本轮 `not_run/BLOCKED.md` 已如实标注为 blocked。

---

## 9. 流程提醒（给下一轮）

1. **写测试前先问：它会走进被测分支吗？** 本轮 WP06 第一版就没走进去
   （D15 再犯）。用 scratch 脚本打决策点是有效手段。
2. **gate 不要用子串匹配源码** —— 会被自己的注释欺骗
   （本轮 §4.2）。用 AST。
3. **回退验牙的 target 集合要写对** —— RB-C 那次是**我的 target 错**，
   不是 gate 没牙（本轮 §4.1）。
4. **任务书里的数字要跟仓库常量核对** —— 900s vs 14400s（本轮 §4.4）。
5. **`pyproject.toml` 的 `addopts="-q"`** 会吞汇总行，务必 `-o addopts=""`。
6. **PowerShell 写文件用 `[System.IO.File]::WriteAllText(..., UTF8Encoding($false))`**
   —— `Set-Content -Encoding utf8` 会写 BOM。
