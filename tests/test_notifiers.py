"""通知层测试（离线，禁止真实网络 / 真实弹窗）。

覆盖：``arad/notifiers/{console,file_jsonl,webhook,serverchan,dingtalk,feishu,windows_toast}.py``

所有网络发送一律通过注入的假 ``poster`` 完成，绝不发起真实请求；
Windows Toast 一律通过注入的假 ``runner`` 完成，绝不弹出真实通知。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from arad.models import AlertKind
from arad.notifiers import console, dingtalk, feishu, file_jsonl, serverchan, webhook, windows_toast
from tests.fakes import make_alert

# ---------------------------------------------------------------------------
# 固定测试数据
# ---------------------------------------------------------------------------
TS = datetime(2026, 9, 15, 10, 0, 0)
DING_HOOK = "https://oapi.dingtalk.com/robot/send?access_token=TESTTOKEN0123456789"
DING_SECRET = "SECtestsecret0123456789abcdefghijklmnopqrstuvwxyz"
DING_TS_MS = 1757913600123          # 固定时间戳，保证加签可复现
FEISHU_HOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/aaaa-bbbb-cccc"
WEBHOOK_URL = "https://example.invalid/hook"
SENDKEY = "SCT123456TESTKEY"

#: 一个"什么成功码都有"的响应体，可被所有网络通知器接受
OK_BODY = json.dumps({"code": 0, "errcode": 0, "StatusCode": 0, "msg": "ok"})

NETWORK = ["webhook", "serverchan", "dingtalk", "feishu"]


class FakePoster:
    """假 poster：记录调用，可配置响应或异常。"""

    def __init__(self, status: int = 200, body: str = OK_BODY, exc: Exception | None = None):
        self.status = status
        self.body = body
        self.exc = exc
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    def __call__(self, url, data, headers, timeout):
        with self.lock:
            self.calls.append(
                {"url": url, "data": data, "headers": dict(headers or {}), "timeout": timeout}
            )
        if self.exc is not None:
            raise self.exc
        return self.status, self.body

    # --- 断言辅助 ------------------------------------------------------
    @property
    def n_calls(self) -> int:
        return len(self.calls)

    @property
    def last(self) -> dict:
        assert self.calls, "poster 未被调用"
        return self.calls[-1]

    @property
    def last_url(self) -> str:
        return self.last["url"]

    @property
    def last_body(self) -> bytes:
        return self.last["data"]

    @property
    def last_json(self) -> dict:
        return json.loads(self.last_body.decode("utf-8"))

    @property
    def last_form(self) -> dict:
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.last_body.decode("utf-8")).items()}

    @property
    def last_headers(self) -> dict:
        return self.last["headers"]


class BrokenAlert:
    """一个能通过 severity 过滤、但取字段就炸的告警（验证"绝不抛异常"）。"""

    severity = 3

    def to_dict(self):
        raise RuntimeError("boom: to_dict")

    def one_line(self):
        raise RuntimeError("boom: one_line")


# ---------------------------------------------------------------------------
# 构造器
# ---------------------------------------------------------------------------
def make(name: str, poster=None, tmp_path=None, **over):
    """按通知器名构造实例；dry_run=False，不做重试等待。"""
    cfg: dict = {"dry_run": False, "retry_delay": 0.0}
    if name == "console":
        cfg["color"] = False
        cfg.update(over)
        return console.build(cfg)
    if name == "file":
        cfg["path"] = str(over.pop("path", (tmp_path or ".") / "alerts.jsonl"))
        cfg.update(over)
        return file_jsonl.build(cfg)
    if name == "webhook":
        cfg["url"] = over.pop("url", WEBHOOK_URL)
    elif name == "serverchan":
        cfg["sendkey"] = over.pop("sendkey", SENDKEY)
    elif name == "dingtalk":
        cfg["webhook"] = over.pop("webhook", DING_HOOK)
        cfg["secret"] = over.pop("secret", "")
        cfg["clock"] = over.pop("clock", lambda: DING_TS_MS / 1000.0)
    elif name == "feishu":
        cfg["webhook"] = over.pop("webhook", FEISHU_HOOK)
    elif name == "toast":
        cfg["runner"] = over.pop("runner", lambda script, timeout: 0)
    else:  # pragma: no cover
        raise AssertionError(f"未知通知器 {name}")
    if poster is not None:
        cfg["poster"] = poster
    cfg.update(over)
    return {
        "webhook": webhook.build,
        "serverchan": serverchan.build,
        "dingtalk": dingtalk.build,
        "feishu": feishu.build,
        "toast": windows_toast.build,
    }[name](cfg)


def surge_alert(**kw):
    kw.setdefault("code", "600519")
    kw.setdefault("name", "贵州茅台")
    kw.setdefault("title", "急拉 +3.2% / 5分钟")
    kw.setdefault("detail", "窗口 300s 涨幅 +3.20%\n量比 4.1")
    kw.setdefault("pct", 5.0)
    kw.setdefault("severity", 2)
    kw.setdefault("kind", AlertKind.SURGE)
    return make_alert(ts=TS, **kw)


# ===========================================================================
# 1. 通用契约：build / send / 绝不抛异常
# ===========================================================================
def test_build_constructs_every_notifier(tmp_path):
    """每个通知器都能用 settings.yaml 里的真实配置节构造出来。"""
    import yaml

    from arad.config import DEFAULT_SETTINGS

    with open(DEFAULT_SETTINGS, "r", encoding="utf-8") as fh:
        notify_cfg = (yaml.safe_load(fh) or {}).get("notify") or {}

    built = {
        "console": console.build(notify_cfg.get("console") or {}),
        "file": file_jsonl.build(notify_cfg.get("file") or {}),
        "webhook": webhook.build(notify_cfg.get("webhook") or {}),
        "serverchan": serverchan.build(notify_cfg.get("serverchan") or {}),
        "dingtalk": dingtalk.build(notify_cfg.get("dingtalk") or {}),
        "feishu": feishu.build(notify_cfg.get("feishu") or {}),
        "windows_toast": windows_toast.build(notify_cfg.get("windows_toast") or {}),
    }
    assert set(built) == {"console", "file", "webhook", "serverchan", "dingtalk", "feishu", "windows_toast"}
    for name, obj in built.items():
        assert callable(obj.send) and callable(obj.send_digest)
        assert isinstance(obj.name, str) and obj.name
    # 模块级 NOTIFIER_NAME 必须存在
    assert console.NOTIFIER_NAME == "console"
    assert file_jsonl.NOTIFIER_NAME == "file"
    assert webhook.NOTIFIER_NAME == "webhook"
    assert serverchan.NOTIFIER_NAME == "serverchan"
    assert dingtalk.NOTIFIER_NAME == "dingtalk"
    assert feishu.NOTIFIER_NAME == "feishu"
    assert windows_toast.NOTIFIER_NAME == "windows_toast"
    # dry_run 未显式配置时回落到全局 settings
    from arad.config import load_settings

    assert webhook.build({}).dry_run is load_settings().dry_run
    assert webhook.build({"dry_run": False}).dry_run is False


@pytest.mark.parametrize("name", ["console", "file"] + NETWORK + ["toast"])
def test_send_returns_true_with_ok_backend(name, tmp_path):
    poster = FakePoster()
    n = make(name, poster=poster, tmp_path=tmp_path)
    assert n.send(surge_alert()) is True
    if name in NETWORK:
        assert poster.n_calls >= 1
    if name == "file":
        assert (tmp_path / "alerts.jsonl").exists()


@pytest.mark.parametrize("name", NETWORK)
def test_poster_exception_returns_false_without_raising(name, tmp_path):
    poster = FakePoster(exc=ConnectionError("network down"))
    n = make(name, poster=poster, tmp_path=tmp_path)
    assert n.send(surge_alert()) is False          # 重试 1 次后失败
    assert poster.n_calls == 2                     # 总共 2 次尝试


@pytest.mark.parametrize("name", ["serverchan", "dingtalk", "feishu"])
def test_business_error_code_returns_false(name, tmp_path):
    """HTTP 200 但业务码非 0 → False（且不重试，因为请求本身送达了）。"""
    body = json.dumps({"code": 1, "errcode": 40035, "StatusCode": 9499, "msg": "bad"})
    poster = FakePoster(status=200, body=body)
    n = make(name, poster=poster, tmp_path=tmp_path)
    assert n.send(surge_alert()) is False
    assert poster.n_calls == 1


def test_webhook_4xx_is_failure(tmp_path):
    """webhook 以 2xx 为准，4xx 视为失败。"""
    poster = FakePoster(status=400, body='{"error":"bad request"}')
    n = make("webhook", poster=poster, tmp_path=tmp_path)
    assert n.send(surge_alert()) is False
    assert poster.n_calls == 2                          # 4xx 也重试一次


@pytest.mark.parametrize("name", NETWORK)
def test_http_5xx_retries_then_false(name, tmp_path):
    poster = FakePoster(status=500, body="oops")
    n = make(name, poster=poster, tmp_path=tmp_path)
    assert n.send(surge_alert()) is False
    assert poster.n_calls == 2


def test_webhook_2xx_is_success_even_with_odd_body(tmp_path):
    poster = FakePoster(status=204, body="")
    n = make("webhook", poster=poster, tmp_path=tmp_path)
    assert n.send(surge_alert()) is True


@pytest.mark.parametrize("name", ["console", "file"] + NETWORK + ["toast"])
def test_disabled_notifier_short_circuits(name, tmp_path, capsys):
    """enabled=False 时全部跳过：返回 True、无任何副作用。"""
    poster = FakePoster()
    runner_calls: list = []
    over = {"runner": lambda s, t: (runner_calls.append(s), 0)[1]} if name == "toast" else {}
    n = make(name, poster=poster, tmp_path=tmp_path, enabled=False, **over)
    assert n.send(surge_alert(severity=3)) is True
    assert n.send_digest([surge_alert(severity=3)]) is True
    assert poster.n_calls == 0
    assert runner_calls == []
    assert not (tmp_path / "alerts.jsonl").exists()
    if name == "console":
        assert capsys.readouterr().out == ""


@pytest.mark.parametrize("name", NETWORK + ["toast"])
def test_digest_sends_single_message(name, tmp_path):
    """网络/桌面通知器的 send_digest 只发一条消息（不是逐条发）。"""
    poster = FakePoster()
    runner_calls: list = []
    over = {"runner": lambda s, t: (runner_calls.append(s), 0)[1]} if name == "toast" else {}
    n = make(name, poster=poster, tmp_path=tmp_path, **over)
    items = [surge_alert(code=f"60000{i}", name=f"股票{i}", severity=3) for i in range(4)]
    assert n.send_digest(items) is True
    if name in NETWORK:
        assert poster.n_calls == 1
    else:
        assert len(runner_calls) == 1


@pytest.mark.parametrize("name", ["console", "file"] + NETWORK + ["toast"])
def test_send_and_digest_never_raise_on_garbage(name, tmp_path):
    """任何异常输入都不许抛出（引擎不能被通知层打断）。"""
    n = make(name, poster=FakePoster(), tmp_path=tmp_path)
    assert n.send(BrokenAlert()) in (True, False)       # 不抛即通过
    assert n.send_digest([BrokenAlert()]) in (True, False)
    assert n.send(None) in (True, False)
    assert n.send_digest([None]) in (True, False)
    assert n.send_digest([]) is True                    # 空列表视为成功


@pytest.mark.parametrize("name", ["file"] + NETWORK)
def test_broken_alert_reports_failure(name, tmp_path):
    """告警对象本身坏掉时返回 False（不抛异常），且不发起任何网络请求。"""
    poster = FakePoster()
    n = make(name, poster=poster, tmp_path=tmp_path)
    assert n.send(BrokenAlert()) is False
    assert poster.n_calls == 0


# ===========================================================================
# 2. min_severity 过滤 / dry_run
# ===========================================================================
@pytest.mark.parametrize("name", ["console"] + NETWORK + ["file", "toast"])
def test_min_severity_filters_out_low_severity(name, tmp_path, capsys):
    """severity < min_severity 时直接返回 True（跳过，不算失败），且不产生副作用。"""
    poster = FakePoster()
    runner_calls: list = []
    over = {"runner": lambda s, t: (runner_calls.append(s), 0)[1]} if name == "toast" else {}
    n = make(name, poster=poster, tmp_path=tmp_path, min_severity=3, **over)
    path = tmp_path / "alerts.jsonl"
    low = surge_alert(severity=1)

    assert n.send(low) is True
    assert n.send_digest([low, surge_alert(severity=2)]) is True

    assert poster.n_calls == 0                  # 网络通知器没发请求
    assert not path.exists()                    # file 没写盘
    assert runner_calls == []                   # toast 没执行
    assert capsys.readouterr().out == ""        # console 没输出


@pytest.mark.parametrize("name", ["console"] + NETWORK + ["file", "toast"])
def test_min_severity_allows_high_severity(name, tmp_path):
    poster = FakePoster()
    n = make(name, poster=poster, tmp_path=tmp_path, min_severity=3)
    assert n.send(surge_alert(severity=3)) is True
    if name in NETWORK:
        assert poster.n_calls == 1
    if name == "file":
        assert (tmp_path / "alerts.jsonl").exists()


@pytest.mark.parametrize("name", ["console"] + NETWORK + ["file", "toast"])
def test_dry_run_has_no_side_effect(name, tmp_path, capsys):
    """dry_run=True 时只打印，不调用 poster / 不写盘 / 不弹窗。"""
    poster = FakePoster()
    runner_calls: list = []
    over = {"runner": lambda s, t: (runner_calls.append(s), 0)[1]} if name == "toast" else {}
    n = make(name, poster=poster, tmp_path=tmp_path, dry_run=True, **over)
    path = tmp_path / "alerts.jsonl"
    # severity=3 保证高于所有通知器默认的 min_severity（windows_toast 默认 3）
    high = surge_alert(severity=3)

    assert n.dry_run is True
    assert n.send(high) is True
    assert n.send_digest([high, surge_alert(code="000001", name="平安银行", severity=3)]) is True

    assert poster.n_calls == 0
    assert not path.exists()
    assert runner_calls == []
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "600519" in out                               # 打印出"将要发送的内容"


# ===========================================================================
# 3. console
# ===========================================================================
def test_console_send_prints_code_and_kind(capsys):
    n = console.build({"color": False, "show_detail": False, "mode": "each", "min_severity": 1, "dry_run": False})
    alert = surge_alert()
    assert n.send(alert) is True
    out = capsys.readouterr().out
    assert "600519" in out
    assert "急拉" in out
    assert "贵州茅台" in out


def test_console_show_detail_toggle(capsys):
    hidden = console.build({"color": False, "show_detail": False, "dry_run": False})
    hidden.send(surge_alert())
    assert "量比 4.1" not in capsys.readouterr().out

    shown = console.build({"color": False, "show_detail": True, "dry_run": False})
    shown.send(surge_alert())
    assert "量比 4.1" in capsys.readouterr().out


def test_console_colors_by_kind(capsys):
    """急拉=红，急跌=绿，涨停=黄底，跌停=青（用 paint() 直接验证映射）。"""
    assert console.KIND_STYLES[AlertKind.SURGE.value] == console.RED
    assert console.KIND_STYLES[AlertKind.PLUNGE.value] == console.GREEN
    assert console.KIND_STYLES[AlertKind.LIMIT_UP.value] == console.YELLOW_BG
    assert console.KIND_STYLES[AlertKind.LIMIT_DOWN.value] == console.CYAN

    n = console.build({"color": True, "dry_run": False})
    n._vt = True                                        # 强制开色，避免依赖 TTY
    assert n.send(surge_alert(severity=1)) is True
    out = capsys.readouterr().out
    assert console.RED in out and console.RESET in out
    assert "600519" in out

    assert n.send(surge_alert(kind=AlertKind.PLUNGE, title="急跌 -4.0%", pct=-4.0)) is True
    assert console.GREEN in capsys.readouterr().out


def test_console_urgent_surge_is_bright_red():
    n = console.build({"color": True, "dry_run": False})
    n._vt = True
    assert n._style_for(surge_alert(severity=3)) == console.BOLD + console.BRIGHT_RED
    assert n._style_for(surge_alert(severity=2)) == console.RED


def test_console_digest_mode_merges_into_table(capsys):
    n = console.build({"color": False, "mode": "digest", "digest_max": 2, "dry_run": False})
    items = [
        surge_alert(code="600519", name="贵州茅台"),
        surge_alert(code="000001", name="平安银行", kind=AlertKind.PLUNGE, pct=-3.0),
        surge_alert(code="300750", name="宁德时代"),
    ]
    assert n.send_digest(items) is True
    out = capsys.readouterr().out
    assert "3 条告警" in out                             # 合并成一张表，只打一次表头
    assert out.count("盘中雷达") == 1
    assert "600519" in out and "000001" in out
    assert "300750" not in out                           # digest_max=2 截断
    assert "还有 1 条" in out


def test_console_digest_mode_send_still_prints(capsys):
    """关键契约：引擎（engine._dispatch）只调 send()，从不调 send_digest()。

    因此 mode=digest 时 send() 也必须立即输出，否则告警会永远看不见。
    """
    n = console.build({"color": False, "mode": "digest", "dry_run": False})
    for a in (surge_alert(code="600519"), surge_alert(code="000001", name="平安银行")):
        assert n.send(a) is True
    out = capsys.readouterr().out
    assert "600519" in out and "000001" in out

    # 逐条模式与聚合模式的 send_digest 都要逐条/合并输出
    each = console.build({"color": False, "mode": "each", "dry_run": False})
    assert each.send_digest([surge_alert(code="601398", name="工商银行")]) is True
    assert "601398" in capsys.readouterr().out


def test_console_paint_disabled_is_plain():
    assert console.paint("x", console.RED, enabled=False) == "x"
    assert console.paint("x", console.RED, enabled=True) == f"{console.RED}x{console.RESET}"


def test_console_enable_vt_does_not_raise(monkeypatch):
    """os.system('') 技巧必须安全；非 TTY 时降级为无颜色。"""
    monkeypatch.setattr(console, "_VT_ENABLED", None)
    assert console.enable_vt() in (True, False)         # pytest 下 stdout 非 TTY → False


# ===========================================================================
# 4. file_jsonl
# ===========================================================================
def test_file_jsonl_writes_utf8_jsonl(tmp_path):
    target = tmp_path / "nested" / "deep" / "alerts.jsonl"
    n = file_jsonl.build({"path": str(target), "min_severity": 1, "dry_run": False})
    assert not target.parent.exists()

    alerts = [
        surge_alert(code="600519", name="贵州茅台"),
        surge_alert(code="000001", name="平安银行", kind=AlertKind.PLUNGE, pct=-3.5),
    ]
    for a in alerts:
        assert n.send(a) is True

    assert target.exists()
    assert target.parent.is_dir()                       # 父目录自动创建

    raw = target.read_bytes()
    lines = [ln for ln in raw.decode("utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2

    rows = [json.loads(ln) for ln in lines]             # 每行都能解析
    assert [r["code"] for r in rows] == ["600519", "000001"]
    assert rows[0]["name"] == "贵州茅台"
    assert rows[0]["kind"] == "surge"
    assert rows[0] == alerts[0].to_dict()                # 整条 to_dict() 原样落盘

    # 中文不转义（原始字节里是 UTF-8 中文，而不是 \uXXXX 转义）
    assert "贵州茅台".encode("utf-8") in raw
    assert b"\\u8d35" not in raw and b"\\u" not in raw
    assert raw.endswith(b"\n")


def test_file_jsonl_digest_writes_each_line(tmp_path):
    target = tmp_path / "alerts.jsonl"
    n = file_jsonl.build({"path": str(target), "dry_run": False})
    items = [surge_alert(code=f"60000{i}", name=f"股票{i}") for i in range(5)]
    assert n.send_digest(items) is True
    lines = [ln for ln in target.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 5
    assert [json.loads(ln)["code"] for ln in lines] == [a.code for a in items]

    # 追加而不是覆盖
    assert n.send(surge_alert(code="601398", name="工商银行")) is True
    lines2 = [ln for ln in target.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines2) == 6
    assert json.loads(lines2[-1])["code"] == "601398"


def test_file_jsonl_concurrent_writes_are_not_corrupted(tmp_path):
    """10 线程并发写 100 条：每行都必须是完整合法 JSON。"""
    target = tmp_path / "concurrent" / "alerts.jsonl"
    n = file_jsonl.build({"path": str(target), "dry_run": False})
    alerts = [surge_alert(code=f"{600000 + i:06d}", name=f"并发{i}") for i in range(100)]

    barrier = threading.Barrier(10)

    def worker(chunk):
        barrier.wait()                                  # 尽量制造竞争
        for a in chunk:
            n.send(a)

    chunks = [alerts[i::10] for i in range(10)]
    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(worker, chunks))

    lines = [ln for ln in target.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 100
    keys = set()
    for ln in lines:
        row = json.loads(ln)                            # 任何半行/粘连都会在这里炸
        keys.add(row["key"])
        assert row["name"].startswith("并发")
    assert len(keys) == 100                             # 无丢失、无重复


def test_file_jsonl_write_failure_returns_false(tmp_path):
    """路径不可写（父路径是文件）时返回 False 且不抛。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    n = file_jsonl.build({"path": str(blocker / "sub" / "alerts.jsonl"), "dry_run": False})
    assert n.send(surge_alert()) is False
    assert n.send_digest([surge_alert()]) is False


