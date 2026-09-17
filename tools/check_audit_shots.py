"""校验审计截图是不是"真内容"（防止存下一堆空白图当证据）。

用 stdlib 解 PNG（zlib + struct）：统计非背景色像素比例与颜色种类。
一张 1440x900 的正常看板截图，颜色种类应该上百、且大量非纯色像素；
空白图/纯色图会立刻露馅。

跑法：``python tools\\check_audit_shots.py``
"""
from __future__ import annotations

import struct
import sys
import zlib
from collections import Counter
from pathlib import Path

SHOTS = Path(__file__).resolve().parent / "audit_shots"


def read_png(path: Path) -> tuple[int, int, list[tuple[int, int, int]]]:
    """极简 PNG 解码：只支持 8bit RGB/RGBA 非隔行（Playwright 存的就是这种）。"""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG")
    pos, idat, w, h, depth, ctype = 8, bytearray(), 0, 0, 0, 0
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", body[:10])
            if depth != 8 or ctype not in (2, 6):
                raise ValueError(f"不支持的 PNG 格式 depth={depth} ctype={ctype}")
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
        pos += 12 + ln

    bpp = 4 if ctype == 6 else 3
    raw = zlib.decompress(bytes(idat))
    stride = w * bpp
    out: list[tuple[int, int, int]] = []
    prev = bytearray(stride)
    p = 0
    for _ in range(h):
        f = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        # 反 filters（Playwright 常用 0/1/2/3/4）
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if f == 1:
                line[i] = (line[i] + a) & 0xFF
            elif f == 2:
                line[i] = (line[i] + b) & 0xFF
            elif f == 3:
                line[i] = (line[i] + (a + b) // 2) & 0xFF
            elif f == 4:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        prev = line
        out.extend((line[i], line[i + 1], line[i + 2])
                   for i in range(0, stride, bpp))
    return w, h, out


def main() -> int:
    if not SHOTS.exists():
        print(f"✗ 找不到截图目录：{SHOTS}")
        return 1
    shots = sorted(SHOTS.glob("*.png"))
    if not shots:
        print(f"✗ {SHOTS} 里没有 PNG")
        return 1

    print(f"{'文件':<40}{'尺寸':<13}{'色数':>7}{'主色占比':>10}"
          f"{'墨迹行':>7}{'墨迹占比':>8}  判定")
    print("-" * 78)
    bad: list[str] = []
    for p in shots:
        try:
            w, h, px = read_png(p)
        except Exception as exc:  # noqa: BLE001
            print(f"{p.name:<40}{'解析失败':<13}{'-':>7}{'-':>10}  ✗ {exc}")
            bad.append(p.name)
            continue
        cnt = Counter(px)
        colors = len(cnt)
        top_color, top_n = cnt.most_common(1)[0]
        top_ratio = top_n / max(1, len(px))

        # 判据要**尺度不变**。曾经用「主色占比 < 92%」，但那个判据是错的：
        # `#spiritList` 是滚动容器，局部截图会把**全部可滚动内容**一起截下来，
        # 而文字只占顶部一小段 —— 一张 420×4604 的图里 81% 高度是合法空白，
        # 主色占比自然冲到 94%，于是把一张正常截图误判成空白图。
        # （它一度「变好」只是因为行高从 46.5px 降到 23.2px、图变矮了，
        #   属于运气，不是判据修好了。）
        #
        # 改成数**有内容的行**（ink rows）：一行里只要存在任何一个与主色
        # （即背景色）不同的像素，这一行就算有内容。空白区域不产生 ink row，
        # 所以「图很高但内容很少」不再影响判定，而真正的空白图 ink_rows == 0。
        ink_rows = 0
        ink_px = 0
        for y in range(h):
            row = px[y * w:(y + 1) * w]
            n = sum(1 for c in row if c != top_color)
            if n:
                ink_rows += 1
                ink_px += n
        ink_ratio = ink_px / max(1, len(px))

        # 真截图：颜色多、有若干条文字行、有可见墨迹。
        # 空白/纯色图：颜色极少，或一行墨迹都没有。
        ok = colors >= 50 and ink_rows >= 3 and ink_ratio >= 0.001
        if not ok:
            bad.append(p.name)
        print(f"{p.name:<40}{f'{w}x{h}':<13}{colors:>7}{top_ratio:>9.1%}"
              f"{ink_rows:>7}{ink_ratio:>8.2%}  "
              f"{'✓' if ok else '✗ 疑似空白/纯色'}")

    print("\n" + "=" * 78)
    if bad:
        print(f"✗ {len(bad)} 张截图可疑：{bad}")
        return 1
    print(f"✓ {len(shots)} 张截图都是真实渲染内容")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
