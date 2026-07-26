#!/usr/bin/env python3
"""Замір відпускання вводу на **живому** пульті, незалежним каналом.

## Навіщо саме такий інструмент

Головна вимога безпеки моста — «втративши телефон, негайно відпустити ввід
на пульті» — досі доведена на програмному двійнику. Цього мало: на ESP32
інший мережевий стек, інші таймінги і справжній розрив Wi-Fi замість
закритого сокета на тій самій машині.

Ще важливіше — **чим міряти**. Лічильники самого моста тут не доказ: вони
кажуть «я надіслав обнулений `INPUT_STATE`», а питання стоїть інакше — «чи
пульт справді відпустив». Тому міряємо **збоку**, через режим USB-джойстика:
пульт віддає в ПК свої канали після мікшера, і цей канал ніяк не пов'язаний
ні з нашим протоколом, ні з Wi-Fi (`state/DECISIONS.md`, 2026-07-25).

Спосіб той самий, яким у задачі 0008 доводили залипання: **утримуємо тример**.
Поки його тримають, канал повзе; щойно ввід відпущено — канал завмирає. Різниця
між «останній рух каналу» і «моментом втрати телефона» і є те, що ми міряємо.

```
   ПК ──Wi-Fi──► міст ──UART──► пульт
    ▲                             │
    └──────── USB-джойстик ───────┘   ← незалежний канал спостереження
```

## Два способи втратити телефон, і вони різні

| Спосіб | Що імітує | Чого чекаємо |
|---|---|---|
| `close` | вкладку закрито, застосунок згорнуто | десятки мілісекунд |
| `silence` | телефон винесли за межу зв'язку | ~750 мс (сторож моста) |

⚠️ Третій спосіб — **справжній розрив Wi-Fi** — автоматизувати не можна:
хтось має вимкнути Wi-Fi на телефоні. Для нього є режим `--manual`.

## Запуск

Пульт має бути в режимі USB-джойстика і під'єднаний до ПК по USB, а ПК —
у мережі моста.

    python3 tools/release_watch.py --ws ws://192.168.4.1/ws
    python3 tools/release_watch.py --ws ws://192.168.4.1/ws --manual

Працює і проти двійника (`--ws ws://127.0.0.1:8080/ws`), і проти симулятора
(`--tcp`) — але джойстик у симулятора не з'явиться, тож там воно тільки для
перевірки самого інструмента.
"""

import argparse
import fcntl
import glob
import os
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import remote_ui_proto as proto  # noqa: E402

# Скільки тримати тример, щоб переконатися, що канал справді повзе.
HOLD_S = 1.5

# Скільки чекати після втрати телефона. Більше і за сторожа моста (750 мс),
# і за тайм-аут відпускання в прошивці (1000 мс), із запасом.
WATCH_AFTER_S = 3.0

# Рух каналу менший за це вважаємо шумом АЦП, а не тримером.
MOVE_EPS = 2


def find_joystick():
    """Знайти пульт серед джойстиків.

    Жорсткий `/dev/input/js0` — це вже пройдена пастка: у задачі 0010
    `jsinfo.py` шукав пристрій за номером, і при зміні номера помилка
    виглядала як брак прав.
    """
    found = []
    for path in sorted(glob.glob("/dev/input/js*")):
        try:
            with open(path, "rb"):
                found.append(path)
        except PermissionError:
            print(f"  {path}: немає прав (група uucp — див. docs/10-arch-linux.md)")
        except OSError:
            pass
    return found


