"""看短线精灵播报效果：真实回放数据 + 已启用的 spirit_price / spirit_order 模块。

用法：
    python tools/shot_spirit.py            # 默认 45 分钟
    python tools/shot_spirit.py 60         # 60 分钟
"""
import io
import sys
from collections import Counter

sys.path.insert(0, "src")
sys.path.insert(0, "tests")

import arad.notifiers.console as C
from arad.config import load_settings
from arad.notifiers.console import ConsoleNotifier
from arad.replay import Replay

C._VT_ENABLED = True          # 强制开色，便于看到真实观感

minutes = int(sys.argv[1]) if len(sys.argv) > 1 else 45

# 打开短线精灵的两个离线可验证模块
# （spirit_index 需要 poll.index_codes 指数行情管道，回放剧本里没有指数）
st = load_settings()
for name in ("spirit_price", "spirit_order"):
    st.section("rules").setdefault(name, {})["enabled"] = True

res = Replay(seed=7, minutes=minutes, settings=st).run()

print(f"（回放 seed=7 {minutes} 分钟，共 {res.total} 条告警）")
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
