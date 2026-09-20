"""IT-P1-006-R1 回归：东财 ``universe()`` 在翻页被 ``max_pages`` 截断时必须
报 ``complete=False``。

缺陷（修复前）：``pages = min(self.max_pages, ceil(total/page_size))`` 静默截断，
而 ``complete = bool(total > 0) and not failed`` 只看「有总数 + 无失败页」，
于是**少于 ``expected_total`` 的部分 universe 被当成完整池**。引擎据此把
300 只的部分池当成全市场（5913 只），静默丢掉 95% 的票。

修复后语义：
* ``required_pages > max_pages`` -> ``truncated=True``，``complete=False``；
* ``required_pages <= max_pages`` -> ``truncated=False``，``complete`` 沿用
  「有总数且无失败页」；
* 恰好等于 ``max_pages`` **不算**截断（边界）；
* 截断**不抛异常** —— 调用方依赖「拿回部分数据继续跑」。

全程离线：伪造 fetcher，禁止真实网络。
"""
from __future__ import annotations

import json
import urllib.parse

import pytest

from arad.config import load_settings
from arad.engine import Engine
from arad.session import TradingCalendar
from arad.sources.eastmoney import EastmoneySource, PAGE_LIMIT

# 真实量级：2026-09-15 探针实测全市场 total=5913，page_size 被服务端压到 100。
CLIST_TOTAL = 5913
EM_CFG = {"page_size": 100, "max_pages": 80, "workers": 8, "timeout": 15, "retries": 3}


# --------------------------------------------------------------------------
# 离线替身（本文件自足，不依赖其它测试模块）
# --------------------------------------------------------------------------
class FakeFetcher:
    """``fetcher(url, headers, timeout) -> bytes`` 的可编程替身。"""

    def __init__(self, responder):
        self.responder = responder
        self.calls: list[str] = []

    def __call__(self, url: str, headers: dict, timeout: float) -> bytes:
        self.calls.append(url)
        out = self.responder(url)
        if isinstance(out, str):
            out = out.encode("utf-8")
        if isinstance(out, Exception):
            raise out
        return out

    @property
    def clist_pages(self) -> list[int]:
        return sorted(
            int(v)
            for u in self.calls
            for k, v in urllib.parse.parse_qsl(urllib.parse.urlparse(u).query)
            if k == "pn"
        )


def _row(code: str) -> dict:
    return {
        "f2": 10.0, "f3": 1.0, "f5": 1000, "f6": 1_000_000.0, "f8": 1.0,
        "f10": 1.0, "f11": 0.0, "f12": code, "f13": 0, "f14": f"股票{code}",
        "f15": 10.2, "f16": 9.8, "f17": 9.9, "f18": 9.9,
        "f20": 1_000_000_000, "f21": 800_000_000, "f22": 0.0,
        "f26": 20200101, "f124": 1789371291,
    }


def _payload(total: int, rows: list[dict]) -> str:
    return json.dumps({"rc": 0, "data": {"total": total, "diff": rows}})


def _paging_fetcher(total: int = CLIST_TOTAL, per_page: int = 100,
                    fail_pages: set[int] | None = None) -> FakeFetcher:
    """按 pn 生成分页 payload；``fail_pages`` 里的页抛异常。"""
    bad = set(fail_pages or ())

    def responder(url: str):
        pn = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["pn"][0])
        if pn in bad:
            raise RuntimeError(f"page {pn} boom")
        start = (pn - 1) * per_page
        n = max(0, min(per_page, total - start))
        return _payload(total, [_row(f"{600000 + start + i}") for i in range(n)])

    return FakeFetcher(responder)


def _ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def _shrinking_fetcher(total: int = CLIST_TOTAL, rows_per_page: int = 10,
                       page_span: int = 100, clip: bool = True) -> FakeFetcher:
    """``total`` 声明不变，但**每页只回 ``rows_per_page`` 行**（内容缩水）。

    ``clip=True`` 时末页按 ``total`` 截齐（``len(out) <= total``）；
    ``clip=False`` 时末页照发满 ``rows_per_page`` 行，制造
    ``len(out) > expected_total`` 的**上界重叠**（翻页边界重复）。

    页码步进仍按 ``page_span=100``（即 ``page_size``），所以 ``required_pages``
    与足额时一致 —— 用来单独分离「页数够但行数不够」这一根因。
    """

    def responder(url: str):
        pn = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["pn"][0])
        start = (pn - 1) * page_span
        n = rows_per_page
        if clip:
            n = max(0, min(rows_per_page, total - start))
        return _payload(total, [_row(f"{600000 + start + i}") for i in range(n)])

    return FakeFetcher(responder)


