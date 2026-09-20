"""IT-P1-TIME-ROLE-004 / 003-R1：route 级 source epoch（第二轮校验）。

上一轮（`100e06a`）加上了 source epoch，但**只由 `ROUTE_STOCKS` 驱动**，
而 `accepted_watermark` 是 `code -> ts` —— **同时服务股票和指数**。于是：

* **004-a 该接纳却被拒**：指数路由自己切源（个股没切）时不开 epoch，
  指数新源首包被旧水位线误杀；
* **004-b 该拒绝却放行**：个股切源会把**指数**的水位线一起清掉，
  指数那边真正的乱序就钻过去了；
* **003-R1 合同矛盾**：`source_time_contract.json` 对三源都写
  `role=unknown` / `freshness_allowed=false`，Engine 却直接用这些 provider ts
  做 4 小时 hard stale reject —— **实现与自己的合同矛盾**。本轮下调为
  "合同驱动"：`freshness_allowed=false` 时只做 age 诊断、不硬拒绝；
  硬拒绝能力按 `(route, epoch)` 保留，等某源拿到权威语义即可打开。
  （`begin_source_epoch` 清了 watermark 却不清 `first_seen` 的问题，
  在"受信任来源"路径下仍必须按 epoch 分账才能正确，已有专项用例。）

修法：水位线与"该 epoch 内是否首见"都必须按 **route** 分账，
`begin_source_epoch(route, source_tag)`，水位线键改为 `(route, code)`。
注意 `000001` 既是平安银行（个股）也是上证指数 —— 两条路由**会撞码**，
所以按 route 分账不只是为了切源，key 本身就必须带 route。

## 红测性质说明（如实交代）

涉及 `route=` 参数与 `(route, code)` 键的用例需要**新签名**，在旧代码上按
`TypeError` / `KeyError` 失败 —— 那是**结构性**红，只证明"接口不存在"。

真正有牙的是 `test_engine_*` 三条**行为级**用例：只用修复前就存在的 API
（`Engine` / `SourceManager` / `poll_once` / `RoundObservationSet` / `state.quotes`
/ `state.stats`），按真实触发条件驱动，在旧代码上按 **AssertionError** 失败。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fakes import make_quote

from arad.engine import (ROUTE_INDEX, ROUTE_STOCKS, Engine, EngineState,
                         SourceManager)
from arad.session import SessionPhase, TradingCalendar

NOW = datetime(2026, 9, 15, 10, 31, 0)

STOCKS = "stocks"
INDEX = "index"

# 指数必须写成**带前缀**形式：裸 000001 无法判断是上证指数还是平安银行，
# engine 会忽略并告警（见 _load_index_codes）。个股侧用裸 6 位。
IDX = "sh000001"        # 指数（上证指数）
STK = "000001"          # 个股（平安银行）—— 故意与指数同号，验证 key 带 route


def _ago(sec: float) -> datetime:
    return NOW - timedelta(seconds=sec)


def _q(code: str, ts, price: float = 10.0, vol: float = 100.0):
    return make_quote(code=code, price=price, volume_lots=vol, ts=ts,
                      prev_close=10.0, open=10.0, high=10.0, low=10.0)


def _cal():
    cal = TradingCalendar(holidays=set())
    cal.phase = lambda now=None: SessionPhase.MORNING      # type: ignore[method-assign]
    return cal


class _Src:
    """可控脚本源；个股脚本与指数脚本分开，便于两条路由不同步切源。

    注意 ``SourceManager.call`` **不会**把 ``route`` 转发给源方法（它只用于
    失败记账），所以源必须像真实实现那样**按代码前缀**区分指数与个股。
    """

    def __init__(self, name, stocks=None, index=None, *,
                 fail_stocks=False, fail_index=False):
        self.name = name
        self.stocks = stocks or {}
        self.index = index or {}
        self.fail_stocks = fail_stocks
        self.fail_index = fail_index

    def universe(self):
        return []

    def snapshots(self, codes):
        out = []
        for c in codes:
            is_index = str(c)[:2].lower() in ("sh", "sz", "bj")
            if is_index:
                if self.fail_index:
                    raise RuntimeError(f"{self.name} index fail")
                table = self.index
            else:
                if self.fail_stocks:
                    raise RuntimeError(f"{self.name} stocks fail")
                table = self.stocks
            rows = table.get(c)
            if rows:
                out.append(rows.pop(0))
        return out

    def health(self):
        return {"name": self.name, "ok": True, "latency_ms": 0, "err": ""}


# ===========================================================================
# 004-a —— 指数路由切源：指数新源首包必须被接纳
# ===========================================================================
def test_index_route_gets_its_own_epoch():
    st = EngineState(history_len=360)
    st.begin_source_epoch(INDEX, "tencent#0")
    st.update([_q(IDX, _ago(20), price=3000.0)], NOW, route=INDEX)
    key = (INDEX, IDX)
    assert abs(st.accepted_watermark_by_route[key] - _ago(20).timestamp()) < 1e-6

    st.begin_source_epoch(INDEX, "sina#1")                  # 只有指数切源
    admitted = st.update([_q(IDX, _ago(55), price=3050.0)], NOW, route=INDEX)
    assert IDX in admitted, (
        "指数新源首包被旧 epoch 的水位线误杀 —— 指数路由没开自己的 epoch")


def test_stocks_switch_does_not_clear_index_watermark():
    """个股切源**不得**清掉指数的水位线（当前实现会清）。"""
    st = EngineState(history_len=360)
    st.begin_source_epoch(STOCKS, "tencent#0")
    st.begin_source_epoch(INDEX, "tencent#0")
    st.update([_q(IDX, _ago(20), price=3000.0)], NOW, route=INDEX)
    before = st.accepted_watermark_by_route[(INDEX, IDX)]

    st.begin_source_epoch(STOCKS, "sina#1")                 # 只有个股切源
    after = st.accepted_watermark_by_route.get((INDEX, IDX))
    assert after == before, (
        "个股切源把指数水位线一起清了 —— 指数真实乱序会钻过去")


def test_index_out_of_order_still_rejected_after_stocks_switch():
    """004-b：个股切源后，指数路由内的真实乱序**仍必须被拒**。"""
    st = EngineState(history_len=360)
    st.begin_source_epoch(STOCKS, "tencent#0")
    st.begin_source_epoch(INDEX, "tencent#0")
    st.update([_q(IDX, _ago(20), price=3000.0)], NOW, route=INDEX)

    st.begin_source_epoch(STOCKS, "sina#1")                 # 个股切源
    admitted = st.update([_q(IDX, _ago(90), price=2990.0)], NOW, route=INDEX)
    assert IDX not in admitted, (
        "个股切源把指数水位线清掉了，指数真实乱序被放行")


def test_code_collision_across_routes_is_isolated():
    """000001 同时是指数与个股 —— 两条路由的水位线必须互不干扰。"""
    st = EngineState(history_len=360)
    st.begin_source_epoch(STOCKS, "tencent#0")
    st.begin_source_epoch(INDEX, "tencent#0")
    st.update([_q(STK, _ago(20), price=10.5)], NOW, route=STOCKS)
    st.update([_q(IDX, _ago(20), price=3000.0)], NOW, route=INDEX)
    assert (STOCKS, STK) in st.accepted_watermark_by_route
    assert (INDEX, IDX) in st.accepted_watermark_by_route


def test_a_to_b_to_a_per_route():
    """A→B→A 在每条路由上都要形成新 epoch。"""
    st = EngineState(history_len=360)
    for r in (STOCKS, INDEX):
        st.begin_source_epoch(r, "A#0")
    st.update([_q(IDX, _ago(20), price=3000.0)], NOW, route=INDEX)
    st.update([_q(STK, _ago(20), price=10.5)], NOW, route=STOCKS)

    for r in (STOCKS, INDEX):
        st.begin_source_epoch(r, "B#1")
    assert IDX in st.update([_q(IDX, _ago(55), price=3050.0)], NOW, route=INDEX)
    assert STK in st.update([_q(STK, _ago(55), price=10.6)], NOW, route=STOCKS)

    for r in (STOCKS, INDEX):
        st.begin_source_epoch(r, "A#0")
    assert IDX in st.update([_q(IDX, _ago(10), price=3060.0)], NOW, route=INDEX)
    assert STK in st.update([_q(STK, _ago(10), price=10.7)], NOW, route=STOCKS)


def test_same_route_same_source_is_idempotent():
    """同一路由同一来源重复上报必须幂等（epoch 内乱序闸门不能被清）。"""
    st = EngineState(history_len=360)
    st.begin_source_epoch(STOCKS, "tencent#0")
    st.update([_q(STK, _ago(20), price=10.5)], NOW, route=STOCKS)
    st.begin_source_epoch(STOCKS, "tencent#0")              # 同源，幂等
    admitted = st.update([_q(STK, _ago(90), price=10.4)], NOW, route=STOCKS)
    assert STK not in admitted, "重复 begin 清掉了水位线，epoch 内乱序不再被拒"


# ===========================================================================
# 003-R1 —— 陈旧判定：合同驱动（本轮**下调**上一轮的硬拒绝）
# ===========================================================================
def test_stale_first_packet_is_admitted_under_unknown_contract():
    """**行为级**：合同三源 role=unknown / freshness_allowed=false，

    因此 provider ts **不得**被用来做 hard stale reject（WP03）。
    3 天前的首包按新合同应被**接纳**，只留 age 诊断。

    这条是本轮对上一轮的**下调**：上一轮 `100e06a` 无条件硬拒绝，
    与自己的 `source_time_contract.json` 矛盾。

    行为级 —— 只看 ``quotes``（修复前就有的）：旧代码会拒绝 -> 红。
    """
    st = EngineState(history_len=360)
    st.begin_source_epoch(STOCKS, "tencent#0")
    st.update([_q(STK, NOW - timedelta(days=3), price=99.9)], NOW, route=STOCKS)
    assert STK in st.quotes, (
        "合同 freshness_allowed=false 时不该硬拒绝 provider ts —— "
        "上一轮的 4 小时硬拒绝与自己的合同矛盾")


def test_stale_is_diagnosed_even_when_not_rejected():
    """不拦也必须留痕：否则"该源时间是否可信"完全无从观察。"""
    st = EngineState(history_len=360)
    st.begin_source_epoch(STOCKS, "tencent#0")
    st.update([_q(STK, NOW - timedelta(days=3), price=99.9)], NOW, route=STOCKS)
    assert st.time_age_seconds.get(STK, 0) > 4 * 3600, "必须记录 age 诊断"
    assert st.stale_diagnosed.get(STOCKS, 0) >= 1, "必须记录陈旧诊断计数"


def test_stale_rejected_per_route_epoch_when_freshness_trusted():
    """**结构性**：若某源将来拿到权威时间语义，硬拒绝能力必须已就绪，

    且必须按 **(route, epoch)** 分账 —— 不能沿用全局 `first_seen`：
    切源后同码在新 epoch 重新算"首见"，陈旧包必须**再次**被拒。

    需要新 API（``source_name`` / ``TIME_POLICY``），在旧代码上按
    TypeError/KeyError 失败 —— 属结构性红，不谎称行为级。
    """
    from arad import engine as eng_mod

    saved = dict(eng_mod.TIME_POLICY.get("tencent", {}))
    eng_mod.TIME_POLICY["tencent"] = {"freshness_allowed": True,
                                      "strict_ordering_allowed": True}
    try:
        st = EngineState(history_len=360)
        st.begin_source_epoch(STOCKS, "tencent#0", source_name="tencent")
        old = NOW - timedelta(days=3)
        assert STK not in st.update([_q(STK, old, price=99.9)], NOW,
                                    route=STOCKS), "受信任来源的陈旧首包应被拒"
        assert st.stats.get("t_reject:stale", 0) >= 1

        # 换 epoch：该码在新 epoch 重新算"首见"，陈旧包必须**再次**被拒。
        # 若沿用全局 first_seen，这里会漏放（IT-P1-TIME-ROLE-003-R1）。
        st.begin_source_epoch(STOCKS, "sina#1", source_name="tencent")
        assert STK not in st.update([_q(STK, old, price=99.9)], NOW,
                                    route=STOCKS), \
            "新 epoch 的首包绕过了陈旧下限 —— first_seen 没随 epoch 重置"
    finally:
        eng_mod.TIME_POLICY["tencent"] = saved


def test_fresh_first_packet_in_new_epoch_is_admitted():
    """回归保护：新 epoch 里正常的较旧首包必须被接受。"""
    st = EngineState(history_len=360)
    st.begin_source_epoch(STOCKS, "tencent#0")
    st.update([_q(STK, _ago(20), price=10.5)], NOW, route=STOCKS)
    st.begin_source_epoch(STOCKS, "sina#1")
    assert STK in st.update([_q(STK, _ago(55), price=10.6)], NOW, route=STOCKS)


# ===========================================================================
# 端到端（**行为级**：只用修复前就存在的 API）
# ===========================================================================
def _engine(a, b, seen):
    from arad.config import load_settings
    from arad.store import AlertStore

    class _Cap:
        name = "capture"
        #: 声明需要指数 —— Engine._wants_indices 是只读 property，
        #: 由规则的这个属性推导，不能直接赋值。
        wants_indices = True

        def evaluate(self, snap, ctx):
            seen.append(getattr(ctx, "observation", None))
            return []

    settings = load_settings(use_cache=False)
    cal = _cal()
    # threshold=2：单次失败只进热备（serving 切到备用源），不正式换主源。
    # 这正是 004 的场景 —— 某条 route 的 serving source 变了，另一条没变。
    eng = Engine(source=SourceManager([a, b], threshold=2), settings=settings,
                 rules=[_Cap()], notifiers=[], calendar=cal, now_fn=lambda: NOW,
                 store=AlertStore(settings, calendar=cal))
    eng._codes = [STK]
    eng._codes_pinned = True
    eng.watchlist = []
    eng.refresh_universe = lambda *x, **k: 0
    eng.index_codes = [IDX]
    return eng


def test_engine_index_epoch_opens_when_index_route_switches():
    """004-a 端到端：**只有指数**路由切源时，指数新源首包必须被接纳。

    场景：R1 指数由 tencent 供 10:30:40（水位线建立）；R2 指数在 tencent 上
    失败、改由 sina 热备，而 sina 首包 10:30:05 更旧。
    旧代码：epoch 只跟个股走（个股仍是 tencent），指数不开新 epoch，水位线
            仍是 10:30:40 → 10:30:05 被当乱序丢掉 → index_admitted=0。
    新代码：指数路由开自己的 epoch，水位线重新起算 → 接纳。

    行为级 —— 只看 ``RoundObservationSet.index_admitted``（修复前就有的字段）。
    """
    a = _Src("tencent",
             {STK: [_q(STK, _ago(20), price=10.5), _q(STK, _ago(20), price=10.5)]},
             {IDX: [_q(IDX, _ago(20), price=3000.0)]})
    b = _Src("sina",
             {STK: [_q(STK, _ago(20), price=10.5), _q(STK, _ago(20), price=10.5)]},
             {IDX: [_q(IDX, _ago(55), price=3050.0)]})
    seen = []
    eng = _engine(a, b, seen)

    eng.poll_once(force=True)
    assert seen[-1].index_admitted == 1, "R1：指数首包（tencent）应被接受"

    a.fail_index = True                        # **只有指数**路由切源
    eng.poll_once(force=True)
    obs = seen[-1]
    # 用 serving_of 判断谁切了源（修复前就有的 API），**不要**引用新符号，
    # 否则本用例会退化成结构性红（见下方 004-b 的教训）。
    assert eng.sources.serving_of(ROUTE_STOCKS) == 0, \
        "个股路由不该切源（本场景只切指数）"
    assert eng.sources.serving_of(ROUTE_INDEX) == 1, "指数路由应已切到 sina"
    assert obs.index_requested >= 1, "R2：应请求了指数"
    assert obs.index_admitted >= 1, (
        "指数路由切源后数据被丢（指数没开自己的 source epoch）"
        f"：index_admitted={obs.index_admitted}")


def test_engine_stocks_switch_does_not_admit_index_out_of_order():
    """004-b 端到端：**只有个股**切源时，指数路由内的真实乱序不得被放行。

    场景：R1 两条路由都由 tencent 供，指数水位线 = 10:30:40；R2 个股在 tencent
    上失败、改由 sina 热备，指数仍由 tencent 供，但这一包 ts=10:29:01 是
    **真实倒退**，必须被拒。
    旧代码：个股切源会清掉**所有**水位线（含指数）→ 指数乱序被放行。
    新代码：按 route 分账，指数水位线不受影响 → 仍被拒。

    行为级 —— 只看 ``index_admitted``。
    """
    a = _Src("tencent", {STK: [_q(STK, _ago(20), price=10.5)]},
             {IDX: [_q(IDX, _ago(20), price=3000.0),
                    _q(IDX, _ago(90), price=2990.0)]})   # R2 指数包：真实倒退
    b = _Src("sina", {STK: [_q(STK, _ago(20), price=10.6)]}, {})
    seen = []
    eng = _engine(a, b, seen)

    eng.poll_once(force=True)
    assert seen[-1].index_admitted == 1, "R1：指数首包应被接受"

    a.fail_stocks = True                       # **只有个股**路由切源
    eng.poll_once(force=True)
    obs = seen[-1]
    # 只看修复前就有的 serving_of，不引用新符号（否则退化成结构性红）。
    assert eng.sources.serving_of(ROUTE_STOCKS) == 1, "个股路由应已切到 sina"
    assert eng.sources.serving_of(ROUTE_INDEX) == 0, "指数路由应仍是 tencent"
    assert obs.index_admitted == 0, (
        "个股切源把指数水位线清掉了，指数真实乱序被放行"
        f"（index_admitted={obs.index_admitted}，应为 0）")


def test_engine_stale_first_packet_in_new_epoch_is_admitted():
    """003-R1 端到端（**本轮下调**）：合同禁止用 provider ts 硬判新鲜度，

    所以切源后 3 天前的首包按新合同应被**接纳**（只记诊断）。
    上一轮 `100e06a` 会硬拒绝 —— 那是与 `source_time_contract.json`
    （三源 ``freshness_allowed=false``）矛盾的。

    行为级 —— 只看 ``state.quotes``（修复前就有的）。
    """
    a = _Src("tencent", {STK: [_q(STK, _ago(20), price=10.5)]})
    b = _Src("sina", {STK: [_q(STK, NOW - timedelta(days=3), price=99.9)]})
    seen = []
    eng = _engine(a, b, seen)

    eng.poll_once(force=True)
    assert eng.state.quotes[STK].price == 10.5

    a.fail_stocks = True                       # 切源 -> 新 epoch
    eng.poll_once(force=True)
    # 新合同下数据被接纳（这正是"不拦"的含义），但必须留下 age 诊断。
    assert STK in eng.state.quotes, "按新合同陈旧包应被接纳（不硬拒绝）"
    assert eng.state.time_age_seconds.get(STK, 0) > 4 * 3600, \
        "接纳的同时必须留下 age 诊断，否则无从观察该源时间是否可信"
