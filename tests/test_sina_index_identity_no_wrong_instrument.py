"""`IT-P1-SINA-INDEX-PREFIX-LOSS-SILENT-WRONG-INSTRUMENT-012`

云端 `2026-09-24_16-02-09_JST.md` §1 报告的新缺陷。

缺陷本体
--------
Sina 的两条取数路径都先**剥掉**显式交易所前缀，再用
:func:`arad.models.guess_prefix` 重建 wire 符号：

```python
wanted   = _norm_codes(codes)                      # sh000001 -> 000001
prefixed = [f"{guess_prefix(c)}{c}" for c in codes] # 000001 -> sz000001
```

而 :func:`guess_prefix` 的 docstring 自己就写着「**只对个股可靠**。
指数必须由调用方显式给出前缀」—— 调用方给的 `sh000001` 前缀
**已经在上一行被剥掉了**。

实测（`config/settings.yaml` 的默认 5 个 index_codes）：

| 请求 | 剥前缀后 | 重建 | 结果 |
|---|---|---|---|
| `sh000001` | `000001` | `sz000001` | **平安银行**（不是上证指数） |
| `sz399001` | `399001` | `sz399001` | OK |
| `sz399006` | `399006` | `sz399006` | OK |
| `sh000300` | `000300` | `sz000300` | **错** |
| `sh000688` | `000688` | `sz000688` | **错** |

⇒ **3/5 默认指数抓的是错误证券。**

为什么比 missing 严重
----------------------
`sz000001` 是**真实存在的股票**，且旧 `snapshots()` 的过滤条件是
``q.code in wanted_set``，而 ``wanted_set`` 恰好是**裸码** ——
所以错误证券的响应**能通过过滤被收下**。调用方拿到一个"看起来完全正常"
的 Quote，只是它是**另一个证券**。missing 至少是可见的；这个是**静默的**。
"""

from __future__ import annotations

from arad.models import guess_prefix, looks_like_index
from arad.sources import sina

# 复用既有测试已经验证过的 payload 构造器，**不手搓**字段 ——
# 我第一版手写了 6 字段的 payload，`MIN_FIELDS=32` 直接把它们全丢了，
# 于是三条"阳性对照"全部失败（又是"探针没穿过被测路径"）。
# 用真实构造器，失败就一定是产品行为，不是我的 payload 写错。
from test_sources_sina import bulk_body

DEFAULT_INDEX = ["sh000001", "sz399001", "sz399006", "sh000300", "sh000688"]


class _Capture(sina.SinaSource):
    """只替换网络层；normalization + wire 构造全部走真实实现。"""

    def __init__(self, response: str = ""):
        super().__init__()
        self.bulk_chunk = 800
        self.urls: list[str] = []
        self._resp = response

    def _request(self, url: str) -> str:
        self.urls.append(url)
        return self._resp

    def wire_symbols(self) -> list[str]:
        """从捕获的 URL 里抽出实际发出的 wire 符号。"""
        for u in self.urls:
            if "list=" in u:
                return u.split("list=")[1].split("&")[0].split(",")
        return []


# ===========================================================================
# 单元：wire 符号构造
# ===========================================================================

def test_wire_symbol_preserves_explicit_prefix():
    """**核心**：显式前缀必须原样保留到 wire。"""
    for sym in DEFAULT_INDEX:
        got = sina._wire_symbol(sym)
        assert got == sym, (
            f"{sym} 的 wire 符号必须是它自己，实测 {got} —— "
            f"剥前缀再 guess_prefix 会把它改成别的交易所的**真实证券**")


def test_wire_symbol_still_guesses_for_bare_stock():
    """**阳性对照**：裸码个股仍按规则补前缀（既有行为不能坏）。"""
    assert sina._wire_symbol("600000") == "sh600000"
    assert sina._wire_symbol("000001") == "sz000001"   # 平安银行，裸码语义
    assert sina._wire_symbol("300750") == "sz300750"
    assert sina._wire_symbol("830799") == "bj830799"


def test_default_index_codes_would_be_wrong_under_guess_prefix():
    """把**旧机制的错误**钉成文档：证明这不是空想。

    这条直接复现旧路径（剥前缀 -> guess_prefix），确认它真的会改错
    交易所。它绿的**前提**是 `guess_prefix` 保持"只对个股可靠"的语义 ——
    若有人把 `guess_prefix` 改成能猜指数，这条会红，提醒他
    这里曾有一个真实事故。
    """
    wrong = []
    for sym in DEFAULT_INDEX:
        bare = sym[2:]
        rebuilt = f"{guess_prefix(bare)}{bare}"
        if rebuilt != sym:
            wrong.append((sym, rebuilt))
    assert len(wrong) == 3, (
        f"旧机制应当改错 3 个默认指数，实测 {len(wrong)}: {wrong}")
    assert ("sh000001", "sz000001") in wrong
    assert ("sh000300", "sz000300") in wrong
    assert ("sh000688", "sz000688") in wrong


# ===========================================================================
# 行为：真实调用链发出的 wire 符号
# ===========================================================================

def test_snapshots_sends_correct_index_wire_symbols():
    """**端到端 RED 面**：`snapshots()` 实际发出的符号必须是请求的那些。

    这条走**真实** `snapshots()`（只替换 `_request`），
    所以 normalization 与 wire 构造都是生产代码。
    """
    src = _Capture()
    src.snapshots(list(DEFAULT_INDEX))
    got = src.wire_symbols()
    assert got == DEFAULT_INDEX, (
        f"发出的 wire 符号必须等于请求的指数符号。\n"
        f"  期望 {DEFAULT_INDEX}\n  实测 {got}")


