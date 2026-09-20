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

# 状态文件**按端口分文件**（IT-P2-DAEMON-STATE-001）。
#
# 原来固定叫 `arad-daemon.json`，是**单槽**的。一旦用户按"先用 8905 看回放
# 预览、正式守护仍在 8899"这种很自然的用法跑第二个守护，后启动的就会
# **覆盖**先启动的进程记录：`--status` 从此报的是另一个端口的守护，
# 单实例检查也会看错对象（拿 8905 的记录去判 8899 是否重复）。
#
# 实测过这个坑：8899 守护（PID 12180）还活着，`--status` 却显示 8905 的
# PID 31516 —— 记录被顶掉了。按端口分文件后各端口互不干扰。
def _state_file(port: int | None = None) -> Path:
    if port is None:
        return RUN_DIR / "arad-daemon.json"      # 兼容旧路径（--status 无端口时）
    return RUN_DIR / f"arad-daemon-{int(port)}.json"


STATE_FILE = _state_file()                        # 旧名保留，避免外部引用炸

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


def _read_state(port: int | None = None) -> dict:
    """读状态。``port`` 给定时读该端口自己的文件，否则读旧的单槽文件。"""
    path = _state_file(port)
    if port is not None and not path.exists():
        # 向后兼容：旧版本只写单槽文件；若它的 port 正好匹配就当自己的
        legacy = _state_file()
        if legacy.exists():
            try:
                st = json.loads(legacy.read_text(encoding="utf-8"))
                if int(st.get("port") or 0) == int(port):
                    return st
            except Exception:  # noqa: BLE001
                pass
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001  缺文件/半截写入都当没有
        return {}


def _write_state(_port: int | None = None, **kw) -> None:
    """写状态。端口既可位置传入，也可放在 ``port=`` 里。

    ⚠ 第一个形参刻意叫 ``_port`` 而**不是** ``port``：若叫 ``port``，
    调用方写 ``_write_state(args.port, port=args.port)`` 会让 CPython 在
    **进入函数体之前**就抛 ``TypeError: got multiple values for argument
    'port'`` —— 函数内部再 ``pop`` 也拦不住（我实际踩过：守护启动即崩）。
    改名后两种写法都安全。
    """
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    kw_port = kw.pop("port", None)
    port = _port if _port is not None else kw_port
    path = _state_file(port)
    st = _read_state(port)
    st.update(kw)
    st["port"] = port if port is not None else st.get("port")
    st["updatedAt"] = datetime.now().isoformat(timespec="seconds")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)                # 原子替换，避免读到写一半的文件
    if port is not None:
        # 同时刷新单槽文件，作为 `--status`（不带端口）的"最近一个"兜底
        _write_legacy_slot(st)


def _write_legacy_slot(st: dict) -> None:
    """把状态同步写入旧的单槽文件，仅供不带端口的 `--status` 兜底。"""
    try:
        path = _state_file()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001  兜底失败不该影响守护
        pass


def _healthy(port: int) -> bool:
    """看板 /api/status 是否可用 —— 判断"服务真的起来了"。"""
    url = f"http://127.0.0.1:{int(port)}/api/status"
    try:
        with urllib.request.urlopen(url, timeout=HEALTH_TIMEOUT) as r:
            return 200 <= int(r.status) < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _child_owns_service(port: int, child_pid: int, timeout: float = 15.0) -> bool:
    """端口上的服务是否**确实是这个子进程**提供的（而不是已有实例）。

    IT-P2-DAEMON-HEALTH-001（云端 04:10 报告指出，确实是真缺陷）：
    原来只轮询 ``_healthy(port)``。若 8899 上**已经有**一个健康服务，
    新起的 child 会因为单实例检查而立刻退出，但父进程仍会因为
    "端口返回 200" 而打印"启动成功"——把**旧服务**误认成**新 PID** 的成绩。
    用户于是拿到一句假的成功提示，而他要的那个新进程根本没起来。

    判据改为**绑定身份**：状态文件里的 ``pid`` 必须等于新 child 的 pid，
    且该 pid 仍然活着、端口同时健康。三者同时成立才算成功。
    """
    end = time.time() + timeout
    while time.time() < end:
        st = _read_state(port)
        st_pid = int(st.get("pid") or 0)
        # 状态文件必须已经是**新 child** 写的，且它活着，且端口健康
        if st_pid == int(child_pid) and _pid_alive(child_pid) \
                and _healthy(port):
            return True
        if not _pid_alive(child_pid):
            return False                 # child 已退出：绝不算成功
        time.sleep(0.25)
    return False


