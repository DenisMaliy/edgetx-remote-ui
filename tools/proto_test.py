#!/usr/bin/env python3
"""Тести протоколу на боці ПК: еталонні вектори, кадрування, RLE16, сесія.

Головне тут — **звірка з прошивкою**. Числа взяті не з нашого ж коду, а з
тестів C++ (`firmware/edgetx-patch/remote_ui/test/`), тобто з другої,
незалежної реалізації. Якщо два розбори розійдуться, це побачить тест, а не
людина через тиждень на залізі.

    python3 tools/proto_test.py

Залежностей немає: `unittest` зі стандартної бібліотеки. Тести сесії не
створюють вікна, тому проходять і без графіки.
"""

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import remote_ui_proto as proto  # noqa: E402
from test_client import Input, Link, Session, tile_rgb  # noqa: E402


def hello_payload(width, height, keys=(), target="X10", fw="2.12.2-remoteui", flags=0x03):
    """Синтетичний HELLO — щоб тести не залежали від запущеного симулятора."""
    out = bytearray()
    out.append(1)
    out += struct.pack("<HH", width, height)
    out.append(proto.HELLO_PIXFMT_RGB565)
    out.append(flags)  # типово сенсор + енкодер
    out.append(6)  # тримерів
    out += struct.pack("<I", 0xFF)
    out.append(len(keys))
    for code, name in keys:
        out.append(code)
        out += name.encode()[:16].ljust(16, b"\0")
    out += target.encode()[:32].ljust(32, b"\0")
    out += fw.encode()[:16].ljust(16, b"\0")
    return bytes(out)


def rle_pairs(pairs):
    """[(лічильник, піксель), …] → байти RLE16."""
    out = bytearray()
    for count, pixel in pairs:
        out += bytes([count, pixel & 0xFF, (pixel >> 8) & 0xFF])
    return bytes(out)


def tile_payload(x, y, w, h, method, body):
    return struct.pack("<HHHHB", x, y, w, h, method) + body


class GoldenVectors(unittest.TestCase):
    """Байти на дроті. Ті самі числа, що в test_protocol.cpp і в docs/03."""

    def test_empty_ping(self):
        # docs/03-protocol.md і test_protocol.cpp: EncodeGoldenFrame
        self.assertEqual(
            proto.encode_frame(proto.PKT_PING),
            bytes.fromhex("E77E86000066 45".replace(" ", "")),
        )

    def test_hello_one_byte(self):
        # test_protocol.cpp: HELLO з єдиним байтом 0xAA
        self.assertEqual(
            proto.encode_frame(proto.PKT_HELLO, b"\xAA"),
            bytes.fromhex("E77E010100AAE4D1"),
        )

    def test_input_state(self):
        # Той самий вектор розбирає C++ (test_input.cpp): Python кодує — пульт
        # застосовує. Стан: клавіша 4, напрямок тримера 1, палець у (100, 200).
        self.assertEqual(
            proto.encode_input_state(1 << 4, 1 << 1, True, 100, 200),
            bytes.fromhex("E77E870D001000000002000000016400C800B36E"),
        )

    def test_crc_of_check_string(self):
        # Класичний вектор CRC-16/CCITT-FALSE: "123456789" -> 0x29B1.
        self.assertEqual(proto.crc16_ccitt_false(b"123456789"), 0x29B1)

    def test_constants_match_spec(self):
        self.assertEqual(proto.MAX_PAYLOAD, 4096)
        self.assertEqual(proto.FRAME_OVERHEAD, 7)

    def test_periods_match_spec(self):
        # Поріг відпускання в прошивці — 1000 мс, тобто чотири пропущені
        # періоди INPUT_STATE. Числа пов'язані й міняються тільки разом.
        self.assertEqual(proto.INPUT_STATE_PERIOD_S, 0.25)
        self.assertEqual(proto.PING_PERIOD_S, 2.0)


