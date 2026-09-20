"""交易时段与板块判定：边界回归测试。

这些断言覆盖 A 股硬规则（涨跌停比例、板块归属、时段边界），
任何一条挂掉都意味着会真金白银地误报或漏报。
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from arad.models import (Alert, AlertKind, Board, board_of, guess_prefix,
                         limit_rate_of, Quote)
from arad.session import CONTINUOUS, SessionPhase, TradingCalendar


def at(h: int, m: int, s: int = 0, d: date = date(2026, 9, 15)) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, s)


@pytest.fixture
def cal() -> TradingCalendar:
    return TradingCalendar(holidays=set())      # 只测时段，不叠加节假日


# ==========================================================================
# 时段边界
# ==========================================================================
@pytest.mark.parametrize("dt,expect,desc", [
    (at(0, 0), SessionPhase.CLOSED, "午夜"),
    (at(9, 14, 59), SessionPhase.CLOSED, "集合竞价前"),
    (at(9, 15), SessionPhase.CALL_AUCTION, "集合竞价开始"),
    (at(9, 24, 59), SessionPhase.CALL_AUCTION, "集合竞价末"),
    (at(9, 25), SessionPhase.SILENCE, "竞价结束→静默"),
    (at(9, 29, 59), SessionPhase.SILENCE, "静默末"),
    (at(9, 30), SessionPhase.MORNING, "开盘"),
    (at(11, 29, 59), SessionPhase.MORNING, "上午末秒"),
    (at(11, 30), SessionPhase.LUNCH, "午休开始（左闭右开）"),
    (at(12, 59, 59), SessionPhase.LUNCH, "午休末秒"),
    (at(13, 0), SessionPhase.AFTERNOON, "下午开盘"),
    (at(14, 56, 59), SessionPhase.AFTERNOON, "连续竞价末秒"),
    (at(14, 57), SessionPhase.CLOSE_AUCTION, "收盘集合竞价开始（IT-P0-001）"),
    (at(14, 59, 59), SessionPhase.CLOSE_AUCTION, "收盘集合竞价末秒"),
    (at(15, 0), SessionPhase.POST, "收盘瞬间即 POST"),
    (at(23, 59, 59), SessionPhase.POST, "深夜"),
])
def test_phase_boundaries(cal, dt, expect, desc):
    assert cal.phase(dt) is expect, desc


def test_auction_aliases_match():
    """语义别名必须指向同一个成员，避免"以为拿到竞价、实为静默"。"""
    assert SessionPhase.CALL_AUCTION is SessionPhase.PRE_OPEN
    assert SessionPhase.SILENCE is SessionPhase.AUCTION
    assert SessionPhase.PRE_OPEN.value == "pre_open"
    assert SessionPhase.AUCTION.value == "auction"


def test_continuous_contains_only_two_sessions():
    assert set(CONTINUOUS) == {SessionPhase.MORNING, SessionPhase.AFTERNOON}
    assert SessionPhase.LUNCH not in CONTINUOUS
    assert SessionPhase.PRE_OPEN not in CONTINUOUS
    # IT-P0-001：收盘集合竞价不是连续竞价
    assert SessionPhase.CLOSE_AUCTION not in CONTINUOUS


def test_is_open_matches_phase(cal):
    assert cal.is_open(at(10, 0)) is True
    assert cal.is_open(at(14, 0)) is True
    assert cal.is_open(at(11, 30)) is False
    assert cal.is_open(at(15, 0)) is False
    assert cal.is_open(at(9, 20)) is False      # 集合竞价不可连续竞价成交

    # --- IT-P0-001：14:57-15:00 是收盘集合竞价，不是连续竞价 ---
    assert cal.is_open(at(14, 56, 59)) is True
    assert cal.is_open(at(14, 57)) is False, "收盘集合竞价不是连续竞价"
    assert cal.is_open(at(14, 58)) is False
    assert cal.is_open(at(14, 59, 59)) is False


def test_close_auction_is_still_observable(cal):
    """收盘集合竞价不算连续竞价，但**必须**仍然抓行情（价格确实在动）。

    这是"不过度修正"的守卫：把 14:57-15:00 从抓取窗口里一并排除，
    同样会丢数据 —— 只是错误方向相反。
    """
    assert cal.is_call_auction(at(9, 20)) is True
    assert cal.is_call_auction(at(14, 58)) is True
    assert cal.is_call_auction(at(10, 0)) is False

    assert cal.is_tradable_window(at(14, 58)) is True, "收盘集合竞价应可观测"
    assert cal.is_tradable_window(at(9, 20)) is True
    assert cal.is_tradable_window(at(9, 27)) is False, "静默期不可观测"
    assert cal.is_tradable_window(at(15, 0)) is False


def test_elapsed_trading_seconds_full_day_is_14220(cal):
    """连续竞价时长止于 **14:57**（IT-P0-001），全天 14220 秒而非 14400。"""
    assert cal.elapsed_trading_seconds(at(9, 30)) == 0.0
    assert cal.elapsed_trading_seconds(at(10, 0)) == 1800.0
    assert cal.elapsed_trading_seconds(at(11, 30)) == 7200.0
    assert cal.elapsed_trading_seconds(at(13, 0)) == 7200.0
    # 13:00-14:57 = 7020 秒；14:57-15:00 是收盘集合竞价，不计入连续竞价
    assert cal.elapsed_trading_seconds(at(14, 57)) == 7200.0 + 7020.0
    assert cal.elapsed_trading_seconds(at(14, 57)) == 14220.0
    # 收盘集合竞价期间与收盘后，连续竞价时长都**不再增长**
    assert cal.elapsed_trading_seconds(at(14, 58)) == 14220.0
    assert cal.elapsed_trading_seconds(at(15, 0)) == 14220.0
    assert cal.elapsed_trading_seconds(at(20, 0)) == 14220.0


def test_elapsed_freezes_across_close_auction(cal):
    """收盘集合竞价窗口内分母必须冻结。

    这是 IT-P0-001 的**实质**后果（12:07 报告要求）：若量能时钟仍走到 15:00，
    ``volume_burst`` 的 ``avg_per_min = volume_lots / elapsed * 60`` 会出现
    "分子不动、分母继续涨"，把放量速率人为稀释 —— 只加枚举不改时钟不算修完。
    """
    a = cal.elapsed_trading_seconds(at(14, 57))
    b = cal.elapsed_trading_seconds(at(14, 58))
    c = cal.elapsed_trading_seconds(at(14, 59, 59))
    assert a == b == c, f"收盘集合竞价期间分母应冻结：{a} / {b} / {c}"


def test_elapsed_never_decreases(cal):
    """已交易秒数必须单调不减（量能节奏对比的前提）。"""
    prev = -1.0
    for h in range(9, 16):
        for m in (0, 15, 30, 45):
            v = cal.elapsed_trading_seconds(at(h, m))
            assert v >= prev, f"{h}:{m:02d} 倒退"
            prev = v


def test_non_trading_day_is_always_closed(cal):
    for d in (date(2026, 9, 19), date(2026, 9, 20)):    # 周六、周日
        assert cal.phase(at(10, 0, d=d)) is SessionPhase.CLOSED
        assert cal.is_trading_day(d) is False
        assert cal.elapsed_trading_seconds(at(10, 0, d=d)) == 0.0


def test_holiday_is_not_trading_day():
    cal = TradingCalendar(holidays={"2026-10-01"})
    assert cal.is_trading_day(date(2026, 10, 1)) is False
    assert cal.phase(at(10, 0, d=date(2026, 10, 1))) is SessionPhase.CLOSED


def test_next_open_skips_weekend_and_holiday():
    cal = TradingCalendar(holidays={"2026-09-21"})       # 周一放假
    nxt = cal.next_open(datetime(2026, 9, 18, 16, 0))    # 周五收盘后
    assert nxt == datetime(2026, 9, 22, 9, 30)           # 跳过周六日 + 周一


def test_minutes_to_close(cal):
    assert cal.minutes_to_close(at(15, 0)) == 0.0
    assert cal.minutes_to_close(at(20, 0)) == 0.0
    assert cal.minutes_to_close(at(14, 0)) == pytest.approx(60.0)
    assert cal.minutes_to_close(at(9, 30)) == pytest.approx(330.0)   # 5.5 小时


# ==========================================================================
# 板块与涨跌幅限制（A 股硬规则）
# ==========================================================================
@pytest.mark.parametrize("code,name,board,rate", [
    ("600000", "浦发银行", Board.MAIN, 0.10),
    ("601318", "中国平安", Board.MAIN, 0.10),
    ("603259", "药明康德", Board.MAIN, 0.10),
    ("605499", "东鹏饮料", Board.MAIN, 0.10),
    ("000001", "平安银行", Board.MAIN, 0.10),
    ("001979", "招商蛇口", Board.MAIN, 0.10),
    ("002594", "比亚迪", Board.MAIN, 0.10),
    ("003816", "中国广核", Board.MAIN, 0.10),
    ("300750", "宁德时代", Board.GEM, 0.20),
    ("301029", "怡合达", Board.GEM, 0.20),
    ("688111", "金山办公", Board.STAR, 0.20),
    ("689009", "九号公司", Board.STAR, 0.20),
    ("830799", "艾融软件", Board.BJ, 0.30),
    ("430047", "诺思兰德", Board.BJ, 0.30),
    ("920001", "某北交所", Board.BJ, 0.30),
    ("600001", "ST某", Board.MAIN, 0.10),         # 2026-07-06 起主板 ST 为 10%
    ("000002", "*ST某", Board.MAIN, 0.10),
    ("300001", "ST创业板", Board.GEM, 0.20),      # ST 不改变双创 20%
])
def test_board_and_limit_rate(code, name, board, rate):
    assert board_of(code, name) is board
    assert limit_rate_of(code, name) == pytest.approx(rate)


@pytest.mark.parametrize("code,name,when,rate", [
    # 主板风险警示：制度在 2026-07-06 由 5% 变 10%（IT-P1-MARKET-RULE-20260706-001）
    ("600001", "ST某", date(2026, 7, 3), 0.05),
    ("600001", "ST某", date(2026, 7, 5), 0.05),
    ("600001", "ST某", date(2026, 7, 6), 0.10),     # 边界当天即新规
    ("000002", "*ST某", date(2026, 7, 3), 0.05),
    ("000002", "*ST某", date(2026, 7, 6), 0.10),
    ("600001", "ST某", datetime(2026, 7, 5, 14, 30), 0.05),
    ("600001", "ST某", None, 0.10),                 # 无日期 = 现行制度
    # 双创 ST 一直是 20%，不因日期变
    ("300001", "ST创业板", date(2026, 7, 3), 0.20),
    ("300001", "ST创业板", date(2026, 9, 21), 0.20),
    ("688001", "ST科创", date(2026, 7, 3), 0.20),
    # 非 ST 主板不受该制度影响
    ("600000", "浦发银行", date(2026, 7, 3), 0.10),
    ("600000", "浦发银行", date(2026, 9, 21), 0.10),
])
def test_st_limit_rate_is_trade_date_aware(code, name, when, rate):
    """主板 ST 涨跌幅必须按**交易日**取，不能写死 5%。"""
    assert limit_rate_of(code, name, when) == pytest.approx(rate)


def _q(code, name, pc):
    return Quote(code=code, name=name, price=pc, prev_close=pc,
                 board=board_of(code, name), open=pc, high=pc, low=pc,
                 volume_lots=10000.0, amount=pc * 1_000_000.0)


@pytest.mark.parametrize("code,name,pc,up,dn", [
    ("600000", "浦发银行", 10.00, 11.00, 9.00),
    ("600519", "贵州茅台", 1500.00, 1650.00, 1350.00),
    ("600000", "浦发银行", 10.05, 11.06, 9.05),     # 11.055 -> 四舍五入到分
    ("300750", "宁德时代", 100.00, 120.00, 80.00),
    ("688111", "金山办公", 100.00, 120.00, 80.00),
    ("830799", "艾融软件", 10.00, 13.00, 7.00),
    ("600001", "ST某", 10.00, 11.00, 9.00),        # 现行制度 10%
])
def test_limit_prices_rounded_to_cent(code, name, pc, up, dn):
    q = _q(code, name, pc)
    assert q.limit_up_price == pytest.approx(up, abs=0.005)
    assert q.limit_down_price == pytest.approx(dn, abs=0.005)


def test_st_quote_uses_its_own_trade_date_not_runtime_clock():
    """``Quote`` 的限价必须按**行情自己的交易日**算，而不是运行当天。

    这是本制度修复的核心判据：同一只 ST 股、同一价格，``ts`` 在 2026-07-03
    就要给 5% 的板，``ts`` 在 2026-09-21 就要给 10% 的板。
    若实现里偷偷用 ``date.today()``，两组会得到**同一个**结果，本测试即失败。
    """
    q_old = _q("600001", "ST某", 10.00)
    q_old.ts = datetime(2026, 7, 3, 10, 0)
    q_new = _q("600001", "ST某", 10.00)
    q_new.ts = datetime(2026, 9, 21, 10, 0)

    assert q_old.limit_up_price == pytest.approx(10.50, abs=0.005)
    assert q_old.limit_down_price == pytest.approx(9.50, abs=0.005)
    assert q_new.limit_up_price == pytest.approx(11.00, abs=0.005)
    assert q_new.limit_down_price == pytest.approx(9.00, abs=0.005)
    assert q_old.limit_up_price != q_new.limit_up_price, \
        "旧/新交易日给出了相同限价 —— 说明没有真的按交易日判定"


def test_suspended_minus_one_limit_price_is_recomputed():
    """停牌/PT 股常给 -1.0 的限价 -> 必须按板率自行推算，不能拿 -1 去比较。"""
    q = _q("600000", "浦发银行", 10.0).copy_with(limit_up=-1.0, limit_down=-1.0)
    assert q.limit_up_price == pytest.approx(11.0)
    assert q.limit_down_price == pytest.approx(9.0)


def test_limit_price_never_negative_or_zero():
    """任何情况下限价都必须是正数，否则规则里的比较会全部失真。"""
    for code, name in (("600000", "浦发银行"), ("300750", "宁德时代"),
                       ("688111", "金山办公"), ("830799", "艾融软件")):
        q = _q(code, name, 20.0)
        assert q.limit_up_price > 0
        assert q.limit_down_price > 0
        assert q.limit_down_price < q.price < q.limit_up_price


# ==========================================================================
# 审计修复回归（全部是执行确认过的真实缺陷）
# ==========================================================================
def test_limit_rate_accepts_board_enum():
    """``limit_rate_of`` 必须容忍误传 Board 枚举。

    回归：`replay.py` 曾写 `limit_rate_of(self.board, self.name)`，传的是枚举。
    `board_of` 收到非字符串会判成 `Board.OTHER` -> 10%，于是创业板/科创板剧本
    的涨停价全算错（300750 算成 55.00 而非 60.00），离线校验形同虚设。
    现在传枚举也能得到正确比例。
    """
    assert limit_rate_of(Board.GEM, "测试") == pytest.approx(0.20)
    assert limit_rate_of(Board.STAR, "测试") == pytest.approx(0.20)
    assert limit_rate_of(Board.BJ, "测试") == pytest.approx(0.30)
    assert limit_rate_of(Board.MAIN, "测试") == pytest.approx(0.10)
    # 与按代码算的结果一致
    for code, name in (("300750", "宁德时代"), ("688111", "金山办公"),
                       ("830799", "艾融软件"), ("600000", "浦发银行")):
        assert limit_rate_of(board_of(code, name), name) == \
            pytest.approx(limit_rate_of(code, name))


def test_board_of_tolerates_int_code():
    """回归：`board_of(600000)` 曾抛 AttributeError（int 没有 .strip）。"""
    assert board_of(600000) is Board.MAIN          # type: ignore[arg-type]
    assert board_of(300750) is Board.GEM           # type: ignore[arg-type]
    assert board_of(None) is Board.OTHER           # type: ignore[arg-type]
    assert board_of(1) is Board.OTHER              # type: ignore[arg-type]  八进制陷阱后的残值


def test_guess_prefix_handles_b_shares():
    """回归：B 股前缀曾判错（900901 沪B 被判 bj，200011 深B 被判 sh）。"""
    assert guess_prefix("900901") == "sh"          # 沪B
    assert guess_prefix("200011") == "sz"          # 深B
    # 常见 A 股不受影响
    assert guess_prefix("600000") == "sh"
    assert guess_prefix("000001") == "sz"
    assert guess_prefix("300750") == "sz"
    assert guess_prefix("688111") == "sh"
    assert guess_prefix("830799") == "bj"
    assert guess_prefix("920001") == "bj"


@pytest.mark.parametrize("code,name", [
    ("000001", ""),            # 平安银行，数据源偶尔不返名称
    ("000010", ""),
    ("000016", ""),
])
def test_missing_name_does_not_make_real_stock_an_index(code, name):
    """回归：名称缺失时 `000001` 曾被判为上证指数。

    那会让 `filters.exclude_boards=["index"]` 把平安银行（就在默认自选里）
    静默剔除——最难发现的一类问题：不报错，只是永远不告警。
    """
    assert board_of(code, name) is Board.MAIN
    # 明确像指数的名称仍然判 INDEX
    assert board_of("000001", "上证指数") is Board.INDEX
    # 399xxx 恒为指数
    assert board_of("399001", "") is Board.INDEX


def test_nonfinite_numbers_never_reach_json():
    """回归：NaN/Infinity 曾被 json.dumps 输出成裸 NaN -> 非法 JSON。

    浏览器端 `JSON.parse` 会直接抛错，整条 SSE 推送失效。
    真实数据源能产出：东财返回 f2="1e999" -> inf。
    """
    import json
    import math

    q = Quote(code="600000", name="测试", price=float("inf"), prev_close=10.0,
              board=Board.MAIN, open=10.0, high=10.0, low=10.0,
              volume_lots=1.0, amount=1.0)
    alert = Alert(key="k", kind=AlertKind.SURGE, code="600000", name="测试",
                  ts=datetime(2026, 9, 15, 10, 0, 0), price=float("inf"),
                  pct=float("nan"), title="t", detail="d", severity=2,
                  metrics={"a": float("nan"), "b": float("inf"),
                           "c": float("-inf"), "d": 1.5, "e": "s"})
    d = alert.to_dict()
    assert d["price"] is None
    assert d["pct"] is None
    assert d["metrics"]["a"] is None
    assert d["metrics"]["b"] is None
    assert d["metrics"]["c"] is None
    assert d["metrics"]["d"] == 1.5
    assert d["metrics"]["e"] == "s"                # 非数值原样透传

    s = json.dumps(d, ensure_ascii=False)
    assert "NaN" not in s and "Infinity" not in s, f"非法 JSON: {s}"
    json.loads(s)                                   # 必须能被严格解析
    # 顺带确认 Quote 自身的派生字段也不会炸
    assert isinstance(q.pct, float)
    assert math.isfinite(q.limit_up_price)
