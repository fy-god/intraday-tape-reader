"""``arad.sources.tencent`` 的离线单测。

**完全离线**：真实 HTTP 由注入的 ``fetcher`` 顶替，样本来自
``fixtures/raw/tencent_bulk_sample.txt``（2026-09-14 收盘后抓取的真实响应）。

样本实测（用脚本逐字段核对，见 docs/NOTES_tencent.md）：
* 424 行，其中 423 行合法（≥54 段），最后一行是被截断的 ``v_sz000757="51~浩物股份~000757~4.29~4.23~4.``
* 全部 423 只都是深市（``sz``），**样本里没有 ``sh600000`` / 浦发银行**，
  契约 2.1 节那行只是示意 —— 因此用真实存在的 ``sz000001 平安银行`` 做抽查。
* ``sz000003 PT金田A`` 等 55 行是停牌股：``limit_up/limit_down = -1``、成交量为 0。
"""
from __future__ import annotations

import re
import ssl
import threading
from datetime import datetime

import pytest

from tests.fakes import load_raw  # noqa: E402  （conftest 已把项目根与 src 加进 sys.path）

from arad.models import Board, Quote
from arad.sources import tencent
from arad.sources.base import Source, SourceError
from arad.sources.tencent import (
    BACKOFF_SECONDS,
    MAX_CHUNK,
    TencentSource,
    decode_body,
    parse_response,
)

# --------------------------------------------------------------------------
# 样本真实值（用 pwsh/python 读 fixtures 后人工核对，非猜测）
# --------------------------------------------------------------------------
FIXTURE = "tencent_bulk_sample.txt"
VALID_ROWS = 423              # 合法行数（>300）
MALFORMED_ROWS = 1            # 结尾被截断的那一行

#: 真实接口抓到的 sh600000 行（2026-09-14 16:14:38 收盘，GBK 解码后逐字节原样）。
#: 样本文件里没有它，但契约 2.1 节用它举例，故把真实响应内联做离线 ground truth。
SH600000_LINE = (
    'v_sh600000="1~浦发银行~600000~9.40~9.26~9.28~771871~432658~339213~9.39~1145~'
    '9.38~2011~9.37~2185~9.36~1755~9.35~2451~9.40~2425~9.41~17606~9.42~17796~'
    '9.43~13629~9.44~10580~~20260914161438~0.14~1.51~9.43~9.24~9.40/771871/722307374~'
    '771871~72231~0.23~6.11~~9.43~9.24~2.05~3130.75~3130.75~0.42~10.19~8.33~1.27~'
    '-52489~9.36~5.06~6.26~~~0.01~72230.7374~54.6140~581~   A~GP-A~-21.80~1.84~'
    '4.47~6.14~0.50~13.27~8.07~2.62~3.98~7.55~33305838300~33305838300~-73.33~-15.47~'
    '33305838300~~~-28.68~0.11~~CNY~0~___D__F__N~9.45~-23202~";'
)

# sz000001 平安银行 2026-09-14 16:14:48 收盘数据（样本文件实测）
PF = {
    "code": "000001",
    "name": "平安银行",
    "price": 11.85,
    "prev_close": 11.74,
    "open": 11.73,
    "high": 11.90,
    "low": 11.72,
    "volume_lots": 741248.0,
    "amount_wan": 87607.0,        # idx37 万元
    "turnover": 0.38,
    "volume_ratio": 0.90,
    "bid1": 11.85,
    "bid_vol": 1671.0,
    "ask1": 11.86,
    "ask_vol": 3788.0,
    "float_cap": 2299.57,
    "total_cap": 2299.60,
    "limit_up": 12.91,
    "limit_down": 10.57,
    "avg_price": 11.82,           # idx51，Quote 无该字段，用 vwap 交叉验证
    "ts": datetime(2026, 9, 14, 16, 14, 48),
}


def fixture_text() -> str:
    """样本文件的文本（真实接口是 GBK，见 decode_body 的说明）。"""
    return decode_body(load_raw(FIXTURE))


@pytest.fixture(scope="module")
def pf_quotes() -> list[Quote]:
    return parse_response(fixture_text(), seq=7)


def by_code(quotes: list[Quote], code: str) -> Quote:
    hit = [q for q in quotes if q.code == code]
    assert hit, f"缺少 {code}"
    return hit[0]


# --------------------------------------------------------------------------
# 1. 真实样本解析
# --------------------------------------------------------------------------
def test_fixture_parses_more_than_300_quotes(pf_quotes):
    assert len(pf_quotes) > 300
    assert len(pf_quotes) == VALID_ROWS
    assert len({q.code for q in pf_quotes}) == VALID_ROWS      # 无重复
    assert all(len(q.code) == 6 and q.code.isdigit() for q in pf_quotes)
    assert all(isinstance(q.board, Board) for q in pf_quotes)


