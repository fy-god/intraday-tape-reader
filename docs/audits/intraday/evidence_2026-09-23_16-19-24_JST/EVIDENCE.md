# 证据日志 — 2026-09-23 16:25:42 JST（WP01 Universe Evidence Completeness v2）

> ⚠ **上传格式说明**：原始文件是 `.log`，而本仓库 `.gitignore:25` 有 `*.log`，
> 因此**无法作为 `.log` 提交**。我**没有修改 `.gitignore`**（那是配置变更，超出本轮边界），
> 改为把**原文**放进本 Markdown 一并提交。原始 `.log` 仅存在于本地工作树。

| 文件 | 结果 |
|---|---|
| `contract_red.log` | 20 failed / 45 passed（pre-fix blob `eb12ef35d5d363705279a31bf8973cb8822b37a9`，逐字节核实） |
| `contract_green.log` | 65 passed |
| `contract_rollback.log` | 65 passed（恢复后 hash `b2f791f0f4c8fb91b1a41a5382bee806679a6e77` = HEAD） |
| `full_pytest_green.log` | 1830 passed |
| `gates.log` | check_* 8/8 + dash_render_check exit 0 + selftest exit 0 |

**红测方法说明**：用 `git checkout bf0b83bf212f169c720ca8a1c5108405c18a9712 -- tools/live_session.py`
把源码换回 pre-fix，并用 `git hash-object` 核对就位 blob 与目标 blob **逐字节一致**，
才跑 RED。恢复用 `git checkout HEAD -- tools/live_session.py`。
（`git show <sha>:path > file` 在本机 PowerShell 会写 UTF-16，hash 对不上；`git stash push`
对**已提交**文件不做任何事，会让 RED 假装是绿。）
## contract_red.log