# ==========================================================================
# 1. 核心缺陷：截断 -> complete 必须为 False
# ==========================================================================
class TestTruncationIsIncomplete:
    def test_truncated_universe_is_not_complete(self):
        """total=5913 需 60 页，max_pages=3 -> 必须报 complete=False。

        修复前这里是红的：complete=True，300 只被当成全市场 5913 只。
        """
        f = _paging_fetcher()
        src = EastmoneySource({**EM_CFG, "max_pages": 3}, fetcher=f)
        quotes = src.universe()
        info = src.universe_info()

        assert _ceil_div(CLIST_TOTAL, src.page_size) == 60 > src.max_pages
        assert f.clist_pages == [1, 2, 3], "只应抓 max_pages 页"
        assert len(quotes) == 300, "截断仍要拿回部分数据（不抛异常）"
        assert info["complete"] is False, (
            f"截断必须报 incomplete，实际 complete={info['complete']!r}, info={info}"
        )

    def test_truncation_is_exposed_for_diagnostics(self):
        """截断事实要显式可见：truncated 字段 + 三个页数对比。"""
        f = _paging_fetcher()
        src = EastmoneySource({**EM_CFG, "max_pages": 5}, fetcher=f)
        src.universe()
        info = src.universe_info()

        assert info.get("truncated") is True, f"缺 truncated 标志: {info}"
        assert info["required_pages"] == 60, info
        assert info["max_pages"] == 5, info
        assert info["expected_total"] == CLIST_TOTAL, info
        assert info["required_pages"] > info["max_pages"]
        assert "截断" in (info.get("reason") or ""), f"reason 应说明截断: {info}"

    def test_truncation_does_not_raise(self):
        """截断不是错误路径：调用方依赖「部分数据继续跑」。"""
        src = EastmoneySource({**EM_CFG, "max_pages": 1}, fetcher=_paging_fetcher())
        quotes = src.universe()          # 不得抛 SourceError
        assert len(quotes) == 100
        assert src.universe_info()["complete"] is False


# ==========================================================================
# 2. 回归保护：正常路径不能被修坏
# ==========================================================================
class TestUntruncatedStaysComplete:
    def test_full_universe_is_complete(self):
        """max_pages=80 >= 所需 60 页 -> complete=True（既有行为不变）。"""
        f = _paging_fetcher()
        src = EastmoneySource(EM_CFG, fetcher=f)
        quotes = src.universe()
        info = src.universe_info()

        assert len(f.clist_pages) == 60
        assert len(quotes) == CLIST_TOTAL
        assert info["complete"] is True, info
        assert info.get("truncated") is False, info
        assert info["required_pages"] == 60
        assert info["max_pages"] == 80
        assert info["pages_failed"] == 0

    def test_accounting_matches_required_pages(self):
        """未截断时 required_pages 应与实际抓取页数一致。"""
        f = _paging_fetcher(total=1500)
        src = EastmoneySource(EM_CFG, fetcher=f)
        src.universe()
        info = src.universe_info()
        assert src.page_size == PAGE_LIMIT == 100
        assert info["required_pages"] == 15 == len(f.clist_pages)
        assert info["complete"] is True
        assert info.get("truncated") is False

    def test_failed_page_still_incomplete_without_truncation(self):
        """未截断但有失败页 -> complete=False（原有语义必须保留）。"""
        f = _paging_fetcher(total=500, fail_pages={3})
        src = EastmoneySource({**EM_CFG, "retries": 1}, fetcher=f)
        src.universe()
        info = src.universe_info()
        assert info["required_pages"] == 5 <= info["max_pages"]
        assert info.get("truncated") is False
        assert info["complete"] is False, info
        assert info["pages_failed"] == 1

    def test_truncated_and_failed_page_still_incomplete(self):
        """截断 + 失败页叠加，仍是 incomplete。"""
        f = _paging_fetcher(fail_pages={2})
        src = EastmoneySource({**EM_CFG, "max_pages": 3, "retries": 1}, fetcher=f)
        src.universe()
        info = src.universe_info()
        assert info["complete"] is False
        assert info.get("truncated") is True
        assert info["pages_failed"] == 1


