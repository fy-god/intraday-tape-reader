"""Server酱（sctapi.ftqq.com）通知器。

``POST https://sctapi.ftqq.com/{sendkey}.send``，form 编码 ``title`` + ``desp``
（``desp`` 为 markdown，包含 code/name/price/pct/detail）。响应 JSON 中 ``code == 0`` 视为成功。
"""
from __future__ import annotations

import json
import logging
import urllib.parse
from dataclasses import dataclass
from typing import Any

from ..models import Alert
from .webhook import (
    DEFAULT_TIMEOUT,
    MAX_ATTEMPTS,
    RETRY_DELAY,
    Poster,
    _dry_run_dump,
    default_poster,
    kind_label,
    post_with_retry,
    resolve_dry_run,
    severity_of,
)

__all__ = ["NOTIFIER_NAME", "API_TEMPLATE", "ServerChanNotifier", "build"]

NOTIFIER_NAME = "serverchan"
API_TEMPLATE = "https://sctapi.ftqq.com/{sendkey}.send"
log = logging.getLogger(__name__)


def _md_alert(alert: Alert) -> str:
    """Markdown 正文（code / name / price / pct / detail 全都要有）。"""
    sign = "+" if float(alert.pct) >= 0 else ""
    lines = [
        f"### {kind_label(alert.kind)} {alert.code} {alert.name}",
        "",
        f"- 代码：`{alert.code}`",
        f"- 名称：{alert.name}",
        f"- 现价：{float(alert.price):.2f}",
        f"- 涨跌幅：{sign}{float(alert.pct):.2f}%",
        f"- 时间：{alert.ts.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 级别：{severity_of(alert)}",
    ]
    metrics = getattr(alert, "metrics", None) or {}
    if metrics:
        lines.append("- 指标：" + "，".join(f"{k}={v}" for k, v in metrics.items()))
    if alert.detail:
        lines += ["", "```", str(alert.detail).strip(), "```"]
    return "\n".join(lines)


def _md_digest(alerts: list[Alert]) -> str:
    lines = [f"### 盘中雷达 · {len(alerts)} 条告警", "", "| 代码 | 名称 | 现价 | 涨跌幅 | 类型 |", "|---|---|---|---|---|"]
    for a in alerts:
        sign = "+" if float(a.pct) >= 0 else ""
        lines.append(
            f"| {a.code} | {a.name} | {float(a.price):.2f} | {sign}{float(a.pct):.2f}% | {kind_label(a.kind)} |"
        )
    return "\n".join(lines)


@dataclass
class ServerChanNotifier:
    """Server酱推送。"""

    sendkey: str = ""
    timeout: float = DEFAULT_TIMEOUT
    min_severity: int = 1
    dry_run: bool = False
    enabled: bool = True
    poster: Poster = default_poster
    retry_delay: float = RETRY_DELAY
    url_template: str = API_TEMPLATE
    name: str = NOTIFIER_NAME

    # --- 内部 ---------------------------------------------------------
    def _skip(self, alert: Any) -> bool:
        return severity_of(alert) < int(self.min_severity or 0)

    def _endpoint(self) -> str:
        return self.url_template.replace("{sendkey}", urllib.parse.quote(str(self.sendkey), safe=""))

    def _post_form(self, title: str, desp: str) -> bool:
        body = urllib.parse.urlencode(
            {"title": str(title)[:100], "desp": desp}
        ).encode("utf-8")
        url = self._endpoint()
        if self.dry_run:
            _dry_run_dump(self.name, url, body)
            return True
        if not self.sendkey:
            log.error("%s: 未配置 sendkey，跳过发送", self.name)
            return False
        headers = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
        status, text = post_with_retry(
            self.poster, url, body, headers, self.timeout,
            attempts=MAX_ATTEMPTS, delay=self.retry_delay,
        )
        if not (200 <= int(status or 0) < 300):
            log.warning("%s: 推送失败 status=%s body=%s", self.name, status, str(text)[:300])
            return False
        try:
            payload = json.loads(text)
        except Exception:
            log.warning("%s: 响应非 JSON，按失败处理: %s", self.name, str(text)[:200])
            return False
        if isinstance(payload, dict) and int(payload.get("code", -1) or 0) == 0:
            return True
        log.warning("%s: 业务返回失败: %s", self.name, str(payload)[:300])
        return False

    # --- Notifier 协议 -------------------------------------------------
    def send(self, alert: Alert) -> bool:
        try:
            if not self.enabled or self._skip(alert):
                return True
            title = f"{kind_label(alert.kind)} {alert.code} {alert.name} {float(alert.pct):+.2f}%"
            return self._post_form(title, _md_alert(alert))
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
            title = f"盘中雷达 · {len(items)} 条告警"
            return self._post_form(title, _md_digest(items))
        except Exception:
            log.exception("%s.send_digest 异常（已忽略，不影响引擎）", self.name)
            return False


def build(cfg: dict) -> ServerChanNotifier:
    """由 ``notify.serverchan`` 节构造。"""
    cfg = dict(cfg or {})
    return ServerChanNotifier(
        sendkey=str(cfg.get("sendkey") or cfg.get("key") or "").strip(),
        timeout=float(cfg.get("timeout") or DEFAULT_TIMEOUT),
        min_severity=int(cfg.get("min_severity") if cfg.get("min_severity") is not None else 1),
        dry_run=resolve_dry_run(cfg),
        enabled=bool(cfg.get("enabled", True)),
        poster=cfg.get("poster") or default_poster,
        retry_delay=float(cfg.get("retry_delay") if cfg.get("retry_delay") is not None else RETRY_DELAY),
        url_template=str(cfg.get("url_template") or API_TEMPLATE),
        name=str(cfg.get("name") or NOTIFIER_NAME),
    )
