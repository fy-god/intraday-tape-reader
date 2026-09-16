# A股盘中雷达 — 冻结数据契约与模块接口 (v1)

> 本文件是**唯一权威接口约定**。所有并行开发的 agent 必须严格遵守，
> 不得修改本文件描述的函数签名、字段名、枚举值。
> 所有字段索引均已用真实行情数据算术验证（见 `tools/verify_tencent_fields.py`，
> 422/422 只股票 100% 一致），不是从文档抄来的。

---

## 0. 基础约定

| 项 | 约定 |
|---|---|
| Python | 3.13（`py` / `python`），仅用**标准库** + `PyYAML`(已装)。**禁止** import httpx/requests/pandas/numpy/akshare |
| 包根 | `D:\ccc\ashare-radar`，源码在 `src/arad/`，运行时把 `src` 加入 sys.path |
| 命名 | 模块 `snake_case`，类 `PascalCase`，常量 `UPPER_SNAKE` |
| 类型 | 一律写 `from __future__ import annotations`；用 `dataclass(slots=True)` |
| 单位 | 价格=元；成交量=`volume_lots`=**手**；成交额=`amount`=**元**；市值=`*_cap`=**亿元**；比率若非特别说明=百分数(如 `pct=3.5` 表示 +3.5%) |
| 时区 | 全部用北京时间 naive `datetime`（`datetime.now()` 即北京时间，机器已在 CST） |
| 编码 | 源文件一律 UTF-8；腾讯接口是 **GBK**，新浪是 **GBK**，东财是 **UTF-8** |
| 禁止 | 不得 `raise SystemExit`；不得在 import 时做网络请求；不得写 `D:\ccc\ashare-radar` 以外的路径 |
| 测试 | 每个模块作者需自写 `tests/test_<模块>.py`，`python -m pytest -q` 全绿 |
| 离线 | 测试**必须离线可跑**，统一用 `tests/fakes.py` 的 FakeSource |

### 唯一真相源文件（不要改这些）
- `docs/DATA_CONTRACT.md`（本文件）
- `config/settings.yaml`（运行时参数）
- `config/watchlist.yaml`（自选股）
- `tests/fakes.py`（测试替身）
- `fixtures/raw/*`（真实 API 原始响应，供离线解析测试）

---

## 1. 核心数据模型 — `arad/models.py`（由脚手架提供，禁止改字段名）

```python
class Board(str, Enum):        # 板块
    MAIN = "main"              # 主板 60/00   ±10%
    STAR = "star"              # 科创板 688  ±20%
    GEM  = "gem"               # 创业板 300/301 ±20%
    BJ   = "bj"                # 北交所 43/83/87/88/920 ±30%
    INDEX = "index"            # 指数
    OTHER = "other"

class AlertKind(str, Enum):    # 告警类型（稳定标识，写日志/存库/推送都用它）
    SURGE      = "surge"        # 急拉
    PLUNGE     = "plunge"       # 急跌
    LIMIT_UP   = "limit_up"     # 涨停（封板/触板/炸板）
    LIMIT_DOWN = "limit_down"   # 跌停
    VOLUME_BURST = "volume_burst"  # 放量异动
    UNUSUAL    = "unusual"      # 其他异动（高开低走/巨震/快速回封等）

@dataclass(slots=True)
class Quote:
    code: str; name: str; board: Board
    price: float; prev_close: float; open: float
    high: float; low: float
    volume_lots: float          # 累计成交量(手)
    amount: float               # 累计成交额(元)
    turnover: float = 0.0       # 换手率%
    volume_ratio: float = 0.0   # 量比
    bid1: float = 0.0; ask1: float = 0.0
    bid_vol: float = 0.0; ask_vol: float = 0.0
    float_cap: float = 0.0      # 流通市值(亿)
    total_cap: float = 0.0      # 总市值(亿)
    limit_up: float = 0.0; limit_down: float = 0.0
    ts: datetime | None = None  # 行情自带时间戳
    seq: int = 0                # 快照序号(单调递增，用于去重/排序)

    # 派生量（只读 property，全部要处理 prev_close<=0 的停牌情况）
    @property
    def pct(self) -> float            # (price/prev_close-1)*100
    @property
    def change(self) -> float         # price - prev_close
    @property
    def amplitude(self) -> float      # (high-low)/prev_close*100
    @property
    def vwap(self) -> float           # amount/(volume_lots*100)
    @property
    def above_vwap(self) -> bool
    @property
    def is_suspended(self) -> bool    # price<=0 or prev_close<=0 or volume_lots<=0

@dataclass(slots=True)
class Snapshot:
    ts: datetime
    seq: int
    quotes: dict[str, Quote]     # code(6位, 如 "600000") -> Quote
    def get(self, code: str) -> Quote | None

@dataclass(slots=True)
class Alert:
    key: str            # 幂等键 f"{code}:{kind}:{bucket}"  同键在去重窗口内只发一次
    kind: AlertKind
    code: str; name: str
    ts: datetime
    price: float; pct: float
    title: str          # 一行短标题，如 "急拉 +3.2% / 5分钟"
    detail: str         # 多行明细文本
    severity: int = 2   # 1=提示 2=重要 3=紧急
    metrics: dict[str, float] = field(default_factory=dict)
```

