"""短线精灵看板的**前端渲染**测试。

为什么需要单独一组：``test_server_web.py`` 里的 spirit 用例验证的是"服务端
给对了数据"（接口形状、方向映射、字段齐全），但**前端拿到数据后画得对不对**
是另一回事 —— 把 ``打开涨停`` 画成红色、把中文名硬编码回 JS、长会话不设 DOM
上限，这些服务端测试全都看不见。

做法：用 Node 把 ``dashboard.html`` 里那段 ``<script>`` 原样执行（配一个极小
DOM 替身），直接调 ``spiritRow`` / ``pushSpirit`` 断言渲染结果。比引入 jsdom
轻得多，又比"grep 一下 HTML 里有没有某个字符串"强得多。

没有 Node 时自动跳过（本项目的核心测试不依赖 Node）。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "dash_render_check.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="未安装 Node，跳过前端渲染测试（核心测试不依赖 Node）",
)


def test_dashboard_frontend_renders_spirit_rows():
    """跑 Node 渲染检查脚本，全绿才算通过。"""
    assert SCRIPT.exists(), f"缺少 {SCRIPT}"
    proc = subprocess.run(
        ["node", str(SCRIPT)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, f"前端渲染检查失败：\n{out}"
    assert "前端渲染正常" in out, out
    # 关键方向断言必须在输出的 ✓ 列表里真的出现过
    assert "打开涨停方向为 down" in out, out
    assert "指数行保留带前缀代码" in out, out
    assert "DOM 行数被 MAX_SPIRIT 截住" in out, out


def test_frontend_does_not_hardcode_signal_names():
    """精灵面板的中文信号名不许在前端硬编码，必须由服务端 /api/spirit 下发。

    前端复制一份映射的后果：服务端改名（比如把"火箭发射"改成"快速拉升"）时，
    前端会静默继续显示旧名，两边长期不一致却没人发现。

    例外：**实时告警流**的 ``KIND`` 映射（急拉/急跌/涨停/跌停/放量/异动）
    是另一套东西 —— 它描述的是 ``AlertKind``（引擎的告警分类），属于既有功能，
    且与精灵信号恰好同名。这几个名字从 HTML 里解析出来做白名单，不在检查范围内。
    """
    import re
    import sys

    html = (ROOT / "src" / "arad" / "server" / "dashboard.html").read_text(encoding="utf-8")
    m = re.search(r"<script[^>]*>([\s\S]*?)</script>", html)
    assert m, "看板里找不到 <script> 块"
    js = m.group(1)

    # 告警流的 KIND 标签（合法例外）：从 `var KIND = { surge:{t:"急拉"}, ... }` 解析
    kind_block = re.search(r"var KIND\s*=\s*\{(.*?)\};", js, re.S)
    assert kind_block, "看板里找不到 KIND 映射（结构变了？）"
    allowed = set(re.findall(r't\s*:\s*"([^"]+)"', kind_block.group(1)))
    assert allowed, "没能从 KIND 里解析出任何标签"
    assert "急拉" in allowed

    sys.path.insert(0, str(ROOT / "src"))
    from arad.spirit import SIGNALS

    leaked = sorted(
        sig.cn for sig in SIGNALS.values()
        if sig.cn in js and sig.cn not in allowed
    )
    assert not leaked, (
        f"前端脚本里硬编码了精灵信号中文名 {leaked}；"
        f"中文名必须来自服务端 /api/spirit（见 dashboard.html 的 spiritRow）。"
        f"若确有正当理由，请加进本测试的白名单并说明原因。"
    )
