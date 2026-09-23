# 证据 — 2026-09-23 21:00:00 JST（WP01 三条 RED + WP02 call_detailed）

> ⚠ `*.log` 被 `.gitignore:25` 拦下；**未改 `.gitignore`**（配置变更超边界），
> 原文并入本 Markdown 提交。

| 文件 | 内容 |
|---|---|
| `coverage_cases.json` | RED 1 双轴：R=3 P=3 Q=1 -> raw **1.0** / usable **1/3** |
| `tencent_detailed_key_axis_matrix.csv` | RED 2 键轴矩阵（8 组真实调用） |
| `snapshot_outcome_cases.json` | RED 3 merge invalid->valid（overlap 为空） |
| `round_attribution_cases.json` | WP02 failover 前后 **terminals_identical=true** |
| `wp01_red.log` / `wp01_green.log` | WP01 回退验牙：3 行为性 RED / 0 结构性 ERROR |
| `coverage_semantics_rollback.log` | RED 1+RED 3 注入旧实现 -> 3 failed |
| `tencent_key_axis_rollback.log` | RED 2 注入修前 `_req_key` -> 1 failed，**阳性对照保持绿** |
| `source_manager_detailed_red.log` | WP02 注入去掉 source 绑定 -> 1 failed |
| `gates.log` / `full_pytest.log` | check_* 8/8 + dash + selftest；**1891 passed** |

**回退验牙方法**：保持 API 形状、只替换实现体（**不**用
`git checkout <sha> -- <file>` —— 那会得到 ImportError 这种**结构性** ERROR，
证明不了断言有探测力，而且会抹掉未提交的工作树改动）。
注入器一律用 `UTF8Encoding($false)` 写盘 —— PowerShell 的
`Set-Content -Encoding utf8` 会加 BOM，加在 `.py` 首行直接 `SyntaxError`，
那又成了结构性 RED。
## wp01_red.log

```text
FF..F..                                                                  [100%]
================================== FAILURES ===================================
E   AssertionError: 传输轴：三条 raw 都回来了 -> 1.0
    assert 0.3333333333333333 == 1.0 ± 1.0e-06
      
      comparison failed
      Obtained: 0.3333333333333333
      Expected: 1.0 ± 1.0e-06
D:\ccc\ashare-radar\tests\test_snapshot_outcome_contract.py:569: AssertionError: 传输轴：三条 raw 都回来了 -> 1.0
E   AssertionError: 没有 raw 证据就不得声称 raw 覆盖率
    assert 1.0 is None
     +  where 1.0 = SnapshotFetchResult(route='stocks', source='legacy', requested_keys=frozenset({'600002'}), raw_presence_known=False, r...(), unexpected_raw_keys=frozenset(), duplicate_raw_keys=frozenset(), raw_rows=(), provenance='quote_projection_legacy').raw_return_coverage
D:\ccc\ashare-radar\tests\test_snapshot_outcome_contract.py:592: AssertionError: 没有 raw 证据就不得声称 raw 覆盖率
E   AssertionError: 合并后按 P-Q 重算 -> 该码已可用，quality 必须为空
    assert frozenset({'600001'}) == frozenset()
      
      Extra items in the left set:
      '600001'
      Use -v to get more diff
D:\ccc\ashare-radar\tests\test_snapshot_outcome_contract.py:670: AssertionError: 合并后按 P-Q 重算 -> 该码已可用，quality 必须为空
=========================== short test summary info ===========================
FAILED tests/test_snapshot_outcome_contract.py::test_wp01_red1_coverage_has_two_axes
FAILED tests/test_snapshot_outcome_contract.py::test_wp01_red1_legacy_raw_coverage_is_unmeasured_not_one
FAILED tests/test_snapshot_outcome_contract.py::test_wp01_red3_merge_recomputes_quality_globally
3 failed, 4 passed, 35 deselected in 0.09s
```

## wp01_green.log

```text
..........................................                               [100%]
42 passed in 0.11s
```

## coverage_semantics_rollback.log

```text
  exit=1  3 failed, 39 deselected in 0.08s
  行为性 assert 行=9   结构性 ERROR=0
```

## tencent_key_axis_rollback.log

```text
F.                                                                       [100%]
================================== FAILURES ===================================
E   AssertionError: 个股必须落裸码轴，不得带前缀：['sh600000']
    assert frozenset({'sh600000'}) == frozenset({'600000'})
      
      Extra items in the left set:
      'sh600000'
      Extra items in the right set:
      '600000'
      Use -v to get more diff
D:\ccc\ashare-radar\tests\test_snapshot_outcome_contract.py:620: AssertionError: 个股必须落裸码轴，不得带前缀：['sh600000']
=========================== short test summary info ===========================
FAILED tests/test_snapshot_outcome_contract.py::test_wp01_red2_explicit_prefix_is_not_index_identity
1 failed, 1 passed, 40 deselected in 0.08s
```

## source_manager_detailed_red.log

```text
  exit=1  1 failed, 9 deselected in 0.08s
  行为性 assert 行=3   结构性 ERROR=0
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