def _preflight(args: argparse.Namespace) -> str | None:
    """``--detach`` 之前的占用检查；返回占用原因，``None`` 表示可以启动。

    必须在 Popen **之前**做（IT-P2-DAEMON-HEALTH-001）：否则新 child 会
    因为"已有实例"直接退出，而父进程却已经准备好把旧服务当成自己的成功。
    """
    st = _read_state(args.port)
    busy = int(st.get("pid") or 0)
    if busy and busy != os.getpid() and _pid_alive(busy):
        return f"端口 {args.port} 上已有守护在跑（PID {busy}）"
    if _healthy(args.port):
        return f"{args.port} 端口已有服务在响应（不是本守护启动的）"
    return None


def cmd_status(port: int) -> int:
    st = _read_state(port)
    if not st:
        print(f"没有端口 {port} 的守护状态文件 —— 该端口的守护没跑过（或已清理）")
        others = _list_state_files()
        if others:
            print()
            print("其它端口上的守护：")
            for p in others:
                _print_state_one(p, _read_state(p))
        return 1
    _print_state_one(port, st)
    return 0 if _pid_alive(int(st.get("pid") or 0)) else 1


def _list_state_files() -> list[int]:
    """列出所有**按端口分文件**的状态（IT-P2-DAEMON-STATE-001）。"""
    out: list[int] = []
    try:
        for f in RUN_DIR.glob("arad-daemon-*.json"):
            stem = f.stem[len("arad-daemon-"):]
            if stem.isdigit():
                out.append(int(stem))
    except Exception:  # noqa: BLE001
        pass
    return sorted(out)


def _print_state_one(port: int, st: dict) -> None:
    pid = int(st.get("pid") or 0)
    alive = _pid_alive(pid)
    print(f"[端口 {port}] 守护 PID {pid}（{'运行中' if alive else '已退出'}）")
    print(f"  子进程 PID    ：{st.get('childPid')}")
    print(f"  启动于        ：{st.get('startedAt')}")
    print(f"  重启次数      ：{st.get('restarts')}")
    print(f"  最近健康检查  ：{'通过' if _healthy(port) else '失败'}  /api/status")
    print(f"  日志          ：{LOG_FILE}")


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
    # 回放预览也要能常驻：休市时用户想随时打开看板看短线精灵效果
    if getattr(args, "replay", False):
        argv += ["--replay"]
        if getattr(args, "stocks", None):
            argv += ["--stocks", str(args.stocks)]
        if getattr(args, "seed", None) is not None:
            argv += ["--seed", str(args.seed)]
        if getattr(args, "minutes", None):
            argv += ["--minutes", str(args.minutes)]
        if getattr(args, "speed", None):
            argv += ["--speed", str(args.speed)]
    return argv


def _detach_flags() -> int:
    """让子进程**独立于启动它的终端**的 Windows 进程创建标志。

    **为什么需要**（IT-P1-DAEMON-DETACH-001）：``run_daemon.py`` 的定位是
    "让项目自己跑起来，不依赖任何对话/定时消息"。但原来的 Popen **没有任何
    脱离标志** —— 守护是终端的前台子进程，用户关掉窗口（或会话结束）就有
    被杀掉的风险，"常驻"名不副实。用户真正要的是"明天开盘它在盯着"，
    而不是"只要那个黑窗口不关它就盯着"。

    * ``CREATE_NEW_PROCESS_GROUP``：新进程组，不受父终端 Ctrl+C 影响；
    * ``DETACHED_PROCESS``：**不继承**父控制台 —— 这是关窗口杀不掉它的关键。
      子进程的 stdout/stderr 本来就重定向到日志文件，不需要控制台。

    非 Windows 平台返回 0（POSIX 上真正的脱离需要 setsid/fork 双开，
    本项目运行环境是 Windows，这里不假装跨平台支持）。
    """
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | \
        getattr(subprocess, "DETACHED_PROCESS", 0)