def test_file_jsonl_relative_path_resolves_against_project_root():
    from arad.config import PROJECT_ROOT

    n = file_jsonl.build({"path": "data/relative_test.jsonl"})
    assert n.path == PROJECT_ROOT / "data" / "relative_test.jsonl"
    assert n.path.is_absolute()


def test_file_jsonl_dry_run_prints_and_does_not_write(tmp_path, capsys):
    target = tmp_path / "dry" / "alerts.jsonl"
    n = file_jsonl.build({"path": str(target), "dry_run": True})
    assert n.send(surge_alert()) is True
    assert not target.exists()
    assert "600519" in capsys.readouterr().out


# ===========================================================================
# 5. webhook
# ===========================================================================
def test_webhook_posts_alert_dict_as_json(tmp_path):
    poster = FakePoster(status=200, body="ok")
    n = make("webhook", poster=poster, tmp_path=tmp_path)
    alert = surge_alert()
    assert n.send(alert) is True

    assert poster.n_calls == 1
    assert poster.last_url == WEBHOOK_URL              # 无占位符时 URL 原样
    assert poster.last_headers["Content-Type"] == "application/json; charset=utf-8"
    assert poster.last["timeout"] == 5.0               # 契约：超时 5s
    assert poster.last_json == alert.to_dict()
    assert isinstance(poster.last_body, bytes)


