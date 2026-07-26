#!/usr/bin/env python3
"""Тестовий клієнт Remote UI: вікно з екраном пульта живцем.

Діагностичний інструмент етапу 1, не продукт. Справжній клієнт буде в
браузері (етап 2). Тут задача одна: бачити те, що зараз на екрані пульта, і
бачити це **в русі** — блимання, рвані кадри, накопичення затримки видно
тільки так, знімок їх не показує.

Розмір, формат і назви клавіш беруться з пакета `HELLO`. Жодного числа про
конкретний пульт у коді немає: коли на кроці 1.7 з'явиться симулятор іншої
цілі, клієнт підхопить його без правок.

Запуск (симулятор має бути зібраний із `-DREMOTE_UI=ON`):

    tools/test_client.py                      # tcp 127.0.0.1:7616
    tools/test_client.py --scale 2
    tools/test_client.py --serial /dev/ttyUSB0 --baud 921600
    tools/test_client.py --serial socket://127.0.0.1:7616   # без заліза

Залежності: стандартна бібліотека (`tkinter` — пакет `tk` на Arch:
`sudo pacman -S tk`). `pyserial` потрібен лише для `--serial`:
`sudo pacman -S python-pyserial`.

Вводу тут немає навмисно — клавіші, енкодер і сенсор це крок 1.6.
"""

import argparse
import os
import struct
import sys
import threading
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from remote_ui_proto import (  # noqa: E402
    HELLO_PIXFMT_RGB565,
    PING_PERIOD_S,
    PING_TIMEOUT_S,
    PKT_FRAME_END,
    PKT_HELLO,
    PKT_LOG,
    PKT_PING,
    PKT_REFRESH,
    PKT_STATE,
    PKT_TILE,
    SILENCE_RESET_S,
    TILE_METHOD_RAW,
    TILE_METHOD_RLE16,
    Decoder,
    add_transport_args,
    encode_frame,
    make_connector,
    parse_hello,
)

# --- Пікселі --------------------------------------------------------------

# RGB565 → три байти RGB888. Таблиця на 64 Кі записів будується один раз і
# знімає з гарячого шляху всю арифметику: розпаковка плитки стає склеюванням
# готових шматочків.
_LUT = None


def lut():
    global _LUT
    if _LUT is None:
        table = []
        for p in range(65536):
            r = (p >> 11) & 0x1F
            g = (p >> 5) & 0x3F
            b = p & 0x1F
            # Старші біти повторюються в молодші, інакше білий став би
            # 248-м відтінком сірого.
            table.append(bytes(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))))
        _LUT = table
    return _LUT


class Screen:
    """Позаекранний кадр у RGB888. Показується лише на FRAME_END."""

    def __init__(self, width: int, height: int):
        self.w = width
        self.h = height
        self.stride = width * 3
        self.buf = bytearray(self.stride * height)
        self.header = b"P6\n%d %d\n255\n" % (width, height)

    def blit(self, x: int, y: int, w: int, h: int, rgb: bytes) -> bool:
        """Кладе розпаковану плитку. False — плитка бреше про себе."""
        if x < 0 or y < 0 or w <= 0 or h <= 0:
            return False
        if x + w > self.w or y + h > self.h:
            return False
        row = w * 3
        if len(rgb) != row * h:
            return False
        for r in range(h):
            dst = (y + r) * self.stride + x * 3
            self.buf[dst : dst + row] = rgb[r * row : (r + 1) * row]
        return True

    def ppm(self) -> bytes:
        """Кадр у форматі, який tkinter читає без сторонніх бібліотек."""
        return self.header + bytes(self.buf)


def tile_rgb(method: int, body: bytes, want_pixels: int):
    """Плитка → готові байти RGB888, або None, якщо вміст битий."""
    table = lut()
    if method == TILE_METHOD_RAW:
        if len(body) != want_pixels * 2:
            return None
        return b"".join([table[v] for v in struct.unpack(f"<{want_pixels}H", body)])
    if method == TILE_METHOD_RLE16:
        if len(body) % 3:
            return None
        out = bytearray()
        for i in range(0, len(body), 3):
            count = body[i]
            if count == 0:
                return None
            out += table[body[i + 1] | (body[i + 2] << 8)] * count
        return bytes(out) if len(out) == want_pixels * 3 else None
    return None  # невідомий метод — не наша справа, мовчки повз