def test_fixture_skips_truncated_and_nonmatching_lines():
    text = fixture_text()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) == VALID_ROWS + MALFORMED_ROWS           # 424
    # 最后一行确实是被截断的坏行
    assert lines[-1].startswith('v_sz000757="51~浩物股份~000757~4.29~4.23~4.')
    assert not lines[-1].rstrip().endswith('"')
    codes = {q.code for q in parse_response(text)}
    assert "000757" not in codes                                # 坏行被跳过
    # 无匹配 / pv_none_match 行一律跳过
    junk = 'v_pv_none_match="1";\nnot a quote line\n\nv_bad="x";\n'
    assert parse_response(junk) == []


def test_contract_example_pufa_bank_from_real_response():
    """契约 2.1 节的 v_sh600000 例子：用真实响应（离线内联）验证 9.40/9.26/9.43/9.24。

    注意样本文件里 **没有 sh600000**（全是深市），契约那行只是示意；
    真实值来自 2026-09-14 对 qt.gtimg.cn 的实测抓取。
    """
    qs = parse_response(SH600000_LINE, seq=1)
    assert len(qs) == 1
    q = qs[0]
    assert q.code == "600000" and q.name == "浦发银行"
    assert q.price == 9.40
    assert q.prev_close == 9.26
    assert q.high == 9.43
    assert q.low == 9.24
    assert q.open == 9.28
    assert q.board is Board.MAIN
    assert q.volume_lots == 771871.0                     # 手
    assert q.amount == pytest.approx(722_310_000.0)      # 72231 万 -> 元
    assert q.amount > 1e8
    assert q.limit_up == 10.19 and q.limit_down == 8.33
    assert q.turnover == 0.23 and q.volume_ratio == 1.27
    assert q.bid1 == 9.39 and q.ask1 == 9.40
    assert q.float_cap == 3130.75 and q.total_cap == 3130.75
    assert q.ts == datetime(2026, 9, 14, 16, 14, 38)
    assert q.vwap == pytest.approx(9.36, abs=0.01)       # idx51 均价
    assert q.is_suspended is False
    # 契约里 idx4=昨收、idx5=今开 的顺序（和顺位相反的新浪不同）
    assert q.prev_close == 9.26 and q.open == 9.28
    # 真实接口是 GBK：整行 GBK 编码 -> 走解码器 -> 仍然一致
    assert parse_response(decode_body(SH600000_LINE.encode("gbk")))[0].name == "浦发银行"


def test_fixture_spot_check_pingan_bank(pf_quotes):
    q = by_code(pf_quotes, PF["code"])
    assert q.name == PF["name"] == "平安银行"
    assert q.price == PF["price"] == 11.85
    assert q.prev_close == PF["prev_close"] == 11.74
    assert q.high == PF["high"] == 11.90
    assert q.low == PF["low"] == 11.72
    assert q.open == PF["open"] == 11.73
    assert q.ts == PF["ts"]
    assert q.board is Board.MAIN
    assert q.seq == 7
    # 派生量自洽
    assert q.pct == pytest.approx((11.85 / 11.74 - 1) * 100, abs=1e-6)
    assert q.is_suspended is False


def test_fixture_odd_lot_and_cap_fields(pf_quotes):
    q = by_code(pf_quotes, PF["code"])
    assert q.turnover == PF["turnover"]
    assert q.volume_ratio == PF["volume_ratio"]
    assert q.bid1 == PF["bid1"] and q.bid_vol == PF["bid_vol"]
    assert q.ask1 == PF["ask1"] and q.ask_vol == PF["ask_vol"]
    assert q.float_cap == PF["float_cap"] and q.total_cap == PF["total_cap"]
    assert q.limit_up == PF["limit_up"] and q.limit_down == PF["limit_down"]


def test_amount_is_yuan_and_volume_is_lots(pf_quotes):
    """idx37 是万元必须 ×10000；idx6 是手。用 amount/(手*100) ≈ 均价 交叉验证。"""
    q = by_code(pf_quotes, PF["code"])
    assert q.amount == pytest.approx(PF["amount_wan"] * 10_000)   # 8.7607e8 元
    assert q.amount == pytest.approx(876_070_000.0)
    assert q.amount > 1e8                                          # 单位是元，不是万元
    assert q.volume_lots == PF["volume_lots"]                      # 手，不乘 100
    # 反推均价 == idx51（11.82），证明 元 / 手*100 自洽
    assert q.vwap == pytest.approx(PF["avg_price"], abs=0.02)
    assert q.amount / (q.volume_lots * 100) == pytest.approx(PF["avg_price"], abs=0.02)

    # 全样本量级：最大成交额 419507 万元 -> 4.195e9 元
    amts = [x.amount for x in pf_quotes]
    assert max(amts) == pytest.approx(4_195_070_000.0)
    assert max(amts) > 1e9
    assert sum(1 for a in amts if a > 1e8) > 10


