"""钉钉自定义机器人通知器（支持加签）。

* 消息体：``{"msgtype":"markdown","markdown":{"title":...,"text":...}}``
* 加签：``string_to_sign = f"{timestamp}\\n{secret}"``，以 ``secret`` 为密钥做
  **HMAC-SHA1**，``base64`` 编码后 ``urlencode``（官方文档用 ``urlencode``，
  即 :func:`urllib.parse.quote_plus`），与 ``timestamp`` 一起拼到 webhook URL；
  无 ``secret`` 时**不加签**。
* 响应 JSON 中 ``errcode == 0`` 视为成功。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
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
    kind_label,
    post_with_retry,
    resolve_dry_run,
    severity_of,
)

__all__ = ["NOTIFIER_NAME", "DingTalkNotifier", "build", "sign_params", "signed_url", "sign"]

NOTIFIER_NAME = "dingtalk"
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 加签（正确性是重点，独立可测）
# ---------------------------------------------------------------------------
def sign(secret: str, timestamp: str | int) -> str:
    """官方加签算法：``base64(hmac_sha1(secret, f"{timestamp}\\n{secret}"))``。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        str(secret).encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def _now_ms() -> int:
    return int(round(time.time() * 1000))


def sign_params(secret: str, timestamp: str | int | None = None, *, signer: Callable = sign) -> dict[str, str]:
    """生成 ``{"timestamp": "...", "sign": "<urlencoded>"}``（secret 为空返回 ``{}``）。"""
    if not secret:
        return {}
    ts = str(timestamp if timestamp is not None else _now_ms())
    raw = signer(secret, ts)
    return {"timestamp": ts, "sign": urllib.parse.quote_plus(str(raw))}


def signed_url(webhook: str, secret: str, timestamp: str | int | None = None, *, signer: Callable = sign) -> str:
    """把 ``timestamp`` / ``sign`` 拼到 webhook URL（无 secret 时原样返回）。"""
    params = sign_params(secret, timestamp, signer=signer)
    if not params:
        return str(webhook or "")
    sep = "&" if "?" in str(webhook) else "?"
    return f"{webhook}{sep}timestamp={params['timestamp']}&sign={params['sign']}"


# ---------------------------------------------------------------------------
# 通知器
# ---------------------------------------------------------------------------
def _md_text(alert: Alert) -> str:
    sign_s = "+" if float(alert.pct) >= 0 else ""
    metrics = getattr(alert, "metrics", None) or {}
    lines = [
        f"### {kind_label(alert.kind)} {alert.code} {alert.name}",
        "",
        f"- 现价：**{float(alert.price):.2f}**（{sign_s}{float(alert.pct):.2f}%）",
        f"- 时间：{alert.ts.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 级别：{severity_of(alert)}",
    ]
    if metrics:
        lines.append("- 指标：" + "，".join(f"{k}={v}" for k, v in metrics.items()))
    if alert.detail:
        lines += ["", "> " + str(alert.detail).strip().replace("\n", "\n> ")]
    return "\n".join(lines)


def _md_text_digest(alerts: list[Alert]) -> str:
    lines = [f"### 盘中雷达 · {len(alerts)} 条告警", ""]
    for a in alerts:
        sign_s = "+" if float(a.pct) >= 0 else ""
        lines.append(
            f"- {kind_label(a.kind)} **{a.code} {a.name}** {float(a.price):.2f} "
            f"({sign_s}{float(a.pct):.2f}%) {a.title}"
        )
    return "\n".join(lines)


@dataclass
class DingTalkNotifier:
    """钉钉机器人（markdown 消息 + 可选加签）。"""

    webhook: str = ""
    secret: str = ""
    timeout: float = DEFAULT_TIMEOUT
    min_severity: int = 1
    dry_run: bool = False
    enabled: bool = True
    poster: Poster = default_poster
    retry_delay: float = RETRY_DELAY
    clock: Callable[[], float] = field(default=time.time)
    name: str = NOTIFIER_NAME

    # --- 内部 ---------------------------------------------------------
    def _skip(self, alert: Any) -> bool:
        return severity_of(alert) < int(self.min_severity or 0)

    def _endpoint(self) -> str:
        ts = int(round(float(self.clock()) * 1000))
        return signed_url(self.webhook, self.secret, ts)

    def _post_markdown(self, title: str, text: str) -> bool:
        payload = {"msgtype": "markdown", "markdown": {"title": str(title), "text": str(text)}}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = self._endpoint()
        if self.dry_run:
            _dry_run_dump(self.name, url, body, note="(含加签)" if self.secret else "(未加签)")
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
        if isinstance(data, dict) and int(data.get("errcode", -1) or 0) == 0:
            return True
        log.warning("%s: 业务返回失败: %s", self.name, str(data)[:300])
        return False

    # --- Notifier 协议 -------------------------------------------------
    def send(self, alert: Alert) -> bool:
        try:
            if not self.enabled or self._skip(alert):
                return True
            title = f"{kind_label(alert.kind)} {alert.code} {alert.name} {float(alert.pct):+.2f}%"
            return self._post_markdown(title, _md_text(alert))
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
            return self._post_markdown(f"盘中雷达 · {len(items)} 条告警", _md_text_digest(items))
        except Exception:
            log.exception("%s.send_digest 异常（已忽略，不影响引擎）", self.name)
            return False


def build(cfg: dict) -> DingTalkNotifier:
    """由 ``notify.dingtalk`` 节构造（``webhook`` 也接受 ``url`` 别名）。"""
    cfg = dict(cfg or {})
    return DingTalkNotifier(
        webhook=str(cfg.get("webhook") or cfg.get("url") or "").strip(),
        secret=str(cfg.get("secret") or "").strip(),
        timeout=float(cfg.get("timeout") or DEFAULT_TIMEOUT),
        min_severity=int(cfg.get("min_severity") if cfg.get("min_severity") is not None else 1),
        dry_run=resolve_dry_run(cfg),
        enabled=bool(cfg.get("enabled", True)),
        poster=cfg.get("poster") or default_poster,
        retry_delay=float(cfg.get("retry_delay") if cfg.get("retry_delay") is not None else RETRY_DELAY),
        clock=cfg.get("clock") or time.time,
        name=str(cfg.get("name") or NOTIFIER_NAME),
    )