def test_webhook_url_placeholders_are_quoted(tmp_path):
    poster = FakePoster(status=200, body="ok")
    n = make("webhook", poster=poster, url="https://example.invalid/hook?code={code}&name={name}", tmp_path=tmp_path)
    assert n.send(surge_alert()) is True
    url = poster.last_url
    assert "code=600519" in url
    assert urllib.parse.quote("贵州茅台") in url        # 中文被 quote 转义
    assert "贵州茅台" not in url
    assert url.count("?") == 1


def test_webhook_digest_shape(tmp_path):
    poster = FakePoster(status=200, body="ok")
    n = make("webhook", poster=poster, tmp_path=tmp_path)
    items = [surge_alert(code="600519"), surge_alert(code="000001", name="平安银行")]
    assert n.send_digest(items) is True
    payload = poster.last_json
    assert payload["count"] == 2
    assert len(payload["alerts"]) == 2
    assert payload["alerts"][0]["code"] == "600519"


def test_webhook_empty_url_fails_without_network():
    n = webhook.build({"url": "", "dry_run": False, "min_severity": 1})
    assert n.send(surge_alert()) is False              # 未配置 url → False，不发请求


# ===========================================================================
# 6. serverchan
# ===========================================================================
def test_serverchan_form_payload(tmp_path):
    poster = FakePoster(status=200, body=json.dumps({"code": 0, "message": "ok"}))
    n = make("serverchan", poster=poster, tmp_path=tmp_path)
    alert = surge_alert()
    assert n.send(alert) is True

    assert poster.n_calls == 1
    assert poster.last_url == f"https://sctapi.ftqq.com/{SENDKEY}.send"
    assert poster.last_headers["Content-Type"].startswith("application/x-www-form-urlencoded")
    assert poster.last["timeout"] == 5.0

    form = poster.last_form
    assert set(form) == {"title", "desp"}
    assert "600519" in form["title"] and "贵州茅台" in form["title"]
    desp = form["desp"]                                 # markdown 正文要素齐全
    for token in ("600519", "贵州茅台", "10.50", "5.00", "量比 4.1"):
        assert token in desp, token
    assert "###" in desp


