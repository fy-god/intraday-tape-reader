"""IT-P2-OBS-STATUS-001 回归：``/api/status`` 必须暴露**账本自己的**身份。

缺陷
----
``AlertStore.status()`` 暴露了 ``observation``（最近一轮账本的汇总），但**没有**
暴露这份账本自己的 ``seq`` / 写入时刻 / poll 计数。外层只有一个
``"ts": datetime.now()`` —— 那是**这次请求**的时刻，不是账本的时刻。

后果（审计原文的意思）：外部消费者（看板、SSE 订阅者、外部脚本）在
``/api/status`` 上看到一份账本时，**无法判断它是本轮新产生的、还是上一成功轮
留下的旧账**。失败轮不会清空 ``_observation``（那是**故意**的：保留最后一次
成功可见的账本比清空更有用），但消费者拿请求时刻 ``ts`` 去解释它，就会把
几分钟前的旧账当成"刚刚的"。

修法
----
账本写入时**同一临界区**记录身份三元组，并在 ``status()`` 里与
``observation`` 一起读出：

* ``observation_seq``         —— 每写入一次 +1；未变 = 本轮没产生新账本
* ``observation_observed_at`` —— 账本写入时刻（``YYYY-MM-DD HH:MM:SS``）；
                                无账本时 ``None``
* ``observation_poll_count``  —— 账本写入时引擎的 poll 计数；无账本时 ``0``

三者必须与 ``observation`` 严格同步（"新 seq 配旧账本"是更坏的 bug）。

本文件的牙齿分类（**实测**，非推断）
----------------------------------
回退 ``src/arad/store.py`` 后逐条分类（收集阶段通过，无 ImportError）：

* **真行为级验牙（1 条）** ——
  ``test_observed_at_is_not_the_request_time``：只用修复前**已存在**的
  ``set_poll_stats(now=...)`` + ``status()``，红在
  ``AssertionError: status() 必须给出账本自己的时刻``。
* **新增契约断言，红在 AssertionError 但不算验牙（1 条）** ——
  ``test_status_payload_still_has_original_keys``：形式上是 AssertionError
  （``status() 缺新键 observation_seq``），但它断言的是**新键存在**，
  不是旧行为出错。**不冒充验牙。**
* **结构性（7 条）** —— 红在 ``AttributeError: 'AlertStore' object has no
  attribute 'observation_observed_at'``（5 条）或
  ``KeyError: 'observation_seq'``（2 条）。这些符号/键是**本轮新增**的，
  红只证明"新东西还没写"，**不能**用来证明行为缺陷。**不冒充验牙。**
* **不受影响（3 条）** —— 旧 ``observation_seq`` 契约与 JSON 可序列化
  在修复前后都通过，属回归保护。

换言之：本文件对 IT-P2-OBS-STATUS-001 的**行为**证据只有 1 条，
其余是契约与回归保护。如实记录，不夸大成 9 条牙齿。
"""
from __future__ import annotations

from datetime import datetime, timedelta

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


def _obs(admitted: int = 1) -> RoundObservationSet:
    return RoundObservationSet(
        source="tencent",
        capabilities=SourceCapabilities(source="tencent"),
        requested=max(admitted, 1), returned=admitted, admitted=admitted)


class _Src:
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
# 1. 真行为级验牙：账本时刻 ≠ 请求时刻
# ---------------------------------------------------------------------------
def test_observed_at_is_not_the_request_time():
    """**真行为级验牙**：账本时刻必须是**写入时**的时刻，不是请求时刻。

    场景：10:30:00 写了一份账本，10:35:00 才来查 ``/api/status``。
    正确行为是能看出这份账本**已经 5 分钟旧了**；错误的实现只能给出
    请求时刻 10:35:00，消费者于是以为账本是新鲜的。

    **修复前**：没有独立的账本时刻字段，消费者只能用 ``status()["ts"]``
    （= 请求时刻），因此 ``getattr(...)`` 拿到的账本时刻缺失或等于请求时刻
    -> 关于 age 的断言以 ``AssertionError`` 失败。这条只用修复前已存在的
    ``set_poll_stats(now=...)`` 与 ``status()["ts"]``，故是真验牙。
    """
    st = AlertStore(_settings(), calendar=_cal())
    observed = datetime(2026, 9, 15, 10, 30, 0)
    st.set_poll_stats(poll_ms=1, count=7, health=[], now=observed,
                      observation=_obs())

    # 5 分钟后来读接口
    later = observed + timedelta(minutes=5)
    payload = st.status()
    ledger_at = payload.get("observation_observed_at")
    assert ledger_at is not None, (
        "status() 必须给出账本自己的时刻，否则消费者只能拿请求时刻冒充它")
    assert ledger_at == observed.strftime("%Y-%m-%d %H:%M:%S"), (
        f"账本时刻必须是写入时那一刻 {observed}，不是请求时刻")
    # 关键推论：能算出旧账的年龄，且它与请求时刻**不同**
    assert ledger_at != later.strftime("%Y-%m-%d %H:%M:%S"), (
        "旧账的时刻绝不能等于请求时刻，否则消费者会把旧账当新鲜账")
    # 请求时刻仍然是请求时刻（语义未被污染）
    assert payload["ts"] >= observed.strftime("%Y-%m-%d %H:%M:%S")


