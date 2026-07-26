#!/usr/bin/env python3
"""Керування пультом за сценарієм: натиснути, дочекатись кадру, зняти PNG.

Навіщо окремий інструмент, коли є вікно (`tools/test_client.py`). Вікно
доводить, що керувати **можна**, але доказом у задачі є пройдений сценарій, а
сценарій, пройдений руками, неможливо ані повторити, ані показати. Тут той
самий протокол, але дії описані рядком, кожен крок вимірюється, а результат
лягає у PNG.

Заразом це наскрізна перевірка безпеки, і рубежів тут три, різних за природою:

    drop        обрив з'єднання, нічого не відпустивши;
    silence     труба ціла, але клієнт замовк — ловить тайм-аут;
    keylost     ⚠️ труба ціла, клієнт живий і шле далі, зникає рівно **один**
                пакет «відпущено». Тайм-аут такого не ловить за побудовою —
                ловить лише періодичний повтор рівня (INPUT_STATE, задача 0008).

    tools/input_check.py --steps "shot:00-start,key:MDL,shot:01-model"
    tools/input_check.py --steps "hold:PAGE>:1500,shot:02-repeat"
    tools/input_check.py --steps "press:ENTER,drop,wait:2000,shot:03-after-drop"

Проба на точкову втрату — той самий сценарій двічі, тим самим файлом:

    tools/input_check.py --no-input-state --steps "keylost:PAGE>:300:2000,shot:a"
    tools/input_check.py                  --steps "keylost:PAGE>:300:2000,shot:b"

Кроки (через кому, зліва направо):

    key:МІТКА          коротке натискання (натиснув і відпустив)
    press:МІТКА        натиснути й не відпускати
    release:МІТКА      відпустити
    hold:МІТКА:МС      натиснути, потримати МС, відпустити
    keylost:МІТКА:УТР:СТЕЖ   натиснути, потримати УТР мс, «відпустити» так, що
                       пакет губиться, і СТЕЖ мс дивитись, чи пульт замовк
    trimlost:І:УТР:СТЕЖ      те саме для тримера — ⚠️ саме він придатний як
                       доказ: у нього автоповтор видно числом на екрані,
                       а клавіші сторінок автоповтору не мають зовсім
    enc:±N             N клацань енкодера
    tap:X/Y            дотик: натиск і відпускання в точці
    taplost:X/Y:УТР:СТЕЖ     те саме, що keylost, але для дотику
    swipe:X1/Y1/X2/Y2  дотик із протягуванням
    wait:МС            просто зачекати (періодика клієнта йде далі)
    shot:НАЗВА         зберегти поточний кадр у PNG
    silence:МС         замовкнути при цілій трубі
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
    INPUT_STATE_PERIOD_S,
    InputMirror,
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

    def __init__(self, host: str, port: int, quiet: float = 0.25,
                 input_state: bool = True):
        self.host = host
        self.port = port
        # Скільки тиші означає «пульт домалював». Кадри в русі йдуть один за
        # одним, тому чекати треба не першого FRAME_END, а паузи після нього.
        self.quiet = quiet

        # Чи повторювати повний стан вводу. Вимикається прапорцем, щоб «до» і
        # «після» знімались тим самим виконуваним файлом і тим самим сценарієм
        # — інакше порівняння нечесне.
        self.input_state = input_state
        self.mirror = InputMirror()

        self.sock = None
        self.dec = Decoder()
        self.hello = None
        self.frame = None
        self.frames = 0
        self.last_ping = 0.0
        self.last_state = 0.0
        self.log = []

    # --- Труба -------------------------------------------------------------

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=5.0)
        self.sock.settimeout(0.01)
        self.dec.reset()
        # Пульт при розриві відпустив усе — дзеркало має погодитись із ним, а не
        # переконувати його, що клавіша досі натиснута.
        self.mirror.clear()
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

    def keepalive_if_due(self):
        """Періодика клієнта: повний стан вводу і, значно рідше, PING."""
        now = time.monotonic()

        if now - self.last_state >= INPUT_STATE_PERIOD_S:
            if self.input_state:
                self.send(self.mirror.encode())
            else:
                # ⚠️ Режим «як було до INPUT_STATE», і він мусить лишатись
                # чесним. Просто замовкнути не можна: прошивка відпускає ввід за
                # тишею, і залипання, яке ми хочемо показати, сховалось би за
                # тайм-аутом — вийшло б, що вади немає.
                #
                # Тому шлемо порожнє клацання енкодера: за правилами прошивки
                # це пакет, що несе ввід, тобто зв'язок доведено живий, але
                # рівня він не повторює. Саме та ситуація, заради якої
                # INPUT_STATE і з'явився: жива труба, живий клієнт, загублене
                # «відпущено».
                self.send(encode_enc(0))
            self.last_state = now

        # PING лишається перевіркою живого **пульта** — у відповідь іде HELLO.
        # Ввід на ньому більше не тримається (docs/03-protocol.md).
        if now - self.last_ping >= PING_PERIOD_S:
            self.send(encode_frame(PKT_PING))
            self.last_ping = now

    # --- Ввід: дзеркало оновлюється **до** відправлення переходу ------------

    def key(self, code: int, pressed: bool, lose: bool = False):
        """`lose=True` — клієнт вважає, що надіслав, а на дріт нічого не пішло.

        Це і є точкова втрата одного пакета при живому зв'язку: не обрив, не
        тиша, а рівно один зниклий кадр — те, що тайм-аут спіймати не може.
        """
        self.mirror.key(code, pressed)
        if not lose:
            self.send(encode_key(code, pressed))

    def trim(self, index: int, pressed: bool, lose: bool = False):
        self.mirror.trim(index, pressed)
        if not lose:
            self.send(encode_trim(index, pressed))

    def touch(self, event: int, x: int, y: int, lose: bool = False):
        self.mirror.touch(event, x, y)
        if not lose:
            self.send(encode_touch(event, x, y))

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
                self.keepalive_if_due()
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

    def watch(self, seconds: float):
        """Стежить, скільки пульт ще малює. Повертає (кадрів, останній кадр, мс).

        Це і є вимірювання залипання. Відпущена клавіша означає, що пульт
        замовк майже одразу; залипла — що автоповтор EdgeTX жене далі й кадри
        не припиняються до кінця вікна спостереження.
        """
        started = time.monotonic()
        frames_before = self.frames
        last_frame_at = started
        while time.monotonic() - started < seconds:
            before = self.frames
            self.pump(0.01)
            if self.frames != before:
                last_frame_at = time.monotonic()
        return self.frames - frames_before, (last_frame_at - started) * 1000

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
            ms = radio.react(lambda c=code: (radio.key(c, True),
                                             radio.key(c, False)))
            report.append(f"  {arg:<8} натиснуто й відпущено, реакція {ms:5.1f} мс")

        elif name == "press":
            code = radio.key_code(arg)
            ms = radio.react(lambda c=code: radio.key(c, True))
            report.append(f"  {arg:<8} натиснуто (тримаю), реакція {ms:5.1f} мс")

        elif name == "release":
            code = radio.key_code(arg)
            radio.key(code, False)
            radio.settle()
            report.append(f"  {arg:<8} відпущено")

        elif name == "hold":
            label, _, ms_text = arg.partition(":")
            code = radio.key_code(label)
            hold_ms = float(ms_text or 1000)
            frames_before = radio.frames
            radio.key(code, True)
            radio.pump(hold_ms / 1000.0)
            radio.key(code, False)
            radio.settle()
            report.append(
                f"  {label:<8} утримано {hold_ms:.0f} мс, кадрів за цей час "
                f"{radio.frames - frames_before}"
            )

        elif name == "trimpress":
            # Натиснути й **не** відпускати. Для перевірки безпеки: далі йде
            # `drop`, і пульт має відпустити тример сам.
            radio.trim(int(arg), True)
            radio.settle()
            report.append(f"  тример {arg} натиснуто (тримаю)")

        elif name == "trim":
            # Тример — єдине місце на TX16S, де автоповтор видно очима: поки
            # напрямок тримають, EdgeTX сам рухає значення далі й далі.
            index_text, _, ms_text = arg.partition(":")
            index = int(index_text)
            hold_ms = float(ms_text or 800)
            frames_before = radio.frames
            radio.trim(index, True)
            radio.pump(hold_ms / 1000.0)
            radio.trim(index, False)
            radio.settle()
            report.append(
                f"  тример {index} утримано {hold_ms:.0f} мс, кадрів "
                f"{radio.frames - frames_before}"
            )

        elif name == "enc":
            # `enc:±N` — по одному клацанню, чекаючи перемальовки між ними.
            # `enc:±N:ГАП` — черга клацань через ГАП мс, без очікування. Другий
            # вид потрібен, щоб перевірити **прискорення**: EdgeTX рахує його з
            # проміжку між клацаннями, тож повільне й швидке обертання мають
            # давати різний крок. Однакова відповідь на обидва темпи — це вада,
            # яку й лікує частина 2 задачі 0008.
            steps_text, _, gap_text = arg.partition(":")
            steps_n = int(steps_text)
            direction = 1 if steps_n > 0 else -1

            if gap_text:
                gap_s = float(gap_text) / 1000.0
                frames_before = radio.frames
                for _ in range(abs(steps_n)):
                    radio.send(encode_enc(direction))
                    radio.pump(gap_s)
                radio.settle()
                report.append(
                    f"  енкодер  {steps_n:+d} чергою через {gap_text} мс, "
                    f"кадрів {radio.frames - frames_before}"
                )
            else:
                ms = radio.react(lambda: radio.send(encode_enc(direction)))
                for _ in range(abs(steps_n) - 1):
                    radio.send(encode_enc(direction))
                    radio.settle()
                report.append(f"  енкодер  {steps_n:+d}, реакція {ms:5.1f} мс")

        elif name == "tap":
            x, y = (int(v) for v in arg.split("/"))
            ms = radio.react(lambda: (radio.touch(TOUCH_DOWN, x, y),
                                      radio.touch(TOUCH_UP, x, y)))
            report.append(f"  дотик    {x},{y}, реакція {ms:5.1f} мс")

        elif name == "swipe":
            x1, y1, x2, y2 = (int(v) for v in arg.split("/"))
            radio.touch(TOUCH_DOWN, x1, y1)
            for i in range(1, 9):
                radio.touch(TOUCH_MOVE,
                            x1 + (x2 - x1) * i // 8,
                            y1 + (y2 - y1) * i // 8)
                radio.pump(0.02)
            radio.touch(TOUCH_UP, x2, y2)
            radio.settle()
            report.append(f"  протяг   {x1},{y1} -> {x2},{y2}")

        # --- Проба на точкову втрату одного пакета -------------------------
        #
        # Головний доказ задачі 0008. Не обрив і не тиша: труба ціла, клієнт
        # живий і шле далі, зникає рівно один кадр — «відпущено». Тайм-аут
        # такого не ловить за побудовою, ловить лише повтор рівня.

        elif name == "keylost":
            label, _, rest = arg.partition(":")
            hold_text, _, watch_text = rest.partition(":")
            code = radio.key_code(label)
            hold_ms = float(hold_text or 300)
            watch_ms = float(watch_text or 2000)

            radio.key(code, True)
            radio.pump(hold_ms / 1000.0)
            radio.key(code, False, lose=True)  # клієнт вважає, що відпустив
            frames, last_ms = radio.watch(watch_ms / 1000.0)
            report.append(
                f"  ⚠️ {label:<8} «відпущено» ВТРАЧЕНО. За {watch_ms:.0f} мс "
                f"після втрати: кадрів {frames}, останній на {last_ms:.0f} мс"
            )

        elif name == "trimlost":
            # Тример — єдиний орган на TX16S, де залипання видно **числом**:
            # поки напрямок утримується, EdgeTX сам жене значення далі й
            # перемальовує екран. Клавіші сторінок автоповтору не мають, тому
            # на них залипання нічим себе не виявляє — і проба на них показала б
            # «нуль кадрів» однаково в обох режимах, тобто не показала б нічого.
            index_text, _, rest = arg.partition(":")
            hold_text, _, watch_text = rest.partition(":")
            index = int(index_text)
            hold_ms = float(hold_text or 400)
            watch_ms = float(watch_text or 3000)

            radio.trim(index, True)
            radio.pump(hold_ms / 1000.0)
            radio.trim(index, False, lose=True)
            frames, last_ms = radio.watch(watch_ms / 1000.0)
            report.append(
                f"  ⚠️ тример {index} «відпущено» ВТРАЧЕНО. За {watch_ms:.0f} мс "
                f"після втрати: кадрів {frames}, останній на {last_ms:.0f} мс"
            )

        elif name == "taplost":
            point, _, rest = arg.partition(":")
            hold_text, _, watch_text = rest.partition(":")
            x, y = (int(v) for v in point.split("/"))
            hold_ms = float(hold_text or 300)
            watch_ms = float(watch_text or 2000)

            radio.touch(TOUCH_DOWN, x, y)
            radio.pump(hold_ms / 1000.0)
            radio.touch(TOUCH_UP, x, y, lose=True)
            frames, last_ms = radio.watch(watch_ms / 1000.0)
            report.append(
                f"  ⚠️ дотик {x},{y} «відпущено» ВТРАЧЕНО. За {watch_ms:.0f} мс "
                f"після втрати: кадрів {frames}, останній на {last_ms:.0f} мс"
            )

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
            # ⚠️ Мовчить **усе**, включно з INPUT_STATE: інакше проба
            # перевіряла б сама себе, а не тайм-аут.
            ms = float(arg)
            started = time.monotonic()
            frames_before = radio.frames
            last_frame_at = started
            while time.monotonic() - started < ms / 1000.0:
                before = radio.frames
                radio.pump(0.01, ping=False)
                if radio.frames != before:
                    last_frame_at = time.monotonic()
            report.append(
                f"  ⚠️ мовчання {ms:.0f} мс при цілій трубі: кадрів "
                f"{radio.frames - frames_before}, останній на "
                f"{(last_frame_at - started) * 1000:.0f} мс"
            )

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
    ap.add_argument(
        "--no-input-state",
        action="store_true",
        help="не повторювати повний стан вводу — знімок поведінки «як було до 0008»",
    )
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    radio = Radio(args.host, args.port, input_state=not args.no_input_state)
    radio.connect()

    hello = radio.hello
    report = [
        f"Пульт: {hello['target']} {hello['fw']}  {hello['width']}x{hello['height']}",
        "Клавіші: " + ", ".join(f"{name}({code})" for code, name in hello["keys"]),
        f"Повтор стану вводу: {'так, раз на 250 мс' if radio.input_state else 'ВИМКНЕНО'}",
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
