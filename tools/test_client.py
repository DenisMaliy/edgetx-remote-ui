#!/usr/bin/env python3
"""Тестовий клієнт Remote UI: вікно з екраном пульта живцем і керування ним.

Діагностичний інструмент етапу 1, не продукт. Справжній клієнт буде в
браузері (етап 2). Тут задача одна: бачити те, що зараз на екрані пульта, і
бачити це **в русі** — блимання, рвані кадри, накопичення затримки видно
тільки так, знімок їх не показує. Тепер ще й керувати: клавіатура, колесо
миші й миша по картинці.

Розмір, формат і назви клавіш беруться з пакета `HELLO`. Жодного числа про
конкретний пульт у коді немає: коли на кроці 1.7 з'явиться симулятор іншої
цілі, клієнт підхопить його без правок — розкладка клавіш теж будується з
того, що назвав пульт.

Запуск (симулятор має бути зібраний із `-DREMOTE_UI=ON`):

    tools/test_client.py                      # tcp 127.0.0.1:7616
    tools/test_client.py --scale 2
    tools/test_client.py --serial /dev/ttyUSB0 --baud 921600
    tools/test_client.py --serial socket://127.0.0.1:7616   # без заліза

Залежності: стандартна бібліотека (`tkinter` — пакет `tk` на Arch:
`sudo pacman -S tk`). `pyserial` потрібен лише для `--serial`:
`sudo pacman -S python-pyserial`.
"""

import argparse
import os
import queue
import struct
import sys
import threading
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from remote_ui_proto import (  # noqa: E402
    FRAME_WAIT_MAX_MS,
    HELLO_FLAG_ENCODER,
    HELLO_FLAG_INPUT_STATE,
    HELLO_FLAG_TOUCH,
    HELLO_PIXFMT_RGB565,
    INPUT_STATE_PERIOD_S,
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
    TOUCH_DOWN,
    TOUCH_MOVE,
    TOUCH_UP,
    Decoder,
    InputMirror,
    add_transport_args,
    encode_enc,
    encode_frame,
    encode_key,
    encode_touch,
    frame_wait_ms,
    hold_packet,
    make_connector,
    parse_frame_end,
    parse_hello,
)

# Поріг, після якого клієнт **сам про себе** каже «я замовк». Копія
# `BRIDGE_CLIENT_SILENCE_MS` із `firmware/esp32/main/bridge_cfg.h`.
#
# ⚠️ Клієнт за цим числом нічого не робить — воно тільки для друку. Сенс у
# тому, щоб при спрацюванні сторожа на мості одразу було видно, чи клієнт
# визнає за собою ту саму паузу. Якщо визнає — шукати треба в ньому, а не в
# Wi-Fi і не в мості.
CLIENT_SILENCE_REPORT_MS = 750.0

