#!/usr/bin/env python3
"""Протокол Remote UI на боці ПК: кадрування, CRC, HELLO, RLE16, транспорти.

Другий, незалежний примірник протоколу — перший живе в
`firmware/edgetx-patch/remote_ui/` на C++. Дві реалізації сходяться на
однакових байтах (`tools/proto_test.py`, еталонні вектори задачі 0004), і
саме це робить `docs/03-protocol.md` специфікацією, а не переказом коду.

Модуль спільний для `tools/capture_check.py` (PNG після факту) і
`tools/test_client.py` (вікно живцем): розбір протоколу має бути в одному
примірнику, інакше два клієнти розійдуться в дрібницях і шукати доведеться
довго.

Залежності: стандартна бібліотека. `pyserial` потрібен **лише** для
послідовного транспорту й імпортується в момент відкриття порту.
"""

import base64
import hashlib
import os
import socket
import struct
import threading

MARKER = b"\xE7\x7E"

# Пульт → клієнт
PKT_HELLO = 0x01
PKT_TILE = 0x02
PKT_FRAME_END = 0x03
PKT_STATE = 0x04
PKT_LOG = 0x05
PKT_BAUD = 0x06

# Клієнт → пульт
PKT_KEY = 0x81
PKT_ENC = 0x82
PKT_TOUCH = 0x83
PKT_REFRESH = 0x84
PKT_TRIM = 0x85
PKT_PING = 0x86
PKT_INPUT_STATE = 0x87
PKT_BAUD_SET = 0x88

TILE_METHOD_RAW = 0
TILE_METHOD_RLE16 = 1

# Події сенсора в пакеті TOUCH.
TOUCH_DOWN = 0
TOUCH_MOVE = 1
TOUCH_UP = 2

HELLO_PIXFMT_RGB565 = 1
HELLO_PIXFMT_MONO1 = 2

# Прапорці в HELLO (docs/03-protocol.md, байт 6).
#
# Біти 0…2 описують залізо, біт3 — версію прошивки: «розумію INPUT_STATE».
# Стара прошивка його не виставляє, бо його там просто немає, і нуль означає
# рівно те, що й має означати, — незадіяні біти прапорців завжди були нулями.
HELLO_FLAG_TOUCH = 0x01
HELLO_FLAG_ENCODER = 0x02
HELLO_FLAG_FILE_OPS = 0x04
HELLO_FLAG_INPUT_STATE = 0x08

MAX_PAYLOAD = 4096
FRAME_OVERHEAD = 7

DEFAULT_TCP_PORT = 7616
DEFAULT_BAUD = 921600

# Скид декодувальника за тишею в каналі. Дзеркало SILENCE_RESET_MS з
# transport_simu.cpp: шар кадрування часу не має, тому обірваний посеред кадру
# потік звільняє транспорт. Число описує протокол, а не трубу, тож однакове
# для TCP і для UART.
SILENCE_RESET_S = 0.100

# Періодичність PING і стеля мовчання пульта.
#
# PING — перевірка живого **пульта**, і більше нічого: у відповідь приходить
# HELLO. Утримання вводу на ньому не тримається, тому 2 с достатньо.
PING_PERIOD_S = 2.0
PING_TIMEOUT_S = 5.0

# Періодичність INPUT_STATE — повного стану вводу.
#
# ⚠️ Безумовна, а не «поки щось утримується», і це головна неочевидність
# протоколу. Лікувальний пакет надходить саме від клієнта, який вважає, що не
# утримує **нічого**: «відпущено» він уже надіслав, воно й загубилось. Тобто
# періодичність, прив'язана до «щось утримується», лікувала б рівно ті випадки,
# яких не буває.
#
# Прошивка відпускає ввід після INPUT_RELEASE_TIMEOUT_MS (1000 мс) без
# INPUT_STATE, тож 0.25 с — це чотири пропущені періоди запасу. Два числа
# пов'язані й міняються тільки разом (docs/03-protocol.md).
INPUT_STATE_PERIOD_S = 0.25


