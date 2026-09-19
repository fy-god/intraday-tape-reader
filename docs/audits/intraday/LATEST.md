# 最新审计

最新完整报告：[`2026-09-19_17-59-54_JST.md`](./2026-09-19_17-59-54_JST.md)

## 版本与本轮性质

- 仓库与分支：`fy-god/intraday-tape-reader` / `main`。
- 审计时间：2026-09-19 17:59 JST。
- `reviewed_source_sha`：`f695102f8794b0b235d5f6ebf24564520d663ad8`（网页版 16:06 JST 报告所审源码）。
- 上轮产品修复基线：`e6ed35204cc82b9e072a9a27a2e8d6ad3acab089`。
- 本轮开始 HEAD：`b04dd3e`（含网页版 16:06 JST 审计报告与索引提交）。
- 本轮报告提交：`34778204dee41575697357f5d2e20a40aad3bc9d`。

本轮**有产品源码修复**：读取网页版 GPT Pro 提交的 16:06 JST 审计报告，复现并修复
其中的 `IT-P0-003`（累积缓存被包装成本轮 Snapshot），新增 4 项回归测试并做回退验牙。
全量 1068 passed / 0 failed。

## 本轮已修复

- `IT-P0-003`：`Engine.poll_once()` 的规则 Snapshot 改从**本轮实际返回**的观测构造，
  不再遍历累计缓存 `state.quotes`。修复前，provider 少返回的代码会带旧价旧量进入规则，
  使 `SpiritOrderRule._drop_stale()` 看不到缺席、`_check_trades()` 把 180 秒真实缺口
  洗成 5 秒，长缺口安全阀失效。

## 继续开放（本轮未复现，维持风险等级）

- `IT-P0-002`：窗口缺 `<= now` 上界、provider 时间未做写前质量门控。
- `IT-P0-001`：14:57-15:00 应按收盘集合竞价而非连续竞价处理。
- `IT-P1-SOURCE-EMPTY-001`：三源缺 requested-vs-returned 完整性合同。
- `IT-P1-WINDOW-001`：change-only 压缩导致横盘后跳变算不出。
- `IT-P1-LIMIT-001`：封单跨越门槛时状态先写 `sealed` 吞掉首次合格封板事件。
- `IT-P2-OBS-001`：`tools/live_session.py` 用累计 cache key 数冒充每轮覆盖。
- `IT-P1-008`（SSE 重连补账）、`IT-P1-009`（慢客户端静默 drop-oldest）、
  `IT-P1-003`（series 主体容量）。

已修并保留回归：`IT-P1-006`、`IT-P1-007`（分路由故障转移记账 + 拒绝部分池覆盖完整池）。

## 下一轮必须检查的产物

1. 按 R2 把 180s 缺口负控制迁入 `tests/test_full_day_simulation.py`（该文件至今 `not_run`）。
2. `IT-P0-002`：窗口上界 + 真实未来点/乱序数据复现。
3. `IT-P1-SOURCE-EMPTY-001`：单源 requested/returned 计数日志，观察真实缺失率。

没有独立真实标注时 Precision/Recall 继续标为不可用；未执行的测试明确 `not_run`，
不能用旧归档结果冒充本轮验证。

## 前轮线索

上一份完整报告：[`2026-09-19_16-06-05_JST.md`](./2026-09-19_16-06-05_JST.md)，
其报告提交 `4e2228e4510ecd0c2ddf646d874020b7a90fef88`，
索引提交 `b04dd3e`（=`e37ed7c` 之后的索引）。

后续报告仍只在本目录新增 Markdown 报告并更新本索引；报告提交不计作产品源码升级，
不修改产品代码以外的部署、Actions 或 PR。
