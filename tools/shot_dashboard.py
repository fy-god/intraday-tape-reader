"""端到端验证「短线精灵看板」：起真服务 + 真回放告警 + 打真 HTTP 接口。

回放本身不带 Web 服务，所以这里手工把 ``Replay`` 的告警灌进 ``AlertStore``，
再按 ``web.serve(store, ...)`` 起服务，然后用 ``urllib`` 打 ``/api/spirit``、
``/api/alerts``、``/api/stream``，确认：

* ``/api/spirit`` 返回的是 ``to_feed_item`` 形状（有 cn/dir/group），不是原始 Alert；
* 分组目录来自服务端（前端不硬编码信号名）；
* SSE 里有 ``spirit`` 事件，且内容是合法 JSON；
* 坏数据（NaN / 非 Alert）不会让接口 500，也不会毒死 SSE。

跑法：``python tools\\shot_dashboard.py``（会临时占用一个端口，跑完自动关）
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

import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from arad.config import load_settings                    # noqa: E402
from arad.replay import Replay                            # noqa: E402
from arad.server import web as webmod                     # noqa: E402
from arad.store import AlertStore                         # noqa: E402

FAIL: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {label}" + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(label)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


# --- 1) 造数据：真回放，开两个精灵模块 -------------------------------------
print("--- 1) 准备回放数据 ---")
st = load_settings(use_cache=False)
for name in ("spirit_price", "spirit_order"):
    st.section("rules").setdefault(name, {})["enabled"] = True

res = Replay(seed=7, minutes=45, settings=st).run()
print(f"  回放产出 {res.total} 条告警")

spirit_alerts = [a for a in res.alerts if (a.metrics or {}).get("pattern")]
check("回放里有短线精灵信号", bool(spirit_alerts), f"{len(spirit_alerts)} 条")

store = AlertStore(max_alerts=500)
for a in res.alerts:
    store.add_alert(a)
print(f"  已灌入 AlertStore，recent_alerts = {len(store.recent_alerts(500))}")

# --- 2) 起服务 --------------------------------------------------------------
print("\n--- 2) 起 HTTP 服务 ---")
port = free_port()
srv = webmod.serve(store, {"port": port, "host": "127.0.0.1"}, block=False)
base = f"http://127.0.0.1:{port}"
print(f"  {base}")
time.sleep(0.4)

try:
    # --- 3) /api/spirit ---
    print("\n--- 3) GET /api/spirit ---")
    status, body = get(f"{base}/api/spirit?limit=50")
    check("HTTP 200", status == 200, str(status))
    items = body.get("items", [])
    check("返回 items 列表", isinstance(items, list) and bool(items), f"{len(items)} 条")

    if items:
        it = items[0]
        keys = {"key", "ts", "code", "name", "signal", "cn", "dir", "group"}
        missing = keys - set(it)
        check("feed item 含展示字段", not missing, f"缺 {sorted(missing)}" if missing else "")
        check("cn 是中文信号名", bool(it.get("cn")) and it["cn"] != it.get("signal"),
              f"{it.get('signal')} -> {it.get('cn')}")
        check("dir 取值合法", it.get("dir") in ("up", "down", "flat"), repr(it.get("dir")))
        # 必须是展示形状，不是原始 Alert
        check("不是原始 Alert 形状", "metrics" not in it and "detail" not in it)

    groups = body.get("groups", [])
    check("返回分组目录（前端不硬编码信号名）", bool(groups), f"{len(groups)} 组")
    if groups:
        gnames = {g.get("group") for g in groups}
        check("分组含 price/order 等", bool(gnames & {"price", "order", "limit"}),
              str(sorted(gnames)))

    # limit 上限保护
    status2, body2 = get(f"{base}/api/spirit?limit=99999")
    check("limit 被夹到上限而非报错", status2 == 200 and body2.get("limit", 0) <= 1000,
          f"limit={body2.get('limit')}")

    # --- 4) /api/alerts 仍正常（向后兼容）---
    print("\n--- 4) GET /api/alerts（回归）---")
    status3, body3 = get(f"{base}/api/alerts?limit=20")
    check("HTTP 200", status3 == 200)
    check("仍有原始告警", bool(body3.get("items")), f"{len(body3.get('items', []))} 条")

    # --- 5) /api/stream 的 spirit 事件 ---
    print("\n--- 5) GET /api/stream（SSE）---")
    seen_events: set[str] = set()
    spirit_payloads: list[dict] = []
    raw_lines: list[str] = []

    def read_sse():
        try:
            with urllib.request.urlopen(f"{base}/api/stream", timeout=6.0) as r:
                buf = b""
                deadline = time.time() + 3.5
                while time.time() < deadline:
                    chunk = r.read(1)
                    if not chunk:
                        break
                    buf += chunk
                    if buf.endswith(b"\n\n"):
                        text = buf.decode("utf-8", "replace")
                        raw_lines.append(text)
                        buf = b""
        except Exception:  # noqa: BLE001 - 超时/关闭都正常
            pass

    t = threading.Thread(target=read_sse, daemon=True)
    t.start()
    time.sleep(0.5)
    # 灌一条新告警，触发推送
    extra = spirit_alerts[0] if spirit_alerts else res.alerts[0]
    from dataclasses import replace as _replace
    import datetime as _dt
    pushed = _replace(extra, key=extra.key + ":probe",
                      ts=_dt.datetime.now())
    store.add_alert(pushed)
    t.join(timeout=6.0)

    for block in raw_lines:
        for line in block.splitlines():
            if line.startswith("event:"):
                seen_events.add(line.split(":", 1)[1].strip())
            if line.startswith("data:"):
                try:
                    obj = json.loads(line[5:].strip())
                    if isinstance(obj, dict) and {"cn", "dir"} & set(obj):
                        spirit_payloads.append(obj)
                except json.JSONDecodeError:
                    pass

    check("SSE 收到事件", bool(seen_events), str(sorted(seen_events)))
    check("SSE 含 spirit 事件", "spirit" in seen_events, str(sorted(seen_events)))
    check("spirit 事件是合法 JSON 且含展示字段", bool(spirit_payloads),
          f"{len(spirit_payloads)} 条")

    # --- 6) 坏数据不致命 ---
    print("\n--- 6) 坏数据韧性 ---")
    try:
        store.add_alert(object())        # 非 Alert 对象
    except Exception as exc:             # noqa: BLE001
        print(f"    （store.add_alert(object()) 抛出 {type(exc).__name__}，属预期防御）")
    status4, body4 = get(f"{base}/api/spirit?limit=10")
    check("坏数据后 /api/spirit 仍 200", status4 == 200)
    check("坏数据后仍返回列表", isinstance(body4.get("items"), list))

    # --- 7) 看板 HTML ---
    print("\n--- 7) 看板页面 ---")
    with urllib.request.urlopen(f"{base}/", timeout=5) as r:
        html = r.read().decode("utf-8", "replace")
    check("HTML 200 且非空", len(html) > 5000, f"{len(html)} 字节")
    for token, label in (("精灵", "短线精灵面板"), ("/api/spirit", "拉取 spirit 接口"),
                         ("api/stream", "SSE 接线")):
        check(f"页面含{label}", token in html)

finally:
    srv.shutdown()
    srv.server_close()
    print("\n  服务已关闭")

print("\n" + "=" * 60)
if FAIL:
    print(f"✗ {len(FAIL)} 项未通过：{FAIL}")
    raise SystemExit(1)
print("✓ 短线精灵看板全链路正常")