def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: поліном 0x1021, початок 0xFFFF, без рефлексії."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def encode_frame(ptype: int, payload: bytes = b"") -> bytes:
    """Кадр на дроті: маркер, тип, довжина, дані, CRC."""
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload {len(payload)} Б понад стелю {MAX_PAYLOAD}")
    body = bytes([ptype]) + struct.pack("<H", len(payload)) + payload
    return MARKER + body + struct.pack("<H", crc16_ccitt_false(body))


# --- Пакети вводу (клієнт -> пульт) ---------------------------------------
#
# Окремі функції, а не «зібрати руками на місці»: два інструменти вже колись
# розійшлися в дрібницях, і саме тому весь протокол живе в одному модулі.


def encode_key(code: int, pressed: bool) -> bytes:
    """KEY: код клавіші з EnumKeys і стан."""
    return encode_frame(PKT_KEY, bytes([code & 0xFF, 1 if pressed else 0]))


def encode_enc(steps: int) -> bytes:
    """ENC: зсув енкодера, знаковий байт."""
    return encode_frame(PKT_ENC, struct.pack("<b", max(-128, min(127, steps))))


def encode_touch(event: int, x: int, y: int) -> bytes:
    """TOUCH: подія (DOWN/MOVE/UP) і точка."""
    return encode_frame(PKT_TOUCH, struct.pack("<BHH", event & 0xFF, x & 0xFFFF, y & 0xFFFF))


def encode_trim(index: int, pressed: bool) -> bytes:
    """TRIM: номер напрямку тримера і стан."""
    return encode_frame(PKT_TRIM, bytes([index & 0xFF, 1 if pressed else 0]))


def encode_input_state(keys: int, trims: int, touch_down: bool, x: int, y: int) -> bytes:
    """INPUT_STATE: повний стан вводу — рівень, а не перехід.

    13 байтів: маска клавіш (4), маска напрямків тримерів (4), прапорці
    дотику (1), x і y (по 2), усе little-endian.

    Енкодера тут немає навмисно: він накопичувальний, і заміщення накопичувача
    рівнем дало б фантомний оберт назад (docs/03-protocol.md, правило 5).

    Координати при touch_down=False пульт не читає взагалі, але кладемо туди
    останню відому точку — так у журналі видно, де палець був востаннє.
    """
    return encode_frame(
        PKT_INPUT_STATE,
        struct.pack(
            "<IIBHH",
            keys & 0xFFFFFFFF,
            trims & 0xFFFFFFFF,
            1 if touch_down else 0,
            x & 0xFFFF,
            y & 0xFFFF,
        ),
    )


def encode_baud_set(baud: int, nonce: int) -> bytes:
    """BAUD_SET: попросити пульт перемкнути швидкість каналу (ADR-0005).

    5 байтів: швидкість (4, LE) і nonce (1).

    ⚠️ nonce потрібен не для краси. Людина тисне «2 000 000», відповіді немає,
    тисне «1 000 000» — а підтвердження на **першу** команду доїжджає із
    запізненням. Без nonce його прийняли б за відповідь на другу, і сторони
    розійшлися б **без жодної відмови в каналі**: обидві вважали б, що
    домовились, але про різне. Пульт nonce не перевіряє, а повертає як є, —
    сплутати має бути неможливо саме на боці того, хто питав.
    """
    return encode_frame(PKT_BAUD_SET, struct.pack("<IB", baud & 0xFFFFFFFF, nonce & 0xFF))


# Вердикти пакета BAUD. Дзеркало BaudVerdict із firmware/.../baudrate.h.
BAUD_ACCEPTED = 0
BAUD_UNSUPPORTED = 1
BAUD_BUSY = 2
BAUD_NOT_APPLICABLE = 3
BAUD_REVERTED = 4

BAUD_VERDICT_NAMES = {
    BAUD_ACCEPTED: "перемкнеться",
    BAUD_UNSUPPORTED: "швидкості немає в переліку",
    BAUD_BUSY: "уже триває перемикання",
    BAUD_NOT_APPLICABLE: "транспорт не має поняття швидкості",
    BAUD_REVERTED: "пульт повернувся сам",
}


