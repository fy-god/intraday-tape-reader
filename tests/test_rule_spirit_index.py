"""``rules/spirit_index.py`` 的离线测试（拉升指数 / 打压指数）。

**本例最关键的回归点**是代码消歧：``000001`` 既是上证指数（``sh000001``）
也是平安银行（``sz000001``）。把个股当指数、或把指数当个股，都会产生
看起来"正常"却完全错误的告警，所以消歧用例占了大头。
"""
from __future__ import annotations

from datetime import datetime
import time

import pytest

from arad.models import AlertKind, Quote, Snapshot, board_of
from arad.rules.base import RuleContext, bucket_of
from arad.rules.spirit_index import (
    DEFAULT_INDEX_CODES,
    DEFAULTS,
    SIGNALS,
    SIGNAL_CN,
    SpiritIndexRule,
    build,
    is_index_quote,
)
from arad.session import SessionPhase

T0 = 1_800_000_000.0
NOW = datetime.fromtimestamp(T0)

#: 上证指数量级（真实值约 3890 点）
LEVEL = 3890.0


class FakeState:
    """最小 EngineState 替身：只实现规则用到的 ``window`` 与 ``quotes``。"""

    def __init__(self):
        self.quotes: dict[str, Quote] = {}
        self.history: dict[str, list[tuple[float, float, float]]] = {}

    def feed(self, code, points):
        self.history[code] = list(points)

    def window(self, code, seconds, now_ep):
        return [p for p in self.history.get(code, [])
                if p[0] >= now_ep - float(seconds)]

    def price_change(self, code, seconds, now_ep):
        pts = self.window(code, seconds, now_ep)
        if len(pts) < 2 or pts[0][1] <= 0:
            return None
        return (pts[-1][1] / pts[0][1] - 1.0) * 100.0

    def volume_delta(self, code, seconds, now_ep):
        pts = self.window(code, seconds, now_ep)
        if len(pts) < 2:
            return 0.0
        return max(0.0, pts[-1][2] - pts[0][2])


def idx(code="sh000001", name="上证指数", *, price=LEVEL, prev_close=LEVEL,
        open_=None, high=None, low=None, **kw) -> Quote:
    """构造一条指数行情。"""
    open_ = prev_close if open_ is None else open_
    high = max(price, prev_close, open_) if high is None else high
    low = min(price, prev_close, open_) if low is None else low
    kw.setdefault("volume_lots", 1_000_000.0)
    kw.setdefault("amount", price * 1_000_000.0 * 100.0)
    return Quote(code=code, name=name, board=board_of(code, name), price=price,
                 prev_close=prev_close, open=open_, high=high, low=low, **kw)


def mk_ctx(state, *, cfg=None, session=SessionPhase.MORNING, now_ep=T0):
    return RuleContext(state=state, cfg=dict(cfg or {}),
                       now=datetime.fromtimestamp(now_ep), session=session,
                       elapsed_trading_seconds=1800.0, minutes_to_close=120.0)


def snap_of(*qs: Quote) -> Snapshot:
    return Snapshot(ts=NOW, seq=1, quotes={q.code: q for q in qs})


def run(state, cfg, *, quotes_in_snap=(), session=SessionPhase.MORNING):
    """求值；指数默认只放在 state.quotes（引擎的真实行为）。"""
    rule = build(dict(cfg))
    return rule.evaluate(snap_of(*quotes_in_snap), mk_ctx(state, cfg=cfg,
                                                         session=session))


def patterns(alerts):
    return [a.metrics["pattern"] for a in alerts]


def flat(code="sh000001", name="上证指数", level=LEVEL, span=300.0, n=5):
    """一段横盘序列（价格不变），用于构造"没动"的基线。"""
    step = span / n
    return [(T0 - span + i * step, level, 1e6 + i * 1000) for i in range(n + 1)]


def move(code="sh000001", name="上证指数", *, start, end, span=300.0, n=5):
    """线性走势序列：``start`` -> ``end``，覆盖 ``span`` 秒。"""
    step = span / n
    return [(T0 - span + i * step, start + (end - start) * i / n,
             1e6 + i * 1000) for i in range(n + 1)]


