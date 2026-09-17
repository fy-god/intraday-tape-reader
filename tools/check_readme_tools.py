"""核对 README 的工具清单与 tools/ 实际内容是否一致。

README 维护了一张工具表。新增工具忘了登记、或删了工具忘了删表，
都会让读者按文档操作时扑空。这个检查让它变成一条可跑的命令。
"""
from __future__ import annotations

try:
    import _console  # noqa: F401,E402
except ImportError:  # pragma: no cover
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    import _console  # noqa: F401,E402

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
README = ROOT / "README.md"

# 不作为"工具"登记的内部文件
SKIP = {"_console.py"}


def main() -> int:
    if not README.exists():
        print(f"✗ 找不到 README：{README}")
        return 1
    readme = README.read_text(encoding="utf-8")

    on_disk = sorted(p.name for p in TOOLS.glob("*.py") if p.name not in SKIP)
    # README 里以 `name.py` 形式提到的工具
    mentioned = set(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*\.py)`", readme))

    missing = [n for n in on_disk if n not in mentioned]
    ghost = sorted(n for n in mentioned if not (TOOLS / n).exists())

    print(f"tools/ 实际脚本 {len(on_disk)} 个；README 提到 {len(mentioned)} 个")
    bad = 0
    if missing:
        bad += len(missing)
        print(f"\n✗ 有 {len(missing)} 个脚本没登记进 README 的工具表：")
        for n in missing:
            print(f"    {n}")
    if ghost:
        bad += len(ghost)
        print(f"\n✗ README 提到了 {len(ghost)} 个不存在的脚本：")
        for n in ghost:
            print(f"    {n}")
    if not bad:
        print("✓ README 工具清单与 tools/ 完全一致")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