# Як часто друкувати розкид пауз під час прогону.
GAP_REPORT_PERIOD_S = 30.0

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

    def __init__(self, on_frame=None, mask_input_state_bit=False):
        self.on_frame = on_frame
        # Тестова підміна: викинути біт3 з прийнятого HELLO і тим самим вдати
        # стару прошивку. Підмінюється саме байт на дроті, а не рішення клієнта,
        # тому запасний шлях вибирає той самий код, що й у житті.
        self.mask_input_state_bit = mask_input_state_bit
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
        # ⚠️ Дві різні величини, і плутати їх не можна.
        #
        # `frames` — скільки кадрів **показано**, тобто що бачила людина.
        # `frame_ends` — скільки `FRAME_END` **прийшло по дроту**.
        #
        # До задачі 0019 вони збігалися: кожен FRAME_END одразу показував кадр.
        # Тепер між ними лежить очікування, і саме різниця між ними — ціна
        # задачі. Критерій 4.1 («кадрів за секунду до і після») питає про
        # `frames`: частота на дроті не змінилась і збрехала б у потрібний бік.
        self.frame_ends = 0
        # Звідки взявся кожен показаний кадр (крім `forced`);
        # сума трьох дорівнює `frames` рівно.
        self.frames_whole = 0  # dirtyTiles = 0 — кадр цілісний
        self.frames_timeout = 0  # решта не доїхала за строк
        self.frames_legacy = 0  # прошивка без ознаки повноти
        self.pending_since = None  # початок поточної низки очікування
        self.pending_until = None  # коли показати неповний кадр як є
        self.logs = 0
        self.hello_at = None  # час останнього HELLO — відповідь на PING
        self.hello_count = 0

    def handle(self, ptype: int, payload: bytes, now: float | None = None):
        """`now` передає транспорт — він його вже має. None означає «зараз».

        Час приходить ззовні заради тестів строку очікування: без цього їх
        довелося б писати зі справжніми паузами, і вони або гальмували б прогін,
        або мигали б залежно від навантаження машини.
        """
        if ptype == PKT_HELLO:
            self.hello_at = time.monotonic()
            if len(payload) < 13:
                return  # обрізаний HELLO — читати нічого
            self.hello_count += 1
            if self.mask_input_state_bit:
                payload = bytearray(payload)
                payload[6] &= ~HELLO_FLAG_INPUT_STATE & 0xFF
                payload = bytes(payload)
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
            self._frame_end(parse_frame_end(payload), now)

        elif ptype == PKT_LOG:
            self.logs += 1
            print("пульт:", payload.decode("utf-8", "replace"), file=sys.stderr)

        elif ptype == PKT_STATE:
            pass  # необов'язковий, клієнту етапу 1 не потрібен

        else:
            self.unknown += 1

    def _frame_end(self, dirty, now=None):
        """FRAME_END прийшов. `dirty` — скільки плиток ще в дорозі, або None.

        Дзеркало правила з `webui/app.js`: цілий кадр показуємо негайно,
        неповний — чекаємо доїзду решти зі строком. Клієнти не мають права
        розходитись у тому, що людина бачить на екрані: саме цим вікном знімали
        доказ розламу (дві рамки виділення в одному кадрі), ним же його й
        знімають назад.
        """
        if now is None:
            now = time.monotonic()

        self.frame_ends += 1

        if dirty is None:
            # Стара прошивка: ознаки повноти немає — поводимось як досі.
            self.frames += 1
            self.frames_legacy += 1
            self.pending_since = None
            self._show()
            return

        if dirty == 0:
            self.frames += 1
            self.frames_whole += 1
            self.pending_since = None
            self._show()
            return

        # Низка очікування починається з першого неповного кадру, і стеля
        # рахується від неї — не від кожного FRAME_END окремо (екран замерз би)
        # і не від останнього показаного кадру (після паузи запас був би вже
        # вичерпаний, тобто саме на гортанні після зупинки очікування не було б
        # зовсім).
        if self.pending_since is None:
            self.pending_since = now

        wait_s = min(
            frame_wait_ms(dirty),
            FRAME_WAIT_MAX_MS - (now - self.pending_since) * 1000.0,
        ) / 1000.0

        if wait_s <= 0:
            self.frames += 1
            self.frames_timeout += 1
            self.pending_since = None
            self._show()
            return

        self.pending_until = now + wait_s

    def _show(self):
        self.last_frame_tiles = self.tiles_in_frame
        self.tiles_in_frame = 0
        self.pending_until = None
        if self.on_frame:
            self.on_frame(self.screen)

    def flush_stale(self, now: float):
        """Показати кадр, який зачекався. Дві різні причини, обидві — дефекти.

        Викликається транспортом на кожному проході. Лічильники ростуть, щоб
        дефекти було видно у вікні, а не заметено під килим.
        """
        # 1. Кадр неповний, і решта плиток не доїхала за строк.
        if self.pending_until is not None and now >= self.pending_until:
            self.frames += 1
            self.frames_timeout += 1
            self.pending_since = None
            self._show()
            return

        # 2. Плитки прийшли, а FRAME_END так і не прийшов узагалі.
        if self.tiles_in_frame and now - self.last_tile_at > FORCE_SHOW_S:
            self.forced += 1
            self.pending_since = None
            self._show()


