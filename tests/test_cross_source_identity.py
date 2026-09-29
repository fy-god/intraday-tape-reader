"""WP03：跨源 canonical identity。

同一指数在 **Tencent** 与 **Sina** 上必须产生**逐元素相同**的：
``Quote.code`` / ``requested key(R)`` / ``raw returned key(P)`` /
``admitted key(Q)``。

为什么必须钉：账本是研究/模型质量标签的来源。若一次 failover
（腾讯 -> 新浪，新浪是默认兜底源）改变了同一证券的身份键，
那么"同一 raw fact"在两条链路上会落进**不同的账本桶** ——
样本标签会随"当时谁在供数"漂移，而这不可观测。
"""

from __future__ import annotations

import pytest
from test_sources_sina import bulk_body
from test_sources_tencent import make_line

from arad.sources import sina, tencent

DEFAULT_INDEX = ["sh000001", "sz399001", "sz399006", "sh000300", "sh000688"]


class _SinaCap(sina.SinaSource):
    def __init__(self, body: str, index_codes=frozenset(DEFAULT_INDEX)):
        super().__init__()
        self._body = body
        self.bulk_chunk = 800
        self.index_codes = set(index_codes)

    def _request(self, url: str) -> str:
        return self._body


class _TencCap(tencent.TencentSource):
    def __init__(self, body: str):
        super().__init__()
        self._body = body
        self.chunk = 800

    def _request(self, url: str) -> str:
        return self._body


def _tencent_body(symbols=None) -> str:
    syms = symbols or DEFAULT_INDEX
    return "\n".join(
        make_line(code=s[2:], prefix=s[:2], n=54,
                  fields={3: "3900.00", 4: "3890.00"})
        for s in syms)


# ===========================================================================
# 跨源一致性
# ===========================================================================

def test_quote_code_identical_across_sources():
    """`Quote.code` 逐元素一致（默认 5 个 index_codes）。"""
    t = tencent.parse_response(_tencent_body(), 0,
                               index_codes=set(DEFAULT_INDEX), route="index")
    s = sina.parse_response(bulk_body(DEFAULT_INDEX), 0,
                            index_codes=set(DEFAULT_INDEX))
    assert sorted(q.code for q in t) == sorted(q.code for q in s), (
        f"两源的 Quote.code 必须一致。\n"
        f"  tencent {sorted(q.code for q in t)}\n"
        f"  sina    {sorted(q.code for q in s)}")
    assert sorted(q.code for q in s) == sorted(DEFAULT_INDEX), (
        "且必须全部带交易所前缀")


def test_board_identical_across_sources():
    """`Quote.board` 对 5 个指数必须都是 INDEX（两源一致）。"""
    t = tencent.parse_response(_tencent_body(), 0,
                               index_codes=set(DEFAULT_INDEX), route="index")
    s = sina.parse_response(bulk_body(DEFAULT_INDEX), 0,
                            index_codes=set(DEFAULT_INDEX))
    from arad.models import Board
    assert all(q.board is Board.INDEX for q in t), "tencent 侧"
    assert all(q.board is Board.INDEX for q in s), "sina 侧"


def test_rpq_identical_across_sources():
    """R / P / Q 三轴逐元素一致。"""
    tc, sc = _TencCap(_tencent_body()), _SinaCap(bulk_body(DEFAULT_INDEX))
    t = tc.snapshots_detailed(list(DEFAULT_INDEX), route="index")
    s = sc.snapshots_detailed(list(DEFAULT_INDEX), route="index")

    for label, a, b in (
        ("R", sorted(t.requested_keys), sorted(s.requested_keys)),
        ("P", sorted(t.raw_returned_requested_keys),
         sorted(s.raw_returned_requested_keys)),
        ("Q", sorted(t.admitted_keys), sorted(s.admitted_keys)),
    ):
        assert a == b, (
            f"{label} 轴两源必须一致。\n  tencent {a}\n  sina    {b}")
        assert a == sorted(DEFAULT_INDEX), (
            f"{label} 轴必须全部是带前缀的 canonical identity，实测 {a}")


