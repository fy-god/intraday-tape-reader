# NOTES_web — 实时看板（`arad/server/web.py` + `dashboard.html`）

作者：Web agent ｜ 契约：`docs/DATA_CONTRACT.md` 第 7 节

## 交付
| 文件 | 说明 |
|---|---|
| `src/arad/server/web.py` | `create_server()` / `serve()` / `NullStore` / 全部路由 + SSE |
| `src/arad/server/dashboard.html` | 单文件自包含看板（内联 CSS/JS，零外链） |
| `tests/test_server_web.py` | 44 个离线测试（真实本地 HTTP + SSE 断开清理断言） |

`src/arad/server/__init__.py` 保持原样未改。

---

## 发现的契约问题（**请主 agent 裁决，我没有擅自改契约**）

### 1. `top_quotes` 的 `sort` 取值，契约第 7 节未定义，且与实现不一致（**重要**）
- 契约第 7 节只写 `/api/quotes?limit=30`，**没有约定 `sort` 的合法取值**；
  但给 Web agent 的架构说明里写的是 `sort: str = "speed"`。
- 真实实现 `src/arad/store.py` 的 `_SORTS` 只认这 7 个键：
  `speed` / `speed5` / `pct` / `up` / `down` / `amount` / `volume_ratio`。
- **风险**：前端表头很自然地会传列名 `speed_1m` / `speed_5m` / `turnover` / `amplitude`。
  `store.top_quotes()` 对不认识的键**静默退化成 `speed`**（`key = sort if sort in _SORTS else "speed"`），
  于是「点了排序但没排」——这是最难查的一类 bug。
- **我的处理**：`web.py` 里加了一层显式别名映射 `resolve_sort()`，
  **绝不把引擎不认识的键透传下去**：
  `speed_1m→speed`、`speed_5m→speed5`、`turnover→amount`、`amplitude→pct`、`price→pct`。
  未知值一律回落 `speed`，并且响应里同时返回 `sort`（前端请求的）和 `sort_key`（实际下发的引擎键）。
  测试 `test_resolve_sort_never_passes_unknown_key_to_engine` 会 import 真实的
  `arad.store._SORTS` 做交叉断言——**引擎侧改键名会让这个测试失败**，从而暴露漂移。
- **建议**：把 `sort` 的合法取值写进契约第 7 节；或让 `store.top_quotes()` 对未知键抛
  `ValueError` 而不是静默退化（我这边已能容错，抛错也可以）。

### 2. `AlertStore.ack()` 的语义
- 契约第 7 节写「POST `/api/ack`：可选，前端标记已读（幂等，body 忽略）」。
- 真实实现 `ack(key)` 是**按 key 记入 `_acked` 集合**，`recent_alerts()` 会给每条附 `acked` 布尔。
  也就是说 **body 不能忽略**（需要 `key`），与契约「body 忽略」不符。
- **我的处理**：兼容三种来源——JSON body 的 `key`、query 的 `key`、表单 `key`；
  都没有则返回 400 `{"ok":false,"error":"missing key"}`；成功返回 `{"ok":true,"key":...,"acked":bool}`。
  前端已消费返回的 `acked` 字段。

### 3. SSE 事件名不止 `alert` / `tick`
- 契约第 7 节说事件名是 `alert` / `tick`；真实引擎还会 `broadcast("phase", {"phase":...})`
  （见 `engine.py:409`）。
- **我的处理**：SSE 是**透传**的——队列里来什么事件名就发什么（`event: phase` 也会正常发出），
  所以引擎加新事件类型不需要改 web 层。前端只处理 `tick` / `alert`，其余事件在
  `EventSource` 里自然被忽略。已加测试 `test_engine_style_phase_broadcast_is_tolerated`。

### 4. 契约缺 `/api/health`
- 任务书要求 `/api/health`，契约第 7 节的表格里没有它。已按任务书实现。

### 5. `status()` 字段比契约多
- 契约第 7 节列了 8 个字段；真实 `store.status()` 还返回
  `session_desc` / `is_open` / `by_kind` / `subscribers` / `notes` / `replay` / `ts`。
- **我的处理**：`/api/status` 原样透传（多字段无害），前端优先用 `session_desc`
  （引擎给的完整描述含「下次开盘时间」）并在 `is_open` 时加 ✓；
  用 `by_kind` 在筛选按钮上显示各类型条数。

---

## 实现要点 / 已知取舍

### SSE 断开检测（**关键实现细节**）
只靠「写入时抛 `BrokenPipeError`」**不足以**发现客户端断开：小 chunk 会先落进
socket 发送缓冲，服务端要等到内核返回 RST 才报错，而浏览器关页后通常没有后续写入。
结果是订阅队列迟迟不释放（测试里表现为 `unsubscribe` 永不调用）。

因此 `_peer_gone()` 每个循环做一次 **`recv(1, MSG_PEEK)` 非阻塞探测**：
- `BlockingIOError` → 连接仍然活着（正常情况）
- `ECONNRESET` / `b""` → 对端已断开 → 结束生成器，`finally` 里立即 `unsubscribe`

配合 `daemon_threads = True`、`Cache-Control: no-cache`、`X-Accel-Buffering: no`，
以及 `RadarHTTPServer.stop_event`（`shutdown()` 时通知 SSE 线程主动收尾，
不等 3600s 超时），实测 shutdown 后 **250ms 内所有线程退出、端口释放**。

### SSE 超时
`cfg["sse_timeout"]` 默认 3600 秒。到期发 `event: bye` 后**正常结束**该连接，
浏览器 `EventSource` 会按 `retry: 3000` 自动重连——这既防止长连接无限堆积，
又不丢数据。

### 前端去重
SSE 断线重连会重放，前端用 `key` 字段去重（`S.seen`），并在页面启动/重新可见时
调 `/api/alerts` 补齐断线期间错过的告警（同样靠 key 去重，重复拉取安全）。

### `NullStore`
无引擎时单机调试用；额外提供 `publish(event, data)` / `subscribers` 方便手工造事件看 UI。

### 测试
- 全部用真实 `http.server` + `port=0`，`socket` 超时 5s，fixture teardown 必定
  `shutdown()` + `server_close()` + `join`。
- SSE 断开测试用**裸 socket + `SO_LINGER(1,0)`** 制造 RST。
  注意：`http.client` 在 `getresponse()` 后会把 `conn.sock` 置空，且
  `HTTPResponse` 内部 `sock.makefile()` 让 `socket._io_refs +1`，
  **只调 `sock.close()` 不会真正关闭 fd**（对端什么都收不到）——必须连 response 一起关。
  这是踩过的坑，测试里已封装成 `SSE.close_hard()`。
- 覆盖：全部路由 / limit clamp（quotes 200、alerts 1000）/ 404 vs 405 /
  异常转 500 JSON / SSE 事件·心跳·多客户端·RST·FIN·超时·shutdown /
  端口回收 / `NullStore` / 真实 `AlertStore` 端到端。

### 前端校验
`dashboard.html` 的 JS 用 `node --check` 过了语法校验——这一步真的抓到了一个
会导致**整个看板白屏**的多余括号（`renderSources` 里的三元表达式），已修。
建议后续把这一步纳入 CI（当前仓库没有前端构建链）。

### 契约问题反馈
以上第 1、2 点建议主 agent 统一裁决后更新 `docs/DATA_CONTRACT.md`。
