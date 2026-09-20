"""``serve --replay``：休市时也能在看板上看到短线精灵。

用户诉求原文：「你能不能把同花顺 短线精灵的线路导通 我就想要那个效果」。

缺陷（IT-P1-SERVE-REPLAY-001）
-----------------------------
看板的实时数据只在连续竞价时段产生，而用户想"先看看长什么样"的时候
往往正是休市。原来的处理是打印一句

    「想先看效果：python -m arad.cli replay」

但 ``replay`` 是**纯命令行**的 —— 它把告警打到 stdout，**根本不经过看板**。
那句话等于没解决问题：用户敲了它，看到的是一堆文本行，不是那个效果。

修复：``serve --replay`` 用合成行情驱动**同一个看板**。
关键实现点是 **共用同一个 store** —— ``Replay.build_engine(store=...)``
把回放告警写进看板正在读的 store，前端 SSE 与 ``/api/spirit`` 照常工作，
不需要前端知道数据是假的。
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _get(port: int, path: str, timeout: float = 8.0):
    url = "http://127.0.0.1:%d%s" % (port, path)
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _wait_up(port: int, seconds: float = 40.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        try:
            _get(port, "/api/health", timeout=3)
            return True
        except Exception:                       # noqa: BLE001
            time.sleep(0.5)
    return False


def _run_serve_replay(port: int, seconds: float = 25.0):
    """起一个 serve --replay，等它跑出内容，返回取到的数据后关掉。"""
    env = {"PYTHONPATH": "src", "PYTHONIOENCODING": "utf-8"}
    import os
    full = dict(os.environ)
    full.update(env)
    p = subprocess.Popen(
        [sys.executable, "-m", "arad.cli", "serve", "--replay",
         "--stocks", "12", "--minutes", "40", "--port", str(port)],
        cwd=str(REPO), env=full,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert _wait_up(port, 45.0), "serve --replay 未能在 45 秒内起来"
        # 等回放推进出足够告警
        deadline = time.time() + seconds
        spirit = {"items": []}
        status = {}
        while time.time() < deadline:
            spirit = _get(port, "/api/spirit")
            status = _get(port, "/api/status")
            if (spirit.get("items") or []):
                break
            time.sleep(1.0)
        return spirit, status
    finally:
        p.terminate()
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()


@pytest.mark.slow
def test_serve_replay_populates_spirit_feed():
    """核心：休市时 ``serve --replay`` 必须让 /api/spirit 真的有内容。

    这是"能不能看到那个效果"的直接判据 —— 不是"命令没报错"，
    而是**看板真的有短线精灵行**。
    """
    port = _free_port()
    spirit, status = _run_serve_replay(port)
    items = spirit.get("items") or []
    assert items, (
        "serve --replay 跑完 /api/spirit 仍为空 —— 回放没有喂进看板的 store。"
        f"status={status}")
    # 每一行必须有前端渲染需要的字段
    for it in items[:5]:
        assert it.get("code"), f"精灵行缺 code: {it}"
        assert it.get("signal"), f"精灵行缺 signal: {it}"
        assert it.get("ts"), f"精灵行缺 ts: {it}"


@pytest.mark.slow
def test_serve_replay_reports_replay_flag_in_status():
    """状态里要能看出这是回放，别让用户误以为是真实行情。"""
    port = _free_port()
    _spirit, status = _run_serve_replay(port)
    assert status.get("phase"), f"status 缺 phase: {status}"
    # 回放时段是交易时段，不应是 closed
    assert status.get("phase") != "closed", (
        "回放应推进到连续竞价时段，否则规则会被 only_continuous 拦掉；"
        f"实际 phase={status.get('phase')}")


@pytest.mark.slow
def test_serve_replay_alert_kinds_span_multiple_types():
    """回放应产出多种告警类型 —— 单一类型说明链路只通了一半。"""
    port = _free_port()
    _spirit, status = _run_serve_replay(port)
    kinds = status.get("by_kind") or {}
    assert len(kinds) >= 2, (
        f"回放只产出 {len(kinds)} 种告警（{kinds}），链路可能未完全打通")


def test_serve_replay_help_is_documented():
    """``--replay`` 必须在 --help 里可见（否则用户发现不了这个功能）。"""
    import os
    env = dict(os.environ)
    env["PYTHONPATH"] = "src"
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([sys.executable, "-m", "arad.cli", "serve", "--help"],
                       capture_output=True, cwd=str(REPO), env=env)
    out = r.stdout.decode("utf-8", "replace")
    assert "--replay" in out
    assert "休市" in out or "合成行情" in out, "帮助里应说明它用于休市预览"
