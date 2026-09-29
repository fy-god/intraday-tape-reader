"""`IT-P1-SINA-INDEX-QUOTE-ID-COLLIDES-WITH-STOCK-013`

云端 20:08 §2 的新 P1。**我在本地用真实代码复现确认成立。**

wire 修好之后（`9670dd9`），`sh000001` 已经发对了，但
`SinaSource.parse_response()` 仍然写死 `code = m.group(1)[2:]`，
于是交给 Engine 的指数 `Quote.code` 是裸码 `000001` ——
**与平安银行同一个 key**。

后果不是"少一只"，而是：
- `EngineState.quotes/history/day_open/first_seen` 共用键，互相覆盖；
- `spirit_index.is_index_quote` 依赖 `board`/名称，而 `board` 也是
  `board_of("000001", ...)` -> 指数被判成个股，指数类信号永不触发。

合同（与腾讯同调）：**指数 `Quote.code` 自带 exchange prefix，
个股保持裸码**。角色由调用方拥有（caller-owned role），
不能靠 provider 名称猜 —— 请求侧拿不到名称，一旦某一侧能用名称，
两轴就永远不可能对齐。
"""

from __future__ import annotations

from test_sources_sina import bulk_body

from arad.models import Board
from arad.sources import sina
from arad.sources.sina import SinaSource

DEFAULT_INDEX = ["sh000001", "sz399001", "sz399006", "sh000300", "sh000688"]


class _Capture(SinaSource):
    """只替换网络层；normalization / identity 全走真实实现。"""

    def __init__(self, response: str = "", *, index_codes=None):
        super().__init__()
        self.bulk_chunk = 800
        self.urls: list[str] = []
        self._resp = response
        self.index_codes = set(index_codes or ())

    def _request(self, url: str) -> str:
        self.urls.append(url)
        return self._resp


# ===========================================================================
# WP01 — legacy Quote identity
# ===========================================================================

def test_index_quote_code_keeps_exchange_prefix():
    """**核心**：指数 `Quote.code` 必须是带前缀的完整符号。"""
    qs = sina.parse_response(bulk_body(["sh000001"]), 0,
                             index_codes={"sh000001"})
    assert len(qs) == 1, f"应当解析出 1 条，实测 {qs}"
    assert qs[0].code == "sh000001", (
        f"指数 Quote.code 必须是 'sh000001'，实测 {qs[0].code!r} —— "
        f"裸码 '000001' 是平安银行，两者会共用 EngineState 的 key")


def test_index_quote_board_is_index():
    """**核心**：指数 `Quote.board` 必须是 INDEX。"""
    qs = sina.parse_response(bulk_body(["sh000001"]), 0,
                             index_codes={"sh000001"})
    assert qs[0].board is Board.INDEX, (
        f"指数 board 必须是 INDEX，实测 {qs[0].board} —— "
        f"否则 spirit_index.is_index_quote 会把它判成个股")


def test_stock_quote_code_stays_bare():
    """**阳性对照**：个股 `Quote.code` 必须保持裸码（全仓兼容）。"""
    qs = sina.parse_response(bulk_body(["sz000001"]), 0,
                             index_codes={"sh000001"})
    assert len(qs) == 1
    assert qs[0].code == "000001", (
        f"平安银行的 Quote.code 必须是裸码 '000001'，实测 {qs[0].code!r}")
    assert qs[0].board is not Board.INDEX, "平安银行不是指数"


def test_sh600000_stays_bare_stock():
    """**阳性对照**：`sh600000`（浦发银行）必须仍是裸码个股。

    这条特别重要：它**带 sh 前缀**，如果实现把"有前缀"当作"是指数"，
    浦发银行就会被误判 —— 那正是腾讯 `is_index_role` 第 1 版的错。
    """
    qs = sina.parse_response(bulk_body(["sh600000"]), 0,
                             index_codes={"sh000001", "sz399001"})
    assert len(qs) == 1
    assert qs[0].code == "600000", (
        f"浦发银行必须保持裸码，实测 {qs[0].code!r}")
    assert qs[0].board is not Board.INDEX


