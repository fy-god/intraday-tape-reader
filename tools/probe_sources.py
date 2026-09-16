"""Live probe of A-share quote data sources.

Records RAW payloads into fixtures/raw/ so that parser unit tests can run
fully offline against ground truth captured from the real endpoints.

Run:  python tools/probe_sources.py
"""
from __future__ import annotations

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

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
TIMEOUT = 20

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


def main() -> int:
    print("=" * 78)
    print("A-share source probe")
    print("=" * 78)

    uni = timed("eastmoney clist universe (full market)", build_universe)
    if not uni:
        print("universe probe failed - aborting")
        return 1
    print(f"       universe rows={len(uni)}  sample={uni[0]}")

    # raw fixture: first 100 rows, verbatim response
    raw_first = http_get(
        "https://push2.eastmoney.com/api/qt/clist/get?"
        + urllib.parse.urlencode({
            "pn": 1, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "fid": "f3", "fs": FS_ALL, "fields": EM_FIELDS,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281"})
    )
    (RAW / "eastmoney_clist_p1.txt").write_bytes(raw_first)

    prefixed = [
        ("sh" if c.startswith(("6", "9", "5")) else "bj" if c.startswith(("4", "8")) else "sz") + c
        for c in (str(r["f12"]) for r in uni)
    ]
    print(f"       prefixed codes={len(prefixed)} sample={prefixed[:6]}")

    # --- tencent: one shot, whole market, in 800-code chunks
    t0 = time.perf_counter()
    bodies = timed(f"tencent bulk whole market ({len(prefixed)} codes, 800/chunk)", lambda: tencent_bulk(prefixed))
    if bodies:
        lines = sum(len([x for x in b.split("\n") if x.strip()]) for b in bodies)
        nbytes = sum(len(b) for b in bodies)
        print(f"       tencent lines={lines} bytes={nbytes} wall={time.perf_counter()-t0:.2f}s")
        (RAW / "tencent_bulk_sample.txt").write_text("\n".join(bodies)[:200000], encoding="utf-8")

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
        (RAW / "eastmoney_ulist_5.txt").write_text(r, encoding="utf-8")
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
        (RAW / "sina_bulk_5.txt").write_text(s, encoding="utf-8")
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
        (RAW / "eastmoney_trends2_600000.txt").write_text(t, encoding="utf-8")
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
    (RAW / "universe_sample.json").write_text(
        json.dumps(slim, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwrote fixtures/raw/ : {sorted(p.name for p in RAW.iterdir())}")
    print("\nDONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
