# 长跑内存与配置接线审计

本文回答一个盘中系统最基本、也最容易含糊过去的问题：**让它跑一整天，会怎样？**

上一个证据是 12 分钟的 soak（`README.md` 的「盘中可用吗」一节）。12 分钟能证明
"数据链路是通的、延迟有富余"，但**证明不了内存有界** —— 一个每轮泄漏几 KB 的
程序，12 分钟只涨几 MB，看起来完全正常，下午两点才 OOM。

所以这里直接把"一整天"压进十几秒跑完，并且把结论**拆成可复现的数字**。

跑法：

```powershell
$env:PYTHONPATH='src'; $env:PYTHONIOENCODING='utf-8'
python tools\probe_memory_day.py
```

---

## 1. 结论速览

| 问题 | 结论 |
|---|---|
| 内存会泄漏吗？ | **不会**。两个场景都收敛（`+0.00 MB`） |
| 现实负载下一整天多少内存？ | **约 44 MB**（起始 22.6 → 43.6 MB） |
| 最坏情况（全市场都记分时）？ | **约 412 MB**，且到达上限后不再增长 |
| 增长在哪里停止？ | `series` 每条 `deque` 的 `maxlen` 填满时 |
| 有配置键被静默忽略吗？ | **有 1 个**：`storage.series_len`（已修，见第 4 节） |

---

## 2. 为什么必须先搞清楚"引擎到底喂什么给 record_tick"

这是本次审计里最重要的一步，因为它决定了测试是不是在测一个**不存在的负载**。

`AlertStore.record_tick` 会为每只股票开一条分时 `deque`（`maxlen=series_len`，
默认 240 点）。最朴素的想法是"每轮把全市场 5563 只都喂进去"——**但引擎并不是
这样做的**。`engine.py` 里写得很清楚：

```python
# 分时序列（只记自选 + 异动榜，控制内存）
track = {c: self.state.quotes[c] for c in self.watchlist if c in self.state.quotes}
for a in fresh:
    if a.code in self.state.quotes:
        track[a.code] = self.state.quotes[a.code]
```

所以 `_series` 的键集合是**自选股 ∪ 当天出现过异动的票**，量级是"几百到两千多"，
而不是 5563。

> 第一版探针就是按全市场喂的，量出 412 MB，差点把这个数字当成"系统的内存占用"
> 写进文档。所以最终版本**两个场景都测，并把差别摆出来** —— 场景 B 是上界，
> 场景 A 才是现实。用错的那一个会得出错误 10 倍的结论。

---

## 3. 实测数据

### 场景 A：现实负载（分时只记「自选股 ∪ 当日异动票」）

模拟一整天里累计 2400 只不同个股出现异动（按盘中实测外推：12 分钟 118 条告警
→ 一整天约 2400 条，且分散在不同个股上）：

```text
      轮   RSS(MB)        增量   _series  _alerts
      1      22.6      +0.0         8        0
     60      26.6      +4.1       480       18
    120      31.3      +8.7       960       36
    180      35.5     +12.9      1440       54
    240      39.6     +17.0      1920       72
    300      43.6     +21.0      2400       90
耗时 0.4s，_series 键数 = 2,400
再跑 1 轮后增量: +0.00 MB  ✓ 收敛（无泄漏）
```

**一整天约 44 MB**，增长与"见过的股票数"成正比（每只约 9 KB），**与轮数无关**。

### 场景 B：最坏上界（假设全市场 5563 只全部记分时）

```text
      轮   RSS(MB)        增量   _series  _series_codes  _alerts
      1      44.4      +3.3      5563            120        0
    120     221.6    +180.5      5563            120       36
    240     411.8    +370.7      5563            120       72
    270     411.8    +370.7      5563            120       81
    300     411.8    +370.7      5563            120       90
5,563 只 × 300 轮 = 1,668,900 次 record_tick，耗时 14.2s
再跑 1 轮后增量: +0.00 MB  ✓ 收敛（无泄漏）
```

注意 **240 轮之后 RSS 完全不再变**（411.8 平了 60 轮）—— 因为 240 = `series_len`，
每条 `deque` 到 240 点就停止增长。这正是"有界"该有的形状：
**线性涨到上限，然后平掉**，而不是持续爬升。

---

## 4. 顺带发现并修掉的真缺陷：`storage.series_len` 被静默忽略

审计过程中注意到 `AlertStore.__init__` 的 `series_len` 是**硬编码默认值 240**，
它不会自己去读 settings；而 engine 里的构造是：

```python
self.store = AlertStore(self.settings, calendar=self.calendar)   # 少传一个参数
```

于是 `config/settings.yaml` 与 `config.py` 里都声明了的 `storage.series_len`
**改了完全不生效，也不报任何错**。

为什么这个键重要：它是**内存开关**。上面场景 B 说明"全市场都记分时"要 412 MB，
而调小 `series_len` 正是降内存最直接的手段（240 → 60 大约省 3/4）。
一个"改了没反应"的内存开关会让人以为已经生效，然后在长跑里被咬到。

已修（`engine.py` 显式传参），并加了回归测试
`tests/test_config_wiring.py::test_storage_series_len_actually_reaches_the_store`。
已验证该测试能抓到缺陷（把参数去掉后它报
`storage.series_len=7 没传到 AlertStore，实际是 240（配置被静默忽略）`）。

### 为什么 `check_orphan_config.py` 没抓到它

那个检查只确认"键名**作为字符串字面量**在 `src/` 里出现过"，而
`"series_len"` 确实出现在 `store.py` 的 `DEFAULTS` 里 —— 于是判定通过。

