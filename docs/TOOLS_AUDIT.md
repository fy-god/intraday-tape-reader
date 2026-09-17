# `tools/` 脚本审计报告 · TOOLS_AUDIT

**审计对象**：`tools/` 下全部 **21** 个脚本（20 个 `.py` + 1 个 `.js`）
**审计起点**：`HEAD = 76eaba6`，工作树干净（`git status` 无输出）
**审计终点**：`HEAD = f50b012`（另一 agent 的修复提交，见附录 A），工作树仅多出本报告
**环境**：Windows · Python 3.13.12 · Node v25.9.0 · Playwright + Chromium 已装
**基线**：`python -m pytest -q` → **1026 passed**（审计开始时）；
审计结束时为 **1029 passed**（连跑 2 次稳定；期间撞见 1 次既有并发 flake，见「需要修」第 3 条）

> ⚠️ **审计过程中仓库被另一个 agent 并发修改**（非本审计所为）。
> 详细时间线与影响见文末「附录 A」。本报告对每个脚本都给出
> **审计当时（`76eaba6`）** 与 **审计结束时（`f50b012`）** 两种状态。

---

## 后续处理（审计之后）

本报告「需要修的」4 条已全部处理完，改动在 `f50b012` 之后：

| # | 条目 | 处理 |
|---|---|---|
| 1 | `probe_sources.py` 会**静默覆盖** `fixtures/raw/` 测试基线 | **已修**：新增 `write_fixture()`，默认拒绝覆盖已存在文件并给出解释，需显式 `--force`；6 处裸 `write_*` 全部改走它，并补上 argparse。已实测：默认模式拒绝写入且文件字节不变，`--force` 仍可用 |
| 2 | `shot_spirit.py --help` 崩溃（唯一没有 argparse 的脚本） | **已修**：包成 `main()` + argparse，同时保留裸位置参数用法（`shot_spirit.py 60` 仍可用）。四条路径实测：`--help`→0、裸参数→0、`--minutes`→0、非法值→2 带中文提示 |
| 3 | `test_chunking_1300_codes_into_3_batches` 并发竞态 | **已修**：`fetcher.urls` 是**线程完成顺序**，多 worker 下本就不等于提交顺序。断言改为顺序无关（`sorted(sizes) == [100, 600, 600]`），并**加强**为「三批不重不漏、并起来正好 1300 只且前缀正确」。8 线程饱和下重复 **40 次：0 失败**（原先约 1/25 复现） |
| 4 | `check_audit_shots.py` 判据不是尺度不变的 | **已修**：`top_ratio < 0.92` 换成数 **ink rows**（有内容的行数）+ 墨迹占比。已用 4 个合成用例实测：纯色/全白/少色图仍被拦下，「长图但内容只在顶部」（即旧判据的误报场景）正常放行 |

第 3 条**不是本审计引入的**，缺陷在 `76eaba6` 就存在（原报告已核实 `f50b012` 未改动该文件），
只是当时「全绿」的负载没触发它。四条修完后基线为 **1029 passed**。

---

## 结论速览

**21 个脚本全部能被 Python / Node 解析，20/21 能跑通到成功结束。**

真正的坏消息只有一个：**`probe_sources.py`**。它不仅在当前网络下直接崩，
而且**再跑一次会破坏被 git 跟踪的测试夹具**，进而打掉 1026 项基线。

另有 2 个脚本**非致命但确实有问题**：`shot_spirit.py`
（21 个里唯一没有 argparse 的，`--help` 会崩）、
`check_audit_shots.py`（空白图判据在长列表上必然误报）。
外加一个不在 `tools/`、但同样威胁基线的既有测试竞态（见「需要修」第 3 条）。

| 状态 | 数量 | 脚本 |
|---|---|---|
| **BROKEN** | 1 | `probe_sources.py` |
| **BROWSER** | 3 | `audit_dashboard_visual.py`, `check_colors.py`, `shot_browser.py` |
| **LONG** | 1 | `live_session.py` |
| **NETWORK** | 4 | `probe_index_codes.py`, `probe_index_live.py`, `probe_live_ready.py`, `probe_spirit_fields.py` |
| **OK** | 12 | `bench_round.py`, `check_audit_shots.py`, `check_config_wiring.py`, `check_orphan_config.py`, `check_spirit_mapping.py`, `dash_render_check.js`, `probe_nan_safety.py`, `probe_session_boundaries.py`, `shot_dashboard.py`, `shot_index.py`, `shot_spirit.py`, `verify_tencent_fields.py` |

