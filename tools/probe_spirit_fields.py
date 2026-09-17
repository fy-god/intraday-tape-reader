"""验证腾讯源解析出的盘口/内外盘字段是否真实可用。"""

# Windows 控制台 UTF-8（见 tools/_console.py）。
# 先正常导入；若失败说明本文件是被**按路径**加载的（例如测试用 importlib
# 从 tests/ 里 exec 它），此时 tools/ 不在 sys.path 上——把本文件所在目录
# 补进去再试一次，这样"直接跑"和"被当模块加载"两种场景都能用。
try:
    import _console  # noqa: F401,E402
except ImportError:  # pragma: no cover - 取决于调用方式
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    import _console  # noqa: F401,E402
import sys

sys.path.insert(0, "src")
from arad.sources.tencent import TencentSource

src = TencentSource({})
codes = ["600000", "300750", "688111", "000001", "601127", "600519"]
qs = src.snapshots(codes)

print(f"{'代码':8s} {'现价':>8s} {'涨跌%':>7s} {'外盘':>9s} {'内盘':>9s} {'外/内':>6s} "
      f"{'买5合计(手)':>12s} {'卖5合计(手)':>12s} {'档位':>4s}")
for q in qs:
    print(f"{q.code:8s} {q.price:>8.2f} {q.pct:>+7.2f} {q.outer_vol:>9.0f} {q.inner_vol:>9.0f} "
          f"{q.outer_inner_ratio:>6.2f} {q.bid_total_vol:>12.0f} {q.ask_total_vol:>12.0f} "
          f"{len(q.bid_vols):>4d}")

print()
print("=== 一致性校验 ===")
bad = 0
for q in qs:
    # 1. 五档价必须递减/递增且与买卖一一致
    if q.has_depth and q.bid_vols:
        if q.bid_prices[0] > 0 and q.bid1 > 0 and abs(q.bid_prices[0] - q.bid1) > 0.011:
            print(f"  [X] {q.code} 买一价不一致 {q.bid_prices[0]} vs {q.bid1}"); bad += 1
        if q.ask_prices[0] > 0 and q.ask1 > 0 and abs(q.ask_prices[0] - q.ask1) > 0.011:
            print(f"  [X] {q.code} 卖一价不一致 {q.ask_prices[0]} vs {q.ask1}"); bad += 1
        if q.bid_vols[0] > 0 and abs(q.bid_vols[0] - q.bid_vol) > 1.0:
            print(f"  [X] {q.code} 买一量不一致 {q.bid_vols[0]} vs {q.bid_vol}"); bad += 1
        # 买价低于卖价
        if q.bid_prices[0] > 0 and q.ask_prices[0] > 0 and q.bid_prices[0] >= q.ask_prices[0]:
            print(f"  [X] {q.code} 买一 >= 卖一 ({q.bid_prices[0]} >= {q.ask_prices[0]})"); bad += 1
    # 2. 外盘+内盘 ≈ 总量
    if q.outer_vol > 0 and q.inner_vol > 0:
        drift = abs((q.outer_vol + q.inner_vol) - q.volume_lots) / q.volume_lots
        status = "OK" if drift <= 0.05 else "DRIFT"
        if drift > 0.05:
            bad += 1
        print(f"  {q.code}: 外+内={q.outer_vol+q.inner_vol:.0f} 总量={q.volume_lots:.0f} "
              f"偏差={drift*100:.3f}% {status}")
    else:
        print(f"  {q.code}: 内外盘缺失（可能停牌）")

print()
print("=== 短线精灵阈值实测（80 万股 / 0.1% 换手）===")
for q in qs:
    float_sh = q.float_shares
    print(f"  {q.code} {q.name}: 流通股 {float_sh/1e8:.2f}亿股")
    if q.bid_total_vol > 0:
        wan = q.bid_total_vol * 100 / 1e4
        ratio = (q.bid_total_vol * 100 / float_sh * 100) if float_sh > 0 else 0
        print(f"      买5盘 {wan:.0f}万股 ({ratio:.3f}%流通盘) "
              f"-> 有大买盘{'是' if wan >= 80 or ratio >= 0.8 else '否'}")
    if q.ask_total_vol > 0:
        wan = q.ask_total_vol * 100 / 1e4
        ratio = (q.ask_total_vol * 100 / float_sh * 100) if float_sh > 0 else 0
        print(f"      卖5盘 {wan:.0f}万股 ({ratio:.3f}%流通盘) "
              f"-> 有大卖盘{'是' if wan >= 80 or ratio >= 0.8 else '否'}")

print()
print(f"结论：{'全部通过' if bad == 0 else str(bad) + ' 项异常'}")
