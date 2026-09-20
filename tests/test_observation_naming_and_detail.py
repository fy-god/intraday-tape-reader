"""IT-P1-OBS-006 / IT-P1-OBS-007 回归（由独立审计 13:39 提出，本机复核成立）。

OBS-006：``stale_rejected`` 装的是"未来拒绝数"（语义相反）
------------------------------------------------------------
字段名说"陈旧"，唯一数据来源却是 ``engine.py`` 的 ``t_reject:future`` 差分。
消费方 ``tools/live_session.py`` 早就把它输出成 ``future_rejected`` ——
说明"未来"才是真实语义。会让人误判"没有陈旧数据"。

修法：按实际语义改名为 ``future_rejected``；``stale_rejected`` 降级为
**已弃用别名属性**（返回同一值），不打断既有调用方。

同时必须说清：**当前不存在"陈旧拒绝"路径** —— ``_admit_time()`` 对任何早于
now 的 ``ts`` 都无条件接受，首见码甚至接受数天前的 ``ts``
（IT-P1-TIME-ROLE-003）。所以不要指望该字段能反映"数据太旧"。

OBS-007：观测账本在 Store 边界丢明细
-------------------------------------
``decisions`` 逐 (code, field, reason) 写了明细，但 ``as_dict()`` 只导出
``unavailable_capability`` 一个计数 —— 看不到**是哪只票、缺哪个字段**，
削弱 IT-P1-CAPABILITY-001 的可验收性。

修法：导出**有界样本**（最多 50 条）+ **按原因聚合的计数** + 截断标志。
既恢复可诊断性，又不让 Store/SSE 载荷无界。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fakes import make_quote

from arad.capabilities import (
    ObservationDecision, RoundObservationSet, SourceCapabilities,
)
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
    def __init__(self, name: str = "tencent", quotes=None, ok: bool = True):
        self.name = name
        self._quotes = quotes or []
        self.ok = ok

    def universe(self):
        return []

    def snapshots(self, codes):
        if not self.ok:
            raise RuntimeError("故意失败")
        want = set(codes)
        return [q for q in self._quotes if q.code in want]

    def health(self):
        return {"name": self.name, "ok": self.ok, "latency_ms": 0, "err": ""}


class _Cap:
    name = "capture"

    def __init__(self):
        self.obs = []

    def evaluate(self, snap, ctx):
        self.obs.append(getattr(ctx, "observation", None))
        return []


def _engine(quotes, codes):
    cap = _Cap()
    eng = Engine(source=SourceManager([_Src("tencent", quotes)], threshold=1),
                 settings=_settings(), rules=[cap], notifiers=[],
                 calendar=_cal(), now_fn=lambda: NOW,
                 store=AlertStore(_settings(), calendar=_cal()))
    eng._codes = list(codes)
    eng._codes_pinned = True
    eng.watchlist = []
    eng.refresh_universe = lambda *a, **k: 0
    return eng, cap


# ---------------------------------------------------------------------------
# OBS-006：字段改名 + 别名向后兼容
# ---------------------------------------------------------------------------
def test_future_rejected_is_the_real_field():
    far = NOW + timedelta(seconds=9999)
    quotes = [make_quote(code="600000", price=10.0, volume_lots=100.0, ts=NOW),
              make_quote(code="600001", price=10.0, volume_lots=100.0, ts=far)]
    eng, cap = _engine(quotes, ["600000", "600001"])
    eng.poll_once(force=True)

    obs = cap.obs[-1]
    assert obs.future_rejected == 1
    # WP04 / IT-P1-OBS-010：别名**不再**等于 future_rejected —— 那正是原缺陷
    # （两个互斥的桶永远同值，消费方无从分辨"太旧"还是"来自未来"）。
    # 别名现在指向**陈旧诊断**；本用例是"超前"，所以陈旧诊断应为 0。
    assert obs.provider_stale_diagnosed == 0
    assert obs.stale_rejected == obs.provider_stale_diagnosed
    assert obs.stale_rejected != obs.future_rejected, \
        "陈旧与超前是两个互斥的桶，别名不得再让它们同值"


def test_as_dict_prefers_new_name_but_keeps_old():
    obs = RoundObservationSet(
        source="tencent", capabilities=SourceCapabilities(source="tencent"),
        requested=1, returned=1, admitted=1, future_rejected=3,
        provider_stale_diagnosed=7)
    d = obs.as_dict()
    assert d["future_rejected"] == 3
    # WP04：陈旧是**诊断**计数，单独导出，与 future 不同值。
    assert d["provider_stale_diagnosed"] == 7
    assert obs.stale_rejected == 7, "弃用别名指向陈旧诊断"
    assert obs.stale_rejected != obs.future_rejected
    # schema note 必须说清"诊断不是拒绝"，避免又被误读成拒绝计数
    assert "不是" in d["_schema_note"]["provider_stale_diagnosed"]


def test_future_rejected_zero_when_all_fresh():
    quotes = [make_quote(code="600000", price=10.0, volume_lots=100.0, ts=NOW)]
    eng, cap = _engine(quotes, ["600000"])
    eng.poll_once(force=True)
    assert cap.obs[-1].future_rejected == 0


def test_status_exposes_future_rejected():
    far = NOW + timedelta(seconds=9999)
    quotes = [make_quote(code="600000", price=10.0, volume_lots=100.0, ts=far)]
    eng, cap = _engine(quotes, ["600000"])
    eng.poll_once(force=True)
    obs = eng.store.status()["observation"]
    assert obs["future_rejected"] == 1


def test_stale_alias_is_documented_as_deprecated():
    """别名必须自带"已弃用"说明，避免下一个人继续用错名。"""
    doc = RoundObservationSet.stale_rejected.__doc__ or ""
    assert "弃用" in doc or "deprecated" in doc.lower()
    assert "future_rejected" in doc


# ---------------------------------------------------------------------------
# OBS-007：Store 边界要带出"哪只票缺什么"
# ---------------------------------------------------------------------------
def test_as_dict_carries_bounded_sample():
    obs = RoundObservationSet(
        source="sina", capabilities=SourceCapabilities(source="sina"),
        requested=2, returned=2, admitted=2)
    obs.decisions.append(ObservationDecision(
        code="600000", status="unavailable_capability", rule="volume_burst",
        reason="turnover_not_provided", missing=("turnover",)))
    d = obs.as_dict()

    assert d["unavailable_sample"], "必须带出有界样本（OBS-007）"
    row = d["unavailable_sample"][0]
    assert row["code"] == "600000"
    assert "turnover" in row["missing"]
    assert row["reason"] == "turnover_not_provided"
    assert d["unavailable_by_reason"]["turnover_not_provided"] == 1
    assert d["decisions_truncated"] is False


def test_sample_is_bounded_to_50():
    """样本必须有界 —— 全市场一轮可达成千上万条，不能整份导出。"""
    obs = RoundObservationSet(
        source="sina", capabilities=SourceCapabilities(source="sina"),
        requested=200, returned=200, admitted=200)
    for i in range(200):
        code = f"6000{i:02d}"
        # 注意：计数来自 unavailable_codes（集合），明细来自 decisions。
        # 两者由规则分别写入 —— 这里必须都写，否则计数为 0。
        obs.unavailable_codes.add(code)
        obs.decisions.append(ObservationDecision(
            code=code, status="unavailable_capability",
            rule="volume_burst", reason="turnover_not_provided",
            missing=("turnover",)))
    d = obs.as_dict()
    assert len(d["unavailable_sample"]) == 50, "样本上限应为 50"
    assert d["decisions_truncated"] is True
    # 但聚合计数覆盖全部 200 条
    assert d["unavailable_by_reason"]["turnover_not_provided"] == 200
    assert d["unavailable_capability"] == 200


def test_sample_is_json_safe():
    import json
    obs = RoundObservationSet(
        source="sina", capabilities=SourceCapabilities(source="sina"),
        requested=1, returned=1, admitted=1)
    obs.decisions.append(ObservationDecision(
        code="600000", status="unavailable_capability", rule="volume_burst",
        reason="turnover_not_provided", missing=("turnover",)))
    json.dumps(obs.as_dict(), ensure_ascii=False)


def test_no_decisions_yields_empty_sample_not_error():
    obs = RoundObservationSet(source="tencent", requested=1, returned=1, admitted=1)
    d = obs.as_dict()
    assert d["unavailable_sample"] == []
    assert d["unavailable_by_reason"] == {}
    assert d["decisions_truncated"] is False


def test_store_status_carries_sample():
    """端到端：Store/status 必须能读到样本。"""
    quotes = [make_quote(code="600000", price=0.0, volume_lots=0.0, ts=NOW)]
    eng, cap = _engine(quotes, ["600000"])
    eng.poll_once(force=True)
    obs = eng.store.status()["observation"]
    assert "unavailable_sample" in obs
    assert "unavailable_by_reason" in obs


def test_live_session_exposes_sample_and_by_reason():
    """live_session 的轮样本也要带出这两项（否则报告里还是看不到）。"""
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "ls_obs67", root / "tools" / "live_session.py")
    assert spec and spec.loader
    ls = importlib.util.module_from_spec(spec)
    sys.modules["ls_obs67"] = ls
    spec.loader.exec_module(ls)

    s = ls.make_round_sample(
        index=1, latency_ms=1.0, alerts=(), error=False, quotes=1, universe=1,
        history_points=0, history_codes=0, max_deque=0, history_maxlen=360,
        watchlist_only=False,
        observation={"unavailable_sample": [{"code": "600000"}],
                     "unavailable_by_reason": {"turnover_not_provided": 7},
                     "future_rejected": 2})
    assert s["unavailable_sample"] == [{"code": "600000"}]
    assert s["unavailable_by_reason"] == {"turnover_not_provided": 7}
    assert s["future_rejected"] == 2


def test_live_session_reads_new_name_and_falls_back_to_old():
    """新名优先；只有旧键时也要能读到（兼容旧 Store 快照）。"""
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "ls_obs67b", root / "tools" / "live_session.py")
    ls = importlib.util.module_from_spec(spec)
    sys.modules["ls_obs67b"] = ls
    spec.loader.exec_module(ls)

    def sample(obs):
        return ls.make_round_sample(
            index=1, latency_ms=1.0, alerts=(), error=False, quotes=1, universe=1,
            history_points=0, history_codes=0, max_deque=0, history_maxlen=360,
            watchlist_only=False, observation=obs)

    assert sample({"future_rejected": 5})["future_rejected"] == 5
    assert sample({"stale_rejected": 5})["future_rejected"] == 5   # 旧键兜底
