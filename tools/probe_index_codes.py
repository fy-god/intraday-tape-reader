"""验证指数代码与前缀的冲突：同一个 6 位码在不同前缀下是不同标的。"""

# Windows 控制台 UTF-8（见 tools/_console.py）。
# 先正常导入；若失败说明本文件是被**按路径**加载的（例如测试用 importlib
# 从 tests/ 里 exec 它），此时 tools/ 不在 sys.path 上——把本文件所在目录
# 补进去再试一次，这样"直接跑"和"被当模块加载"两种场景都能用。
try:
    import _console  # noqa: F401,E402
except ImportError:  # pragma: no cover - 取决于调用方式
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    import _console  # noqa: F401,E402
import sys
import urllib.request

sys.path.insert(0, 'src')
from arad.models import guess_prefix, board_of

specs = ("sh000001", "sz000001", "sz399001", "sz399006", "sh000300",
         "sh000688", "sz399005")
print('--- 实时核对（腾讯）---')
for spec in specs:
    url = "https://qt.gtimg.cn/q=" + spec
    req = urllib.request.Request(url, headers={
        "Referer": "https://gu.qq.com/", "User-Agent": "Mozilla/5.0"})
    try:
        raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk", "replace")
        f = raw.split("~")
        print('%-9s -> name=%-12s code=%-8s price=%s'
              % (spec, f[1][:12], f[2], f[3]))
    except Exception as e:
        print('%-9s -> ERR %s' % (spec, e))

print('\n--- guess_prefix 对 6 位码的推断 ---')
for c in ('000001', '399001', '399006', '000300', '000688', '399005'):
    print('%-8s guess_prefix=%-4s board=%-8s'
          % (c, guess_prefix(c), board_of(c, '').value))
print('\n结论：000001 用 guess_prefix 会得到 sz000001（平安银行），')
print('      上证指数必须显式写 sh000001 —— 6 位纯数字码无法区分两者。')
