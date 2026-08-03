#!/usr/bin/env python3
"""Програмний двійник моста ESP32: HTTP + WebSocket на ПК.

Робить те саме, що прошивка моста, тільки на комп'ютері: віддає `webui/` по
HTTP, приймає WebSocket на `/ws` і перекладає байти між ним і пультом (TCP до
симулятора або послідовний порт).

**Навіщо це є.** Клієнт у браузері інакше можна було б перевірити тільки на
зібраному стенді з живим ESP32 — тобто найпізніше й найдорожче. З двійником
браузерний клієнт перевіряється на симуляторі EdgeTX і приїжджає на залізо
вже робочим. Та сама причина, з якої існує `tools/test_client.py`.

⚠️ **Двійник має право відрізнятися від моста в чому завгодно, крім
поведінки, яку на ньому доводять.** Рецензія 2026-07-27 спіймала саме це:
двійник стверджував, що витісняє клієнта, а насправді лишав потоки старого —
два потоки читали один транспорт, старий клієнт далі слав ввід, обнулений
`INPUT_STATE` при витісненні не йшов. Тобто пункт «кілька телефонів
одночасно» на ньому **не перевірявся й не міг бути перевірений**.

Тому будова тут навмисно та сама, що в мості: **один** читач пульта на весь
сервер (як `rx_task`), один поточний клієнт, і те саме розрізнення двох
різних висновків:

| Подія | Що робить | Як у мості |
|---|---|---|
| сокет закрився | відпустити ввід, забути клієнта | `client_drop` |
| прибулець на зайнятий пульт | сказати «зайнято» й закрити сокет | `client_arrived` |
| переймання (`take=1`) | відпустити ввід, вигнати старого | `client_arrived` |
| повернення свого (`resume=1`) | те саме, але окремим лічильником | `client_arrived` |
| мовчання понад 750 мс | відпустити ввід, **сокет лишити** | `ws_bridge_release_if_silent` |

⚠️ Це **інструмент розробки**. Він слухає всі інтерфейси, бо на нього
заходять із телефона в тій самій мережі, і пароля не питає. У полі його не
використовують.

Приклади:

    python3 tools/webui_serve.py                      # проти симулятора
    python3 tools/webui_serve.py --serial /dev/ttyUSB0  # проти живого пульта

Далі відкрити http://<адреса цього ПК>:8080/ у браузері або з телефона.
"""

import argparse
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import remote_ui_proto as proto  # noqa: E402

# ⚠️ Не константа, а типове значення: `--dir` перемикає стенд на **зібрану**
# сторінку (задача 0022). Клієнт їде у флеш мініфікованим, і зелені тести
# проти джерела нічого не кажуть про те, що там опинилось: зламане
# мініфікатором пройшло б повз них непоміченим.
#
#     node webui/minify.mjs /tmp/built
#     python3 tools/webui_serve.py --dir /tmp/built
WEBUI_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webui"))

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BINARY = 0x0, 0x1, 0x2
OP_CLOSE, OP_PING, OP_PONG = 0x8, 0x9, 0xA

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    # Опис застосунку й значок — щоб стенд без заліза вмів те саме, що міст:
    # без правильного типу браузер опис просто не візьме (задача 0023, 1.4).
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".png": "image/png",
}

# Те саме число, що BRIDGE_CLIENT_SILENCE_MS у мості: три пропущені періоди
# INPUT_STATE і менше за тайм-аут відпускання в прошивці (1000 мс).
CLIENT_SILENCE_S = 0.750
WATCHDOG_PERIOD_S = 0.1


def bridge_lost_name(taken):
    """Причина втрати сеансу тими самими словами, що `bridge_lost_name` у мості."""
    return "керування перейняв інший телефон" if taken else "господар повернувся на свій слот"


