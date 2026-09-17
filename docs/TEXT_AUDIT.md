# 告警文案审计（用户可见文本缺陷）

审计对象：`D:\ccc\ashare-radar`，7 个规则模块 + 展示层（`spirit.py` / `notifiers/` / `server/`）。
审计日期：本轮会话。基线：`python -m pytest -q` → **1032 passed，exit 0**（未改动任何源码）。

审计口径：**只看用户能看到的字**（`Alert.title` / `Alert.detail` / `one_line()` / 看板悬停提示 / 配置注释），
以及这些字与 `metrics`、与源码算术是否自洽。判定「错」的标准有两条，缺一不可：

1. 打印出来的文本与它自己声称的算法/单位不符（含 10^n 量级错误、正负号错误、口径张冠李戴）；
2. 同一屏上另一处文本或 `metrics` 给出了与之矛盾的数，用户无法判断该信哪个。

每条缺陷都附**实跑输出**与**手算核对**。所有探针脚本在 `%TEMP%\arad_audit\p*.py`，可原样复跑。

---

## 1. 结论速览

| 编号 | 位置 | 一句话 | 严重度 |
|---|---|---|---|
| **D1** | `rules/spirit_price.py:358,370` | 「快速反弹 / 高台跳水」标题印的是**窗口涨跌幅**，而分级用的是**信号强度**；`sev=3` 的急信号标题可能显示 `+0.00%` 甚至带负号 | 高 |
| **D2** | `rules/limit_board.py:195,199,213` | 一字跌停被标成「**非一字**」；跌停侧的一字板识别是硬编码 `return False` | 高 |
| **D3** | `rules/tick_surge.py:260` | 「**急跌**」告警的详情里印「**距涨停** +13.40%」；跌停方向没有任何距离字段 | 中 |
| **D4** | `rules/unusual.py:322,326` | 「快速回封」写「**近 10 分钟内**炸板回落（最低 X 元）」，X 取的是**当日**最低价，可能在窗口之外几小时 | 中 |
| **D5** | `rules/limit_board.py:331` | 「**炸板**」告警的详情里印「买一**封单** 126万」——板已炸开，此时不存在封单 | 中 |
| **D6** | `rules/spirit_index.py:273` | 详情第 2 行硬编码「**5 分钟**」，窗口配成别的值时与同一行的 `_fmt_s(seconds)`（第 272 行）自相矛盾 | 中 |
| **D7** | `spirit.py:81,87,110,111,112` + `dashboard.html:908` | 注册表里 5 个 `group='limit'` 信号**永远不会出现**，看板仍悬停显示「涨跌停 **11** 个信号」 | 中 |
| **D8** | `spirit.py:93,95` | 指数信号悬停提示硬编码「**5 分钟**内」，窗口可配 | 低 |
| **D9** | `spirit.py:68,70` | 「机构吃货/吐货」提示写「成交**单**巨大」（单笔口径），实现只有两快照区间增量 | 低 |
| **D10** | `dashboard.html:833` | 悬停 `extra` 原样拼 `键 + 值`，无单位：`amount 102000000` | 低 |
| **D11** | `config/settings.yaml:233` + `rules/spirit_price.py:65` | 注释称 `rebound_off_low_pct`「等价于 rebound_pct（保留兼容）」，该键**从未被读取** | 低 |

合计 **11 条**确认缺陷。其中 D1 是本轮最严重的一条：它会让**最高级别（会弹窗）**的告警在标题上写着「+0.00%」或一个负数。

---

## 2. 确认的缺陷

### D1 「快速反弹 / 高台跳水」标题数字与信号强度不是同一个量

**位置**：`src/arad/rules/spirit_price.py:358`、`:370`（配合 `:355` 的分级逻辑）

```python
# :355  severity 用 change（信号强度）判定
if thr > 0 and abs(change) >= thr * self.urgent_multiple:
    severity = 3

# :358  标题却改用 actual（窗口涨跌幅）
actual = change if window_pct is None else window_pct
...
# :370
title=f"{cn} {fmt_pct(actual)}",
```

`rebound` 传 `change=off_low`（自低点拉起幅度）、`window_pct=change窗口`；
`dive` 传 `change=-fall`（自峰值回落幅度）、`window_pct=change窗口`（见 `:234-238`、`:274-278`）。
于是**分级看 `signal_pct`、标题看 `window_pct`，两个数在语义上就不是一回事**。

**实跑输出**（`p9_rebound_worst.py`，现价涨回窗口起点）：

```
### 最坏情形 1：快速反弹 —— 现价涨回窗口起点   条数=1
  severity=3   kind=surge
  title  : 快速反弹 +0.00%
  detail1: 快速反弹 · 现价 10.50  涨跌 +5.00%  窗口 3分钟 +0.00%
  detail2: 窗口内先跌 -4.76%（低点 10.00）后自低点拉 +5.00%
  metrics: {'pattern': 'rebound', 'window_pct': 0.0, 'signal_pct': 5.0, ...}
  看板悬停 extra : {'amount': 105000000.0, 'turnover': 3.0, 'window_pct': 0.0, 'amplitude': 5.5}
  注：signal_pct 不在 extra 白名单里 -> 用户永远看不到真正的信号强度
```

**手算**：

```
窗口涨幅 = (10.50 / 10.50 - 1) * 100 = 0.00%     <- 标题印这个
自低点拉起 = (10.50 / 10.00 - 1) * 100 = +5.00%  <- 真正的信号强度
severity: thr = rocket_pct = 2.0, urgent_multiple = 2.0
          abs(5.00) >= 2.0 * 2.0 = 4.0  ->  sev 3（会弹窗）
```

一条 **sev 3（会弹窗）** 的告警，标题写「快速反弹 **+0.00%**」。

同一根因还有三种更糟的呈现：

```
### 最坏情形 2：快速反弹 —— 标题为负（与「反弹」语义相反）
  title  : 快速反弹 -0.98%
  metrics: {'window_pct': -0.98, 'signal_pct': 2.02, ...}
手算：窗口 (10.10/10.20-1)*100 = -0.98%；自低点 (10.10/9.90-1)*100 = +2.02%

### 最坏情形 3：高台跳水 —— sev 3 却只显示 -0.50%
  severity=3
  title  : 高台跳水 -0.50%
  detail2: 窗口内先涨 +6.00%（高点 10.60）后跳水 -6.53%
  metrics: {'window_pct': -0.5, 'signal_pct': -6.533, ...}
手算：窗口 (9.95/10.00-1)*100 = -0.50%；自峰值 (10.60/9.95-1)*100 = +6.53%
     abs(6.533) >= 2.0*2.0 -> sev 3

### 最坏情形 4：高台跳水 —— 标题为正数（与「跳水」语义相反）
  kind=plunge
  title  : 高台跳水 +2.00%
  detail2: 窗口内先涨 +5.00%（高点 10.50）后跳水 -2.94%
  metrics: {'window_pct': 2.0, 'signal_pct': -2.941, ...}
```

**为什么这是缺陷而不是「设计如此」**：源码 `:344-348` 的 docstring 自己解释了为什么要分开两个量 ——
「后两者的窗口涨跌幅可能很小甚至反向，用它分级会永远评不上『紧急』」。
即作者**已经知道**窗口涨跌幅对这两个形态没有意义，分级绕开了它，**但标题没绕开**。
`detail2` 里其实印了正确数字（`后自低点拉 +2.02%`），所以用户在**同一屏**上能看到
标题 `-0.98%` 与正文 `+2.02%` 两个互相矛盾的数，且标题是列表行里唯一显眼的字。