**签名（谁都不许改）**

```python
# arad/sources/base.py
class Source(Protocol):
    name: str
    def universe(self) -> list[Quote]: ...      # 全市场静态+快照，失败抛 SourceError
    def snapshots(self, codes: list[str]) -> list[Quote]: ...  # 代码为6位不带前缀
    def health(self) -> dict: ...               # {"name":..,"ok":bool,"latency_ms":int,"err":str}

class SourceError(RuntimeError): ...

# arad/rules/base.py
class Rule(Protocol):
    name: str
    def evaluate(self, snap: Snapshot, ctx: RuleContext) -> list[Alert]: ...

@dataclass
class RuleContext:
    state: EngineState           # 见下
    cfg: dict                    # 该规则的配置字典(已解析默认值)
    now: datetime
    session: SessionPhase

# arad/notifiers/base.py
class Notifier(Protocol):
    name: str
    def send(self, alert: Alert) -> bool: ...
    def send_digest(self, alerts: list[Alert]) -> bool: ...
```

---

## 2. 数据源（已实测，2026-09-15 验证）

### 2.1 腾讯 `qt.gtimg.cn` — **主通道（全市场快照 + 自选股快照）**
```
GET https://qt.gtimg.cn/q=sh600000,sz000001,...      Referer: https://gu.qq.com/
响应编码 GBK。每行: v_sh600000="1~浦发银行~600000~9.40~9.26~...";
```
实测：**5913 个代码 / 8 次请求(每批 800) / 2.13 秒**；单批 800 码 URL 约 7221 字符，返回 ~261KB。
**硬约束：每批最多 800 码（保守取 600），超时 10s，失败重试 3 次(指数退避 0.3/0.9/2.0s)。**

字段下标（按 `~` split 后，**已 100% 算术验证**）：

| idx | 含义 | idx | 含义 | idx | 含义 |
|---|---|---|---|---|---|
|1|名称|19|卖一价|37|成交额(万元)|
|2|代码|20|卖一量(手)|38|换手率%|
|3|**现价**|21|卖二价|39|市盈率TTM|
|4|**昨收**|22|卖二量|41|最高(同33)|
|5|**今开**|23|卖三价|42|最低(同34)|
|6|**成交量(手)**|24|卖三量|43|**振幅%**|
|7|外盘|25|卖四价|44|流通市值(亿)|
|8|内盘|26|卖四量|45|总市值(亿)|
|9|买一价|27|卖五价|46|市净率|
|10|买一量(手)|28|卖五量|47|**涨停价**|
|11|买二价|29|最近逐笔|48|**跌停价**|
|12|买二量|30|时间 `YYYYMMDDHHMMSS`|49|**量比**|
|13|买三价|31|**涨跌额**|50|委差|
|14|买三量|32|**涨跌幅%**|51|**均价**|
|15|买四价|33|**最高**|52|市盈率(动)|
|16|买四量|34|**最低**|53|市盈率(静)|
|17|买五价|35|`价/量/额`| | |
|18|买五量|36|成交量(手)| | |

