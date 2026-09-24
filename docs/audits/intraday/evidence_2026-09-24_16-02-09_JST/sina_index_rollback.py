"""回退验牙：Sina 指数身份修复（IT-P1-SINA-INDEX-PREFIX-LOSS-...-012）。

每条牙都回退**生产代码的一处**，确认它**自己的**目标测试变红。
只回退、不新增测试；源码 sha256 前后必须一致。
"""
import hashlib
import subprocess
import sys
from pathlib import Path

REPO = Path(r"D:\ccc\ashare-radar")
SINA = REPO / "src" / "arad" / "sources" / "sina.py"

TEETH = {
    "RB-S1": {
        "desc": "回退 _wire_symbols -> 旧的剥前缀+guess_prefix 重猜",
        "old": "        wanted = _wire_symbols(codes)",
        "new": "        wanted = [f\"{guess_prefix(c)}{c}\" for c in _norm_codes(codes)]",
        "targets": ["test_snapshots_sends_correct_index_wire_symbols"],
    },
    "RB-S2": {
        "desc": "回退 _fetch_bulk 的严格过滤 -> 纯裸码过滤",
        # ⚠ 上一版我把锚点写成了 parse_response_detailed 里的 `if strict:`，
        # 但目标测试走的是 snapshots() -> _fetch_bulk，**根本没碰到那个函数**
        # → 牙"没咬"。这是 D15 的第四次犯（探针没穿过被测分支）。
        # 正确的锚点在 _fetch_bulk 的过滤表达式上。
        "old": "        return [q for q in quotes\n"
               "                if q.code in allowed_bare or q.code in loose_bare]",
        "new": "        return [q for q in quotes\n"
               "                if q.code in {c[2:] if len(c) > 6 else c for c in codes}]",
        "targets": ["test_wrong_instrument_response_is_rejected"],
    },
    "RB-S3": {
        "desc": "回退 parse_response_detailed 的 wire 过滤（不传 wire_symbols）",
        "old": "                                       wire_symbols=codes)",
        "new": "                                       )",
        "targets": ["test_detailed_does_not_self_consistently_report_wrong_instrument"],
    },
}


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12]


def read_text(p: Path) -> str:
    """读为 LF 文本。

    ⚠ `Path.read_text()` 会做 universal-newline 转换（CRLF -> LF），
    配 `write_text()` 会把整个文件**重写成 CRLF** —— 上一版就是这么
    把 sha256 搞不一致的。这里显式按 bytes 读、按 bytes 写，只动锚点。
    """
    return p.read_bytes().decode("utf-8")


def write_text(p: Path, text: str) -> None:
    """按 bytes 写回（不碰行尾、不加 BOM）。"""
    p.write_bytes(text.encode("utf-8"))


def run(targets):
    r = subprocess.run(
        [sys.executable, "-m", "pytest", *[f"tests/{t}.py::{t}" if False else
         f"tests/test_sina_index_identity_no_wrong_instrument.py::{t}"
         for t in targets],
         "-o", "addopts=", "-q", "-p", "no:cacheprovider", "--tb=no"],
        cwd=REPO, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    return r.returncode, r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""


def main():
    orig = read_text(SINA)
    before = sha(SINA)
    print(f"sina.py sha256(前) = {before}")
    print("=" * 74)

    results = []
    for name, t in TEETH.items():
        if t["old"] not in orig:
            print(f"{name}: ✗ 回退锚点未找到（脚本失效，非产品问题）")
            results.append((name, "ANCHOR-MISSING", ""))
            continue
        write_text(SINA, orig.replace(t["old"], t["new"], 1))
        rc, last = run(t["targets"])
        write_text(SINA, orig)
        # 结构 vs 行为：import/collection 阶段的错只证明符号缺失
        structural = "ImportError" in last or "collection" in last.lower() \
            or "errors during collection" in last.lower()
        kind = "结构性(不算数)" if structural else ("行为性" if rc != 0 else "**没咬**")
        print(f"{name}: rc={rc} {kind}")
        print(f"    {t['desc']}")
        print(f"    target={t['targets']} -> {last}")
        results.append((name, kind, last))

    write_text(SINA, orig)
    after = sha(SINA)
    print("=" * 74)
    print(f"sina.py sha256(后) = {after}  {'一致 OK' if after == before else '**不一致**'}")
    print()
    bite = [n for n, k, _ in results if k == "行为性"]
    struct = [n for n, k, _ in results if k.startswith("结构")]
    dead = [n for n, k, _ in results if k == "**没咬**"]
    print(f"行为性咬合: {len(bite)}/{len(TEETH)}  {bite}")
    print(f"结构性(不算数): {struct}")
    print(f"没咬(牙无效): {dead}")
    return 0 if not dead and after == before else 1


if __name__ == "__main__":
    sys.exit(main())
