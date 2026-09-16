"""JSONL 落盘通知器：每条告警一行 ``json.dumps(alert.to_dict(), ensure_ascii=False)``。

* 追加写；父目录不存在自动创建；
* 行级线程安全（同一路径共享一把 :class:`threading.Lock`），并发写不会写坏行；
* 任何异常都吞掉，返回 ``False``（契约：通知失败不许打断引擎）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import PROJECT_ROOT, load_settings
from ..models import Alert
from .webhook import resolve_dry_run, severity_of

__all__ = ["NOTIFIER_NAME", "FileJsonlNotifier", "build", "default_path", "path_locks"]

NOTIFIER_NAME = "file"
log = logging.getLogger(__name__)

_LOCK_REGISTRY_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.Lock] = {}


def path_locks() -> dict[str, threading.Lock]:
    """（测试用）当前已登记的 路径 -> 锁 映射。"""
    return _PATH_LOCKS


def _lock_for(path: Path) -> threading.Lock:
    key = os.path.normcase(str(path))
    with _LOCK_REGISTRY_GUARD:
        lk = _PATH_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _PATH_LOCKS[key] = lk
        return lk


def default_path() -> Path:
    """未配置 path 时的默认落盘位置（storage.alerts_path，再退回 data/alerts.jsonl）。"""
    try:
        st = load_settings()
        raw = st.get("storage.alerts_path") or "data/alerts.jsonl"
        p = Path(str(raw))
        return p if p.is_absolute() else PROJECT_ROOT / p
    except Exception:
        log.debug("default_path: 读取 settings 失败，使用 data/alerts.jsonl", exc_info=True)
        return PROJECT_ROOT / "data" / "alerts.jsonl"


def _resolve_path(raw: Any) -> Path:
    if raw in (None, ""):
        return default_path()
    p = Path(str(raw)).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


@dataclass
class FileJsonlNotifier:
    """把告警逐行追加到 JSONL 文件。"""

    path: Path = field(default_factory=default_path)
    min_severity: int = 1
    dry_run: bool = False
    enabled: bool = True
    name: str = NOTIFIER_NAME

    def __post_init__(self) -> None:
        self.path = Path(self.path)

    # --- 内部 ---------------------------------------------------------
    def _skip(self, alert: Any) -> bool:
        return severity_of(alert) < int(self.min_severity or 0)

    def _dumps(self, alert: Alert) -> str | None:
        """序列化一行；失败返回 None（异常不抛出）。"""
        try:
            return json.dumps(alert.to_dict(), ensure_ascii=False) + "\n"
        except Exception:
            log.exception("%s: 序列化告警失败 key=%s", self.name, getattr(alert, "key", "?"))
            return None

    def _append(self, line: str) -> bool:
        """加锁追加一行；父目录不存在自动创建。返回是否成功。"""
        try:
            parent = self.path.parent
            if parent and not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)
            with _lock_for(self.path):
                with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(line)
                    fh.flush()
            return True
        except Exception:
            log.exception("%s: 写入失败 path=%s", self.name, self.path)
            return False

    def _write_one(self, alert: Alert) -> bool:
        """写一行；成功 True。异常不抛出。"""
        line = self._dumps(alert)
        if line is None:
            return False
        if self.dry_run:
            try:
                print(f"[dry-run] {self.name} -> {self.path}\n{line.rstrip()}")
            except Exception:
                pass
            return True
        return self._append(line)

    # --- Notifier 协议 -------------------------------------------------
    def send(self, alert: Alert) -> bool:
        try:
            if not self.enabled or self._skip(alert):
                return True
            return self._write_one(alert)
        except Exception:
            log.exception("%s.send 异常（已忽略，不影响引擎）", self.name)
            return False

    def send_digest(self, alerts: list[Alert]) -> bool:
        """逐条写；任一条写失败即返回 False（仍会尝试写完剩余条目）。"""
        try:
            if not self.enabled:
                return True
            items = [a for a in (alerts or []) if not self._skip(a)]
            if not items:
                return True
            ok = True
            for a in items:
                if not self._write_one(a):
                    ok = False
            return ok
        except Exception:
            log.exception("%s.send_digest 异常（已忽略，不影响引擎）", self.name)
            return False


def build(cfg: dict) -> FileJsonlNotifier:
    """由 ``notify.file`` 节构造（``path`` 为相对项目根的路径）。"""
    cfg = dict(cfg or {})
    return FileJsonlNotifier(
        path=_resolve_path(cfg.get("path")),
        min_severity=int(cfg.get("min_severity") if cfg.get("min_severity") is not None else 1),
        dry_run=resolve_dry_run(cfg),
        enabled=bool(cfg.get("enabled", True)),
        name=str(cfg.get("name") or NOTIFIER_NAME),
    )