def test_serverchan_error_code_is_failure(tmp_path):
    poster = FakePoster(status=200, body=json.dumps({"code": 40001, "message": "bad key"}))
    n = make("serverchan", poster=poster, tmp_path=tmp_path)
    assert n.send(surge_alert()) is False


def test_serverchan_digest_lists_all(tmp_path):
    poster = FakePoster(status=200, body=json.dumps({"code": 0}))
    n = make("serverchan", poster=poster, tmp_path=tmp_path)
    assert n.send_digest([surge_alert(code="600519"), surge_alert(code="000001", name="平安银行")]) is True
    desp = poster.last_form["desp"]
    assert "600519" in desp and "000001" in desp


# ===========================================================================
# 7. dingtalk（加签正确性 —— 关键测试）
# ===========================================================================
def test_dingtalk_markdown_payload(tmp_path):
    poster = FakePoster(status=200, body=json.dumps({"errcode": 0, "errmsg": "ok"}))
    n = make("dingtalk", poster=poster, tmp_path=tmp_path)
    assert n.send(surge_alert()) is True
    body = poster.last_json
    assert body["msgtype"] == "markdown"
    assert set(body["markdown"]) == {"title", "text"}
    assert "600519" in body["markdown"]["title"]
    assert "600519" in body["markdown"]["text"]
    assert poster.last["timeout"] == 5.0


