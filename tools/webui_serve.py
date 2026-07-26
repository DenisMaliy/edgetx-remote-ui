#!/usr/bin/env python3
"""Програмний двійник моста ESP32: HTTP + WebSocket на ПК.

Робить рівно те саме, що прошивка моста, тільки на комп'ютері: віддає
`webui/` по HTTP, приймає WebSocket на `/ws` і перекладає байти між ним і
пультом (TCP до симулятора або послідовний порт).

**Навіщо це є.** Клієнт у браузері інакше можна було б перевірити тільки на
зібраному стенді з живим ESP32 — тобто найпізніше і найдорожче. З цим
двійником браузерний клієнт перевіряється на симуляторі EdgeTX, і на залізо
приїжджає вже робочим. Та сама причина, з якої в проєкті існує
`tools/test_client.py`.

⚠️ Це **інструмент розробки**, а не полегшена версія моста. Він слухає всі
інтерфейси, бо на нього заходять з телефона в тій самій мережі, і жодного
пароля не питає — у полі його не використовують.

Поведінку, від якої залежить безпека, двійник повторює навмисно:

* втративши клієнта, **негайно** шле пульту обнулений `INPUT_STATE`;
* сам `PING` не породжує — тільки пересилає те, що надіслав клієнт;
* новий клієнт витісняє попереднього.

Приклади:

    # проти симулятора EdgeTX (він слухає 127.0.0.1:7616)
    python3 tools/webui_serve.py

    # проти живого пульта через перетворювач USB-UART
    python3 tools/webui_serve.py --serial /dev/ttyUSB0

Далі відкрити http://<адреса цього ПК>:8080/ у браузері або з телефона.
"""

import argparse
import base64
import hashlib
import json
import os
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import remote_ui_proto as proto  # noqa: E402

WEBUI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webui")

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BINARY = 0x0, 0x1, 0x2
OP_CLOSE, OP_PING, OP_PONG = 0x8, 0x9, 0xA

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}

# Скільки мовчання клієнта означає «телефон зник». Те саме число, що в
# bridge_cfg.h: три пропущені періоди INPUT_STATE і менше за тайм-аут
# відпускання в прошивці (1000 мс).
CLIENT_SILENCE_S = 0.750


class Stats:
    """Лічильники, названі так само, як у `/api/stats` справжнього моста."""

    def __init__(self):
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.d = {
            "uart_bytes": 0, "packets": 0, "tiles": 0, "frames": 0,
            "crc_errors": 0, "oversized": 0,
            "ws_bytes": 0, "ws_chunks": 0, "ws_errors": 0,
            "client_bytes": 0, "client_packets": 0, "client_input": 0,
            "clients_seen": 0, "clients_lost": 0,
            "input_releases": 0, "silence_timeouts": 0,
        }

    def bump(self, key, by=1):
        with self.lock:
            self.d[key] += by

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
            },
            "ws": {
                "bytes": d["ws_bytes"], "chunks": d["ws_chunks"], "errors": d["ws_errors"],
                # Двійник нічого не відкидає: у ПК канал не вузький. Поля
                # лишаються, щоб панель стану виглядала однаково.
                "tiles_dropped": 0, "packets_dropped": 0,
                "chunks_dropped": 0, "chunks_noclient": 0,
            },
            "client_to_radio": {
                "bytes": d["client_bytes"], "packets": d["client_packets"],
                "input": d["client_input"],
            },
            "session": {
                "seen": d["clients_seen"], "lost": d["clients_lost"],
                "releases": d["input_releases"], "silence_timeouts": d["silence_timeouts"],
            },
            "note": "програмний двійник моста (tools/webui_serve.py), не ESP32",
        }


STATS = Stats()