# --- Стан з'єднання -------------------------------------------------------


# Скільки чекати FRAME_END, якщо плитки вже прийшли, а кадр не закрився.
#
# Запобіжник проти реальної поведінки прошивки: `FRAME_END` вилітає лише коли
# зріс лічильник кадрів захоплення, а `REFRESH` на нерухомому екрані його не
# рухає. У симуляторі по TCP це не видно (перше з'єднання шле FRAME_END
# примусово), але на послідовному порту, де поняття «підключився» немає,
# клієнт після REFRESH отримує 135 плиток і жодного FRAME_END — і чесний
# клієнт не показав би нічого. Подробиці — у результаті задачі 0006.
FORCE_SHOW_S = 0.3


class Session:
    """Складання кадру з пакетів. Без жодного tkinter — тому й тестується.

    Невідомі типи пакетів мовчки ігноруються (docs/03-protocol.md): лічильник
    росте, розбір триває далі.
    """

    def __init__(self, on_frame=None):
        self.on_frame = on_frame
        self.hello = None
        self.screen = None
        self.frames = 0
        self.tiles = 0
        self.tiles_in_frame = 0
        self.last_frame_tiles = 0
        self.last_tile_at = 0.0
        self.bad_tiles = 0
        self.unknown = 0
        self.forced = 0  # кадрів, показаних без FRAME_END
        self.logs = 0
        self.hello_at = None  # час останнього HELLO — відповідь на PING
        self.hello_count = 0

    def handle(self, ptype: int, payload: bytes):
        if ptype == PKT_HELLO:
            self.hello_at = time.monotonic()
            if len(payload) < 13:
                return  # обрізаний HELLO — читати нічого
            self.hello_count += 1
            hello = parse_hello(payload)
            self.hello = hello
            # HELLO приходить на кожен PING, тобто раз на дві секунди. Кадр
            # перестворюється лише коли справді змінився розмір, інакше екран
            # блимав би порожнім двічі на секунду.
            if self.screen is None or (self.screen.w, self.screen.h) != (
                hello["width"],
                hello["height"],
            ):
                self.screen = Screen(hello["width"], hello["height"])
                # ЧБ-пульти — етап 5.3. Плитки такого пульта клієнт відкине як
                # биті, і без цього рядка це виглядало б як поломка захоплення.
                if hello["pixfmt"] != HELLO_PIXFMT_RGB565:
                    print(
                        f"Увага: формат пікселя {hello['pixfmt']} цей клієнт не вміє "
                        f"(тільки RGB565). Плитки не малюватимуться.",
                        file=sys.stderr,
                    )

        elif ptype == PKT_TILE:
            if self.screen is None or len(payload) < 9:
                return
            x, y, w, h, method = struct.unpack_from("<HHHHB", payload, 0)
            rgb = tile_rgb(method, payload[9:], w * h)
            if rgb is None or not self.screen.blit(x, y, w, h, rgb):
                self.bad_tiles += 1
                return
            self.tiles += 1
            self.tiles_in_frame += 1
            self.last_tile_at = time.monotonic()

        elif ptype == PKT_FRAME_END:
            if self.screen is None:
                return
            self.frames += 1
            self._show()

        elif ptype == PKT_LOG:
            self.logs += 1
            print("пульт:", payload.decode("utf-8", "replace"), file=sys.stderr)

        elif ptype == PKT_STATE:
            pass  # необов'язковий, клієнту етапу 1 не потрібен

        else:
            self.unknown += 1

    def _show(self):
        self.last_frame_tiles = self.tiles_in_frame
        self.tiles_in_frame = 0
        if self.on_frame:
            self.on_frame(self.screen)

    def flush_stale(self, now: float):
        """Показати кадр, якщо плитки прийшли, а FRAME_END так і не прийшов.

        Викликається транспортом на кожному проході. Лічильник `forced` росте,
        щоб дефект було видно у вікні, а не заметено під килим.
        """
        if self.tiles_in_frame and now - self.last_tile_at > FORCE_SHOW_S:
            self.forced += 1
            self._show()


