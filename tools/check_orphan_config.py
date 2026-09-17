"""配置键体检：找出「写在 settings.yaml 里但代码从不读取」的键。

为什么需要这个：本项目已经两次栽在"配置静默失效"上 ——
  1. ``max_per_round: 0`` 被 `or 默认值` 悄悄换回非 0；
  2. ``load_settings(use_cache=False)`` 仍写缓存，污染了共享单例。
这类 bug 的共同特征是**不报错**：用户改了配置，系统照旧按老行为跑。

本脚本把 settings.yaml 里所有叶子键的**末段名**收集起来，再到 src/ 里搜
字符串字面量。搜不到 = 很可能没人读（也可能是故意留着给用户参考的）。
它是启发式的，不做断言，只列出来供人眼确认。

跑法：``python tools\\check_orphan_config.py``
退出码：0 = 没有可疑键；1 = 有可疑键（供 CI 提醒，不阻断）
"""
from __future__ import annotations

import _console  # noqa: F401,E402  —— Windows 控制台 UTF-8（见 tools/_console.py）

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CFG = ROOT / "config" / "settings.yaml"

#: 明确允许"只写不读"的键（给人看的注释/占位/由外部工具消费）
ALLOW: dict[str, str] = {
    "bulk_chunk": "由各源自己的 __init__ 读，不在 arad.*.get 里出现",
    "referer": "同上",
    "workers": "同上",
    "pages": "历史别名",
}


def leaf_keys(text: str) -> dict[str, int]:
    """粗略提取 YAML 叶子键（缩进比子项深的最后一级）-> 行号。

    不引 PyYAML 是为了保留注释与重复键，且能在 YAML 语法出错时仍给出线索。
    """
    out: dict[str, int] = {}
    stack: list[tuple[int, str]] = []          # (indent, key)
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip() or ":" not in line:
            continue
        indent = len(line) - len(line.lstrip())
        key = line.strip().split(":", 1)[0].strip().strip('"\'')
        if not key or key.startswith("-"):
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key))
        # 只看有值的行（叶子）；纯父节点（值为空）跳过
        val = line.strip().split(":", 1)[1].strip()
        if val:
            out[key] = i
    return out


def main() -> int:
    if not CFG.exists():
        print(f"✗ 找不到配置：{CFG}")
        return 1
    keys = leaf_keys(CFG.read_text(encoding="utf-8"))

    # 收集 src/ 里所有字符串字面量（含 f-string 片段）
    blob_parts: list[str] = []
    for p in sorted(SRC.rglob("*.py")):
        blob_parts.append(p.read_text(encoding="utf-8-sig"))
    blob = "\n".join(blob_parts)

    suspicious: list[tuple[str, int]] = []
    for k, ln in sorted(keys.items(), key=lambda kv: kv[1]):
        if k in ALLOW:
            continue
        esc = re.escape(k)
        # 三种命中形式，缺一不可：
        #   1. 独立键名   "min_amount"
        #   2. 点分路径末段 "poll.index_codes" -> 引擎就是这么读的
        #   3. 路径中段   "poll.index_codes" 当键名本身含点时（如 sources.universe）
        # 只匹配引号边界，避免 substring 误判（code 命中 encode）。
        hit = (
            re.search(rf"""['"]{esc}['"]""", blob)
            or re.search(rf"""['"][\w.]*\.{esc}['"]""", blob)
            or re.search(rf"""['"]{esc}\.[\w.]*['"]""", blob)
        )
        if hit:
            continue
        suspicious.append((k, ln))

    print(f"配置叶子键 {len(keys)} 个，源码字符串字面量命中 "
          f"{len(keys) - len(suspicious)} 个")
    if not suspicious:
        print("✓ 没有可疑的孤立配置键")
        return 0

    print(f"\n以下 {len(suspicious)} 个键在 src/ 里搜不到字符串字面量，"
          f"请确认是否有意保留：")
    for k, ln in suspicious:
        print(f"  settings.yaml:{ln:<4} {k}")
    print("\n（可能是：给人看的占位、由测试读取、或拼在 f-string 里 —— 需人工判断）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
