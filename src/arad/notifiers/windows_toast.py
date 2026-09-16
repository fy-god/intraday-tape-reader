"""Windows 托盘/操作中心 Toast 通知（不新增任何第三方依赖）。

实现方式：把一段 PowerShell 脚本写到临时 ``.ps1`` 文件（``utf-8-sig`` 编码，
保证 PowerShell 正确读中文），再用::

    subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
                   shell=False, timeout=8)

执行（用 ``-File`` 而不是 ``-Command``，彻底绕开引号转义问题）。优先级：

1. 优先用 ``[Windows.UI.Notifications.ToastNotificationManager]``
   （Win10+ 原生 WinRT，无需安装 BurntToast）；
2. 失败则降级为 ``msg *``（老系统）；
3. 再失败直接返回 ``False`` 并记日志。

``dry_run=True`` 时只打印，不启动任何进程。**任何异常都吞掉返回 False**。
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..config import PROJECT_ROOT
from ..models import Alert
from .webhook import kind_label, resolve_dry_run, severity_of

__all__ = [
    "NOTIFIER_NAME",
    "WindowsToastNotifier",
    "build",
    "POWERSHELL_SCRIPT",
    "MSGTYPE_SCRIPT",
    "ps_quote",
]

NOTIFIER_NAME = "windows_toast"
log = logging.getLogger(__name__)
TIMEOUT = 8.0
APP_ID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"

# PowerShell 脚本模板：``{title}`` / ``{body}`` 是 PS 字符串字面量，
# ``{title_xml}`` / ``{body_xml}`` 是 XML 文本节点内容（已做 XML 转义）。
POWERSHELL_SCRIPT = """\
$ErrorActionPreference = 'Stop'
$Title = {title}
$Body = {body}
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$template = @'
<toast><visual><binding template="ToastGeneric"><text>{title_xml}</text><text>{body_xml}</text></binding></visual></toast>
'@
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($template)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier({app_id}).Show($toast)
"""

# 降级脚本：msg 弹窗（老系统 / WinRT 不可用时）
MSGTYPE_SCRIPT = """\
$ErrorActionPreference = 'Continue'
msg * /TIME:10 {text}
"""


def ps_quote(text: Any) -> str:
    """PowerShell **单引号**字符串字面量（内部单引号翻倍转义）。"""
    return "'" + str(text).replace("'", "''") + "'"


def _xml_escape(text: Any) -> str:
    s = str(text)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _temp_dirs() -> list[Path]:
    """临时脚本目录候选：优先项目内 data/（契约要求不写项目外路径），再退回系统临时目录。"""
    dirs: list[Path] = []
    data_dir = PROJECT_ROOT / "data"
    if data_dir.is_dir():
        dirs.append(data_dir)
    try:
        dirs.append(Path(tempfile.gettempdir()))
    except Exception:
        pass
    return dirs


def _default_runner(script: str, timeout: float = TIMEOUT) -> int:
    """把 PS 脚本写入临时 .ps1 后执行；返回 returncode（异常由调用方处理）。"""
    path: str | None = None
    last_exc: Exception | None = None
    for d in _temp_dirs():
        try:
            fd, path = tempfile.mkstemp(prefix="arad_toast_", suffix=".ps1", dir=str(d))
            with os.fdopen(fd, "w", encoding="utf-8-sig", newline="\r\n") as fh:
                fh.write(script)
            break
        except Exception as exc:
            last_exc = exc
            path = None
    if path is None:
        raise OSError(f"无法创建临时脚本: {last_exc}")
    try:
        exe = "powershell.exe" if os.name == "nt" else "pwsh"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        proc = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", path],
            shell=False,
            timeout=float(timeout),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        return int(proc.returncode or 0)
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


def _wrap(text: str, width: int = 60, max_lines: int = 6) -> str:
    """把长文本简单折行（按字符数）。"""
    out: list[str] = []
    for raw in str(text).splitlines():
        line = raw.rstrip()
        while len(line) > width:
            out.append(line[:width])
            line = line[width:]
        out.append(line)
        if len(out) >= max_lines:
            break
    return "\n".join(out[:max_lines])


@dataclass
class WindowsToastNotifier:
    """Windows 桌面通知。"""

    min_severity: int = 3
    dry_run: bool = False
    enabled: bool = True
    timeout: float = TIMEOUT
    title_prefix: str = "A股盘中雷达"
    app_id: str = APP_ID
    use_fallback_msg: bool = True
    runner: Callable[[str, float], int] | None = None
    name: str = NOTIFIER_NAME

    def __post_init__(self) -> None:
        if self.runner is None:
            self.runner = _default_runner

    # --- 内部 ---------------------------------------------------------
    def _skip(self, alert: Any) -> bool:
        return severity_of(alert) < int(self.min_severity or 0)

    def _build_script(self, title: str, body: str) -> str:
        return POWERSHELL_SCRIPT.format(
            title=ps_quote(title),
            body=ps_quote(body),
            title_xml=_xml_escape(title),
            body_xml=_xml_escape(body),
            app_id=ps_quote(self.app_id),
        )

    def _build_fallback(self, title: str, body: str) -> str:
        return MSGTYPE_SCRIPT.format(text=ps_quote(f"{title} - {body}".replace("\n", " ")[:200]))

    def _run(self, script: str) -> int:
        runner = self.runner or _default_runner
        return int(runner(script, float(self.timeout)))

    def _notify(self, title: str, body: str) -> bool:
        if self.dry_run:
            # dry_run：只打印，不启动任何进程（不依赖 stdout 是否被捕获）
            try:
                print(f"[dry-run] {self.name} -> {title}\n{body}", flush=True)
            except Exception:
                pass
            log.info("[dry-run] %s -> %s | %s", self.name, title, body.replace("\n", " / "))
            return True
        if os.name != "nt":
            log.info("%s: 非 Windows 平台，跳过（仅 dry_run 可用）", self.name)
            return False
        try:
            if self._run(self._build_script(title, body)) == 0:
                return True
            log.warning("%s: Toast 脚本返回非 0，尝试降级", self.name)
        except subprocess.TimeoutExpired:
            log.warning("%s: Toast 脚本超时(%.0fs)", self.name, self.timeout)
        except Exception:
            log.warning("%s: Toast 发送失败，尝试降级", self.name, exc_info=True)
        if self.use_fallback_msg:
            try:
                if self._run(self._build_fallback(title, body)) == 0:
                    return True
            except Exception:
                log.warning("%s: msg 降级同样失败", self.name, exc_info=True)
        return False

    # --- Notifier 协议 -------------------------------------------------
    def send(self, alert: Alert) -> bool:
        try:
            if not self.enabled or self._skip(alert):
                return True
            title = f"{self.title_prefix} · {kind_label(alert.kind)} {alert.code} {alert.name}"
            body = _wrap(alert.one_line() + (("\n" + str(alert.detail)) if alert.detail else ""))
            return self._notify(title[:120], body)
        except Exception:
            log.exception("%s.send 异常（已忽略，不影响引擎）", self.name)
            return False

    def send_digest(self, alerts: list[Alert]) -> bool:
        try:
            if not self.enabled:
                return True
            items = [a for a in (alerts or []) if not self._skip(a)]
            if not items:
                return True
            title = f"{self.title_prefix} · {len(items)} 条告警"
            body = _wrap("\n".join(a.one_line() for a in items), max_lines=5)
            return self._notify(title[:120], body)
        except Exception:
            log.exception("%s.send_digest 异常（已忽略，不影响引擎）", self.name)
            return False


def build(cfg: dict) -> WindowsToastNotifier:
    """由 ``notify.windows_toast`` 节构造。"""
    cfg = dict(cfg or {})
    timeout = float(cfg.get("timeout") or TIMEOUT)
    return WindowsToastNotifier(
        min_severity=int(cfg.get("min_severity") if cfg.get("min_severity") is not None else 3),
        dry_run=resolve_dry_run(cfg),
        enabled=bool(cfg.get("enabled", True)),
        timeout=timeout,
        title_prefix=str(cfg.get("title_prefix") or "A股盘中雷达"),
        app_id=str(cfg.get("app_id") or APP_ID),
        use_fallback_msg=bool(cfg.get("use_fallback_msg", True)),
        runner=cfg.get("runner") or _default_runner,
        name=str(cfg.get("name") or NOTIFIER_NAME),
    )