class Link(threading.Thread):
    """Труба, PING і перепідключення. Живе у власному потоці.

    Розбір і розпаковка теж тут: інтерфейсний потік має лише показувати
    готовий кадр, інакше вікно застигало б на кожній великій плитці.
    """

    daemon = True

    def __init__(self, connect, on_frame):
        super().__init__(name="remote-ui-link")
        self.connect = connect
        self.session = Session(on_frame=on_frame)
        self.stop = threading.Event()
        # Декодувальник один на всі з'єднання: `reset()` чистить лише
        # недочитаний кадр, тому лічильники помилок не обнуляються при
        # перепідключенні — інакше числа у вікні їхали б назад.
        self.decoder = Decoder()

        self.status = "підключаюсь…"
        self.rx_bytes = 0
        self.crc_errors = 0
        self.oversized = 0
        self.drops = 0  # скільки разів рвався зв'язок

    def run(self):
        while not self.stop.is_set():
            try:
                transport = self.connect()
            except Exception as exc:  # OSError, RuntimeError від pyserial…
                self.status = f"немає зв'язку: {exc}"
                self.stop.wait(1.0)
                continue
            self.status = f"з'єднано: {transport.name}"
            try:
                self._serve(transport)
            finally:
                transport.close()

    def _serve(self, transport):
        decoder = self.decoder
        decoder.reset()
        session = self.session

        # Порядок вітання важливий: спершу PING, і лише отримавши у відповідь
        # HELLO — REFRESH. До HELLO клієнт не знає розміру екрана, і плитки
        # йому нікуди класти: він їх викине.
        #
        # По TCP цього не видно — пульт вітається сам, щойно хтось під'єднався.
        # У послідовного порту поняття «підключився» немає взагалі: перше слово
        # має сказати клієнт. Помилка знайшлася саме на порту (задача 0006).
        transport.send(encode_frame(PKT_PING))
        hello_mark = session.hello_count
        refresh_sent = False

        now = time.monotonic()
        session.hello_at = now
        last_ping = now
        last_rx = now

        while not self.stop.is_set():
            data = transport.recv()
            if data is None:
                self.status = "пульт закрив з'єднання"
                self.drops += 1
                return
            now = time.monotonic()
            if data:
                self.rx_bytes += len(data)
                last_rx = now
                for ptype, payload in decoder.feed(data):
                    session.handle(ptype, payload)
                self.crc_errors = decoder.crc_errors
                self.oversized = decoder.oversized
            elif now - last_rx > SILENCE_RESET_S:
                # Тиша в каналі. Якщо декодувальник завис посеред обірваного
                # кадру — звільняємо його, як це робить транспорт у прошивці.
                decoder.reset()
                last_rx = now

            session.flush_stale(now)

            if not refresh_sent and session.hello_count != hello_mark:
                # Пульт назвався — тепер є куди класти пікселі.
                try:
                    transport.send(encode_frame(PKT_REFRESH))
                except OSError as exc:
                    self.status = f"розрив на передачі: {exc}"
                    self.drops += 1
                    return
                refresh_sent = True

            if now - last_ping >= PING_PERIOD_S:
                try:
                    transport.send(encode_frame(PKT_PING))
                except OSError as exc:
                    self.status = f"розрив на передачі: {exc}"
                    self.drops += 1
                    return
                last_ping = now

            if now - session.hello_at > PING_TIMEOUT_S:
                self.status = f"пульт мовчить {PING_TIMEOUT_S:.0f} с — перепідключаюсь"
                self.drops += 1
                return


# --- Вікно ----------------------------------------------------------------