**"名字存在"不等于"接线正确"**：这个键既被声明、又被读取（读的是默认值），
只是那个值从来没被传下去。这类"声明完整、读取正常、但值是默认值"的失效，
静态找键名是抓不到的，只能靠"改一个值看它有没有生效"的行为测试。

---

## 4b. 顺着这条线查下去：又找到 5 个死键

既然"名字存在 ≠ 接线正确"，就写了个反向检查
`tools/check_config_consumed.py`：它**先把 `config.py` 里 `DEFAULTS` 那段
声明挖掉**，再看每个键在剩余源码里有没有真正的读取点
（`.get("...")` / `["..."]` / `setdefault(...)`）。

> 第一版它是**整个排除 `config.py`** 的，结果两头不讨好：
> 对 `series_len` 抓不到（因为 `store.py` 的 DEFAULTS 里也有这个名字），
> 又对 `holidays_file` 误报（因为合法的读取点恰好就在 `config.py` 里）。
> 改成"只挖声明点、保留读取点"之后才两者都对。
> 这个教训值得记：**检查器自己也要被检查** —— 两次都用"把代码改坏再跑"
> 的方式验过它到底能不能抓到。

结果找出 5 个键，逐个用"写哨兵值看行为变不变"确认过：

| 键 | 症状 | 处理 |
|---|---|---|
| `session.holidays_file` | `load_holidays()` 支持传路径，但**所有调用点都不传参** | **接线**（见下） |
| `session.warmup_seconds` | "开盘预热"功能**从未实现**，src/ 零引用 | 删除 |
| `session.record_auction` | "集合竞价记快照"功能**从未实现** | 删除 |
| `storage.snapshot_every` | "原始快照落盘"功能**从未实现**（无对应 notifier） | 删除 |
| `storage.snapshot_path` | 同上 | 删除 |

**为什么"删除"也是正确答案**：一个改了不生效、又不报错的键，比没有这个键更糟
—— 它让人以为功能存在。留着"以后可能要做"的键，代价是每个读配置的人都要
重新判断一次它到底通不通。真要做这些功能时，加回来**并同时接线**即可。

`holidays_file` 则值得接线，因为它对应的功能**已经存在**（`config/holidays.txt`
真的被读了 33 个休市日），只是路径写死、无法指到别处；而它的注释写着
"每年需更新"，正是最该能改的键。已修：`load_holidays(path, settings=)` 读配置，
`TradingCalendar.load(settings=)` 透传。测试用**行为**验证（指向不存在的文件
必须让节假日集合变空），因为这类 bug 的特征恰恰是"读得到、但没用上"。

顺带把 `storage.series_len` 的注释补成它真实的身份（内存开关），
并指向本文。

---

## 5. 其余内存相关结论

| 结构 | 上界机制 | 实测 |
|---|---|---|
| `AlertStore._alerts` | `deque(maxlen=max_alerts)`，默认 300 | 90 条后不再增长 ✓ |
| `AlertStore._series`（每只） | `deque(maxlen=series_len)`，默认 240 | 240 轮后 RSS 走平 ✓ |
| `AlertStore._series`（键数） | **无显式上界**，只随"见过的股票数"增长 | 全市场 5563 封顶 ✓ |
| `AlertStore._series_codes` | `deque(maxlen=120)` | 恒为 120 ✓ |
| `AlertStore._notes` | 手动 `del self._notes[:-20]` | 恒 ≤ 20 ✓ |
| `EngineState.history`（每只） | `deque(maxlen=history_len)`，默认 360 | 喂 1000 点后恒为 360 ✓ |
| `EngineState.history`（键数） | `state.prune(keep)`，**条件触发** | 见下 |

### `EngineState.history` 的裁剪是条件触发的，已单独验证

单只历史由 `deque(maxlen=history_len)` 管住，但**键数**靠 `engine.py` 里这段：

```python
if len(self.state.history) > len(keep) * 2:
    self.state.prune(keep)
```

"有裁剪代码"和"裁剪真的会发生"是两回事 —— 如果条件恒假，这段就形同虚设。
所以单独写了 `tools/probe_history_bound.py` 验证：

```text
history_len 上限 = 360
喂 1000 点后，单只历史长度 = 360（应 == maxlen 360）
累计 2000 只股票 -> history 键数 = 2,000
prune(keep=100 只) -> 裁掉 1,900 只，剩余 100
✓ state.history 单只受 maxlen 约束、键数靠 prune 收敛
```

顺带确认一处**容易踩的细节**（代码里已有注释说明）：`keep` 里必须包含
`index_codes`。指数会被 `exclude_boards` 过滤掉、因而不进 `eligible`，
如果 `keep` 只由 `eligible ∪ watchlist` 组成，`prune` 会把刚攒起来的指数窗口
清空 —— 表现是"拉升指数/打压指数永远等不到样本"，且只在触发 prune 之后才出现，
属于很难复现的那类 bug。

---

## 6. 这台机器的实测环境

- Python 3.13，Windows
- `rss_mb()` 用 `psapi.GetProcessMemoryInfo` 读工作集。
  ⚠ 两个坑都踩过：该函数在 `psapi.dll` 而非 `kernel32`；且**必须声明
  `argtypes`/`restype`**，否则 64 位下 `HANDLE` 被按 32 位传、返回值被截断，
  调用静默失败返回 0（表现为"内存 0.0 MB"这种明显假数据）。
- 探针里量不出来时返回 `nan` 而不是 0 —— 返回 0 会让"增量"算出假的负数，
  把"测不到"伪装成"内存下降了"。