class Framing(unittest.TestCase):
    def decode_all(self, decoder, data, chunk=None):
        got = []
        if chunk is None:
            got.extend(decoder.feed(data))
        else:
            for i in range(0, len(data), chunk):
                got.extend(decoder.feed(data[i : i + chunk]))
        return got

    def test_roundtrip_whole_and_byte_by_byte(self):
        stream = (
            proto.encode_frame(proto.PKT_HELLO, b"\x01\x02")
            + proto.encode_frame(proto.PKT_FRAME_END)
            + proto.encode_frame(proto.PKT_TILE, bytes(range(64)))
        )
        for chunk in (None, 1, 3, 7):
            decoder = proto.Decoder()
            got = self.decode_all(decoder, stream, chunk)
            self.assertEqual([p[0] for p in got],
                             [proto.PKT_HELLO, proto.PKT_FRAME_END, proto.PKT_TILE])
            self.assertEqual(got[2][1], bytes(range(64)))
            self.assertEqual(decoder.crc_errors, 0)

    def test_garbage_before_marker(self):
        decoder = proto.Decoder()
        stream = b"\x00\xFF\x7E\x12\xE7\x34\xAA\xE7\xE7" + proto.encode_frame(proto.PKT_PING)
        got = self.decode_all(decoder, stream)
        self.assertEqual([p[0] for p in got], [proto.PKT_PING])

    def test_marker_inside_payload(self):
        # Маркер у даних не екранується: його має відсіяти довжина й CRC.
        payload = b"\xE7\x7E\x01\x00\x00" * 4
        decoder = proto.Decoder()
        got = self.decode_all(decoder, proto.encode_frame(proto.PKT_TILE, payload))
        self.assertEqual(got, [(proto.PKT_TILE, payload)])

    def test_bad_crc_dropped_next_packet_survives(self):
        broken = bytearray(proto.encode_frame(proto.PKT_TILE, b"\x11\x22\x33"))
        broken[-1] ^= 0xFF
        decoder = proto.Decoder()
        got = self.decode_all(decoder, bytes(broken) + proto.encode_frame(proto.PKT_PING))
        self.assertEqual([p[0] for p in got], [proto.PKT_PING])
        self.assertEqual(decoder.crc_errors, 1)

    def test_oversized_length_resyncs_from_next_byte(self):
        # LEN = 0xFFFF: стільки не буває. Ресинхронізація починається одразу
        # за старшим байтом довжини, а не через 65535 байтів.
        liar = bytes([0xE7, 0x7E, proto.PKT_TILE, 0xFF, 0xFF])
        decoder = proto.Decoder()
        got = self.decode_all(decoder, liar + proto.encode_frame(proto.PKT_PING))
        self.assertEqual([p[0] for p in got], [proto.PKT_PING])
        self.assertEqual(decoder.oversized, 1)
        self.assertEqual(decoder.crc_errors, 0)

    def test_reset_forgets_partial_frame(self):
        head = proto.encode_frame(proto.PKT_TILE, b"\x01" * 32)[:10]
        decoder = proto.Decoder()
        self.assertEqual(self.decode_all(decoder, head), [])
        decoder.reset()
        got = self.decode_all(decoder, proto.encode_frame(proto.PKT_PING))
        self.assertEqual([p[0] for p in got], [proto.PKT_PING])


class Rle16(unittest.TestCase):
    """Вектори з test_rle16.cpp, тільки з боку розпакування."""

    def test_solid_block(self):
        # RleSolidBlockCollapses: 1024 пікселі 0xF81F -> 4 серії по 255 + одна на 4.
        data = rle_pairs([(255, 0xF81F)] * 4 + [(4, 0xF81F)])
        self.assertEqual(len(data), 15)
        self.assertEqual(data[0], 255)
        self.assertEqual((data[1], data[2]), (0x1F, 0xF8))
        self.assertEqual(data[12], 4)
        self.assertEqual(proto.rle16_decode(data, 1024), [0xF81F] * 1024)

    def test_counter_stops_at_255(self):
        # RleCounterStopsAt255: 300 пікселів 0x1234 -> 255 + 45.
        data = rle_pairs([(255, 0x1234), (45, 0x1234)])
        self.assertEqual(len(data), 6)
        self.assertEqual(proto.rle16_decode(data, 300), [0x1234] * 300)

    def test_rejects_zero_count(self):
        self.assertIsNone(proto.rle16_decode(rle_pairs([(0, 0x1234)]), 1))

    def test_rejects_truncated_and_wrong_size(self):
        self.assertIsNone(proto.rle16_decode(b"\x02\x34", 2))
        self.assertIsNone(proto.rle16_decode(rle_pairs([(2, 0x1234)]), 3))

    def test_raw_and_rle_give_same_picture(self):
        pixels = [0xF800] * 8 + [0x001F] * 8
        raw = tile_rgb(proto.TILE_METHOD_RAW, struct.pack("<16H", *pixels), 16)
        rle = tile_rgb(proto.TILE_METHOD_RLE16, rle_pairs([(8, 0xF800), (8, 0x001F)]), 16)
        self.assertEqual(raw, rle)
        self.assertEqual(raw[:3], b"\xFF\x00\x00")  # чистий червоний лишається чистим
        self.assertEqual(raw[-3:], b"\x00\x00\xFF")

    def test_unknown_method_rejected(self):
        self.assertIsNone(tile_rgb(7, b"\x00" * 32, 16))


