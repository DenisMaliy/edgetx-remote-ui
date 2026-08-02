#!/usr/bin/env python3
"""Пульт, якого немає: програмний бік пульта для перевірки клієнта.

**Навіщо це є.** Браузерний клієнт інакше перевіряється лише на зібраному
стенді — тобто кожна правка `webui/` коштує прошивання моста і рук людини
біля пульта. Симулятор EdgeTX цю дірку колись закривав, але зараз він не
збирається (немає `clang`, `PROGRESS.md`), і бокові панелі задачі 0022
довелося б віддати людині неперевіреними.

Цей інструмент прикидається **пультом**: слухає TCP на 127.0.0.1:7616 — там
само, де його слухає транспорт симулятора, — говорить тим самим протоколом і
записує весь ввід, який приходить від клієнта. Далі над ним звичайним чином
піднімається `tools/webui_serve.py`, і браузер не бачить різниці.

⚠️ **Він доводить поведінку клієнта, а не пульта.** Усе, що стосується
EdgeTX — прискорення енкодера, довге натискання, автоповтор, малювання, —
тут не відтворюється й відтворюватись не може. Питання, на які він відповідає:
чи пішов пакет, чи пішов **саме той**, чи пішла пара «натиснув-відпустив»
цілком, чи не лишилось натиснутого. Це рівно те, чого не видно зсередини
браузера.

Запуск:

    python3 tools/fake_radio.py --log /tmp/input.jsonl &
    python3 tools/webui_serve.py --port 8080 &
    # далі браузер на http://localhost:8080/

Кожен прийнятий пакет вводу друкується рядком JSON — і в журнал, і в stdout.
"""

import argparse
import json
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))

import remote_ui_proto as proto  # noqa: E402


# Екран TX16S. ⚠️ Числа тут описують **удаваний** пульт, а не наш код:
# клієнт бере їх із HELLO, і саме це перевіряється.
WIDTH, HEIGHT = 480, 272
TILE = 32

# Той самий набір клавіш, який віддає справжній TX16S (`keysGetLabel`).
KEYS = [
    (0, "RTN"),
    (1, "Enter"),
    (2, "PAGE<"),
    (3, "PAGE>"),
    (4, "MDL"),
    (5, "TELE"),
    (6, "SYS"),
]

BAUD_LIST = [921600, 1000000, 1500000, 2000000, 2625000]


def build_hello(baud_current: int) -> bytes:
    """HELLO за `docs/03-protocol.md`. Хвіст зі швидкостями — теж."""
    flags = (proto.HELLO_FLAG_TOUCH | proto.HELLO_FLAG_ENCODER
             | proto.HELLO_FLAG_INPUT_STATE)
    keymask = 0
    for code, _ in KEYS:
        keymask |= 1 << code

    p = struct.pack("<BHHBBBIB", 1, WIDTH, HEIGHT,
                    proto.HELLO_PIXFMT_RGB565, flags, 4, keymask, len(KEYS))
    for code, name in KEYS:
        p += struct.pack("<B", code) + name.encode()[:15].ljust(16, b"\0")
    p += b"FAKE-TX16S".ljust(32, b"\0")
    p += b"fake-2.12.2".ljust(16, b"\0")
    p += struct.pack("<IIB", baud_current, BAUD_LIST[0], len(BAUD_LIST))
    for b in BAUD_LIST:
        p += struct.pack("<I", b)
    return proto.encode_frame(proto.PKT_HELLO, p)


def rle_tile(x: int, y: int, w: int, h: int, color: int) -> bytes:
    """Однотонна плитка RLE16: пари [лічильник][піксель], лічильник 1…255."""
    body = struct.pack("<HHHHB", x, y, w, h, proto.TILE_METHOD_RLE16)
    left = w * h
    while left:
        n = min(255, left)
        body += struct.pack("<BH", n, color)
        left -= n
    return proto.encode_frame(proto.PKT_TILE, body)


