"""真实盘中 soak（浸泡）测试：让真引擎跑真行情 N 分钟，然后给一个诚实的结论。

与 ``tools/probe_live_ready.py`` 的分工
--------------------------------------
``probe_live_ready.py`` 回答的是"**能不能**盘中用"（单点能力探针：股票池、
五档、指数、单轮耗时）。本工具回答的是另一个问题："**连续跑一段**会不会烂掉"
—— 抓取失败、看板接口报错、SSE 断流、内存无界增长，这些只有跑一段时间才暴露。

三个刻意的设计决定
------------------
1. ``poll_once(force=True)``：不带 force 时引擎在非连续竞价时段直接 return ``[]``
   （见 engine.poll_once 的 idle_when_closed 分支），于是"休市时跑 soak"会变成
   什么都没测到 —— 正因为大多数人只有晚上/周末有空跑它，才必须带 force。
2. **0 条告警不算失败**。休市时行情源给的是"最后成交快照"，价格根本不动，
   没有告警才是正确行为。把它判成失败会训练使用者忽略这个工具。
3. 指标聚合与结论判定全部是**纯函数**（plain data in -> metrics/verdict out），
   因此可以在没有网络、没有服务端的条件下离线单测（见
   ``tests/test_live_session_tool.py``）。

跑法::

    python tools\\live_session.py --minutes 1
    python tools\\live_session.py --rounds 20 --browser

退出码：0 = 健康；1 = 有检查项不通过；2 = 连一轮都没跑完（harness 级失败）。
"""
from __future__ import annotations

# Windows 控制台 UTF-8（见 tools/_console.py）。
# 先正常导入；若失败说明本文件是被**按路径**加载的（例如测试用 importlib
# 从 tests/ 里 exec 它），此时 tools/ 不在 sys.path 上——把本文件所在目录
# 补进去再试一次，这样"直接跑"和"被当模块加载"两种场景都能用。
try:
    import _console  # noqa: F401,E402
except ImportError:  # pragma: no cover - 取决于调用方式
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    import _console  # noqa: F401,E402

import argparse
import enum
import json
import logging
import math
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
# 与仓库里其它 tools/*.py 一致：直接以源码树运行，不要求先 pip install -e .
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

UA = "arad-live-session/1.0"
#: 报告格式版本。2 = WP05/IT-P1-OBS-011：``metrics`` 新增观测账本聚合
#: （coverage 分位、拒绝总计、source mix、capability 缺失、最差 N 轮），
#: 并且 verdict 新增 ``warn``/``fail`` 与每项 ``level``。
#: 消费旧报告的工具请按 ``report_version`` 分支（字段是**新增**，不是改名，
#: 旧字段全部保留，所以只读旧字段的消费者不受影响）。
REPORT_VERSION = 2
DEFAULT_MINUTES = 10
#: 观测健康阈值（WP05 / IT-P1-OBS-011），可被 ``evaluate_health(tolerances=...)``
#: 逐项覆盖。**刻意不要求 100% 覆盖**：一轮要请求 5000+ 只票，停牌/新股/源端漏发
#: 让它永远到不了 100%，把 100% 当门槛只会训练使用者忽略这一项。
#:
#: 取值依据 —— 先取一个**真实锚点**（tencent 主源，全市场，2026-09 实测 3 轮）：
#:
#:     requested=5569  returned=5564  coverage=0.9991  unavailable=0
#:
#: 即健康轮次的覆盖率是 **99.9%**，不是 100%。据此把"正常抖动"与"真的丢数据"
#: 的分界定在下面这些位置（都留了 >=1.5 个百分点的余量，避免网络抖动误报）：
#:
#: * ``coverage_warn_p05`` 0.95 / ``coverage_fail_p05`` 0.90 —— p05 逼近"最差的
#:   那几轮"。20 轮里最差几轮掉破 95% 就该看一眼；掉破 90%（5000 只里丢 500 只）
#:   不是抖动，而是整批请求被砍（限流、半截快照）。
#: * ``coverage_warn_p05/p50`` 的 fail 都取 0.90：93% 这种 soft-partial 是
#:   **WARN** 而不是 FAIL —— 7% 的缺口值得警觉，但它不构成"这场 soak 白跑"，
#:   判红会让这个工具在真实降级路径（切备用源）上天天报错，反而没人看。
#: * ``coverage_warn_p50`` 0.97 —— 一半轮次都不到 97% 说明是**系统性**丢数，
#:   不是个别轮次的坏运气。实测健康值 99.9%，这个门槛有近 3 个百分点余量。
#: * ``coverage_warn_min`` 0.90 / ``coverage_fail_min`` 0.75 —— min 是唯一能抓住
#:   "绝大多数轮都很好、就一轮几乎全丢"的指标（均值/中位数都抓不住）。fail 放到
#:   0.75 是为了不跟 p05 的 fail 重复喊：单轮 80% 已经由 p05 抓，min 专门抓塌方。
#: * ``unavailable_warn_ratio`` 0.0 / ``unavailable_fail_ratio`` 1.0 —— 只要有一轮
#:   出现"这类规则本来源就评不了"就值得黄一下（能力缺失是硬缺口，不是网络抖动）；
#:   **每一轮**都不可评估，说明整类规则在这整场 soak 里一次都没被验证过 ——
#:   那是一场没测到的 soak，必须红。
DEFAULT_OBSERVATION_TOLERANCES: dict[str, Any] = {
    "coverage_warn_p05": 0.95,
    "coverage_fail_p05": 0.90,
    "coverage_warn_p50": 0.97,
    "coverage_fail_p50": 0.90,
    "coverage_warn_min": 0.90,
    "coverage_fail_min": 0.75,
    "unavailable_warn_ratio": 0.0,
    "unavailable_fail_ratio": 1.0,
    # --- WP03 / IT-P1-CAPABILITY-003：逐 signal 可评估率 -------------------
    # 为什么用**逐 signal** 而不是"轮里有没有缺失"：后者分母不是同一件事。
    # 5000 只票里每轮只坏 1 只，真实可评估率 99.98%，旧口径却给出
    # "轮比例 = 1.0" 并报 whole-class FAIL。新口径直接算
    # ``evaluable / considered``，把分母修正回标的级。
    #
    # 语义：某个 signal 的 ``considered`` 全被判 blocked（coverage == 0）
    # 才是"整类规则一次都没被评估过"的**唯一**站得住的证据。
    "evaluability_warn_coverage": 0.95,
    "evaluability_fail_coverage": 0.0,
    # 样本不足时**最多只到 warn**，不判 fail。理由：coverage 是小样本比率，
    # 一只票坏掉就能让 3 只票的 signal 从 1.0 掉到 0.67；在没有真实 soak
    # 样本的情况下把它判红，等于用噪声定罪。宁可黄着让人去看明细。
    "evaluability_min_samples": 200,
}

#: 允许的抓取失败比例。为什么不是 0：README §5.5 写得很清楚 —— 东财限流是常态，
#: 主源偶发失败会由 SourceManager 自动退到备用源，这是**正常降级路径**而不是故障。
DEFAULT_TOLERANCES: dict[str, Any] = {
    "max_error_ratio": 0.10, "min_rounds": 1, **DEFAULT_OBSERVATION_TOLERANCES}

#: "最差 N 轮"摘要的默认条数。5 条足以回查坏轮，又不至于让报告退化成逐轮日志。
DEFAULT_WORST_ROUNDS = 5

#: 带观测账本的轮样本里会出现这些键（任一出现即认为该轮带账本）。
#: 它是"这条轮样本来自新版本（有 observation）"的判据，也是
#: "没采集到"与"确实是 0"的区分依据 —— 键不在 = 没采到，不能当 0 分。
_OBSERVATION_MARKER_KEYS: tuple[str, ...] = (
    "requested", "returned", "admitted", "coverage", "source",
    "future_rejected", "stale_rejected", "out_of_order_rejected",
    "unknown_missing", "rejected_quality", "unavailable_capability",
    "observation_fields", "capabilities", "unavailable_by_reason",
    "signal_evaluability",
    # IT-P1-SOAK-LEDGER-BLIND-001：新交付账本必须也在这里，否则 soak 对它
    # 完全盲 —— 白名单只到 signal_evaluability 时，汇总仍从旧的 eval 账本
    # 累加 ev_committed，而那个数只覆盖 2/7 规则（9.1% 覆盖率）。
    "signal_delivery", "delivery_accounting",
)

#: 可观测性字段在轮样本里的"采集标记"：只有真的可测的字段才写进去。
#: 见 ``make_round_sample`` 的说明 —— 值仍然是 _safe_int/_safe_float 归一化过的
#: 旧形状（向后兼容），但汇总方靠这份标记就能分清 0 与"没采到"。
_OBSERVATION_VALUE_FIELDS: tuple[str, ...] = (
    "requested", "returned", "admitted", "coverage", "source",
    "future_rejected", "stale_rejected", "out_of_order_rejected",
    "unknown_missing", "rejected_quality", "unavailable_capability",
    "capabilities", "unavailable_by_reason", "signal_evaluability",
    # IT-P1-SOAK-LEDGER-BLIND-001：同上，交付账本必须进入 value 白名单，
    # 否则 ``make_round_sample`` 的 slim 视图会把它整段丢掉。
    "signal_delivery", "delivery_accounting",
)
#: 股票池规模允许的膨胀倍数（首末对比）。留足余量：新股上市、股票池 TTL 到期后
#: 从"自选股降级"恢复到全市场，都会让这个数字变大，那不是内存泄漏。
MEM_GROWTH_LIMIT = 2.0
MEM_GROWTH_SLACK = 500

#: R-21 / IT-P1-UNIVERSE-HEALTH-GATE-001：扫描池规模的下限与警戒线。
#:
#: A 股全市场约 5900+ 只（eastmoney `total=5913`）。源端限流或分页失败时
#: `refresh_universe()` 仍会**成功返回一个更小的池**（实测 4100/5913 = 69.3%），
#: 而 `evaluate_health` 此前对此完全无感 —— 扫描范围缩 30% 仍报
#: `healthy=True / exit=0`。
#:
#: 这里不写"必须等于 5913"（新股/退市/停牌使其本就会浮动），只设**下限**：
#:   < UNIVERSE_MIN_ABS  -> fail（扫描范围过小，漏报风险高）
#:   < UNIVERSE_WARN_ABS -> warn（偏小，不改退出码，但必须点名）
#: 真实长跑若恒远超这两条线，说明源端稳定、本担心不成立 —— 可证伪。
UNIVERSE_MIN_ABS = 3000
UNIVERSE_WARN_ABS = 4500

#: R-12 / WP01：**相对**覆盖阈值（active_scan_codes / expected_total）。
#:
#: 为什么绝对阈值不够（实测）：只要分母已知，
#:   active=4500 / expected=5913 = **76.10%** 会被绝对门禁判 **OK**
#:   active=4100 / expected=4200 =  97.62% 却被判 WARN
#: —— 缺 24% 的那个反而"更好看"。**这不是阈值再调一下能解决的，是分母缺失。**
#:
#: 云端 §8.5 给出的候选起点（**非已认证最优**，先落地再据真实长跑校准）：
#:   < 90%  -> fail
#:   90-95% -> warn
#:   >= 95% -> ok
#: 分母未知 -> **不判红**（未测量）。
UNIVERSE_COV_FAIL = 0.90
UNIVERSE_COV_WARN = 0.95

#: IT-P1-UNIVERSE-META-STALE-AFTER-FAILED-REFRESH-001（云端 09:49 / 12:03）：
#: 活跃股票池的**陈旧度**门禁（秒）。
#:
#: 为什么需要：全源刷新失败时 `refresh_universe()` 返回 0 但**不更新**
#: `_universe_meta` —— 保留 active pool 是对的（不能因一次失败就抹掉扫描集），
#: 但实测 `universe_truth()` 在断供前后**逐字段相同**，
#: 消费者**无从知道**那份 "coverage_active=93.85%" 是多久以前的。
#: 没有这两条线，"上次成功"会被读成"现在健康"。
#:
#: 上限取 2 倍默认刷新周期（默认 1800s）—— 超过两个周期没成功刷新，
#: 扫描范围已不足以代表当前全市场。
UNIVERSE_AGE_WARN_S = 1800.0
UNIVERSE_AGE_FAIL_S = 3600.0

EXIT_HEALTHY = 0
EXIT_UNHEALTHY = 1
EXIT_NO_DATA = 2


# ==========================================================================
# 纯函数区：不碰网络、不碰文件、不碰时钟 —— 离线可测
# ==========================================================================
def percentile(values: Sequence[float], p: float) -> float | None:
    """线性插值分位数（0<=p<=100）。空序列返回 None。

    为什么不用 ``statistics.quantiles``：它对 n<2 会抛异常，而 soak 测试很可能
    只跑到 1 轮（``--rounds 1`` 是排查问题的常用姿势），此时不能让统计先崩掉。
    """
    vals = sorted(float(v) for v in values if _finite(v))
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    frac = min(max(float(p), 0.0), 100.0) / 100.0 * (len(vals) - 1)
    lo = int(math.floor(frac))
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (frac - lo)


def latency_stats(values: Sequence[float]) -> dict:
    """单轮耗时统计：min / median / p95 / max / count（毫秒，保留 2 位）。

    p95 而不是平均值才是判断"5 秒轮询够不够"的依据：平均 400ms 但尾部 9s 的
    引擎在盘中会持续堆积，平均数是看不出来的。
    """
    vals = [float(v) for v in values if _finite(v)]
    if not vals:
        return {"count": 0, "min": None, "median": None, "p95": None, "max": None}
    return {
        "count": len(vals),
        "min": round(min(vals), 2),
        "median": round(float(percentile(vals, 50) or 0.0), 2),
        "p95": round(float(percentile(vals, 95) or 0.0), 2),
        "max": round(max(vals), 2),
    }


def _round_quote_count(obs: dict | None, state: Any) -> int:
    """本轮真实拿到多少条行情（**不是**累计缓存规模）。

    见 ``_soak_loop`` 写入点的长注释：``len(state.quotes)`` 是从不缩小的
    累计缓存，拿它当逐轮行情数会让静默断供拿到完整假绿
    （``healthy=True / exit=0 / fail=[]``）。

    取数优先级：

    1. ``obs['returned']`` —— provider **本轮原始返回**条数（含 price<=0）。
       这是"我看到多少行情"最直接的口径，也是 ``coverage`` 的分子。
    2. ``obs['admitted']`` —— 通过时间准入的条数。
    3. 都没有 -> 退回 ``len(state.quotes)``（**退化**，仅用于旧报告/离线样本）。

    必须容忍脏值：可观测性字段坏了不能让 soak 崩。
    """
    if isinstance(obs, dict):
        for key in ("returned", "admitted"):
            raw = obs.get(key)
            if raw is None:
                continue
            try:
                val = int(raw)
            except (TypeError, ValueError):
                continue
            if val >= 0:
                return val
    return len(getattr(state, "quotes", {}) or {})


def make_round_sample(
    *,
    index: int,
    latency_ms: float,
    alerts: Iterable[Any],
    error: bool,
    quotes: int,
    universe: int,
    history_points: int,
    history_codes: int,
    max_deque: int,
    history_maxlen: int,
    watchlist_only: bool,
    observation: dict | None = None,
    universe_truth: dict | None = None,
) -> dict:
    """把一轮的观测值收敛成一个 plain dict（纯函数，便于离线造样本）。

    ``alerts`` 接受 Alert 对象、dict 或裸 kind 字符串：真实运行给的是 Alert，
    测试里手写样本时不必构造一整个 Alert。

    ``observation`` 是本轮的 ``RoundObservationSet.as_dict()``（WP02）。
    以前这里只有 ``quotes=len(state.quotes)`` —— 那是**累计缓存**的 key 数，
    不是本轮真实观测数，休市/切源时会把旧值冒充成本轮覆盖（IT-P2-OBS-001）。
    """
    obs = dict(observation or {})
    out = {
        "index": int(index),
        "latency_ms": float(latency_ms),
        "kinds": [_kind_of(a) for a in (alerts or [])],
        "error": bool(error),
        "quotes": int(quotes),
        "universe": int(universe),
        "history_points": int(history_points),
        "history_codes": int(history_codes),
        "max_deque": int(max_deque),
        "history_maxlen": int(history_maxlen),
        "watchlist_only": bool(watchlist_only),
    }
    # 本轮真实观测账本（有则并入；离线造样本不传时保持旧形状）。
    # 全部走 _safe_int/_safe_float：可观测性字段脏了不能让 soak 崩，
    # 报告宁可少几项也不能因为一个 None/字符串把整场 soak 打断。
    if obs:
        out["requested"] = _safe_int(obs.get("requested"))
        out["returned"] = _safe_int(obs.get("returned"))
        out["admitted"] = _safe_int(obs.get("admitted"))
        out["coverage"] = _safe_float(obs.get("coverage"))
        out["source"] = str(obs.get("source") or "")
        # 优先读新名 future_rejected；旧名 stale_rejected 为兼容保留
        # （IT-P1-OBS-006：旧名语义相反，曾把"未来拒绝"叫成"陈旧拒绝"）。
        fr = obs.get("future_rejected")
        if fr is None:
            fr = obs.get("stale_rejected")
        out["future_rejected"] = _safe_int(fr)
        # 旧键兜底时把原值也留下来：汇总方要能看出这个"未来拒绝"数其实来自
        # 旧版本报告里的 ``stale_rejected``（IT-P1-OBS-006 改名前的数据），
        # 并据此把 reject 总数记到 ``stale_total``。没有这一步，
        # ``observation_fields`` 里写着采到了 stale_rejected，值却读不出来。
        if "future_rejected" not in obs and "stale_rejected" in obs:
            out["stale_rejected"] = _safe_int(obs.get("stale_rejected"))
        out["out_of_order_rejected"] = _safe_int(obs.get("out_of_order_rejected"))
        # WP01：拒绝的 **route 归属**。合计值分不出
        # `index future + stock ooo` 与 `index ooo + stock future`
        # （两者合计都是 1/1），soak 报告若只留合计，事后无法复盘
        # 是哪条 route 在超时/乱序。只保留有界的 route 分账，不引入明细。
        rbr = obs.get("reject_by_route")
        if isinstance(rbr, dict) and rbr:
            out["reject_by_route"] = {
                str(r): {"future": _safe_int(
                             (v or {}).get("future") if isinstance(v, dict) else 0),
                         "out_of_order": _safe_int(
                             (v or {}).get("out_of_order")
                             if isinstance(v, dict) else 0)}
                for r, v in rbr.items()
            }
        for _k in ("admitted_by_route", "returned_by_route"):
            _v = obs.get(_k)
            if isinstance(_v, dict) and _v:
                out[_k] = {str(r): _safe_int(n) for r, n in _v.items()}
        missing = obs.get("unknown_missing")
        out["unknown_missing"] = len(missing) if isinstance(missing, (list, tuple)) else 0
        quality = obs.get("rejected_quality")
        out["rejected_quality"] = len(quality) if isinstance(quality, (list, tuple)) else 0
        out["unavailable_capability"] = _safe_int(obs.get("unavailable_capability"))
        # WP03 / IT-P1-CAPABILITY-003：逐 signal 可评估率。
        # 只保留判定需要的**有界**计数，丢掉 blocked_sample 明细（逐股，
        # 可能几千条）—— soak 报告是长时间跑的，不能让明细撑爆内存/文件。
        sig_ev = obs.get("signal_evaluability")
        if isinstance(sig_ev, dict) and sig_ev:
            slim: dict[str, dict] = {}
            for sig, cell in sig_ev.items():
                if not isinstance(cell, dict):
                    continue
                row = {
                    "considered": _safe_int(cell.get("considered")),
                    "evaluable": _safe_int(cell.get("evaluable")),
                    "blocked_capability": _safe_int(cell.get("blocked_capability")),
                    "advisory_missing": _safe_int(cell.get("advisory_missing")),
                    "hit_candidates": _safe_int(cell.get("hit_candidates")),
                    # 兼容键：等价 rule_selected。保留是为了旧消费方不炸，
                    # 但**不再是**交付数的真值来源。
                    "published": _safe_int(cell.get("published")),
                    "blocked_reasons": (
                        dict(cell.get("blocked_reasons"))
                        if isinstance(cell.get("blocked_reasons"), dict) else {}),
                }
                # IT-P1-EVAL-PUBLISH-001：交付三级**仅在源样本真的有该键时**
                # 才写进 slim。写成 `_safe_int(cell.get(...))` 会让缺失的键
                # 变成 0 —— 于是下游"键在不在"的探测永远为真，旧轮样本
                # 会被读成"交付 0 条"（把字段缺失误报成灾难）。
                for k in ("rule_selected", "bus_accepted", "committed",
                          "dropped_by_bus"):
                    if k in cell:
                        row[k] = _safe_int(cell.get(k))
                slim[str(sig)] = row
            out["signal_evaluability"] = slim
        # --- IT-P1-SOAK-LEDGER-BLIND-001：新交付账本必须进 slim -------------
        # 旧白名单只到 signal_evaluability，于是 soak 汇总仍从 eval 账本累加
        # ev_committed —— 那个数只覆盖 2/7 规则（9.1% 覆盖率），
        # 新交付账本（8/8）反而看不见。这里如实带出来。
        sig_dl = obs.get("signal_delivery")
        if isinstance(sig_dl, dict) and sig_dl:
            dlslim: dict[str, dict] = {}
            for sig, cell in sig_dl.items():
                if not isinstance(cell, dict):
                    continue
                dlslim[str(sig)] = {
                    "rule_selected": _safe_int(cell.get("rule_selected")),
                    "global_ignored": _safe_int(cell.get("global_ignored")),
                    "bus_accepted": _safe_int(cell.get("bus_accepted")),
                    "committed": _safe_int(cell.get("committed")),
                    "dropped_by_bus": _safe_int(cell.get("dropped_by_bus")),
                }
            out["signal_delivery"] = dlslim
        # delivery_accounting 是**逐轮**口径（含 _scope），整体带出。
        # 只带标量/列表，避免无界载荷。
        acct = obs.get("delivery_accounting")
        if isinstance(acct, dict) and acct:
            out["delivery_accounting"] = dict(acct)
        caps = obs.get("capabilities")
        out["capabilities"] = dict(caps) if isinstance(caps, dict) else {}
        # IT-P1-OBS-007：把"哪只票缺什么"带出 Store
        sample = obs.get("unavailable_sample")
        out["unavailable_sample"] = (
            list(sample)[:5] if isinstance(sample, (list, tuple)) else [])
        reasons = obs.get("unavailable_by_reason")
        out["unavailable_by_reason"] = (
            dict(reasons) if isinstance(reasons, dict) else {})
        # --- WP05 / IT-P1-OBS-011：采集**标记** ---------------------------
        # 汇总方必须能分清"这轮 coverage 真的是 0"和"这轮根本没采到 coverage"。
        # 只看 ``out.get("coverage")`` 做不到：两者都读成 0.0/缺键，而"缺键"
        # 又与"轮样本来自旧版本"混在一起。所以在轮样本里显式记一份"哪些字段
        # 真的采集到了"（并在下面保留 ``None`` 语义给分位/最差轮用）。
        present = [k for k in _OBSERVATION_VALUE_FIELDS if k in obs]
        out["observation_fields"] = present
        # None 明确表示"采到了字段但这个值是坏的"，与"没采到"（键不在标记里）
        # 和"确实是 0"（标记里有且值是 0）三者互不混淆。
        #
        # 判定"坏"要连**越界**一起算：coverage 是比例，只能在 [0, 1]。
        # 不拦的话 ``True`` 会被读成 0.0（bool 是 int 的子类）、``-1``/``1.5``
        # 会被原样带进分位，于是汇总里出现"覆盖率 -100%"这种不可能的数字，
        # 而它其实是脏数据 —— 记成 None（读不出）远好过记成假成绩。
        cov = obs.get("coverage")
        if "coverage" in obs:
            bad_cov = (cov is None or isinstance(cov, bool)
                       or not _finite(cov)
                       or not 0.0 <= float(cov) <= 1.0)
            if bad_cov:
                out["coverage"] = None
        out["observation_missing_fields"] = [
            k for k in _OBSERVATION_VALUE_FIELDS if k not in obs]
    # --- WP02 / IT-P1-HEALTH-COVERAGE-SNAPSHOT-PRELOOP-001 --------------------
    #
    # 云端 12:03：`setup` 只在 soak **开始前**取一次 `universe_truth`，
    # 于是 soak 中途发生的 universe 刷新失败**永远进不了最终判决** ——
    # final verdict 用的是 t0 快照。
    #
    # 所以每轮都采一份**新鲜度**（只取标量，避免无界载荷），
    # 由 `summarize_rounds` 聚合成"最新/最坏"。
    if isinstance(universe_truth, dict) and universe_truth:
        _fresh = universe_truth.get("freshness")
        _fresh = _fresh if isinstance(_fresh, dict) else {}
        _attempt = universe_truth.get("latest_attempt")
        _attempt = _attempt if isinstance(_attempt, dict) else {}
        out["universe_attempt_status"] = str(
            universe_truth.get("attempt_status")
            or _attempt.get("status") or "")
        out["universe_fresh_state"] = str(_fresh.get("state") or "unknown")
        # IT-P1-UNIVERSE-TRANSPORT-TRISTATE-R1：**三态必须端到端保留**。
        # 修前这里写 `bool(universe_truth.get("transport_complete"))` ——
        # 即便 Engine 已把 None 修成"未测量"，这一层又会把它压回 False，
        # 于是 session 聚合把"未测量"误计为"被截断"。
        # 修一个假绿会立刻造出一个**新的假红** —— 所以顺序必须是
        # 先修 tri-state 贯穿，再真正消费 `_ti`。
        out["universe_transport_complete"] = _optional_bool(
            universe_truth.get("transport_complete"))
        # 年龄同理：`_safe_float(None)` 会给 0.0，而 0.0 是"最年轻"——
        # "年龄未知"绝不能变成数值上最年轻。
        _age_raw = _fresh.get("age_s")
        if _age_raw is None:
            _age_raw = universe_truth.get("full_market_age_s")
        if _age_raw is None:
            _age_raw = universe_truth.get("active_age_s")
        out["universe_age_s"] = _optional_float(_age_raw)
        out["universe_refresh_id"] = _safe_int(_attempt.get("refresh_id"))
        out["universe_truth_fields"] = sorted(str(k) for k in universe_truth)
        # --- Session Universe Evidence Contract v1 / WP01 ------------------
        #
        # IT-P1-UNIVERSE-ACTIVE-COVERAGE-SESSION-BLIND-001（20:04 §5）：
        # `setup['universe_truth']` 是 **t0 快照**。会话中途活跃覆盖从
        # 5900/6000=98.33% 掉到 4500/6000=75% 时，**没有任何会话级消费者**
        # —— `universe_coverage` 仍然读 t0 的 98.33%，判 ok。
        #
        # `summary_rounds` 算出的 `metrics['universe'].min`（4500）**确实存在**，
        # 但它没有分母（expected_total），算不出**覆盖率**，也没有判决项读它。
        #
        # 所以每轮必须采"**能算覆盖率的两个数**"：分子 + 分母。
        # 只采分子（universe size）等于采了一个**无法解释**的数字。
        #
        # 三态纪律：**未知 != 0**。分母拿不到就写 None，绝不写 0
        # （0 会让 coverage 变成 0% 或除零，凭空造出一个假红）。
        _exp_raw = universe_truth.get("expected_total")
        out["universe_expected_total"] = _optional_int(_exp_raw)
        _act_raw = universe_truth.get("active_scan_codes")
        out["universe_active_scan_codes"] = _optional_int(_act_raw)
        _cov_raw = universe_truth.get("coverage_active")
        # 人口覆盖率只能在 [0,1]；越界/脏值记 None（读不出），不记假成绩。
        if isinstance(_cov_raw, bool) or not _finite(_cov_raw):
            out["universe_coverage_active"] = None
        else:
            _c = float(_cov_raw)
            out["universe_coverage_active"] = (
                round(_c, 6) if 0.0 <= _c <= 1.0 else None)
        # 分母**种类**必须一起带走：`provider_declared_total` 与
        # `unknown` 是两种证据强度完全不同的结论，不能塌缩成一个比率
        # （ADDENDUM2 §3 `IT-P2-UNIVERSE-DROP-CAUSE-COLLAPSED-001`）。
        _dk = universe_truth.get("denominator_kind")
        out["universe_denominator_kind"] = (
            str(_dk) if isinstance(_dk, str) and _dk else None)
        # --- Scope 四态（C1：零证据不得被肯定成"全市场"）-------------------
        #
        # `IT-P2-UNIVERSE-EMPTY-SCAN-ASSERTED-AS-FULL-MARKET-001`（20:04 §3）：
        # 修前 scope 只有 `watchlist_only` 布尔，于是**零证据**（0 轮）与
        # **空扫描**（池子被清空）都落进"未降级"的肯定分支，
        # 输出「会话期间未降级为仅自选股（**全程全市场扫描**）」。
        # 一轮都没跑的报告也这么说 —— 这是**零证据被当成肯定证据**。
        #
        # 清空 vs 全市场在旧口径下**不可区分**，因为两者都是
        # `watchlist_only=False`。必须显式分开成四态。
        out["universe_scope_state"] = _scope_state_of(
            universe_truth, universe=out.get("universe"),
            watchlist_only=bool(out.get("watchlist_only")),
            # 量级证据必须显式传进去 —— 否则 `active > 0` 会被当成"全市场"
            # （`IT-P2-UNIVERSE-FULL-MARKET-IS-MAGNITUDE-BLIND-001`）。
            expected_total=out.get("universe_expected_total"),
            coverage_active=out.get("universe_coverage_active"))
    return out


