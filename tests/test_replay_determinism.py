"""回放确定性回归测试：同 seed 必须**跨进程**字节级可复现。

真实缺陷（IT-P1-REPLAY-DETERMINISM-001）
---------------------------------------
``replay.py`` 文档承诺「同 seed + 同参数字节级可复现」，但 ``generate_script``
用 ``hash(s.code)`` 播种。CPython 对 ``str`` 的 ``hash()`` 按 ``PYTHONHASHSEED``
**每进程随机化**，于是同 seed 在不同进程生成**不同**行情。

实测三进程（seed=42）数据指纹互不相同::

    8ba5542d97278e85…  4d73762d9dfef9d7…  95a79558b73e56a9…

固定 ``PYTHONHASHSEED=0`` 后三者全部相同 -> 根因确认。
后果：``selftest`` 告警数在 68~71 之间漂移，**不能当回归判据**；
历次报告里"selftest N 条告警"的说法都不是稳定不变量。

修复：改用 SHA-256 派生的稳定种子（``_stable_code_seed``）。

---
验牙说明
--------
本文件刻意**只用修复前已存在的 API**（``Replay``、``generate_script``），
不引用 ``_stable_code_seed`` 等新符号 —— 否则回退时会整文件 ImportError，
那是结构性的，证明不了行为。跨进程比对必须开子进程（同进程内 ``hash()``
的随机化种子是固定的，测不出这个 bug）。
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from datetime import datetime

import pytest

from arad.replay import Replay, generate_script

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_FINGERPRINT_CHILD = r"""
import hashlib, sys
sys.path.insert(0, sys.argv[1] + r"\src")
from arad.replay import Replay
rp = Replay(seed=42, minutes=40)
h = hashlib.sha256()
for code in sorted(rp.frames):
    for q in rp.frames[code]:
        h.update(("%s|%.4f|%.1f\n" % (q.code, q.price, q.volume_lots)).encode())
sys.stdout.write(h.hexdigest())
"""


def _fingerprint_in_subprocess() -> str:
    """在**独立进程**里取数据指纹（关键：同进程测不出 hash 随机化）。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = "src"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("PYTHONHASHSEED", None)        # 确保是默认的随机化状态
    r = subprocess.run([sys.executable, "-c", _FINGERPRINT_CHILD, REPO],
                       capture_output=True, cwd=REPO, env=env)
    return r.stdout.decode("utf-8", "replace").strip()


def _fingerprint_inline(seed: int = 42) -> str:
    rp = Replay(seed=seed, minutes=40)
    h = hashlib.sha256()
    for code in sorted(rp.frames):
        for q in rp.frames[code]:
            h.update(("%s|%.4f|%.1f\n" % (q.code, q.price, q.volume_lots)).encode())
    return h.hexdigest()


# --------------------------------------------------------------------------
# 1. 核心：跨进程可复现（这是修复前**真的会红**的测试）
# --------------------------------------------------------------------------
@pytest.mark.slow
def test_replay_is_reproducible_across_processes():
    """同 seed 在两个独立进程里必须产出相同数据。

    修复前：两个指纹不同 -> AssertionError（行为级真验牙）。
    修复后：相同。
    """
    a = _fingerprint_in_subprocess()
    b = _fingerprint_in_subprocess()
    assert a, "子进程未产出指纹（收集失败？）"
    assert a == b, (
        "同 seed 跨进程不可复现 —— hash() 随机化回归了？\n"
        f"  {a}\n  {b}")


def test_replay_is_reproducible_within_process():
    """同进程内可复现（这条修复前后都该绿，作为对照）。"""
    assert _fingerprint_inline() == _fingerprint_inline()


def test_different_seeds_give_different_data():
    """不同 seed 必须给出不同数据 —— 防止"修复"变成忽略 seed。"""
    assert _fingerprint_inline(seed=42) != _fingerprint_inline(seed=43)


def test_seed_is_actually_used():
    """seed 必须真正参与生成，不能退化成常量。"""
    a = Replay(seed=1, minutes=10)
    b = Replay(seed=999, minutes=10)
    assert a.frames != b.frames


# --------------------------------------------------------------------------
# 2. 直接钉住"用稳定哈希而不是内置 hash"这个契约
# --------------------------------------------------------------------------
def test_generate_script_does_not_depend_on_builtin_hash():
    """把 ``PYTHONHASHSEED`` 变掉，生成结果必须**不变**。

    这是同一缺陷的另一种证法：内置 ``hash()`` 会随该环境变量改变，
    稳定哈希不会。修复前这条会红。
    """
    day = datetime(2026, 9, 21, 9, 30, 0)
    fps = []
    for hs in ("0", "1", "12345"):
        env = dict(os.environ)
        env["PYTHONPATH"] = "src"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONHASHSEED"] = hs
        r = subprocess.run([sys.executable, "-c", _FINGERPRINT_CHILD, REPO],
                           capture_output=True, cwd=REPO, env=env)
        fps.append(r.stdout.decode("utf-8", "replace").strip())
    assert len(set(fps)) == 1, (
        "生成结果随 PYTHONHASHSEED 变化 = 仍在用内置 hash()；\n  " + "\n  ".join(fps))


# --------------------------------------------------------------------------
# 3. 告警数现在必须是稳定量（此前 68~71 漂移）
# --------------------------------------------------------------------------
@pytest.mark.slow
def test_selftest_alert_count_is_stable_across_processes():
    """``selftest`` 的告警数跨进程必须一致 —— 此前是 68/69/71 随机。

    这就把"selftest N 条告警"从漂移描述升级成可用的回归判据。
    """
    counts = []
    for _ in range(3):
        env = dict(os.environ)
        env["PYTHONPATH"] = "src"
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop("PYTHONHASHSEED", None)
        r = subprocess.run([sys.executable, "-m", "arad.cli", "selftest"],
                           capture_output=True, cwd=REPO, env=env)
        t = r.stdout.decode("utf-8", "replace")
        line = next((l for l in t.splitlines() if "共" in l and "条告警" in l), "")
        n = "".join(ch for ch in line.split("共")[-1].split("条")[0] if ch.isdigit())
        counts.append(n)
    assert len(set(counts)) == 1, f"selftest 告警数仍在漂移: {counts}"