**影响面**：`rocket` / `accel_down` 的 `window_pct == signal_pct`（探针已核，见第 3 节），
所以缺陷**只落在 `rebound` 与 `dive` 上**——恰好是最需要「先跌后拉 / 先涨后跌」判断的两种形态。

**建议改法**（未实施）：

```diff
-        actual = change if window_pct is None else window_pct
+        # 标题用**信号强度**（与 severity 同源），窗口涨跌幅放 detail
+        actual = change
...
-            f"{cn} · 现价 {q.price:.2f}  涨跌 {fmt_pct(q.pct)}  "
-            f"窗口 {self._fmt_s(seconds)} {fmt_pct(actual)}",
+            f"{cn} · 现价 {q.price:.2f}  涨跌 {fmt_pct(q.pct)}  "
+            f"信号 {fmt_pct(change)}"
+            + (f"  窗口 {self._fmt_s(seconds)} {fmt_pct(window_pct)}"
+               if window_pct is not None and abs(window_pct - change) > 1e-9 else ""),
...
             metrics={
                 "pattern": pattern,
                 "window_seconds": float(seconds),
-                "window_pct": round(actual, 3),
+                "window_pct": round(window_pct if window_pct is not None else change, 3),
                 "signal_pct": round(change, 3),
```

（若不想改 `metrics["window_pct"]` 的对外含义，最低限度也应让 `title` 用 `change`，
并把 `signal_pct` 加进 `spirit.to_feed_item` 的 `extra` 白名单，让悬停能看到真正的强度。）

---

### D2 一字跌停被标成「非一字」

**位置**：`src/arad/rules/limit_board.py:192-201`（判定）、`:213`（文案）

```python
# :192
def _is_one_word(self, q: Quote, limit: float, rising: bool) -> bool:
    if rising:
        return q.open > 0 and q.open >= limit - self.tol and abs(q.high - q.low) < 1e-9
    return False                      # <- 跌停侧永远 False

# :197
def _is_first_board(self, q: Quote, limit: float, rising: bool) -> bool:
    if not rising:
        return False                  # <- 跌停侧永远 False

# :213
board_txt = "一字板" if one_word else ("首板" if first_board else "非一字")
```

两个判定在跌停侧都短路成 `False`，于是 `board_txt` **必然**落到 `"非一字"`。

**实跑输出**（`p6_labels.py`）：

```
### 一字跌停（开=高=低=跌停价 9.0，全天没有价格波动）   条数=1
  title : 封跌停 封单2700万
  detail1: 现价 9.00  涨跌 -10.00%  跌停价 9.00
  detail2: 封单2700万  非一字  换手 0.10%  成交额 0.01亿       <-- 「非一字」
  detail3: 开盘 9.00（-10.00%）  最高 9.00  最低 9.00  振幅 0.00%
  metrics: {'one_word_board': 0.0, 'is_first_board': 0.0, ...}
```

**手算/事实核对**：一字跌停的定义是「开盘即跌停价，且全天最高 = 最低 = 跌停价」。
这里 `open == high == low == limit_down == 9.00`，振幅 `0.00%` —— 是标准的一字跌停。
`detail3` 自己把 `开盘 9.00（-10.00%）最高 9.00 最低 9.00 振幅 0.00%` 全印出来了，
**用户在下一行就能看到它是一字板，而上一行刚说「非一字」**。

对照组（探针同批跑出，说明只有跌停侧坏）：

```
### 一字涨停（对照组，应标「一字板」）
  detail2: 封单3300万  一字板  换手 0.10%  成交额 0.01亿        <-- 正确
  metrics: {'one_word_board': 1.0, ...}

### 半路封跌停（开 9.80 -> 砸到跌停 9.00）
  detail2: 封单2700万  非一字  换手 5.00%  成交额 0.45亿        <-- 这条「非一字」才对
```

**注意**：既有测试 `tests/test_rule_limit_board.py:666-674`
（`test_one_word_helper_is_disabled_for_limit_down`）把这个行为**当成契约钉住了**
（`assert rule._is_one_word(q, 9.0, False) is False`），所以改这里要连测试一起改。
测试名里的「is_disabled」说明作者是**有意**不做的 —— 但「有意不做识别」和
「不做识别却输出一个肯定的错误标签」是两件事：前者可接受，后者是缺陷。
不识别时应当**不输出**该字段，而不是输出「非一字」。

**建议改法**（未实施）：

```diff
-        board_txt = "一字板" if one_word else ("首板" if first_board else "非一字")
+        if not rising:
+            board_txt = "一字跌停" if self._is_one_word(q, limit, rising) else ""
+        else:
+            board_txt = "一字板" if one_word else ("首板" if first_board else "非一字")
...
-            f"{seal_txt}  {board_txt}  换手 {q.turnover:.2f}%  成交额 {q.amount / 1e8:.2f}亿",
+            "  ".join(p for p in (seal_txt, board_txt,
+                                  f"换手 {q.turnover:.2f}%",
+                                  f"成交额 {q.amount / 1e8:.2f}亿") if p),
```

（并让 `_is_one_word` 的跌停分支按镜像条件返回：`low >= limit - tol and abs(high - low) < 1e-9`。）

---

### D3 「急跌」告警里印「距涨停 +13.40%」

**位置**：`src/arad/rules/tick_surge.py:248-250`（计算）、`:260`（文案）

```python
# :248  只算到涨停的距离，没有跌停方向的对应量
to_limit = 0.0
if q.limit_up_price > 0 and q.price > 0:
    to_limit = (q.limit_up_price / q.price - 1.0) * 100.0

# :260  这一行对「急拉」和「急跌」用的是同一份 detail
f"距涨停 {to_limit:+.2f}%  最高 {q.high:.2f}  最低 {q.low:.2f}  开盘 {q.open:.2f}",
```

**实跑输出**（`p10_plunge.py`）：

```
### tick_surge 急跌（现价 9.70，60s -2.51% / 300s -4.90%）   条数=1
  kind=plunge  sev=2
  title : 急跌 -2.51% / 1分钟
  detail1: 现价 9.70  涨跌 -3.00%  窗口 1分钟 -2.51%
  detail2: 分时均价 10.20（现价在其下方）  量能比 数据不足
  detail3: 振幅 5.50%  换手 3.00%  成交额 1.02亿
  detail4: 距涨停 +13.40%  最高 10.20  最低 9.65  开盘 10.00     <-- 急跌却报「距涨停」
  metrics: {'window_pct': -2.513, 'to_limit_pct': 13.4, ...}
```

**手算**：`(11.00 / 9.70 - 1) * 100 = +13.40%`。数字本身没算错，但这是**跌停方向**的告警：

* 一只正在急跌的票，用户最需要的是「离跌停还有多远」（`(9.70/9.00-1)*100 = +7.78%`），
  而这个数**在整条告警里不存在**（`metrics` 里只有 `to_limit_pct`，只有涨停口径）；
* 反方向的 `距涨停 +13.40%` 带着 `+` 号出现在一条 `kind=plunge`、标题 `-2.51%` 的告警里，
  「+」在 A 股语境里读作「涨」，与告警方向相反。

对照「急拉」告警（同一探针，`p4_misc.py`）这一行是正确的、也是有用的：

```
### tick_surge 急拉（临近涨停）
  title : 急拉 +9.00% / 3分钟
  detail4: 距涨停 +0.92%  最高 10.90  最低 10.00  开盘 10.00
  metrics: {'to_limit_pct': 0.92, ...}
```

**建议改法**（未实施）：

