"""WP03 / IT-P1-TIME-ROLE-003-R1：时间策略矩阵与代码/合同的三方一致性守卫。

**为什么需要这个文件**：上一轮（`100e06a`）的缺陷本质是
"实现与自己的合同不一致" —— `source_time_contract.json` 写三源
``role=unknown`` / ``freshness_allowed=false``，Engine 却用这些 provider ts
做 4 小时 hard stale reject。**没有任何测试会发现这种矛盾**，因为两者各自
自洽。

本文件把三方绑死：

    source_time_contract.json   ←→   time_policy_matrix.json   ←→   engine.TIME_POLICY

任一方漂移（改合同不改策略、改策略不改代码）立刻变红。
"""
from __future__ import annotations

import json
import os

import pytest

from arad.engine import (DEFAULT_TIME_POLICY, TIME_POLICY, EngineState,
                         time_policy_for)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AUD = os.path.join(_ROOT, "docs", "audits", "intraday")


def _load(name: str) -> dict:
    with open(os.path.join(_AUD, name), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def contract() -> dict:
    return _load("source_time_contract.json")


@pytest.fixture(scope="module")
def matrix() -> dict:
    return _load("time_policy_matrix.json")


# ---------------------------------------------------------------- 合同 vs 矩阵
def test_matrix_covers_exactly_the_contract_sources(contract, matrix):
    assert set(matrix["policy"]) == set(contract["sources"]), (
        "时间策略矩阵与合同的来源集合不一致 —— 加/删来源时必须同时改两处")


def test_matrix_role_matches_contract(contract, matrix):
    for name, pol in matrix["policy"].items():
        assert pol["role"] == contract["sources"][name]["role"], (
            f"{name} 的 role 与合同不一致；矩阵不得自行判定语义")


def test_freshness_flag_is_consistent_with_contract(contract, matrix):
    """矩阵的 freshness_allowed == 合同的 freshness_allowed。"""
    for name, pol in matrix["policy"].items():
        assert pol["freshness_allowed"] == \
            contract["sources"][name]["freshness_allowed"], (
                f"{name} 的 freshness_allowed 与合同不一致")


# ------------------------------------------------------------ 矩阵 vs 引擎代码
def test_engine_policy_matches_matrix(matrix):
    """`engine.TIME_POLICY` 必须与矩阵逐项一致（防代码漂移）。"""
    for name, pol in matrix["policy"].items():
        got = TIME_POLICY.get(name)
        assert got is not None, f"engine.TIME_POLICY 缺少来源 {name}"
        assert got["freshness_allowed"] == pol["freshness_allowed"], (
            f"{name}.freshness_allowed 代码与矩阵不一致")


def test_unregistered_source_uses_strict_default(matrix):
    d = matrix["_unregistered_source_default"]
    assert DEFAULT_TIME_POLICY["freshness_allowed"] == d["freshness_allowed"]
    assert time_policy_for("某个没登记的新源") == DEFAULT_TIME_POLICY


def test_unknown_source_name_is_not_trusted():
    """未登记来源必须**从严**：不得被当作可信时间源。"""
    assert time_policy_for("")["freshness_allowed"] is False
    assert time_policy_for("nope")["freshness_allowed"] is False


# ------------------------------------------------------- 合同为 false -> 不硬拒
def test_contract_false_means_no_hard_reject(matrix):
    """**核心守卫**：合同 freshness_allowed=false 的来源，引擎不得硬拒绝。

    这正是上一轮缺失的那条链 —— 合同说不许，代码却做了，且无人发现。
    """
    if TIME_POLICY.get("tencent", {}).get("freshness_allowed"):
        pytest.skip("该来源已获权威语义，硬拒绝是合法的")
    from datetime import datetime, timedelta

    from fakes import make_quote

    now = datetime(2026, 9, 15, 10, 31, 0)
    st = EngineState(history_len=360)
    st.begin_source_epoch("stocks", "tencent#0", source_name="tencent")
    q = make_quote(code="600000", price=10.0, volume_lots=100.0,
                   ts=now - timedelta(days=3), prev_close=10.0, open=10.0,
                   high=10.0, low=10.0)
    st.update([q], now, route="stocks")
    assert "600000" in st.quotes, (
        "合同 freshness_allowed=false，代码却硬拒绝了 provider ts —— "
        "实现与合同矛盾（IT-P1-TIME-ROLE-003-R1）")
    assert st.stats.get("t_reject:stale", 0) == 0


def test_hard_reject_capability_is_retained(matrix):
    """下调不等于删除：受信任来源必须仍能硬拒绝。"""
    from datetime import datetime, timedelta

    from fakes import make_quote

    from arad import engine as eng_mod

    saved = dict(eng_mod.TIME_POLICY.get("tencent", {}))
    eng_mod.TIME_POLICY["tencent"] = {"freshness_allowed": True,
                                      "strict_ordering_allowed": True}
    try:
        now = datetime(2026, 9, 15, 10, 31, 0)
        st = EngineState(history_len=360)
        st.begin_source_epoch("stocks", "tencent#0", source_name="tencent")
        q = make_quote(code="600000", price=10.0, volume_lots=100.0,
                       ts=now - timedelta(days=3), prev_close=10.0, open=10.0,
                       high=10.0, low=10.0)
        admitted = st.update([q], now, route="stocks")
        assert "600000" not in admitted, "受信任来源的陈旧包应被硬拒绝"
        assert st.stats.get("t_reject:stale", 0) >= 1
    finally:
        eng_mod.TIME_POLICY["tencent"] = saved


def test_age_diagnostic_always_recorded(matrix):
    """不硬拒绝时**必须**留 age 诊断，否则无从观察该源时间是否可信。"""
    from datetime import datetime, timedelta

    from fakes import make_quote

    now = datetime(2026, 9, 15, 10, 31, 0)
    st = EngineState(history_len=360)
    st.begin_source_epoch("stocks", "tencent#0", source_name="tencent")
    q = make_quote(code="600000", price=10.0, volume_lots=100.0,
                   ts=now - timedelta(days=3), prev_close=10.0, open=10.0,
                   high=10.0, low=10.0)
    st.update([q], now, route="stocks")
    # ts 恰好是 now-3天，age 精确等于 259200 秒 —— 用 >= 而不是 >。
    assert st.time_age_seconds.get("600000", 0) >= 3 * 24 * 3600


def test_matrix_documents_that_downgrade_is_deliberate(matrix):
    """矩阵必须显式记录"当前没有任何 provider-ts 陈旧拦截"这个代价。"""
    lim = matrix["_known_limitation"]
    assert "没有" in lim or "不再" in lim, (
        "必须如实写明当前不存在 provider-ts 硬拦截，不能含糊")