def test_star_board_volume_normalized_from_shares_to_lots():
    """科创板 688/689 的 idx6 单位是「股」，必须 /100 折成「手」。

    回归测试：修复前 688111 的 vwap 算成 2.30（真实 230.06），
    价格过滤/量能判断会整体失真。真实响应行内联如下。
    """
    line = (
        'v_sh688111="1~金山办公~688111~228.59~228.82~226.00~3506475~1731576~1774899'
        '~228.56~2~228.54~2~228.53~7~228.52~5~228.51~3~228.59~213~228.60~5~228.69~4'
        '~228.70~8~228.72~173~~20260914161439~-0.23~-0.10~232.70~226.00'
        '~228.59/3506475/806688058~3506475~80669~0.76~29.41~~232.70~226.00~2.93'
        '~1060.69~1060.69~7.04~274.58~183.06~0.75~-384~230.06~21.07~57.76~~~1.61'
        '~80668.8058~9.1436~400~A RA~GP-A-KCB~-25.25~-4.57~0.55~23.92~17.27~413.75'
        '~198.75~-8.44~-10.51~1.29~464013435~464013435~-91.00~-26.54~464013435~~~'
        '-25.08~-0.07~~CNY~0~___D__F__NY~228.82~-31~100"'
    )
    q = parse_response(line)[0]
    assert q.code == "688111" and q.board is Board.STAR
    # idx6 原始 3506475 股 -> 35064.75 手
    assert q.volume_lots == pytest.approx(35064.75)
    # idx7(外盘)+idx8(内盘) 与 idx6 完全相等，佐证 idx6 就是股数
    assert q.amount == pytest.approx(806_690_000.0, rel=1e-4)      # 80669 万元
    # 关键断言：均价必须贴近现价，而不是差 100 倍
    assert q.vwap == pytest.approx(230.06, abs=0.05)
    assert 0.9 < q.vwap / q.price < 1.1
    # 封单额按「手」口径才正确（bid_vol 也同步折算）
    assert q.bid_vol == pytest.approx(0.02)                        # 2 股 -> 0.02 手
    assert q.ask_vol == pytest.approx(2.13)                        # 213 股 -> 2.13 手

    # 其他板块不受影响：手 -> 手，不做折算
    assert parse_response(SH600000_LINE)[0].volume_lots == pytest.approx(771871.0)
    for code in ("sz000001", "sz300750", "bj920002"):
        raw = f'v_{code}="1~测试~{code[2:]}~10.0~10.0~10.0~12345~1~1~10.0~1~10.0~1~10.0~1~10.0~1~10.0~1~10.0~1~10.0~1~10.0~1~~20260914150000~0~0~10~10~10/12345/12345000~12345~1234~0.1~1~~10~10~1~1~1~1~11~9~1~1~10.0~1~1~~~1~1~1~100~A~GP~1~1~1~1~1~1~1~1~1~1~1~1~1~1~~~1~1~~CNY~0~___D__F__NY~10.0~1~100"'
        assert parse_response(raw)[0].volume_lots == pytest.approx(12345.0)


def test_suspended_rows_kept_and_no_negative_limit(pf_quotes):
    """停牌/PT 股：limit_up/down 的 -1 不能存进 Quote，也不能崩。"""
    limits = [(q.limit_up, q.limit_down) for q in pf_quotes]
    assert all(lu >= 0 and ld >= 0 for lu, ld in limits)           # 全样本无 -1
    assert not any(lu == -1 or ld == -1 for lu, ld in limits)

    pt = by_code(pf_quotes, "000003")                              # PT金田A
    assert pt.name == "PT金田A"
    assert pt.limit_up == 0.0 and pt.limit_down == 0.0             # -1 -> 0
    assert pt.volume_lots == 0.0 and pt.price > 0
    assert pt.is_suspended is True                                 # 成交量为 0
    assert pt.limit_up_price == pytest.approx(round(2.71 * 1.10, 2))   # 2.98，由 Quote 推算
    assert pt.limit_down_price == pytest.approx(round(2.71 * 0.90, 2))
    assert pt.limit_up_price > 0

    # 55 行 limit=-1 的样本都被归一为 0
    text = fixture_text()
    neg_lines = [
        ln for ln in text.splitlines()
        if re.match(r'^v_[a-z]{2}\d{6}="', ln) and len(ln.split("~")) >= 54
        and ln.split("~")[47] in ("-1", "-1.0")
    ]
    assert len(neg_lines) == 55
    assert sum(1 for lu, _ in limits if lu == 0.0) >= len(neg_lines)


