#!/usr/bin/env python3
"""Керування пультом за сценарієм: натиснути, дочекатись кадру, зняти PNG.

Навіщо окремий інструмент, коли є вікно (`tools/test_client.py`). Вікно
доводить, що керувати **можна**, але доказом у задачі є пройдений сценарій, а
сценарій, пройдений руками, неможливо ані повторити, ані показати. Тут той
самий протокол, але дії описані рядком, кожен крок вимірюється, а результат
лягає у PNG.

Заразом це наскрізна перевірка безпеки: крок `drop` рве з'єднання, **не
відпустивши клавішу**, і далі видно, чи пульт лишився натискати щось сам.

    tools/input_check.py --steps "shot:00-start,key:MDL,shot:01-model"
    tools/input_check.py --steps "hold:PAGE>:1500,shot:02-repeat"
    tools/input_check.py --steps "press:ENTER,drop,wait:2000,shot:03-after-drop"

Кроки (через кому, зліва направо):

    key:МІТКА          коротке натискання (натиснув і відпустив)
    press:МІТКА        натиснути й не відпускати
    release:МІТКА      відпустити
    hold:МІТКА:МС      натиснути, потримати МС, відпустити
    enc:±N             N клацань енкодера
    tap:X/Y            дотик: натиск і відпускання в точці
    swipe:X1/Y1/X2/Y2  дотик із протягуванням
    wait:МС            просто зачекати (PING іде далі)
    shot:НАЗВА         зберегти поточний кадр у PNG
    drop               обірвати з'єднання, нічого не відпускаючи
    connect            під'єднатись знову

Мітки клавіш беруться з HELLO, а не з коду: на іншому пульті сценарій або
спрацює, або чесно скаже, що такої клавіші немає.
"""

import argparse
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from capture_check import Frame, write_png  # noqa: E402
from remote_ui_proto import (  # noqa: E402
    DEFAULT_TCP_PORT,
    PING_PERIOD_S,
    PKT_FRAME_END,
    PKT_HELLO,
    PKT_LOG,
    PKT_TILE,
    TILE_METHOD_RAW,
    TILE_METHOD_RLE16,
    TOUCH_DOWN,
    TOUCH_MOVE,
    TOUCH_UP,
    Decoder,
    encode_enc,
    encode_frame,
    encode_key,
    encode_touch,
    encode_trim,
    PKT_PING,
    PKT_REFRESH,
    parse_hello,
    rle16_decode,
)


