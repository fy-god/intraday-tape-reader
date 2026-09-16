"""指数行情管道：前缀保真 + 引擎按需抓取 + 历史不被 prune 清掉。

这一组测试守的是三个具体的坑：

1. ``sh000001`` 与 ``000001`` 是**两个不同标的**（上证指数 / 平安银行）。
   ``normalize_code`` 会剥前缀，一旦在错误的位置调用，指数就会被悄悄换成个股，
   拿到一份"看起来正常"的错误数据。
2. 指数走 ``filters.accept`` 会被 ``exclude_boards=["index"]`` 拦下，所以它
   不进 ``eligible``；但 ``state.prune`` 只看 keep 集合，不额外保住指数的话，
   刚攒起来的 5 分钟窗口每轮都被清空，「拉升指数」永远等不到样本。
3. 没有任何规则消费指数时不该发请求 —— 否则每轮白抓 5 个代码。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arad.models import (Board, Quote, board_of, guess_prefix,  # noqa: E402
                         looks_like_index)
from arad.sources.tencent import (_norm_specs, parse_response,  # noqa: E402
                                 split_prefix)
from tests.fakes import FakeSource, make_quote  # noqa: E402

# ==========================================================================
# split_prefix / _norm_specs
# ==========================================================================
@pytest.mark.parametrize("raw,want", [
    ("sh000001", ("sh", "000001")),
    ("SZ399001", ("sz", "399001")),
    ("bj430047", ("bj", "430047")),
    ("000001", ("", "000001")),
    ("sh600000", ("sh", "600000")),
    ("", ("", "")),
])
def test_split_prefix(raw, want):
    assert split_prefix(raw) == want


def test_norm_specs_keeps_explicit_prefix():
    """显式前缀必须活下来：它是 000001 歧义的唯一解药。"""
    specs = _norm_specs(["sh000001", "sz399001"])
    assert specs == [("sh000001", True), ("sz399001", True)]


def test_norm_specs_marks_bare_codes_as_not_explicit():
    """裸码补出来的前缀不算"显式"，因此不允许它触发指数判定。"""
    assert _norm_specs(["000001"]) == [("sz000001", False)]
    assert _norm_specs(["600000"]) == [("sh600000", False)]


def test_norm_specs_dedupes_on_resolved_symbol():
    """``600000`` 与 ``sh600000`` 解析后是同一个符号，只能留一个。"""
    assert _norm_specs(["600000", "sh600000", "SH600000", " 600000 "]) == [
        ("sh600000", False)
    ]


def test_norm_specs_drops_garbage():
    assert _norm_specs(["", "abc", "60000", "6000000", None]) == []


# ==========================================================================
# parse_response：指数带前缀、个股保持原样
# ==========================================================================
def _line(symbol: str, name: str, price: float) -> str:
    """造一行腾讯响应：只填到 name/price/prev_close，其余留空。"""
    fields = [""] * 60
    fields[1] = name
    fields[2] = symbol[2:]
    fields[3] = f"{price:.2f}"
    fields[4] = f"{price:.2f}"
    return f'v_{symbol}="{"~".join(fields)}";'


def test_index_keeps_prefixed_code_and_gets_index_board():
    text = _line("sh000001", "上证指数", 3891.60)
    got = parse_response(text, 0, {"sh000001"})
    assert len(got) == 1
    assert got[0].code == "sh000001"      # 前缀保留
    assert got[0].board is Board.INDEX


def test_ordinary_stock_is_byte_identical_to_before():
    """个股行为不能因为指数支持而改变：code 仍是裸 6 位。"""
    text = _line("sh600000", "浦发银行", 10.00)
    got = parse_response(text, 0, {"sh000001"})
    assert got[0].code == "600000"
    assert got[0].board is Board.MAIN


def test_bare_000001_is_not_treated_as_index_even_if_declared():
    """声明集合只认带前缀的符号；裸 000001 仍然是个股（平安银行）。"""
    text = _line("sz000001", "平安银行", 11.70)
    got = parse_response(text, 0, {"sh000001"})
    assert got[0].code == "000001"
    assert got[0].board is Board.MAIN


def test_index_declared_but_name_contradicts_stays_stock():
    """配置写错（把个股写成 sh600000）时不能因为"声明过"就标成指数。"""
    text = _line("sh600000", "浦发银行", 10.00)
    got = parse_response(text, 0, {"sh600000"})
    assert got[0].board is Board.MAIN
    assert got[0].code == "600000"


# ==========================================================================
# looks_like_index：全项目唯一判据
# ==========================================================================
@pytest.mark.parametrize("symbol,name,want", [
    ("sh000001", "上证指数", True),
    ("sh000001", "", True),            # 白名单 + sh 前缀
    ("sz000001", "平安银行", False),    # 撞码个股
    ("000001", "平安银行", False),
    ("sz399001", "深证成指", True),
    ("sz399006", "", True),            # 399 段无条件
    ("sh600000", "浦发银行", False),
    ("sh000300", "沪深300", True),
    ("sh000688", "科创50", True),
    ("sz000688", "", False),           # 688 只在沪市是指数
    ("bj430047", "诺思兰德", False),
])
def test_looks_like_index(symbol, name, want):
    assert looks_like_index(symbol, name) is want


def test_guess_prefix_is_documented_as_stock_only():
    """``guess_prefix("000001")`` 推 sz（平安银行）—— 这正是不能拿它猜指数的原因。"""
    assert guess_prefix("000001") == "sz"
    assert board_of("000001", "平安银行") is Board.MAIN


# ==========================================================================
# 端到端：行情 -> Engine -> spirit_index -> 中文播报
# ==========================================================================
def _engine_for_index(monkeypatch, source):
    """搭一个只跑 spirit_index 的引擎（板块过滤照旧，指数会被 exclude 拦掉）。

    ⚠ ``load_settings()`` 带进程内缓存，返回的是**同一个可变对象**。直接改它
    会污染同一进程里的其它测试（实测：把 4 个生产规则改写 enabled=False 后，
    后续 test_replay_cli 整组变成 0 条告警）。所以这里走 ``use_cache=False``
    拿一份独立副本。
    """
    from arad.config import load_settings
    from arad.engine import Engine
    from arad.session import SessionPhase, TradingCalendar

    st = load_settings(use_cache=False)
    st.section("rules").setdefault("spirit_index", {})["enabled"] = True
    for name in ("tick_surge", "limit_board", "volume_burst", "unusual",
                 "spirit_price", "spirit_order"):
        st.section("rules").setdefault(name, {})["enabled"] = False
    st.section("poll")["index_codes"] = ["sh000001"]

    cal = TradingCalendar(holidays=set())
    monkeypatch.setattr(cal, "phase", lambda now=None: SessionPhase.MORNING)
    eng = Engine(source=source, settings=st, notifiers=[], calendar=cal)
    # index_codes 由配置读出；这里复核一遍，顺便保证测试不依赖 YAML 内容
    eng.index_codes = ["sh000001"]
    return eng


def test_index_pipeline_end_to_end(monkeypatch):
    """指数持续拉升 -> 真的产出「拉升指数」，且 code 保留 sh 前缀。"""
    from datetime import timedelta

    base = datetime(2026, 9, 15, 9, 30, 0)

    class RisingIndex:
        def __init__(self):
            self.t = base
            self.price = 3890.0

        def snapshots(self, codes):
            self.t += timedelta(seconds=10)
            self.price += 0.2
            return [
                Quote(code="sh000001", name="上证指数", board=Board.INDEX,
                      price=round(self.price, 2), prev_close=3864.28,
                      open=3866.0, high=round(self.price, 2), low=3865.0,
                      volume_lots=1e7, amount=1e11, ts=self.t)
                for _ in codes
            ]

        def health(self):
            return {"name": "idx", "ok": True, "latency_ms": 1, "err": ""}

    src = RisingIndex()
    eng = _engine_for_index(monkeypatch, src)
    assert [getattr(r, "name", "") for r in eng.rules] == ["spirit_index"]

    alerts = []
    for _ in range(40):
        monkeypatch.setattr(eng, "now", lambda s=src: s.t)
        alerts.extend(eng.poll_once(force=True))

    assert alerts, "指数从 3890 拉到 3906（+16 点）却没有告警"
    pats = {(a.metrics or {}).get("pattern") for a in alerts}
    assert "index_pull" in pats, f"期望 index_pull，实际 {pats}"
    for a in alerts:
        assert a.code == "sh000001", "指数 code 丢了前缀，会与平安银行撞码"
        assert a.metrics.get("signal") or a.metrics.get("pattern")


def test_index_alert_renders_as_chinese_spirit_line(monkeypatch):
    """展示层能把指数告警渲染成中文精灵行（含方向：拉升=红=up）。"""
    from arad.spirit import SIGNALS, direction_of, fmt_line, signal_of

    class FakeAlert:
        code = "sh000001"
        name = "上证指数"
        kind = None
        price = 3895.0
        pct = 0.79
        ts = datetime(2026, 9, 15, 9, 34, 0)
        severity = 1
        metrics = {"pattern": "index_pull"}

    assert signal_of(FakeAlert) == "index_pull"
    assert direction_of(FakeAlert) == "up"
    assert SIGNALS["index_pull"].cn == "拉升指数"
    line = fmt_line(FakeAlert)
    assert "拉升指数" in line and "sh000001" in line


def test_index_press_maps_to_down_direction():
    """打压指数必须是绿色（down）——方向搞反比不报还糟。"""
    from arad.spirit import SIGNALS, direction_of

    class FakeAlert:
        code = "sh000001"
        name = "上证指数"
        kind = None
        price = 3880.0
        pct = -0.4
        ts = datetime(2026, 9, 15, 10, 0, 0)
        severity = 1
        metrics = {"pattern": "index_press"}

    assert SIGNALS["index_press"].cn == "打压指数"
    assert direction_of(FakeAlert) == "down"

