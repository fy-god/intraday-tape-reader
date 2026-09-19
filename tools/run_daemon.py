"""盘中雷达常驻守护 —— 让项目自己跑起来，不依赖任何对话/定时消息。

作用
----
1. **免配置启动**：自己把 ``src/`` 放进 ``sys.path``，因此不要求先
   ``pip install -e .`` 或手工设 ``PYTHONPATH``。
2. **崩溃自拉**：看板服务进程异常退出时按退避重启（5s→10s→20s→40s→60s，
   稳定运行 60s 后重置退避）。
3. **单实例**：同一端口只允许一个守护在跑；重复启动直接退出，不会
   出现两个进程抢端口或双份告警。
4. **可观测**：子进程 stdout/stderr 全量重定向到日志文件（不用管道，
   避免沙箱/句柄问题），并周期性写状态 JSON。

用法
----
    python tools/run_daemon.py                     # 默认端口 8899
    python tools/run_daemon.py --watch-only        # 只盯自选股，启动即用
    python tools/run_daemon.py --no-restart        # 跑一次，不退避重启
    python tools/run_daemon.py --status            # 只查状态后退出

退出码：0 正常停止，2 已有实例/参数错误，3 子进程反复崩溃超出上限。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
RUN_DIR = ROOT / "data" / "run"
LOG_FILE = RUN_DIR / "arad-daemon.log"
STATE_FILE = RUN_DIR / "arad-daemon.json"

# 退避表：崩溃越快，等得越久；避免坏配置把 CPU 打满
BACKOFF = [5, 10, 20, 40, 60]
STABLE_SECONDS = 60.0        # 连续活过这么久就认为"这次启动是好的"
HEALTH_TIMEOUT = 3.0


# ==========================================================================
# 单实例
# ==========================================================================
def _pid_alive(pid: int) -> bool:
    """进程是否还活着。

    ⚠ Windows 上**不能**用 ``os.kill(pid, 0)`` 探活：Windows 只支持
    CTRL_C_EVENT/CTRL_BREAK_EVENT，其它"信号"会被当成 TerminateProcess，
    也就是说"探测"会直接把对方杀掉。所以走 OpenProcess。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        SYNCHRONIZE = 0x00100000
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = k32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not h:
            return False
        try:
            # WAIT_TIMEOUT(0x102) = 还活着；WAIT_OBJECT_0(0) = 已退出
            return k32.WaitForSingleObject(h, 0) == 0x102
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001  缺文件/半截写入都当没有
        return {}


def _write_state(**kw) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    st = _read_state()
    st.update(kw)
    st["updatedAt"] = datetime.now().isoformat(timespec="seconds")
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)          # 原子替换，避免读到写一半的文件


def _healthy(port: int) -> bool:
    """看板 /api/status 是否可用 —— 判断"服务真的起来了"。"""
    url = f"http://127.0.0.1:{int(port)}/api/status"
    try:
        with urllib.request.urlopen(url, timeout=HEALTH_TIMEOUT) as r:
            return 200 <= int(r.status) < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def cmd_status(port: int) -> int:
    st = _read_state()
    if not st:
        print("没有守护状态文件 —— 守护没跑过（或已清理）")
        return 1
    pid = int(st.get("pid") or 0)
    alive = _pid_alive(pid)
    print(f"守护 PID      ：{pid}（{'运行中' if alive else '已退出'}）")
    print(f"子进程 PID    ：{st.get('childPid')}")
    print(f"端口          ：{st.get('port')}")
    print(f"启动于        ：{st.get('startedAt')}")
    print(f"重启次数      ：{st.get('restarts')}")
    print(f"最近健康检查  ：{'通过' if _healthy(port) else '失败'}  /api/status")
    print(f"日志          ：{LOG_FILE}")
    return 0 if alive else 1


# ==========================================================================
# 主流程
# ==========================================================================
def _child_argv(args: argparse.Namespace) -> list[str]:
    argv = [sys.executable, "-m", "arad.cli", "serve", "--port", str(args.port)]
    if args.host:
        argv += ["--host", args.host]
    if args.watch_only:
        argv += ["--watch-only"]
    if args.config:
        argv += ["--config", args.config]
    return argv


def _child_env() -> dict:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(SRC) + (os.pathsep + existing if existing else "")
    env["PYTHONIOENCODING"] = "utf-8"     # 否则中文日志在 GBK 控制台炸
    env["PYTHONUNBUFFERED"] = "1"         # 日志要实时落盘，不能攒缓冲区
    return env


