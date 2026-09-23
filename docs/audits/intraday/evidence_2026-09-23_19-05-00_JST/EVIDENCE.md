# 证据日志 — 2026-09-23 19:05:00 JST（WP01 v4 + 三条云端指认修复）

> ⚠ **上传格式说明**：原始是 `.log`，而 `.gitignore:25` 有 `*.log`。
> 我**没有修改 `.gitignore`**（那是配置变更，超出本轮边界），
> 改为把**原文**放进本 Markdown 一并提交。原始 `.log` 仅存在于本地工作树。

| 文件 | 内容 |
|---|---|
| `wp01_red.log` / `wp01_green.log` | WP01 回退验牙（行为性注入）：8 failed / 35 passed |
| `wp01_cross_source.log` | 三源跨源同义性探针（修正夹具后 5/5） |
| `state_collapse_control.log` | 五态塌缩阳性对照：注入后 **exit 1** |
| `wallclock_acceptance.log` | 墙钟门双向验收：抗离群 **且** 真变慢仍红 |
| `daemon_timing.log` | 第三处墙钟断言实测余量 **7.5e6 x**（判定**非**同类） |
| `ce_red.log` / `ce_green.log` | CE1/CE2 回退验牙：3 行为性 RED / 0 结构性 ERROR |
| `ce_repro.log` | 修后：两条假绿已消除 |
| `ce_repro_before_fix.log` | **探针自证**：修前输出云端引用的原句 |
| `t0_red.log` | P0 假红回退验牙：`assert 0 is None` 行为性失败 |
| `full_pytest_final{1,2,3}.log` | 全量 **1874 passed** ×3 |
| `gates.log` | check_* 8/8 + dash exit 0 + selftest exit 0 |

**回退验牙方法说明**：**不**用 `git checkout bf0b83b -- <file>`
（老版本没有新函数 → import 阶段 `ImportError` = **结构性 ERROR**，
只证明"函数不存在"，不证明断言有探测力；而且它会抹掉未提交的工作树改动）。
改为**保持 API 形状**、只把函数体换成审计**明文禁止**的实现，并**逐字节核对恢复**。
注入器必须用 `UTF8Encoding($false)` —— PowerShell 的 `Set-Content -Encoding utf8`
会加 BOM，加在 `.py` 首行直接 `SyntaxError`，那就又成了结构性 RED。
## ce_red.log

```text
.........F.F.F........................................................   [100%]
================================== FAILURES ===================================
E   AssertionError: 必须显式写出覆盖不足：会话期间 1/30 轮 transport 均为完整（无截断）
    assert '未全程' in '会话期间 1/30 轮 transport 均为完整（无截断）'
D:\ccc\ashare-radar\tests\test_health_consumer_contract.py:325: AssertionError: 必须显式写出覆盖不足：会话期间 1/30 轮 transport 均为完整（无截断）
E   AssertionError: 29 轮无证据不得被算成已测量：30
    assert 30 == 1
     +  where 30 = <built-in method get of dict object at 0x000002A30A2097C0>('freshness_measured_rounds', 30)
     +    where <built-in method get of dict object at 0x000002A30A2097C0> = {'measured': True, 'rounds': 30, 'states': {'unknown': 29, 'fresh': 1}, 'worst_state': 'fresh', ...}.get
D:\ccc\ashare-radar\tests\test_health_consumer_contract.py:380: AssertionError: 29 轮无证据不得被算成已测量：30
E   AssertionError: universe_transport 不得对 7/30 用全称句：会话期间 7/30 轮 transport 均为完整（无截断）
    assert '均为完整' not in '会话期间 7/30 轮...rt 均为完整（无截断）'
      
      '均为完整' is contained here:
        会话期间 7/30 轮 transport 均为完整（无截断）
      ?                       ++++
D:\ccc\ashare-radar\tests\test_health_consumer_contract.py:440: AssertionError: universe_transport 不得对 7/30 用全称句：会话期间 7/30 轮 transport 均为完整（无截断）
=========================== short test summary info ===========================
FAILED tests/test_health_consumer_contract.py::test_transport_positive_never_uses_evidenced_subset
FAILED tests/test_health_consumer_contract.py::test_freshness_unknown_records_are_not_evidence
FAILED tests/test_health_consumer_contract.py::test_transport_and_freshness_denominators_are_session_total
3 failed, 67 passed in 0.36s
```