# ==========================================================================
# 3. 边界：恰好等于 max_pages 不算截断
# ==========================================================================
class TestTruncationBoundary:
    def test_exactly_max_pages_is_not_truncated(self):
        """total=300 需 3 页，max_pages=3 -> required == max_pages，不算截断。"""
        f = _paging_fetcher(total=300)
        src = EastmoneySource({**EM_CFG, "max_pages": 3}, fetcher=f)
        quotes = src.universe()
        info = src.universe_info()

        assert info["required_pages"] == 3 == info["max_pages"]
        assert info.get("truncated") is False, info
        assert info["complete"] is True, info
        assert len(quotes) == 300
        assert f.clist_pages == [1, 2, 3]

    def test_one_page_over_max_pages_is_truncated(self):
        """total=301 需 4 页，max_pages=3 -> required > max_pages，算截断。"""
        f = _paging_fetcher(total=301)
        src = EastmoneySource({**EM_CFG, "max_pages": 3}, fetcher=f)
        quotes = src.universe()
        info = src.universe_info()

        assert info["required_pages"] == 4 > info["max_pages"]
        assert info.get("truncated") is True, info
        assert info["complete"] is False, info
        assert len(quotes) == 300

    def test_exact_multiple_boundary(self):
        """整页倍数：total=500 需 5 页，max_pages=5 恰好翻完 -> 完整。"""
        f = _paging_fetcher(total=500)
        src = EastmoneySource({**EM_CFG, "max_pages": 5}, fetcher=f)
        src.universe()
        info = src.universe_info()
        assert info["required_pages"] == 5
        assert info.get("truncated") is False
        assert info["complete"] is True


# ==========================================================================
# 4. IT-P1-COMPLETE-001：页数够但**行数缩水** -> complete 必须为 False
# ==========================================================================
class TestShrunkenPagesAreIncomplete:
    """``complete`` 必须与实际拿到的行数挂钩，不能只看页数。

    修复前 ``complete = bool(total>0) and not failed and not truncated``：
    每页只回 10 行时 ``returned=600 / expected_total=5913``（10.1%）仍标
    ``complete=True``，``engine.py`` 的 ``if not meta.get("complete", True)``
    门控放行，引擎把 600 只当成完整全市场采用。
    """

    def test_shrunken_pages_are_not_complete(self):
        """审计实测场景：total=5913 需 60 页（<= max_pages=80），每页只回 10 行。

        修复前这里是红的：返回 600 行仍报 complete=True。
        """
        f = _shrinking_fetcher(rows_per_page=10)
        src = EastmoneySource(EM_CFG, fetcher=f)
        quotes = src.universe()
        info = src.universe_info()

        # 页数轴完全正常 —— 缺陷只在行数轴上，故截断/失败页都必须为假
        assert info["required_pages"] == 60 <= info["max_pages"] == 80
        assert len(f.clist_pages) == 60, "60 页照常请求"
        assert info["truncated"] is False, "页数够，不是截断"
        assert info["pages_failed"] == 0, "没有失败页"
        assert len(quotes) == 600, "缩水仍要拿回部分数据（不抛异常）"
        assert info["returned"] == 600
        assert info["expected_total"] == CLIST_TOTAL

        assert info["complete"] is False, (
            f"只拿到 {len(quotes)}/{CLIST_TOTAL} 行却报 complete=True，"
            f"引擎会把 10.1% 当完整全市场；info={info}"
        )

    def test_shrunk_reason_names_the_count_shortfall(self):
        """reason 必须能看出是**行数不足**（区别于截断/失败页）。"""
        f = _shrinking_fetcher(rows_per_page=10)
        src = EastmoneySource(EM_CFG, fetcher=f)
        src.universe()
        info = src.universe_info()

        reason = info.get("reason") or ""
        assert "行数" in reason and "缩水" in reason, f"reason 应说明行数缩水: {reason}"
        assert "600" in reason and str(CLIST_TOTAL) in reason, (
            f"reason 应给出实际行数与期望总数: {reason}")
        assert "截断" not in reason, f"未截断就不该说截断: {reason}"

    def test_shrunk_universe_does_not_raise(self):
        """缩水不是异常路径：调用方依赖「部分数据继续跑」。"""
        src = EastmoneySource(EM_CFG, fetcher=_shrinking_fetcher(rows_per_page=1))
        quotes = src.universe()          # 不得抛 SourceError
        info = src.universe_info()
        assert len(quotes) == 60
        assert info["complete"] is False

    def test_truncated_and_shrunk_reason_mentions_both(self):
        """截断 + 缩水叠加时，reason 两种不完整都要说明。"""
        f = _shrinking_fetcher(rows_per_page=10)
        src = EastmoneySource({**EM_CFG, "max_pages": 3}, fetcher=f)
        src.universe()
        info = src.universe_info()
        assert info["truncated"] is True
        assert info["complete"] is False
        reason = info.get("reason") or ""
        assert "截断" in reason, reason
        assert "行数缩水" in reason, reason