**语法解析**：20/20 `.py` 全部 `ast.parse` 通过；`dash_render_check.js` `node --check` 通过。
**进口标识符**：70 个 `from arad...` 导入名**全部存在**（`arad.server.web` 等 6 处
`from arad.server import web` 是子模块导入，非缺失）。
**没有任何脚本引用已改名/已删除的函数、配置键或文件。**

---

## 状态表

按 状态 → 名称 排序。所有脚本都以 `D:\ccc\ashare-radar` 为工作目录运行。

| 脚本 | 状态 | 做什么 | 实测结果与备注 |
|---|---|---|---|
| `probe_sources.py` | **BROKEN** | 探数据源连通性，把原始响应落盘到 `fixtures/raw/` | **exit 1**：`RemoteDisconnected: Remote end closed connection without response` → `universe probe failed - aborting`。**且会破坏被跟踪的夹具**，见下节 |
| `audit_dashboard_visual.py` | BROWSER | 真 Chromium 视觉/交互审计，68 项 ✓/✗ + 截图 | **exit 0，68/68 通过**（23.1s）。审计当时为 67 项 / 10 项失败，见附录 A |
| `check_colors.py` | BROWSER | 真浏览器核对红涨绿跌（含最易搞反的「打开涨停」） | **exit 0**（2.7s），10 个信号配色全对。端口正常释放 |
| `shot_browser.py` | BROWSER | 起真服务 + 回放 + Chromium 断言 + 截图 | **exit 0**（3.9s），11 项断言全过。**会覆盖 `tools/spirit_dashboard.png`** |
| `live_session.py` | LONG | 真引擎跑真行情 N 分钟 soak，给健康结论 | `--minutes 1` → **exit 0**（93.1s，12 轮）。默认 `--minutes 10` 会超 4 分钟，必须缩短 |
| `probe_index_codes.py` | NETWORK | 指数代码/前缀冲突（`sh000001` vs `sz000001`） | **exit 0**（1.2s）。实时值：`sh000001=上证指数 3891.60`、`sz000001=平安银行 11.70` |
| `probe_index_live.py` | NETWORK | 同一 `000001` 带前缀 vs 裸码的区别 | **exit 0**（0.3s）。识别指数 5/6，结论「前缀生效」 |
| `probe_live_ready.py` | NETWORK | 盘中可用性总检：股票池 + 抓取 + 3 个 spirit 模块 | **exit 0**（25.7s）。东财被限流后**自动退到新浪**，拿到 5563 只（18.3s） |
| `probe_spirit_fields.py` | NETWORK | 验腾讯源盘口/内外盘字段真实可用 | **exit 0**（0.3s）。6 只票内外盘一致性偏差 ≤0.004% |
| `bench_round.py` | OK | 单轮耗时基准（`--offline` 纯 CPU / `--live` 端到端） | **两种模式都 exit 0**。`--offline` 0.4s；`--live --repeat 5` 32.7s → p50 1069ms，余量 4.7× |
| `check_audit_shots.py` | OK | 校验审计截图不是空白图（纯 stdlib 解 PNG） | 当前 **exit 0**「21 张截图都是真实渲染内容」。**但启发式会误报**，见「需要修」 |
| `check_config_wiring.py` | OK | spirit 模块与配置/引擎接线完整性 | **exit 0**（0.2s）。7 模块、18/18/12 键全在 YAML、`max_per_round=0` 保持不限量 |
| `check_orphan_config.py` | OK | 找「写了但代码从不读」的配置键 | **exit 0**（0.1s）。115 个叶子键 115 个命中，无孤立键 |
| `check_spirit_mapping.py` | OK | 规则产出的 pattern → 展示层是否都认识 | **exit 0**（0.2s）。14 个信号全部映射，缺失：无 |
| `dash_render_check.js` | OK | Node 跑看板真实 JS，验渲染/去重/DOM 上限 | **exit 0**（0.1s）。17 项断言 + 1 行小结全过（含 `打开涨停=绿`、`MAX_SPIRIT=200`）。**被 `tests/test_dashboard_frontend.py` 直接调用** |
| `probe_nan_safety.py` | OK | NaN/Inf 不会让 `/api/spirit` 或 SSE 吐非法 JSON | **exit 0**（3.6s）。严格模式 `json.loads` 通过，SSE 存活 |
| `probe_session_boundaries.py` | OK | 假时钟走开盘/午休/收盘边界 | **exit 0**（0.2s）。16 个时刻全对（左闭右开） |
| `shot_dashboard.py` | OK | 真服务 + 真回放 + 真 HTTP 打 3 个接口 | **exit 0**（5.6s）。接口/SSE/坏数据韧性全过，末行「服务已关闭」 |
| `shot_index.py` | OK | 指数管道 → 拉升指数 → 中文播报全链路 | **exit 0**（21.8s）。2 条 `index_pull` 告警，端到端正常 |
| `shot_spirit.py` | OK | 看短线精灵播报效果（真实回放） | **exit 0**（0.3s，`python tools\shot_spirit.py 5`）。**无 argparse，`--help` 会崩**，见「需要修」 |
| `verify_tencent_fields.py` | OK | 算术核对腾讯字段索引契约 | **exit 0**（0.1s）。422/422 一致，5 个索引 100% 吻合。**离线**（读夹具，无网络） |

