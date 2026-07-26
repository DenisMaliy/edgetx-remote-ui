#!/usr/bin/env python3
"""Перевірка захоплення екрана Remote UI: підключитись, зібрати кадр, зберегти PNG.

Це **не** графічний клієнт (це крок 1.5), а доказ, що протокол справді
переносить пікселі: підключаємось до симулятора, розбираємо пакети, складаємо
кадр і кладемо його у файл.

Заразом рахує те, що потім іде в звіт: скільки байтів пройшло дротом, у скільки
разів стиснуто, скільки плиток пішло сирими, скільки вийшло кадрів за секунду.

Залежностей немає — PNG пишеться через zlib зі стандартної бібліотеки.
Розбір протоколу спільний із тестовим клієнтом: `tools/remote_ui_proto.py`.

    tools/capture_check.py --out docs/img/capture.png
    tools/capture_check.py --seconds 10 --stats-only
"""

import argparse
import binascii
import os
import socket
import struct
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from remote_ui_proto import (  # noqa: E402
    DEFAULT_TCP_PORT as DEFAULT_PORT,
    PKT_FRAME_END,
    PKT_HELLO,
    PKT_LOG,
    PKT_REFRESH,
    PKT_TILE,
    TILE_METHOD_RAW,
    TILE_METHOD_RLE16,
    Decoder,
    encode_frame,
    parse_hello,
    rle16_decode,
)


class Frame:
    """Кадр у пам'яті: RGB565, як на пульті."""

    def __init__(self, width: int, height: int):
        self.w = width
        self.h = height
        self.px = [0] * (width * height)

    def blit(self, x: int, y: int, w: int, h: int, pixels: list) -> bool:
        # Це наш доказ, що захоплення працює, тож він не має права тихо
        # намалювати неправду: зріз за межами списку не впав би, а мовчки
        # подовжив би його й зсунув увесь кадр.
        if x < 0 or y < 0 or x + w > self.w or y + h > self.h:
            return False
        if len(pixels) != w * h:
            return False
        for row in range(h):
            dst = (y + row) * self.w + x
            self.px[dst : dst + w] = pixels[row * w : (row + 1) * w]
        return True

    def to_rgb888(self) -> bytes:
        out = bytearray(self.w * self.h * 3)
        for i, p in enumerate(self.px):
            r = (p >> 11) & 0x1F
            g = (p >> 5) & 0x3F
            b = p & 0x1F
            # Розтягуємо 5/6 бітів на 8, повторюючи старші біти: так білий
            # лишається білим, а не 248-м відтінком сірого.
            out[i * 3] = (r << 3) | (r >> 2)
            out[i * 3 + 1] = (g << 2) | (g >> 4)
            out[i * 3 + 2] = (b << 3) | (b >> 2)
        return bytes(out)


def write_png(path: str, width: int, height: int, rgb: bytes):
    """PNG без сторонніх бібліотек: заголовок, зображення, кінець."""
    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)  # тип фільтра «без фільтра»
        raw.extend(rgb[y * stride : (y + 1) * stride])

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", binascii.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", ihdr))
        fh.write(chunk(b"IDAT", zlib.compress(bytes(raw), 9)))
        fh.write(chunk(b"IEND", b""))


