# NOTES_tencent — 腾讯源实现记录与契约问题

作者：tencent 源 agent ｜ 文件：`src/arad/sources/tencent.py`、`tests/test_sources_tencent.py`
冻结契约：`docs/DATA_CONTRACT.md`（本文件只记录，**未改动任何他人文件**）

---

## 0. 结论速览

| 项 | 结果 |
|---|---|
| 字段下标表（契约 2.1） | ✅ **完全正确**，实测/离线双向验证通过 |
| 响应编码 GBK | ✅ 真实接口确认是 GBK |
| 每批 800 硬上限 | ✅ 实测 5913 码 / 8 批 / 1.00 秒 |
| 停牌 `limit_up=-1` | ✅ 实测存在（样本 55 行），按契约置 0 由 Quote 推算 |
| `fixtures/raw/tencent_bulk_sample.txt` | ⚠️ **不是 GBK，是 UTF-8**；且**不含 `sh600000`**、结尾被截断 |
| `guess_prefix()` vs 契约 2.5 | ⚠️ 前缀集合比文档宽（`9` 开头全给 `bj`，含上海 B 股 900xxx） |

---

## 1. `fixtures/raw/tencent_bulk_sample.txt` 的三个坑（**最重要**）

### 1.1 编码是 UTF-8，不是 GBK

契约第 0 节说「腾讯接口是 **GBK**」——这对真实接口是对的，但**样本文件不是 GBK**。

```python
raw = open('fixtures/raw/tencent_bulk_sample.txt','rb').read()
raw.decode('gbk')    # UnicodeDecodeError: 'gbk' codec can't decode byte 0xa1 in position 25
raw.decode('utf-8')  # OK
```

**根因**：`tools/probe_sources.py:140` 写样本时先 `.decode("gbk","replace")` 再
`.write_text(..., encoding="utf-8")`，即 GBK→str→UTF-8 重编码，不是原始字节。

**影响**：谁要是照契约「GBK 解码」去读样本做离线测试，会直接炸。
**对策**：`decode_body()` 先试 GBK（真实接口），失败回退 UTF-8（样本）。
经验证该文件 GBK↔UTF-8 往返无损（无 `\ufffd`），所以两条路径拿到的是同一份数据。
**建议（交给主 agent 裁决）**：要么把样本重存为 GBK 原始字节，要么在契约第 2.1 节标注
「fixtures 样本为 UTF-8 重编码，仅用于解析测试」。我没有改样本文件。

### 1.2 样本里**没有 `sh600000` / 浦发银行**

契约 2.1 节写 `每行: v_sh600000="1~浦发银行~600000~9.40~9.26~...";`，
但样本 423 行**全部是深市（`sz`）**，一个 `sh` 都没有（`.write_text[:200000]` 只截到第 423 行）。

**对策**：把**真实接口抓到的 `sh600000` 整行**内联进测试
（`SH600000_LINE`，2026-09-14 16:14:38，GBK 解码后逐字节原样），
离线验证契约给出的 `price=9.40 / prev_close=9.26 / high=9.43 / low=9.24`。
**实测结果与契约完全一致**，另补：`open=9.28`、`volume_lots=771871`、
`amount=72231万=7.2231e8元`、`limit_up=10.19`、`limit_down=8.33`、`turnover=0.23`、`量比=1.27`。

### 1.3 结尾是一行**被截断**的残片

```
v_sz000757="51~浩物股份~000757~4.29~4.23~4.      ← 无收尾引号，仅 39 字符
```

所以文件 424 行 = 423 行合法 + 1 行残片。解析器必须容忍（本实现靠正则收尾 `";?$` 天然拒绝）。
测试里断言了 `VALID_ROWS=423`、`MALFORMED_ROWS=1`，避免以后有人把 424 当预期值。

---

## 2. 字段下标表复核（契约 2.1）——**全部正确**

用真实 `sh600000` 行 + 样本 423 行双向核对，未发现任何下标错误：

| 校验项 | 契约下标 | 实测 |
|---|---|---|
| 涨跌额 | `idx31` | `9.40-9.26 = 0.14` = idx31 ✅ |
| 涨跌幅% | `idx32` | `(9.40/9.26-1)*100 = 1.5119` → idx32 `1.51` ✅ |
| 振幅% | `idx43` | `(9.43-9.24)/9.26*100 = 2.0518` → idx43 `2.05` ✅ |
| 成交额 | `idx37` 万元 | `72231 * 1e4 = 7.2231e8` 元 ✅ |
| 均价 | `idx51` | `amount/(手*100) = 9.3579` → idx51 `9.36` ✅ |
| 最高/最低重复列 | `idx41/42` | 与 `idx33/34` 完全相同（`9.43`/`9.24`）✅ |
| 成交量重复列 | `idx36` | 与 `idx6` 相同（`771871` 手）✅ |

### 2.1 两处可改进（**不是错误，是精度/冗余**）

