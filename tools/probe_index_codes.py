"""验证指数代码与前缀的冲突：同一个 6 位码在不同前缀下是不同标的。"""
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
