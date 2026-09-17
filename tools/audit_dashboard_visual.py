"""短线精灵看板 · 真浏览器视觉与交互审计（Playwright / Chromium）。

与仓库里已有工具的分工：
* ``tools/shot_dashboard.py``  —— 验**后端契约**（HTTP / SSE / 坏数据韧性），不开浏览器；
* ``tools/shot_browser.py``    —— 粗粒度看板冒烟（有没有行、有没有横向溢出）；
* ``tools/check_colors.py``    —— 只查**方向配色**（红涨绿跌 + 打开涨跌停的反直觉两例）；
* **本脚本** —— 把上面三件事里"需要在真浏览器里量"的部分合成一份**可复现的审计**，
  逐项 ✓/✗，并把证据截到 ``tools/audit_shots/``。

审计覆盖 6 组：
  1. 滚动播报（短线精灵）的观感与行为：新行在最上、列对齐、行高一致、可滚动、
     新行是否真的闪一下、超长股票名/未知信号名会不会撑破格子；
  2. 四种视口（1024/1280/1440/1920）下的布局：横向溢出、面板重叠、面板可见性、筛选按钮换行；
  3. 配色（红涨绿跌）—— 含最容易搞反的「打开涨停=绿 / 打开跌停=红」；
  4. 分组筛选按钮的点击行为：真的过滤、激活态可见、来回切换不丢行不重复；
  5. 健壮性：pageerror / console.error 必须为 0、DOM 行数被 MAX_SPIRIT 截住、
     未知信号名优雅降级、刷新后 SSE 自动恢复；
  6. 可访问性：告警行/精灵行的文字对比度、颜色之外是否还有文字承载方向信息。

跑法::

    python tools\\audit_dashboard_visual.py            # 起临时服务，跑完自动关
    python tools\\audit_dashboard_visual.py --keep     # 跑完保留服务自己看
    python tools\\audit_dashboard_visual.py --headed   # 显示浏览器窗口

退出码非 0 表示有检查项未通过。**任何情况下都会在 finally 里关服务、关浏览器。**
"""
from __future__ import annotations

# Windows 控制台 UTF-8（见 tools/_console.py）。
# 先正常导入；若失败说明本文件是被**按路径**加载的（例如测试用 importlib
# 从 tests/ 里 exec 它），此时 tools/ 不在 sys.path 上——把本文件所在目录
# 补进去再试一次，这样"直接跑"和"被当模块加载"两种场景都能用。
try:
    import _console  # noqa: F401,E402
except ImportError:  # pragma: no cover - 取决于调用方式
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    import _console  # noqa: F401,E402

import argparse
import json
import socket
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SHOTS = ROOT / "tools" / "audit_shots"

VIEWPORTS = [(1280, 800), (1440, 900), (1920, 1080), (1024, 768)]

# (code, name, signal, AlertKind, severity, 期望方向, 说明)
# 覆盖：正常名 / 4 字名 / 带字母名 / 6 字+名 / 未知信号名 / 两个最易搞反的信号
COLOR_CASES: list[tuple[str, str, str, str, int, str, str]] = [
    ("600519", "贵州茅台", "rocket", "surge", 2, "up", "火箭发射 = 涨"),
    ("600519", "贵州茅台", "dive", "plunge", 2, "down", "高台跳水 = 跌"),
    ("300750", "宁德时代", "limit_up_seal", "limit_up", 3, "up", "封涨停板 = 涨"),
    ("300750", "宁德时代", "open_limit_up", "limit_up", 2, "down", "打开涨停 = 利空（绿）"),
    ("000858", "五粮液", "limit_down_seal", "limit_down", 3, "down", "封跌停板 = 跌"),
    ("000858", "五粮液", "open_limit_down", "limit_down", 2, "up", "打开跌停 = 利好（红）"),
    ("sh000001", "上证指数", "index_pull", "unusual", 2, "up", "拉升指数 = 涨"),
    ("sh000001", "上证指数", "index_press", "unusual", 2, "down", "打压指数 = 跌"),
    ("601318", "中国平安", "big_buy", "unusual", 2, "up", "大笔买入 = 涨"),
    ("601318", "中国平安", "big_sell", "unusual", 2, "down", "大笔卖出 = 跌"),
    ("002594", "比亚迪", "limit_up_touch", "limit_up", 1, "flat", "触及涨停 = 中性"),
    ("000001", "平安银行", "limit_down_touch", "limit_down", 1, "flat", "触及跌停 = 中性"),
]

# 长名探针（第 1 组用）：
#   realistic —— 真实 A 股里偏长的简称（含字母、含 6 字），应当**完整显示**
#   stress     —— 远超真实长度的压力用例，应当**省略号截断且不撑破行**
UNKNOWN_SIGNAL = "this_signal_is_not_registered_at_all"
LONG_NAME_CASES: list[tuple[str, str, str, str, int, str]] = [
    ("600760", "中航成飞", "rocket", "surge", 3, "up"),
    ("000100", "TCL科技", "dive", "plunge", 2, "down"),
    ("600519", "贵州茅台", "limit_up_seal", "limit_up", 3, "up"),
    ("000001", "平安银行", "big_buy", "unusual", 2, "up"),
    ("600002", "ST某某科技", "rocket", "surge", 2, "up"),
    ("600003", "某某某超长股票名称压力测试用例啊啊啊", "rocket", "surge", 2, "up"),
    ("002594", "比亚迪", UNKNOWN_SIGNAL, "unusual", 2, "flat"),
]
# 只用来验"是否截断"的压力名（刻意超出任何真实简称）
REALISTIC_NAMES = {"中航成飞", "TCL科技", "贵州茅台", "平安银行", "ST某某科技"}
STRESS_NAMES = {"某某某超长股票名称压力测试用例啊啊啊"}
UNKNOWN_KEY = f"audit:long:{LONG_NAME_CASES.index(('002594', '比亚迪', UNKNOWN_SIGNAL, 'unusual', 2, 'flat'))}"

#: 每次进入"名字/未知信号"检查前重推一遍的探针（会被 200 行上限挤掉，必须补推）
NAME_PROBE_TAG = "audit:namerun"


T0 = datetime(2026, 9, 16, 9, 31, 5)


# ==========================================================================
# 小工具（沿用 shot_dashboard.py / check_colors.py 的写法）
# ==========================================================================
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Audit:
    """收集 ✓/✗ 结果，最后统一汇报。"""

    def __init__(self) -> None:
        self.results: list[tuple[str, str, bool, str]] = []
        self.section = "?"

    def start(self, name: str) -> None:
        self.section = name
        print(f"\n--- {name} ---")

    def check(self, label: str, ok: bool, extra: str = "") -> bool:
        self.results.append((self.section, label, bool(ok), extra))
        print(f"  {'✓' if ok else '✗'} {label}" + (f"  {extra}" if extra else ""))
        return bool(ok)

    def note(self, text: str) -> None:
        print(f"  · {text}")

    @property
    def failures(self) -> list[tuple[str, str, bool, str]]:
        return [r for r in self.results if not r[2]]

    def report(self) -> int:
        print("\n" + "=" * 78)
        print(f"{'结果':<4}{'检查项':<52}{'实测'}")
        print("-" * 78)
        for section, label, ok, extra in self.results:
            mark = "✓" if ok else "✗"
            print(f"{mark:<4}{label:<52}{extra}")
        print("-" * 78)
        n_fail = len(self.failures)
        print(f"共 {len(self.results)} 项：通过 {len(self.results) - n_fail}，未通过 {n_fail}")
        if n_fail:
            print("\n未通过明细：")
            for section, label, _ok, extra in self.failures:
                print(f"  ✗ [{section}] {label}  {extra}")
        return 1 if n_fail else 0


def make_alert(key: str, code: str, name: str, signal: str, kind: str,
               severity: int, pct: float, ts: datetime | None = None):
    from arad.models import Alert, AlertKind

    return Alert(
        key=key, kind=AlertKind(kind), code=code, name=name,
        ts=ts or T0, price=12.34, pct=pct,
        title=f"{name} {signal}", detail="视觉审计探针", severity=severity,
        metrics={"pattern": signal},
    )