class SessionBehaviour(unittest.TestCase):
    """Поведінка клієнта поверх протоколу. Без вікна."""

    def hello(self, session, width, height):
        session.handle(proto.PKT_HELLO, hello_payload(width, height))

    def test_screen_size_comes_from_hello(self):
        # Жодного 480x272 у коді: клієнт бере розмір із пакета. Перевіряємо
        # саме на іншому пульті — інакше тест нічого не доводить.
        session = Session()
        self.hello(session, 320, 240)
        self.assertEqual((session.screen.w, session.screen.h), (320, 240))
        self.assertEqual(len(session.screen.buf), 320 * 240 * 3)

        self.hello(session, 800, 480)
        self.assertEqual((session.screen.w, session.screen.h), (800, 480))

    def test_hello_does_not_wipe_the_picture(self):
        # Пульт відповідає на кожен PING пакетом HELLO, тобто раз на дві
        # секунди. Якби клієнт перестворював кадр — екран блимав би.
        session = Session()
        self.hello(session, 64, 32)
        session.handle(
            proto.PKT_TILE,
            tile_payload(0, 0, 2, 2, proto.TILE_METHOD_RLE16, rle_pairs([(4, 0xFFFF)])),
        )
        before = bytes(session.screen.buf)
        self.hello(session, 64, 32)
        self.assertEqual(bytes(session.screen.buf), before)

    def test_unknown_packet_types_are_ignored(self):
        # Правило сумісності docs/03-protocol.md. Перевіряємо не «не впало», а
        # що розбір триває далі й кадр збирається правильно.
        shown = []
        session = Session(on_frame=lambda s: shown.append(bytes(s.buf)))
        self.hello(session, 4, 1)
        session.handle(0x7F, "майбутній пакет".encode())
        session.handle(0xF0, b"")
        session.handle(
            proto.PKT_TILE,
            tile_payload(0, 0, 4, 1, proto.TILE_METHOD_RLE16, rle_pairs([(4, 0xF800)])),
        )
        session.handle(0x42, b"\x00" * 100)
        session.handle(proto.PKT_FRAME_END, b"")

        self.assertEqual(session.unknown, 3)
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown[0], b"\xFF\x00\x00" * 4)
        self.assertEqual(session.bad_tiles, 0)

    def test_frame_shown_only_on_frame_end(self):
        shown = []
        session = Session(on_frame=lambda s: shown.append(bytes(s.buf)))
        self.hello(session, 2, 1)
        session.handle(
            proto.PKT_TILE,
            tile_payload(0, 0, 2, 1, proto.TILE_METHOD_RAW, struct.pack("<2H", 0xFFFF, 0)),
        )
        self.assertEqual(shown, [])  # плитка є, кадру ще немає
        session.handle(proto.PKT_FRAME_END, b"")
        self.assertEqual(len(shown), 1)
        self.assertEqual(session.last_frame_tiles, 1)

    def test_stale_tiles_are_shown_without_frame_end(self):
        # Запобіжник проти реальної поведінки прошивки: на нерухомому екрані
        # REFRESH дає плитки й жодного FRAME_END. Показуємо, але лічимо.
        import test_client

        shown = []
        session = Session(on_frame=lambda s: shown.append(bytes(s.buf)))
        self.hello(session, 4, 1)
        session.handle(
            proto.PKT_TILE,
            tile_payload(0, 0, 4, 1, proto.TILE_METHOD_RLE16, rle_pairs([(4, 0x001F)])),
        )
        now = session.last_tile_at

        session.flush_stale(now + test_client.FORCE_SHOW_S / 2)
        self.assertEqual(shown, [])  # ще рано: кадр може бути недомальований

        session.flush_stale(now + test_client.FORCE_SHOW_S + 0.01)
        self.assertEqual(len(shown), 1)
        self.assertEqual(session.forced, 1)
        self.assertEqual(session.frames, 0)  # це не справжній кадр

        # Показувати нема чого — вдруге не спрацьовує.
        session.flush_stale(now + 10)
        self.assertEqual(len(shown), 1)

    def test_bad_tiles_do_not_break_the_frame(self):
        session = Session()
        self.hello(session, 8, 8)
        good = bytes(session.screen.buf)

        # Плитка за межами екрана.
        session.handle(
            proto.PKT_TILE,
            tile_payload(6, 6, 4, 4, proto.TILE_METHOD_RLE16, rle_pairs([(16, 0xFFFF)])),
        )
        # Плитка, що бреше про свій розмір.
        session.handle(
            proto.PKT_TILE,
            tile_payload(0, 0, 4, 4, proto.TILE_METHOD_RLE16, rle_pairs([(4, 0xFFFF)])),
        )
        # Обрізаний заголовок.
        session.handle(proto.PKT_TILE, b"\x00\x00\x00")

        self.assertEqual(session.bad_tiles, 2)
        self.assertEqual(bytes(session.screen.buf), good)

    def test_tiles_before_hello_are_dropped(self):
        session = Session()
        session.handle(
            proto.PKT_TILE,
            tile_payload(0, 0, 2, 1, proto.TILE_METHOD_RLE16, rle_pairs([(2, 0xFFFF)])),
        )
        self.assertIsNone(session.screen)

    def test_tile_lands_at_its_coordinates(self):
        session = Session()
        self.hello(session, 4, 2)
        session.handle(
            proto.PKT_TILE,
            tile_payload(2, 1, 2, 1, proto.TILE_METHOD_RLE16, rle_pairs([(2, 0x07E0)])),
        )
        row0 = bytes(session.screen.buf[0:12])
        row1 = bytes(session.screen.buf[12:24])
        self.assertEqual(row0, b"\x00" * 12)
        self.assertEqual(row1, b"\x00" * 6 + b"\x00\xFF\x00" * 2)


