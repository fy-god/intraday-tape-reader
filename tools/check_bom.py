"""检查仓库文本文件是否带 UTF-8 BOM —— 独立于 pytest 的自检（IT-P0-004/005）。

为什么必须**独立于 pytest**
---------------------------
`pyproject.toml` 带 BOM 时，`pytest` 自己就会在启动阶段解析失败退出：

    ERROR: pyproject.toml: Invalid statement (at line 1, column 1)
    exit code: 4

于是"用 pytest 测试来守卫 BOM"这条路是**自相矛盾**的 —— 守卫和被守卫的
东西一起死了（本文件的存在正是回退验牙过程中实测到这个悖论的结果）。
所以这道检查必须是纯标准库脚本，不经 pytest、不读 pyproject。

退出码：0 = 干净；1 = 发现 BOM。

用法：
    python tools/check_bom.py
    python tools/check_bom.py --all      # 连文档也一起扫
"""
from __future__ import annotations

import sys
from pathlib import Path

# Windows 控制台默认 GBK，✓/✗ 会直接 UnicodeEncodeError 把脚本打断。
# 这里主动把 stdout/stderr 切到 utf-8；失败也不影响检查本身。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

ROOT = Path(__file__).resolve().parents[1]
BOM = b"\xef\xbb\xbf"

# 代码与配置：这些带 BOM 会真实破坏工具链
CODE_EXTS = {".py", ".toml", ".cfg", ".ini", ".yaml", ".yml", ".json", ".js"}
# 文档：BOM 不会破坏解析，但会造成"幽灵首行"、diff 噪音
DOC_EXTS = {".md", ".html", ".txt", ".rst", ".csv"}

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache",
             ".mypy_cache", ".ruff_cache", "venv", ".venv"}


def scan(exts: set[str]) -> list[str]:
    offenders: list[str] = []
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in exts:
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts):
            continue
        try:
            if p.read_bytes().startswith(BOM):
                offenders.append(str(p.relative_to(ROOT)))
        except OSError as exc:
            print(f"  [警告] 读不了 {p.relative_to(ROOT)}: {exc}")
    return offenders


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    exts = CODE_EXTS | DOC_EXTS if "--all" in args else CODE_EXTS

    print("=" * 66)
    print("UTF-8 BOM 检查（独立于 pytest —— BOM 会让 pytest 起不来）")
    print("=" * 66)

    code_bad = scan(CODE_EXTS)

    if code_bad:
        print(f"\n[✗] {len(code_bad)} 个代码/配置文件带 BOM：")
        for f in code_bad:
            print(f"      {f}")
        print("\n  这类文件带 BOM 会让 tomllib / pytest / 打包工具解析失败。")
        print("  修法：以 utf-8（无 BOM）重新保存，或执行")
        print("        python -c \"import pathlib;p=pathlib.Path(r'FILE');"
              "p.write_bytes(p.read_bytes().lstrip(b'\\xef\\xbb\\xbf'))\"")
        return 1

    print(f"\n[✓] 代码/配置文件均无 BOM（扫描 {len(CODE_EXTS)} 种扩展名）")

    if "--all" in args:
        doc_bad = scan(DOC_EXTS)
        if doc_bad:
            print(f"\n[!] {len(doc_bad)} 个文档带 BOM（不破坏解析，但建议清掉）：")
            for f in doc_bad:
                print(f"      {f}")
        else:
            print("[✓] 文档也无 BOM")

    # 顺带确认 pytest 能真的启动 —— 这才是 BOM 检查的最终意义
    try:
        import tomllib
        tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        print("[✓] pyproject.toml 可被 tomllib 解析 —— pytest 能正常启动")
    except FileNotFoundError:
        print("[!] 找不到 pyproject.toml")
    except Exception as exc:  # noqa: BLE001
        print(f"[✗] pyproject.toml 解析失败：{type(exc).__name__}: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