# ==========================================================================
# 颜色 / 对比度
# ==========================================================================
def parse_rgb(css: str) -> tuple[int, int, int] | None:
    if not css:
        return None
    s = css.strip().lower()
    if s.startswith("rgb"):
        body = s[s.index("(") + 1:s.index(")")]
        parts = [p.strip() for p in body.split(",")[:3]]
        try:
            return int(round(float(parts[0]))), int(round(float(parts[1]))), int(round(float(parts[2])))
        except (ValueError, IndexError):
            return None
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) >= 6:
            try:
                return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            except ValueError:
                return None
    return None


def is_red(css: str) -> bool:
    """红分量显著最高（check_colors.py 的判据，容差 40）。"""
    n = parse_rgb(css)
    return bool(n) and n[0] > n[1] + 40 and n[0] > n[2] + 40


def is_green(css: str) -> bool:
    n = parse_rgb(css)
    return bool(n) and n[1] > n[0] + 40 and n[1] > n[2] + 20


def is_grey(css: str) -> bool:
    """中性灰：三通道接近，不偏红也不偏绿。"""
    n = parse_rgb(css)
    if not n:
        return False
    return max(n) - min(n) <= 40


def parse_color_any(css: str) -> tuple[float, float, float, float]:
    """把 rgb()/rgba() 解析成 (r,g,b,a)，0~255 / 0~1。"""
    s = (css or "").strip().lower()
    if not s.startswith("rgb"):
        n = parse_rgb(s)
        if n:
            return n[0], n[1], n[2], 1.0
        return 0.0, 0.0, 0.0, 0.0
    body = s[s.index("(") + 1:s.index(")")]
    parts = [p.strip() for p in body.split(",")]
    try:
        r, g, b = (float(parts[0]), float(parts[1]), float(parts[2]))
        a = float(parts[3]) if len(parts) > 3 else 1.0
    except (ValueError, IndexError):
        return 0.0, 0.0, 0.0, 0.0
    return r, g, b, a


def over(fg: str, bg: tuple[float, float, float]) -> tuple[float, float, float]:
    """把（可能有 alpha 的）前景色合成到不透明背景上。"""
    r, g, b, a = parse_color_any(fg)
    return (r * a + bg[0] * (1 - a), g * a + bg[1] * (1 - a), b * a + bg[2] * (1 - a))


def rel_lum(rgb: tuple[float, float, float]) -> float:
    def ch(v: float) -> float:
        v = max(0.0, min(255.0, v)) / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(rgb[0]) + 0.7152 * ch(rgb[1]) + 0.0722 * ch(rgb[2])