class FakeTransport:
    """Труба з наперед записаним потоком. Дає перевірити порядок вітання."""

    name = "fake"

    def __init__(self, script):
        self.script = list(script)
        self.sent = []

    def recv(self):
        return self.script.pop(0) if self.script else None

    def send(self, data):
        self.sent.append(data)

    def close(self):
        pass


class Handshake(unittest.TestCase):
    """Хто починає розмову. На UART це не дрібниця, а різниця між картинкою і
    порожнім вікном: пульт там не знає, що клієнт з'явився."""

    def test_ping_first_then_refresh_after_hello(self):
        tile = tile_payload(0, 0, 2, 1, proto.TILE_METHOD_RLE16, rle_pairs([(2, 0xF800)]))
        script = [
            b"",  # тиша: клієнт уже мав привітатись
            proto.encode_frame(proto.PKT_HELLO, hello_payload(2, 1)),
            proto.encode_frame(proto.PKT_TILE, tile)
            + proto.encode_frame(proto.PKT_FRAME_END),
            None,  # труба закрилась — _serve виходить
        ]
        transport = FakeTransport(script)
        shown = []
        link = Link(lambda: transport, lambda s: shown.append(bytes(s.buf)))
        link._serve(transport)

        self.assertEqual(transport.sent[0], proto.encode_frame(proto.PKT_PING))
        self.assertEqual(transport.sent[1], proto.encode_frame(proto.PKT_REFRESH))
        self.assertEqual(len(shown), 1)
        self.assertEqual(link.session.frames, 1)
        self.assertEqual(link.session.tiles, 1)

    def test_no_refresh_until_hello(self):
        # Пульт мовчить: REFRESH слати нема кому — плитки все одно нікуди класти.
        transport = FakeTransport([b"", b"", None])
        link = Link(lambda: transport, lambda s: None)
        link._serve(transport)
        self.assertEqual(transport.sent, [proto.encode_frame(proto.PKT_PING)])


class Transports(unittest.TestCase):
    """Дві труби за одним інтерфейсом. Перевіряємо саме інтерфейс."""

    def test_tcp_transport(self):
        import socket
        import threading

        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        received = []

        def serve():
            conn, _ = server.accept()
            received.append(conn.recv(64))
            conn.sendall(proto.encode_frame(proto.PKT_FRAME_END))
            conn.close()

        thread = threading.Thread(target=serve)
        thread.start()

        transport = proto.TcpTransport("127.0.0.1", port)
        transport.send(proto.encode_frame(proto.PKT_PING))
        data = b""
        for _ in range(40):
            chunk = transport.recv()
            if chunk is None:
                break
            data += chunk
            if data:
                break
        transport.close()
        thread.join()
        server.close()

        self.assertEqual(received[0], proto.encode_frame(proto.PKT_PING))
        self.assertEqual(list(proto.Decoder().feed(data)), [(proto.PKT_FRAME_END, b"")])

    def test_serial_transport_on_loopback(self):
        # `loop://` — заглушка самого pyserial: те, що записали, читається назад.
        # Заліза для перевірки шляху не потрібно.
        try:
            transport = proto.SerialTransport("loop://")
        except RuntimeError as exc:
            self.skipTest(str(exc))
        transport.send(proto.encode_frame(proto.PKT_REFRESH))
        data = b""
        for _ in range(40):
            data += transport.recv() or b""
            if data:
                break
        transport.close()
        self.assertEqual(list(proto.Decoder().feed(data)), [(proto.PKT_REFRESH, b"")])

    def test_connector_picks_transport_by_flags(self):
        import argparse

        parser = argparse.ArgumentParser()
        proto.add_transport_args(parser)

        where, _ = proto.make_connector(parser.parse_args([]))
        self.assertEqual(where, "tcp 127.0.0.1:7616")

        where, _ = proto.make_connector(parser.parse_args(["--tcp", "10.0.0.5:9000"]))
        self.assertEqual(where, "tcp 10.0.0.5:9000")

        where, _ = proto.make_connector(parser.parse_args(["--serial", "/dev/ttyUSB0"]))
        self.assertEqual(where, "serial /dev/ttyUSB0 @ 921600")


