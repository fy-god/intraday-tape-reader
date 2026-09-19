# 最新审计

最新完整报告：[`2026-09-20_02-41-38_JST.md`](./2026-09-20_02-41-38_JST.md)

## 版本与本轮性质

- 仓库与分支：`fy-god/intraday-tape-reader` / `main`。
- 审计时间：2026-09-20 02:41 JST（本地 01:41 CST）。
- 本轮开始 HEAD：`003ecca08a96cb2b961dd1c9496b7d3c8c9bc0ad`。
- 上游审计 `reviewed_source_sha`：`34778204dee41575697357f5d2e20a40aad3bc9d`。
- 本轮性质：**本地 checkout 真实执行** —— 所有结论都在本机 `D:\ccc\ashare-radar`
  上用真实代码复现，非沙箱推演。
- 本轮未启动任何真实通知、长期采集、交易或付费训练。

## 本轮最重要：测试的前提本身被破坏过

`3477820`（上一个产品提交）把 **UTF-8 BOM** 提交进了 `pyproject.toml`，
`tomllib` 不接受 BOM，于是 README 记录的命令直接失败：

```text
$ python -m pytest -q
ERROR: D:\ccc\ashare-radar\pyproject.toml: Invalid statement (at line 1, column 1)
exit code: 4
```

一个测试都没收集。**这意味着 `3477820` 之后，"测试通过"无法按文档命令复现。**
严重性高于任何单个规则缺陷。

## 本轮已修复（4 项，全部带回归测试与回退验牙）

| 编号 | 级别 | 一句话 |
|---|---|---|
| `IT-P0-004` | P0 | `pyproject.toml` 带 BOM，`pytest` 无法启动（全仓测试被废） |
| `IT-P0-005` | P0 | `src/arad/__init__.py` 带 BOM |
| `IT-P1-CAPABILITY-001` | P1 | Sina 的 `turnover=0.0` 占位值被当真实零，放量规则静默失效 |
| `IT-P0-002` | P0 | provider 事件时间无写前准入 + `window()` 缺上界 + 乱序倒挂 |

### 一个方法论发现

用 pytest 守卫 BOM 是**自相矛盾**的 —— 回退验牙时测试没有变红，而是
**根本没能启动**。因此追加了不经 pytest 的独立守卫 `tools/check_bom.py`，
它的验牙是真实有效的（写回 BOM 后 exit 1，而此时 pytest 已死）。

### 一个自己踩过并纠正的坑

`IT-P1-CAPABILITY-001` 的第一版修法我改成了"缺 turnover 就跳过门槛"，
复现脚本立刻显示误报率被悄悄改变。上游报告 §3.4 明确禁止第一阶段这么做。
已回退为**只记账、不改判定**：行为与改动前逐字一致，新增的只是把
"Sina 期间放量规则不可评估"从静默变为可见。

## 已修复并保留回归

- `IT-P0-003`（产品提交 `3477820`）：规则 Snapshot 只消费本轮 `returned`。
- `IT-P1-006` / `IT-P1-007`：主体完整性保护、分路由故障转移记账。

## 继续开放

- `IT-P0-001`：`session.py` 把 14:57–15:00 归入连续竞价（应为收盘集合竞价）。
- `IT-P1-SOURCE-EMPTY-001`：指定代码抓取缺 requested-vs-returned 合同。
- `IT-P1-WINDOW-001`：change-only history 删除"横盘但持续新鲜"的窗口 anchor。
- `IT-P1-LIMIT-001`：封单状态机吞告警风险。
- `IT-P1-006-R1`：Eastmoney `max_pages` 超限仍标 `complete=True`。
- `IT-P1-008/009/003/004`：SSE 补账、慢客户端 drop-oldest、series 容量、
  Tencent TLS fallback。
- `IT-P2-OBS-001`：`live_session.py` 缺本轮 current/capability coverage。
  **本轮只交付 sidecar 结构，尚未接进 `live_session.py`**（属下一轮 WP08）。
- `per-route idx` 仍未分账。

## 研究推进

`blocked_no_mounted_multiday_continuous_corpus`。真实多日连续 A 股语料未挂载，
因此**没有**产出训练类产物（`dataset_manifest.json` 等）。本轮不重复 synthetic
TCN/粗筛，也不把 synthetic 数字包装成实盘结果。研究侧真实增量在工程契约：
`SourceCapabilities` 与 `RoundObservationSet` 已落地为可测试代码。

## 测试与门禁（真实执行）

```text
python -m pytest                 -> 1114 passed / 0 failed  (70s)
                                   基线 1068；本轮新增 46 项
tools/check_*.py                 -> 全部 exit 0（含新增 check_bom.py）
node tools/dash_render_check.js  -> exit 0
python tools/run_daemon.py       -> 崩溃自拉实测 PASS
```

## 顺带补齐：项目自运行能力

项目此前无法无人值守运行（`arad` 未装进环境、无 `python -m arad` 入口、
无监督进程）。本轮新增 `src/arad/__main__.py` 与 `tools/run_daemon.py`
（免配置启动 + 崩溃自拉 + 单实例锁）。实测：未设 `PYTHONPATH` 起服务，
强杀子进程后 12 秒自动拉起（PID 2956 → 30628），看板恢复。

## 下一轮优先核查

1. `RoundObservationSet` 接进 `live_session.py`（WP08 验收：failover 时整体
   健康但 `volume_burst` capability coverage 必须下降且原因明确）；
2. WP03：35 因子 capability mask 接真实 `Quote` 字段，blocked 用 mask/NaN；
3. `IT-P0-001` 收盘集合竞价语义；
4. `IT-P1-SOURCE-EMPTY-001` requested-vs-returned 合同；
5. 真实多日语料 manifest（即使不足 30×20 也要交付）；
6. `IT-P1-WINDOW-001` 的 `ObservationInterval`。