---

## 需要修的

### 1. `probe_sources.py` — BROKEN（且会破坏基线）

**现状**：从本机直接崩溃，一个字节都没探测到。

```
[FAIL] eastmoney clist universe (full market)   103.9 ms  RemoteDisconnected: Remote end closed connection without response
universe probe failed - aborting
exit code: 1
```

**一行原因**：`main()` 把「东财股票池」当硬前置，`if not uni: return 1`（第 112–114 行），
而东财对高频来源限流是**本项目已知的常态**（README「东财限流是常态，不是故障」）。

**这不是「代码写错」，是「把预期失败当致命错误」**。我逐条单独验证了它探测的 6 个端点：

| 端点 | 结果 |
|---|---|
| `push2.eastmoney.com` clist 股票池 | ✗ `RemoteDisconnected`（被限流） |
| `qt.gtimg.cn` 腾讯批量 | ✓ 120.6 ms |
| `push2.eastmoney.com` ulist.np | ✓ 96.3 ms |
| `hq.sinajs.cn` 新浪批量 | ✓ 93.5 ms |
| `push2his.eastmoney.com` trends2 | ✓ 146.3 ms |
| `vip.stock.finance.sina.com.cn` 股票池（兜底） | ✓ 190.3 ms |

**6 个里 5 个是好的**，脚本却因为第 1 个失败而整体放弃 —— 而系统其它部分
（`live_session.py` / `probe_live_ready.py` / `bench_round.py --live`）都能靠新浪兜底跑通。

#### ⚠️ 更危险：重跑会静默破坏 1026 项基线

`probe_sources.py` 会**原地重写 6 个被 git 跟踪的夹具**：

```
fixtures/raw/eastmoney_clist_p1.txt      ← 第 125 行 .write_bytes()
fixtures/raw/tencent_bulk_sample.txt      ← 第 140 行 .write_text(...)[:200000]
fixtures/raw/eastmoney_ulist_5.txt       ← 第 159 行
fixtures/raw/sina_bulk_5.txt             ← 第 178 行
fixtures/raw/eastmoney_trends2_600000.txt← 第 196 行
fixtures/raw/universe_sample.json        ← 第 210 行
```

实测（**只读模拟，未改写任何文件**）：当前夹具是 **424 行**，而
`write_text("\n".join(bodies)[:200000])` 的切片产出 **421 行**。

```
current fixture: bytes=203555  chars(gbk-decoded)=201242
SIMULATED re-run output (chars=200000): lines = 421
tests assert 424 lines total (423 valid + 1 malformed)
```

`tests/test_sources_tencent.py` 硬断言：

```python
VALID_ROWS = 423
MALFORMED_ROWS = 1
assert len(lines) == VALID_ROWS + MALFORMED_ROWS   # 424
assert len(pf_quotes) == VALID_ROWS
```

且 `tests/test_sources_eastmoney.py` / `test_sources_sina.py` 各有一大批断言
依赖这些夹具的**采集时点**（例如 `sz000001` 夹具价 `11.85`，今天是 `11.70`）。
**这些脚本记录的「真实响应」是测试的事实基准，重抓即失效。**

