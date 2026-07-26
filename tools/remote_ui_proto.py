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

# Клієнт → пульт
PKT_KEY = 0x81
PKT_ENC = 0x82
PKT_TOUCH = 0x83
PKT_REFRESH = 0x84
PKT_TRIM = 0x85
PKT_PING = 0x86
PKT_INPUT_STATE = 0x87

TILE_METHOD_RAW = 0
TILE_METHOD_RLE16 = 1

# Події сенсора в пакеті TOUCH.
TOUCH_DOWN = 0
TOUCH_MOVE = 1
TOUCH_UP = 2

HELLO_PIXFMT_RGB565 = 1
HELLO_PIXFMT_MONO1 = 2

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

    def encode(self) -> bytes:
        """Пакет будується в момент відправлення, а не зі знятого раніше знімка."""
        with self.lock:
            return encode_input_state(self.keys, self.trims, self.touch_down, self.x, self.y)


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

    def close(self):
        try:
            self.ser.close()
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
    if args.serial:
        device, baud = args.serial, args.baud
        return f"serial {device} @ {baud}", lambda: SerialTransport(device, baud, poll)

    host, _, port = args.tcp.rpartition(":")
    host = host or "127.0.0.1"
    port = int(port)
    return f"tcp {host}:{port}", lambda: TcpTransport(host, port, poll=poll)
