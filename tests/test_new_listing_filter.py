"""新股过滤的第二层防线：名称前缀 ``N``/``C`` —— 回归测试。

真实缺陷（2026-09-21 走腾讯链路的实盘全市场 5564 只行情中发现）
----------------------------------------------------------------
``config/settings.yaml`` 里 ``filters.min_list_days = 11``，本意是过滤新股。
但这一层**静默失效**了：``list_dates`` 只有**东财**股票池提供，而东财在本机
被 IP 封禁（``RemoteDisconnected``），实际走的是腾讯链路 —— ``list_date`` 是
空串（``engine.py`` 已注明"该字段为空、新股过滤自动失效"），``_too_new``
据"数据缺失时放行"的约定直接返回 ``False``。

后果：上市第 2~5 日的 ``601091 C沈鼓``（当天 **+177.74%**）与
``688837 C信诺维``（+34.46%）畅通无阻地进入全部规则，产出

* 「打开跌停 +208.60%」（已由 ``models`` 层修复：新股无涨跌幅限制）
* 「巨震 326.5%」「低开高走 +285.1%」等 —— 新股上市前 5 日无涨跌幅限制，
  这类"巨震"是**制度性必然**，不是有用信号，只会淹没真正的异动。

修复：``_too_new`` 增加第二层判据 —— ``list_dates`` 无精确日期时退回
**名称前缀**（``N``=首日，``C``=第 2~5 日）。这是该链路上唯一可得的新股信号。
判据见 :func:`arad.models.is_new_listing`（"首字母 N/C 且第二字符为中文"），
实测全市场 5564 只里符合者恰好 2 只、无假阳性。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from arad.filters import Filters
from arad.models import board_of


def _q(code: str, name: str, price: float = 10.0, amount: float = 1e8):
    from arad.models import Quote
    return Quote(
        code=code, name=name, board=board_of(code, name),
        price=price, prev_close=price, open=price, high=price, low=price,
        volume_lots=10000.0, amount=amount,
    )


def _f(**kw) -> Filters:
    base = dict(min_price=1.5, max_price=2000.0, min_amount=8_000_000.0)
    base.update(kw)
    return Filters(**base)


# --------------------------------------------------------------------------
# 行为级：新股必须被拒绝（只用修复前已存在的 API）
# --------------------------------------------------------------------------
def test_c_new_listing_rejected_when_no_list_date():
    """核心场景：腾讯链路没有 list_date，C 股仍必须被过滤掉。

    修复前 ``accept`` 返回 True（新股直接进规则）。
    """
    f = _f(min_list_days=11)
    assert f.accept(_q("601091", "C沈鼓", price=57.77)) is False


def test_n_first_day_listing_rejected_when_no_list_date():
    f = _f(min_list_days=11)
    assert f.accept(_q("301999", "N测试", price=88.88)) is False


def test_star_c_listing_rejected():
    f = _f(min_list_days=11)
    assert f.accept(_q("688837", "C信诺维", price=55.60)) is False


def test_normal_stock_still_accepted():
    """正常股必须照常放行 —— 否则修复就是把整条链路的股票都滤掉了。"""
    f = _f(min_list_days=11)
    assert f.accept(_q("600000", "浦发银行", price=10.0)) is True
    assert f.accept(_q("300750", "宁德时代", price=200.0)) is True
    assert f.accept(_q("688111", "金山办公", price=300.0)) is True


def test_new_listing_filter_respects_min_list_days_zero():
    """``min_list_days <= 0`` = 显式关闭新股过滤，此时不得再按名称过滤。

    否则用户想关掉这个功能也关不掉 —— 那是把默认值偷偷变成强制行为。
    """
    f = _f(min_list_days=0)
    assert f.accept(_q("601091", "C沈鼓", price=57.77)) is True
    assert f.accept(_q("301999", "N测试", price=88.88)) is True


def test_precise_list_date_still_wins():
    """有精确上市日时按天数算，而不是一律按名称。

    这里给一个**名称普通**但 3 天前上市的股票：它必须被过滤，
    证明第一层判据没被第二层挤掉。
    """
    f = _f(min_list_days=11)
    today = date(2026, 9, 21)
    f.list_dates = {"600001": (today - timedelta(days=3)).strftime("%Y%m%d")}
    assert f.accept(_q("600001", "普通名称", price=10.0), today=today) is False


def test_precise_list_date_old_enough_passes():
    """上市已满 11 天且名称普通 -> 放行（第一层判据不能变成一律拒绝）。"""
    f = _f(min_list_days=11)
    today = date(2026, 9, 21)
    f.list_dates = {"600001": (today - timedelta(days=300)).strftime("%Y%m%d")}
    assert f.accept(_q("600001", "普通名称", price=10.0), today=today) is True


def test_explicit_list_date_overrides_name_prefix_for_old_stock():
    """名称像新股但精确上市日很老 —— 以精确日期为准，放行。

    反例保护：万一将来有正常股票名称恰好以 C+中文 开头，只要东财链路
    给了上市日，就不会被误杀。
    """
    f = _f(min_list_days=11)
    today = date(2026, 9, 21)
    f.list_dates = {"600001": (today - timedelta(days=500)).strftime("%Y%m%d")}
    assert f.accept(_q("600001", "C某正常股", price=10.0), today=today) is True


def test_malformed_list_date_falls_back_to_name_prefix():
    """上市日字段损坏（非 8 位数字）时，退回名称判据，不能直接放行。"""
    f = _f(min_list_days=11)
    f.list_dates = {"601091": "not-a-date"}
    assert f.accept(_q("601091", "C沈鼓", price=57.77)) is False


def test_impossible_list_date_falls_back_to_name_prefix():
    """日期非法（13 月）时同样退回名称判据。"""
    f = _f(min_list_days=11)
    f.list_dates = {"601091": "20261301"}
    assert f.accept(_q("601091", "C沈鼓", price=57.77)) is False


@pytest.mark.parametrize("name,should_filter", [
    ("C沈鼓", True),
    ("N沈鼓", True),
    ("TCL科技", False),      # 首字母非 N/C
    ("NVIDIA概念", False),   # N + ASCII
    ("CPU概念", False),      # C + ASCII
    ("浦发银行", False),
    ("ST沈鼓", False),
])
def test_name_prefix_boundary_in_filter(name, should_filter):
    """过滤器的名称判据边界（与 models 层保持同一口径）。"""
    f = _f(min_list_days=11)
    got = f.accept(_q("600000", name, price=10.0)) is False
    assert got is should_filter, f"{name!r} 过滤结果应为 {should_filter}"
