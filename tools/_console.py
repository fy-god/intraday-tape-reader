"""共享的控制台 UTF-8 引导。**这是被 import 的工具库，不是拿来跑的脚本。**

Windows 控制台默认编码是 GBK，而这些工具会打印 ``✓`` / ``✗`` / ``─`` 这类字符，
于是直接跑就会：

    UnicodeEncodeError: 'gbk' codec can't encode character '\\u2713'

``arad.cli`` 自己有这一步（``_setup_stdout()``），但 ``tools/`` 下的脚本没有，
所以"克隆下来照着 README 跑一条检查命令"在中文 Windows 上会直接崩 ——
这不是使用者的问题，是仓库的问题。

用法（放在其它 import 之前）：

    import _console  # noqa: F401   （导入即生效）

脚本所在目录本来就在 ``sys.path`` 上，所以裸 ``import _console`` 可用。
"""
from __future__ import annotations

import sys

__all__ = ["enable_utf8"]


def enable_utf8() -> bool:
    """把 stdout/stderr 切成 UTF-8。返回是否成功（失败不抛，宁可少个彩蛋也别崩）。"""
    ok = False
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
            ok = True
        except Exception:  # noqa: BLE001  —— 已被重定向/不支持的流，忽略即可
            pass
    return ok


# 导入即生效：调用方不需要再写一行
enable_utf8()
