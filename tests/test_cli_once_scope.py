"""`IT-P2-ONCE-SCOPE-MISREPORT-048`

## 缺陷（真实代码测量，2026-09-30）

``python -m arad.cli once`` **显示**"扫描 10 只 / 取到 10 只行情"，
但 ``poll_once`` **实际**扫了全市场 5571 只。

### 机制

``cli.py cmd_once`` 里：

```python
eng = Engine(...)                       # _universe_src 仍是 None
codes = eng._codes or load_watchlist()  # _codes 尚空 -> 10 只自选股
quotes = eng.sources.call("snapshots", codes)   # 为这 10 只白抓一次
...
alerts = eng.poll_once(force=True)      # 内部懒刷新 -> 5571 只
```

实测：

```
cmd_once:123 那一刻   eng._codes = []       -> codes 长度 = 10
poll_once 之后        eng._codes 长度 = 5571
                      quotes 数       = 5571
                      本轮 requested  = 5571
```

**所以覆盖面本来是对的**（走 ``poll_once`` 的全市场口径），
错的只有**显示** —— 以及那 10 只的**多一次无用网络请求**。

### 为什么这是真缺陷（不是"文案小问题"）

用户看到"扫描 10 只"却拿到全市场结论，无法判断
"本轮无告警"到底是"全市场真没异动"还是"只盯了自选股"。
这正是本仓库反复命中的 bug 类 **(e) 证据子集自称全程** ——
观测口径与用户被告知的口径不一致。

### 修法

不再自己抓一遍；直接让 ``poll_once`` 走完整链路（`serve` 用的同一条路径），
再从**观测账本**读真实口径来报告。降级时**显式**告知用户
"本轮无告警不能解释为全市场无异常"。

### 我自己的一个错误（如实记录）

第一版我用 ``obs.get("watchlist_only")`` 判断降级 —— 但
``watchlist_only`` **不是** ``RoundObservationSet`` 的字段，
那个键**永远不存在**，恒得 ``False``：一个**永远不会触发的告警**。
那正是我在批评的 bug 类 (c)「数据算了但没人读」的镜像 ——
**读一个不存在的键**。改用真实的判据：扫描池规模 vs 自选股数量。
"""

from __future__ import annotations

import ast
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

CLI = ROOT / "src" / "arad" / "cli.py"
CAP = ROOT / "src" / "arad" / "capabilities.py"


def _src(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _cmd_once_raw() -> str:
    """``cmd_once`` 的源码段（**含注释**，仅供人读/调试）。"""
    return ast.get_source_segment(_src(CLI), _cmd_once_node()) or ""


def _cmd_once_node() -> ast.FunctionDef:
    tree = ast.parse(_src(CLI))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "cmd_once"),
              None)
    assert fn is not None, "找不到 cmd_once"
    return fn


def _cmd_once_code() -> str:
    """``cmd_once`` 的**代码**（**剥掉注释与 docstring**）。

    ⚠ 为什么必须剥：我第一版直接用 ``ast.get_source_segment``，结果
    **自己的解释性注释**被断言命中 —— 注释里写了 "`watchlist_only`
    不是账本字段"，于是 ``"watchlist_only" not in seg`` 失败。
    这正是本仓库反复出现的**子串同义反复**：门禁用子串匹配时，
    一句注释就能把门禁满足（或反过来，把正确的代码判红）。
    剥注释后，断言只看**真的会执行的代码**。
    """
    import io
    import tokenize

    node = _cmd_once_node()
    src = _src(CLI)
    seg = ast.get_source_segment(src, node) or ""
    # 用 tokenize 去掉 COMMENT 与字符串以外的噪音；docstring 单独摘掉
    out: list[str] = []
    toks = tokenize.generate_tokens(io.StringIO(seg).readline)
    for tok in toks:
        if tok.type == tokenize.COMMENT:
            continue
        out.append(tok.string)
    code = " ".join(out)
    # docstring 会被当普通 STRING token 留下 —— 去掉函数首行 docstring
    doc = ast.get_docstring(node) or ""
    if doc:
        code = code.replace(doc, "")
    return code