def test_zero_price_row_present_but_does_not_crash(pf_quotes):
    """sz001246 力勤资源 price/prev_close 全 0：保留在快照里，is_suspended=True。"""
    q = by_code(pf_quotes, "001246")
    assert q.price == 0.0 and q.prev_close == 0.0
    assert q.is_suspended is True
    assert q.pct == 0.0 and q.change == 0.0        # 派生量不炸


def test_parse_response_pure_and_seq_propagates():
    text = fixture_text()
    a = parse_response(text)
    b = parse_response(text)
    assert [q.code for q in a] == [q.code for q in b]           # 纯函数、稳定
    assert all(q.seq == 0 for q in a)
    assert all(q.seq == 99 for q in parse_response(text, seq=99))


# --------------------------------------------------------------------------
# 2. 数值容错（合成行）
# --------------------------------------------------------------------------
def make_line(code: str = "600000", prefix: str = "sh", n: int = 54,
              fields: dict[int, str] | None = None) -> str:
    """构造一行腾讯响应。默认 54 段、字段值合法；``fields`` 按 idx 覆盖。"""
    f = [""] * n
    base: dict[int, str] = {
        1: "测试股", 2: code, 3: "10.00", 4: "9.50", 5: "9.60", 6: "1234",
        9: "9.99", 10: "100", 19: "10.01", 20: "200",
        30: "20260914100000", 33: "10.20", 34: "9.40", 37: "1234",
        38: "1.23", 43: "8.4", 44: "50.5", 45: "60.6",
        47: "10.45", 48: "8.55", 49: "2.5", 51: "9.95",
    }
    base.update(fields or {})
    for i, v in base.items():
        f[i] = v
    return f'v_{prefix}{code}="' + "~".join(f) + '";'


def test_helper_safe_conversion_keeps_row():
    """空串/非数字的次要字段 -> 默认 0.0，整条保留。"""
    line = make_line(code="000001", prefix="sz",
                     fields={38: "", 49: "abc", 9: " ", 37: ""})
    qs = parse_response(line)
    assert len(qs) == 1
    q = qs[0]
    assert q.turnover == 0.0 and q.volume_ratio == 0.0 and q.bid1 == 0.0
    assert q.amount == 0.0
    assert q.price == 10.0 and q.prev_close == 9.50      # 主字段仍然正确


def test_invalid_price_drops_row_but_valid_suspended_kept():
    bad_price = make_line(code="600001", fields={3: ""})
    bad_prev = make_line(code="600002", fields={4: "abc"})
    suspended = make_line(code="600003", fields={3: "0.00", 4: "0.00", 6: "0"})
    qs = parse_response("\n".join([bad_price, bad_prev, suspended]))
    assert [q.code for q in qs] == ["600003"]
    assert qs[0].is_suspended is True


def test_short_payload_skipped():
    short = make_line(code="600010", n=53)
    ok = make_line(code="600011", n=54)
    ids = [q.code for q in parse_response(short + "\n" + ok)]
    assert ids == ["600011"]


def test_bad_timestamp_becomes_none():
    line = make_line(code="600020", fields={30: "not-a-date"})
    line2 = make_line(code="600021", fields={30: ""})
    line3 = make_line(code="600022", fields={30: "20261332100000"})   # 月/日非法
    qs = parse_response("\n".join([line, line2, line3]))
    assert len(qs) == 3
    assert all(q.ts is None for q in qs)


def test_decode_body_gbk_first_then_utf8_fallback():
    gbk = 'v_sz000001="51~平安银行~000001~11.85";'.encode("gbk")
    assert "平安银行" in decode_body(gbk)
    utf8 = 'v_sz000001="51~平安银行~000001~11.85";'.encode("utf-8")
    assert "平安银行" in decode_body(utf8)                        # 回退路径（样本文件就是 UTF-8）
    weird = b"\xff\xfe\x00\x01"
    assert isinstance(decode_body(weird), str)                    # 绝不抛
    assert decode_body("already str") == "already str"