class Radio:
    """З'єднання з пультом: шле дії, збирає кадри, міряє час реакції."""

    def __init__(self, host: str, port: int, quiet: float = 0.25):
        self.host = host
        self.port = port
        # Скільки тиші означає «пульт домалював». Кадри в русі йдуть один за
        # одним, тому чекати треба не першого FRAME_END, а паузи після нього.
        self.quiet = quiet

        self.sock = None
        self.dec = Decoder()
        self.hello = None
        self.frame = None
        self.frames = 0
        self.last_ping = 0.0
        self.log = []

    # --- Труба -------------------------------------------------------------

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=5.0)
        self.sock.settimeout(0.01)
        self.dec.reset()
        self.send(encode_frame(PKT_PING))
        self.pump(0.3)
        if self.hello is None:
            raise RuntimeError("пульт не назвався: HELLO не прийшов")
        self.send(encode_frame(PKT_REFRESH))
        self.settle()

    def drop(self):
        """Розрив без жодного «відпущено» — саме те, чого боїмось."""
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def send(self, data: bytes):
        if self.sock is None:
            raise RuntimeError("немає з'єднання")
        self.sock.sendall(data)

    def ping_if_due(self):
        now = time.monotonic()
        if now - self.last_ping >= PING_PERIOD_S:
            self.send(encode_frame(PKT_PING))
            self.last_ping = now

    # --- Кадри -------------------------------------------------------------

    def pump(self, seconds: float, ping: bool = True):
        """Читає й розбирає все, що прийшло за `seconds`.

        `ping=False` — не подавати ознак життя: труба ціла, але клієнт мовчить.
        Саме так виглядає зависла програма або обірваний дріт на UART, де
        поняття «з'єднання закрито» немає взагалі.
        """
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if self.sock is None:
                time.sleep(0.005)
                continue
            if ping:
                self.ping_if_due()
            try:
                data = self.sock.recv(65536)
            except TimeoutError:
                continue
            except OSError:
                return
            if not data:
                return
            for ptype, payload in self.dec.feed(data):
                self.handle(ptype, payload)

    def handle(self, ptype: int, payload: bytes):
        if ptype == PKT_HELLO:
            self.hello = parse_hello(payload)
            if self.frame is None:
                self.frame = Frame(self.hello["width"], self.hello["height"])
        elif ptype == PKT_TILE and self.frame is not None:
            x, y, w, h = (
                payload[0] | payload[1] << 8,
                payload[2] | payload[3] << 8,
                payload[4] | payload[5] << 8,
                payload[6] | payload[7] << 8,
            )
            method, body = payload[8], payload[9:]
            if method == TILE_METHOD_RAW:
                pixels = [body[i] | body[i + 1] << 8 for i in range(0, len(body), 2)]
            elif method == TILE_METHOD_RLE16:
                pixels = rle16_decode(body, w * h)
            else:
                pixels = None
            if pixels is not None:
                self.frame.blit(x, y, w, h, pixels)
        elif ptype == PKT_FRAME_END:
            self.frames += 1
        elif ptype == PKT_LOG:
            self.log.append(payload.decode("utf-8", "replace"))

    def settle(self, limit: float = 3.0) -> float:
        """Чекає, доки пульт перестане малювати. Повертає, скільки чекав."""
        started = time.monotonic()
        last_frames = self.frames
        quiet_since = time.monotonic()
        while time.monotonic() - started < limit:
            self.pump(0.01)
            if self.frames != last_frames:
                last_frames = self.frames
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since >= self.quiet:
                break
        return time.monotonic() - started

    def react(self, action) -> float:
        """Виконує дію і повертає час до **першого** кадру після неї, мс.

        Саме перший кадр, а не останній: людина бачить реакцію тоді, коли
        екран уперше змінився, а не коли перемальовування скінчилось.
        """
        frames_before = self.frames
        started = time.monotonic()
        action()
        while time.monotonic() - started < 3.0:
            self.pump(0.005)
            if self.frames != frames_before:
                break
        latency = (time.monotonic() - started) * 1000
        self.settle()
        return latency

    # --- Дії ---------------------------------------------------------------

    def key_code(self, label: str) -> int:
        for code, name in self.hello["keys"]:
            if name.upper() == label.upper():
                return code
        known = ", ".join(name for _c, name in self.hello["keys"])
        raise SystemExit(f"пульт не має клавіші «{label}». Є: {known}")

    def save(self, path: str):
        write_png(path, self.frame.w, self.frame.h, self.frame.to_rgb888())