def run(args: argparse.Namespace) -> int:
    if not SRC.is_dir():
        print(f"[✗] 找不到源码目录：{SRC}")
        return 2

    # --- 单实例：同端口已有活着的守护就别再起 ---
    st = _read_state()
    busy = int(st.get("pid") or 0)
    if busy and busy != os.getpid() and _pid_alive(busy) \
            and int(st.get("port") or 0) == int(args.port):
        print(f"[!] 端口 {args.port} 上已有守护在跑（PID {busy}）——不重复启动")
        print(f"    查看：python tools/run_daemon.py --status")
        return 2
    if _healthy(args.port):
        print(f"[!] {args.port} 端口已有服务在响应 ——不重复启动")
        return 2

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    _write_state(pid=os.getpid(), port=args.port, childPid=None,
                 restarts=0, startedAt=datetime.now().isoformat(timespec="seconds"),
                 watchOnly=bool(args.watch_only))

    print(f"[√] 守护启动 PID {os.getpid()}；看板 http://127.0.0.1:{args.port}/")
    print(f"    日志：{LOG_FILE}")
    print("    Ctrl+C 停止")

    stopping = {"flag": False}

    def _on_signal(_sig, _frm):        # noqa: ANN001
        stopping["flag"] = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):   # 非主线程/不支持的平台
            pass

    restarts = 0
    while not stopping["flag"]:
        started = time.monotonic()
        argv = _child_argv(args)
        with LOG_FILE.open("ab") as log:
            log.write(f"\n{'=' * 70}\n".encode())
            log.write(f"[daemon] {datetime.now():%Y-%m-%d %H:%M:%S} 启动子进程 "
                      f"(第 {restarts + 1} 次) argv={' '.join(argv)}\n".encode())
            log.flush()
            try:
                # stdout/stderr 直接写文件：不用管道，避免句柄/权限坑
                child = subprocess.Popen(argv, cwd=str(ROOT),
                                         stdout=log, stderr=subprocess.STDOUT,
                                         env=_child_env())
            except OSError as exc:
                print(f"[✗] 子进程启动失败：{exc}")
                return 2
            _write_state(childPid=child.pid, restarts=restarts)

        # 等待子进程结束，同时定期刷健康状态
        while child.poll() is None and not stopping["flag"]:
            time.sleep(1.0)
        rc = child.poll()
        lived = time.monotonic() - started

        if stopping["flag"]:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
            print("\n[√] 守护已停止")
            _write_state(childPid=None, stoppedAt=datetime.now().isoformat(timespec="seconds"))
            return 0

        # 子进程自己退了
        with LOG_FILE.open("ab") as log:
            log.write(f"[daemon] 子进程退出 rc={rc} 存活 {lived:.0f}s\n".encode())
        if args.no_restart:
            print(f"[!] 子进程退出 rc={rc}（--no-restart 不重启）")
            _write_state(childPid=None, lastExit=rc)
            return 0 if rc == 0 else 3
        if lived >= STABLE_SECONDS:
            restarts = 0                      # 活得够久，退避归零
        restarts += 1
        if args.max_restarts and restarts > args.max_restarts:
            print(f"[✗] 子进程已连续重启 {restarts - 1} 次仍失败 —— 停止，请查日志")
            print(f"    {LOG_FILE}")
            _write_state(childPid=None, lastExit=rc, restarts=restarts)
            return 3
        delay = BACKOFF[min(restarts - 1, len(BACKOFF) - 1)]
        print(f"[!] 子进程退出 rc={rc}，{delay}s 后重启（第 {restarts} 次）")
        _write_state(restarts=restarts, lastExit=rc)
        end = time.time() + delay
        while time.time() < end and not stopping["flag"]:
            time.sleep(0.2)

    _write_state(childPid=None, stoppedAt=datetime.now().isoformat(timespec="seconds"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_daemon",
        description="盘中雷达常驻守护：免配置启动 + 崩溃自拉 + 单实例",
    )
    p.add_argument("--host", default=None, help="绑定地址，默认取 settings.yaml")
    p.add_argument("--port", type=int, default=8899, help="看板端口（默认 8899）")
    p.add_argument("--config", default=None, help="备用配置文件")
    p.add_argument("--watch-only", action="store_true",
                   help="只盯自选股，不拉全市场股票池")
    p.add_argument("--no-restart", action="store_true", help="子进程退出后不重启")
    p.add_argument("--max-restarts", type=int, default=5,
                   help="连续重启上限（默认 5，超过则退出码 3）")
    p.add_argument("--status", action="store_true", help="打印守护状态后退出")
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    args = build_parser().parse_args(argv)
    if args.status:
        return cmd_status(args.port)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