1. **`idx37`（万元）有舍入损失，`idx35` 才是分毫不差的元值。**
   `idx35 = "9.40/771871/722307374"`，第三段 `722307374` 是**元**；
   而 `idx37*10000 = 722310000`，差 **2626 元**（万元四舍五入导致）。
   契约强制用 `idx37`，我按契约实现（`amount` 单位=元）。
   若将来需要精确成交额（例如封单额核对），可考虑改用 `idx35.split("/")[2]`。
2. **`idx29`（最近逐笔）、`idx43`（振幅）、`idx51`（均价）在 `Quote` 里没有对应字段。**
   `Quote` 用派生 property 代替：`amplitude`（≈idx43）、`vwap`（≈idx51）。
   实测误差在 0.01 以内，可安全交叉校验。本实现不映射这三列（无从映射），已在模块 docstring 注明。

---

## 3. 停牌 / PT 股（契约 2.1 末）

* 样本中 **55 行** `limit_up = limit_down = "-1"`（PT金田A、*ST石化A、招商地产…）。
* 本实现：`limit_up/limit_down <= 0` → 一律置 `0.0`，由 `Quote.limit_up_price` /
  `limit_down_price` 按 `limit_rate_of(code, name)` 推算（ST 主板 5%、科创/创业 20%、北交所 30%）。
  **绝不把 `-1` 存进 Quote**（否则 `limit_up_price` 会返回 -1，涨停规则全乱）。
* 样本中 `sz001246 力勤资源`：`price=prev_close=0.00`、`idx38`(换手率) 与 `idx44`(流通市值)
  是**空串**。本实现不整条丢弃（契约要求保留在快照里），空串安全转 `0.0`，
  `Quote.is_suspended` 自然为 `True`。
* 真实全市场实测：5908 只里 **358 只停牌**，**没有任何一只** `limit_up/limit_down` 为负 → 归一化生效。

---

## 4. 全市场实测（2026-09-14 收盘后，用 `fixtures/raw/universe_sample.json` 的 5913 码）

```
codes=5913  chunks=10(每批600)  requests=10  errors=0  wall=1.00s   ← 600/批
codes=5913  chunks=8 (每批800)  quotes=5908   wall≈1.0s              ← 800/批
boards: main 3486 / gem 1449 / star 621 / bj 348 / other 4
```

**契约 2.1 说「5913 个代码」—— 那是请求的代码数，不是解析出的条数。**
实际 **5908** 条：有 **5 个退市/无效代码返回 `v_pv_none_match="1";`**，被正确跳过：

```
600849, 920025, 920201, 920229, 920298   →  响应体就是 v_pv_none_match="1";
```

建议主 agent 在契约里补一句「5913 为请求码数，有效约 5908」，免得下游把 5913 当断言目标。

---

## 5. 代码前缀：`guess_prefix()` 比契约 2.5 更宽（⚠️ 潜在 bug）

契约 2.5：`6→sh；0/3→sz；4/8/920→bj`。
`models.guess_prefix()` 实际是：

```python
if c.startswith("6"):        return "sh"
if c.startswith(("0","3")):  return "sz"
if c.startswith(("4","8","9")): return "bj"     # ← 比契约宽：整个 "9" 段
return "sh"                                      # ← 兜底全给 sh
```

任务要求「代码前缀用 `arad.models.guess_prefix()`」，我照做了（**没有改 models.py**）。后果：

| 代码 | `guess_prefix` | 应为 | 说明 |
|---|---|---|---|
| `900948`（沪市 B 股） | `bj` ❌ | `sh` | 契约只说 `920`，实现吃了整个 `9` |
| `920001`（北交所） | `bj` ✅ | `bj` | 正常 |
| `200011`（深市 B 股） | `sh` ❌ | `sz` | 兜底分支把 `2` 段给了 sh |
| `500011`（沪市基金） | `sh` ✅ | `sh` | 靠兜底蒙对 |
| `123456`（深市转债） | `sh` ❌ | `sz` | 同上 |

**当前无实际影响**：股票池由 eastmoney 提供，实测 5913 码里没有 900xxx/200xxx/1xxxxx，
且 `bj` 前缀对这些代码腾讯也只是返回 `v_pv_none_match`。但这是**契约与实现不一致**，
按纪律记在这里，请主 agent 裁决（改 `guess_prefix` 会动到他人文件，我没动）。

同类不一致：`board_of("000001")`（**不带名称**）返回 `INDEX`，
带名称 `board_of("000001","平安银行")` 返回 `MAIN`。
本实现在解析时就传入了名称，所以平安银行正确判为 `MAIN` ✅；
但下游若用裸代码单独调 `board_of`，000001 会被误判成指数。

---

## 6. 实现约定（与契约/任务书的对齐说明）