```diff
         to_limit = 0.0
         if q.limit_up_price > 0 and q.price > 0:
             to_limit = (q.limit_up_price / q.price - 1.0) * 100.0
+        to_limit_dn = 0.0
+        if q.limit_down_price > 0 and q.price > 0:
+            to_limit_dn = (q.price / q.limit_down_price - 1.0) * 100.0
...
-            f"距涨停 {to_limit:+.2f}%  最高 {q.high:.2f}  最低 {q.low:.2f}  开盘 {q.open:.2f}",
+            (f"距涨停 {to_limit:+.2f}%" if kind is AlertKind.SURGE
+             else f"距跌停 {to_limit_dn:+.2f}%")
+            + f"  最高 {q.high:.2f}  最低 {q.low:.2f}  开盘 {q.open:.2f}",
```

---

### D4 「快速回封」把**当日**最低价说成「近 10 分钟内」的最低价

**位置**：`src/arad/rules/unusual.py:322`、`:326-327`

```python
# :322
gap = (limit - float(q.low)) / limit * 100.0
# :326
extra=f"近 {reseal_seconds / 60.0:.0f} 分钟内炸板回落（最低 {float(q.low):.2f} 元，"
      f"较涨停价 {gap:.2f}%）后重新封上涨停 {limit:.2f} 元",
```

`q.low` 是 `Quote.low` = **当日**最低价（`models.py:165` 附近的字段定义），
不是 `reseal_seconds` 窗口内的最低价。同一函数上面明明取了窗口数据
（`:296` `start = now_epoch - reseal_seconds`，`:301-318` 遍历 `rows` 做状态机），
却没有顺手算窗口内最低。

**实跑输出**（`p7_scope.py`）：构造「当日最低 10.30 出现在 09:35（约 5.4 小时前），
最近 600 秒内最低只有 10.94」：

```
### 实证输出   条数=1
  title : 快速回封
  detail2: 近 10 分钟内炸板回落（最低 10.30 元，较涨停价 6.36%）后重新封上涨停 11.00 元
  metrics: {'pattern': 'reseal', 'limit_up_price': 11.0, 'low': 10.3, ...}
```

**手算**：

```
文案声称的是「近 10 分钟内」：
  窗口内实际最低 = 10.94
  (11.00 - 10.94) / 11.00 * 100 = 0.55%     <- 窗口口径的真实回落幅度

文案印出来的是当日口径：
  (11.00 - 10.30) / 11.00 * 100 = 6.36%     <- 印出来的数
```

**差 11.6 倍**（6.36% vs 0.55%）。用户读到「近 10 分钟内炸板回落 6.36%」会以为
十分钟内有过一次深达 6.36% 的砸盘，实际上那发生在几小时前，最近的回落只有 0.55%。

**同一函数还有一处措辞问题**：`:326` 写「近 N 分钟内**炸板**回落」，
但 `:315` 的判定是 `if p <= floor: pulled_back = True`，`floor = limit * (1 - pullback_pct/100)`，
`pullback_pct` 默认 `0.5` —— 只要**跌离涨停价 0.5%** 就算「炸板」。
真正的炸板（`limit_board` 口径）默认要回落 `0.3%`（`break_retreat`），两者不一致，
但这条更像口径差异而非文案错误，故不单列为缺陷。

**建议改法**（未实施）：

```diff
-        gap = (limit - float(q.low)) / limit * 100.0
+        win_low = min((float(p[1]) for p in rows
+                       if start <= float(p[0]) <= now_epoch and float(p[1]) > 0.0),
+                      default=float(q.low))
+        gap = (limit - win_low) / limit * 100.0
         return self._mk(
             q, ctx, ts, bucket, "reseal", _SEV_RESEAL,
             title="快速回封",
-            extra=f"近 {reseal_seconds / 60.0:.0f} 分钟内炸板回落（最低 {float(q.low):.2f} 元，"
+            extra=f"近 {reseal_seconds / 60.0:.0f} 分钟内炸板回落（最低 {win_low:.2f} 元，"
                   f"较涨停价 {gap:.2f}%）后重新封上涨停 {limit:.2f} 元",
-            metrics={"limit_up_price": limit, "low": float(q.low)},
+            metrics={"limit_up_price": limit, "low": win_low},
         )
```

---

### D5 「炸板」告警里印「买一**封单** 126万」

**位置**：`src/arad/rules/limit_board.py:327`（计算）、`:331`（文案）

```python
# :327  直接复用「封单额」函数
seal_wan = self._seal_amount_wan(q, True)
# :331
f"换手 {q.turnover:.2f}%  成交额 {q.amount / 1e8:.2f}亿  买一封单 {seal_wan:.0f}万",
```

`_seal_amount_wan`（`:184-190`）算的是 `vol * 100.0 * q.price / 10000.0`，其中 `vol = q.bid_vol`（买一量）。
在 `_check_break` 里，触发前提是 `:318` `q.price <= limit * (1 - break_retreat / 100)`、
`:321` `q.price < limit - tol` —— **现价已经离涨停价很远了，板上根本没有封单**。
此时 `q.bid_vol` 只是普通买一挂单量，把它叫「封单」是指鹿为马。

**实跑输出**（`p7_scope.py`）：

```
### (b) 炸板文案里的「买一封单」
  title : 炸板 -4.55%
  detail1: 现价 10.50  涨跌 +5.00%  涨停价 11.00  回落 4.55%
  detail2: 最高 11.00  最低 10.30  振幅 7.00%
  detail3: 换手 5.00%  成交额 0.53亿  买一封单 126万      <-- 「封单」
  metrics: {'price': 10.5, 'limit_up': 11.0, 'seal_amount_wan': 126.0, ...}
```

**手算与前提核对**：

```
seal_wan = 1200 手 * 100 股 * 10.50 元 / 1e4 = 126.0 万元     <- 数字算对了
炸板触发前提：q.price <= 11.00 * (1 - 0.3/100) = 10.9670
              现价 10.50 远低于 10.9670  ->  板上无封单
```

对照真封板（同一探针）：

```
### (c) 对照组：真封板时「封单」措辞正确
  title : 封涨停 封单3300万
  detail2: 封单3300万  首板  换手 5.00%  成交额 0.55亿
手算：30000 手 * 100 股 * 11.00 元 / 1e4 = 33000 万元 = 3.30 亿   -> 正确
```

注意 `metrics["seal_amount_wan"]` 在炸板告警里**也是 126.0**（`:342`），
即这个错标不只影响文案，还影响下游把所有 `seal_amount_wan` 都当封单看的消费者
（`spirit.to_feed_item` 的 `extra` 白名单里就有它）。

**建议改法**（未实施）：

```diff
-            f"换手 {q.turnover:.2f}%  成交额 {q.amount / 1e8:.2f}亿  买一封单 {seal_wan:.0f}万",
+            f"换手 {q.turnover:.2f}%  成交额 {q.amount / 1e8:.2f}亿  买一挂单 {seal_wan:.0f}万",
```

（`metrics` 侧建议改名为 `bid1_amount_wan`，或干脆在炸板路径不写 `seal_amount_wan`。）

---

### D6 `spirit_index` 详情第 2 行硬编码「5 分钟」

**位置**：`src/arad/rules/spirit_index.py:270-274`

```python
detail = "\n".join([
    f"{q.name or q.code} · 现价 {q.price:,.2f}  {fmt_pct(change_pct)}"
    f"  窗口 {self._fmt_s(seconds)} {delta:+.2f}点",          # :272 用 _fmt_s，正确
    f"5 分钟{direction_cn} {abs(delta):.2f} 点（{abs(change_bp):.2f}bp），"   # :273 硬编码
    f"命中阈值：{hit_desc}",
```

