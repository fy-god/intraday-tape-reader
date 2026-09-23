"""Snapshot Outcome Contract v4 —— 跨源同义性 + 集合代数（WP01）。

审计 FAIL 条件（`2026-09-23_12-37-02_JST_AGENT_TASK.md` §267-274）：:

    - Sina raw-present suspended row 仍进 unknown_missing -> FAIL
    - source failover 会改变同一 raw fact 的 missing/quality 语义 -> FAIL
    - returned 使用 raw row count、可大于 requested -> FAIL
    - unexpected raw code 能掩盖 requested missing -> FAIL
    - legacy quote projection 被标成 exact raw presence -> FAIL
    - rollback 后 RED 不重新红 -> 测试无探测力

本文件的核心判据是**同一个 raw 事实在三源上得到同一个桶**。
修前：腾讯保留 ``price<=0`` 行 -> 引擎记 ``rejected_quality``；
新浪/东财 parser 直接 ``continue``/``return None`` -> 引擎只能记
``unknown_missing``。**默认链路是腾讯主 + 新浪备**，所以一次 failover
就能改变同一事实的账本语义。
"""

from __future__ import annotations

import pytest

from arad.sources.eastmoney import parse_ulist, parse_ulist_detailed
from arad.sources.outcome import (
    PROVENANCE_EXACT,
    PROVENANCE_LEGACY,
    build_outcome,
)
from arad.sources.sina import parse_response as sina_old
from arad.sources.sina import parse_response_detailed as sina_detailed
from arad.sources.tencent import parse_response_detailed as tx_detailed

#: 三源共同的受影响代码（raw 在、数值不可用）与正常代码。
BAD_CODE = "600001"
OK_CODE = "600002"
ABSENT_CODE = "600003"
REQ = [BAD_CODE, OK_CODE, ABSENT_CODE]


# ---------------------------------------------------------------------------
# fixtures：真实 provider 文本形态（**不联网**，纯字符串）
# ---------------------------------------------------------------------------

def _tx_fields(n: int, **over: str) -> str:
    f = ["0"] * n
    for k, v in over.items():
        f[int(k[1:])] = v
    return "~".join(f)


def _tx_line(symbol: str, code: str, price: str, prev: str = "10.00") -> str:
    body = _tx_fields(60, i1="测试", i2=code, i3=price, i4=prev)
    return 'v_' + symbol + '="' + body + '";'


TENCENT_TEXT = "\n".join([
    # 现价空串 -> parse_response 在 :321 丢弃该行（但 raw 行确实在）
    _tx_line("sh600001", BAD_CODE, ""),
    _tx_line("sh600002", OK_CODE, "11.00"),
])


def _sina_line(code: str, price: str, prev: str = "10.00") -> str:
    f = ["0"] * 33
    f[0], f[1], f[2], f[3] = "测试", "10.00", prev, price
    f[4], f[5], f[6], f[7], f[8], f[9] = "11", "9", "10", "10", "1000", "10000"
    return 'var hq_str_sh' + code + '="' + ",".join(f) + '";'


SINA_TEXT = "\n".join([
    # 现价 0.00（停牌）-> parse_response 在 :197 整行 continue
    _sina_line(BAD_CODE, "0.00"),
    _sina_line(OK_CODE, "11.00"),
])


def _em_row(code: str, *, valid: bool, name: str = "测试") -> dict:
    if not valid:
        # f2/f18 是 "-" 占位 -> _quote_of 在 :199 return None
        return {"f12": code, "f14": name, "f2": "-", "f18": "-"}
    return {"f12": code, "f14": name, "f2": 11.0, "f18": 10.0, "f17": 10.5,
            "f15": 11.2, "f16": 10.4, "f5": 100, "f6": 1100, "f8": 1.0,
            "f10": 1.0, "f20": 1e9, "f21": 1e9, "f124": 1700000000,
            "f26": "20100101"}


EASTMONEY_JSON = {"data": {"total": 3, "diff": [
    _em_row(BAD_CODE, valid=False, name="占位股"),
    _em_row(OK_CODE, valid=True, name="正常股"),
]}}