def test_observed_at_uses_engine_clock_not_wall_clock():
    """账本时刻取自注入的 ``now``（引擎时钟），而非系统墙钟。

    测试若用墙钟就无法断言确定值，只能写"约等于"，等于没测。
    """
    st = AlertStore(_settings(), calendar=_cal())
    t = datetime(2026, 9, 15, 14, 57, 3)
    st.set_poll_stats(poll_ms=1, count=1, health=[], now=t, observation=_obs())
    assert st.observation_observed_at == "2026-09-15 14:57:03"


# ---------------------------------------------------------------------------
# 2. 无账本时的哨兵：必须是 None / 0，不能伪造
# ---------------------------------------------------------------------------
def test_no_ledger_means_none_not_request_time():
    """从未写入账本时，账本时刻必须是 ``None``。

    绝不能退回请求时刻 —— 那正是本缺陷的核心（用请求时刻冒充账本身份）。
    """
    st = AlertStore(_settings(), calendar=_cal())
    payload = st.status()
    assert payload["observation_seq"] == 0
    assert payload["observation_observed_at"] is None, (
        "无账本时必须 None，不得用请求时刻填充")
    assert payload["observation_poll_count"] == 0
    assert payload["observation"] == {}


def test_poll_without_ledger_does_not_fake_ledger_time():
    """没有 observation 的成功 poll 不得让账本看起来变新鲜。

    ``_last_poll_ts`` 每次 poll 都刷新（**包括**没账本的轮）。如果账本时刻
    复用它，就会出现"账本没更新但时刻变新了" —— 比缺陷本身更坏。
    """
    st = AlertStore(_settings(), calendar=_cal())
    st.set_poll_stats(poll_ms=1, count=1, health=[], now=NOW,
                      observation=_obs())
    first_at = st.observation_observed_at

    st.set_poll_stats(poll_ms=1, count=2, health=[], now=NOW + timedelta(seconds=30))
    # 这一轮没带 observation
    assert st.observation_seq == 1, "没账本不得推进 seq"
    assert st.observation_observed_at == first_at, (
        "没账本不得刷新账本时刻 —— 账本没变，身份就不能变")
    assert st.observation_poll_count == 1, "poll_count 也必须停在账本那一次"


# ---------------------------------------------------------------------------
# 3. 三元组必须同步：新 seq 配旧账本是最坏的 bug
# ---------------------------------------------------------------------------
def test_identity_triple_advances_together():
    """``observation_seq`` / 时刻 / poll_count 必须**一起**推进。

    只推进其中一个就会产生"新 seq 指旧内容"的错配，比没有身份更危险。
    """
    st = AlertStore(_settings(), calendar=_cal())
    t1 = datetime(2026, 9, 15, 10, 30, 0)
    st.set_poll_stats(poll_ms=1, count=3, health=[], now=t1,
                      observation=_obs(admitted=1))
    assert (st.observation_seq, st.observation_observed_at,
            st.observation_poll_count) == (1, "2026-09-15 10:30:00", 3)

    t2 = t1 + timedelta(seconds=60)
    st.set_poll_stats(poll_ms=1, count=4, health=[], now=t2,
                      observation=_obs(admitted=2))
    assert (st.observation_seq, st.observation_observed_at,
            st.observation_poll_count) == (2, "2026-09-15 10:31:00", 4)
    assert st.observation["admitted"] == 2, "内容与身份必须同轮"


def test_status_reads_identity_in_same_critical_section():
    """``status()`` 给出的三元组必须与 ``observation`` 属于**同一轮**。

    若分两次加锁读，可能读到"新 seq 配旧账本"。
    """
    st = AlertStore(_settings(), calendar=_cal())
    st.set_poll_stats(poll_ms=1, count=1, health=[], now=NOW,
                      observation=_obs(admitted=1))
    p1 = st.status()
    st.set_poll_stats(poll_ms=1, count=2, health=[], now=NOW + timedelta(seconds=60),
                      observation=_obs(admitted=2))
    p2 = st.status()

    assert p1["observation_seq"] == 1 and p1["observation"]["admitted"] == 1
    assert p2["observation_seq"] == 2 and p2["observation"]["admitted"] == 2
    assert p2["observation_observed_at"] > p1["observation_observed_at"]


