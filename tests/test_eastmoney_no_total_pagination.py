"""WP06 —— Eastmoney 无总数时的翻页终止条件。

任务书 `2026-09-24_12-04-56_JST_AGENT_TASK.md` §WP06：
「把已有 RED/GREEN 正式落产品：raw/member empty stop + page ledger + rollback。」

缺陷（`IT-P2-EASTMONEY-UNKNOWN-TOTAL-USABLE-EMPTY-STOPS-PAGINATION-001`）
------------------------------------------------------------------------
`src/arad/sources/eastmoney.py` 的 ``universe()`` 在 **``data.total`` 不可用**
时走"顺序探测"分支，终止条件是：

```python
if not page.quotes:      # 可用行情为空 -> 停
    break
```

``page.quotes`` 是**解析后可用行情**，而一页完全可能**有原始代码行、
但可用行情为 0** —— 例如该页正好全是停牌股（无最新价）。此时循环**提前终止**，
后面真正有行情的页永远抓不到，池子被**静默截断**。

这与本仓库反复出现的那一类错完全相同：
**拿"可用子集为空"当成"传输层已经没有更多数据"**
（bug 类 e「证据子集自称全程」，只是这次方向相反 ——
把"子集为空"当成了"全集为空"）。

正确判据：**原始代码行为空**才是真的翻完了。
"""
from __future__ import annotations

import pytest

from arad.sources.eastmoney import (
    ClistPage,
    EastmoneySource,
    parse_clist_page,
)


# ===========================================================================
# 用真实解析函数构造页，不用手搓对象
# ===========================================================================

def _real_page(payload: dict, seq: int = 0) -> ClistPage:
    """走**真实** `parse_clist_page`（不是手搓 ClistPage）。

    直接 new 一个 ClistPage 会绕过解析层，那页的 raw_code_rows / codes
    是否一致就没被验证 —— 而我这个测试的全部要害正是这两者的关系。
    """
    return parse_clist_page(payload, seq)


def _payload(rows: list[dict], total=None) -> dict:
    d: dict = {"diff": rows}
    if total is not None:
        d["total"] = total
    return {"data": d, "rc": 0, "rt": 1}


# 一页"有原始行但无可用行情"：f2(最新价) 缺失/为 0 -> 停牌
#
# ⚠ 我第一版把这行写成 `{"f12","f14","f2":"-","f3":"-"}`，测试自己先红了
# （`test_parse_of_live_page_has_both` 也红）—— 因为 `_quote_of` 还要求
# **f18（昨收）** 存在且 > 0。F2 缺失本来就该丢（那是停牌/无效），
# 但"正常页"必须把 f18 给全，否则我测的不是翻页逻辑、而是我自己的
# payload 写错。这里按真实 clist 字段补齐。
_SUSPENDED_ROW = {"f12": "600001", "f14": "停牌股", "f2": "-", "f18": "10.0"}
# 一页正常股（f2 + f18 都要有）
_LIVE_ROW = {"f12": "600002", "f14": "正常股",
             "f2": "10.5", "f18": "10.0", "f3": "1.2"}


def test_parse_of_suspended_page_has_raw_but_no_usable():
    """**前提校验**：停牌页必须"有原始代码行、可用行情为 0"。

    这条不成立的话，下面那个缺陷就无从谈起 —— 所以先把前提钉住。
    """
    p = _real_page(_payload([_SUSPENDED_ROW]))
    assert p.raw_code_rows == 1, f"应有 1 个原始代码行，实测 {p.raw_code_rows}"
    assert p.codes == ("600001",), f"code 应被记下，实测 {p.codes}"
    assert p.quotes == [], (
        f"停牌股不该产出可用行情，实测 {p.quotes} —— "
        f"若这里是非空，说明测试前提变了，要重新检查")


def test_parse_of_live_page_has_both():
    """**阳性对照**：正常页两个账都非空。"""
    p = _real_page(_payload([_LIVE_ROW]))
    assert p.raw_code_rows == 1
    assert len(p.quotes) == 1


# ===========================================================================
# 核心：无总数时的终止条件
# ===========================================================================

class _StubSource(EastmoneySource):
    """把网络层换掉，但**保留真实的 universe() 翻页逻辑**。"""

    def __init__(self, pages: list[dict]):
        super().__init__()
        self.max_pages = 10
        self.page_size = 1
        self._pages = pages            # 每页的原始 payload
        self.requested_pages: list[int] = []

    def _request_json(self, url: str, seq: int = 0):  # noqa: D102
        # url 里带 pn=<n>；直接按顺序取
        pn = len(self.requested_pages) + 1
        self.requested_pages.append(pn)
        if pn <= len(self._pages):
            return self._pages[pn - 1]
        return _payload([])


def _live(code: str, name: str = "正常股") -> dict:
    return {"f12": code, "f14": name, "f2": "10.5", "f18": "10.0"}


def _susp(code: str) -> dict:
    return {"f12": code, "f14": "停牌股", "f2": "-", "f18": "10.0"}


