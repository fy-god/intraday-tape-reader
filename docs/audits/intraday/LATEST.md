# 最新审计

最新完整报告：[`2026-09-20_09-32-43_JST.md`](./2026-09-20_09-32-43_JST.md)

## 版本与证据边界（2026-09-20 09:32 JST）

- 仓库与分支：`fy-god/intraday-tape-reader` / `main`。
- 审计开始 HEAD：`ac9011ac7e7b4aa6a00ce365147ef22e7d758955`。
- `reviewed_source_sha`（**产品**提交）：`a88094e915340b4eaec534af2826b5ffef68fe0e`；`60fdee5`、`87b0abd`、`61ebc61`、`ac9011a` 均为审计文档/索引提交，**不计产品升级**。
- 本轮执行位置：**本机本地 checkout**（`D:\ccc\ashare-radar`），非云端隔离沙箱。
- **本轮实测全量**：`python -m pytest -o addopts="" -p no:cacheprovider` → **`1155 passed in 95.74s`，exit 0**。与 05:30 本地归档数一致，故该数字**已由本轮独立复验**。
- `git diff a88094e..HEAD -- src/` **为空** → 产品源码自 `a88094e` 起未变，故所有开放项按**回归**处理。
- 真实多日连续 A 股语料仍 `blocked_no_mounted_multiday_continuous_corpus`；本轮无新研究实验，**无新实股结果**。

## 本轮独立复核结论

1. **`IT-P0-002-R2` 第三次独立确认**：平价新鲜观测不推进水位线 → 迟到价被准入、`latest` 倒退、`ooo=0`。**新增关键事实**：现有测试只覆盖 R2 **变动**价格路径，「平价空洞」**至今无测试**；验收用例已在当前源码上实测为**红**（`1 failed`）。
2. **`IT-P2-OBS-005` 已确认**：provider 返回 `price<=0` 的票落入 `unknown_missing`；`rejected_quality` 桶**有文档零实现**。跨源差异实测：仅 tencent 保留 `price<=0` 行（`tencent.py:294`），故该混算只在 tencent 路径可观察。
3. **`IT-P1-TIME-ROLE-001` 比原报告更硬**：`ts` **就是**排序/水位线键（非仅有效性过滤）；同码跨源 ts 实测差 **2400s / 2328s**，跨源共享 provider-time 水位线**确定会错**。`source_epoch`/`time_role`/`round_id` 在**代码中零命中**（仅提案文本）。
4. **`IT-P0-001` 升级为代码与自身文档矛盾**：`session.py:4` docstring 声明 14:57–15:00 收盘集合竞价，而 `:132` 把它并入 `AFTERNOON`，`CONTINUOUS`(`:58`) 含 AFTERNOON → `is_open(14:58)` 为 True。
5. **`IT-P1-006-R1` 已确认**：`eastmoney.py:331` `min(max_pages, required)` 静默截断，而 `:352` `complete` 只看「有总数且无失败页」→ 截断仍报 complete。
6. **`IT-P2-OBS-004` 已确认**：异常轮（`live_session.py:858`）仍取 store「最近一轮」的 observation（`:873-875`）写入本轮（`:891`），且 sample 无 `round_id`/`observed_at`（`:142` 只有 `index`）。
7. **其余仍在**：`IT-P2-OBS-002`（被时间准入拒绝的 provider-returned 票同时进入 `unknown_missing` 与 future/ooo reject，语义双计——本轮已在 §2.2 用真实探针复现）、`IT-P2-OBS-003`（`:904` 混算 eligible 与 idx_admitted）、`IT-P1-CAPABILITY-002`（`rules/base.py:43` 单一 capabilities）、`IT-P1-WINDOW-001`（`window()` 只读 change-only history）、`IT-P1-LIMIT-001`（无 `at_limit_unqualified` 状态）、`IT-P1-SOURCE-EMPTY-001`、`IT-P1-008/009/003`。
8. **已修且无回归**：`IT-P0-003`、`IT-P0-002-R1` 主路径、`IT-P1-006` 主体、`IT-P1-007`（均已实测）。

## 工具提示（本轮新增）

`pyproject.toml` 设 `addopts="-q"`；若命令行再加 `-q`，pytest **汇总行会被吞掉**（只打印点阵，看不到 `N passed`）。取真实计数须用 `-o addopts=""`。上轮若因此误判「无法取得通过数」，可据此重跑。

## 上一份完整报告（云端轮）

[`2026-09-20_08-10-29_JST.md`](./2026-09-20_08-10-29_JST.md) — 该轮自报「无本地 checkout、未跑全量 pytest」；其 provider-specialist routing 的 PR AUC（base `0.6454` / capmask `0.6577` / specialist `0.6646` / universal `0.6373`）**脚本与逐行预测均未入库**（`predictions.csv` 标 `not_saved`），**无法复算**，本轮不转述、不背书。

## 下一轮必须核查

1. `IT-P0-002-R2` 的显式水位线重构（不得再借用 `history[-1][0]`），以及**平价观测**的独立可失败断言；
2. `source_epoch`/`time_role` 契约：`provider_time=False` 时 `ts` 只允许同源排序，跨源比较须显式失败；
3. Observation Ledger v2 的互斥桶 + 机械恒等式对账（`requested == hit + evaluated_no_hit + unavailable + rejected_quality + future + ooo + unknown_missing`）与失败轮 identity；
4. targeted fallback 前是否已有 per-code provenance/capability；
5. 14:57 收盘集合竞价、`ObservationInterval`、limit qualified 状态、Eastmoney 截断显式判 incomplete；
6. 真实多日数据若挂载，只跑固定三 HGB 做 time-forward/provider/unseen-stock；无真实数据就维持 blocked，不重复 synthetic 冒充实盘进展；
7. 研究运行必须保存 row-level `predictions.csv` 与完整 `RUN_MANIFEST`。

## 前轮线索

上一份**本机**完整报告：[`2026-09-20_05-30-48_JST.md`](./2026-09-20_05-30-48_JST.md)，产品提交同为 `a88094e915340b4eaec534af2826b5ffef68fe0e`，该轮实测 `1155 passed / 0 failed`；本轮独立复验一致。

## 历史完整审计

- [2026-09-20 08:10 JST](2026-09-20_08-10-29_JST.md) — 云端轮（无 checkout）
- [2026-09-20 05:30 JST](2026-09-20_05-30-48_JST.md) — IT-P0-002-R2 首次发现
- [2026-09-20 05:16 JST](2026-09-20_05-16-58_JST.md)
- [2026-09-20 04:05 JST](2026-09-20_04-05-31_JST.md)
- [2026-09-20 02:41 JST](2026-09-20_02-41-38_JST.md)