第 272 行已经用 `self._fmt_s(seconds)` 正确渲染窗口，第 273 行**紧挨着**却写死「5 分钟」。

**实跑输出**（`p5b_spirit_index.py`，配置 `windows` 改成 60/90/120/600）：

```
  windows=[60]  _fmt_s(60) = '1分钟'
    title      : 拉升指数 +10.00点 (+0.26%)
    detail 行1 : 上证指数 · 现价 3,900.00  +0.26%  窗口 1分钟 +10.00点
    detail 行2 : 5 分钟拉升 10.00 点（25.71bp），命中阈值：0.5点 或 1.2bp
    metrics    : window_seconds=60.0

  windows=[120]  _fmt_s(120) = '2分钟'
    detail 行1 : 上证指数 · 现价 3,900.00  +0.26%  窗口 2分钟 +10.00点
    detail 行2 : 5 分钟拉升 10.00 点（25.71bp），命中阈值：0.5点 或 1.2bp
    metrics    : window_seconds=120.0

  windows=[600]  _fmt_s(600) = '10分钟'
    detail 行1 : 上证指数 · 现价 3,900.00  +0.26%  窗口 10分钟 +10.00点
    detail 行2 : 5 分钟拉升 10.00 点（25.71bp），命中阈值：0.5点 或 1.2bp
    metrics    : window_seconds=600.0
```

窗口 60 秒时，**相邻两行**分别写「窗口 1分钟」和「5 分钟拉升」——用户无法判断该信哪个。

**建议改法**（未实施）：

```diff
-            f"5 分钟{direction_cn} {abs(delta):.2f} 点（{abs(change_bp):.2f}bp），"
+            f"{self._fmt_s(seconds)}{direction_cn} {abs(delta):.2f} 点（{abs(change_bp):.2f}bp），"
```

---

### D7 注册表里 5 个「涨跌停」信号永远不会出现，看板仍宣称有 11 个

**位置**：`src/arad/spirit.py:79-88,110-112`（注册）、`src/arad/server/web.py:226-243`（计数）、
`src/arad/server/dashboard.html:908`（显示）

```javascript
// dashboard.html:908
if(g.count) b.title = b.textContent + " " + g.count + " 个信号";
```

**实跑输出**（`p11b_registry.py`，用驱动规则的方式逐个验证可达性）：

```
### 驱动结果：limit_board 实际产出的 pattern
  封涨停      pattern=limit_up_seal      title=封涨停 封单3300万
  炸板        pattern=open_limit_up      title=炸板 -4.55%
  触及涨停    pattern=limit_up_touch     title=触及涨停 +9.95%
  封跌停      pattern=limit_down_seal    title=封跌停 封单2700万

  实际产出 pattern 集合 = ['limit_down_seal', 'limit_up_seal', 'limit_up_touch', 'open_limit_up']

### spirit.SIGNALS 中 group='limit' 的可达性
  limit_up_seal      cn=封涨停板   dir=up    ✓ 规则直出
  limit_down_seal    cn=封跌停板   dir=down  ✓ 规则直出
  open_limit_up      cn=打开涨停   dir=down  ✓ 规则直出
  open_limit_down    cn=打开跌停   dir=up    ✗ 永不出现
  limit_up_touch     cn=触及涨停   dir=flat  ✓ 规则直出
  limit_down_touch   cn=触及跌停   dir=flat  ✗ 永不出现
  limit_up           cn=涨停      dir=up    ~ kind 兜底可出
  limit_down         cn=跌停      dir=down  ~ kind 兜底可出
  seal               cn=封板      dir=up    ✗ 永不出现
  break              cn=炸板      dir=down  ✗ 永不出现
  touch              cn=触板      dir=flat  ✗ 永不出现

### 看板筛选按钮的计数
  {'group': 'limit', 'cn': '涨跌停', 'count': 11}
  -> 悬停显示「涨跌停 11 个信号」
  -> 实际能出现 6 个，虚报 5 个
```

**根因**：`limit_board._check_side:154-156` 在跌停侧直接短路

```python
if not rising:
    self._state[tag] = "away"
    return None
```

所以 `_make_touch(rising=False)` 与 `_check_break` 在跌停侧**永远不可达**，
`limit_down_touch` / `open_limit_down` 永不产生。
另有 `seal` / `break` / `touch` 三个信号（`spirit.py:110-112`）属于早期命名，
现在规则改用 `limit_up_seal` / `open_limit_up` / `limit_up_touch`，这三个旧名已无产出点。

**为什么算用户可见缺陷**：这 5 个信号各自带 hint（`打开跌停`→「跌停封单被撬开（利好）」、
`触板`→「触及涨跌停价」等），用户在悬停提示的「信号 / 分组」两行里会读到它们的名字，
筛选按钮也宣称有 11 个；但无论行情怎么走，这 5 个永远不出现。
用户排查「为什么从来没见过『打开跌停』」会怀疑自己的配置或数据源。

**建议改法**（未实施）：给 `Signal` 加一个 `active: bool = True` 字段，
把 5 个不可达项标 `active=False`，`spirit_groups()` 计数时跳过，
或直接从 `SIGNALS` 删掉这 5 个（`limit_down_touch` 若将来实现跌停侧撬板再恢复）。

---

### D8 指数信号提示硬编码「5 分钟内」

**位置**：`src/arad/spirit.py:92-95`

```python
"index_pull": Signal("index_pull", "拉升指数", UP, "index",
                     "5 分钟内指数拉升超阈值"),
"index_press": Signal("index_press", "打压指数", DOWN, "index",
                      "5 分钟内指数打压超阈值"),
```

`hint` 会被 `to_feed_item`（`spirit.py:216`）送进前端，
`dashboard.html:826` 把它作为悬停提示的**第一行**。窗口可配（`spirit_index.DEFAULTS["windows"] = [300]`，
`config/settings.yaml:258` 也允许改），配成 60 秒时提示仍然写「5 分钟内」。

与 D6 同源（都是把默认窗口当常量写进文案），但位置不同、修法不同，故分列。

**建议改法**（未实施）：把 hint 里的时长去掉（「指数在扫描窗口内拉升超阈值」），
或让 hint 变成可格式化模板、由规则在产出时填 `_fmt_s(seconds)`。

---

### D9 「机构吃货 / 机构吐货」提示写「成交**单**巨大」（单笔口径）

**位置**：`src/arad/spirit.py:67-70`

```python
"institution_eat": Signal("institution_eat", "机构吃货", UP, "order",
                          "主动买入成交单巨大（≥50万股/100万元/0.1%流通盘）"),
"institution_vomit": Signal("institution_vomit", "机构吐货", DOWN, "order",
                            "主动卖出成交单巨大"),
```

「成交**单**」在盘口语境里指**单笔成交**。但实现在 `src/arad/rules/spirit_order.py:377-419`：

```python
"""成交类 4 个信号：用**相邻两轮快照的增量**近似本区间的主动买卖。"""
...
d_outer = outer - float(p_outer)
d_inner = inner - float(p_inner)
...
# 区间成交额按内外盘比例归因：主动买占 d_outer/d_act，主动卖占 d_inner/d_act。
# 注意这是**近似**：真实资金流还受撤单、大单拆分影响，见模块文档。
```

模块 docstring `:18-22` 自己写得很清楚：

> **最关键的诚实说明：本项目只有行情快照，没有逐笔成交明细。**
> 「大笔买入 / 机构吃货」这类信号在官方口径里是**逐笔**判定的，我们用**相邻两轮快照的增量**去近似……
> * 得到的粒度是「**一轮快照区间内的主动买/卖**」，不是严格意义的单笔；