class InputPackets(unittest.TestCase):
    """Байти пакетів вводу. Розкладка полів — з docs/03-protocol.md."""

    def decode_one(self, frame):
        packets = list(proto.Decoder().feed(frame))
        self.assertEqual(len(packets), 1)
        return packets[0]

    def test_key_packet(self):
        ptype, payload = self.decode_one(proto.encode_key(13, True))
        self.assertEqual(ptype, proto.PKT_KEY)
        self.assertEqual(payload, bytes([13, 1]))

        _, payload = self.decode_one(proto.encode_key(13, False))
        self.assertEqual(payload, bytes([13, 0]))

    def test_encoder_packet_is_signed(self):
        ptype, payload = self.decode_one(proto.encode_enc(-1))
        self.assertEqual(ptype, proto.PKT_ENC)
        self.assertEqual(payload, b"\xFF")  # int8, -1

        _, payload = self.decode_one(proto.encode_enc(1))
        self.assertEqual(payload, b"\x01")

    def test_touch_packet(self):
        ptype, payload = self.decode_one(proto.encode_touch(proto.TOUCH_DOWN, 480, 271))
        self.assertEqual(ptype, proto.PKT_TOUCH)
        self.assertEqual(payload, struct.pack("<BHH", 0, 480, 271))

    def test_trim_packet(self):
        ptype, payload = self.decode_one(proto.encode_trim(5, True))
        self.assertEqual(ptype, proto.PKT_TRIM)
        self.assertEqual(payload, bytes([5, 1]))

    def test_input_state_packet(self):
        ptype, payload = self.decode_one(
            proto.encode_input_state(0xDEADBEEF, 0x0000FFFF, True, 479, 271)
        )
        self.assertEqual(ptype, proto.PKT_INPUT_STATE)
        self.assertEqual(len(payload), 13)
        self.assertEqual(payload, struct.pack("<IIBHH", 0xDEADBEEF, 0x0000FFFF, 1, 479, 271))

    def test_input_state_flag_is_bit0_only(self):
        # Решта бітів прапорцевого байта зарезервована й має йти нулями.
        _, payload = self.decode_one(proto.encode_input_state(0, 0, False, 0, 0))
        self.assertEqual(payload[8], 0)
        _, payload = self.decode_one(proto.encode_input_state(0, 0, True, 0, 0))
        self.assertEqual(payload[8], 1)


class InputMirrorState(unittest.TestCase):
    """Дзеркало вводу клієнта — джерело рівня для INPUT_STATE."""

    def unpack(self, mirror):
        packets = list(proto.Decoder().feed(mirror.encode()))
        keys, trims, flags, x, y = struct.unpack("<IIBHH", packets[0][1])
        return keys, trims, bool(flags & 1), x, y

    def test_keys_and_trims_are_levels(self):
        m = proto.InputMirror()
        m.key(4, True)
        m.key(7, True)
        m.trim(1, True)
        keys, trims, _, _, _ = self.unpack(m)
        self.assertEqual(keys, (1 << 4) | (1 << 7))
        self.assertEqual(trims, 1 << 1)

        m.key(4, False)
        keys, _, _, _, _ = self.unpack(m)
        self.assertEqual(keys, 1 << 7)

    def test_key_out_of_mask_is_dropped(self):
        # Пульт клавіші >= 32 теж відкидає — дзеркало не має розходитись із ним.
        m = proto.InputMirror()
        m.key(32, True)
        self.assertEqual(self.unpack(m)[0], 0)

    def test_touch_level_follows_down_and_up(self):
        m = proto.InputMirror()
        m.touch(proto.TOUCH_DOWN, 10, 20)
        _, _, down, x, y = self.unpack(m)
        self.assertTrue(down)
        self.assertEqual((x, y), (10, 20))

        m.touch(proto.TOUCH_UP, 10, 20)
        self.assertFalse(self.unpack(m)[2])

    def test_move_shifts_the_point_but_not_the_level(self):
        m = proto.InputMirror()
        m.touch(proto.TOUCH_DOWN, 10, 20)
        m.touch(proto.TOUCH_MOVE, 30, 40)
        _, _, down, x, y = self.unpack(m)
        self.assertTrue(down)
        self.assertEqual((x, y), (30, 40))


