"""Live probe of A-share quote data sources.

Records RAW payloads into fixtures/raw/ so that parser unit tests can run
fully offline against ground truth captured from the real endpoints.

Run:  python tools/probe_sources.py

⚠ 这个脚本会**覆盖 fixtures/raw/ 下的样本文件**，而那些文件是 35 处解析器测试
的基线（tests/ 里 load_raw(...) 的调用点）。覆盖的后果很隐蔽：测试仍然全绿，
但它们比对的已经不是你 review 过的那份数据了 —— 若实时接口某天改了格式，
覆盖后测试会「跟着一起改」而不是报错，等于悄悄丢失回归能力。

因此默认**拒绝覆盖已存在的文件**，要求显式 --force。想更新基线时：
    1. python tools/probe_sources.py --force
    2. git diff fixtures/    ← 必须人工看一眼差异，确认只是格式微调
    3. python -m pytest -q   ← 确认解析器仍能应付新格式
"""
from __future__ import annotations

import _console  # noqa: F401,E402  —— Windows 控制台 UTF-8（见 tools/_console.py）

import argparse
import concurrent.futures as cf
import json
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "fixtures" / "raw"
RAW.mkdir(parents=True, exist_ok=True)

# 让 `import arad...` 可用（前缀判定要复用生产代码，不再自己写一份内联规则）。
sys.path.insert(0, str(ROOT / "src"))

#: 由 --force 置位。False 时遇到已存在的夹具文件直接报错退出，
#: 避免"顺手跑一下"就把测试基线洗掉。
FORCE = False


def write_fixture(name: str, data: bytes | str) -> None:
    """写夹具，默认不覆盖已存在的文件。

    ``name`` 是 fixtures/raw/ 下的文件名；存在且未加 --force 时直接退出 ——
    宁可让这个脚本失败，也不要静默替换测试基线。
    """
    p = RAW / name
    if p.exists() and not FORCE:
        sys.exit(
            f"拒绝覆盖已存在的夹具：fixtures/raw/{name}\n"
            f"它是解析器测试的基线（tests/ 里 load_raw 的比对对象）。\n"
            f"确实要更新基线请加 --force，然后务必 git diff fixtures/ 人工核对差异。"
        )
    if isinstance(data, str):
        p.write_text(data, encoding="utf-8")
    else:
        p.write_bytes(data)


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
TIMEOUT = 20

UNIVERSE_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
                "json_v2.php/Market_Center.getHQNodeData")

FS_ALL = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
EM_FIELDS = "f2,f3,f5,f6,f8,f10,f11,f12,f13,f14,f15,f16,f17,f18,f20,f21,f22,f26,f124"


def http_get(url: str, referer: str | None = None, timeout: int = TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    if referer:
        req.add_header("Referer", referer)
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as resp:
        return resp.read()


def timed(label, fn):
    t0 = time.perf_counter()
    try:
        out = fn()
        ms = (time.perf_counter() - t0) * 1000
        print(f"[OK]   {label:<46} {ms:8.1f} ms")
        return out
    except Exception as exc:  # noqa: BLE001
        ms = (time.perf_counter() - t0) * 1000
        print(f"[FAIL] {label:<46} {ms:8.1f} ms  {type(exc).__name__}: {exc}")
        return None


# --------------------------------------------------------------------------
# 1. Eastmoney full-market universe (paginated, page size is hard-capped at 100)
# --------------------------------------------------------------------------
def em_page(pn: int):
    q = urllib.parse.urlencode(
        {
            "pn": pn, "pz": 100, "po": 0, "np": 1, "fltt": 2, "invt": 2,
            "fid": "f12", "fs": FS_ALL, "fields": EM_FIELDS,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        }
    )
    url = "https://push2.eastmoney.com/api/qt/clist/get?" + q
    return json.loads(http_get(url).decode("utf-8", "replace"))


def build_universe(workers: int = 8):
    first = em_page(1)
    total = first["data"]["total"]
    pages = (total + 99) // 100
    print(f"       eastmoney universe total={total} pages={pages}")
    rows = list(first["data"]["diff"])
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(em_page, p): p for p in range(2, pages + 1)}
        for fut in cf.as_completed(futs):
            try:
                rows.extend(fut.result()["data"]["diff"] or [])
            except Exception:  # noqa: BLE001
                pass
    seen, out = set(), []
    for r in rows:
        code = str(r.get("f12", ""))
        if len(code) == 6 and code not in seen:
            seen.add(code)
            out.append(r)
    return out


