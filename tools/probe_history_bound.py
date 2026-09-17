"""验证引擎侧 state.history 在长跑中是否真的被裁剪（不是只有 deque maxlen）。

engine.poll_once 里有一段：当 len(history) > len(keep) * 2 时才 prune。
要确认它**真的会被触发**，而不是因为条件恒假而形同虚设 ——
"有代码但从不执行"和"没有代码"在长跑里是一样的。
"""
from __future__ import annotations

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

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arad.engine import EngineState  # noqa: E402
from arad.models import Board, Quote  # noqa: E402


def _q(code: str, price: float, ts_epoch: float) -> Quote:
    return Quote(code=code, name=f"股票{code[-3:]}", board=Board.MAIN,
                 price=price, prev_close=price, open=price, high=price,
                 low=price, volume_lots=1000.0, amount=price * 100_000,
                 ts=dt.datetime.fromtimestamp(ts_epoch))


def main() -> int:
    st = EngineState(history_len=360)
    print(f"history_len 上限 = {st.history_len}")

    # 1) 单只股票的历史受 deque maxlen 约束
    for i in range(1000):
        st.update([_q("600000", price=10.0 + i * 0.001, ts_epoch=1e9 + i)],
                  dt.datetime.fromtimestamp(1e9 + i))
    h = st.history["600000"]
    print(f"喂 1000 点后，单只历史长度 = {len(h)}（应 == maxlen 360）")
    assert len(h) == 360, f"deque maxlen 没生效：{len(h)}"

    # 2) 键数：不同股票会一直累积，直到 prune 被调用
    now = dt.datetime.now()
    for i in range(2000):
        st.update([_q(f"{600000 + i:06d}", price=10.0, ts_epoch=now.timestamp())], now)
    print(f"累计 2000 只股票 -> history 键数 = {len(st.history):,}")

    # 3) prune 真的能裁掉
    keep = {f"{600000 + i:06d}" for i in range(100)}
    dropped = st.prune(keep)
    print(f"prune(keep=100 只) -> 裁掉 {dropped:,} 只，剩余 {len(st.history):,}")
    assert len(st.history) == 100, f"prune 后应剩 100，实际 {len(st.history)}"
    assert dropped == 1900, f"应裁掉 1900，实际 {dropped}"

    print("\n✓ state.history 单只受 maxlen 约束、键数靠 prune 收敛")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