class Stats:
    """Лічильники, названі так само, як у `/api/stats` справжнього моста."""

    def __init__(self):
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.d = {
            "uart_bytes": 0, "packets": 0, "tiles": 0, "frames": 0,
            "crc_errors": 0, "oversized": 0, "silence_resets": 0,
            "ws_bytes": 0, "ws_chunks": 0, "ws_errors": 0,
            "client_bytes": 0, "client_packets": 0, "client_input": 0,
            "clients_seen": 0, "clients_lost": 0,
            "input_releases": 0, "silence_timeouts": 0,
            # Черга (задача 0024) — ті самі назви, що в `/api/stats` моста.
            "busy_refused": 0, "takeovers": 0, "resumes": 0, "status_polls": 0,
            "lost_evicted": 0, "lost_resumed": 0,
        }

    def bump(self, key, by=1):
        with self.lock:
            self.d[key] += by

    def put(self, key, value):
        with self.lock:
            self.d[key] = value

    def snapshot(self, has_client):
        with self.lock:
            d = dict(self.d)
        return {
            "uptime_s": int(time.monotonic() - self.started),
            "baud": "—", "heap_free": "—", "heap_min": "—",
            "client": has_client,
            "uart": {
                "bytes": d["uart_bytes"], "packets": d["packets"], "tiles": d["tiles"],
                "frames": d["frames"], "crc_errors": d["crc_errors"],
                "oversized": d["oversized"], "dropped": 0,
                "silence_resets": d["silence_resets"],
            },
            "ws": {
                "bytes": d["ws_bytes"], "chunks": d["ws_chunks"], "errors": d["ws_errors"],
                # ⚠️ Двійник нічого не відкидає: у ПК канал не вузький, і
                # черги перед Wi-Fi тут немає взагалі. Поля лишаються з тими
                # самими назвами, щоб панель стану й проби виглядали однаково,
                # але **нулі тут нічого не доводять** — втрати перевіряються
                # на залізі.
                "tiles_dropped": 0, "packets_dropped": 0,
                "chunks_dropped": 0, "chunks_noclient": 0, "send_dropped": 0,
            },
            "client_to_radio": {
                "bytes": d["client_bytes"], "packets": d["client_packets"],
                "input": d["client_input"],
            },
            "session": {
                "seen": d["clients_seen"], "lost": d["clients_lost"],
                "releases": d["input_releases"], "silence_timeouts": d["silence_timeouts"],
                # ⚠️ Двійник рахує лише сокети WebSocket: решта з'єднань тут
                # живе в потоках `http.server`, і числа, порівнянного з
                # `httpd_get_client_list` моста, з них не вийде.
                "sockets": BRIDGE.socket_count(),
            },
            "queue": {
                "busy_refused": d["busy_refused"], "takeovers": d["takeovers"],
                "resumes": d["resumes"], "status_polls": d["status_polls"],
            },
            "lost_by": {
                "evicted": d["lost_evicted"], "resumed": d["lost_resumed"],
            },
            # ⚠️ Прилад купи двійник має віддавати **об'єктом**, а не рискою:
            # плашка пам'яті в клієнті з'являється рівно тоді, коли в `/api/stats`
            # є поле `heap` (задача 0021). Без нього перевірити «за одне
            # торкання видно пам'ять моста» на стенді без заліза неможливо —
            # плашка просто лишалась би схованою.
            #
            # ⚠️ Числа тут **вигадані й нічого не доводять**: у ПК купа не
            # закінчується. Замір пам'яті робиться на живому мості, а тут
            # перевіряється тільки те, що клієнт це поле вміє показати.
            "heap": {
                "ceiling": 262144, "warn_at": 32768, "warned": False,
                "min_window": 131072, "min_idle": 131072, "min_stream": None,
                "min_page": None, "min_first_min": None, "min_last_min": None,
                "recover_first_min": None, "recover_last_min": None,
                "note": "двійник: справжньої купи тут немає",
            },
            "note": "програмний двійник моста (tools/webui_serve.py), не ESP32",
        }


STATS = Stats()