def sina_universe(pages: int = 80):
    """新浪的股票池（东财被限流时的兜底）。

    返回条目刻意做成和东财 ``diff`` 同形的字典（f12/f14/f2…），
    这样下游的 ``prefixed`` 与 ``slim`` 两处逻辑不用分叉。

    为什么需要它：东财 ``push2.eastmoney.com`` 在很多网络下会被限流
    （``RemoteDisconnected``），而这是**本项目已知的常态降级路径**，
    不是异常。探测脚本若把它当硬失败直接退出，就永远拿不到完整结果。
    """
    out: list[dict] = []
    for pn in range(1, pages + 1):
        q = urllib.parse.urlencode({
            "page": pn, "num": 100, "sort": "symbol", "asc": 1,
            "node": "hs_a", "symbol": "", "_s_r_a": "page"})
        url = (UNIVERSE_URL + "?" + q)
        try:
            body = http_get(url, referer="https://finance.sina.com.cn")
        except Exception:  # noqa: BLE001
            break
        try:
            # 新浪这个列表接口是 **UTF-8 JSON**（不是 hq.sinajs.cn 的 GBK）
            rows = json.loads(body.decode("utf-8", "replace") or "[]")
        except Exception:  # noqa: BLE001
            break
        if not rows:
            break
        for r in rows:
            code = str(r.get("code") or str(r.get("symbol", ""))[2:])
            if len(code) != 6:
                continue
            out.append({
                "f12": code, "f14": r.get("name"),
                "f2": r.get("trade"), "f18": r.get("settlement"),
                "f3": r.get("changepercent"),
                # 新浪的 volume 单位是股，东财是手 -> /100 保持同形
                "f5": (float(r["volume"]) / 100.0) if r.get("volume") else None,
                "f6": r.get("amount"), "f8": r.get("turnoverratio"),
                "f21": r.get("nmc"), "f20": r.get("mktcap"),
                "f26": "",
            })
    seen, uniq = set(), []
    for r in out:
        if r["f12"] not in seen:
            seen.add(r["f12"])
            uniq.append(r)
    return uniq


# --------------------------------------------------------------------------
# 2. Tencent bulk quotes qt.gtimg.cn/q=  (primary intraday tick lane)
# --------------------------------------------------------------------------
def tencent_bulk(codes, chunk=800):
    """codes: list of 'sh600000' style prefixed codes."""
    out = []
    for i in range(0, len(codes), chunk):
        part = codes[i:i + chunk]
        url = "https://qt.gtimg.cn/q=" + ",".join(part)
        body = http_get(url, referer="https://gu.qq.com/").decode("gbk", "replace")
        out.append(body)
    return out