## ce_green.log

```text
......................................................................   [100%]
70 passed in 0.36s
```

## ce_repro.log

```text
    universe_transport: level=ok
       detail=provider 声明传输完整（reported=None/None，分母已知）
    universe_freshness: level=ok
       detail=会话期间**未全程取得新鲜度证据**：1/30 轮有新鲜度记录（states={'unknown': 29, 'fresh': 1}，最坏 fresh）—— 跳过不算失败，但**不能**说'会话期间股票池新鲜'
    universe_scope: level=ok
       detail=会话级扫描范围证据**不足**（测到 0/30 轮）—— 跳过不算失败，但**不能**据此说'全程全市场扫描'
    生产端 rounds_total=30 freshness_measured_rounds=1 states={'unknown': 29, 'fresh': 1}

============================================================================
阳性对照（云端已跑出，我复核）：换成 stale / incomplete 必须变红
============================================================================
  阳性对照 CE2-stale
    healthy=False exit=1 fail=['universe_freshness'] warn=[]
    universe_transport: level=ok
       detail=transport 完整性**未测量**（无 transport_complete 字段 / 冷启动未声明 / 旧报告）—— 跳过不算失败，但也不代表完整
    universe_freshness: level=fail
       detail=会话期间活跃股票池曾陈旧（最坏 stale，最长 Nones；0 轮刷新未生效）
    universe_scope: level=ok
       detail=无会话级扫描范围记录（旧报告/未采集），无法判定（跳过不算失败）

  阳性对照 CE1-incomplete
    healthy=False exit=1 fail=['universe_transport'] warn=[]
    universe_transport: level=fail
       detail=会话期间有 1 轮 **provider 明确声明股票池被截断**（transport_complete=False，共测到 1 轮）——这些轮次对全市场事件是硬缺口，此期间的'没有告警'不能解释为'没有异动'
    universe_freshness: level=ok
       detail=会话期间股票池新鲜度**未测量**（states=None；缺年龄证据）—— 跳过不算失败，但也不代表新鲜
    universe_scope: level=ok
       detail=无会话级扫描范围记录（旧报告/未采集），无法判定（跳过不算失败）

============================================================================
判定（**用文本，不用 healthy**）
============================================================================
  ⚠ 我第一版判定写的是 `healthy is True and exit_code == 0` —— **错的**：
     '缺证据' 本来就不该判 fail，所以修完 healthy 仍为 True 是**正确的**。
     云端 §1.1/§1.2 的判据是**打印出来的肯定句**。

  CE1 修后仍说'会话期间均完整'? False
      -> 会话期间**未全程取得 transport 完整性证据**：1/30 轮测到且均完整（其余 29 轮无 transport 证据）—— 跳过不算失败，但**不能**说'会话期间无截断'
  CE2 修后仍肯定'会话期间股票池新鲜'? False
      -> 会话期间**未全程取得新鲜度证据**：1/30 轮有新鲜度记录（states={'unknown': 29, 'fresh': 1}，最坏 fresh）—— 跳过不算失败，但**不能**说'会话期间股票池新鲜'

  阳性对照 CE2-stale 确实变红: True
  阳性对照 CE1-incomplete 确实变红: True

  [√] CE1/CE2 假绿已消除，且阳性对照仍有牙
```

## ce_repro_before_fix.log

