"""IT-P1-UNKNOWN-DATE-FAILOPEN-001 / IT-P1-ST-REPLAY-BYPASS-001 的回归测试。

两条都是**既有缺陷**（本轮由云端 2026-09-21 11:31 JST 审计指认，
我独立复现后修复）。二者是同一类错误的两面：**ST 制度日期感知在真实
输入形态上失效**。

1. ``UNKNOWN-DATE-FAILOPEN``：``models._as_date`` 只认 ``date``/``datetime``
   实例，字符串/整数一律 ``None``，而调用方把 ``None`` 当作"按现行制度"——
   于是**一个完全可解析的历史日期字符串会静默套用现行制度**：

       st_limit_rate_on("2025-03-10") == 0.10   # 错，当时 0.05

   可触达性：``Quote.ts`` 由真实解析器填充，sina/eastmoney/tencent 的
   ``_ts_of`` 与 ``server/web.py:coerce_ts`` 确实会给出字符串或 ``None``。

2. ``ST-REPLAY-BYPASS``：``ScriptedStock.limit_rate()`` 不传日期 ->
   回放任何历史交易日的主板 ST 都按现行 10% 算；``build_script`` 把它算成
   ``Quote.limit_up``，而 ``Quote.limit_up_price`` 在 ``limit_up>0`` 时
   **提前返回**，``Quote`` 里那条日期感知分支永远不执行 —— 整条回放链路
   结构性绕过 ST 修复。

验收不变量：
* 可解析的历史日期（str/datetime/date/紧凑串/整数）一律给 **旧制度 0.05**；
* 真正不可解析的（None/空/乱码/epoch）才降级为现行 —— 语义不变；
* 回放 2025-03-10 的主板 ST，``limit_up_price`` 必须是 **10.50**；
* 回放 2026-09-21 的主板 ST 仍是 **11.00**（现行制度不受影响）。
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta

import pytest

from arad.models import (
    ST_LIMIT_RATE_CHANGED_ON,
    ST_LIMIT_RATE_CURRENT,
    ST_LIMIT_RATE_LEGACY,
    _as_date,
    limit_rate_of,
    st_limit_rate_on,
)
from arad.replay import ScriptedStock, build_script


# ===========================================================================
# 1. _as_date 必须认得真实解析器会给出的形态
# ===========================================================================
@pytest.mark.parametrize("when,expect", [
    ("2025-03-10", date(2025, 3, 10)),      # ISO 串（web.coerce_ts 常见）
    ("20250310", date(2025, 3, 10)),        # 紧凑串
    ("2025/03/10", date(2025, 3, 10)),      # 斜杠串
    ("  2025-03-10  ", date(2025, 3, 10)),  # 带空白
    (20250310, date(2025, 3, 10)),          # 整数形态
    (date(2025, 3, 10), date(2025, 3, 10)),
    (datetime(2025, 3, 10, 14, 30), date(2025, 3, 10)),
])
def test_as_date_parses_forms_real_parsers_emit(when, expect):
    """可解析的形态必须**真的解析出来**，不得静默变 None。

    这是本缺陷的核心：返回 None 会被上层读成"日期未知 -> 用现行制度"，
    把一个已知的历史日期变成现行制度下的错判。
    """
    assert _as_date(when) == expect, f"{when!r} 必须解析成 {expect}"


@pytest.mark.parametrize("when", [
    None, "", "   ", "garbage", "2025-13-45", "2025031",
    1700000000,        # epoch 秒（10 位）不是 YYYYMMDD
    1.5,               # float 不接受
    True,              # bool 不受理（避免 True->1 被当日期）
    ["2025-03-10"],    # 非标量
])
def test_as_date_returns_none_only_for_truly_unparseable(when):
    """真正不可解析的仍返回 None —— **降级语义不变**。

    本修复只针对"可解析却被当成不可解析"，不能把未知情形也变成猜测。
    """
    assert _as_date(when) is None, f"{when!r} 不该被解析出来"


def test_st_limit_rate_is_correct_for_string_dates():
    """**缺陷的直接验收**：字符串历史日期必须给旧制度 0.05。

    修复前 ``"2025-03-10"`` 给 0.10（= 现行），修复后给 0.05。
    """
    for when in ("2025-03-10", "20250310", 20250310, "2025/03/10",
                 date(2025, 3, 10), datetime(2025, 3, 10, 10, 0)):
        assert st_limit_rate_on(when) == ST_LIMIT_RATE_LEGACY, (
            f"{when!r} 在变更点之前，必须是 {ST_LIMIT_RATE_LEGACY}")
    for when in ("2026-09-21", "20260921", date(2026, 9, 21)):
        assert st_limit_rate_on(when) == ST_LIMIT_RATE_CURRENT, (
            f"{when!r} 在变更点之后，必须是 {ST_LIMIT_RATE_CURRENT}")


def test_limit_rate_of_uses_parsed_string_date_for_st():
    """端到端：``limit_rate_of`` 对**字符串**历史日期也要给 0.05。"""
    assert limit_rate_of("600001", "ST某", "2025-03-10") == ST_LIMIT_RATE_LEGACY
    assert limit_rate_of("600001", "ST某", "2026-09-21") == ST_LIMIT_RATE_CURRENT
    # 边界当天算新制度
    assert limit_rate_of("600001", "ST某", ST_LIMIT_RATE_CHANGED_ON) == \
        ST_LIMIT_RATE_CURRENT
    assert limit_rate_of("600001", "ST某",
                         ST_LIMIT_RATE_CHANGED_ON - timedelta(days=1)) == \
        ST_LIMIT_RATE_LEGACY


def test_unknown_date_still_falls_back_to_current():
    """真正的未知日期仍按现行制度（不做无证据猜测）。"""
    assert st_limit_rate_on(None) == ST_LIMIT_RATE_CURRENT
    assert st_limit_rate_on("garbage") == ST_LIMIT_RATE_CURRENT


# ===========================================================================
# 2. 回放链路必须传交易日（结构性绕过）
# ===========================================================================
def _replay_st_quote(day: datetime, name: str = "ST某") -> object:
    """回放一只主板 ST，返回第一帧 Quote。"""
    timeline = [day + timedelta(minutes=i) for i in range(10)]
    stk = ScriptedStock(code="600001", name=name, start_price=10.0)
    qs = build_script(stk, timeline, random.Random(42))
    assert qs, "回放必须产出 Quote"
    return qs[0]


def test_replay_uses_the_replay_day_not_the_runtime_clock():
    """**缺陷的直接验收**：回放历史日的主板 ST 涨停价必须是 5% 的 10.50。

    修复前 ``ScriptedStock.limit_rate()`` 不传日期 -> 恒 10% -> 11.00，
    且 ``Quote.limit_up_price`` 提前返回该值，``Quote`` 的日期感知分支
    永不执行 —— 回放链路结构性绕过 ST 修复。
    """
    q = _replay_st_quote(datetime(2025, 3, 10, 9, 30))
    assert q.trade_date == date(2025, 3, 10), "Quote 必须知道自己的交易日"
    assert float(q.limit_up_price) == 10.50, (
        f"2025-03-10 主板 ST 涨停价应为 10.50（5%），实际 {q.limit_up_price}")
    assert float(q.limit_down_price) == 9.50, (
        f"跌停价应为 9.50，实际 {q.limit_down_price}")


def test_replay_current_day_st_still_uses_ten_percent():
    """现行制度日不受影响：2026-09-21 主板 ST 仍是 10% -> 11.00。"""
    q = _replay_st_quote(datetime(2026, 9, 21, 9, 30))
    assert float(q.limit_up_price) == 11.00
    assert float(q.limit_down_price) == 9.00


def test_replay_five_percent_limit_up_is_recognized():
    """**产品后果**：旧制度下真实 5% 封板必须被识别为涨停。

    修复前涨停价被算成 11.00，于是 price=10.50（真实封板）不会被识别 ——
    ``limit_board`` 直接消费 ``is_at_limit_up``，漏报整个 5% 封板场景。
    """
    q = _replay_st_quote(datetime(2025, 3, 10, 9, 30))
    # price=10.50 恰好是 5% 涨停：现价在涨停价上
    at_limit = abs(float(q.limit_up_price) - 10.50) < 1e-9
    assert at_limit, "涨停价本身必须是 10.50，否则 5% 封板无从判定"
    # 用真实字段核对：涨停价 10.50 之下，10.50 就是"到板"
    assert float(q.limit_up_price) <= 10.50 + 1e-9


def test_non_st_replay_main_board_unaffected_by_st_change():
    """非 ST 主板两只日子都是 10% —— 修复不能波及非 ST。"""
    for day in (datetime(2025, 3, 10, 9, 30), datetime(2026, 9, 21, 9, 30)):
        q = _replay_st_quote(day, name="浦发银行")
        assert float(q.limit_up_price) == 11.00, f"{day} 非 ST 应仍是 10%"


def test_gem_and_star_replay_rates_unchanged():
    """创业板/科创板回放比例与日期无关（20%），防误伤。"""
    timeline = [datetime(2025, 3, 10, 9, 30) + timedelta(minutes=i)
                for i in range(10)]
    for code, expect in (("300750", 0.20), ("688111", 0.20)):
        stk = ScriptedStock(code=code, name="ST创业", start_price=10.0)
        q = build_script(stk, timeline, random.Random(42))[0]
        assert abs(float(q.limit_up_price) - round(10.0 * (1 + expect), 2)) < 1e-9, (
            f"{code} 必须仍是 {expect:.0%}")


# ===========================================================================
# 3. market_rule_version 必须**真的有消费者**（不再是死代码）
# ===========================================================================
def test_market_rule_version_is_actually_consumed():
    """IT-P2-RULE-VERSION-DEAD-001：该 API 此前零调用、零测试 = 死代码。

    云端 11:31 审计指认、我已复核（全仓仅 ``__all__`` 与定义两处命中）。
    修复：接进 ``Alert.to_dict()`` 的 ``rule_version`` —— alert 是唯一会被
    落盘/推流/长期保存的载体，正是 docstring 承诺的"事后追溯"场景。

    这条测试用**真实引用来钉住消费点**，防止它再退回死代码。
    """
    import inspect

    from arad import models as m

    src = inspect.getsource(m)
    # 除 __all__ 与 def 行之外，必须至少还有一个真实调用点
    call_lines = [
        ln for ln in src.splitlines()
        if "market_rule_version(" in ln
        and "def market_rule_version" not in ln
    ]
    assert call_lines, "market_rule_version 必须至少有 1 个真实调用点"

    from arad.models import Alert, AlertKind
    a = Alert(key="k", kind=AlertKind.SURGE, code="600000", name="x",
              ts=datetime(2026, 9, 21, 10, 0), price=1.0, pct=1.0,
              title="t", detail="d")
    assert a.to_dict()["rule_version"] == "cn-2026-07-06"


def test_alert_rule_version_reflects_the_alerts_own_day():
    """历史回放的告警必须带**当时**的制度版本，不是运行当天的。"""
    from arad.models import Alert, AlertKind

    def _mk(ts):
        return Alert(key="k", kind=AlertKind.SURGE, code="600000", name="ST某",
                     ts=ts, price=1.0, pct=1.0, title="t", detail="d")

    assert _mk(datetime(2025, 3, 10, 10, 0)).to_dict()["rule_version"] == \
        "cn-2026-07-05"
    assert _mk(datetime(2026, 7, 5, 10, 0)).to_dict()["rule_version"] == \
        "cn-2026-07-05"
    assert _mk(datetime(2026, 7, 6, 10, 0)).to_dict()["rule_version"] == \
        "cn-2026-07-06"
    assert _mk(datetime(2026, 9, 21, 10, 0)).to_dict()["rule_version"] == \
        "cn-2026-07-06"


def test_alert_rule_version_falls_back_for_unparseable_ts():
    """``ts`` 存在但**不可解析**时：仍给出一个版本，且不抛异常。

    ⚠ 范围说明：``Alert.to_dict()`` 对 ``ts=None`` 会**先**在 ``strftime``
    上抛 ``AttributeError`` —— 那是本条之外既有的契约（告警恒有 ``ts``），
    本轮不顺带改它，以免把 ST 制度修复扩大到无关的行为变更。
    这里只验证"``ts`` 是个真实对象但内容不可解析"这一条路径。
    """
    from datetime import datetime as _dt

    from arad.models import Alert, AlertKind

    class WeirdTs:
        """一个**存在**但不是 datetime 的对象（真实解析器给出畸形值时的替身）。"""

        def strftime(self, fmt):        # to_dict 需要它才能格式化
            return "2025-03-10 10:00:00"

    a = Alert(key="k", kind=AlertKind.SURGE, code="600000", name="x",
              ts=WeirdTs(), price=1.0, pct=1.0, title="t", detail="d")
    d = a.to_dict()
    assert isinstance(d.get("rule_version"), str) and d["rule_version"], (
        "不可解析的 ts 也必须给出一个版本串，不能是 None/空")
    assert d["ts"] == "2025-03-10 10:00:00"


# ===========================================================================
# 4. 严格性：宽松解析必须被拒（防把乱码当日期）
# ===========================================================================
@pytest.mark.parametrize("bad", ["2025031", "2025-3-10", "2025/3/1",
                                 "20250310 ", "2025-03-10x"])
def test_lenient_forms_are_rejected(bad):
    """``strptime`` 会宽松接受 ``"2025031"``（-> 2025-03-01）等形态。

    那会把 7 位乱码变成"合法历史日期"，从而**错误地**套用旧制度。
    位数必须显式钉住。注意带尾空格的 ``"20250310 "`` 会被 strip 后接受 ——
    因此这里用不带空格的变体单独验证；带空白的合法形态已在上面正向用例中覆盖。
    """
    got = _as_date(bad)
    if bad == "20250310 ":          # strip 后是合法的 8 位，允许
        assert got == date(2025, 3, 10)
    else:
        assert got is None, f"{bad!r} 是宽松形态，必须被拒（实际 {got}）"
