"""`IT-P2-TENCENT-IDX-SET-FROM-PREFIX-INFERENCE-002`

## ⚠ 一次**诚实降级**：回滚牙齿逼出来的结论

我最初的判断：腾讯的
``idx_set = {sym for sym, explicit in norm if explicit}``
把"调用方**写了**前缀"当成"它**是指数**"，是 bug 类 (g) 的温床，
应改成 caller-owned role。

**回滚牙齿证明这个"修复"对行为几乎无影响。**
`is_index_role`（`tencent.py:323-324`）里 `idx_set` 只是**预过滤**，
成员**还要过** `looks_like_index(sym, "", bare)`。实测：

```
symbol       looks_like_index   在 idx_set 里 -> index_role
sh000001     True               True
sh000905     True               True
sz399001     True               True
sh600000     False              False    <- 被兜底判据否掉，不靠 idx_set
sz000001     False              False
sh000922     False              False    <- 只能靠 route(index_role=True)
```

**结论（如实记录）：**

* 对**能靠代码段/名称判定的**指数（5 个默认指数都在内），
  `idx_set` 收不收它们**不改变结果**；
* 对**判据否掉的**指数（如 `sh000922` 中证红利），
  `idx_set` **也救不了**，唯一出路是 `index_role=True`（route 轴）；
* 而 `snapshots()` 的调用方是引擎、引擎本就在**指数路由**上，
  `index_role=True` **已经覆盖**。

**所以 `index_codes` 是"防御性结构对齐"，不是行为修复。**
它的价值是让腾讯与新浪**共用同一个 caller-owned 角色接口**，
消除"角色从字符串拼法推断"这个**结构**隐患（迟早会在某次重构里
真的咬人），而**不是**修一个当下可复现的错误。

本文件因此分两层，并**明确标注**哪层有牙齿：
1. **行为不变量**（真正有牙齿）：裸写 000001 是平安银行、
   带前缀个股不是指数、5 个默认指数全保留前缀、deterministic、
   detailed 身份不变量。
2. **结构对齐**（无行为牙齿）：`index_codes` 接口存在、引擎真的送达、
   `_role_set` 优先用声明。标注为结构，不冒充行为修复。

第 4 节专门钉住**本修复的边界**，防止以后有人误以为
`index_codes` 修好了 `sh000922` 那一类符号。
"""

from __future__ import annotations

from test_sources_tencent import make_line

from arad.models import Board
from arad.sources.tencent import TencentSource, is_index_role, looks_like_index

DEFAULT_INDEX = ["sh000001", "sz399001", "sz399006", "sh000300", "sh000688"]


class _Cap(TencentSource):
    def __init__(self, body: str, index_codes=None):
        super().__init__()
        self._body = body
        self.chunk = 800
        if index_codes is not None:
            self.index_codes = set(index_codes)

    def _request(self, url: str) -> str:
        return self._body


def _body(syms) -> str:
    return "\n".join(
        make_line(code=s[2:], prefix=s[:2], n=54,
                  fields={3: "3900.00", 4: "3890.00"}) for s in syms)


# ===========================================================================
# 1) 行为不变量 —— 真正有牙齿的部分
# ===========================================================================