# ==========================================================================
# 5. IT-P1-COMPLETE-001 边界：容差必须是「不少于」，不是「等于」
# ==========================================================================
class TestReturnedCountBoundary:
    def test_upper_bound_overlap_stays_complete(self):
        """``len(out) > expected_total``（翻页边界重复）是**正常**上界重叠。

        审计明确：``returned=6000`` 略高于 ``expected_total=5913`` 不是缺陷。
        修复**不得**因为它略多就判 incomplete（即不能写成 ``==``）。

        这里每页回 120 行、页码步进仍是 100，相邻页有 20 行重叠（模拟翻页
        边界重复），去重后 600000..606019 共 6020 行 > 5913。
        """
        f = _shrinking_fetcher(rows_per_page=120, clip=False)
        src = EastmoneySource(EM_CFG, fetcher=f)
        quotes = src.universe()
        info = src.universe_info()

        assert info["truncated"] is False
        assert len(quotes) == 6020 > info["expected_total"] == CLIST_TOTAL
        assert info["returned"] == 6020
        assert info["complete"] is True, (
            f"略高于总数是正常上界重叠，不得判 incomplete: {info}")
        assert info["reason"] == "已按总数翻完", info

    def test_exactly_expected_total_is_complete(self):
        """恰好 ``len(out) == expected_total`` -> complete=True（回归保护）。"""
        f = _paging_fetcher(total=CLIST_TOTAL)
        src = EastmoneySource(EM_CFG, fetcher=f)
        quotes = src.universe()
        info = src.universe_info()

        assert len(quotes) == info["returned"] == info["expected_total"] == CLIST_TOTAL
        assert info["complete"] is True, info
        # shortfall 是本次新加的诊断字段；用 get 以便回退验牙时本用例仍能通过
        assert info.get("shortfall", 0) == 0, info

    def test_one_row_below_expected_total_is_incomplete(self):
        """只少 1 行（``len(out) == expected_total - 1``）-> complete=False。

        页数轴完全正常（60 页、无失败、无截断），只把末页最后一行丢掉 ——
        证明判据确实是「不少于」，而不是只看页数。
        """
        def responder(url: str):
            pn = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["pn"][0])
            start = (pn - 1) * 100
            n = max(0, min(100, CLIST_TOTAL - start))
            rows = [_row(f"{600000 + start + i}") for i in range(n)]
            if pn == 60:
                rows = rows[:-1]                     # 恰好少 1 行
            return _payload(CLIST_TOTAL, rows)

        f = FakeFetcher(responder)
        src = EastmoneySource(EM_CFG, fetcher=f)
        quotes = src.universe()
        info = src.universe_info()

        assert info["required_pages"] == 60 <= info["max_pages"]
        assert info["truncated"] is False and info["pages_failed"] == 0
        assert len(quotes) == CLIST_TOTAL - 1 == 5912
        assert info["complete"] is False, (
            f"少 1 行也是不完整（判据是「不少于」）: {info}")
        assert info["shortfall"] == 1, info
        assert "行数缩水" in (info["reason"] or ""), info