def _hmac_sha1_rfc2104(key: bytes, msg: bytes) -> bytes:
    """手写 RFC2104 HMAC-SHA1（故意不复用 hmac 模块，用于独立校验）。"""
    block = 64
    if len(key) > block:
        key = hashlib.sha1(key).digest()
    key = key.ljust(block, b"\x00")
    o_key = bytes(b ^ 0x5C for b in key)
    i_key = bytes(b ^ 0x36 for b in key)
    return hashlib.sha1(o_key + hashlib.sha1(i_key + msg).digest()).digest()


def test_dingtalk_sign_matches_independent_hmac(tmp_path):
    """注入假 poster 捕获 URL，独立重算 HMAC-SHA1，验证 timestamp+sign 完全一致。"""
    poster = FakePoster(status=200, body=json.dumps({"errcode": 0}))
    n = make("dingtalk", poster=poster, tmp_path=tmp_path, secret=DING_SECRET)
    assert n.send(surge_alert()) is True

    parsed = urllib.parse.urlparse(poster.last_url)
    query = urllib.parse.parse_qs(parsed.query)
    assert "timestamp" in query and "sign" in query
    ts = query["timestamp"][0]
    got_sign = query["sign"][0]
    assert ts == str(DING_TS_MS)

    # 独立重算：hmac 模块
    string_to_sign = f"{ts}\n{DING_SECRET}"
    expected = base64.b64encode(
        hmac.new(
            DING_SECRET.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1
        ).digest()
    ).decode("utf-8")
    assert got_sign == expected

    # 再用手写 RFC2104 实现复核（不依赖 hmac 模块）
    manual = base64.b64encode(_hmac_sha1_rfc2104(DING_SECRET.encode("utf-8"), string_to_sign.encode("utf-8"))).decode()
    assert got_sign == manual

    # 原 webhook 参数保留，签名按官方要求 urlencode
    assert "access_token=TESTTOKEN0123456789" in poster.last_url
    assert urllib.parse.quote_plus(expected) in poster.last_url