# --------------------------------------------------------------------------
# 3. URL / 请求头 / 分批 / 并发
# --------------------------------------------------------------------------
class RecordingFetcher:
    """记录 (url, headers, timeout)；按调用返回预设 body 或抛异常。"""

    def __init__(self, body: bytes | str = "", fail_times: int = 0,
                 exc: Exception | None = None, per_call=None):
        self.body = body.encode("gbk") if isinstance(body, str) else body
        self.fail_times = fail_times
        self.exc = exc or RuntimeError("boom")
        self.per_call = per_call
        self.calls: list[tuple[str, dict, float]] = []
        self._lock = threading.Lock()

    @property
    def urls(self) -> list[str]:
        return [c[0] for c in self.calls]

    def __call__(self, url: str, headers: dict, timeout: float) -> bytes:
        with self._lock:
            self.calls.append((url, dict(headers), timeout))
            idx = len(self.calls)
        if self.per_call is not None:
            return self.per_call(url, idx)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.exc
        return self.body or b""


def test_url_headers_and_prefix_conversion():
    fetcher = RecordingFetcher(fail_times=0)
    src = TencentSource({"timeout": 7, "workers": 2}, fetcher=fetcher)
    src.snapshots(["600000", "000001", "300750", "920001", "688111"])

    assert len(fetcher.calls) == 1
    url, headers, timeout = fetcher.calls[0]
    assert url.startswith("https://qt.gtimg.cn/q=")
    expected = "https://qt.gtimg.cn/q=sh600000,sz000001,sz300750,bj920001,sh688111"
    assert url == expected
    assert headers["Referer"] == "https://gu.qq.com/"
    assert "Mozilla/5.0" in headers["User-Agent"] and "Windows NT" in headers["User-Agent"]
    assert headers["Accept"] == "*/*"
    assert timeout == 7
    assert src.health()["ok"] is True


def test_duplicate_and_prefixed_codes_normalized():
    fetcher = RecordingFetcher()
    src = TencentSource({}, fetcher=fetcher)
    src.snapshots(["600000", "sh600000", "SH600000", " 600000 "])
    assert fetcher.urls == ["https://qt.gtimg.cn/q=sh600000"]
    assert src.snapshots([]) == []                       # 空输入不打网络
    assert len(fetcher.calls) == 1


def test_chunking_1300_codes_into_3_batches():
    """1300 只票按 bulk_chunk=600 切成 3 批（600/600/100）。

    ⚠ 断言必须**与批次完成顺序无关**。``RecordingFetcher`` 在锁内 append，
    所以 ``fetcher.urls`` 的顺序是**线程实际完成的顺序**；源码走
    ``ThreadPoolExecutor``（默认多 worker），高负载下线程调度错位，
    完成顺序本来就不保证等于提交顺序 —— 曾实测到 ``[600, 100, 600]``。
    所以这里比的是**多重集合**（排序后），而不是按下标逐一比对。

    顺序无关的写法并不比原来弱：下面额外校验了「批次不重不漏、合起来正好是
    全部 1300 只」，比单纯看三个数字更能说明切分是对的。
    """
    fetcher = RecordingFetcher()
    src = TencentSource({"bulk_chunk": 600, "workers": 4}, fetcher=fetcher)
    codes = [f"{600000 + i:06d}" for i in range(1300)]
    quotes = src.snapshots(codes)

    assert quotes == []                                  # fetcher 返回空 body
    assert len(fetcher.urls) == 3                        # 600 + 600 + 100
    batches = [u.split("=", 1)[1].split(",") for u in fetcher.urls]
    sizes = sorted(len(b) for b in batches)
    assert sizes == [100, 600, 600]
    assert all(s <= MAX_CHUNK for s in sizes)
    assert all(s <= 600 for s in sizes)

    # 不重不漏：三批并起来正好是输入的 1300 只，且没有一只出现在两个批次里
    flat = [c for b in batches for c in b]
    assert len(flat) == len(set(flat)) == 1300
    assert set(flat) == {f"sh{c}" for c in codes}        # 6 开头 -> sh 前缀

    assert src.stats()["chunks"] == 3
    assert src.stats()["requests"] == 3
    assert src.stats()["errors"] == 0


def test_chunk_is_clamped_to_hard_cap_800():
    assert TencentSource({"bulk_chunk": 5000}).chunk == MAX_CHUNK
    assert TencentSource({"bulk_chunk": 0}).chunk == 600          # 非法 -> 默认
    assert TencentSource({"bulk_chunk": -3}).chunk == 600
    assert TencentSource({"batch_size": 250}).chunk == 250        # 回退 poll.batch_size
    assert TencentSource({}).chunk == 600
    assert TencentSource({"bulk_chunk": "300"}).chunk == 300      # 字符串数字

    fetcher = RecordingFetcher()
    src = TencentSource({"bulk_chunk": 5000}, fetcher=fetcher)
    src.snapshots([f"{600000 + i:06d}" for i in range(900)])
    sizes = [len(u.split("=", 1)[1].split(",")) for u in fetcher.urls]
    assert sizes == [800, 100]                                    # 真的按 800 切
    assert max(sizes) <= MAX_CHUNK