def _all_sources():
    """三源的 detailed 结果 —— **同一个 raw 事实**。

    三源必须让**同一个代码**不可用；否则测的是三个不同场景，
    "一致性"就成了假命题。
    """
    return {
        "tencent": tx_detailed(TENCENT_TEXT, requested=REQ),
        "sina": sina_detailed(SINA_TEXT, requested=REQ),
        "eastmoney": parse_ulist_detailed(EASTMONEY_JSON, requested=REQ),
    }


# ---------------------------------------------------------------------------
# RED A：跨源同义性
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source", ["tencent", "sina", "eastmoney"])
def test_raw_present_unusable_lands_in_rejected_quality(source):
    """**核心 RED（RED A）**：raw 在、数值不可用 -> ``rejected_quality``。

    `IT-P1-SNAPSHOT-OUTCOME-SOURCE-SEMANTICS-DRIFT-001`（12:37 §2）：

    修前新浪（默认兜底源）在 ``sina.py:197`` 把停牌行 ``continue`` 掉，
    东财在 ``eastmoney.py:199`` ``return None``，而腾讯**保留**它。
    于是**同一个 raw 事实**在腾讯落 ``rejected_quality``、
    在新浪/东财落 ``unknown_missing`` —— **一次 failover 改变账本语义**。
    """
    got = _all_sources()[source]
    assert BAD_CODE in got.raw_returned_requested_keys, (
        f"{source}: provider 明确返回了该行，raw present 必须为真")
    assert BAD_CODE in got.rejected_quality_keys, (
        f"{source}: raw 在但数值不可用 -> 必须落 rejected_quality")
    assert BAD_CODE not in got.unknown_missing_keys, (
        f"{source}: **不得**落进 unknown_missing（那是'provider 没返回'的意思）")
    assert got.identity_holds(), f"{source}: 机械恒等必须成立"


def test_cross_source_buckets_agree_exactly():
    """三源对同一 raw 事实必须给出**逐元素相同**的三分。

    这是整个 WP01 的存在理由：账本是研究/模型质量标签的来源，
    **不能让服务源的身份渗进标签**。
    """
    res = _all_sources()
    buckets = {
        name: (frozenset(g.raw_returned_requested_keys),
               frozenset(g.rejected_quality_keys),
               frozenset(g.unknown_missing_keys),
               frozenset(x.code for x in g.quotes))
        for name, g in res.items()
    }
    ref = buckets["tencent"]
    for name, got in buckets.items():
        assert got == ref, (
            f"{name} 的账本与 tencent 不一致 —— "
            f"failover 会改变同一 raw 事实的语义\n"
            f"  tencent={ref}\n  {name}={got}")


def test_cross_source_counts_agree():
    """三源的 requested / returned / admitted 也必须一致。"""
    res = _all_sources()
    for name, g in res.items():
        assert g.requested == 3, name
        assert g.returned == 2, f"{name}: |P| 必须是 2"
        assert g.admitted == 1, f"{name}: |Q| 必须是 1"
        assert g.returned <= g.requested, (
            f"{name}: returned 不得超过 requested（错误 A）")


# ---------------------------------------------------------------------------
# RED B：真 raw absent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source", ["tencent", "sina", "eastmoney"])
def test_genuinely_absent_code_is_unknown_missing(source):
    """**RED B**：raw **真的**没有这个代码 -> ``unknown_missing``。

    与 RED A 配对才有意义：只测 RED A 的话，一个"把什么都塞进
    rejected_quality"的实现也能通过。
    """
    got = _all_sources()[source]
    assert ABSENT_CODE not in got.raw_returned_requested_keys
    assert ABSENT_CODE in got.unknown_missing_keys, (
        f"{source}: 真缺席必须落 unknown_missing")
    assert ABSENT_CODE not in got.rejected_quality_keys, (
        f"{source}: 真缺席**不得**落 quality")


# ---------------------------------------------------------------------------
# RED C：集合代数
# ---------------------------------------------------------------------------

def _algebra_fixture():
    """审计 §6 §412-427 的精确 fixture。

    ::

        requested:   600001, 600002, 600003
        raw rows:    600001 invalid
                     600001 duplicate invalid
                     600002 valid
                     600999 unexpected valid
    """
    return {"data": {"total": 4, "diff": [
        _em_row(BAD_CODE, valid=False, name="无效"),
        _em_row(BAD_CODE, valid=False, name="无效重复"),
        _em_row(OK_CODE, valid=True, name="正常"),
        _em_row("600999", valid=True, name="未请求"),
    ]}}


