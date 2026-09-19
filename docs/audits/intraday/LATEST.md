# 最新审计

最新完整报告：[`2026-09-19_16-06-05_JST.md`](./2026-09-19_16-06-05_JST.md)

## 版本与本轮性质

- 仓库与分支：`fy-god/intraday-tape-reader` / `main`。
- 审计时间：2026-09-19 16:06:05 JST。
- `reviewed_source_sha`：`f695102f8794b0b235d5f6ebf24564520d663ad8`。
- 最后产品修复基线：`e6ed35204cc82b9e072a9a27a2e8d6ad3acab089`。
- 本轮开始 HEAD：`e37ed7c4990050b011465b84af51d965b5f6f24a`；从 `f695102...` 到该 HEAD 只有审计文档提交，没有 `src/`、`tests/`、`config/`、`tools/` 产品变化。
- 本轮报告提交：[`4e2228e4510ecd0c2ddf646d874020b7a90fef88`](https://github.com/fy-god/intraday-tape-reader/commit/4e2228e4510ecd0c2ddf646d874020b7a90fef88)。

本轮是**无新增产品源码的深化审计**，重点把“provider soft-partial → 累积缓存冒充 current Snapshot → `spirit_order` 长缺口被洗短 → soak 仍可能显示满覆盖”贯通到同一份 Round Observation Contract。当前 PR 列表为空。本轮没有全量仓库 pytest、真实浏览器断线测试或新实盘运行；机制级离线探针 5/5 表示问题控制流被复现，不是产品修复验收。

## 本轮新增/深化问题

- `IT-P0-003`：规则 Snapshot 仍从累计 `state.quotes` 构造，缺失代码可继续作为“当前”进入规则；同时会让 `spirit_order` 的 stale-cache 清理失效，并把真实长 gap 洗短。
- `IT-P0-002`：窗口缺少 `<= now` 上界，provider 时间也没有形成写前质量门控；未来点/乱序仍是开放问题。
- `IT-P1-SOURCE-EMPTY-001`：扩大为 **soft-partial success**——腾讯/新浪/东财指定代码快照缺少 requested-vs-returned 完整性合同，无异常的少行结果会被当成功。
- `IT-P2-OBS-001`：本轮新增。`tools/live_session.py` 用累计 cache key 数与 universe 配置规模做代理，不能证明每轮当前观测覆盖。
- `IT-P1-WINDOW-001`：继续开放。本轮候选收敛为“有效观测区间压缩”，同时保留横盘 freshness 证据和真实 gap。
- `IT-P1-LIMIT-001`：本轮新增。价格一直在限价、封单从低于门槛变为高于门槛时，状态先写 `sealed` 会吞掉第二轮首次合格封板事件。
- `IT-P0-001`：官方资料复核后继续开放；股票 14:57-15:00 应按收盘集合竞价而非连续竞价处理。

继续开放但本轮不重复展开：`IT-P1-008`（SSE 重连补账）、`IT-P1-009`（慢客户端满队列静默 drop-oldest）、`IT-P1-003`（series 主体容量）。继续保留并回归已修 `IT-P1-007` 分路由故障转移记账。

## 主改造与优先实验

主改造：**Round Observation Contract v1**。将 `latest_cache`、`round_observations` 与 `admitted_current` 分离；新增 BatchResult/ObservationDecision/RoundObservationSet，显式记录 requested、returned、explicit_unavailable、unknown_missing、source epoch、provider/receive/process 时间；规则只消费 admitted current，soak 按 admitted/requested 报覆盖。

优先实验：`EXP-IT-OBS-001 v2`——同一固定输入比较 current change-only、save-all admitted oracle 与 interval-compressed admitted。固定矩阵包含 quiet、横盘后跳变、真断线、重复、乱序、future point、source switch、午休和跨日。候选若无限前填真实断线、吞同秒合法修订、跨 source epoch 做累计差分或真实仓库成本不可接受，则淘汰。

## 下一轮必须检查的产物

按报告 WP1→WP9 接续，优先要求本地 agent 回传：

1. 真实仓库类上的红测/负控制：soft-partial、stale current Snapshot、180s gap laundering、future point、横盘 anchor、封单 100→300 万、14:57 边界；
2. 三源 requested/returned/missing 分类与 source epoch；
3. Engine `latest_cache` / `admitted_current` 分离后的事件 diff；
4. 固定 fixture/replay/config hash；
5. old/new feature 与 event 对账；
6. server committed/sent/client received/applied 对账；
7. 同机器、同负载的 p50/p95/p99、CPU/RSS 与覆盖表；
8. 验收失败时的 trace 与回滚说明。

没有独立真实标注时 Precision/Recall 继续标为不可用；未执行的测试明确 `not_run`，不能用旧归档结果冒充本轮验证。

## 前轮线索

上一份完整报告：[`2026-09-19_04-04-37_JST.md`](./2026-09-19_04-04-37_JST.md)，其报告提交 `fb941cf1d068e24739dad6412867338cab19f4c2`，后续索引提交 `e37ed7c4990050b011465b84af51d965b5f6f24a`。

后续报告仍只在本目录新增 Markdown 报告并更新本索引；报告提交不计作产品源码升级，不修改产品代码、测试、配置、README、CHANGELOG、Actions 或 PR。