def main() -> int:
    ap = argparse.ArgumentParser(description="Перевірка захоплення екрана Remote UI")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--out", default="capture.png", help="куди зберегти PNG")
    ap.add_argument("--seconds", type=float, default=3.0, help="скільки слухати")
    ap.add_argument("--frames", type=int, default=0, help="вийти після N кадрів (0 = за часом)")
    ap.add_argument("--timeout", type=float, default=30.0, help="стеля за часом для --frames")
    ap.add_argument("--refresh", action="store_true", help="попросити повний кадр")
    ap.add_argument("--stats-only", action="store_true", help="не писати PNG")
    args = ap.parse_args()

    sock = socket.create_connection((args.host, args.port), timeout=5.0)
    sock.settimeout(0.2)

    dec = Decoder()
    hello = None
    frame = None

    # Лічильники ведуться двома наборами: перший повний кадр (його завжди шлють
    # при підключенні) і все, що після нього. Інакше вартість типової дії
    # потонула б у вартості першого кадру.
    stats = {
        "full": dict(rx=0, tiles=0, raw=0, payload=0, pixels=0),
        "delta": dict(rx=0, tiles=0, raw=0, payload=0, pixels=0),
    }
    frames_done = 0

    started = time.monotonic()
    delta_started = None

    if args.refresh:
        sock.sendall(encode_frame(PKT_REFRESH))

    # Стеля за часом діє в обох режимах: якщо FRAME_END не прийде (екран не
    # змінюється), режим --frames інакше чекав би вічно.
    deadline = started + (args.timeout if args.frames else args.seconds)

    while True:
        if time.monotonic() >= deadline:
            if args.frames and frames_done < args.frames:
                print(
                    f"вийшов час: кадрів {frames_done} із {args.frames}",
                    file=sys.stderr,
                )
            break
        if args.frames and frames_done >= args.frames:
            break

        try:
            data = sock.recv(65536)
        except socket.timeout:
            continue
        if not data:
            print("З'єднання закрито пультом", file=sys.stderr)
            break

        phase = "delta" if frames_done else "full"
        stats[phase]["rx"] += len(data)

        for ptype, payload in dec.feed(data):
            if ptype == PKT_HELLO:
                hello = parse_hello(payload)
                frame = Frame(hello["width"], hello["height"])
                print(
                    f"HELLO: {hello['target']} {hello['fw']}, "
                    f"{hello['width']}x{hello['height']}, "
                    f"формат {hello['pixfmt']}, прапорці 0x{hello['flags']:02X}, "
                    f"тримерів {hello['trims']}, клавіш {len(hello['keys'])}"
                )
                print("  клавіші: " + ", ".join(f"{n}({c})" for c, n in hello["keys"]))
            elif ptype == PKT_TILE:
                if frame is None:
                    continue
                x, y, w, h, method = struct.unpack_from("<HHHHB", payload, 0)
                body = payload[9:]
                if method == TILE_METHOD_RAW:
                    pixels = (
                        list(struct.unpack(f"<{w * h}H", body))
                        if len(body) == w * h * 2
                        else None
                    )
                elif method == TILE_METHOD_RLE16:
                    pixels = rle16_decode(body, w * h)
                else:
                    print(f"невідомий метод стиснення {method} — пропущено", file=sys.stderr)
                    continue
                if pixels is None:
                    print(
                        f"битий вміст плитки {w}x{h} @ {x},{y} — пропущено",
                        file=sys.stderr,
                    )
                    continue
                if not frame.blit(x, y, w, h, pixels):
                    print(
                        f"плитка поза екраном або битий розмір: "
                        f"{w}x{h} @ {x},{y} — пропущено",
                        file=sys.stderr,
                    )
                    continue
                bucket = stats["delta" if frames_done else "full"]
                bucket["tiles"] += 1
                bucket["raw"] += 1 if method == TILE_METHOD_RAW else 0
                bucket["payload"] += len(payload)
                bucket["pixels"] += w * h * 2
            elif ptype == PKT_FRAME_END:
                frames_done += 1
                if delta_started is None:
                    delta_started = time.monotonic()
            elif ptype == PKT_LOG:
                print("LOG:", payload.decode("utf-8", "replace"))

    # Останній стан екрана цікавіший за перший: на ньому видно те, що людина
    # встигла понатискати, поки скрипт слухав.
    if frame is not None and not args.stats_only:
        write_png(args.out, frame.w, frame.h, frame.to_rgb888())
        print(f"PNG збережено: {args.out}")

    now = time.monotonic()
    duration = now - started
    delta_duration = (now - delta_started) if delta_started else 0.0

    def report(title: str, bucket: dict, seconds: float, frames: int):
        print(f"--- {title} ---")
        print(f"час                 {seconds:.2f} с")
        rate = bucket["rx"] / seconds / 1024 if seconds > 0 else 0
        print(f"прийнято            {bucket['rx']} Б  ({rate:.1f} КіБ/с)")
        if frames is not None:
            fps = frames / seconds if seconds > 0 else 0
            print(f"кадрів (FRAME_END)  {frames}  ({fps:.1f} за секунду)")
        print(f"плиток              {bucket['tiles']}, з них сирими {bucket['raw']}")
        if bucket["tiles"]:
            print(f"пікселів у плитках  {bucket['pixels']} Б")
            print(f"на дроті (payload)  {bucket['payload']} Б")
            print(f"стиснення           {bucket['pixels'] / bucket['payload']:.2f}x")
        print()

    print()
    report("перший кадр (повний екран)", stats["full"], duration - delta_duration,
           min(frames_done, 1))
    if frames_done > 1:
        report("далі: приріст на дії", stats["delta"], delta_duration, frames_done - 1)

    if hello:
        full = hello["width"] * hello["height"] * 2
        print(f"повний сирий кадр   {full} Б  (для порівняння)")
    if dec.crc_errors:
        print(f"биті кадри          {dec.crc_errors}")

    sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