def test_suspended_page_does_not_stop_pagination():
    """**RED**：无总数时，循环内某页"停牌（有 raw 行、无可用行情）"**不得**终止。

    ⚠ 我第一版把这个测试写成 ``pages=[停牌, 正常, 空]``，结果 **6 passed** ——
    看起来像"产品已经是对的"。用 `r12_pagination_diag.py` 打决策点才发现
    **它根本没走进被测分支**（D15 教训：探针必须真的穿过被测分支）：

    * 第 1 页是**循环外**的 ``first``，无条件 ``pages.append(first)``，
      不受那个 ``break`` 管辖；
    * 循环从 ``pn=2`` 开始，所以"第 1 页停牌"**测不到**那个 break。

    正确的构造必须让**循环内**的一页停牌，且**各页代码互不相同** ——
    否则"数据丢失"根本不可观察（第 1 页那只照样能拿到）。

      1) 正常页 600011（first，循环外）
      2) **停牌页 600022**（raw=1, usable=0）  <- 旧代码在这里 break
      3) 正常页 600033                          <- 永远抓不到
      4) 真空页（raw=0）                        <- 真正的终止点

    旧行为：请求 [1,2]，600033 **永久丢失**。
    正确行为：请求 [1,2,3,4]，拿到 600011 与 600033。
    """
    src = _StubSource([
        _payload([_live("600011")]),   # p1: 正常（循环外 first）
        _payload([_susp("600022")]),   # p2: 停牌 <- 旧代码在这里 break
        _payload([_live("600033")]),   # p3: 正常 <- 旧代码永远抓不到
        _payload([]),                  # p4: 真空页 -> 停
    ])
    quotes = src.universe()
    codes = {q.code for q in quotes}
    assert "600011" in codes, "第 1 页必须拿到"
    assert "600033" in codes, (
        f"第 3 页的 600033 必须拿到 —— 旧代码在第 2 页（停牌）就 break，"
        f"这只票**静默丢失**。实测 codes={sorted(codes)}，"
        f"请求页号 {src.requested_pages}")
    assert src.requested_pages == [1, 2, 3, 4], (
        f"必须翻到第 4 页（真空页）才停，实测请求页号 {src.requested_pages} —— "
        f"在 [1,2] 停说明把『可用行情为空』当成了『传输层翻完』")


def test_first_page_suspended_also_continues():
    """**边界/阳性对照**：第 1 页就停牌时，循环也必须继续。

    第 1 页是循环外的 ``first``，不受那个 break 管辖 —— 这条本来就应该过。
    它存在的意义是证明**我的测试构造真能区分两种情形**
    （所以它能反证上面那条不是因为 payload 写错才红的）。
    """
    src = _StubSource([
        _payload([_susp("600011")]),   # p1: 停牌（first）
        _payload([_live("600033")]),   # p2: 正常
        _payload([]),                  # p3: 真空页 -> 停
    ])
    quotes = src.universe()
    codes = {q.code for q in quotes}
    assert "600033" in codes, (
        f"第 2 页正常股必须拿到，实测 codes={sorted(codes)}")
    assert src.requested_pages == [1, 2, 3], f"实测 {src.requested_pages}"


def test_first_page_suspended_also_continues():
    """**边界**：第 1 页就停牌时，循环也必须继续（first 页不受 break 管辖）。

    这条是**阳性对照**式的存在性证明：确认"第 1 页停牌"这种情形本身
    不会提前结束（旧代码在这条上本来就对，所以它证明的是我的
    测试构造真的能区分两种情形）。
    """
    src = _StubSource([
        _payload([_SUSPENDED_ROW]),   # p1: 停牌（first）
        _payload([_LIVE_ROW]),        # p2: 正常
        _payload([]),                 # p3: 真空页 -> 停
    ])
    quotes = src.universe()
    codes = {q.code for q in quotes}
    assert "600002" in codes, (
        f"第 2 页正常股必须拿到，实测 codes={sorted(codes)}")
    assert src.requested_pages == [1, 2, 3], f"实测 {src.requested_pages}"


def test_empty_raw_page_stops_pagination():
    """**阳性对照**：真正翻完（raw 行也为空）时必须停，不能无限翻。"""
    src = _StubSource([
        _payload([_LIVE_ROW]),        # p1: 正常
        _payload([]),                 # p2: 真空页 -> 停
    ])
    src.universe()
    assert src.requested_pages == [1, 2], (
        f"空 raw 页应终止，实测 {src.requested_pages}")


def test_all_suspended_pages_do_not_loop_forever():
    """**边界**：全停牌时必须靠 max_pages 收住，不能死循环。"""
    src = _StubSource([_payload([_SUSPENDED_ROW]) for _ in range(20)])
    src.universe()
    assert len(src.requested_pages) <= src.max_pages, (
        f"不得超 max_pages，实测 {len(src.requested_pages)}")


def test_no_total_still_reports_incomplete():
    """无总数时 transport_complete 必须**仍**为 False（不能假称完整）。

    这是原有语义（"没有基准就证明不了完整"），本轮修复**不得**弄坏它 ——
    否则就是把"翻页提前停"换成"假报完整"，更糟。
    """
    src = _StubSource([
        _payload([_LIVE_ROW]),
        _payload([]),
    ])
    src.universe()
    meta = src.universe_info()
    assert meta.get("transport_complete") is False, (
        f"无总数不得声称传输完整，实测 {meta.get('transport_complete')}")
    assert meta.get("complete") is False