> 夹具文件当前 mtime 仍是 `2026/9/15 0:38`，**未被破坏**；`git status` 对 `fixtures/` 无输出。
> 我在审计中**没有执行** `probe_sources.py` 的写盘路径（它在写盘前就 abort 了）。

**建议**（不在本次范围内，仅记录）：把东财失败降级为 warning 继续跑；
夹具写入改为显式 `--refresh-fixtures` opt-in，默认只读。

---

### 2. `shot_spirit.py` — `--help` 崩溃（无 argparse）

```
File "tools\shot_spirit.py", line 21, in <module>
  minutes = int(sys.argv[1]) if len(sys.argv) > 1 else 45
ValueError: invalid literal for int() with base 10: '--help'
exit code: 1
```

**一行原因**：脚本用 `sys.argv[1]` 裸取位置参数，没有 argparse。
**影响有限**：它文档化的用法（`python tools/shot_spirit.py 60`）是好用的，
纯粹传 `--help` 才会炸。它是 21 个脚本里**唯一**没有 argparse 的 Python 脚本。
对照：`audit_dashboard_visual.py` / `bench_round.py` / `live_session.py` / `shot_browser.py`
四个有 argparse 的脚本 `--help` 全部 `exit 0` 正常。

---

### 3. `tests/test_sources_tencent.py::test_chunking_1300_codes_into_3_batches` — 并发竞态（**非 tools/ 脚本，但审计中发现**）

**不属于 `tools/`，是审计过程中撞见的既有 flake**，因直接威胁「1026 基线」故一并记录。

全量 `pytest` 期间出现过一次失败：

```
FAILED tests/test_sources_tencent.py::test_chunking_1300_codes_into_3_batches
1 failed, 1028 passed in 54.58s
```

**复现**：空闲时单跑 60/60 通过；用 8 个 CPU 占满后台负载后跑 25 次，
**复现 1 次**，拿到确切断言：

```
E   assert [600, 100, 600] == [600, 600, 100]
D:\ccc\ashare-radar\tests\test_sources_tencent.py:408: assert [600, 100, 600] == [600, 600, 100]
```

**一行原因**：该测试用 `TencentSource({"bulk_chunk": 600, "workers": 4})`，
源码走 `ThreadPoolExecutor`（`src/arad/sources/tencent.py:628-632`），
而断言按**下标顺序**检查 `fetcher.urls` 的尺寸：

```python
sizes = [len(u.split("=", 1)[1].split(",")) for u in fetcher.urls]
assert sizes == [600, 600, 100]
```

`RecordingFetcher` 在锁内 `append`，所以 `calls` 的顺序 = **线程实际完成的顺序**，
在多 worker 下本来就不保证等于提交顺序。负载高时线程调度错位即失败。

**性质**：与并发修复提交 `f50b012` **无关** —— 该 commit 没有改这个文件
（`git diff 76eaba6 f50b012 -- tests/test_sources_tencent.py` 为空），
缺陷在 HEAD `76eaba6` 就存在，只是「1026 全绿」时没被负载触发。
**这不是我引入的，也不是 tools/ 脚本的问题；修法是把断言排序（`sorted(sizes)`）
或按 chunk 内容而非完成顺序比对。**

### 4. `check_audit_shots.py` — 启发式会误报真实截图（**非确定性缺陷**）

审计当时（HEAD 截图）它 `exit 1`：

```
03_spirit_rows_zoom.png   420x4604   1168   94.0%  ✗ 疑似空白/纯色
✗ 1 张截图可疑：['03_spirit_rows_zoom.png']
```

**但这张图不是空白。** 我用独立解码器复核（`measure.py`，与脚本同款 stdlib PNG 解码）：

```
03_spirit_rows_zoom.png: 420x4604
  colours=1168        ← 远超「色数 < 10 = 纯色」的判据
  ink px=53594 (2.77%)
  text bands=18       ← 18 条真实文字行（y=0..708）
  last row with ink y=708 (of 4604)
```

**一行原因**：`top_ratio < 0.92` 不是尺度不变的判据。
`#spiritList` 是滚动容器，局部截图会连**全部可滚动内容**一起截下来，
而文字只占顶部一小段 —— 一张 420×4604 的图里 81% 高度是合法空白，
主色占比自然冲到 94%。

