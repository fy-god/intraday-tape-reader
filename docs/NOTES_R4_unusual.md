# NOTES — R4 规则作者（volume_burst / unusual）

> 按 `docs/DATA_CONTRACT.md` 第 10.4 条：发现契约问题**不擅自改**，在此记录，由主 agent 统一裁决。
> 本文件不修改任何他人文件。

## 交付物

| 文件 | 说明 |
|---|---|
| `src/arad/rules/volume_burst.py` | 放量异动 `AlertKind.VOLUME_BURST`，`build(cfg)` + `RULE` |
| `src/arad/rules/unusual.py` | 形态类异动 `AlertKind.UNUSUAL`，5 个子 pattern，`build(cfg)` + `RULE` |
| `tests/test_rule_volume_burst.py` | 16 个用例（含性能冒烟） |
| `tests/test_rule_unusual.py` | 22 个用例（含性能冒烟） |

`python -m pytest -q tests/test_rule_volume_burst.py tests/test_rule_unusual.py` → **38 passed**。

---

## 契约问题 1（**阻断级**）：`Alert.to_dict()` 无法处理字符串型 metric

`arad/models.py` 第 241 行：

```python
"metrics": {k: round(float(v), 4) for k, v in self.metrics.items()},
```

对**每一个** metric 无条件 `float(v)`。但本任务的 `rules.unusual` 契约明确要求
「每条告警在 metrics 里带 `pattern` 标识」（字符串，取值为
`high_open_fade` / `low_open_rise` / `wide_amplitude` / `late_surge` / `reseal`）。

实测：

```python
a.metrics            # {'pattern': 'high_open_fade', 'pct': 1.0, ...}
a.to_dict()          # ValueError: could not convert string to float: 'high_open_fade'
```

**影响面**（`to_dict()` 的下游全部会抛异常，且都是真实代码路径）：

| 调用方 | 位置 |
|---|---|
| JSONL 落盘 | `notifiers/file_jsonl.py:84` |
| Webhook POST / 模板取值 | `notifiers/webhook.py:98, 206, 249` |
| 摘要推送 | `notifiers/webhook.py:268` |
| Web 看板 `/api/alerts` | `server/`（同一 `to_dict()`） |

**这不是我一个人踩到的坑**：`rules/tick_surge.py:284` 也写了
`a.metrics["hit_windows"] = hits`（list 类型），同样会在 `to_dict()` 炸掉。
即「metrics 里放非 float」是本项目多个规则的自然需求，而 `to_dict()` 目前不支持。

**建议裁决**（三选一，我倾向 1）：

1. `to_dict()` 改为「能转 float 就转，否则原样保留」：
   ```python
   def _m(v):
       try:
           return round(float(v), 4)
       except (TypeError, ValueError):
           return v
   "metrics": {k: _m(v) for k, v in self.metrics.items()},
   ```
   向后兼容，且前端仍能拿到 `pattern` 字符串用于筛选。
2. 强制约定 metrics 只放数值，字符串元数据改放 `Alert` 的新字段 / `title` 前缀。
   —— 但这会与本次任务书「metrics 里带 `pattern`」的要求冲突，需任务书同步修改。
3. 各规则自行保证 metrics 全数值，pattern 挤进 key / title。
   —— 最脏，前端无法干净地按 pattern 筛选，不推荐。

**我的现状**：按任务书要求把 `pattern` 放进了 `metrics`（字符串），
因此**任何走 `to_dict()` 的通知器/接口在收到 unusual 告警时会抛 `ValueError`**。
在裁决前我没有改 `models.py`（冻结文件）。若裁决结果不是方案 1，我改 `unusual.py`
的成本很低（一处 `_mk()` 内 metrics 组装）。

---

## 契约问题 2（提示级）：`Alert.metrics` 的类型标注与实际不符

`models.py:100` 标注 `metrics: dict[str, float]`，但任务书要求与
`tick_surge.py` 的实际用法都突破了该类型。建议放宽为 `dict[str, object]`
或在文档中显式声明「可以携带字符串标识」。

---

## 契约问题 3（提示级）：`config/settings.yaml` 的 `rules.volume_burst` 缺 `max_per_round`