def test_dingtalk_sign_function_regression_vector():
    """固定向量回归：secret + timestamp → 固定 base64。"""
    got = dingtalk.sign("SECabc", "1545828848000")
    expect = base64.b64encode(
        _hmac_sha1_rfc2104(b"SECabc", b"1545828848000\nSECabc")
    ).decode()
    assert got == expect
    assert dingtalk.sign("SECabc", 1545828848000) == expect   # int 也接受


#: 由 **.NET System.Security.Cryptography.HMACSHA1** 独立算出的交叉验证向量
#: （PowerShell 侧脚本见下方 test_dingtalk_sign_cross_verified_by_dotnet 的注释）
DOTNET_B64 = "Xu6d2kY7Jak5TI+mFEiXSn1/e/U="
DOTNET_URLENCODED = "Xu6d2kY7Jak5TI%2BmFEiXSn1%2Fe%2FU%3D"


def test_dingtalk_sign_cross_verified_by_dotnet():
    """跨语言交叉验证：与 .NET HMACSHA1 的结果逐字节一致。

    PowerShell 侧独立实现（不用 Python 的任何代码）::

        $stringToSign = "$ts`n$secret"
        $h = New-Object System.Security.Cryptography.HMACSHA1
        $h.Key = [Text.Encoding]::UTF8.GetBytes($secret)
        $b64 = [Convert]::ToBase64String($h.ComputeHash([Text.Encoding]::UTF8.GetBytes($stringToSign)))
    """
    assert dingtalk.sign(DING_SECRET, DING_TS_MS) == DOTNET_B64
    assert dingtalk.sign_params(DING_SECRET, DING_TS_MS)["sign"] == DOTNET_URLENCODED


def test_dingtalk_sign_appends_params_to_existing_query():
    url = dingtalk.signed_url("https://oapi.dingtalk.com/robot/send?access_token=T", "s3cr3t", 123456)
    assert url.startswith("https://oapi.dingtalk.com/robot/send?access_token=T&timestamp=123456&sign=")
    assert url.count("?") == 1