**当前**它 `exit 0`（「21 张截图都是真实渲染内容」），但那是因为并发修复把行高
从 46.5px 降到 23.2px、图高从 4604px 缩到 2303px、主色占比从 94.0% 掉到 85.0% ——
**是运气变好，不是判据变对**。同一个判据在「列表很长、内容很短」时必然再次误报。

---

## 冗余

| 组 | 脚本 | 重叠内容 |
|---|---|---|
| **指数前缀** | `probe_index_codes.py` + `probe_index_live.py` | 都在验「`000001` 裸码 ≠ `sh000001`」。前者裸 `urllib` + `guess_prefix`，后者走 `TencentSource` + `looks_like_index`。结论完全一致 |
| **腾讯字段** | `probe_spirit_fields.py` + `verify_tencent_fields.py` | 都在核腾讯字段索引。前者**实时**抓 6 只票看内外盘/五档，后者**离线**用夹具做 422 行算术核对。互补但目的重合 |
| **看板截图** | `shot_browser.py` + `shot_dashboard.py` + `shot_index.py` + `shot_spirit.py` | 四个都是「起服务 + 灌回放 + 断言/截图」。差别只在断言深浅与是否需要浏览器 |
| **方向配色** | `check_colors.py` ⊂ `audit_dashboard_visual.py` | `audit_dashboard_visual.py` 的 docstring 自己写明「`check_colors.py` —— 只查方向配色」，并被它第 3 组完整覆盖（12 个信号 vs check_colors 的 10 个） |
| **前端渲染** | `dash_render_check.js` ⊂ `audit_dashboard_visual.py` | 前者 Node 验渲染逻辑（快、无需浏览器），后者验真浏览器最终效果。docstring 明确分工，属**有意保留**的重叠 |

**可以放心合并/删除的**：`probe_index_codes.py`、`probe_index_live.py` 二者留一；
`check_colors.py`（已被 68 项审计完全覆盖）。

---

## README 漂移

### 审计当时（HEAD）：**完全没有工具表**

`README.md` 里 `tools/` 只被提到 **2 次**，都是正文散文，没有任何表格：

- 第 98 行：`（复现：python tools\bench_round.py --live）`
- 第 195 行：``tools/bench_round.py`` 测全市场规模的真实耗时；`tools/live_session.py` 做限时…

→ **21 个脚本里 19 个在 README 中零提及**（只有 `bench_round.py`、`live_session.py` 出现过）。
用户照 README 走，根本不知道有 `check_*.py` 这些能救命的自检脚本。

### 审计结束时：README 被并发补上了「工具清单」表（第 200–235 行）

逐条核对现状：

| 检查项 | 结果 |
|---|---|
| 表里点名的文件名是否存在 | ✅ 全部存在（21 个文件名 + `settings.yaml` 全部 `Test-Path` 通过） |
| 有无脚本**没进表** | ⚠️ **`probe_spirit_fields.py` 没进表**（21 个里唯一漏的） |
| 表里有无点不存在的文件 | ✅ 无 |
| `audit_dashboard_visual.py`「68 项」 | ✅ 实测 `共 68 项：通过 68，未通过 0` |
| `python -m pytest -q  # 1029 项` | ✅ 实测 `1029 passed`（审计开始时是 1026） |
| `probe_sources.py` 归入「联网」组 | ⚠️ 归组没错，但**没提它会 abort、会改写夹具** |
| `verify_tencent_fields.py` 归入「**联网**」组 | ❌ **分类错误**：它只读 `fixtures/raw/tencent_bulk_sample.txt`，**零网络调用**，应归「离线自检」 |
| 末尾「`probe_*` 只做观测…`check_*` 会给出断言式 ✓/✗」 | ✅ 与实测一致 |

**建议补的两处**：① 把 `probe_spirit_fields.py` 加进联网表；
② 把 `verify_tencent_fields.py` 从联网表挪到离线表。

---

## 未文档化的副作用

`.gitignore` 覆盖 `data/`、`*.jsonl`、`*.log`、`tools/audit_shots/`、`tools/*.png`
（第 23/24/29–31 行）。注意 **`fixtures/` 不在 `.gitignore` 里 —— 它是被跟踪的测试基准**，
这正是下面 `probe_sources.py` 那一行危险的原因。

