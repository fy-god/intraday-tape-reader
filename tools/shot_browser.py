"""短线精灵看板 · 真浏览器验证 + 截图（Playwright）。

这是**最接近用户实际观感**的一步：起真服务、灌真回放数据、用真 Chromium 打开、
等 SSE 把行推上来，然后断言渲染结果并截图。

与 `dash_render_check.js` 的分工：
* `dash_render_check.js` —— 不依赖浏览器，验渲染**逻辑**（配色/去重/DOM 上限），
  快且能在 CI 跑；
* 本脚本 —— 验**真浏览器里的最终效果**（CSS 真的生效、布局不溢出、SSE 真的推到了）。

需要 Playwright（`pip install playwright && playwright install chromium`），
没装会友好退出而不是报错。

用法：
    python tools\\shot_browser.py              # 自动选端口，跑完关服务
    python tools\\shot_browser.py --keep       # 跑完不关服务，你可以自己打开看
    python tools\\shot_browser.py --port 8899
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
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arad.config import load_settings                       # noqa: E402
from arad.replay import Replay                              # noqa: E402
from arad.server import web as webmod                       # noqa: E402
from arad.store import AlertStore                           # noqa: E402

OUT_PNG = ROOT / "tools" / "spirit_dashboard.png"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0, help="0 = 自动选空闲端口")
    ap.add_argument("--minutes", type=int, default=45, help="回放时长（分钟）")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--keep", action="store_true",
                    help="跑完保留服务（自己打开浏览器看）")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("未安装 Playwright，跳过浏览器验证。")
        print("  pip install playwright && playwright install chromium")
        return 0

    port = args.port or free_port()

    # --- 数据：开两个精灵模块的真回放 -------------------------------------
    st = load_settings(use_cache=False)
    for name in ("spirit_price", "spirit_order"):
        st.section("rules").setdefault(name, {})["enabled"] = True

    store = AlertStore(max_alerts=500)
    srv = webmod.create_server(store, {"host": "127.0.0.1", "port": port})
    url = f"http://127.0.0.1:{port}/"
    print(f"[web] {url}")
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2},
                     name="arad-web", daemon=True).start()
    time.sleep(0.4)

    # --- 关键顺序：先起服务，再跑回放 -------------------------------------
    # 这样每条告警都经 store.broadcast() 实时推到 SSE，验证的是**活链路**；
    # 反过来（先跑完回放再起服务）SSE 什么都收不到，只能验证历史补齐。
    res = Replay(seed=args.seed, minutes=args.minutes, settings=st).run()
    for a in res.alerts:
        store.add_alert(a)
    spirit_n = sum(1 for a in res.alerts if (a.metrics or {}).get("pattern"))
    print(f"[replay] {res.total} 条告警（其中精灵信号 {spirit_n} 条）")

    fails: list[str] = []
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(viewport={"width": 1440, "height": 900})
            errors: list[str] = []
            pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            pg.on("console", lambda m: errors.append(f"console.error: {m.text}")
                  if m.type == "error" else None)

            pg.goto(url, wait_until="networkidle")
            pg.wait_for_selector("#spiritList .sp", timeout=8000)
            pg.wait_for_timeout(1200)

            rows = pg.eval_on_selector_all(
                "#spiritList .sp",
                """els => els.map(e => ({
                    cls: e.className, group: e.dataset.group, dir: e.dataset.dir,
                    text: e.innerText.replace(/\\s+/g,' ').trim(),
                    color: getComputedStyle(e.querySelector('.sg')).color,
                }))""")
            meta = pg.inner_text("#spiritMeta")
            filters = pg.eval_on_selector_all("#spFilters .sfbtn",
                                              "els => els.map(e => e.textContent)")
            empty = pg.evaluate(
                "() => { const e = document.querySelector('#spiritList .empty');"
                " return e ? e.textContent : null; }")
            layout = pg.evaluate("""() => {
                const d = document.documentElement;
                const sp = document.getElementById('spiritPanel').getBoundingClientRect();
                const al = document.getElementById('alertsPanel').getBoundingClientRect();
                return {scrollW: d.scrollWidth, clientW: d.clientWidth,
                        spH: Math.round(sp.height), alH: Math.round(al.height),
                        spTop: Math.round(sp.top), alTop: Math.round(al.top)};
            }""")

            # 分组过滤真的生效
            pg.click('#spFilters .sfbtn[data-group="limit"]')
            pg.wait_for_timeout(300)
            filtered = pg.eval_on_selector_all(
                "#spiritList .sp:not([style*='display: none'])",
                "els => els.map(e => e.dataset.group)")
            pg.click('#spFilters .sfbtn[data-group="all"]')
            pg.wait_for_timeout(200)

            pg.screenshot(path=str(OUT_PNG))

            # --- SSE 活链路：新告警要能自动冒到最上面 --------------------
            from dataclasses import replace as _replace
            import datetime as _dt

            base = next((a for a in res.alerts if (a.metrics or {}).get("pattern")),
                        res.alerts[0])
            probe = _replace(base, key=base.key + ":browserprobe",
                             ts=_dt.datetime.now())
            before_top = pg.inner_text("#spiritList .sp:first-child")
            store.add_alert(probe)
            pg.wait_for_function(
                "prev => { const e = document.querySelector('#spiritList .sp');"
                " return e && e.innerText !== prev; }",
                arg=before_top, timeout=6000)
            after_top = pg.inner_text("#spiritList .sp:first-child")
            b.close()

        # --- 断言 ---------------------------------------------------------
        def check(label: str, cond: bool, extra: str = "") -> None:
            print(f"  {'✓' if cond else '✗'} {label}" + (f"  {extra}" if extra else ""))
            if not cond:
                fails.append(label)

        print(f"\n页面错误：{errors or '无'}")
        print(f"spiritMeta：{meta}")
        print(f"分组按钮：{filters}")
        print(f"布局：{layout}\n")
        print(f"渲染出 {len(rows)} 行，前 8 行：")
        for r in rows[:8]:
            print(f"  [{r['cls']:<20}] {r['text']}")

        print()
        check("浏览器无 JS 报错", not errors, str(errors[:2]))
        check("精灵面板渲染出内容行", len(rows) > 0, f"{len(rows)} 行")
        check("面板不是空态", empty is None, repr(empty))
        check("分组筛选按钮来自服务端", len(filters) > 1, str(filters))
        check("页面无横向溢出（没被挤成第四栏）",
              layout["scrollW"] <= layout["clientW"],
              f"scrollW={layout['scrollW']} clientW={layout['clientW']}")
        check("精灵面板在告警流上方", layout["spTop"] < layout["alTop"],
              f"spTop={layout['spTop']} alTop={layout['alTop']}")
        check("精灵面板有实际高度", layout["spH"] > 100, f"{layout['spH']}px")
        check("分组筛选真的生效（limit 只剩涨跌停）",
              bool(filtered) and set(filtered) <= {"limit"}, str(sorted(set(filtered))))
        check("颜色确实按方向渲染（有红有绿）",
              len({r["color"] for r in rows}) >= 2,
              str(sorted({r["color"] for r in rows})))
        check("SSE 新告警自动冒到最上面",
              after_top != before_top, f"{before_top[:24]!r} -> {after_top[:24]!r}")

        print(f"\n截图：{OUT_PNG}")
    finally:
        if args.keep:
            print(f"\n服务保留在 {url}（Ctrl+C 结束本进程）")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
        srv.shutdown()
        srv.server_close()

    print("\n" + "=" * 58)
    if fails:
        print(f"✗ {len(fails)} 项未通过：{fails}")
        return 1
    print("✓ 真浏览器里的短线精灵看板正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