# ==========================================================================
# 6. 配置与既有契约不被破坏
# ==========================================================================
class TestConfigContractUnchanged:
    def test_max_pages_default_is_still_80(self):
        """修复不得改默认值。"""
        src = EastmoneySource({}, fetcher=FakeFetcher(lambda u: _payload(0, [])))
        assert src.max_pages == 80

    def test_no_total_still_incomplete(self):
        """total 不可用（data/diff 为 null）时仍报 incomplete（IT-P1-006）。"""
        f = FakeFetcher(lambda u: '{"rc":0,"data":null}')
        src = EastmoneySource(EM_CFG, fetcher=f)
        quotes = src.universe()
        info = src.universe_info()
        assert quotes == []
        assert info["complete"] is False
        assert info.get("truncated") is False, "没有总数就没有截断的概念"

    def test_full_market_realistic_case(self):
        """真实量级：5913 只 / 100 每页 = 60 页，默认 max_pages=80 够用。"""
        f = _paging_fetcher(total=CLIST_TOTAL)
        src = EastmoneySource(EM_CFG, fetcher=f)
        quotes = src.universe()
        assert len(quotes) == CLIST_TOTAL
        assert src.universe_info()["complete"] is True


# ==========================================================================
# 7. WP01 / IT-P1-COMPLETE-001-R1：transport 与 usable **双账**
# ==========================================================================
#: 修复前：``shortfall = expected_total - len(out)``，而 ``len(out)`` 是 post-parse
#: 的 usable Quote 数、``expected_total`` 是 API 的 transport/universe 总数。
#: 契约明文允许 parser 丢弃停牌/无效行（``docs/DATA_CONTRACT.md`` 第 2.2 节），
#: 于是「传输完全健康 + 市场里有停牌股」被误判成 transport incomplete，
#: ``engine.py`` 的 ``complete`` 门控拒绝刷新已有池 —— 新上市代码永远进不来。
#
#: 修复后：两个口径分开记账，``complete`` 只由 transport 决定。
NEW_CODES = ["605999", "605998", "605997", "605996", "605995",
             "605994", "605993", "605992", "605991", "605990"]


def _suspended_row(code: str) -> dict:
    """停牌/无效行：``f2`` 为 ``"-"``，按契约必须整条丢弃。"""
    row = _row(code)
    row["f2"] = "-"
    row["f18"] = "-"
    return row


def _suspend_every_fetcher(every: int, total: int = CLIST_TOTAL,
                           new_codes: list[str] | None = None) -> FakeFetcher:
    """每页**足额**返回（raw transport 健康），但每 ``every`` 行插 1 行停牌。

    ``every=0`` -> 无停牌。``new_codes`` 追加在末尾，池子总数因此变成
    ``total + len(new_codes)``（模拟新上市让全市场变大）。

    停牌只打在**原有**代码上（``index < total``）：现实的停牌是老股票，而
    新上市代码正是因为在交易才需要进池。停牌行**仍有唯一 code**，只是价格
    无效 —— 这正是两个口径必须分开的地方。
    """
    extra = list(new_codes or [])
    codes = [f"{600000 + i}" for i in range(total)] + extra
    feed_total = len(codes)

    def responder(url: str):
        pn = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["pn"][0])
        start = (pn - 1) * 100
        rows = []
        for i, c in enumerate(codes[start:start + 100]):
            idx = start + i
            bad = every > 0 and idx < total and (idx % every == 0)
            rows.append(_suspended_row(c) if bad else _row(c))
        return _payload(feed_total, rows)

    return FakeFetcher(responder)


def _dup_code_fetcher(unique_per_page: int = 50) -> FakeFetcher:
    """raw 行数够（6000 行）但 unique code 只有 3000（大量重复 code）。

    页码步进 50，每页 100 行、只有 50 个唯一 code 重复两次 —— raw 行数
    溢出 ``total``，但真实代码覆盖只有一半。
    """
    def responder(url: str):
        pn = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["pn"][0])
        base = (pn - 1) * unique_per_page
        rows = [_row(f"{600000 + base + (i % unique_per_page)}") for i in range(100)]
        return _payload(CLIST_TOTAL, rows)

    return FakeFetcher(responder)


def _engine_with_pool(pool: list[str]):
    """构造一个离线 Engine，股票池专用来源留给调用方注入。"""
    st = load_settings(use_cache=False)
    st.section("sources")["universe"] = ["eastmoney"]
    cal = TradingCalendar(holidays=set())
    eng = Engine(source=None, settings=st, rules=[], notifiers=[],
                 watchlist=[], calendar=cal)
    eng._codes = list(pool)
    return eng