**实跑输出**（`p6_labels.py`）：

```
### spirit.SIGNALS hint 的口径声明 vs 规则实现口径
  big_buy            hint='主动买盘（外盘）成交占流通盘 ≥0.1%'
  big_sell           hint='主动卖盘（内盘）成交占流通盘 ≥0.1%'
  institution_eat    hint='主动买入成交单巨大（≥50万股/100万元/0.1%流通盘）'
  institution_vomit  hint='主动卖出成交单巨大'
  institution_buy    hint='买队列出现大额挂单（≥50万股/100万元/0.25%流通盘）'
  institution_sell   hint='卖队列出现大额挂单'
```

**对比**：`big_buy` / `big_sell` 写的是「**成交**占流通盘」——没提「单」，
而实际产出的 `extra` 文案是「**本区间**主动买入 6,000 手」（`spirit_order.py:432`），
用的是诚实口径。真正自相矛盾的只有 eat/vomit 两个：**实现说「区间」，提示说「单」**。

另外注意 `spirit_order.py` 产出的 `extra` 一律用「**本区间**主动买入/卖出」，
即**已经在正确表述了** —— 只有 `spirit.py` 的 hint 这一个地方没跟上。

**建议改法**（未实施）：

```diff
 "institution_eat": Signal("institution_eat", "机构吃货", UP, "order",
-                          "主动买入成交单巨大（≥50万股/100万元/0.1%流通盘）"),
+                          "本区间主动买入巨大（≥50万股/100万元/0.1%流通盘；"
+                          "按相邻两轮快照增量近似，非逐笔）"),
 "institution_vomit": Signal("institution_vomit", "机构吐货", DOWN, "order",
-                            "主动卖出成交单巨大"),
+                            "本区间主动卖出巨大（按相邻两轮快照增量近似，非逐笔）"),
```

---

### D10 看板悬停 `extra` 原样拼「键 + 值」，没有单位

**位置**：`src/arad/server/dashboard.html:831-834`

```javascript
var ex = a.extra || {};
for(var k in ex){
  if(Object.prototype.hasOwnProperty.call(ex, k) && ex[k] != null) tip.push(k + " " + ex[k]);
}
```

键名与值的单位完全不做映射，直接拼。`extra` 的来源是 `spirit.py:223-226` 的白名单
`("amount", "volume_ratio", "turnover", "seal_amount_wan", "window_pct", "amplitude", "ratio_vs_float")`。

**实跑输出**（`p6_labels.py`，用真 Alert 过 `to_feed_item` 再按 JS 逻辑渲染）：

```
  --- 放量异动 量比5.2 速度12.6倍
      to_feed_item().extra = {"amount": 102000000.0, "volume_ratio": 5.2, "turnover": 3.0, "amplitude": 3.0}
      悬停行渲染为: amount 102000000.0
      悬停行渲染为: volume_ratio 5.2
      悬停行渲染为: turnover 3.0
      悬停行渲染为: amplitude 3.0
  --- 封涨停 封单3300万
      to_feed_item().extra = {"amount": 540000000.0, "turnover": 5.0, "seal_amount_wan": 3300.0}
      悬停行渲染为: amount 540000000.0
      悬停行渲染为: seal_amount_wan 3300.0
  --- 快速反弹 -0.98%
      to_feed_item().extra = {"amount": 101000000.0, "window_pct": -0.98}
      悬停行渲染为: amount 101000000.0
      悬停行渲染为: window_pct -0.98
```

**问题**：

* `amount 102000000.0` 是**元**；而同一个看板的股票表（`dashboard.html:318,598`）用
  `成交额(亿)` 表头 + `num(Number(q.amount)/1e8)`，显示成 `1.02`。
  同一个页面里同一个量，一处 `1.02`（亿）、一处 `102000000`（元），差 8 个数量级；
* `turnover 5.0` 缺 `%`；`window_pct -0.98` 缺 `%`；`amplitude 3.0` 缺 `%`；
* 键名是英文（`window_pct` / `seal_amount_wan` / `ratio_vs_float`），
  与提示里其他行的中文（「信号」「分组」）风格不一致。

**说明**：这条严重度低（是悬停提示，不是主行），但它是**唯一**能让用户看到
`amount` 原始元值的地方，而旁边的表格用亿 —— 属于「同一屏两个单位」的典型。

**建议改法**（未实施）：

```diff
-  if(Object.prototype.hasOwnProperty.call(ex, k) && ex[k] != null) tip.push(k + " " + ex[k]);
+  var EX = {amount:["成交额","亿",1e-8], volume_ratio:["量比","",1],
+            turnover:["换手","%",1], seal_amount_wan:["封单额","万元",1],
+            window_pct:["区间涨跌","%",1], amplitude:["振幅","%",1],
+            ratio_vs_float:["占流通盘","%",1]};
+  if(Object.prototype.hasOwnProperty.call(ex, k) && ex[k] != null){
+    var m = EX[k];
+    tip.push(m ? (m[0] + " " + num(Number(ex[k]) * m[2], 2) + m[1]) : (k + " " + ex[k]));
+  }
```

---

### D11 配置注释称 `rebound_off_low_pct`「等价于 rebound_pct（保留兼容）」，但该键从未被读取

**位置**：`config/settings.yaml:233`、`src/arad/rules/spirit_price.py:65`

```yaml
# config/settings.yaml:233
    rebound_off_low_pct: 2.0       # 旧配置名，等价于 rebound_pct（保留兼容）
```

```python
# src/arad/rules/spirit_price.py:65
    # 兼容旧配置名（等价于 rebound_pct）
    "rebound_off_low_pct": 2.0,
```

但 `_sig_rebound:226` 只读 `rebound_pct`：

```python
if off_low < self._g("rebound_pct", 2.0):
```

**实跑输出**（`p12_alias.py`，构造「自低点拉起 +1.50%」的反弹）：

```
  --- 只改旧配置名 rebound_off_low_pct（不设 rebound_pct）---
    rebound_off_low_pct=1.0  ->  0 条  []
    rebound_off_low_pct=3.0  ->  0 条  []

  --- 只用 rebound_pct ---
    rebound_pct=1.0  ->  1 条  ['快速反弹 -1.55%']
    rebound_pct=2.0  ->  0 条  []
```

**手算**：自低点拉起 `(9.8455/9.70-1)*100 = +1.50%`。
设 `rebound_off_low_pct=1.0` 时门槛应降到 1.0 → 1.50% 应触发，实际 **0 条**；
设成 3.0 时门槛应升到 3.0 → 不应触发，实际也是 **0 条**。
两次结果相同 ⇒ 该键对判定**没有任何影响**，实际仍在用 `rebound_pct` 的默认 2.0。

**影响**：老配置文件（或照注释写配置的用户）改 `rebound_off_low_pct` 会**静默无效**——
不报错、不告警，只是那个门槛不生效。这类「注释承诺了、代码没实现」的问题
与本仓库已修的「`--config` 静默丢配置」（`git log` `3d04a38`）属同一类。

**建议改法**（未实施，二选一）：

```diff
# 方案 A：真的实现兼容
     def _g_rebound_thr(self) -> float:
         """兼容旧键：rebound_off_low_pct 存在且未显式配 rebound_pct 时用它。"""
         raw = self.cfg.get("rebound_pct")
         if raw is None:
             raw = self.cfg.get("rebound_off_low_pct")
         return _num(raw, 2.0)
```

