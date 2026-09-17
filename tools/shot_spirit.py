"""看短线精灵播报效果：真实回放数据 + 已启用的 spirit_price / spirit_order 模块。

用法：
    python tools/shot_spirit.py            # 默认 45 分钟
    python tools/shot_spirit.py 60         # 60 分钟
    python tools/shot_spirit.py --minutes 60 --seed 7

这是**离线**的：用回放引擎合成行情，不需要开盘、不需要联网，随时可跑。
"""
from __future__ import annotations

import argparse
import io
import sys
from collections import Counter

sys.path.insert(0, "src")
sys.path.insert(0, "tests")

import arad.notifiers.console as C
from arad.config import load_settings
from arad.notifiers.console import ConsoleNotifier
from arad.replay import Replay


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="回放一遍交易日，看短线精灵的播报效果（离线）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例：python tools/shot_spirit.py 60   # 回放 60 分钟")
    # 位置参数保留，历史上就是 `python tools/shot_spirit.py 60` 这种用法
    ap.add_argument("minutes_pos", nargs="?", type=int, default=None,
                    metavar="MINUTES", help="回放分钟数（默认 45）")
    ap.add_argument("--minutes", type=int, default=None, help="回放分钟数（默认 45）")
    ap.add_argument("--seed", type=int, default=7, help="随机种子（默认 7）")
    args = ap.parse_args(argv)

    minutes = args.minutes if args.minutes is not None else (
        args.minutes_pos if args.minutes_pos is not None else 45)
    if minutes <= 0:
        ap.error("回放分钟数必须为正整数")

    C._VT_ENABLED = True          # 强制开色，便于看到真实观感

    # 打开短线精灵的两个离线可验证模块
    # （spirit_index 需要 poll.index_codes 指数行情管道，回放剧本里没有指数）
    st = load_settings()
    for name in ("spirit_price", "spirit_order"):
        st.section("rules").setdefault(name, {})["enabled"] = True

    res = Replay(seed=args.seed, minutes=minutes, settings=st).run()

    print(f"（回放 seed={args.seed} {minutes} 分钟，共 {res.total} 条告警）")
    print("按类型：", dict(Counter(a.kind.value for a in res.alerts)))
    spirit = Counter((a.metrics or {}).get("pattern") for a in res.alerts)
    spirit.pop(None, None)
    print("精灵信号：", dict(spirit))

    buf = io.StringIO()
    n = ConsoleNotifier(color=True, style="spirit", show_header=True, align_name=10,
                        stream=buf, enabled=True)
    n._vt = True
    for a in res.alerts:
        n.send(a)
    sys.stdout.write(buf.getvalue())
    print(f"... 共 {res.total} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