# --------------------------------------------------------------------------
# 4. 重试 / 退避 / 错误
# --------------------------------------------------------------------------
def test_retry_exhausted_raises_source_error(monkeypatch):
    delays: list[float] = []
    monkeypatch.setattr(tencent, "_SLEEP", lambda s: delays.append(s))

    fetcher = RecordingFetcher(fail_times=10_000, exc=OSError("conn reset"))
    src = TencentSource({"retries": 3}, fetcher=fetcher)
    with pytest.raises(SourceError) as ei:
        src.snapshots(["600000", "000001"])
    assert "批次" in str(ei.value) and "4 次尝试" in str(ei.value)

    assert len(fetcher.calls) == 4                      # 1 次首发 + 3 次重试
    assert delays == [0.3, 0.9, 2.0]
    assert delays == list(BACKOFF_SECONDS)
    assert src.stats()["requests"] == 4 and src.stats()["errors"] == 4
    assert src.stats()["retries"] == 3

    h = src.health()
    assert h["ok"] is False and "OSError" in h["err"]


def test_retry_succeeds_after_failures(monkeypatch):
    delays: list[float] = []
    monkeypatch.setattr(tencent, "_SLEEP", lambda s: delays.append(s))

    body = make_line(code="600000", prefix="sh", fields={3: "11.85", 4: "11.74"})
    fetcher = RecordingFetcher(body, fail_times=2, exc=TimeoutError("slow"))
    src = TencentSource({"retries": 3}, fetcher=fetcher)
    quotes = src.snapshots(["600000"])

    assert len(fetcher.calls) == 3
    assert delays == [0.3, 0.9]
    assert [q.code for q in quotes] == ["600000"]
    assert quotes[0].price == 11.85 and quotes[0].prev_close == 11.74
    assert src.health()["ok"] is True and src.health()["err"] == ""
    assert src.stats()["retries"] == 2


def test_retries_zero_means_single_attempt(monkeypatch):
    monkeypatch.setattr(tencent, "_SLEEP", lambda s: pytest.fail("不该退避"))
    fetcher = RecordingFetcher(fail_times=10_000)
    src = TencentSource({"retries": 0}, fetcher=fetcher)
    with pytest.raises(SourceError):
        src.snapshots(["600000"])
    assert len(fetcher.calls) == 1


def test_parse_error_also_triggers_retry(monkeypatch):
    """fetcher 本身不抛、但返回坏数据时不会重试；fetcher 抛异常才会。"""
    monkeypatch.setattr(tencent, "_SLEEP", lambda s: None)
    fetcher = RecordingFetcher(body=b"\xff\xfe not gbk")
    src = TencentSource({"retries": 3}, fetcher=fetcher)
    assert src.snapshots(["600000"]) == []
    assert len(fetcher.calls) == 1


def test_partial_batch_failure_raises_and_keeps_cache_clean(monkeypatch):
    monkeypatch.setattr(tencent, "_SLEEP", lambda s: None)

    def per_call(url: str, idx: int) -> bytes:
        if "600000" in url:
            return make_line(code="600000", prefix="sh").encode("gbk")
        raise OSError("second chunk down")

    fetcher = RecordingFetcher(per_call=per_call)
    src = TencentSource({"bulk_chunk": 1, "retries": 0, "workers": 2}, fetcher=fetcher)
    with pytest.raises(SourceError) as ei:
        src.snapshots(["600000", "000001"])
    assert "1/2 批次失败" in str(ei.value)
    assert src.cache_size == 0                         # 失败快照不污染缓存
    assert src.stats()["chunks"] == 2


# --------------------------------------------------------------------------
# 5. universe（快照缓存，不依赖东财）
# --------------------------------------------------------------------------
def test_universe_raises_when_cache_empty():
    src = TencentSource({}, fetcher=RecordingFetcher())
    with pytest.raises(SourceError) as ei:
        src.universe()
    assert "缓存为空" in str(ei.value)


def test_universe_returns_snapshot_cache():
    body = "\n".join([
        make_line(code="600000", prefix="sh"),
        make_line(code="000001", prefix="sz"),
    ])
    src = TencentSource({}, fetcher=RecordingFetcher(body))
    with pytest.raises(SourceError):
        src.universe()

    quotes = src.snapshots(["600000", "000001"])
    assert len(quotes) == 2
    uni = src.universe()
    assert {q.code for q in uni} == {"600000", "000001"}
    assert src.cache_size == 2

    # 缓存是并集：再拉一只，universe 有 3 只
    src2_body = make_line(code="300750", prefix="sz")
    src._fetcher = RecordingFetcher(src2_body)
    src.snapshots(["300750"])
    assert {q.code for q in src.universe()} == {"600000", "000001", "300750"}

    # 显式注入 codes 时等价 snapshots（本源不依赖 eastmoney）
    src._fetcher = RecordingFetcher(make_line(code="688111", prefix="sh"))
    assert [q.code for q in src.universe(["688111"])] == ["688111"]
    assert "股票池由 eastmoney 源提供" in TencentSource.universe.__doc__