| 脚本 | 写什么 | 文档有提吗 |
|---|---|---|
| `probe_sources.py` | **`fixtures/raw/` 6 个文件 —— 但这些是 git 跟踪的测试夹具！** | ❌ docstring 只说“Records RAW payloads”，没说会覆盖既有基准 |
| `bench_round.py` | `data/bench_round_<时间戳>.json` | ❌ docstring 未提 |
| `live_session.py` | `data/live_session_<时间戳>.json` | ❌ docstring 未提（只提退出码语义） |
| `shot_browser.py` | `tools/spirit_dashboard.png`（覆盖） | ⚠️ 仅末尾打印路径，docstring 未提 |
| `audit_dashboard_visual.py` | `<--shots>/….png`（默认 `tools/audit_shots/`，21 张） | ✅ docstring 明确写了 `--shots` |
| `probe_nan_safety.py` / `check_colors.py` / `shot_dashboard.py` / `shot_browser.py` / `live_session.py` | 绑定临时端口 | ✅ 都在 `finally` 里关；实测全部释放 |
| `shot_index.py` / `shot_spirit.py` / `dash_render_check.js` / `check_*.py` | 无写盘 | — |

**唯一真正有破坏性的**是 `probe_sources.py`：它写的是**被跟踪的测试基准**，
而不是自己的输出目录。其余都只写 `data/` 或 `.gitignore` 覆盖的图片。

---

## 建议保留清单

**核心保留（12 个）—— 覆盖离线自检 + 真浏览器 + 联网三条线，且都实测通过：**

| 脚本 | 为什么留 |
|---|---|
| `check_spirit_mapping.py` | 33 信号展示层映射，防「静默降级成兜底名」 |
| `check_config_wiring.py` | 防「配置改了不生效」这一本项目踩过两次的坑 |
| `check_orphan_config.py` | 防「写了但没人读」的配置键 |
| `probe_session_boundaries.py` | 开盘/午休/收盘边界，错了会「开盘十分钟没告警」 |
| `dash_render_check.js` | 唯一无需浏览器、且**被 pytest 直接调用**的前端渲染测试 |
| `probe_nan_safety.py` | 防「看板突然不动了」且服务端无报错的历史事故 |
| `shot_dashboard.py` | 后端契约（HTTP/SSE/坏数据韧性），最便宜的全链路验证 |
| `audit_dashboard_visual.py` | 68 项视觉/交互审计，覆盖最广，实测全绿 |
| `check_colors.py` | 红涨绿跌（含两个最易搞反的信号）；被审计覆盖但跑得快 |
| `shot_browser.py` | 真浏览器冒烟 + 截图，最接近用户观感 |
| `bench_round.py` | 回答「5 秒轮询够不够」，README 引用它 |
| `live_session.py` | 唯一回答「连续跑会不会烂掉」的 soak 工具 |

**建议修复后保留（1 个）**：

- `probe_sources.py` —— 价值在于采集夹具，但**必须先加 `--refresh-fixtures` opt-in**，
  否则默认路径会静默打掉基线。

**可以安全删除（冗余）**：

- `probe_index_codes.py` 或 `probe_index_live.py`（留一即可，结论完全重合）
- `check_colors.py`（已被 `audit_dashboard_visual.py` 第 3 组完全覆盖；
  若想保留一个 2.7 秒的快速冒烟，则删掉审计里的配色组——二选一）
- `shot_spirit.py`（纯打印观感，无断言；与其他 shot_* 重叠，且是唯一没有 argparse 的）

**建议保留但需注意**：

- `shot_index.py` —— 21.8s 且依赖网络，但它是**唯一**端到端验指数管道的工具；
  另有 `tests/test_index_plumbing.py`（29 个用例）覆盖同一路径，可考虑降级为按需运行
- `check_audit_shots.py` —— 只在「需要核验截图证据」时有意义，
  且必须先修 `top_ratio` 判据（改成按墨迹分布，而非全局主色占比）

---

## 附录 A · 并发修改时间线

本审计**只读** `tools/`、`tests/`、`src/`，**没有修改任何脚本、测试或源文件**
（唯一新建的仓库内文件是本报告 `docs/TOOLS_AUDIT.md`）。但审计期间工作树被
**另一个 agent** 改动，为免混淆，记录如下：

