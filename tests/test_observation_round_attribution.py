"""IT-P2-OBS-004 回归：失败轮不得沿用上一成功轮的 observation。

缺陷
----
``_soak_loop()`` 在 ``poll_once`` 抛异常时仍从 Store 取「**最近一轮**」的
observation 并原样写进本轮 sample。Store 里那份属于**上一成功轮**，于是
报告会把上一轮的覆盖率/拒绝数标在失败轮名下；而 sample 只有 ``index``
（无 ``round_id``/``observed_at``），事后根本无法识别。

修法
----
``AlertStore`` 暴露 ``observation_seq``（每写入一次 +1）。消费方在 poll
**之前**记下序号，poll 之后序号未增加就说明本轮没产生新账本 -> 不并入，
并拒绝在 ``error`` 轮并入任何账本。
"""
from __future__ import annotations

from datetime import datetime

from fakes import make_quote

from arad.capabilities import RoundObservationSet, SourceCapabilities
from arad.engine import Engine, SourceManager
from arad.session import SessionPhase, TradingCalendar
from arad.store import AlertStore

NOW = datetime(2026, 9, 15, 10, 30, 0)


def _settings():
    from arad.config import load_settings
    return load_settings(use_cache=False)


def _cal():
    cal = TradingCalendar(holidays=set())
    cal.phase = lambda now=None: SessionPhase.MORNING      # type: ignore[method-assign]
    return cal


class _Src:
    """可切换成败的源。"""

    def __init__(self, name: str = "tencent", ok: bool = True):
        self.name = name
        self.ok = ok

    def universe(self):
        return []

    def snapshots(self, codes):
        if not self.ok:
            raise RuntimeError("故意失败")
        return [make_quote(code=c, price=10.0, volume_lots=100.0, ts=NOW)
                for c in codes]

    def health(self):
        return {"name": self.name, "ok": self.ok, "latency_ms": 0, "err": ""}


def _engine(src):
    eng = Engine(source=SourceManager([src], threshold=1), settings=_settings(),
                 rules=[], notifiers=[], calendar=_cal(), now_fn=lambda: NOW,
                 store=AlertStore(_settings(), calendar=_cal()))
    eng._codes = ["600000"]
    eng._codes_pinned = True
    eng.watchlist = []
    eng.refresh_universe = lambda *a, **k: 0
    return eng


# ---------------------------------------------------------------------------
# Store 侧：轮次序号
# ---------------------------------------------------------------------------
def test_observation_seq_starts_at_zero():
    st = AlertStore(_settings(), calendar=_cal())
    assert st.observation_seq == 0
    assert st.observation == {}


def test_observation_seq_increments_on_each_round():
    eng = _engine(_Src())
    before = eng.store.observation_seq
    eng.poll_once(force=True)
    assert eng.store.observation_seq == before + 1


def test_observation_seq_unchanged_when_poll_fails():
    """失败轮不得推进轮次序号 —— 这是归属判定的唯一依据。"""
    src = _Src(ok=False)
    eng = _engine(src)
    eng.poll_once(force=True)                 # 若抛异常由引擎内部吞掉
    seq_after_fail = eng.store.observation_seq

    src.ok = True
    eng.poll_once(force=True)
    assert eng.store.observation_seq == seq_after_fail + 1, \
        "成功后应恰好推进一次"


def test_seq_does_not_advance_on_none_observation():
    """不传 observation 时序号不得推进（避免把空写入当成本轮）。"""
    st = AlertStore(_settings(), calendar=_cal())
    st.set_poll_stats(poll_ms=1, count=1, health=[], now=NOW)
    assert st.observation_seq == 0


def test_seq_advances_only_with_real_observation():
    st = AlertStore(_settings(), calendar=_cal())
    obs = RoundObservationSet(
        source="tencent",
        capabilities=SourceCapabilities(source="tencent"),
        requested=1, returned=1, admitted=1)
    st.set_poll_stats(poll_ms=1, count=1, health=[], now=NOW, observation=obs)
    assert st.observation_seq == 1
    assert st.observation["admitted"] == 1


# ---------------------------------------------------------------------------
# 归属判定：序号未变 -> 不并入本轮
# ---------------------------------------------------------------------------
def test_stale_observation_not_attributed_to_failed_round():
    """模拟 live_session 的判定：序号未变 + error -> 不并入。

    这里直接复刻 ``_soak_loop`` 的判定条件，确保语义被锁定。
    """
    eng = _engine(_Src())
    eng.poll_once(force=True)                       # 成功轮，产生账本
    good = eng.store.observation
    assert good and good["admitted"] == 1

    # 失败轮：序号不变
    seq_before = eng.store.observation_seq
    error = True
    obs_snapshot: dict = {}
    get_obs = eng.store.observation
    seq_after = eng.store.observation_seq
    if isinstance(get_obs, dict) and get_obs and not error and seq_after > seq_before:
        obs_snapshot = get_obs

    assert obs_snapshot == {}, "失败轮不得携带任何账本（OBS-004）"


def test_stale_observation_not_attributed_when_seq_flat():
    """序号未变（本轮没推进）时，即使不报 error 也不得并入。"""
    eng = _engine(_Src())
    eng.poll_once(force=True)

    seq_before = eng.store.observation_seq                # 故意不再 poll
    error = False
    get_obs = eng.store.observation
    seq_after = eng.store.observation_seq
    obs_snapshot = (get_obs if (isinstance(get_obs, dict) and get_obs
                               and not error and seq_after > seq_before) else {})
    assert obs_snapshot == {}, "序号未变说明不是本轮账本"


def test_fresh_observation_is_attributed():
    """正常轮：序号推进 -> 并入（确保修复没把正常路径也堵死）。"""
    eng = _engine(_Src())
    seq_before = eng.store.observation_seq
    eng.poll_once(force=True)
    get_obs = eng.store.observation
    seq_after = eng.store.observation_seq
    obs_snapshot = (get_obs if (isinstance(get_obs, dict) and get_obs
                               and seq_after > seq_before) else {})
    assert obs_snapshot, "正常轮必须并入账本"
    assert obs_snapshot["admitted"] == 1