def parse_baud(payload: bytes) -> dict | None:
    """BAUD: відповідь пульта про швидкість. 16 байтів.

    Повертає None, якщо вантаж коротший за відому частину: частково прочитане
    число швидкості гірше за нечитане. Довший хвіст ігнорується — це правило
    сумісності протоколу.
    """
    if len(payload) < 16:
        return None

    verdict, nonce, target, current, switch_delay, revert_window, reverts = struct.unpack(
        "<BBIIHHH", payload[:16]
    )
    return {
        "verdict": verdict,
        "verdict_name": BAUD_VERDICT_NAMES.get(verdict, f"невідомий ({verdict})"),
        "nonce": nonce,
        "target": target,
        # Нуль означає «поняття не застосовне» (USB CDC, TCP симулятора), а не
        # швидкість нуль: нуля в переліку немає й бути не може.
        "current": current if current else None,
        "switch_delay_ms": switch_delay,
        "revert_window_ms": revert_window,
        "reverts": reverts,
    }


class InputMirror:
    """Дзеркало власного вводу клієнта — джерело для INPUT_STATE.

    Живе в спільному модулі, а не в кожному інструменті окремо: рівень, який
    розійшовся з надісланими переходами, — це рівно та вада, яку INPUT_STATE і
    має лікувати, тож двох реалізацій тут бути не повинно.

    ⚠️ Порядок обов'язковий: спершу оновити дзеркало, потім слати перехід.
    Тоді стан на дроті ніколи не суперечить раніше надісланому переходу.
    Пульт від дотримання цього правила не залежить, але з ним поведінка
    передбачувана.

    Замок потрібен, бо в тестовому клієнті дзеркало пише потік вікна, а читає
    потік труби. Без нього пакет міг би зібратись із нової маски клавіш і
    старого стану дотику — рівно та розбіжність, яку INPUT_STATE лікує.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.keys = 0
        self.trims = 0
        self.touch_down = False
        self.x = 0
        self.y = 0

    def key(self, code: int, pressed: bool) -> None:
        if code >= 32:
            return  # у 32-бітову маску не влазить — пульт таку клавішу теж відкине
        bit = 1 << code
        with self.lock:
            self.keys = self.keys | bit if pressed else self.keys & ~bit

    def trim(self, index: int, pressed: bool) -> None:
        if index >= 32:
            return
        bit = 1 << index
        with self.lock:
            self.trims = self.trims | bit if pressed else self.trims & ~bit

    def touch(self, event: int, x: int, y: int) -> None:
        with self.lock:
            self.x, self.y = x, y
            if event == TOUCH_DOWN:
                self.touch_down = True
            elif event == TOUCH_UP:
                self.touch_down = False
            # MOVE лише пересуває точку — рівень від нього не змінюється.

    def clear(self) -> None:
        """Відпустити все у дзеркалі. Втратили вікно, рвемо з'єднання, виходимо."""
        with self.lock:
            self.keys = 0
            self.trims = 0
            self.touch_down = False

    def holding(self) -> bool:
        """Чи утримується хоч що-небудь.

        Потрібне лише запасному шляху для старої прошивки: там утримання
        тримається на `PING`, а слати його часто є сенс тільки поки щось
        натиснуте (docs/03-protocol.md, таблиця в розділі 0x87). Енкодера тут
        немає й бути не може — він накопичувальний, рівня в нього не існує.
        """
        with self.lock:
            return bool(self.keys or self.trims or self.touch_down)

    def encode(self) -> bytes:
        """Пакет будується в момент відправлення, а не зі знятого раніше знімка."""
        with self.lock:
            return encode_input_state(self.keys, self.trims, self.touch_down, self.x, self.y)