class LostPacketProbe(unittest.TestCase):
    """Механіка проби на точкову втрату (`tools/input_check.py`).

    Інструмент доказу сам має бути доведений: якщо `lose=True` насправді шле
    пакет або, навпаки, не оновлює дзеркало, проба показуватиме що завгодно,
    тільки не те, що написано у звіті.
    """

    def make(self, **kw):
        import input_check

        radio = input_check.Radio.__new__(input_check.Radio)
        radio.mirror = proto.InputMirror()
        radio.wire = []
        radio.send = radio.wire.append
        radio.input_state = kw.get("input_state", True)
        radio.last_ping = radio.last_state = 0.0
        return radio

    def packets(self, radio):
        out = []
        for frame in radio.wire:
            out.extend(proto.Decoder().feed(frame))
        return out

    def test_lost_release_never_reaches_the_wire(self):
        radio = self.make()
        radio.key(4, True)
        radio.key(4, False, lose=True)

        # На дроті лише «натиснуто» — «відпущено» зникло, як і задумано.
        self.assertEqual(self.packets(radio), [(proto.PKT_KEY, bytes([4, 1]))])

    def test_lost_release_still_clears_the_mirror(self):
        # Клієнт **вважає**, що відпустив. Саме тому наступний INPUT_STATE
        # лікує залипання: він каже правду про намір клієнта.
        radio = self.make()
        radio.key(4, True)
        self.assertEqual(radio.mirror.keys, 1 << 4)

        radio.key(4, False, lose=True)
        self.assertEqual(radio.mirror.keys, 0)

    def test_lost_touch_release_clears_the_level(self):
        radio = self.make()
        radio.touch(proto.TOUCH_DOWN, 10, 20)
        radio.touch(proto.TOUCH_UP, 10, 20, lose=True)

        self.assertEqual(self.packets(radio),
                         [(proto.PKT_TOUCH, struct.pack("<BHH", proto.TOUCH_DOWN, 10, 20))])
        self.assertFalse(radio.mirror.touch_down)

    def test_keepalive_sends_state_when_enabled(self):
        radio = self.make(input_state=True)
        radio.key(4, True)
        radio.wire.clear()
        radio.keepalive_if_due()

        types = [p[0] for p in self.packets(radio)]
        self.assertIn(proto.PKT_INPUT_STATE, types)

    def test_keepalive_without_state_still_proves_the_client_alive(self):
        # ⚠️ Режим «як було до 0008» не сміє просто мовчати: прошивка відпускає
        # ввід за тишею, і залипання, яке проба показує, сховалось би за
        # тайм-аутом. Тому йде пакет, що несе ввід, але рівня не повторює.
        radio = self.make(input_state=False)
        radio.wire.clear()
        radio.keepalive_if_due()

        types = [p[0] for p in self.packets(radio)]
        self.assertNotIn(proto.PKT_INPUT_STATE, types)
        self.assertIn(proto.PKT_ENC, types)


class FakeRoot:
    """Заміна tk.Tk для тестів вводу: тільки відкладені виклики."""

    def __init__(self):
        self.jobs = {}
        self.seq = 0

    def after(self, _ms, fn):
        self.seq += 1
        self.jobs[self.seq] = fn
        return self.seq

    def after_cancel(self, job):
        self.jobs.pop(job, None)

    def run_pending(self):
        """Наче минув час: виконує все відкладене."""
        for job in sorted(self.jobs):
            self.jobs.pop(job)()


class FakeEvent:
    def __init__(self, keysym="", num=0, delta=0):
        self.keysym = keysym
        self.num = num
        self.delta = delta


class FakeLink:
    """Заміна Link. Повторює його обгортки разом із дзеркалом вводу.

    Дзеркало тут не декорація: рівень, що розійшовся з надісланими переходами,
    — це рівно та вада, яку INPUT_STATE має лікувати, а не породжувати. Тому
    фальшива труба тримає його так само, як справжня, і тести можуть на нього
    дивитись.
    """

    def __init__(self):
        self.sent = {"key": 0, "enc": 0, "touch": 0, "state": 0}
        self.frames = []
        self.mirror = proto.InputMirror()

    def send(self, frame, kind):
        self.frames.append((kind, frame))
        self.sent[kind] += 1

    def send_key(self, code, pressed):
        self.mirror.key(code, pressed)
        self.send(proto.encode_key(code, pressed), "key")

    def send_touch(self, kind, x, y):
        self.mirror.touch(kind, x, y)
        self.send(proto.encode_touch(kind, x, y), "touch")

    def send_enc(self, steps):
        self.send(proto.encode_enc(steps), "enc")

    def state(self):
        """Рівень, який пішов би наступним INPUT_STATE."""
        packets = list(proto.Decoder().feed(self.mirror.encode()))
        keys, trims, flags, x, y = struct.unpack("<IIBHH", packets[0][1])
        return keys, trims, bool(flags & 1), x, y

    def packets(self):
        out = []
        for _kind, frame in self.frames:
            out.extend(proto.Decoder().feed(frame))
        return out


