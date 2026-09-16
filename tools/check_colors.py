"""在真浏览器里核对**方向配色**：红涨绿跌，且「打开涨停」必须是绿色。

为什么单独查这个：`打开涨停` 是**利空**（封单被砸开），`打开跌停` 是**利好**
（跌停被撬开）—— 这两个最容易搞反，而搞反比不报还糟。逻辑层有测试
（`test_rule_spirit_index.py` 等），这里验的是 CSS 真的把它画成了绿色。

跑法：``python tools\\check_colors.py``
"""
from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arad.models import Alert, AlertKind          # noqa: E402
from arad.server import web as webmod             # noqa: E402
from arad.store import AlertStore                 # noqa: E402
from datetime import datetime                     # noqa: E402

# (code, name, signal, 期望方向, 说明)
CASES = [
    ("600519", "贵州茅台", "rocket", "up", "火箭发射 = 涨"),
    ("600519", "贵州茅台", "dive", "down", "高台跳水 = 跌"),
    ("300750", "宁德时代", "limit_up_seal", "up", "封涨停板 = 涨"),
    ("300750", "宁德时代", "open_limit_up", "down", "打开涨停 = 利空（绿）"),
    ("000858", "五粮液", "limit_down_seal", "down", "封跌停板 = 跌"),
    ("000858", "五粮液", "open_limit_down", "up", "打开跌停 = 利好（红）"),
    ("sh000001", "上证指数", "index_pull", "up", "拉升指数 = 涨"),
    ("sh000001", "上证指数", "index_press", "down", "打压指数 = 跌"),
    ("601318", "中国平安", "big_buy", "up", "大笔买入 = 涨"),
    ("601318", "中国平安", "big_sell", "down", "大笔卖出 = 跌"),
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("未安装 Playwright，跳过。")
        return 0

    store = AlertStore(max_alerts=200)
    t0 = datetime(2026, 9, 15, 10, 0, 0)
    for i, (code, name, sig, _dir, _why) in enumerate(CASES):
        store.add_alert(Alert(
            key=f"colorprobe:{i}", kind=AlertKind.UNUSUAL, code=code, name=name,
            ts=t0, price=100.0, pct=(1.5 if i % 2 == 0 else -1.5),
            title=f"{name} {sig}", detail="配色探针", severity=2,
            metrics={"pattern": sig},
        ))

    port = free_port()
    srv = webmod.create_server(store, {"host": "127.0.0.1", "port": port})
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2},
                     daemon=True).start()
    time.sleep(0.4)
    url = f"http://127.0.0.1:{port}/"

    fails: list[str] = []
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(viewport={"width": 1440, "height": 900})
            pg.goto(url, wait_until="networkidle")
            pg.wait_for_selector("#spiritList .sp", timeout=8000)
            pg.wait_for_timeout(800)

            # 按 key 取每行的实际颜色
            got = pg.evaluate("""() => {
                const out = {};
                document.querySelectorAll('#spiritList .sp').forEach(e => {
                    const sg = e.querySelector('.sg');
                    out[e.dataset.key] = {
                        dir: e.dataset.dir,
                        cn: sg ? sg.textContent : '',
                        color: sg ? getComputedStyle(sg).color : '',
                        cls: e.className,
                    };
                });
                return out;
            }""")
            b.close()
    finally:
        srv.shutdown()
        srv.server_close()

    # 收集所有出现过的颜色，把"绿"和"红"各自归一
    colors = {v["color"] for v in got.values() if v["color"]}
    print(f"页面里出现的信号颜色：{sorted(colors)}\n")

    def is_red(c: str) -> bool:
        # rgb(255, 77, 79) 之类：红分量最高
        n = [int(x) for x in c.replace("rgb(", "").replace(")", "").split(",")[:3]]
        return len(n) == 3 and n[0] > n[1] + 40 and n[0] > n[2] + 40

    def is_green(c: str) -> bool:
        n = [int(x) for x in c.replace("rgb(", "").replace(")", "").split(",")[:3]]
        return len(n) == 3 and n[1] > n[0] + 40

    print(f"{'信号':<18}{'中文':<12}{'期望':<7}{'实际':<7}{'颜色':<18}{'判定'}")
    print("-" * 74)
    for i, (code, name, sig, want, why) in enumerate(CASES):
        row = got.get(f"colorprobe:{i}")
        if not row:
            print(f"{sig:<18}{'-':<12}{want:<7}{'缺失':<7}{'-':<18}✗ 行没渲染出来")
            fails.append(sig)
            continue
        c = row["color"]
        ok_dir = row["dir"] == want
        ok_color = is_red(c) if want == "up" else is_green(c)
        ok = ok_dir and ok_color
        if not ok:
            fails.append(sig)
        print(f"{sig:<18}{row['cn']:<12}{want:<7}{row['dir']:<7}{c:<18}"
              f"{'✓' if ok else '✗'} {why if ok else why + ' —— 不符！'}")

    print("\n" + "=" * 74)
    if fails:
        print(f"✗ 方向/配色不符：{fails}")
        return 1
    print("✓ 全部信号的红涨绿跌配色正确（含最易搞反的打开涨停/打开跌停）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
