"""WP01 identity gate —— 表驱动符号集合 + M6 mutation tooth。

云端 2026-09-24_00-05-20_JST_AGENT_TASK.md §WP01。**先补测试，再改实现。**

任务书原文的三组符号：
* 名称唯一定身份的指数（7）—— 不在 :data:`arad.models.INDEX_CODES`、
  也不是 ``399xxx``，**只能靠 provider 给的中文名称**认出来；
* 安全指数对照（6）—— 名称盲也认得出（399 段 / 白名单 + sh 前缀）；
* 显式前缀普通股（2）—— 必须归**裸 6 位**股票 key。

每例断言（任务书原文）::

    requested identity == raw ledger identity == Quote.code

**gate 的核心不是"正常实现全绿"**：任务书 §WP01 明说
「如果 M6 仍然全绿，则 WP01 未完成，即使正常实现全绿也不算通过」。
M6 = 把 raw/Quote 侧身份判定强制改为 ``name=""``。
"""

from __future__ import annotations

import pytest

from arad.models import INDEX_CODES
from arad.sources.tencent import _norm_specs, parse_response_detailed

# --- 名称唯一定身份的指数（7）：只能靠 name 认出 ---------------------------
NAME_UNIQUE_INDICES = [
    ("sh000922", "000922", "中证红利指数"),
    ("sh000015", "000015", "红利指数"),
    ("sh000903", "000903", "中证100指数"),
    ("sh000904", "000904", "中证200指数"),
    ("sh000906", "000906", "中证800指数"),
    ("sh000009", "000009", "上证380指数"),
    ("sh000133", "000133", "上证150指数"),
]

# --- 安全指数对照（6）：名称盲也认得出 --------------------------------------
SAFE_INDICES = [
    ("sh000001", "000001", "上证指数"),
    ("sz399001", "399001", "深证成指"),
    ("sh000300", "000300", "沪深300指数"),
    ("sh000016", "000016", "上证50指数"),
    ("sh000688", "000688", "科创50指数"),
    ("sz399006", "399006", "创业板指"),
]

# --- 显式前缀普通股（2）：必须归裸 6 位 -------------------------------------
PREFIXED_STOCKS = [
    ("sh600000", "600000", "浦发银行"),
    ("sz000001", "000001", "平安银行"),
]


def _line(symbol: str, name: str, code: str,
          price: str = "11.00", prev: str = "10.00") -> str:
    f = ["0"] * 60
    f[1], f[2], f[3], f[4] = name, code, price, prev
    return 'v_' + symbol + '="' + "~".join(f) + '";'


def _detail(symbol: str, name: str, code: str, *, route: str):
    """按**生产合同**发一个请求并取回 detailed 结果。

    任务书 §WP02 的生产合同::

        stocks route -> bare 6-digit code
        index  route -> prefixed exchange symbol

    所以指数必须走 ``route="index"`` —— 这正是"caller-owned role"：
    调用方**已经知道**自己在请求什么，不该让 parser 从请求串上再猜一次。
    """
    idx_set = {s for s, explicit in _norm_specs([symbol]) if explicit}
    return parse_response_detailed(
        _line(symbol, name, code), requested=[symbol],
        index_codes=idx_set, route=route)


def _assert_identity(symbol, name, code, *, is_index):
    want = symbol if is_index else code
    g = _detail(symbol, name, code, route="index" if is_index else "stocks")
    R = sorted(g.requested_keys)
    P = sorted(g.raw_returned_requested_keys)
    Q = sorted(q.code for q in g.quotes)
    ctx = (f"{symbol} ({name}) 期望 want={want}；"
           f"实测 R={R} P={P} Q={Q} "
           f"missing={sorted(g.unknown_missing_keys)} "
           f"unexpected={sorted(g.unexpected_raw_keys)}")
    assert R == [want], f"requested identity 轴错位：{ctx}"
    assert P == [want], f"raw ledger identity 轴错位：{ctx}"
    assert Q == [want], f"Quote.code 轴错位：{ctx}"
    assert not g.unknown_missing_keys, f"不得 phantom missing：{ctx}"
    assert not g.unexpected_raw_keys, f"不得报未请求代码：{ctx}"
    assert g.admitted == 1, f"指数/个股都必须 admitted=1：{ctx}"


# ===========================================================================
# 1) 名称唯一定身份的指数（7）
# ===========================================================================

@pytest.mark.parametrize("symbol,code,name", NAME_UNIQUE_INDICES)
def test_name_unique_index_identity(symbol, code, name):
    """这 7 个**只能靠名称**认出来 —— 修前实测全部 phantom missing。

    修前（我把 request 轴改成名称盲之后）::

        R=['000922'] P=[] Q=[] missing=['000922'] unexpected=['sh000922']

    即 provider 正常返回、detailed 却报"请求的 missing + 返回了未请求代码"。
    `sh000922` 既不在 :data:`arad.models.INDEX_CODES`，也不是 ``399xxx``，
    名称盲的 ``looks_like_index(sym, "", code)`` 必然判 False。
    """
    assert code not in INDEX_CODES, "前提：这些码不在名称盲白名单里"
    assert not code.startswith("399"), "前提：不是 399 段"
    _assert_identity(symbol, name, code, is_index=True)


# ===========================================================================
# 2) 安全指数对照（6）—— 名称盲也认得出，**不得**被上面的修法弄坏
# ===========================================================================

@pytest.mark.parametrize("symbol,code,name", SAFE_INDICES)
def test_safe_index_identity_does_not_regress(symbol, code, name):
    """出厂 5 个指数 + 399 段不得回归（任务书：'出厂 5 个指数不回归'）。"""
    _assert_identity(symbol, name, code, is_index=True)