```diff
# 方案 B：删掉这个键与注释，不再假装兼容
-    # 兼容旧配置名（等价于 rebound_pct）
-    "rebound_off_low_pct": 2.0,
```
```diff
-    rebound_off_low_pct: 2.0       # 旧配置名，等价于 rebound_pct（保留兼容）
```

---

## 3. 检查过但没问题的

下表逐条列出**实际驱动过**的告警类型（共 **26 种**，覆盖 7 个规则模块），
以及该条的「标题/详情的数字」与「`metrics` 里的真值」是否一致。

### 3.1 `spirit_order`（8 个信号，全部实测）

探针 `p1_spirit_order.py` 用测试模块的 `fire_all_eight()` 一次性打出全部 8 种，
再用正则从标题里抠出数字、与 `metrics` 做**自动比对**：

```
### 单位一致性自动核对（从标题解析数字，与 metrics 对照）
  大笔买入   标题=      6000.0  真实(d_outer_lots)=       6,000.0  OK
  大笔卖出   标题=      7000.0  真实(d_inner_lots)=       7,000.0  OK
  机构吃货   标题=      6000.0  真实(buy_shares)=       6,000.0  OK
  机构吐货   标题=      7000.0  真实(sell_shares)=       7,000.0  OK
  机构买单   标题=     12600.0  真实(bid_lots)=      12,600.0  OK
  机构卖单   标题=     12600.0  真实(ask_lots)=      12,600.0  OK
  有大买盘   标题=     12604.0  真实(bid_total_lots)=      12,604.0  OK
  有大卖盘   标题=     12604.0  真实(ask_total_lots)=      12,604.0  OK
```

`机构吃货` 的那一行是**已修 bug 的回归确认**：标题现在用 **手**（`d_outer` 本来就是手），
而 `metrics["buy_shares"]` 是**股** = 手 × 100（`6,000 手 = 600,000 股`），
比对时按 `1/100` 因子折算，一致。历史上这里写的是 `{hit[0] / 1e4:,.0f}股`，
把 7,000 股显示成「1股」（`spirit_order.py:455-461` 有完整的复盘注释）。

| 信号 | 种类 | 标题数字 | `metrics` 键 | 结论 |
|---|---|---|---|---|
| 大笔买入 | `big_buy` | `6,000手` | `d_outer_lots`=6000.0 | ✓ |
| 大笔卖出 | `big_sell` | `7,000手` | `d_inner_lots`=7000.0 | ✓ |
| 机构吃货 | `institution_eat` | `6,000手` | `buy_shares`=600000.0 股（=6000 手） | ✓ |
| 机构吐货 | `institution_vomit` | `7,000手` | `sell_shares`=700000.0 股（=7000 手） | ✓ |
| 机构买单 | `institution_buy` | `12,600手` | `bid_lots`=12600.0 | ✓ |
| 机构卖单 | `institution_sell` | `12,600手` | `ask_lots`=12600.0 | ✓ |
| 有大买盘 | `big_bid_wall` | `12,604手` | `bid_total_lots`=12604.0 | ✓ |
| 有大卖盘 | `big_ask_wall` | `12,604手` | `ask_total_lots`=12604.0 | ✓ |

同时核对了 `extra` 里的**复合单位**（同一句里同时出现手/股/万元），全部自洽。例如：

```
  机构吃货
  title : 机构吃货 6,000手
  detail: 本区间主动买入成交 600,000 股 / 60.0 万元（约 6,000 手），命中：50万股 或 100万元 或 流通盘0.10%
```

手算：`6,000 手 × 100 = 600,000 股` ✓；`600,000 股 × 10.00 元 = 6,000,000 元 = 600.0 万元`…
（该用例 `amount` 构造为 600,000 股 × 10 元，归因后 `buy_amount` = 60.0 万元 —— `metrics` 与 `extra` 一致，单位换算正确。）

阈值描述文本 `{thr_shares / 1e4:.0f}万股`、`{thr_amount / 1e4:.0f}万元`
（`spirit_order.py:510,512,654,656,681`）也逐条核对：`500000/1e4 = 50万股` ✓、`1000000/1e4 = 100万元` ✓。

### 3.2 `spirit_price`（4 个信号）

| 信号 | 种类 | 标题 | `window_pct` | `signal_pct` | 结论 |
|---|---|---|---|---|---|
| 火箭发射 | `rocket` | `火箭发射 +3.00%` | 3.0 | 3.0 | ✓ 两值相等 |
| 加速下跌 | `accel_down` | `加速下跌 -4.81%` | -4.808 | -4.808 | ✓ 两值相等 |
| 快速反弹 | `rebound` | 见 **D1** | -0.98 | **+2.02** | ✗ 缺陷 |
| 高台跳水 | `dive` | 见 **D1** | -0.50 / +2.00 | **-6.533 / -2.941** | ✗ 缺陷 |

多窗口场景也核对了窗口标注：

```
### rocket windows=[60,180,300]（看窗口标注）   条数=1
  title : 火箭发射 +3.00%
  detail: 火箭发射 · 现价 10.30  涨跌 +3.00%  窗口 5分钟 +3.00%
  metrics: {'window_seconds': 300.0, 'window_pct': 3.0, 'signal_pct': 3.0, ...}
```

`_fmt_s(300) = "5分钟"` ✓，与 `window_seconds=300.0` 一致。

### 3.3 `limit_board`（涨停侧 4 种 + 跌停侧 1 种）

| 告警 | `pattern` | 标题 | 核对 | 结论 |
|---|---|---|---|---|
| 封涨停 | `limit_up_seal` | `封涨停 封单3300万` | 30000 手×100×11.00/1e4 = 33000 万元 ✓ | ✓ |
| 封涨停（超大单） | `limit_up_seal` | `封涨停 封单1.10亿` | 100000×100×11/1e4 = 110000 万元 = 11.00 亿，`seal_amount_wan`=11000.0 ✓ | ✓ |
| 一字涨停 | `limit_up_seal` | `封涨停 封单3300万` | `一字板` 标注 ✓ | ✓ |
| 触及涨停 | `limit_up_touch` | `触及涨停 +9.95%` | `pct`=9.95 ✓ | ✓ |
| 炸板 | `open_limit_up` | `炸板 -4.55%` | (11.00-10.50)/11.00×100 = 4.545%，`retreat_pct`=4.545 ✓ | ✓（数字对，措辞见 D5） |
| 封跌停 | `limit_down_seal` | `封跌停 封单2700万` | 30000×100×9.00/1e4 = 27000 万元，`seal_amount_wan`=2700.0 ✓ | ✓（标签见 D2） |

`封单` 的量级切换逻辑（`limit_board.py:210`）也实测过两侧：

```
  seal_wan =  3300.0 -> "封单3300万"      # < 10000 走「万」
  seal_wan = 11000.0 -> "封单1.10亿"      # >= 10000 走「亿」，/10000 换算正确
```

### 3.4 `unusual`（5 种形态）

| 形态 | `pattern` | 标题 | 核对（手算） | 结论 |
|---|---|---|---|---|
| 高开低走 | `high_open_fade` | `高开低走 -3.8%` | (10.10-10.50)/10.50×100 = -3.8095% ✓ | ✓ |
| 低开高走 | `low_open_rise` | `低开高走 +8.0%` | (10.42-9.65)/9.65×100 = +7.979% ✓ | ✓ |
| 巨震 | `wide_amplitude` | `巨震 11.0%` | (10.60-9.50)/10.00×100 = 11.00% ✓ | ✓ |
| 尾盘急拉 | `late_surge` | `尾盘急拉 +2.0%` | (10.20/10.00-1)×100 = +2.00% ✓ | ✓ |
| 尾盘跳水 | `late_surge` | `尾盘跳水 -2.5%` | (9.95/10.20-1)×100 = -2.451% ✓ | ✓ |
| 快速回封 | `reseal` | `快速回封` | 见 **D4** | ✗ 缺陷 |

