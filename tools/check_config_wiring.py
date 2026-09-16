"""核对三个 spirit 模块与配置/引擎的接线是否完整。

检查项：
1. ``config/settings.yaml`` 里三个 spirit 节都存在且默认关闭；
2. 引擎能按配置装配出全部 7 个规则模块；
3. 每个模块的 ``DEFAULTS`` 键都在 YAML 里出现（漏一个就只能靠代码默认值，
   运维改配置时会以为改生效了其实没有）；
4. ``max_per_round: 0``（不限量）这类"假值"不会被 ``or N`` 悄悄换成默认值。

跑法：``python tools/check_config_wiring.py``
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arad.config import load_settings                       # noqa: E402
from arad.engine import RULE_MODULES, build_rules            # noqa: E402

SPIRIT = ("spirit_price", "spirit_order", "spirit_index")
ok = True

print(f"引擎声明的规则模块（{len(RULE_MODULES)} 个）：{RULE_MODULES}\n")

st = load_settings()

# --- 1) YAML 节存在性 ---
print("--- 1) 配置节 ---")
for name in SPIRIT:
    sec = st.section("rules").get(name)
    if sec is None:
        print(f"  ✗ {name}: 配置节缺失（模块 DEFAULTS 拦不住 rule_cfg 的 setdefault(True)）")
        ok = False
    elif sec.get("enabled") is not False:
        print(f"  ⚠ {name}: enabled={sec.get('enabled')!r}（期望 False，默认关闭）")
    else:
        print(f"  ✓ {name}: 存在，enabled=False，{len(sec)} 个键")

# --- 2) 默认配置下装配出的规则 ---
print("\n--- 2) 默认配置装配 ---")
rules = build_rules(st)
names = sorted(getattr(r, "name", type(r).__name__) for r in rules)
print(f"  默认启用 {len(rules)} 个: {names}")
for name in SPIRIT:
    if name in names:
        print(f"  ⚠ {name} 默认就启用了，与 DEFAULTS['enabled']=False 的意图不符")
        ok = False
    else:
        print(f"  ✓ {name} 默认未启用")

# --- 3) DEFAULTS 键是否都写进了 YAML ---
print("\n--- 3) DEFAULTS vs settings.yaml ---")
import importlib  # noqa: E402

for name in SPIRIT:
    mod = importlib.import_module(f"arad.rules.{name}")
    defaults = set(getattr(mod, "DEFAULTS", {}) or {})
    sec = st.section("rules").get(name) or {}
    missing = sorted(defaults - set(sec))
    extra = sorted(set(sec) - defaults)
    if missing:
        print(f"  ✗ {name}: YAML 缺少 {missing}（改配置不生效）")
        ok = False
    else:
        print(f"  ✓ {name}: {len(defaults)} 个默认键全部在 YAML 中")
    if extra:
        print(f"      （YAML 多出：{extra}）")

# --- 4) max_per_round=0 语义 ---
print("\n--- 4) max_per_round=0 是否真的不限量 ---")
for name in SPIRIT:
    mod = importlib.import_module(f"arad.rules.{name}")
    d = dict(getattr(mod, "DEFAULTS", {}) or {})
    d["enabled"] = True
    d["max_per_round"] = 0
    rule = mod.build(d)

    # 两种风格都要认：有的模块把 max_per_round 存成属性（构造时读一次），
    # 有的模块每轮从 ctx.cfg 现取（如 spirit_order）。只查属性会误报。
    stored = getattr(rule, "max_per_round", None)
    if stored is not None:
        if stored == 0:
            print(f"  ✓ {name}: max_per_round=0 保持 0（属性，不限量）")
        else:
            print(f"  ✗ {name}: 显式 0 被改成了 {stored!r} —— 又是 `or` 惯用法")
            ok = False
        continue

    # 每轮现取的模块：直接问它的取值函数
    getter = getattr(rule, "_get", None)
    if callable(getter):
        class _Ctx:
            cfg = {"max_per_round": 0}

        got = getter(_Ctx(), "max_per_round", 20)
        if got == 0:
            print(f"  ✓ {name}: max_per_round=0 原样取出（每轮现取，不限量）")
        else:
            print(f"  ✗ {name}: 显式 0 取出来是 {got!r}")
            ok = False
    else:
        print(f"  ? {name}: 既无属性也无 _get，无法自动核对（请人工确认）")

print("\n" + ("✓ 接线完整" if ok else "✗ 存在问题（见上）"))
raise SystemExit(0 if ok else 1)
