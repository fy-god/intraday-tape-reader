# NOTES — 通知层（notifiers/*）

作者：通知层 agent（NOTIFY）。范围：`src/arad/notifiers/{console,file_jsonl,webhook,serverchan,dingtalk,feishu,windows_toast}.py`
+ `tests/test_notifiers.py`。**未修改任何他人文件**（`notifiers/__init__.py` 保持原样 `"""package."""`）。

测试：`python -m pytest -q tests/test_notifiers.py` → **113 passed**（离线，无真实网络/无真实弹窗）。

---

## A. 契约问题与偏差（提交主 agent 裁决，我未擅自改契约文件）

### A1【重要】引擎从不调用 `send_digest()`，`notify.console.mode: "digest"` 实际上是死配置
`arad/engine.py::Engine::_dispatch()`（第 510-515 行）只有：

```python
def _dispatch(self, alert: Alert) -> None:
    for n in self.notifiers:
        try:
            n.send(alert)
        except Exception as exc:
            ...
```

**只调 `send()`，全文没有任何 `send_digest` 调用点**。而 `settings.yaml` 的 `notify.console.mode: "digest"`
语义是"同一轮多条时合并成一条"。如果照字面实现（`send()` 时把告警缓存进 list、等 `send_digest()` 统一输出），
那么在当前引擎下 **console 会永远不打印任何东西**——这是静默失效，比报错更危险。

我的处理：`console.send()` **始终立即输出**（在告警旁注明原因）；`send_digest()` 保留完整语义
（`mode=digest` 时把这一批渲染成合并表格，`mode=each` 时逐条打印）。这样两种调用方都正确。
对应测试：`test_console_digest_mode_sends_immediately`。

> 建议（主 agent 裁决）：要么让引擎改为批量调用 `send_digest(fresh)`，要么把 `mode` 默认改成 `each`
> 并在 settings 注释里说明 digest 需引擎配合。**注意 `send_digest` 的语义"这批告警合并成一条消息"
> 与引擎逐条 `send()` 的调用方式存在结构性不一致**，其余 6 个通知器的 `send_digest` 同样不会被引擎触发。

### A2【契约小误】`notify.file` 的模块名与 NOTIFIER_NAME
契约第 6 节列出的内置通知器文件名是 `file_jsonl.py`，但 `settings.yaml` 里该节叫 `file`，
`engine.NOTIFIER_MAP` 也把 `"file" -> "file_jsonl"`。我按**文件名**导出 `NOTIFIER_NAME = "file_jsonl"`，
同时把实例 `name` 默认设为 `"file"`（与配置节/`enabled` 列表一致，日志更直观）。这不冲突，仅记录。

### A3【契约未定义】`dry_run=True` 且通知器未配置时的返回值语义
契约只说"dry_run 只打印"。我统一定义为：**dry_run 下即使 `url`/`sendkey`/`webhook` 为空也返回 `True`**
（因为"没有真发"是预期行为，不算失败）；非 dry_run 且凭据为空才返回 `False`。
若主 agent 认为应一律 `False`，改动点在各 `_post_*` 的 dry_run 早返回分支。

### A4【契约未定义】`min_severity` 命中时的返回值
契约/任务书要求"直接返回 True（视为跳过，不算失败）"——已实现。`enabled=False` 时同样返回 `True`。

### A5【观察】`windows_toast` 的 `enabled` 默认与 `min_severity: 3`
`settings.yaml` 里 `windows_toast.min_severity: 3`，而多数规则默认 `severity: 2`
（`tick_surge.severity: 2`、`volume_burst.severity: 1`、`unusual.severity: 1`）。
即**默认配置下 Windows Toast 只会收到 `urgent_multiple` 触发的 severity=3 告警**。
这是配置意图（托盘通知很吵，只推最紧急的），不是 bug，但首次联调时容易误判"通知器没工作"，特此记录。

### A6【契约歧义】`Alert.severity` 缺省与 `severity_of()` 的降级值
`Alert.severity` 默认 2。我实现 `webhook.severity_of()` 在**取不到 severity 属性**时返回 `1`（最低级别，
保守不丢弃）；`Alert` 本身永远有该属性，故实际业务路径不受影响。仅当传入非 Alert 对象（如测试替身）时可见。

---

## B. 实现要点（供主 agent 与后续 agent 参考）

### B1 共用工具集中在 `webhook.py`（避免重复造轮子）
其余 6 个通知器都从 `webhook.py` 导入，**没有自建 HTTP 逻辑**：