任务书要求「默认 20，**若配置无此项则用 20**」。`settings.yaml` 的
`rules.volume_burst` 节确实没有 `max_per_round`（`tick_surge`/`limit_board` 有）。
已按任务书在 `build()` 的 `DEFAULTS` 里补 20。**未改 settings.yaml**（冻结文件），
仅在此记录，供主 agent 决定是否补进配置。

---

## 实现要点与主动决策（供主 agent 复核）

### volume_burst.py
- **速度倍数**（任务书核心）：`recent_per_min = volume_delta(W)/W*60`；
  `avg_per_min = volume_lots/max(elapsed,1)*60`；`ratio = recent/avg`。
  `avg_per_min <= 0` 时跳过；`W <= 0` 时不计算近端速度（视为 0）→ 不达标。
- **量比缺失（==0）**按任务书「跳过该项」处理，而非判负；title 里量比显示为 `-`。
- **`max_per_round<=0` 视为不限量**（与「配置为 0 = 关闭」的整体语义一致）。
- 排序按 `ratio` 降序，**同值时用 code 升序兜底**，保证输出确定性（便于测试与去重）。
- severity：`ratio >= 2*speed_multiple` 或 `vr >= 2*vr_threshold` → `max(configured, 2)`；
  **不降级**配置里更高的 severity。

### unusual.py
- **reseal 状态机**（任务书要求「无法判定则不报」）：
  1. 先判「快照现价是否在涨停价上」，**不在则直接早退**（性能关键，1000 只正常股票 2.7ms）；
  2. 再查 `state.history`，窗口 `[now-reseal_seconds, now]` 内：
     从未触及涨停 → **不报**；曾触及但**从未跌离涨停 `pullback_pct`(0.5%) 以上** → **不报**
     （那是一直封板，不是回封）；有炸板证据且现在封回 → 报，severity 3。
  3. `reseal_seconds<=0` 或 `limit_up_price<=0` → 不报，不崩。
- **多 pattern 并存**：key 为 `f"{code}:unusual:{pattern}:{bucket}"`，同票同轮可出多条
  不同 pattern（实测 600009 可同时命中 `reseal` + `late_surge`），**同 pattern 每轮至多一条**。
- **排序**：severity 降序 → `|pct|` 降序 → code 升序（兜底，保证确定性）。
- **边界**：`price<=0`/`prev_close<=0`/`is_suspended` 直接跳过；`open<=0` 时高开/低开两项
  自动跳过但巨震仍按昨收判定（有专门用例覆盖）。
- `late_surge` 归入连续竞价时段（`only_continuous` 同样约束它），任务书第 10 条要求一致。
- 尾盘窗口秒数做成可配（`late_session_window_seconds`，默认 300 = 近 5 分钟），
  任务书写死了 300s，此处只是让测试/调参不必改代码。

### 两规则共有
- 配置读取用 `ctx.cfg` 优先、实例 `self.cfg` 兜底（`ctx.cfg` 为 `{}` 时仍可用默认值），
  `None` 不覆盖默认值。
- `RuleContext.cfg` 与 `self.cfg` 均支持；`build(cfg)` 用 `DEFAULTS` 补齐后存进实例。
- 仅标准库；`evaluate` 内**无任何 I/O**；import 时无 I/O。

---

## 性能实测（1000 只股票、单次 evaluate，取 5 次最优）

| 规则 | 耗时 | 预算 | 结果 |
|---|---|---|---|
| `volume_burst`（全部命中放量） | **8.8 ms** | 200 ms | ✅ |
| `volume_burst`（无 history） | < 1 ms | 200 ms | ✅ |
| `unusual`（1000 只全在涨停价上、走满 reseal 状态机，最坏情况） | 2.9 ms | 200 ms | ✅ |
| `unusual`（1000 只普通股票） | **2.7 ms** | 200 ms | ✅ |

---

## 未做（等主 agent 裁决，避免与任务书冲突）

1. 未改 `models.py` 的 `to_dict()`（契约问题 1）。
2. 未改 `config/settings.yaml`（契约问题 3）。
3. 未在 `rules/__init__.py` 注册规则 —— 该文件当前只有一行 docstring，
   任务书要求「只创建这 4 个文件」，注册留给主 agent 统一做。