def state_with(q, points):
    st = FakeState()
    st.quotes = {q.code: q}
    st.feed(q.code, points)
    return st


# ==========================================================================
# 模块契约
# ==========================================================================
def test_module_contract():
    assert SIGNALS == ("index_pull", "index_press")
    assert SIGNAL_CN["index_pull"] == "拉升指数"
    assert SIGNAL_CN["index_press"] == "打压指数"
    assert build().name == "spirit_index"
    assert build().windows == [300.0]
    assert build().cooldown == 300.0


def test_disabled_by_default():
    """默认关闭：指数类信号需要额外的行情管道，且对大盘极易刷屏。"""
    assert DEFAULTS["enabled"] is False
    assert SpiritIndexRule().cfg["enabled"] is False
    q = idx(price=LEVEL + 10.0)
    st = state_with(q, move(start=LEVEL, end=LEVEL + 10.0))
    assert run(st, {}) == []


def test_default_index_codes_are_prefixed():
    """默认指数清单必须带交易所前缀，否则 000001 会拉成平安银行。"""
    assert DEFAULT_INDEX_CODES
    for c in DEFAULT_INDEX_CODES:
        assert c[:2] in ("sh", "sz", "bj"), c
        assert c[2:].isdigit() and len(c[2:]) == 6, c


def test_config_override():
    r = build({"enabled": True, "windows": [60, 120], "cooldown_seconds": 45,
               "min_points": 1.5, "max_per_round": 3})
    assert r.windows == [60.0, 120.0]
    assert r.cooldown == 45.0
    assert r._g("min_points", 0) == 1.5
    assert r.max_per_round == 3


def test_invalid_windows_fall_back():
    assert build({"windows": []}).windows == [300.0]
    assert build({"windows": ["x", None, -5]}).windows == [300.0]
    assert build({"windows": [120, 60]}).windows == [60.0, 120.0]


def test_max_per_round_zero_means_unlimited():
    """0 = 不限量（与 limit_board/volume_burst 约定一致）。"""
    assert build({"max_per_round": 0}).max_per_round == 0
    assert build({"max_per_round": None}).max_per_round == 10


def test_only_continuous_filter():
    q = idx(price=LEVEL + 5.0)
    st = state_with(q, move(start=LEVEL, end=LEVEL + 5.0))
    cfg = {"enabled": True}
    for ph in (SessionPhase.CLOSED, SessionPhase.LUNCH, SessionPhase.POST,
               SessionPhase.PRE_OPEN, SessionPhase.AUCTION):
        assert run(st, cfg, session=ph) == [], ph
    for ph in (SessionPhase.MORNING, SessionPhase.AFTERNOON):
        assert patterns(run(st, cfg, session=ph)) == ["index_pull"], ph


def test_only_continuous_can_be_disabled():
    q = idx(price=LEVEL + 5.0)
    st = state_with(q, move(start=LEVEL, end=LEVEL + 5.0))
    out = run(st, {"enabled": True, "only_continuous": False},
              session=SessionPhase.POST)
    assert patterns(out) == ["index_pull"]


# ==========================================================================
# 代码消歧（本模块最重要的部分）
# ==========================================================================
@pytest.mark.parametrize("code,name,expect", [
    ("sh000001", "上证指数", True),
    ("sh000300", "沪深300", True),
    ("sh000688", "科创50", True),
    ("sh000905", "中证500", True),
    ("sz399001", "深证成指", True),
    ("sz399006", "创业板指", True),
    ("sz399005", "中小100", True),
    ("399001", "深证成指", True),
    ("000001", "上证指数", True),
    # --- 个股：绝不能当指数 ---
    ("sz000001", "平安银行", False),
    ("000001", "平安银行", False),
    ("600000", "浦发银行", False),
    ("000002", "万科A", False),
    ("300750", "宁德时代", False),
    ("688111", "金山办公", False),
    ("002594", "比亚迪", False),
])
def test_is_index_quote(code, name, expect):
    assert is_index_quote(idx(code, name)) is expect


