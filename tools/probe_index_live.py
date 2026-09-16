"""验证指数前缀链路：同一个 000001，带前缀是上证指数，裸码是平安银行。

跑法： ``python tools\\probe_index_live.py``
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arad.models import Board, looks_like_index   # noqa: E402
from arad.sources.tencent import TencentSource    # noqa: E402

REQUEST = ["sh000001", "sz399001", "sz399006", "sh000300", "sh000688", "000001"]

src = TencentSource({})
print(f"请求：{REQUEST}\n")
try:
    quotes = src.snapshots(REQUEST)
except Exception as exc:  # noqa: BLE001 - 探测脚本，网络失败直接打印
    print(f"抓取失败（网络问题，非代码问题）：{type(exc).__name__}: {exc}")
    raise SystemExit(1)

by_code = {q.code: q for q in quotes}
print(f"{'code':<12}{'name':<14}{'price':>12}{'prev':>12}{'board':<8}{'looks_idx'}")
print("-" * 70)
for q in quotes:
    print(
        f"{q.code:<12}{q.name:<14}{q.price:>12.2f}{q.prev_close:>12.2f}"
        f"{q.board.value:<8}{looks_like_index(q.code, q.name)}"
    )

print()
idx = [q for q in quotes if q.board is Board.INDEX]
print(f"识别为指数的：{len(idx)} / {len(quotes)}")
for q in idx:
    print(f"  ✓ {q.code} {q.name} {q.price}")

# 核心断言：sh000001 与 000001 必须同时存在且是**两个不同**标的。
sh = by_code.get("sh000001")
sz = by_code.get("000001")
if sh and sz:
    same = abs(sh.price - sz.price) < 1e-9
    print(f"\nsh000001 = {sh.name} {sh.price}   (board={sh.board.value})")
    print(f"000001  = {sz.name} {sz.price}   (board={sz.board.value})")
    print("✗ 两者价格相同，前缀没有生效！" if same else "✓ 前缀生效，000001 的歧义已解开")
else:
    print(f"\n✗ 缺少标的: sh000001={sh is not None} 000001={sz is not None}")