class TestTransportUsableDoubleLedger:
    """``transport_complete``（传输/池规模）与 ``usable_coverage``（解析可用率）分开。"""

    def test_suspended_rows_keep_transport_complete(self):
        """**真验牙**：raw code 全覆盖 + 约 6% 行 parse 不可用。

        每页足额 100 行、无失败页、未截断，唯一的变化是市场里有停牌股
        （``f2="-"``，契约要求丢弃）。修复前 ``complete=False``（红）；
        修复后 ``transport_complete is True`` 且 ``usable_coverage < 1.0``。
        """
        src = EastmoneySource(EM_CFG, fetcher=_suspend_every_fetcher(every=16))
        quotes = src.universe()
        info = src.universe_info()

        # 传输轴完全健康（旧字段，修复前后都存在）
        assert info["pages_requested"] == 60
        assert info["pages_failed"] == 0
        assert info["truncated"] is False
        assert info["required_pages"] == 60 <= info["max_pages"]

        # **先断言语义**：修复前 complete=False（红），不能只因为缺新键而红。
        # 用 .get 取新键，缺失即 None -> 在传输轴语义上失败，而不是 KeyError。
        assert info.get("transport_complete") is True, (
            f"传输完全健康，停牌行不该让 transport 判 incomplete: {info}")
        assert info["complete"] is True, "兼容字段必须跟随 transport_complete"
        assert info.get("usable_coverage") is not None
        assert info["usable_coverage"] < 1.0, (
            f"usable_coverage 必须单独暴露解析损失: {info}")

        # 双账：raw 覆盖满，usable 少一截
        assert info["transport_expected_total"] == CLIST_TOTAL
        assert info["raw_rows"] == CLIST_TOTAL, f"raw 行数应足额: {info}"
        assert info["raw_unique_codes"] == CLIST_TOTAL, f"raw code 应全覆盖: {info}"
        assert info["usable_quotes"] == len(quotes) == 5543
        assert info["dropped_invalid"] == 370
        assert info["duplicate_codes"] == 0
        assert info["usable_coverage"] == pytest.approx(5543 / CLIST_TOTAL)

    def test_double_ledger_reconciles_exactly(self):
        """两侧账本必须能对上（供 universe_transport_reconcile.json 用）。"""
        src = EastmoneySource(EM_CFG, fetcher=_suspend_every_fetcher(every=16))
        src.universe()
        info = src.universe_info()

        assert info["raw_rows"] == info["usable_rows"] + info["dropped_invalid"]
        assert (info["raw_unique_codes"] + info["duplicate_codes"]
                == info["raw_code_rows"])
        assert info["usable_quotes"] <= info["usable_rows"] <= info["raw_rows"]
        assert info["returned"] == info["usable_quotes"]

    def test_complete_field_mirrors_transport_complete(self):
        """兼容要求：``meta["complete"] = meta["transport_complete"]``。

        ``engine.py`` 的 ``if not meta.get("complete", True)`` 必须继续工作。
        """
        for every, expected in ((0, True), (16, True), (1, True)):
            src = EastmoneySource(EM_CFG, fetcher=_suspend_every_fetcher(every=every))
            src.universe()
            info = src.universe_info()
            assert info["complete"] is info["transport_complete"] is expected, info

    def test_shrinking_pages_break_transport_coverage(self):
        """回归保护：每页**真的**缩水 -> ``transport_complete=False``。

        这条保住 IT-P1-COMPLETE-001（16:25 修的轴）与旧 ``shortfall`` 判据同向。
        """
        f = _shrinking_fetcher(rows_per_page=10)
        src = EastmoneySource(EM_CFG, fetcher=f)
        quotes = src.universe()
        info = src.universe_info()

        # 先断言修复前后都存在字段的语义（旧判据同向），再查新键
        assert info["truncated"] is False and info["pages_failed"] == 0
        assert len(quotes) == 600
        assert info["complete"] is False, info
        assert info["expected_total"] == CLIST_TOTAL
        assert info["returned"] == 600
        assert "行数缩水" in (info["reason"] or ""), info

        assert info["raw_rows"] == 600
        assert info["raw_unique_codes"] == 600
        assert info["transport_expected_total"] == CLIST_TOTAL
        assert info["shortfall"] == CLIST_TOTAL - 600
        assert info["transport_complete"] is False, info

    def test_duplicate_codes_break_transport_coverage(self):
        """raw 行数够但 unique code 不足（大量重复 code）-> False。"""
        src = EastmoneySource(EM_CFG, fetcher=_dup_code_fetcher())
        src.universe()
        info = src.universe_info()

        assert info["complete"] is False, info
        assert info["raw_rows"] == 6000 >= CLIST_TOTAL, "raw 行数确实够"
        assert info["raw_unique_codes"] == 3000 < CLIST_TOTAL, "唯一 code 不足"
        assert info["duplicate_codes"] == 3000
        assert info["shortfall"] == CLIST_TOTAL - 3000
        assert info["transport_complete"] is False, info

    def test_upward_overlap_stays_transport_complete(self):
        """翻页边界重叠（unique code 略多于 total）仍是正常完整。

        回归保护：判据必须是「不少于」而非「等于」。
        """
        f = _shrinking_fetcher(rows_per_page=120, clip=False)
        src = EastmoneySource(EM_CFG, fetcher=f)
        quotes = src.universe()
        info = src.universe_info()

        assert len(quotes) == 6020 > info["expected_total"] == CLIST_TOTAL
        assert info["complete"] is True, info
        assert info["reason"] == "已按总数翻完", info
        assert info["raw_unique_codes"] == 6020
        assert info["shortfall"] == 0
        assert info["transport_complete"] is True, info

    def test_no_total_still_transport_incomplete(self):
        """没有总数就没有基准 -> transport 仍不可证明为完整。"""
        src = EastmoneySource(EM_CFG, fetcher=FakeFetcher(lambda u: '{"rc":0,"data":null}'))
        assert src.universe() == []
        info = src.universe_info()
        assert info["complete"] is False
        assert info["transport_complete"] is False


