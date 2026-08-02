#!/usr/bin/env python3
"""Значок клієнта: чорний квадрат із помаранчевою рамкою екрана.

Робить webui/icon.png — 192×192, палітра з 16 відтінків від чорного до
неонового помаранчевого (--neon у style.css). Згладжування — усередненням
за сіткою 4×4, тому окремої бібліотеки не треба.
"""
import struct
import sys
import zlib

N = 192          # сторона значка
SS = 4           # супервибірка
NEON = (0xFF, 0x7D, 0x1A)

INSET = 22.0     # відступ рамки від краю
THICK = 16.0     # товщина рамки
RAD = 26.0       # радіус заокруглення


def rounded_rect(x, y, x0, y0, x1, y1, r):
    """Чи лежить точка всередині заокругленого прямокутника."""
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= r * r


def coverage(px, py):
    """Частка підпікселів, накритих рамкою."""
    hit = 0
    for sy in range(SS):
        for sx in range(SS):
            x = px + (sx + 0.5) / SS
            y = py + (sy + 0.5) / SS
            outer = rounded_rect(x, y, INSET, INSET, N - INSET, N - INSET, RAD)
            inner = rounded_rect(x, y, INSET + THICK, INSET + THICK,
                                 N - INSET - THICK, N - INSET - THICK,
                                 max(1.0, RAD - THICK))
            if outer and not inner:
                hit += 1
    return hit / (SS * SS)


def chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def main(out):
    levels = 16
    palette = b"".join(bytes(round(c * i / (levels - 1)) for c in NEON)
                       for i in range(levels))

    rows = []
    for py in range(N):
        # Глибина 4 біти: два пікселі в байт.
        row = bytearray([0])          # тип фільтра 0
        acc, half = 0, False
        for px in range(N):
            v = min(levels - 1, int(coverage(px, py) * levels))
            if half:
                row.append(acc | v)
                half = False
            else:
                acc = v << 4
                half = True
        if half:
            row.append(acc)
        rows.append(bytes(row))

    ihdr = struct.pack(">IIBBBBB", N, N, 4, 3, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", ihdr)
           + chunk(b"PLTE", palette)
           + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
           + chunk(b"IEND", b""))
    with open(out, "wb") as f:
        f.write(png)
    print(f"{out}: {len(png)} Б")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("вжиток: tools/make_icon.py <куди-писати.png>\n"
              "  примірник у сховищі:  tools/make_icon.py webui/icon.png\n"
              "  ⚠️ вивід має збігатися байт у байт із webui/icon.png —\n"
              "     це звіряє tools/webui_built_check.py", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])
