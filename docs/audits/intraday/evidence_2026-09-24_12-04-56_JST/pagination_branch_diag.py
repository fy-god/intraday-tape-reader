"""诊断：我的 WP06 测试真的走进了 no-total 分支吗？

D15 教训：**探针必须真的穿过被测分支**，否则它只是在测别的东西。
上一轮我就在这上面栽过（monkeypatch 变成 vacuous）。

这里直接把 universe() 的关键决策点打出来。
"""
import sys
sys.path.insert(0, "src")

from arad.sources.eastmoney import EastmoneySource, parse_clist_page

REQ = []


def payload(rows, total=None):
    d = {"diff": rows}
    if total is not None:
        d["total"] = total
    return {"data": d, "rc": 0, "rt": 1}


SUSP = {"f12": "600001", "f14": "停牌股", "f2": "-", "f18": "10.0"}
LIVE = {"f12": "600002", "f14": "正常股", "f2": "10.5", "f18": "10.0"}
PAGES = [payload([SUSP]), payload([LIVE]), payload([])]


class Stub(EastmoneySource):
    def __init__(self):
        super().__init__()
        self.max_pages = 10
        self.page_size = 1

    def _request_json(self, url, seq=0):
        pn = len(REQ) + 1
        REQ.append(pn)
        page = PAGES[pn - 1] if pn <= len(PAGES) else payload([])
        parsed = parse_clist_page(page, seq)
        print(f"    p{pn}: raw_code_rows={parsed.raw_code_rows} "
              f"quotes={len(parsed.quotes)} total={parsed.total}")
        return page


s = Stub()
q = s.universe()
print(f"\nrequested pages = {REQ}")
print(f"codes = {sorted(x.code for x in q)}")
print()

# 关键：first.total 是 0 吗？-> 决定走哪个分支
first = parse_clist_page(PAGES[0], 0)
print(f"first.total = {first.total}  -> "
      f"{'no-total 分支（顺序探测）' if first.total <= 0 else '有 total 分支'}")

# 直接看源码里那个 break 条件是否可达
import inspect
src = inspect.getsource(EastmoneySource.universe)
i = src.find("for pn in range(2")
print("\n--- 顺序探测分支源码 ---")
print(src[i:i + 420])