# --------------------------------------------------------------------------
# 6. health / stats / 协议 / 默认 fetcher
# --------------------------------------------------------------------------
def test_health_fields():
    src = TencentSource({}, fetcher=RecordingFetcher(make_line()))
    h0 = src.health()
    assert set(h0) == {"name", "ok", "latency_ms", "err"}
    assert h0["name"] == "tencent"
    assert isinstance(h0["ok"], bool)
    assert isinstance(h0["latency_ms"], int) and h0["latency_ms"] >= 0
    assert isinstance(h0["err"], str)

    src.snapshots(["600000"])
    h1 = src.health()
    assert h1["ok"] is True and h1["err"] == "" and h1["latency_ms"] >= 0
    assert h1["name"] == "tencent"


def test_health_latency_uses_clock(monkeypatch):
    """latency_ms = round((perf_counter 结束 - 开始) * 1000)。"""
    seq = [0.0, 0.25, 1.0, 1.4]

    def fake_clock() -> float:
        return seq.pop(0) if seq else 1.4        # 兜底：绝不 StopIteration

    monkeypatch.setattr(tencent.time, "perf_counter", fake_clock)
    src = TencentSource({}, fetcher=RecordingFetcher())
    src.snapshots(["600000"])

    # 第一次调用实际耗时 250ms（0.0 -> 0.25）
    assert src.health()["latency_ms"] == 250


def test_implements_source_protocol():
    src = TencentSource({})
    assert isinstance(src, Source)
    assert src.name == "tencent"
    for attr in ("universe", "snapshots", "health"):
        assert callable(getattr(src, attr))
    assert tencent.build({"bulk_chunk": 10}).name == "tencent"


def test_stats_snapshot_is_a_copy():
    src = TencentSource({}, fetcher=RecordingFetcher())
    s = src.stats()
    s["requests"] = 999
    assert src.stats()["requests"] == 0


def test_lazy_ssl_context_and_default_fetcher_signature():
    ctx = tencent._ssl_context()
    assert ctx.verify_mode.name == "CERT_REQUIRED"
    insecure = tencent._ssl_context(insecure=True)
    assert insecure.check_hostname is False
    assert insecure.verify_mode.name == "CERT_NONE"
    assert tencent._ssl_context() is ctx                      # 惰性缓存
    assert callable(tencent.default_fetcher)
    assert tencent._is_ssl_error(ssl.SSLCertVerificationError("nope"))
    assert not tencent._is_ssl_error(OSError("connection reset"))


def test_insecure_tls_fallback_is_opt_outable(monkeypatch):
    """证书校验失败时的"不校验重试"必须可以关掉。

    这是安全开关：默认开（兼容 Windows 根证书链不全的环境），
    但在不可信网络上必须能关，且关掉后**直接抛错**而不是静默降级 ——
    静默使用未校验的连接等于放弃中间件攻击防护。
    """
    # 默认（未设环境变量）为开启回退
    monkeypatch.delenv("ARAD_ALLOW_INSECURE_TLS", raising=False)
    assert tencent._env_allows_insecure_tls() is True

    for off in ("0", "false", "no", "FALSE", "No", ""):
        monkeypatch.setenv("ARAD_ALLOW_INSECURE_TLS", off)
        assert tencent._env_allows_insecure_tls() is False, off
    for on in ("1", "true", "yes", "TRUE"):
        monkeypatch.setenv("ARAD_ALLOW_INSECURE_TLS", on)
        assert tencent._env_allows_insecure_tls() is True, on


def test_tls_error_not_retried_insecurely_when_disabled(monkeypatch):
    """关闭回退后：证书错误必须原样抛出，不能悄悄换个 unchecked 连接。"""
    monkeypatch.setattr(tencent, "ALLOW_INSECURE_TLS", False)
    calls: list[bool] = []

    def fake_urlopen(req, timeout=None, context=None):
        calls.append(bool(context and context.verify_mode == ssl.CERT_NONE))
        raise ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED")

    monkeypatch.setattr(tencent.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ssl.SSLCertVerificationError):
        tencent.default_fetcher("https://qt.gtimg.cn/q=sh600000", {})
    assert calls == [False], "关闭回退后不应发起第二次（不校验）请求"