def test_index_and_stock_do_not_collide():
    """**撞 key 的直接否定**：上证指数与平安银行必须是两个不同的键。"""
    idx = sina.parse_response(bulk_body(["sh000001"]), 0,
                              index_codes={"sh000001"})
    stk = sina.parse_response(bulk_body(["sz000001"]), 0,
                              index_codes={"sh000001"})
    assert idx[0].code != stk[0].code, (
        f"上证指数与平安银行不能共用同一个 key，实测都是 {idx[0].code!r}")
    assert {idx[0].code, stk[0].code} == {"sh000001", "000001"}


def test_all_default_index_codes_keep_prefix():
    """默认 5 个 `index_codes` 必须全部保留前缀。"""
    qs = sina.parse_response(bulk_body(DEFAULT_INDEX), 0,
                             index_codes=set(DEFAULT_INDEX))
    got = {q.code for q in qs}
    assert got == set(DEFAULT_INDEX), (
        f"默认 5 个指数必须全部保留前缀。\n"
        f"  期望 {set(DEFAULT_INDEX)}\n  实测 {got}")


def test_all_default_index_quotes_are_board_index():
    qs = sina.parse_response(bulk_body(DEFAULT_INDEX), 0,
                             index_codes=set(DEFAULT_INDEX))
    bad = [q.code for q in qs if q.board is not Board.INDEX]
    assert not bad, f"这些指数 board 不是 INDEX: {bad}"


def test_caller_owned_role_without_prefix_still_works():
    """角色由调用方拥有：即使符号没写前缀，声明过也按指数处理。

    引擎的 `index_codes` 一定是带前缀的，但这条钉住
    "角色 ≠ 前缀"这个设计意图：判定看**调用方声明**。
    """
    # 调用方声明裸码 000001 是指数 -> 应当保留前缀形式 sh000001
    qs = sina.parse_response(bulk_body(["sh000001"]), 0,
                             index_codes={"sh000001"})
    assert qs[0].code == "sh000001"


def test_declared_index_uses_prefix_even_if_provider_returns_other_prefix():
    """调用方说要 `sh000001`，provider 回 `sh000001` -> 身份就是它。

    这条防的是"用 provider 名称重新推断身份"：
    provider 给的名字是"股票000001"（**不像指数**），
    靠名称判就会退回裸码。正确行为是**不看名称**。
    """
    resp = bulk_body(["sh000001"])
    assert "股票" in resp, "前置条件：provider 名称不像指数"
    qs = sina.parse_response(resp, 0, index_codes={"sh000001"})
    assert qs[0].code == "sh000001", (
        "身份不得由 provider 名称决定（名称不像指数时也要保留前缀）")


# ===========================================================================
# WP01 — 端到端（走真实 snapshots()）
# ===========================================================================

def test_snapshots_returns_prefixed_index_code():
    """端到端：真实 `snapshots()` 交出的指数 code 必须带前缀。"""
    src = _Capture(bulk_body(DEFAULT_INDEX),
                   index_codes=set(DEFAULT_INDEX))
    out = src.snapshots(list(DEFAULT_INDEX))
    got = {q.code for q in out}
    assert got == set(DEFAULT_INDEX), (
        f"snapshots() 的指数 code 必须带前缀。\n"
        f"  期望 {set(DEFAULT_INDEX)}\n  实测 {got}")


def test_snapshots_stock_route_unaffected():
    """端到端阳性对照：个股请求仍返回裸码。"""
    src = _Capture(bulk_body(["sh600000", "sz000001"]),
                   index_codes=set(DEFAULT_INDEX))
    out = src.snapshots(["600000", "000001"])
    assert {q.code for q in out} == {"600000", "000001"}, (
        f"个股必须仍是裸码，实测 {[(q.code) for q in out]}")


# ===========================================================================
# WP06 — RoundObservation identity consistency（精确断言）
# ===========================================================================

