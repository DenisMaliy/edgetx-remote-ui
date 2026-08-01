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
import statistics
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import remote_ui_proto as proto  # noqa: E402

# Скільки тримати тример, щоб переконатися, що канал справді повзе.
#
# ⚠️ Було 1.5 с — і цього **не вистачало**. Авто-повтор тримера в EdgeTX
# розганяється не одразу (довге натискання 320 мс, потім повтор), а перші
# кроки міняють канал менше ніж на `MOVE_EPS`. Заміряно на живому пульті:
# утримання 2 с давало **нуль** подій руху на всіх 12 напрямках, тоді як
# 12 с — від 60 до 415. Тобто інструмент виглядав зламаним там, де просто
# не дочекався.
HOLD_S = 4.0

# Скільки утримувати кожен напрямок, підбираючи найкращий.
PROBE_S = 2.5

# Скільки чекати після втрати телефона. Більше і за сторожа моста (750 мс),
# і за тайм-аут відпускання в прошивці (1000 мс), із запасом.
WATCH_AFTER_S = 3.0

# Рух каналу менший за це вважаємо шумом АЦП, а не тримером.
MOVE_EPS = 2

# ⚠️ Головна перевірка дійсності заміру.
#
# Тример **доїжджає до упору** і після цього канал завмирає, хоча ввід
# утримують далі. Тоді «остання мить руху» — це мить упору, а не мить
# відпускання, і різниця з `t0` перетворюється на випадкове число: у задачі
# 0012 воно давало розкид 0…1829 мс, а від'ємне значення затискалось у нуль,
# тобто **впор виглядав як відмінний результат**.
#
# Тому перед тим, як втрачати телефон, ми вимагаємо доказу, що канал живий
# **саме зараз**: остання подія руху має бути не старша за цей поріг. Джойстик
# віддає рух тримера десятками подій за секунду на швидких напрямках, але на
# повільних — раз на ~200 мс. Тому поріг 400 мс: удвічі більший за найповільніший
# спостережений період повтору, тобто «стоїть» упевнено, і водночас удвічі
# менший за 750 мс, які ми міряємо.
#
# Правити треба саме доказовість, а не спосіб: обхід «повертати тример у
# вихідне між прогонами» лікує симптом і лишає можливим тихий недостовірний
# результат, а перевірка робить його неможливим.
STALL_EPS_MS = 400.0

# ⚠️ Межа роздільності всього заміру.
#
# Ми бачимо не «мить відпускання», а «мить останнього руху каналу», і частіше
# за повтор тримера канал не рухається. Заміряно: найповільніший напрямок дає
# подію раз на ~200 мс, найшвидший — раз на ~29 мс. Тому інструмент обирає
# **найрухливіший** напрямок, а не перший-ліпший: це прямо покращує точність
# числа, яке він потім друкує.


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
        self.move_times = []    # позначки часу всіх подій руху
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
                    self.move_times.append(now)
                    self.moves += 1
                    self.moved_axes.add(num)

    def mark(self):
        """Забути попередню історію руху."""
        with self.lock:
            self.last_move_t = None
            self.move_times = []
            self.moves = 0
            self.moved_axes = set()

    def snapshot(self):
        with self.lock:
            return self.last_move_t, self.moves, set(self.moved_axes)

    def moves_since(self, t):
        """Скільки подій руху сталося після моменту `t`.

        Потрібне, щоб відрізнити «канал зупинився, бо ввід відпустили» від
        «канал зупинився, бо тример доїхав до упору **вже під час заміру**».
        Перший випадок дає кілька подій після команди й тоді тишу; другий —
        жодної або одну, і при цьому число виходить меншим за справжнє.
        """
        with self.lock:
            return sum(1 for ts in self.move_times if ts > t)

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


