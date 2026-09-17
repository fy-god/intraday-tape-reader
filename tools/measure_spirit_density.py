"""量一下短线精灵面板实际能显示多少条 —— 用真浏览器，不靠看图。

这个脚本存在的理由：我没法"看"截图，所以把"密度够不够"变成可打印的数字。
旧版每行 46.5px（6 个格子塞进 5 列网格，涨跌幅被挤到第二行），
一屏只能显示 5 条；修复后应为 10 条。
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

import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from playwright.sync_api import sync_playwright          # noqa: E402

from arad.config import load_settings                    # noqa: E402
from arad.replay import Replay                           # noqa: E402
from arad.server import web as webmod                     # noqa: E402
from arad.store import AlertStore                        # noqa: E402

st = load_settings(use_cache=False)
for name in ("spirit_price", "spirit_order"):
    st.section("rules").setdefault(name, {})["enabled"] = True

res = Replay(seed=7, minutes=45, settings=st).run()
print(f"回放产生 {res.total} 条告警")

store = AlertStore(max_alerts=500)
for a in res.alerts:
    store.add_alert(a)

srv = webmod.create_server(store, {"host": "127.0.0.1", "port": 0})
port = srv.server_address[1]
th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2},
                      daemon=True)
th.start()
time.sleep(0.5)

try:
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        pg.goto(f"http://127.0.0.1:{port}/", wait_until="load")
        pg.wait_for_selector("#spiritList .sp", timeout=15000)
        time.sleep(1.0)

        m = pg.evaluate("""() => {
          const panel = document.querySelector('#spiritPanel');
          const body  = document.querySelector('#spiritBody');
          const list  = document.querySelector('#spiritList');
          const row   = document.querySelector('#spiritList .sp');
          const pr = panel.getBoundingClientRect(), br = body.getBoundingClientRect();
          const rr = row.getBoundingClientRect();
          return {
            panelH: pr.height, bodyH: br.height,
            rowH: rr.height, rowW: rr.width,
            nRows: list.querySelectorAll('.sp').length,
            scrollH: list.scrollHeight, clientH: list.clientHeight,
            gridCols: getComputedStyle(row).gridTemplateColumns.split(' ').length,
            nCells: row.children.length,
            hdrH: document.querySelector('#spiritPanel .phead').getBoundingClientRect().height,
            filterRows: (() => {
              const bs = [...document.querySelectorAll('#spFilters .sfbtn')];
              return new Set(bs.map(b => Math.round(b.getBoundingClientRect().top))).size;
            })(),
          };
        }""")
        b.close()
finally:
    srv.shutdown()
    srv.server_close()

print()
print("=" * 66)
print(f"面板高度           {m['panelH']:.0f}px")
print(f"表头高度           {m['hdrH']:.0f}px   （分组按钮占 {m['filterRows']} 行）")
print(f"列表可视高度       {m['bodyH']:.0f}px")
print(f"单行高度           {m['rowH']:.1f}px")
print(f"网格列数 / 格子数  {m['gridCols']} / {m['nCells']}   "
      f"{'✓ 相等（不会折行）' if m['gridCols'] == m['nCells'] else '✗ 不相等 -> 会折行！'}")
print()
visible = int(m['bodyH'] // m['rowH'])
print(f"一屏可见           {visible} 条")
print(f"列表已生成         {m['nRows']} 条（滚动可看全部）")
print("=" * 66)

ok = True
if m['gridCols'] != m['nCells']:
    print("✗ 列数与格子数不等，涨跌幅会被挤到第二行")
    ok = False
if m['rowH'] > 26:
    print(f"✗ 行高 {m['rowH']:.1f}px 过大（> 26px 说明折行了）")
    ok = False
if visible < 9:
    print(f"✗ 一屏只有 {visible} 条，密度不够（应 >= 9）")
    ok = False
if m['filterRows'] != 1:
    print(f"✗ 分组按钮占了 {m['filterRows']} 行，应排在同一行")
    ok = False

print()
print("✓ 密度达标：一行一条、一屏 %d 条、分组按钮单行" % visible if ok else "✗ 有问题，见上")
sys.exit(0 if ok else 1)
