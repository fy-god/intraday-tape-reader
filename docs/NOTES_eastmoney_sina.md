# NOTES — EastmoneySource / SinaSource 实现记录

作者：数据源 agent（`src/arad/sources/eastmoney.py`、`src/arad/sources/sina.py` 及对应测试）
日期：2026-09-15

按 `docs/DATA_CONTRACT.md` 第 10 节第 4 条，这里只**记录**契约疑点，**未擅自修改**任何
他人文件或 `docs/DATA_CONTRACT.md`，请主 agent 裁决。

---

## 1. 发现并已自行处理的契约空缺（不阻塞，但请主 agent 确认）

### 1.1 `Quote.list_date` —— 契约第 1 节没有这个字段，但 `models.py` 已经有了
`docs/DATA_CONTRACT.md` 第 1 节的 `Quote` 定义里**没有** `list_date`，但当前
`src/arad/models.py` 第 130 行已新增：

```python
list_date: str = ""   # 上市日期 "YYYYMMDD"（股票池源提供，用于新股过滤；缺失为空串）
```

并且 `arad/filters.py` 的 `Filters._too_new()` 与 `arad/engine.py` 第 387-390 行
**依赖它**（把 `Quote.list_date` 灌进 `filters.list_dates` 做次新股过滤，
`config/settings.yaml` 的 `filters.min_list_days=11`）。

为了不把次新股过滤变成空转，`EastmoneySource.parse_clist` 已用 `f26` 填充该字段：

- `f26` 是 `YYYYMMDD` 整数（真实样本 `20220810`）→ 转成 `"YYYYMMDD"` 字符串；
- 解析失败 / 缺失 / 非法日期（如 `20220230`）→ `""`（空串，`filters` 见到空串会**放行**而不是误杀）。

**待主 agent 确认**：第 1 节的 `Quote` 代码块应补上 `list_date: str = ""`，
否则「唯一权威接口约定」与 `models.py` 实际代码不一致。
`SinaSource` 不提供该字段（新浪无上市日期），保持 `""`。

### 1.2 `SinaSource.universe()` 的语义
> ⚠ **本节结论已作废（2026-09-17）**：新浪**行情中心**另有列表接口
> `Market_Center.getHQNodeData`，实测能枚举全市场 5563 只。`universe()` 现在是
> 东财被限流时的自动兜底，**不再抛 `SourceError`**。详见新增的 **§6**。
> 下面这段保留作历史记录。

契约第 1 节把 `universe()` 定义为「全市场股票池」，但新浪 `hq.sinajs.cn` 是**按代码查询**
的接口（`list=sh600000,...`），**无法枚举全市场**——契约第 2.3 节也只说新浪是
「交叉校验 / 兜底」，股票池主力明确是东财（第 2.2 节 + `settings.yaml` 的
`sources.universe: eastmoney`）。

因此 `SinaSource.universe()` **明确抛 `SourceError`**（而不是返回 `[]`）：
返回空列表会被引擎误判为「市场里没有股票」从而静默失效，抛错才能触发故障转移，
并给出可读原因。若主 agent 希望它有别的语义（例如抛 `NotImplementedError`、
或返回 `snapshots(引擎传入的代码)`），请裁决。

### 1.3 新浪成交量的单位换算方向
契约第 0 节规定 `volume_lots` 单位是**手**，第 2.3 节注明新浪字段 8 是**股**「注意除以 100 转手」。
已按此实现，并用腾讯样本**交叉验证**（同一次采集、几乎同一时刻）：

| 代码 | 新浪 f8(股) | /100 后(手) | 腾讯 f6(手) |
|---|---|---|---|
| sh600000 | 77187101 | 771871.01 | 771871 |
| sz000001 | 74124802 | 741248.02 | 741248 |

两者吻合（新浪是当日全量、腾讯样本截取稍早，故有±1 手内的零头）。

---

## 2. fixture 与真实线路的**编码不一致**（重要，已兼容）

契约第 0 节规定「新浪是 **GBK**」。但 `fixtures/raw/sina_bulk_5.txt` 是
`tools/probe_sources.py` 第 178 行用 **`encoding="utf-8"`** 落盘的：

```python
(RAW / "sina_bulk_5.txt").write_text(s, encoding="utf-8")   # s 已由 GBK 解码成 str
```

所以该文件的字节流其实是 **UTF-8**，**严格按 GBK 解码会抛
`UnicodeDecodeError: 'gbk' codec can't decode byte 0xa1 in position 31`**。

`parse_response` 因此做了**编码回退**：先按 GBK 解码，失败再按 UTF-8 解码。
两条路径的测试都已覆盖（`test_parses_both_utf8_fixture_and_gbk_wire_bytes`）。