def find_live_trim(client, watcher, count, probe_s=PROBE_S, retry=False):
    """Підібрати напрямок тримера, який рухає канал **найчастіше**.

    Не вгадуємо: у моделі може не бути мікшера на потрібний канал, і тоді
    тример нічого не зрушить — а виглядало б це як «ввід не доходить».

    ⚠️ Перебираємо **всі** напрямки і беремо найрухливіший, а не перший, що
    ворухнувся. Причина не в акуратності, а в точності: роздільність усього
    заміру дорівнює періоду повтору тримера, і різниця між напрямками на
    цьому пульті — сім разів (подія раз на 29 мс проти раз на 200 мс).
    Взявши перший-ліпший, ми б додали до кожного числа похибку, якої легко
    уникнути.

    Індекс — це номер **напрямку** (0..2*кількість-1), як `trim_keys` в
    EdgeTX, а не номер тримера.
    """
    best_index, best_moves, best_axes = None, 0, set()

    for index in range(max(2, count * 2)):
        watcher.mark()
        client.trim(index, True)
        time.sleep(probe_s)
        _, moves, axes = watcher.snapshot()
        client.trim(index, False)
        time.sleep(0.4)

        rate = moves / probe_s
        if moves == 0:
            print(f"  напрямок {index}: канал не ворухнувся")
            continue
        print(f"  напрямок {index}: {moves} подій ({rate:.0f}/с), осі {sorted(axes)}")
        if moves > best_moves:
            best_index, best_moves, best_axes = index, moves, axes

    if best_index is None and not retry:
        # ⚠️ Перш ніж оголошувати «жоден тример не рухає каналів», спробувати
        # ще раз і довше. Саме ця хибна діагностика вже коштувала окремого
        # розслідування: тример працював, а проба була закоротка для розгону
        # авто-повтору EdgeTX.
        print("  жоден напрямок не ворухнув каналу — повторюю з утриманням "
              f"{PROBE_S * 3:.0f} с, перш ніж здаватись")
        return find_live_trim(client, watcher, count, probe_s=PROBE_S * 3, retry=True)

    if best_index is not None:
        print(f"  беру напрямок {best_index}: найрухливіший, "
              f"{best_moves / probe_s:.0f} подій/с")
    return best_index, best_axes


