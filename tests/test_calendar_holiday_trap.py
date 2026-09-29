"""`IT-P2-CALENDAR-BARE-CONSTRUCT-SILENT-EMPTY-HOLIDAYS-003`

## 发现经过（一次**被我自己纠正的**结论）

全日仿真 agent 先怀疑 `config/holidays.txt` **缺失**，验证后**撤回**，
结论是"文件存在、正确加载、非缺陷"。

我独立复核，发现**它的撤回本身是错的**，但错的不是文件 —— 是
**构造方式**：

```
TradingCalendar()        -> holidays=0  条  2026-10-01 交易日=True   ❌
TradingCalendar.load()   -> holidays=33 条  2026-10-01 交易日=False  ✅
```

`config/holidays.txt` **确实存在**（48 行、33 个假期日，含 2026-10-01
国庆），agent 对文件的判断是对的。但 **裸构造 `TradingCalendar()`
静默得到空假期表**，于是把国庆当交易日 —— 这是 agent 的探针
`TradingCalendar()` 一调就踩到的坑，而它把结果归因成了"文件没问题，
所以没缺陷"，把**构造方式的坑**漏掉了。

## 严重性：**潜在陷阱，非当下生产缺陷**（如实分级）

生产路径**都用** `load()`：
  * `src/arad/engine.py:1061` — `self.calendar = calendar or TradingCalendar.load()`
  * `src/arad/store.py:51`   — `self.calendar = calendar or TradingCalendar.load()`
  * `src/arad/cli.py:86,112,172,223`、`tools/live_session.py:3789`

而显式 `TradingCalendar(holidays=set())` 的调用点
（`replay.py:572`、`bench_round.py:73`、`probe_session_boundaries.py:91`、
`shot_index.py:49`）都是**刻意**要空假期表的工具/回放场景。

所以：**今天不会错**。但裸构造的**默认值是"没有假期"**，
即"默认不安全" —— 任何新代码 `TradingCalendar()` 都会静默把
国庆/春节当交易日，且**没有任何报错**。这是本仓库反复出现的
**假绿出口**形状（bug 类 d 的近亲）：默认分支悄悄给出错误答案。

## 我做的处置：**钉住陷阱**，不改产品语义

不改 `load()`/裸构造的行为（那会波及 replay/bench 等刻意用法），
而是加测试把**两个构造方式的差别**写成显式合同，任何人改坏
`load()` 或误以为裸构造安全，都会当场转红。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from arad.session import SessionPhase, TradingCalendar

NATIONAL_DAY = datetime(2026, 10, 1, 10, 0)
NORMAL_DAY = datetime(2026, 9, 29, 10, 0)


# ===========================================================================
# 1) load() 必须真的加载假期表（生产路径的行为）
# ===========================================================================

class TestLoadIsAuthoritative:
    def test_load_reads_the_holiday_file(self):
        """**生产路径**：`load()` 必须载入非空假期表。

        引擎（engine.py:1061）与 store（store.py:51）都走 `load()`。
        """
        cal = TradingCalendar.load()
        assert len(cal.holidays) > 0, (
            "load() 必须读进 config/holidays.txt —— "
            "空表意味着所有法定假日都被当交易日")
        assert len(cal.holidays) >= 20, (
            f"一年法定休市日不应只有 {len(cal.holidays)} 个")

    def test_national_day_is_closed(self):
        """国庆必须 CLOSED —— 这是 `load()` 正确与否的直接判据。"""
        cal = TradingCalendar.load()
        assert cal.is_trading_day(NATIONAL_DAY.date()) is False, (
            "2026-10-01 是国庆，必须是休市日")
        assert cal.phase(NATIONAL_DAY) is SessionPhase.CLOSED

    def test_normal_trading_day_is_morning(self):
        """**阳性对照**：普通交易日不得被误判休市（防"全判 CLOSED"）。"""
        cal = TradingCalendar.load()
        assert cal.is_trading_day(NORMAL_DAY.date()) is True
        assert cal.phase(NORMAL_DAY) is SessionPhase.MORNING

    def test_holiday_file_contains_the_documented_blocks(self):
        """假期文件必须覆盖文件头注释里列出的各个法定假期块。"""
        cal = TradingCalendar.load()
        # 文件头承诺：元旦 / 春节 / 清明 / 劳动节 / 端午 / 中秋 / 国庆
        for probe, what in (("2026-01-01", "元旦"),
                            ("2026-02-17", "春节"),
                            ("2026-04-05", "清明"),
                            ("2026-05-01", "劳动节"),
                            ("2026-06-19", "端午"),
                            ("2026-09-25", "中秋"),
                            ("2026-10-01", "国庆")):
            assert probe in cal.holidays, f"{what} {probe} 必须在假期表里"


# ===========================================================================
# 2) **陷阱**：裸构造静默得到空假期表 —— 钉住，不修语义
# ===========================================================================

class TestBareConstructIsADocumentedTrap:
    def test_bare_construct_yields_empty_holidays(self):
        """裸构造 = **空假期表**。这是"默认不安全"，必须被如实记录。"""
        bare = TradingCalendar()
        assert bare.holidays == set(), (
            "裸构造当前就是空表 —— 本测试钉住这个**事实**；"
            "若哪天改成默认 load()，此测试转红，提示需复查全部调用点")

    def test_bare_construct_treats_national_day_as_trading(self):
        """**陷阱本体**：裸构造把国庆当交易日，且**不报错**。

        生产路径不走裸构造，所以今天不出错；但任何新代码
        `TradingCalendar()` 都会静默拿到错误答案。
        """
        bare = TradingCalendar()
        assert bare.is_trading_day(NATIONAL_DAY.date()) is True, (
            "裸构造会把国庆当交易日 —— 这就是要防的静默错误")
        assert bare.phase(NATIONAL_DAY) is not SessionPhase.CLOSED

    def test_bare_and_loaded_calendars_disagree_on_holidays(self):
        """两者**必须**不同 —— 断开就说明有人把陷阱改掉了（需复查）。"""
        bare = TradingCalendar()
        loaded = TradingCalendar.load()
        assert bare.holidays != loaded.holidays, (
            "裸构造与 load() 在假期表上必须不同；"
            "若相同，说明语义变了，需重新审计全部调用点")

    def test_explicit_empty_holidays_is_still_supported(self):
        """**刻意的**空表必须继续可用（replay / bench 依赖它）。"""
        cal = TradingCalendar(holidays=set())
        # 非交易日判定只剩周末
        assert cal.is_trading_day(date(2026, 10, 3)) is False   # 周六
        assert cal.is_trading_day(date(2026, 10, 1)) is True    # 刻意当交易日


# ===========================================================================
# 3) 生产调用点必须用 load()（结构门禁，防以后有人改成裸构造）
# ===========================================================================

class TestProductionCallSitesUseLoad:
    def test_engine_and_store_use_load(self):
        """`engine.py` / `store.py` 必须调 `TradingCalendar.load()`。

        用 AST 找 `TradingCalendar()` 的裸调用 —— 注释/字符串
        无法满足本测试（防子串同义反复）。
        """
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        for rel in ("src/arad/engine.py", "src/arad/store.py"):
            src = (root / rel).read_text(encoding="utf-8")
            tree = ast.parse(src)
            bare = []
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "load"):
                    continue
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "TradingCalendar"):
                    if not node.args and not node.keywords:
                        bare.append(node.lineno)
            assert not bare, (
                f"{rel} 出现裸构造 TradingCalendar()（行 {bare}）——"
                f"会静默得到空假期表，必须用 TradingCalendar.load()")