def test_wrong_instrument_response_is_rejected():
    """**最严重的一面**：错误证券的响应不得被当成成功结果收下。

    场景：请求 `sh000001`（上证指数），provider 返回 `sz000001`
    （平安银行，真实股票）。旧代码的过滤键是裸码 `000001`，
    裸码相同 → **错误证券被收下**。

    正确行为：这两个是**不同证券**，响应必须被拒（结果是 missing 账）。
    """
    resp = bulk_body(["sz000001"])          # provider 回了别的交易所的证券
    src = _Capture(resp)
    out = src.snapshots(["sh000001"])
    assert not out, (
        f"请求 sh000001（上证指数）时，**不得**把 sz000001（平安银行）"
        f"的响应当成成功结果收下。实测 {[(q.code, q.name) for q in out]}")


def test_correct_index_response_is_accepted():
    """**阳性对照**：请求 `sh000001` 且响应也是 `sh000001` 时必须收下。

    否则上面那条就变成"永远拒绝"的假修复。
    """
    resp = bulk_body(["sh000001"])
    src = _Capture(resp)
    out = src.snapshots(["sh000001"])
    assert len(out) == 1, f"正确证券的响应必须收下，实测 {out}"


def test_stock_path_unchanged():
    """**阳性对照**：个股请求的 wire 符号与收下行为不得改变。"""
    src = _Capture()
    src.snapshots(["600000", "000001", "300750"])
    assert src.wire_symbols() == ["sh600000", "sz000001", "sz300750"], (
        "个股 wire 符号必须与修复前一致")


def test_bare_stock_request_still_accepts_prefixed_response():
    """**边界**：裸码个股请求仍应收下带前缀的响应（既有行为）。

    个股的裸码是无歧义的（`600000` 只能是沪市），所以宽松匹配是对的。
    """
    resp = bulk_body(["sh600000"])
    src = _Capture(resp)
    out = src.snapshots(["600000"])
    assert len(out) == 1, f"裸码个股请求必须收下带前缀响应，实测 {out}"
    assert out[0].name == "股票600000"


# ===========================================================================
# detailed 路径：不得对错误证券产出 "exact but wrong"
# ===========================================================================

def test_detailed_identity_axis_keeps_index_prefix():
    """detailed 路径的请求身份轴必须与 wire 同域（指数带前缀）。"""
    resp = bulk_body(["sh000001"])
    src = _Capture(resp)
    res = src.snapshots_detailed(["sh000001"], route="index")
    # 抓到 1 条，且请求身份是带前缀的指数符号
    assert res.requested == 1, f"requested 应为 1，实测 {res.requested}"
    assert res.returned >= 1, f"returned 应 >=1，实测 {res.returned}"
    assert res.admitted >= 1, f"admitted 应 >=1，实测 {res.admitted}"
    # 三轴一致：不应出现 phantom missing
    assert not res.unknown_missing_keys, (
        f"正确抓到时不该有 missing，实测 {res.unknown_missing_keys}")


def test_detailed_does_not_self_consistently_report_wrong_instrument():
    """**关键**：detailed 不得对**错误证券**自洽地报"精确"。

    旧代码 wire 用裸码猜、R/P/Q 也用裸码，于是即使抓的是
    `sz000001`，三轴也会**机械自洽** —— 产出 "exact but wrong"。
    这条要求：请求 `sh000001` 而响应是 `sz000001` 时，
    **不得**被记成一次成功的精确抓取。
    """
    resp = bulk_body(["sz000001"])          # 错误交易所的证券
    src = _Capture(resp)
    res = src.snapshots_detailed(["sh000001"], route="index")

    assert res.quotes == (), (
        f"错误证券不得出现在结果里，实测 "
        f"{[(q.code, q.name) for q in res.quotes]}")
    assert res.admitted == 0, (
        f"错误证券不得被准入，实测 admitted={res.admitted}")
    # 而且这个缺失必须是**可观测**的（missing 账），不是静默成功
    assert res.unknown_missing_keys or res.raw_keys == (), (
        f"错误证券应当体现为 missing 账，实测 unknown_missing_keys="
        f"{res.unknown_missing_keys} raw_keys={res.raw_keys}")


def test_split_prefix_separates_index_from_stock():
    """`_split_prefix` 是整套修复的地基：它必须把两者分开。

    我原本给这条写了一个 `_identity_of` 辅助函数，但实现完发现
    **生产代码根本不需要它**（账本轴保持裸码、过滤轴用 wire 符号，
    两者分工已经够）。留一个只有测试在用的函数就是本仓库
    bug 类 (c)「数据算了、判决层零读者」—— 所以删掉它，
    改成直接测真正在用的 `_split_prefix`。
    """
    assert sina._split_prefix("sh000001") == ("sh", "000001")
    assert sina._split_prefix("sz000001") == ("sz", "000001")
    assert sina._split_prefix("600000") == ("", "600000")
    assert sina._split_prefix("SH600000") == ("sh", "600000")
    # 关键区分：两者**必须不同**
    assert sina._split_prefix("sh000001") != sina._split_prefix("sz000001")
    assert sina._split_prefix("bad!") is None