class Session:
    """Один під'єднаний клієнт. Поточний завжди рівно один — як у мості."""

    _next_id = 0
    _id_lock = threading.Lock()

    def __init__(self, sock):
        with Session._id_lock:
            Session._next_id += 1
            self.id = Session._next_id
        self.sock = sock
        self.since = time.monotonic()   # коли з'явився: свіжий ще не мав коли заговорити
        self.send_lock = threading.Lock()
        self.stop = threading.Event()
        self.scan = proto.Decoder()
        self.last_input = None      # коли востаннє прийшов пакет із вводом
        self.input_released = False  # засувка: відпускаємо раз на епізод

    def send_frame(self, payload, opcode=OP_BINARY):
        with self.send_lock:
            self.sock.sendall(ws_encode(payload, opcode))

    def kick(self):
        """Вигнати: розбудити всі читання й закрити сокет."""
        self.stop.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class Bridge:
    """Стан двійника: пульт, поточний клієнт і те, що між ними."""

    def __init__(self, radio):
        self.radio = radio
        self.lock = threading.Lock()
        self.current = None
        self.current_id = ""
        self.sessions = 0          # живих сокетів WebSocket, включно з чергою
        self.stop = threading.Event()

    # --- клієнт ---------------------------------------------------------

    def attach(self, session, claim):
        """Пустити, віддати керування на вимогу або сказати «зайнято».

        Те саме рішення, що `client_arrived` у мості, і в тому самому порядку:
        усе під одним замком, дії — після нього.

        :return: True, якщо клієнт став господарем.
        """
        with self.lock:
            old = self.current
            busy = old is not None and old is not session
            # ⚠️ «Це знову я» діє лише коли того господаря вже не чути — те саме
            # правило, що в мості, і з тієї самої причини: ім'я живе одне
            # завантаження сторінки, тож збіг при **живому** господарі означає
            # наш власний другий сокет, і «повернення свого» стало б
            # витісненням самого себе.
            heard = old.last_input if (busy and old.last_input) else (
                old.since if busy else None)
            ghost = busy and (time.monotonic() - heard > CLIENT_SILENCE_S)
            mine = busy and ghost and claim["resume"] and claim["id"] != "" \
                and claim["id"] == self.current_id
            allow = (not busy) or claim["take"] or mine
            if allow:
                self.current = session
                self.current_id = claim["id"]

        if not allow:
            STATS.bump("busy_refused")
            print(f"  клієнт {session.id} прийшов на зайнятий пульт — кажу «зайнято»")
            return False

        if claim["take"]:
            STATS.bump("takeovers")
        elif claim["resume"]:
            STATS.bump("resumes")

        STATS.bump("clients_seen")

        if busy:
            # ⚠️ Саме те, чого двійник не робив: справді вигнати старого і
            # відпустити ввід. Старий міг щось утримувати, а новий заявить
            # власний стан не пізніше ніж за 250 мс.
            taken = claim["take"]
            what = ("керування перейнято" if taken else "господар повернувся на свій слот")
            print(f"  {what} (клієнт {old.id} → {session.id})")
            old.kick()
            STATS.bump("clients_lost")
            STATS.bump("lost_evicted" if taken else "lost_resumed")
            # Слова ті самі, що в мості: журнали двійника й моста звіряються.
            self.release_input(bridge_lost_name(taken))

        print(f"  клієнт {session.id} під'єднався")
        return True

    def socket_count(self):
        with self.lock:
            return self.sessions

    def detach(self, session, reason):
        """Клієнт зник: забути його й відпустити ввід."""
        with self.lock:
            if self.current is not session:
                return   # нас уже витіснив новіший — він і відпустив ввід
            self.current = None
            # Ім'я гасне разом із сокетом: інакше `resume` пускав би до
            # чужого слота. Те саме правило, що в `client_drop` моста.
            self.current_id = ""

        STATS.bump("clients_lost")
        self.release_input(reason)
        print(f"  клієнт {session.id} зник ({reason}) — ввід на пульті відпущено")

    def peek(self):
        with self.lock:
            return self.current

    # --- ввід -----------------------------------------------------------

    def release_input(self, reason, silence=False):
        """Обнулений `INPUT_STATE` — негайно, не чекаючи тайм-ауту."""
        STATS.bump("input_releases")
        if silence:
            STATS.bump("silence_timeouts")
        try:
            self.radio.send(proto.encode_input_state(0, 0, False, 0, 0))
        except OSError:
            pass

    # --- потоки ---------------------------------------------------------

    def radio_reader(self):
        """Один читач пульта на весь сервер — рівно як `rx_task` у мості.

        ⚠️ Саме «один» тут і є виправленням: доти кожен сеанс заводив
        власного читача, і два потоки ділили байти одного транспорту між
        собою, породжуючи суцільні помилки CRC у клієнта.
        """
        decoder = proto.Decoder()
        last_byte = time.monotonic()

        while not self.stop.is_set():
            data = self.radio.recv()
            if data is None:
                print("  пульт закрив канал")
                self.stop.set()
                break

            now = time.monotonic()
            if not data:
                # Скид розбирача за тишею — обов'язок транспортного шару.
                if now - last_byte > proto.SILENCE_RESET_S and decoder.buf:
                    decoder.reset()
                    STATS.bump("silence_resets")
                    last_byte = now
                continue

            last_byte = now
            STATS.bump("uart_bytes", len(data))
            for ptype, _payload in decoder.feed(data):
                STATS.bump("packets")
                if ptype == proto.PKT_TILE:
                    STATS.bump("tiles")
                elif ptype == proto.PKT_FRAME_END:
                    STATS.bump("frames")
            STATS.put("crc_errors", decoder.crc_errors)
            STATS.put("oversized", decoder.oversized)

            session = self.peek()
            if session is None:
                continue
            try:
                session.send_frame(data)
                STATS.bump("ws_bytes", len(data))
                STATS.bump("ws_chunks")
            except OSError:
                STATS.bump("ws_errors")
                self.detach(session, "розрив на передачі")

    def watchdog(self):
        """Сторож мовчання: відпускає ввід, але **не рве сокет** — як у мості."""
        while not self.stop.wait(WATCHDOG_PERIOD_S):
            session = self.peek()
            if session is None:
                continue
            last = session.last_input
            if last is None or session.input_released:
                continue
            if time.monotonic() - last > CLIENT_SILENCE_S:
                session.input_released = True
                self.release_input("мовчання", silence=True)
                print(f"  клієнт {session.id} мовчить — ввід відпущено, сокет лишаю")