def test_failover_does_not_change_identity():
    """**核心**：failover（腾讯失败 -> 新浪兜底）不得改变身份键。

    这是本条缺陷的真实业务后果：新浪是默认兜底源，如果它在兜底时
    把指数压成裸码，那么"腾讯挂了"这一个事实，会让指数的账本键
    从 `sh000001` 变成 `000001` —— 与平安银行撞车。

    ⚠ 比对必须**按请求符号**而不是按名称：两个 fixture 的名称
    都是占位串（"测试股"/"股票000001"），按名称聚合会把 5 条
    塌成 1 条 —— 我第一版正是踩了这个，报出
    `tencent=None sina='sh000001'` 的假失败。
    """
    t = tencent.parse_response(_tencent_body(), 0,
                               index_codes=set(DEFAULT_INDEX), route="index")
    s = sina.parse_response(bulk_body(DEFAULT_INDEX), 0,
                            index_codes=set(DEFAULT_INDEX))
    # 按"裸码 -> 身份键"聚合；若两源对同一裸码给出不同键就是缺陷。
    tmap: dict[str, set[str]] = {}
    for q in t:
        tmap.setdefault(q.code[-6:], set()).add(q.code)
    smap: dict[str, set[str]] = {}
    for q in s:
        smap.setdefault(q.code[-6:], set()).add(q.code)

    for sym in DEFAULT_INDEX:
        bare = sym[2:]
        assert tmap.get(bare) == smap.get(bare), (
            f"{sym}: tencent={sorted(tmap.get(bare) or ())} "
            f"sina={sorted(smap.get(bare) or ())} —— failover 改变了身份键")
        assert tmap.get(bare) == {sym}, (
            f"{sym} 的身份键必须恰好是 {sym}，实测 "
            f"{sorted(tmap.get(bare) or ())}")


def test_identity_holds_on_both_sources():
    """两源的 mechanical identity invariant 都要成立。"""
    tc, sc = _TencCap(_tencent_body()), _SinaCap(bulk_body(DEFAULT_INDEX))
    for name, res in (("tencent", tc.snapshots_detailed(
                          list(DEFAULT_INDEX), route="index")),
                      ("sina", sc.snapshots_detailed(
                          list(DEFAULT_INDEX), route="index"))):
        assert res.identity_holds(), (
            f"{name} 的 R = 互斥并集(terminals) 不成立："
            f"R={sorted(res.requested_keys)} "
            f"Q={sorted(res.admitted_keys)} "
            f"quality={sorted(res.rejected_quality_keys)} "
            f"missing={sorted(res.unknown_missing_keys)}")
        assert not res.unknown_missing_keys, (
            f"{name} 抓全了不该有 missing，实测 {res.unknown_missing_keys}")


# ===========================================================================
# 个股跨源一致（不得为了修指数而破坏个股）
# ===========================================================================

def test_stock_identity_identical_across_sources():
    """阳性对照：同一只个股在两源上都是裸码。"""
    stocks = ["600000", "000001", "300750"]
    tsyms = [f"{'sh' if s[0] == '6' else 'sz'}{s}" for s in stocks]
    t = tencent.parse_response(_tencent_body(tsyms), 0,
                               index_codes=set(DEFAULT_INDEX), route="stocks")
    s = sina.parse_response(
        bulk_body(tsyms), 0, index_codes=set(DEFAULT_INDEX))
    assert sorted(q.code for q in t) == sorted(stocks), (
        f"tencent 个股 code 实测 {sorted(q.code for q in t)}")
    assert sorted(q.code for q in s) == sorted(stocks), (
        f"sina 个股 code 实测 {sorted(q.code for q in s)}")


def test_stock_and_index_mixed_request_no_collision():
    """混合请求：`sz000001`（股票）与 `sh000001`（指数）不得撞 key。"""
    mixed = ["sh000001", "sz000001"]
    s = sina.parse_response(bulk_body(mixed), 0, index_codes={"sh000001"})
    got = sorted(q.code for q in s)
    assert got == ["000001", "sh000001"], (
        f"混合请求必须产出两个不同的键，实测 {got}")