class Window:
    """Одне вікно: картинка зверху, кілька чисел знизу."""

    POLL_MS = 15  # частіше за будь-який кадр пульта, дешевше за нього ж

    def __init__(self, title: str, scale: int):
        self.scale = scale
        self.root = tk.Tk()
        self.root.title(title)
        self.root.configure(bg="#101010")
        self.canvas = tk.Label(self.root, bg="#101010", text="очікую HELLO…", fg="#909090")
        self.canvas.pack()
        self.stats = tk.Label(
            self.root, font=("monospace", 9), justify="left", anchor="w",
            bg="#101010", fg="#c0c0c0",
        )
        self.stats.pack(fill="x")
        self.image = None

        self.lock = threading.Lock()
        self.pending = None  # (ppm, час приходу) — тільки найсвіжіший кадр
        self.skipped = 0  # кадри, які застаріли, доки вікно малювало попередній

        self.link = None
        self.shown = 0
        self.window_started = time.monotonic()
        self.window_shown = 0
        self.window_rx = 0
        self.window_frames = 0
        self.lag_ms = 0.0
        self.lag_max_ms = 0.0
        self.line = ("", "")

        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def submit(self, screen: Screen):
        """Викликається потоком зв'язку на кожен FRAME_END."""
        ppm = screen.ppm()
        with self.lock:
            if self.pending is not None:
                self.skipped += 1
            self.pending = (ppm, time.monotonic())

    def close(self):
        if self.link:
            self.link.stop.set()
        self.root.quit()

    def run(self, link: Link):
        self.link = link
        self.root.after(self.POLL_MS, self._tick)
        self.root.mainloop()

    def _tick(self):
        with self.lock:
            item, self.pending = self.pending, None

        if item is not None:
            ppm, arrived = item
            image = tk.PhotoImage(data=ppm)
            if self.scale > 1:
                image = image.zoom(self.scale)
            self.canvas.configure(image=image, text="")
            self.image = image  # tkinter не тримає посилання сам
            self.shown += 1
            self.window_shown += 1
            self.lag_ms = (time.monotonic() - arrived) * 1000
            self.lag_max_ms = max(self.lag_max_ms, self.lag_ms)

        self._update_stats()
        self.root.after(self.POLL_MS, self._tick)

    def _update_stats(self):
        link = self.link
        now = time.monotonic()
        elapsed = now - self.window_started
        if elapsed >= 0.5:
            session = link.session
            fps_shown = self.window_shown / elapsed
            fps_got = (session.frames - self.window_frames) / elapsed
            kib = (link.rx_bytes - self.window_rx) / elapsed / 1024
            hello = session.hello
            head = (
                f"{hello['target']} {hello['fw']}  {hello['width']}x{hello['height']}  "
                f"клавіш {len(hello['keys'])}"
                if hello
                else "HELLO ще не прийшов"
            )
            self.line = (
                f"{head}   {link.status}",
                f"кадрів/с {fps_shown:4.1f} (прийнято {fps_got:4.1f})   "
                f"{kib:6.1f} КіБ/с   плиток {session.last_frame_tiles:3d}   "
                f"затримка {self.lag_ms:4.1f} мс (макс {self.lag_max_ms:.0f})   "
                f"CRC {link.crc_errors}  довж {link.oversized}  "
                f"биті плитки {session.bad_tiles}  "
                f"без FRAME_END {session.forced}  "
                f"застарілі {self.skipped}  розриви {link.drops}",
            )
            self.window_started = now
            self.window_shown = 0
            self.window_rx = link.rx_bytes
            self.window_frames = session.frames
        self.stats.configure(text="\n".join(self.line))


def main() -> int:
    ap = argparse.ArgumentParser(description="Тестовий клієнт Remote UI: вікно з екраном пульта")
    add_transport_args(ap)
    ap.add_argument("--scale", type=int, default=1, help="ціле збільшення картинки")
    args = ap.parse_args()

    where, connect = make_connector(args)

    window = Window(f"Remote UI — {where}", max(1, args.scale))
    link = Link(connect, window.submit)
    link.start()
    try:
        window.run(link)
    except KeyboardInterrupt:
        pass
    link.stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