class TestBehaviorInvariants:
    def test_bare_000001_is_pingan_stock_not_index(self):
        """**阳性对照**：裸写 `000001` 必须是平安银行（个股）。"""
        src = _Cap(_body(["sz000001"]))
        qs = src.snapshots(["000001"])
        assert len(qs) == 1
        assert qs[0].code == "000001", (
            f"裸写 000001 必须是个股裸码，实测 {qs[0].code!r}")
        assert qs[0].board is not Board.INDEX, (
            "裸写 000001 不能因为它在 INDEX_CODES 里就被标成上证指数")

    def test_bare_000001_not_index_even_with_other_declarations(self):
        src = _Cap(_body(["sz000001"]), index_codes={"sh000001", "sz399001"})
        qs = src.snapshots(["000001"])
        assert qs[0].code == "000001"
        assert qs[0].board is not Board.INDEX

    def test_prefixed_stock_is_not_index(self):
        """**v1 病根的直接否定**：`sh600000`（浦发银行）带前缀但非指数。"""
        src = _Cap(_body(["sh600000"]))
        qs = src.snapshots(["sh600000"])
        assert len(qs) == 1
        assert qs[0].code == "600000", (
            f"浦发银行必须被判个股（裸码），实测 {qs[0].code!r}")
        assert qs[0].board is not Board.INDEX

    def test_prefixed_stock_not_index_even_when_declared(self):
        src = _Cap(_body(["sh600000"]), index_codes={"sh000001"})
        qs = src.snapshots(["sh600000"])
        assert qs[0].code == "600000"
        assert qs[0].board is not Board.INDEX

    def test_default_index_codes_all_prefixed(self):
        src = _Cap(_body(DEFAULT_INDEX), index_codes=set(DEFAULT_INDEX))
        qs = src.snapshots(list(DEFAULT_INDEX))
        got = sorted(q.code for q in qs)
        assert got == sorted(DEFAULT_INDEX), (
            f"5 个默认指数必须全部保留前缀。\n  期望 {sorted(DEFAULT_INDEX)}\n"
            f"  实测 {got}")
        assert not [q for q in qs if q.board is not Board.INDEX]

    def test_snapshots_is_deterministic(self):
        src = _Cap(_body(DEFAULT_INDEX), index_codes=set(DEFAULT_INDEX))
        a = sorted(q.code for q in src.snapshots(list(DEFAULT_INDEX)))
        b = sorted(q.code for q in src.snapshots(list(DEFAULT_INDEX)))
        assert a == b

    def test_detailed_identity_holds_for_default_indices(self):
        src = _Cap(_body(DEFAULT_INDEX), index_codes=set(DEFAULT_INDEX))
        res = src.snapshots_detailed(list(DEFAULT_INDEX), route="index")
        assert sorted(res.requested_keys) == sorted(DEFAULT_INDEX)
        assert sorted(res.admitted_keys) == sorted(DEFAULT_INDEX)
        assert res.identity_holds(), "身份不变量必须成立"
        assert not res.unknown_missing_keys

    def test_detailed_stock_route_unaffected(self):
        src = _Cap(_body(["sh600000"]), index_codes=set(DEFAULT_INDEX))
        res = src.snapshots_detailed(["600000"], route="stocks")
        assert set(res.requested_keys) == {"600000"}
        assert res.quotes and res.quotes[0].code == "600000"


# ===========================================================================
# 2) 判据边界 —— 钉住"idx_set 只是预过滤"这个**事实**
# ===========================================================================

class TestJudgeBoundary:
    def test_idx_set_membership_alone_cannot_make_a_stock_an_index(self):
        """**关键**：`idx_set` 成员**仍须**过 `looks_like_index`。

        这条钉住"为什么 `index_codes` 是**结构**对齐而非行为修复"：
        `sh600000` 即使在 `idx_set` 里也**不会**变成指数。
        """
        assert is_index_role("sh600000", index_role=False,
                             idx_set={"sh600000"}) is False, (
            "idx_set 只是预过滤，浦发银行仍必须被判个股")

    def test_bare_000001_in_idx_set_still_not_index(self):
        assert is_index_role("sz000001", index_role=False,
                             idx_set={"sz000001"}) is False

    def test_index_role_route_is_authoritative(self):
        """**route 轴才是权威** —— 覆盖 `idx_set` 救不了的符号。"""
        assert looks_like_index("sh000922", "", "000922") is False, (
            "前提：该符号名称盲判据为 False")
        assert is_index_role("sh000922", index_role=True) is True, (
            "index 路由上请求 -> 权威判指数")
        assert is_index_role("sh000922", index_role=False,
                             idx_set={"sh000922"}) is False, (
            "而 idx_set 路径救不了它")

    def test_declared_default_indices_pass_judge(self):
        """声明为指数的默认指数确实过判据（否则接口无意义）。"""
        for s in DEFAULT_INDEX:
            assert is_index_role(s, index_role=False, idx_set={s}) is True, s