- 停牌/PT 股 `limit_up`/`limit_down` 可能为 `-1.0`，此时按 `prev_close*板率` 自行计算。
- 时间戳 idx30 之后常跟 `~` 结尾，`payload` split 后长度 ≥54 才有效；代码行必须匹配 `^v_([a-z]{2}\d{6})="(.*)";?$`。
- 无效代码返回 `v_pv_none_match="1";` → 跳过。

### 2.2 东方财富 `push2.eastmoney.com` — **股票池(universe) + 备用快照**
```
GET https://push2.eastmoney.com/api/qt/clist/get
    ?pn=1&pz=100&po=0&np=1&fltt=2&invt=2&fid=f12
    &fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048
    &fields=f2,f3,f5,f6,f8,f10,f11,f12,f13,f14,f15,f16,f17,f18,f20,f21,f22,f26,f124
    &ut=bd1d9ddb04089700cf9c27f6f7426281
响应编码 UTF-8 (JSON)。
```
实测：`total=5913`，**`pz` 被硬限制为 100**（pz=1000 仍只返回 100 行），必须翻页 `pn=1..60`。
并发 8 线程抓 60 页约 **1.42 秒**。

字段：`f2`现价 `f3`涨跌幅% `f5`成交量(手) `f6`成交额(元) `f8`换手率% `f10`量比 `f11`5分钟涨跌%
`f12`代码 `f13`市场(0=深/北,1=沪) `f14`名称 `f15`最高 `f16`最低 `f17`今开 `f18`昨收
`f20`总市值 `f21`流通市值 `f26`上市日期 `f124`行情时间戳(秒)
- `data` 为 `null` 或 `diff` 为 `null` 表示该页无数据；`f2/f18` 可能是 `"-"` 字符串 → 视为无效剔除。
- 备用批量快照 `GET /api/qt/ulist.np/get?secids=1.600000,0.000001&fields=...`，
  实测 **5 码可用，800 码返回 HTTP 502**（URL 过长）→ **每批最多 50 码**。

### 2.3 新浪 `hq.sinajs.cn` — 交叉校验 / 兜底
```
GET https://hq.sinajs.cn/list=sh600000,sz000001    Referer: https://finance.sina.com.cn
响应 GBK。var hq_str_sh600000="名称,今开,昨收,现价,最高,最低,买一,卖一,成交量(股),成交额(元),...,日期,时间,..";
```
实测 800 码 / 310ms 可用。字段下标：`0`名称 `1`今开 `2`昨收 `3`现价 `4`最高 `5`最低
`6`买一 `7`卖一 `8`成交量(**股**,注意除以100转手) `9`成交额(元) `30`日期 `31`时间。
**注意顺位与腾讯不同：新浪是「今开,昨收,现价」，腾讯是「现价,昨收,今开」。**

### 2.4 东财分时（预热用）
`GET https://push2his.eastmoney.com/api/qt/stock/trends2/get?secid=1.600000&fields1=f1,f2,f3,f4,f5&fields2=f51,f52,f53,f54,f55,f56,f57,f58&iscr=0&ndays=1&ut=fa5fd1943c7b386f172d6893dbfba10b`
`data.trends` 为 `"YYYY-MM-DD HH:MM,开,收,高,低,量(手),额(元),均价"` 字符串数组，1分钟一根。

### 2.5 代码前缀规则（重要）
```
6 开头 -> sh ; 0/3 开头 -> sz ; 4/8/920 开头 -> bj
```
`board` 判定：`688/689`→STAR，`300/301`→GEM，`43/83/87/88/920`→BJ，
`60/601/603/605/000/001/002/003`→MAIN，指数(000001 上证等)单列。

---

## 3. 交易时段 — `arad/session.py`（脚手架提供）

