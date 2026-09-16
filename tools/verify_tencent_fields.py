"""Verify the Tencent qt.gtimg.cn field-index contract arithmetically.

The whole detector depends on these indices being right, so we assert them
against internally-consistent arithmetic (涨跌 = 现价-昨收, 振幅 = (高-低)/昨收,
涨停 = 昨收*1.1 rounded, 成交额 = 成交量手*100*均价) rather than trusting docs.
"""
from __future__ import annotations

import re
from pathlib import Path

RAW = Path(__file__).resolve().parents[1] / "fixtures" / "raw"

# index -> semantic name (0-based, after splitting the payload on '~')
F = {
    1: "name", 2: "code", 3: "price", 4: "prev_close", 5: "open",
    6: "volume_lots", 7: "outer_vol", 8: "inner_vol",
    9: "bid1", 10: "bid1_vol", 11: "bid2", 12: "bid2_vol", 13: "bid3", 14: "bid3_vol",
    15: "bid4", 16: "bid4_vol", 17: "bid5", 18: "bid5_vol",
    19: "ask1", 20: "ask1_vol", 21: "ask2", 22: "ask2_vol", 23: "ask3", 24: "ask3_vol",
    25: "ask4", 26: "ask4_vol", 27: "ask5", 28: "ask5_vol",
    29: "last_tick", 30: "timestamp", 31: "change", 32: "pct",
    33: "high", 34: "low", 35: "price_vol_amount", 36: "volume_lots_2",
    37: "amount_wan", 38: "turnover", 39: "pe", 40: "blank",
    41: "high_2", 42: "low_2", 43: "amplitude", 44: "float_cap_yi",
    45: "total_cap_yi", 46: "pb", 47: "limit_up", 48: "limit_down",
    49: "volume_ratio", 50: "order_diff", 51: "avg_price",
    52: "pe_dynamic", 53: "pe_static",
}


def parse_line(line: str):
    m = re.match(r'v_([a-z]{2}\d{6})="(.*)";?\s*$', line.strip())
    if not m:
        return None, None
    code, payload = m.group(1), m.group(2)
    return code, payload.split("~")


def fnum(parts, idx):
    try:
        return float(parts[idx])
    except (ValueError, IndexError):
        return None


def main() -> int:
    text = (RAW / "tencent_bulk_sample.txt").read_text(encoding="utf-8", errors="replace")
    lines = [ln for ln in text.split("\n") if ln.strip().startswith("v_")]
    print(f"lines={len(lines)}")
    ok = bad = 0
    failures: list[str] = []
    checked = 0
    # per-check tallies: name -> [pass, fail]
    tally: dict[str, list[int]] = {
        k: [0, 0] for k in ("change[31]", "pct[32]", "amplitude[43]", "limit_up[47]", "amount[6,37,51]")
    }

    def rec(name: str, good: bool):
        tally[name][0 if good else 1] += 1
    for ln in lines:
        code, p = parse_line(ln)
        if not p or len(p) < 54:
            continue
        price, prev = fnum(p, 3), fnum(p, 4)
        high, low = fnum(p, 33), fnum(p, 34)
        chg, pct = fnum(p, 31), fnum(p, 32)
        amp, lim_up = fnum(p, 43), fnum(p, 47)
        vol, amt_wan, avg = fnum(p, 6), fnum(p, 37), fnum(p, 51)
        if None in (price, prev, high, low, chg, pct) or not prev or price <= 0:
            continue
        checked += 1
        errs = []
        rec("change[31]", abs((price - prev) - chg) <= 0.011)
        if abs((price - prev) - chg) > 0.011:
            errs.append(f"chg {chg} != {price - prev:.3f}")
        rec("pct[32]", abs((price / prev - 1) * 100 - pct) <= 0.02)
        if abs((price / prev - 1) * 100 - pct) > 0.02:
            errs.append(f"pct {pct} != {(price/prev-1)*100:.3f}")
        if high and low:
            rec("amplitude[43]", abs((high - low) / prev * 100 - (amp or 0)) <= 0.02)
            if abs((high - low) / prev * 100 - (amp or 0)) > 0.02:
                errs.append(f"amp {amp} != {(high-low)/prev*100:.3f}")
        board = 1.2 if code[2:5] in ("300", "301", "688", "689") else 1.1
        # suspended names report limit price -1.0 -> not a contract failure
        if lim_up and lim_up > 0:
            rec("limit_up[47]", abs(lim_up - round(prev * board, 2)) <= 0.011)
            if abs(lim_up - round(prev * board, 2)) > 0.011:
                errs.append(f"limit_up {lim_up} != {round(prev*board,2)}")
        if vol and amt_wan and avg:
            est_yuan = vol * 100 * avg
            good = abs(est_yuan - amt_wan * 10000) / max(est_yuan, 1) <= 0.02
            rec("amount[6,37,51]", good)
            if not good:
                errs.append(f"amount {amt_wan}万 != vol*100*avg={est_yuan/1e4:.0f}万")
        if errs:
            bad += 1
            if len(failures) < 8:
                failures.append(f"  {code} {p[1]}: " + "; ".join(errs))
        else:
            ok += 1
    print(f"checked={checked} consistent={ok} inconsistent={bad}")
    print("\nper-index agreement:")
    for k, (p_, f_) in tally.items():
        tot = p_ + f_
        pct = 100.0 * p_ / tot if tot else 0.0
        print(f"  {k:<18} {p_:>4}/{tot:<4} = {pct:6.2f}%")
    for f in failures:
        print(f)
    print("\nresolved field map:")
    for i in sorted(F):
        print(f"  [{i:>2}] {F[i]}")
    return 0 if bad * 20 < max(checked, 1) else 1


if __name__ == "__main__":
    raise SystemExit(main())