def test_set_algebra_matches_audit_fixture_exactly():
    """**RED C**：``R/P/Q`` 与四类诊断**逐个**对上审计给定的期望值。"""
    g = parse_ulist_detailed(_algebra_fixture(), requested=REQ)
    assert g.requested == 3
    assert g.returned == 2, "returned = |P| = 2（**不是** raw row count 4）"
    assert g.admitted == 1, "admitted = |Q| = 1"
    assert g.rejected_quality_keys == frozenset({BAD_CODE})
    assert g.unknown_missing_keys == frozenset({ABSENT_CODE})
    assert g.unexpected_raw_keys == frozenset({"600999"})
    assert g.duplicate_raw_keys == frozenset({BAD_CODE})
    assert g.identity_holds()


def test_ledger_identity_is_mechanical():
    """``requested == admitted + quality + missing``（互斥并集）。"""
    g = parse_ulist_detailed(_algebra_fixture(), requested=REQ)
    assert (g.admitted + len(g.rejected_quality_keys)
            + len(g.unknown_missing_keys)) == g.requested
    # 三桶两两不交
    q = frozenset(x.code for x in g.quotes)
    assert not (q & g.rejected_quality_keys)
    assert not (q & g.unknown_missing_keys)
    assert not (g.rejected_quality_keys & g.unknown_missing_keys)


def test_unexpected_cannot_mask_requested_missing():
    """**错误 B 反证**：未请求的代码**不得**补上请求代码的分子。

    审计 §6 §458-488 点名的错误实现：

    ::

        错误 B：returned = 未过滤 raw unique = 3, requested = 3
        表面看 coverage=100%，但请求的 600003 其实没回来，
        只是被不相关的 600999 '补齐'了分子。
    """
    g = parse_ulist_detailed(_algebra_fixture(), requested=REQ)
    raw_unique_unfiltered = 3          # {600001, 600002, 600999}
    assert g.returned != raw_unique_unfiltered, (
        "错误 B：用未过滤 raw unique 当 returned，会让 missing 被 unexpected 掩盖")
    assert g.returned == 2
    assert g.coverage is not None and g.coverage < 1.0, (
        "600003 没回来 -> coverage 不得是 100%")
    assert ABSENT_CODE in g.unknown_missing_keys


def test_returned_never_uses_raw_row_count():
    """**错误 A 反证**：``returned`` 不得用 raw row 数量（会越界）。"""
    g = parse_ulist_detailed(_algebra_fixture(), requested=REQ)
    raw_row_count = 4
    assert raw_row_count > g.requested, "该 fixture 专门让 raw row 数超过 requested"
    assert g.returned == 2
    assert g.returned <= g.requested, "错误 A：returned 越界"


def test_quotes_are_intersected_with_request():
    """``Q`` 必须与 ``R`` 相交 —— 未请求的代码不得出现在 ``quotes`` 里。

    这是调用方契约（新浪/东财 parser 注释都写了"服务端可能回带未请求的行"），
    不能靠 caller 事后过滤。
    """
    g = parse_ulist_detailed(_algebra_fixture(), requested=REQ)
    assert {x.code for x in g.quotes} == {OK_CODE}
    assert "600999" not in {x.code for x in g.quotes}


# ---------------------------------------------------------------------------
# 旧接口兼容（"原 snapshots() 可兼容"）
# ---------------------------------------------------------------------------

def test_legacy_list_api_unchanged_sina():
    """旧的 ``parse_response`` 必须**逐字节**保持原行为。"""
    assert [q.code for q in sina_old(SINA_TEXT)] == [OK_CODE]


def test_legacy_list_api_unchanged_eastmoney():
    assert [q.code for q in parse_ulist(EASTMONEY_JSON)] == [OK_CODE]