```python
class SessionPhase(str, Enum):
    CLOSED="closed"; PRE_OPEN="pre_open"      # 09:15-09:25 集合竞价
    AUCTION="auction"                          # 09:25-09:30 静默
    MORNING="morning"                          # 09:30-11:30
    LUNCH="lunch"                              # 11:30-13:00
    AFTERNOON="afternoon"                      # 13:00-15:00
    POST="post"                                # 15:00 之后
class TradingCalendar:
    def phase(self, now: datetime | None = None) -> SessionPhase
    def is_trading_day(self, d: date) -> bool          # 周末 + 内置节假日表
    def is_open(self, now=None) -> bool                # MORNING/AFTERNOON 才算连续竞价
```

---

## 4. 引擎与状态 — `arad/engine.py`（脚手架提供骨架）

```python
@dataclass
class EngineState:
    quotes: dict[str, Quote]                       # 最新快照
    history: dict[str, deque[tuple[float,float,float]]]  # code -> deque[(ts_epoch, price, cum_volume_lots)]
    first_seen: dict[str, Quote]
    last_alert: dict[str, float]                   # key -> ts_epoch (去重)
    last_price: dict[str, float]
    universe: dict[str, Quote]
    day_open: dict[str, float]
    session: SessionPhase
    stats: dict[str, int]

    def window(self, code: str, seconds: float, now_epoch: float) -> list[tuple[float,float,float]]
    def price_change(self, code: str, seconds: float, now_epoch: float) -> float | None  # 百分比
    def volume_delta(self, code: str, seconds: float, now_epoch: float) -> float         # 手
```

**engine 会做的事（规则作者不用管）**：按 `config.settings.poll` 轮询，把每轮快照写入
`state.quotes` 和 `state.history`（history 每条保留 ≥30 分钟，deque maxlen 计算方式见 settings），
维护 `state.first_seen/last_price/day_open`，**不负责去重**（去重由 `AlertBus` 统一做）。

---

## 5. 规则模块 — `arad/rules/*.py`

每个规则文件导出一个 `RULE` 变量（实例）或 `build(cfg) -> Rule`，并提供一个工厂：

```python
def build(cfg: dict) -> Rule: ...
RULE = build({})   # 用默认配置
```

规则返回的 `Alert.key` 必须形如 `f"{code}:{kind.value}:{bucket}"`，
其中 `bucket` 是把时间按规则自己的冷却粒度取整后的整数，例如
`bucket = int(now_epoch // cooldown_seconds)`。**同 key 同冷却窗口只触发一次。**

**但分桶 key 不是唯一的去重手段**——规则对"状态类"信号必须自己做**状态跃迁**判定，
因为分桶 key 每隔一个冷却窗口就会变，会让一个持续存在的状态被反复重报。判定口径：

* **事件型**（急拉、放量、炸板、首次封板）：发生即报，用分桶 key + `AlertBus` 去重；
* **状态型**（连续封板、持续触板）：**只在跃迁时报一次**（如 `away→sealed`），
  状态不变时不产出任何告警；回到 `away` 后才算重新武装。

`limit_board` 即为后者的参考实现（见 `rules/limit_board.py` 的 `_check_side` 状态机，
状态按自然日清空）。否则一只票封板两小时会每 5 分钟刷一条完全相同的告警。

### 规则清单与归属（避免重复实现）
| 文件 | 规则 | 负责 agent |
|---|---|---|
| `rules/tick_surge.py` | 急拉 SURGE / 急跌 PLUNGE（窗口涨幅、加速度、VWAP 上方确认、量能配合） | R1 |
| `rules/limit_board.py` | 涨停触板/封板/炸板 LIMIT_UP、跌停 LIMIT_DOWN、连板高度 | R2 |
| `rules/volume_burst.py` | 放量异动 VOLUME_BURST（量比突增、分时量能脉冲、对比同时段） | R3 |
| `rules/unusual.py` | 高开低走/低开高走/巨震/快速回封/尾盘异动 UNUSUAL | R4 |

---

## 6. 通知模块 — `arad/notifiers/*.py`

每个 notifier 提供 `build(cfg: dict) -> Notifier`。
内置：`console.py`（脚手架提供，带颜色）、`file_jsonl.py`、`webhook.py`、
`serverchan.py`、`dingtalk.py`、`feishu.py`、`windows_toast.py`。
**所有网络通知必须：(1) 超时 5s；(2) 失败只记日志绝不抛异常打断引擎；(3) 支持 dry_run 只打印。**

