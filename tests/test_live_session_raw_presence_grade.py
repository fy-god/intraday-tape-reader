"""WP08：soak 报告必须**读** ``raw_presence_known_by_route`` 并给出证据等级。

缺陷（`IT-P1-SOAK-RAW-PRESENCE-GRADE-BLIND-001`）
--------------------------------------------------
上一轮把 ``raw_presence_known_by_route: dict[str, bool]`` 加进了
``RoundObservationSet``（``capabilities.py``），Engine 也逐 route 填了它。
但 ``tools/live_session.py`` **零读者** —— 它只做::

    out["unknown_missing"] = len(obs.get("unknown_missing") or [])
    out["rejected_quality"] = len(obs.get("rejected_quality") or [])

然后把这两个**长度**当作硬事实上报。当某条 route 的
``raw_presence_known_by_route[route] is False`` 时，这两个桶的分界是从
**可用 Quote 投影反推**的：此时

    「provider 根本没返回这一行」 与 「provider 返回了但 price<=0」

**不可区分**（见 ``sources/outcome.py`` 的集合代数 R/P/Q）。
把投影数按精确数的权威上报，同时命中本仓库两类反复出现的病灶：

* bug 类 (c)「数据算了、判决层零读者」；
* bug 类 (e)「证据子集自称全程」—— 混源 failover 时一条 route 精确、
  另一条在投影，报告却只有一句话。

本文件锁定的**三态纪律**
------------------------
``True`` / ``False`` / **键缺席（未测量）** 是三种状态，**不得压成两态**：

* ``True``  -> exact：分界来自精确 provider raw presence 出口；
* ``False`` -> **降级**：投影反推，计数只能当**上界**；
* 键缺席 -> **未测量**（旧版本轮样本、legacy 来源）：既不是 exact，
  也不是 projected。**这是本文件最关键的一条 fail-closed 测试** ——
  把"没测到"读成 ``True`` 就是 `bool(None) is False` 的**镜像错误**
  （「不知道」被当成「没问题」），而它比原缺陷更危险，因为它是静默的。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "live_session.py"

_SPEC = importlib.util.spec_from_file_location("live_session_tool_rpg", TOOL)
assert _SPEC and _SPEC.loader
ls = importlib.util.module_from_spec(_SPEC)
sys.modules["live_session_tool_rpg"] = ls
_SPEC.loader.exec_module(ls)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def _obs(**over) -> dict:
    """一份典型的 ``RoundObservationSet.as_dict()``。

    注意 ``unknown_missing`` 是**列表**（账本形状），报告侧取 ``len``。
    """
    base = {
        "source": "sina",
        "capabilities": {"source": "sina"},
        "requested": 100,
        "returned": 97,
        "admitted": 95,
        "coverage": 0.97,
        "unknown_missing": ["000002", "000003", "000004"],
        "rejected_quality": ["600001"],
        "future_rejected": 0,
        "out_of_order_rejected": 1,
        "unavailable_capability": 0,
        # engine 逐 route 的准入/返回分账 —— 聚合侧用它恢复"本轮哪条
        # route 真的出过数"，从而识别"某条 route 出数却没给证据等级"。
        "admitted_by_route": {"stocks": 90, "index": 5},
        "returned_by_route": {"stocks": 92, "index": 5},
        "reject_by_route": {"stocks": {"future": 0, "out_of_order": 1},
                            "index": {"future": 0, "out_of_order": 0}},
    }
    base.update(over)
    return base


def _sample(**over) -> dict:
    kw = dict(index=1, latency_ms=400.0, alerts=(), error=False, quotes=5000,
              universe=5000, history_points=10, history_codes=10, max_deque=1,
              history_maxlen=360, watchlist_only=False)
    kw.update(over)
    return ls.make_round_sample(**kw)


def _summary(rounds: list[dict]) -> dict:
    return ls.summarize_rounds(rounds)


# ===========================================================================
# 1. 降级（False）route -> 必须**显式**报成降级，计数不得当精确事实
# ===========================================================================
def test_projected_route_is_surfaced_as_degraded():
    """**核心 RED**：``stocks=False`` 必须让报告自曝其短。

    修前：``raw_presence_*`` 这些键**一个都不存在**，本测试在
    ``s["raw_presence_grade_state"]`` 处抛 ``KeyError``。
    """
    s = _sample(observation=_obs(
        raw_presence_known_by_route={"stocks": False, "index": True}))

    assert s["raw_presence_grade_state"] == "mixed", (
        "一条 route 投影、另一条精确 -> mixed（不得塌缩成 exact/projected）")
    assert "stocks" in s["raw_presence_projected_routes"], (
        "被投影反推的 route 必须**点名** —— 只知道'有降级'不够可行动")
    assert s["raw_presence_exact_all_routes"] is False, (
        "单布尔诚实旗标在有投影 route 时**必须**为 False")


def test_projected_route_counts_are_not_presented_as_exact():
    """降级轮的 missing/quality 计数不得与精确轮**同级**出现。

    允许保留原始数字，但诚实旗标必须可见且**相邻**。
    """
    s = _sample(observation=_obs(
        raw_presence_known_by_route={"stocks": False, "index": True}))
    # 原始数字保留（可回溯），但旗标必须在同一条记录里
    assert s["unknown_missing"] == 3
    assert s["rejected_quality"] == 1
    assert s["raw_presence_grade_state"] != "exact"
    assert s["raw_presence_exact_all_routes"] is False
    assert s["raw_presence_projected_routes"] == ["stocks"]


def test_aggregate_reports_projected_rounds_and_upper_bounds():
    """会话级聚合：降级必须**点名到轮**，且计数单独成"上界口径"。"""
    exact = _sample(index=1, observation=_obs(
        raw_presence_known_by_route={"stocks": True, "index": True}))
    proj = _sample(index=2, observation=_obs(
        raw_presence_known_by_route={"stocks": False, "index": True}))
    m = _summary([exact, proj])
    g = m["raw_presence_grade"]

    assert g["state"] == "projected"
    assert g["projected_rounds"] == 1
    assert g["projected_round_indexes"] == [2], "必须能回查是第几轮降级"
    assert g["exact_all_rounds"] is False
    assert g["by_route"]["stocks"]["projected"] == 1
    assert g["by_route"]["stocks"]["exact"] == 1
    # 投影轮的两个计数**单独命名**，不与 missing_total / quality_total 同轴
    assert g["projected_missing_upper_bound"] == 3
    assert g["projected_quality_upper_bound"] == 1
    # 总数仍然保留（可回溯），但语义边界由上面的键给出
    assert m["missing_total"] == 6
    assert g["_semantics"]


def test_worst_round_carries_grade_adjacent_to_counts():
    """最差轮表：旗标必须与 missing/quality **相邻**，不能藏在别处。"""
    proj = _sample(index=1, observation=_obs(
        raw_presence_known_by_route={"stocks": False, "index": False}))
    m = _summary([proj])
    row = m["worst_rounds"][0]
    assert row["index"] == 1
    assert row["raw_presence_grade"] == "projected"
    assert row["missing"] == 3 and row["quality"] == 1   # 原始数保留
    assert row["raw_presence_projected_routes"] == ["index", "stocks"]
    assert "投影" in row["_grade_note"], (
        "降级轮的 missing/quality 必须带一句'这是上界'的话，"
        "否则读者会把它当硬事实")


def test_degraded_grade_is_visible_in_human_summary():
    """人读的结论表也必须说清降级 —— 报告不是只有 JSON 消费者。"""
    proj = _sample(index=7, observation=_obs(
        raw_presence_known_by_route={"stocks": False, "index": True}))
    m = _summary([proj])
    text = ls._fmt_observation_summary(m)
    assert "降级" in text
    assert "#7" in text and "#7=97.0%!" in text, (
        "最差轮里降级的那一轮要被标出来，不能与精确轮长得一样")


# ===========================================================================
# 2. 精确（True）route -> 必须报成 exact（正向对照）
# ===========================================================================
def test_exact_route_reported_as_exact():
    """**正向对照**：全 True 必须报 exact。

    没有这条，把**所有**轮次一律标成"降级"也能让上面的测试全绿 ——
    那是一个更糟的修法（把真话变成永远的黄灯）。
    """
    s = _sample(observation=_obs(
        raw_presence_known_by_route={"stocks": True, "index": True}))
    assert s["raw_presence_grade_state"] == "exact"
    assert s["raw_presence_projected_routes"] == []
    assert s["raw_presence_exact_all_routes"] is True

    m = _summary([s, _sample(index=2, observation=_obs(
        raw_presence_known_by_route={"stocks": True, "index": True}))])
    g = m["raw_presence_grade"]
    assert g["state"] == "exact"
    assert g["exact_all_rounds"] is True
    assert g["projected_rounds"] == 0
    assert g["rounds_not_measured"] == 0
    assert g["rounds_partial"] == 0
    assert g["projected_missing_upper_bound"] == 0
    assert g["by_route"]["stocks"] == {
        "exact": 2, "projected": 0, "not_measured": 0}
    # 精确轮不得被标成降级/未测量
    text = ls._fmt_observation_summary(m)
    worst_line = text.split("最差轮次")[1].split("\n")[0]
    assert "#1=97.0% #" in worst_line and "#1=97.0%!" not in worst_line
    assert "#2=97.0%!" not in worst_line


# ===========================================================================
# 3. **关键 fail-closed**：键缺席 = 未测量，**不是** True
# ===========================================================================
def test_missing_key_is_not_measured_not_true():
    """**本文件最关键的一条**：键整个缺席 -> ``not_measured``。

    ``bool(None) is False`` 与「键缺席 -> 当成 True」是同一枚硬币的两面：
    **三态被压成两态**。任务书明确要求缺席按**第三种状态**处理。

    修前的行为：这个键根本没有消费者，于是缺席与否在报告里**完全不可见**。
    把缺席读成 True 则会让旧样本/legacy 源拿到"精确"的背书 —— silent
    fail-open，比原缺陷更危险。
    """
    obs = _obs()
    assert "raw_presence_known_by_route" not in obs, "夹具本身不得带这个键"
    s = _sample(observation=obs)

    assert s["raw_presence_grade_state"] == "not_measured", (
        "键缺席必须读成**未测量**这第三种状态")
    assert s["raw_presence_grade_state"] != "exact"
    assert s["raw_presence_exact_all_routes"] is False, (
        "未测量**绝不**能给 exact 背书（fail-closed）")
    assert s["raw_presence_projected_routes"] == [], (
        "未测量也**不是**降级 —— 不得凭空定罪，那是镜像错误")


def test_missing_key_aggregates_as_not_measured_not_exact():
    """会话级：缺键轮必须进 ``rounds_not_measured``，**不进** exact。"""
    with_key = _sample(index=1, observation=_obs(
        raw_presence_known_by_route={"stocks": True, "index": True}))
    without = _sample(index=2, observation=_obs())       # 旧样本：无该键
    m = _summary([with_key, without])
    g = m["raw_presence_grade"]

    assert g["rounds_not_measured"] == 1
    assert g["rounds_with_fact"] == 1
    assert g["exact_all_rounds"] is False, (
        "存在未测量轮时不得给「全程精确」背书")
    assert g["state"] == "not_measured", (
        "有未测量轮就没有「全程精确」可报 —— 不得只报已测到那部分的结论")
    # 未测量轮**不得**被计成 projected（那是凭空定罪）
    assert g["projected_rounds"] == 0
    assert g["projected_missing_upper_bound"] == 0
    assert m["rounds_with_observation"] == 2, "两轮都带账本"


def test_all_rounds_missing_key_is_not_measured():
    """全场都没有这个键 -> ``not_measured``（不是 exact）。"""
    m = _summary([_sample(index=i + 1, observation=_obs()) for i in range(3)])
    g = m["raw_presence_grade"]
    assert g["state"] == "not_measured"
    assert g["rounds_not_measured"] == 3
    assert g["rounds_with_fact"] == 0
    assert g["exact_all_rounds"] is False
    text = ls._fmt_observation_summary(m)
    assert "未测量" in text


def test_dirty_grade_value_is_not_measured_not_true():
    """脏值（``None`` / ``"maybe"``）读不出 -> 未测量，**不得**猜成 True。"""
    for bad in (None, "maybe", {}, [], "3.5"):
        s = _sample(observation=_obs(raw_presence_known_by_route={
            "stocks": bad, "index": "maybe"}))
        # 两条 route 的键都在，但值都读不出 -> **未测量**（不是 exact）
        assert s["raw_presence_known_by_route"] == {}, f"bad={bad!r}"
        assert s["raw_presence_grade_state"] == "not_measured", f"bad={bad!r}"
        assert s["raw_presence_exact_all_routes"] is False, f"bad={bad!r}"
    # 一条脏、一条明确 False -> 降级（脏的那条不得把结论拉回 exact）
    s = _sample(observation=_obs(raw_presence_known_by_route={
        "stocks": "maybe", "index": False}))
    assert s["raw_presence_grade_state"] == "projected"
    assert s["raw_presence_projected_routes"] == ["index"]
    assert s["raw_presence_exact_all_routes"] is False


def test_string_bools_are_accepted_but_unknown_strings_are_not():
    """``"false"`` 是合法降级；``"maybe"`` 不是 True。"""
    s = _sample(observation=_obs(raw_presence_known_by_route={
        "stocks": "false", "index": "true"}))
    assert s["raw_presence_grade_state"] == "mixed"
    assert s["raw_presence_projected_routes"] == ["stocks"]


# ===========================================================================
# 4. 结构 / 机械不变量
# ===========================================================================
def test_grade_keys_are_in_the_observation_whitelists():
    """新字段必须进**两个**白名单，否则聚合方看不见它。

    ``_OBSERVATION_MARKER_KEYS`` 少了它 -> 旧样本探测不到；
    ``_OBSERVATION_VALUE_FIELDS`` 少了它 -> ``make_round_sample`` 的
    "采到了没有"标记里没有它，聚合的轮数会对不上。
    """
    for attr in ("_OBSERVATION_MARKER_KEYS", "_OBSERVATION_VALUE_FIELDS"):
        keys = getattr(ls, attr)
        assert "raw_presence_known_by_route" in keys, f"{attr} 缺键"
        assert "raw_presence_grade_state" in keys, f"{attr} 缺键"


def test_route_cells_account_for_every_round_with_observation():
    """逐 route 三格之和 == 带账本的轮数（机械对账，不许丢轮）。"""
    rounds = [
        _sample(index=1, observation=_obs(
            raw_presence_known_by_route={"stocks": True, "index": True})),
        _sample(index=2, observation=_obs(
            raw_presence_known_by_route={"stocks": False, "index": True})),
        _sample(index=3, observation=_obs()),           # 缺键
        _sample(index=4, observation=_obs(
            raw_presence_known_by_route={"stocks": True, "index": False})),
    ]
    m = _summary(rounds)
    g = m["raw_presence_grade"]
    assert m["rounds_with_observation"] == 4
    for route, cell in g["by_route"].items():
        assert sum(cell.values()) == 4, f"{route} 三格之和必须等于 4，实测 {cell}"
        assert set(cell) == {"exact", "projected", "not_measured"}
    # 缺键那一轮让**两条** route 都落进 not_measured
    assert g["by_route"]["stocks"]["not_measured"] == 1
    assert g["by_route"]["index"]["not_measured"] == 1
    # 三条互斥的轮级分账
    assert g["rounds_with_fact"] + g["rounds_not_measured"] == 4
    assert g["rounds_not_measured"] == 1


def test_route_missing_from_map_is_flagged_as_partial_evidence():
    """**bug 类 (e)**：键只覆盖一部分 route 时，不得读出"全程精确"。

    真实来源：engine 只在 detailed 出口真正给出 outcome 时才登记 route，
    所以"指数走 legacy、个股走 detailed"的轮次**只带一个键**。此时另一条
    route 出过数却没有证据 —— 只看已有的键就会读出 exact。
    """
    # 只登记 stocks；但本轮 index **出过数**（admitted_by_route 有 index）
    partial = _sample(index=5, observation=_obs(
        raw_presence_known_by_route={"stocks": True}))
    s_grade = partial["raw_presence_grade_state"]
    assert s_grade == "not_measured", (
        "index 出过数却没登记等级 —— 轮级也不得读出 exact")

    m = _summary([partial])
    g = m["raw_presence_grade"]
    assert g["rounds_partial"] == 1, (
        "index 出过数却没登记证据等级 —— 必须点名为**部分证据**")
    assert g["partial_round_indexes"] == [5]
    assert g["exact_all_rounds"] is False, (
        "部分证据不得给「全程精确」背书（bug 类 e）")
    assert g["by_route"]["index"]["not_measured"] == 1
    assert g["rounds_seen_by_route"]["index"] == 1


def test_grade_survives_json_and_health_evaluation():
    """新键必须 JSON 可序列化，且不得让 evaluate_health 抛异常。"""
    proj = _sample(index=1, observation=_obs(
        raw_presence_known_by_route={"stocks": False, "index": True}))
    metrics = ls.finalize_metrics(
        [proj],
        api={"requests": 5, "non_200": 0, "malformed": 0, "unreachable": 0},
        sse={"connected": True, "stalled": False, "events_total": 3,
             "events_by_type": {"bye": 1}},
        logs={"WARNING": 0, "ERROR": 0, "CRITICAL": 0},
        browser={"ran": False, "skipped": True, "errors": []},
    )
    json.dumps(metrics, ensure_ascii=False)
    assert metrics["raw_presence_grade"]["state"] == "projected"
    ls.evaluate_health(metrics)          # 不抛即通过


def test_no_observation_keeps_legacy_shape():
    """没有 observation 的离线样本不得凭空多出这些键。"""
    s = _sample()
    for k in ("raw_presence_known_by_route", "raw_presence_grade_state",
              "raw_presence_projected_routes", "raw_presence_exact_all_routes"):
        assert k not in s, f"没有 observation 时不应有 {k}"


def test_empty_rounds_grade_is_not_measured():
    """零轮会话：未测量，**不是** exact（零证据不得被肯定）。"""
    g = ls.summarize_rounds([])["raw_presence_grade"]
    assert g["state"] == "not_measured"
    assert g["exact_all_rounds"] is False


@pytest.mark.parametrize("bad", [
    None, "oops", 5, ["stocks"], {"stocks": {"nested": True}},
])
def test_dirty_route_map_does_not_crash(bad):
    """整个 map 是脏值时不得炸掉 soak（报告宁缺勿崩）。"""
    s = _sample(observation=_obs(raw_presence_known_by_route=bad))
    assert s["index"] == 1
    assert s["raw_presence_exact_all_routes"] is False