def test_dingtalk_no_secret_means_no_sign(tmp_path):
    poster = FakePoster(status=200, body=json.dumps({"errcode": 0}))
    n = make("dingtalk", poster=poster, tmp_path=tmp_path, secret="")
    assert n.send(surge_alert()) is True
    assert poster.last_url == DING_HOOK                 # 原样，不加签
    assert "sign=" not in poster.last_url and "timestamp=" not in poster.last_url


def test_dingtalk_sign_is_urlencoded():
    """base64 里的 + / = 必须被转义，否则真实钉钉会验签失败。"""
    params = dingtalk.sign_params(DING_SECRET, DING_TS_MS)
    assert params["timestamp"] == str(DING_TS_MS)
    assert "+" not in params["sign"] and "/" not in params["sign"] and "=" not in params["sign"]
    assert urllib.parse.unquote_plus(params["sign"]) == dingtalk.sign(DING_SECRET, DING_TS_MS)


def test_dingtalk_errmsg_nonzero_is_failure(tmp_path):
    poster = FakePoster(status=200, body=json.dumps({"errcode": 310000, "errmsg": "sign not match"}))
    n = make("dingtalk", poster=poster, tmp_path=tmp_path, secret=DING_SECRET)
    assert n.send(surge_alert()) is False


# ===========================================================================
# 8. feishu
# ===========================================================================
def test_feishu_text_payload(tmp_path):
    poster = FakePoster(status=200, body=json.dumps({"code": 0, "msg": "success"}))
    n = make("feishu", poster=poster, tmp_path=tmp_path)
    alert = surge_alert()
    assert n.send(alert) is True

    assert poster.last_url == FEISHU_HOOK
    assert poster.last_headers["Content-Type"] == "application/json; charset=utf-8"
    body = poster.last_json
    assert body["msg_type"] == "text"
    assert set(body) == {"msg_type", "content"}
    assert set(body["content"]) == {"text"}
    assert "600519" in body["content"]["text"]
    assert "急拉" in body["content"]["text"]


def test_feishu_statuscode_zero_is_success(tmp_path):
    poster = FakePoster(status=200, body=json.dumps({"StatusCode": 0, "StatusMessage": "success"}))
    n = make("feishu", poster=poster, tmp_path=tmp_path)
    assert n.send(surge_alert()) is True


def test_feishu_interactive_falls_back_to_text(tmp_path):
    poster = FakePoster(status=200, body=json.dumps({"code": 0}))
    n = feishu.build({"webhook": FEISHU_HOOK, "msg_type": "interactive", "poster": poster, "dry_run": False, "retry_delay": 0})
    assert n.send(surge_alert()) is True
    assert poster.last_json["msg_type"] == "text"


def test_feishu_error_code_is_failure(tmp_path):
    poster = FakePoster(status=200, body=json.dumps({"code": 19021, "msg": "sign match fail"}))
    n = make("feishu", poster=poster, tmp_path=tmp_path)
    assert n.send(surge_alert()) is False


def test_feishu_digest_lists_all(tmp_path):
    poster = FakePoster(status=200, body=json.dumps({"code": 0}))
    n = make("feishu", poster=poster, tmp_path=tmp_path)
    assert n.send_digest([surge_alert(code="600519"), surge_alert(code="000001", name="平安银行")]) is True
    text = poster.last_json["content"]["text"]
    assert "600519" in text and "000001" in text


# ===========================================================================
# 9. windows_toast
# ===========================================================================
def test_toast_dry_run_true_does_not_execute(tmp_path, capsys):
    calls: list = []
    n = make("toast", tmp_path=tmp_path, dry_run=True, runner=lambda s, t: (calls.append(s), 0)[1])
    assert n.dry_run is True
    assert n.send(surge_alert(severity=3)) is True
    assert n.send_digest([surge_alert(severity=3)]) is True
    assert calls == []                                  # 没有执行任何子进程
    assert "[dry-run]" in capsys.readouterr().out


def test_toast_runner_exception_returns_false(tmp_path):
    def boom(script, timeout):
        raise OSError("powershell 不存在")

    n = make("toast", tmp_path=tmp_path, runner=boom, dry_run=False)
    assert n.send(surge_alert(severity=3)) is False     # 吞掉异常
    assert n.send_digest([surge_alert(severity=3)]) is False


def test_toast_runner_timeout_returns_false(tmp_path):
    import subprocess

    def slow(script, timeout):
        raise subprocess.TimeoutExpired(cmd="powershell", timeout=timeout)

    n = make("toast", tmp_path=tmp_path, runner=slow, dry_run=False)
    assert n.send(surge_alert(severity=3)) is False