class AxisWatcher:
    """Стежить за осями джойстика і запам'ятовує, коли був останній рух."""

    def __init__(self, path):
        self.f = open(path, "rb")
        fcntl.fcntl(self.f, fcntl.F_SETFL, os.O_NONBLOCK)
        self.lock = threading.Lock()
        self.last = {}          # вісь → останнє значення
        self.last_move_t = None  # коли востаннє щось рухалось
        self.moves = 0
        self.moved_axes = set()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            try:
                rec = self.f.read(8)
            except BlockingIOError:
                time.sleep(0.002)
                continue
            except OSError:
                break
            if not rec or len(rec) < 8:
                time.sleep(0.002)
                continue

            _, val, typ, num = struct.unpack("IhBB", rec)
            if not (typ & 0x02):
                continue   # кнопки нас тут не цікавлять

            now = time.monotonic()
            with self.lock:
                prev = self.last.get(num)
                self.last[num] = val
                # Перша подія по осі — це початкове значення, а не рух.
                if prev is not None and abs(val - prev) >= MOVE_EPS:
                    self.last_move_t = now
                    self.moves += 1
                    self.moved_axes.add(num)

    def mark(self):
        """Забути попередню історію руху."""
        with self.lock:
            self.last_move_t = None
            self.moves = 0
            self.moved_axes = set()

    def snapshot(self):
        with self.lock:
            return self.last_move_t, self.moves, set(self.moved_axes)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=1)
        self.f.close()


class Client:
    """Клієнт, який поводиться рівно як браузер: рівень раз на 250 мс."""

    def __init__(self, connect):
        self.t = connect()
        self.dec = proto.Decoder()
        self.mirror = proto.InputMirror()
        self.hello = None
        self.stop = threading.Event()
        self.silent = threading.Event()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        while not self.stop.is_set():
            data = self.t.recv()
            if data is None:
                break
            for ptype, payload in self.dec.feed(data):
                if ptype == proto.PKT_HELLO and self.hello is None:
                    self.hello = proto.parse_hello(payload)

    def greet(self, timeout=5.0, retry=0.5):
        """⚠️ `PING` повторюється, а не шлеться один раз.

        Пульт мовчить у порожній канал, доки не почує клієнта: привітальний
        пакет — єдина подія, яка запускає розмову. Загубився (пульт ще
        вантажиться, завада на дроті) — і чекати можна вічно, а виглядатиме
        це як несправний міст.
        """
        end = time.monotonic() + timeout
        next_ping = 0.0
        while self.hello is None and time.monotonic() < end:
            now = time.monotonic()
            if now >= next_ping:
                self.send(proto.encode_frame(proto.PKT_PING))
                next_ping = now + retry
            time.sleep(0.02)
        return self.hello

    def send(self, frame):
        if not self.silent.is_set():
            try:
                self.t.send(frame)
            except OSError:
                pass

    def start_hold(self):
        """Повтор рівня — те, на чому й тримається ввід."""
        def loop():
            while not self.stop.is_set():
                frame, _ = proto.hold_packet(self.mirror, bool(self.hello
                                                               and self.hello["has_input_state"]))
                if frame:
                    self.send(frame)
                time.sleep(proto.INPUT_STATE_PERIOD_S)
        threading.Thread(target=loop, daemon=True).start()

    def trim(self, index, pressed):
        self.mirror.trim(index, pressed)
        self.send(proto.encode_trim(index, pressed))

    def close(self):
        self.stop.set()
        self.t.close()


def find_live_trim(client, watcher, count):
    """Підібрати тример, який справді рухає якийсь канал.

    Не вгадуємо: у моделі може не бути мікшера на потрібний канал, і тоді
    тример нічого не зрушить — а виглядало б це як «ввід не доходить».
    """
    for index in range(max(1, count)):
        watcher.mark()
        client.trim(index, True)
        time.sleep(0.8)
        _, moves, axes = watcher.snapshot()
        client.trim(index, False)
        time.sleep(0.4)
        if moves > 0:
            return index, axes
        print(f"  тример {index}: канал не ворухнувся")
    return None, set()