# --- Ввід -----------------------------------------------------------------


# Мітка клавіші (приходить у HELLO) -> клавіша клавіатури ПК (keysym tkinter).
#
# Це зручність клієнта, а не опис заліза. Перелік клавіш пульта приходить від
# самого пульта; мітка, якої тут немає, отримає вільну F-клавішу. Тому на
# іншому пульті клієнт не «не знає клавіш», а просто розкладе їх інакше.
LABEL_TO_KEYSYM = {
    "RTN": "Escape",
    "EXIT": "Escape",
    "ENTER": "Return",
    "MENU": "F1",
    "SYS": "s",
    "MDL": "m",
    "TELE": "t",
    "PAGE<": "Prior",
    "PGUP": "Prior",
    "PAGE>": "Next",
    "PGDN": "Next",
    "UP": "Up",
    "DOWN": "Down",
    "LEFT": "Left",
    "RIGHT": "Right",
    "+": "plus",
    "PLUS": "plus",
    "-": "minus",
    "MINUS": "minus",
    "SHIFT": "Shift_L",
    "BIND": "b",
}

# Для міток, яких немає в таблиці вище.
SPARE_KEYSYMS = [f"F{i}" for i in range(1, 13)]

# Як показувати клавішу ПК людині.
KEYSYM_SHORT = {
    "Escape": "Esc",
    "Return": "Enter",
    "Prior": "PgUp",
    "Next": "PgDn",
    "Up": "↑",
    "Down": "↓",
    "Left": "←",
    "Right": "→",
    "plus": "+",
    "minus": "-",
    "Shift_L": "Shift",
}


