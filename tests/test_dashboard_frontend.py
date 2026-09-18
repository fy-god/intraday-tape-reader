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

import re
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


# ==========================================================================
# CSS 结构：格子数与网格列数必须一致
# ==========================================================================
def _css_block(html: str, selector: str) -> str:
    """取出 ``selector{...}`` 的声明块（选择器需出现在行首）。"""
    import re

    m = re.search(
        rf"^\s*{re.escape(selector)}\s*\{{([^}}]*)\}}", html, re.M | re.S)
    assert m, f"看板 CSS 里找不到 {selector} 规则"
    return m.group(1)


def test_spirit_row_grid_columns_match_rendered_cells():
    """``.sp`` 的网格列数必须等于 ``spiritRow`` 实际产出的格子数。

    为什么单独立一条（离线、不需要浏览器）：这两个数字写在不同地方 ——
    列数在 CSS 的 ``grid-template-columns``，格子数在 JS 的 innerHTML 拼接里。
    一旦不一致（**历史上真的漏过一列**：5 列网格配 6 个格子），CSS Grid 会把
    多出来的那个自动换到第二行，于是每行从 23px 变成 46.5px 的**两行文字**，
    一屏可见条数从 10 条掉到 5 条。

    这个 bug 不会报错、不会让测试变红、截图乍看也正常 —— 它只让滚动列表的
    信息密度腰斩，而信息密度正是短线精灵这东西的全部价值。
    """
    import re

    html = (ROOT / "src" / "arad" / "server" / "dashboard.html").read_text(encoding="utf-8")

    cols = _css_block(html, ".sp")
    m = re.search(r"grid-template-columns\s*:\s*([^;]+);", cols)
    assert m, ".sp 里找不到 grid-template-columns"
    n_css_cols = len(m.group(1).split())
    assert n_css_cols >= 2, f"列数解析可疑：{m.group(1)!r}"

    script = re.search(r"<script[^>]*>([\s\S]*?)</script>", html).group(1)
    body = re.search(r"function spiritRow\(a\)\{(.*?)\n\}", script, re.S)
    assert body, "找不到 spiritRow 函数（结构变了？）"
    # 数 span：'<span class="xx ...">' 形式
    n_cells = len(re.findall(r"'<span class=", body.group(1)))
    assert n_cells >= 2, f"没能从 spiritRow 里数出格子，解析可疑：{n_cells}"

    assert n_css_cols == n_cells, (
        f".sp 声明了 {n_css_cols} 列，但 spiritRow 产出 {n_cells} 个格子 —— "
        f"多出的格子会被 CSS Grid 换到第二行，行高翻倍、一屏条数腰斩。"
        f"请让 grid-template-columns 的列数与 span 数量一致。"
    )


def test_spirit_filters_do_not_wrap_and_header_stays_short():
    """分组按钮不许折行：折行会把表头从 31px 撑到 53px，白吃掉一条精灵的高度。

    左栏窄（420px）时 flex 默认 ``min-width:auto``（= 内容宽度）不允许收缩，
    6 个按钮就会换行。所以要同时钉住 nowrap 与 min-width:0 两个前提。
    """
    html = (ROOT / "src" / "arad" / "server" / "dashboard.html").read_text(encoding="utf-8")
    css = _css_block(html, ".spfilters")
    assert "nowrap" in css.replace(" ", ""), (
        ".spfilters 必须 flex-wrap:nowrap（否则窄屏下按钮折行、表头变高）")
    assert "min-width:0" in css.replace(" ", ""), (
        ".spfilters 必须有 min-width:0 —— flex 项默认 min-width:auto 等于内容宽度，"
        "不允许收缩，光写 nowrap 也拦不住折行")


def test_theme_foreground_colors_meet_contrast():
    """前景色对面板底色必须达到 WCAG AA 正文对比度（4.5:1）。

    时间戳用的是 ``--fg3``，它曾经是 ``#5d6a80``（实测 3.5:1），在大屏暗色面板上
    偏糊。这里用真实公式算，避免"看着还行"的主观判断 —— 它也是唯一一条防止
    有人随手把颜色调暗而没人发现的护栏。
    """

    def _lum(hexs: str) -> float:
        h = hexs.lstrip("#")
        chans = []
        for i in (0, 2, 4):
            c = int(h[i:i + 2], 16) / 255.0
            chans.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
        r, g, b = chans
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    def ratio(fg: str, bg: str) -> float:
        a, b = _lum(fg), _lum(bg)
        hi, lo = max(a, b), min(a, b)
        return (hi + 0.05) / (lo + 0.05)

    html = (ROOT / "src" / "arad" / "server" / "dashboard.html").read_text(encoding="utf-8")
    var_block = re.search(r":root\{(.*?)\}", html, re.S).group(1)
    colors = dict(re.findall(r"(--[a-z0-9]+)\s*:\s*(#[0-9a-fA-F]{6})", var_block))
    assert {"--fg2", "--fg3", "--bg", "--bg2", "--bg3"} <= set(colors), colors

    backgrounds = [colors["--bg"], colors["--bg2"], colors["--bg3"]]
    for name in ("--fg2", "--fg3"):
        worst = min(ratio(colors[name], b) for b in backgrounds)
        assert worst >= 4.5, (
            f"{name}={colors[name]} 在最差底色上对比度仅 {worst:.2f}:1，"
            f"低于 WCAG AA 正文要求 4.5:1（会把时间戳这类小字糊掉）")

    # 层级不能乱：fg3 必须比 fg2 暗，否则"次要信息"看起来比主要信息还重
    assert _lum(colors["--fg3"]) < _lum(colors["--fg2"]), (
        "--fg3 应比 --fg2 暗（它承载时间戳等次要信息）")