class TestNewListingsEnterPoolDespiteInvalidRows:
    """真实业务后果验收：正常无效行不得阻止新上市代码进入股票池。"""

    def test_new_listings_enter_pool(self):
        """**真验牙**：现有 5913 只全市场池 + 10 只新上市 + 约 6% 停牌。

        修复前 ``complete=False`` -> 走 partial 分支 -> 「不得小于现有池」
        保护拒绝覆盖 -> ``refresh_universe() == 0``，新代码 0/10 进入（红）。
        修复后 transport 完整 -> 立即采纳 -> 10/10 进入。
        """
        new = list(NEW_CODES)
        pool = [q.code for q in
                EastmoneySource(EM_CFG, fetcher=_suspend_every_fetcher(every=0)).universe()]
        assert len(pool) == CLIST_TOTAL

        src = EastmoneySource(EM_CFG, fetcher=_suspend_every_fetcher(every=16, new_codes=new))
        eng = _engine_with_pool(pool)
        eng._universe_src = [src]

        n = eng.refresh_universe()
        info = src.universe_info()

        # **先断言业务后果**：修复前这里就是 0/10（红）。
        entered = sorted(c for c in new if c in set(eng._codes))
        assert entered == sorted(new), (
            f"新上市代码必须全部进入股票池，实际 {len(entered)}/{len(new)}: {entered}；"
            f"refresh_universe 返回 {n}；complete={info.get('complete')}")
        assert n != 0, f"完整 transport 结果必须被采纳，实际 refresh_universe 返回 {n}"
        # 再查账本
        assert info.get("transport_complete") is True, info
        assert info["transport_expected_total"] == CLIST_TOTAL + len(new)

    def test_new_listings_also_enter_when_pool_is_pinned_large(self):
        """现有池比 usable 结果更大时也要采纳（新股增长不能被尺寸保护挡住）。"""
        new = list(NEW_CODES)
        src = EastmoneySource(EM_CFG, fetcher=_suspend_every_fetcher(every=16, new_codes=new))
        eng = _engine_with_pool([f"{600000 + i}" for i in range(CLIST_TOTAL)])
        eng._universe_src = [src]

        assert eng.refresh_universe() != 0
        assert all(c in set(eng._codes) for c in new)


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-o", "addopts=", "-q"]))
