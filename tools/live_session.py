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
)

#: 可观测性字段在轮样本里的"采集标记"：只有真的可测的字段才写进去。
#: 见 ``make_round_sample`` 的说明 —— 值仍然是 _safe_int/_safe_float 归一化过的
#: 旧形状（向后兼容），但汇总方靠这份标记就能分清 0 与"没采到"。
_OBSERVATION_VALUE_FIELDS: tuple[str, ...] = (
    "requested", "returned", "admitted", "coverage", "source",
    "future_rejected", "stale_rejected", "out_of_order_rejected",
    "unknown_missing", "rejected_quality", "unavailable_capability",
    "capabilities", "unavailable_by_reason",
)
#: 股票池规模允许的膨胀倍数（首末对比）。留足余量：新股上市、股票池 TTL 到期后
#: 从"自选股降级"恢复到全市场，都会让这个数字变大，那不是内存泄漏。
MEM_GROWTH_LIMIT = 2.0
MEM_GROWTH_SLACK = 500

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
        missing = obs.get("unknown_missing")
        out["unknown_missing"] = len(missing) if isinstance(missing, (list, tuple)) else 0
        quality = obs.get("rejected_quality")
        out["rejected_quality"] = len(quality) if isinstance(quality, (list, tuple)) else 0
        out["unavailable_capability"] = _safe_int(obs.get("unavailable_capability"))
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
    return out


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
        "capability_unavailable_ratio": (
            round(unavailable_rounds / obs_rounds, 4) if obs_rounds else None),
        "capability_missing_rounds": capability_missing,
        "unavailable_by_reason": unavailable_by_reason,
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
        "worst_rounds": [],
        "memory": {"bounded": True, "reason": "（未采样）"},
        "api": {"requests": 0, "non_200": 0, "malformed": 0, "unreachable": 0, "by_route": {}},
        "sse": {"connected": False, "stalled": False, "events_total": 0, "events_by_type": {},
                "status": None, "error": None},
        "logs": {"WARNING": 0, "ERROR": 0, "CRITICAL": 0},
        "browser": {"ran": False, "skipped": True, "errors": []},
        "setup": {"engine_build_s": None, "universe_refresh_s": None, "universe_size": 0},
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
    if obs_rounds <= 0:
        add("capability", True,
            f"无观测账本，无法判定能力缺失（unavailable 合计 {unavail_total}）"
            "（跳过不算失败）")
    else:
        if u_fail is not None and unavail_ratio is not None and unavail_ratio >= u_fail:
            cap_level = "fail"
        elif u_warn is not None and unavail_ratio is not None and unavail_ratio > u_warn:
            cap_level = "warn"
        else:
            cap_level = "ok"
        reasons = dict(m.get("unavailable_by_reason") or {})
        detail = (
            f"{unavail_rounds}/{obs_rounds} 轮出现'能力缺失导致规则无法评估'"
            f"（比例 {unavail_ratio:.0%}），累计不可评估标的 {unavail_total} 个；"
            f"阈值 warn >{u_warn:.0%}，fail >={u_fail:.0%}")
        if reasons:
            top = sorted(reasons.items(), key=lambda kv: (-int(kv[1]), str(kv[0])))[:5]
            detail += f"；主要原因 {dict(top)}"
        if cap_level == "fail":
            detail += " —— 整类规则在这整场 soak 里一次都没被评估过"
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

    total = sum(int(v) for v in by_kind.values())
    add("alerts", True, f"共 {total} 条告警（0 条不算失败：休市时本就无告警）")

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


def _safe_float(v: Any) -> float:
    """可观测性比率转 float；None/脏值一律 0.0（**不抛**）。"""
    if isinstance(v, bool) or v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


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
        f"  能力缺失：{metrics.get('capability_unavailable_rounds')} 轮出现，"
        f"累计 {metrics.get('capability_unavailable_total')} 个标的不可评估"
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
            quotes=len(getattr(state, "quotes", {}) or {}),
            universe=len(codes),
            history_points=sum(len(h) for h in history.values()),
            history_codes=len(history),
            max_deque=max((len(h) for h in history.values()), default=0),
            history_maxlen=maxlen,
            watchlist_only=bool(codes) and len(codes) <= len(watch),
            observation=obs_snapshot,
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
    t0 = time.perf_counter()
    universe_size = 0
    try:
        universe_size = int(engine.refresh_universe() or 0)
    except Exception as exc:                         # noqa: BLE001
        print(f"      [!] 股票池刷新异常：{type(exc).__name__}: {exc}")
    refresh_s = time.perf_counter() - t0
    codes = list(getattr(engine, "_codes", []) or [])
    watch = list(getattr(engine, "watchlist", []) or [])
    fell_back = bool(codes) and len(codes) <= len(watch)
    print(f"      股票池：{len(codes)} 只（刷新 {refresh_s:.1f}s）"
          + ("  [!] 已降级为仅自选股" if fell_back else ""))
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
    args = ap.parse_args(list(argv) if argv is not None else None)
    if args.minutes <= 0 and not args.rounds:
        ap.error("--minutes 必须 > 0，或者用 --rounds 指定轮数")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return run(args)


if __name__ == "__main__":                           # pragma: no cover - 入口
    raise SystemExit(main())