def test_tooltip_extra_has_units_and_covers_every_whitelisted_key():
    """悬停提示里的 extra 必须带单位，且**每个**服务端会下发的键都有映射。

    这是一条真实的用户可见缺陷：``extra`` 的键名是英文内部名、值也不带单位，
    前端直接 ``tip.push(k + " " + v)`` 拼出来是::

        amount 102000000    <- 单位是"元"
        turnover 5          <- 缺 %
        window_pct -0.98    <- 缺 %

    而**同一个看板**的股票表用「成交额(亿)」显示同一个量（1.02），
    于是同屏出现两套单位、差 8 个数量级，用户没法比。

    这条用例做两件事：
      1. 钉住「服务端白名单里的每个键，前端都有中文名+单位」—— 防止有人
         在 spirit.py 里新增一个 extra 字段、前端却默默按裸键渲染；
      2. 钉住未知键**不被丢弃**（原样显示），因为静默丢字段会让排查
         问题的人以为数据压根没产生。
    """
    import re

    from arad import spirit as sp

    html = (ROOT / "src" / "arad" / "server" / "dashboard.html").read_text(encoding="utf-8")

    # 1) 服务端白名单：从 spirit.py 的**源码**里解析出来，而不是在测试里抄一份。
    #    ⚠ 抄一份会漏掉关键情形：有人在 spirit.py 白名单里新增键、前端没补映射时，
    #    测试仍拿旧列表比对，照样通过 —— 这条用例就白写了。
    #    （我第一版就是这么写的，用"往白名单里塞一个未映射键"验证时发现抓不到。）
    src = (ROOT / "src" / "arad" / "spirit.py").read_text(encoding="utf-8")
    m_wl = re.search(r'"extra":\s*\{k:\s*_finite\(m\[k\]\)\s*for\s*k\s*in\s*\((.*?)\)',
                     src, re.S)
    assert m_wl, "没能从 spirit.py 里解析出 extra 白名单（结构变了？）"
    keys = tuple(re.findall(r'"(\w+)"', m_wl.group(1)))
    assert len(keys) >= 5, f"白名单解析可疑：{keys}"

    # 顺带确认这些键确实会被下发（解析出来的列表与真实行为一致）
    from arad.models import Alert, AlertKind
    a = Alert(key="k", kind=AlertKind.SURGE, code="600000", name="x", ts=None,
              price=1.0, pct=1.0, title="t", detail="d",
              metrics={k: 1.0 for k in keys})
    extra = sp.to_feed_item(a)["extra"]
    assert set(extra) == set(keys), f"白名单解析结果与实际不符：{sorted(extra)}"

    # 2) 前端必须有对应的映射表，且覆盖全部键
    m = re.search(r"var EX_UNITS = \{(.*?)\n  \};", html, re.S)
    assert m, "dashboard.html 里找不到 EX_UNITS 映射表"
    mapped = set(re.findall(r"^\s*(\w+):", m.group(1), re.M))
    missing = set(keys) - mapped
    assert not missing, (
        f"这些 extra 键在悬停提示里没有单位映射，会原样渲染成裸键名："
        f"{sorted(missing)}。请在 dashboard.html 的 EX_UNITS 里补上"
        f"（中文名 + 换算 + 单位）。")

    # 3) 未知键必须原样保留（不能被静默丢弃）
    loop = html[html.index("for(var k in ex){"):]
    loop = loop[:loop.index("el.title")]
    assert "tip.push(k + \" \" + ex[k])" in loop.replace("'", '"'), (
        "未知 extra 键必须原样显示；静默丢字段会让排查者以为数据没产生")

    # 4) amount 必须换算成"亿" —— 与同屏股票表的表头口径一致
    assert re.search(r"amount:\s*\[\s*\"成交额\",\s*1e-8,\s*\"亿\"", m.group(1)), (
        "amount 应换算成亿（同屏表格用的是「成交额(亿)」），"
        "否则同一个量会同时以元和亿两种单位出现")
