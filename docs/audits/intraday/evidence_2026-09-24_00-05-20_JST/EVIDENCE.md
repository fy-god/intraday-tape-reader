# EVIDENCE —— 2026-09-24 05:52:56 JST

对应报告：`docs/audits/intraday/2026-09-24_05-52-56_JST.md`

**轮审范围**：云端任务书 `2026-09-24_00-05-20_JST_AGENT_TASK.md`
§WP01（身份测试 + M6）、§WP02（caller-owned role）、
§WP03（`call_detailed` 合同校验）、§WP05（coverage alias gate）。

**reviewed_source_sha**：`bd711c4a5a5a2a636717c7f883d2f9c5976aeabc`
（本轮开始 HEAD；产品代码改动见本轮提交）。

---

## 1. 结论速览

| 条目 | 状态 | 证据文件 |
|---|---|---|
| 7 个名称唯一指数 phantom missing（**我上轮引入的回归**） | **已确认错误 → 已修复** | `identity_matrix_red.log` / `identity_matrix_green.log` |
| `call_detailed` 错类型不 failover | **已确认错误 → 已修复** | `call_detailed_type_red.log` / `call_detailed_type_green.log` |
| route 静默分叉 | **已确认错误 → 已修复** | `call_detailed_route_green.log` |
| 任务书 M6 定义在修后失效 | **下调**（no-op，非 gate 无牙） | `m6_literal_noop.log` |
| WP05 scan 范围误报 `capabilities.py:892` | **已修正**并加自检 | `coverage_alias_gate.log` |
| 用户真正拿到可用预警 | **进度 0**（诚实） | 本文件 §4 |

---

## 2. 复现步骤

```powershell
cd D:\ccc\ashare-radar
$env:PYTHONPATH='src'; $env:PYTHONIOENCODING='utf-8'

# 全量
python -m pytest -o addopts="" -q -p no:cacheprovider

# WP01 身份 gate（21 条）
python -m pytest tests/test_tencent_identity_matrix.py -o addopts="" -q -p no:cacheprovider

# WP03 合同（7 条）
python -m pytest tests/test_source_manager_contract.py -o addopts="" -q -p no:cacheprovider

# 门禁
python tools/check_bom.py
Get-ChildItem tools\check_*.py | ForEach-Object { python $_.FullName }
node tools/dash_render_check.js
python -m arad.cli selftest
```

> ⚠ `pyproject.toml` 的 `addopts="-q"` 会让 `-q` 叠加 —— 一律显式
> `-o addopts=""`。另：`arad` **未** pip 安装，必须 `PYTHONPATH=src`。

**回退验牙脚本**（临时探针，不在仓库内，逻辑已完整记录在报告 §7）：
注入器都是"改源码 → 跑测试 → 断言目标测试红 **且** 阳性对照绿 → 恢复并校验 sha256"。

---

## 3. 关键数字

| 指标 | 值 |
|---|---|
| 全量 pytest | **1919 passed / 0 failed** |
| 新增测试 | identity 21 + contract 7 |
| `check_*.py` | **8/8 exit 0** |
| `check_bom.py` | exit 0 |
| `dash_render_check.js` | exit 0 |
| `arad.cli selftest` | 68 告警 / 6 类型 / exit 0 |
| 身份矩阵（纯函数层） | **15/15 三轴一致** |
| 身份矩阵（**类级生产路径**） | **15/15 OK**，默认 5 指数 coverage = 1.0 |
| 回退验牙 | 7 条全部**行为性**，**0 结构性** |

---

## 4. 诚实边界：修 bug ≠ 用户拿到预警

本轮修的两个 P1 都是**可靠性**缺陷（身份错位、源切换失效）。
它们让系统"更不容易错"，但**没有让用户多收到一条有用预警**：

- Precision / Recall / 漏报率 / 交易收益：**仍 unavailable**。
- **本轮没有任何编造的数字。**
- `spirit_*` 仍 `enabled: false`。
- 部署配置 / Windows 计划任务 / 行情服务：**未改动**（硬性边界）。

详见 `not_run/BLOCKED.md`（研究阶段物化文件的阻塞原因）。

---

## 5. 已知局限（本轮证据的）

1. **夹具非真实 provider 响应**：身份矩阵用的是按字段位构造的
   `v_sym="..."` 文本，`name` 字段是**我写的**。真实腾讯返回的名称
   （如"中证红利指数"的确切写法）未在此验证 —— 但**修法不依赖名称**
   （`name` 在身份判定中 0 引用），所以这个局限不影响结论。
2. **`is_index_role` 的 `idx_set` 回退分支**（`index_role=False` 但
   `symbol in idx_set`）覆盖较弱：生产 `snapshots_detailed` 目前
   总是整批同 route，混合调用路径未被真实触发。
3. **未见真实盘中回归**：以上全部是离线单测/门禁。