class Input:
    """Клавіатура й миша -> пакети протоколу.

    Розкладка будується з `HELLO`. Жодного коду клавіші тут не прибито:
    клієнт бере пари «код + мітка» від пульта і сам вирішує, яку клавішу ПК
    на що повісити.
    """

    # Скільки чекати, перш ніж повірити у відпускання клавіші.
    #
    # X11 на утримуваній клавіші шле не «натиснуто й тримається», а пари
    # «відпущено — натиснуто» з частотою автоповтору (близько 30 мс). Без цієї
    # затримки довге натискання розсипалось би на десяток коротких — тобто
    # рівно те, що ми хочемо довести працюючим, ламав би сам клієнт.
    REPEAT_GRACE_MS = 60

    def __init__(self, root: tk.Misc, link):
        self.root = root
        self.link = link

        self.signature = None  # за чим помічаємо, що HELLO описав інший пульт
        self.keysym_to_code = {}
        self.keysym_to_enc = {}
        self.layout_text = "розкладки ще немає — чекаю HELLO"
        self.has_touch = False
        self.has_encoder = False

        self.held = {}  # keysym -> код клавіші, яку пульт вважає натиснутою
        self.pending_release = {}  # keysym -> ідентифікатор відкладеного after
        self.touch_down = False
        self.touch_last = None

    # --- Розкладка ---------------------------------------------------------

    def sync(self, hello) -> None:
        """Перебудовує розкладку, якщо пульт назвався інакше.

        Викликається з потоку вікна на кожному кадрі опитування: `HELLO`
        приходить у потоці зв'язку, і будувати розкладку там означало б
        читати її з двох потоків.
        """
        if hello is None:
            return
        signature = (tuple(hello["keys"]), hello["flags"], hello["target"])
        if signature == self.signature:
            return

        self.signature = signature
        self.has_touch = bool(hello["flags"] & HELLO_FLAG_TOUCH)
        self.has_encoder = bool(hello["flags"] & HELLO_FLAG_ENCODER)

        self.release_all()
        self.keysym_to_code = {}
        self.keysym_to_enc = {}

        spare = list(SPARE_KEYSYMS)
        shown = []
        for code, label in hello["keys"]:
            keysym = LABEL_TO_KEYSYM.get(label.upper())
            if keysym is None or keysym in self.keysym_to_code:
                keysym = spare.pop(0) if spare else None
            if keysym is None:
                continue  # клавіш більше, ніж вільних місць на клавіатурі
            self.keysym_to_code[keysym] = code
            shown.append(f"{label}={KEYSYM_SHORT.get(keysym, keysym)}")

        # Стрілки віддаємо енкодеру, тільки якщо пульт не має власних клавіш
        # «вгору/вниз»: інакше ми відібрали б у нього справжні клавіші.
        if self.has_encoder:
            if "Up" not in self.keysym_to_code and "Down" not in self.keysym_to_code:
                self.keysym_to_enc = {"Up": -1, "Down": 1}
                shown.append("енкодер=колесо,↑↓")
            else:
                shown.append("енкодер=колесо")
        if self.has_touch:
            shown.append("дотик=миша")

        self.layout_text = "  ".join(shown)
        print("Розкладка з HELLO:", self.layout_text, file=sys.stderr)

    # --- Клавіатура --------------------------------------------------------

    def on_key_press(self, event) -> None:
        keysym = event.keysym

        # Автоповтор X11: «відпущено» вже прилетіло, і зараз ми бачимо його
        # пару. Скасовуємо відкладене відпускання — клавішу насправді тримають.
        job = self.pending_release.pop(keysym, None)
        if job is not None:
            self.root.after_cancel(job)
            return

        if keysym in self.held:
            return  # уже натиснута, повторний пакет пульту не потрібен

        step = self.keysym_to_enc.get(keysym)
        if step is not None:
            self.encoder(step)
            return

        code = self.keysym_to_code.get(keysym)
        if code is None:
            return
        self.held[keysym] = code
        self.link.send_key(code, True)

    def on_key_release(self, event) -> None:
        keysym = event.keysym
        if keysym not in self.held:
            return
        job = self.pending_release.pop(keysym, None)
        if job is not None:
            self.root.after_cancel(job)
        self.pending_release[keysym] = self.root.after(
            self.REPEAT_GRACE_MS, lambda k=keysym: self._release_now(k)
        )

    def _release_now(self, keysym: str) -> None:
        self.pending_release.pop(keysym, None)
        code = self.held.pop(keysym, None)
        if code is not None:
            self.link.send_key(code, False)

    def release_all(self) -> None:
        """Відпустити все. Вікно втратило фокус, розкладка змінилась, вихід.

        Пульт відпустив би сам — за тишею в каналі, — але робити це вчасно й
        зі свого боку правильніше: тайм-аут прошивки це остання перешкода, а
        не спосіб керування.
        """
        for job in self.pending_release.values():
            self.root.after_cancel(job)
        self.pending_release.clear()

        for keysym in list(self.held):
            code = self.held.pop(keysym)
            self.link.send_key(code, False)

        if self.touch_down:
            self.touch_down = False
            x, y = self.touch_last or (0, 0)
            self.link.send_touch(TOUCH_UP, x, y)

    # --- Енкодер -----------------------------------------------------------

    def encoder(self, steps: int) -> None:
        if not self.has_encoder or steps == 0:
            return
        self.link.send_enc(steps)

    def on_wheel(self, event) -> None:
        # X11 віддає колесо кнопками 4 і 5, решта світу — подією з delta.
        if event.num == 4:
            steps = -1
        elif event.num == 5:
            steps = 1
        else:
            steps = -1 if getattr(event, "delta", 0) > 0 else 1
        self.encoder(steps)

    # --- Сенсор ------------------------------------------------------------

    def touch(self, kind: int, point) -> None:
        if not self.has_touch or point is None:
            return
        x, y = point
        if kind == TOUCH_MOVE and (not self.touch_down or point == self.touch_last):
            return  # рух без натиску або на місці — пульту нема чого сказати
        if kind == TOUCH_DOWN:
            self.touch_down = True
        elif kind == TOUCH_UP:
            if not self.touch_down:
                return
            self.touch_down = False
        self.touch_last = point
        self.link.send_touch(kind, x, y)