def run_case(client, watcher, trim_index, how, pre_hold_s=0.0):
    """Один замір: тримаємо тример, втрачаємо телефон, дивимось, коли завмре.

    Повертає `(delay_ms, note)`. `delay_ms is None` означає **прогін відкинуто**:
    інструмент не має права віддати число, якого він не міряв. Ні великого, ні
    нульового — саме нуль і був найнебезпечнішою формою брехні.
    """
    watcher.mark()
    client.trim(trim_index, True)
    if pre_hold_s > 0:
        # Навмисне доведення тримера до упору — щоб показати, що інструмент
        # це помічає. Використовується тільки прапорцем `--pre-hold`.
        time.sleep(pre_hold_s)
    time.sleep(HOLD_S)

    last_before, moves, axes = watcher.snapshot()
    t0 = time.monotonic()

    if moves == 0:
        client.trim(trim_index, False)
        return None, ("канал не ворухнувся жодного разу за час утримання — "
                      "міряти нічого")

    stale_ms = (t0 - last_before) * 1000.0
    if stale_ms > STALL_EPS_MS:
        client.trim(trim_index, False)
        return None, (f"канал завмер за {stale_ms:.0f} мс ДО команди відпускання "
                      f"(поріг {STALL_EPS_MS:.0f}) — тример у впорі, і «остання мить "
                      f"руху» показала б упор, а не відпускання")

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

    # ⚠️ Друга половина перевірки дійсності, і без неї перша марна.
    #
    # Перевірка перед `t0` доводить лише те, що канал був живий **у мить
    # команди**. Але тример тримають ще й далі, і він цілком може доїхати до
    # упору **всередині вікна заміру**. Тоді «остання мить руху» — це знову
    # мить упору, число виходить додатним, меншим за справжнє, і проходить
    # усі перевірки як відмінний результат.
    #
    # Розрізняє їх кількість подій після команди: справжнє відпускання дає
    # кілька рухів і тоді тишу, упор — жодного або один. Це не теорія: у
    # першому наборі цієї ж задачі так проскочило 15 мс при медіані 89.
    after = watcher.moves_since(t0)
    if after < 2:
        return None, (f"після команди відпускання канал ворухнувся {after} раз(и) — "
                      f"замало, щоб відрізнити відпускання від упору всередині "
                      f"вікна заміру")

    delay_ms = (last_after - t0) * 1000.0
    if delay_ms < 0:
        # Рух скінчився раніше за команду, хоч перевірка вище пройшла. Це вже
        # не «майже нуль», а суперечність — і затискати її в нуль означало б
        # видати за відмінний результат саме те, чого ми не поміряли.
        return None, (f"останній рух каналу на {-delay_ms:.0f} мс раніший за команду "
                      f"відпускання — замір суперечливий")

    return delay_ms, f"осей рухалось: {sorted(axes)}, подій руху {moves_after}"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    proto.add_transport_args(ap)
    ap.add_argument("--js", metavar="ПРИСТРІЙ", help="джойстик пульта (типово — пошук)")
    ap.add_argument("--trim", type=int, metavar="НАПРЯМОК",
                    help="номер **напрямку** тримера (0..2*кількість-1, як `trim_keys` "
                         "в EdgeTX), а не номер тримера. Типово — підібрати найрухливіший")
    ap.add_argument("--manual", action="store_true",
                    help="замість автоматичного розриву чекати, поки Wi-Fi вимкнуть руками")
    ap.add_argument("--runs", type=int, default=5, metavar="N",
                    help="скільки прогонів на кожен спосіб (типово 5). Один прогін "
                         "нічого не доводить: саме розкид викрив ваду інструмента")
    ap.add_argument("--pre-hold", type=float, default=0.0, metavar="СЕК",
                    help="утримувати тример стільки секунд ДО заміру. Потрібно, щоб "
                         "навмисно довести його до упору й показати, що прогін буде "
                         "відкинуто, а не порахований")
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

    results = {}   # спосіб → список дійсних чисел
    rejected = {}  # спосіб → список причин відкидання
    base_trim = args.trim

    try:
        for how, title in cases:
            print(f"\n=== {title}")
            results[title] = []
            rejected[title] = []

            for run_no in range(args.runs):
                client = Client(connect)
                hello = client.greet()
                if not hello:
                    print("  пульт не привітався — перевір ланцюг")
                    client.close()
                    return 1
                if run_no == 0:
                    print(f"  HELLO: {hello['target']} {hello['fw']}, "
                          f"тримерів {hello['trims']}, "
                          f"біт3 {'є' if hello['has_input_state'] else 'НЕМАЄ'}")
                client.start_hold()

                if base_trim is None:
                    base_trim, _ = find_live_trim(client, watcher, hello["trims"])
                    if base_trim is None:
                        print("  ⚠️ жоден тример не рухає каналів. У моделі мають бути")
                        print("     мікшери на канали — інакше джойстик не побачить")
                        print("     нічого (DECISIONS, «джойстик віддає канали після")
                        print("     мікшера»).")
                        client.close()
                        return 1
                    print(f"  міряю напрямком {base_trim}")

                # ⚠️ Напрямок чергується між прогонами. Це не косметика: тример
                # односпрямовано доїжджає до упору за кілька прогонів, і далі
                # кожен наступний був би відкинутий перевіркою дійсності. Так
                # він гуляє туди-сюди й лишається в робочій частині ходу.
                index = base_trim if run_no % 2 == 0 else base_trim ^ 1

                delay, note = run_case(client, watcher, index, how, args.pre_hold)
                client.close()
                time.sleep(0.5)

                if delay is None:
                    print(f"  прогін {run_no + 1}: ✗ ВІДКИНУТО — {note}")
                    rejected[title].append(note)
                else:
                    print(f"  прогін {run_no + 1}: ввід завмер через {delay:.0f} мс "
                          f"({note})")
                    results[title].append(delay)
    finally:
        watcher.close()

    print("\n=== підсумок")
    ok = True
    measured = True
    for _, title in cases:
        vals = results[title]
        bad = rejected[title]
        print(f"\n  {title}")
        print(f"    відкинуто прогонів: {len(bad)} з {args.runs}")
        for note in bad:
            print(f"      ✗ {note}")
        if not vals:
            print("    ✗ дійсних чисел немає — міряти не вдалося")
            measured = False
            continue

        vals_sorted = sorted(vals)
        median = statistics.median(vals_sorted)
        print(f"    дійсних {len(vals)}: "
              f"{', '.join(f'{v:.0f}' for v in vals_sorted)} мс")
        print(f"    медіана {median:.0f} мс, розкид {min(vals):.0f}…{max(vals):.0f} мс")

        # Тайм-аут відпускання в прошивці — 1000 мс. Якщо вклалися помітно
        # раніше, значить спрацював саме міст, а не остання перешкода.
        worst = max(vals)
        if worst < 900:
            print("    міст встиг першим у всіх прогонах")
        elif worst < 1500:
            print("    ⚠️ найгірший прогін близький до тайм-ауту пульта (1000 мс)")
        else:
            print("    ⚠️ найгірший прогін НЕ вклався в тайм-аут пульта")
            ok = False

    # ⚠️ «Не поміряли» і «поміряли, і погано» — різні висновки, і плутати їх
    # не можна саме тут. Прогін із тримером у впорі не дає жодного числа, і
    # оголошувати через це ввід несправним означало б робити ту саму підміну,
    # проти якої писалась уся перевірка дійсності.
    if not measured:
        print("\nЗАМІРУ НЕ ВИЙШЛО: усі прогони відкинуто, про поведінку вводу "
              "це не каже нічого.\nПричини вище — найчастіше тример у впорі; "
              "поверніть його в робочу частину ходу.")
        return 2

    print("\n" + ("усе гаразд" if ok else "ПОМИЛКА: ввід відпускається не так, як обіцяно"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