def run_steps(radio: Radio, steps, outdir: str, report):
    for step in steps:
        step = step.strip()
        if not step:
            continue
        name, _, arg = step.partition(":")
        name = name.lower()

        if name == "shot":
            path = os.path.join(outdir, f"{arg}.png")
            radio.save(path)
            report.append(f"  знімок  {path}")

        elif name == "key":
            code = radio.key_code(arg)
            ms = radio.react(lambda c=code: (radio.send(encode_key(c, True)),
                                             radio.send(encode_key(c, False))))
            report.append(f"  {arg:<8} натиснуто й відпущено, реакція {ms:5.1f} мс")

        elif name == "press":
            code = radio.key_code(arg)
            ms = radio.react(lambda c=code: radio.send(encode_key(c, True)))
            report.append(f"  {arg:<8} натиснуто (тримаю), реакція {ms:5.1f} мс")

        elif name == "release":
            code = radio.key_code(arg)
            radio.send(encode_key(code, False))
            radio.settle()
            report.append(f"  {arg:<8} відпущено")

        elif name == "hold":
            label, _, ms_text = arg.partition(":")
            code = radio.key_code(label)
            hold_ms = float(ms_text or 1000)
            frames_before = radio.frames
            radio.send(encode_key(code, True))
            radio.pump(hold_ms / 1000.0)
            radio.send(encode_key(code, False))
            radio.settle()
            report.append(
                f"  {label:<8} утримано {hold_ms:.0f} мс, кадрів за цей час "
                f"{radio.frames - frames_before}"
            )

        elif name == "trimpress":
            # Натиснути й **не** відпускати. Для перевірки безпеки: далі йде
            # `drop`, і пульт має відпустити тример сам.
            radio.send(encode_trim(int(arg), True))
            radio.settle()
            report.append(f"  тример {arg} натиснуто (тримаю)")

        elif name == "trim":
            # Тример — єдине місце на TX16S, де автоповтор видно очима: поки
            # напрямок тримають, EdgeTX сам рухає значення далі й далі.
            index_text, _, ms_text = arg.partition(":")
            index = int(index_text)
            hold_ms = float(ms_text or 800)
            frames_before = radio.frames
            radio.send(encode_trim(index, True))
            radio.pump(hold_ms / 1000.0)
            radio.send(encode_trim(index, False))
            radio.settle()
            report.append(
                f"  тример {index} утримано {hold_ms:.0f} мс, кадрів "
                f"{radio.frames - frames_before}"
            )

        elif name == "enc":
            steps_n = int(arg)
            direction = 1 if steps_n > 0 else -1
            ms = radio.react(lambda: radio.send(encode_enc(direction)))
            for _ in range(abs(steps_n) - 1):
                radio.send(encode_enc(direction))
                radio.settle()
            report.append(f"  енкодер  {steps_n:+d}, реакція {ms:5.1f} мс")

        elif name == "tap":
            x, y = (int(v) for v in arg.split("/"))
            ms = radio.react(lambda: (radio.send(encode_touch(TOUCH_DOWN, x, y)),
                                      radio.send(encode_touch(TOUCH_UP, x, y))))
            report.append(f"  дотик    {x},{y}, реакція {ms:5.1f} мс")

        elif name == "swipe":
            x1, y1, x2, y2 = (int(v) for v in arg.split("/"))
            radio.send(encode_touch(TOUCH_DOWN, x1, y1))
            for i in range(1, 9):
                radio.send(encode_touch(TOUCH_MOVE,
                                        x1 + (x2 - x1) * i // 8,
                                        y1 + (y2 - y1) * i // 8))
                radio.pump(0.02)
            radio.send(encode_touch(TOUCH_UP, x2, y2))
            radio.settle()
            report.append(f"  протяг   {x1},{y1} -> {x2},{y2}")

        elif name == "wait":
            radio.pump(float(arg) / 1000.0)
            report.append(f"  пауза    {arg} мс, кадрів усього {radio.frames}")

        elif name == "refresh":
            # Борг задачі 0006: REFRESH має завершуватись FRAME_END навіть на
            # нерухомому екрані. Тут це видно числом, а не на око.
            radio.settle()
            frames_before = radio.frames
            radio.send(encode_frame(PKT_REFRESH))
            radio.settle()
            got = radio.frames - frames_before
            report.append(
                f"  REFRESH  на нерухомому екрані -> FRAME_END: "
                f"{'так' if got else 'НІ'} (кадрів {got})"
            )

        elif name == "silence":
            radio.pump(float(arg) / 1000.0, ping=False)
            report.append(f"  ⚠️ мовчання {arg} мс при цілій трубі")

        elif name == "drop":
            radio.drop()
            report.append("  ⚠️ з'єднання обірвано, нічого не відпускаючи")

        elif name == "connect":
            radio.connect()
            report.append("  під'єднано наново")

        else:
            raise SystemExit(f"невідомий крок: {step}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Сценарій керування пультом через Remote UI")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_TCP_PORT)
    ap.add_argument("--out", default="docs/img/input", help="куди класти знімки")
    ap.add_argument("--steps", required=True, help="кроки через кому")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    radio = Radio(args.host, args.port)
    radio.connect()

    hello = radio.hello
    report = [
        f"Пульт: {hello['target']} {hello['fw']}  {hello['width']}x{hello['height']}",
        "Клавіші: " + ", ".join(f"{name}({code})" for code, name in hello["keys"]),
        "",
    ]

    run_steps(radio, args.steps.split(","), args.out, report)

    print("\n".join(report))
    if radio.log:
        print("\nЖурнал пульта:")
        for line in radio.log:
            print(" ", line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