# ===========================================================================
# 3) 接口合同 + 引擎真的送达（防零读者字段 = bug 类 c）
# ===========================================================================

class TestCallerOwnedRolePlumbing:
    def test_source_exposes_index_codes_attribute(self):
        assert hasattr(TencentSource, "index_codes"), (
            "腾讯必须暴露 caller-owned `index_codes`，"
            "否则引擎 `_publish_index_role` 会静默跳过")

    def test_index_codes_defaults_to_empty_set(self):
        assert TencentSource().index_codes == set(), (
            "类级默认必须为空 —— 不得默认把符号当指数")

    def test_role_set_reads_declaration_not_only_prefix(self):
        """**结构**：`_role_set` 真的读 `index_codes`。"""
        src = _Cap("", index_codes={"sh000001"})
        norm = [("sh000001", False)]        # 没写前缀
        src.index_codes = set()
        assert src._role_set(norm) == set(), "无声明 + 无前缀 -> 空集"
        src.index_codes = {"sh000001"}
        assert src._role_set(norm) == {"sh000001"}, (
            "有声明 -> 收进来（即使请求侧没写前缀）")

    def test_role_set_falls_back_to_prefix_semantics(self):
        """既有语义必须保留：写了前缀仍进集合（自选股兼容）。"""
        src = _Cap("")
        src.index_codes = set()
        norm = [("sh000001", True), ("000001", False)]
        role = src._role_set(norm)
        assert "sh000001" in role
        assert "000001" not in role, (
            "裸写 000001 不得进集合 —— 用户要的是平安银行")

    def test_engine_publishes_role_to_every_source_in_chain(self):
        """**端到端**：`_publish_index_role` 必须送达链上每个源。"""
        from arad.engine import Engine, SourceManager
        from arad.sources.sina import SinaSource

        ten, sin = TencentSource(), SinaSource()
        eng = Engine.__new__(Engine)
        eng.sources = SourceManager([ten, sin], threshold=3)
        eng.index_codes = ["sh000001", "sz399001"]
        eng._publish_index_role(eng.index_codes)

        want = {"sh000001", "sz399001"}
        assert ten.index_codes == want, f"腾讯未收到角色: {ten.index_codes}"
        assert sin.index_codes == want, f"新浪未收到角色: {sin.index_codes}"

    def test_publish_role_tolerates_sources_without_attribute(self):
        """不持有 `index_codes` 的源必须被静默跳过，不抛错。"""
        from arad.engine import Engine, SourceManager

        class Bare:
            name = "bare"

            def snapshots(self, codes):
                return []

            def health(self):
                return {"name": self.name, "ok": True, "latency_ms": 0,
                        "err": ""}

        eng = Engine.__new__(Engine)
        eng.sources = SourceManager([Bare()], threshold=3)
        eng.index_codes = ["sh000001"]
        eng._publish_index_role(eng.index_codes)      # 不得抛异常

    def test_publish_role_is_idempotent(self):
        from arad.engine import Engine, SourceManager

        ten = TencentSource()
        eng = Engine.__new__(Engine)
        eng.sources = SourceManager([ten], threshold=3)
        for _ in range(3):
            eng._publish_index_role(["sh000001"])
        assert ten.index_codes == {"sh000001"}


# ===========================================================================
# 4) **诚实标注本修复的边界** —— 防后人误以为它修好了 sh000922
# ===========================================================================

class TestHonestScope:
    def test_declaration_does_not_rescue_name_only_index(self):
        """如实钉住**边界**：`sh000922` 靠声明也救不了，只能靠 route。

        这条测试存在的意义是防止以后有人**误以为**
        `index_codes` 修好了这一类符号。
        """
        assert is_index_role("sh000922", index_role=False,
                             idx_set={"sh000922"}) is False, (
            "sh000922 名称盲判据为 False，声明救不了 —— 唯一出路是 route")
        assert is_index_role("sh000922", index_role=True) is True