# ===========================================================================
# 1. 不得再"先取 codes 再抓一遍"（那会显示错误口径 + 白抓一次）
# ===========================================================================

class TestOnceDoesNotPrefetchStaleCodes:
    def test_no_manual_snapshot_call_before_poll(self):
        """``cmd_once`` 不得在 ``poll_once`` **之前**手工抓快照。

        那正是"显示 10 只"的来源：手工抓的是 ``_codes or watchlist``，
        而 ``_codes`` 此刻还是空。
        """
        seg = _cmd_once_code()
        assert 'eng.sources.call("snapshots"' not in seg, (
            "cmd_once 仍在 poll_once 之前手工抓快照 —— "
            "会得到 _codes 尚空时的错误口径（10 只自选股）")

    def test_poll_once_is_the_scan_driver(self):
        """扫描必须由 ``poll_once`` 驱动（与 serve 同一条链路）。

        用 **AST** 找 ``poll_once(...)`` 调用节点，不用子串 ——
        ``tokenize`` 会把 ``poll_once(force=True)`` 拆成多个 token，
        子串匹配在这里不可靠。
        """
        tree = ast.parse(_src(CLI))
        fn = _cmd_once_node()
        calls = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "poll_once"
        ]
        assert calls, "cmd_once 必须用 poll_once 驱动扫描"
        assert any(
            any(kw.arg == "force" for kw in c.keywords) for c in calls), (
            "poll_once 必须带 force=True（非交易时段也要能跑一次）")

    def test_no_stale_codes_pattern(self):
        """不得再出现 ``eng._codes or load_watchlist()`` 这种取值。"""
        seg = _cmd_once_code()
        assert "_codes or load_watchlist()" not in seg, (
            "`_codes or load_watchlist()` 在刷新前取值为自选股，"
            "会造成口径误报")


# ===========================================================================
# 2. 报告口径必须来自观测账本
# ===========================================================================

class TestReportedScopeComesFromLedger:
    def test_reads_requested_from_observation(self):
        """报告的数量必须读 ``observation`` 的 ``requested``。"""
        seg = _cmd_once_code()
        assert "observation" in seg, "必须从观测账本读真实口径"
        assert '"requested"' in seg or "'requested'" in seg, (
            "必须读 requested 字段")

    def test_does_not_report_len_of_prefetched_quotes(self):
        """不得再报"我们手工抓了多少只"（那不是扫描口径）。"""
        seg = _cmd_once_code()
        assert "len(quotes)" not in seg, (
            "len(quotes) 是手工预抓的数量，不是真实扫描口径")


# ===========================================================================
# 3. 降级必须显式告知（bug 类 e：证据子集自称全程）
# ===========================================================================

class TestDegradationIsDisclosed:
    def test_watchlist_fallback_is_announced(self):
        """扫描池 ≈ 自选股数量时必须显式提示用户。"""
        seg = _cmd_once_code()
        assert "自选股" in seg, (
            "降级为自选股时必须显式告知 —— 否则用户会把"
            "『无告警』误读为『全市场无异常』")
        assert "不能" in seg, (
            "提示必须说明『不能解释为全市场无异常』")

    def test_does_not_read_nonexistent_watchlist_only_key(self):
        """**我自己的错误**：不得读 ``watchlist_only`` —— 它不是账本字段。

        钉住这条，防止有人"顺手"改回去。判据用 AST 找字符串常量。
        """
        seg = _cmd_once_code()
        assert "watchlist_only" not in seg, (
            "`watchlist_only` 不是 RoundObservationSet 的字段，"
            "读它恒得 False —— 那是一个永远不会触发的告警")

    def test_ledger_really_lacks_that_field(self):
        """阳性对照：核实账本**确实**没有该字段（证明上一条不是空断言）。"""
        import dataclasses

        from arad.capabilities import RoundObservationSet

        names = {f.name for f in dataclasses.fields(RoundObservationSet)}
        assert "watchlist_only" not in names, (
            "账本若有该字段，则上一条断言的前提不成立，需重新评估")
        assert "requested" in names, "账本必须有 requested"