def test_tls_error_retried_insecurely_when_enabled(monkeypatch):
    """开启回退（默认）时：先严格校验，失败后不校验重试一次。"""
    monkeypatch.setattr(tencent, "ALLOW_INSECURE_TLS", True)
    seen: list[bool] = []

    class Resp:
        def read(self):
            return b'v_sh600000="ok";'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None, context=None):
        insecure = bool(context and context.verify_mode == ssl.CERT_NONE)
        seen.append(insecure)
        if not insecure:
            raise ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED")
        return Resp()

    monkeypatch.setattr(tencent.urllib.request, "urlopen", fake_urlopen)
    out = tencent.default_fetcher("https://qt.gtimg.cn/q=sh600000", {})
    assert out == b'v_sh600000="ok";'
    assert seen == [False, True], "应先校验、失败后再不校验重试"


def test_public_api_surface():
    """导出的常量/helper 齐全，且默认参数符合契约。"""
    from arad.sources import tencent as t

    assert t.URL_BASE == "https://qt.gtimg.cn/q="
    assert t.DEFAULT_REFERER == "https://gu.qq.com/"
    assert t.MAX_CHUNK == 800 and t.DEFAULT_CHUNK == 600
    assert t.BACKOFF_SECONDS == (0.3, 0.9, 2.0)
    assert t.MIN_FIELDS == 54
    assert t.LINE_RE.match('v_sh600000="a~b";')
    # parse_response(text, seq=0, index_codes=None, route="stocks")：
    # 第三/四个参数是**调用方声明的身份角色**（任务书 §WP02 caller-owned role），
    # 让 000001 这类歧义码能区分「上证指数」与「平安银行」。
    #
    # ⚠ `route` 是 2026-09-24 新增的：身份判定曾三次返工 ——
    #   第 1 版拿"显式前缀"当"它是指数"（`sh600000` 浦发银行被误判指数）；
    #   第 2 版让 request 侧名称盲推断、raw 侧仍用 provider 真名，
    #   于是 7 个只能靠名称认定的指数（`sh000922` 中证红利指数等）两轴分离。
    #   现在 request/raw/Quote 三处**只消费同一份 role**，不再各自猜。
    assert tencent.parse_response.__defaults__ == (0, None, "stocks")
    src = TencentSource({})
    assert src.retries == 3 and src.timeout == 10.0 and src.workers == 4
    assert src.referer == t.DEFAULT_REFERER


def test_concurrent_chunks_are_thread_safe():
    """4 线程并发 4 批，统计不丢写。"""
    body = "\n".join(make_line(code=f"{600000 + i:06d}") for i in range(4))
    src = TencentSource({"bulk_chunk": 1, "workers": 4}, fetcher=RecordingFetcher(body))
    quotes = src.snapshots([f"{600000 + i:06d}" for i in range(4)])
    assert len(quotes) == 4
    assert src.stats()["requests"] == 4 and src.stats()["chunks"] == 4
    assert src.cache_size == 4
    assert src.health()["ok"] is True


def test_chunks_really_run_in_parallel_and_honour_workers():
    """worker 数来自 cfg，且多批真的并发（用 Barrier 证明）。"""
    state = {"cur": 0, "max": 0, "barrier_hits": 0}
    lock = threading.Lock()
    n_chunks = 4
    barrier = threading.Barrier(n_chunks, timeout=5)

    def per_call(url: str, idx: int) -> bytes:
        with lock:
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
        try:
            barrier.wait()               # 4 个线程必须同时在场，否则超时
            with lock:
                state["barrier_hits"] += 1
        finally:
            with lock:
                state["cur"] -= 1
        return b""

    fetcher = RecordingFetcher(per_call=per_call)
    src = TencentSource({"bulk_chunk": 1, "workers": 4}, fetcher=fetcher)
    src.snapshots([f"{600000 + i:06d}" for i in range(n_chunks)])

    assert len(fetcher.calls) == n_chunks
    assert state["max"] == n_chunks          # 峰值并发 == 批数
    assert state["barrier_hits"] == n_chunks  # 无死锁、真并行

    # workers=1 -> 串行，Barrier 必然超时被打破，但请求都完成
    serial = TencentSource({"bulk_chunk": 1, "workers": 1},
                           fetcher=RecordingFetcher())
    assert serial.workers == 1
    assert serial.snapshots(["600000", "000001"]) == []


def test_workers_zero_or_bad_falls_back_to_one():
    assert TencentSource({"workers": 0}).workers == 1
    assert TencentSource({"workers": -2}).workers == 1
    assert TencentSource({"workers": "8"}).workers == 8
    assert TencentSource({"workers": None}).workers == 4