---

## 7. 服务端 / 前端 — `arad/server/`

纯标准库 `http.server.ThreadingHTTPServer`，端口取 `config.settings.web.port`（默认 8899），
绑定 `127.0.0.1`。接口：

| 方法 | 路径 | 返回 |
|---|---|---|
| GET | `/` | 内置单文件 HTML 看板（`server/dashboard.html`，自包含 CSS/JS，无外链） |
| GET | `/api/status` | `{"phase","session","uptime_s","universe":int,"alerts_total":int,"sources":[...],"last_poll_ms":int,"poll_count":int}` |
| GET | `/api/quotes?limit=30&sort=` | 涨跌幅榜/异动榜 `{"items":[{code,name,price,pct,speed_1m,speed_5m,volume_ratio,amount,turnover,board}], "ts":..., "sort":..., "sort_key":...}` |
| GET | `/api/alerts?limit=100&kind=` | 最近告警（倒序） |
| GET | `/api/watchlist` | 自选股实时行情 |
| GET | `/api/health` | 存活探针 `{"ok":true,"ts":...,"service":"arad"}` |
| GET | `/api/stream` | **SSE**，`text/event-stream`，事件名透传（`alert` / `tick` / `phase`），每 2s 推一次心跳+榜单 |
| POST | `/api/ack` | 标记已读（幂等）。`key` 必填，取自 JSON body、query 或表单；缺失返回 400 |

JSON 一律 `ensure_ascii=False`、`Content-Type: application/json; charset=utf-8`。
SSE 必须支持多客户端并发且客户端断开后清理。

**`sort` 取值（已冻结）**——`store._SORTS` 只认下列键，传其他值会**静默退化为 `speed`**：

| 引擎键 | 前端列名别名 | 含义 |
|---|---|---|
| `speed` | `speed_1m` | 1 分钟涨速 |
| `speed5` | `speed_5m` | 5 分钟涨速 |
| `pct` | `amplitude` | 涨跌幅 |
| `up` / `down` | — | 涨幅榜 / 跌幅榜 |
| `amount` | `turnover` | 成交额 |
| `volume_ratio` | — | 量比 |

web 层负责别名映射，**绝不把引擎不认识的键透传下去**；响应同时回 `sort`（原始请求）
与 `sort_key`（实际生效键）便于排查。前端表头用的是客户端排序，两套口径都可用。

---

## 8. 配置 — `config/settings.yaml`（脚手架提供，字段名冻结）

见文件内注释。规则配置在 `rules:` 下按规则名分节，每个规则读自己那一节。
读取统一走 `arad/config.py`：
```python
@dataclass class Settings: ...          # 强类型
def load_settings(path=None) -> Settings        # 缺失字段用默认值补齐
def load_watchlist(path=None) -> list[str]      # 返回6位代码
def source_cfg(name: str) -> dict
```
**任何模块都不许自己 yaml.safe_load，一律用 `arad.config`。**

---

## 9. 测试替身 — `tests/fakes.py`（脚手架提供）

```python
class FakeSource:            # 实现 Source 协议
    def __init__(self, quotes: list[Quote] | None = None)
    def universe(self) -> list[Quote]
    def snapshots(self, codes) -> list[Quote]
    def health(self) -> dict
    def push(self, quotes: list[Quote]) -> None   # 测试里推进行情
def make_quote(code="600000", name="测试股", price=10.0, prev_close=10.0, **kw) -> Quote
class RecordingNotifier:     # 记录收到的 Alert，供断言
    def send(self, alert) -> bool
    def send_digest(self, alerts) -> bool
def load_raw(name: str) -> bytes      # 读 fixtures/raw/<name>
```

---

## 10. 并行开发纪律
1. **只改自己负责的文件**，绝不改他人文件（冲突由主 agent 合并）。
2. import 时不要做 I/O；不要新增第三方依赖。
3. 写完立刻跑 `python -m pytest -q tests/test_<你的模块>.py` 并确保通过。
4. 若发现本契约有错，**不要擅自改**，在 `docs/NOTES_<你的名字>.md` 里记一条，主 agent 统一裁决。
