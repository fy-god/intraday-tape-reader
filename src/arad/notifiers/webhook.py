"""通用 Webhook 通知器：把 ``alert.to_dict()`` 以 JSON POST 到 ``cfg['url']``。

本模块同时提供通知层共用的几个小工具（仅标准库实现）：

* :func:`default_poster` —— 默认 HTTP POST 实现（``urllib.request``，超时 5s）；
* :func:`post_with_retry` —— 失败重试 1 次（共 2 次尝试）的包装；
* :func:`format_url` —— URL 模板占位符替换（值一律 ``urllib.parse.quote`` 转义）；
* :func:`resolve_dry_run` / :func:`severity_of` / :func:`alert_values`。

契约（docs/DATA_CONTRACT.md 第 6 节）：
``send()`` / ``send_digest()`` **绝不抛异常**，任何失败只记日志并返回 ``False``。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from ..config import load_settings
from ..models import Alert

__all__ = [
    "NOTIFIER_NAME",
    "DEFAULT_TIMEOUT",
    "MAX_ATTEMPTS",
    "Poster",
    "WebhookNotifier",
    "build",
    "default_poster",
    "post_with_retry",
    "format_url",
    "resolve_dry_run",
    "severity_of",
    "alert_values",
    "alert_payload",
    "kind_label",
    "KIND_LABELS",
    "_dry_run_dump",
    "_dry_run_console",
]

NOTIFIER_NAME = "webhook"
DEFAULT_TIMEOUT = 5.0
MAX_ATTEMPTS = 2          # 首次 + 重试 1 次
RETRY_DELAY = 0.3         # 重试前的等待（秒）
log = logging.getLogger(__name__)

#: 注入用的 HTTP 发送函数签名：``poster(url, data: bytes, headers: dict, timeout: float) -> (status, body)``
Poster = Callable[[str, bytes, dict, float], tuple]

KIND_LABELS: dict[str, str] = {
    "surge": "急拉",
    "plunge": "急跌",
    "limit_up": "涨停",
    "limit_down": "跌停",
    "volume_burst": "放量",
    "unusual": "异动",
}

# URL 模板中允许出现的占位符（值一律 quote 后替换）
_URL_FIELDS = (
    "key", "kind", "code", "name", "ts", "price", "pct",
    "title", "detail", "severity", "text", "count",
)


# ---------------------------------------------------------------------------
# 共用小工具
# ---------------------------------------------------------------------------
def resolve_dry_run(cfg: dict | None) -> bool:
    """dry_run 优先取 cfg['dry_run']，未配置时回落到全局 settings（app.dry_run）。"""
    if isinstance(cfg, dict) and cfg.get("dry_run") is not None:
        return bool(cfg["dry_run"])
    try:
        return bool(load_settings().dry_run)
    except Exception:  # 配置读不出来时按"安全优先"当作 dry_run
        log.debug("resolve_dry_run: 读取全局 settings 失败，按 dry_run=True 处理", exc_info=True)
        return True


def severity_of(alert: Any) -> int:
    """容错地取 ``alert.severity``；对象没有该属性或值非法时**按 1（提示级）**处理。

    取不到 severity 的告警不应该被静默丢弃，所以保守地当作最低级别。
    """
    try:
        value = getattr(alert, "severity", 1)
    except Exception:
        return 1
    try:
        return int(value)
    except Exception:
        return 1


def kind_label(kind: Any) -> str:
    key = getattr(kind, "value", kind)
    return KIND_LABELS.get(str(key), str(key))


def alert_values(alert: Alert) -> dict[str, Any]:
    """URL 模板可用的值（在 ``to_dict()`` 基础上补一个 ``text``）。

    ``to_dict()`` 自身抛异常时降级为最小字段集，保证 URL 仍能拼出来。
    """
    try:
        values = dict(alert.to_dict())
    except Exception:
        values = {"code": getattr(alert, "code", ""), "name": getattr(alert, "name", "")}
    try:
        values["text"] = alert.one_line()
    except Exception:
        values["text"] = str(values.get("code", ""))
    return values


def alert_payload(alert: Alert) -> dict[str, Any]:
    """单条告警的请求体；``to_dict()`` 失败时抛出（由调用方统一兜底记日志）。"""
    return alert.to_dict()


def format_url(template: str, values: dict[str, Any]) -> str:
    """替换 URL 模板里的 ``{title}`` / ``{text}`` 等占位符。

    只替换白名单字段，且替换值一律 :func:`urllib.parse.quote` 转义（``safe=""``，
    即连 ``/`` 也转义），避免中文、空格、``&``、``?``、``/`` 混进 URL 造成请求错乱。
    """
    tpl = str(template or "")
    if "{" not in tpl or not values:
        return tpl
    out = tpl
    for key in _URL_FIELDS:
        token = "{" + key + "}"
        if token in out and key in values:
            out = out.replace(token, urllib.parse.quote(str(values[key]), safe=""))
    return out


def default_poster(url: str, data: bytes, headers: dict, timeout: float = DEFAULT_TIMEOUT) -> tuple:
    """标准库 POST 实现，返回 ``(status, body_text)``；任何异常都吞掉并返回 ``(0, "")``。"""
    try:
        req = urllib.request.Request(  # 非法 URL 在这一步就会抛，所以放进 try
            url,
            data=data,
            headers=dict(headers or {}),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=float(timeout)) as resp:
            raw = resp.read()
            status = int(getattr(resp, "status", 0) or resp.getcode() or 0)
    except urllib.error.HTTPError as exc:  # 4xx/5xx 仍要读回响应体（里面常有 errcode）
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return int(exc.code or 0), body
    except Exception as exc:  # 超时/连接失败/URL 非法……
        log.debug("default_poster 失败: %s", exc)
        return 0, ""
    return status, raw.decode("utf-8", "replace")


def post_with_retry(
    poster: Poster,
    url: str,
    data: bytes,
    headers: dict,
    timeout: float = DEFAULT_TIMEOUT,
    *,
    attempts: int = MAX_ATTEMPTS,
    delay: float = RETRY_DELAY,
) -> tuple:
    """调用 ``poster``，传输层失败时重试；返回最后一次的 ``(status, body)``。

    只有"没拿到响应"（异常 / status 0 / 4xx / 5xx）才重试；业务错误码由调用方判断。
    """
    tries = max(1, int(attempts or 1))
    last: tuple = (0, "")
    for i in range(tries):
        try:
            status, body = poster(url, data, headers, float(timeout))
            last = (int(status or 0), str(body or ""))
        except Exception as exc:
            log.warning("%s: 请求失败(第 %d/%d 次): %s", NOTIFIER_NAME, i + 1, tries, exc)
            last = (0, "")
        else:
            if 0 < last[0] < 400:
                return last
        if i + 1 < tries and delay:
            try:
                time.sleep(float(delay))
            except Exception:
                pass
    return last


def _is_2xx(status: int) -> bool:
    return 200 <= int(status or 0) < 300


def _dry_run_dump(tag: str, url: str, body: bytes | str, note: str = "") -> None:
    """dry_run 时只打印将要发送的内容（截断，避免刷屏）。"""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    if len(text) > 2000:
        text = text[:2000] + "…(截断)"
    try:
        print(f"[dry-run] {tag} -> {url}{(' ' + note) if note else ''}\n{text}")
    except Exception:  # 控制台编码异常也不能打断引擎
        pass


def _dry_run_console(tag: str, note: str = "") -> None:
    """console 的 dry_run 标记（只打印一行，不打印告警内容，避免与正常输出重复）。"""
    try:
        print(f"[dry-run] {tag}{(' ' + note) if note else ''}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 通知器
# ---------------------------------------------------------------------------
@dataclass
class WebhookNotifier:
    """POST ``alert.to_dict()`` 的 JSON 到 ``url``；返回 2xx 视为成功。"""

    url: str = ""
    timeout: float = DEFAULT_TIMEOUT
    min_severity: int = 1
    dry_run: bool = False
    enabled: bool = True
    poster: Poster = default_poster
    retry_delay: float = RETRY_DELAY
    name: str = NOTIFIER_NAME

    # --- 内部 ---------------------------------------------------------
    def _headers(self) -> dict:
        return {"Content-Type": "application/json; charset=utf-8"}

    def _skip(self, alert: Any) -> bool:
        return severity_of(alert) < int(self.min_severity or 0)

    def _post_json(self, url: str, payload: dict) -> bool:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if self.dry_run:
            _dry_run_dump(self.name, url, body)
            return True
        if not url:
            log.error("%s: 未配置 url，跳过发送", self.name)
            return False
        status, text = post_with_retry(
            self.poster, url, body, self._headers(), self.timeout,
            attempts=MAX_ATTEMPTS, delay=self.retry_delay,
        )
        ok = _is_2xx(status)
        if ok:
            log.debug("%s: 已推送 (%s)", self.name, url)
        else:
            log.warning("%s: 推送失败 status=%s body=%s", self.name, status, str(text)[:300])
        return ok

    # --- Notifier 协议 -------------------------------------------------
    def send(self, alert: Alert) -> bool:
        try:
            if not self.enabled or self._skip(alert):
                return True
            body = alert_payload(alert)          # 失败由外层 except 统一兜底
            url = format_url(self.url, alert_values(alert))
            return self._post_json(url, body)
        except Exception:
            log.exception("%s.send 异常（已忽略，不影响引擎）", self.name)
            return False

    def send_digest(self, alerts: list[Alert]) -> bool:
        """合并成一条 ``{"count":N,"alerts":[...]}`` 的消息推送。"""
        try:
            items = [a for a in (alerts or []) if not self._skip(a)]
            if not self.enabled or not items:
                return True
            values = {
                "count": len(items),
                "title": f"{len(items)} 条告警",
                "text": " | ".join(a.one_line() for a in items),
            }
            url = format_url(self.url, values)
            return self._post_json(url, {
                "count": len(items),
                "alerts": [a.to_dict() for a in items],
            })
        except Exception:
            log.exception("%s.send_digest 异常（已忽略，不影响引擎）", self.name)
            return False


def build(cfg: dict) -> WebhookNotifier:
    """由 ``config/settings.yaml`` 的 ``notify.webhook`` 节构造。"""
    cfg = dict(cfg or {})
    url = str(cfg.get("url") or cfg.get("webhook") or "").strip()
    return WebhookNotifier(
        url=url,
        timeout=float(cfg.get("timeout") or DEFAULT_TIMEOUT),
        min_severity=int(cfg.get("min_severity") if cfg.get("min_severity") is not None else 1),
        dry_run=resolve_dry_run(cfg),
        enabled=bool(cfg.get("enabled", True)),
        poster=cfg.get("poster") or default_poster,
        retry_delay=float(cfg.get("retry_delay") if cfg.get("retry_delay") is not None else RETRY_DELAY),
        name=str(cfg.get("name") or NOTIFIER_NAME),
    )
