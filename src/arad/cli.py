"""命令行入口 —— ``python -m arad.cli <子命令>``。

子命令
------
``check``     体检配置/自选/节假日，打印当前时段与下次开盘
``once``      实时抓一次快照并跑规则（休市时会明确提示）
``serve``     启动 Web 看板（HTTP + SSE）
``replay``    离线回放：用合成行情跑完整段交易时段，验证/演示全链路
``selftest``  离线自检：回放必须产出告警，否则非零退出（可脚本化）

退出码：0 成功，1 业务失败（如自检未产出告警），2 参数/环境错误。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

__all__ = ["main", "build_parser"]


# ==========================================================================
# 输出编码：Windows 控制台默认 GBK，中文会乱码
# ==========================================================================
def _setup_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001  —— 非 TTY/被重定向时忽略
            pass


def _say(*parts: object) -> None:
    print(*parts)


def _rule(title: str = "", width: int = 72) -> None:
    if title:
        _say(f"\n{'=' * 3} {title} {'=' * max(0, width - len(title) - 5)}")
    else:
        _say("=" * width)


# ==========================================================================
# 子命令实现
# ==========================================================================
def cmd_check(args: argparse.Namespace) -> int:
    """体检：配置能否加载、自选是否为空、当前时段、下次开盘。"""
    from .config import (PROJECT_ROOT, load_holidays, load_settings,
                         load_watchlist)
    from .engine import build_notifiers, build_rules
    from .session import PHASE_CN, TradingCalendar

    _rule("配置体检")
    try:
        st = load_settings(args.config)
    except Exception as exc:  # noqa: BLE001
        _say(f"[✗] 配置加载失败: {exc}")
        return 2
    _say(f"[✓] 配置：{args.config or (PROJECT_ROOT / 'config' / 'settings.yaml')}")

    wl = []
    try:
        wl = load_watchlist()
        _say(f"[{'✓' if wl else '!'}] 自选股：{len(wl)} 只"
             + (f" -> {', '.join(wl)}" if wl else "（为空，仍会扫描全市场）"))
    except Exception as exc:  # noqa: BLE001
        _say(f"[✗] 自选股加载失败: {exc}")
        return 2

    try:
        hol = load_holidays()
        _say(f"[✓] 节假日：{len(hol)} 天")
    except Exception as exc:  # noqa: BLE001
        _say(f"[!] 节假日加载失败（将按周末判断）: {exc}")

    rules = build_rules(st)
    _say(f"[{'✓' if rules else '✗'}] 规则：{len(rules)} 个 -> "
         + ", ".join(getattr(r, "name", "?") for r in rules))
    nots = build_notifiers(st)
    _say(f"[{'✓' if nots else '!'}] 通知器：{len(nots)} 个 -> "
         + ", ".join(getattr(n, "name", "?") for n in nots))

    cal = TradingCalendar.load()
    now = datetime.now()
    _rule("当前状态")
    _say(f"本机时间：{now:%Y-%m-%d %H:%M:%S}（{['一', '二', '三', '四', '五', '六', '日'][now.weekday()]}）")
    _say(f"时段：{PHASE_CN.get(cal.phase(now), '?')}（{cal.phase(now).value}）")
    _say(f"是否交易日：{'是' if cal.is_trading_day(now) else '否'}")
    _say(f"下次开盘：{cal.next_open(now):%Y-%m-%d %H:%M}")
    _say(f"已交易：{cal.elapsed_trading_seconds(now) / 60:.0f} 分钟"
         f"，距收盘 {cal.minutes_to_close(now):.0f} 分钟")
    _say(f"数据源：{st.get('sources.primary', 'tencent')}"
         f"（备用 {st.get('sources.fallback', [])}）")

    if not cal.is_open(now):
        _say("\n提示：当前非连续竞价时段，实时告警不会触发。")
        _say("      想立刻看效果请运行：python -m arad.cli replay")
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    """实时抓一次快照并跑规则。"""
    from .config import load_settings, load_watchlist
    from .engine import Engine, build_notifiers, build_rules
    from .session import TradingCalendar

    _rule("单次实时扫描")
    st = load_settings(args.config)
    cal = TradingCalendar.load()
    now = datetime.now()
    if not cal.is_trading_day(now):
        _say(f"[!] {now:%Y-%m-%d} 非交易日（或节假日），大概率无行情。")
    elif not cal.is_open(now):
        _say(f"[!] 当前非连续竞价时段（{cal.describe(now)}），行情为最后成交快照。")

    rules = build_rules(st)
    nots = build_notifiers(st)
    eng = Engine(source=None, settings=st, rules=rules, notifiers=nots,
                 watchlist=load_watchlist(), calendar=cal)
    codes = eng._codes or load_watchlist()
    if not codes:
        _say("[✗] 无可扫描的代码（自选为空且股票池抓取失败）")
        return 2

    _say(f"扫描 {len(codes)} 只…")
    try:
        quotes = eng.sources.call("snapshots", list(codes))
    except Exception as exc:  # noqa: BLE001
        _say(f"[✗] 行情抓取失败: {exc}")
        return 2
    if not quotes:
        _say("[!] 未取到任何行情")
        return 1

    _say(f"[✓] 取到 {len(quotes)} 只行情，运行 {len(rules)} 个规则…")
    alerts = eng.poll_once(force=True)
    _rule("结果")
    if not alerts:
        _say("本轮无告警（这是正常的：急拉急跌不常有）")
    else:
        for a in alerts:
            _say(f"  {a.one_line()}")
    _say(f"\n共 {len(alerts)} 条告警")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """启动 Web 看板 + 实时引擎。

    ``--replay``：用合成行情驱动**同一个看板**，让用户在休市时也能看到
    短线精灵滚动、告警弹出、分组计数这些**真实前端效果**。
    为什么需要它：看板的实时数据只在连续竞价时段产生，而用户想"先看看
    长什么样"的时候往往正是休市。原来只提示"去跑 replay"，
    但 ``replay`` 是纯命令行的，**看不到看板** —— 那句话等于没解决问题。
    """
    from .config import load_settings
    from .engine import Engine, build_notifiers, build_rules
    from .server import web as webmod
    from .session import TradingCalendar

    st = load_settings(args.config)
    host = args.host or st.get("web.host", "127.0.0.1")
    port = int(args.port or st.get("web.port", 8899))

    if getattr(args, "replay", False):
        return _serve_replay(args, st, host, port, webmod)

    engine = Engine(source=None, settings=st, rules=build_rules(st),
                    notifiers=build_notifiers(st), calendar=TradingCalendar.load())
    cfg = dict(st.web)
    srv = webmod.create_server(engine.store, cfg, host=host, port=port)
    real_host, real_port = srv.server_address[0], srv.server_address[1]

    _rule("启动 Web 看板")
    _say(f"地址：http://{real_host}:{real_port}/")
    _say(f"时段：{engine.calendar.describe()}")
    if not engine.calendar.is_open():
        _say("[!] 当前非连续竞价时段，看板可打开但没有实时告警。")
        _say("    想现在就看到短线精灵滚动：python -m arad.cli serve --replay")
    _say("按 Ctrl+C 停止")

    import threading
    threading.Thread(target=srv.serve_forever, name="arad-web", daemon=True).start()
    try:
        engine.run_forever(watch_only=bool(getattr(args, "watch_only", False)))
    except KeyboardInterrupt:
        _say("\n正在停止…")
    finally:
        engine.stop()
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:  # noqa: BLE001
            pass
    _say("已停止")
    return 0


def _serve_replay(args, st, host: str, port: int, webmod) -> int:
    """``serve --replay``：合成行情 + 真看板，把回放"喂"给前端。

    关键点是**共用同一个 store**：``Replay.build_engine`` 接受 ``store=``，
    所以可以让回放引擎把告警写进看板正在读的那个 store 里，
    前端 SSE 就能照常收到 alert / spirit 事件 —— 不需要前端知道数据是假的。
    """
    import threading
    import time as _time

    from .engine import build_rules
    from .replay import Replay, default_universe
    from .store import AlertStore
    from .session import TradingCalendar

    stocks = default_universe(max(1, int(getattr(args, "stocks", 30))))
    minutes = getattr(args, "minutes", None)
    seed = int(getattr(args, "seed", 42))
    speed = float(getattr(args, "speed", 0.0) or 0.0)

    # 看板读这个 store；回放引擎往同一个 store 写
    store = AlertStore(st, calendar=TradingCalendar.load())
    rp = Replay(stocks, seed=seed, minutes=minutes, settings=st,
                rules=build_rules(st), notifiers=[], store=store)
    engine = rp.build_engine()
    engine._codes = [s.code for s in stocks]

    cfg = dict(st.web)
    srv = webmod.create_server(store, cfg, host=host, port=port)
    real_host, real_port = srv.server_address[0], srv.server_address[1]

    _rule("启动 Web 看板（回放模式）")
    _say(f"地址：http://{real_host}:{real_port}/")
    _say(f"回放：{len(stocks)} 只 / seed={seed} / "
         f"{'全时段' if not minutes else f'{minutes} 分钟'} / "
         f"速度={'尽快' if speed <= 0 else f'{speed}x'}")
    _say("[i] 这是**合成行情**，用于预览看板与短线精灵效果，非真实盘口。")
    _say("按 Ctrl+C 停止")

    threading.Thread(target=srv.serve_forever, name="arad-web", daemon=True).start()

    stop = {"flag": False}

    def _pump() -> None:
        """逐 tick 推进回放；每 tick 之间按 speed 控速，并响应 Ctrl+C。"""
        total = len(rp.timeline)
        for i, ts in enumerate(rp.timeline):
            if stop["flag"]:
                return
            rp.clock.set(ts)
            try:
                engine.poll_once(force=True)
            except Exception as exc:  # noqa: BLE001
                _say(f"[!] 回放第 {i + 1} 轮异常：{exc!r}")
            if speed > 0:
                _time.sleep(max(0.0, rp.tick_seconds / speed))
        _say("[√] 回放已跑完 —— 看板仍可访问，按 Ctrl+C 退出")
        _say(f"    共 {total} 轮")

    pump = threading.Thread(target=_pump, name="arad-replay", daemon=True)
    pump.start()
    try:
        while pump.is_alive():
            pump.join(timeout=0.5)
        while True:
            _time.sleep(0.5)
    except KeyboardInterrupt:
        _say("\n正在停止…")
    finally:
        stop["flag"] = True
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:  # noqa: BLE001
            pass
    _say("已停止")
    return 0


def _do_replay(args: argparse.Namespace) -> "object":
    """构造并运行一次回放（replay / selftest 共用）。"""
    from .config import load_settings
    from .engine import build_notifiers, build_rules
    from .replay import Replay, default_universe

    st = load_settings(args.config)
    stocks = default_universe(max(1, int(args.stocks)))
    rp = Replay(stocks, seed=int(args.seed), minutes=args.minutes,
                settings=st, rules=build_rules(st),
                notifiers=build_notifiers(st) if args.notify else [])
    return rp, rp.run()


def cmd_replay(args: argparse.Namespace) -> int:
    """离线回放。"""
    _rule("离线回放")
    _say(f"种子 {args.seed}，股票 {args.stocks} 只，"
         f"时段 {args.minutes if args.minutes else '全天'} 分钟")
    rp, result = _do_replay(args)
    _say(f"行情 tick：{result.ticks} 个/只（共 {len(rp.stocks)} 只）")
    _rule("汇总")
    for line in result.summary_lines():
        _say(line)
    if result.alerts:
        _rule("告警明细（前 40 条）")
        for a in result.alerts[:40]:
            _say(f"  {a.ts:%H:%M:%S} {a.one_line()}")
        if result.total > 40:
            _say(f"  … 另有 {result.total - 40} 条")
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            for a in result.alerts:
                fh.write(json.dumps(a.to_dict(), ensure_ascii=False) + "\n")
        _say(f"\n[✓] 已写出 {result.total} 条告警 -> {out}")
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    """离线自检：回放必须产出告警，且每个剧本都要被命中。"""
    _rule("离线自检")
    rp, result = _do_replay(args)
    for line in result.summary_lines():
        _say(line)

    problems: list[str] = []
    if result.total <= 0:
        problems.append("回放未产出任何告警 —— 链路可能断了")
    if result.ticks <= 0:
        problems.append("回放没有生成任何 tick")
    missed = result.missed()
    if missed:
        problems.append("以下剧本股未触发告警：" +
                        ", ".join(f"{s.code}({s.kind})" for s in missed))
    # 关键类型必须真的出现（不只是"有告警"）
    for need in ("surge", "plunge", "limit_up", "limit_down"):
        if not result.by_kind.get(need):
            problems.append(f"缺少 {need} 类型告警")

    _rule("结论")
    if problems:
        for p in problems:
            _say(f"[✗] {p}")
        return 1
    _say(f"[✓] 全链路正常：{result.total} 条告警，"
         f"覆盖 {len(result.by_kind)} 种类型，全部剧本命中")
    return 0


# ==========================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="arad",
        description="A股盘中雷达 —— 急拉急跌实时预警系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python -m arad.cli check\n"
               "  python -m arad.cli replay --seed 42 --stocks 30 --minutes 40\n"
               "  python -m arad.cli selftest\n"
               "  python -m arad.cli serve --port 8899\n",
    )
    sub = p.add_subparsers(dest="cmd", metavar="<子命令>")

    c = sub.add_parser("check", help="体检配置并打印当前时段")
    c.add_argument("--config", default=None, help="备用配置文件路径")
    c.set_defaults(func=cmd_check)

    o = sub.add_parser("once", help="实时抓一次快照并跑规则")
    o.add_argument("--config", default=None, help="备用配置文件路径")
    o.set_defaults(func=cmd_once)

    s = sub.add_parser("serve", help="启动 Web 看板")
    s.add_argument("--host", default=None)
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--config", default=None)
    s.add_argument("--watch-only", action="store_true",
                   help="只盯自选股，不拉全市场股票池（启动即用，不必等 20s 刷新）")
    s.add_argument("--replay", action="store_true",
                   help="回放模式：用合成行情填满看板，休市时也能看到短线精灵滚动")
    s.add_argument("--stocks", type=int, default=30,
                   help="（--replay）合成股票数，默认 30")
    s.add_argument("--seed", type=int, default=42, help="（--replay）随机种子")
    s.add_argument("--minutes", type=float, default=None,
                   help="（--replay）只跑前 N 分钟，默认整个交易日")
    s.add_argument("--speed", type=float, default=0.0,
                   help="（--replay）倍速；0=尽快跑完（默认），1=真实速度")
    s.set_defaults(func=cmd_serve)

    def _add_replay_flags(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--seed", type=int, default=42, help="随机种子（决定行情）")
        sp.add_argument("--stocks", type=int, default=30, help="股票数量")
        sp.add_argument("--minutes", type=float, default=None,
                        help="只回放上午前 N 分钟（默认全天）")
        sp.add_argument("--notify", action="store_true",
                        help="同时走真实通知器（默认不回放到通知层）")
        sp.add_argument("--config", default=None)

    r = sub.add_parser("replay", help="离线回放（演示/验证）")
    _add_replay_flags(r)
    r.add_argument("--out", default=None, help="把告警写成 JSONL 到此路径")
    r.set_defaults(func=cmd_replay)

    t = sub.add_parser("selftest", help="离线自检（无告警则非零退出）")
    _add_replay_flags(t)
    t.set_defaults(func=cmd_selftest)
    return p


def main(argv: list[str] | None = None) -> int:
    _setup_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        _say("\n已中断")
        return 130
    except Exception as exc:  # noqa: BLE001
        _say(f"[✗] 执行失败: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