```text
    universe_transport: level=ok
       detail=provider 声明传输完整（reported=None/None，分母已知）
    universe_freshness: level=ok
       detail=会话期间股票池新鲜（30/30 轮，{'unknown': 29, 'fresh': 1}）
    universe_scope: level=ok
       detail=会话级扫描范围证据**不足**（测到 0/30 轮）—— 跳过不算失败，但**不能**据此说'全程全市场扫描'
    生产端 rounds_total=30 freshness_measured_rounds=30 states={'unknown': 29, 'fresh': 1}

============================================================================
阳性对照（云端已跑出，我复核）：换成 stale / incomplete 必须变红
============================================================================
  阳性对照 CE2-stale
    healthy=False exit=1 fail=['universe_freshness'] warn=[]
    universe_transport: level=ok
       detail=transport 完整性**未测量**（无 transport_complete 字段 / 冷启动未声明 / 旧报告）—— 跳过不算失败，但也不代表完整
    universe_freshness: level=fail
       detail=会话期间活跃股票池曾陈旧（最坏 stale，最长 Nones；0 轮刷新未生效）
    universe_scope: level=ok
       detail=无会话级扫描范围记录（旧报告/未采集），无法判定（跳过不算失败）

  阳性对照 CE1-incomplete
    healthy=False exit=1 fail=['universe_transport'] warn=[]
    universe_transport: level=fail
       detail=会话期间有 1 轮 **provider 明确声明股票池被截断**（transport_complete=False，共测到 1 轮）——这些轮次对全市场事件是硬缺口，此期间的'没有告警'不能解释为'没有异动'
    universe_freshness: level=ok
       detail=会话期间股票池新鲜度**未测量**（states=None；缺年龄证据）—— 跳过不算失败，但也不代表新鲜
    universe_scope: level=ok
       detail=无会话级扫描范围记录（旧报告/未采集），无法判定（跳过不算失败）

============================================================================
判定（**用文本，不用 healthy**）
============================================================================
  ⚠ 我第一版判定写的是 `healthy is True and exit_code == 0` —— **错的**：
     '缺证据' 本来就不该判 fail，所以修完 healthy 仍为 True 是**正确的**。
     云端 §1.1/§1.2 的判据是**打印出来的肯定句**。

  CE1 修后仍说'会话期间均完整'? True
      -> 会话期间 1/30 轮 transport 均为完整（无截断）
  CE2 修后仍肯定'会话期间股票池新鲜'? True
      -> 会话期间股票池新鲜（30/30 轮，{'unknown': 29, 'fresh': 1}）

  阳性对照 CE2-stale 确实变红: True
  阳性对照 CE1-incomplete 确实变红: True

  [X] CE1/CE2 假绿已消除，且阳性对照仍有牙
```

## t0_red.log

```text
..............F......................................................... [ 97%]
..                                                                       [100%]
================================== FAILURES ===================================
________ test_refresh_failed_but_pool_retained_is_unmeasured_not_zero _________
tests\test_health_consumer_contract.py:464: in test_refresh_failed_but_pool_retained_is_unmeasured_not_zero
    assert f(0, retained_pool=5563) is None
E   assert 0 is None
E    +  where 0 = <function _t0_universe_size at 0x00000221FAEC2DE0>(0, retained_pool=5563)
=========================== short test summary info ===========================
FAILED tests/test_health_consumer_contract.py::test_refresh_failed_but_pool_retained_is_unmeasured_not_zero
1 failed, 73 passed in 0.41s
```

## gates.log

```text
[PASS] check_audit_shots.py
[PASS] check_bom.py
[PASS] check_colors.py
[PASS] check_config_consumed.py
[PASS] check_config_wiring.py
[PASS] check_orphan_config.py
[PASS] check_readme_tools.py
[PASS] check_spirit_mapping.py
check_*: 8/8
dash_render_check: exit=0
=== 结论 =================================================================
[✓] 全链路正常：68 条告警，覆盖 6 种类型，全部剧本命中
selftest: exit=0
```

## full_pytest_final1.log（尾部）

```text
........................................................................ [ 96%]
........................................................................ [ 99%]
..                                                                       [100%]
1874 passed in 103.26s (0:01:43)
```