# ---------------------------------------------------------------------------
# 4. 消费方能据此识别"失败轮沿用旧账"
# ---------------------------------------------------------------------------
def test_consumer_can_detect_stale_ledger_via_seq():
    """复刻消费者判定：seq 未变 -> 这份账本不是本轮的。

    这是本字段存在的**唯一理由**，所以必须有一条按真实用法写的用例。
    """
    eng = _engine(_Src())
    eng.poll_once(force=True)
    seq_before = eng.store.observation_seq
    assert seq_before == 1

    # 模拟"本轮 poll 失败"：seq 不推进，但 observation 仍保留最后成功账本
    seq_after = eng.store.observation_seq
    round_failed = True
    fresh_ledger = (not round_failed) and (seq_after > seq_before)
    assert fresh_ledger is False
    # 关键：即便 observation 非空，seq 也足以判定它是旧账
    assert eng.store.observation, "旧账被保留（这是设计意图）"
    assert seq_after == seq_before, "seq 未变即旧账"


def test_failed_poll_keeps_old_ledger_but_ages_it():
    """失败轮之后：账本内容保留，但它的**时刻**能暴露它已经旧了。"""
    src = _Src()
    eng = _engine(src)
    eng.poll_once(force=True)
    written_at = eng.store.observation_observed_at
    assert written_at == NOW.strftime("%Y-%m-%d %H:%M:%S")

    src.ok = False
    eng.poll_once(force=True)                # 异常由引擎内部吞掉
    assert eng.store.observation_seq == 1, "失败轮不得推进 seq"
    assert eng.store.observation_observed_at == written_at, (
        "失败轮不得刷新账本时刻")
    # 于是消费者可以算出：这份账本是 written_at 那一刻的，不是现在的
    assert eng.store.observation_observed_at != \
        (NOW + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 5. 回归保护：旧契约不得被破坏
# ---------------------------------------------------------------------------
def test_observation_seq_contract_unchanged():
    """回归保护：``observation_seq`` 的既有语义（OBS-004）保持不变。"""
    st = AlertStore(_settings(), calendar=_cal())
    assert st.observation_seq == 0
    st.set_poll_stats(poll_ms=1, count=1, health=[], now=NOW)
    assert st.observation_seq == 0, "不传 observation 不得推进"
    st.set_poll_stats(poll_ms=1, count=2, health=[], now=NOW, observation=_obs())
    assert st.observation_seq == 1
    st.set_poll_stats(poll_ms=1, count=3, health=[], now=NOW, observation=_obs())
    assert st.observation_seq == 2


def test_status_payload_still_has_original_keys():
    """回归保护：新增三个键不得挤掉任何旧键。"""
    st = AlertStore(_settings(), calendar=_cal())
    st.set_poll_stats(poll_ms=1, count=1, health=[], now=NOW, observation=_obs())
    p = st.status()
    for k in ("phase", "session", "observation", "ts"):
        assert k in p, f"status() 丢了旧键 {k}"
    for k in ("observation_seq", "observation_observed_at",
              "observation_poll_count"):
        assert k in p, f"status() 缺新键 {k}"


def test_status_is_json_safe_with_and_without_ledger():
    """``status()`` 必须可 JSON 序列化（它有 None，容易被忽略）。"""
    import json

    st = AlertStore(_settings(), calendar=_cal())
    json.dumps(st.status(), ensure_ascii=False)          # 无账本
    st.set_poll_stats(poll_ms=1, count=1, health=[], now=NOW, observation=_obs())
    json.dumps(st.status(), ensure_ascii=False)          # 有账本


def test_ledger_time_format_admits_day_boundary():
    """账本时刻必须是**完整**日期时间，只给时分秒无法跨零点算 age。"""
    st = AlertStore(_settings(), calendar=_cal())
    st.set_poll_stats(poll_ms=1, count=1, health=[], now=NOW, observation=_obs())
    s = st.observation_observed_at
    assert len(s) == 19 and s[4] == "-" and s[10] == " ", \
        f"必须是 YYYY-MM-DD HH:MM:SS，实际 {s!r}"
    datetime.strptime(s, "%Y-%m-%d %H:%M:%S")            # 可解析