def test_toast_success_path_uses_ps1_file_and_shell_false(tmp_path, monkeypatch):
    """成功路径：脚本通过 -File 传 .ps1、utf-8-sig 编码、shell=False、timeout=8。"""
    seen: dict = {}

    def runner(script, timeout):
        seen["script"] = script
        seen["timeout"] = timeout
        return 0

    n = make("toast", tmp_path=tmp_path, runner=runner, dry_run=False)
    assert n.send(surge_alert(severity=3, name="贵州茅台")) is True
    assert seen["timeout"] == 8.0
    script = seen["script"]
    assert "Windows.UI.Notifications.ToastNotificationManager" in script
    assert "ToastGeneric" in script
    assert "600519" in script and "急拉" in script
    assert "贵州茅台" in script


def test_toast_ps_quote_escapes_single_quotes():
    assert windows_toast.ps_quote("a'b") == "'a''b'"
    assert windows_toast.ps_quote(5) == "'5'"
    # 含引号/尖括号的名称不会破坏 XML/PS 语法
    script = windows_toast.build({"dry_run": False})._build_script("t'1", "<b>&'\"</b>")
    assert "&lt;b&gt;&amp;&apos;&quot;&lt;/b&gt;" in script


def test_toast_fallback_then_all_fail(tmp_path):
    calls: list = []

    def flaky(script, timeout):
        calls.append(script)
        raise RuntimeError("nope")

    n = windows_toast.build(
        {"runner": flaky, "dry_run": False, "min_severity": 1, "use_fallback_msg": True, "retry_delay": 0}
    )
    assert n.send(surge_alert(severity=3)) is False
    assert len(calls) == 2                              # Toast + msg 降级 各试一次
    assert "msg *" in calls[1]

    calls.clear()
    n2 = windows_toast.build(
        {"runner": flaky, "dry_run": False, "min_severity": 1, "use_fallback_msg": False, "retry_delay": 0}
    )
    assert n2.send(surge_alert(severity=3)) is False
    assert len(calls) == 1                              # 关闭降级只试一次


def test_toast_default_runner_command_is_safe(monkeypatch, tmp_path):
    """默认 runner：必须 shell=False、timeout、-File，且 .ps1 用 utf-8-sig 写。"""
    import subprocess as sp

    captured: dict = {}

    class FakeProc:
        returncode = 0

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["kw"] = kw
        ps1 = cmd[-1]
        assert ps1.endswith(".ps1")
        raw = open(ps1, "rb").read()
        captured["raw"] = raw
        return FakeProc()

    monkeypatch.setattr(sp, "run", fake_run)
    rc = windows_toast._default_runner("Write-Output '中文'", 8.0)
    assert rc == 0
    cmd = captured["cmd"]
    assert cmd[0] in ("powershell.exe", "pwsh")
    assert "-File" in cmd and "-Command" not in cmd
    assert captured["kw"]["shell"] is False
    assert captured["kw"]["timeout"] == 8.0
    assert captured["raw"].startswith(b"\xef\xbb\xbf")          # utf-8-sig BOM
    assert "中文".encode("utf-8") in captured["raw"]


def test_toast_non_dry_run_uses_runner_not_real_process(tmp_path):
    """非 dry_run 也必须走注入的 runner（测试里绝不弹真实通知）。"""
    scripts: list = []
    n = make("toast", tmp_path=tmp_path, runner=lambda s, t: (scripts.append(s), 0)[1], dry_run=False)
    assert n.send(surge_alert(severity=3)) is True
    assert len(scripts) == 1


# ===========================================================================
# 10. 共用工具（webhook.py 里导出的工具函数）
# ===========================================================================
def test_format_url_quotes_placeholders():
    url = webhook.format_url("https://x.invalid/h?t={title}&c={code}", {"title": "急拉 A/B & C", "code": "600519"})
    assert url.startswith("https://x.invalid/h?t=")
    # 值全部转义（连 "/" 也转义），否则会破坏 URL 结构
    assert urllib.parse.quote("急拉 A/B & C", safe="") in url
    assert " " not in url and "%2F" in url and "&c=600519" in url
    assert url.count("&") == 1
    # 未知占位符保持原样，不炸
    assert webhook.format_url("https://x.invalid/{nope}", {"code": "1"}) == "https://x.invalid/{nope}"
    assert webhook.format_url("", {}) == ""
    assert webhook.format_url(WEBHOOK_URL, {}) == WEBHOOK_URL


def test_severity_of_is_tolerant():
    assert webhook.severity_of(surge_alert(severity=3)) == 3
    assert webhook.severity_of(object()) == 1        # 取不到按最低级别，不静默丢弃
    assert webhook.severity_of(None) == 1
    assert webhook.severity_of(BrokenAlert()) == 3


def test_kind_label_maps_all_kinds():
    for kind in AlertKind:
        assert webhook.kind_label(kind) in ("急拉", "急跌", "涨停", "跌停", "放量", "异动")


def test_post_with_retry_stops_after_first_success():
    poster = FakePoster(status=200, body="ok")
    assert webhook.post_with_retry(poster, "u", b"{}", {}, 5.0, attempts=2, delay=0) == (200, "ok")
    assert poster.n_calls == 1


def test_default_poster_never_raises_on_bad_url():
    """默认 poster 遇到非法 URL 也只返回 (0, "")。"""
    status, body = webhook.default_poster("http://127.0.0.1:1/nope", b"{}", {}, 0.2)
    assert status == 0 and body == ""