class Link(threading.Thread):
    """Труба, PING і перепідключення. Живе у власному потоці.

    Розбір і розпаковка теж тут: інтерфейсний потік має лише показувати
    готовий кадр, інакше вікно застигало б на кожній великій плитці.
    """

    daemon = True

    def __init__(self, connect, on_frame, mask_input_state_bit=False):
        super().__init__(name="remote-ui-link")
        self.connect = connect
        self.session = Session(on_frame=on_frame,
                               mask_input_state_bit=mask_input_state_bit)
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

        # Ввід кладеться в чергу, а не пишеться в сокет із потоку вікна: труба
        # одна, і два потоки, що пишуть у неї одночасно, рано чи пізно
        # переплетуть половинки кадрів.
        self.outbox = queue.Queue(maxsize=512)
        self.sent = {"key": 0, "enc": 0, "touch": 0, "state": 0, "hold": 0}
        self.outbox_dropped = 0

        # Рівень вводу, який ми періодично повторюємо пульту. Пише потік вікна,
        # читає цей потік — звідси замок усередині InputMirror.
        self.mirror = InputMirror()

        # --- прилад під критерій 4-біс задачі 0013 ------------------------
        #
        # Питання, на яке він відповідає: коли міст каже «клієнт мовчав понад
        # 750 мс», клієнт справді мовчав — чи слав, а мовчання виникло далі
        # по дорозі? Числа з обох боків мають зійтись, інакше винен не той,
        # на кого думають.
        #
        # ⚠️ Поріг тут — копія `BRIDGE_CLIENT_SILENCE_MS` із моста. Він тільки
        # для друку: клієнт нічого за ним не робить, лише називає подію.
        self.state_gap_max_ms = 0.0
        self.state_gaps_over = 0
        self.state_sent = 0
        self.state_worst = ""
        # Ті самі відра, що в `/api/stats` моста — щоб два боки порівнювались
        # рядок у рядок, а не «на око».
        #
        # ⚠️ Порівняння дійсне лише коли клієнт не шле нічого, крім
        # `INPUT_STATE`. Міст рахує паузи між **будь-якими** пакетами вводу
        # (`KEY`, `ENC`, `TOUCH`, `TRIM`, `INPUT_STATE`), а тут — лише між
        # власними `INPUT_STATE`: те, що йде через `_drain_outbox`, у ці
        # відра не потрапляє. Тобто під час живого натискання числа
        # розійдуться, і це не розбіжність приладів.
        self.state_buckets = {"le300": 0, "le500": 0, "le750": 0, "over": 0}

    def send(self, frame: bytes, kind: str) -> None:
        """Кладе кадр вводу в чергу передачі. Викликається потоком вікна."""
        try:
            self.outbox.put_nowait(frame)
            self.sent[kind] += 1
        except queue.Full:
            self.outbox_dropped += 1

    # Переходи йдуть через ці три обгортки, а не через send() напряму: рівень
    # має оновитись **до** відправлення переходу, інакше INPUT_STATE, зібраний
    # на мілісекунду пізніше, суперечив би щойно надісланому пакету.

    def send_key(self, code: int, pressed: bool) -> None:
        self.mirror.key(code, pressed)
        self.send(encode_key(code, pressed), "key")

    def send_touch(self, kind: int, x: int, y: int) -> None:
        self.mirror.touch(kind, x, y)
        self.send(encode_touch(kind, x, y), "touch")

    def send_enc(self, steps: int) -> None:
        # Енкодера в дзеркалі немає: він накопичувальний, і рівня в нього
        # просто не існує (docs/03-protocol.md, правило 5).
        self.send(encode_enc(steps), "enc")

    def _hold_packet(self, session):
        """Обгортка над спільним `hold_packet()`: гілку вибирає біт3 у HELLO.

        Поки `HELLO` не прийшов, не шлемо нічого: утримувати ще нічого, а
        вгадувати версію прошивки — найкращий спосіб вибрати не ту гілку.
        """
        hello = session.hello
        if hello is None:
            return None, "state"

        return hold_packet(self.mirror, hello["has_input_state"])

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

    def _drain_outbox(self, transport) -> bool:
        """Віддає накопичений ввід. False — труба обірвалась."""
        while True:
            try:
                frame = self.outbox.get_nowait()
            except queue.Empty:
                return True
            try:
                transport.send(frame)
            except OSError as exc:
                self.status = f"розрив на передачі: {exc}"
                self.drops += 1
                return False

    def _serve(self, transport):
        decoder = self.decoder
        decoder.reset()
        session = self.session

        # Черга від попереднього з'єднання нікого не стосується: пульт уже
        # відпустив усе, що там могло лежати натиснутим.
        while not self.outbox.empty():
            try:
                self.outbox.get_nowait()
            except queue.Empty:
                break

        # Разом із чергою чиститься й рівень: пульт при розриві відпустив усе,
        # і наше дзеркало має погодитись із ним, а не переконувати його, що
        # клавіша досі натиснута.
        self.mirror.clear()

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
        last_state = now
        last_rx = now

        # Скільки часу й байтів набігло від попереднього INPUT_STATE. Потрібно
        # не для звіту, а щоб при довгій паузі одразу було видно, **на що**
        # вона пішла: на очікування даних чи на їх розпакування.
        span_recv_s = 0.0
        span_decode_s = 0.0
        span_bytes = 0
        last_report = time.monotonic()

        while not self.stop.is_set():
            t_recv0 = time.monotonic()
            data = transport.recv()
            t_recv1 = time.monotonic()
            span_recv_s += t_recv1 - t_recv0
            if data is None:
                self.status = "пульт закрив з'єднання"
                self.drops += 1
                return
            now = t_recv1
            if data:
                self.rx_bytes += len(data)
                span_bytes += len(data)
                last_rx = now
                for ptype, payload in decoder.feed(data):
                    session.handle(ptype, payload, now)
                self.crc_errors = decoder.crc_errors
                self.oversized = decoder.oversized
                span_decode_s += time.monotonic() - now
            elif now - last_rx > SILENCE_RESET_S:
                # Тиша в каналі. Якщо декодувальник завис посеред обірваного
                # кадру — звільняємо його, як це робить транспорт у прошивці.
                decoder.reset()
                last_rx = now

            session.flush_stale(now)

            # Ввід іде першим: людина чекає на реакцію, а не на статистику.
            if not self._drain_outbox(transport):
                return

            # ⚠️ Повний стан вводу — безумовно, а не «поки щось утримується».
            # Саме цей пакет тримає ввід натиснутим (тайм-аут прошивки 1000 мс
            # рахує тишу від нього, а не від PING) і саме він лікує втрачене
            # «відпущено». Лікувальний пакет надходить від клієнта, який вважає,
            # що не утримує нічого, — тому й безумовно.
            if now - last_state >= INPUT_STATE_PERIOD_S:
                packet, kind = self._hold_packet(session)
                if packet is not None:
                    try:
                        transport.send(packet)
                    except OSError as exc:
                        self.status = f"розрив на передачі: {exc}"
                        self.drops += 1
                        return
                    self.sent[kind] += 1

                    # ⚠️ Час беремо заново, а не `now`: `now` знято **до**
                    # розпакування, і саме різниця між ними — те, що міст
                    # бачить як мовчання клієнта.
                    sent_at = time.monotonic()
                    gap_ms = (sent_at - last_state) * 1000.0
                    self.state_sent += 1
                    if gap_ms <= 300:
                        self.state_buckets["le300"] += 1
                    elif gap_ms <= 500:
                        self.state_buckets["le500"] += 1
                    elif gap_ms <= CLIENT_SILENCE_REPORT_MS:
                        self.state_buckets["le750"] += 1
                    else:
                        self.state_buckets["over"] += 1
                    if gap_ms > self.state_gap_max_ms:
                        self.state_gap_max_ms = gap_ms
                        self.state_worst = (
                            f"{gap_ms:.0f} мс: чекав {span_recv_s * 1000:.0f}, "
                            f"розпаковував {span_decode_s * 1000:.0f}, "
                            f"{span_bytes} Б"
                        )
                    if gap_ms > CLIENT_SILENCE_REPORT_MS:
                        self.state_gaps_over += 1
                        print(
                            f"[пауза] INPUT_STATE через {gap_ms:.0f} мс "
                            f"(поріг моста {CLIENT_SILENCE_REPORT_MS:.0f}): "
                            f"чекав {span_recv_s * 1000:.0f} мс, "
                            f"розпаковував {span_decode_s * 1000:.0f} мс, "
                            f"прийняв {span_bytes} Б",
                            flush=True,
                        )
                    last_state = sent_at
                else:
                    last_state = now
                span_recv_s = 0.0
                span_decode_s = 0.0
                span_bytes = 0

            # Розкид пауз — на екран раз на пів хвилини. Без цього довгий прогін
            # довелось би читати очима, а порівнювати з мостом — по пам'яті.
            if now - last_report >= GAP_REPORT_PERIOD_S:
                b = self.state_buckets
                print(
                    f"[паузи клієнта] надіслано {self.state_sent}, "
                    f"максимум {self.state_gap_max_ms:.0f} мс, "
                    f"≤300:{b['le300']} ≤500:{b['le500']} "
                    f"≤750:{b['le750']} понад:{b['over']}",
                    flush=True,
                )
                last_report = now

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
        # Ані рамки, ані відступів: координати миші в цьому віджеті мають бути
        # координатами картинки, поділеними на масштаб, і нічим іншим.
        self.canvas = tk.Label(
            self.root, bg="#101010", text="очікую HELLO…", fg="#909090",
            bd=0, padx=0, pady=0, highlightthickness=0,
        )
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
        self.input = None
        self.shown = 0
        self.window_started = time.monotonic()
        self.window_shown = 0
        self.window_rx = 0
        self.window_frames = 0
        self.lag_ms = 0.0
        self.lag_max_ms = 0.0
        self.line = ("", "", "")

        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def submit(self, screen: Screen):
        """Викликається потоком зв'язку на кожен FRAME_END."""
        ppm = screen.ppm()
        with self.lock:
            if self.pending is not None:
                self.skipped += 1
            self.pending = (ppm, time.monotonic())

    def close(self):
        if self.input:
            self.input.release_all()
        if self.link:
            self.link.stop.set()
        self.root.quit()

    def run(self, link: Link):
        self.link = link
        self.input = Input(self.root, link)
        self._bind_input()
        self.root.after(self.POLL_MS, self._tick)
        self.root.mainloop()

    # --- Ввід ---------------------------------------------------------------

    def _bind_input(self):
        inp = self.input

        self.root.bind("<KeyPress>", inp.on_key_press)
        self.root.bind("<KeyRelease>", inp.on_key_release)

        # Колесо: X11 віддає його кнопками 4 і 5, решта світу — <MouseWheel>.
        for sequence in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
            self.root.bind(sequence, inp.on_wheel)

        self.canvas.bind("<ButtonPress-1>", self._on_touch_down)
        self.canvas.bind("<B1-Motion>", self._on_touch_move)
        self.canvas.bind("<ButtonRelease-1>", self._on_touch_up)

        # Вікно втратило фокус — клавіатура більше не наша, і те, що людина
        # тримала, треба відпустити самим, не чекаючи тайм-ауту прошивки.
        self.root.bind("<FocusOut>", lambda _event: inp.release_all())

        self.root.focus_set()

    def _point(self, event):
        """Координати миші -> піксель екрана пульта, обрізаний по межах.

        Саме обрізаний, а не відкинутий: якщо палець виїхав за картинку,
        відпускання все одно має дійти, інакше дотик лишиться натиснутим.
        """
        session = self.link.session if self.link else None
        screen = session.screen if session else None
        if screen is None:
            return None
        x = max(0, min(screen.w - 1, int(event.x) // self.scale))
        y = max(0, min(screen.h - 1, int(event.y) // self.scale))
        return (x, y)

    def _on_touch_down(self, event):
        self.root.focus_set()  # клік по картинці не має забирати клавіатуру
        session = self.link.session if self.link else None
        screen = session.screen if session else None
        if screen is None:
            return
        if not (0 <= event.x < screen.w * self.scale and
                0 <= event.y < screen.h * self.scale):
            return  # натиск повз картинку пульта не стосується
        self.input.touch(TOUCH_DOWN, self._point(event))

    def _on_touch_move(self, event):
        self.input.touch(TOUCH_MOVE, self._point(event))

    def _on_touch_up(self, event):
        self.input.touch(TOUCH_UP, self._point(event))

    def _tick(self):
        if self.input is not None and self.link is not None:
            self.input.sync(self.link.session.hello)

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
            fps_got = (session.frame_ends - self.window_frames) / elapsed
            kib = (link.rx_bytes - self.window_rx) / elapsed / 1024
            hello = session.hello
            head = (
                f"{hello['target']} {hello['fw']}  {hello['width']}x{hello['height']}  "
                f"клавіш {len(hello['keys'])}"
                if hello
                else "HELLO ще не прийшов"
            )
            sent = link.sent
            self.line = (
                f"{head}   {link.status}",
                f"кадрів/с {fps_shown:4.1f} (FRAME_END/с {fps_got:4.1f})   "
                f"{kib:6.1f} КіБ/с   плиток {session.last_frame_tiles:3d}   "
                f"затримка {self.lag_ms:4.1f} мс (макс {self.lag_max_ms:.0f})   "
                f"CRC {link.crc_errors}  довж {link.oversized}  "
                f"биті плитки {session.bad_tiles}  "
                f"цілих {session.frames_whole}  за строком {session.frames_timeout}  "
                f"без FRAME_END {session.forced}  "
                f"застарілі {self.skipped}  розриви {link.drops}",
                f"надіслано: клавіш {sent['key']:4d}  енкодер {sent['enc']:4d}  "
                f"дотик {sent['touch']:5d}  стан {sent['state']:4d}  "
                f"утримання {sent['hold']:4d}  "
                f"черга не влізла {link.outbox_dropped}"
                f"   |   {self.input.layout_text if self.input else ''}",
            )
            self.window_started = now
            self.window_shown = 0
            self.window_rx = link.rx_bytes
            self.window_frames = session.frame_ends
        self.stats.configure(text="\n".join(self.line))


def main() -> int:
    ap = argparse.ArgumentParser(description="Тестовий клієнт Remote UI: вікно з екраном пульта")
    add_transport_args(ap)
    ap.add_argument("--scale", type=int, default=1, help="ціле збільшення картинки")
    ap.add_argument(
        "--mask-input-state-bit",
        action="store_true",
        help="тестова підміна: викинути біт3 з прийнятого HELLO і вдати стару "
        "прошивку — клієнт має перейти на PING раз на 250 мс, поки щось "
        "утримується",
    )
    args = ap.parse_args()

    # 5 мс, а не типові 50: цим вікном ще й керують, і кожна мілісекунда тут
    # додається до затримки «натиснув -> побачив».
    where, connect = make_connector(args, poll=0.005)

    window = Window(f"Remote UI — {where}", max(1, args.scale))
    link = Link(connect, window.submit,
                mask_input_state_bit=args.mask_input_state_bit)
    link.start()
    try:
        window.run(link)
    except KeyboardInterrupt:
        pass
    link.stop.set()

    # Підсумок пауз — на екран, а не в нікуди: без нього довгий прогін довелось
    # би переглядати очима по рядках «[пауза]».
    print(
        f"\nINPUT_STATE: надіслано {link.state_sent}, "
        f"найдовша пауза {link.state_gap_max_ms:.0f} мс, "
        f"пауз понад {CLIENT_SILENCE_REPORT_MS:.0f} мс — {link.state_gaps_over}",
        flush=True,
    )
    if link.state_worst:
        print(f"найгірша: {link.state_worst}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
