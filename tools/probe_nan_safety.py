"""端到端验证：坏数值不会让 /api/spirit 或 SSE 流崩掉。

背景：``json.dumps`` 默认会写裸 ``NaN``/``Infinity``，浏览器 ``JSON.parse``
直接抛错 -> 整条 SSE 推送失效 -> 看板上表现为"短线精灵突然不动了"，
而且服务端没有任何报错。历史真出过这个事故。

本脚本起真服务、塞进 NaN/Inf 告警，然后：
  1. 直接 GET /api/spirit，用**严格模式** json.loads 解析（有 NaN 必抛）；
  2. 开一条真 SSE 连接，确认还能收到事件、连接没断；
  3. 断言响应体里不出现裸 NaN / Infinity 字面量。

跑法：``python tools\\probe_nan_safety.py``
"""
from __future__ import annotations

import _console  # noqa: F401,E402  —— Windows 控制台 UTF-8（见 tools/_console.py）

import json
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arad.models import Alert, AlertKind          # noqa: E402
from arad.server import web as webmod             # noqa: E402
from arad.store import AlertStore                 # noqa: E402

HOST = "127.0.0.1"
fails: list[str] = []


def check(label: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}" + (f"  {extra}" if extra else ""))
    if not ok:
        fails.append(label)


def bad_alert(i: int) -> Alert:
    """每个数值字段都塞非有限值 —— 模拟数据源返回 1e999 或除零。"""
    nan, inf = float("nan"), float("inf")
    return Alert(
        key=f"60000{i}:surge:{i}", kind=AlertKind.SURGE, code=f"60000{i}",
        name=f"坏票{i}", ts=datetime(2026, 9, 17, 9, 30) + timedelta(seconds=i),
        price=nan if i % 2 else inf, pct=inf, title="坏数值",
        detail="d", severity=3,
        metrics={"pattern": "rocket", "amount": nan, "turnover": inf,
                 "volume_ratio": float("-inf"), "window_pct": nan,
                 "amplitude": inf, "ratio_vs_float": nan, "note": "留着"},
    )


def main() -> int:
    store = AlertStore(max_alerts=500)
    for i in range(6):
        store.add_alert(bad_alert(i))

    srv = webmod.create_server(store, {"enabled": True}, host=HOST, port=0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://{HOST}:{port}"
    print(f"服务已起：{base}")

    try:
        time.sleep(0.3)

        # --- 1) /api/spirit 严格解析 ---
        print("\n=== 1) /api/spirit 严格 JSON 解析 ===")
        raw = urllib.request.urlopen(f"{base}/api/spirit?limit=50", timeout=10).read()
        text = raw.decode("utf-8")
        check("响应体不含裸 NaN 字面量", "NaN" not in text)
        check("响应体不含裸 Infinity 字面量", "Infinity" not in text)
        try:
            obj = json.loads(text, parse_constant=_reject)
            check("严格模式解析成功（有 NaN 会抛）", True)
            items = obj.get("items") if isinstance(obj, dict) else obj
            check("拿到精灵条目", bool(items), f"{len(items or [])} 条")
            if items:
                it = items[0]
                check("price 已归零", it.get("price") == 0.0, str(it.get("price")))
                check("pct 已归零", it.get("pct") == 0.0, str(it.get("pct")))
                check("extra 数值已归零",
                      all(isinstance(v, (int, float)) for v in (it.get("extra") or {}).values()),
                      str(it.get("extra")))
        except Exception as exc:  # noqa: BLE001
            check("严格模式解析成功（有 NaN 会抛）", False, f"{type(exc).__name__}: {exc}")

        # --- 2) 真 SSE 连接 ---
        print("\n=== 2) SSE 流是否存活 ===")
        got_event = threading.Event()
        got_text: list[str] = []

        def listen() -> None:
            try:
                resp = urllib.request.urlopen(f"{base}/api/stream", timeout=8)
                for line in resp:
                    s = line.decode("utf-8", "replace")
                    got_text.append(s)
                    if s.startswith("data:") or s.startswith("event:"):
                        got_event.set()
                        if len(got_text) > 40:
                            break
            except Exception:  # noqa: BLE001
                pass

        t = threading.Thread(target=listen, daemon=True)
        t.start()
        time.sleep(1.0)
        # 推一条**正常**告警，确认流还活着（NaN 若把流搞崩，这里收不到）
        store.add_alert(Alert(
            key="600000:surge:ok", kind=AlertKind.SURGE, code="600000",
            name="浦发银行", ts=datetime(2026, 9, 17, 9, 31), price=9.28,
            pct=1.2, title="正常", detail="d", severity=2, metrics={}))
        got_event.wait(4.0)
        t.join(timeout=2.0)

        check("SSE 收到了事件", got_event.is_set(),
              f"共 {len(got_text)} 行")
        joined = "".join(got_text)
        check("SSE 文本里没有裸 NaN", "NaN" not in joined)
        check("SSE 文本里没有裸 Infinity", "Infinity" not in joined)

    finally:
        srv.shutdown()
        srv.server_close()

    print("\n" + "=" * 60)
    if fails:
        print(f"✗ {len(fails)} 项未通过：{fails}")
        return 1
    print("✓ 非有限值全链路安全：HTTP 与 SSE 都输出合法 JSON")
    return 0


def _reject(name: str):
    raise ValueError(f"响应里出现非法常量 {name}（裸 NaN/Infinity 不是合法 JSON）")


if __name__ == "__main__":
    raise SystemExit(main())
