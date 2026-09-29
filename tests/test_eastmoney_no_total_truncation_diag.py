"""R4 / `IT-P1-006-R1-R2`：无 total 分支的 max_pages 截断诊断必须诚实。

云端任务书把 `IT-P1-006-R1`（Eastmoney `max_pages` 截断仍标 complete）
列为**待独立复核**项，因为上轮修复只覆盖了有 total 的路径。

**本地复核结论（真实代码）**：

* **安全性没有漏**：无 total 时 `transport_complete` 恒为 `False`
  （没有基准就证明不了完整，fail-closed）。三种场景实测都是 False。
* **但诊断字段说谎**：`truncated` 只在 `if total > 0` 分支被赋值，
  无 total 分支从不写它 → 当循环**跑满 `max_pages` 且末页仍有代码行**
  （= 确证被页数上限截断）时，`truncated` 报告 `False`，
  reason 还写成"无法确认是否翻完"。

后果不是漏报不完整，而是**误诊**：下游会把"被 `max_pages` 卡住"
读成"接口不返回总数"，从而去调错的那个旋钮。这是 bug 类 (g)
「同一语义在两个时点/两个轴上分别推断」——"是否被页数截断"
在两条分支上被各自推断了一次，只有一条真的推断。

⚠ 这不是"安全漏洞"，报为**诊断正确性**缺陷。诚实分级。
"""

from __future__ import annotations

import json
import urllib.parse as up

import pytest

from arad.sources import eastmoney as em


def _clist_body(codes: list[str], total: int | None) -> str:
    diff = [{"f12": c, "f14": f"股票{c}", "f2": 10.0, "f18": 9.9,
             "f8": 1.0, "f10": 1.0, "f20": 1e10, "f21": 1e10,
             "f15": 10.1, "f16": 9.8, "f17": 10.0, "f5": 1000,
             "f6": 10000, "f13": 0, "f1": 2, "f11": 1.0}
            for c in codes]
    return json.dumps({"rc": 0, "data": {"total": total, "diff": diff}})


class _Cap(em.EastmoneySource):
    """按页号供数；第 `pages_available` 页之后返回空页（= 真到底）。"""

    def __init__(self, *, total, page_size=10, max_pages=5,
                 pages_available=100):
        super().__init__(cfg={"page_size": page_size,
                              "max_pages": max_pages, "workers": 1})
        self._total = total
        self._pages_available = pages_available
        self.reqs: list[int] = []

    def _request_json(self, url):
        q = up.parse_qs(up.urlparse(url).query)
        pn = int(q.get("pn", ["1"])[0])
        self.reqs.append(pn)
        if pn > self._pages_available:
            return _clist_body([], self._total)
        base = (pn - 1) * self.page_size
        codes = [f"{600000 + base + i:06d}"
                 for i in range(min(self.page_size, 1000 - base))]
        return _clist_body(codes, self._total)


# ===========================================================================
# 核心：无 total + 翻满 max_pages 且末页仍有数据 -> truncated 必须 True
# ===========================================================================

def test_no_total_exhausting_max_pages_is_marked_truncated():
    """**核心**：确证被页数上限截断时，`truncated` 必须为 True。"""
    src = _Cap(total=None, pages_available=100, max_pages=5)
    src.universe()
    info = src.universe_info()

    assert info["pages_requested"] == 5, (
        f"应当跑满 5 页，实测 {info['pages_requested']}")
    assert info["truncated"] is True, (
        "翻满 max_pages 而末页仍有代码行 = 确证被截断，"
        f"`truncated` 不能报 {info['truncated']!r}。"
        f"reason={info.get('reason')!r}")


def test_no_total_truncation_reason_says_confirmed_not_unknown():
    """reason 必须说"确证被截断"，不能再说"无法确认"。"""
    src = _Cap(total=None, pages_available=100, max_pages=5)
    src.universe()
    reason = src.universe_info().get("reason", "")
    assert "截断" in reason, f"reason 应提到截断，实测 {reason!r}"
    assert "无法确认" not in reason, (
        f"我们**有**证据证明被截断，reason 不该说无法确认，实测 {reason!r}")


def test_transport_complete_still_false_when_truncated_no_total():
    """安全性不变：无 total 被截断时 `transport_complete` 仍须 False。"""
    src = _Cap(total=None, pages_available=100, max_pages=5)
    src.universe()
    info = src.universe_info()
    assert info["transport_complete"] is False
    assert info["complete"] is False, "兼容键 complete 必须同步为 False"


# ===========================================================================
# 阳性对照：真的翻到底 -> truncated 必须 False（不能无脑设 True）
# ===========================================================================

def test_no_total_reaching_the_end_is_not_marked_truncated():
    """**阳性对照**：真的翻到底（末页空）时 `truncated` 必须 False。

    否则上面的修复就变成"永远报截断"，那是另一种假数据。
    """
    src = _Cap(total=None, pages_available=3, max_pages=5)
    src.universe()
    info = src.universe_info()
    assert info["truncated"] is False, (
        f"已到底不该报截断，实测 {info['truncated']!r}")
    # `pages_requested` 数的是**被收下的**页（终止用的空页不计入）——
    # 第 4 页是空页，所以是 3。我第一版写 4 是我对语义的误读，
    # 不是代码错。
    assert info["pages_requested"] == 3, (
        f"pages_requested 实测 {info['pages_requested']}")


def test_no_total_end_reason_does_not_claim_truncation():
    src = _Cap(total=None, pages_available=3, max_pages=5)
    src.universe()
    reason = src.universe_info().get("reason", "")
    assert "截断" not in reason, (
        f"到底了不该说截断，实测 {reason!r}")


def test_no_total_reaching_end_still_not_complete():
    """无 total 时即使到底也**不能**报完整（没有基准）。"""
    src = _Cap(total=None, pages_available=3, max_pages=5)
    src.universe()
    assert src.universe_info()["transport_complete"] is False


# ===========================================================================
# 有 total 分支不得被回归（上轮已修，只做回归）
# ===========================================================================

def test_total_branch_truncation_still_reported():
    """有 total 且需要页数 > max_pages -> 仍须报截断 + 不完整。"""
    src = _Cap(total=1000, pages_available=100, max_pages=5)
    src.universe()
    info = src.universe_info()
    assert info["truncated"] is True
    assert info["transport_complete"] is False
    assert "max_pages" in info.get("reason", "")


def test_total_branch_fully_fetched_is_complete():
    """有 total 且能翻全 -> transport_complete 必须 True（假阴性也要防）。"""
    src = _Cap(total=30, page_size=10, pages_available=100, max_pages=5)
    src.universe()
    info = src.universe_info()
    assert info["truncated"] is False, (
        f"只需 3 页，不该报截断，实测 {info['truncated']!r}")
    assert info["transport_complete"] is True, (
        f"应当完整。reason={info.get('reason')!r} "
        f"pages_requested={info.get('pages_requested')} "
        f"expected_total={info.get('expected_total')}")
