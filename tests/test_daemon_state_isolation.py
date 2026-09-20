"""守护状态文件按端口分文件（IT-P2-DAEMON-STATE-001）。

**真实复现的缺陷**：状态文件原来固定叫 `arad-daemon.json`，是**单槽**的。
用户一个很自然的用法 —— "正式守护在 8899 盯着盘，另外起个 8905 看回放预览"
—— 就会让后启动的守护**覆盖**先启动的记录：

  实测：8899 守护（PID 12180）还活着，`--status` 却显示 8905 的 PID 31516。
  连带影响：单实例检查也会拿错端口的记录去判"是否重复启动"。

修法：状态写 `arad-daemon-<port>.json`；旧的单槽文件仅在它的 `port`
恰好匹配时作为兼容读取，并继续被刷新作为"最近一个"兜底。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "run_daemon_state", REPO / "tools" / "run_daemon.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rd = _load()


def test_state_file_is_per_port():
    """不同端口必须落到**不同**文件。"""
    a = rd._state_file(8899)
    b = rd._state_file(8905)
    assert a != b, "两个端口共用同一状态文件 —— 会互相覆盖"
    assert "8899" in a.name and "8905" in b.name


def test_state_file_none_keeps_legacy_slot():
    """``port=None`` 仍指向旧单槽文件（兼容 `--status` 无端口）。"""
    assert rd._state_file(None).name == "arad-daemon.json"


def test_write_state_does_not_clobber_other_port(tmp_path, monkeypatch):
    """**核心判据**：写 8905 不能动到 8899 的记录。

    这是缺陷的最小复现 —— 旧实现下第二个断言会失败。
    """
    monkeypatch.setattr(rd, "RUN_DIR", tmp_path)
    rd._write_state(8899, pid=1111, childPid=2222)
    rd._write_state(8905, pid=3333, childPid=4444)

    a = rd._read_state(8899)
    b = rd._read_state(8905)
    assert int(a.get("pid") or 0) == 1111, f"8899 记录被覆盖: {a}"
    assert int(a.get("childPid") or 0) == 2222
    assert int(b.get("pid") or 0) == 3333
    assert int(b.get("childPid") or 0) == 4444


def test_write_state_records_its_own_port(tmp_path, monkeypatch):
    """状态里必须记下自己属于哪个端口（否则归属判断无从谈起）。"""
    monkeypatch.setattr(rd, "RUN_DIR", tmp_path)
    rd._write_state(8899, pid=1111)
    rd._write_state(8905, pid=3333)
    assert int(rd._read_state(8899).get("port") or 0) == 8899
    assert int(rd._read_state(8905).get("port") or 0) == 8905


def test_write_state_tolerates_port_in_kwargs(tmp_path, monkeypatch):
    """``port`` 同时作为位置参数与 kw 传入不得抛 TypeError。

    这是我在实现本修复时**自己踩到的真实崩溃**：
    `_write_state(args.port, port=args.port, ...)` ->
    `TypeError: got multiple values for argument 'port'`，
    守护因此启动即退出。已通过 pop 掉 kw 里的 port 修掉。
    """
    monkeypatch.setattr(rd, "RUN_DIR", tmp_path)
    rd._write_state(8899, pid=1111, port=8899)     # 不应抛异常
    assert int(rd._read_state(8899).get("pid") or 0) == 1111


def test_read_state_absent_port_returns_empty(tmp_path, monkeypatch):
    """没跑过的端口 -> 空 dict（而不是误读别的端口）。"""
    monkeypatch.setattr(rd, "RUN_DIR", tmp_path)
    rd._write_state(8905, pid=3333)
    assert rd._read_state(8899) == {}


def test_legacy_slot_is_still_written(tmp_path, monkeypatch):
    """旧单槽文件仍被刷新（兜底 `--status` 不带端口的场景）。"""
    monkeypatch.setattr(rd, "RUN_DIR", tmp_path)
    rd._write_state(8899, pid=1111)
    legacy = tmp_path / "arad-daemon.json"
    assert legacy.exists(), "旧单槽文件没了 —— 不带端口的 --status 会失效"
    assert int(json.loads(legacy.read_text(encoding="utf-8")).get("pid") or 0) == 1111


def test_read_state_uses_legacy_only_when_port_matches(tmp_path, monkeypatch):
    """兼容读取：旧单槽文件的 port 不匹配时**不能**当成本端口的状态。"""
    monkeypatch.setattr(rd, "RUN_DIR", tmp_path)
    (tmp_path / "arad-daemon.json").write_text(
        json.dumps({"pid": 9999, "port": 8905}), encoding="utf-8")
    assert rd._read_state(8899) == {}, "把 8905 的旧记录当成了 8899 的"
    got = rd._read_state(8905)
    assert int(got.get("pid") or 0) == 9999


def test_list_state_files_finds_all_ports(tmp_path, monkeypatch):
    """能列出所有端口的守护（供 `--status` 展示其它端口）。"""
    monkeypatch.setattr(rd, "RUN_DIR", tmp_path)
    rd._write_state(8899, pid=1)
    rd._write_state(8905, pid=2)
    assert rd._list_state_files() == [8899, 8905]