| 符号 | 作用 |
|---|---|
| `default_poster(url, data, headers, timeout) -> (status, body)` | 标准库 POST；异常（含非法 URL）一律吞掉返回 `(0, "")` |
| `post_with_retry(...)` | **只在拿不到响应时重试**（异常/0/4xx/5xx），共 2 次尝试；业务码不重试 |
| `format_url(tpl, values)` | URL 占位符替换，值一律 `quote(..., safe="")`（连 `/` 也转义） |
| `resolve_dry_run(cfg)` | `cfg["dry_run"]` 优先，缺失回落全局 `app.dry_run`；settings 读失败 → `True`（安全优先） |
| `severity_of` / `kind_label` / `alert_values` | severity 容错读取 / 中文类型标签 / URL 模板值（`to_dict()` 坏掉时降级为最小字段集） |

重试计数是硬要求：**失败重试 1 次 = `poster` 恰好被调用 2 次**（测试 `test_poster_exception_returns_false_without_raising` 断言 `n_calls == 2`）。

### B2 钉钉加签（已独立验证）
```python
string_to_sign = f"{timestamp}\n{secret}"
sign = base64(hmac_sha1(key=secret, msg=string_to_sign))   # 再 quote_plus 拼 URL
```
`timestamp` 用**毫秒**（`int(time.time() * 1000)`），无 `secret` 时**完全不加签**（`timestamp`/`sign` 都不出现）。
构造函数注入 `clock` 便于测试取固定时间戳。

**双重独立验证已通过**：
1. `tests/test_notifiers.py` 里手写 RFC2104 HMAC-SHA1（`_hmac_sha1_rfc2104`，不用 `hmac` 模块）重算一致；
2. 用 **PowerShell/.NET `System.Security.Cryptography.HMACSHA1`** 独立算出
   `secret=SECtestsecret...`, `ts=1757913600123` → `Xu6d2kY7Jak5TI+mFEiXSn1/e/U=`（urlencode 后 `Xu6d2kY7Jak5TI%2BmFEiXSn1%2Fe%2FU%3D`），
   与本实现逐字节一致，已固化为回归向量 `DOTNET_B64` / `DOTNET_URLENCODED`。
   ⚠️ base64 里的 `+` `/` `=` **必须** urlencode，否则真实钉钉验签会失败（已单测 `test_dingtalk_sign_is_urlencoded`）。

### B3 windows_toast：真实可用性已验证（不是"写了就算"）
- 写临时 `.ps1`（**`encoding='utf-8-sig'`**，PowerShell 才能正确读中文）→ `subprocess.run([...,"-File",path], shell=False, timeout=8)`。
  用 `-File` 而非 `-Command`，**彻底规避引号转义**问题。
- PS 侧所有字符串用单引号字面量（`ps_quote()` 内部 `'` 翻倍），XML 文本节点单独做 XML 转义（`_xml_escape()`）。
- 优先级：WinRT `ToastNotificationManager` → 失败降级 `msg *` → 再失败返回 `False` 并记日志。**未新增任何依赖**。
- 临时脚本目录优先项目内 `data/`（遵守契约"不写项目外路径"），不可用时回退系统临时目录，用完即删。
- **实机验证**：在本机真实执行过一次 Toast，返回码 0、stderr 为空（走的是 WinRT 路径，不是降级）；
  另外用 PowerShell AST Parser 校验生成脚本语法合法、用 .NET `XmlDocument` 校验 toast XML 合法且中文/单引号/`<&>` 正确往返。

### B4 console 颜色
VT 用 `os.system("")` 启用（Windows 安全，已实测）；**stdout 非 TTY（重定向/管道/pytest）时自动降级为无颜色**，
避免转义码污染日志文件。急拉=红（severity≥3 用 `BOLD+BRIGHT_RED` 亮红）、急跌=绿、涨停=黄底、跌停=青、其他=白。
`_VT_ENABLED` 全局缓存一次；`os.system('')` 在 try/except 内。

### B5 file_jsonl 线程安全
按**路径**（`os.path.normcase` 归一化）共享一把 `threading.Lock`，同路径多实例也互斥。
100 条 × 10 线程并发写测试通过（每行都是完整合法 JSON、无丢失无重复）。
写入用 `ensure_ascii=False`，已断言原始字节含 UTF-8 中文且**不含 `\uXXXX` 转义**。

---

## C. 契约确认无误的部分
- `Alert.to_dict()` / `one_line()` 字段与第 1 节完全一致，直接复用，未做任何字段改写。
- 各通知器 `min_severity` 均能从 `notify.<name>.min_severity` 正确读取（含 `windows_toast` 无 `timeout` 字段的情况）。
- `build(cfg)` 全部接受 `engine.build_notifiers()` 传入的 cfg（它已 `setdefault("dry_run", settings.dry_run)`），
  端到端联调通过：7 个通知器全部装载成功、`send()`/`send_digest()` 全返回 `True`。