def main(argv: list[str] | None = None) -> int:
    global FORCE
    ap = argparse.ArgumentParser(
        description="探测行情数据源并把原始响应存成离线夹具",
        epilog="⚠ 会覆盖 fixtures/raw/ 下的测试基线，需显式 --force")
    ap.add_argument("--force", action="store_true",
                    help="允许覆盖已存在的夹具（覆盖后请 git diff fixtures/ 核对）")
    args = ap.parse_args(argv)
    FORCE = args.force

    print("=" * 78)
    print("A-share source probe")
    print("=" * 78)

    uni = timed("eastmoney clist universe (full market)", build_universe)
    if not uni:
        # 东财被限流是本项目**已知的常态降级路径**（见 docs/NOTES_eastmoney_sina.md），
        # 不是异常。所以这里降级到新浪继续探，而不是直接退出 ——
        # 否则明明另外 5 个端点都好好的，却什么都拿不到。
        print("       [!] 东财股票池不可用（多半是被限流），降级到新浪…")
        uni = timed("sina universe (fallback)", sina_universe)
    if not uni:
        print("universe probe failed on BOTH sources - aborting")
        return 1
    print(f"       universe rows={len(uni)}  sample={uni[0]}")

    # raw fixture: first 100 rows, verbatim response（东财专用，失败就跳过）
    try:
        raw_first = http_get(
            "https://push2.eastmoney.com/api/qt/clist/get?"
            + urllib.parse.urlencode({
                "pn": 1, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                "fid": "f3", "fs": FS_ALL, "fields": EM_FIELDS,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281"})
        )
        write_fixture("eastmoney_clist_p1.txt", raw_first)
    except Exception as exc:  # noqa: BLE001
        print(f"       [!] 跳过 eastmoney_clist_p1.txt 夹具：{exc}")

    # ⚠ 前缀判定必须与生产代码一致，所以直接复用 `models.guess_prefix`，
    # 不再自己写一份内联规则。这里原来写的是
    #     "sh" if c.startswith(("6","9","5")) else "bj" if c.startswith(("4","8")) else "sz"
    # 它把 **920xxx 判成了沪市**——920 段是北交所（安徽凤凰 920000 就是），
    # 拿去问腾讯会得到空行，探测结果里那批票静默消失。
    # 生产实现把 900/200 段（沪B/深B）单独处理，其余 4/8/9 → bj，才是对的。
    from arad.models import guess_prefix as _guess_prefix
    prefixed = [_guess_prefix(str(r["f12"])) + str(r["f12"]) for r in uni]
    print(f"       prefixed codes={len(prefixed)} sample={prefixed[:6]}")

    # --- tencent: one shot, whole market, in 800-code chunks
    t0 = time.perf_counter()
    bodies = timed(f"tencent bulk whole market ({len(prefixed)} codes, 800/chunk)", lambda: tencent_bulk(prefixed))
    if bodies:
        lines = sum(len([x for x in b.split("\n") if x.strip()]) for b in bodies)
        nbytes = sum(len(b) for b in bodies)
        print(f"       tencent lines={lines} bytes={nbytes} wall={time.perf_counter()-t0:.2f}s")
        # ⚠ 存**完整**响应，不再按 [:200000] 截断。
        # 原来那个是按**字符**切，会把 424 行的样本截成 421 行，
        # 而 tests/test_sources_tencent.py 断言的是 424 行（423 有效 + 1 条畸形）——
        # 截断会让夹具与测试期望对不上，且很难看出是这里造成的。
        # 真要控制体积，应当按**整行**截，而不是按字符。
        write_fixture("tencent_bulk_sample.txt", "\n".join(bodies))

    # --- tencent small batch (watchlist lane)
    timed("tencent bulk 5 codes", lambda: tencent_bulk(["sh600000", "sz000001", "sz300750", "sh688111", "bj430047"], chunk=5))

    # --- eastmoney ulist.np batch (alternative tick lane)
    def em_ulist(codes):
        secids = ",".join(
            ("1." if c.startswith("sh") else "0.") + c[2:] for c in codes
        )
        q = urllib.parse.urlencode({
            "fltt": 2, "invt": 2, "secids": secids,
            "fields": "f2,f3,f5,f6,f8,f12,f14,f15,f16,f17,f18,f20,f21,f22,f124",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281"})
        url = "https://push2.eastmoney.com/api/qt/ulist.np/get?" + q
        return http_get(url).decode("utf-8", "replace")

    r = timed("eastmoney ulist.np 5 codes", lambda: em_ulist(["sh600000", "sz000001", "sz300750", "sh688111", "bj430047"]))
    if r:
        write_fixture("eastmoney_ulist_5.txt", r)
        print("       " + r[:260])

    chunk = prefixed[:800]
    r2 = timed("eastmoney ulist.np 800 codes (url-len test)", lambda: em_ulist(chunk))
    if r2:
        try:
            j = json.loads(r2)
            print(f"       ulist rows={len(j.get('data', {}).get('diff') or [])} bytes={len(r2)}")
        except Exception:  # noqa: BLE001
            print("       non-json response, head=" + r2[:200])

    # --- sina bulk (secondary / cross-check lane)
    def sina_bulk(codes):
        url = "https://hq.sinajs.cn/list=" + ",".join(codes)
        return http_get(url, referer="https://finance.sina.com.cn").decode("gbk", "replace")

    s = timed("sina bulk 5 codes", lambda: sina_bulk(["sh600000", "sz000001", "sz300750", "sh688111", "bj430047"]))
    if s:
        write_fixture("sina_bulk_5.txt", s)
        print("       " + s.replace("\n", " | ")[:240])
    s2 = timed("sina bulk 800 codes", lambda: sina_bulk(chunk))
    if s2:
        lines = len([x for x in s2.split("\n") if x.strip()])
        print(f"       sina lines={lines} bytes={len(s2)}")

    # --- intraday minute bars (for pre-open warmup / gap detection)
    def em_trend(code, market=1):
        q = urllib.parse.urlencode({
            "secid": f"{market}.{code}", "fields1": "f1,f2,f3,f4,f5",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58", "iscr": 0,
            "ndays": 1, "ut": "fa5fd1943c7b386f172d6893dbfba10b"})
        url = "https://push2his.eastmoney.com/api/qt/stock/trends2/get?" + q
        return http_get(url).decode("utf-8", "replace")

    t = timed("eastmoney trends2 1-min bars (600000)", lambda: em_trend("600000"))
    if t:
        write_fixture("eastmoney_trends2_600000.txt", t)
        print("       " + t[:260])

    # --- persist the universe for offline use
    slim = [
        {
            "code": str(r.get("f12")), "name": r.get("f14"),
            "price": r.get("f2"), "prev_close": r.get("f18"),
            "pct": r.get("f3"), "volume": r.get("f5"), "amount": r.get("f6"),
            "turnover": r.get("f8"), "float_cap": r.get("f21"), "total_cap": r.get("f20"),
            "list_date": r.get("f26"),
        }
        for r in uni
    ]
    write_fixture("universe_sample.json",
                  json.dumps(slim, ensure_ascii=False, indent=1))
    print(f"\nwrote fixtures/raw/ : {sorted(p.name for p in RAW.iterdir())}")
    print("\nDONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