class FakeRadio:
    """Один клієнт за раз — як і справжній послідовний порт."""

    def __init__(self, log_path):
        self.log_path = log_path
        self.lock = threading.Lock()
        self.sock = None
        self.baud = BAUD_LIST[0]
        self.decoder = proto.Decoder()
        # Смуга виділення: рухається енкодером, щоб на екрані було видно, що
        # ввід доїжджає, і щоб кадри взагалі йшли.
        self.cursor = 0
        self.events = []

    # --- відправлення ------------------------------------------------------

    def send(self, data: bytes) -> None:
        with self.lock:
            if self.sock is None:
                return
            try:
                self.sock.sendall(data)
            except OSError:
                self.sock = None

    def full_frame(self) -> None:
        """Увесь екран плитками, потім FRAME_END з нулем — кадр цілий."""
        for y in range(0, HEIGHT, TILE):
            for x in range(0, WIDTH, TILE):
                h = min(TILE, HEIGHT - y)
                color = 0x001F if (x // TILE + y // TILE) % 2 else 0x0000
                self.send(rle_tile(x, y, TILE, h, color))
        self.draw_cursor()
        self.send(proto.encode_frame(proto.PKT_FRAME_END, struct.pack("<H", 0)))

    def draw_cursor(self) -> None:
        rows = HEIGHT // TILE
        y = (self.cursor % rows) * TILE
        h = min(TILE, HEIGHT - y)
        self.send(rle_tile(0, y, WIDTH, h, 0xFFE0))

    def move_cursor(self, steps: int) -> None:
        rows = HEIGHT // TILE
        old = (self.cursor % rows) * TILE
        oldh = min(TILE, HEIGHT - old)
        # Стерти стару смугу тим же візерунком, що й тло.
        for x in range(0, WIDTH, TILE):
            color = 0x001F if (x // TILE + old // TILE) % 2 else 0x0000
            self.send(rle_tile(x, old, TILE, oldh, color))
        self.cursor += steps
        self.draw_cursor()
        self.send(proto.encode_frame(proto.PKT_FRAME_END, struct.pack("<H", 0)))

    # --- приймання ---------------------------------------------------------

    def note(self, kind: str, **fields) -> None:
        ev = dict(t=round(time.monotonic(), 4), kind=kind, **fields)
        self.events.append(ev)
        line = json.dumps(ev, ensure_ascii=False)
        print(line, flush=True)
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def on_packet(self, ptype: int, payload: bytes) -> None:
        if ptype == proto.PKT_PING:
            self.send(build_hello(self.baud))

        elif ptype == proto.PKT_REFRESH:
            self.note("refresh")
            self.full_frame()

        elif ptype == proto.PKT_KEY and len(payload) >= 2:
            code, pressed = payload[0], bool(payload[1])
            name = dict(KEYS).get(code, "?")
            self.note("key", code=code, name=name, pressed=pressed)
            # Кадр у відповідь: інакше клієнт не побачить реакції, а його
            # вимірювач затримки не отримає жодного зразка.
            self.move_cursor(0)

        elif ptype == proto.PKT_ENC and len(payload) >= 1:
            steps = struct.unpack("<b", payload[:1])[0]
            self.note("enc", steps=steps)
            self.move_cursor(steps)

        elif ptype == proto.PKT_TOUCH and len(payload) >= 5:
            ev, x, y = struct.unpack("<BHH", payload[:5])
            self.note("touch", event=ev, x=x, y=y)
            self.move_cursor(0)

        elif ptype == proto.PKT_INPUT_STATE and len(payload) >= 13:
            keys, trims, flags, x, y = struct.unpack("<IIBHH", payload[:13])
            self.note("state", keys=keys, trims=trims, down=bool(flags & 1), x=x, y=y)

        elif ptype == proto.PKT_BAUD_SET and len(payload) >= 5:
            baud, nonce = struct.unpack("<IB", payload[:5])
            self.note("baud_set", baud=baud, nonce=nonce)
            ok = baud in BAUD_LIST
            # Формат звіту — 16 байтів (`parseBaud`): вердикт, nonce, ціль,
            # поточна, затримка перемикання, вікно відкоту, лічильник відкотів.
            self.send(proto.encode_frame(
                proto.PKT_BAUD,
                struct.pack("<BBIIHHH", 0 if ok else 1, nonce, baud,
                            baud if ok else self.baud, 0, 500, 0)))
            if ok:
                self.baud = baud
                self.send(build_hello(self.baud))

    # --- цикл --------------------------------------------------------------

    def serve(self, host: str, port: int) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(1)
        print(f"удаваний пульт слухає {host}:{port}", file=sys.stderr)

        while True:
            sock, addr = srv.accept()
            print(f"клієнт {addr}", file=sys.stderr)
            with self.lock:
                self.sock = sock
            self.decoder = proto.Decoder()
            try:
                while True:
                    data = sock.recv(4096)
                    if not data:
                        break
                    for ptype, payload in self.decoder.feed(data):
                        self.on_packet(ptype, payload)
            except OSError:
                pass
            finally:
                with self.lock:
                    self.sock = None
                sock.close()
                print("клієнт пішов", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=proto.DEFAULT_TCP_PORT)
    ap.add_argument("--log", help="куди дописувати рядки JSON із вводом")
    args = ap.parse_args()

    FakeRadio(args.log).serve(args.host, args.port)


if __name__ == "__main__":
    main()