def test_detailed_quotes_equal_legacy_quotes():
    """``detailed.quotes`` 必须与旧接口**同一个集合**（兼容性的形式化）。"""
    assert ({x.code for x in sina_detailed(SINA_TEXT, requested=REQ).quotes}
            == {q.code for q in sina_old(SINA_TEXT)})
    assert ({x.code for x in parse_ulist_detailed(
        EASTMONEY_JSON, requested=REQ).quotes}
            == {q.code for q in parse_ulist(EASTMONEY_JSON)})


# ---------------------------------------------------------------------------
# legacy 路径：不得伪装 exact raw presence
# ---------------------------------------------------------------------------

def test_legacy_projection_never_claims_exact_presence():
    """审计 §8：未迁移的源**必须**声明 ``raw_presence_known=False``。

    明确禁止的写法（审计原文）：

    ::

        raw_returned_codes = quote codes
        raw_presence_known = True

    因为"后一种写法会把 source parser 丢掉的 raw-present-invalid row
    重新包装成 provider 完全没返回"。
    """
    legacy = build_outcome(
        route="stocks", source="sina_legacy",
        normalized_request=(BAD_CODE, OK_CODE),
        raw_keys=[BAD_CODE],            # 即使给了 raw，也不许声称 exact
        quotes=[], raw_presence_known=False)
    assert legacy.raw_presence_known is False
    assert legacy.provenance == PROVENANCE_LEGACY
    assert legacy.provenance != PROVENANCE_EXACT
    # 非 exact 时，P 退化为 Q 投影，missing 不能被称作"provider 没返回"
    assert legacy.returned == legacy.admitted
    assert legacy.is_exact is False


def test_exact_path_declares_exact_provenance():
    """迁移后的源必须声明 exact —— 与 legacy 配对（否则开关没意义）。"""
    for name, g in _all_sources().items():
        assert g.raw_presence_known is True, name
        assert g.provenance == PROVENANCE_EXACT, name
        assert g.is_exact is True, name


# ---------------------------------------------------------------------------
# 边界：空请求 / 空响应 / 脏值都不许崩
# ---------------------------------------------------------------------------

def test_empty_request_is_not_a_division_by_zero():
    """``R`` 为空时 ``coverage`` 必须是 ``None``（不是 0.0，也不是 1.0）。"""
    g = parse_ulist_detailed(EASTMONEY_JSON, requested=[])
    assert g.requested == 0
    assert g.coverage is None, "空请求的覆盖率是'未测量'，不是 0% 也不是 100%"


def test_empty_payload_is_safe_for_all_three():
    assert tx_detailed("", requested=REQ).returned == 0
    assert sina_detailed("", requested=REQ).returned == 0
    assert parse_ulist_detailed({}, requested=REQ).returned == 0
    for g in (tx_detailed("", requested=REQ), sina_detailed("", requested=REQ),
              parse_ulist_detailed({}, requested=REQ)):
        assert g.unknown_missing_keys == frozenset(REQ), (
            "空响应 + exact -> 全部 requested 都是真缺席")


def test_dirty_payload_never_raises():
    """脏 payload 不许让 parser 抛 —— 判决/取数层崩掉比判错更糟。"""
    for bad in (None, 0, [], "not json", {"data": None},
                {"data": {"diff": None}}, {"data": {"diff": "zzz"}},
                {"data": {"diff": [None, 1, "x", {}]}}):
        parse_ulist_detailed(bad, requested=REQ)      # 不得抛
    for bad in (None, 123, "v_;;", "\n\n"):
        sina_detailed(bad, requested=REQ)
        tx_detailed(bad if isinstance(bad, str) else "", requested=REQ)


def test_duplicate_detection_covers_unrequested_codes():
    """未请求的代码被返回两次，同样是 provider 侧异常，不该被静默吞掉。"""
    payload = {"data": {"total": 2, "diff": [
        _em_row("600999", valid=True), _em_row("600999", valid=True)]}}
    g = parse_ulist_detailed(payload, requested=REQ)
    assert "600999" in g.duplicate_raw_keys
    assert "600999" in g.unexpected_raw_keys
    assert g.unknown_missing_keys == frozenset(REQ)