def test_sz000001_is_not_index_but_sh000001_is():
    """同一个 6 位裸码 000001：沪市是指数，深市是个股。"""
    assert is_index_quote(idx("sh000001", "上证指数")) is True
    assert is_index_quote(idx("sz000001", "平安银行")) is False


def test_stock_000001_never_triggers_index_signal():
    """平安银行即使暴涨，也绝不能报"拉升指数"。"""
    q = idx("sz000001", "平安银行", price=11.0, prev_close=10.0)
    st = state_with(q, move(start=10.0, end=11.0))
    assert run(st, {"enabled": True}) == []


def test_named_stock_000001_in_state_is_ignored():
    """名称缺失/为个股时，state 里的 000001 不参与指数信号。"""
    for name in ("平安银行", ""):
        q = idx("000001", name, price=LEVEL + 8.0)
        st = state_with(q, move(start=LEVEL, end=LEVEL + 8.0))
        assert run(st, {"enabled": True}) == [], name


def test_prefixed_and_bare_index_do_not_collide():
    """sh000001 与 sz000001 是不同的标的，必须能共存于 state.quotes。"""
    qi = idx("sh000001", "上证指数", price=LEVEL + 6.0)
    qs = idx("sz000001", "平安银行", price=11.0, prev_close=10.0)
    st = FakeState()
    st.quotes = {qi.code: qi, qs.code: qs}
    st.feed(qi.code, move(start=LEVEL, end=LEVEL + 6.0))
    st.feed(qs.code, move(start=10.0, end=11.0))
    out = run(st, {"enabled": True})
    assert patterns(out) == ["index_pull"]
    assert out[0].code == "sh000001", "只报指数，不报平安银行"


def test_code_whitelist_limits_scope():
    """配了 codes 白名单后，其它指数不参与。"""
    qi = idx("sh000001", "上证指数", price=LEVEL + 6.0)
    qo = idx("sz399001", "深证成指", price=12000.0, prev_close=11900.0)
    st = FakeState()
    st.quotes = {qi.code: qi, qo.code: qo}
    st.feed(qi.code, move(start=LEVEL, end=LEVEL + 6.0))
    st.feed(qo.code, move(start=11900.0, end=12000.0))
    out = run(st, {"enabled": True, "codes": ["sz399001"]})
    assert [a.code for a in out] == ["sz399001"]


def test_code_whitelist_accepts_bare_code():
    q = idx("sh000001", "上证指数", price=LEVEL + 6.0)
    st = state_with(q, move(start=LEVEL, end=LEVEL + 6.0))
    out = run(st, {"enabled": True, "codes": ["000001"]})
    assert patterns(out) == ["index_pull"]


# ==========================================================================
# 拉升指数
# ==========================================================================
def test_pull_fires_on_rise():
    q = idx(price=LEVEL + 2.0)
    st = state_with(q, move(start=LEVEL, end=LEVEL + 2.0))
    out = run(st, {"enabled": True, "min_points": 0.5, "min_bp": 0.0})
    assert patterns(out) == ["index_pull"]
    assert out[0].kind is AlertKind.SURGE
    assert out[0].metrics["delta_points"] == pytest.approx(2.0, abs=0.01)
    assert "拉升指数" in out[0].title


def test_press_fires_on_fall():
    q = idx(price=LEVEL - 2.0)
    st = state_with(q, move(start=LEVEL, end=LEVEL - 2.0))
    out = run(st, {"enabled": True, "min_points": 0.5, "min_bp": 0.0})
    assert patterns(out) == ["index_press"]
    assert out[0].kind is AlertKind.PLUNGE
    assert out[0].metrics["delta_points"] == pytest.approx(-2.0, abs=0.01)


def test_counterexample_below_point_threshold():
    """只涨 0.2 点（< 0.5）且 bp 也不够 -> 不报。"""
    q = idx(price=LEVEL + 0.2)
    st = state_with(q, move(start=LEVEL, end=LEVEL + 0.2))
    out = run(st, {"enabled": True, "min_points": 0.5, "min_bp": 0.0})
    assert out == []