- 对**生产**无影响：真实响应是 GBK，走第一分支。
- 对**离线测试**是必需的，否则 fixture 里的中文名会变成乱码。
- 顺带修正：`tools/probe_sources.py` 里 `tencent_bulk_sample.txt` 也是同样情况
  （真实 GBK、落盘 UTF-8），若后续有 agent 写腾讯源，会遇到同一个坑。
  **建议**（不擅自改他人文件）：把 fixture 落盘改为 `write_bytes(raw)` 保留原始字节。

---

## 3. 契约未写、但实测/推导后确定的行为（已按合理默认实现）

### 3.1 东财 `f20/f21` 的单位
契约第 2.2 节只说 `f20`总市值 `f21`流通市值，**没说单位**；第 0 节要求 `*_cap` 一律是**亿元**。
实测 `f20=8643794354`（元）→ `/1e8` = `86.44` 亿，与 `Quote` 语义一致，已按此换算
（`universe_sample.json` 里也保留的是元值，可交叉验证）。
测试断言 `total_cap < 1e6`（亿元量级）以防回归。

### 3.2 东财不提供涨跌停价
`clist`/`ulist` 都没有涨跌停字段 → `limit_up`/`limit_down` 保持 `0.0`，
由 `Quote.limit_up_price` / `limit_down_price` 按板块费率推算（创业板满坤科技
`48.64*1.2=58.37`，与实测现价 58.37 完全一致，说明该推算路径可靠）。

### 3.3 东财 `f11` 不是买一价
`f11` 是「5 分钟涨跌幅%」，契约第 2.2 节已写明。故 `bid1/ask1/bid_vol/ask_vol`
全部保持 `0.0`（东财不提供委买卖盘），**没有**把它误当买一价。

### 3.4 新浪 `f10`/`f20` 是买一量/卖一量（股）
契约第 2.3 节只列到字段 9 和 30/31。实测字段 10/20 是买一量/卖一量**股数**
（`sh600000`: 114500 / 242450，与字段 6/7 的买一/卖一**价** 9.390/9.400 成对）。
已按 `/100` 转成手存入 `bid_vol`/`ask_vol`。契约若需固定这些下标，建议补进 2.3 节。

### 3.5 停牌/无效行**丢弃**而非置 0
- 东财：`f2` 或 `f18` 为 `"-"` → 整条丢弃。实测 `eastmoney_ulist_5.txt` 里
  `430047 诺思兰德(已切换)` 整行是 `"-"`，5 码只解析出 4 条。
- 新浪：空 payload、字段 <32、现价或昨收 `<=0` → 整行跳过（`bj430047` 全 0 被跳过）。

这样 `is_suspended` 的判定不会被伪造的 0 值污染。

### 3.6 `f26` 上市日期解析失败的处理
题目要求「解析失败置 0」，但 `Quote.list_date` 是**字符串**类型（见 1.1），
置 `0` 会变成 `"0"` 而 `filters._too_new` 需要 8 位数字才生效。已统一置 **`""`**（空串），
语义等价于「缺失」，且与 `models.py` 注释「缺失为空串」一致。

### 3.7 东财请求头
契约第 2.2 节**没有**要求东财带 `Referer`（`tools/probe_sources.py` 抓东财时也没带），
但本实现默认发送 `Referer: https://quote.eastmoney.com/` 以提高反爬健壮性，
并可用 `cfg["referer"]` 覆盖。实测该头不影响接口返回。若主 agent 认为应严格
只发契约列出的头，可把默认值改为空串——不改变任何解析逻辑。

---

## 4. 实现细节备忘（非契约问题）

- **东财翻页**：`total=5913` → 页数 `min(max_pages=80, ceil(5913/100)) = 60`，
  第 1 页单独请求取 `total`，其余 59 页并发（`workers=8`）→ 共 60 次请求。
  实测 `pz` 被服务端硬限制为 100，故 `page_size` 构造时被 `min(..., 100)` 压回。
- **`data`/`diff` 为 `null` 不算失败**：第 1 页为 null → 返回 `[]`；此时退化为
  「顺序探测，遇空页即停」，避免空转 80 页。
- **单页失败容错**：`universe()` 丢 1 页（100 只）仍返回其余 5900+，
  但**会把失败页号记进 `health().err` 并令 `ok=False`**。
  否则「少数页持续丢失」会被报成全健康、故障转移永不触发。
  若**所有**页都失败则抛 `SourceError`。
- **`snapshots()` 分批**：东财每批 ≤50 码（800 码实测 502），新浪默认 600（实测 800 可用）。
  任一批失败即抛 `SourceError`——目标是「这批指定代码」，缺批不能算成功。
- **只返回被请求的代码**：两个源的 `snapshots()` 都会过滤掉未被请求的代码
  （服务端可能回带额外行），避免调用方按 code 取值时被意外覆盖。