def test_as_dict_is_json_serializable_and_complete():
    """回传产物必须可 JSON 化，且键集覆盖审计要求的诊断。"""
    import json
    g = parse_ulist_detailed(_algebra_fixture(), requested=REQ)
    d = g.as_dict()
    json.dumps(d)                                     # 不得抛
    for k in ("route", "source", "provenance", "raw_presence_known",
              "requested", "returned", "admitted", "coverage",
              "requested_keys", "raw_returned_requested_keys",
              "unknown_missing", "rejected_quality",
              "unexpected_raw_keys", "duplicate_raw_keys",
              "raw_rows", "identity_holds"):
        assert k in d, f"产物缺 {k}"
    assert d["identity_holds"] is True
    assert d["returned"] == 2 and d["requested"] == 3


# ---------------------------------------------------------------------------
# 键轴一致性（易错点：指数带前缀、个股裸码）
# ---------------------------------------------------------------------------

def test_tencent_key_axis_matches_quote_code_for_stocks():
    """请求集与 raw 键必须**同一轴**，否则交集恒空（把正常取数报成全 missing）。

    腾讯的 ``_norm_specs`` 一律返回 ``sh600000`` 形式，
    而 ``Quote.code`` 个股是裸 ``600000``、**只有指数**才带前缀。
    直接用 ``_norm_specs`` 的 ``sym`` 当请求集，会让每一次正常取数
    都变成"全部 missing"。
    """
    g = tx_detailed(TENCENT_TEXT, requested=["600000", "600002"])
    assert {q.code for q in g.quotes} <= {"600000", "600002"}
    assert g.requested_keys == frozenset({"600000", "600002"}), (
        f"个股请求必须是裸码轴，实测 {g.requested_keys}")
    # 600002 在 fixture 里是正常行 -> 必须被认出来（交集非空）
    assert OK_CODE in g.raw_returned_requested_keys
    assert g.admitted >= 1, "裸码轴正确时 600002 必须可解析出来"


def test_tencent_index_uses_prefixed_axis():
    """显式前缀的指数必须用**带前缀**键（000001 无法区分上证指数与平安银行）。"""
    idx_text = "\n".join([
        _tx_line("sh000001", "000001", "3900.00", "3890.00"),
        _tx_line("sz000001", "000001", "11.00", "10.00"),
    ])
    g = tx_detailed(idx_text, requested=["sh000001", "000001"],
                    index_codes={"sh000001"})
    assert "sh000001" in g.requested_keys, "显式指数请求必须是带前缀键"
    assert "000001" in g.requested_keys, "裸 000001 是个股（平安银行）"
    assert g.admitted == 2, (
        f"指数与个股必须各占一个键，实测 quotes={[q.code for q in g.quotes]}")


# ---------------------------------------------------------------------------
# merge_outcomes：多批/多页合并（腾讯 chunk、东财 page）
# ---------------------------------------------------------------------------

def test_merge_union_semantics():
    """合并必须是**并集**，且任一批次有 raw present 就整体算 present。

    ⚠ 夹具要小心：批次 b 的 ``raw_keys`` **不能**包含 ``ABSENT_CODE``，
    否则 b 自己就把它声明成 raw-present-no-quote，合并后进 quality 是**正确**的
    —— 我第一版就是这么写的，于是误判 merge 有 bug。
    """
    from arad.sources.outcome import merge_outcomes
    a = build_outcome(route="stocks", source="s", normalized_request=REQ,
                      raw_keys=[BAD_CODE], quotes=[])
    b = build_outcome(route="stocks", source="s", normalized_request=REQ,
                      raw_keys=[OK_CODE],
                      quotes=parse_ulist_detailed(
                          EASTMONEY_JSON, requested=REQ).quotes)
    m = merge_outcomes([a, b])
    assert m.requested_keys == frozenset(REQ)
    assert m.raw_returned_requested_keys == frozenset({BAD_CODE, OK_CODE}), (
        "两批的 P 必须并起来")
    assert m.unknown_missing_keys == frozenset({ABSENT_CODE}), (
        "600003 在两批里都没 raw 证据 -> 仍是真缺席")
    assert m.rejected_quality_keys == frozenset({BAD_CODE})
    assert m.identity_holds()


