"""``arad.sources.eastmoney`` 离线单测 —— 禁止真实网络。

全部断言基于 ``fixtures/raw/`` 里探针抓下来的**真实响应**（2026-09-15 采集）。
契约：``docs/DATA_CONTRACT.md`` 第 2.2 节。
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse

import pytest
from fakes import load_raw

from arad.models import Board
from arad.sources.base import SourceError, Source
from arad.sources.eastmoney import (
    BULK_LIMIT,
    CLIST_URL,
    FS_ALL,
    ULIST_URL,
    UT,
    EastmoneySource,
    parse_clist,
    parse_ulist,
    secid_of,
)

# --------------------------------------------------------------------------
# 真实样本的ground truth（用 pwsh/python 从 fixture 读出，不是猜的）
# --------------------------------------------------------------------------
# eastmoney_clist_p1.txt: total=5913, diff=100 行, 按 f3 降序（探针用 fid=f3 抓的）
CLIST_TOTAL = 5913
CLIST_ROWS = 100

# diff[0] 满坤科技 301132
ROW0 = {
    "f2": 58.37, "f3": 20.0, "f5": 165919, "f6": 898002256.64, "f8": 17.51,
    "f10": 1.7, "f11": 0.0, "f12": "301132", "f13": 0, "f14": "满坤科技",
    "f15": 58.37, "f16": 46.08, "f17": 47.56, "f18": 48.64,
    "f20": 8643794354, "f21": 5530017111, "f22": 0.0,
    "f26": 20220810, "f124": 1789371291,
}
# diff[1] 高凯技术 688835（沪市，f13=1）
ROW1 = {
    "f2": 324.41, "f12": "688835", "f13": 1, "f14": "高凯技术",
    "f15": 324.41, "f16": 274.0, "f17": 274.0, "f18": 270.34,
    "f20": 32425009182, "f21": 6093323282, "f26": 20260825, "f124": 1789373501,
}
ROW_LAST = {"f2": 119.47, "f12": "688260", "f14": "昀冢科技", "f18": 109.9, "f26": 20210406}

EM_CFG = {"page_size": 100, "max_pages": 80, "workers": 8, "timeout": 15, "retries": 3}


# --------------------------------------------------------------------------
# 假 fetcher：记录调用、可按 URL 返回不同 payload
# --------------------------------------------------------------------------
class FakeFetcher:
    """``fetcher(url, headers, timeout) -> bytes`` 的可编程替身。"""

    def __init__(self, responder):
        self.responder = responder
        self.calls: list[tuple[str, dict, float]] = []
        self._lock = threading.Lock()

    def __call__(self, url: str, headers: dict, timeout: float) -> bytes:
        with self._lock:
            self.calls.append((url, dict(headers or {}), timeout))
        out = self.responder(url, headers, timeout)
        if isinstance(out, str):
            out = out.encode("utf-8")
        if isinstance(out, Exception):
            raise out
        return out

    # --- 断言辅助 -------------------------------------------------------
    @property
    def urls(self) -> list[str]:
        return [c[0] for c in self.calls]

    @property
    def headers(self) -> list[dict]:
        return [c[1] for c in self.calls]

    @property
    def timeouts(self) -> list[float]:
        return [c[2] for c in self.calls]

    def param(self, url: str, key: str) -> str | None:
        """取 query 参数原始值（保序、不做 ``+``->空格 的 plus 解码）。

        ``parse_qs`` 会把 ``+`` 解成空格，而 ``fs`` 里正好全是 ``+``（契约要求
        ``m:0+t:6,...`` 原样），故用 ``parse_qsl(keep_blank_values)`` 后手工 unquote。
        """
        qs = urllib.parse.urlparse(url).query
        for k, v in urllib.parse.parse_qsl(qs, keep_blank_values=True):
            if k == key:
                return urllib.parse.unquote(v)
        return None

    def count_path(self, needle: str) -> int:
        return sum(1 for u in self.urls if needle in u)

    def clist_pages(self) -> list[int]:
        return [
            int(self.param(u, "pn"))
            for u in self.urls
            if CLIST_URL in u and self.param(u, "pn") is not None
        ]


def _clist_payload(total: int, rows: list[dict]) -> str:
    return json.dumps({"rc": 0, "data": {"total": total, "diff": rows}})


def _row(code: str, **kw) -> dict:
    """造一行结构完整的 clist 记录（默认值取真实样本的量级）。"""
    base = {
        "f2": 10.0, "f3": 1.0, "f5": 1000, "f6": 1_000_000.0, "f8": 1.0,
        "f10": 1.0, "f11": 0.0, "f12": code, "f13": 0, "f14": f"股票{code}",
        "f15": 10.2, "f16": 9.8, "f17": 9.9, "f18": 9.9,
        "f20": 1_000_000_000, "f21": 800_000_000, "f22": 0.0,
        "f26": 20200101, "f124": 1789371291,
    }
    base.update(kw)
    return base


# ==========================================================================
# 1. parse_clist —— 真实样本
# ==========================================================================
class TestParseClistFixture:
    def test_returns_exactly_100_rows(self):
        quotes = parse_clist(load_raw("eastmoney_clist_p1.txt"))
        assert len(quotes) == CLIST_ROWS == 100
        # 全部是 6 位数字代码，且无重复
        assert all(re.fullmatch(r"\d{6}", q.code) for q in quotes)
        assert len({q.code for q in quotes}) == 100

    def test_first_row_matches_file_verbatim(self):
        q = parse_clist(load_raw("eastmoney_clist_p1.txt"))[0]
        assert q.code == ROW0["f12"] == "301132"
        assert q.name == ROW0["f14"] == "满坤科技"
        # f2=现价  f18=昨收  f17=今开  f15=最高  f16=最低
        assert q.price == pytest.approx(ROW0["f2"]) == pytest.approx(58.37)
        assert q.prev_close == pytest.approx(ROW0["f18"]) == pytest.approx(48.64)
        assert q.open == pytest.approx(ROW0["f17"]) == pytest.approx(47.56)
        assert q.high == pytest.approx(ROW0["f15"])
        assert q.low == pytest.approx(ROW0["f16"])
        assert q.board is Board.GEM  # 301xxx -> 创业板

    def test_second_row_is_star_board_shanghai(self):
        qs = parse_clist(load_raw("eastmoney_clist_p1.txt"))
        q = qs[1]
        assert q.code == ROW1["f12"] == "688835"
        assert q.name == ROW1["f14"] == "高凯技术"
        assert q.price == pytest.approx(ROW1["f2"]) == pytest.approx(324.41)
        assert q.prev_close == pytest.approx(ROW1["f18"]) == pytest.approx(270.34)
        assert q.board is Board.STAR  # 688 -> 科创板
        assert secid_of(q.code).startswith("1.")  # f13=1 沪市

    def test_last_row_matches_file(self):
        q = parse_clist(load_raw("eastmoney_clist_p1.txt"))[-1]
        assert q.code == ROW_LAST["f12"] == "688260"
        assert q.name == ROW_LAST["f14"] == "昀冢科技"
        assert q.price == pytest.approx(ROW_LAST["f2"])
        assert q.prev_close == pytest.approx(ROW_LAST["f18"])

    def test_volume_amount_turnover_ratio_units_are_contract_units(self):
        """f5 已是手、f6 已是元、f8 是 %、f10 是量比 —— 都不再换算。"""
        q = parse_clist(load_raw("eastmoney_clist_p1.txt"))[0]
        assert q.volume_lots == pytest.approx(ROW0["f5"]) == pytest.approx(165919)
        assert q.amount == pytest.approx(ROW0["f6"]) == pytest.approx(898002256.64)
        assert q.turnover == pytest.approx(ROW0["f8"]) == pytest.approx(17.51)
        assert q.volume_ratio == pytest.approx(ROW0["f10"]) == pytest.approx(1.7)
        # 派生量自洽：vwap = 额 / (手*100)
        assert q.vwap == pytest.approx(q.amount / (q.volume_lots * 100.0))
        assert q.pct == pytest.approx(20.0, abs=0.01)  # 涨停 +20%

    def test_caps_are_yi_yuan_not_yuan(self):
        """f20/f21 是元，必须 /1e8 变成亿元（契约第 0 节）。"""
        q = parse_clist(load_raw("eastmoney_clist_p1.txt"))[0]
        assert q.total_cap == pytest.approx(ROW0["f20"] / 1e8)
        assert q.float_cap == pytest.approx(ROW0["f21"] / 1e8)
        # 亿元量级：远小于 1e6（若忘记换算会是 86 亿 -> 8.6e9）
        assert q.total_cap < 1e6
        assert q.float_cap < 1e6
        assert 1.0 < q.total_cap < 1e5
        assert q.float_cap < q.total_cap  # 流通 <= 总市值
        # 全表都必须是亿元量级
        for x in parse_clist(load_raw("eastmoney_clist_p1.txt")):
            assert 0 < x.total_cap < 1e6, x.code

    def test_ts_parsed_from_f124_and_board_from_code(self):
        q = parse_clist(load_raw("eastmoney_clist_p1.txt"))[0]
        assert q.ts is not None
        assert q.ts.year == 2026 and q.ts.month == 9
        assert q.ts.strftime("%Y-%m-%d %H:%M:%S") == "2026-09-14 15:34:51"

    def test_seq_is_propagated(self):
        quotes = parse_clist(load_raw("eastmoney_clist_p1.txt"), seq=7)
        assert quotes and all(q.seq == 7 for q in quotes)

    def test_no_bid_ask_in_clist(self):
        """clist 不含委买委卖；f11 是「5分钟涨跌幅」不是买一价，必须留在 0。"""
        for q in parse_clist(load_raw("eastmoney_clist_p1.txt")):
            assert q.bid1 == 0.0 and q.ask1 == 0.0
            assert q.bid_vol == 0.0 and q.ask_vol == 0.0

    def test_limit_prices_left_zero_for_fallback_computation(self):
        q = parse_clist(load_raw("eastmoney_clist_p1.txt"))[0]
        assert q.limit_up == 0.0 and q.limit_down == 0.0
        # 东财不给涨跌停价 -> 由 Quote 按板块费率推算（创业板 20%）
        assert q.limit_up_price == pytest.approx(round(q.prev_close * 1.2, 2))


# ==========================================================================
# 2. parse_clist —— 边界与防御
# ==========================================================================
class TestParseClistEdgeCases:
    def test_dash_placeholder_rows_are_dropped_not_zeroed(self):
        """f2/f18 为 "-" -> 该条**丢弃**（不是置 0）。"""
        payload = _clist_payload(
            3,
            [
                _row("600000"),
                _row("430047", f2="-", f18=8.17, f14="诺思兰德(已切换)"),
                _row("600001", f2=10.0, f18="-"),
                _row("600002"),
            ],
        )
        quotes = parse_clist(payload)
        assert [q.code for q in quotes] == ["600000", "600002"]
        assert all(q.code != "430047" for q in quotes)
        assert all(q.price > 0 for q in quotes)

    def test_ulist_fixture_drops_the_dashed_stock(self):
        """真实 ulist 样本里 430047 整行是 "-"，必须被剔除 -> 只剩 4 条。"""
        quotes = parse_ulist(load_raw("eastmoney_ulist_5.txt"))
        assert [q.code for q in quotes] == ["600000", "000001", "300750", "688111"]
        assert "430047" not in {q.code for q in quotes}

    def test_data_null_returns_empty_list(self):
        assert parse_clist('{"rc":0,"data":null}') == []
        assert parse_clist({"rc": 0, "data": None}) == []

    def test_diff_null_returns_empty_list(self):
        assert parse_clist('{"rc":0,"data":{"total":5913,"diff":null}}') == []
        assert parse_clist({"rc": 0, "data": {"total": 5913, "diff": None}}) == []

    def test_garbage_payload_returns_empty_list(self):
        for bad in ("", "   ", "not json at all", "<html>502</html>", "[]", "null"):
            assert parse_clist(bad) == []

    def test_suspended_rows_with_zero_price_dropped(self):
        quotes = parse_clist(_clist_payload(2, [_row("600000", f2=0.0), _row("600001")]))
        assert [q.code for q in quotes] == ["600001"]

    def test_star_st_name_maps_to_main_but_quote_keeps_name(self):
        quotes = parse_clist(_clist_payload(1, [_row("600018", f14="ST上港")]))
        assert quotes[0].name == "ST上港"
        assert quotes[0].board is Board.MAIN

    def test_missing_optional_fields_default_safely(self):
        rows = [{"f12": "600000", "f14": "浦发银行", "f2": 9.4, "f18": 9.26}]
        q = parse_clist(_clist_payload(1, rows))[0]
        assert q.volume_lots == 0.0 and q.amount == 0.0
        assert q.total_cap == 0.0 and q.float_cap == 0.0
        assert q.ts is None
        # 开/高/低缺失时用现价与昨收兜底，避免 0 值污染振幅计算
        assert q.open == pytest.approx(9.26)
        assert q.high == pytest.approx(9.4) and q.low == pytest.approx(9.26)

    def test_f26_list_date_parsed_to_yyyymmdd_string(self):
        """f26 上市日期是 YYYYMMDD 整数；解析失败置 0/空串（不抛异常）。"""
        q = parse_clist(load_raw("eastmoney_clist_p1.txt"))[0]
        assert q.list_date == "20220810"  # 满坤科技 f26=20220810
        bad = parse_clist(_clist_payload(1, [_row("600000", f26="bad-date")]))[0]
        assert bad.list_date == ""
        zero = parse_clist(_clist_payload(1, [_row("600000", f26=0)]))[0]
        assert zero.list_date == ""
        missing = parse_clist(_clist_payload(1, [{"f12": "600000", "f2": 9.4, "f18": 9.26}]))[0]
        assert missing.list_date == ""

    def test_invalid_calendar_date_rejected(self):
        """20220230 不是合法日期 -> 空串（否则 filters 会 ValueError）。"""
        q = parse_clist(_clist_payload(1, [_row("600000", f26=20220230)]))[0]
        assert q.list_date == ""

    def test_json_string_and_dict_inputs_are_equivalent(self):
        payload = _clist_payload(1, [_row("600000")])
        assert parse_clist(payload) == parse_clist(json.loads(payload))

    def test_bytes_payload_utf8_decoded(self):
        payload = _clist_payload(1, [_row("600000", f14="浦发银行")]).encode("utf-8")
        assert parse_clist(payload)[0].name == "浦发银行"

    def test_diff_as_mapping_is_tolerated(self):
        payload = {"data": {"total": 1, "diff": {"0": _row("600000")}}}
        assert [q.code for q in parse_clist(payload)] == ["600000"]


# ==========================================================================
# 3. secid / 前缀
# ==========================================================================
class TestSecid:
    @pytest.mark.parametrize(
        "code,expected",
        [
            ("600000", "1.600000"),   # 沪
            ("688111", "1.688111"),   # 沪科创
            ("000001", "0.000001"),   # 深
            ("300750", "0.300750"),   # 深创业
            ("002594", "0.002594"),   # 深中小
            ("430047", "0.430047"),   # 北交所 -> 0.
            ("830799", "0.830799"),   # 北交所 -> 0.
            ("920819", "0.920819"),   # 北交所新代码段 -> 0.
        ],
    )
    def test_secid_market_prefix(self, code, expected):
        assert secid_of(code) == expected

    def test_secid_accepts_prefixed_input(self):
        assert secid_of("sh600000") == "1.600000"
        assert secid_of("sz000001") == "0.000001"


# ==========================================================================
# 4. universe() 翻页
# ==========================================================================
class TestUniversePaging:
    def _paging_fetcher(self, total: int = 5913, per_page: int = 100):
        def responder(url, headers, timeout):
            pn = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["pn"][0])
            start = (pn - 1) * per_page
            n = max(0, min(per_page, total - start))
            rows = [_row(f"{600000 + start + i}") for i in range(n)]
            return _clist_payload(total, rows)

        return FakeFetcher(responder)

    def test_total_5913_issues_60_page_requests(self):
        """first 请求拿 total=5913 -> 1 + 59 并发页 = 60 次。"""
        f = self._paging_fetcher()
        src = EastmoneySource(EM_CFG, fetcher=f)
        quotes = src.universe()

        pages = f.clist_pages()
        assert len(pages) == 60, pages
        assert sorted(pages) == list(range(1, 61))
        assert f.count_path(CLIST_URL) == 60
        assert len(quotes) == 5913

    def test_page1_requested_once_and_first(self):
        f = self._paging_fetcher()
        EastmoneySource(EM_CFG, fetcher=f).universe()
        # 第 1 页必须先单独请求（用来拿 total），不能并发重复抓
        assert f.clist_pages()[0] == 1
        assert f.clist_pages().count(1) == 1

    def test_max_pages_caps_requests(self):
        """max_pages=3 -> 只抓 3 页，即使 total 需要 60 页。"""
        f = self._paging_fetcher()
        src = EastmoneySource({**EM_CFG, "max_pages": 3}, fetcher=f)
        quotes = src.universe()
        assert sorted(f.clist_pages()) == [1, 2, 3]
        assert len(quotes) == 300

    def test_page_size_is_forced_to_100(self):
        """实测 pz 被服务端硬限制为 100：配置 1000 也必须压回 100。"""
        f = self._paging_fetcher()
        src = EastmoneySource({**EM_CFG, "page_size": 1000}, fetcher=f)
        assert src.page_size == 100
        src.universe()
        assert all(f.param(u, "pz") == "100" for u in f.urls if CLIST_URL in u)
        assert len(f.clist_pages()) == 60

    def test_request_parameters_match_contract(self):
        f = self._paging_fetcher(total=150)
        EastmoneySource(EM_CFG, fetcher=f).universe()
        url = f.urls[0]
        assert f.param(url, "po") == "0"
        assert f.param(url, "np") == "1"
        assert f.param(url, "fltt") == "2"
        assert f.param(url, "invt") == "2"
        assert f.param(url, "fid") == "f12"
        assert f.param(url, "ut") == UT
        assert f.param(url, "fs") == FS_ALL
        assert f.param(url, "fields") == (
            "f2,f3,f5,f6,f8,f10,f11,f12,f13,f14,f15,f16,f17,f18,f20,f21,f22,f26,f124"
        )

    def test_null_first_page_is_not_a_failure(self):
        """data/diff 为 null 只代表该页无数据，不算失败。"""
        f = FakeFetcher(lambda u, h, t: '{"rc":0,"data":null}')
        quotes = EastmoneySource(EM_CFG, fetcher=f).universe()
        assert quotes == []
        assert EastmoneySource(EM_CFG, fetcher=f).health()["ok"] is True

    def test_null_diff_probes_pages_until_empty(self):
        """total 不可用时顺序探测，遇空页即停（不轰炸 80 页）。"""
        def responder(url, headers, timeout):
            pn = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["pn"][0])
            if pn <= 3:
                return _clist_payload(0, [_row(f"60000{pn}")])
            return '{"rc":0,"data":{"total":0,"diff":null}}'

        f = FakeFetcher(responder)
        quotes = EastmoneySource(EM_CFG, fetcher=f).universe()
        assert len(quotes) == 3
        assert sorted(f.clist_pages()) == [1, 2, 3, 4]

    def test_universe_dedupes_codes(self):
        """分页边界重码（同一只股票出现在两页）只保留一条。"""
        def responder(url, headers, timeout):
            pn = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["pn"][0])
            if pn == 1:
                return _clist_payload(300, [_row("600000"), _row("600001")])
            return _clist_payload(300, [_row("600001"), _row("600002")])

        quotes = EastmoneySource(EM_CFG, fetcher=FakeFetcher(responder)).universe()
        assert [q.code for q in quotes] == ["600000", "600001", "600002"]

    def test_single_bad_page_is_tolerated_but_reported(self):
        """universe 容忍个别页失败（统计可见），但不会整体崩掉。"""
        def responder(url, headers, timeout):
            pn = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["pn"][0])
            if pn == 3:
                raise RuntimeError("boom")
            return _clist_payload(500, [_row(f"{600000 + pn}")])

        f = FakeFetcher(responder)
        src = EastmoneySource({**EM_CFG, "retries": 1}, fetcher=f)
        quotes = src.universe()
        # total=500 -> 5 页；其中第 3 页整页丢失
        assert sorted(f.clist_pages()) == [1, 2, 3, 4, 5]
        assert len(quotes) == 4
        assert src.stats()["pages_failed"] == 1
        assert "3" in src.stats()["last_err"]
        assert src.health()["ok"] is False

    def test_universe_raises_source_error_when_every_page_fails(self):
        f = FakeFetcher(lambda u, h, t: RuntimeError("network down"))
        src = EastmoneySource({**EM_CFG, "retries": 1}, fetcher=f)
        with pytest.raises(SourceError):
            src.universe()
        assert src.health()["ok"] is False
        assert src.health()["err"]


# ==========================================================================
# 5. snapshots() 分批
# ==========================================================================
class TestSnapshotsBatching:
    def _ulist_fetcher(self):
        def responder(url, headers, timeout):
            secids = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["secids"][0]
            rows = [_row(s.split(".")[1]) for s in secids.split(",")]
            return json.dumps({"rc": 0, "data": {"total": len(rows), "diff": rows}})

        return FakeFetcher(responder)

    def test_800_codes_split_into_50_code_batches(self):
        """实测 800 码 -> HTTP 502；每批最多 50 码 -> 16 批。"""
        f = self._ulist_fetcher()
        src = EastmoneySource(EM_CFG, fetcher=f)
        codes = [f"{600000 + i}" for i in range(800)]
        quotes = src.snapshots(codes)
        assert len(f.urls) == 16
        assert all(ULIST_URL in u for u in f.urls)
        for u in f.urls:
            secids = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)["secids"][0]
            assert 1 <= len(secids.split(",")) <= BULK_LIMIT <= 50
        assert len(quotes) == 800

    def test_50_codes_is_a_single_request(self):
        f = self._ulist_fetcher()
        EastmoneySource(EM_CFG, fetcher=f).snapshots([f"{600000 + i}" for i in range(50)])
        assert len(f.urls) == 1

    def test_51_codes_needs_two_requests(self):
        f = self._ulist_fetcher()
        EastmoneySource(EM_CFG, fetcher=f).snapshots([f"{600000 + i}" for i in range(51)])
        assert len(f.urls) == 2

    def test_bulk_chunk_config_is_also_capped_at_50(self):
        f = self._ulist_fetcher()
        src = EastmoneySource({**EM_CFG, "bulk_chunk": 800}, fetcher=f)
        assert src.bulk_chunk == 50
        src.snapshots([f"{600000 + i}" for i in range(80)])
        assert len(f.urls) == 2

    def test_snapshots_fixture_parses(self):
        """用真实 ulist 样本喂 fetcher，验证单位换算与无效行剔除。"""
        raw = load_raw("eastmoney_ulist_5.txt")
        f = FakeFetcher(lambda u, h, t: raw)
        quotes = EastmoneySource(EM_CFG, fetcher=f).snapshots(["600000", "000001", "300750", "688111", "430047"])
        assert [q.code for q in quotes] == ["600000", "000001", "300750", "688111"]
        pufa = quotes[0]
        assert pufa.price == pytest.approx(9.4)
        assert pufa.prev_close == pytest.approx(9.26)
        assert pufa.total_cap == pytest.approx(313074880020 / 1e8)  # 3130.7488002 亿
        assert pufa.total_cap < 1e6

    def test_snapshots_empty_input_no_request(self):
        f = self._ulist_fetcher()
        assert EastmoneySource(EM_CFG, fetcher=f).snapshots([]) == []
        assert f.calls == []

    def test_snapshots_seqs_increase_across_calls(self):
        f = self._ulist_fetcher()
        src = EastmoneySource(EM_CFG, fetcher=f)
        a = src.snapshots(["600000"])
        b = src.snapshots(["600000"])
        assert a[0].seq == 1 and b[0].seq == 2

    def test_snapshots_failure_raises_source_error(self):
        f = FakeFetcher(lambda u, h, t: RuntimeError("502 bad gateway"))
        src = EastmoneySource({**EM_CFG, "retries": 1}, fetcher=f)
        with pytest.raises(SourceError):
            src.snapshots(["600000", "000001"])
        assert src.health()["ok"] is False

    def test_secids_use_correct_market_prefix(self):
        f = self._ulist_fetcher()
        EastmoneySource(EM_CFG, fetcher=f).snapshots(["600000", "000001", "430047"])
        secids = urllib.parse.parse_qs(urllib.parse.urlparse(f.urls[0]).query)["secids"][0]
        assert secids == "1.600000,0.000001,0.430047"


# ==========================================================================
# 6. 重试 / headers / health
# ==========================================================================
class TestRetriesHeadersHealth:
    def test_retries_then_succeeds(self):
        state = {"n": 0}

        def responder(url, headers, timeout):
            state["n"] += 1
            if state["n"] < 3:
                raise RuntimeError("temporary")
            return _clist_payload(5, [_row("600000")])

        f = FakeFetcher(responder)
        src = EastmoneySource({**EM_CFG, "retries": 3}, fetcher=f)
        quotes = src.universe()
        assert [q.code for q in quotes] == ["600000"]
        assert state["n"] == 3
        assert src.stats()["retries"] == 2
        assert src.health()["ok"] is True

    def test_exhausted_retries_raise_source_error(self):
        f = FakeFetcher(lambda u, h, t: RuntimeError("permanent"))
        src = EastmoneySource({**EM_CFG, "retries": 3}, fetcher=f)
        with pytest.raises(SourceError) as ei:
            src.universe()
        assert "eastmoney" in str(ei.value)
        assert len(f.calls) == 3  # 正好 retries 次尝试
        h = src.health()
        assert h["ok"] is False and "permanent" in h["err"]

    def test_backoff_schedule_is_0_3_0_9_2_0(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
        f = FakeFetcher(lambda u, h, t: RuntimeError("down"))
        src = EastmoneySource({**EM_CFG, "retries": 4}, fetcher=f)
        with pytest.raises(SourceError):
            src.universe()
        assert slept == [0.3, 0.9, 2.0]

    def test_empty_body_is_treated_as_failure(self):
        f = FakeFetcher(lambda u, h, t: b"")
        src = EastmoneySource({**EM_CFG, "retries": 2}, fetcher=f)
        with pytest.raises(SourceError):
            src.universe()

    def test_non_json_body_raises_source_error(self):
        f = FakeFetcher(lambda u, h, t: b"<html>502 Bad Gateway</html>")
        src = EastmoneySource({**EM_CFG, "retries": 1}, fetcher=f)
        with pytest.raises(SourceError):
            src.universe()

    def test_headers_sent_to_fetcher(self):
        """记录 fetcher 收到的 headers，逐一核对契约要求。"""
        f = FakeFetcher(lambda u, h, t: _clist_payload(1, [_row("600000")]))
        EastmoneySource(EM_CFG, fetcher=f).universe()
        assert f.headers, "fetcher 必须收到 headers"
        for h in f.headers:
            assert "Referer" in h
            assert h["Referer"].startswith("https://")
            assert "Mozilla/5.0" in h["User-Agent"]
            assert h["Accept"] == "*/*"
        assert f.timeouts and all(t == 15 for t in f.timeouts)

    def test_referer_configurable(self):
        f = FakeFetcher(lambda u, h, t: _clist_payload(1, [_row("600000")]))
        EastmoneySource({**EM_CFG, "referer": "https://example.test/"}, fetcher=f).universe()
        assert f.headers[0]["Referer"] == "https://example.test/"

    def test_health_shape_and_counters(self):
        f = FakeFetcher(lambda u, h, t: _clist_payload(1, [_row("600000")]))
        src = EastmoneySource(EM_CFG, fetcher=f)
        h = src.health()
        assert set(h) == {"name", "ok", "latency_ms", "err"}
        assert h["name"] == "eastmoney"
        assert h["ok"] is True and h["err"] == ""
        assert isinstance(h["latency_ms"], int)
        src.universe()
        assert src.stats()["calls"] == 1
        assert src.stats()["quotes"] == 1

    def test_satisfies_source_protocol(self):
        src = EastmoneySource(EM_CFG, fetcher=FakeFetcher(lambda u, h, t: "{}"))
        assert isinstance(src, Source)
        assert src.name == "eastmoney"

    def test_health_thread_safe_under_concurrency(self):
        """并发翻页 + 统计读写在 Lock 保护下不应丢计数。"""
        def responder(url, headers, timeout):
            pn = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["pn"][0])
            return _clist_payload(500, [_row(f"{600000 + pn}")])

        src = EastmoneySource({**EM_CFG, "retries": 1}, fetcher=FakeFetcher(responder))
        errors: list[Exception] = []

        def worker():
            try:
                src.universe()
                src.health()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert src.stats()["calls"] == 4 * 5  # 每次 universe 抓 5 页
