"""``python -m arad`` 入口 —— 等价于 ``python -m arad.cli``。

存在意义只有一个：让"跑起来"这件事不依赖调用者记清模块路径。
``python -m arad serve`` 和 ``python -m arad.cli serve`` 行为完全一致，
所以 README、脚本、计划任务里写哪种都行。

注意：本文件不改变任何导入方式。``arad`` 包仍需可被解释器找到
（仓库内靠 ``PYTHONPATH=src``，或 ``pip install -e .`` 之后全局可用）。
真正的"免配置启动"由 ``tools/run_daemon.py`` 负责——它会自己设好路径。
"""
from __future__ import annotations

from .cli import main

__all__ = ["main"]

if __name__ == "__main__":
    raise SystemExit(main())
