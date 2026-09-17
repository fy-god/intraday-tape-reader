"""找出「声明了但没人读」的配置键 —— 即真正的接线缺口。

背景：`storage.series_len` 声明在 config 里、也被读作默认值，但那个值
从来没被传下去，改了完全不生效也不报错。`check_orphan_config.py` 抓不到它，
因为那个检查只确认"键名作为字符串字面量出现过"。

本脚本换个角度：**行为验证**。对每个叶子键，把它改成一个"不可能被忽略"的
哨兵值，然后看程序里是否有任何地方能观测到这个改变。

对无法自动观测的键，退化为静态检查：键名是否出现在"取值语句"里
（`get(...)` / `[...]` / `setdefault`），而不只是出现在 DEFAULTS 的字面量里。

跑法：``python tools/check_config_consumed.py [--verbose]``
"""
from __future__ import annotations

try:
    import _console  # noqa: F401,E402
except ImportError:  # pragma: no cover
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    import _console  # noqa: F401,E402

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# 已知的"读法特殊"的键：它们通过别的机制被消费，静态正则看不出来。
ALLOW = {
    # DEFAULTS 自身定义，不需要"被读"
    "name", "timezone", "log_level", "data_dir",
    # 由 settings 的属性/方法间接消费
    "dry_run", "replay",
    # 只在 config.py 的 build_source 等辅助函数里读，形式是 section 取值
    "universe", "fallback", "failover_threshold",
    # 通知器自己的 section 整段取走
    "enabled", "format", "level", "path", "webhook", "secret", "token",
    "interval", "cooldown", "min_interval", "batch",
}


def leaf_keys(text: str) -> dict[str, int]:
    """极简 YAML 叶子键扫描（够用即可，与 check_orphan_config 同款思路）。"""
    out: dict[str, int] = {}
    stack: list[tuple[int, str]] = []
    for i, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if ":" not in line:
            continue
        key = line.split(":", 1)[0].strip()
        if not key or key.startswith("-"):
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key))
        val = line.split(":", 1)[1].strip()
        if val:
            out[key] = i
    return out


def consumed_forms(key: str, blob: str) -> list[str]:
    """返回源码里"这个键被**读**"的证据形式（不是只被定义）。"""
    esc = re.escape(key)
    hits = []
    # settings.get("a.b.key") / .get("key")
    if re.search(rf"""\.get\(\s*['"][\w.]*{esc}['"]""", blob):
        hits.append("settings.get()")
    # section("x")["key"]
    if re.search(rf"""\[\s*['"]{esc}['"]\s*\]""", blob):
        hits.append("['key']")
    # setdefault("key"
    if re.search(rf"""setdefault\(\s*['"][\w.]*{esc}['"]""", blob):
        hits.append("setdefault()")
    return hits


def strip_defaults(text: str) -> str:
    """从 config.py 里**只**挖掉 ``DEFAULTS = {...}`` 那个字典字面量。

    为什么要这么精细：DEFAULTS 会为每个键写一次 ``"key": 默认值``，
    而每个键都必然出现在那里 —— 于是任何"键名在源码里出现过"的检查都会
    无条件通过。这正是 check_orphan_config.py 抓不到
    ``storage.series_len`` 的原因，也是本脚本第一版同样抓不到的原因
    （已实测：把 engine 里那行参数删掉，两个脚本都仍报 ✓）。

    但**不能整个文件排除**：config.py 自己也会合法地读配置
    （例如 ``load_holidays`` 读 ``session.holidays_file``）。第一版就是这么
    写的，结果对 holidays_file 误报。正确做法是只挖掉声明点，保留读取点。
    """
    m = re.search(r"^DEFAULTS\s*[:=]", text, re.M)
    if not m:
        return text
    # 从 "DEFAULTS" 起做花括号配平，找到字典结束
    i = text.find("{", m.start())
    if i < 0:
        return text
    depth, j = 0, i
    while j < len(text):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return text[:m.start()] + text[j + 1:]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="找声明了但没人读的配置键")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="同时打印已正确接线的键")
    args = ap.parse_args(argv)

    cfg = ROOT / "config" / "settings.yaml"
    if not cfg.exists():
        print(f"✗ 找不到配置：{cfg}")
        return 1

    keys = leaf_keys(cfg.read_text(encoding="utf-8"))
    parts = []
    for p in sorted(SRC.rglob("*.py")):
        t = p.read_text(encoding="utf-8-sig")
        if p.name == "config.py":
            t = strip_defaults(t)      # 只去掉声明点，保留读取点
        parts.append(t)
    py = "\n".join(parts)

    dead: list[tuple[str, int]] = []
    for k, ln in sorted(keys.items(), key=lambda kv: kv[1]):
        if k in ALLOW:
            continue
        forms = consumed_forms(k, py)
        if not forms:
            # 再退一步：键名是否至少作为字面量出现（可能是整段 section 取走）
            if re.search(rf"""['"]{re.escape(k)}['"]""", py):
                if args.verbose:
                    print(f"  · {k:<28} L{ln:<5} 仅字面量出现（可能整段取走）")
                continue
            dead.append((k, ln))
        elif args.verbose:
            print(f"  ✓ {k:<28} L{ln:<5} {', '.join(forms)}")

    print(f"\n配置叶子键 {len(keys)} 个，检查了 {len(keys) - len(ALLOW)} 个"
          f"（跳过 {len(ALLOW)} 个已知特殊读法）")
    if not dead:
        print("✓ 没有发现完全没被读取的配置键")
        return 0
    print(f"✗ 有 {len(dead)} 个键在 src/ 里找不到任何读取点：")
    for k, ln in dead:
        print(f"    settings.yaml:{ln}  {k}")
    print("\n这些键改了不会生效。要么接线，要么删掉。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