def _child_env() -> dict:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(SRC) + (os.pathsep + existing if existing else "")
    env["PYTHONIOENCODING"] = "utf-8"     # 否则中文日志在 GBK 控制台炸
    env["PYTHONUNBUFFERED"] = "1"         # 日志要实时落盘，不能攒缓冲区
    return env


def _detach_and_return(args: argparse.Namespace) -> int:
    """``--detach``：以脱离控制台的方式重新拉起自己，然后**立刻返回**。

    做法就是最朴素可靠的那一种：把一个等价命令（去掉 ``--detach``，
    否则会无限递归）用带 ``DETACHED_PROCESS`` 的 Popen 起一遍，
    父进程马上退出，终端随即可以关闭。

    为什么不直接给子进程加标志了事：守护自己必须是"脱离"的那个进程，
    而当前进程已经附着在终端上了 —— 窗口一关它就没了。
    只有**另起一个脱离进程**才能真正做到。

    ⚠ 非 Windows 平台上 ``_detach_flags()`` 返回 0，也就是**做不到**真正
    脱离。那种情况下**不打印 Windows 等价的成功承诺**，明确告知用户
    这个进程仍会随终端结束（IT-P2-DAEMON-HEALTH-001 的第二条）。
    """
    # 必须在 Popen 之前查占用：否则新 child 会因"已有实例"退出，
    # 而父进程会把旧服务的 200 当成新进程的成功（IT-P2-DAEMON-HEALTH-001）。
    reason = _preflight(args)
    if reason:
        print(f"[!] {reason} —— 不重复启动")
        print(f"    查状态：python tools/run_daemon.py --status")
        return 2

    truly_detaches = bool(_detach_flags())
    argv = [sys.executable, str(Path(__file__).resolve())]
    if args.host:
        argv += ["--host", args.host]
    argv += ["--port", str(args.port)]
    if args.config:
        argv += ["--config", args.config]
    if args.watch_only:
        argv += ["--watch-only"]
    if args.no_restart:
        argv += ["--no-restart"]
    if args.max_restarts:
        argv += ["--max-restarts", str(args.max_restarts)]
    if getattr(args, "replay", False):
        argv += ["--replay", "--stocks", str(getattr(args, "stocks", 30)),
                 "--seed", str(getattr(args, "seed", 42))]
        if getattr(args, "minutes", None):
            argv += ["--minutes", str(args.minutes)]
        if getattr(args, "speed", None):
            argv += ["--speed", str(args.speed)]

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("ab") as log:
        log.write(f"\n{'=' * 70}\n".encode())
        log.write(f"[daemon] {datetime.now():%Y-%m-%d %H:%M:%S} "
                  f"--detach 重新拉起（离开当前终端）\n".encode())
        log.flush()
        try:
            child = subprocess.Popen(
                argv, cwd=str(ROOT), env=_child_env(),
                stdout=log, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=_detach_flags(),
            )
        except OSError as exc:
            print(f"[✗] --detach 启动失败：{exc}")
            return 2

    # 判据必须**绑定新 child 的身份**，不能只看端口 200
    ok = _child_owns_service(args.port, child.pid)

    print(f"[√] 已脱离终端启动守护 PID {child.pid}；看板 http://127.0.0.1:{args.port}/")
    print(f"    日志：{LOG_FILE}")
    print(f"    查状态：python tools/run_daemon.py --status")
    if ok and truly_detaches:
        print("    [√] 健康检查通过（已确认是该 PID 提供的服务）"
              " —— 现在可以关闭这个窗口了")
        return 0
    if ok and not truly_detaches:
        print("    [!] 服务已起来，但**本平台不支持真正的脱离**"
              "（非 Windows）—— 关闭终端仍会停止它")
        return 0
    if _pid_alive(child.pid):
        print("    [!] 15 秒内未确认「该 PID 提供的服务」——请查日志（进程仍在）")
        return 0
    print("    [✗] 新守护进程已退出，启动失败 —— 请查日志")
    print(f"    {LOG_FILE}")
    return 3