| 时间 | 文件 | 变化 | 谁 |
|---|---|---|---|
| 审计开始 | — | `git status` 干净，HEAD `76eaba6`，pytest **1026 passed** | — |
| 07:58 | `tools/spirit_dashboard.png` | 我的 `shot_browser.py` 运行按脚本设计覆盖（gitignored） | **本审计** |
| 08:05:52 | `src/arad/server/dashboard.html` | +14/−1：加 `@media(max-width:1140px)` 两栏降级、`.sp` 网格 5 列 → **6 列** | 外部 agent |
| 08:10:48 | `tools/audit_dashboard_visual.py` | 1241 → 1262 行：把「6 个格子 y 必须相等」改成按**行高**判定 | 外部 agent |
| ~08:17 | `tools/audit_shots/*.png` | 21 张截图重新生成（行高 46.5 → 23.2px） | 外部 agent |
| ~08:18 | `tests/test_dashboard_frontend.py` | 新增 3 个用例（列数/筛选换行/对比度） | 外部 agent |
| ~08:19 | `README.md` | +49/−11：新增「工具清单」三张表、1026 → **1029** | 外部 agent |
| **08:23** | **commit `f50b012`** | 上述改动全部提交（工作树重新变干净） | 外部 agent |
| 08:25 | `docs/TOOLS_AUDIT.md` | 本报告（唯一新文件） | **本审计** |

**外部修改对我的结论的影响**：

- `audit_dashboard_visual.py`：审计当时 **67 项 / 10 项失败**（其中 3 项是真实产品缺陷：
  6 格子挤成两行、1024px 横向溢出 186px、时间列对比度 3.53:1 低于 WCAG AA）；
  并发修复后 **68 项 / 0 项失败**。**该脚本本身自始至终是对的**，
  它准确报出了真实缺陷 —— 这恰好是它作为审计工具的价值证明。
- `check_audit_shots.py`：从 `exit 1`（误报）变为 `exit 0`，原因是截图变矮了，
  **判据缺陷依然存在**。
- 基线：1026 → **1029 passed**（新增 3 个测试，无删除、无失败）。

**最终验证**（在 `f50b012` 上）：

```
python -m pytest        →  1029 passed in 56.38s
python -m pytest        →  1029 passed in 61.50s
git status --porcelain  →  ?? docs/TOOLS_AUDIT.md      （仅本报告）
```

工作树中 `tools/`、`tests/`、`src/` **相对 `f50b012` 无任何改动**（那些改动已由该
commit 收编，不是本审计所为）。

---

## 附录 B · 审计方法

每个脚本都以相同流程过一遍（全部真实执行，非静态推断）：

1. **解析**：`.py` → `python -c "import ast; ast.parse(open(p,encoding='utf-8-sig').read())"`；
   `.js` → `node --check`。
2. **进口核对**：AST 抽出每个 `from arad... import NAME`，逐个 `hasattr` 校验（70 个名字）。
   同时把所有字符串字面量里的仓库相对路径做存在性检查。
3. **`--help`**：对全部 20 个 `.py` 无差别调用，记录 exit code。
4. **实跑**：`D:\ccc\ashare-radar` 下执行，UTF-8 输出 + 秒级计时落盘到 `D:\ccc\audit_ws\logs\`。
5. **端口/进程**：每个起服务的脚本跑完后立刻查 `Get-NetTCPConnection -State Listen`
   与 `Get-Process python,chrome`，确认端口释放、无残留。
   **全部 5 个起服务的脚本（`probe_nan_safety` / `check_colors` / `shot_dashboard` /
   `shot_browser` / `live_session`）都正常释放了端口。**
6. **超时**：单脚本上限 4 分钟。`live_session.py` 用 `--minutes 1` 限时（93.1s）。
   所有脚本均未触发 kill。
7. **截图核验**：自写 stdlib PNG 解码器（`D:\ccc\audit_ws\measure.py`）统计色数、
   墨迹像素、文字带位置，独立复核 `check_audit_shots.py` 的判定。

**网络约束**：`push2.eastmoney.com` 的 **clist** 接口从本机被 IP 限流
（`RemoteDisconnected`，约 95–105ms 快速失败），其余 5 个端点全部正常。
这与 README「东财限流是常态，不是故障」一致，**不计为脚本缺陷**。

**遗留进程**：审计结束后无任何本审计产生的 `python` / `chrome` / Playwright 进程，
无用例端口被占用（唯一监听中的 `python` 属外部 `D:\xm` 服务的 8765 端口，与本项目无关）。
