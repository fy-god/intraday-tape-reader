"""WP04 + WP05：core state identity 与 SpiritIndex 假信号负对照。

WP04：`EngineState` 的核心价格状态按 `q.code` 存
（`quotes` / `history` / `day_open` / `first_seen` / `last_price`）。
若指数 `Quote.code` 被压成裸码，正确的指数会与平安银行**共用这些 map**。
本文件钉住：指数 `sh000001` 与股票 `sz000001` 在 state 里互不影响。

WP05：预载 5 分钟平安银行历史后，第一条上证指数样本进入 ——
不得因为 key 撞车而把平安银行的 5 分钟历史当成指数的历史，
从而伪造出指数类拉升/打压信号。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from arad.engine import EngineState
from arad.models import Board, Quote

T0 = datetime(2026, 9, 24, 10, 0, 0)


def _quote(code: str, price: float, *, ts: datetime, board: Board,
           name: str = "") -> Quote:
    return Quote(code=code, name=name or code, board=board,
                 price=price, prev_close=price - 0.1, open=price - 0.05,
                 high=price + 0.1, low=price - 0.2,
                 volume_lots=1000.0, amount=price * 1000.0, ts=ts)


def _stock(price: float, *, ts: datetime, code: str = "000001") -> Quote:
    return _quote(code, price, ts=ts, board=Board.MAIN, name="平安银行")


def _idx(price: float, *, ts: datetime, code: str = "sh000001") -> Quote:
    return _quote(code, price, ts=ts, board=Board.INDEX, name="上证指数")


def _ep(dt: datetime) -> float:
    return dt.timestamp()


# ===========================================================================
# WP04 — EngineState core identity
# ===========================================================================

def test_stock_state_untouched_by_index_admission():
    """预载股票 `000001`，再准入指数 `sh000001` —— 股票各 map 必须不变。

    这正是云端 WP04 的场景。修前指数会被压成 `000001`，
    于是它**直接覆盖**平安银行的价格与历史。
    """
    st = EngineState()
    stock = _stock(11.0, ts=T0)
    st.update([stock], T0)
    assert "000001" in st.quotes
    stk_price = st.quotes["000001"].price
    stk_open = st.day_open["000001"]
    stk_first = st.first_seen["000001"].price
    stk_hist_len = len(st.history["000001"])

    st.update([_idx(3900.0, ts=T0 + timedelta(seconds=5))],
              T0 + timedelta(seconds=5))

    assert st.quotes["000001"].price == stk_price, (
        f"股票价格被指数覆盖：{stk_price} -> {st.quotes['000001'].price}")
    assert st.day_open["000001"] == stk_open, "股票 day_open 被覆盖"
    assert st.first_seen["000001"].price == stk_first, "股票 first_seen 被覆盖"
    assert len(st.history["000001"]) == stk_hist_len, (
        "股票 history 被指数写入了")


def test_index_state_is_independent():
    """指数必须**独立存在**于 `sh000001` 这个键下。"""
    st = EngineState()
    st.update([_stock(11.0, ts=T0)], T0)
    st.update([_idx(3900.0, ts=T0 + timedelta(seconds=5))],
              T0 + timedelta(seconds=5))

    assert "sh000001" in st.quotes, (
        f"指数必须独立存在于 'sh000001'，实测 keys={sorted(st.quotes)}")
    assert st.quotes["sh000001"].price == 3900.0
    assert st.quotes["sh000001"].board is Board.INDEX
    assert "sh000001" in st.history, "指数必须有独立 history"
    assert "sh000001" in st.day_open, "指数必须有独立 day_open"
    assert "sh000001" in st.first_seen, "指数必须有独立 first_seen"
    assert "sh000001" in st.last_price, "指数必须有独立 last_price"


def test_two_keys_coexist_after_both_admitted():
    """两个键必须**同时存在**，且各自价格正确。"""
    st = EngineState()
    st.update([_stock(11.0, ts=T0), _idx(3900.0, ts=T0)], T0)
    assert set(st.quotes) == {"000001", "sh000001"}, (
        f"两个键必须同时存在，实测 {sorted(st.quotes)}")
    assert st.quotes["000001"].price == 11.0
    assert st.quotes["sh000001"].price == 3900.0


def test_prune_keeps_both_identities():
    """prune 必须把两个身份**都**当作要保留的键（历史不得被误回收）。"""
    st = EngineState()
    st.update([_stock(11.0, ts=T0), _idx(3900.0, ts=T0)], T0)
    st.prune(keep_codes={"000001", "sh000001"})
    assert "000001" in st.history and "sh000001" in st.history, (
        f"prune 不得丢掉任一身份的历史，实测 {sorted(st.history)}")


def test_prune_drops_only_unlisted():
    """prune 仍要真的回收不在 keep 里的键（历史/水位线/首见表）。

    ⚠ `prune` 只清扫 `history` 及与之配套的派生表，
    **不**清扫 `quotes`（累计 latest 缓存）—— 这是既有语义。
    我第一版误以为它会清 `quotes`，写出了一条假失败的断言。
    """
    st = EngineState()
    st.update([_stock(11.0, ts=T0), _idx(3900.0, ts=T0)], T0)
    assert "000001" in st.history and "sh000001" in st.history, "前置条件"
    st.prune(keep_codes={"sh000001"})
    assert "000001" not in st.history, "未列出的股票历史应被回收"
    assert "000001" not in st.first_seen, "未列出的股票首见表应被回收"
    assert "sh000001" in st.history, "列出的指数历史必须保留"
    assert "sh000001" in st.first_seen, "列出的指数首见表必须保留"


# ===========================================================================
# WP05 — SpiritIndex 假信号负对照
# ===========================================================================

def test_index_window_does_not_inherit_stock_history():
    """**核心负对照**：预载平安银行 5 分钟历史后，第一条指数样本进入。

    指数的窗口必须**不够 5 分钟**（不得继承平安银行的历史）。
    修前两者共用 `000001`，指数会"已经有 5 分钟历史" -> 假信号。
    """
    st = EngineState()
    # 1) 预载 5 分钟平安银行历史
    for i in range(0, 301, 5):
        st.update([_stock(11.0 + i * 0.001, ts=T0 + timedelta(seconds=i))],
                  T0 + timedelta(seconds=i))

    # 前置条件：股票侧确实有 5 分钟历史（否则这条测试没有意义）
    stk_win = st.window("000001", 300, _ep(T0 + timedelta(seconds=300)))
    assert len(stk_win) >= 2, (
        f"前置条件不成立：股票窗口应有历史，实测 {len(stk_win)}")

    # 2) 第一条指数样本
    t_idx = T0 + timedelta(seconds=305)
    st.update([_idx(3900.0, ts=t_idx)], t_idx)

    # 3) 指数窗口不得继承股票历史
    idx_win = st.window("sh000001", 300, _ep(t_idx))
    assert len(idx_win) <= 1, (
        f"指数窗口不得继承平安银行的历史 —— 修前共用 '000001' 时会拿到 "
        f"股票的全部点。实测 {len(idx_win)} 点：{idx_win[:4]}")


def test_index_first_sample_yields_no_price_change():
    """指数第一条样本的 `price_change` 不得由股票历史算出。"""
    st = EngineState()
    for i in range(0, 301, 5):
        st.update([_stock(11.0 + i * 0.001, ts=T0 + timedelta(seconds=i))],
                  T0 + timedelta(seconds=i))
    t_idx = T0 + timedelta(seconds=305)
    st.update([_idx(3900.0, ts=t_idx)], t_idx)

    # price_change 对指数必须无可比较的前值（0 或未定义），
    # 绝不能是"从平安银行 11 元到 3900 元"这种跨证券涨幅。
    chg = st.price_change("sh000001", 300, _ep(t_idx))
    # `price_change` 在"窗口内不足两点"时返回 None（而非 0）——
    # 这本身就是"没有可比较的前值"的正确表达。
    assert chg is None or abs(chg) < 100.0, (
        f"指数首样本的 price_change 不得来自平安银行的历史，实测 {chg}")


def test_spirit_index_no_synthetic_alert_on_first_sample():
    """**负对照**：第一条指数样本不得产生指数类信号。

    走真实规则的 `_evaluate_one`（只喂最小 ctx），
    验证它不会因为撞车的历史报出拉升/打压。
    """
    from arad.models import Snapshot
    from arad.rules.spirit_index import SpiritIndexRule

    st = EngineState()
    for i in range(0, 301, 5):
        st.update([_stock(11.0 + i * 0.001, ts=T0 + timedelta(seconds=i))],
                  T0 + timedelta(seconds=i))
    t_idx = T0 + timedelta(seconds=305)
    idx_q = _idx(3900.0, ts=t_idx)
    st.update([idx_q], t_idx)

    rule = SpiritIndexRule({"enabled": True, "only_continuous": False,
                            "windows": [300], "bp_threshold": 10.0,
                            "points_threshold": 0.5})
    ctx = _MinCtx(state=st, now=t_idx)
    # 只给**本轮**的指数（current view）
    ctx.current_indices = {"sh000001": idx_q}
    snap = Snapshot(ts=t_idx, seq=0, quotes={})
    alerts = rule.evaluate(snap, ctx)
    assert not alerts, (
        f"第一条指数样本不得产生指数类信号，实测 "
        f"{[(getattr(a, 'type', None), getattr(a, 'code', None)) for a in alerts]}")


def test_spirit_index_fires_with_real_index_history():
    """**阳性对照**：真正积累足够指数历史后，规则必须能触发。

    否则上面那条就变成"永远不响"的假修复。指数从 3900 单调拉到 3990。
    """
    from arad.models import Snapshot
    from arad.rules.spirit_index import SpiritIndexRule

    st = EngineState()
    for i in range(0, 321, 5):
        p = 3900.0 + i * 0.3          # 5 分钟约 +2.3%
        st.update([_idx(p, ts=T0 + timedelta(seconds=i))],
                  T0 + timedelta(seconds=i))

    t_last = T0 + timedelta(seconds=320)
    last_q = _idx(3900.0 + 320 * 0.3, ts=t_last)
    rule = SpiritIndexRule({"enabled": True, "only_continuous": False,
                            "windows": [300], "bp_threshold": 10.0,
                            "points_threshold": 0.5})
    ctx = _MinCtx(state=st, now=t_last)
    ctx.current_indices = {"sh000001": last_q}
    snap = Snapshot(ts=t_last, seq=0, quotes={})
    alerts = rule.evaluate(snap, ctx)
    assert alerts, (
        "真正积累足够指数历史后必须能触发（否则是'永远不响'的假修复）")


class _MinCtx:
    """最小 RuleContext —— 只提供 spirit_index 需要的字段。"""

    def __init__(self, *, state, now):
        self.state = state
        self.now = now
        self.now_epoch = now.timestamp()
        self.cfg = {}
        self.session = "continuous"
        self.current_indices = None