def test_merge_dedupes_quotes_keeps_first():
    """跨批次同一个 code 的 Quote 只留**首个**（与各源既有 ``_dedupe`` 同口径）。"""
    from arad.sources.outcome import merge_outcomes
    q = parse_ulist_detailed(EASTMONEY_JSON, requested=REQ).quotes
    m = merge_outcomes([
        build_outcome(route="stocks", source="s", normalized_request=REQ,
                      raw_keys=[OK_CODE], quotes=q),
        build_outcome(route="stocks", source="s", normalized_request=REQ,
                      raw_keys=[OK_CODE], quotes=q),
    ])
    assert len(m.quotes) == 1, "同一个 code 不得出现两次"
    assert m.duplicate_raw_keys == frozenset({OK_CODE}), (
        "跨批次重复同样要记进 duplicate_raw_keys")


def test_merge_is_monotone_when_any_part_is_legacy():
    """D14：``unknown`` 是单位元 —— 任一批次无 raw 证据，整体就不得声称 exact。

    这是**合并单调性**：加进一个 legacy 批次之后，整体证据只能变弱。
    """
    from arad.sources.outcome import merge_outcomes
    exact = build_outcome(route="stocks", source="s", normalized_request=REQ,
                          raw_keys=[BAD_CODE, OK_CODE], quotes=[])
    legacy = build_outcome(route="stocks", source="s", normalized_request=REQ,
                           raw_keys=[BAD_CODE], quotes=[],
                           raw_presence_known=False)
    assert exact.raw_presence_known is True
    assert legacy.raw_presence_known is False
    m = merge_outcomes([exact, legacy])
    assert m.raw_presence_known is False, (
        "混合 exact + legacy 必须降级为 legacy，**不得**自称 exact")
    assert m.provenance == PROVENANCE_LEGACY
    assert m.is_exact is False


def test_merge_empty_is_safe():
    """空批次列表不得崩，且必须声明 legacy（没有证据就不能声称 exact）。"""
    from arad.sources.outcome import merge_outcomes
    m = merge_outcomes([])
    assert m.requested == 0
    assert m.coverage is None
    assert m.provenance == PROVENANCE_LEGACY
    assert m.raw_presence_known is False


# ---------------------------------------------------------------------------
# 类级出口：snapshots_detailed 必须存在，且旧 snapshots() 保留
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cls_name", ["TencentSource", "SinaSource",
                                      "EastmoneySource"])
def test_source_class_exposes_v4_outlet(cls_name):
    """三源都**必须**提供 v4 出口 —— 只做一个会被 failover 打回原形。

    审计明确要求"三源同时实现"：默认链路是 tencent 主 + sina 备，
    只迁移东财的话，一次 failover 就回到旧的语义漂移。
    """
    import arad.sources.eastmoney as em
    import arad.sources.sina as sn
    import arad.sources.tencent as tx
    cls = {"TencentSource": tx.TencentSource, "SinaSource": sn.SinaSource,
           "EastmoneySource": em.EastmoneySource}[cls_name]
    src = cls()
    assert hasattr(src, "snapshots_detailed"), (
        f"{cls_name} 缺 v4 出口 —— 该源上的 raw-presence 仍不可分辨")
    assert hasattr(src, "snapshots"), (
        f"{cls_name} 必须保留旧接口（兼容性）")
    assert src.name in ("tencent", "sina", "eastmoney")


@pytest.mark.parametrize("cls_name", ["TencentSource", "SinaSource",
                                      "EastmoneySource"])
def test_v4_outlet_empty_request_returns_declared_outcome(cls_name):
    """空请求必须返回一个**声明完整**的 outcome（不是 [] 也不是 None）。"""
    from arad.sources.outcome import SnapshotFetchResult
    import arad.sources.eastmoney as em
    import arad.sources.sina as sn
    import arad.sources.tencent as tx
    cls = {"TencentSource": tx.TencentSource, "SinaSource": sn.SinaSource,
           "EastmoneySource": em.EastmoneySource}[cls_name]
    got = cls().snapshots_detailed([])
    assert isinstance(got, SnapshotFetchResult)
    assert got.requested == 0
    assert got.coverage is None, "空请求的覆盖率是'未测量'"
    assert got.raw_presence_known is True, (
        "空请求没有 parser 丢行的可能，可以声称 exact")