class InputLayout(unittest.TestCase):
    """Розкладка будується з HELLO, а не зі списку в коді."""

    def make(self, keys, flags=0x03):
        root, link = FakeRoot(), FakeLink()
        inp = Input(root, link)
        inp.sync(proto.parse_hello(hello_payload(480, 272, keys=keys, flags=flags)))
        return root, link, inp

    def test_codes_come_from_hello(self):
        # Ті самі мітки, що віддає TX16S, але з навмисно іншими кодами: клієнт
        # має взяти саме те, що назвав пульт.
        _root, link, inp = self.make([(41, "RTN"), (42, "Enter"), (43, "SYS")])

        inp.on_key_press(FakeEvent(keysym="Escape"))
        inp.on_key_press(FakeEvent(keysym="Return"))
        inp.on_key_press(FakeEvent(keysym="s"))

        self.assertEqual(
            [p for p in link.packets()],
            [(proto.PKT_KEY, bytes([41, 1])),
             (proto.PKT_KEY, bytes([42, 1])),
             (proto.PKT_KEY, bytes([43, 1]))],
        )

    def test_unknown_label_gets_spare_key(self):
        _root, link, inp = self.make([(9, "WEIRD")])
        inp.on_key_press(FakeEvent(keysym="F1"))
        self.assertEqual(link.packets(), [(proto.PKT_KEY, bytes([9, 1]))])

    def test_press_and_release(self):
        root, link, inp = self.make([(1, "RTN")])

        inp.on_key_press(FakeEvent(keysym="Escape"))
        inp.on_key_release(FakeEvent(keysym="Escape"))
        root.run_pending()  # затримка на автоповтор минула

        self.assertEqual(
            link.packets(),
            [(proto.PKT_KEY, bytes([1, 1])), (proto.PKT_KEY, bytes([1, 0]))],
        )

    # X11 на утримуваній клавіші шле пари «відпущено-натиснуто». Пульт має
    # бачити одне довге натискання, інакше довгих натискань не буде взагалі.
    def test_autorepeat_does_not_release_the_key(self):
        root, link, inp = self.make([(1, "RTN")])

        inp.on_key_press(FakeEvent(keysym="Escape"))
        for _ in range(5):  # автоповтор
            inp.on_key_release(FakeEvent(keysym="Escape"))
            inp.on_key_press(FakeEvent(keysym="Escape"))
        self.assertEqual(link.packets(), [(proto.PKT_KEY, bytes([1, 1]))])

        # І лише справжнє відпускання доходить до пульта.
        inp.on_key_release(FakeEvent(keysym="Escape"))
        root.run_pending()
        self.assertEqual(
            link.packets(),
            [(proto.PKT_KEY, bytes([1, 1])), (proto.PKT_KEY, bytes([1, 0]))],
        )

    # Втрата фокуса й вихід не мають лишати нічого натиснутим. Прошивка
    # відпустила б сама за тишею, але це остання перешкода, а не спосіб роботи.
    def test_release_all_lets_go_of_everything(self):
        root, link, inp = self.make([(1, "RTN"), (2, "Enter")])

        inp.on_key_press(FakeEvent(keysym="Escape"))
        inp.on_key_press(FakeEvent(keysym="Return"))
        inp.touch(proto.TOUCH_DOWN, (10, 20))
        link.frames.clear()

        inp.release_all()
        root.run_pending()

        got = link.packets()
        self.assertIn((proto.PKT_KEY, bytes([1, 0])), got)
        self.assertIn((proto.PKT_KEY, bytes([2, 0])), got)
        self.assertIn((proto.PKT_TOUCH, struct.pack("<BHH", proto.TOUCH_UP, 10, 20)), got)

    # --- Рівень не має розходитись із переходами -------------------------
    #
    # INPUT_STATE лікує втрачений пакет тільки доти, доки сам каже правду.
    # Дзеркало, що розійшлося з надісланими переходами, перетворює лікування на
    # джерело хвороби: воно періодично відтискатиме те, що людина тримає.

    def test_level_follows_press_and_release(self):
        root, link, inp = self.make([(1, "RTN"), (2, "Enter")])

        inp.on_key_press(FakeEvent(keysym="Escape"))
        self.assertEqual(link.state()[0], 1 << 1)

        inp.on_key_press(FakeEvent(keysym="Return"))
        self.assertEqual(link.state()[0], (1 << 1) | (1 << 2))

        inp.on_key_release(FakeEvent(keysym="Escape"))
        root.run_pending()
        self.assertEqual(link.state()[0], 1 << 2)

    def test_autorepeat_does_not_dent_the_level(self):
        # Пари «відпущено-натиснуто» від X11 пульту не йдуть — і рівень вони
        # теж не сміють просідати, інакше повтор стану відпустив би клавішу
        # під пальцем.
        root, link, inp = self.make([(1, "RTN")])

        inp.on_key_press(FakeEvent(keysym="Escape"))
        for _ in range(5):
            inp.on_key_release(FakeEvent(keysym="Escape"))
            inp.on_key_press(FakeEvent(keysym="Escape"))
            self.assertEqual(link.state()[0], 1 << 1)

    def test_level_empty_after_release_all(self):
        root, link, inp = self.make([(1, "RTN"), (2, "Enter")])

        inp.on_key_press(FakeEvent(keysym="Escape"))
        inp.on_key_press(FakeEvent(keysym="Return"))
        inp.touch(proto.TOUCH_DOWN, (10, 20))
        self.assertEqual(link.state()[0], (1 << 1) | (1 << 2))
        self.assertTrue(link.state()[2])

        inp.release_all()
        root.run_pending()

        keys, trims, down, _, _ = link.state()
        self.assertEqual((keys, trims, down), (0, 0, False))

    def test_level_tracks_drag_point(self):
        root, link, inp = self.make([(1, "RTN")])

        inp.touch(proto.TOUCH_DOWN, (10, 20))
        inp.touch(proto.TOUCH_MOVE, (30, 40))
        _, _, down, x, y = link.state()
        self.assertTrue(down)
        self.assertEqual((x, y), (30, 40))

        inp.touch(proto.TOUCH_UP, (30, 40))
        self.assertFalse(link.state()[2])

    def test_arrows_drive_encoder_when_radio_has_no_up_down(self):
        _root, link, inp = self.make([(1, "RTN")])

        inp.on_key_press(FakeEvent(keysym="Down"))
        inp.on_key_press(FakeEvent(keysym="Up"))
        self.assertEqual(
            link.packets(),
            [(proto.PKT_ENC, b"\x01"), (proto.PKT_ENC, b"\xFF")],
        )

    def test_arrows_stay_keys_when_radio_has_them(self):
        _root, link, inp = self.make([(5, "UP"), (6, "DOWN")])

        inp.on_key_press(FakeEvent(keysym="Down"))
        self.assertEqual(link.packets(), [(proto.PKT_KEY, bytes([6, 1]))])

    def test_wheel_turns_the_encoder(self):
        _root, link, inp = self.make([(1, "RTN")])

        inp.on_wheel(FakeEvent(num=5))
        inp.on_wheel(FakeEvent(num=4))
        self.assertEqual(
            link.packets(),
            [(proto.PKT_ENC, b"\x01"), (proto.PKT_ENC, b"\xFF")],
        )

    def test_no_touch_no_touch_packets(self):
        # Пульт без сенсора: миша не має слати нічого.
        _root, link, inp = self.make([(1, "RTN")], flags=0x02)
        inp.touch(proto.TOUCH_DOWN, (10, 20))
        inp.touch(proto.TOUCH_UP, (10, 20))
        self.assertEqual(link.packets(), [])

    def test_move_without_press_is_not_sent(self):
        _root, link, inp = self.make([(1, "RTN")])
        inp.touch(proto.TOUCH_MOVE, (10, 20))
        self.assertEqual(link.packets(), [])

    def test_drag_sends_only_real_movement(self):
        _root, link, inp = self.make([(1, "RTN")])

        inp.touch(proto.TOUCH_DOWN, (10, 20))
        inp.touch(proto.TOUCH_MOVE, (10, 20))  # на місці — пульту байдуже
        inp.touch(proto.TOUCH_MOVE, (11, 21))
        inp.touch(proto.TOUCH_UP, (11, 21))

        self.assertEqual(
            link.packets(),
            [
                (proto.PKT_TOUCH, struct.pack("<BHH", proto.TOUCH_DOWN, 10, 20)),
                (proto.PKT_TOUCH, struct.pack("<BHH", proto.TOUCH_MOVE, 11, 21)),
                (proto.PKT_TOUCH, struct.pack("<BHH", proto.TOUCH_UP, 11, 21)),
            ],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