#: 会话扫描范围四态。**必须四态**：`bool` 的两态无法区分
#: "空扫描"（池子被清空）与"全程全市场"，两者旧口径都是 `watchlist_only=False`。
SCOPE_FULL_MARKET = "full_market"
SCOPE_WATCHLIST_ONLY = "watchlist_only"
SCOPE_EMPTY_SCAN = "empty_scan"
SCOPE_UNKNOWN = "unknown"
#: `IT-P2-UNIVERSE-FULL-MARKET-IS-MAGNITUDE-BLIND-001`（09-23 00:12 §6）：
#: **分母未知时 active>0 只能证明"扫得挺广"，不能证明"全市场"。**
#: 修前 `_scope_state_of` 只判 `active > 0` 就返回 `full_market`，
#: 实测 active=400 / 3000 / 3001（分母未知）**全部**叫"全程全市场扫描"。
#: 量级证据（`expected_total` + `coverage_active`）是"全市场"这个词的**必要条件**。
SCOPE_BROAD_UNQUANTIFIED = "broad_scan_unquantified"


def _scope_state_of(universe_truth: dict | None, *, universe: Any,
                    watchlist_only: bool, expected_total: Any = None,
                    coverage_active: Any = None,
                    cov_ok: float | None = None) -> str:
    """本轮扫描范围 -> **五态**之一（纯函数）。

    `IT-P2-UNIVERSE-EMPTY-SCAN-ASSERTED-AS-FULL-MARKET-001`（20:04 §3）：
    * `empty_scan`：股票池**真的空了**（`active_scan_codes == 0`，
      或没有 active 数时本轮 `universe` 计数为 0）。
      **这与"全市场"在旧口径下不可区分，正是缺陷本体。**
    * `watchlist_only`：显式降级为自选股。
    * `unknown`：**没有任何证据**。**绝不能归入 `full_market`**。

    `IT-P2-UNIVERSE-FULL-MARKET-IS-MAGNITUDE-BLIND-001`（09-23 00:12 §6）：
    * `full_market`（**量级已证**）：分母已知 **且** 覆盖率 ≥ 全市场线。
      **"全市场"是量级断言，不是存在性断言。**
    * `broad_scan_unquantified`：`active > 0` 但**量级证不出来**
      （分母未知 / 算不出覆盖率 / 覆盖率低于线）——
      只能说"有较广的扫描集合"，**不能说全市场**。
    """
    ut = universe_truth if isinstance(universe_truth, dict) else {}
    if watchlist_only:
        return SCOPE_WATCHLIST_ONLY
    active = ut.get("active_scan_codes")
    has_active = (isinstance(active, (int, float))
                  and not isinstance(active, bool))
    if has_active:
        if float(active) <= 0:
            return SCOPE_EMPTY_SCAN
    else:
        # 没有 active 数时退回本轮池计数（`make_round_sample` 的 `universe`）。
        if isinstance(universe, (int, float)) and not isinstance(universe, bool):
            if float(universe) <= 0:
                return SCOPE_EMPTY_SCAN
            active = universe
        else:
            return SCOPE_UNKNOWN
    # --- 到这里 active > 0。现在问：**量级**证得了吗？--------------------
    _exp = expected_total
    if _exp is None:
        _exp = ut.get("expected_total")
    has_exp = (isinstance(_exp, (int, float)) and not isinstance(_exp, bool)
               and float(_exp) > 0)
    if not has_exp:
        return SCOPE_BROAD_UNQUANTIFIED          # 分母未知 -> 不能说"全市场"
    _cov = coverage_active
    if _cov is None:
        _cov = ut.get("coverage_active")
    if (isinstance(_cov, (int, float)) and not isinstance(_cov, bool)
            and _finite(_cov) and 0.0 <= float(_cov) <= 1.0):
        _line = UNIVERSE_COV_WARN if cov_ok is None else cov_ok
        return (SCOPE_FULL_MARKET if float(_cov) >= _line
                else SCOPE_BROAD_UNQUANTIFIED)
    # 有分母但算不出覆盖率：仍无量级证据。
    return SCOPE_BROAD_UNQUANTIFIED