def hold_packet(mirror: InputMirror, fw_input_state: bool):
    """Що слати раз на INPUT_STATE_PERIOD_S, щоб пульт не відпустив ввід.

    Повертає пару `(кадр або None, назва лічильника)`.

    Дві гілки, і вибирає між ними **прошивка**, а не клієнт — біт3 у `HELLO`
    (docs/03-protocol.md, таблиця в розділі 0x87):

    - **біт виставлений** — повний стан вводу, безумовно. Лікувальний пакет
      надходить саме від клієнта, який вважає, що не утримує нічого: «відпущено»
      він уже надіслав, воно й загубилось.
    - **біт нуль** — стара прошивка. Вона `INPUT_STATE` не знає й відкине його
      як невідомий тип, зате в ній ще діє правило «будь-який пакет доводить, що
      клієнт живий», тому утримання тримається на `PING`. І тільки поки щось
      утримується: на старій прошивці частий `PING` — це зайвий трафік і зайве
      `HELLO` у відповідь, а лікувати там усе одно нічого.

    Живе тут, а не в кожному інструменті: гілка «стара прошивка» інакше існувала
    б лише в одному з них, тобто наполовину. Та сама причина, з якої тут живе
    `InputMirror`.
    """
    if fw_input_state:
        return mirror.encode(), "state"

    if mirror.holding():
        return encode_frame(PKT_PING), "hold"

    return None, "hold"


class Decoder:
    """Потоковий розбір кадрів. Дзеркало remote_ui::Decoder, тільки на Python.

    Той самий порядок ресинхронізації, що в прошивці: після відкинутого кадру
    пошук маркера продовжується з байта, наступного за цим кадром, а вже
    прочитаний payload повторно не переглядається (docs/03-protocol.md).
    """

    def __init__(self):
        self.buf = bytearray()
        self.packets = 0
        self.crc_errors = 0
        self.oversized = 0

    def reset(self):
        """Забути недочитаний кадр. Викликає транспорт після тиші в каналі."""
        self.buf.clear()

    def feed(self, data: bytes):
        self.buf.extend(data)
        while True:
            start = self.buf.find(MARKER)
            if start < 0:
                # Маркера немає: лишаємо останній байт — раптом він половина маркера.
                del self.buf[: max(0, len(self.buf) - 1)]
                return
            if start:
                del self.buf[:start]
            if len(self.buf) < FRAME_OVERHEAD:
                return
            ptype = self.buf[2]
            length = self.buf[3] | (self.buf[4] << 8)
            if length > MAX_PAYLOAD:
                # Брехлива довжина: стільки байтів не читаємо й не пропускаємо —
                # ресинхронізація починається одразу за старшим байтом LEN.
                del self.buf[:5]
                self.oversized += 1
                continue
            total = FRAME_OVERHEAD + length
            if len(self.buf) < total:
                return
            body = bytes(self.buf[2 : 5 + length])
            got = self.buf[5 + length] | (self.buf[6 + length] << 8)
            payload = bytes(self.buf[5 : 5 + length])
            del self.buf[:total]
            if got == crc16_ccitt_false(body):
                self.packets += 1
                yield ptype, payload
            else:
                self.crc_errors += 1