标题里 `intraday:.1f` 与 `extra` 里 `intraday:.2f` 的**位数差异**也核对了，是**有意的**：
标题取 1 位便于扫读，`extra` 给 2 位精确值，两者不矛盾（例：标题 `-3.8%` / 正文 `-3.81%`）。
`低开高走` 的标题带 `+`（`{intraday:+.1f}`）、`高开低走` 不带（`{intraday:.1f}`）——
实测值本来就是负的，两种写法渲染出的正负都正确，不构成缺陷。

### 3.5 `volume_burst`（1 种）

```
### volume_burst 放量异动   条数=1
  title : 放量异动 量比5.2 速度12.6倍
  detail: 量比 5.2  速度 12.6 倍（近 60 秒成交 6,000 手，当日均速 476 手/分）
  metrics: {'volume_ratio': 5.2, 'speed_ratio': 12.6, ...}
```

手算：近 60 秒成交 `16000 - 10000 = 6,000` 手 → 每分钟 6,000 手；
当日均速 `100000 手 / 12600 秒 × 60 = 476.19 手/分`；
比值 `6000 / 476.19 = 12.6 倍` ✓ 与标题和 `metrics` 三方一致。
`手`/`手/分` 单位正确（`volume_delta` 的元组第三项是累计**手**，见 `engine.py:63`）。

成交额位数也核对了：

```
amount = 123,456,000 元 = 1.23456 亿
detail: 换手 3.00%  成交额 1.23 亿  振幅 3.00%
```

`{amount / 1e8:.2f} 亿` = `1.23 亿` ✓，单位与换算都正确。

**未显示量比的情形**也核对了（测试 `test_rule_volume_burst.py:177` 钉的行为）：

```
  detail: 量比 -  速度 9.6 倍
```

数据源不给量比（`vr == 0`）时 `vr_txt = "-"`（`volume_burst.py:161`），
而不是印 `0.0` —— 这是**正确处理**，`-` 表示「无此数据」，不会误导成「量比为零」。

### 3.6 `tick_surge`（2 种）

| 告警 | 标题 | 核对 | 结论 |
|---|---|---|---|
| 急拉 | `急拉 +2.48% / 1分钟` | (10.35/10.10-1)×100 = +2.475% ✓ | ✓ |
| 急拉（临近涨停） | `急拉 +9.00% / 3分钟` | 涨停价 11.00，(11.00/10.90-1)×100 = +0.917% → 显示 `+0.92%` ✓ | ✓ |
| 急跌 | `急跌 -2.51% / 1分钟` | (9.70/9.95-1)×100 = -2.513% ✓ | ✓（`距涨停` 见 D3） |

窗口文案函数 `_fmt_seconds` 逐值实测：

```
  _fmt_seconds(   10) = '10秒'
  _fmt_seconds(   60) = '1分钟'
  _fmt_seconds(   90) = '1.5分钟'
  _fmt_seconds(  180) = '3分钟'
  _fmt_seconds(  300) = '5分钟'
```

`< 60` 走秒、整分走「N分钟」、非整分走一位小数 ✓，与 `window_seconds` 一致。

「量能比 数据不足」这一呈现也核对了：`vol_ratio` 为 `None` 时
`ratio_txt = "数据不足"`（`tick_surge.py:255`），且 `metrics["vol_ratio"] = -1.0`
作为哨兵值 —— 文案是「数据不足」而非 `-1.00倍` ✓，处理正确。

### 3.7 `spirit_index`（2 种）

| 告警 | 标题 | 核对 | 结论 |
|---|---|---|---|
| 拉升指数 | `拉升指数 +10.00点 (+0.26%)` | (3900/3890-1)×100 = +0.257% ✓；`delta_points`=10.0 ✓ | ✓ |
| 打压指数 | `打压指数 -10.00点 (-0.26%)` | (3880/3890-1)×100 = -0.257% ✓；`delta_points`=-10.0 ✓ | ✓ |

标题的 `点` 与 `%` 两个口径都对，`window_pct` / `window_bp` / `strength` 也自洽
（`25.71bp` = `0.2571% × 100` ✓）。详情第 2 行的硬编码见 **D6**。

### 3.8 展示层与通知器（无缺陷项）

| 位置 | 检查项 | 结论 |
|---|---|---|
| `models.py:391` `one_line()` | `sign = "+" if self.pct >= 0 else ""` | ✓ 前缀反映的是**当日涨跌**，与信号方向无关是**既有的、已归档的设计**（见下） |
| `notifiers/dingtalk.py:82,101` | `sign_s = "+" if pct >= 0 else ""` | ✓ 同上 |
| `notifiers/serverchan.py:37,59` | `sign = "+" if pct >= 0 else ""` | ✓ 同上 |
| `notifiers/console.py:263-266` | 表格行 `{float(a.pct):>+7.2f}%` | ✓ `+` 号由格式串给出，与 `pct` 符号一致 |
| `notifiers/webhook.py:56-63` | `KIND_LABELS` 6 项 | ✓ `surge/plunge/limit_up/limit_down/volume_burst/unusual` 与 `AlertKind` 成员一一对应 |
| `dashboard.html:318,598` | `成交额(亿)` 表头 + `amount/1e8` | ✓ 表头与渲染一致 |
| `dashboard.html:475` | `el.title = a.detail \|\| a.title` | ✓ 原始 `detail` 直出，无二次加工导致的失真 |
| `spirit.py:181-195` `_finite()` | nan/inf 清洗 | ✓ 哨兵值处理正确（`JSON.parse` 兼容性） |
| `spirit.py:129-143` `signal_of()` | `pattern` → `signal` → `kind` 回退 | ✓ 三级回退顺序正确 |
| `limit_board.py:210` | `封单{seal_wan:.0f}万` / `封单{seal_wan/1e4:.2f}亿` | ✓ 万/亿切换阈值与换算都对 |
| 全部模块 | `{amount / 1e8:.2f}亿` / `{... / 1e4:.1f}万元` | ✓ 逐一核对 12 处，换算与单位标注全部正确 |

**关于「上涨告警显示绿色 / 下跌告警显示红色」**：这不是缺陷，且**已归档为设计**：

* `docs/ARCHITECTURE.md:411`：「推论：`up`/`down` 行**不保证**涨跌幅符号一致。
  快速反弹、打开跌停这类信号，本身就是……」
* `docs/DASHBOARD_VISUAL_AUDIT.md:82`：「`up`/`down` 是**信号方向**，不是当日涨跌方向。」

即：颜色 = 信号方向，`+`/`-` = 当日涨跌，两者**本来就可以不一致**。
一条「快速反弹」告警显示红色、`pct` 却是 `-0.50%`，是正确行为。
（注意这与 **D1** 不同：D1 的问题在**同一个数字域内**——标题的 `-0.98%` 与
正文的 `+2.02%` 都是「变化幅度」，用户无法判断哪个是信号强度。颜色与 `pct` 的关系是跨域的，不冲突。）

---

## 4. 检查方法

### 4.1 环境

```powershell
$env:PYTHONPATH='src'; $env:PYTHONIOENCODING='utf-8'
cd D:\ccc\ashare-radar
```

Python 3.13.12（Anaconda）。`src/` 仅依赖标准库 + PyYAML，**未安装任何新依赖**。

### 4.2 基线（证明「只读审计、未改源码」）