# Поточний клієнт. Двійник, як і справжній міст, тримає рівно одного.
CLIENT_LOCK = threading.Lock()
CLIENT_ID = 0


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

    # --- звичайний HTTP --------------------------------------------------

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path == "/ws":
            self.handle_ws()
            return

        if path == "/api/stats":
            body = json.dumps(STATS.snapshot(CLIENT_ID != 0)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        name = "index.html" if path == "/" else path.lstrip("/")
        # Ходити вище webui/ не даємо: інструмент інструментом, а віддавати
        # весь диск тому, хто відкрив сторінку, не треба.
        full = os.path.normpath(os.path.join(WEBUI_DIR, name))
        if not full.startswith(WEBUI_DIR) or not os.path.isfile(full):
            self.send_error(404, "немає такого файлу")
            return

        with open(full, "rb") as f:
            body = f.read()
        ctype = CONTENT_TYPES.get(os.path.splitext(full)[1], "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.split("?", 1)[0] == "/api/wifi/off":
            body = json.dumps({
                "ok": False,
                "note": "це програмний двійник моста — Wi-Fi тут вимикати нема чого",
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
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

        global CLIENT_ID
        with CLIENT_LOCK:
            CLIENT_ID += 1
            my_id = CLIENT_ID
        STATS.bump("clients_seen")
        print(f"  клієнт {my_id} під'єднався")

        try:
            self.pump(my_id)
        except (OSError, struct.error):
            pass
        finally:
            self.client_lost(my_id, silence=False)

    def client_lost(self, my_id, silence):
        """Втратили клієнта — негайно відпускаємо ввід на пульті."""
        global CLIENT_ID
        with CLIENT_LOCK:
            if CLIENT_ID != my_id:
                return   # нас уже витіснив новіший клієнт
            CLIENT_ID = 0

        STATS.bump("clients_lost")
        STATS.bump("input_releases")
        if silence:
            STATS.bump("silence_timeouts")

        # ⚠️ Та сама вимога протоколу, що й до справжнього моста: розрив має
        # відпускати ввід за мілісекунди, а не за тайм-аутом у 1000 мс.
        try:
            self.server.radio.send(proto.encode_input_state(0, 0, False, 0, 0))
        except OSError:
            pass
        print(f"  клієнт {my_id} зник ({'мовчання' if silence else 'розрив'}) — "
              f"ввід на пульті відпущено")

    def pump(self, my_id):
        radio = self.server.radio
        sock = self.connection
        send_lock = threading.Lock()
        stop = threading.Event()

        last_input = [None]   # коли востаннє прийшов пакет із вводом

        def radio_to_ws():
            """Пульт → браузер. Двійник нічого не відкидає, лише рахує."""
            decoder = proto.Decoder()
            while not stop.is_set():
                data = radio.recv()
                if data is None:
                    stop.set()
                    break
                if not data:
                    continue

                STATS.bump("uart_bytes", len(data))
                for ptype, _payload in decoder.feed(data):
                    STATS.bump("packets")
                    if ptype == proto.PKT_TILE:
                        STATS.bump("tiles")
                    elif ptype == proto.PKT_FRAME_END:
                        STATS.bump("frames")
                STATS.d["crc_errors"] = decoder.crc_errors
                STATS.d["oversized"] = decoder.oversized

                try:
                    with send_lock:
                        sock.sendall(ws_encode(data))
                    STATS.bump("ws_bytes", len(data))
                    STATS.bump("ws_chunks")
                except OSError:
                    STATS.bump("ws_errors")
                    stop.set()
                    break

        def watchdog():
            """Сторож мовчання — третій спосіб помітити втрату клієнта."""
            while not stop.wait(0.1):
                t = last_input[0]
                if t is not None and time.monotonic() - t > CLIENT_SILENCE_S:
                    self.client_lost(my_id, silence=True)
                    stop.set()
                    try:
                        sock.close()
                    except OSError:
                        pass
                    break

        threading.Thread(target=radio_to_ws, daemon=True).start()
        threading.Thread(target=watchdog, daemon=True).start()

        # Браузер → пульт. Пересилаємо як є; типи дивимось лише заради живості.
        client_scan = proto.Decoder()
        while not stop.is_set():
            opcode, payload = ws_read(self.rfile)
            if opcode is None or opcode == OP_CLOSE:
                break
            if opcode == OP_PING:
                with send_lock:
                    sock.sendall(ws_encode(payload, OP_PONG))
                continue
            if opcode not in (OP_BINARY, OP_TEXT, OP_CONT) or not payload:
                continue

            radio.send(payload)
            STATS.bump("client_bytes", len(payload))

            for ptype, _p in client_scan.feed(payload):
                STATS.bump("client_packets")
                # ⚠️ Той самий перелік, що в прошивці й у мості: PING і
                # REFRESH вводу не несуть і живим клієнта не роблять.
                if ptype in (proto.PKT_KEY, proto.PKT_ENC, proto.PKT_TOUCH,
                             proto.PKT_TRIM, proto.PKT_INPUT_STATE):
                    STATS.bump("client_input")
                    last_input[0] = time.monotonic()

        stop.set()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    proto.add_transport_args(ap)
    ap.add_argument("--port", type=int, default=8080, help="порт HTTP (типово 8080)")
    ap.add_argument("--bind", default="0.0.0.0", help="адреса для прослуховування")
    ap.add_argument("-v", "--verbose", action="store_true", help="журнал запитів HTTP")
    args = ap.parse_args()

    desc, connect = proto.make_connector(args, poll=0.02)
    print(f"пульт: {desc}")
    radio = connect()

    httpd = ThreadingHTTPServer((args.bind, args.port), Handler)
    httpd.radio = radio
    httpd.verbose = args.verbose

    print(f"сторінка: http://localhost:{args.port}/")
    print("Ctrl-C — зупинити")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nзупиняюсь")
    finally:
        radio.close()


if __name__ == "__main__":
    main()