BRIDGE = None   # заповнюється в main()


def ws_encode(payload, opcode=OP_BINARY):
    """Кадр WebSocket від сервера — без маски, за RFC 6455."""
    head = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head.append(n)
    elif n < 65536:
        head.append(126)
        head += struct.pack(">H", n)
    else:
        head.append(127)
        head += struct.pack(">Q", n)
    return bytes(head) + payload


def ws_read(rfile):
    """Прочитати кадр від клієнта. Повертає (opcode, payload) або (None, None)."""
    head = rfile.read(2)
    if len(head) < 2:
        return None, None

    opcode = head[0] & 0x0F
    masked = bool(head[1] & 0x80)
    length = head[1] & 0x7F

    if length == 126:
        length = struct.unpack(">H", rfile.read(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", rfile.read(8))[0]

    mask = rfile.read(4) if masked else None
    payload = rfile.read(length) if length else b""

    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "tx16s-remote-ui-bridge-double"

    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write("  http: " + (fmt % args) + "\n")

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # --- звичайний HTTP --------------------------------------------------

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path == "/ws":
            self.handle_ws()
            return

        if path == "/api/status":
            # Найдешевша відповідь моста: нею живе клієнт, що стоїть у черзі.
            STATS.bump("status_polls")
            self._json({"busy": 1 if BRIDGE.peek() is not None else 0,
                        "sockets": BRIDGE.socket_count()})
            return

        if path == "/api/stats":
            self._json(STATS.snapshot(BRIDGE.peek() is not None))
            return

        name = "index.html" if path == "/" else path.lstrip("/")
        # Ходити вище webui/ не даємо: інструмент інструментом, а віддавати
        # увесь диск тому, хто відкрив сторінку, не треба.
        full = os.path.normpath(os.path.join(WEBUI_DIR, name))
        # ⚠️ Через `commonpath`, а не `startswith`: для теки `/tmp/built` той
        # пропустив би й `/tmp/built-evil`, бо порівнює рядки, а не шляхи.
        try:
            inside = os.path.commonpath([full, WEBUI_DIR]) == WEBUI_DIR
        except ValueError:      # різні диски — спільного шляху немає взагалі
            inside = False
        if not inside or not os.path.isfile(full):
            # ⚠️ Кирилиця йде **третім** аргументом (тіло, UTF-8), а не другим:
            # другий потрапляє в рядок стану HTTP, а той кодується latin-1 і
            # валить обробник `UnicodeEncodeError`. Замість чесного 404 клієнт
            # бачив розрив з'єднання — тобто діагностика ламала діагностику.
            self.send_error(404, "Not Found", "немає такого файлу")
            return

        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type",
                         CONTENT_TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.split("?", 1)[0] == "/api/wifi/off":
            self._json({"ok": False,
                        "note": "це програмний двійник моста — Wi-Fi тут вимикати нема чого"})
            return
        self.send_error(404)

    # --- WebSocket -------------------------------------------------------

    def handle_ws(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self.send_error(400, "це не WebSocket")
            return

        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
        ).decode("ascii")

        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        # Після переходу на WebSocket наступного запиту HTTP у цьому з'єднанні
        # не буде ніколи: лишений відкритим, обробник читав би кадри WebSocket
        # як заголовки запиту.
        self.close_connection = True

        session = Session(self.connection)
        with BRIDGE.lock:
            BRIDGE.sessions += 1
        try:
            if not BRIDGE.attach(session, self.claim()):
                # ⚠️ Слово, а не мовчазний розрив: клієнт має відрізнити
                # «зайнято» від обриву, інакше він повернеться через секунду —
                # і гойдалка, заради якої все робилось, повернеться з ним.
                try:
                    session.send_frame(b'{"busy":1}', OP_TEXT)
                except OSError:
                    pass
                session.kick()
                return

            reason = "розрив"
            try:
                reason = self.pump(session)
            except (OSError, struct.error):
                pass
            finally:
                BRIDGE.detach(session, reason)
        finally:
            with BRIDGE.lock:
                BRIDGE.sessions -= 1

    def claim(self):
        """Чим назвався прибулець у рядку запиту `/ws` — як `read_claim` у мості."""
        query = parse_qs(urlparse(self.path).query)
        name = query.get("id", [""])[0]
        return {
            "take": query.get("take", ["0"])[0] == "1",
            "resume": query.get("resume", ["0"])[0] == "1",
            # ⚠️ Задовге ім'я тут **відкидається**, а не обрізається — рівно як
            # у мості (`WS_CLIENT_ID_MAX`): обрізані імена двох різних клієнтів
            # злилися б в одне, і `resume` пускав би чужого.
            "id": name if len(name) <= 16 else "",
        }

    def pump(self, session):
        """Браузер → пульт. Пересилаємо як є; типи дивимось лише для живості."""
        while not session.stop.is_set():
            opcode, payload = ws_read(self.rfile)
            if opcode is None:
                return "розрив"
            if opcode == OP_CLOSE:
                return "сокет закрито"
            if opcode == OP_PING:
                session.send_frame(payload, OP_PONG)
                continue
            if opcode not in (OP_BINARY, OP_TEXT, OP_CONT) or not payload:
                continue

            BRIDGE.radio.send(payload)
            STATS.bump("client_bytes", len(payload))

            for ptype, _p in session.scan.feed(payload):
                STATS.bump("client_packets")
                # ⚠️ Той самий перелік, що в прошивці й у мості: `PING` і
                # `REFRESH` вводу не несуть і живим клієнта не роблять.
                if ptype in (proto.PKT_KEY, proto.PKT_ENC, proto.PKT_TOUCH,
                             proto.PKT_TRIM, proto.PKT_INPUT_STATE):
                    STATS.bump("client_input")
                    session.last_input = time.monotonic()
                    session.input_released = False

        return "витіснено"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    proto.add_transport_args(ap)
    ap.add_argument("--dir", help="тека клієнта (типово webui/); "
                                  "для прогону проти зібраної сторінки")
    ap.add_argument("--port", type=int, default=8080, help="порт HTTP (типово 8080)")
    ap.add_argument("--bind", default="0.0.0.0", help="адреса для прослуховування")
    ap.add_argument("-v", "--verbose", action="store_true", help="журнал запитів HTTP")
    args = ap.parse_args()

    if args.dir:
        global WEBUI_DIR
        WEBUI_DIR = os.path.normpath(os.path.abspath(args.dir))
        if not os.path.isfile(os.path.join(WEBUI_DIR, "index.html")):
            ap.error(f"у теці {WEBUI_DIR} немає index.html")
        print(f"клієнт: {WEBUI_DIR}")

    desc, connect = proto.make_connector(args, poll=0.02)
    print(f"пульт: {desc}")

    global BRIDGE
    BRIDGE = Bridge(connect())

    # Перше, що двійник каже пульту, — «відпусти все»: як і міст при старті.
    BRIDGE.release_input("старт")

    threading.Thread(target=BRIDGE.radio_reader, daemon=True).start()
    threading.Thread(target=BRIDGE.watchdog, daemon=True).start()

    httpd = ThreadingHTTPServer((args.bind, args.port), Handler)
    httpd.verbose = args.verbose

    print(f"сторінка: http://localhost:{args.port}/")
    print("Ctrl-C — зупинити")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nзупиняюсь")
    finally:
        BRIDGE.stop.set()
        BRIDGE.radio.close()


if __name__ == "__main__":
    main()