```text
    assert got != ref, (
E   AssertionError: `coverage_active_last`（载体 `coverage_active_last`）改了但判决与产物都没变 —— 它仍然是个死字段（审计要求：接进判决或删掉）
E   assert ((('alerts', 'ok'), ('api', 'ok'), ('browser', 'ok'), ('capability', 'ok'), ('coverage', 'ok'), ('data', 'ok'), ...), ...力缺失'（比例 0%），累计缺失标的 0 个；无逐 signal 可评估率数据，故只做提示不判死（阈值 warn >0%，fail >=100%）", '请求 300 次：非 200 0，JSON 解析失败 0，连不上 0', ...)) != ((('alerts', 'ok'), ('api', 'ok'), ('browser', 'ok'), ('capability', 'ok'), ('coverage', 'ok'), ('data', 'ok'), ...), ...力缺失'（比例 0%），累计缺失标的 0 个；无逐 signal 可评估率数据，故只做提示不判死（阈值 warn >0%，fail >=100%）", '请求 300 次：非 200 0，JSON 解析失败 0，连不上 0', ...))
_ test_previously_write_only_field_now_has_a_production_reader[coverage_active_p05-coverage_active_p05] _
tests\test_health_consumer_contract.py:1189: in test_previously_write_only_field_now_has_a_production_reader
    assert got != ref, (
E   AssertionError: `coverage_active_p05`（载体 `coverage_active_p05`）改了但判决与产物都没变 —— 它仍然是个死字段（审计要求：接进判决或删掉）
E   assert ((('alerts', 'ok'), ('api', 'ok'), ('browser', 'ok'), ('capability', 'ok'), ('coverage', 'ok'), ('data', 'ok'), ...), ...力缺失'（比例 0%），累计缺失标的 0 个；无逐 signal 可评估率数据，故只做提示不判死（阈值 warn >0%，fail >=100%）", '请求 300 次：非 200 0，JSON 解析失败 0，连不上 0', ...)) != ((('alerts', 'ok'), ('api', 'ok'), ('browser', 'ok'), ('capability', 'ok'), ('coverage', 'ok'), ('data', 'ok'), ...), ...力缺失'（比例 0%），累计缺失标的 0 个；无逐 signal 可评估率数据，故只做提示不判死（阈值 warn >0%，fail >=100%）", '请求 300 次：非 200 0，JSON 解析失败 0，连不上 0', ...))
_ test_previously_write_only_field_now_has_a_production_reader[scope_counts-scope_counts] _
tests\test_health_consumer_contract.py:1189: in test_previously_write_only_field_now_has_a_production_reader
    assert got != ref, (
E   AssertionError: `scope_counts`（载体 `scope_counts`）改了但判决与产物都没变 —— 它仍然是个死字段（审计要求：接进判决或删掉）
E   assert ((('alerts', 'ok'), ('api', 'ok'), ('browser', 'ok'), ('capability', 'ok'), ('coverage', 'ok'), ('data', 'ok'), ...), ...力缺失'（比例 0%），累计缺失标的 0 个；无逐 signal 可评估率数据，故只做提示不判死（阈值 warn >0%，fail >=100%）", '请求 300 次：非 200 0，JSON 解析失败 0，连不上 0', ...)) != ((('alerts', 'ok'), ('api', 'ok'), ('browser', 'ok'), ('capability', 'ok'), ('coverage', 'ok'), ('data', 'ok'), ...), ...力缺失'（比例 0%），累计缺失标的 0 个；无逐 signal 可评估率数据，故只做提示不判死（阈值 warn >0%，fail >=100%）", '请求 300 次：非 200 0，JSON 解析失败 0，连不上 0', ...))
_ test_previously_write_only_field_now_has_a_production_reader[universe_active_scan_codes-universe_active_scan_codes_first] _
tests\test_health_consumer_contract.py:1165: in test_previously_write_only_field_now_has_a_production_reader
    assert key in agg, f"{field} 的会话载体 `{key}` 必须存在"
E   AssertionError: universe_active_scan_codes 的会话载体 `universe_active_scan_codes_first` 必须存在
E   assert 'universe_active_scan_codes_first' in {'measured': True, 'rounds': 30, 'states': {'fresh': 30}, 'worst_state': 'fresh', ...}
=========================== short test summary info ===========================
FAILED tests/test_health_consumer_contract.py::test_scope_positive_never_uses_evidenced_subset_as_denominator[1]
FAILED tests/test_health_consumer_contract.py::test_scope_positive_never_uses_evidenced_subset_as_denominator[5]
FAILED tests/test_health_consumer_contract.py::test_scope_positive_never_uses_evidenced_subset_as_denominator[15]
FAILED tests/test_health_consumer_contract.py::test_scope_positive_never_uses_evidenced_subset_as_denominator[29]
FAILED tests/test_health_consumer_contract.py::test_full_market_requires_magnitude_evidence[400-None-400]
FAILED tests/test_health_consumer_contract.py::test_full_market_requires_magnitude_evidence[3000-None-3000]
FAILED tests/test_health_consumer_contract.py::test_full_market_requires_magnitude_evidence[3001-None-3001]
FAILED tests/test_health_consumer_contract.py::test_full_market_requires_magnitude_evidence[3000-6000-3000]
FAILED tests/test_health_consumer_contract.py::test_scope_broad_unquantified_is_a_distinct_state
FAILED tests/test_health_consumer_contract.py::test_freshness_positive_never_uses_evidenced_subset[1]
FAILED tests/test_health_consumer_contract.py::test_freshness_positive_never_uses_evidenced_subset[15]
FAILED tests/test_health_consumer_contract.py::test_freshness_positive_never_uses_evidenced_subset[29]
FAILED tests/test_health_consumer_contract.py::test_t0_measured_zero_is_not_read_as_missing[0-True]
FAILED tests/test_health_consumer_contract.py::test_session_absolute_shrink_is_consumed
FAILED tests/test_health_consumer_contract.py::test_coverage_session_evidence_survives_missing_t0_truth
FAILED tests/test_health_consumer_contract.py::test_previously_write_only_field_now_has_a_production_reader[coverage_active_denominator_kinds-coverage_active_denominator_kinds]
FAILED tests/test_health_consumer_contract.py::test_previously_write_only_field_now_has_a_production_reader[coverage_active_last-coverage_active_last]
FAILED tests/test_health_consumer_contract.py::test_previously_write_only_field_now_has_a_production_reader[coverage_active_p05-coverage_active_p05]
FAILED tests/test_health_consumer_contract.py::test_previously_write_only_field_now_has_a_production_reader[scope_counts-scope_counts]
FAILED tests/test_health_consumer_contract.py::test_previously_write_only_field_now_has_a_production_reader[universe_active_scan_codes-universe_active_scan_codes_first]
20 failed, 45 passed in 0.92s
```

## contract_green.log

```text
.................................................................        [100%]
65 passed in 0.32s
```

## contract_rollback.log

```text
.................................................................        [100%]
65 passed in 0.35s
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
[exit=0] dash_render_check
[exit=0] arad.cli selftest
```

## full_pytest_green.log（尾部）

```text
........................................................................ [ 86%]
........................................................................ [ 90%]
........................................................................ [ 94%]
........................................................................ [ 98%]
..............................                                           [100%]
1830 passed in 96.59s (0:01:36)
```