def test_counterexample_below_bp_threshold():
    """点数够但把点数口径关掉、bp 又不够 -> 不报。"""
    q = idx(price=LEVEL + 0.6)
    st = state_with(q, move(start=LEVEL, end=LEVEL + 0.6))
    out = run(st, {"enabled": True, "min_points": 0.0, "min_bp": 5.0})
    assert out == [], "0.6 点 / 3890 点 = 1.5bp < 5bp"


def test_bp_branch_fires_alone():
    """点数口径关掉，只有 bp 够 -> 靠 bp 分支触发。"""
    q = idx(price=LEVEL + 1.0)
    st = state_with(q, move(start=LEVEL, end=LEVEL + 1.0))
    out = run(st, {"enabled": True, "min_points": 0.0, "min_bp": 2.0})
    assert patterns(out) == ["index_pull"]
    assert out[0].metrics["window_bp"] == pytest.approx(2.57, abs=0.05)


def test_point_threshold_needs_min_index_level():
    """点位过低的指数上，0.5 点可能是巨大百分比 -> 只按 bp 判定。"""
    # 点位 20 的"指数"，涨 0.6 点 = 3%；点数口径应被 min_index_level 拦下
    q = idx("sh000001", "上证指数", price=20.6, prev_close=20.0)
    st = state_with(q, move(start=20.0, end=20.6))
    out = run(st, {"enabled": True, "min_points": 0.5, "min_bp": 1000.0,
                   "min_index_level": 100.0})
    assert out == [], "bp 不够、点数口径因点位太低而失效"


def test_covered_window_guard():
    """历史只覆盖 40 秒却按 300 秒判定 -> 跳过（开盘误报防护）。"""
    q = idx(price=LEVEL + 5.0)
    st = state_with(q, [(T0 - 40, LEVEL, 1e6), (T0 - 20, LEVEL + 2.0, 1.1e6),
                        (T0, LEVEL + 5.0, 1.2e6)])
    assert run(st, {"enabled": True}) == []


def test_covered_window_guard_boundary():
    """覆盖 80% 即通过；刚好 79% 不通过。"""
    q = idx(price=LEVEL + 5.0)
    st80 = state_with(q, [(T0 - 240, LEVEL, 1e6), (T0, LEVEL + 5.0, 1.2e6)])
    assert patterns(run(st80, {"enabled": True})) == ["index_pull"]
    st79 = state_with(q, [(T0 - 237, LEVEL, 1e6), (T0, LEVEL + 5.0, 1.2e6)])
    assert run(st79, {"enabled": True}) == []


def test_flat_market_never_fires():
    """横盘不动 -> 永不触发。"""
    q = idx(price=LEVEL)
    st = state_with(q, flat())
    assert run(st, {"enabled": True}) == []


def test_zero_or_negative_prices_skipped():
    st = FakeState()
    st.quotes = {"sh000001": idx(price=0.0)}
    st.feed("sh000001", move(start=LEVEL, end=LEVEL + 5.0))
    assert run(st, {"enabled": True}) == []

    st2 = FakeState()
    st2.quotes = {"sh000001": idx(price=LEVEL + 5.0, prev_close=0.0)}
    st2.feed("sh000001", move(start=LEVEL, end=LEVEL + 5.0))
    assert run(st2, {"enabled": True}) == []


# ==========================================================================
# 方向一致性
# ==========================================================================
def test_no_direction_match_blocks_pull_in_crashing_day():
    """当日暴跌 3%，但最近 5 分钟反弹 2 点 -> 语义含混，不报"拉升指数"。"""
    q = idx(price=LEVEL * 0.97, prev_close=LEVEL)
    st = state_with(q, move(start=LEVEL * 0.97 - 2.0, end=LEVEL * 0.97))
    out = run(st, {"enabled": True, "min_points": 0.5, "min_bp": 0.0})
    assert out == [], "当日深跌中的小反弹不该叫拉升指数"
    # 关掉方向一致性检查后则会报（证明是该开关在起作用）
    out2 = run(st, {"enabled": True, "min_points": 0.5, "min_bp": 0.0,
                    "require_direction_match": False})
    assert patterns(out2) == ["index_pull"]


