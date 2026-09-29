"""`IT-P1-SOAK-RAW-PRESENCE-GRADE-GATE-002`

WP08 把 `raw_presence_known_by_route` 读成了三态证据等级，但**只呈现、
不判定** —— 判决层零读者。那是 bug 类 (c)「数据算了、判决层零读者」，
本项目已因它栽过两次（`raw_presence_known_by_route` 零读者本身、
`truncated` 无赋值）。

本文件钉住 `evaluate_health` 里新增的 `raw_presence_evidence` 检查。

## 判定矩阵（依据是"知道 vs 不知道"，不是"精确 vs 不精确"）

| 会话状态 | 等级 | 理由 |
|---|---|---|
| 无观测账本（`rounds_with_observation == 0`） | **ok（跳过）** | 本项**不适用**；与既有 16 个 check 的"缺字段跳过"一致 |
| 有账本、无 grade 字段（旧版采集） | **warn** | 版本差异，非事故；但**不得**默认成精确 |
| `state == "not_measured"` | **warn** | 定义就是"轮样本无等级字段"，是采集属性，非完整性事故 |
| `projected`（明确知道是投影反推） | **warn** | 已知降级 + 计数已降为上界；判 fail 会惩罚合法的 legacy 用法 |
| `partial`（某 route 产数据却没登记证据） | **fail** | **唯一真事故**：bug 类 (e)「证据子集自称全程」 |
| `exact` | **ok** | 全 route 有精确出品证据 |

## 我写错过一次（如实记录）

第一版我把"有账本但无 grade 字段"判 **fail**，当场把 **21 个既有测试**
打红 —— 那是把**版本差异**误判成**事故**，方向搞反了。
第二版把 `not_measured` 也判 fail，又打红 9 个。
第三版才收敛到上表。**是既有测试抓住了我**，不是我一次写对。
本文件因此特别包含 (a) 三条"不是事故"的**阴性对照**，
防止以后有人为了"更严格"把 warn 又升回 fail。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_spec = importlib.util.spec_from_file_location(
    "live_session_gate", ROOT / "tools" / "live_session.py")
ls = importlib.util.module_from_spec(_spec)
sys.modules["live_session_gate"] = ls
_spec.loader.exec_module(ls)

CHECK_NAME = "raw_presence_evidence"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def _obs(**over) -> dict:
    base = {
        "source": "sina",
        "capabilities": {"source": "sina"},
        "requested": 100,
        "returned": 97,
        "admitted": 95,
        "coverage": 0.97,
        "unknown_missing": ["000002"],
        "rejected_quality": [],
        "future_rejected": 0,
        "out_of_order_rejected": 0,
        "unavailable_capability": 0,
    }
    base.update(over)
    return base


def _sample(**over) -> dict:
    kw = dict(index=1, latency_ms=400.0, alerts=(), error=False, quotes=5000,
              universe=5000, history_points=10, history_codes=10, max_deque=1,
              history_maxlen=360, watchlist_only=False)
    kw.update(over)
    return ls.make_round_sample(**kw)


def _setup() -> dict:
    return {"engine_build_s": 1.0, "universe_refresh_s": 1.0,
            "universe_size": 5000, "fell_back_to_watchlist": False}


def _metrics(rounds: list[dict], **over) -> dict:
    m = ls.finalize_metrics(
        rounds,
        api={"requests": 20, "non_200": 0, "malformed": 0, "unreachable": 0},
        sse={"connected": True, "stalled": False, "events_total": 15,
             "events_by_type": {"tick": 5, "bye": 1}},
        logs={"WARNING": 0, "ERROR": 0, "CRITICAL": 0},
        browser={"ran": False, "skipped": True, "errors": []},
        setup=_setup(),
    )
    m.update(over)
    return m


def _check(v: dict, name: str) -> dict:
    hits = [c for c in v["checks"] if c["name"] == name]
    assert hits, (f"verdict 必须有 {name} 检查项，"
                  f"实际 {[c['name'] for c in v['checks']]}")
    return hits[0]


# ===========================================================================
# 1) 检查项本身存在（防"字段加了但没接判决层"回归）
# ===========================================================================

class TestGateIsWired:
    def test_check_item_exists(self):
        """判决层必须**真的**有这个检查项 —— 这是本项的全部意义。"""
        v = ls.evaluate_health(_metrics([_sample(observation=_obs())]))
        item = _check(v, CHECK_NAME)
        assert "level" in item and "ok" in item

    def test_gate_uses_ast_not_substring_tautology(self):
        """门禁必须**真调** `evaluate_health` 内的等级逻辑，不是摆设。

        用 AST 检查 `evaluate_health` 里确实读了 `raw_presence_grade`
        这个 key —— 不能只靠源码里出现字符串（那会被我自己的注释满足，
        是本仓库踩过的"子串同义反复"）。
        """
        import ast

        src = (ROOT / "tools" / "live_session.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "evaluate_health")
        consts = {n.value for n in ast.walk(fn)
                  if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assert "raw_presence_grade" in consts, (
            "evaluate_health 必须**读取** raw_presence_grade 这个 key")


# ===========================================================================
# 2) 唯一判 fail 的分支：partial（证据子集自称全程）
# ===========================================================================

class TestPartialIsFail:
    def test_partial_evidence_fails(self):
        """某 route **产出了数据却没登记证据** -> 必须 fail。

        这是 bug 类 (e) 的**本体**：其余 route 全绿会把它
        伪装成"全部精确"。
        """
        # stocks 有证据；index 真的产出了数据却不在 grade map 里
        obs = _obs(raw_presence_known_by_route={"stocks": True},
                   admitted_by_route={"stocks": 90, "index": 5},
                   returned_by_route={"stocks": 92, "index": 5})
        v = ls.evaluate_health(_metrics([_sample(observation=obs)]))
        item = _check(v, CHECK_NAME)
        assert item["ok"] is False, f"必须 fail，实测 {item}"
        assert item["level"] == "fail"
        assert v["healthy"] is False
        assert CHECK_NAME in v["fail"]

    def test_partial_message_names_the_ungraded_route(self):
        """报错必须**点名**未登记的 route —— 只知有降级不够可行动。"""
        obs = _obs(raw_presence_known_by_route={"stocks": True},
                   admitted_by_route={"stocks": 90, "index": 5},
                   returned_by_route={"stocks": 92, "index": 5})
        v = ls.evaluate_health(_metrics([_sample(observation=obs)]))
        item = _check(v, CHECK_NAME)
        msg = str(item.get("msg") or item.get("detail") or item)
        assert "index" in msg, f"必须点名未登记 route，实测 {msg!r}"


# ===========================================================================
# 3) 阴性对照：三种"不是事故"的情形**不得**被判 fail
#    （防止以后有人为了"更严格"把 warn 升回 fail）
# ===========================================================================

class TestNotIncidentsAreNotFail:
    def test_no_observation_ledger_is_skipped_not_failed(self):
        """无观测账本 = 本项**不适用**，判 ok 跳过。"""
        v = ls.evaluate_health(_metrics([_sample()]))     # 不传 observation
        item = _check(v, CHECK_NAME)
        assert item["ok"] is True, (
            f"无账本是不适用，不是失败，实测 {item}")
        assert item["level"] == "ok"

    def test_missing_grade_field_is_warn_not_fail(self):
        """有账本但**整个 grade 字段缺失**（旧版采集）-> warn。"""
        m = _metrics([_sample(observation=_obs())])
        m.pop("raw_presence_grade", None)
        v = ls.evaluate_health(m)
        item = _check(v, CHECK_NAME)
        assert item["level"] == "warn", (
            f"版本差异应 warn，实测 {item}")
        assert item["ok"] is True, "warn 不得阻断"
        assert v["healthy"] is True

    def test_not_measured_state_is_warn_not_fail(self):
        """`state == not_measured`（轮样本无等级字段）-> warn。"""
        m = _metrics([_sample(observation=_obs())])
        m["raw_presence_grade"] = {"state": "not_measured",
                                   "rounds_not_measured": 1,
                                   "rounds_partial": 0,
                                   "projected_rounds": 0,
                                   "rounds_seen_by_route": {}}
        v = ls.evaluate_health(m)
        item = _check(v, CHECK_NAME)
        assert item["level"] == "warn", f"实测 {item}"
        assert v["healthy"] is True

    def test_projected_is_warn_not_fail(self):
        """**已知**投影降级 -> warn（计数已降为上界，非事故）。"""
        m = _metrics([_sample(observation=_obs())])
        m["raw_presence_grade"] = {
            "state": "projected", "rounds_not_measured": 0,
            "rounds_partial": 0, "projected_rounds": 1,
            "rounds_seen_by_route": {"stocks": 1},
            "projected_routes": ["stocks"]}
        v = ls.evaluate_health(m)
        item = _check(v, CHECK_NAME)
        assert item["level"] == "warn", f"实测 {item}"
        assert item["ok"] is True
        assert v["healthy"] is True
        assert CHECK_NAME in v["warn"], "warn 必须被点名（否则等于没说）"


# ===========================================================================
# 4) 阳性对照：exact 必须判 ok（防"永远报警"）
# ===========================================================================

class TestExactIsGreen:
    def test_exact_is_ok(self):
        """全 route 精确 -> ok。防"永远判红"的反向假数据。"""
        m = _metrics([_sample(observation=_obs(
            raw_presence_known_by_route={"stocks": True, "index": True}))])
        v = ls.evaluate_health(m)
        item = _check(v, CHECK_NAME)
        assert item["level"] == "ok", f"实测 {item}"
        assert item["ok"] is True
        assert CHECK_NAME not in v["fail"]

    def test_exact_grade_actually_reached(self):
        """夹具真的产生 exact —— 否则上面的阳性对照是空转。"""
        m = _metrics([_sample(observation=_obs(
            raw_presence_known_by_route={"stocks": True, "index": True}))])
        g = m.get("raw_presence_grade") or {}
        assert g.get("state") == "exact", (
            f"夹具应产出 exact，实测 {g.get('state')!r}")


# ===========================================================================
# 5) 与既有 check 的兼容性（防破坏性变更）
# ===========================================================================

class TestNoBreakage:
    def test_all_sibling_checks_still_present(self):
        """新增检查不得挤掉任何既有检查项。"""
        v = ls.evaluate_health(_metrics([_sample(observation=_obs())]))
        names = {c["name"] for c in v["checks"]}
        for want in ("rounds", "fetch", "data", "coverage", "capability",
                     "api", "sse", "memory", "browser", "universe",
                     "alerts", "delivery_accounting"):
            assert want in names, f"既有检查 {want} 不见了"

    def test_every_check_has_level(self):
        v = ls.evaluate_health(_metrics([_sample(observation=_obs())]))
        for c in v["checks"]:
            assert "level" in c, f"{c['name']} 缺 level"

    def test_verdict_survives_json_round_trip(self):
        """判决必须能 JSON 序列化（报告要落盘）。"""
        import json

        v = ls.evaluate_health(_metrics([_sample(observation=_obs())]))
        again = json.loads(json.dumps(v, default=str))
        assert again["healthy"] == v["healthy"]

    def test_warn_does_not_change_exit_code(self):
        """warn 不得改变退出码（既有纪律：黄色不把 CI 打红）。"""
        m = _metrics([_sample(observation=_obs())])
        m["raw_presence_grade"] = {"state": "projected",
                                   "rounds_not_measured": 0,
                                   "rounds_partial": 0,
                                   "projected_rounds": 1,
                                   "rounds_seen_by_route": {"stocks": 1},
                                   "projected_routes": ["stocks"]}
        v = ls.evaluate_health(m)
        assert v["exit_code"] == ls.EXIT_HEALTHY, (
            f"warn 不得改变退出码，实测 {v['exit_code']}")