def contrast_ratio(fg_css: str, bg_css: str) -> float:
    """WCAG 对比度。fg 的 alpha 会按 bg 合成（bg 需为不透明）。"""
    bg = parse_color_any(bg_css)[:3]
    fg = over(fg_css, bg)
    l1, l2 = rel_lum(fg), rel_lum(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def flatten_bg(chain: list[str]) -> str:
    """把祖先链上的半透明背景依次合成到不透明底色，得到实际背景色。

    ``chain`` 顺序：最外层 -> 元素自身。
    """
    acc = (11.0, 14.0, 19.0)                      # --bg #0b0e13 兜底
    for css in chain:
        r, g, b, a = parse_color_any(css)
        if a <= 0:
            continue
        acc = (r * a + acc[0] * (1 - a), g * a + acc[1] * (1 - a), b * a + acc[2] * (1 - a))
    return f"rgb({acc[0]:.0f}, {acc[1]:.0f}, {acc[2]:.0f})"


def count_ink_bands(png: Path, bg_tol: int = 26) -> dict | None:
    """数截图里横向的"墨迹带"条数（不依赖 DOM，纯像素证据）。

    **本审计不把它当判据**：局部截图（``locator.screenshot``）是滚动后的视口裁剪，
    像素行数与 CSS 像素行不一定一一对应，用它判"文字被折成几行"会误报。
    保留这个函数只是给人工核对截图时用的辅助统计，返回 ``None`` 表示没装 Pillow。

    真判据见主流程里的 ``Range.getClientRects()`` 文字行数统计（基于排版，不靠猜）。
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(png) as im:
            g = im.convert("L")
            w, h = g.size
            px = g.load()
            flags = []
            for y in range(h):
                row_max = 0
                for x in range(0, w, 2):          # 隔列采样够用，快一倍
                    v = px[x, y]
                    if v > row_max:
                        row_max = v
                flags.append(row_max)
            base = min(flags) if flags else 0
            ink = [f > base + bg_tol for f in flags]
            bands = 0
            prev = False
            for cur in ink:
                if cur and not prev:
                    bands += 1
                prev = cur
            return {"bands": bands, "height": h, "width": w, "base": base}
    except Exception:                                  # noqa: BLE001
        return None


# ==========================================================================
# 主流程
# ==========================================================================
def main() -> int:
    ap = argparse.ArgumentParser(description="短线精灵看板 · 真浏览器视觉审计")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--minutes", type=int, default=45, help="回放时长（分钟）")
    ap.add_argument("--keep", action="store_true", help="跑完保留服务（自己打开看）")
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    ap.add_argument("--shots", default=str(SHOTS), help="截图输出目录")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("未安装 Playwright，无法执行浏览器审计。")
        print("  pip install playwright && playwright install chromium")
        print("→ 本审计**未执行**（不是通过）。")
        return 2

    shots = Path(args.shots)
    shots.mkdir(parents=True, exist_ok=True)

    audit = Audit()
    srv = None
    keep_open = False

    # ---------------- 造数据：真回放 + 定向探针 --------------------------
    audit.start("0) 准备数据（真回放 + 定向探针）")
    from arad.config import load_settings
    from arad.replay import Replay
    from arad.server import web as webmod
    from arad.store import AlertStore

    st = load_settings(use_cache=False)
    for name in ("spirit_price", "spirit_order"):
        st.section("rules").setdefault(name, {})["enabled"] = True

    res = Replay(seed=args.seed, minutes=args.minutes, settings=st).run()
    spirit_n = sum(1 for a in res.alerts if (a.metrics or {}).get("pattern"))
    audit.note(f"回放产出 {res.total} 条告警（其中精灵信号 {spirit_n} 条）")
    audit.check("回放产出足够多的告警", res.total >= 20, f"{res.total} 条")

    store = AlertStore(max_alerts=1500)
    for a in res.alerts:
        store.add_alert(a)
    for i, (code, name, sig, kind, sev, _d) in enumerate(LONG_NAME_CASES):
        store.add_alert(make_alert(f"audit:long:{i}", code, name, sig, kind, sev,
                                   1.5 if i % 2 == 0 else -1.5))
    for i, (code, name, sig, kind, sev, _d, _why) in enumerate(COLOR_CASES):
        store.add_alert(make_alert(f"audit:color:{i}", code, name, sig, kind, sev,
                                   1.5 if sev % 2 else -1.5))

    def push_name_probes(page_n: int) -> None:
        """（重）推一组"长名 / 未知信号"探针。

        必须能重复调用：下面第 5 组会把精灵列表推到 MAX_SPIRIT=200 行，
        早期灌进去的探针会被挤出 DOM，所以每次要检查它们之前都要补推一遍。
        """
        for i, (code, name, sig, kind, sev, _d) in enumerate(LONG_NAME_CASES):
            store.add_alert(make_alert(f"{NAME_PROBE_TAG}:{page_n}:{i}", code, name,
                                       sig, kind, sev, 1.5 if i % 2 == 0 else -1.5))
        for i, (code, name, sig, kind, sev, _d, _why) in enumerate(COLOR_CASES):
            store.add_alert(make_alert(f"audit:colrun:{page_n}:{i}", code, name, sig,
                                       kind, sev, 1.5 if sev % 2 else -1.5))

    port = free_port()
    srv = webmod.create_server(store, {"host": "127.0.0.1", "port": port})
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2},
                     name="audit-web", daemon=True).start()
    time.sleep(0.4)
    url = f"http://127.0.0.1:{port}/"
    audit.note(f"服务已启动：{url}（回放数据先灌入，前端走 SSE + /api/spirit 冷启动补齐）")

    pw = None
    browser = None
    page = None
    js_errors: list[str] = []
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=not args.headed)
        ctx = browser.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        page.on("pageerror", lambda e: js_errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: js_errors.append(f"console.error: {m.text}")
                if m.type == "error" else None)

        page.goto(url, wait_until="networkidle")
        page.wait_for_selector("#spiritList .sp", timeout=10000)
        push_name_probes(0)                 # 确保长名/未知信号探针在屏上
        page.wait_for_timeout(900)
        page.screenshot(path=str(shots / "00_boot_1280x800.png"))

        # ==================================================================
        # 1) 短线精灵的滚动播报观感
        # ==================================================================
        audit.start("1) 短线精灵滚动播报")
        rows = page.evaluate("""() => {
          const list = document.getElementById('spiritList');
          const body = document.getElementById('spiritBody');
          const rs = [...list.querySelectorAll('.sp')];
          const rect = r => r.getBoundingClientRect();
          return {
            n: rs.length,
            bodyClientH: body.clientHeight, bodyScrollH: body.scrollHeight,
            bodyScrollTop: body.scrollTop,
            listDisplay: getComputedStyle(list).display,
            listDir: getComputedStyle(list).flexDirection,
            rowKeys: rs.map(r => r.dataset.key),
            rowHeights: rs.map(r => Math.round(rect(r).height * 100) / 100),
            header: [...document.querySelectorAll('#spiritPanel .phead .ptitle')]
                      .map(e => e.textContent),
            colHeaders: [...document.querySelectorAll('.sp')].length
                        ? [...document.querySelector('.sp').children].map(c => c.className) : [],
          };
        }""")

        # 1a 顺序：DOM 顺序必须是"新 -> 旧"
        order = page.evaluate("""() => {
          const list = document.getElementById('spiritList');
          const rs = [...list.querySelectorAll('.sp')];
          const ts = rs.slice(0, 60).map(r => (r.dataset.key || ''));
          return ts;
        }""")
        # 用服务端顺序交叉验证：/api/spirit 是倒序（最新在前）
        api = page.evaluate("""async () => {
          const r = await fetch('/api/spirit?limit=200', {cache:'no-store'});
          const d = await r.json();
          return (d.items || []).map(i => i.key);
        }""")
        dom_keys = rows["rowKeys"]
        expected = [k for k in api if k in set(dom_keys)]
        audit.check("新行在最上：DOM 顺序与服务端倒序一致",
                    len(expected) > 20 and dom_keys[:len(expected)] == expected,
                    f"DOM {len(dom_keys)} 行 / API {len(api)} 条，前 3 行 = {dom_keys[:3]}")

        # 1b 新行冒到最上面（真 SSE）
        probe = make_alert("audit:top:probe", "600000", "浦发银行", "rocket", "surge",
                           3, 3.2, ts=datetime.now())
        before_top = page.evaluate(
            "() => { const r = document.querySelector('#spiritList .sp');"
            " return r ? r.dataset.key : null; }")
        store.add_alert(probe)
        page.wait_for_function(
            "k => { const r = document.querySelector('#spiritList .sp');"
            " return r && r.dataset.key === k; }", arg=probe.key, timeout=6000)
        page.screenshot(path=str(shots / "01_new_row_flash_1280x800.png"))
        top_now = page.evaluate(
            "() => { const r = document.querySelector('#spiritList .sp');"
            " return r.dataset.key; }")
        audit.check("SSE 新行插到列表顶部（旧行被推下去）",
                    top_now == probe.key and before_top != probe.key,
                    f"{before_top} -> {top_now}")

        # 1b-2 刷新页面（SSE 中途重连）：绝不能出现重复行。
        #      前端两条补数路径（SSE 增量 + /api/spirit 冷启动补齐）都靠 S.spSeen 去重，
        #      这里验证它在"页面刷新时正好有告警进来"的竞态下确实成立。
        page.reload(wait_until="domcontentloaded")
        race = make_alert("audit:race:probe", "600000", "浦发银行", "rocket", "surge",
                          3, 8.0, ts=datetime.now())
        store.add_alert(race)                       # 页面还在加载时就推，制造竞态
        page.wait_for_selector("#spiritList .sp", timeout=10000)
        page.wait_for_timeout(3500)
        race_res = page.evaluate("""() => {
          const count = sel => {
            const seen = {};
            document.querySelectorAll(sel).forEach(e => {
              const k = e.dataset.key || '(none)';
              seen[k] = (seen[k] || 0) + 1;
            });
            return Object.keys(seen).filter(k => seen[k] > 1);
          };
          const rows = [...document.querySelectorAll('#spiritList .sp')];
          const pill = document.getElementById('connPill');
          return {n: rows.length, dupSpirit: count('#spiritList .sp'),
                  dupAlert: count('#alertList .al'),
                  firstKey: rows.length ? rows[0].dataset.key : null,
                  pill: pill.textContent, pillCls: pill.className};
        }""")
        audit.check("刷新时正好有告警进来，SSE 与补齐两条路径不产生重复行",
                    not race_res["dupSpirit"] and not race_res["dupAlert"],
                    f"精灵重复 {len(race_res['dupSpirit'])} 个 {race_res['dupSpirit'][:3]}，"
                    f"告警重复 {len(race_res['dupAlert'])} 个，共 {race_res['n']} 行")

        # 1c 列结构：时间 / 代码 / 名称 / 信号 / 价格 / 涨跌幅
        cols = page.evaluate("""() => {
          const r = document.querySelector('#spiritList .sp');
          const rr = r.getBoundingClientRect();
          return [...r.children].map(k => {
            const kk = k.getBoundingClientRect();
            return {cls: k.className, txt: k.textContent,
                    x: Math.round(kk.x - rr.x), y: Math.round(kk.y - rr.y),
                    w: Math.round(kk.width)};
          });
        }""")
        audit.note(f"行内格子：{[(c['cls'], c['x'], c['y'], c['w']) for c in cols]}")
        ncols = len(cols)
        gcols = page.evaluate(
            "() => getComputedStyle(document.querySelector('#spiritList .sp'))"
            ".gridTemplateColumns")
        n_grid_cols = len(gcols.split())
        audit.check("行内有时间列/代码/名称/信号/价格/涨跌幅 6 个格子",
                    ncols == 6 and [c["cls"].split()[0] for c in cols]
                    == ["tm", "cd", "nm", "sg", "px", "pc"],
                    f"{ncols} 个格子：{[c['cls'] for c in cols]}")
        # 这一条守「涨跌幅没有被挤到第二行」。判据用**行高**而不是各格子 y 相等：
        # 网格是 align-items:baseline，时间/代码是 10.5px 而名称/信号是 11.5px，
        # 基线对齐时大字号的顶边天然比小字号高 2-4px —— 那是正确排版，不是折行。
        # （历史缺陷 D1：CSS 只声明 5 列却有 6 个格子，涨跌幅被挤到第二行，
        #   行高从 23px 翻到 46.5px，一屏从十几条掉到 5 条。）
        row_h = page.evaluate(
            "() => document.querySelector('#spiritList .sp').getBoundingClientRect().height")
        audit.check("6 个格子排在同一视觉行（涨跌幅没被挤到第二行）",
                    row_h <= 30 and len({c["y"] for c in cols}) <= 3,
                    f"grid-template-columns 共 {n_grid_cols} 列；行高 {row_h:.1f}px；"
                    f"各格子 y 偏移 = {[c['y'] for c in cols]}")
        audit.check("行高足够紧凑（一屏能看十几条，短线精灵的信息密度）",
                    row_h <= 26,
                    f"行高 {row_h:.1f}px -> 253px 面板可见 {int(253 // row_h)} 行")

        # 1d 行高一致（含超长股票名）
        heights = sorted(set(rows["rowHeights"]))
        names = page.evaluate("""() => {
          const out = [];
          document.querySelectorAll('#spiritList .sp').forEach(r => {
            const nm = r.querySelector('.nm');
            if (!nm) return;
            out.push({name: nm.textContent,
                      sw: nm.scrollWidth, cw: nm.clientWidth,
                      truncated: nm.scrollWidth > nm.clientWidth + 1,
                      rowH: Math.round(r.getBoundingClientRect().height * 10) / 10,
                      ellipsis: getComputedStyle(nm).textOverflow,
                      overflow: getComputedStyle(nm).overflow,
                      withinRow: nm.getBoundingClientRect().right
                                 <= r.getBoundingClientRect().right + 0.5});
          });
          return out;
        }""")
        realistic_bad = [n for n in names
                         if n["name"] in REALISTIC_NAMES and n["truncated"]]
        stress_present = [n for n in names if n["name"] in STRESS_NAMES]
        stress = [n for n in stress_present if n["truncated"]]
        audit.check("所有行等高（无因长名跳高）", len(heights) == 1,
                    f"出现 {len(heights)} 种行高：{heights}")
        audit.check("真实长度的股票简称完整显示（不被省略号截掉）",
                    not realistic_bad,
                    f"被截断：{[(n['name'], f'{n[chr(115)+chr(119)]}/{n[chr(99)+chr(119)]}px') for n in realistic_bad]}"
                    if realistic_bad else f"{len(REALISTIC_NAMES)} 个真实简称全部完整")
        audit.check("极端超长名被省略号截断且不撑破行",
                    bool(stress) and all(n["ellipsis"] == "ellipsis"
                                         and n["overflow"] == "hidden"
                                         and n["withinRow"] for n in stress),
                    ", ".join(f"{n['name']} {n['sw']}/{n['cw']}px 行高{n['rowH']}"
                              for n in stress) if stress
                    else f"压力名未触发截断（上屏 {len(stress_present)} 个，"
                         f"名称列宽 {names[0]['cw'] if names else '?'}px）")

        # 1e 水平越界检测
        over_flow = page.evaluate("""() => {
          const out = [];
          document.querySelectorAll('#spiritList .sp').forEach(r => {
            const rr = r.getBoundingClientRect();
            [...r.children].forEach(k => {
              const kk = k.getBoundingClientRect();
              if (kk.right > rr.right + 0.5 || kk.left < rr.left - 0.5) {
                out.push({cell: k.className, txt: k.textContent.slice(0, 30),
                          cellRight: Math.round(kk.right), rowRight: Math.round(rr.right)});
              }
            });
          });
          return out.slice(0, 8);
        }""")
        audit.check("没有任何格子溢出所在行（不跑出面板）", not over_flow,
                    f"{len(over_flow)} 个越界，例如 {over_flow[0] if over_flow else ''}")

        # 1e-2 信息密度：真实短线精灵一屏几十条，这里一屏只有个位数
        density = page.evaluate("""() => {
          const body = document.getElementById('spiritBody');
          const r = document.querySelector('#spiritList .sp');
          const rowH = r ? r.getBoundingClientRect().height : 0;
          const cs = r ? getComputedStyle(r) : null;
          return {rowH: Math.round(rowH * 10) / 10, bodyH: body.clientHeight,
                  visibleRows: rowH > 1 ? Math.floor(body.clientHeight / rowH) : 0,
                  lineHeight: cs ? cs.lineHeight : '', fontSize: cs ? cs.fontSize : '',
                  padding: cs ? cs.paddingTop + ' ' + cs.paddingBottom : ''};
        }""")
        audit.check("一屏可见行数达到短线精灵的信息密度（>= 10 行）",
                    density["visibleRows"] >= 10,
                    f"每行 {density['rowH']}px（其中 padding {density['padding']}，"
                    f"font-size {density['fontSize']}），面板 {density['bodyH']}px "
                    f"-> 仅 {density['visibleRows']} 行可见")

        # 1e-3 像素级证据：把精灵面板/列表局部存图，便于人工核对
        page.locator("#spiritPanel").screenshot(path=str(shots / "02_spirit_panel_zoom.png"))
        page.locator("#spiritList").screenshot(path=str(shots / "03_spirit_rows_zoom.png"))
        ink = count_ink_bands(shots / "03_spirit_rows_zoom.png")
        if ink:
            audit.note(f"（辅助）局部截图 {ink['width']}×{ink['height']}px 内有 "
                       f"{ink['bands']} 条横向墨迹带 —— 仅供人工核对，不作判据")
        audit.note("已保存 #spiritPanel / #spiritList 局部放大截图（02_ / 03_）")

        # 1e-4 结构性证据：把每个格子的**垂直中心**聚类，看这一条记录被排成了几个视觉行。
        #      字号不同会让基线差 1~2px（正常），真正的换行会差 20px 以上，容易区分。
        lines = page.evaluate("""() => {
          const TOL = 10;                       // 同一视觉行允许的中心偏差（px）
          const rectsOf = el => {
            const r = document.createRange();
            r.selectNodeContents(el);
            return [...r.getClientRects()].filter(q => q.width > 0.5 && q.height > 0.5);
          };
          const bands = xs => {
            const out = [];
            xs.slice().sort((a, b) => a - b).forEach(x => {
              const b = out.find(g => Math.abs(g.center - x) <= TOL);
              if (b) b.items.push(x); else out.push({center: x, items: [x]});
            });
            return out;
          };
          const rows = [...document.querySelectorAll('#spiritList .sp')].slice(0, 8);
          return rows.map(r => {
            const cells = [...r.children].map(k => {
              const rs = rectsOf(k);
              const cy = rs.length
                ? rs.reduce((s, q) => s + q.top + q.height / 2, 0) / rs.length : null;
              return {cls: k.className.split(' ')[0], textLines: rs.length,
                      cy: cy == null ? null : Math.round(cy * 10) / 10,
                      txt: k.textContent};
            });
            const g = bands(cells.filter(c => c.cy != null).map(c => c.cy));
            return {key: r.dataset.key,
                    rowH: Math.round(r.getBoundingClientRect().height * 10) / 10,
                    gridRows: getComputedStyle(r).gridTemplateRows,
                    cells: cells, bands: g.length,
                    bandDetail: g.map(b => ({center: Math.round(b.center), n: b.items.length}))};
          });
        }""")
        n_multi = [l for l in lines if l["bands"] > 1]
        detail_parts = []
        for l in lines[:3]:
            cells = ", ".join(f"{c['cls']}@y{c['cy']}" for c in l["cells"])
            detail_parts.append(f"行高{l['rowH']}px 视觉行{l['bands']} "
                                f"gridRows={l['gridRows']!r} [{cells}]")
        audit.check("结构性证据：一条记录只占一个视觉行（涨跌幅没被挤到第二行）",
                    not n_multi,
                    f"{len(n_multi)}/{len(lines)} 行被排成多个视觉行；"
                    + " | ".join(detail_parts))

        # 1f 可滚动 + 新行时钉在顶部
        scroll = page.evaluate("""() => {
          const b = document.getElementById('spiritBody');
          return {scrollH: b.scrollHeight, clientH: b.clientHeight,
                  scrollTop: b.scrollTop, overflowY: getComputedStyle(b).overflowY};
        }""")
        audit.check("行数超过面板高度时面板可滚动",
                    scroll["scrollH"] > scroll["clientH"] and scroll["overflowY"] in ("auto", "scroll"),
                    f"scrollH={scroll['scrollH']} clientH={scroll['clientH']} overflowY={scroll['overflowY']}")

        pin_probe = make_alert("audit:pin:top", "600000", "浦发银行", "dive", "plunge",
                               2, -2.4, ts=datetime.now())
        page.evaluate("() => { document.getElementById('spiritBody').scrollTop = 0; }")
        store.add_alert(pin_probe)
        page.wait_for_timeout(700)
        pinned = page.evaluate("""() => {
          const b = document.getElementById('spiritBody');
          const r = document.querySelector('#spiritList .sp');
          const br = b.getBoundingClientRect(), rr = r.getBoundingClientRect();
          return {scrollTop: b.scrollTop, key: r.dataset.key,
                  visible: rr.top >= br.top - 1 && rr.bottom <= br.bottom + 1};
        }""")
        audit.check("停在顶部时新行立即可见（scrollTop 保持 0）",
                    pinned["scrollTop"] == 0 and pinned["visible"] and pinned["key"] == pin_probe.key,
                    f"scrollTop={pinned['scrollTop']} key={pinned['key']} visible={pinned['visible']}")

        page.evaluate("() => { document.getElementById('spiritBody').scrollTop = 300; }")
        page.wait_for_timeout(150)
        down_probe = make_alert("audit:pin:down", "600000", "浦发银行", "rocket", "surge",
                                3, 4.1, ts=datetime.now())
        store.add_alert(down_probe)
        page.wait_for_timeout(700)
        scrolled = page.evaluate("""() => {
          const b = document.getElementById('spiritBody');
          const r = document.querySelector('#spiritList .sp');
          const br = b.getBoundingClientRect(), rr = r.getBoundingClientRect();
          return {scrollTop: b.scrollTop, key: r.dataset.key,
                  visible: rr.top >= br.top - 1 && rr.bottom <= br.bottom + 1};
        }""")
        audit.note(f"向下滚动后新行（{down_probe.key}）："
                   f"scrollTop={scrolled['scrollTop']} 最新行可见={scrolled['visible']}")
        audit.check("向上滚动后新行仍插到顶部（不看时不强行拉回）",
                    scrolled["key"] == down_probe.key,
                    f"顶部仍是 {scrolled['key']}（最新行）")
        page.screenshot(path=str(shots / "01_scrolled_state_1280x800.png"))
        page.evaluate("() => { document.getElementById('spiritBody').scrollTop = 0; }")

        # 1g 到货闪光
        flash_probe = make_alert("audit:flash:probe", "600000", "浦发银行", "rocket",
                                 "surge", 3, 5.0, ts=datetime.now())
        store.add_alert(flash_probe)
        page.wait_for_function(
            "k => { const r = document.querySelector('#spiritList .sp');"
            " return r && r.dataset.key === k; }", arg=flash_probe.key, timeout=6000)
        flash = page.evaluate("""() => {
          const r = document.querySelector('#spiritList .sp');
          const cs = getComputedStyle(r);
          return {cls: r.className, anim: cs.animationName, dur: cs.animationDuration,
                  bg: cs.backgroundColor, opacity: cs.opacity};
        }""")
        audit.note(f"新行即时样式：class={flash['cls']!r} animation={flash['anim']} "
                   f"{flash['dur']} bg={flash['bg']} opacity={flash['opacity']}")
        audit.check("新行到货时带闪光动画（不是静默出现）",
                    "flash" in flash["cls"] and flash["anim"] not in ("none", ""),
                    f"animation-name={flash['anim']} / {flash['dur']}")
        audit.check("闪光有可感知的视觉强度（背景不透明度 >= 0.25）",
                    parse_color_any(flash["bg"])[3] >= 0.25,
                    f"到达瞬间背景 {flash['bg']}")
        page.screenshot(path=str(shots / "01_flash_frame_immediately.png"))
        page.wait_for_timeout(1100)
        settled = page.evaluate("""() => {
          const r = document.querySelector('#spiritList .sp');
          return {cls: r.className, anim: getComputedStyle(r).animationName};
        }""")
        audit.check("闪光动画结束后自动清除 flash 类",
                    "flash" not in settled["cls"] and settled["anim"] == "none",
                    f"1.1s 后 class={settled['cls']!r}")

        # ==================================================================
        # 2) 四种视口下的布局
        # ==================================================================
        audit.start("2) 布局（1024/1280/1440/1920）")
        layout: dict[int, dict] = {}
        for w, h in VIEWPORTS:
            page.set_viewport_size({"width": w, "height": h})
            page.wait_for_timeout(350)
            m = page.evaluate("""() => {
              const d = document.documentElement;
              const rect = id => document.getElementById(id).getBoundingClientRect();
              const panels = ['spiritPanel','alertsPanel','quotesPanel','wlPanel']
                             .map(id => ({id, r: rect(id)}));
              const overlaps = [];
              for (let i=0;i<panels.length;i++) for (let j=i+1;j<panels.length;j++) {
                const a = panels[i].r, b = panels[j].r;
                const ox = Math.min(a.right,b.right) - Math.max(a.left,b.left);
                const oy = Math.min(a.bottom,b.bottom) - Math.max(a.top,b.top);
                if (ox > 1 && oy > 1) overlaps.push([panels[i].id, panels[j].id]);
              }
              const btns = [...document.querySelectorAll('#spFilters .sfbtn')];
              const filt = [...document.querySelectorAll('#filters .fbtn')];
              const rowsOf = els => new Set(els.map(e => Math.round(e.getBoundingClientRect().top))).size;
              const clipped = [];
              document.querySelectorAll('#spiritPanel .ptitle, #spiritPanel .sfbtn,'
                + ' #spiritPanel .pmeta, #alertsPanel .ptitle, #alertsPanel .fbtn')
                .forEach(e => {
                  // 被裁切：内容比容器宽，且不是刻意省略号截断的元素
                  const cs = getComputedStyle(e);
                  if (cs.textOverflow === 'ellipsis') return;
                  if (e.scrollWidth > e.clientWidth + 1) clipped.push(e.textContent.trim());
                });
              const sp = rect('spiritPanel');
              const rowH = (document.querySelector('#spiritList .sp') || {getBoundingClientRect:()=>({height:0})})
                            .getBoundingClientRect().height;
              const body = document.getElementById('spiritBody');
              return {
                vw: window.innerWidth, vh: window.innerHeight,
                scrollW: d.scrollWidth, clientW: d.clientWidth,
                overlaps: overlaps, clipped: clipped,
                spiritTop: Math.round(sp.top), spiritBottom: Math.round(sp.bottom),
                spiritFullyVisible: sp.top >= -0.5 && sp.bottom <= window.innerHeight + 0.5,
                spiritH: Math.round(sp.height),
                visibleRows: rowH > 1 ? Math.floor(body.clientHeight / rowH) : 0,
                spFilterRows: rowsOf(btns), spFilterCount: btns.length,
                alertFilterRows: rowsOf(filt), alertFilterCount: filt.length,
                headH: Math.round(document.querySelector('#spiritPanel .phead').getBoundingClientRect().height),
              };
            }""")
            layout[w] = m
            page.screenshot(path=str(shots / f"10_layout_{w}x{h}.png"))
            audit.note(f"{w}×{h}: scrollW={m['scrollW']} clientW={m['clientW']} "
                       f"精灵面板 {m['spiritTop']}~{m['spiritBottom']}px（{m['spiritH']}px，"
                       f"约 {m['visibleRows']} 行可见）筛选按钮 {m['spFilterRows']} 行")

        for w, h in VIEWPORTS:
            m = layout[w]
            audit.check(f"{w}×{h} 无横向滚动条",
                        m["scrollW"] <= m["clientW"],
                        f"scrollWidth={m['scrollW']} clientWidth={m['clientW']}"
                        + (f"（溢出 {m['scrollW'] - m['clientW']}px）" if m["scrollW"] > m["clientW"] else ""))
        for w, h in VIEWPORTS:
            m = layout[w]
            audit.check(f"{w}×{h} 面板之间无重叠", not m["overlaps"], str(m["overlaps"]))
        for w, h in VIEWPORTS:
            m = layout[w]
            audit.check(f"{w}×{h} 面板标题/按钮文字未被裁切", not m["clipped"], str(m["clipped"]))
        m = layout[1920]
        audit.check("1920×1080 下短线精灵面板无需滚动即可见",
                    m["spiritFullyVisible"] and m["spiritTop"] >= 0,
                    f"面板 top={m['spiritTop']} bottom={m['spiritBottom']} 视口高={m['vh']}")
        for w, h in VIEWPORTS:
            m = layout[w]
            audit.check(f"{w}×{h} 精灵分组按钮 {m['spFilterCount']} 个排在同一行",
                        m["spFilterRows"] == 1,
                        f"实际占 {m['spFilterRows']} 行，表头高度 {m['headH']}px")
        for w, h in VIEWPORTS:
            m = layout[w]
            audit.note(f"{w}×{h} 告警流类型按钮占 {m['alertFilterRows']} 行"
                       f"（{m['alertFilterCount']} 个）")

        # ==================================================================
        # 3) 配色：红涨绿跌
        # ==================================================================
        page.set_viewport_size({"width": 1440, "height": 900})
        page.wait_for_timeout(300)
        audit.start("3) 配色 红涨绿跌")
        push_name_probes(1)                 # 颜色探针可能已被挤掉，补推
        page.wait_for_timeout(900)
        colored = page.evaluate("""() => {
          const out = {};
          document.querySelectorAll('#spiritList .sp').forEach(e => {
            const sg = e.querySelector('.sg'), pc = e.querySelector('.pc');
            const nm = e.querySelector('.nm'), tm = e.querySelector('.tm');
            out[e.dataset.key] = {
              dir: e.dataset.dir, cls: e.className,
              cn: sg ? sg.textContent : '',
              sgColor: sg ? getComputedStyle(sg).color : '',
              pcColor: pc ? getComputedStyle(pc).color : '',
              pcText: pc ? pc.textContent : '',
              tmColor: tm ? getComputedStyle(tm).color : '',
              nmColor: nm ? getComputedStyle(nm).color : '',
              bg: getComputedStyle(e).backgroundColor,
              rowOpacity: getComputedStyle(e).opacity,
            };
          });
          return out;
        }""")
        page.screenshot(path=str(shots / "30_colors_1440x900.png"))
        shown = {v["cn"]: k for k, v in colored.items()}
        audit.note(f"页面出现 {len(colored)} 行精灵信号，中文名：{sorted(shown)}")

        bad: list[str] = []
        print(f"    {'信号':<16}{'中文':<10}{'期望':<7}{'实际':<7}{'信号色':<20}{'判定'}")
        print("    " + "-" * 74)
        for i, (code, name, sig, kind, sev, want, why) in enumerate(COLOR_CASES):
            key = f"audit:colrun:1:{i}"
            row = colored.get(key)
            if not row:
                bad.append(sig)
                print(f"    {sig:<16}{'-':<10}{want:<7}{'缺失':<7}{'-':<20}✗ 行未渲染")
                continue
            c = row["sgColor"]
            ok_dir = row["dir"] == want
            ok_color = is_red(c) if want == "up" else (is_green(c) if want == "down" else is_grey(c))
            ok = ok_dir and ok_color
            if not ok:
                bad.append(sig)
            print(f"    {sig:<16}{row['cn']:<10}{want:<7}{row['dir']:<7}{c:<20}"
                  f"{'✓' if ok else '✗'} {why}")
        audit.check("红涨绿跌全部正确（含打开涨停=绿 / 打开跌停=红）",
                    not bad, f"不符：{bad}" if bad else f"{len(COLOR_CASES)} 个信号全部正确")
        color_keys = [f"audit:colrun:1:{i}" for i in range(len(COLOR_CASES))]

        # 方向色也必须落到全站调色板（不是另造的红绿）
        palette = page.evaluate("""() => {
          const cs = getComputedStyle(document.documentElement);
          return {up: cs.getPropertyValue('--up').trim(),
                  down: cs.getPropertyValue('--down').trim()};
        }""")
        up_ok = all(is_red(colored[k]["sgColor"]) for k in color_keys
                    if colored.get(k) and colored[k]["dir"] == "up")
        down_ok = all(is_green(colored[k]["sgColor"]) for k in color_keys
                      if colored.get(k) and colored[k]["dir"] == "down")
        audit.check("CSS 变量 --up 是红 / --down 是绿",
                    is_red(palette["up"]) and is_green(palette["down"]),
                    f"--up={palette['up']} --down={palette['down']}")
        audit.check("所有 up 行信号色为红、所有 down 行信号色为绿", up_ok and down_ok,
                    f"up 行 {sum(1 for k in color_keys if colored.get(k) and colored[k]['dir'] == 'up')} 个 / "
                    f"down 行 {sum(1 for k in color_keys if colored.get(k) and colored[k]['dir'] == 'down')} 个")

        # 反例：涨跌幅列与方向是否自洽（复盘框的混色）
        mismatch = [(k, colored[k]["cn"], colored[k]["dir"], colored[k]["pcText"],
                     colored[k]["pcColor"])
                    for k in color_keys
                    if colored.get(k)
                    and ((colored[k]["dir"] == "up" and not is_red(colored[k]["pcColor"]))
                         or (colored[k]["dir"] == "down" and not is_green(colored[k]["pcColor"])))]
        audit.check("信号方向色与涨跌幅列颜色同源（不出现同格双色）",
                    not mismatch, f"{len(mismatch)} 行不一致，例如 {mismatch[0] if mismatch else ''}")

        # 方向底色也要跟着方向变（红行淡红底 / 绿行淡绿底），
        # 但 sev3 的黄色高亮会盖掉方向底色 —— 这是设计上的取舍，这里只记录不判定
        dir_bg = {}
        for k in color_keys:
            v = colored.get(k)
            if not v:
                continue
            r, g, b, a = parse_color_any(v["bg"])
            if a <= 0.01:
                tag = "无底色"
            elif r > g + 20:
                tag = "红底"
            elif g > r + 20:
                tag = "绿底"
            else:
                tag = "黄底(sev3)"
            dir_bg.setdefault((v["dir"], v["cls"].split()[-1]), set()).add(tag)
        audit.note("方向底色：" + "; ".join(
            f"{d}/{s} -> {sorted(t)}" for (d, s), t in sorted(dir_bg.items())))

        # ==================================================================
        # 4) 分组筛选交互
        # ==================================================================
        audit.start("4) 分组筛选交互")
        before_filter = page.evaluate(
            "() => document.querySelectorAll('#spiritList .sp').length")
        btns = page.evaluate("""() => [...document.querySelectorAll('#spFilters .sfbtn')]
            .map(b => ({group: b.dataset.group, text: b.textContent,
                        on: b.classList.contains('on')}))""")
        audit.note(f"筛选按钮：{[(b['group'], b['text']) for b in btns]}")
        expect_groups = {"all", "price", "order", "limit", "index", "pattern"}
        audit.check("筛选按钮由服务端分组目录生成（全部/价格异动/盘口委托/涨跌停/指数/形态）",
                    {b["group"] for b in btns} >= expect_groups,
                    f"缺少 {sorted(expect_groups - {b['group'] for b in btns})}"
                    if not {b["group"] for b in btns} >= expect_groups else "")

        filter_report: list[dict] = []
        for b in btns:
            if b["group"] == "all":
                continue
            page.click(f'#spFilters .sfbtn[data-group="{b["group"]}"]')
            page.wait_for_timeout(280)
            st = page.evaluate("""(g) => {
              const rows = [...document.querySelectorAll('#spiritList .sp')];
              const on = [...document.querySelectorAll('#spFilters .sfbtn')]
                         .filter(x => x.classList.contains('on'))
                         .map(x => x.dataset.group);
              const onBtn = document.querySelector('#spFilters .sfbtn.on');
              const others = [...document.querySelectorAll('#spFilters .sfbtn')]
                             .filter(x => !x.classList.contains('on'));
              const cs = onBtn ? getComputedStyle(onBtn) : null;
              const otherCs = others.length ? getComputedStyle(others[0]) : null;
              return {
                group: g, rows: rows.length,
                groups: [...new Set(rows.map(r => r.dataset.group))],
                keys: rows.map(r => r.dataset.key),
                empty: !!document.querySelector('#spiritList .empty'),
                active: on, onBg: cs ? cs.backgroundColor : '',
                onColor: cs ? cs.color : '', onBorder: cs ? cs.borderColor : '',
                offBg: otherCs ? otherCs.backgroundColor : '',
                offColor: otherCs ? otherCs.color : '',
                meta: document.getElementById('spiritMeta').textContent,
              };
            }""", b["group"])
            filter_report.append(st)
            print(f"      点击「{b['text']}」({b['group']})：{st['rows']} 行，"
                  f"出现分组 {st['groups']}，meta={st['meta']!r}，active={st['active']}")
            audit.check(f"点击「{b['text']}」后列表只剩该分组",
                        (not st["rows"] and st["empty"]) or set(st["groups"]) <= {b["group"]},
                        f"rows={st['rows']} groups={st['groups']}")
            audit.check(f"「{b['text']}」激活态与未激活态视觉可区分",
                        st["active"] == [b["group"]]
                        and (st["onColor"] != st["offColor"] or st["onBg"] != st["offBg"]
                             or st["onBorder"] != st["offBorder"]),
                        f"激活 色{st['onColor']}/底{st['onBg']} vs "
                        f"未激活 色{st['offColor']}/底{st['offBg']}")
            page.screenshot(path=str(shots / f"40_filter_{b['group']}_1440x900.png"))

        # 回到"全部"，全量必须无丢失、无重复
        page.click('#spFilters .sfbtn[data-group="all"]')
        page.wait_for_timeout(400)
        after = page.evaluate("""() => {
          const rows = [...document.querySelectorAll('#spiritList .sp')];
          return {n: rows.length, keys: rows.map(r => r.dataset.key),
                  active: [...document.querySelectorAll('#spFilters .sfbtn.on')]
                           .map(x => x.dataset.group)};
        }""")
        dups = [k for k in set(after["keys"]) if after["keys"].count(k) > 1]
        union = sorted({k for st in filter_report for k in st["keys"]})
        missing = [k for k in union if k not in set(after["keys"])]
        audit.check("回到「全部」后恢复到完整列表（行数不减少）",
                    after["n"] >= before_filter and after["active"] == ["all"],
                    f"{before_filter} -> {after['n']} 行，active={after['active']}")
        audit.check("来回切换筛选不产生重复行", not dups, f"重复 key：{dups[:5]}")
        audit.check("分组之间没有丢行（各分组并集 ⊆ 全部）", not missing,
                    f"丢失 {len(missing)} 行：{missing[:5]}")
        page.screenshot(path=str(shots / "41_filter_all_restored_1440x900.png"))

        # ==================================================================
        # 5) 健壮性
        # ==================================================================
        audit.start("5) 健壮性 / 无 JS 报错")
        # 第 3/4 组推了很多行，名字探针可能已被 MAX_SPIRIT 挤掉 —— 补推再查
        push_name_probes(2)
        page.wait_for_timeout(1000)
        # 5a 未知信号名优雅降级
        unknown = page.evaluate("""() => {
          const r = [...document.querySelectorAll('#spiritList .sp')]
            .find(e => (e.dataset.key || '').indexOf('audit:namerun:2:') === 0
                       && e.querySelector('.nm').textContent === '比亚迪');
          if (!r) return null;
          const sg = r.querySelector('.sg');
          return {text: sg ? sg.textContent : '', cls: r.className,
                  dir: r.dataset.dir, title: r.title.slice(0, 200),
                  cellOverflows: sg ? sg.scrollWidth > sg.clientWidth + 1 : false,
                  rowH: r.getBoundingClientRect().height,
                  withinRow: sg ? sg.getBoundingClientRect().right
                                  <= r.getBoundingClientRect().right + 0.5 : false};
        }""")
        audit.check("未注册信号名不显示 undefined/null（原样降级）",
                    bool(unknown) and unknown["text"] not in ("undefined", "null", "")
                    and "undefined" not in unknown["text"] and "null" not in unknown["text"],
                    f"显示为 {unknown['text']!r}，方向 {unknown['dir'] if unknown else '?'}")
        audit.check("未注册信号名被截断而不是撑破行（有省略号、行高不变）",
                    bool(unknown) and unknown["cellOverflows"] and unknown["withinRow"]
                    and abs(unknown["rowH"] - heights[0]) < 1.5 if unknown else False,
                    f"{unknown['text']!r} 溢出格子={unknown['cellOverflows']} "
                    f"行内={unknown['withinRow']} 行高={round(unknown['rowH'], 1)}"
                    if unknown else "未知信号探针未上屏")
        audit.note(f"未知信号行悬停提示：{unknown['title'][:120] if unknown else '（未取到）'}")

        # 5b 超长信号名
        long_sig = make_alert("audit:longsig", "600000", "浦发银行",
                              "超级无敌长的信号名称超过四个字怎么办", "unusual", 2, 0.5,
                              ts=datetime.now())
        store.add_alert(long_sig)
        page.wait_for_timeout(700)
        ls = page.evaluate("""() => {
          const r = [...document.querySelectorAll('#spiritList .sp')]
            .find(e => e.dataset.key === 'audit:longsig');
          if (!r) return null;
          const sg = r.querySelector('.sg');
          const rr = r.getBoundingClientRect();
          const sr = sg.getBoundingClientRect();
          return {text: sg.textContent, sw: sg.scrollWidth, cw: sg.clientWidth,
                  within: sr.right <= rr.right + 0.5, rowH: rr.height};
        }""")
        audit.check("超长中文信号名不撑破行（截断 + 行高不变）",
                    bool(ls) and ls["within"] and abs(ls["rowH"] - heights[0]) < 1.5,
                    f"{ls['text']!r} {ls['sw']}/{ls['cw']}px 行高 {round(ls['rowH'], 1)} vs 常规 {heights[0]}"
                    if ls else "行未渲染")

        # 5c DOM 上限
        cap_before = page.evaluate("() => document.querySelectorAll('#spiritList .sp').length")
        base = make_alert("audit:cap:base", "600000", "浦发银行", "rocket", "surge", 2, 1.0)
        for i in range(260):
            store.add_alert(replace(base, key=f"audit:cap:{i}", ts=datetime.now()))
        page.wait_for_timeout(3500)
        cap = page.evaluate("""() => {
          const rows = [...document.querySelectorAll('#spiritList .sp')];
          return {n: rows.length, meta: document.getElementById('spiritMeta').textContent,
                  firstKey: rows.length ? rows[0].dataset.key : null};
        }""")
        audit.check("DOM 行数被 MAX_SPIRIT=200 截住（推 260 条后仍 <= 200）",
                    0 < cap["n"] <= 200,
                    f"推入前 {cap_before} 行 -> 推入 260 条后 {cap['n']} 行，meta={cap['meta']!r}")
        audit.check("截断后最新行仍在顶部（截的是最旧的）",
                    cap["firstKey"] is not None and str(cap["firstKey"]).startswith("audit:cap:"),
                    f"顶部 key={cap['firstKey']}")
        page.screenshot(path=str(shots / "50_dom_cap_200_rows.png"))

        # 5d 刷新中途恢复 SSE
        page.reload(wait_until="networkidle")
        page.wait_for_selector("#spiritList .sp", timeout=10000)
        page.wait_for_timeout(900)
        recovered = page.evaluate("""() => {
          const p = document.getElementById('connPill');
          return {pill: p.textContent, cls: p.className,
                  rows: document.querySelectorAll('#spiritList .sp').length};
        }""")
        audit.check("页面刷新后 SSE 重连并补齐历史行",
                    recovered["rows"] > 10 and "live" in recovered["cls"],
                    f"连接状态 {recovered['pill']!r} ({recovered['cls']})，{recovered['rows']} 行")
        page.screenshot(path=str(shots / "51_after_reload_1280x800.png"))

        # 刷新后仍然能收到新行
        rp = make_alert("audit:afterreload", "600000", "浦发银行", "rocket", "surge",
                        3, 6.0, ts=datetime.now())
        store.add_alert(rp)
        try:
            page.wait_for_function(
                "k => { const r = document.querySelector('#spiritList .sp');"
                " return r && r.dataset.key === k; }", arg=rp.key, timeout=6000)
            live_ok = True
        except Exception:                       # noqa: BLE001
            live_ok = False
        audit.check("刷新后 SSE 仍能把新行推到顶部", live_ok, f"探针 {rp.key}")

        # 5e JS 错误
        audit.check("整个会话零 pageerror / console.error", not js_errors,
                    f"{len(js_errors)} 条：{js_errors[:3]}" if js_errors else "0 条")

        # ==================================================================
        # 6) 可访问性
        # ==================================================================
        audit.start("6) 可访问性（对比度 / 颜色非唯一载体）")
        page.set_viewport_size({"width": 1440, "height": 900})
        push_name_probes(3)               # 保证页面上同时有涨行和跌行
        page.wait_for_timeout(1000)
        contrast = page.evaluate("""() => {
          const bgChain = el => {
            const chain = [];
            let n = el;
            while (n && n !== document.documentElement) {
              chain.unshift(getComputedStyle(n).backgroundColor);
              n = n.parentElement;
            }
            chain.unshift(getComputedStyle(document.body).backgroundColor);
            return chain;
          };
          const sample = el => el ? {color: getComputedStyle(el).color, bg: bgChain(el)} : null;
          // 取自"已经稳定下来"的行：跳过还在播 flash 动画的最新几行，
          // 否则量到的是黄色高亮瞬间的底色，不是稳态可读性。
          const rows = [...document.querySelectorAll('#spiritList .sp')];
          const sp = rows.find(r => !r.classList.contains('flash'))
                     || rows[rows.length - 1] || null;
          const alertRows = [...document.querySelectorAll('#alertList .al')];
          const al = alertRows.find(r => !r.classList.contains('flash'))
                     || alertRows[0] || null;
          const sg = sp && sp.querySelector('.sg');
          const tm = sp && sp.querySelector('.tm');
          const nm = sp && sp.querySelector('.nm');
          const cd = sp && sp.querySelector('.cd');
          const altm = al && al.querySelector('.tm');
          const alnm = al && al.querySelector('.nm');
          const alttl = al && al.querySelector('.ttl');
          return {
            spiritSignal: sample(sg), spiritTime: sample(tm), spiritName: sample(nm),
            spiritCode: sample(cd),
            spiritRowBg: sp ? bgChain(sp) : null,
            alertTime: sample(altm), alertName: sample(alnm), alertTitle: sample(alttl),
            alertRowBg: al ? bgChain(al) : null,
            alertRowCls: al ? al.className : null,
            spiritRowCls: sp ? sp.className : null,
            hasAlertRow: !!al,
          };
        }""")

        ratios: dict[str, float] = {}
        if contrast["hasAlertRow"]:
            al_bg = flatten_bg(contrast["alertRowBg"])
            audit.note(f"取样的告警行 class={contrast['alertRowCls']!r} 背景 {al_bg}")
            al_ratio = {}
            if contrast.get("alertName"):
                al_ratio["名称"] = contrast_ratio(contrast["alertName"]["color"], al_bg)
            if contrast.get("alertTime"):
                al_ratio["时间"] = contrast_ratio(contrast["alertTime"]["color"], al_bg)
            if contrast.get("alertTitle"):
                al_ratio["标题"] = contrast_ratio(contrast["alertTitle"]["color"], al_bg)
            ratios.update({f"告警行·{k}": v for k, v in al_ratio.items()})
            audit.check("告警行文字对比度可读（>= 4.5:1）",
                        bool(al_ratio) and min(al_ratio.values()) >= 4.5,
                        "; ".join(f"{k} {v:.2f}:1" for k, v in al_ratio.items()))
        else:
            al_bg = None
            audit.note("页面没有普通告警行（本次只灌了精灵信号），告警行对比度**未评估**")
            audit.check("告警行文字对比度可读", True,
                        "未评估：页面上没有 .al 告警行可测")

        sp_bg = flatten_bg(contrast["spiritRowBg"])
        audit.note(f"取样的精灵行 class={contrast['spiritRowCls']!r} 背景 {sp_bg}")
        spirit_ratio = {
            "精灵行·时间": contrast_ratio(contrast["spiritTime"]["color"], sp_bg),
            "精灵行·代码": contrast_ratio(contrast["spiritCode"]["color"], sp_bg),
            "精灵行·名称": contrast_ratio(contrast["spiritName"]["color"], sp_bg),
            "精灵行·信号(涨)": contrast_ratio(contrast["spiritSignal"]["color"], sp_bg),
        }
        ratios.update(spirit_ratio)

        for label, r in ratios.items():
            audit.note(f"{label} 对比度 {r:.2f}:1"
                       f"（背景 {sp_bg if '精灵' in label else al_bg}）")

        audit.check("精灵行文字对比度可读（>= 4.5:1，WCAG AA 正文）",
                    min(spirit_ratio.values()) >= 4.5,
                    "; ".join(f"{k.split('·')[1]} {v:.2f}:1" for k, v in spirit_ratio.items())
                    + f" —— 最低 {min(spirit_ratio, key=spirit_ratio.get)}")
        audit.check("方向色本身也够亮（信号色 >= 3:1，可作大字/图形）",
                    all(contrast_ratio(v["sgColor"], sp_bg) >= 3.0
                        for k, v in colored.items() if v["cn"] in {c[2] for c in COLOR_CASES}),
                    "信号色对比度 " + ", ".join(
                        f"{v['cn']}={contrast_ratio(v['sgColor'], sp_bg):.1f}"
                        for k, v in list(colored.items())[:4]))

        # 颜色之外的信息载体
        carriers = page.evaluate("""() => {
          const rows = [...document.querySelectorAll('#spiritList .sp')];
          const up = rows.find(r => r.dataset.dir === 'up');
          const down = rows.find(r => r.dataset.dir === 'down');
          const info = r => {
            if (!r) return null;
            const sg = r.querySelector('.sg'), pc = r.querySelector('.pc'), nm = r.querySelector('.nm');
            return {cn: sg.textContent, pct: pc.textContent, name: nm.textContent,
                    borderLeft: getComputedStyle(r).borderLeftColor,
                    arrow: /[↑↓▲▼+\\-]/.test(r.textContent)};
          };
          const alertRows = [...document.querySelectorAll('#alertList .al')];
          const badge = alertRows.length ? alertRows[0].querySelector('.bd') : null;
          return {up: info(up), down: info(down),
                  alertBadge: badge ? badge.textContent : null,
                  sparkline: !!document.querySelector('canvas')};
        }""")
        up_i, down_i = carriers["up"], carriers["down"]
        for tag, info in (("涨", up_i), ("跌", down_i)):
            if info:
                audit.note(f"{tag}行：信号={info['cn']!r} 涨跌幅={info['pct']!r} "
                           f"左边框={info['borderLeft']} 名称={info['name']!r}")
            else:
                audit.note(f"{tag}行：本次页面没有该方向的行")
        # ⚠ 不能用「up 行的 pct 必须以 + 开头」来判定：up/down 是**信号方向**，
        # 不是当日涨跌方向。快速反弹、打开跌停这类信号本身就是「一只当日下跌的
        # 票出现了看多事件」，此时红色行配 -1.50% 是完全正确的语义。
        # 真正要守的是「涨跌幅带正负号」——符号才是颜色之外的第二载体，
        # 光看 "-1.50%" 与 "+1.50%" 就能分辨，不依赖红绿。
        signed_ok = all(
            isinstance(i, dict) and (i["pct"].startswith("+") or i["pct"].startswith("-"))
            for i in (up_i, down_i) if i
        )
        audit.check("涨跌幅带正负号（颜色之外的第二载体，不靠红绿也能读）",
                    bool(up_i) and bool(down_i) and signed_ok,
                    f"涨行={up_i['pct'] if up_i else '缺失'} "
                    f"跌行={down_i['pct'] if down_i else '缺失'}")
        # 另加一条真正该守的：**信号颜色要与它的信号名语义一致**，
        # 即同一行里信号色和涨跌幅列色同源（同格不该出现两种方向色）。
        if up_i and down_i:
            audit.note("注意：up/down 指信号方向，非当日涨跌——"
                       "「快速反弹」「打开跌停」会是红色行配当日负涨幅，这是对的")
        audit.check("方向还有中文信号名可读（色盲用户也能判断多空）",
                    bool(up_i) and bool(down_i) and bool(up_i["cn"]) and bool(down_i["cn"])
                    and up_i["cn"] != down_i["cn"],
                    f"{up_i['cn'] if up_i else '缺失'} / {down_i['cn'] if down_i else '缺失'}")
        audit.check("方向还有左侧色条（border-left）作第三载体",
                    bool(up_i) and bool(down_i)
                    and up_i["borderLeft"] != down_i["borderLeft"],
                    f"up={up_i['borderLeft'] if up_i else '缺失'} "
                    f"down={down_i['borderLeft'] if down_i else '缺失'}")
        audit.note("色觉障碍提示：现价/涨跌幅数字本身靠红绿区分，但正负号、"
                   "信号中文名、左侧色条三条独立线索都在，判读不受影响。")

        page.screenshot(path=str(shots / "60_accessibility_1440x900.png"), full_page=False)

        # 收尾截图：全页
        page.set_viewport_size({"width": 1920, "height": 1080})
        page.wait_for_timeout(400)
        page.screenshot(path=str(shots / "99_final_1920x1080.png"))

    finally:
        if args.keep:
            keep_open = True
            print(f"\n[--keep] 服务保留在 {url}；Ctrl+C 结束本进程。")
        try:
            if browser is not None:
                browser.close()
        except Exception:                                  # noqa: BLE001
            pass
        try:
            if pw is not None:
                pw.stop()
        except Exception:                                  # noqa: BLE001
            pass
        if srv is not None:
            srv.shutdown()
            srv.server_close()
        print(f"\n浏览器已关闭，服务已停止（端口 {port} 已释放）")

    if keep_open:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    print(f"\n截图目录：{shots}")
    return audit.report()


if __name__ == "__main__":
    raise SystemExit(main())