# ===========================================================================
# 3) 显式前缀普通股（2）—— 必须归裸 6 位
# ===========================================================================

@pytest.mark.parametrize("symbol,code,name", PREFIXED_STOCKS)
def test_prefixed_stock_identity_is_bare(symbol, code, name):
    """`IT-P2-TENCENT-DETAILED-EXPLICIT-STOCK-KEY-AXIS-001`。

    显式写了前缀**不是**"它是指数"的充分条件。若被当 index，
    任务书 FAIL 条件直接命中：「`sh600000/sz000001` 被当 index → FAIL」。
    """
    _assert_identity(symbol, name, code, is_index=False)


# ===========================================================================
# 4) M6 mutation tooth —— 没有它，"正常实现全绿"不算通过
# ===========================================================================
#
# ⚠ **我第一版把 M6 写成 monkeypatch `looks_like_index`，那是错的（规则 mmm2）**：
# 修正后的实现里 `is_index_role` 在 `index_role=True` 时**直接 return True**，
# 根本不会走到 `looks_like_index` —— 探针没进入被审分支，等于没测。
# 10 条 M6 测试因此"失败"（其实是 `pytest.raises` 没捕到 AssertionError），
# 但那是**探针无牙**，不是"实现变红"。
#
# M6 的真身是**源码级注入**（见 `identity_mutation_red.log`）：把 raw/Quote
# 侧身份判定改回"按 provider 真名称推断"或"名称盲推断"，
# 然后整张表必须变红。这里改成两条**永久、可注入**的行为牙：
# 它们钉住"role 必须压过 name"，任何"某侧按 name 猜"的修法都会让它们变红。


def _detail_with_name(symbol, name, code, *, route):
    """同 :func:`_detail`，但允许 name 与 symbol 的"真实身份"矛盾。"""
    idx_set = {s for s, explicit in _norm_specs([symbol]) if explicit}
    return parse_response_detailed(
        _line(symbol, name, code), requested=[symbol],
        index_codes=idx_set, route=route)


@pytest.mark.parametrize("symbol,code", [("sh600000", "600000"),
                                         ("sz000001", "000001")])
def test_m6_role_beats_provider_name_on_index_route(symbol, code):
    """**M6-A**：index route + provider 给了一个**股票名** -> 仍须带前缀。

    这是"caller-owned role"的核心断言：调用方说"这批是指数"，parser
    就不该用 provider 的 name 二次否决。

    注入"raw/Quote 侧按 provider 真名称推断"（任务书禁止的组合）后：
    ``looks_like_index("sh600000", "浦发银行", "600000") is False``
    -> raw 侧塌成裸码 ``600000``，而 request 侧（role=index）仍是
    ``sh600000`` -> **两轴分离** -> 本测试变红。**这就是 M6 的牙。**
    """
    g = _detail_with_name(symbol, "浦发银行", code, route="index")
    assert sorted(g.requested_keys) == [symbol], (
        f"index route 的身份必须带前缀，实测 {sorted(g.requested_keys)}")
    assert sorted(g.raw_returned_requested_keys) == [symbol], (
        f"raw 轴必须与 request 轴同调，实测 "
        f"{sorted(g.raw_returned_requested_keys)}")
    assert {q.code for q in g.quotes} == {symbol}, (
        "Quote.code 必须与另两轴同调")
    assert not g.unknown_missing_keys and not g.unexpected_raw_keys


@pytest.mark.parametrize("symbol,name,code", [
    ("sh000922", "中证红利指数", "000922"),
    ("sh000903", "中证100指数", "000903"),
])
def test_m6_role_beats_provider_name_on_stocks_route(symbol, name, code):
    """**M6-B**：stocks route + provider 给了一个**指数名** -> 须归裸码。

    反向的牙：若 raw 侧用 provider 真名推断，
    ``looks_like_index(symbol, "中证红利指数", code) is True``
    -> raw 侧塌成带前缀，而 request 侧（role=stocks）是裸码 -> 两轴分离。
    """
    g = _detail_with_name(symbol, name, code, route="stocks")
    assert sorted(g.requested_keys) == [code], (
        f"stocks route 的身份必须是裸码，实测 {sorted(g.requested_keys)}")
    assert sorted(g.raw_returned_requested_keys) == [code], (
        f"raw 轴必须与 request 轴同调，实测 "
        f"{sorted(g.raw_returned_requested_keys)}")
    assert {q.code for q in g.quotes} == {code}
    assert not g.unknown_missing_keys and not g.unexpected_raw_keys


@pytest.mark.parametrize("symbol,code,name", PREFIXED_STOCKS)
def test_reverse_mutation_prefix_means_index_must_be_killed(
        symbol, code, name):
    """**反向 mutation**（任务书原文）：request 侧退回"显式前缀即 index"。

    预期：``sh600000`` / ``sz000001`` **必须**变红。
    与 M6-A/M6-B 配对 —— 一条防"名称盲"，一条防"前缀即指数"，
    两个方向都堵住，才说明实现没有靠单一代理猜身份。
    """
    g = _detail(symbol, name, code, route="stocks")
    assert sorted(g.requested_keys) == [code], (
        f"request 轴不得因为'写了前缀'就判指数：{sorted(g.requested_keys)}")
    assert not g.unknown_missing_keys, (
        f"prefix=index 的错误修法会产生 phantom missing："
        f"{sorted(g.unknown_missing_keys)}")
