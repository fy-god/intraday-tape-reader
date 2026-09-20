"""IT-P1-TIME-ROLE-001：``source_time_contract.json`` 与真实 parser 的一致性守卫。

合同文件只有在**和代码不漂移**时才有价值。本文件把合同里的
``provider_field`` 绑到真实的解析位置：谁改了 parser 的 ts 取值来源而没改合同，
这里就红。

同时守住合同里最重要的一条**语义约束**：三家 ``role`` 都是 ``unknown``，
因此**禁止跨源比较 ts**。若将来有人在 role 仍为 unknown 的情况下把它当成
权威事件时间去跨源使用，这里的断言会提醒他先补证据。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs" / "audits" / "intraday" / "source_time_contract.json"


@pytest.fixture(scope="module")
def contract():
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def test_contract_file_exists_and_parses(contract):
    assert isinstance(contract, dict)
    assert "sources" in contract and "consequences" in contract


def test_all_three_sources_present(contract):
    assert set(contract["sources"]) == {"tencent", "sina", "eastmoney"}


@pytest.mark.parametrize("name", ["tencent", "sina", "eastmoney"])
def test_each_source_has_role_and_evidence(contract, name):
    entry = contract["sources"][name]
    assert entry["role"], f"{name} 缺少 role"
    assert entry["evidence"], f"{name} 缺少 evidence（无证据不得下结论）"
    for ev in entry["evidence"]:
        assert re.search(r"\S+\.py:\d+|\.py:\w+", ev), (
            f"{name} 的 evidence 不是可核查的『文件:行号』形式: {ev!r}")


@pytest.mark.parametrize("name", ["tencent", "sina", "eastmoney"])
def test_unknown_role_implies_no_cross_source_comparison(contract, name):
    """role=unknown 时不得声称可以判新鲜度。"""
    entry = contract["sources"][name]
    if entry["role"] == "unknown":
        assert entry["freshness_allowed"] is False, (
            f"{name} 的 role 是 unknown，却声称可用它判新鲜度 —— 无依据")


def test_contract_forbids_cross_source_comparison(contract):
    """三家都 unknown 时，跨源比较必须被明文禁止。"""
    roles = {n: e["role"] for n, e in contract["sources"].items()}
    assert all(r == "unknown" for r in roles.values()), (
        f"有源的 role 不再是 unknown（{roles}）—— 若已找到权威证据，"
        "请同时更新 consequences.cross_source_comparison 的结论")
    cons = contract["consequences"]
    assert "禁止" in cons["cross_source_comparison"]
    assert "禁止" in cons["freshness_judgement"]


# ---------------------------------------------------------------------------
# 与真实 parser 的绑定：合同说的取值位置必须真的是代码里的取值位置
# ---------------------------------------------------------------------------
def test_tencent_contract_matches_parser():
    src = (ROOT / "src" / "arad" / "sources" / "tencent.py").read_text(
        encoding="utf-8")
    assert 'FIELD_INDEX["timestamp"]' in src, (
        "tencent parser 改动了 timestamp 的取值方式，合同需同步")
    assert "ts=_parse_ts(fields[I_TIMESTAMP])" in src


def test_sina_contract_matches_parser():
    src = (ROOT / "src" / "arad" / "sources" / "sina.py").read_text(
        encoding="utf-8")
    assert "fields[30]" in src and "fields[31]" in src, (
        "sina parser 的日期/时间字段位置变了，合同需同步")
    assert "ts=_ts_of(fields)" in src


def test_eastmoney_contract_matches_parser():
    src = (ROOT / "src" / "arad" / "sources" / "eastmoney.py").read_text(
        encoding="utf-8")
    assert "f124" in src, "eastmoney parser 不再读 f124，合同需同步"
    assert 'ts=_ts_of(row.get("f124"))' in src


def test_capabilities_still_say_provider_time_false():
    """capabilities 把三家 provider_time 都声明为 false —— 与合同的 unknown 一致。"""
    src = (ROOT / "src" / "arad" / "capabilities.py").read_text(encoding="utf-8")
    assert src.count("provider_time=False") >= 3, (
        "capabilities 的 provider_time 声明变了；合同 role=unknown 的结论需复核")


# ---------------------------------------------------------------------------
# 引擎侧：原始 ts 必须留痕（合同 consequences.provider_ts_raw 的可执行形态）
# ---------------------------------------------------------------------------
def test_engine_state_exposes_provider_ts_raw():
    from arad.engine import EngineState
    st = EngineState(history_len=360)
    assert hasattr(st, "provider_ts_raw")
    assert hasattr(st, "effective_event_time")
    assert hasattr(st, "received_at")


def test_engine_state_exposes_source_epoch():
    from arad.engine import EngineState
    st = EngineState(history_len=360)
    assert hasattr(st, "source_epoch")
    assert hasattr(st, "begin_source_epoch")
