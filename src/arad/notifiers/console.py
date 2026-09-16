"""控制台通知器：带 ANSI 颜色的终端输出。

配色（按 ``AlertKind``）：

============  ==================
类型          颜色
============  ==================
急拉 SURGE    红 / 亮红（severity>=3 用亮红加粗）
急跌 PLUNGE   绿
涨停 LIMIT_UP 黄底
跌停 LIMIT_DOWN 青
其他          白
============  ==================

* ``mode``: ``each`` = 逐条打印；``digest`` = 聚合模式（``send`` 只缓存，
  ``send_digest`` 时合并成表格一次打印）。
* ``show_detail``: 多行 detail 是否打印。
* ``digest_max``: 表格最多显示多少条。
* Windows 终端通过 ``os.system('')`` 启用 VT 转义；失败或无 TTY 时自动降级为无颜色。
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Callable, TextIO

from ..models import Alert, AlertKind
from .webhook import _dry_run_console, resolve_dry_run, severity_of

__all__ = ["NOTIFIER_NAME", "ConsoleNotifier", "build", "enable_vt", "paint", "KIND_STYLES"]

NOTIFIER_NAME = "console"
log = logging.getLogger(__name__)

RESET = "\033[0m"
BOLD = "\033[1m"
# 颜色码
RED = "\033[31m"
BRIGHT_RED = "\033[91m"
GREEN = "\033[32m"
BRIGHT_GREEN = "\033[92m"
YELLOW_BG = "\033[43;30m"
CYAN = "\033[36m"
WHITE = "\033[37m"
DIM = "\033[2m"


def _fit(s: str, width: int) -> str:
    """把 ``s`` 按显示宽度补齐/截断到恰好 ``width`` 列。

    截断是**必需**的：A 股名称长短差别很大（``浦发银行`` 8 列、``*ST康得新`` 更长），
    长名一旦超出对齐宽度就会顶掉后面的信号列，整行错位、看着很乱。
    截断时留一个 ``…`` 提示被裁过，而不是无声截掉。
    """
    w = _disp_width(s)
    if w == width:
        return s
    if w < width:
        return s + " " * (width - w)
    # 需要截断：按显示宽度逐字符累加，留 1 列给省略号（省略号本身 2 列则留 2）
    out = ""
    used = 0
    budget = width - 1
    for ch in s:
        cw = _disp_width(ch)
        if used + cw > budget:
            break
        out += ch
        used += cw
    out += "…"
    used += 1
    return out + " " * max(0, width - used)


def _disp_width(s: str) -> int:
    """显示宽度：CJK 全角字符算 2 列，其余算 1 列。

    终端里中文占两格，直接 ``len()`` 对齐会错位——短线精灵是长时间盯着看的，
    参差不齐很难受。
    """
    w = 0
    for ch in s or "":
        o = ord(ch)
        # 常见 CJK / 全角区段
        if (0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0xA4CF
                or 0xAC00 <= o <= 0xD7A3 or 0xF900 <= o <= 0xFAFF
                or 0xFE30 <= o <= 0xFE6F or 0xFF00 <= o <= 0xFF60
                or 0xFFE0 <= o <= 0xFFE6):
            w += 2
        else:
            w += 1
    return w

#: kind -> ANSI 码
KIND_STYLES: dict[str, str] = {
    AlertKind.SURGE.value: RED,
    AlertKind.PLUNGE.value: GREEN,
    AlertKind.LIMIT_UP.value: YELLOW_BG,
    AlertKind.LIMIT_DOWN.value: CYAN,
}
DEFAULT_STYLE = WHITE

_VT_ENABLED: bool | None = None


def enable_vt(stream: TextIO | None = None) -> bool:
    """Windows 上开启 VT 转义支持（``os.system('')`` 技巧）。

    非 Windows / 失败时返回 False，调用方降级为无颜色输出。
    """
    global _VT_ENABLED
    if _VT_ENABLED is not None:
        return _VT_ENABLED
    ok = False
    if os.name == "nt":
        try:
            os.system("")  # 触发 conhost/Windows Terminal 打开 VT 处理
            ok = True
        except Exception:
            ok = False
    else:
        ok = True
    # 非 TTY（重定向到文件/管道）时不要输出转义码
    try:
        target = stream if stream is not None else sys.stdout
        if target is not None and hasattr(target, "isatty") and not target.isatty():
            ok = False
    except Exception:
        ok = False
    _VT_ENABLED = ok
    return ok


def paint(text: str, style: str, *, enabled: bool = True) -> str:
    """给文本套 ANSI 样式；``enabled=False`` 时原样返回。"""
    if not enabled or not style:
        return text
    return f"{style}{text}{RESET}"


@dataclass
class ConsoleNotifier:
    """终端输出（带颜色）。"""

    color: bool = True
    show_detail: bool = False
    min_severity: int = 1
    mode: str = "each"
    digest_max: int = 10
    dry_run: bool = False
    enabled: bool = True
    stream: TextIO | None = None
    name: str = NOTIFIER_NAME
    #: 输出形态：``radar`` = 原有多行/表格；``spirit`` = 短线精灵式单行滚动播报。
    style: str = "radar"
    #: spirit 模式下股票名的对齐宽度（中文按 2 字符宽计）。
    align_name: int = 10
    #: spirit 模式下是否画表头（长时间盯盘时表头有用，管道输出时会污染）。
    show_header: bool = True
    _vt: bool | None = field(default=None, init=False, repr=False)
    _header_done: bool = field(default=False, init=False, repr=False)

    # --- 内部 ---------------------------------------------------------
    @property
    def colors_on(self) -> bool:
        if not self.color:
            return False
        if self._vt is None:
            self._vt = enable_vt(self.stream)
        return self._vt

    def _out(self, text: str) -> None:
        stream = self.stream if self.stream is not None else sys.stdout
        try:
            print(text, file=stream)
        except UnicodeEncodeError:
            # 极端情况下（GBK 控制台）降级为可编码文本，绝不抛异常
            try:
                enc = getattr(stream, "encoding", None) or "utf-8"
                print(text.encode(enc, "replace").decode(enc, "replace"), file=stream)
            except Exception:
                pass
        except Exception:
            pass

    def _style_for(self, alert: Alert) -> str:
        kind = getattr(alert.kind, "value", alert.kind)
        if kind == AlertKind.SURGE.value and severity_of(alert) >= 3:
            return BOLD + BRIGHT_RED      # 急拉加急：亮红加粗
        return KIND_STYLES.get(str(kind), DEFAULT_STYLE)

    def _line(self, alert: Alert) -> str:
        style = self._style_for(alert)
        return paint(alert.one_line(), style, enabled=self.colors_on)

    def _detail_lines(self, alert: Alert) -> list[str]:
        if not self.show_detail or not alert.detail:
            return []
        return [
            paint("    " + ln, DIM, enabled=self.colors_on)
            for ln in str(alert.detail).strip().splitlines()
        ]

    def _render_each(self, alert: Alert) -> None:
        if self.dry_run:
            _dry_run_console(self.name, f"(would print) {alert.one_line()}")
        self._out(self._line(alert))
        for ln in self._detail_lines(alert):
            self._out(ln)

    # --- 短线精灵式播报 -------------------------------------------------
    def _spirit_style(self, alert: Alert) -> str:
        """按信号方向取色：偏多红、偏空绿、中性灰。

        A 股红涨绿跌。注意「打开涨停」偏空（绿）、「打开跌停」偏多（红），
        这两个最容易配反，所以方向由 :mod:`arad.spirit` 统一定义，这里只取色。
        """
        from ..spirit import DOWN, UP, direction_of

        d = direction_of(alert)
        if d == UP:
            return BOLD + BRIGHT_RED if severity_of(alert) >= 3 else RED
        if d == DOWN:
            return BRIGHT_GREEN if severity_of(alert) >= 3 else GREEN
        return DIM

    def _spirit_header(self) -> None:
        if not self.show_header or self._header_done:
            return
        self._header_done = True
        cols = f"{'时间':<8} {'代码':<7}{'名称':<10}{'信号':<10}{'现价':>9}{'涨跌':>9}"
        self._out(paint("── 短线精灵 ── " + cols, DIM, enabled=self.colors_on))

    def _render_spirit(self, alert: Alert) -> None:
        """单行滚动播报（短线精灵风格）。"""
        from ..spirit import describe

        self._spirit_header()
        s = describe(alert)
        ts = alert.ts.strftime("%H:%M:%S") if alert.ts is not None else "--:--:--"
        name = _fit(alert.name or "", int(self.align_name))
        row = (
            f"{ts:<8} {alert.code:<7}{name}"
            f"{s.cn:<10}{float(alert.price):>9.2f}{float(alert.pct):>+8.2f}%"
        )
        self._out(paint(row, self._spirit_style(alert), enabled=self.colors_on))
        if self.show_detail:
            for ln in self._detail_lines(alert):
                self._out(ln)

    def _render_table(self, alerts: list[Alert]) -> None:
        """合并成表格（代码/名称/现价/涨跌幅/标题），超 digest_max 截断。"""
        n = int(self.digest_max or 0)
        shown = alerts[:n] if n > 0 else list(alerts)
        if self.dry_run:
            _dry_run_console(self.name, f"(would print {len(alerts)} 行表格)")
        header = f"── 盘中雷达 · {len(alerts)} 条告警 ──"
        self._out(paint(header, BOLD, enabled=self.colors_on))
        for a in shown:
            style = self._style_for(a)
            row = (
                f"{a.code:<6} {a.name:<8} {float(a.price):>8.2f} "
                f"{float(a.pct):>+7.2f}%  {a.title}"
            )
            self._out("  " + paint(row, style, enabled=self.colors_on))
            for ln in self._detail_lines(a):
                self._out(ln)
        if len(alerts) > len(shown):
            self._out(paint(f"  … 还有 {len(alerts) - len(shown)} 条", DIM, enabled=self.colors_on))

    # --- Notifier 协议 -------------------------------------------------
    def send(self, alert: Alert) -> bool:
        try:
            if not self.enabled or severity_of(alert) < int(self.min_severity or 0):
                return True
            if self.dry_run:
                _dry_run_console(self.name, f"(would print) {alert.one_line()}")
            # 注意：引擎（arad/engine.py::_dispatch）只调用 send()，从不调用
            # send_digest()，所以 send() 必须立即输出，绝不能把告警缓存起来
            # 等 send_digest —— 那会导致 digest 模式下告警永远不显示。
            if self._is_spirit:
                self._render_spirit(alert)
            else:
                self._render_each(alert)
            return True
        except Exception:
            log.exception("%s.send 异常（已忽略，不影响引擎）", self.name)
            return False

    def send_digest(self, alerts: list[Alert]) -> bool:
        """``mode=digest`` 时把这一批合并成一张表格；``mode=each`` 时逐条打印。"""
        try:
            if not self.enabled:
                return True
            items = [a for a in (alerts or []) if severity_of(a) >= int(self.min_severity or 0)]
            if not items:
                return True
            if self._is_spirit:
                # 短线精灵就是逐条滚动的：批量也逐条打，保持时间顺序
                for a in items:
                    self._render_spirit(a)
            elif str(self.mode).lower() == "digest":
                self._render_table(items)
            else:
                for a in items:
                    self._render_each(a)
            return True
        except Exception:
            log.exception("%s.send_digest 异常（已忽略，不影响引擎）", self.name)
            return False

    @property
    def _is_spirit(self) -> bool:
        return str(self.style or "").strip().lower() == "spirit"


def build(cfg: dict) -> ConsoleNotifier:
    """由 ``notify.console`` 节构造。"""
    cfg = dict(cfg or {})
    mode = str(cfg.get("mode") or "each").strip().lower()
    if mode not in ("each", "digest"):
        mode = "each"
    style = str(cfg.get("style") or "radar").strip().lower()
    if style not in ("radar", "spirit"):
        style = "radar"
    stream: Callable | None = cfg.get("stream")
    return ConsoleNotifier(
        color=bool(cfg.get("color", True)),
        show_detail=bool(cfg.get("show_detail", False)),
        min_severity=int(cfg.get("min_severity") if cfg.get("min_severity") is not None else 1),
        mode=mode,
        digest_max=int(cfg.get("digest_max") if cfg.get("digest_max") is not None else 10),
        dry_run=resolve_dry_run(cfg),
        enabled=bool(cfg.get("enabled", True)),
        stream=stream,
        name=str(cfg.get("name") or NOTIFIER_NAME),
        style=style,
        align_name=int(cfg.get("align_name") if cfg.get("align_name") is not None else 10),
        show_header=bool(cfg.get("show_header", True)),
    )
