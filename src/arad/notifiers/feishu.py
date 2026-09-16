"""飞书自定义机器人通知器。

消息体：``{"msg_type":"text","content":{"text":...}}``。
响应 JSON 中 ``code == 0`` **或** ``StatusCode == 0`` 视为成功。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from ..models import Alert
from .webhook import (
    DEFAULT_TIMEOUT,
    MAX_ATTEMPTS,
    RETRY_DELAY,
    Poster,
    _dry_run_dump,
    default_poster,
    format_url,
    post_with_retry,
    resolve_dry_run,
    severity_of,
)

__all__ = ["NOTIFIER_NAME", "FeishuNotifier", "build"]

NOTIFIER_NAME = "feishu"
log = logging.getLogger(__name__)


def _text_of(alert: Alert) -> str:
    text = alert.one_line()
    if alert.detail:
        text += "\n" + str(alert.detail).strip()
    return text


def _text_digest(alerts: list[Alert]) -> str:
    head = f"盘中雷达 · {len(alerts)} 条告警"
    return "\n".join([head] + [f"{i}. {a.one_line()}" for i, a in enumerate(alerts, 1)])


@dataclass
class FeishuNotifier:
    """飞书自定义机器人（text 消息）。"""

    webhook: str = ""
    timeout: float = DEFAULT_TIMEOUT
    min_severity: int = 1
    dry_run: bool = False
    enabled: bool = True
    poster: Poster = default_poster
    retry_delay: float = RETRY_DELAY
    msg_type: str = "text"
    clock: Callable[[], float] = field(default=None)  # 预留：未来支持飞书加签
    name: str = NOTIFIER_NAME

    def __post_init__(self) -> None:
        if self.clock is None:
            import time

            self.clock = time.time
        if str(self.msg_type or "").lower() == "interactive":
            # 简化处理：interactive/post 富文本退化为 text（契约允许）
            self.msg_type = "text"

    # --- 内部 ---------------------------------------------------------
    def _skip(self, alert: Any) -> bool:
        return severity_of(alert) < int(self.min_severity or 0)

    def _post_text(self, text: str) -> bool:
        payload = {"msg_type": "text", "content": {"text": str(text)}}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = format_url(self.webhook, {"text": text})
        if self.dry_run:
            _dry_run_dump(self.name, url, body)
            return True
        if not self.webhook:
            log.error("%s: 未配置 webhook，跳过发送", self.name)
            return False
        headers = {"Content-Type": "application/json; charset=utf-8"}
        status, resp = post_with_retry(
            self.poster, url, body, headers, self.timeout,
            attempts=MAX_ATTEMPTS, delay=self.retry_delay,
        )
        if not (200 <= int(status or 0) < 300):
            log.warning("%s: 推送失败 status=%s body=%s", self.name, status, str(resp)[:300])
            return False
        try:
            data = json.loads(resp)
        except Exception:
            log.warning("%s: 响应非 JSON，按失败处理: %s", self.name, str(resp)[:200])
            return False
        if isinstance(data, dict):
            code = data.get("code", data.get("StatusCode"))
            if code is None and str(data.get("msg", "")).lower() in ("success", "ok"):
                return True
            try:
                if int(code or 0) == 0:
                    return True
            except Exception:
                pass
        log.warning("%s: 业务返回失败: %s", self.name, str(data)[:300])
        return False

    # --- Notifier 协议 -------------------------------------------------
    def send(self, alert: Alert) -> bool:
        try:
            if not self.enabled or self._skip(alert):
                return True
            return self._post_text(_text_of(alert))
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
            return self._post_text(_text_digest(items))
        except Exception:
            log.exception("%s.send_digest 异常（已忽略，不影响引擎）", self.name)
            return False


def build(cfg: dict) -> FeishuNotifier:
    """由 ``notify.feishu`` 节构造（``webhook`` 也接受 ``url`` 别名）。"""
    cfg = dict(cfg or {})
    return FeishuNotifier(
        webhook=str(cfg.get("webhook") or cfg.get("url") or "").strip(),
        timeout=float(cfg.get("timeout") or DEFAULT_TIMEOUT),
        min_severity=int(cfg.get("min_severity") if cfg.get("min_severity") is not None else 1),
        dry_run=resolve_dry_run(cfg),
        enabled=bool(cfg.get("enabled", True)),
        poster=cfg.get("poster") or default_poster,
        retry_delay=float(cfg.get("retry_delay") if cfg.get("retry_delay") is not None else RETRY_DELAY),
        msg_type=str(cfg.get("msg_type") or "text"),
        name=str(cfg.get("name") or NOTIFIER_NAME),
    )