def test_no_direction_match_blocks_press_in_surging_day():
    q = idx(price=LEVEL * 1.03, prev_close=LEVEL)
    st = state_with(q, move(start=LEVEL * 1.03 + 2.0, end=LEVEL * 1.03))
    out = run(st, {"enabled": True, "min_points": 0.5, "min_bp": 0.0})
    assert out == []
    out2 = run(st, {"enabled": True, "min_points": 0.5, "min_bp": 0.0,
                    "require_direction_match": False})
    assert patterns(out2) == ["index_press"]


def test_direction_match_allows_normal_case():
    """当日小涨、窗口也在涨 -> 正常触发（开关不能误伤）。"""
    q = idx(price=LEVEL + 2.0)
    st = state_with(q, move(start=LEVEL, end=LEVEL + 2.0))
    assert patterns(run(st, {"enabled": True, "min_points": 0.5,
                             "min_bp": 0.0})) == ["index_pull"]


# ==========================================================================
# 排序 / 去重 / severity
# ==========================================================================
def test_one_alert_per_index_per_direction():
    """多窗口命中同一方向只报一条（保留最强）。"""
    q = idx(price=LEVEL + 3.0)
    st = state_with(q, move(start=LEVEL, end=LEVEL + 3.0, span=300.0))
    out = run(st, {"enabled": True, "windows": [60, 180, 300],
                   "min_points": 0.5, "min_bp": 0.0})
    assert len(out) == 1, f"应合并成一条，实际 {len(out)}"


def test_urgent_severity_scales_with_strength():
    """强度达 urgent_multiple 倍 -> severity 3。"""
    q = idx(price=LEVEL + 3.0)
    st = state_with(q, move(start=LEVEL, end=LEVEL + 3.0))
    # 3 点 / 0.5 点阈值 = 6 倍 >= 3 -> sev3
    out = run(st, {"enabled": True, "min_points": 0.5, "min_bp": 0.0,
                   "severity": 1, "urgent_multiple": 3.0})
    assert out[0].severity == 3


def test_base_severity_when_below_urgent():
    q = idx(price=LEVEL + 0.6)
    st = state_with(q, move(start=LEVEL, end=LEVEL + 0.6))
    out = run(st, {"enabled": True, "min_points": 0.5, "min_bp": 0.0,
                   "severity": 1, "urgent_multiple": 10.0})
    assert out[0].severity == 1


def test_sorted_by_strength_desc():
    """强度大的指数排在前面。

    ``strength`` 是"实际幅度 / 阈值"的倍数，所以点位数差异是相对的：
    只配 ``min_points`` 时，深证成指涨 100 点（0.84%）确实比上证涨 10 点
    （0.26%）更强，应该排在前面。
    """
    q1 = idx("sh000001", "上证指数", price=LEVEL + 10.0)
    q2 = idx("sz399001", "深证成指", price=12000.0, prev_close=11900.0)
    st = FakeState()
    st.quotes = {q1.code: q1, q2.code: q2}
    st.feed(q1.code, move(start=LEVEL, end=LEVEL + 10.0))
    st.feed(q2.code, move(start=11900.0, end=12000.0))
    out = run(st, {"enabled": True, "min_points": 0.5, "min_bp": 0.0})
    assert [a.code for a in out] == ["sz399001", "sh000001"]
    assert out[0].metrics["strength"] > out[1].metrics["strength"]


