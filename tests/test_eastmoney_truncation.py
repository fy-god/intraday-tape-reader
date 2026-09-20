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
# 4. 配置与既有契约不被破坏
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


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-o", "addopts=", "-q"]))