def run(args: argparse.Namespace) -> int:
    if not SRC.is_dir():
        print(f"[✗] 找不到源码目录：{SRC}")
        return 2

    if getattr(args, "detach", False):
        return _detach_and_return(args)

    # --- 单实例：同端口已有活着的守护就别再起 ---
    st = _read_state(args.port)
    busy = int(st.get("pid") or 0)
    if busy and busy != os.getpid() and _pid_alive(busy):
        print(f"[!] 端口 {args.port} 上已有守护在跑（PID {busy}）——不重复启动")
        print(f"    查看：python tools/run_daemon.py --status --port {args.port}")
        return 2
    if _healthy(args.port):
        print(f"[!] {args.port} 端口已有服务在响应 ——不重复启动")
        return 2

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    _write_state(args.port, pid=os.getpid(), childPid=None,
                 restarts=0, startedAt=datetime.now().isoformat(timespec="seconds"),
                 watchOnly=bool(args.watch_only))

    print(f"[√] 守护启动 PID {os.getpid()}；看板 http://127.0.0.1:{args.port}/")
    print(f"    日志：{LOG_FILE}")
    print("    Ctrl+C 停止")
    print("    提示：想让它不受此窗口影响，用 --detach 启动")

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
                # creationflags：看板子进程也不该被父终端 Ctrl+C 带走
                child = subprocess.Popen(argv, cwd=str(ROOT),
                                         stdout=log, stderr=subprocess.STDOUT,
                                         creationflags=_detach_flags(),
                                         env=_child_env())
            except OSError as exc:
                print(f"[✗] 子进程启动失败：{exc}")
                return 2
            _write_state(args.port, childPid=child.pid, restarts=restarts)

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
            _write_state(args.port, childPid=None,
                         stoppedAt=datetime.now().isoformat(timespec="seconds"))
            return 0

        # 子进程自己退了
        with LOG_FILE.open("ab") as log:
            log.write(f"[daemon] 子进程退出 rc={rc} 存活 {lived:.0f}s\n".encode())
        if args.no_restart:
            print(f"[!] 子进程退出 rc={rc}（--no-restart 不重启）")
            _write_state(args.port, childPid=None, lastExit=rc)
            return 0 if rc == 0 else 3
        if lived >= STABLE_SECONDS:
            restarts = 0                      # 活得够久，退避归零
        restarts += 1
        if args.max_restarts and restarts > args.max_restarts:
            print(f"[✗] 子进程已连续重启 {restarts - 1} 次仍失败 —— 停止，请查日志")
            print(f"    {LOG_FILE}")
            _write_state(args.port, childPid=None, lastExit=rc, restarts=restarts)
            return 3
        delay = BACKOFF[min(restarts - 1, len(BACKOFF) - 1)]
        print(f"[!] 子进程退出 rc={rc}，{delay}s 后重启（第 {restarts} 次）")
        _write_state(args.port, restarts=restarts, lastExit=rc)
        end = time.time() + delay
        while time.time() < end and not stopping["flag"]:
            time.sleep(0.2)

    _write_state(args.port, childPid=None,
                 stoppedAt=datetime.now().isoformat(timespec="seconds"))
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
    p.add_argument("--detach", action="store_true",
                   help="脱离当前终端启动（关掉窗口也不会停；推荐）")
    p.add_argument("--replay", action="store_true",
                   help="回放预览模式：休市时也能看到短线精灵滚动（合成行情）")
    p.add_argument("--stocks", type=int, default=30, help="（--replay）合成股票数")
    p.add_argument("--seed", type=int, default=42, help="（--replay）随机种子")
    p.add_argument("--minutes", type=float, default=None,
                   help="（--replay）只跑前 N 分钟")
    p.add_argument("--speed", type=float, default=0.0,
                   help="（--replay）倍速；0=尽快跑完")
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
