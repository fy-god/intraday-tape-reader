"""三个 spirit 规则模块 → spirit 展示层的映射完整性检查。

这个脚本回答一个问题：**规则实际产出的 metrics["pattern"]，展示层是否都认识？**
认不出的会被降级成 kind 兜底文案（例如把"机构吃货"显示成"异动"），
这正是本项目踩过的坑（触板被显示成涨停）。所以这里要穷举核对。
"""
import sys
sys.path.insert(0, 'src')

from arad.spirit import KIND_FALLBACK, SIGNALS, signal_of
from arad.models import Alert, AlertKind
from datetime import datetime

# 1) 三个模块声明的信号
mods = {}
for name in ("spirit_price", "spirit_order", "spirit_index"):
    mod = __import__(f"arad.rules.{name}", fromlist=["*"])
    mods[name] = (getattr(mod, "PATTERNS", None) or getattr(mod, "SIGNALS", None)
                  or tuple(getattr(mod, "PATTERN_CN", {}) or {}))
    print(f"{name:14s} {len(mods[name]):2d} 个: {tuple(mods[name])}")

print()
print("--- 展示层是否认识每个信号 ---")
missing = []
for mod, pats in mods.items():
    for p in pats:
        sig = SIGNALS.get(p)
        if sig is None:
            missing.append((mod, p))
            print(f"  ✗ {mod:14s} {p:20s} 展示层无此信号 -> 会降级为 {KIND_FALLBACK}")
        else:
            print(f"  ✓ {mod:14s} {p:20s} -> {sig.cn:6s} dir={sig.direction:7s} group={sig.group}")
print()
print("缺失:", missing or "无")

# 2) 用假 Alert 走一遍 signal_of，确认能取到 pattern
print()
print("--- signal_of 取值链路 ---")
for p in ("rocket", "institution_eat", "index_pull", "limit_up_seal",
          "open_limit_up", "big_bid_wall"):
    a = Alert(key="k", kind=AlertKind.UNUSUAL, code="600000", name="测试",
              ts=datetime.now(), price=10.0, pct=1.0, title="t", detail="d",
              metrics={"pattern": p})
    got = signal_of(a)
    sig = SIGNALS.get(got)
    print(f"  metrics['pattern']={p:18s} -> {got:18s} {sig.cn if sig else '(未识别)'}")