def parse_hello(payload: bytes) -> dict:
    """Розбір HELLO. Читаємо рівно ті поля, які знаємо: решта — сумісність."""
    version, width, height, pixfmt, flags, trims, keymask, nkeys = struct.unpack_from(
        "<BHHBBBIB", payload, 0
    )
    pos = 13
    keys = []
    for _ in range(nkeys):
        if pos + 17 > len(payload):
            break
        code = payload[pos]
        name = payload[pos + 1 : pos + 17].split(b"\0")[0].decode("utf-8", "replace")
        keys.append((code, name))
        pos += 17
    target = payload[pos : pos + 32].split(b"\0")[0].decode("utf-8", "replace")
    pos += 32
    fw = payload[pos : pos + 16].split(b"\0")[0].decode("utf-8", "replace")
    pos += 16

    # --- Швидкість каналу (ADR-0005) --------------------------------------
    #
    # Хвіст, якого стара прошивка не шле. Його відсутність — не помилка, а
    # відповідь: перемикання ця прошивка не вміє. Тому все під перевіркою
    # довжини, і жодного винятку.
    baud_current = baud_home = None
    baud_list = []
    if len(payload) >= pos + 9:
        cur, home, n = struct.unpack_from("<IIB", payload, pos)
        pos += 9
        # ⚠️ Нуль означає «поняття не застосовне» (TCP, USB CDC), а не
        # швидкість нуль: нуля в переліку немає й бути не може.
        baud_current = cur or None
        baud_home = home or None
        for _ in range(n):
            if pos + 4 > len(payload):
                break
            baud_list.append(struct.unpack_from("<I", payload, pos)[0])
            pos += 4

    return {
        "version": version,
        "width": width,
        "height": height,
        "pixfmt": pixfmt,
        "flags": flags,
        "trims": trims,
        "keymask": keymask,
        "keys": keys,
        "target": target,
        "fw": fw,
        # Розібрані прапорці лежать поруч із сирим байтом, а не замість нього:
        # сире значення потрібне, щоб побачити біт, якого ця версія клієнта ще
        # не знає.
        "has_touch": bool(flags & HELLO_FLAG_TOUCH),
        "has_encoder": bool(flags & HELLO_FLAG_ENCODER),
        "has_file_ops": bool(flags & HELLO_FLAG_FILE_OPS),
        "has_input_state": bool(flags & HELLO_FLAG_INPUT_STATE),
        # Швидкість каналу. `baud_list` порожній означає рівно одне:
        # перемикання ця прошивка або цей транспорт не підтримують. Окремого
        # прапорця немає навмисно — два сигнали про одну річ розійшлися б.
        "baud_current": baud_current,
        "baud_home": baud_home,
        "baud_list": baud_list,
        "can_switch_baud": bool(baud_list),
    }


def rle16_decode(data: bytes, want: int):
    """Повертає список пікселів або None, якщо потік битий.

    Саме None, а не виняток: одна зіпсута плитка не має валити інструмент.
    """
    if len(data) % 3:
        return None
    out = []
    for i in range(0, len(data), 3):
        count = data[i]
        if count == 0:
            return None
        out.extend([data[i + 1] | (data[i + 2] << 8)] * count)
    return out if len(out) == want else None


# --- Транспорти -----------------------------------------------------------
#
# Один інтерфейс, дві труби. На кроці 2.4 той самий клієнт піде через
# перетворювач USB-UART — тоді міняється рівно рядок командного рядка, а не
# клієнт.
#
# Домовленість про повернене з recv():
#   bytes  — дані (порожні = у каналі тиша, це не помилка);
#   None   — канал закрито з того боку, потрібне перепідключення.