- **重试退避**：`0.3 / 0.9 / 2.0` 秒，最后一次不再 sleep；`retries` 次尝试全失败抛
  `SourceError`。空响应体也视为失败。
- **`health()`**：固定 `{'name','ok','latency_ms','err'}`（契约第 1 节格式），
  统计字段用 `threading.Lock` 保护，另提供非契约的 `stats()` 供调试。
- **注入点**：`__init__(self, cfg=None, fetcher=None)`，
  `fetcher(url, headers, timeout) -> bytes`；API Key/URL 等常量集中在模块顶部。

---

## 5. 测试

```
python -m pytest -q tests/test_sources_eastmoney.py tests/test_sources_sina.py
# 108 passed
```

全部离线（`fetcher` 注入 + `fixtures/raw/`），不发任何真实网络请求；
已额外做过 700+ 轮畸形输入 fuzz，解析器不抛异常。

---

## 6. 新发现：新浪**行情中心**列表接口能枚举全市场（2026-09-17 补记）

> 作者：测试 agent（`tests/test_sources_sina.py` 追加小节，未改 `src/`）。
> **本节取代 §1.2 的结论** —— 当时认为「新浪无法枚举全市场」，依据是
> `hq.sinajs.cn` 只支持按代码查询；后来发现新浪**行情中心**另有一套列表接口。

### 6.1 实测结论

| 项 | 实测值 |
|---|---|
| 接口 | `https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData` |
| 节点 | `node=hs_a`（沪深A股），`sort=symbol&asc=1` |
| 单页 | `num=100` 稳定（调大会被服务端截断，故 `UNIVERSE_PAGE_SIZE=100`） |
| 全市场 | **5563 只代码**，共 **56 页**，实测约 **19 秒**拉完 |
| 页数上限 | `UNIVERSE_MAX_PAGES=80`（56 页 + 余量，配置项 `universe_max_pages`） |

爬完的判定有两条：某页解析为空（到底了），或某页**没有新代码**（数据源回绕）。
后者是防"永远翻不完"的兜底，与 `UNIVERSE_MAX_PAGES` 一起构成双重保护。

### 6.2 编码是 **UTF-8**，不是 GBK（与 `hq.sinajs.cn` 不同）

`hq.sinajs.cn` 是 GBK（契约第 0 节 / 本文件第 2 节）；行情中心这套接口返回的是
**UTF-8 JSON 数组**，直接 `json.loads(bytes.decode("utf-8"))` 即可，**不能套用
GBK 解码**，否则中文股票名会变成乱码（注意别把它接到 `parse_response` 那条链路上）。

### 6.3 字段比行情接口少：没有盘口 / 市值 / 涨跌停价

列表接口只提供 `symbol`(带 `sh`/`sz` 前缀) / `code` / `name` / `trade` /
`settlement` / `open` / `high` / `low` / `volume`(股) / `amount`(元)。
**没有**五档、内外盘、市值、换手率、量比、涨跌停价 —— 这些一律置 0，与契约 2.3
的既有取舍一致（涨跌停价由 `Quote.limit_up_price` 按板块费率推算）。
因此 `universe()` 返回的 `Quote` **只保证 `code`/`name` 可靠，唯一用途是拿代码清单**，
其余字段会在下一轮 `snapshots()` 被真实值覆盖。

`volume` 是**股**，与行情接口 f8 同口径 → 同样 `/100` 转手（`volume_lots`）。

### 6.4 停牌行**保留**，与行情接口相反

行情接口遇到停牌行（现价/昨收 <= 0）是**整行丢弃**（§3.5）；列表接口相反：
`trade` 为 0 的行必须**保留**，因为它的**唯一用途是代码清单** —— 丢了它，一只票
停牌一天就会从轮询池里消失，复牌当天完全收不到信号。

### 6.5 `settlement` 为 0 / 缺失时的兜底

列表接口偶发 `settlement=0`。若直接当昨收用，`prev_close=0` 会让该行被
`spirit_price.evaluate` 的 `prev_close <= 0` / `is_suspended` 直接跳过（整只票当轮
什么都不报），且 `limit_up_price` 退化为 0、涨停/炸板判定一起失效。
实现按 `settlement → open → trade` 依次兜底，保证有价格的行 `prev_close > 0`。

### 6.6 现在的定位：东财被限流时的**自动兜底**

`config/settings.yaml` 已配成 `sources.universe: ["eastmoney", "sina"]`：
东财优先（字段最全，含 `list_date` 用于新股过滤），失败自动退到新浪行情中心
（`engine._universe_sources()` / `refresh_universe()` 按顺序尝试，任一来源返回
非空即采用）。两者都失败才降级为「仅监控自选股」并打警告，绝不清空原股票池。

代价：新浪这条链路拿不到 `list_date`，新股过滤在该轮自动失效（`filters` 对空串
一律放行，不会误杀）。
