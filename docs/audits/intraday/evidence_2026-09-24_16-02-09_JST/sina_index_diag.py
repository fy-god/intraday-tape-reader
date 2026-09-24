"""端到端验证：Sina 指数 wire symbol 是否真的被改成错误交易所。

不复现"我猜的机制"，而是把签名钉进**真实调用链**：
`SinaSource.snapshots(["sh000001", ...])` -> `_norm_codes` -> `_fetch_bulk`
-> `guess_prefix` -> 实际发出的 URL。
"""
import sys
sys.path.insert(0, "src")

from arad.sources import sina

CAPTURED_URLS = []


class CaptureSource(sina.SinaSource):
    """只换网络层，normalization + wire 构造全部保留真实实现。"""

    def _request(self, url: str) -> str:
        CAPTURED_URLS.append(url)
        # 返回一个"里面有个 000001 行"的最小响应，让解析层能跑
        return 'var hq_str_sz000001="平安银行,11.0,10.0,11.5,11.6,10.9";'


DEFAULT_INDEX = ["sh000001", "sz399001", "sz399006", "sh000300", "sh000688"]
STOCKS = ["600000", "000001", "300750"]

print("=" * 74)
print("A. 指数 route：传给 sina 的是带前缀的 index_codes")
print("=" * 74)
s = CaptureSource()
s.bulk_chunk = 800
CAPTURED_URLS.clear()
s.snapshots(list(DEFAULT_INDEX))
for u in CAPTURED_URLS:
    # 抽出 list= 里的符号
    if "list=" in u:
        syms = u.split("list=")[1].split("&")[0]
        print(f"  实际发出的 wire 符号: {syms}")

print()
print("=" * 74)
print("B. 逐个比对：请求 vs 实际")
print("=" * 74)
bad = []
for want in DEFAULT_INDEX:
    got = f"{sina.guess_prefix(want[2:])}{want[2:]}"
    ok = (got == want)
    if not ok:
        bad.append((want, got))
    print(f"  {want:10s} -> {got:12s}  {'OK' if ok else '<<< 抓错证券'}")

print()
print(f"  默认 5 个 index_codes 中，{len(bad)}/5 被改成错误交易所")
for w, g in bad:
    print(f"    {w} -> {g}")

print()
print("=" * 74)
print("C. 个股 route 是否受影响（阳性对照）")
print("=" * 74)
CAPTURED_URLS.clear()
s2 = CaptureSource()
s2.bulk_chunk = 800
s2.snapshots(list(STOCKS))
for u in CAPTURED_URLS:
    if "list=" in u:
        print(f"  个股 wire: {u.split('list=')[1].split('&')[0]}")
print("  期望: sh600000,sz000001,sz300750")

print()
print("=" * 74)
print("D. why this is worse than 'missing'")
print("=" * 74)
print("  sz000001 是真实股票（平安银行）。")
print("  sina.snapshots 的过滤是 `q.code in wanted_set`，")
print("  而 wanted_set 恰好是**裸码** {000001, 399001, ...}。")
print("  => 'sz000001' 解析出的裸码 '000001' **在** wanted_set 里")
print("  => 错误证券的响应被当成合法行情**收下**。")