def _universe_truth_now(engine: Any) -> dict | None:
    """安全取一次 ``engine.universe_truth()``（**绝不抛**）。

    soak 是长时间跑的：可观测性取数失败不能打断主链路，
    但也不能静默 —— 失败返回 ``None``，由聚合方判"未测量"。
    """
    try:
        fn = getattr(engine, "universe_truth", None)
        if not callable(fn):
            return None
        got = fn()
        return got if isinstance(got, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _universe_session(rows: Sequence[dict]) -> dict:
    """逐轮股票池样本 -> 会话级新鲜度（纯函数，WP02）。

    **为什么必须存在**（云端 12:03 的
    `IT-P1-HEALTH-COVERAGE-SNAPSHOT-PRELOOP-001`）：
    `setup['universe_truth']` 只在 soak **开始前**取一次。如果 soak 跑到一半
    universe 刷新开始连续失败，最终判决用的**仍然是 t0 那份快照** ——
    中途的失效结构上进不了结论。

    这里把每轮采到的（`make_round_sample` 写入）
    `universe_attempt_status` / `universe_fresh_state` / `universe_age_s` /
    `universe_transport_complete` 聚合成会话级事实。

    口径纪律：
    * `worst_state` 取**最坏**（stale > aging > fresh > unknown），
      判决读它 —— "中途曾坏过"不能被"最后又好了"抹掉。
    * `rounds_not_applied` 数"最近一次刷新尝试未生效"的轮数；
      `attempt_statuses` 给出**实际出现过**的状态集合，便于对账。
    * 老轮样本没有这些键 -> `measured=False`，消费方据此跳过（不判红）。
    """
    # `IT-P1-FRESHNESS-GATE-COUNTS-RECORDS-NOT-EVIDENCE-001`（云端 18:07 §1.2）
    # 的**正确**修法在这里**不在**本表：
    #
    # 我一开始按云端的建议 (b) 把 `unknown` 提到 4（高于 stale），
    # **那会造出一个新的镜像错误**：`29 轮 unknown + 1 轮 stale` 时
    # `max()` 会取 `unknown`，于是会话级结论变成"未测量"，
    # **把一次真实的陈旧掩盖成"没测到"** —— 用一个假绿换一个假"未测量"。
    #
    # 真正的修法是**在生产端**让 `freshness_measured_rounds` 只数
    # **有年龄证据**的轮（`unknown` 不计），这样消费端 `:2337` 那条
    # 本来正确的守卫就会正常触发。本表保持原样：
    # 只回答"最坏的真实状态是什么"，`unknown` 只在**全部轮次**都无证据时胜出。
    _order = {"stale": 3, "aging": 2, "fresh": 1, "unknown": 0}
    states: dict[str, int] = {}
    statuses: dict[str, int] = {}
    ages: list[float] = []
    not_applied = 0
    transport_incomplete = 0
    transport_measured = 0
    ids: list[int] = []
    watch_only = 0
    watch_seen = 0
    # --- Session Universe Evidence Contract v1（WP01）---------------------
    # 四态 scope 计数 + per-round active coverage 数值证据。
    scope_counts: dict[str, int] = {}
    cov_vals: list[float] = []
    cov_denoms: dict[str, int] = {}
    for r in rows:
        if (r.get("watchlist_only") is not None
                and ("quotes" in r or "universe" in r)):
            watch_seen += 1
            if r.get("watchlist_only"):
                watch_only += 1
        # scope 四态：老轮样本没有这个键 -> 不计入（未测量，不是 unknown 读数）。
        _sc = r.get("universe_scope_state")
        if isinstance(_sc, str) and _sc:
            scope_counts[_sc] = scope_counts.get(_sc, 0) + 1
        # 会话内活跃覆盖率：只在**两个数都有**时才算 —— 有分子没分母
        # 算不出覆盖率，必须留空而不是拿 0 顶上。
        _cv = r.get("universe_coverage_active")
        if (isinstance(_cv, (int, float)) and not isinstance(_cv, bool)
                and 0.0 <= float(_cv) <= 1.0):
            cov_vals.append(float(_cv))
            _dk = r.get("universe_denominator_kind")
            _dk = str(_dk) if isinstance(_dk, str) and _dk else "unknown"
            cov_denoms[_dk] = cov_denoms.get(_dk, 0) + 1
        if "universe_fresh_state" not in r and "universe_attempt_status" not in r:
            continue
        st = str(r.get("universe_fresh_state") or "unknown")
        states[st] = states.get(st, 0) + 1
        at = str(r.get("universe_attempt_status") or "")
        if at:
            statuses[at] = statuses.get(at, 0) + 1
            if at not in ("applied", "applied_partial"):
                not_applied += 1
        # 三态：只有**明确 False** 才算被截断；None（未测量）不计入。
        # 修前是 `is False` 但上游已用 bool() 把 None 压成 False，
        # 所以这里必须配合上游 tri-state 修复才有意义。
        _tc = r.get("universe_transport_complete")
        if _tc is None:
            pass
        elif _tc is False:
            transport_incomplete += 1
            transport_measured += 1
        else:
            transport_measured += 1
        age = r.get("universe_age_s")
        if isinstance(age, (int, float)) and not isinstance(age, bool):
            ages.append(float(age))
        rid = r.get("universe_refresh_id")
        if isinstance(rid, int) and not isinstance(rid, bool) and rid > 0:
            ids.append(rid)
    # watchlist 比例是**独立事实**，即使没有任何 universe 新鲜度样本也要给出。
    # IT-P1-UNIVERSE-WATCHLIST-FALLBACK-SESSION-BLIND-001：只看
    # `fell_back_to_watchlist`（只有 30/30 轮才为 True）会漏掉 29/30，
    # 所以必须保留 round count / ratio / ever 三个事实。
    watch_facts = {
        "watchlist_only_rounds": watch_only,
        "watchlist_only_ratio": (round(watch_only / watch_seen, 4)
                                 if watch_seen else None),
        "ever_watchlist_only": bool(watch_only),
    }
    # --- Session Universe Evidence Contract v1：scope / active coverage ----
    #
    # `scope_measured_rounds` 是**四态证据的测量计数**，与 `measured`
    # （新鲜度证据）是**两条独立轴** —— 老轮样本可能一条有一条没有。
    # 消费者必须能问"scope 到底测了几轮"，否则零证据与全市场不可分（C1）。
    scope_measured = sum(scope_counts.values())
    scope_facts = {
        "scope_counts": scope_counts,
        "scope_measured_rounds": scope_measured,
        "full_market_rounds": scope_counts.get(SCOPE_FULL_MARKET, 0),
        "empty_scan_rounds": scope_counts.get(SCOPE_EMPTY_SCAN, 0),
        "unknown_scope_rounds": scope_counts.get(SCOPE_UNKNOWN, 0),
        "broad_scan_unquantified_rounds": scope_counts.get(
            SCOPE_BROAD_UNQUANTIFIED, 0),
    }
    # 会话内活跃覆盖率：**最坏值**优先（与 worst_state 同纪律，
    # "中途掉下去过"不能被"最后又好了"抹掉）。
    cov_facts = {
        "coverage_active_measured_rounds": len(cov_vals),
        "coverage_active_min": (round(min(cov_vals), 6) if cov_vals else None),
        "coverage_active_p05": (round(percentile(cov_vals, 5), 6)
                                if cov_vals else None),
        "coverage_active_last": (round(cov_vals[-1], 6) if cov_vals else None),
        "coverage_active_denominator_kinds": cov_denoms,
    }
    # --- Universe Evidence Completeness Contract v2（09-23 00:12 §7）------
    #
    # 最近三轮共同暴露的根因不只是"证据合并"，而是：
    #   **肯定结论的量词和实际证据覆盖范围不一致。**
    #
    # 所以 `measured` 是**不够的** —— 它只说明"有证据"，
    # **不说明"证据覆盖了全部轮次"**。全称肯定句（"全程"/"会话期间…新鲜"）
    # 要求 `measured_rounds == rounds_total > 0`；只覆盖 k/N 时**必须写出 k/N**。
    #
    # 修前实测（我自己的 bf0b83b 留下的残余）：
    #   `universe_scope` 用 "1/1 轮有明确扫描范围证据" 说出了"全程全市场扫描"
    #   —— **拿证据子集当了自己的分母**。
    #   freshness 只测 1/30 轮也输出「会话期间股票池新鲜」。
    evidence_facts = {
        #: 会话总轮数 —— 一切"全程/k of N"断言的**唯一**分母。
        "rounds_total": len(rows),
        #: freshness 轴的测量轮数。
        #:
        #: `IT-P1-FRESHNESS-GATE-COUNTS-RECORDS-NOT-EVIDENCE-001`
        #: （云端 18:07 §1.2）：这里修前是 `sum(states.values())` ——
        #: **数的是记录条数，不是证据**。29 轮 `unknown`（压根没测到，
        #: age 为 None）+ 1 轮 `fresh` 会得出 `measured_rounds=30`，
        #: 于是消费方 `:2337 elif _fresh_measured < _rounds_total`
        #: 这道**本来正确的**守卫 `30 < 30` 恒假、**永远不会触发**，
        #: 产品打出一个新鲜样本代表的"会话级新鲜"。
        #: 修法：`unknown`（= 无年龄证据）**不计入测量轮数**。
        "freshness_measured_rounds": sum(
            v for k, v in states.items() if k != "unknown"),
    }
    # --- `IT-P1-UNIVERSE-ABS-SIZE-SESSION-BLIND-001`（09-23 00:12 §3）-----
    #
    # `IT-P1-UNIVERSE-ABS-SIZE-SESSION-BLIND-001`（09-23 00:12 §3）：
    # 绝对项只读 t0 的 `setup.universe_size`，会话里池子从 5000 掉到 2000
    # **没有任何消费者**。这条与 `...-ACTIVE-COVERAGE-SESSION-BLIND-001`
    # **不同**：后者是 numeric coverage 已知但中途下降（`bf0b83b` 已修）；
    # 本条是**分母不可得**时仍应拿绝对 session min 做最低限度运行保护。
    abs_vals: list[int] = []
    for r in rows:
        v = r.get("universe")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            abs_vals.append(int(v))
    evidence_facts["universe_abs_measured_rounds"] = len(abs_vals)
    evidence_facts["universe_abs_min"] = min(abs_vals) if abs_vals else None
    # --- 死字段接活 ③：`universe_active_scan_codes` 需要**会话侧配对值** ----
    #
    # 轮样本里的 `universe_active_scan_codes` 此前零生产读者。
    # 单看一轮它只是 t0 `active_scan_codes` 的复读；**与 t0 配对**才有信息量：
    # 首轮值 ≠ t0 值 => 启动后活跃扫描集变过（分母种类相同也不行）。
    _act_first = None
    for r in rows:
        v0 = r.get("universe_active_scan_codes")
        if isinstance(v0, (int, float)) and not isinstance(v0, bool):
            _act_first = int(v0)
            break
    evidence_facts["universe_active_scan_codes_first"] = _act_first
    _evidence = {**scope_facts, **cov_facts, **evidence_facts}
    if not states and not statuses:
        return {"measured": False, **watch_facts, **_evidence}
    worst = max(states, key=lambda k: _order.get(k, 0)) if states else "unknown"
    return {
        "measured": True,
        "rounds": len(rows),
        "states": states,
        "worst_state": worst,
        "attempt_statuses": statuses,
        "rounds_not_applied": not_applied,
        "rounds_transport_incomplete": transport_incomplete,
        #: 有多少轮的 transport 是**真的测到了**（True/False 都算）。
        #: `rounds_transport_incomplete > 0` 与 `transport_measured == 0`
        #: 是两件不同的事，必须能分开读。
        "rounds_transport_measured": transport_measured,
        # 最新 = 最后一轮观测到的；最坏 = 全程最坏。判决读 worst。
        "last_state": (str(rows[-1].get("universe_fresh_state") or "unknown")
                       if rows else "unknown"),
        "max_age_s": (round(max(ages), 3) if ages else None),
        "last_age_s": (round(ages[-1], 3) if ages else None),
        "refresh_ids_seen": len(set(ids)),
        **watch_facts,
        **_evidence,
    }


def summarize_rounds(rounds: Sequence[dict], *,
                     worst_n: int = DEFAULT_WORST_ROUNDS) -> dict:
    """逐轮样本 -> 聚合指标（纯函数）。

    输出的字段名就是报告里 ``metrics`` 的形状，``evaluate_health`` 直接消费它，
    两者之间没有第二套命名，避免"报告里是这个名、判定时读那个名"的错位。

    WP05 / IT-P1-OBS-011：``make_round_sample()`` 早就把每轮的
    ``requested / returned / admitted / coverage / rejections / capability``
    采下来了，但这里以前把它们**全部丢掉** —— soak 报告里看不到观测覆盖率、
    拒绝构成、来源混合、能力缺失，于是"覆盖率只有 93% 的 soft-partial"和
    "整类规则一次都没被评估"这两种问题在报告里完全隐形。现在补齐六组聚合：

    ``coverage`` / ``coverage_p05`` / ``coverage_p50`` / ``coverage_min``
        **不给均值当结论**。均值会掩盖坏轮次：19 轮 99% 加 1 轮 40% 的均值仍有
        96%，看起来一切正常。p05 逼近"最差的那几轮"，min 直接点名塌方那一轮。
    ``requested_total`` / ``returned_total`` / ``admitted_total``
        全场累计账本。三者一起看才知道丢在哪一段（源端没返回 / 被准入拒掉）。
    ``missing_total`` / ``quality_total`` / ``future_total`` / ``stale_total`` /
    ``ooo_total``
        拒绝构成。``stale_total`` 只统计**旧样本**里显式写的 ``stale_rejected``
        （IT-P1-OBS-006 已改名 future_rejected，新样本里不再有"陈旧拒绝"路径），
        所以它与 ``future_total`` 不会重复计数。
    ``source_mix``
        ``{来源: 服务轮数}`` 及 ``source_mix_rounds`` / ``source_mix_unknown_rounds``。
        混源是结论可解释性的前提：同一场 soak 里一半轮次其实走的备用源，
        把这些轮次和主源轮次当同一条曲线比，任何结论都站不住。
    ``capability_unavailable_total`` / ``capability_unavailable_rounds`` /
    ``capability_unavailable_ratio`` / ``unavailable_by_reason``
        能力缺失的总量与**轮覆盖率**。"整类规则不可评估"是 soak 最该报的
        盲区之一：规则没告警到底是"没放量"还是"根本评不了"，只有这个数字能答。
    ``worst_rounds``
        按 coverage 升序取最差 N 轮，带 ``index`` 便于回查原始轮样本。

    缺数据的安全处理（旧版本轮样本 / 脏值）：所有计数走 ``_safe_int`` /
    ``_safe_float``，并且**只在真的采到该字段时才累计**（见
    ``_round_observation_fields``）。没采到的轮次不进 ``rounds_with_observation``，
    也不进 coverage 分位数 —— 绝不把"没采集到"伪造成 0 分成绩；一个有效
    coverage 都没有时 ``coverage`` 是 ``None`` 而不是 ``0.0``。
    """
    rows = [r for r in (rounds or []) if isinstance(r, dict)]
    by_kind: dict[str, int] = {}
    for r in rows:
        for k in r.get("kinds") or []:
            by_kind[str(k)] = by_kind.get(str(k), 0) + 1

    lat = [float(r.get("latency_ms") or 0.0) for r in rows]
    quotes = [int(r.get("quotes") or 0) for r in rows]
    universe = [int(r.get("universe") or 0) for r in rows]

    def _edge(seq: Sequence[int]) -> dict:
        if not seq:
            return {"first": 0, "last": 0, "min": 0, "max": 0}
        return {"first": seq[0], "last": seq[-1], "min": min(seq), "max": max(seq)}

    # 降级判据：股票池规模不比自己那几只自选股大，说明全市场扫描没起来
    # （engine.run_forever 用的是同一个判据）。
    watch_only_rounds = sum(1 for r in rows if r.get("watchlist_only"))

    # ---- WP05：观测账本聚合 ------------------------------------------------
    def _row_index(r: dict, pos: int) -> Any:
        """轮号：优先用样本自带的 index；没有/脏了就退回 1 基位置（不抛）。"""
        idx = r.get("index")
        if isinstance(idx, bool) or idx is None:
            return pos + 1
        try:
            return int(idx)
        except (TypeError, ValueError):
            return pos + 1

    obs_rounds = 0
    coverage_vals: list[float] = []              # 只有**有效** coverage 才进
    ranked: list[tuple[float, int, dict]] = []   # (coverage, 位置, 轮样本)
    coverage_unreadable_rounds: list[Any] = []   # 采到了但值坏（None/NaN/非数）
    totals = {"requested_total": 0, "returned_total": 0, "admitted_total": 0}
    reject_totals = {out_key: 0 for _in_key, out_key in _REJECT_TOTAL_KEYS}
    reject_totals["stale_total"] = 0
    reject_rounds = {out_key: 0 for out_key in reject_totals}
    source_mix: dict[str, int] = {}
    source_unknown_rounds = 0
    unavailable_total = 0
    unavailable_rounds = 0
    unavailable_by_reason: dict[str, int] = {}
    capability_missing: dict[str, int] = {}      # {能力名: 有多少轮该源不提供}
    # --- WP03：逐 signal 可评估率（新口径，替代整类轮比例当判据） --------
    # 形状：{signal: {"considered": int, "evaluable": int, ...}}
    # 为什么按 signal 而不是按轮：不同 signal 需要不同 capability
    # （volume_burst 要 turnover，spirit_order.big_bid_wall 要 depth_l5），
    # 把它们并成一个轮级布尔就是把"少判一项"夸大成"整类全废"。
    ev_total: dict[str, int] = {}                # signal -> considered
    ev_evaluable: dict[str, int] = {}            # signal -> evaluable
    ev_blocked: dict[str, int] = {}              # signal -> blocked_capability
    ev_advisory: dict[str, int] = {}             # signal -> advisory_missing
    ev_hits: dict[str, int] = {}                 # signal -> hit_candidates
    ev_published: dict[str, int] = {}            # signal -> published（兼容别名）
    # IT-P1-EVAL-PUBLISH-001：交付阶段链三级。
    # ``published`` 只是 ``rule_selected`` 的别名，**不能**代表交付；
    # 用户真正收到多少看 ``ev_committed``。
    ev_selected: dict[str, int] = {}             # signal -> rule_selected
    ev_bus: dict[str, int] = {}                  # signal -> bus_accepted
    ev_committed: dict[str, int] = {}            # signal -> committed
    ev_blocked_by_reason: dict[str, int] = {}    # reason -> 次数（跨 signal 汇总）
    ev_rounds = 0                                # 真的带了 signal_evaluability 的轮数

    # --- IT-P1-SOAK-LEDGER-BLIND-001：**新**交付账本的会话累计 -------------
    # 为什么必须有这一层：``signal_evaluability`` 只覆盖 2/7 规则（9.1%），
    # 而新的 ``signal_delivery`` sidecar 覆盖全部带 signal_id 的告警。
    # 若 soak 只累加旧账本，会得出"系统只交付了 6 条"的**错误**结论 ——
    # 实际交付 66 条。两个账本都要留，且必须**分别命名**，不能混成一个数。
    dl_selected: dict[str, int] = {}             # signal -> rule_selected
    dl_ignored: dict[str, int] = {}              # signal -> global_ignored
    dl_bus: dict[str, int] = {}                  # signal -> bus_accepted
    dl_committed: dict[str, int] = {}            # signal -> committed
    dl_rounds = 0                                # 带了 signal_delivery 的轮数
    # 全局门禁的会话累计：真实 committed 总数 vs 带 signal_id 的数。
    # 用**求和**而不是"取最后一轮"：逐轮口径在末轮无告警时会自然归零
    # （IT-P1-DELIVERY-GATE-PEROUND-001），会话级必须独立累加。
    dl_acct_total = 0                            # Σ committed_alerts_total
    dl_acct_named = 0                            # Σ committed_with_signal_id
    dl_acct_errors = 0                            # Σ accounting_errors
    acct_status_bad_rounds: list[int] = []       # accounting_status 不自洽的轮号
    acct_status_seen: dict[str, int] = {}        # status -> 出现轮数
    delivery_present_rounds = 0                  # delivery_accounting 出现的轮数

    for pos, r in enumerate(rows):
        present = _round_observation_fields(r)
        if not present:
            continue                              # 旧版本样本：不参与观测聚合
        obs_rounds += 1

        # requested/returned/admitted：同一段管道的三个截面。缺的那个按 0 计，
        # 但"缺"本身由 presence 记账，判定方能看到有多少轮真的带了这段账本。
        for key, out_key in (("requested", "requested_total"),
                             ("returned", "returned_total"),
                             ("admitted", "admitted_total")):
            if key in present:
                totals[out_key] += _safe_int(r.get(key))

        if "coverage" in present:
            raw = r.get("coverage")
            if raw is None or not _finite(raw):
                # 采到了字段但值是坏的 —— 既不能算 0 分，也不能算 100%。
                # 单独记账，判定时按"读不出覆盖率"处理。
                coverage_unreadable_rounds.append(_row_index(r, pos))
            else:
                cov = float(raw)
                coverage_vals.append(cov)
                ranked.append((cov, pos, r))

        # 拒绝构成。两个键都在的旧样本不会双计：新名优先，旧名只在
        # 新名缺席时读（与 make_round_sample 的读取顺序一致）。
        for in_key, out_key in _REJECT_TOTAL_KEYS:
            if in_key in present:
                n = _safe_int(r.get(in_key))
            elif in_key == "future_rejected" and "stale_rejected" in present:
                n = _safe_int(r.get("stale_rejected"))
            else:
                continue
            reject_totals[out_key] += n
            if n > 0:
                reject_rounds[out_key] += 1
        # 旧样本里显式写下的 stale_rejected（新样本已无此路径，不会双计）。
        if "stale_rejected" in present and "future_rejected" not in present:
            stale = _safe_int(r.get("stale_rejected"))
            reject_totals["stale_total"] += stale
            if stale > 0:
                reject_rounds["stale_total"] += 1

        # source mix：本轮是哪家源在供数（空/缺失归 unknown，不猜）。
        if "source" in present:
            src = str(r.get("source") or "").strip()
            if src:
                source_mix[src] = source_mix.get(src, 0) + 1
            else:
                source_unknown_rounds += 1

        if "unavailable_capability" in present:
            n = _safe_int(r.get("unavailable_capability"))
            unavailable_total += n
            if n > 0:
                unavailable_rounds += 1
        if "unavailable_by_reason" in present:
            reasons = r.get("unavailable_by_reason")
            if isinstance(reasons, dict):
                for k, v in reasons.items():
                    unavailable_by_reason[str(k)] = (
                        unavailable_by_reason.get(str(k), 0) + _safe_int(v))
        caps = r.get("capabilities") if "capabilities" in present else None
        if isinstance(caps, dict):
            for key, val in caps.items():
                if key == "source":
                    continue
                if val is False:
                    capability_missing[str(key)] = capability_missing.get(str(key), 0) + 1

        # ---- WP03：逐 signal 可评估率 ----------------------------------
        # 轮样本里的 ``signal_evaluability`` 是每轮 ``as_dict()`` 的结果，
        # 逐轮**相加**得到整场 soak 的 considered/evaluable 总量。
        # 注意相加前先做类型校验：脏值只能被跳过，不能被当 0 累加
        # （当 0 会把一条"读不出"的轮稀释成"这轮没标的需要评估"）。
        ev = r.get("signal_evaluability")
        if isinstance(ev, dict) and ev:
            ev_rounds += 1
            for sig, cell in ev.items():
                if not isinstance(cell, dict):
                    continue
                s_key = str(sig)
                c = _safe_int(cell.get("considered"))
                e = _safe_int(cell.get("evaluable"))
                b = _safe_int(cell.get("blocked_capability"))
                # 只接受自洽的单元：evaluable + blocked == considered。
                # 不自洽说明写入方有问题 —— 宁可丢弃也不要把坏数带进判定。
                if c < 0 or e < 0 or b < 0 or e + b != c:
                    continue
                ev_total[s_key] = ev_total.get(s_key, 0) + c
                ev_evaluable[s_key] = ev_evaluable.get(s_key, 0) + e
                ev_blocked[s_key] = ev_blocked.get(s_key, 0) + b
                ev_advisory[s_key] = ev_advisory.get(s_key, 0) + _safe_int(
                    cell.get("advisory_missing"))
                ev_hits[s_key] = ev_hits.get(s_key, 0) + _safe_int(
                    cell.get("hit_candidates"))
                ev_published[s_key] = ev_published.get(s_key, 0) + _safe_int(
                    cell.get("published"))
                # IT-P1-EVAL-PUBLISH-001：交付三级各自累计。
                # 读不到时**退回 published**（旧轮样本只有这一个键）——
                # 不能退成 0，否则旧样本会被读成"一条都没交付"。
                _legacy_pub = _safe_int(cell.get("published"))
                ev_selected[s_key] = ev_selected.get(s_key, 0) + (
                    _safe_int(cell.get("rule_selected"))
                    if "rule_selected" in cell else _legacy_pub)
                ev_bus[s_key] = ev_bus.get(s_key, 0) + (
                    _safe_int(cell.get("bus_accepted"))
                    if "bus_accepted" in cell else _legacy_pub)
                ev_committed[s_key] = ev_committed.get(s_key, 0) + (
                    _safe_int(cell.get("committed"))
                    if "committed" in cell else _legacy_pub)
                reasons = cell.get("blocked_reasons")
                if isinstance(reasons, dict):
                    for rk, rv in reasons.items():
                        k = str(rk)
                        # advisory 明细也在这张表里（带 advisory/ 前缀），
                        # 分开统计，避免把它误当"阻断原因"。
                        ev_blocked_by_reason[k] = (
                            ev_blocked_by_reason.get(k, 0) + _safe_int(rv))

        # ---- IT-P1-SOAK-LEDGER-BLIND-001：新交付账本累计 -------------------
        # 与上面 eval 账本**分开**累加，绝不合并成一个"committed" ——
        # 两者覆盖的规则集合不同（eval 2/7，delivery 8/8），合并会让
        # "交付了多少"这个数既不是这个也不是那个。
        dl = r.get("signal_delivery")
        if isinstance(dl, dict) and dl:
            dl_rounds += 1
            for sig, cell in dl.items():
                if not isinstance(cell, dict):
                    continue
                s_key = str(sig)
                sel = _safe_int(cell.get("rule_selected"))
                ign = _safe_int(cell.get("global_ignored"))
                bus = _safe_int(cell.get("bus_accepted"))
                cmt = _safe_int(cell.get("committed"))
                # 只接受自洽单元：bus <= sel - ign 且 cmt <= bus。
                # 不自洽说明写入方坏了 —— 宁可丢弃也不把坏数带进判定。
                if sel < 0 or ign < 0 or bus < 0 or cmt < 0:
                    continue
                if bus > sel - ign or cmt > bus:
                    continue
                dl_selected[s_key] = dl_selected.get(s_key, 0) + sel
                dl_ignored[s_key] = dl_ignored.get(s_key, 0) + ign
                dl_bus[s_key] = dl_bus.get(s_key, 0) + bus
                dl_committed[s_key] = dl_committed.get(s_key, 0) + cmt

        # ---- IT-P1-DELIVERY-FALSE-GREEN-001：记账自洽性跨轮检查 -----------
        # 门禁 ``first_party_committed_without_signal_id == 0`` **不足以**
        # 证明账本健康（分母被吞时它也是 0）。这里把 status 单独统计，
        # 只要有一轮 inconsistent，soak 就必须报出来，不能只看门禁。
        acct = r.get("delivery_accounting")
        if isinstance(acct, dict) and acct:
            delivery_present_rounds += 1
            dl_acct_total += _safe_int(acct.get("committed_alerts_total"))
            dl_acct_named += _safe_int(acct.get("committed_with_signal_id"))
            dl_acct_errors += _safe_int(acct.get("accounting_errors"))
            status = str(acct.get("accounting_status") or "")
            if status:
                acct_status_seen[status] = acct_status_seen.get(status, 0) + 1
            if status == "inconsistent":
                acct_status_bad_rounds.append(_row_index(r, pos))

    # ---- 最差 N 轮：按 coverage 升序（同分保持轮号稳定），只收有效 coverage --
    ranked.sort(key=lambda t: (t[0], t[1]))
    worst: list[dict] = []
    for cov, pos, r in ranked[:max(0, int(worst_n))]:
        worst.append({
            "index": _row_index(r, pos),
            "coverage": round(cov, 4),
            "requested": _safe_int(r.get("requested")),
            "returned": _safe_int(r.get("returned")),
            "admitted": _safe_int(r.get("admitted")),
            "source": str(r.get("source") or ""),
            "missing": _safe_int(r.get("unknown_missing")),
            "quality": _safe_int(r.get("rejected_quality")),
            "future": _safe_int(r.get("future_rejected")),
            "ooo": _safe_int(r.get("out_of_order_rejected")),
            "unavailable_capability": _safe_int(r.get("unavailable_capability")),
        })

    def _r4(v: float | None) -> float | None:
        return None if v is None else round(float(v), 4)

    return {
        "rounds": len(rows),
        "latency_ms": latency_stats(lat),
        "error_rounds": sum(1 for r in rows if r.get("error")),
        "no_data_rounds": sum(1 for r in rows if int(r.get("quotes") or 0) <= 0),
        "alerts_total": sum(len(r.get("kinds") or []) for r in rows),
        "alerts_by_kind": by_kind,
        "quotes": _edge(quotes),
        "universe": _edge(universe),
        # --- WP02 / IT-P1-HEALTH-COVERAGE-SNAPSHOT-PRELOOP-001 ---------------
        # 会话级股票池新鲜度。**必须**有这一层：`setup` 只在 soak 前取一次
        # universe_truth，中途发生的刷新失败**永远进不了最终判决**。
        # 这里给出"最新"与"最坏"两口径，判决读最坏的那个。
        "universe_session": _universe_session(rows),
        "watchlist_only_rounds": watch_only_rounds,
        "fell_back_to_watchlist": bool(rows) and watch_only_rounds >= len(rows),
        "memory_samples": [
            {
                "quotes": int(r.get("quotes") or 0),
                "history_points": int(r.get("history_points") or 0),
                "history_codes": int(r.get("history_codes") or 0),
                "max_deque": int(r.get("max_deque") or 0),
                "history_maxlen": int(r.get("history_maxlen") or 0),
            }
            for r in rows
        ],
        # ---- WP05 观测聚合 -------------------------------------------------
        "rounds_with_observation": obs_rounds,
        "coverage_rounds": len(coverage_vals),
        # 有效 coverage 一个都没有 -> None（**不是 0.0**）：0.0 会让判定误以为
        # "覆盖率真的为 0"，None 才是"这次没读到"。
        "coverage": _r4(sum(coverage_vals) / len(coverage_vals))
        if coverage_vals else None,
        "coverage_p05": _r4(percentile(coverage_vals, 5)),
        "coverage_p50": _r4(percentile(coverage_vals, 50)),
        "coverage_min": _r4(min(coverage_vals)) if coverage_vals else None,
        "coverage_max": _r4(max(coverage_vals)) if coverage_vals else None,
        "coverage_unreadable_rounds": coverage_unreadable_rounds,
        **totals,
        **reject_totals,
        "rejection_rounds": reject_rounds,
        "source_mix": source_mix,
        "source_mix_rounds": sum(source_mix.values()),
        "source_mix_unknown_rounds": source_unknown_rounds,
        "capability_unavailable_total": unavailable_total,
        "capability_unavailable_rounds": unavailable_rounds,
        # 分母用"带账本的轮数"而不是全部轮：旧样本/失败轮没账本，
        # 不该把它们算成"能力正常"而稀释掉真实缺口。
        #
        # **WP03 起这个比例只是诊断量，不再是 health 判据** ——
        # 它的分母是"轮"，而"整类规则能否评估"的分母必须是"标的"。
        # 5000 码里每轮只坏 1 个，这里给 1.0，但真实可评估率 99.98%。
        # 判定改看下面的 ``signal_evaluability``。
        "capability_unavailable_ratio": (
            round(unavailable_rounds / obs_rounds, 4) if obs_rounds else None),
        "capability_missing_rounds": capability_missing,
        "unavailable_by_reason": unavailable_by_reason,
        # --- WP03：逐 signal 可评估率（新判据的真值来源） ----------------
        "signal_evaluability": {
            sig: {
                "considered": ev_total.get(sig, 0),
                "evaluable": ev_evaluable.get(sig, 0),
                "blocked_capability": ev_blocked.get(sig, 0),
                "advisory_missing": ev_advisory.get(sig, 0),
                "hit_candidates": ev_hits.get(sig, 0),
                # 交付三级（IT-P1-EVAL-PUBLISH-001）
                "rule_selected": ev_selected.get(sig, 0),
                "bus_accepted": ev_bus.get(sig, 0),
                "committed": ev_committed.get(sig, 0),
                "dropped_by_bus": max(
                    ev_selected.get(sig, 0) - ev_bus.get(sig, 0), 0),
                # 兼容别名：== rule_selected。**不是**交付数。
                "published": ev_published.get(sig, 0),
                # 没有分母时是 None（not_measured），**不是 0.0** ——
                # 填 0 会把"这一项没测"误报成"整类失效"。
                "evaluable_coverage": (
                    round(ev_evaluable.get(sig, 0) / ev_total[sig], 6)
                    if ev_total.get(sig, 0) > 0 else None),
            }
            for sig in sorted(ev_total)
        },
        "signal_evaluability_rounds": ev_rounds,
        "evaluability_considered_total": sum(ev_total.values()),
        "evaluability_evaluable_total": sum(ev_evaluable.values()),
        "evaluability_blocked_total": sum(ev_blocked.values()),
        "evaluability_advisory_total": sum(ev_advisory.values()),
        "evaluability_hit_candidates_total": sum(ev_hits.values()),
        "evaluability_published_total": sum(ev_published.values()),
        # IT-P1-EVAL-PUBLISH-001：交付三级总量。
        # ``evaluability_published_total`` == ``evaluability_rule_selected_total``
        # （别名），它**不是**交付数；真正交付看 ``evaluability_committed_total``。
        "evaluability_rule_selected_total": sum(ev_selected.values()),
        "evaluability_bus_accepted_total": sum(ev_bus.values()),
        "evaluability_committed_total": sum(ev_committed.values()),
        "evaluability_dropped_by_bus_total": sum(
            max(ev_selected.get(s, 0) - ev_bus.get(s, 0), 0)
            for s in ev_selected),
        "evaluability_blocked_by_reason": ev_blocked_by_reason,
        # --- IT-P1-SOAK-LEDGER-BLIND-001：**新交付账本**的会话累计 ---------
        # 与上面的 evaluability 分开命名、分开统计。为什么不能合并：
        # eval 账本只覆盖 2/7 规则（9.1%），delivery 覆盖 8/8（100%）。
        # 合并会让"交付了多少"既不是 6 也不是 66，而是一个无意义的中间数。
        #
        # ⚠ 命名纪律（IT-P1-DELIVERY-GATE-PEROUND-001）：
        # ``delivery_accounting`` 是**逐轮**口径；这里是**会话累计**。
        # 消费方必须看清后缀，不要拿逐轮值当累计值。
        "signal_delivery": {
            sig: {
                "rule_selected": dl_selected.get(sig, 0),
                "global_ignored": dl_ignored.get(sig, 0),
                "bus_accepted": dl_bus.get(sig, 0),
                "committed": dl_committed.get(sig, 0),
                "dropped_by_bus": max(
                    dl_selected.get(sig, 0) - dl_ignored.get(sig, 0)
                    - dl_bus.get(sig, 0), 0),
                "committed_ratio": (
                    round(dl_committed.get(sig, 0) / dl_selected[sig], 6)
                    if dl_selected.get(sig, 0) > 0 else None),
            }
            for sig in sorted(dl_selected)
        },
        "signal_delivery_rounds": dl_rounds,
        "delivery_rule_selected_total": sum(dl_selected.values()),
        "delivery_global_ignored_total": sum(dl_ignored.values()),
        "delivery_bus_accepted_total": sum(dl_bus.values()),
        # 会话级**真实**交付总数（新账本口径）。这是回答"用户收到多少条"的
        # 正确分母，也是 T+5/T+30 标签该引用的集合。
        "delivery_committed_total": sum(dl_committed.values()),
        "delivery_committed_signals": sum(1 for v in dl_committed.values() if v > 0),
        "delivery_dropped_by_bus_total": sum(
            max(dl_selected.get(s, 0) - dl_ignored.get(s, 0) - dl_bus.get(s, 0), 0)
            for s in dl_selected),
        # 全局门禁的会话累计（独立于上面 sidecar 之和 —— 分母由 Engine 数）。
        "delivery_accounting_session": {
            "committed_alerts_total": dl_acct_total,
            "committed_with_signal_id": dl_acct_named,
            "signed_ratio": (
                round(dl_acct_named / dl_acct_total, 6)
                if dl_acct_total > 0 else None),
            "unsigned_total": max(dl_acct_total - dl_acct_named, 0),
            # IT-P1-DELIVERY-FALSE-GREEN-001：门禁为 0 **不足以**证明健康。
            "accounting_errors": dl_acct_errors,
            "accounting_status_seen": acct_status_seen,
            "inconsistent_rounds": acct_status_bad_rounds,
            "delivery_accounting_rounds": delivery_present_rounds,
            # 只要有一轮不自洽（或吞过异常），整体就不是 ok。
            "accounting_status": (
                "inconsistent"
                if (acct_status_bad_rounds or dl_acct_errors > 0)
                else ("ok" if dl_acct_total > 0 else "not_measured")),
        },
        "worst_rounds": worst,
    }


def check_memory(samples: Sequence[dict], *, growth_limit: float = MEM_GROWTH_LIMIT,
                 slack: int = MEM_GROWTH_SLACK) -> dict:
    """内存代理指标的有界性判定（纯函数）。

    为什么不看"历史点数首末比值"：那是**预期增长**而不是泄漏 —— state.history
    每个 code 一个 deque，跑 1 分钟就从 1 个点涨到 12 个点（12 倍），可它由
    ``maxlen=history_len`` 硬性封顶，永远不会无界。所以判据是三条真正有意义的：

    1. ``max_deque <= maxlen``：deque 没超上限（这是"有界"的**充分**证据）；
    2. ``history_points <= history_codes * maxlen``：单只股票的点数不超过上限；
    3. 股票池规模首末对比不爆炸（唯一可能真正无界增长的是 key 的数量）。
    """
    rows = [s for s in (samples or []) if isinstance(s, dict)]
    if not rows:
        return {"bounded": False, "reason": "无样本，无法判定", "samples": 0,
                "deques_ok": False, "density_ok": False, "codes_ok": False}

    first, last = rows[0], rows[-1]
    maxlen = max(int(s.get("history_maxlen") or 0) for s in rows)
    limit = max(maxlen, 30)          # engine 里 deque 是 maxlen=max(history_len, 30)
    max_deque = max(int(s.get("max_deque") or 0) for s in rows)

    quotes_first = int(first.get("quotes") or 0)
    quotes_last = int(last.get("quotes") or 0)
    pts_last = int(last.get("history_points") or 0)
    codes_last = max(int(last.get("history_codes") or 0), 1)

    deques_ok = max_deque <= limit
    density_ok = pts_last <= codes_last * limit
    codes_ok = quotes_last <= max(quotes_first, 1) * float(growth_limit) + int(slack)

    reasons = []
    if not deques_ok:
        reasons.append(f"单只历史点数 {max_deque} 超过上限 {limit}")
    if not density_ok:
        reasons.append(f"历史点总数 {pts_last} > 代码数 {codes_last}×{limit}")
    if not codes_ok:
        reasons.append(f"股票池 {quotes_first} -> {quotes_last} 增长超 {growth_limit}×")
    return {
        "bounded": bool(deques_ok and density_ok and codes_ok),
        "reason": "；".join(reasons) if reasons else "历史窗口有界，股票池无爆炸",
        "samples": len(rows),
        "history_maxlen": limit,
        "max_deque": max_deque,
        "quotes_first": quotes_first,
        "quotes_last": quotes_last,
        "history_points_first": int(first.get("history_points") or 0),
        "history_points_last": pts_last,
        "history_codes_last": int(last.get("history_codes") or 0),
        "deques_ok": bool(deques_ok),
        "density_ok": bool(density_ok),
        "codes_ok": bool(codes_ok),
    }


def empty_metrics() -> dict:
    """一份"健康骨架"指标。离线单测直接改其中一项即可模拟某种失败模式。"""
    return {
        "rounds": 0,
        "latency_ms": {"count": 0, "min": None, "median": None, "p95": None, "max": None},
        "error_rounds": 0,
        "no_data_rounds": 0,
        "alerts_total": 0,
        "alerts_by_kind": {},
        "quotes": {"first": 0, "last": 0, "min": 0, "max": 0},
        "universe": {"first": 0, "last": 0, "min": 0, "max": 0},
        "watchlist_only_rounds": 0,
        "fell_back_to_watchlist": False,
        # WP05：观测账本骨架。None 一律表示"没读到"，不是"读到了 0"。
        "rounds_with_observation": 0,
        "coverage_rounds": 0,
        "coverage": None,
        "coverage_p05": None,
        "coverage_p50": None,
        "coverage_min": None,
        "coverage_max": None,
        "coverage_unreadable_rounds": [],
        "requested_total": 0,
        "returned_total": 0,
        "admitted_total": 0,
        "missing_total": 0,
        "quality_total": 0,
        "future_total": 0,
        "stale_total": 0,
        "ooo_total": 0,
        "rejection_rounds": {},
        "source_mix": {},
        "source_mix_rounds": 0,
        "source_mix_unknown_rounds": 0,
        "capability_unavailable_total": 0,
        "capability_unavailable_rounds": 0,
        "capability_unavailable_ratio": None,
        "capability_missing_rounds": {},
        "unavailable_by_reason": {},
        # WP03：逐 signal 可评估率。空 dict 表示"没有逐 signal 数据"
        # （旧报告），判定方据此退回旧口径且只到 warn（见 evaluate_health）。
        "signal_evaluability": {},
        "signal_evaluability_rounds": 0,
        "evaluability_considered_total": 0,
        "evaluability_evaluable_total": 0,
        "evaluability_blocked_total": 0,
        "evaluability_advisory_total": 0,
        "evaluability_hit_candidates_total": 0,
        "evaluability_published_total": 0,
        # IT-P1-EVAL-PUBLISH-001：交付三级总量也必须在这里出现，
        # 否则"空视图"与"正常视图"的 schema 不一致，消费方要写两套取值逻辑。
        "evaluability_rule_selected_total": 0,
        "evaluability_bus_accepted_total": 0,
        "evaluability_committed_total": 0,
        "evaluability_dropped_by_bus_total": 0,
        "evaluability_blocked_by_reason": {},
        # IT-P1-SOAK-LEDGER-BLIND-001：新交付账本的空视图（与正常视图同 schema）。
        "signal_delivery": {},
        "signal_delivery_rounds": 0,
        "delivery_rule_selected_total": 0,
        "delivery_global_ignored_total": 0,
        "delivery_bus_accepted_total": 0,
        "delivery_committed_total": 0,
        "delivery_committed_signals": 0,
        "delivery_dropped_by_bus_total": 0,
        "delivery_accounting_session": {
            "committed_alerts_total": 0,
            "committed_with_signal_id": 0,
            "signed_ratio": None,
            "unsigned_total": 0,
            "accounting_errors": 0,
            "accounting_status_seen": {},
            "inconsistent_rounds": [],
            "delivery_accounting_rounds": 0,
            "accounting_status": "not_measured",
        },
        "worst_rounds": [],
        "memory": {"bounded": True, "reason": "（未采样）"},
        "api": {"requests": 0, "non_200": 0, "malformed": 0, "unreachable": 0, "by_route": {}},
        "sse": {"connected": False, "stalled": False, "events_total": 0, "events_by_type": {},
                "status": None, "error": None},
        "logs": {"WARNING": 0, "ERROR": 0, "CRITICAL": 0},
        "browser": {"ran": False, "skipped": True, "errors": []},
        # `universe_size` 用 **`None`** = 未测量（骨架/空会话 = 没有采集过）。
        # 写 `0` 会把"骨架/未采集"伪装成"**明确测到 0 只票**"，
        # 而 0 现在是**明确坏**（一只票都没扫）。
        # `IT-P1-UNIVERSE-T0-MEASURED-ZERO-READ-AS-MISSING-001` 的三态纪律：
        # `None != 0`。
        "setup": {"engine_build_s": None, "universe_refresh_s": None,
                  "universe_size": None},
    }


def finalize_metrics(
    rounds: Sequence[dict],
    *,
    api: dict | None = None,
    sse: dict | None = None,
    logs: dict | None = None,
    browser: dict | None = None,
    setup: dict | None = None,
) -> dict:
    """逐轮样本 + 看板/SSE/日志统计 -> 完整的 metrics（纯函数）。

    各子项用 ``empty_metrics()`` 的默认值兜底：某个探针没能启动时（例如端口被占），
    报告仍然是完整形状，不会因为缺 key 让下游判定 KeyError。
    """
    out = empty_metrics()
    out.update(summarize_rounds(rounds))
    out["memory"] = check_memory(out.pop("memory_samples", []))
    for key, val in (("api", api), ("sse", sse), ("logs", logs),
                     ("browser", browser), ("setup", setup)):
        if isinstance(val, dict):
            merged = dict(out[key])
            merged.update(val)
            out[key] = merged
    return out


def evaluate_health(metrics: dict, *, tolerances: dict | None = None) -> dict:
    """指标 -> 结论（纯函数）。返回 ``{"healthy", "exit_code", "checks"}``。

    判定项只有"真的出错"的那几类，**告警条数不参与判定**（休市 0 条是正常的）。
    每项都带 ``detail``，因为"不健康"这三个字对使用者毫无信息量，必须说清哪项、
    差多少 —— 尤其是失败轮数 3 轮、容差 1 轮这种"只差一点"的情况。

    WP05 / IT-P1-OBS-011：新增两项基于 observation 的检查，让以前**静默通过**的
    两类问题显式变黄/红：

    ``coverage``
        用 ``coverage_p05`` / ``coverage_p50`` / ``coverage_min`` 三个分位判定
        （见 ``DEFAULT_OBSERVATION_TOLERANCES`` 的取值依据）。**刻意不要求
        100%** —— 一轮要请求 5000+ 只票，停牌/新股/源端漏发让它永远到不了 100%。
        但 93% 这种 soft-partial 必须报出来：``coverage_p50`` 掉到 warn/fail 阈值
        以下时该项变黄/红。
    ``capability``
        每轮 ``unavailable_capability > 0`` 的比例。只要出现就 WARN（能力缺失是
        硬缺口，不是网络抖动）；**每一轮都不可评估**（比例 >= fail 阈值）说明整类
        规则在这整场 soak 里一次都没被验证过，判红。

    三级结论通过 ``checks[*]["level"]`` 暴露（``ok`` / ``warn`` / ``fail``）：
    ``ok`` 仍是布尔，保持既有消费方（``healthy = all(ok)``）不变；``warn``
    只降级不判死 —— 退出码仍是 0，但报告里看得见。

    阈值全部可配置：``tolerances`` 里传入下列任一键即可覆盖默认值 ——
    ``coverage_warn_p05`` / ``coverage_fail_p05`` / ``coverage_warn_p50`` /
    ``coverage_fail_p50`` / ``coverage_warn_min`` / ``coverage_fail_min`` /
    ``unavailable_warn_ratio`` / ``unavailable_fail_ratio`` /
    ``require_observation``。
    """
    tol = dict(DEFAULT_TOLERANCES)
    tol.update(tolerances or {})
    m = metrics if isinstance(metrics, dict) else {}
    rounds = int(m.get("rounds") or 0)
    error_rounds = int(m.get("error_rounds") or 0)
    no_data_rounds = int(m.get("no_data_rounds") or 0)
    api = dict(m.get("api") or {})
    sse = dict(m.get("sse") or {})
    memory = dict(m.get("memory") or {})
    browser = dict(m.get("browser") or {})
    by_kind = dict(m.get("alerts_by_kind") or {})
    quotes_max = int((m.get("quotes") or {}).get("max") or 0)

    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, *, level: str | None = None) -> None:
        """记一项检查。``ok`` 保持布尔（既有消费方不变），``level`` 额外表达黄。"""
        lvl = level or ("ok" if ok else "fail")
        checks.append({"name": name, "ok": bool(ok), "level": lvl, "detail": detail})

    def _num(key: str) -> float | None:
        """读一个可配置阈值。

        脏值（None / 非数字）**退回默认值**，不是退回 None：``None`` 会
        静默关掉这一项判定，而"阈值写坏了"正是最该照默认档干活的时候
        （见 ``test_missing_threshold_key_falls_back_to_default``）。
        只有默认值也不存在时才返回 None。
        """
        default = DEFAULT_OBSERVATION_TOLERANCES.get(key)
        try:
            v = tol.get(key)
        except (AttributeError, TypeError):
            v = None
        if v is not None:
            try:
                f = float(v)
            except (TypeError, ValueError):
                f = None
            if f is not None and math.isfinite(f):
                return f
        try:
            return float(default) if default is not None else None
        except (TypeError, ValueError):
            return None

    def _metric_float(key: str) -> float | None:
        """读一个指标里的覆盖率；缺键/None/脏值一律 None（不是 0.0）。

        WP05 的关键一步：**旧版本 soak 报告没有这个键**，此时必须说"读不出"，
        绝不能把它当成 0% 覆盖率判红（那会把一份好报告打成不健康）。
        """
        if key not in m:
            return None
        raw = m.get(key)
        if raw is None or isinstance(raw, bool):
            return None
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return None
        return val if math.isfinite(val) else None

    min_rounds = int(tol.get("min_rounds") or 1)
    add("rounds", rounds >= min_rounds, f"完成 {rounds} 轮（下限 {min_rounds}）")

    ratio = float(tol.get("max_error_ratio") or 0.0)
    budget = int(rounds * ratio)
    bad = error_rounds + no_data_rounds
    add("fetch", bad <= budget,
        f"抓取失败 {error_rounds} 轮 + 空数据 {no_data_rounds} 轮 = {bad}，"
        f"容差 {budget} 轮（{ratio:.0%}）")

    add("data", quotes_max > 0, f"单轮最多拿到 {quotes_max} 只行情")

    # ---- WP05：观测覆盖率判定（分位，不看均值） --------------------------
    obs_rounds = int(m.get("rounds_with_observation") or 0)
    cov_rounds = int(m.get("coverage_rounds") or 0)
    cov_p05 = _metric_float("coverage_p05")
    cov_p50 = _metric_float("coverage_p50")
    cov_min = _metric_float("coverage_min")
    unreadable = m.get("coverage_unreadable_rounds") or []
    # "readable" 的判据是**指标里确实有 coverage 分位值**，而不是 rounds 计数：
    # 旧报告两者都没有，此时必须跳过而不是判红。
    have_cov = cov_p50 is not None or cov_p05 is not None or cov_min is not None

    if not have_cov:
        require = bool(tol.get("require_observation"))
        missing_detail = (
            f"无有效覆盖率读数（带账本轮数 {obs_rounds}/{rounds}"
            + (f"，其中 {len(unreadable)} 轮 coverage 值不可读" if unreadable else "")
            + "）—— 无法用真实覆盖率判定，本项跳过")
        if require:
            add("coverage", False,
                missing_detail + "；require_observation=true 要求必须有可读账本")
        else:
            add("coverage", True,
                missing_detail + "（旧版本报告/未采到，跳过不算失败）")
    else:
        w_p05 = _num("coverage_warn_p05")
        f_p05 = _num("coverage_fail_p05")
        w_p50 = _num("coverage_warn_p50")
        f_p50 = _num("coverage_fail_p50")
        w_min = _num("coverage_warn_min")
        f_min = _num("coverage_fail_min")

        def _below(val: float | None, warn: float | None,
                   fail: float | None) -> str | None:
            """返回 None=ok / 'warn' / 'fail'。fail 阈值优先（越低越糟）。"""
            if val is None:
                return None
            if fail is not None and val < fail:
                return "fail"
            if warn is not None and val < warn:
                return "warn"
            return None

        levels = {
            "p05": _below(cov_p05, w_p05, f_p05),
            "p50": _below(cov_p50, w_p50, f_p50),
            "min": _below(cov_min, w_min, f_min),
        }
        if "fail" in levels.values():
            cov_level = "fail"
        elif "warn" in levels.values():
            cov_level = "warn"
        else:
            cov_level = "ok"

        def _fmt(v: float | None) -> str:
            return "—" if v is None else f"{v:.1%}"

        bad_parts = [f"{name}={_fmt(val)}" for name, val in
                     (("p05", cov_p05), ("p50", cov_p50), ("min", cov_min))
                     if levels[name] is not None]
        detail = (
            f"覆盖率 p05/p50/min = {_fmt(cov_p05)} / {_fmt(cov_p50)} / "
            f"{_fmt(cov_min)}（{cov_rounds}/{obs_rounds} 轮有有效读数，"
            f"共 {rounds} 轮）；阈值 warn {_fmt(w_p05)}/{_fmt(w_p50)}/{_fmt(w_min)}，"
            f"fail {_fmt(f_p05)}/{_fmt(f_p50)}/{_fmt(f_min)}")
        if bad_parts:
            detail += f"；未达 warn 的分位：{', '.join(bad_parts)}"
        if unreadable:
            detail += f"（另有 {len(unreadable)} 轮 coverage 值不可读）"
        add("coverage", cov_level != "fail", detail, level=cov_level)

    # ---- WP05：能力缺失（整类规则不可评估）判定 -------------------------
    unavail_rounds = int(m.get("capability_unavailable_rounds") or 0)
    unavail_total = int(m.get("capability_unavailable_total") or 0)
    unavail_ratio = _metric_float("capability_unavailable_ratio")
    if unavail_ratio is None and obs_rounds > 0:
        unavail_ratio = unavail_rounds / obs_rounds
    u_warn = _num("unavailable_warn_ratio")
    u_fail = _num("unavailable_fail_ratio")

    # ---- WP03 / IT-P1-CAPABILITY-003：逐 signal 可评估率（**新判据**） ----
    #
    # 为什么替换旧判据：旧判据的分母是"轮"，问的是"这一轮里有没有出现过
    # 任何能力缺失"；而我们要回答的是"**这类规则到底评没评估过**"，
    # 分母必须是**标的**。5000 码里每轮坏 1 个 -> 旧口径 1.0 -> whole-class
    # FAIL，真实可评估率却是 99.98%。分母错了，阈值再怎么调都没用。
    #
    # 新判据只认一种 fail 形状：某个 signal 的 considered 全部被判 blocked
    # （coverage == 0），即"这一类规则在这整场 soak 里一次都没评过"。
    sig_ev = m.get("signal_evaluability")
    sig_cells = {str(k): v for k, v in sig_ev.items()
                 if isinstance(v, dict)} if isinstance(sig_ev, dict) else {}
    ev_warn_cov = _num("evaluability_warn_coverage")
    ev_fail_cov = _num("evaluability_fail_coverage")
    ev_min_samples = _num("evaluability_min_samples")
    min_samples = int(ev_min_samples) if ev_min_samples is not None else 200

    worst_sig: list[tuple[float, str, int, int]] = []   # (cov, signal, ev, tot)
    all_blocked: list[str] = []
    for sig, cell in sig_cells.items():
        tot = _safe_int(cell.get("considered"))
        ev = _safe_int(cell.get("evaluable"))
        if tot <= 0:
            continue                                    # not_measured：跳过
        cov = ev / tot
        worst_sig.append((cov, sig, ev, tot))
        if ev == 0:
            all_blocked.append(sig)
    worst_sig.sort(key=lambda t: (t[0], t[1]))

    considered_total = _safe_int(m.get("evaluability_considered_total"))
    evaluable_total = _safe_int(m.get("evaluability_evaluable_total"))
    blocked_total = _safe_int(m.get("evaluability_blocked_total"))
    overall_cov = (evaluable_total / considered_total
                   if considered_total > 0 else None)

    if sig_cells:
        # 有逐 signal 数据 -> 用它判，旧比例只作为诊断写进 detail。
        cap_level = "ok"
        if all_blocked:
            cap_level = "fail"          # 唯一站得住的"整类没评估"证据
        elif (ev_fail_cov is not None and ev_fail_cov > 0 and worst_sig
              and worst_sig[0][0] < ev_fail_cov):
            # IT-P1-EVAL-FAIL-COVERAGE-SILENT-001（13:32 §1 / 16:13 §9.1）：
            # 修前 `ev_fail_cov` **取出来从未被读取** —— 一个**说谎的旋钮**：
            # 运维把 `evaluability_fail_coverage: 0.5` 写进配置 **静默无效**。
            #
            # 现在真的消费它：低于该线的 signal 判 fail。
            # **默认值是 0.0**，所以"只有全部阻断才 fail"的既有行为
            # **逐字节不变**（`ev_fail_cov > 0` 才启用这条更严的判据）。
            cap_level = "fail"
        elif (ev_warn_cov is not None and worst_sig
              and worst_sig[0][0] < ev_warn_cov):
            # 有 signal 掉到 warn 线以下：黄，但**不判死**。
            cap_level = "warn"
        # 样本不足时不升级为 fail：coverage 是小样本比率，一只票坏掉就能让
        # 3 只票的 signal 从 1.0 掉到 0.67；没有足够样本时判红等于用噪声定罪。
        # 这里看的是**总可评估样本量**，与"哪个 signal 全阻断"无关 ——
        # 全阻断本身已经是明确信号，但样本量太小仍不足以支撑全场判死。
        if cap_level == "fail" and considered_total < min_samples:
            cap_level = "warn"

        cov_txt = ("—" if overall_cov is None else f"{overall_cov:.2%}")
        detail = (
            f"逐 signal 可评估率 {cov_txt}"
            f"（可评估 {evaluable_total}/{considered_total}，"
            f"因能力缺失被阻断 {blocked_total}）；"
            f"共 {len(sig_cells)} 个 signal，最差 "
            + (", ".join(f"{s}={c:.2%}({e}/{t})"
                         for c, s, e, t in worst_sig[:3]) or "—")
            + f"；阈值 warn <{ev_warn_cov:.0%}，fail=全部阻断")
        if all_blocked:
            detail += (f" —— 这些 signal 在整场 soak 里**一次都没被评估过**："
                       f"{sorted(all_blocked)[:5]}")
        reasons = dict(m.get("evaluability_blocked_by_reason") or {})
        if reasons:
            top = sorted(reasons.items(),
                         key=lambda kv: (-int(kv[1]), str(kv[0])))[:5]
            detail += f"；主要阻断原因 {dict(top)}"
        # 诊断量（不再是判据）：保留旧比例，方便与历史报告对比。
        detail += (f"（诊断：旧口径轮比例 "
                   f"{'—' if unavail_ratio is None else f'{unavail_ratio:.0%}'}"
                   f"、累计缺失标的 {unavail_total}，仅供参考）")
        add("capability", cap_level != "fail", detail, level=cap_level)
    elif obs_rounds <= 0:
        add("capability", True,
            f"无观测账本，无法判定能力缺失（unavailable 合计 {unavail_total}）"
            "（跳过不算失败）")
    else:
        # 旧版本报告：没有逐 signal 数据 -> 退回旧口径，**但只到 warn 为止**。
        # 为什么不继续判 fail：旧口径的分母（轮）不足以支撑"整类没评估"这种
        # 强结论。历史报告继续可见，但不拿它定罪。
        if (u_fail is not None and unavail_ratio is not None
                and unavail_ratio >= u_fail):
            cap_level = "warn"
        elif (u_warn is not None and unavail_ratio is not None
              and unavail_ratio > u_warn):
            cap_level = "warn"
        else:
            cap_level = "ok"
        reasons = dict(m.get("unavailable_by_reason") or {})
        detail = (
            f"旧口径（轮级）：{unavail_rounds}/{obs_rounds} 轮出现'能力缺失'"
            f"（比例 {unavail_ratio:.0%}），累计缺失标的 {unavail_total} 个；"
            "无逐 signal 可评估率数据，故只做提示不判死"
            f"（阈值 warn >{u_warn:.0%}，fail >={u_fail:.0%}）")
        if reasons:
            top = sorted(reasons.items(), key=lambda kv: (-int(kv[1]), str(kv[0])))[:5]
            detail += f"；主要原因 {dict(top)}"
        add("capability", cap_level != "fail", detail, level=cap_level)

    req = int(api.get("requests") or 0)
    non200 = int(api.get("non_200") or 0)
    bad_json = int(api.get("malformed") or 0)
    unreachable = int(api.get("unreachable") or 0)
    add("api", req > 0 and non200 == 0 and bad_json == 0 and unreachable == 0,
        f"请求 {req} 次：非 200 {non200}，JSON 解析失败 {bad_json}，连不上 {unreachable}")

    events = int(sse.get("events_total") or 0)
    add("sse", bool(sse.get("connected")) and not bool(sse.get("stalled")) and events > 0,
        f"连接 {'是' if sse.get('connected') else '否'}，事件 {events} 条，"
        f"断流 {'是' if sse.get('stalled') else '否'}")

    add("memory", bool(memory.get("bounded")), str(memory.get("reason") or ""))

    if browser.get("ran"):
        errs = list(browser.get("errors") or [])
        add("browser", not errs, f"pageerror/console.error {len(errs)} 条")
    else:
        add("browser", True, "未运行（跳过不算失败）")

    # --- R-21 / IT-P1-UNIVERSE-HEALTH-GATE-001：扫描范围必须参与判决 ------
    #
    # 为什么必须有这一项：`evaluate_health` 此前**没有任何一项**读股票池规模。
    # metrics['setup'] 里的 `universe_size` / `fell_back_to_watchlist`
    # **早已在生产路径传入**（`_run_session` -> `finalize_metrics(setup=...)`），
    # 只是**没有读者**。所以：
    #
    #   真实运行 5913 -> 4100（缺 30%）  => healthy=True / exit=0
    #
    # 与 01:00 轮修掉的交付账本假绿**完全同类**（数据算了、没人读），
    # 只是对象不同。云端 h=0 轮用真实 `Engine.refresh_universe` 证明
    # 冷启动可达，并把它从"缺判决项"精确到"**数据早已传入、只差一个读者**"。
    #
    # 判据分三档，且**绝不把"没测"判红**（与 capability/delivery_accounting 同约定）：
    #   * 降级为仅自选股（`fell_back_to_watchlist`）-> **fail**
    #     这是"全市场扫描不可用"，用户拿到的告警面会窄得多，必须点名。
    #   * 规模低于绝对下限（`UNIVERSE_MIN_ABS`）-> **fail**
    #   * 规模低于绝对下限以上但明显偏小 -> **warn**（不改退出码）
    #   * 旧报告没有 setup / `universe_size == 0` -> **ok**（跳过，不算失败）
    _setup = m.get("setup")
    # IT-P1-HEALTH-SESSION-WITHOUT-T0-001（16:13 §5）：会话事实**独立**于
    # t0 快照存在。修前 session 消费被嵌在 `if isinstance(_ut, dict) and _ut:`
    # 里 —— setup 缺 `universe_truth` 时，**整段会话判决被跳过**，
    # 实测 `worst_state=stale / rounds_transport_incomplete=99` 仍 `healthy=True`。
    # 所以这里在**任何 t0 条件之外**先取出会话事实。
    _sess = (m.get("universe_session")
             if isinstance(m.get("universe_session"), dict) else {})
    _sess_measured = bool(_sess.get("measured"))

    # --- IT-P1-UNIVERSE-WATCHLIST-FALLBACK-SESSION-BLIND-001 --------------
    #
    # 为什么独立成项：`universe` 项读的是 **t0** 的 `fell_back_to_watchlist`，
    # 而该布尔只在 **30/30 轮**全降级时才为 True
    # （`summarize_rounds`: `watch_only_rounds >= len(rows)`）。
    # 于是 soak 中途降级 —— 哪怕 **29/30 轮**都只盯自选股 —— 判决层**看不见**。
    #
    # 云端 16:13 §2 明确：不要只加 `setup OR 顶层 fell_back` 布尔，
    # **29/30 仍然会漏**。必须保留 round count / ratio / ever 三个事实。
    _w_rounds = _safe_int(_sess.get("watchlist_only_rounds"))
    _w_ratio = _sess.get("watchlist_only_ratio")
    _w_ever = _sess.get("ever_watchlist_only")
    # Session Universe Evidence Contract v1（WP01 / 20:04 §3）：
    # `IT-P2-UNIVERSE-EMPTY-SCAN-ASSERTED-AS-FULL-MARKET-001`。
    _scope_measured = _safe_int(_sess.get("scope_measured_rounds"))
    _empty_rounds = _safe_int(_sess.get("empty_scan_rounds"))
    _full_rounds = _safe_int(_sess.get("full_market_rounds"))
    _unknown_scope = _safe_int(_sess.get("unknown_scope_rounds"))
    _broad_rounds = _safe_int(_sess.get("broad_scan_unquantified_rounds"))
    # --- 死字段接活 ④：`scope_counts`（ADDENDUM3 §2.1）--------------------
    #
    # 它此前只被 `_universe_session` 内部立即拆成上面几个计数，
    # **作为产物字段零消费者**。接成**自洽性核对**：
    # 各态计数之和必须等于测量轮数。不等 => 聚合与判据**已经不一致**，
    # 此时任何基于这些计数的肯定句都不可信（这正是本文件反复强调的
    # "产物内部自相矛盾"）。
    _scope_counts_raw = _sess.get("scope_counts")
    _scope_counts = (_scope_counts_raw if isinstance(_scope_counts_raw, dict)
                     else {})
    _sc_sum = sum(v for v in _scope_counts.values()
                  if isinstance(v, (int, float))
                  and not isinstance(v, bool))
    _scope_consistent = (not _scope_counts) or _sc_sum == _scope_measured
    # `IT-P2-UNIVERSE-FULL-MARKET-IS-MAGNITUDE-BLIND-001`：
    # **全称肯定句的分母必须是会话总轮数，不是"测到的那几轮"。**
    #
    # 修前（我自己的 bf0b83b 留下的残余）我用 `_scope_measured` 当分母：
    #   1 轮有证据 + 29 轮什么都没有
    #   -> `_full_rounds == _scope_measured`（1 == 1）成立
    #   -> 输出「全程全市场扫描，**1/1** 轮有明确扫描范围证据」
    # **拿证据子集当了自己的分母**，于是"存在至少一轮全市场证据"
    # 被升级成"全程全市场"。这与 22:15 追加指认的 freshness 同一条 bug 类。
    _rounds_total = _safe_int(_sess.get("rounds_total"))
    if _rounds_total <= 0:
        _rounds_total = _scope_measured          # 老报告没有该键时的退化
    _full_covers_all = (_scope_measured > 0
                        and _full_rounds == _scope_measured
                        and _scope_measured == _rounds_total
                        and _scope_consistent)
    # 显式 run mode 例外：`--watch-only` 是**用户指定只盯自选股**，
    # 不是故障。只能靠显式标记识别，绝不能靠"股票数很少"猜意图。
    _watch_only_mode = bool(_setup.get("watch_only")) if isinstance(_setup, dict) else False
    if _watch_only_mode:
        add("universe_scope", True,
            "本次以**显式 `--watch-only` 模式**运行：只盯自选股是预期行为，"
            "不判失败", level="ok")
    elif _w_ever is None:
        # ⚠ **这一支是活代码，绝不能删或合并**（ADDENDUM2 §1 撤回"永不可达"）：
        # 实测 `data/live_session_*.json` **7/7** 都没有 `universe_session` 键，
        # 全部走这里输出诚实的"无法判定"。删掉它 = 把唯一的诚实旧报告路径
        # 换成下面的肯定句 = **引入回归**。
        add("universe_scope", True,
            "无会话级扫描范围记录（旧报告/未采集），无法判定（跳过不算失败）",
            level="ok")
    elif _w_ever:
        _ratio_s = (f"{float(_w_ratio):.1%}"
                    if isinstance(_w_ratio, (int, float))
                    and not isinstance(_w_ratio, bool) else "未知")
        # 只要**曾经**降级过，对"全市场事件"就是硬缺口 —— 该轮扫描面窄得多，
        # 那段时间没有告警**不能**解释为没有异动。
        add("universe_scope", False,
            f"会话期间有 {_w_rounds} 轮**降级为仅自选股**（占比 {_ratio_s}）"
            f"—— 这些轮次对全市场事件是硬缺口，此期间的'没有告警'"
            f"不能解释为'没有异动'", level="fail")
    elif _empty_rounds > 0:
        # **空扫描**：股票池被清空（`active_scan_codes == 0`），但 `state.quotes`
        # 可能还留着旧缓存，于是 `quotes` 看起来正常。
        # 旧口径下它与"全程全市场"**都是 `watchlist_only=False`，不可区分** ——
        # 这正是本缺陷的本体：一个空扫描被肯定成"全程全市场扫描"。
        add("universe_scope", False,
            f"会话期间有 {_empty_rounds} 轮**活跃股票池为空**"
            f"（扫描集合 0 只，共测到 {_scope_measured} 轮）—— "
            f"这些轮次**一只票都没扫**，此期间的'没有告警'"
            f"不能解释为'没有异动'", level="fail")
    elif _full_covers_all:
        # **只有**「测到了**全部**轮次、且每一轮的量级都证得出全市场」
        # 才允许全称肯定句。
        add("universe_scope", True,
            f"会话期间未降级为仅自选股（全程全市场扫描，"
            f"{_full_rounds}/{_rounds_total} 轮有量级证据）",
            level="ok")
    elif _full_rounds > 0:
        # ⚠ **本轮的残余修复**：有全市场轮次、但证据**没有覆盖全部轮次**，
        # 或部分轮次只够"扫得广"。**必须写出真实的 k/N**，
        # 不能把"1/30 轮有全市场证据"说成"全程全市场"。
        _gaps = []
        if _broad_rounds > 0:
            _gaps.append(f"{_broad_rounds} 轮量级未证（分母未知/覆盖率算不出）")
        if _unknown_scope > 0:
            _gaps.append(f"{_unknown_scope} 轮扫描范围未知")
        _gap_txt = ("；" + "，".join(_gaps)) if _gaps else ""
        add("universe_scope", True,
            f"会话期间**未全程取得全市场证据**："
            f"{_full_rounds}/{_rounds_total} 轮量级已证为全市场"
            f"（测到 {_scope_measured}/{_rounds_total} 轮{_gap_txt}）"
            f"—— 跳过不算失败，但**不能**说'全程全市场扫描'",
            level="ok")
    else:
        # 零证据 / 证据不完整：**不得**肯定。
        # 修前这里写的是「会话期间未降级为仅自选股（全程全市场扫描）」——
        # `finalize_metrics([])`（**一轮都没跑**）也会走到这里发出这句肯定句。
        # **零证据被当成了肯定证据**，与"把不知道当没问题"完全同类。
        _extra = (f"，其中 {_unknown_scope} 轮扫描范围**未知**"
                  if _unknown_scope > 0 else "")
        _extra += (f"，{_broad_rounds} 轮量级未证（分母未知）"
                   if _broad_rounds > 0 else "")
        add("universe_scope", True,
            f"会话级扫描范围证据**不足**（测到 {_scope_measured}/"
            f"{_rounds_total} 轮{_extra}）"
            f"—— 跳过不算失败，但**不能**据此说'全程全市场扫描'",
            level="ok")
    if isinstance(_setup, dict) and _setup:
        # --- `IT-P1-UNIVERSE-T0-MEASURED-ZERO-READ-AS-MISSING-001`（09-23 §2）
        #
        # 修前用 `_safe_int(_setup.get("universe_size"))`，而
        # **`_safe_int(None) == _safe_int(0) == 0`** —— 三态被压成两态：
        #   "没有测到股票池大小"  与  "明确测到 active scan = 0"
        # 被合并，两者都落到 `elif _u_size <= 0` 的"无法判定 -> ok"。
        # 实测（22:45 追加，我用同一路径复现）：
        #   universe_size=0 + active_scan_codes=0 + 分母未知
        #   -> healthy=True / exit=0 / fail=[]（**整场全绿**）
        # "测到 0 只票"是**明确坏**，不是"没记录"。
        _u_size_raw = _setup.get("universe_size")
        _u_size = _optional_int(_u_size_raw)          # 三态：None 保留
        _u_fell = bool(_setup.get("fell_back_to_watchlist"))
        _u_min = int(tol.get("universe_min") or UNIVERSE_MIN_ABS)
        _u_warn = int(tol.get("universe_warn") or UNIVERSE_WARN_ABS)
        # --- `IT-P1-UNIVERSE-ABS-SIZE-SESSION-BLIND-001`（09-23 §3）--------
        # 绝对项**必须也读会话内最小值**：t0=5000 + 会话 min=2000
        # 而分母未知时，旧代码只读 t0，会话的缩水**零消费者**。
        # 口径与 coverage 轴一致：**同轴取最坏**，并保持 `None != 0`。
        _abs_min = _optional_int(_sess.get("universe_abs_min"))
        _abs_meas = _safe_int(_sess.get("universe_abs_measured_rounds"))
        _abs_eff = None
        _abs_src = ""
        if _u_size is None and _abs_min is None:
            _abs_eff = None
        elif _u_size is None:
            _abs_eff = _abs_min
            _abs_src = f"（t0 未测，取自会话 {_abs_meas} 轮最小值）"
        elif _abs_min is None:
            _abs_eff = _u_size
        elif _abs_min < _u_size:
            _abs_eff = _abs_min
            _abs_src = (f"（**会话内最小** {_abs_min} < t0 {_u_size}，"
                        f"取最坏；共 {_abs_meas} 轮）")
        else:
            _abs_eff = _u_size
        if _u_fell:
            add("universe", False,
                f"股票池**已降级为仅自选股**（规模 {_abs_eff}）—— "
                f"全市场扫描不可用，此期间'没有告警'不能解释为'没有异动'",
                level="fail")
        elif _abs_eff is None:
            # **只有真的没测到**才走这里。`None` = NOT_MEASURED。
            add("universe", True,
                "无股票池规模记录（旧报告/未采集），无法判定（跳过不算失败）",
                level="ok")
        elif _abs_eff <= 0:
            # **明确测到 0 只** = 一只票都没扫 —— 与"没记录"**必须分开**。
            add("universe", False,
                f"扫描池为 **0 只**（明确测到，非未采集）{_abs_src}—— "
                f"本场**一只票都没扫**，此期间的'没有告警'"
                f"不能解释为'没有异动'", level="fail")
        elif _abs_eff < _u_min:
            add("universe", False,
                f"扫描池仅 {_abs_eff} 只（下限 {_u_min}）{_abs_src}—— "
                f"扫描范围过小，漏报风险高", level="fail")
        elif _abs_eff < _u_warn:
            add("universe", True,
                f"扫描池 {_abs_eff} 只（警戒 {_u_warn}，下限 {_u_min}）"
                f"{_abs_src}—— 偏小，建议核对源端限流", level="warn")
        else:
            add("universe", True,
                f"扫描池 {_abs_eff} 只（下限 {_u_min}）{_abs_src}",
                level="ok")
    else:
        add("universe", True,
            "无 setup 字段，无法判定（跳过不算失败）", level="ok")

    # --- R-12 / WP01：**相对覆盖**必须参与判决 -----------------------------
    #
    # 为什么必须有这一项：上面那个 `universe` 项只回答"池子有多大"，
    # 回答不了 "4500 / ? = ?"。实测（复核云端 EXP-IT-UNIVERSE-TRUTH-002）：
    # 只要分母未知，绝对门禁会把
    #   4500 / 5913 = 76.10%  判 **OK**
    #   4100 / 4200 = 97.62%  判 WARN
    # —— 前者实际缺 24%，却比后者"更好看"。**这不是阈值问题，是分母缺失。**
    #
    # 判据用 ``coverage_active``（= active_scan_codes / expected_total），
    # **不是** `coverage`（那是 C_round = 本轮返回/本轮请求，只说明
    # "我要的拿到了"，不说明"我要的是不是全市场"）。
    # 云端 §8.3 明确：**C_round 永远不能替代 C_active。**
    #
    # 三档起点（云端 §8.5 候选，非已认证最优）：
    #   < 90%  -> fail
    #   90-95% -> warn
    #   >= 95% -> ok
    # 分母未知（无 numeric total，如新浪"翻页翻完但无总数"）-> **ok/未测量**，
    # 与 capability / delivery_accounting / universe 同约定：**不把"没测"判红**。
    _ut = _setup.get("universe_truth") if isinstance(_setup, dict) else None
    if isinstance(_ut, dict) and _ut:
        _dk = str(_ut.get("denominator_kind") or "unknown")
        _exp = _safe_int(_ut.get("expected_total"))
        _act = _safe_int(_ut.get("active_scan_codes"))
        _raw = _ut.get("raw_unique_codes")
        _use = _ut.get("usable_quotes")
        _cov_a = _ut.get("coverage_active")
        _cov_t = _ut.get("coverage_transport")
        _cov_u = _ut.get("coverage_usable")
        _f_cov = _safe_float(tol.get("coverage_active_fail"), UNIVERSE_COV_FAIL)
        _w_cov = _safe_float(tol.get("coverage_active_warn"), UNIVERSE_COV_WARN)
        # IT-P1-UNIVERSE-COVERAGE-GATE-TRUNCATION-BLIND-001（云端 09:49 / 12:03）：
        # `transport_complete` 是**不需要分母**就能知道的硬事实。
        # 注意：**三态**（True/False/None）。`None` = 未测量。
        # 14:00 轮曾在此写 `_t_known = isinstance(_t_complete, bool)`，
        # 改成三态后该变量失去读者 —— 已删除（**不要说"这个变量有人读"**，
        # 本轮的主题正是"赋值 ≠ 消费"）。
        _t_complete = _ut.get("transport_complete")

        def _f(v: object) -> float | None:
            """脏值安全转 float —— **判决层绝不许因为脏值抛异常**。

            实测：`{'coverage_active': 'z', 'expected_total': 100}` 曾让
            `evaluate_health` 直接抛 `ValueError`。判决层崩溃比判错更糟：
            健康检查自己挂掉，调用方拿到的是异常而不是"不健康"。
            """
            try:
                return float(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None

        def _pct(v: object) -> str:
            fv = _f(v)
            return "未知" if fv is None else f"{fv:.2%}"

        _cov_a_f = _f(_cov_a)
        # 注：`universe_coverage` 的**判决**已移到下面的统一块（RED C），
        # 这里只保留 t0 侧的取数与 `_cov_a_f`，供该块与后续项复用。

        # --- IT-P1-UNIVERSE-COVERAGE-GATE-TRUNCATION-BLIND-001 ----------------
        #
        # 为什么必须有**独立**这一项（云端 09:49 发现、12:03 复核）
        #
        # 我 09:00 轮加 `universe_coverage` 时，把"分母未知"一律归入
        # **未测量 -> ok**。这个约定本身是对的（无 numeric total 时确实算不出
        # 比例），但我**漏了一种"分母未知但事实明确"的情况**：
        #
        #   provider 自己说 `transport_complete=False`
        #   —— 本次 universe **被截断/没翻全**。
        #
        # 实测（我独立复现云端场景）：
        #   denominator_kind='unknown', expected_total=0, coverage_active=None,
        #   transport_complete=**False**
        # -> 我的 gate 判 `ok`，`healthy=True / exit=0 / fail=[]`。
        #
        # 这**不是**"分母未知所以无从判断"，而是
        # **provider 已经明确告诉我们结果不完整，health 却把它归入未测量。**
        # 前者是诚实的无知，后者是**可判而未判** —— 又是一种假绿。
        #
        # 所以拆成独立项：`universe_coverage` 管"比例算不算得出"，
        # `universe_transport` 管"provider 说全不全"。二者证据来源不同，
        # 不能合并 —— 合并就会让其中一个被另一个的"未测量"掩盖。
        #
        # IT-P2-UNIVERSE-FRESHNESS-UNKNOWN-SEMANTIC-001 / TRISTATE：
        # `_t_complete` 是三态。`None` = **没测到**（冷启动/钉池/回放），
        # 绝不能说成"provider 声明被截断" —— 那是把"不知道"当成"有罪"，
        # 是本次要修的假绿的**镜像**。把 t0 的 tri-state 留给下面的
        # 统一判决块（会话优先），此处不重复 add 同名项。
        _t0_known = _t_complete is not None
        if _t0_known and _t_complete:
            _t0_transport_detail = (
                f"provider 声明传输完整（reported={_raw}/{_use}，"
                f"分母{'已知' if _exp > 0 else '未知'}）")
        elif _t0_known:
            _short = _safe_int(_ut.get("shortfall"))
            _reason = str(_ut.get("reason") or "")
            _t0_transport_detail = (
                f"**provider 声明本次股票池被截断**（transport_complete=False"
                + (f"，缺口 {_short} 只" if _short else "")
                + (f"，原因：{_reason}" if _reason else "")
                + "）—— 这是**不需要分母**就能确定的硬事实；"
                  "此期间'没有告警'不能解释为'没有异动'")
        else:
            _t0_transport_detail = ""

        # --- IT-P1-UNIVERSE-META-STALE-AFTER-FAILED-REFRESH-001 --------------
        #
        # 云端 09:49 真实复现、我独立复核：全源失败时 `refresh_universe()`
        # 返回 0 但**不更新** `_universe_meta`，于是 `universe_truth()`
        # **逐字段等于断供前** —— 实测两次调用 `ut1 == ut2` 为 `True`，
        # 且**没有** `attempt_status` / `last_attempt_at` 字段。
        #
        # 保留 active pool 是**对的**（不能因为一次刷新失败就把扫描集抹掉），
        # 但消费者**无从知道**这份 "coverage_active=93.85%" 是多久以前的。
        # 陈旧度必须可判 —— 否则"上次成功"会被读成"现在健康"。
        _age = _f(_ut.get("full_market_age_s"))
        if _age is None:
            _age = _f(_ut.get("active_age_s"))
        _att = str(_ut.get("attempt_status") or "")
        # IT-P2-UNIVERSE-FRESHNESS-CONFIG-KEY-001（续）：Engine 已经按
        # `poll.universe_refresh_seconds` 导出了 `warn_s`/`fail_s`，
        # 但判决层此前**只用自己的硬编码常量** —— 生产者有出口、消费者不读，
        # 于是"运维改了 TTL"仍然影响不到判决。**优先**读 Engine 导出的阈值，
        # 没有时才退回本地常量（旧报告 / 未采集）。
        _frs = _ut.get("freshness")
        _frs = _frs if isinstance(_frs, dict) else {}
        _age_fail = _safe_float(tol.get("universe_age_fail_s"),
                                _safe_float(_frs.get("fail_s"),
                                            UNIVERSE_AGE_FAIL_S))
        _age_warn = _safe_float(tol.get("universe_age_warn_s"),
                                _safe_float(_frs.get("warn_s"),
                                            UNIVERSE_AGE_WARN_S))
    else:
        _ut = {}
        _t0_known = False
        _t0_transport_detail = ""
        _age = None
        _att = ""
        _age_fail = _safe_float(tol.get("universe_age_fail_s"),
                                UNIVERSE_AGE_FAIL_S)
        _age_warn = _safe_float(tol.get("universe_age_warn_s"),
                                UNIVERSE_AGE_WARN_S)

    # --- RED C：`universe_coverage` 的**统一消费块**（不依赖 t0 是否存在）---
    #
    # `IT-P1-UNIVERSE-COVERAGE-SESSION-WITHOUT-T0-001`（09-23 00:12 §4）：
    #
    # `bf0b83b` 已经实现了"t0 分母未知但 t0 truth dict 存在 -> 会话数值证据
    # 独立支撑 FAIL"。**但整个 coverage consumer 仍被包在**
    # `if isinstance(_ut, dict) and _ut:` 之内 —— 于是如果 pre-loop 时
    # `engine.universe_truth()` 读取异常 / 暂时不可得（`setup.universe_truth = None`），
    # 而 soak 中途 per-round universe_truth 恢复、会话真有
    # `coverage_active_min=0.75 / measured_rounds=30`，
    # 判决**仍然**因为 t0 `_ut` 缺失而走"未测量"。
    #
    # 这与 transport / freshness 的"session truth 被 t0 条件包住"是**同一类
    # 结构问题**，只是 coverage 这条支路当时没一起搬出来。
    #
    # 修复原则：t0 与 session **独立解析**，之后按同轴最坏显式证据合并。
    # **不能让"t0 没读到"否定后续真实测量。**
    def _cf(v: object) -> float | None:
        """脏值安全转 float —— 判决层绝不许因脏值抛异常。"""
        try:
            return float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    def _cpct(v: object) -> str:
        fv = _cf(v)
        return "未知" if fv is None else f"{fv:.2%}"

    _ut_d = _ut if isinstance(_ut, dict) else {}
    _dk = str(_ut_d.get("denominator_kind") or "unknown")
    _exp = _safe_int(_ut_d.get("expected_total"))
    _act = _safe_int(_ut_d.get("active_scan_codes"))
    # --- 死字段接活 ③：`universe_active_scan_codes`（ADDENDUM3 §2.1）--------
    #
    # 轮样本里已经记了 `universe_active_scan_codes`，但**零生产读者**。
    # 它与 t0 的 `_act` 配对后是**唯一**能发现
    # "本轮活跃扫描集 ≠ 启动时活跃扫描集"的证据。
    # 会话聚合（`_universe_session`）已经把首轮值提了出来。
    _s_act_first = _optional_int(_sess.get("universe_active_scan_codes_first"))
    _act_shift_txt = ""
    if (_act > 0 and _s_act_first is not None and _s_act_first != _act):
        _act_shift_txt = (f"；⚠ 活跃扫描集与启动时**不一致**"
                          f"（t0 {_act} -> 会话首轮 {_s_act_first}）"
                          f"—— 分母种类相同也不代表是同一个集合")
    _raw = _ut_d.get("raw_unique_codes")
    _use = _ut_d.get("usable_quotes")
    _cov_a = _ut_d.get("coverage_active")
    _cov_t = _ut_d.get("coverage_transport")
    _cov_u = _ut_d.get("coverage_usable")
    _f_cov = _safe_float(tol.get("coverage_active_fail"), UNIVERSE_COV_FAIL)
    _w_cov = _safe_float(tol.get("coverage_active_warn"), UNIVERSE_COV_WARN)
    _cov_a_f = _cf(_cov_a)
    _s_cov_min = _cf(_sess.get("coverage_active_min"))
    _s_cov_rounds = _safe_int(_sess.get("coverage_active_measured_rounds"))
    # --- 死字段接活（21:40 §1.2 / ADDENDUM3 §2.1：5 个写后无生产读者）------
    #
    # 审计方独立统计：`coverage_active_denominator_kinds` /
    # `coverage_active_p05` / `coverage_active_last` / `universe_active_scan_codes`
    # / `scope_counts` **写进产物但没有任何生产读者** ——
    # "与本仓库反复出现的'算了没人读'同类"。约定是**接进判决或删掉**。
    # 下面把它们接成**真有判断力**的消费者，而不是为了消掉计数而随便读一下。
    #
    # ① `coverage_active_denominator_kinds` —— **跨轮可比性**门。
    # 每轮覆盖率的**分母种类**决定它是不是同一个量：
    # `provider_declared_total`（占 provider 声明总数）与
    # `page_exhausted`（翻页翻完，无总数）**不是同一个分母**。
    # 跨轮取 min 时把两种混在一起比，是**拿不可比的数当同一个数**。
    _cov_kinds = _sess.get("coverage_active_denominator_kinds")
    _cov_kinds = (_cov_kinds if isinstance(_cov_kinds, dict) else {})
    _cov_mixed = len([k for k, v in _cov_kinds.items()
                      if isinstance(v, (int, float)) and v > 0]) > 1
    _cov_cmp_txt = ""
    if _cov_mixed:
        _cov_cmp_txt = (f"；⚠ 会话内分母种类**不统一**（{_cov_kinds}）"
                        f"—— 跨轮最小值只作参考，不同分母的覆盖率"
                        f"**不可直接比较**")
    # ② `coverage_active_p05` / `coverage_active_last` —— 稳健下界与最新值。
    # min 是**单点**极值；p05 是稳健下界（抗一个尖峰）。last 区分
    # "**已恢复**"与"**仍在恶化**" —— 只看 min 无法区分。
    _s_cov_p05 = _cf(_sess.get("coverage_active_p05"))
    _s_cov_last = _cf(_sess.get("coverage_active_last"))
    _recov_txt = ""
    if (_s_cov_min is not None and _s_cov_last is not None
            and _f_cov is not None and _s_cov_min < _f_cov <= _s_cov_last):
        _recov_txt = (f"；最新一轮已回到 {_cpct(_s_cov_last)}"
                      f"（≥ 下限）—— **已恢复**")
    elif (_s_cov_min is not None and _s_cov_last is not None
            and _f_cov is not None and _s_cov_last < _f_cov):
        _recov_txt = f"；最新一轮 {_cpct(_s_cov_last)} 仍低于下限 —— **仍在恶化**"
    if _s_cov_p05 is not None and _s_cov_min is not None:
        _recov_txt += f"；p05 {_cpct(_s_cov_p05)}"
    _cov_src = ""
    if _cov_a_f is None and _s_cov_min is None:
        _cov_eff = None
    elif _cov_a_f is None:
        _cov_eff = _s_cov_min
        _cov_src = f"（t0 无可用覆盖，取自会话 {_s_cov_rounds} 轮最小值）"
    elif _s_cov_min is None:
        _cov_eff = _cov_a_f
    elif _s_cov_min < _cov_a_f:
        _cov_eff = _s_cov_min
        _cov_src = (f"（**会话内最差** {_cpct(_s_cov_min)} < t0 "
                    f"{_cpct(_cov_a_f)}，取最坏；共 {_s_cov_rounds} 轮）")
    else:
        _cov_eff = _cov_a_f
    _s_txt = (f"；会话内最小 {_cpct(_s_cov_min)}（{_s_cov_rounds} 轮）"
              if _s_cov_min is not None else "；会话内覆盖未测量")
    _s_txt += _act_shift_txt + _recov_txt + _cov_cmp_txt
    if _cov_eff is None:
        # 分母未知 -> **不许**假装知道。新浪 clean pagination 就属这类：
        # 它能证明"翻页翻完了"，但没有 numeric total，二者不能硬塞成
        # 同一种 coverage。脏值同样走这里（判"未测量"而非崩溃）。
        _why = (f"分母未知（{_dk}）" if _exp <= 0
                else f"coverage 值不可解析（{_cov_a!r}）")
        add("universe_coverage", True,
            f"{_why} —— 无法计算全市场覆盖"
            f"（跳过不算失败；此时'没有告警'不能解释为'没有异动'）",
            level="ok")
    elif _cov_eff < _f_cov:
        add("universe_coverage", False,
            f"全市场覆盖过低：active {_act}/{_exp} = {_cpct(_cov_a)}"
            f"{_cov_src}"
            f"（下限 {_f_cov:.0%}）—— transport {_cpct(_cov_t)}、"
            f"usable {_cpct(_cov_u)}；扫描范围缺 {_exp - _act} 只，"
            f"漏报风险高{_s_txt}", level="fail")
    elif _cov_eff < _w_cov:
        add("universe_coverage", True,
            f"全市场覆盖偏低：active {_act}/{_exp} = {_cpct(_cov_a)}"
            f"{_cov_src}"
            f"（警戒 {_w_cov:.0%}，下限 {_f_cov:.0%}）{_s_txt}",
            level="warn")
    else:
        add("universe_coverage", True,
            f"全市场覆盖 active {_act}/{_exp} = {_cpct(_cov_a)}"
            f"（transport {_cpct(_cov_t)}、usable {_cpct(_cov_u)}；"
            f"reported={_raw}/{_use}）{_s_txt}", level="ok")

    # --- 统一消费块：会话事实 **优先于** t0 快照 ------------------------
    #
    # IT-P1-UNIVERSE-TRANSPORT-SESSION-BLIND-001（13:32 / 16:13 §3）：
    # `_universe_session` 早就算出 `rounds_transport_incomplete`，但
    # `evaluate_health` 里 **只赋值、零消费** —— 实测把该值从 0 改成 12，
    # **完整 verdict 签名逐字节相同**。这是"数据算了、判决层零读者"的
    # 第 6 次（16:13 轮 §9 的依据）。
    #
    # IT-P1-HEALTH-SESSION-WITHOUT-T0-001：本块**不依赖** t0 是否存在。
    # 修前整段被嵌在 `if isinstance(_ut, dict) and _ut:` 里，
    # setup 缺 universe_truth 时会话事实被**整体跳过**。
    _sess_measured = bool(_sess.get("measured"))
    _worst = str(_sess.get("worst_state") or "unknown")
    _na = _safe_int(_sess.get("rounds_not_applied"))
    _ti = _safe_int(_sess.get("rounds_transport_incomplete"))
    _tmeas = _safe_int(_sess.get("rounds_transport_measured"))
    # `IT-P2-UNIVERSE-FRESHNESS-POSITIVE-USES-EVIDENCED-SUBSET-001`：
    # 全称肯定句的分母 —— 见下面 freshness 分支的说明。
    # 注意：`_rounds_total` 已在 scope 块里定义过（同一个会话 fact），
    # 这里**不再重复赋值** —— 重复赋值会让"唯一分母"出现两个来源，
    # 正是本文件反复批评的"同一语义实现两次"。
    _fresh_measured = _safe_int(_sess.get("freshness_measured_rounds"))
    #: 会话总轮数 —— transport / freshness 两条**全称肯定句**的唯一分母。
    #:
    #: 刻意**不**复用 scope 块的 `_rounds_total`：那一支在 `<=0` 时会
    #: 退化成 `_scope_measured`（旧报告缺键时的兼容处理），
    #: 而"退化成分母=已测子集"正是本 bug 类的本体。
    #: 这里宁可取 0，让全称门自然不成立。
    _sess_rounds_total = _safe_int(_sess.get("rounds_total"))

    if _sess_measured and _ti > 0:
        # 会话期间**确凿**有轮次被 provider 声明截断 —— 硬缺口，判 fail。
        # 注意：只有 tri-state 修好之后这里才成立；否则"未测量"也会被计进
        # `_ti`，修掉一个假绿会立刻造出一个**新的假红**。
        add("universe_transport", False,
            f"会话期间有 {_ti} 轮 **provider 明确声明股票池被截断**"
            f"（transport_complete=False，共测到 {_tmeas} 轮）——"
            f"这些轮次对全市场事件是硬缺口，此期间的'没有告警'"
            f"不能解释为'没有异动'", level="fail")
    elif _t0_known and _t_complete is False:
        # --- IT-P1-UNIVERSE-TRANSPORT-T0-SUPPRESSED-001（20:04 §4）----------
        #
        # **t0 的明确 False 必须优先于会话的"全完整"。**
        #
        # 修前顺序是「会话全完整 -> ok」排在 t0 之前，于是：
        #   t0 transport_complete=False + session 30 轮全测、0 轮不完整
        #   -> universe_transport = ok，文案还说"均为完整（无截断）"
        # 连 t0=None 都被同一句话盖掉。
        #
        # `transport_complete=False` 是「provider 明确声明股票池被截断」，
        # 是**不需要分母就能确定的硬事实**（本文件 :1645 的注释就这么叫它）。
        # **后来的 positive evidence 不能抹掉同一评估窗口内的 explicit
        # hard negative** —— 一个评估窗口里出现过"明确坏"，窗口结论就是坏。
        # 这与 `worst_state` 的纪律完全一致（"中途曾坏过"不能被"最后又好了"
        # 抹掉），只是对象从新鲜度换成了传输完整性。
        add("universe_transport", False,
            f"启动快照明确声明股票池被截断（transport_complete=False）"
            f"（会话另测到 {_tmeas} 轮，其中 {_ti} 轮不完整）—— "
            f"**会话级的'全完整'不能抹掉这个硬事实**："
            f"此期间的'没有告警'不能解释为'没有异动'", level="fail")
    elif _sess_measured and _ti == 0 and _tmeas > 0 and _sess_rounds_total > 0 \
            and _tmeas == _sess_rounds_total:
        # `IT-P1-TRANSPORT-POSITIVE-USES-EVIDENCED-SUBSET-001`（云端 18:07 §1.1）：
        #
        # **全称肯定句的门必须是全称的，分母必须是会话总轮数。**
        # 修前这里是 `_tmeas > 0` —— **存在量词**，而文案是
        # 「会话期间 N 轮 transport **均为**完整」—— **全称量词**。
        # 实测后果：30 轮会话里 29 轮根本没测（`transport_complete=None`）、
        # 只有 1 轮完整 -> `_tmeas=1`、`_ti=0` -> 判 ok 并打出
        # 「会话期间 **1** 轮 transport 均为完整」——
        # **拿证据子集当了自己的分母**，与 scope 轴 `_full_covers_all`
        # 修掉的是**同一条 bug 类**。
        add("universe_transport", True,
            f"会话期间 {_tmeas}/{_sess_rounds_total} 轮 transport "
            f"均为完整（无截断）", level="ok")
    elif _sess_measured and _ti == 0 and 0 < _tmeas < _sess_rounds_total:
        # 只测到一部分轮次 -> **不许**说"会话期间均完整"，必须写出 k/N。
        add("universe_transport", True,
            f"会话期间**未全程取得 transport 完整性证据**："
            f"{_tmeas}/{_sess_rounds_total} 轮测到且均完整"
            f"（其余 {_sess_rounds_total - _tmeas} 轮无 transport 证据）"
            f"—— 跳过不算失败，但**不能**说'会话期间无截断'", level="ok")
    elif _t0_known and _t0_transport_detail:
        add("universe_transport", _t_complete is True,
            _t0_transport_detail, level="ok" if _t_complete else "fail")
    else:
        # 三态里的 None：**没测到**。既不判红（诬告 provider），
        # 也不说成"新鲜/完整" —— 保持 epistemic state 正确。
        add("universe_transport", True,
            "transport 完整性**未测量**（无 transport_complete 字段 / "
            "冷启动未声明 / 旧报告）—— 跳过不算失败，但也不代表完整",
            level="ok")

    # --- 新鲜度：同样会话优先 ---------------------------------------------
    if _sess_measured:
        if _worst == "stale":
            add("universe_freshness", False,
                f"会话期间活跃股票池曾陈旧（最坏 {_worst}，"
                f"最长 {_sess.get('max_age_s')}s；"
                f"{_na} 轮刷新未生效）", level="fail")
        elif _worst == "aging":
            add("universe_freshness", True,
                f"会话期间活跃股票池曾偏旧（最坏 {_worst}，"
                f"最长 {_sess.get('max_age_s')}s）", level="warn")
        elif _na > 0:
            add("universe_freshness", True,
                f"会话期间有 {_na} 轮股票池刷新**未生效**"
                f"（状态：{_sess.get('attempt_statuses')}）—— "
                f"活跃池已偏离全市场但尚未超时", level="warn")
        elif _worst == "unknown":
            # IT-P2-UNIVERSE-FRESHNESS-UNKNOWN-SEMANTIC-001（13:55 B4 / 16:13 §6）：
            # `unknown` = **压根没测到**（age 为 None）。修前它落进 else
            # 被渲染成「会话期间股票池新鲜」/ ok —— 与真实语义**相反**。
            # 不判红（缺证据不等于坏），但**必须**说"未测量"。
            add("universe_freshness", True,
                f"会话期间股票池新鲜度**未测量**"
                f"（states={_sess.get('states')}；缺年龄证据）"
                f"—— 跳过不算失败，但也不代表新鲜", level="ok")
        elif _fresh_measured < _rounds_total:
            # --- `IT-P2-UNIVERSE-FRESHNESS-POSITIVE-USES-EVIDENCED-SUBSET-001`
            # （09-23 00:12 §5，22:15 追加指认）--------------------------------
            #
            # **全称肯定句的分母必须是会话总轮数，不是"测到的那几轮"。**
            # 修前实测：1 轮有 freshness key、29 轮完全没有 ->
            # `states={'fresh':1}` / `measured=True` / `worst=fresh`
            # -> 输出「会话期间股票池新鲜（{'fresh': 1}）」——
            # 把"**存在至少一轮** fresh 证据"升级成了"**全会话** fresh"。
            #
            # ⚠ 这**不是**"有没有 `_measured` gate"的问题：这里 `_sess_measured`
            # 已经是 True。真正缺的是**时间分母**。
            add("universe_freshness", True,
                f"会话期间**未全程取得新鲜度证据**："
                f"{_fresh_measured}/{_rounds_total} 轮有新鲜度记录"
                f"（states={_sess.get('states')}，最坏 {_worst}）"
                f"—— 跳过不算失败，但**不能**说'会话期间股票池新鲜'",
                level="ok")
        else:
            add("universe_freshness", True,
                f"会话期间股票池新鲜（{_fresh_measured}/{_rounds_total} 轮，"
                f"{_sess.get('states')}）", level="ok")
    elif _age is None and not _att:
        add("universe_freshness", True,
            "无活跃快照时间/刷新尝试信息，无法判定（跳过不算失败）",
            level="ok")
    elif _att and _att not in ("applied", "applied_partial", "unknown", ""):
        # 最近一次尝试**没有**被采用（all_failed / rejected_smaller / crashed）。
        _lvl = "warn" if (_age is not None and _age < _age_warn) else "fail"
        add("universe_freshness", _lvl != "fail",
            f"最近一次股票池刷新**未生效**（attempt_status={_att}）"
            + (f"，当前活跃池已 {_age:.0f}s 未更新"
               if _age is not None else "")
            + " —— 扫描范围可能已不反映全市场", level=_lvl)
    elif _age is not None and _age > _age_fail:
        add("universe_freshness", False,
            f"活跃股票池已 {_age:.0f}s 未成功刷新"
            f"（上限 {_age_fail:.0f}s）—— 区间可能已大幅变化",
            level="fail")
    elif _age is not None and _age > _age_warn:
        add("universe_freshness", True,
            f"活跃股票池已 {_age:.0f}s 未成功刷新"
            f"（警戒 {_age_warn:.0f}s，上限 {_age_fail:.0f}s）",
            level="warn")
    else:
        add("universe_freshness", True,
            f"活跃股票池新鲜（{'未提供 age' if _age is None else f'{_age:.0f}s'}）",
            level="ok")

    total = sum(int(v) for v in by_kind.values())
    add("alerts", True, f"共 {total} 条告警（0 条不算失败：休市时本就无告警）")

    # --- IT-H20-DELIVERY-ACCOUNTING-GATE：交付账本必须参与**判决** ---------
    #
    # 为什么必须有这一项：我上一轮把"假绿"修在了**数据层**
    # （``as_dict()`` 里有了 ``accounting_status``），但 ``evaluate_health``
    # **一项都没读它** —— 实测注入
    # ``accounting_status=inconsistent / accounting_errors=7 /
    #   committed_alerts_total=0 / committed_with_signal_id=66``
    # 之后，``healthy=True / exit_code=0`` 与健康基线**逐字节相同**。
    # 也就是说假绿没有被消除，只是**从数据层搬到了判决层**。
    #
    # 三个设计决定（都有实测依据）：
    #
    # 1. ``not_measured`` 判 **ok**，不判 fail —— 实测仓库四个"应当判健康"的
    #    helper（``empty_metrics()``、``finalize_metrics([])`` 等）产出的都是
    #    ``not_measured``。判红会让所有现存绿色用例立刻转红。这也与
    #    ``capability`` 项既有的"跳过不算失败"约定一致。
    # 2. 键缺失 / 非 dict **不崩** —— ``evaluate_health`` 必须对脏 metrics 稳健
    #    （``tests/test_live_session_observation.py`` 就是钉这条约束的）。
    # 3. 用 ``level="fail"`` 而不是只给 ``ok=False`` —— 复用既有三级语义，
    #    ``ok`` 仍是布尔，既有消费方不受影响。
    #
    # 注意：判据**不能**只看门禁 ``unsigned_total`` —— 那正是假绿的来源
    # （分母被吞时门禁也是 0）。必须看 ``accounting_status`` 与
    # ``accounting_errors``。
    _dl = m.get("delivery_accounting_session")
    if isinstance(_dl, dict) and _dl:
        _dl_status = str(_dl.get("accounting_status") or "")
        _dl_errs = _safe_int(_dl.get("accounting_errors"))
        _dl_rounds = _safe_int(_dl.get("delivery_accounting_rounds"))
        _dl_bad = _dl.get("inconsistent_rounds") or []
        _dl_total = _safe_int(_dl.get("committed_alerts_total"))
        _dl_named = _safe_int(_dl.get("committed_with_signal_id"))
        if _dl_status == "inconsistent" or _dl_errs > 0:
            add("delivery_accounting", False,
                f"记账不自洽：status={_dl_status}、吞异常 {_dl_errs} 次、"
                f"不自洽轮 {list(_dl_bad)[:5]}、账本轮数 {_dl_rounds}、"
                f"committed={_dl_named}/{_dl_total} —— "
                f"分母可能少算，此时门禁为 0 也不能算健康",
                level="fail")
        elif _dl_status == "ok":
            add("delivery_accounting", True,
                f"记账自洽（{_dl_rounds} 轮，committed={_dl_named}/{_dl_total}，"
                f"吞异常 0 次）", level="ok")
        else:
            add("delivery_accounting", True,
                f"无交付账本（status={_dl_status or 'missing'}），无法判定"
                "（跳过不算失败）", level="ok")
    else:
        add("delivery_accounting", True,
            "无交付账本字段，无法判定（跳过不算失败）", level="ok")

    healthy = all(c["ok"] for c in checks)
    if rounds < min_rounds:
        # 一轮都没跑完 = harness 级失败，与"跑起来了但有问题"要能区分开
        return {"healthy": False, "exit_code": EXIT_NO_DATA, "checks": checks,
                "warn": [c["name"] for c in checks if c.get("level") == "warn"],
                "fail": [c["name"] for c in checks if not c["ok"]]}
    return {
        "healthy": healthy,
        "exit_code": EXIT_HEALTHY if healthy else EXIT_UNHEALTHY,
        "checks": checks,
        # WP05：黄色项不改变退出码（避免真实但可接受的降级把 CI 打红），
        # 但必须在报告里点名，否则 "warn" 就等于没说。
        "warn": [c["name"] for c in checks if c.get("level") == "warn"],
        "fail": [c["name"] for c in checks if not c["ok"]],
    }


def sanitize(obj: Any) -> Any:
    """把任意对象转成"JSON 原生类型 + 有限数值"的结构（纯函数）。

    两个必须做的理由：

    * ``json.dumps`` 默认写出裸 ``NaN``/``Infinity``，那是**非法 JSON**，别的
      工具链（jq / 浏览器）读不了；真实行情确实会产出 inf（同
      ``models.Alert.to_dict`` 的处理），所以只靠"我传进去的都是好数"不成立。
    * tuple/set 转成 list：否则
      ``json.loads(json.dumps(report)) == report`` 这个往返断言会假失败。
    """
    if obj is None or isinstance(obj, bool) or isinstance(obj, str):
        return obj
    if isinstance(obj, enum.Enum):
        return sanitize(obj.value)
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        # set 无序，排序后才能保证同一份数据每次序列化结果一致
        return [sanitize(v) for v in sorted(obj, key=str)]
    if isinstance(obj, (list, tuple)):
        return [sanitize(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat(timespec="seconds")
    return str(obj)


def build_report(
    *,
    started_at: str,
    finished_at: str,
    duration_s: float,
    bounds: dict,
    session_info: dict,
    config_snapshot: dict,
    metrics: dict,
    verdict: dict,
    notes: Sequence[str] | None = None,
) -> dict:
    """组装报告（纯函数）。

    ``config_snapshot`` 是刻意留的：soak 结果只有配上"当时用的什么间隔、开了
    哪些规则"才可比较；否则两次运行的差异永远解释不清。
    """
    return sanitize({
        "tool": "tools/live_session.py",
        "report_version": REPORT_VERSION,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_s": round(float(duration_s), 2),
        "bounds": bounds,
        "session": session_info,
        "config": config_snapshot,
        "metrics": metrics,
        "verdict": verdict,
        "notes": list(notes or []),
    })


def _finite(v: Any) -> bool:
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def _kind_of(item: Any) -> str:
    """从 Alert / dict / 字符串里取出 kind（纯函数）。"""
    if item is None:
        return "unknown"
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        raw = item.get("kind")
    else:
        raw = getattr(item, "kind", None)
    if raw is None:
        return "unknown"
    return str(getattr(raw, "value", raw))


def _safe_int(v: Any) -> int:
    """可观测性计数转 int；None/脏值一律 0（**不抛**）。

    这些字段来自 Store 汇总，理论上都是干净的 int，但 soak 是要连续跑几小时
    的，任何一个脏值把整场 soak 打断都得不偿失 —— 报告少一项远好过全丢。
    """
    if isinstance(v, bool) or v is None:
        return 0
    if isinstance(v, int):
        return v
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _safe_float(v: Any, default: float = 0.0) -> float:
    """可观测性比率转 float；None/脏值一律回落到 ``default``（**不抛**）。

    ``default`` 存在的理由：阈值字段的"缺省"不能是 0.0 —— 那会把
    ``coverage_active_fail`` 变成"任何覆盖都算 fail"。所以调用方必须显式给出
    有意义的兜底值（见 ``UNIVERSE_COV_FAIL`` / ``UNIVERSE_COV_WARN``）。
    """
    if isinstance(v, bool) or v is None:
        return float(default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _optional_bool(v: Any) -> bool | None:
    """三态布尔：``True`` / ``False`` / ``None``（**未测量**）。

    IT-P1-UNIVERSE-TRANSPORT-TRISTATE-R1（16:13 §4）：修"把不知道当没问题"
    时最容易引入的**镜像错误**是"把不知道当成有罪并归罪于数据源"。
    两者同源：**三态被压成两态**。

    所以凡是要表达"provider 明确声明了没有"的字段，一律走这里，
    **不得**用 ``bool(x)`` —— ``bool(None) is False`` 会把"没测到"
    渲染成"测出来是坏的"。
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        low = v.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
    return None


def _optional_float(v: Any) -> float | None:
    """可缺省 float：``None``/脏值 -> ``None``（**绝不回落 0.0**）。

    为什么不能用 ``_safe_float``：``universe_age_s = _safe_float(None)``
    得到 ``0.0``，而 0.0 在新鲜度语义里是**最年轻** ——
    于是"年龄未知"被读成"刚刚刷新过"。缺数据必须是缺数据。
    """
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if _finite(f) else None


def _optional_int(v: Any) -> int | None:
    """三态整数：``int`` / ``None``（**未测量**）。

    与 `_optional_bool` / `_optional_float` 同一纪律（WP01
    Session Universe Evidence Contract v1）：

    * `None` -> `None`（**不得**变成 0：0 是"测出来是 0"，
      在分母上会直接变成除零 / 0% 覆盖，凭空造出一个假红）；
    * 脏值（NaN / 字符串 / bool）-> `None`（读不出，不记假成绩）；
    * `bool` **必须**先于 `int` 拦掉 —— `isinstance(True, int)` 是 True，
      否则 `True` 会被读成 `1`。

    传 `str` 只接受能无损转成整数的（`"6000"` -> 6000），
    `"6000.5"` / `"abc"` -> `None`。真实 provider 给的 JSON 数字
    已经是 int/float，这一条只为兼容手写样本与旧归档。
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if _finite(v) else None
    if isinstance(v, str):
        try:
            return int(v.strip())
        except (TypeError, ValueError):
            return None
    return None


def _round_observation_fields(row: dict) -> set[str]:
    """这条轮样本里**真的采集到**的观测字段名集合（纯函数）。

    这是"确实是 0"与"没采集到"的唯一区分手段，也是旧版本轮样本的探测器：

    * 新版本（``make_round_sample`` 带 observation）会写
      ``observation_fields`` 标记，直接用；
    * 更早的版本没有标记，但有观测键 —— 回退成"看键在不在"。注意
      ``stale_rejected`` 只在旧样本里出现（新写的是 ``future_rejected``），
      所以两种命名都算"采到了拒绝数"；
    * 什么都没有（离线造样本 / 只跑了 cache-size 统计的旧 soak）-> 空集，
      汇总方**不得**把这些轮当成 0 分参与判定。

    脏值（类型不对、NaN）不进集合：采到了字段但值是坏的，与"没采到"在
    判定上等价 —— 都不该被当成一个真实读数。
    """
    known = set(row.get("observation_fields") or ())
    if known:
        return {str(k) for k in known}
    return {k for k in _OBSERVATION_MARKER_KEYS if k in row}


#: 拒绝计数字段 -> 汇总里的总计键名。
_REJECT_TOTAL_KEYS: tuple[tuple[str, str], ...] = (
    ("unknown_missing", "missing_total"),
    ("rejected_quality", "quality_total"),
    ("future_rejected", "future_total"),
    ("out_of_order_rejected", "ooo_total"),
)


# ==========================================================================
# 采集器：会碰网络/线程，但只负责计数，不做任何判断
# ==========================================================================
class StreamProbe(threading.Thread):
    """独占一条 ``/api/stream`` SSE 长连接，按事件类型计数。

    为什么必须"真的挂着"而不是只发一次请求：SSE 是这个看板唯一的长连接通道，
    断流只有在**持续订阅**时才会暴露（例如服务端忘了给订阅队列续命、生成器
    提前 return）。判断"断流"用 socket 读超时：服务端每 ``sse_interval`` 秒
    至少发一条 tick 心跳，超过 ``stall_after`` 没动静就是真断了。
    """

    def __init__(self, url: str, *, stall_after: float = 8.0) -> None:
        super().__init__(name="arad-live-sse", daemon=True)
        self.url = url
        self.stall_after = max(1.0, float(stall_after))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._connected = False
        self._status: int | None = None
        self._error: str | None = None
        self._stalled = False
        self._lines = 0
        self._events: dict[str, int] = {}
        self._last_event_at: float | None = None

    # -- 线程体 ---------------------------------------------------------
    def run(self) -> None:
        req = urllib.request.Request(
            self.url, headers={"Accept": "text/event-stream", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=self.stall_after) as resp:
                with self._lock:
                    self._status = int(getattr(resp, "status", 0) or 0)
                    self._connected = self._status == 200
                if self._status != 200:
                    return
                with self._lock:
                    self._last_event_at = time.time()
                while not self._stop.is_set():
                    try:
                        raw = resp.readline()
                    except (socket.timeout, TimeoutError):
                        # 心跳都没了 -> 判定断流并退出（再等下去也只会一直超时）
                        with self._lock:
                            self._stalled = True
                        return
                    except OSError as exc:
                        with self._lock:
                            self._error = f"{type(exc).__name__}: {exc}"
                        return
                    if not raw:
                        return                      # EOF：服务端关了连接
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    with self._lock:
                        self._lines += 1
                    if line.startswith("event:"):
                        name = line[6:].strip() or "message"
                        with self._lock:
                            self._events[name] = self._events.get(name, 0) + 1
                            self._last_event_at = time.time()
        except Exception as exc:                     # noqa: BLE001 —— 探针失败不能拖垮主流程
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"

    # -- 控制/读取 -------------------------------------------------------
    def stop_stream(self) -> None:
        """停止订阅（不阻塞：socket 会在服务端关连接后自然 EOF）。"""
        self._stop.set()

    def snapshot(self, *, final: bool = False) -> dict:
        with self._lock:
            events = dict(self._events)
            last = self._last_event_at
            stalled = self._stalled
            out = {
                "status": self._status,
                "connected": self._connected,
                "stalled": stalled,
                "lines": self._lines,
                "events_by_type": events,
                "events_total": sum(events.values()),
                "error": self._error,
            }
        if final and self._connected and last is not None and not stalled:
            # 收尾时再判一次：整个 run 期间没动静，等同于断流
            out["stalled"] = (time.time() - last) > self.stall_after
        return out


class ApiProbe:
    """轮询看板 REST 接口，统计非 200 / 连不上 / JSON 解析失败。

    只统计**不修复**：看板接口出问题时报出来就是本工具的价值，替它兜底会让
    真实故障在报告里消失。
    """

    def __init__(self, base: str, routes: Sequence[str], *, timeout: float = 5.0) -> None:
        self.base = base.rstrip("/")
        self.routes = list(routes)
        self.timeout = float(timeout)
        self.requests = 0
        self.non_200 = 0
        self.malformed = 0
        self.unreachable = 0
        self.by_route: dict[str, dict[str, int]] = {
            p: {"requests": 0, "non_200": 0, "malformed": 0, "unreachable": 0}
            for p in self.routes
        }
        self.samples: dict[str, Any] = {}

    def hit(self, path: str) -> tuple[int | None, Any]:
        """请求单个路径；返回 (状态码, 解析后的 JSON 或 None)。"""
        url = f"{self.base}{path}"
        self.requests += 1
        self.by_route.setdefault(path, {"requests": 0, "non_200": 0,
                                        "malformed": 0, "unreachable": 0})
        self.by_route[path]["requests"] += 1
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = int(getattr(resp, "status", 0) or 0)
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            self.non_200 += 1
            self.by_route[path]["non_200"] += 1
            return int(exc.code or 0), None
        except Exception:                            # noqa: BLE001 —— 连不上也是一种失败统计
            self.unreachable += 1
            self.by_route[path]["unreachable"] += 1
            return None, None
        if status != 200:
            self.non_200 += 1
            self.by_route[path]["non_200"] += 1
            return status, None
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except Exception:                            # noqa: BLE001
            self.malformed += 1
            self.by_route[path]["malformed"] += 1
            return status, None
        if not isinstance(data, (dict, list)):
            # 合法 JSON 但不是对象/数组 —— 对看板前端等价于坏数据
            self.malformed += 1
            self.by_route[path]["malformed"] += 1
            return status, None
        return status, data

    def poll_all(self) -> None:
        for path in self.routes:
            status, data = self.hit(path)
            if data is not None:
                self.samples[path] = _summarize_payload(path, data)

    def snapshot(self) -> dict:
        return {
            "requests": self.requests,
            "non_200": self.non_200,
            "malformed": self.malformed,
            "unreachable": self.unreachable,
            "by_route": {k: dict(v) for k, v in self.by_route.items()},
            "samples": dict(self.samples),
        }


class LogCounter(logging.Handler):
    """按级别统计日志条数（WARNING/ERROR/CRITICAL）。

    引擎的 ``_errors`` 只覆盖"抓取整轮失败"，而规则求值异常、通知器异常只出现在
    日志里。soak 的结论不该漏掉这些，所以把日志也变成可比的数字。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.counts: dict[str, int] = {"WARNING": 0, "ERROR": 0, "CRITICAL": 0}

    def emit(self, record: logging.LogRecord) -> None:
        name = record.levelname
        self.counts[name] = self.counts.get(name, 0) + 1

    def snapshot(self) -> dict:
        return dict(self.counts)


def _summarize_payload(path: str, data: Any) -> dict:
    """把接口返回压缩成几个可比数字（不整包存进报告，否则报告会变成日志）。"""
    if isinstance(data, dict):
        out: dict[str, Any] = {"type": "dict", "keys": len(data)}
        for key in ("count", "total", "universe", "alerts_total", "phase", "ok", "status"):
            if key in data:
                out[key] = data[key]
        return out
    return {"type": "list", "len": len(data)}


# ==========================================================================
# 运行期（有副作用）
# ==========================================================================
def _configure_stdout() -> None:
    """把 stdout 固定成 UTF-8。

    Windows 默认 cp936 下 ``print("✓")`` 会抛 UnicodeEncodeError —— 一个只在
    中文 Windows 上炸的崩溃，比任何检查失败都难排查。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:                                # noqa: BLE001
        pass


def _setup_logging(verbose: bool, counter: LogCounter) -> None:
    """引擎日志 -> stderr（WARNING 起）+ 计数 handler。

    默认不打印 INFO：soak 一跑几十轮，每轮一条"股票池已刷新"会把结论表冲没。
    """
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    stream = logging.StreamHandler(sys.stderr)
    stream.setLevel(logging.INFO if verbose else logging.WARNING)
    stream.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(stream)
    root.addHandler(counter)
    root.setLevel(logging.INFO)


def _free_port() -> int:
    """返回一个当前空闲的端口（仅用于探测；真正的绑定仍走 create_server）。

    注意这里必须 close 掉探测用的 socket，否则端口一直被占着 —— 这正是
    "找不到空闲端口"这类问题的经典成因。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _fmt_table(checks: Sequence[dict]) -> str:
    lines = []
    for c in checks:
        # WP05：warn 用 ~ 标出来。只显示 ✓/✗ 会把"说得出问题但没到失败"
        # 这一档吞掉，而那正是 observation 覆盖率要表达的东西。
        lvl = c.get("level") or ("ok" if c.get("ok") else "fail")
        mark = {"ok": "✓", "warn": "~", "fail": "✗"}.get(str(lvl), "?")
        lines.append(f"  {mark} {c.get('name', '?'):<11}"
                     f"{c.get('detail', '')}")
    return "\n".join(lines)


def _overall_evaluability(metrics: dict) -> float | None:
    """整场 soak 的整体可评估率 = 可评估标的 / 已考虑标的。

    与旧 ``capability_unavailable_ratio`` 的**根本区别**是分母：这里是**标的**，
    那里是**轮**。5000 码里每轮坏 1 个，这里给出 99.98%，那里给出 100%
    "出现过缺失"。没有分母（considered == 0）时返回 ``None``（not_measured），
    **不是** 0.0 —— 填 0 会把"没测"误报成"整类失效"。
    """
    tot = _safe_int(metrics.get("evaluability_considered_total"))
    if tot <= 0:
        return None
    return _safe_int(metrics.get("evaluability_evaluable_total")) / tot


def _fmt_signal_evaluability(sig_ev: Any) -> str:
    """逐 signal 可评估率里**最差**的几个，供一行人读。

    只打最差的（升序前 3）—— 全量打出来会让报告退化成转储。
    """
    if not isinstance(sig_ev, dict) or not sig_ev:
        return ""
    rows: list[tuple[float, str, int, int]] = []
    for sig, cell in sig_ev.items():
        if not isinstance(cell, dict):
            continue
        tot = _safe_int(cell.get("considered"))
        if tot <= 0:
            continue
        ev = _safe_int(cell.get("evaluable"))
        rows.append((ev / tot, str(sig), ev, tot))
    if not rows:
        return ""
    rows.sort(key=lambda t: (t[0], t[1]))
    parts = [f"{s}={c:.1%}({e}/{t})" for c, s, e, t in rows[:3]]
    return "；最差 signal " + ", ".join(parts)


def _fmt_observation_summary(metrics: dict) -> str:
    """把 WP05 的观测聚合打印成一行（人读的结论表用）。"""
    def _pct(v: Any) -> str:
        return "—" if v is None else f"{float(v):.1%}"

    def _fmt_worst(rows: Sequence[dict]) -> str:
        if not rows:
            return "（无有效 coverage 读数）"
        return " ".join(f"#{r.get('index')}={_pct(r.get('coverage'))}"
                        for r in rows)

    return (
        f"  观测覆盖：p05/p50/min/max = {_pct(metrics.get('coverage_p05'))} / "
        f"{_pct(metrics.get('coverage_p50'))} / {_pct(metrics.get('coverage_min'))} / "
        f"{_pct(metrics.get('coverage_max'))}"
        f"（有效读数 {metrics.get('coverage_rounds')}/{metrics.get('rounds')} 轮，"
        f"带账本 {metrics.get('rounds_with_observation')} 轮）\n"
        f"  观测账本：请求 {metrics.get('requested_total')} / 返回 "
        f"{metrics.get('returned_total')} / 准入 {metrics.get('admitted_total')}；"
        f"拒绝 缺失 {metrics.get('missing_total')} 质量 {metrics.get('quality_total')} "
        f"未来 {metrics.get('future_total')} 陈旧 {metrics.get('stale_total')} "
        f"乱序 {metrics.get('ooo_total')}\n"
        f"  来源混合：{metrics.get('source_mix') or '{}'}"
        f"（未知来源 {metrics.get('source_mix_unknown_rounds')} 轮）\n"
        f"  可评估率（逐 signal）：可评估 "
        f"{metrics.get('evaluability_evaluable_total')}/"
        f"{metrics.get('evaluability_considered_total')}"
        f"（{_pct(_overall_evaluability(metrics))}），"
        f"被能力阻断 {metrics.get('evaluability_blocked_total')}，"
        f"advisory {metrics.get('evaluability_advisory_total')}；"
        f"命中候选 {metrics.get('evaluability_hit_candidates_total')}/"
        f"已发出 {metrics.get('evaluability_published_total')}"
        f"{_fmt_signal_evaluability(metrics.get('signal_evaluability'))}\n"
        f"  （诊断）旧口径轮比例："
        f"{metrics.get('capability_unavailable_rounds')} 轮出现过能力缺失，"
        f"累计 {metrics.get('capability_unavailable_total')} 个标的"
        f"（比例 {_pct(metrics.get('capability_unavailable_ratio'))}）"
        f"{metrics.get('unavailable_by_reason') or ''}\n"
        f"  最差轮次（按 coverage 升序）：{_fmt_worst(metrics.get('worst_rounds') or [])}"
    )


def _collect_config_snapshot(st: Any, engine: Any, watchlist: Sequence[str]) -> dict:
    """配置快照：让多次运行之间可比（尤其是轮询间隔与启用的规则）。"""
    rules_raw = st.section("rules") if hasattr(st, "section") else {}
    return {
        "app": {
            "dry_run": bool(st.get("app.dry_run", True)),
            "replay": bool(st.get("app.replay", False)),
            "log_level": st.get("app.log_level"),
        },
        "poll": {
            "universe_seconds": st.get("poll.universe_seconds"),
            "watchlist_seconds": st.get("poll.watchlist_seconds"),
            "universe_refresh_seconds": st.get("poll.universe_refresh_seconds"),
            "history_len": st.get("poll.history_len"),
            "batch_size": st.get("poll.batch_size"),
            "workers": st.get("poll.workers"),
            "http_timeout": st.get("poll.http_timeout"),
            "retries": st.get("poll.retries"),
            "idle_when_closed": st.get("poll.idle_when_closed"),
            "index_codes": list(st.get("poll.index_codes") or []),
        },
        "sources": {
            "primary": st.get("sources.primary"),
            "universe": st.get("sources.universe"),
            "fallback": list(st.get("sources.fallback") or []),
            "failover_threshold": st.get("sources.failover_threshold"),
        },
        "rules": {
            "effective": [str(getattr(r, "name", r.__class__.__name__)) for r in engine.rules],
            "enabled_in_config": sorted(
                [str(k) for k, v in (rules_raw or {}).items()
                 if isinstance(v, dict) and v.get("enabled")],
            ),
            "raw": rules_raw,
        },
        "notifiers": {
            "enabled_in_config": st.get("notify.enabled"),
            "effective": [str(getattr(n, "name", n.__class__.__name__)) for n in engine.notifiers],
        },
        "web": {
            "sse_interval": st.get("web.sse_interval"),
            "top_n": st.get("web.top_n"),
            "max_alerts": st.get("web.max_alerts"),
        },
        "watchlist_size": len(list(watchlist)),
        "filters": dict(st.section("filters") or {}) if hasattr(st, "section") else {},
    }


def _check_browser(url: str, *, wait_ms: int = 2500) -> dict:
    """可选的浏览器检查：抓 pageerror / console.error。任何问题都只"跳过"。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ~ 未安装 Playwright，跳过浏览器检查"
              "（pip install playwright && playwright install chromium）")
        return {"ran": False, "skipped": True, "errors": [],
                "reason": "playwright 未安装"}
    errors: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
                page.on("console",
                        lambda m: errors.append(f"console.error: {m.text}")
                        if m.type == "error" else None)
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(wait_ms)
            finally:
                browser.close()
    except Exception as exc:                         # noqa: BLE001 —— 浏览器不可用不算失败
        print(f"  ~ 浏览器检查跳过（{type(exc).__name__}: {exc}）")
        return {"ran": False, "skipped": True, "errors": errors,
                "reason": f"{type(exc).__name__}: {exc}"}
    print(f"  浏览器检查完成：pageerror/console.error {len(errors)} 条")
    return {"ran": True, "skipped": False, "errors": errors[:20], "url": url}


def _t0_universe_size(ret: int | None, *, retained_pool: int,
                      attempt: Any = None, say: Any = None) -> int | None:
    """t0 股票池刷新结果 -> 判决层用的**三态** `universe_size`。

    `IT-P1-REFRESH-FAILED-POOL-RETAINED-READ-AS-MEASURED-ZERO-001`
    （云端 18:17 §0，P0 假红回归）。**纯函数**，便于直接测 ——
    修前这段逻辑内联在 `def run`（约 `:3308`）里，而全 `tests/`
    对 `run` 入口点命中 **0 次**，所以缺陷能在 `1830 passed` 下存活。

    三种情况**必须分开**（这是本仓库反复出现的
    "一个 `0` 承载两个语义状态" bug 类）：

    ====================  ==================  ==========================
    真实情况              返回值              判决层应看到
    ====================  ==================  ==========================
    刷新成功且拿到 N 只    N > 0               N（**明确测到**）
    真的扫到 0 只           0，池子**也空**     0（**明确坏**，判红）
    刷新失败但池子被保留    0，池子**非空**     ``None``（**未测量**）
    ====================  ==================  ==========================

    第三行就是被修掉的假红：`engine.refresh_universe()` 的
    `rejected_smaller` 与 `all_failed` 两条路径都**保留原股票池**
    却 `return 0`，而 `int(... or 0)` 把它压成了"明确测到 0"。

    :param ret: `refresh_universe()` 的返回值（异常路径传 ``None``）
    :param retained_pool: 刷新后 `engine._codes` 的长度
    :param attempt: `engine._universe_attempt`（只用于打印留痕，不参与判定）
    :param say: 打印函数（默认不打印，便于测试与静默调用）
    """
    if ret not in (0, None) and int(ret) > 0:
        return int(ret)
    if ret is None:
        return None                     # 异常 -> 未测量
    if int(ret) == 0 and retained_pool > 0:
        # 刷新未生效（保留原池）——**不是**"一只票都没扫"。
        if say is not None:
            _att = dict(attempt) if isinstance(attempt, dict) else {}
            say(f"      [i] 股票池刷新未生效（返回 0，但保留 "
                f"{retained_pool} 只；attempt="
                f"{_att.get('status') or _att.get('outcome') or '?'}）"
                f"—— 记为**未测量**，不判'一只票都没扫'")
        return None
    return int(ret)                     # 真的 0 只且池子也空 -> 明确坏


def _soak_loop(engine: Any, probe: ApiProbe, *, max_rounds: int | None,
               deadline: float, interval: float, verbose: bool) -> tuple[list[dict], dict]:
    """主 soak 循环：逐轮 poll_once(force=True) + 看板探活。"""
    rounds: list[dict] = []
    flags = {"deadline_hit": False, "interrupted": False, "exception": None}
    while True:
        if max_rounds is not None and len(rounds) >= max_rounds:
            break
        if time.monotonic() >= deadline:
            flags["deadline_hit"] = True
            break

        state = engine.state
        # _errors 是引擎内部计数器，也是"这一轮抓取失败了"的唯一真实来源
        # （poll_once 失败时直接 return []，外部看不出与"没告警"的区别）。
        errs_before = int(getattr(engine, "_errors", 0) or 0)
        store = getattr(engine, "store", None)
        obs_seq_before = int(getattr(store, "observation_seq", 0) or 0)
        t0 = time.perf_counter()
        fresh: list[Any] = []
        exc_note: str | None = None
        try:
            fresh = engine.poll_once(force=True) or []
        except KeyboardInterrupt:
            raise
        except Exception as exc:                     # noqa: BLE001 —— 单轮异常要留在报告里
            exc_note = f"{type(exc).__name__}: {exc}"
        latency_ms = (time.perf_counter() - t0) * 1000.0
        error = exc_note is not None or int(getattr(engine, "_errors", 0) or 0) > errs_before

        codes = list(getattr(engine, "_codes", []) or [])
        watch = list(getattr(engine, "watchlist", []) or [])
        history = getattr(state, "history", {}) or {}
        maxlen = max([getattr(h, "maxlen", 0) or 0 for h in history.values()]
                     + [int(getattr(state, "history_len", 0) or 0), 30])
        # 本轮真实观测账本（WP02 / IT-P2-OBS-001）。
        #
        # IT-P2-OBS-004：Store 里存的是**最近一轮**的汇总。若本轮 poll 抛异常
        # 或没有推进（observation_seq 未增加），那份汇总属于**上一成功轮**，
        # 原样写进本轮 sample 会把上一轮的覆盖率/拒绝数标在本轮名下，而
        # sample 只有 index，事后无法识别。所以按轮次序号做归属判定：
        # 序号未变 -> **不并入**，并记 observation_stale_from 指出实际轮次。
        obs_snapshot: dict = {}
        try:
            get_obs = getattr(store, "observation", None)
            obs_seq_after = int(getattr(store, "observation_seq", 0) or 0)
            if isinstance(get_obs, dict) and get_obs and not error \
                    and obs_seq_after > obs_seq_before:
                obs_snapshot = get_obs
        except Exception:                            # noqa: BLE001
            obs_snapshot = {}

        sample = make_round_sample(
            index=len(rounds) + 1,
            latency_ms=latency_ms,
            alerts=fresh,
            error=error,
            # IT-P1-OBS-EMPTY-ROUND-001：**这里必须用本轮的观测数，
            # 绝不能用 ``len(state.quotes)``。**
            #
            # ``state.quotes`` 是**累计**最新报价缓存：
            # ``engine.py:302 self.quotes[q.code] = q`` 是唯一写入，
            # 而 ``engine.py:417-446 prune()`` 回收 history / last_price /
            # watermark / first_seen … **独独不回收 self.quotes**。
            # 实测：灌 5913 只后 ``prune(keep_codes={'sh600001'})``，
            # ``len(state.quotes)`` **仍是 5913** ⇒ 它从不缩小。
            #
            # 后果（实测 6 轮全断供，provider 每轮返回 [] 且不抛异常）：
            #   len(state.quotes) 恒 = 5913 -> sample['quotes'] 恒 = 5913
            # 于是 ``no_data_rounds``（定义 ``int(r['quotes']) <= 0``）
            # **结构上恒为 0**，``evaluate_health`` 的
            # ``add("data", quotes_max > 0)`` 也恒为旧值 ——
            # **两个专门盯行情数的判决项同时失效**。
            # 最严重的形式：静默断供 6 轮得到
            # ``healthy=True / exit=0 / fail=[]``，与健康基线**无法区分**。
            #
            # 所以取本轮真实计数：优先用本轮 observation 的 ``returned``
            # （provider 原始返回条数，含 price<=0），退回 ``admitted``。
            # 两者都拿不到（旧报告 / 离线造样本）时才退回累计口径 ——
            # 那是**退化**，不是正确值。
            quotes=_round_quote_count(obs_snapshot, state),
            universe=len(codes),
            history_points=sum(len(h) for h in history.values()),
            history_codes=len(history),
            max_deque=max((len(h) for h in history.values()), default=0),
            history_maxlen=maxlen,
            watchlist_only=bool(codes) and len(codes) <= len(watch),
            observation=obs_snapshot,
            # WP02：每轮采一份股票池**新鲜度**。setup 那份是 t0 快照，
            # 中途的刷新失败只有靠这里才能进最终判决。
            universe_truth=_universe_truth_now(engine),
        )
        rounds.append(sample)

        probe.poll_all()                             # 每轮顺带探一次看板 REST

        note = f"  异常: {exc_note}" if exc_note else ""
        print(f"  第 {sample['index']:>3} 轮 | {latency_ms / 1000:6.2f}s | "
              f"池 {sample['universe']:>5} | 行情 {sample['quotes']:>5} | "
              f"告警 {len(fresh):>3} | 历史点 {sample['history_points']:>7}{note}",
              flush=True)
        if verbose and fresh:
            for a in fresh[:5]:
                print(f"        · {_kind_of(a)} {getattr(a, 'code', '?')} "
                      f"{getattr(a, 'name', '')}")

        if max_rounds is not None and len(rounds) >= max_rounds:
            break
        # 间隔补偿：poll_once 本身耗时算进间隔，避免实际轮询周期被拉长
        target = time.monotonic() + max(0.0, interval - (time.perf_counter() - t0))
        while time.monotonic() < target:
            if time.monotonic() >= deadline:
                flags["deadline_hit"] = True
                break
            time.sleep(min(0.2, max(0.0, target - time.monotonic())))
    return rounds, flags


def run(args: argparse.Namespace) -> int:
    """执行一次 soak；返回进程退出码。"""
    _configure_stdout()
    counter = LogCounter()
    _setup_logging(bool(args.verbose), counter)

    from arad.config import PROJECT_ROOT, load_settings
    from arad.engine import Engine, build_notifiers, build_rules
    from arad.server import web as webmod
    from arad.session import TradingCalendar

    print("=" * 66)
    print(" A股盘中雷达 · 真实 soak 测试（tools/live_session.py）")
    print("=" * 66)

    # --- 1) 配置与时段 ---------------------------------------------------
    st = load_settings(use_cache=False)              # 必须绕过缓存：下面会改配置
    calendar = TradingCalendar.load()
    now = datetime.now()
    phase = calendar.phase(now)
    is_open = bool(calendar.is_open(now))
    started_at = now.isoformat(timespec="seconds")

    print(f"[1/5] 时段：{calendar.describe(now)}")
    print(f"      市场状态：{'✓ 连续竞价中' if is_open else '✗ 未开市（非连续竞价时段）'}")
    if not is_open:
        print("      说明：仍用 force=True 跑完整数据链路；休市时的行情是"
              "“最后成交快照”，")
        print("            价格基本不动，因此 0 条告警是正常结果，不计入失败。")

    interval = float(st.get("poll.universe_seconds", 5) or 5)
    minutes = float(args.minutes)
    max_rounds = int(args.rounds) if args.rounds and int(args.rounds) > 0 else None
    bound_txt = f"轮数 {max_rounds}" if max_rounds else f"时长 {minutes} 分钟"
    print(f"      边界：{bound_txt}（间隔 {interval:.1f}s，"
          f"--minutes 作为兜底上限始终生效）")

    # --- 2) 引擎 ---------------------------------------------------------
    t0 = time.perf_counter()
    engine = Engine(source=None, settings=st, rules=build_rules(st),
                    notifiers=build_notifiers(st), calendar=calendar)
    build_s = time.perf_counter() - t0
    print(f"[2/5] 引擎就绪（{build_s:.2f}s）：规则 "
          f"{[str(getattr(r, 'name', '?')) for r in engine.rules]}")
    print(f"      通知器：{[str(getattr(n, 'name', '?')) for n in engine.notifiers]}"
          f"（dry_run={st.get('app.dry_run')}）")

    # 先把股票池预热好，不然第一轮的 latency 里会混进 20 秒的股票池刷新，
    # 后面所有 p95/max 都被这一个离群值污染。
    #
    # ⚠ `IT-P1-UNIVERSE-T0-MEASURED-ZERO-READ-AS-MISSING-001`（09-23 00:12 §2）：
    # 这里曾写 `universe_size = 0`，**异常路径与"真的刷新出 0 只"共用同一个 0**。
    # 那正是"测到 0"与"没测到"被合并的**生产者源头** ——
    # 判决层再想分开也已经没有信息了。
    # 现在：异常 -> `None`（**未测量**，不许判红）；
    # `refresh_universe()` 正常返回 0 -> 真的 0（**明确坏**）。
    #
    # ⚠⚠ `IT-P1-REFRESH-FAILED-POOL-RETAINED-READ-AS-MEASURED-ZERO-001`
    # （云端 18:17 §0，**P0 假红回归 —— 我上一轮引入的**）：
    # 上面那句注释**漏掉了第三种情况**。我实测 `engine.refresh_universe()`
    # 的 4 个 return 里，`:1302`（`rejected_smaller`：部分池比现有池小 ->
    # 拒绝覆盖、**保留现有池**）与 `:1326`（`all_failed`：**保留原股票池**）
    # **都是 `return 0`**，且这两条路径**不重写** `self._codes_raw` ——
    # 池子确实还在（实测 5563 只 × 30 轮）。
    #
    # 于是 `int(... or 0)` 把「刷新失败但池子保留（非空）」也压成 `0`，
    # 判决层（`:1942 elif _abs_eff <= 0`）按"明确测到 0"处理，输出
    # 「扫描池为 **0 只**…本场**一只票都没扫**」——**假红**：
    # 有 30 轮真实证据却判"一只票都没扫"。
    #
    # 假红与假绿同样致命：一次全源抖动就让整场判红，运维学会忽略红，
    # 此后真假红不再可区分。
    #
    # 修法：**按"返回值 + 池子是否被保留"的联合状态**判定，而不是 `or 0`。
    # `0` 只在**池子也真的空了**时才代表"测到 0"；池子被保留时，
    # 这次刷新的语义是"**未生效**"，必须落回 `None`（未测量）。
    t0 = time.perf_counter()
    universe_size: int | None = None
    try:
        _ret = engine.refresh_universe()
        universe_size = int(_ret or 0)
    except Exception as exc:                         # noqa: BLE001
        print(f"      [!] 股票池刷新异常：{type(exc).__name__}: {exc}")
    refresh_s = time.perf_counter() - t0
    codes = list(getattr(engine, "_codes", []) or [])
    universe_size = _t0_universe_size(
        universe_size, retained_pool=len(codes),
        attempt=getattr(engine, "_universe_attempt", None), say=print)
    watch = list(getattr(engine, "watchlist", []) or [])
    fell_back = bool(codes) and len(codes) <= len(watch)
    # R-12 / WP01：把股票池**真相**（含分母）取出来。必须在 refresh 之后 ——
    # 分母来自 provider 最近一次 universe()，refresh 前还是上一轮的。
    universe_truth: dict | None = None
    try:
        _ut = getattr(engine, "universe_truth", None)
        if callable(_ut):
            _got = _ut()
            universe_truth = _got if isinstance(_got, dict) else None
    except Exception as exc:                         # noqa: BLE001
        print(f"      [!] 股票池真相读取失败：{type(exc).__name__}: {exc}")
    print(f"      股票池：{len(codes)} 只（刷新 {refresh_s:.1f}s）"
          + ("  [!] 已降级为仅自选股" if fell_back else ""))
    if isinstance(universe_truth, dict):
        _dk = universe_truth.get("denominator_kind")
        _exp = universe_truth.get("expected_total")
        _ca = universe_truth.get("coverage_active")
        print(f"      股票池真相：分母={_exp or '未知'}（{_dk}），"
              f"active coverage="
              + ("未知" if _ca is None else f"{float(_ca):.2%}"))
    if not codes:
        print("      [!] 股票池为空且自选股也没配 -> 本轮不会产生任何行情/告警")

    # --- 3) 看板 + SSE ---------------------------------------------------
    routes_wanted = ["/api/health", "/api/status", "/api/spirit", "/api/alerts"]
    route_table = getattr(webmod, "_GET_ROUTES", {}) or {}
    routes = [p for p in routes_wanted if p in route_table]
    missing = [p for p in routes_wanted if p not in route_table]
    print(f"[3/5] 看板接口探活：{' '.join(routes) if routes else '（无可用路由）'}")
    if missing:
        print(f"      [!] 路由表里不存在，已跳过：{' '.join(missing)}"
              f"（以 web.py 的 _GET_ROUTES 为准）")

    cfg = dict(st.web or {})
    cfg["host"] = "127.0.0.1"
    port_arg = int(args.port or 0)
    server = None
    stream: StreamProbe | None = None
    web_thread: threading.Thread | None = None
    base_url = ""
    rounds: list[dict] = []
    flags: dict = {"deadline_hit": False, "interrupted": False, "exception": None}
    browser_info: dict = {"ran": False, "skipped": True, "errors": [],
                          "reason": "未请求（--browser）"}
    probe: ApiProbe | None = None
    sse_info: dict = {}
    fatal: str | None = None
    t_start = time.monotonic()

    try:
        # port=0：由内核分配空闲端口，再读回真实端口 —— 比"先探测再绑定"少一次竞态
        server = webmod.create_server(engine.store, cfg, host="127.0.0.1", port=port_arg)
        real_port = int(server.server_address[1])
        if real_port <= 0:
            raise RuntimeError(f"看板端口异常：{server.server_address!r}")
        base_url = f"http://127.0.0.1:{real_port}"
        web_thread = threading.Thread(target=server.serve_forever,
                                      kwargs={"poll_interval": 0.2},
                                      name="arad-live-web", daemon=True)
        web_thread.start()
        print(f"      看板已启动：{base_url}/")

        stall_after = max(8.0, 3.0 * float(cfg.get("sse_interval") or 2.0))
        stream = StreamProbe(f"{base_url}/api/stream", stall_after=stall_after)
        stream.start()
        # 等 SSE 真正连上再开跑，否则首轮的 phase/alert 事件会漏掉
        deadline_conn = time.monotonic() + 5.0
        while time.monotonic() < deadline_conn:
            if stream.snapshot().get("connected") or stream.snapshot().get("error"):
                break
            time.sleep(0.05)
        print(f"      SSE 订阅：{stream.snapshot()}")

        probe = ApiProbe(base_url, routes)

        # --- 4) soak ----------------------------------------------------
        print(f"[4/5] 开始 soak …")
        deadline = time.monotonic() + minutes * 60.0
        rounds, flags = _soak_loop(engine, probe, max_rounds=max_rounds,
                                   deadline=deadline, interval=interval,
                                   verbose=bool(args.verbose))

        # --- 5) 可选浏览器 ----------------------------------------------
        print("[5/5] 收尾检查")
        if args.browser:
            browser_info = _check_browser(base_url + "/")
        else:
            print("  ~ 未指定 --browser，跳过浏览器检查")
    except KeyboardInterrupt:
        flags["interrupted"] = True
        print("\n[!] 收到 Ctrl+C：停止轮询，仍然生成本次报告与结论。")
    except Exception as exc:                         # noqa: BLE001 —— 也要写报告，方便事后定位
        fatal = f"{type(exc).__name__}: {exc}"
        print(f"\n[!] 运行异常：{fatal}")
    finally:
        # 收尾顺序有讲究：先让 SSE 自己退出（服务端 shutdown 会给它发 bye 并关闭
        # 连接，客户端读到 EOF 自然结束），再关监听套接字。任何一条都不能漏，
        # 否则端口会一直被占着，下一次运行直接 bind 失败。
        if stream is not None:
            stream.stop_stream()
        if server is not None:
            try:
                server.shutdown()
            except Exception:                        # noqa: BLE001
                pass
            try:
                server.server_close()
            except Exception:                        # noqa: BLE001
                pass
        if stream is not None:
            stream.join(timeout=3.0)
            sse_info = stream.snapshot(final=True)
            if stream.is_alive():
                print("  [!] SSE 线程未在 3s 内退出（daemon，不阻塞进程退出）")
        if web_thread is not None:
            web_thread.join(timeout=3.0)
        if server is not None:
            print(f"      看板已关闭，端口 {int(server.server_address[1])} 已释放")

    durations = time.monotonic() - t_start
    api_info = probe.snapshot() if probe is not None else empty_metrics()["api"]
    metrics = finalize_metrics(
        rounds,
        api=api_info,
        sse=sse_info or {"connected": False, "stalled": False, "events_total": 0},
        logs=counter.snapshot(),
        browser=browser_info,
        setup={"engine_build_s": round(build_s, 3),
               "universe_refresh_s": round(refresh_s, 3),
               "universe_size": universe_size,
               "fell_back_to_watchlist": fell_back,
               # 显式 run mode（见 parse_args 的 --watch-only 说明）。
               "watch_only": bool(getattr(args, "watch_only", False)),
               # R-12 / WP01：**分母**。`_universe_meta` 此前零出口，
               # health 只能看绝对只数，回答不了 "4500 / ? = ?"。
               # 取不到就 None —— 消费方据此判"未测量"，**不猜**。
               "universe_truth": universe_truth,
               "sse_stall_after_s": max(8.0, 3.0 * float(cfg.get("sse_interval") or 2.0))
               if cfg else None},
    )
    verdict = evaluate_health(metrics)
    if fatal and verdict["exit_code"] == EXIT_HEALTHY:
        checks = verdict["checks"] + [{"name": "fatal", "ok": False,
                                       "level": "fail", "detail": fatal}]
        verdict = {"healthy": False, "exit_code": EXIT_UNHEALTHY, "checks": checks,
                   "warn": list(verdict.get("warn") or []),
                   "fail": list(verdict.get("fail") or []) + ["fatal"]}

    notes = [
        "休市时行情为最后成交快照，0 条告警不等于故障",
        "force=True 已强制跑完整数据链路（否则休市时 poll_once 直接返回空）",
        f"抓取失败容差为 {DEFAULT_TOLERANCES['max_error_ratio']:.0%} 轮（东财限流是常态）",
        # WP05 / IT-P1-OBS-011：阈值写进报告，否则两次运行的结论没法比较
        # （不知道当时用的是哪档阈值）。
        "观测覆盖率阈值（warn/fail）："
        f"p05 {DEFAULT_OBSERVATION_TOLERANCES['coverage_warn_p05']:.0%}/"
        f"{DEFAULT_OBSERVATION_TOLERANCES['coverage_fail_p05']:.0%}，"
        f"p50 {DEFAULT_OBSERVATION_TOLERANCES['coverage_warn_p50']:.0%}/"
        f"{DEFAULT_OBSERVATION_TOLERANCES['coverage_fail_p50']:.0%}，"
        f"min {DEFAULT_OBSERVATION_TOLERANCES['coverage_warn_min']:.0%}/"
        f"{DEFAULT_OBSERVATION_TOLERANCES['coverage_fail_min']:.0%}"
        "（刻意不要求 100%：停牌/新股/源端漏发让它永远到不了 100%）",
        "能力缺失阈值为『出现即警告』："
        f"轮覆盖率 > {DEFAULT_OBSERVATION_TOLERANCES['unavailable_warn_ratio']:.0%} 记 warn，"
        f">= {DEFAULT_OBSERVATION_TOLERANCES['unavailable_fail_ratio']:.0%} 记 fail"
        "（每轮都不可评估 = 整类规则一次都没被验证过）",
    ]
    if flags.get("interrupted"):
        notes.append("本次运行被 Ctrl+C 中断，统计仅覆盖已完成的轮次")
    if flags.get("deadline_hit"):
        notes.append("本次运行到达 --minutes 兜底上限后停止")

    session_info = {
        "checked_at": now.isoformat(timespec="seconds"),
        "phase": str(getattr(phase, "value", phase)),
        "is_open": is_open,
        "describe": calendar.describe(now),
        "note": ("连续竞价中，行情为实时数据" if is_open
                 else "非连续竞价时段，行情为最后成交快照；0 条告警属正常"),
    }
    bounds = {
        "minutes": minutes,
        "rounds_requested": max_rounds,
        "interval_s": interval,
        "rounds_done": len(rounds),
        "duration_s": round(durations, 2),
        "deadline_hit": bool(flags.get("deadline_hit")),
        "interrupted": bool(flags.get("interrupted")),
        "dashboard_url": base_url,
        "routes_probed": routes,
        "routes_missing": missing,
    }
    report = build_report(
        started_at=started_at,
        finished_at=datetime.now().isoformat(timespec="seconds"),
        duration_s=durations,
        bounds=bounds,
        session_info=session_info,
        config_snapshot=_collect_config_snapshot(st, engine, watch),
        metrics=metrics,
        verdict=verdict,
        notes=notes,
    )

    report_path = PROJECT_ROOT / "data" / (
        "live_session_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".json")
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        wrote = True
    except Exception as exc:                         # noqa: BLE001 —— 写不了报告也要给结论
        wrote = False
        print(f"[!] 报告写入失败：{type(exc).__name__}: {exc}")

    _print_summary(metrics, verdict, bounds, report_path if wrote else None)
    return int(verdict["exit_code"])


def _print_summary(metrics: dict, verdict: dict, bounds: dict,
                   report_path: Path | None) -> None:
    """打印结论表。人只看这一段，所以关键数字必须都在这里。"""
    lat = metrics.get("latency_ms") or {}
    mem = metrics.get("memory") or {}
    api = metrics.get("api") or {}
    sse = metrics.get("sse") or {}
    logs = metrics.get("logs") or {}
    quotes = metrics.get("quotes") or {}
    universe = metrics.get("universe") or {}

    def _ms(key: str) -> str:
        v = lat.get(key)
        return "—" if v is None else f"{float(v):.0f}ms"

    print()
    print("=" * 66)
    print(" 观测结果")
    print("=" * 66)
    print(f"  轮次 {metrics.get('rounds')}（请求 {bounds.get('rounds_requested') or '—'}，"
          f"用时 {bounds.get('duration_s')}s，间隔 {bounds.get('interval_s')}s）")
    print(f"  单轮耗时 min/median/p95/max = {_ms('min')} / {_ms('median')} / "
          f"{_ms('p95')} / {_ms('max')}")
    print(f"  抓取失败 {metrics.get('error_rounds')} 轮，"
          f"空数据 {metrics.get('no_data_rounds')} 轮")
    print(f"  告警 {metrics.get('alerts_total')} 条："
          f"{metrics.get('alerts_by_kind') or '{}'}")
    print(f"  股票池 {universe.get('first')} -> {universe.get('last')}"
          f"（min {universe.get('min')} / max {universe.get('max')}），"
          f"行情 {quotes.get('first')} -> {quotes.get('last')}，"
          f"自选股降级 {metrics.get('watchlist_only_rounds')} 轮")
    print(f"  内存代理：quotes {mem.get('quotes_first')} -> {mem.get('quotes_last')}，"
          f"历史点 {mem.get('history_points_first')} -> {mem.get('history_points_last')}"
          f"（单只上限 {mem.get('history_maxlen')}）")
    print(f"  看板接口：请求 {api.get('requests')}，非 200 {api.get('non_200')}，"
          f"JSON 坏 {api.get('malformed')}，连不上 {api.get('unreachable')}")
    print(f"  SSE：连接 {'是' if sse.get('connected') else '否'}，"
          f"事件 {sse.get('events_total')} 条 {sse.get('events_by_type') or '{}'}，"
          f"断流 {'是' if sse.get('stalled') else '否'}")
    # --- WP05：观测账本（人只看这一段，所以关键数字必须都在这里）--------------
    print(_fmt_observation_summary(metrics))
    print(f"  日志：{logs}")

    print()
    print("=" * 66)
    print(" 结论")
    print("=" * 66)
    print(_fmt_table(verdict.get("checks") or []))
    print()
    warns = list(verdict.get("warn") or [])
    if verdict.get("healthy"):
        print(f"✓ 健康（exit {verdict.get('exit_code')}）")
    else:
        print(f"✗ 不健康（exit {verdict.get('exit_code')}）"
              f"  —— 失败项："
              f"{[c['name'] for c in verdict.get('checks') or [] if not c.get('ok')]}")
    if warns:
        # 黄色不改变退出码，但如果只写在 checks 里就等于没说 —— 必须显式点名。
        print(f"⚠ 警告项（不影响退出码）：{warns}")
    if report_path is not None:
        print(f"报告：{report_path}")
    else:
        print("报告：未写出（见上方错误）")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="真实盘中 soak 测试：真引擎跑真行情 N 分钟，输出诚实结论。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--minutes", type=float, default=DEFAULT_MINUTES,
                    help="浸泡时长（分钟）；始终作为兜底上限生效")
    ap.add_argument("--rounds", type=int, default=0,
                    help="按轮数设边界（>0 时优先于 --minutes，但仍受 --minutes 兜底）")
    ap.add_argument("--port", type=int, default=0,
                    help="看板端口；0 = 内核分配空闲端口")
    ap.add_argument("--browser", action="store_true",
                    help="额外用 Playwright 打开看板，收集 pageerror/console.error")
    ap.add_argument("--verbose", action="store_true",
                    help="打印引擎 INFO 日志与每轮告警明细")
    # IT-P1-UNIVERSE-WATCHLIST-FALLBACK-SESSION-BLIND-001：**显式 run mode**。
    # 只盯自选股有两种来源：用户**故意**这么跑（不是故障），
    # 或者全市场拿不到而降级（是故障）。二者在轮样本里长得一样，
    # 判决层**不能靠"股票数很少"猜意图** —— 必须有显式标记。
    ap.add_argument("--watch-only", action="store_true",
                    dest="watch_only",
                    help="显式声明只盯自选股（不判失败）；不加则降级一律判失败")
    args = ap.parse_args(list(argv) if argv is not None else None)
    if args.minutes <= 0 and not args.rounds:
        ap.error("--minutes 必须 > 0，或者用 --rounds 指定轮数")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return run(args)


if __name__ == "__main__":                           # pragma: no cover - 入口
    raise SystemExit(main())