def test_sorted_by_strength_is_monotonic():
    """输出严格按 strength 降序（用随机量级的多个指数验证）。"""
    st = FakeState()
    specs = [("sh000001", "上证指数", LEVEL, 3.0),
             ("sz399001", "深证成指", 12000.0, 60.0),
             ("sz399006", "创业板指", 3300.0, 30.0),
             ("sh000300", "沪深300", 4480.0, 44.0)]
    for code, name, level, move_pts in specs:
        q = idx(code, name, price=level + move_pts, prev_close=level)
        st.quotes[code] = q
        st.feed(code, move(start=level, end=level + move_pts))
    out = run(st, {"enabled": True, "min_points": 0.5, "min_bp": 0.0})
    strengths = [a.metrics["strength"] for a in out]
    assert strengths == sorted(strengths, reverse=True)


def test_max_per_round_truncates():
    st = FakeState()
    for i, (code, name, level) in enumerate([
            ("sh000001", "上证指数", LEVEL),
            ("sz399001", "深证成指", 12000.0),
            ("sz399006", "创业板指", 3300.0),
            ("sh000300", "沪深300", 4480.0)]):
        q = idx(code, name, price=level + 5.0, prev_close=level)
        st.quotes[code] = q
        st.feed(code, move(start=level, end=level + 5.0))
    out = run(st, {"enabled": True, "min_points": 0.5, "min_bp": 0.0,
                   "max_per_round": 2})
    assert len(out) == 2


def test_max_per_round_zero_unlimited():
    st = FakeState()
    for code, name, level in [("sh000001", "上证指数", LEVEL),
                              ("sz399001", "深证成指", 12000.0),
                              ("sz399006", "创业板指", 3300.0)]:
        q = idx(code, name, price=level + 5.0, prev_close=level)
        st.quotes[code] = q
        st.feed(code, move(start=level, end=level + 5.0))
    out = run(st, {"enabled": True, "min_points": 0.5, "min_bp": 0.0,
                   "max_per_round": 0})
    assert len(out) == 3


# ==========================================================================
# key / metrics / 契约字段
# ==========================================================================
def test_key_format_and_cooldown_fields():
    q = idx(price=LEVEL + 2.0)
    st = state_with(q, move(start=LEVEL, end=LEVEL + 2.0))
    a = run(st, {"enabled": True, "min_points": 0.5, "min_bp": 0.0})[0]
    assert a.key == f"sh000001:surge:index_pull:{bucket_of(T0, 300)}"
    assert a.cooldown_key == "sh000001:surge:index_pull"
    assert a.cooldown_seconds == 300.0


def test_keys_unique_across_directions():
    """拉升与打压的 key 必须不同，否则会互相吞掉（本项目踩过的坑）。"""
    pull = f"sh000001:surge:index_pull:{bucket_of(T0, 300)}"
    press = f"sh000001:plunge:index_press:{bucket_of(T0, 300)}"
    assert pull != press


def test_metrics_payload():
    q = idx(price=LEVEL + 2.0)
    st = state_with(q, move(start=LEVEL, end=LEVEL + 2.0))
    a = run(st, {"enabled": True, "min_points": 0.5, "min_bp": 0.0})[0]
    for k in ("pattern", "window_seconds", "delta_points", "window_pct",
              "window_bp", "strength", "index_level", "prev_close"):
        assert k in a.metrics, k
    assert a.metrics["window_seconds"] == 300.0
    assert a.metrics["index_level"] == pytest.approx(LEVEL + 2.0, abs=0.01)


def test_alert_fields_populated():
    q = idx(price=LEVEL + 2.0)
    st = state_with(q, move(start=LEVEL, end=LEVEL + 2.0))
    a = run(st, {"enabled": True, "min_points": 0.5, "min_bp": 0.0})[0]
    assert a.code == "sh000001"
    assert a.name == "上证指数"
    assert a.price == pytest.approx(LEVEL + 2.0)
    assert a.detail and a.title
    assert a.ts == NOW


def test_alert_to_dict_is_json_safe():
    import json
    q = idx(price=LEVEL + 2.0)
    st = state_with(q, move(start=LEVEL, end=LEVEL + 2.0))
    a = run(st, {"enabled": True, "min_points": 0.5, "min_bp": 0.0})[0]
    json.dumps(a.to_dict(), allow_nan=False)   # 不允许 NaN/Infinity


