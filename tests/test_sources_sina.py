"""``arad.sources.sina`` 离线单测 —— 禁止真实网络。

全部断言基于 ``fixtures/raw/sina_bulk_5.txt``（2026-09-15 探针抓下的真实响应）。
契约：``docs/DATA_CONTRACT.md`` 第 2.3 节。

**编码坑**：真实线路是 GBK，但探针脚本用 ``encoding="utf-8"`` 把样本落盘了，
所以 fixture 的字节流其实是 UTF-8。测试里两种编码都覆盖：
既验证「GBK 字节 -> 正确解析」，也验证「UTF-8 fixture -> 正确解析」。
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
from arad.sources.base import Source, SourceError
from arad.sources.sina import (
    LINE_RE,
    QUOTE_URL,
    REFERER,
    SINA_ENCODING,
    SinaSource,
    parse_response,
)

# --------------------------------------------------------------------------
# 真实样本 ground truth（从 fixture 读出，不是猜的）
# --------------------------------------------------------------------------
# sh600000 浦发银行: 今开 9.280 / 昨收 9.260 / 现价 9.400 / 高 9.430 / 低 9.240
#                    买一 9.390 / 卖一 9.400 / 成交量 77187101(股) / 成交额 722307374.000(元)
#                    日期 2026-09-14 / 时间 15:34:59
SH600000 = {
    "name": "浦发银行", "open": 9.280, "prev_close": 9.260, "price": 9.400,
    "high": 9.430, "low": 9.240, "bid1": 9.390, "ask1": 9.400,
    "volume_shares": 77187101, "amount": 722307374.000,
    "date": "2026-09-14", "time": "15:34:59",
}
SZ000001 = {
    "name": "平安银行", "open": 11.730, "prev_close": 11.740, "price": 11.850,
    "high": 11.900, "low": 11.720, "bid1": 11.850, "ask1": 11.860,
    "volume_shares": 74124802, "amount": 876071018.220,
}
SZ300750 = {"name": "宁德时代", "price": 337.110, "prev_close": 330.510, "volume_shares": 26184902}
SH688111 = {"name": "金山办公", "price": 228.590, "prev_close": 228.820, "volume_shares": 3506475}
# bj430047 诺思兰德整行全 0（停牌/退市）-> 必须被跳过；且它只有 33 个字段
BJ430047_SUSPENDED = "诺思兰德"

SINA_CFG = {"bulk_chunk": 600, "timeout": 10, "retries": 3, "workers": 4}


def fixture_text() -> str:
    """真实样本按原样（UTF-8）读出。"""
    return load_raw("sina_bulk_5.txt").decode("utf-8")


def fixture_gbk() -> bytes:
    """把样本转成真实线路的 GBK 字节，用于验证 GBK 解码路径。"""
    return fixture_text().encode(SINA_ENCODING)


# --------------------------------------------------------------------------
# 假 fetcher
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
        if isinstance(out, Exception):
            raise out
        return out.encode(SINA_ENCODING) if isinstance(out, str) else out

    @property
    def urls(self) -> list[str]:
        return [c[0] for c in self.calls]

    @property
    def headers(self) -> list[dict]:
        return [c[1] for c in self.calls]

    @property
    def timeouts(self) -> list[float]:
        return [c[2] for c in self.calls]

    def codes_of(self, url: str) -> list[str]:
        """``.../list=sh600000,sz000001`` -> ['sh600000', 'sz000001']"""
        return url.split("list=", 1)[1].split(",") if "list=" in url else []


def bulk_body(prefixed: list[str], *, price: float = 10.0) -> str:
    """造一份结构正常的新浪批量响应（32 字段，日期含逗号后的 30/31 位）。"""
    lines = []
    for p in prefixed:
        fields = ["0"] * 33
        fields[0] = f"股票{p[2:]}"
        fields[1] = "9.90"          # 今开
        fields[2] = "9.80"          # 昨收
        fields[3] = f"{price:.3f}"  # 现价
        fields[4] = "10.20"         # 最高
        fields[5] = "9.70"          # 最低
        fields[6] = "9.95"          # 买一
        fields[7] = "10.00"         # 卖一
        fields[8] = "100000"        # 成交量(股)
        fields[9] = "1000000.000"   # 成交额(元)
        fields[30] = "2026-09-14"
        fields[31] = "15:00:00"
        lines.append(f'var hq_str_{p}="{",".join(fields)}";')
    return "\n".join(lines) + "\n"


# ==========================================================================
# 1. parse_response —— 真实样本
# ==========================================================================
class TestParseResponseFixture:
    def test_parses_both_utf8_fixture_and_gbk_wire_bytes(self):
        """fixture 是 UTF-8 落盘、真实线路是 GBK —— 两条路径都要正确解出中文名。"""
        from_utf8 = parse_response(load_raw("sina_bulk_5.txt"))
        from_gbk = parse_response(fixture_gbk())
        assert [q.code for q in from_utf8] == [q.code for q in from_gbk]
        assert [q.name for q in from_utf8] == [q.name for q in from_gbk]
        assert from_utf8[0].name == "浦发银行"
        assert from_utf8[0].price == pytest.approx(from_gbk[0].price)

    def test_suspended_row_is_skipped(self):
        """bj430047 全 0（停牌/退市）-> 跳过；样本 5 行只解析出 4 条。"""
        quotes = parse_response(load_raw("sina_bulk_5.txt"))
        codes = [q.code for q in quotes]
        assert len(quotes) == 4
        assert codes == ["600000", "000001", "300750", "688111"]
        assert "430047" not in codes
        assert BJ430047_SUSPENDED not in {q.name for q in quotes}

    def test_sh600000_fields_match_file_verbatim(self):
        """契约 2.3 的顺位：0 名称 1 今开 2 昨收 3 现价 4 最高 5 最低 6 买一 7 卖一。"""
        q = next(x for x in parse_response(load_raw("sina_bulk_5.txt")) if x.code == "600000")
        assert q.name == SH600000["name"] == "浦发银行"
        assert q.open == pytest.approx(SH600000["open"]) == pytest.approx(9.28)
        assert q.prev_close == pytest.approx(SH600000["prev_close"]) == pytest.approx(9.26)
        assert q.price == pytest.approx(SH600000["price"]) == pytest.approx(9.40)
        assert q.high == pytest.approx(SH600000["high"]) == pytest.approx(9.43)
        assert q.low == pytest.approx(SH600000["low"]) == pytest.approx(9.24)
        assert q.bid1 == pytest.approx(SH600000["bid1"]) == pytest.approx(9.39)
        assert q.ask1 == pytest.approx(SH600000["ask1"]) == pytest.approx(9.40)
        assert q.board is Board.MAIN

    def test_sh600000_open_prev_close_order_not_tencent_order(self):
        """关键陷阱：新浪是「今开,昨收,现价」，腾讯是「现价,昨收,今开」。

        浦发 今开 9.280 < 昨收 9.260 不成立? 实际 9.280 > 9.260。用宁德时代更明显：
        今开 334.510 / 昨收 330.510 / 现价 337.110 —— 三个数互不相同，
        一旦按腾讯顺位解析，open/price 会明显错位。
        """
        q = next(x for x in parse_response(load_raw("sina_bulk_5.txt")) if x.code == "300750")
        assert q.open == pytest.approx(334.510)
        assert q.prev_close == pytest.approx(330.510)
        assert q.price == pytest.approx(337.110)
        # 若错抄成腾讯顺位，price 会变成 334.510
        assert q.price != pytest.approx(q.open)
        assert q.pct == pytest.approx(1.9969, abs=1e-3)

    def test_volume_lots_is_shares_divided_by_100(self):
        """契约第 0 节：成交量单位是**手**；新浪 f8 是**股** -> 必须 /100。"""
        quotes = {q.code: q for q in parse_response(load_raw("sina_bulk_5.txt"))}
        pufa = quotes["600000"]
        assert pufa.volume_lots == pytest.approx(SH600000["volume_shares"] / 100.0)
        assert pufa.volume_lots == pytest.approx(771871.01)
        assert pufa.amount == pytest.approx(SH600000["amount"]) == pytest.approx(722307374.0)
        # 与腾讯样本交叉验证：同一时刻腾讯 f6 是 771871 手
        assert round(pufa.volume_lots) == 771871

        pingan = quotes["000001"]
        assert pingan.volume_lots == pytest.approx(SZ000001["volume_shares"] / 100.0)
        assert pingan.volume_lots == pytest.approx(741248.02)
        assert round(pingan.volume_lots) == 741248  # 腾讯 f6 = 741248 手

        ningde = quotes["300750"]
        assert ningde.volume_lots == pytest.approx(SZ300750["volume_shares"] / 100.0)

        kingsoft = quotes["688111"]
        assert kingsoft.volume_lots == pytest.approx(SH688111["volume_shares"] / 100.0)
        assert kingsoft.volume_lots == pytest.approx(35064.75)

    def test_all_volume_lots_are_share_count_over_100(self):
        """逐条复查：volume_lots*100 必须等于样本里的股数。"""
        raw = fixture_text()
        shares = {
            m.group(1)[2:]: float(m.group(2).split(",")[8])
            for m in re.finditer(r'var hq_str_([a-z]{2}\d{6})="([^"]*)"', raw)
        }
        for q in parse_response(raw):
            assert q.volume_lots * 100.0 == pytest.approx(shares[q.code])

    def test_amount_is_yuan_not_wan(self):
        q = next(x for x in parse_response(load_raw("sina_bulk_5.txt")) if x.code == "600000")
        assert q.amount == pytest.approx(722307374.0)
        # 成交额/成交量 得到的均价应该在 9.3 元附近（若单位搞错会差 1e4 倍）
        assert q.vwap == pytest.approx(9.3579, abs=1e-3)
        assert 9.0 < q.vwap < 10.0

    def test_date_time_parsed_from_index_30_31(self):
        q = next(x for x in parse_response(load_raw("sina_bulk_5.txt")) if x.code == "600000")
        assert q.ts is not None
        assert q.ts.year == 2026 and q.ts.month == 9 and q.ts.day == 14
        assert q.ts.strftime("%Y-%m-%d %H:%M:%S") == "2026-09-14 15:34:59"

    def test_missing_market_data_stays_zero(self):
        """新浪没有涨停价/量比/市值/换手率：一律 0，由 Quote 属性推算。"""
        for q in parse_response(load_raw("sina_bulk_5.txt")):
            assert q.limit_up == 0.0 and q.limit_down == 0.0
            assert q.volume_ratio == 0.0
            assert q.turnover == 0.0
            assert q.float_cap == 0.0 and q.total_cap == 0.0
        q = next(x for x in parse_response(load_raw("sina_bulk_5.txt")) if x.code == "600000")
        # 主板 ±10%：由 Quote 按 board 推算，与东财样本里的真实涨停价一致
        assert q.limit_up_price == pytest.approx(round(9.26 * 1.10, 2)) == pytest.approx(10.19)
        assert q.limit_down_price == pytest.approx(round(9.26 * 0.90, 2))

    def test_seq_propagated(self):
        quotes = parse_response(load_raw("sina_bulk_5.txt"), seq=42)
        assert quotes and all(q.seq == 42 for q in quotes)

    def test_bid_ask_volumes_converted_to_lots(self):
        """f10/f20 是买一量/卖一量（股）-> 手。"""
        q = next(x for x in parse_response(load_raw("sina_bulk_5.txt")) if x.code == "600000")
        assert q.bid_vol == pytest.approx(114500 / 100.0) == pytest.approx(1145.0)
        assert q.ask_vol == pytest.approx(242450 / 100.0) == pytest.approx(2424.5)


# ==========================================================================
# 2. parse_response —— 边界
# ==========================================================================
class TestParseResponseEdgeCases:
    def test_empty_payload_skipped(self):
        """``hq_str_xxx=""``（停牌/无此代码）整行跳过。"""
        text = 'var hq_str_sh600000="";\nvar hq_str_sz000001="";\n'
        assert parse_response(text) == []

    def test_short_payload_skipped(self):
        """字段不足 32 个 -> 跳过（样本里 430047 只有 33 个字段是巧合，别依赖）。"""
        assert parse_response('var hq_str_sh600000="浦发银行,9.28";') == []

    def test_empty_input(self):
        for bad in ("", "   ", "\n\n"):
            assert parse_response(bad) == []

    def test_unmatched_lines_ignored(self):
        text = "garbage line\nvar hq_str_sh600000=1;\n" + bulk_body(["sz000001"])
        quotes = parse_response(text)
        assert [q.code for q in quotes] == ["000001"]

    def test_response_without_trailing_semicolon_supported(self):
        """契约正则 ``;?`` 允许无分号。"""
        fields = ["测试"] + ["0"] * 32
        fields[1], fields[2], fields[3] = "9.90", "9.80", "10.00"
        fields[30], fields[31] = "2026-09-14", "15:00:00"
        text = f'var hq_str_sh600000="{",".join(fields)}"'
        quotes = parse_response(text)
        assert len(quotes) == 1 and quotes[0].price == pytest.approx(10.0)

    def test_multiple_lines_all_parsed(self):
        quotes = parse_response(bulk_body(["sh600000", "sz000001", "bj430047"]))
        assert [q.code for q in quotes] == ["600000", "000001", "430047"]
        assert quotes[2].board is Board.BJ

    def test_zero_prev_close_row_skipped(self):
        """昨收为 0（新股首日/停牌）-> 跳过，避免 pct 除零产生噪声。"""
        fields = ["新股"] + ["0"] * 32
        fields[1], fields[2], fields[3] = "10.0", "0.000", "12.0"
        fields[30], fields[31] = "2026-09-14", "15:00:00"
        text = f'var hq_str_sh600000="{",".join(fields)}";'
        assert parse_response(text) == []

    def test_zero_price_row_skipped(self):
        fields = ["停牌"] + ["0"] * 32
        fields[1], fields[2], fields[3] = "9.9", "9.8", "0.000"
        fields[30], fields[31] = "2026-09-14", "15:00:00"
        text = f'var hq_str_sh600000="{",".join(fields)}";'
        assert parse_response(text) == []

    def test_bad_date_time_yields_none_ts(self):
        fields = ["测试"] + ["0"] * 32
        fields[1], fields[2], fields[3] = "9.9", "9.8", "10.0"
        fields[30], fields[31] = "not-a-date", "25:99:99"
        text = f'var hq_str_sh600000="{",".join(fields)}";'
        q = parse_response(text)[0]
        assert q.ts is None

    def test_regex_captures_prefixed_key_only(self):
        """正则要求 ``[a-z]{2}\\d{6}`` —— 大写/短代码都不匹配。"""
        assert LINE_RE.search('var hq_str_sh600000="x";').group(1) == "sh600000"
        assert LINE_RE.search('var hq_str_SH600000="x";') is None
        assert LINE_RE.search('var hq_str_sh60000="x";') is None

    def test_low_price_fallback_when_zero(self):
        """高/低为 0 时用现价/今开兜底，避免 amplitude 被 0 污染。"""
        fields = ["测试"] + ["0"] * 32
        fields[1], fields[2], fields[3] = "9.9", "9.8", "10.0"
        fields[4], fields[5], fields[6], fields[7] = "0.000", "0.000", "0.000", "0.000"
        fields[30], fields[31] = "2026-09-14", "15:00:00"
        q = parse_response(f'var hq_str_sh600000="{",".join(fields)}";')[0]
        # high 缺失 -> max(price, open) = 10.0 ；low 缺失 -> min(price, open) = 9.9
        assert q.high == pytest.approx(10.0)
        assert q.low == pytest.approx(9.9)
        assert q.bid1 == pytest.approx(10.0) and q.ask1 == pytest.approx(10.0)
        assert q.amplitude == pytest.approx((10.0 - 9.9) / 9.8 * 100.0)


# ==========================================================================
# 3. snapshots() 分批 / 前缀
# ==========================================================================
class TestSnapshots:
    def test_uses_correct_prefix_per_market(self):
        f = FakeFetcher(lambda u, h, t: bulk_body(f.codes_of(u)))
        SinaSource(SINA_CFG, fetcher=f).snapshots(["600000", "000001", "300750", "688111", "430047"])
        assert f.codes_of(f.urls[0]) == ["sh600000", "sz000001", "sz300750", "sh688111", "bj430047"]

    def test_single_request_under_chunk_size(self):
        f = FakeFetcher(lambda u, h, t: bulk_body(f.codes_of(u)))
        quotes = SinaSource(SINA_CFG, fetcher=f).snapshots([f"{600000 + i}" for i in range(100)])
        assert len(f.urls) == 1
        assert len(quotes) == 100

    def test_chunked_into_multiple_requests(self):
        """bulk_chunk=600 -> 1300 码分 3 批。"""
        f = FakeFetcher(lambda u, h, t: bulk_body(f.codes_of(u)))
        src = SinaSource({**SINA_CFG, "bulk_chunk": 600}, fetcher=f)
        quotes = src.snapshots([f"{600000 + i}" for i in range(1300)])
        assert len(f.urls) == 3
        # 并发抓取 -> 请求到达顺序不确定，必须按大小排序后比较
        assert sorted(len(f.codes_of(u)) for u in f.urls) == [100, 600, 600]
        assert len(quotes) == 1300
        assert len({q.code for q in quotes}) == 1300            # 无重复/无丢失

    def test_default_bulk_chunk_is_600(self):
        """实测 800 可用，但契约要求保守取 600。"""
        assert SinaSource({}, fetcher=lambda u, h, t: b"").bulk_chunk == 600

    def test_empty_input_makes_no_request(self):
        f = FakeFetcher(lambda u, h, t: bulk_body(f.codes_of(u)))
        assert SinaSource(SINA_CFG, fetcher=f).snapshots([]) == []
        assert f.calls == []

    def test_fixture_round_trip_through_source(self):
        """真实样本走完整 snapshots() 路径。"""
        f = FakeFetcher(lambda u, h, t: fixture_gbk())
        quotes = SinaSource(SINA_CFG, fetcher=f).snapshots(["600000", "000001", "300750", "688111", "430047"])
        assert [q.code for q in quotes] == ["600000", "000001", "300750", "688111"]
        pufa = quotes[0]
        assert pufa.name == "浦发银行"
        assert pufa.price == pytest.approx(9.40)
        assert pufa.prev_close == pytest.approx(9.26)
        assert pufa.open == pytest.approx(9.28)
        assert pufa.volume_lots == pytest.approx(77187101 / 100.0)

    def test_requested_but_missing_codes_are_dropped(self):
        """请求了停牌股，返回里没有它不是错误（只是需要调用方按 code 取）。"""
        f = FakeFetcher(lambda u, h, t: fixture_gbk())
        quotes = SinaSource(SINA_CFG, fetcher=f).snapshots(["600000", "430047"])
        assert [q.code for q in quotes] == ["600000"]

    def test_only_requested_codes_are_returned(self):
        """服务端回带了未请求的代码时必须过滤掉，避免调用方被意外覆盖。"""
        f = FakeFetcher(lambda u, h, t: fixture_gbk())
        quotes = SinaSource(SINA_CFG, fetcher=f).snapshots(["600000", "300750"])
        assert [q.code for q in quotes] == ["600000", "300750"]
        assert "000001" not in {q.code for q in quotes}

    def test_dedupes_codes_and_removes_non_numeric(self):
        f = FakeFetcher(lambda u, h, t: bulk_body(f.codes_of(u)))
        SinaSource(SINA_CFG, fetcher=f).snapshots(["600000", "600000", "sh600000", "bad", "", "000001"])
        assert f.codes_of(f.urls[0]) == ["sh600000", "sz000001"]

    def test_seq_increments_across_calls(self):
        f = FakeFetcher(lambda u, h, t: bulk_body(f.codes_of(u)))
        src = SinaSource(SINA_CFG, fetcher=f)
        assert src.snapshots(["600000"])[0].seq == 1
        assert src.snapshots(["600000"])[0].seq == 2

    def test_batch_failure_raises_source_error(self):
        f = FakeFetcher(lambda u, h, t: RuntimeError("403 forbidden"))
        src = SinaSource({**SINA_CFG, "retries": 1}, fetcher=f)
        with pytest.raises(SourceError):
            src.snapshots([f"{600000 + i}" for i in range(700)])
        assert src.health()["ok"] is False

    def test_partial_batch_failure_raises(self):
        """有批次失败就抛错 —— 目标是「这批代码」，缺批不能算成功。"""
        def responder(url, headers, timeout):
            if "600600" in url:
                raise RuntimeError("boom")
            return bulk_body(f.codes_of(url))

        f = FakeFetcher(responder)
        src = SinaSource({**SINA_CFG, "retries": 1, "bulk_chunk": 600}, fetcher=f)
        with pytest.raises(SourceError):
            src.snapshots([f"{600000 + i}" for i in range(1300)])

    def test_universe_pages_until_empty(self):
        """新浪行情中心可列全市场：翻页拉取，空页即停。

        （历史背景：这里原本断言 universe() 抛 SourceError，理由是"新浪不能枚举
        全市场"。后来发现 hq.sinajs.cn 只支持按码查询，但**行情中心**另有
        Market_Center.getHQNodeData 列表接口，实测能列出 5563 只 —— 于是它从
        "不支持" 变成了东财被限流时的全市场兜底。）
        """
        pages = {
            1: json.dumps([{"code": "600000", "name": "浦发银行", "trade": "9.28",
                            "settlement": "9.26", "volume": 100000}], ensure_ascii=False),
            2: json.dumps([{"code": "000001", "name": "平安银行", "trade": "11.70",
                            "settlement": "11.82", "volume": 200000}], ensure_ascii=False),
            3: "[]",
        }

        def fetch(url, headers, timeout):
            m = re.search(r"page=(\d+)", url)
            return pages.get(int(m.group(1)), "[]").encode("utf-8")

        src = SinaSource(SINA_CFG, fetcher=FakeFetcher(fetch))
        got = src.universe()
        assert [q.code for q in got] == ["600000", "000001"]
        assert got[0].name == "浦发银行"
        assert got[0].volume_lots == 1000.0          # 股 -> 手
        assert got[0].board.value == "main"

    def test_universe_first_page_failure_raises(self):
        """首页就失败 -> 抛 SourceError（引擎据此换下一个来源）。"""
        src = SinaSource({**SINA_CFG, "retries": 1},
                         fetcher=FakeFetcher(lambda u, h, t: b""))
        with pytest.raises(SourceError):
            src.universe()

    def test_universe_midway_failure_keeps_partial(self):
        """中途某页失败 -> 保留已拿到的部分，别让整轮股票池刷新报废。"""
        def fetch(url, headers, timeout):
            if "page=1" in url:
                return json.dumps([{"code": "600000", "name": "浦发银行",
                                    "trade": "9.28", "settlement": "9.26"}]).encode()
            raise RuntimeError("network down")

        src = SinaSource({**SINA_CFG, "retries": 1}, fetcher=FakeFetcher(fetch))
        got = src.universe()
        assert [q.code for q in got] == ["600000"]
        assert src.stats()["pages_failed"] >= 1

    def test_universe_skips_indices_and_dedupes(self):
        """指数不进股票池；重复代码只留一次。"""
        body = json.dumps([
            {"code": "600000", "name": "浦发银行", "trade": "9.28", "settlement": "9.26"},
            {"code": "600000", "name": "浦发银行", "trade": "9.28", "settlement": "9.26"},
            {"code": "000001", "name": "上证指数", "trade": "3891.6", "settlement": "3864.2"},
            {"code": "abc", "name": "垃圾", "trade": "1", "settlement": "1"},
        ]).encode()

        def fetch(url, headers, timeout):
            return body if "page=1" in url else b"[]"

        src = SinaSource(SINA_CFG, fetcher=FakeFetcher(fetch))
        got = src.universe()
        assert [q.code for q in got] == ["600000"], [q.code for q in got]

    def test_parse_universe_tolerates_garbage(self):
        """列表接口返回非 JSON / null / 空 -> 空列表，绝不抛异常。"""
        from arad.sources.sina import parse_universe

        for bad in (b"", b"null", b"[]", b"<html>502</html>", b"{", b'{"a":1}'):
            assert parse_universe(bad) == [], bad


# ==========================================================================
# 4. headers / 重试 / health
# ==========================================================================
class TestHeadersRetriesHealth:
    def test_referer_is_sent(self):
        """契约 2.3：**必须带 Referer**，否则新浪 403。"""
        f = FakeFetcher(lambda u, h, t: bulk_body(f.codes_of(u)))
        SinaSource(SINA_CFG, fetcher=f).snapshots(["600000"])
        assert f.headers, "fetcher 必须收到 headers"
        h = f.headers[0]
        assert h["Referer"] == REFERER == "https://finance.sina.com.cn"

    def test_all_headers_correct_on_every_request(self):
        f = FakeFetcher(lambda u, h, t: bulk_body(f.codes_of(u)))
        SinaSource({**SINA_CFG, "bulk_chunk": 10}, fetcher=f).snapshots([f"{600000 + i}" for i in range(30)])
        assert len(f.headers) == 3
        for h in f.headers:
            assert h["Referer"] == "https://finance.sina.com.cn"
            assert "Mozilla/5.0" in h["User-Agent"]
            assert h["Accept"] == "*/*"
            assert h["Accept-Language"].startswith("zh-CN")
        assert all(t == 10 for t in f.timeouts)

    def test_referer_configurable(self):
        f = FakeFetcher(lambda u, h, t: bulk_body(f.codes_of(u)))
        SinaSource({**SINA_CFG, "referer": "https://example.test/"}, fetcher=f).snapshots(["600000"])
        assert f.headers[0]["Referer"] == "https://example.test/"

    def test_url_shape(self):
        f = FakeFetcher(lambda u, h, t: bulk_body(f.codes_of(u)))
        SinaSource(SINA_CFG, fetcher=f).snapshots(["600000", "000001"])
        url = f.urls[0]
        assert url.startswith(QUOTE_URL)
        assert url == "https://hq.sinajs.cn/list=sh600000,sz000001"
        assert urllib.parse.urlparse(url).path == "/list=sh600000,sz000001"

    def test_retries_then_succeeds(self):
        state = {"n": 0}

        def responder(url, headers, timeout):
            state["n"] += 1
            if state["n"] < 3:
                raise RuntimeError("temporary")
            return bulk_body(f.codes_of(url))

        f = FakeFetcher(responder)
        src = SinaSource({**SINA_CFG, "retries": 3}, fetcher=f)
        quotes = src.snapshots(["600000"])
        assert [q.code for q in quotes] == ["600000"]
        assert state["n"] == 3
        assert src.stats()["retries"] == 2
        assert src.health()["ok"] is True

    def test_exhausted_retries_raise_source_error(self):
        f = FakeFetcher(lambda u, h, t: RuntimeError("permanent"))
        src = SinaSource({**SINA_CFG, "retries": 3}, fetcher=f)
        with pytest.raises(SourceError) as ei:
            src.snapshots(["600000"])
        assert "sina" in str(ei.value)
        assert len(f.calls) == 3
        h = src.health()
        assert h["ok"] is False and "permanent" in h["err"]

    def test_backoff_schedule_is_0_3_0_9_2_0(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
        f = FakeFetcher(lambda u, h, t: RuntimeError("down"))
        src = SinaSource({**SINA_CFG, "retries": 4}, fetcher=f)
        with pytest.raises(SourceError):
            src.snapshots(["600000"])
        assert slept == [0.3, 0.9, 2.0]

    def test_empty_body_is_failure(self):
        f = FakeFetcher(lambda u, h, t: b"")
        src = SinaSource({**SINA_CFG, "retries": 2}, fetcher=f)
        with pytest.raises(SourceError):
            src.snapshots(["600000"])

    def test_health_shape(self):
        f = FakeFetcher(lambda u, h, t: bulk_body(f.codes_of(u)))
        src = SinaSource(SINA_CFG, fetcher=f)
        h = src.health()
        assert set(h) == {"name", "ok", "latency_ms", "err"}
        assert h["name"] == "sina"
        assert h["ok"] is True and h["err"] == ""
        assert isinstance(h["latency_ms"], int)
        src.snapshots(["600000"])
        assert src.stats()["calls"] == 1
        assert src.stats()["quotes"] == 1

    def test_satisfies_source_protocol(self):
        src = SinaSource(SINA_CFG, fetcher=FakeFetcher(lambda u, h, t: b""))
        assert isinstance(src, Source)
        assert src.name == "sina"

    def test_health_thread_safe_under_concurrency(self):
        f = FakeFetcher(lambda u, h, t: bulk_body(f.codes_of(u)))
        src = SinaSource({**SINA_CFG, "bulk_chunk": 10, "workers": 4}, fetcher=f)
        errors: list[Exception] = []

        def worker():
            try:
                src.snapshots([f"{600000 + i}" for i in range(50)])
                src.health()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert src.stats()["calls"] == 4 * 5  # 每轮 50 码 / 每批 10 码

    def test_parse_response_is_pure_and_repeatable(self):
        raw = load_raw("sina_bulk_5.txt")
        a = parse_response(raw)
        b = parse_response(raw)
        assert [q.code for q in a] == [q.code for q in b]
        assert a[0].price == b[0].price
        assert a is not b


# ==========================================================================
# 5. universe() —— 新浪行情中心全市场列表（股票池关键路径）
# ==========================================================================
# 为什么单独一大节：``SinaSource.universe()`` 一挂，引擎就只能退化成「只盯自选股
# 那 10 只」（engine.py 的「股票池为空，降级为仅监控自选股」分支），盘中等于失效。
# 下面每条断言都对应一个会让股票池**静默变小**的坑。
#
# 与 hq.sinajs.cn 的三点差异（实现已处理，测试必须钉住）：
#   1. 线路是 **UTF-8 JSON**，不是 GBK（hq.sinajs.cn 才是 GBK）；
#   2. 只有 code/name/trade/settlement/open/high/low/volume/amount，没有五档 /
#      内外盘 / 市值 / 涨跌停价 -> 一律 0；唯一用途是拿代码清单；
#   3. 停牌行（``trade=0``）**保留**（行情接口那边是丢弃），代码必须留在轮询池，
#      否则一只票停牌一天就从监控里消失，复牌当天什么信号都收不到。
#
# ⚠ 脚手架提示：``FakeFetcher`` 把 str 响应按 **GBK** 编码（对 hq.sinajs.cn 是对的），
# 而列表接口是 UTF-8 —— 本节的 fetcher 一律直接返回 bytes，免得中文名被编坏、
# 断言「过了」却验错了编码假设。
#
# 已覆盖、本节不重复的用例（见 ``TestSnapshots``）：
#   * 翻到空页即停 + 首行字段/单位（``test_universe_pages_until_empty``）
#   * 首页失败抛 ``SourceError``（``test_universe_first_page_failure_raises``）
#   * 中途失败保留部分结果（``test_universe_midway_failure_keeps_partial``）
#   * 指数被剔除（``test_universe_skips_indices_and_dedupes``）
#   * 非 JSON / ``null`` / HTML / 截断 JSON（``test_parse_universe_tolerates_garbage``）
# 本节是**加固**：页边界、``max_pages`` 上限、跨页/页内去重、字段默认值、
# settlement 兜底、stats 计数、URL 形状。
import math  # noqa: E402  —— 追加小节，紧跟本节使用

from arad.sources.sina import (  # noqa: E402
    UNIVERSE_MAX_PAGES,
    UNIVERSE_NODE,
    UNIVERSE_PAGE_SIZE,
    UNIVERSE_URL,
    parse_universe,
)

UNIVERSE_CFG = {**SINA_CFG, "retries": 1}     # retries=1：失败用例不必等退避


def uni_json(rows) -> bytes:
    """列表接口真实线路的编码：**UTF-8 JSON**（不是 GBK）。"""
    return json.dumps(rows, ensure_ascii=False).encode("utf-8")


def uni_rows(codes, **extra) -> list:
    """造假行的最小可用集合：code/name/trade/settlement（其余字段真实接口才有）。"""
    return [{"code": c, "name": f"股票{c}", "trade": "10.0", "settlement": "9.9", **extra}
            for c in codes]


def seq_codes(page: int, count: int = UNIVERSE_PAGE_SIZE) -> list[str]:
    """第 ``page`` 页的 ``count`` 个**互不重复**的 6 位代码（模拟"每页全是新票"）。"""
    return [f"{600000 + (page - 1) * count + i}" for i in range(count)]


def page_of(url: str) -> int:
    return int(re.search(r"page=(\d+)", url).group(1))


def pages_of(fetcher: FakeFetcher) -> list[int]:
    """实际请求过的页号序列 —— 断言翻页行为的**权威依据**。

    （不能靠"让第 N+1 页抛异常"来证明没翻到第 N+1 页：fetcher 抛的异常会被
    ``_request`` 吞成 ``SourceError``，最后只表现为「中途失败保留部分结果」，
    测试会因为错误的原因通过。）
    """
    return [page_of(u) for u in fetcher.urls]


def always_new_pages(url, headers, timeout) -> bytes:
    """每页都返回 100 个全新代码：不设上限就会一直翻下去（模拟"永不回绕"）。"""
    return uni_json(uni_rows(seq_codes(page_of(url))))


# --------------------------------------------------------------------------
# 5.1 parse_universe —— 字段映射与容错
# --------------------------------------------------------------------------
class TestParseUniverseFields:
    def test_fields_are_mapped_from_the_list_endpoint(self):
        """列表接口的字段名 -> Quote 字段（trade/settlement/open/high/low/volume/amount）。"""
        row = {
            "symbol": "sh600000", "code": "600000", "name": "浦发银行",
            "trade": "9.280", "settlement": "9.260", "open": "9.270",
            "high": "9.400", "low": "9.200", "volume": 150000, "amount": 1395000.0,
        }
        q = parse_universe(uni_json([row]))[0]
        assert q.code == "600000"
        assert q.name == "浦发银行"
        assert q.board is Board.MAIN
        assert q.price == pytest.approx(9.28)
        assert q.prev_close == pytest.approx(9.26)
        assert q.open == pytest.approx(9.27)
        assert q.high == pytest.approx(9.40)
        assert q.low == pytest.approx(9.20)
        assert q.amount == pytest.approx(1395000.0)
        assert q.pct == pytest.approx(0.2159, abs=1e-3)
        # 涨跌停价由 Quote 按板块费率推算（列表接口不提供）
        assert q.limit_up_price == pytest.approx(round(9.26 * 1.10, 2)) == pytest.approx(10.19)
        assert q.limit_down_price == pytest.approx(round(9.26 * 0.90, 2))

    def test_volume_is_shares_converted_to_lots(self):
        """列表接口的 volume 是**股**，和行情接口 f8 同口径 -> 一律 /100 转手。"""
        q = parse_universe(uni_json([{"code": "600000", "trade": "9.28",
                                      "settlement": "9.26", "volume": 150000}]))[0]
        assert q.volume_lots == pytest.approx(1500.0)
        assert q.volume_lots == pytest.approx(150000 / 100.0)
        # 字符串 / 浮点 / 超大值同样按股处理
        for raw_vol in ("150000", 1.5e5, 150000.0):
            q2 = parse_universe(uni_json([{"code": "600000", "trade": "9.28",
                                           "settlement": "9.26", "volume": raw_vol}]))[0]
            assert q2.volume_lots == pytest.approx(1500.0), raw_vol
        big = parse_universe(uni_json([{"code": "600000", "trade": "9.28",
                                        "settlement": "9.26", "volume": 100000000}]))[0]
        assert big.volume_lots == pytest.approx(1_000_000.0)   # 1 亿股 -> 100 万手
        # 缺省 / null -> 0 手（列表接口偶尔给 null）
        for raw_vol in (None, "", "-", "null", "abc"):
            q3 = parse_universe(uni_json([{"code": "600000", "trade": "9.28",
                                           "settlement": "9.26", "volume": raw_vol}]))[0]
            assert q3.volume_lots == 0.0, raw_vol
        # 与行情接口交叉验证：同样 150000 股，两个解析器必须给出同样的手数
        fields = ["测试"] + ["0"] * 32
        fields[1], fields[2], fields[3], fields[8] = "9.90", "9.26", "9.28", "150000"
        fields[30], fields[31] = "2026-09-14", "15:00:00"
        bulk = parse_response(f'var hq_str_sh600000="{",".join(fields)}";')[0]
        assert bulk.volume_lots == pytest.approx(q.volume_lots)

    def test_order_book_and_cap_fields_are_all_zero(self):
        """列表接口没有五档/内外盘/市值/涨跌停价/换手率/量比 -> 全 0、has_depth False。

        这里**故意**用一条"字段很全"的真实形状的行（含 mktcap/nmc/turnoverratio/
        ticktime 等列表接口特有的字段），验证它们不会被误当成分市值/换手率灌进来。
        """
        row = {
            "symbol": "sh600000", "code": "600000", "name": "浦发银行",
            "trade": "9.280", "settlement": "9.260", "open": "9.270",
            "high": "9.400", "low": "9.200", "volume": 150000, "amount": 1395000.0,
            # 列表接口会带这些，但契约里的 *_cap 是「亿元」、turnover 是「%」，
            # 单位/口径都对不上 -> 一律不许填
            "mktcap": 1.0e12, "nmc": 9.0e11, "turnoverratio": 3.14,
            "pb": 0.52, "per": 4.2, "ticktime": "15:00:00", "pricechange": "0.020",
        }
        q = parse_universe(uni_json([row]))[0]
        assert q.turnover == 0.0
        assert q.volume_ratio == 0.0
        assert q.float_cap == 0.0 and q.total_cap == 0.0
        assert q.limit_up == 0.0 and q.limit_down == 0.0
        assert q.bid1 == 0.0 and q.ask1 == 0.0
        assert q.bid_vol == 0.0 and q.ask_vol == 0.0
        assert q.outer_vol == 0.0 and q.inner_vol == 0.0
        assert q.bid_prices == () and q.ask_prices == ()
        assert q.bid_vols == () and q.ask_vols == ()
        assert q.has_depth is False
        assert q.bid_total_vol == 0.0 and q.ask_total_vol == 0.0
        assert q.float_shares == 0.0
        # 列表接口也不给上市日期（新股过滤在 engine 侧遇到空串会放行，不误杀）
        assert q.list_date == ""

    def test_stopped_row_is_kept(self):
        """``trade=0``（停牌/停板无成交）的行**必须保留** —— 代码要留在轮询池。

        丢了它 => 停牌一天就从监控里消失，复牌当天收不到任何信号。
        """
        rows = [
            {"code": "600000", "name": "停牌股", "trade": 0, "settlement": "9.26", "open": "9.30"},
            {"code": "000001", "name": "正常股", "trade": "11.70", "settlement": "11.82"},
        ]
        got = parse_universe(uni_json(rows))
        assert [q.code for q in got] == ["600000", "000001"]      # 一条都不能少
        stopped = got[0]
        assert stopped.price == 0.0
        assert stopped.prev_close == pytest.approx(9.26)          # 昨收仍在
        assert stopped.is_suspended is True                       # 规则侧据此不告警
        assert stopped.board is Board.MAIN

        # 走一遍完整的 universe() 翻页路径，确认停牌股不在翻页途中被滤掉
        f = FakeFetcher(lambda u, h, t: uni_json(rows) if page_of(u) == 1 else b"[]")
        codes = [q.code for q in SinaSource(UNIVERSE_CFG, fetcher=f).universe()]
        assert codes == ["600000", "000001"]

    def test_stopped_row_kept_here_but_dropped_by_bulk_parser(self):
        """两个接口的取舍**故意相反**：列表=枚举代码（停牌也留），行情=要真价格（丢弃）。"""
        row = {"code": "600000", "name": "停牌股", "trade": 0, "settlement": "9.26", "open": "9.30"}
        assert [q.code for q in parse_universe(uni_json([row]))] == ["600000"]
        # 行情接口：同一只票全 0 -> 整行跳过（契约 2.3 / notes 3.5）
        fields = ["停牌股"] + ["0"] * 32
        fields[30], fields[31] = "2026-09-14", "15:00:00"
        assert parse_response(f'var hq_str_sh600000="{",".join(fields)}";') == []

    def test_settlement_zero_or_missing_falls_back_to_positive(self):
        """``settlement`` 为 0 / 缺失 -> ``prev_close`` 兜底，**必须 > 0**。

        这是真实正确性陷阱：``prev_close <= 0`` 的 Quote 会被
        ``spirit_price.evaluate`` 的 ``q.is_suspended`` / ``q.prev_close <= 0``
        直接 continue 掉（见 src/arad/rules/spirit_price.py:135），也就是这只票
        当轮**什么都报不出来**；同时 ``limit_up_price`` 退化成 0.0，
        一字板/炸板判定跟着一起失效。下面把这两个后果都钉住。
        """
        rows = [
            # settlement=0 -> 用 open 兜底，涨跌幅仍是真实可比的数
            {"code": "600000", "name": "A", "trade": "9.28", "settlement": "0", "open": "9.30",
             "volume": 150000},
            # settlement 缺失 -> 用 open 兜底
            {"code": "600001", "name": "B", "trade": "9.28", "open": "9.30"},
            # settlement + open 都没有 -> 退回现价（涨跌幅 0，但绝不是 inf）
            {"code": "600002", "name": "C", "trade": "9.28"},
            {"code": "600003", "name": "D", "trade": "9.28", "settlement": None, "open": None},
            # 显式 0 字符串 / 全 0 行
            {"code": "600004", "name": "E", "trade": "9.28", "settlement": "0.000", "open": "0.000"},
            {"code": "600005", "name": "F", "trade": 0, "settlement": 0},
        ]
        got = {q.code: q for q in parse_universe(uni_json(rows))}
        assert got["600000"].prev_close == pytest.approx(9.30)      # 0 -> open
        assert got["600000"].pct == pytest.approx(-0.2151, abs=1e-3)
        assert got["600000"].is_suspended is False                  # -> 不会被规则 continue 掉
        assert got["600000"].limit_up_price == pytest.approx(round(9.30 * 1.10, 2))
        assert got["600000"].limit_up_price > 0                     # 不是 0.0（涨停判定不失效）
        assert got["600001"].prev_close == pytest.approx(9.30)
        assert got["600002"].prev_close == pytest.approx(9.28)      # 退回现价
        assert got["600002"].pct == pytest.approx(0.0)
        assert got["600003"].prev_close == pytest.approx(9.28)
        assert got["600004"].prev_close == pytest.approx(9.28)      # open 也是 0 -> 再退现价
        # 兜底必须覆盖"数据源给了价格"的每一行：prev_close > 0
        assert all(got[c].prev_close > 0 for c in got if c != "600005")
        # 退无可退（trade/settlement/open 全 0）时 prev_close 只能是 0，
        # 但 pct 仍不得是 inf/nan，且该行会被 is_suspended 正确拦在规则之外
        assert got["600005"].prev_close == 0.0
        assert got["600005"].pct == 0.0
        assert got["600005"].is_suspended is True
        for q in got.values():
            assert math.isfinite(q.pct), q.code

    def test_b_share_codes_keep_correct_prefix_and_board(self):
        """900xxx(沪B) / 200xxx(深B)：既不能被丢弃，也不能落进北交所。

        ``guess_prefix`` 把 900/200 单独判成 sh/sz（不能按 9/2 开头推成 bj），
        否则下一轮 ``snapshots()`` 会拿 ``bj900901`` 去问行情接口，永远拿不到数据。
        """
        from arad.models import guess_prefix

        rows = [
            {"symbol": "sh900901", "code": "900901", "name": "云赛B股",
             "trade": "1.514", "settlement": "1.500", "volume": 100000},
            {"symbol": "sz200011", "code": "200011", "name": "深物业B",
             "trade": "5.030", "settlement": "5.000", "volume": 200000},
        ]
        got = parse_universe(uni_json(rows))
        assert [q.code for q in got] == ["900901", "200011"]
        assert all(q.board is not Board.BJ for q in got)
        assert guess_prefix("900901") == "sh" and guess_prefix("200011") == "sz"
        assert got[0].price == pytest.approx(1.514)
        assert got[0].volume_lots == pytest.approx(1000.0)
        # 涨跌停按 OTHER(±10%) 推算，而不是 BJ 的 ±30%：
        # 1.500*1.30 = 1.95（错），1.500*1.10 = 1.65（对）
        assert got[0].limit_up_price == pytest.approx(round(1.500 * 1.10, 2)) == pytest.approx(1.65)
        assert got[1].limit_up_price == pytest.approx(round(5.000 * 1.10, 2))
        # 前缀正确 -> bulk 轮询才拿得到这两只（下一步 snapshots 的入参）
        assert [f"{guess_prefix(q.code)}{q.code}" for q in got] == ["sh900901", "sz200011"]

    def test_decimal_and_full_width_codes_are_recorded_as_known_looseness(self):
        """已知宽松点（**未改实现**，只记录）：``str.isdigit()`` 对全角数字也为真。

        ``"６０００００"``（全角）能通过 ``len==6 and isdigit()`` 的校验进股票池，
        板块判成 OTHER、下游按代码取行情永远取不到。真实接口只回 ASCII 数字，
        存量风险可忽略；要收紧得改 sina.py 的 code 校验（本次任务不允许）。
        """
        rows = [{"code": "６０００００", "name": "全角", "trade": "9.28", "settlement": "9.26"}]
        got = parse_universe(uni_json(rows))
        assert [q.code for q in got] == ["６０００００"]
        assert got[0].board is Board.OTHER        # 不是 MAIN —— 确实没被正确识别

    def test_index_rows_are_dropped(self):
        """资金池里混进指数会白占配额（``exclude_boards=["index"]`` 只拦告警不省配额）。"""
        rows = [
            {"code": "000001", "name": "上证指数", "trade": "3891.6", "settlement": "3864.2"},
            {"code": "399001", "name": "深证成指", "trade": "13000.0", "settlement": "12900.0"},
            {"code": "399006", "name": "", "trade": "2600.0", "settlement": "2580.0"},  # 399 段无条件
            {"code": "000001", "name": "平安银行", "trade": "11.70", "settlement": "11.82"},
        ]
        got = parse_universe(uni_json(rows))
        # 只有「平安银行」留下：同名 000001 靠名称消歧，指数那两个被剔
        assert [q.code for q in got] == ["000001"]
        assert got[0].name == "平安银行"
        assert got[0].board is Board.MAIN

    def test_code_must_be_a_six_digit_string(self):
        """非法 code 一律丢弃；``600000``（int）按 6 位数字接受（JSON 里常见）。"""
        def one(code):
            return parse_universe(uni_json([{"code": code, "name": "X",
                                             "trade": "9.28", "settlement": "9.26"}]))

        for bad in (None, 0, True, False, 600000.0, 600000.5, 6.0e5,
                    "sh600000", "600000.0", "0600000", "60000", "6000000", ""):
            assert one(bad) == [], repr(bad)
        assert [q.code for q in one(600000)] == ["600000"]        # int 容忍（str() 后是 6 位数字）
        assert [q.code for q in one("600000")] == ["600000"]

    def test_symbol_column_is_ignored_when_code_is_present(self):
        """``symbol``/``code`` 冲突时以 ``code`` 为准（板块按 code 判，不信前缀）。"""
        got = parse_universe(uni_json([
            {"symbol": "sz300750", "code": "600000", "name": "浦发银行",
             "trade": "9.28", "settlement": "9.26"},
        ]))
        assert [(q.code, q.board.value) for q in got] == [("600000", "main")]

    def test_symbol_only_row_is_resolved_instead_of_dropped(self):
        """真实响应 code/symbol 两列都给，本用例守的是"只靠 symbol 也能识别"这条退路。

        修复前这里是 ``xfail(strict=True)``：只带 symbol 的行会被整行丢弃，
        universe() 第 1 页就"解析为空"-> 直接 break -> 股票池变空 -> 引擎静默
        降级成只盯自选股那 10 只。这是"看着一切正常但永不告警"的最坏故障形态，
        所以 code 缺失时必须退回 symbol[2:]。
        """
        got = parse_universe(uni_json(
            [{"symbol": "sh600000", "name": "浦发银行", "trade": "9.28", "settlement": "9.26"}]))
        assert [q.code for q in got] == ["600000"]

    def test_symbol_only_row_keeps_prefixed_form_working(self):
        """symbol 兜底要能处理带/不带前缀两种写法，且无前缀时原样使用。"""
        got = parse_universe(uni_json([
            {"symbol": "sz300750", "name": "宁德时代", "trade": "180.0", "settlement": "178.0"},
            {"symbol": "600519", "name": "贵州茅台", "trade": "1500.0", "settlement": "1490.0"},
            {"symbol": "xx", "name": "垃圾", "trade": "1", "settlement": "1"},
        ]))
        assert [q.code for q in got] == ["300750", "600519"], [q.code for q in got]
        assert got[0].board.value == "gem"
        assert got[1].board.value == "main"

    def test_non_dict_rows_are_skipped(self):
        """列表里混进字符串/数字/数组/null 时只跳过它们，不整页报废。"""
        payload = json.dumps(
            ["600000", None, 42, [], True, {"code": "000001", "name": "平安银行",
                                            "trade": "11.70", "settlement": "11.82"}],
            ensure_ascii=False,
        ).encode("utf-8")
        assert [q.code for q in parse_universe(payload)] == ["000001"]

    def test_tolerates_more_garbage_than_the_existing_case(self):
        """在 ``test_parse_universe_tolerates_garbage`` 之外补齐：类型/空白/BOM/坏字节。"""
        # 非 list 的合法 JSON
        for bad in (b"{}", b'"600000"', b"123", b"true", b"[", b'[{"code": "600000"',
                    b"<html><body>502 Bad Gateway</body></html>", b"<!DOCTYPE html>",
                    b"[]", b"[ ]", b"null", b"NULL", b"NaN", b"\xff\xfe\xff", b"\x00\x01\x02"):
            assert parse_universe(bad) == [], bad
        # 非 bytes/str 输入（防御性：解析器绝不抛异常）
        for weird in (None, 123, 4.5, True, ["600000"], {"code": "600000"}):
            assert parse_universe(weird) == [], repr(weird)
        # 空列表 / 空白 / 只剩 BOM
        assert parse_universe(b"   \n\t ") == []
        assert parse_universe(b"\xef\xbb\xbf") == []
        assert parse_universe("[]") == []
        # JSON 里的 null 值字段不影响其它行
        assert [q.code for q in parse_universe(uni_json(
            [{"code": "600000", "name": None, "trade": None, "settlement": None,
              "open": None, "high": None, "low": None, "volume": None, "amount": None}]))] == ["600000"]
        # 未知字段 / 嵌套结构不参与解析
        assert [q.code for q in parse_universe(uni_json(
            [{"code": "600000", "trade": "9.28", "settlement": "9.26",
              "extra": {"五档": [1, 2, 3]}, "ticktime": "15:00:00"}]))] == ["600000"]

    def test_accepts_str_json_and_utf8_bytes(self):
        """调用方给 ``str``（已解码）或 ``bytes``（UTF-8）都要能解。"""
        rows = [{"code": "600000", "name": "浦发银行", "trade": "9.28", "settlement": "9.26"}]
        text = json.dumps(rows, ensure_ascii=False)
        assert parse_universe(text)[0].name == "浦发银行"
        assert parse_universe(text.encode("utf-8"))[0].name == "浦发银行"
        # 真实响应就是 UTF-8 字节（这里的 bytes 路径 == 生产路径）
        assert parse_universe(uni_json(rows))[0].name == "浦发银行"
        assert b"\xe6\xb5\xa6\xe5\x8f\x91\xe9\x93\xb6\xe8\xa1\x8c" in uni_json(rows)

    def test_seq_is_propagated(self):
        got = parse_universe(uni_json(uni_rows(["600000", "000001"])), seq=7)
        assert [q.seq for q in got] == [7, 7]
        # universe() 用自己的自增 seq，同一轮里所有页共用一个值
        f = FakeFetcher(lambda u, h, t: (
            uni_json(uni_rows(seq_codes(page_of(u)))) if page_of(u) <= 2 else b"[]"))
        src = SinaSource(UNIVERSE_CFG, fetcher=f)
        first = src.universe()
        second = src.universe()
        assert {q.seq for q in first} == {1} and len(first) == 200
        assert {q.seq for q in second} == {2} and len(second) == 200


# --------------------------------------------------------------------------
# 5.2 universe() —— 翻页边界 / 去重 / 上限
# --------------------------------------------------------------------------
class TestUniversePaging:
    def test_exact_page_size_then_empty_page_stops(self):
        """页边界：第 1 页正好 100 行（= num），第 2 页空 -> 干净收尾，不翻第 3 页。"""
        f = FakeFetcher(lambda u, h, t: (
            uni_json(uni_rows(seq_codes(1))) if page_of(u) == 1
            else b"[]" if page_of(u) == 2
            else uni_json(uni_rows(seq_codes(99)))))     # 真翻到第 3 页会多出 100 个新码
        got = SinaSource(UNIVERSE_CFG, fetcher=f).universe()
        assert UNIVERSE_PAGE_SIZE == 100
        assert len(got) == UNIVERSE_PAGE_SIZE
        assert pages_of(f) == [1, 2]                     # 第 3 页从未被请求
        assert len({q.code for q in got}) == 100

    def test_max_pages_argument_never_requests_beyond_it(self):
        """``max_pages=2`` -> 第 3 页绝不能出现（数据源永不回绕时的硬边界）。"""
        f = FakeFetcher(always_new_pages)
        got = SinaSource(UNIVERSE_CFG, fetcher=f).universe(max_pages=2)
        assert pages_of(f) == [1, 2]
        assert len(got) == 2 * UNIVERSE_PAGE_SIZE
        assert len({q.code for q in got}) == len(got)

    def test_duplicate_codes_across_pages_are_deduped(self):
        """跨页重复：第 1、2 页都含 600000 -> 只留一条，且第 2 页的**新**代码照样收下。"""
        pages = {
            1: uni_json(uni_rows(["600000", "000001"])),
            2: uni_json(uni_rows(["600000", "300750"])),   # 600000 是重复，300750 是新票
            3: b"[]",
        }
        f = FakeFetcher(lambda u, h, t: pages.get(page_of(u), b"[]"))
        got = SinaSource(UNIVERSE_CFG, fetcher=f).universe()
        codes = [q.code for q in got]
        assert codes == ["600000", "000001", "300750"]
        assert codes.count("600000") == 1
        assert pages_of(f) == [1, 2, 3]

    def test_duplicate_codes_within_one_page_are_deduped(self):
        """**回归用例**：同一页内重复的代码只能留一条。

        旧实现是"先整页过滤、再统一更新 seen"，同一页里的 600000 会一起通过 ——
        节点数据抖动时真的出现过重复行，股票池里就会出现重复代码。
        """
        rows = [
            {"code": "600000", "name": "浦发银行", "trade": "9.28", "settlement": "9.26"},
            {"code": "600000", "name": "浦发银行", "trade": "9.30", "settlement": "9.26"},
            {"code": "000001", "name": "平安银行", "trade": "11.70", "settlement": "11.82"},
            {"code": "600000", "name": "浦发银行", "trade": "9.28", "settlement": "9.26"},
        ]
        f = FakeFetcher(lambda u, h, t: uni_json(rows) if page_of(u) == 1 else b"[]")
        got = SinaSource(UNIVERSE_CFG, fetcher=f).universe()
        codes = [q.code for q in got]
        assert codes.count("600000") == 1, codes
        assert codes == ["600000", "000001"]
        # 重复行谁胜出：**首次出现**的那条（engine.state.universe 会被它覆盖）
        assert got[0].price == pytest.approx(9.28)

    def test_page_made_only_of_duplicates_stops_the_loop(self):
        """整页都是前页的重复 -> 数据源在回绕，立即停（第 3 页有新码也不许再翻）。"""
        pages = {
            1: uni_json(uni_rows(["600000", "000001"])),
            2: uni_json(uni_rows(["600000", "000001"])),    # 全是重复
            3: uni_json(uni_rows(["300750"])),              # 有新码，但不该被请求
        }
        f = FakeFetcher(lambda u, h, t: pages.get(page_of(u), b"[]"))
        got = SinaSource(UNIVERSE_CFG, fetcher=f).universe()
        assert [q.code for q in got] == ["600000", "000001"]
        assert pages_of(f) == [1, 2]

    def test_first_page_empty_means_empty_universe_without_page_two(self):
        f = FakeFetcher(lambda u, h, t: b"[]")
        src = SinaSource(UNIVERSE_CFG, fetcher=f)
        assert src.universe() == []
        assert pages_of(f) == [1]

    def test_runaway_source_is_bounded_by_config(self):
        """数据源永不回绕（每页都是新码）时，翻页必须被配置上限兜住。

        没有这层上限就是一个真实的失控循环：80 页之后仍在请求，
        每轮股票池刷新都会白打几百次 HTTP。
        """
        f = FakeFetcher(always_new_pages)
        src = SinaSource(UNIVERSE_CFG, fetcher=f)          # 未显式配置 -> 默认
        got = src.universe()
        assert src.universe_max_pages == UNIVERSE_MAX_PAGES == 80
        assert len(f.urls) == UNIVERSE_MAX_PAGES
        assert pages_of(f) == list(range(1, UNIVERSE_MAX_PAGES + 1))
        assert len(got) == UNIVERSE_MAX_PAGES * UNIVERSE_PAGE_SIZE == 8000
        assert len({q.code for q in got}) == 8000
        assert src.stats()["pages_failed"] == 0

    def test_explicit_max_pages_overrides_config_both_ways(self):
        f = FakeFetcher(always_new_pages)
        src = SinaSource({**UNIVERSE_CFG, "universe_max_pages": 1}, fetcher=f)
        got = src.universe(max_pages=3)                    # 显式参数 > 配置
        assert pages_of(f) == [1, 2, 3]
        assert len(got) == 300

    @pytest.mark.parametrize("bad, expected", [
        # 未配置 / 空串 / 非数字 -> 默认 80 页（覆盖全市场 5563 只）
        (None, UNIVERSE_MAX_PAGES), ("", UNIVERSE_MAX_PAGES),
        ("abc", UNIVERSE_MAX_PAGES), (False, UNIVERSE_MAX_PAGES),
        # 0 / 负数 -> 同样退回默认，**而不是**夹到 1 页。
        # 早先的实现写作 `max(1, int(x or 80))`，于是 0 走 `or` 拿 80、
        # 但 -5 被夹成 1 —— 只翻 1 页 = 100 只的股票池，全市场扫描名存实亡，
        # 而且不报错。非正数没有任何合法用途（"不拉股票池"= 系统失效），
        # 所以统一退回能覆盖全市场的安全默认值。
        (0, UNIVERSE_MAX_PAGES), (0.0, UNIVERSE_MAX_PAGES),
        (-5, UNIVERSE_MAX_PAGES), ("0", UNIVERSE_MAX_PAGES),
        # 合法值原样生效
        (3, 3), ("12", 12),
    ])
    def test_universe_max_pages_bad_values_fall_back_to_a_safe_bound(self, bad, expected):
        """坏配置不能变成"不翻页"或"只翻一页"（两者都等于全市场扫描失效）。"""
        f = FakeFetcher(always_new_pages)
        src = SinaSource({**UNIVERSE_CFG, "universe_max_pages": bad}, fetcher=f)
        assert src.universe_max_pages == expected, bad
        got = src.universe()
        assert len(f.urls) == expected                     # 真的按这个页数上限翻
        assert len(got) == expected * UNIVERSE_PAGE_SIZE

    def test_universe_max_pages_non_numeric_config_does_not_crash_ctor(self):
        """坏配置不能拖垮构造，否则兜底源静默消失。

        修复前这里 ``xfail(raises=ValueError)``：``int("abc")`` 抛异常 ->
        ``SinaSource.__init__`` 失败 -> 引擎在 ``_universe_sources()`` 里只打一条
        warning 就跳过新浪 -> 等东财真被限流那天才发现没得退，而那时已经开盘了。
        现在对齐腾讯源的 ``_to_int``：坏值退回默认页数。
        """
        src = SinaSource({**UNIVERSE_CFG, "universe_max_pages": "abc"},
                         fetcher=FakeFetcher(always_new_pages))
        assert src.universe_max_pages == UNIVERSE_MAX_PAGES, "坏值应退回默认页数"


# --------------------------------------------------------------------------
# 5.3 universe() —— URL 形状 / stats / 与 snapshots 的衔接
# --------------------------------------------------------------------------
class TestUniverseUrlStatsAndRoundTrip:
    def test_url_is_the_market_center_list_endpoint(self):
        """URL 形状钉死：page/num/node/sort/asc 任一写错 -> 服务端静默返回别的集合。"""
        f = FakeFetcher(lambda u, h, t: uni_json(uni_rows(["600000"])) if page_of(u) == 1 else b"[]")
        SinaSource(UNIVERSE_CFG, fetcher=f).universe(max_pages=2)
        assert UNIVERSE_URL == (
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeData")
        assert UNIVERSE_NODE == "hs_a" and UNIVERSE_PAGE_SIZE == 100
        assert f.urls[0] == (f"{UNIVERSE_URL}?page=1&num={UNIVERSE_PAGE_SIZE}"
                             f"&sort=symbol&asc=1&node={UNIVERSE_NODE}")
        assert f.urls[1] == f.urls[0].replace("page=1", "page=2")
        # 列表接口与行情接口共用请求头：Referer 必须带上（否则 403）
        assert f.headers[0]["Referer"] == REFERER

    def test_pages_failed_increments_only_on_partial_failure(self):
        """``pages_failed``：中途失败 +1；成功的一轮既不 +1 也不清零。"""
        state = {"fail": True}

        def respond(url, headers, timeout):
            n = page_of(url)
            if n == 1:
                return uni_json(uni_rows(seq_codes(1)))
            if state["fail"]:
                raise RuntimeError("第 2 页网络失败")
            return b"[]"

        src = SinaSource(UNIVERSE_CFG, fetcher=FakeFetcher(respond))
        assert len(src.universe()) == UNIVERSE_PAGE_SIZE      # 保留部分结果
        assert src.stats()["pages_failed"] == 1
        h = src.health()
        assert h["ok"] is False and "2" in h["err"]           # 页号要能定位问题
        assert len(src.universe()) == UNIVERSE_PAGE_SIZE      # 再失败一次
        assert src.stats()["pages_failed"] == 2
        state["fail"] = False
        assert len(src.universe()) == UNIVERSE_PAGE_SIZE      # 恢复正常
        assert src.stats()["pages_failed"] == 2               # 历史计数不清零
        assert src.health()["ok"] is True and src.health()["err"] == ""

    def test_first_page_failure_is_not_a_partial_page_failure(self):
        """一页都没拿到 = 整体失败（抛错交给故障转移），不算"丢了几页"。"""
        src = SinaSource(UNIVERSE_CFG, fetcher=FakeFetcher(lambda u, h, t: b""))
        with pytest.raises(SourceError):
            src.universe()
        s = src.stats()
        assert s["pages_failed"] == 0        # 别把它计成"部分成功"，否则引擎不换源
        assert s["errors"] >= 1              # 失败尝试有记录
        assert src.health()["ok"] is False

    def test_universe_completeness_is_reported_when_it_reaches_the_end(self):
        """**回归 IT-P1-006**：正常翻到底 -> ``complete=True``。

        没有这个标记，引擎无法区分「翻完了的 100 只」和「翻了 1 页就停的
        100 只」，两者都只是"非空"。
        """
        f = FakeFetcher(lambda u, h, t: (
            uni_json(uni_rows(seq_codes(1))) if page_of(u) == 1 else b"[]"))
        src = SinaSource(UNIVERSE_CFG, fetcher=f)
        assert len(src.universe()) == UNIVERSE_PAGE_SIZE
        info = src.universe_info()
        assert info["complete"] is True
        assert info["pages_failed"] == 0
        assert info["returned"] == UNIVERSE_PAGE_SIZE

    def test_universe_is_incomplete_when_a_middle_page_fails(self):
        """**回归 IT-P1-006**：中途某页失败 -> 部分结果必须标成 ``complete=False``。

        这正是"5563 只被 3000 只静默覆盖"的来源：旧实现把它当成功返回。
        """
        def respond(url, headers, timeout):
            n = page_of(url)
            if n == 1:
                return uni_json(uni_rows(seq_codes(1)))
            raise RuntimeError("第 2 页网络失败")

        src = SinaSource(UNIVERSE_CFG, fetcher=FakeFetcher(respond))
        got = src.universe()
        assert len(got) == UNIVERSE_PAGE_SIZE          # 仍然保留部分结果
        info = src.universe_info()
        assert info["complete"] is False, "部分结果绝不能报成完整"
        assert info["pages_failed"] == 1
        assert info["returned"] == UNIVERSE_PAGE_SIZE
        assert "2" in info["reason"]

    def test_universe_is_incomplete_when_page_cap_truncates(self):
        """**回归 IT-P1-006**：翻满上限仍未到底 -> ``complete=False``。

        这种"不是失败的失败"最危险：返回了几千只、看着很成功，其实只是全市场的
        一个前缀。页数上限被截断时必须如实标成不完整。
        """
        src = SinaSource(UNIVERSE_CFG, fetcher=FakeFetcher(always_new_pages))
        got = src.universe(max_pages=3)
        assert len(got) == 3 * UNIVERSE_PAGE_SIZE
        info = src.universe_info()
        assert info["complete"] is False
        assert info["pages_requested"] == 3
        assert info["pages_ok"] == 3
        assert "截断" in info["reason"]

    def test_universe_complete_resets_after_a_clean_round(self):
        """坏了一轮之后再成功，标记要回到 complete=True（不能永久卡在 False）。"""
        state = {"fail": True}

        def respond(url, headers, timeout):
            n = page_of(url)
            if n == 1:
                return uni_json(uni_rows(seq_codes(1)))
            if state["fail"]:
                raise RuntimeError("挂了")
            return b"[]"

        src = SinaSource(UNIVERSE_CFG, fetcher=FakeFetcher(respond))
        src.universe()
        assert src.universe_info()["complete"] is False
        state["fail"] = False
        assert len(src.universe()) == UNIVERSE_PAGE_SIZE
        assert src.universe_info()["complete"] is True

    def test_first_page_failure_reports_incomplete_metadata(self):
        """第 1 页就失败 -> 抛错，同时元数据也必须是"不完整"（不是空 dict）。"""
        src = SinaSource(UNIVERSE_CFG, fetcher=FakeFetcher(lambda u, h, t: b""))
        with pytest.raises(SourceError):
            src.universe()
        info = src.universe_info()
        assert info["complete"] is False
        assert info["returned"] == 0

    def test_universe_codes_feed_straight_into_snapshots(self):
        """两接口衔接：universe() 给的代码（含 B 股）能原样喂给 snapshots()。"""
        def respond(url, headers, timeout):
            if "Market_Center" in url:
                if page_of(url) == 1:
                    return uni_json(uni_rows(["600000", "000001", "900901"]))
                return b"[]"
            return bulk_body(url.split("list=", 1)[1].split(","))

        f = FakeFetcher(respond)
        src = SinaSource(UNIVERSE_CFG, fetcher=f)
        codes = [q.code for q in src.universe()]
        assert codes == ["600000", "000001", "900901"]        # 已去重、顺序稳定
        quotes = src.snapshots(codes)
        assert [q.code for q in quotes] == codes
        assert f.codes_of(f.urls[-1]) == ["sh600000", "sz000001", "sh900901"]
        assert all(q.price == pytest.approx(10.0) for q in quotes)
        assert src.stats()["pages_failed"] == 0
