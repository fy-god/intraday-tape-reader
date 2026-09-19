"""仓库卫生：防止"跑测试的前提"被破坏（IT-P0-004 / IT-P0-005）。

为什么需要这个文件
------------------
`pyproject.toml` 曾经被带 UTF-8 BOM 提交（提交 `3477820`）。`tomllib`
不接受 BOM，于是 `pytest` 启动时解析 `[tool.pytest.ini_options]` 直接失败：

    ERROR: pyproject.toml: Invalid statement (at line 1, column 1)
    exit code: 4

一个测试都没收集。README 记录的命令 `python -m pytest -q` 完全跑不起来，
而这是"回归通过"这一结论的前提。这种缺陷不会让任何单测变红（因为测试
根本没跑），所以必须由本文件在**能跑起来之后**守住。
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOM = b"\xef\xbb\xbf"
PYPROJECT = ROOT / "pyproject.toml"


# ---------------------------------------------------------------------------
# pyproject.toml
# ---------------------------------------------------------------------------
def test_pyproject_exists():
    assert PYPROJECT.is_file()


def test_pyproject_has_no_bom():
    """有 BOM -> tomllib 解析失败 -> pytest 全线无法启动。"""
    raw = PYPROJECT.read_bytes()
    assert not raw.startswith(BOM), (
        "pyproject.toml 开头有 UTF-8 BOM；tomllib 不接受，pytest 会直接退出"
        "（exit 4）。请以 utf-8（无 BOM）保存。")


def test_pyproject_parses_with_tomllib():
    """用标准库解析 —— 这是 pytest 自己会做的事，有 BOM 必红。"""
    raw = PYPROJECT.read_bytes()
    data = tomllib.loads(raw.decode("utf-8"))
    assert isinstance(data, dict)


def test_pytest_ini_options_present():
    """防"修 BOM 时顺手把配置节删了"。"""
    data = tomllib.loads(PYPROJECT.read_bytes().decode("utf-8"))
    assert "pytest" in data.get("tool", {}), "[tool.pytest.ini_options] 不见了"


def test_project_metadata_intact():
    data = tomllib.loads(PYPROJECT.read_bytes().decode("utf-8"))
    proj = data["project"]
    assert proj["name"] == "intraday-tape-reader"
    assert proj["version"]
    # 纯标准库声明必须还在（唯一第三方依赖是 PyYAML）
    assert "PyYAML" in " ".join(proj.get("dependencies", []))


# ---------------------------------------------------------------------------
# 全仓 BOM 扫描
# ---------------------------------------------------------------------------
SCAN_EXTS = {".py", ".toml", ".cfg", ".ini", ".yaml", ".yml",
             ".json", ".html", ".js"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", "data"}


def test_no_source_file_has_bom():
    """包与配置文件不得带 BOM。

    Python 源码带 BOM 虽然 `import` 能容忍，但会让 diff/工具链出现幽灵首行，
    并且 `src/arad/__init__.py` 确实中过招（IT-P0-005）。
    """
    offenders: list[str] = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in SCAN_EXTS:
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        try:
            if p.read_bytes().startswith(BOM):
                offenders.append(str(p.relative_to(ROOT)))
        except OSError:
            continue
    assert not offenders, f"以下文件带 UTF-8 BOM，请改存为无 BOM：{offenders}"


@pytest.mark.parametrize("rel", [
    "src/arad/__init__.py",
    "src/arad/engine.py",
    "src/arad/cli.py",
    "src/arad/capabilities.py",
])
def test_key_modules_have_no_bom(rel: str):
    p = ROOT / rel
    assert p.is_file(), f"{rel} 不存在"
    assert not p.read_bytes().startswith(BOM), f"{rel} 带 BOM"


def test_arad_init_is_importable_without_bom():
    """`arad/__init__.py` 不带 BOM 时，`__version__` 必须能正常读出来。"""
    import arad
    assert arad.__version__
    assert isinstance(arad.__version__, str)