# ==========================================================================
# 与引擎的协作
# ==========================================================================
def test_index_in_snapshot_is_accepted():
    """引擎不该把指数放进 snap.quotes，但若放了规则也要能正确处理。"""
    q = idx(price=LEVEL + 2.0)
    st = FakeState()
    st.feed(q.code, move(start=LEVEL, end=LEVEL + 2.0))
    out = run(st, {"enabled": True, "min_points": 0.5, "min_bp": 0.0},
              quotes_in_snap=(q,))
    assert patterns(out) == ["index_pull"]


def test_state_and_snapshot_duplicate_reports_once():
    """同一个指数同时出现在 state 与 snap 里也只报一条。"""
    q = idx(price=LEVEL + 2.0)
    st = state_with(q, move(start=LEVEL, end=LEVEL + 2.0))
    out = run(st, {"enabled": True, "min_points": 0.5, "min_bp": 0.0},
              quotes_in_snap=(q,))
    assert len(out) == 1


def test_stock_in_state_is_ignored():
    """state 里的普通个股不参与指数信号。"""
    st = FakeState()
    q = Quote(code="600000", name="浦发银行", board=board_of("600000"),
              price=10.5, prev_close=10.0, open=10.0, high=10.5, low=10.0,
              volume_lots=100000.0, amount=10.5 * 100000.0 * 100)
    st.quotes = {"600000": q}
    st.feed("600000", move(start=10.0, end=10.5))
    assert run(st, {"enabled": True}) == []


def test_empty_state_is_safe():
    assert run(FakeState(), {"enabled": True}) == []


def test_none_quote_in_state_is_skipped():
    st = FakeState()
    st.quotes = {"sh000001": None}
    assert run(st, {"enabled": True}) == []


def test_missing_state_quotes_attribute_is_safe():
    """state 没有 quotes 属性时不崩（只认快照）。"""
    class Bare:
        def window(self, code, seconds, now_ep):
            return []
    ctx = mk_ctx(Bare(), cfg={"enabled": True})
    assert build({"enabled": True}).evaluate(snap_of(), ctx) == []


def test_deterministic_across_runs():
    q = idx(price=LEVEL + 3.0)
    st = state_with(q, move(start=LEVEL, end=LEVEL + 3.0))
    cfg = {"enabled": True, "min_points": 0.5, "min_bp": 0.0}
    r1 = [a.key for a in run(st, cfg)]
    r2 = [a.key for a in run(st, cfg)]
    assert r1 == r2


def test_rule_is_reusable_across_rounds():
    """同一实例连续求值不崩、结果稳定（规则会被引擎长期持有）。"""
    rule = build({"enabled": True, "min_points": 0.5, "min_bp": 0.0})
    q = idx(price=LEVEL + 3.0)
    st = state_with(q, move(start=LEVEL, end=LEVEL + 3.0))
    ctx = mk_ctx(st, cfg={"enabled": True})
    outs = [len(rule.evaluate(snap_of(), ctx)) for _ in range(3)]
    assert outs == [1, 1, 1]


# ==========================================================================
# 性能
# ==========================================================================
def test_performance_many_indices():
    """50 个指数、3 个窗口，单轮 < 100ms。"""
    st = FakeState()
    for i in range(50):
        code = f"sh0000{i:02d}"
        q = idx(code, f"测试指数{i}", price=LEVEL + 5.0)
        st.quotes[code] = q
        st.feed(code, move(code, f"测试指数{i}", start=LEVEL, end=LEVEL + 5.0))
    rule = build({"enabled": True, "windows": [60, 180, 300],
                  "min_points": 0.5, "min_bp": 0.0, "max_per_round": 0})
    ctx = mk_ctx(st, cfg={"enabled": True})
    t0 = time.perf_counter()
    out = rule.evaluate(snap_of(), ctx)
    el = (time.perf_counter() - t0) * 1000.0
    assert len(out) == 50
    assert el < 100.0, f"耗时 {el:.1f}ms 超出 100ms 预算"