def run_case(client, watcher, trim_index, how):
    """Один замір: тримаємо тример, втрачаємо телефон, дивимось, коли завмре."""
    watcher.mark()
    client.trim(trim_index, True)
    time.sleep(HOLD_S)

    last_before, moves, axes = watcher.snapshot()
    if moves == 0:
        return None, "канал не рухався навіть під час утримання — замір беззмістовний"

    t0 = time.monotonic()
    if how == "close":
        client.close()
    elif how == "silence":
        client.silent.set()
    elif how == "manual":
        print("\n  ⚠️ ЗАРАЗ вимкни Wi-Fi на телефоні або витягни живлення моста.")
        print(f"     Чекаю {WATCH_AFTER_S:.0f} с…")

    time.sleep(WATCH_AFTER_S)
    last_after, moves_after, _ = watcher.snapshot()

    if last_after is None:
        return None, "історія руху зникла — це вада інструмента"

    delay_ms = (last_after - t0) * 1000
    if delay_ms < 0:
        delay_ms = 0.0
    return delay_ms, f"осей рухалось: {sorted(axes)}, подій руху {moves_after}"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    proto.add_transport_args(ap)
    ap.add_argument("--js", metavar="ПРИСТРІЙ", help="джойстик пульта (типово — пошук)")
    ap.add_argument("--trim", type=int, help="номер тримера (типово — підібрати)")
    ap.add_argument("--manual", action="store_true",
                    help="замість автоматичного розриву чекати, поки Wi-Fi вимкнуть руками")
    args = ap.parse_args()

    desc, connect = proto.make_connector(args, poll=0.02)
    print(f"пульт через: {desc}")

    js = args.js
    if not js:
        found = find_joystick()
        if not found:
            print("\nДжойстика не знайдено. Пульт має бути ввімкнений, під'єднаний по USB")
            print("і в режимі USB-джойстика. Без нього міряти нічим: лічильники моста")
            print("кажуть «я надіслав», а питання стоїть «чи пульт відпустив».")
            return 1
        js = found[0]
        if len(found) > 1:
            print(f"джойстиків кілька, беру {js} (інші: {', '.join(found[1:])})")
    print(f"спостереження: {js}")

    watcher = AxisWatcher(js)
    cases = [("manual", "розрив Wi-Fi руками")] if args.manual else [
        ("close", "телефон закрив сокет"),
        ("silence", "телефон замовк при живому сокеті"),
    ]

    results = []
    try:
        for how, title in cases:
            print(f"\n=== {title}")
            client = Client(connect)
            hello = client.greet()
            if not hello:
                print("  пульт не привітався — перевір ланцюг")
                client.close()
                return 1
            print(f"  HELLO: {hello['target']} {hello['fw']}, тримерів {hello['trims']}, "
                  f"біт3 {'є' if hello['has_input_state'] else 'НЕМАЄ'}")
            client.start_hold()

            index = args.trim
            if index is None:
                index, _ = find_live_trim(client, watcher, hello["trims"])
                if index is None:
                    print("  ⚠️ жоден тример не рухає каналів. У моделі мають бути мікшери")
                    print("     на канали — інакше джойстик не побачить нічого (DECISIONS,")
                    print("     «джойстик віддає канали після мікшера»).")
                    client.close()
                    return 1
                print(f"  міряю тримером {index}")

            delay, note = run_case(client, watcher, index, how)
            client.close()
            time.sleep(0.5)

            if delay is None:
                print(f"  ✗ {note}")
                results.append((title, None))
            else:
                print(f"  ввід завмер через {delay:.0f} мс після втрати телефона ({note})")
                results.append((title, delay))
    finally:
        watcher.close()

    print("\n=== підсумок")
    ok = True
    for title, delay in results:
        if delay is None:
            print(f"  ✗ {title}: замір не вийшов")
            ok = False
            continue
        # Тайм-аут відпускання в прошивці — 1000 мс. Якщо вклалися помітно
        # раніше, значить спрацював саме міст, а не остання перешкода.
        verdict = "міст встиг першим" if delay < 900 else "⚠️ схоже, спрацював тайм-аут пульта"
        print(f"  {title}: {delay:.0f} мс — {verdict}")
        if delay > 1500:
            ok = False

    print("\n" + ("усе гаразд" if ok else "ПОМИЛКА: ввід відпускається не так, як обіцяно"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
