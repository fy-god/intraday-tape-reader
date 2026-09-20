"""``run_daemon.py --detach`` 的归属与平台契约（IT-P2-DAEMON-HEALTH-001）。

云端 04:10 审计指控（已确认为真缺陷）：
``_detach_and_return()`` 只轮询 ``_healthy(port)``，而单实例检查在**新 child**
的 ``run()`` 里面。若目标端口上**已经有**一个健康服务：
新 child 会因"已有实例"立刻退出，但父进程看到端口 200 就打印
「健康检查通过 —— 现在可以关闭这个窗口了」，把**旧服务**误认成
**新 PID** 的启动成功。用户因此拿到一个假的成功承诺，
而他要的那个新进程根本没起来。

修法两条，本文件分别钉住：
1. Popen **之前**先做占用 preflight；
2. 健康检查必须**绑定新 child 的 pid + 状态文件身份**，不能只看端口 200。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "run_daemon_mod", REPO / "tools" / "run_daemon.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rd = _load()


class _Args:
    def __init__(self, **kw):
        self.host = None
        self.port = 8899
        self.config = None
        self.watch_only = False
        self.no_restart = False
        self.max_restarts = 5
        self.detach = True
        for k, v in kw.items():
            setattr(self, k, v)


def test_preflight_blocks_when_existing_daemon_alive(monkeypatch):
    """已有守护在跑 -> preflight 必须拦下，理由里要带 PID。"""
    monkeypatch.setattr(rd, "_read_state",
                        lambda: {"pid": 4321, "port": 8899})
    monkeypatch.setattr(rd, "_pid_alive", lambda pid: pid == 4321)
    monkeypatch.setattr(rd, "_healthy", lambda port: False)

    reason = rd._preflight(_Args(port=8899))
    assert reason is not None, "已有守护在跑却放行 —— 会重复启动"
    assert "4321" in reason


def test_preflight_blocks_when_port_already_serving(monkeypatch):
    """端口上已有**别的**健康服务 -> 也必须拦下（这正是审计的场景）。"""
    monkeypatch.setattr(rd, "_read_state", lambda: {})
    monkeypatch.setattr(rd, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(rd, "_healthy", lambda port: True)

    reason = rd._preflight(_Args(port=8899))
    assert reason is not None, "端口已被占用却放行 —— 会把旧服务当自己的成功"
    assert "8899" in reason


def test_preflight_allows_when_clean(monkeypatch):
    """端口空、无守护 -> 必须放行（否则功能不可用）。"""
    monkeypatch.setattr(rd, "_read_state", lambda: {})
    monkeypatch.setattr(rd, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(rd, "_healthy", lambda port: False)
    assert rd._preflight(_Args(port=8899)) is None


def test_child_owns_service_rejects_other_pids_service(monkeypatch):
    """**核心判据**：端口健康但状态文件属于**别的** PID -> 不算成功。

    这是审计指控的最小复现：旧服务在响应，新 child 已经死了。
    只看端口会误判成功；绑定身份才会正确返回 False。
    """
    monkeypatch.setattr(rd, "_read_state",
                        lambda: {"pid": 9999, "port": 8899})   # 旧服务的 PID
    monkeypatch.setattr(rd, "_pid_alive", lambda pid: pid == 9999)
    monkeypatch.setattr(rd, "_healthy", lambda port: True)      # 端口确实健康

    assert rd._child_owns_service(8899, child_pid=1234, timeout=0.3) is False, \
        "把旧服务的 200 当成了新 PID 的成功 —— 审计指控的缺陷复现"


def test_child_owns_service_accepts_matching_pid(monkeypatch):
    """状态文件 pid == child pid、child 活着、端口健康 -> 才算成功。"""
    monkeypatch.setattr(rd, "_read_state",
                        lambda: {"pid": 1234, "port": 8899})
    monkeypatch.setattr(rd, "_pid_alive", lambda pid: pid == 1234)
    monkeypatch.setattr(rd, "_healthy", lambda port: True)

    assert rd._child_owns_service(8899, child_pid=1234, timeout=2.0) is True


def test_child_owns_service_returns_false_when_child_died(monkeypatch):
    """child 已退出 -> 立刻 False，不要傻等满超时。"""
    monkeypatch.setattr(rd, "_read_state", lambda: {"pid": 1234})
    monkeypatch.setattr(rd, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(rd, "_healthy", lambda port: True)

    import time
    t0 = time.perf_counter()
    got = rd._child_owns_service(8899, child_pid=1234, timeout=10.0)
    dt = time.perf_counter() - t0
    assert got is False
    assert dt < 3.0, f"child 已死却等了 {dt:.1f}s，应立刻返回"


def test_detach_flags_is_zero_on_non_windows(monkeypatch):
    """非 Windows 上不做"真正脱离"的承诺（平台契约）。"""
    monkeypatch.setattr(rd.os, "name", "posix")
    assert rd._detach_flags() == 0


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 有该平台契约")
def test_detach_flags_on_windows_include_both_bits():
    """Windows 上必须同时带 DETACHED_PROCESS 与 CREATE_NEW_PROCESS_GROUP。"""
    import subprocess
    flags = rd._detach_flags()
    assert flags & getattr(subprocess, "DETACHED_PROCESS", 0x8), \
        "缺 DETACHED_PROCESS：子进程仍附着控制台，关窗口会被带走"
    assert flags & getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200), \
        "缺 CREATE_NEW_PROCESS_GROUP：子进程仍受父终端 Ctrl+C 影响"