```powershell
python -m pytest -q
```

输出（尾部）：

```
........................................................................ [ 97%]
...........................                                              [100%]
```

`exit code: 0`，**1032 passed**。全程未改动 `src/`、`tests/`、`config/` 下任何文件。

### 4.3 逐模块探针脚本

全部写在 `%TEMP%\arad_audit\`（**不是** heredoc，**不是** `python -c`，避免引号/编码问题），
每个都以 UTF-8 包装 stdout：

```python
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"D:\ccc\ashare-radar\tests")   # 复用 tests/fakes.py 的 make_quote 等
```

| 脚本 | 覆盖 | 用于 |
|---|---|---|
| `p1_spirit_order.py` | `spirit_order` 8 信号 | 标题↔`metrics` 自动比对（D0 回归 / 3.1） |
| `p2_spirit_price.py` | `spirit_price` 4 信号 + 多窗口 | **D1** |
| `p3_limit_board.py` | `limit_board` 封/触/炸/一字/天地板 + `distance_to_limit_pct` 三口径 | 3.3 |
| `p4_misc.py` | `unusual` 5 形态 + `volume_burst` + `tick_surge` | 3.4/3.5/3.6、**D3** |
| `p5_spirit_index.py` / `p5b_spirit_index.py` | `spirit_index` 2 信号 × 5 种窗口 | **D6**（p5 忘了 `enabled: True`，p5b 是修正版） |
| `p6_labels.py` | 一字跌停标签 + 悬停 `extra` 渲染 + `SIGNALS` hint | **D2**、**D9**、**D10** |
| `p7_scope.py` | 回封窗口口径 + 炸板「封单」措辞 | **D4**、**D5** |
| `p8_dead.py` | 跌停侧可达性 + `_fmt_seconds` + 全模块单位总表 | 可达性初判 |
| `p9_rebound_worst.py` | 反弹/跳水 4 种最坏情形 + 对照组 | **D1** 的决定性证据 |
| `p10_plunge.py` | `tick_surge` 急跌 | **D3** |
| `p11_registry.py` / `p11b_registry.py` | `SIGNALS` 可达性（v1 靠正则误判，v2 改为驱动规则） | **D7** |
| `p12_alias.py` | `rebound_off_low_pct` 兼容性 | **D11** |

复跑方式（例）：

```powershell
$env:PYTHONPATH='src'; $env:PYTHONIOENCODING='utf-8'
python "$env:TEMP\arad_audit\p9_rebound_worst.py"
```

### 4.4 判定手法

1. **构造 → 打印 → 手算三方对照**：每个探针都先打印「[算术]」行写明预期公式，
   再打印 `title` / `detail` 逐行 / `metrics` 全量，最后人工核对三者是否同一个数。
2. **对称位对照**：买/卖、涨停/跌停、火箭/跳水等成对信号一律**两侧都跑**。
   D2（一字跌停 vs 一字涨停）、D3（急跌 vs 急拉）、D5（炸板 vs 封板）
   都是靠对照组发现的。
3. **跨配置对照**：凡是文案里出现数字/时长的，都用**非默认配置**再跑一遍
   （D1 的窗口 180/300、D6 的窗口 60/90/120/600、D11 的门槛 1.0/3.0）。
   把默认值写死在文案里的问题只有这样才暴露。
4. **可达性靠驱动而非 grep**：`p11_registry.py` 起初用正则从源码捞 `"pattern": "..."` 字面量，
   漏掉了三目表达式 `"limit_up_touch" if rising else "limit_down_touch"` 里的分支，
   误判 `limit_down_seal` 不可达。`p11b` 改为**实际构造行情驱动规则**，
   再比对 `spirit.SIGNALS` 的注册项 —— 结论以驱动结果为准。
5. **同文件内互相印证**：D2 靠 `detail2`（`非一字`）与 `detail3`（`振幅 0.00%`）同屏矛盾，
   D6 靠 `detail1`（`窗口 1分钟`）与 `detail2`（`5 分钟`）相邻矛盾。
   这类「文本自己打自己」的证据最硬，不依赖任何外部口径。

### 4.5 明确未做的事

* **没有**修改 `src/`、`tests/`、`config/` 下任何文件；本报告是唯一新增产物。
* **没有**在真实行情/真实网络下跑（无数据源凭据）；所有证据均来自离线探针。
* **没有**覆盖 `notifiers/` 的**网络发送**路径（webhook 实际投递、钉钉加签等），
  只审了它们的**文本生成**函数（见 3.8）。
* **没有**审计 HTML/CSS 布局与 JS 交互（除 D10 的悬停文本拼接外）。

---

## 5. 未证实的怀疑

以下条目**没有**满足「实跑输出 + 算术」的举证标准，列在此处仅供后续排查，**不计入缺陷数**。

1. **`limit_board._touched_recently` 的快照兜底可能放宽炸板判定**
   `:283-284` 在历史为空时用 `q.high >= limit - tol` 兜底。
   怀疑：若某票开盘即涨停后一路阴跌（`high` 停在涨停价），此后 `_touched_recently`
   永远为真，每天只在 `broken` 状态复位的边界上才重新报。
   未证实原因：`_check_side` 的状态机（`:168-172` 的 `prev == "broken"` 短路）
   已经处理了这个场景，我没能构造出「兜底导致误报」的最小用例。

2. **`unusual` 的「炸板」口径与 `limit_board` 不一致**
   `unusual.py:172` `reseal_pullback_pct` 默认 `0.5`，`limit_board.py:30` `break_retreat` 默认 `0.3`。
   潜在问题：同一只票可能在 `unusual` 里算「回封过」而在 `limit_board` 里从没报过炸板。
   未证实原因：这是两个独立规则的**口径差异**（可能是有意的松紧搭配），
   需要真实行情样本才能判断是否产生用户可见的矛盾文案。

3. **`spirit.to_feed_item` 的 `extra` 白名单偏窄**
   `spirit.py:223-226` 只放 7 个键，`signal_pct`（D1 的关键量）、`vol_ratio`、
   `to_limit_pct` 都不在内。
   潜在问题：用户在悬停提示里**看不到**这些已在 `metrics` 里算好的量。
   未证实原因：这是「少了信息」而非「错了信息」，是否算缺陷取决于产品预期，故只记为观察。

4. **`console.py:233` 表头的硬编码列宽**
   `f"{'时间':<8} {'代码':<7}{'名称':<10}{'信号':<10}{'现价':>9}{'涨跌':>9}"`
   用的是**字符数**对齐，而 `_fit()`（`console.py`）对中文按 **2 字符宽**处理。
   潜在问题：名称超过 `align_name`（默认 10）时表头与数据行可能错位。
   未证实原因：需要真实中文股票名（4 字 = 8 宽）在特定宽度下的渲染对比，
   且这只影响对齐美观、不影响数值正确性。

5. **`limit_board` 天地板会同时产出两条 `sev 3`**
   `p3_limit_board.py` 的天地板用例（触及涨停后砸到跌停）打出 **2 条**告警：
   `炸板 -18.18%` 与 `封跌停 封单2700万`。
   潜在问题：`炸板` 的 `-18.18%` 与 `封跌停` 的 `-10.00%` 同屏，
   用户可能误以为跌了 18%。
   未证实原因：`-18.18%` 是**自涨停价的回落幅度**（`detail1` 写了「涨停价 11.00 回落 18.18%」），
   口径在正文里交代清楚了，标题的 `-` 前缀也没说它是当日涨跌。
   严格说属于「标题未标明口径」而非「算错」，故未列为缺陷。