class Transport:
    """Спільний інтерфейс труби."""

    name = "?"

    def recv(self) -> bytes | None:
        raise NotImplementedError

    def send(self, data: bytes) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class TcpTransport(Transport):
    """TCP до симулятора (transport_simu.cpp слухає 127.0.0.1:7616)."""

    def __init__(self, host: str, port: int, connect_timeout=5.0, poll=0.05):
        self.sock = socket.create_connection((host, port), timeout=connect_timeout)
        self.sock.settimeout(poll)
        self.name = f"tcp {host}:{port}"

    def recv(self):
        try:
            data = self.sock.recv(65536)
        except TimeoutError:
            return b""
        except OSError:
            return None
        # У TCP порожнє читання — це кінець потоку, а не тиша.
        return data if data else None

    def send(self, data: bytes):
        self.sock.sendall(data)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class SerialTransport(Transport):
    """Послідовний порт: залізо на кроці 2.4, заглушка (pty, socat) — уже зараз.

    Пристрій відкривається через `serial_for_url`, тому приймаються і звичайні
    шляхи (`/dev/ttyUSB0`, `/dev/pts/7`), і адреси pyserial (`socket://…`,
    `loop://`) — остання стає в пригоді, коли заліза ще немає.
    """

    def __init__(self, device: str, baud: int = DEFAULT_BAUD, poll=0.05):
        try:
            import serial
        except ImportError:
            raise RuntimeError("Немає pyserial:  pip install --user pyserial")
        self.ser = serial.serial_for_url(device, baudrate=baud, timeout=poll)
        self.name = f"serial {device} @ {baud}"

    def recv(self):
        try:
            waiting = self.ser.in_waiting
            # Читаємо все, що вже прийшло; коли не прийшло нічого — чекаємо
            # один байт не довше за timeout, щоб не крутити процесор даремно.
            data = self.ser.read(waiting if waiting else 1)
        except OSError:
            return None
        # У послідовного порту тиша — звичайна справа, а не розрив.
        return data

    def send(self, data: bytes):
        self.ser.write(data)

    def set_baudrate(self, baud: int) -> None:
        """Перемкнути швидкість цього боку — дзеркало того, що робить міст.

        ⚠️ Порядок такий самий, як у прошивки й у моста, і він не випадковий:

        1. дочекатись, поки все відправлене зійде з дроту (`flush`) — інакше
           недописаний кадр доїде вже на новій швидкості й стане сміттям;
        2. записати нову швидкість;
        3. викинути все, що встигло накопичитись у приймачі, — байти, які
           ловилися в мить перемикання, спотворені за побудовою.

        Пункт 3 робиться **після** пункту 2, а не до нього.
        """
        self.ser.flush()
        self.ser.baudrate = baud
        self.ser.reset_input_buffer()
        self.name = f"serial {self.ser.port} @ {baud}"

    def close(self):
        try:
            self.ser.close()
        except OSError:
            pass