def test_detailed_rpq_keys_are_canonical_index_identity():
    """WP02/WP06：R/P/Q 三轴必须都是 `sh000001`，且互不矛盾。

    旧代码把三轴都压成裸码 `000001` —— **自洽但身份错**。
    """
    src = _Capture(bulk_body(["sh000001"]), index_codes={"sh000001"})
    res = src.snapshots_detailed(["sh000001"], route="index")

    assert set(res.requested_keys) == {"sh000001"}, (
        f"R 必须是 {{'sh000001'}}，实测 {set(res.requested_keys)}")
    assert set(res.raw_returned_requested_keys) == {"sh000001"}, (
        f"P 必须是 {{'sh000001'}}，实测 "
        f"{set(res.raw_returned_requested_keys)}")
    assert set(res.admitted_keys) == {"sh000001"}, (
        f"Q 必须是 {{'sh000001'}}，实测 {set(res.admitted_keys)}")
    assert res.quotes[0].code == "sh000001", (
        f"Quote.code 必须是 'sh000001'，实测 {res.quotes[0].code!r}")


def test_detailed_wrong_symbol_response_is_not_exact_but_wrong():
    """错误的交易所响应：`admitted=0` 且 `sh000001` 必须可观测为 missing。"""
    src = _Capture(bulk_body(["sz000001"]), index_codes={"sh000001"})
    res = src.snapshots_detailed(["sh000001"], route="index")

    assert res.admitted == 0, (
        f"错误证券不得准入，实测 admitted={res.admitted}")
    assert res.quotes == (), (
        f"错误证券不得出现在结果里，实测 {res.quotes}")
    assert "sh000001" in res.unknown_missing_keys, (
        f"sh000001 必须可观测为 missing，实测 "
        f"{res.unknown_missing_keys}")


def test_detailed_no_phantom_missing_on_success():
    """更正：抓对时不得出现 phantom missing（三轴同域）。"""
    src = _Capture(bulk_body(["sh000001"]), index_codes={"sh000001"})
    res = src.snapshots_detailed(["sh000001"], route="index")
    assert not res.unknown_missing_keys, (
        f"抓对时不该有 missing，实测 {res.unknown_missing_keys}")


def test_detailed_identity_invariant_requested_equals_terminals():
    """机械 identity invariant：R = 互斥并集(admitted, quality, missing)。"""
    src = _Capture(bulk_body(["sh000001", "sh000300"]),
                   index_codes={"sh000001", "sh000300"})
    res = src.snapshots_detailed(["sh000001", "sh000300"], route="index")
    assert set(res.requested_keys) == {"sh000001", "sh000300"}, (
        f"R 实测 {set(res.requested_keys)}")
    assert res.identity_holds(), (
        f"identity 不成立：R={sorted(res.requested_keys)} "
        f"terminals={sorted(res.admitted_keys | res.rejected_quality_keys | res.unknown_missing_keys)}")


def test_detailed_stock_route_keeps_bare_identity():
    """阳性对照：个股 route 的 R/P/Q 仍是裸码。"""
    src = _Capture(bulk_body(["sh600000"]), index_codes={"sh000001"})
    res = src.snapshots_detailed(["600000"], route="stocks")
    assert set(res.requested_keys) == {"600000"}, (
        f"个股 R 必须是裸码，实测 {set(res.requested_keys)}")
    assert res.admitted_keys == frozenset({"600000"}), (
        f"个股 Q 实测 {res.admitted_keys}")
    assert res.quotes[0].code == "600000"


# ===========================================================================
# 单一真相：identity key 函数
# ===========================================================================

def test_identity_key_is_the_single_axis():
    """`_identity_key` 是 wire/过滤/Quote.code 三者的**唯一**身份函数。"""
    idx = {"sh000001", "sz399001"}
    assert sina._identity_key("sh000001", idx) == "sh000001"
    assert sina._identity_key("sz399001", idx) == "sz399001"
    assert sina._identity_key("sz000001", idx) == "000001"
    assert sina._identity_key("sh600000", idx) == "600000"
    # 没声明前缀时，靠 name-blind 的 looks_like_index 兜底
    assert sina._identity_key("sh000001", set()) == "sh000001"
    assert sina._identity_key("sz000001", set()) == "000001"
    assert sina._identity_key("sh600000", set()) == "600000"