| 项 | 实现 |
|---|---|
| 重试语义 | `retries=3` → **总尝试 4 次**（1 首发 + 3 重试），退避 `0.3/0.9/2.0`，与契约 2.1 一致 |
| 重试范围 | fetcher 抛任何异常都重试；重试耗尽才抛 `SourceError`（含批次计数与首错） |
| 失败原子性 | 任一批次彻底失败 → 整体抛 `SourceError`，**不污染 `_cache`**（快照要么全有要么没有） |
| `bulk_chunk` | 默认 600，硬上限 **800**（超出夹紧并 `log.warning`，另有 `assert`）；回退读 `poll.batch_size` |
| 并发 | `ThreadPoolExecutor(workers=cfg.workers)`；`workers` 夹到 `[1, 批数]`，`workers<=0` → 1 |
| 顺序 | 结果按**批次提交顺序**合并，与并发完成顺序无关（输出稳定） |
| 解码 | GBK 优先 → UTF-8 回退（见 §1.1） |
| SSL | 惰性 `ssl.create_default_context()`；仅对证书/TLS 类错误回退到不校验上下文重试一次（`tools/probe_sources.py` 同款做法） |
| 线程安全 | `_cache`/`_stats`/`_seq`/health 全部走 `threading.Lock` |
| import 副作用 | 无任何 I/O、不建 SSLContext、不读配置 |
| `universe()` | 返回 `snapshots()` 并集缓存；空缓存抛 `SourceError`；显式传 `codes` 时等价 `snapshots(codes)`（**不依赖东财**） |

`tencent` 配置节额外识别的键（均有默认值，不加也能跑）：
`bulk_chunk` / `batch_size`、`workers`、`timeout`、`retries`、`referer`、`user_agent`、`base_url`。
`config/settings.yaml` 现有的 `sources.tencent {bulk_chunk: 600, timeout: 10, referer: https://gu.qq.com/}`
已足够，`workers`/`retries` 由 `arad.config.source_cfg()` 从 `poll` 注入。

---

## 7. 测试覆盖（`tests/test_sources_tencent.py`，34 项，全离线）

离线手段：注入 `fetcher(url, headers, timeout) -> bytes`，**不碰真实网络**；样本走 `tests/fakes.load_raw`。

* **真实样本**：423 条 Quote（>300）、跳过截断行、抽查 `sz000001 平安银行`
  （11.85/11.74/11.90/11.72/今开11.73/成交额8.7607e8元/741248手）
  与真实 `sh600000 浦发银行`（9.40/9.26/9.43/9.24）；
* **单位**：`amount` 是元（>1e8，最大 4.195e9）、`volume_lots` 是手（反推均价 == idx51 均价交叉验证）；
* **停牌**：55 行 `-1` 全部归一为 0，`limit_up_price` 由 Quote 推算为 2.98 而非 -1；
  零价格行保留且 `is_suspended=True`、派生量不炸；
* **容错**：空串/非数字/短包（53 段）/坏时间戳 → 默认值或跳过，不整条丢；
* **重试**：注入总是抛错 → 断言 **4 次调用**、退避序列 `[0.3,0.9,2.0]`、最终 `SourceError`；
  部分成功路径断言 `[0.3,0.9]`、`retries=0` 只发 1 次；失败批次不污染缓存；
* **分批**：1300 码 → **3 批** `[600,600,100]`，每批 ≤600；`bulk_chunk=5000` → 夹到 800 → `[800,100]`；
* **并发**：`threading.Barrier(4)` 证明 4 批**真的并行**（峰值并发==4），无死锁；
* **health**：字段恰好 `{name,ok,latency_ms,err}`，类型正确，`latency_ms` 用假时钟验证 ==250ms；
* **universe**：空缓存抛 `SourceError`、快照后返回并集、显式 `codes` 注入、docstring 含「股票池由 eastmoney 源提供」。

**变异测试**（临时改实现，确认测试会红，已还原）：成交额不 ×10000、保留 -1 涨停、
不夹紧 800、去掉重试、去掉退避、误丢停牌行、误丢无效价行 —— **7/7 全部被捕获**。

---

## 8. 给主 agent 的待裁决清单

1. **样本编码**：`fixtures/raw/tencent_bulk_sample.txt` 是 UTF-8（§1.1）。
   建议重存为 GBK 原始字节，或在契约 2.1 标注；我的 `decode_body` 已兼容两者。
2. **样本无 `sh600000`**（§1.2）：契约那行是示意。若要让离线测试覆盖沪市，
   建议把一个真实 `sh` 批次的原始响应补进 `fixtures/raw/`。
3. **`guess_prefix` vs 契约 2.5**（§5）：`9` 段过宽、兜底给 `sh`。
   当前无实际影响，但属契约与实现不一致。
4. **5913 是请求码数不是条数**（§4）：有效 5908，5 个 `pv_none_match`。
5. **`idx35` 比 `idx37` 精确**（§2.1）：若后续要精确成交额可切换数据列。
6. **`Quote` 缺 `idx29/43/51` 三个字段**：现由派生 property 覆盖，误差 <0.01，无需改模型。