class WsTransport(Transport):
    """WebSocket до моста ESP32 — третя труба, з тим самим інтерфейсом.

    Потрібна, щоб **уся наявна перевірена оснастка заговорила з мостом**:
    `test_client.py` (вікно з екраном пульта) і будь-який новий інструмент
    отримують міст безкоштовно, замість того щоб писати для нього окремих
    клієнтів. На боці моста це той самий байтовий потік, що й на дроті, —
    міст пересилає, не тлумачачи.

    Клієнт WebSocket написаний тут руками, без сторонніх бібліотек: решта
    `tools/` теж обходиться стандартною бібліотекою, а тягнути залежність
    заради шести кілобайтів коду не варто.

    ⚠️ Кадри від клієнта до сервера **обов'язково маскуються** — це вимога
    RFC 6455, а не забаганка: без маски сервер має розірвати з'єднання.
    """

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    OP_TEXT, OP_BINARY = 0x1, 0x2
    OP_CLOSE, OP_PING, OP_PONG = 0x8, 0x9, 0xA

    def __init__(self, url: str, poll: float = 0.05, connect_timeout: float = 5.0):
        host, port, path = self._split(url)
        self.sock = socket.create_connection((host, port), timeout=connect_timeout)
        self.sock.settimeout(poll)
        self.buf = bytearray()
        self.name = f"ws {url}"
        self._handshake(host, port, path)

    @staticmethod
    def _split(url: str):
        rest = url.split("://", 1)[1] if "://" in url else url
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        return host, int(port) if port else 80, "/" + path if path else "/ws"

    def _handshake(self, host, port, path):
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode("ascii"))

        head = bytearray()
        deadline = 5.0
        while b"\r\n\r\n" not in head:
            try:
                chunk = self.sock.recv(1024)
            except TimeoutError:
                deadline -= 0.05
                if deadline <= 0:
                    raise RuntimeError("міст не відповів на рукостискання WebSocket")
                continue
            if not chunk:
                raise RuntimeError("міст закрив з'єднання під час рукостискання")
            head += chunk

        header, _, tail = bytes(head).partition(b"\r\n\r\n")
        text = header.decode("latin-1")
        if "101" not in text.split("\r\n", 1)[0]:
            raise RuntimeError(f"міст не перейшов на WebSocket: {text.splitlines()[0]}")

        want = base64.b64encode(hashlib.sha1((key + self.GUID).encode()).digest()).decode()
        if want.lower() not in text.lower():
            raise RuntimeError("міст повернув хибний Sec-WebSocket-Accept")

        self.buf += tail   # дані могли приїхати разом із відповіддю

    def _frame(self, payload: bytes, opcode: int) -> bytes:
        head = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mask = os.urandom(4)
        head += mask
        return bytes(head) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

    def _take(self):
        """Вийняти з буфера один цілий кадр. Повертає (opcode, payload) або None."""
        if len(self.buf) < 2:
            return None
        opcode = self.buf[0] & 0x0F
        masked = bool(self.buf[1] & 0x80)
        length = self.buf[1] & 0x7F
        pos = 2

        if length == 126:
            if len(self.buf) < pos + 2:
                return None
            length = struct.unpack_from(">H", self.buf, pos)[0]
            pos += 2
        elif length == 127:
            if len(self.buf) < pos + 8:
                return None
            length = struct.unpack_from(">Q", self.buf, pos)[0]
            pos += 8

        mask = None
        if masked:
            if len(self.buf) < pos + 4:
                return None
            mask = bytes(self.buf[pos:pos + 4])
            pos += 4

        if len(self.buf) < pos + length:
            return None

        payload = bytes(self.buf[pos:pos + length])
        del self.buf[:pos + length]
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    def recv(self):
        try:
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            self.buf += chunk
        except TimeoutError:
            pass
        except OSError:
            return None

        out = bytearray()
        while True:
            got = self._take()
            if got is None:
                break
            opcode, payload = got
            if opcode == self.OP_CLOSE:
                return None
            if opcode == self.OP_PING:
                try:
                    self.sock.sendall(self._frame(payload, self.OP_PONG))
                except OSError:
                    return None
            elif opcode in (self.OP_BINARY, self.OP_TEXT, 0x0):
                out += payload
        return bytes(out)

    def send(self, data: bytes):
        self.sock.sendall(self._frame(data, self.OP_BINARY))

    def close(self):
        try:
            self.sock.sendall(self._frame(b"", self.OP_CLOSE))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def add_transport_args(parser):
    """Прапорці вибору труби — однакові в усіх інструментах."""
    parser.add_argument(
        "--tcp",
        metavar="[ХОСТ:]ПОРТ",
        default=f"127.0.0.1:{DEFAULT_TCP_PORT}",
        help="TCP до симулятора (типово 127.0.0.1:7616)",
    )
    parser.add_argument(
        "--serial",
        metavar="ПРИСТРІЙ",
        help="послідовний порт замість TCP: /dev/ttyUSB0, /dev/pts/7, socket://…",
    )
    parser.add_argument(
        "--ws",
        metavar="URL",
        help="WebSocket до моста ESP32: ws://192.168.4.1/ws",
    )
    parser.add_argument(
        "--baud", type=int, default=DEFAULT_BAUD, help="швидкість порту (типово 921600)"
    )


def make_connector(args, poll: float = 0.05):
    """Повертає (опис, функція-з'єднувач).

    З'єднувач створює **новий** транспорт на кожен виклик: клієнт має право
    перепідключатись скільки завгодно разів.

    `poll` — скільки труба чекає на дані, перш ніж віддати «тиша». Це ще й
    стеля затримки на **передачу**: цикл клієнта віддає накопичений ввід одразу
    після читання, тож інструмент, яким керують, просить тут малого числа, а
    той, що лише дивиться, — звичайного.
    """
    if getattr(args, "ws", None):
        url = args.ws
        return f"ws {url}", lambda: WsTransport(url, poll)

    if args.serial:
        device, baud = args.serial, args.baud
        return f"serial {device} @ {baud}", lambda: SerialTransport(device, baud, poll)

    host, _, port = args.tcp.rpartition(":")
    host = host or "127.0.0.1"
    port = int(port)
    return f"tcp {host}:{port}", lambda: TcpTransport(host, port, poll=poll)
