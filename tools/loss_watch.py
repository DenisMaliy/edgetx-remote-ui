#!/usr/bin/env python3
"""Де саме гинуть пачки: гортати меню довго й рівно, а потім назвати причини.

Задача 0018, критерій 1: «три різні причини — три числа». Людина гортати
десять хвилин не може, тому гортає цей інструмент: він крутить енкодер із
рівним темпом, як робить утримана стрілка ↓ у `tools/test_client.py`, і
наприкінці ділить втрати за причинами.

⚠️ **Міст тримає рівно одного клієнта.** Поки працює цей інструмент, телефон
витіснено — дивитись очима нема на чому. Це навмисно: прогін «за числами» і
прогін «за оком» (критерій 4.1) — різні прогони, і змішувати їх означало б
міряти шум (урок задачі 0012, 53 витіснення за сеанс).

Причини втрат, які інструмент розрізняє:

  черга повна      `ws.chunks_dropped` — `xRingbufferSend` не прийняв пачку
  не відправилось  `ws.send_dropped`, розкладене за `errno`:
                     nomem  ENOMEM/ENOBUFS — у мості скінчилась пам'ять
                     again  EAGAIN         — вікно TCP зачинене, це затор
                     conn   EPIPE/ECONNRESET — сокет мертвий
  телефона немає   `ws.chunks_noclient` — не втрата, рахується окремо

Приклад:

  tools/loss_watch.py --ws ws://192.168.4.1/ws --set-baud 2625000 --minutes 10

⚠️ `REFRESH` на втрати інструмент **не** шле, хоча справжній клієнт шле — раз
на 2 с за лічильником `packets_dropped`. Це навмисно й це різниця з бойовим
режимом: `REFRESH` доливає 20–40 КБ у той самий затор, який ми міряємо, і
причини перестали б розділятись. Тобто прогін показує втрати **без**
самолікування; з ним їх буде більше, а не менше.
"""

import argparse
import concurrent.futures
import json
import struct
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import remote_ui_proto as proto  # noqa: E402
from remote_ui_proto import (  # noqa: E402
    INPUT_STATE_PERIOD_S,
    PKT_BAUD,
    PKT_FRAME_END,
    PKT_HELLO,
    PKT_TILE,
    Decoder,
    InputMirror,
    add_transport_args,
    encode_baud_set,
    encode_enc,
    encode_frame,
    hold_packet,
    make_connector,
    parse_baud,
    parse_hello,
)

SCREEN_PX = None  # заповнюється з HELLO


def stats_url(args) -> str | None:
    """Звідки питати лічильники моста.

    Через міст швидкості дроту й лічильників у самому потоці не видно — вони
    живуть в `/api/stats`. Хост беремо з `--ws`, щоб не просити його двічі.
    """
    if args.stats:
        return args.stats
    if getattr(args, "ws", None):
        rest = args.ws.split("://", 1)[-1]
        host = rest.split("/", 1)[0]
        return f"http://{host}/api/stats"
    return None


def fetch_stats(url: str | None) -> dict | None:
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            body = r.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"  ⚠️ не дістав {url}: {e}", file=sys.stderr)
        return None
    if not body:
        # Міст віддає порожньо, коли JSON не вліз у буфер, і скаржиться
        # в свій журнал. Мовчазний розбір тут виглядав би як наша вада.
        print("  ⚠️ /api/stats порожній — найімовірніше замалий буфер у мості",
              file=sys.stderr)
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        print(f"  ⚠️ /api/stats не розібрався: {e}", file=sys.stderr)
        return None


class StatsPoller:
    """Знімає `/api/stats` **в окремому потоці**.

    ⚠️ Це не причісування, а виправлення вади, яка робила прогін
    безглуздим. `urlopen` блокує до 3 с, і поки він блокує, головний цикл не
    викликає `pump()` і не шле `INPUT_STATE`. Наслідки обидва потрапляють у
    ті самі числа, які ми міряємо:

    - нечитаний сокет за 100 мс переповнює вікно TCP при 260 КБ/с, тобто
      інструмент **сам** створює затор, який потім називає причиною;
    - мовчання понад 750 мс — і сторож моста відпускає ввід, після чого
      звіт друкує це як спостереження про міст.

    За типових `--every 15` таких провалів було б близько сорока за
    десятихвилинний прогін.
    """

    def __init__(self, url: str | None):
        self.url = url
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.pending = None

    def start(self) -> bool:
        """Замовити знімок. False — попередній ще не повернувся."""
        if not self.url or (self.pending and not self.pending.done()):
            return False
        self.pending = self.pool.submit(fetch_stats, self.url)
        return True

    def take(self):
        """Забрати готовий знімок або None, якщо ще не готовий."""
        if self.pending is None or not self.pending.done():
            return None
        out = self.pending.result()
        self.pending = None
        return out

    def blocking(self):
        """Знімок зараз — тільки поза прогоном (до й після)."""
        return fetch_stats(self.url)

    def close(self):
        self.pool.shutdown(wait=False)


def dig(d: dict | None, path: str, default=0):
    """`dig(s, "ws.chunks_dropped")` — щоб відсутнє поле не валило прогін."""
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


class SocketDied(Exception):
    """Сокет помер посеред прогону — числа після цього нічого не варті."""


class Runner:
    def __init__(self, args, link, hello, dec):
        self.args = args
        self.link = link
        self.hello = hello
        # Розбирач той самий, що ловив HELLO: у ньому може лежати початок
        # наступного кадру, і свіжий з'їв би його як сміття.
        self.dec = dec
        self.mirror = InputMirror()
        self.fw_input_state = hello["has_input_state"]

        # Що прийшло до клієнта — друга половина доказу. Лічильники моста
        # кажуть, що він викинув; це каже, що доїхало. Різниця з `uart.tiles`
        # і є справжня наскрізна втрата.
        self.tiles = 0
        self.frames = 0
        self.rx_bytes = 0
        self.last_baud = None
        self.last_tile_at = time.monotonic()
        # Справжня площа плиток беремо з самих пакетів, а не з припущення
        # 32×32: на 480×272 нижній ряд має висоту 16, і специфіка пульта в
        # інструменті — те саме порушення, що специфіка в коді (CLAUDE.md).
        self.tile_px = 0

    def pump(self) -> None:
        """Вичитати все, що прийшло. ⚠️ Обов'язково і безперервно.

        Клієнт, що перестав читати, втрачає сокет мовчки — і `ws.errors`
        лишаються нулями (задача 0012). Тобто інструмент, який лінується
        читати, зіпсував би саме той замір, заради якого написаний.
        """
        data = self.link.recv()
        if data is None:
            # ⚠️ Не те саме, що «нічого не прийшло»: труба віддає `None` лише
            # на смерті сокета. Мовчки продовжувати не можна — далі
            # інструмент гортав би в нікуди, а числа виглядали б проведеним
            # заміром. Саме так у 0012 замір перетворювався на шум.
            raise SocketDied("міст закрив з'єднання")
        if not data:
            return
        self.rx_bytes += len(data)
        for ptype, payload in self.dec.feed(data):
            if ptype == PKT_TILE:
                self.tiles += 1
                self.last_tile_at = time.monotonic()
                if len(payload) >= 8:
                    w, h = struct.unpack_from("<HH", payload, 4)
                    self.tile_px += w * h
            elif ptype == PKT_FRAME_END:
                self.frames += 1
            elif ptype == PKT_BAUD:
                info = parse_baud(payload)
                if info:
                    self.last_baud = info

    def set_baud(self, target: int, nonce: int = 11) -> bool:
        """Попросити швидкість каналу і дочекатись підтвердження."""
        self.link.send(encode_baud_set(target, nonce))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            self.pump()
            info = self.last_baud
            if info and info["nonce"] == nonce:
                ok = info["verdict"] == 0
                print(f"  пульт: {info['verdict_name']}, ціль {info['target']}, "
                      f"вікно повернення {info['revert_window_ms']} мс")
                return ok
            time.sleep(0.01)
        print("  ⚠️ пульт не підтвердив швидкість за 2 с", file=sys.stderr)
        return False


def observe(args) -> int:
    """Дивитись збоку, поки гортає людина з телефона.

    ⚠️ Клієнтом інструмент тут **не стає** — лише питає `/api/stats` по HTTP.
    Це єдиний спосіб отримати числа саме того клієнта, на який людина
    дивиться очима: міст тримає один сеанс, і будь-яке під'єднання з ПК
    витіснило б телефон разом із заміром.
    """
    url = stats_url(args)
    if not url:
        print("⚠️ режим спостереження потребує --ws або --stats", file=sys.stderr)
        return 2

    # Чекаємо на телефон, а не вимагаємо його наперед: людина під'єднується
    # руками, і відлік має починатись від неї, а не від нашої готовності.
    deadline = time.monotonic() + args.wait
    before = None
    said = False
    while time.monotonic() < deadline:
        before = fetch_stats(url)
        if before is not None and dig(before, "client", False):
            break
        if not said:
            print(f"чекаю на телефон (до {args.wait:.0f} с)… під'єднайтесь і "
                  f"починайте гортати")
            said = True
        time.sleep(1.0)
    else:
        print("⚠️ телефон так і не під'єднався — міряти нема чого", file=sys.stderr)
        return 1

    if before is None:
        return 1

    print(f"телефон на місці. Дивлюсь {args.observe:.0f} с — гортайте.\n")
    t0 = time.monotonic()
    prev, prev_t = before, t0
    while time.monotonic() - t0 < args.observe:
        time.sleep(min(args.every, args.observe))
        cur = fetch_stats(url)
        now = time.monotonic()
        if cur is not None:
            report_line(now - t0, prev, cur, prev_t, now, None)
            prev, prev_t = cur, now

    after = fetch_stats(url)
    elapsed = time.monotonic() - t0
    summary(args, before, after, None, elapsed, 0)

    if args.out:
        Path(args.out).write_text(json.dumps(
            {"before": before, "after": after, "elapsed_s": elapsed,
             "mode": "observe", "args": vars(args)},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nзнімки збережено: {args.out}")
    return 0


def run(args) -> int:
    desc, connect = make_connector(args, poll=0.005)
    url = stats_url(args)
    print(f"труба: {desc}")
    print(f"лічильники: {url or '— (немає, буде лише бік клієнта)'}")

    link = connect()

    # Вітання строго за протоколом: PING → HELLO → REFRESH. До HELLO клієнт
    # не знає розміру екрана й викидав би всі плитки.
    dec = Decoder()
    link.send(encode_frame(proto.PKT_PING))
    hello = None
    deadline = time.monotonic() + 3.0
    while hello is None and time.monotonic() < deadline:
        data = link.recv()
        if data:
            for ptype, payload in dec.feed(data):
                if ptype == PKT_HELLO:
                    hello = parse_hello(payload)
                    break
        else:
            time.sleep(0.01)
    if hello is None:
        print("⚠️ пульт не привітався за 3 с — прогону не буде", file=sys.stderr)
        return 1

    global SCREEN_PX
    SCREEN_PX = hello["width"] * hello["height"]
    print(f"HELLO: {hello['width']}×{hello['height']}, клавіш {len(hello['keys'])}, "
          f"енкодер {'+' if hello['has_encoder'] else '−'}, "
          f"INPUT_STATE {'+' if hello['has_input_state'] else '−'}, "
          f"прошивка {hello['fw']}")

    if not hello["has_encoder"]:
        print("⚠️ пульт каже, що енкодера немає — гортати нічим", file=sys.stderr)
        return 1

    r = Runner(args, link, hello, dec)

    if args.set_baud:
        print(f"прошу швидкість {args.set_baud}…")
        r.set_baud(args.set_baud)
        time.sleep(0.5)
        r.pump()

    # Повний кадр після REFRESH — 20–40 КБ. Знімати лічильники в цю мить
    # означало б заблокувати читання рівно на його прибутті, тобто самому
    # створити затор у нульовій точці заміру.
    link.send(encode_frame(proto.PKT_REFRESH))
    drain_until = time.monotonic() + 1.0
    while time.monotonic() < drain_until:
        r.pump()
        time.sleep(0.005)

    poller = StatsPoller(url)
    before = poller.blocking()

    # ⚠️ Лічильники клієнта обнуляються **тут**, а не при з'єднанні.
    #
    # Інакше вони рахують із рукостискання, а лічильники моста — з цієї миті,
    # і різниця «пульт віддав / клієнт отримав» виходить від'ємною: клієнт
    # ніби отримав більше, ніж міст надіслав. У першому ж прогоні це дало
    # «−27 плиток» і хибне попередження про обрізані кадри WebSocket на
    # 61 КБ. Тобто прилад доводив ваду, якої не було, — рівно тим способом,
    # яким вада й ховається.
    r.tiles = r.frames = r.rx_bytes = r.tile_px = 0
    r.dec.crc_errors = r.dec.oversized = 0
    if before is not None:
        got = dig(before, "baud")
        if args.set_baud and got != args.set_baud:
            print(f"⚠️ міст каже, що швидкість {got}, а просили {args.set_baud}. "
                  f"Прогін піде, але порівнювати його з числами 0017 не можна.",
                  file=sys.stderr)
        else:
            print(f"швидкість дроту за /api/stats: {got}")

        # ⚠️ Мінімуми й максимуми (`heap_min`, `ring_free_min`,
        # `chunk_len_max`, `send_ms_max`) наскрізні від старту моста, а не за
        # прогін. Після попереднього прогону вони збрешуть про цей.
        uptime = dig(before, "uptime_s")
        if uptime > 300:
            print(f"⚠️ міст працює вже {uptime // 60} хв. Мінімуми й максимуми "
                  f"({'heap_min, ring_free_min, chunk_len_max, send_ms_max'}) "
                  f"наскрізні — вони можуть бути з попереднього прогону. "
                  f"Для чистого заміру перезавантаж міст.", file=sys.stderr)

    # --- сам прогін --------------------------------------------------------
    total_s = args.minutes * 60.0
    step_period = 1.0 / args.rate
    t0 = time.monotonic()
    t_end = t0 + total_s
    next_step = t0
    next_state = t0
    next_report = t0 + args.every
    direction = 1
    next_flip = t0 + args.sweep
    steps = 0
    prev = before
    prev_t = t0
    broken = False

    print(f"\nгортаю {args.minutes} хв: {args.rate} клацань/с, зміна напрямку "
          f"кожні {args.sweep} с. Ctrl-C — зупинити раніше.\n")

    try:
        while True:
            now = time.monotonic()
            if now >= t_end:
                break

            r.pump()

            if now >= next_flip:
                direction = -direction
                next_flip = now + args.sweep

            if now >= next_step:
                link.send(encode_enc(direction))
                steps += 1
                # Не наздоганяємо пропущені кроки: рівний темп важливіший за
                # їх кількість, а наздоганяння дало б чергу клацань пачкою.
                next_step = now + step_period

            if now >= next_state:
                frame, _ = hold_packet(r.mirror, r.fw_input_state)
                if frame:
                    link.send(frame)
                next_state = now + INPUT_STATE_PERIOD_S

            if now >= next_report:
                poller.start()
                next_report = now + args.every

            # Знімок забираємо, коли він готовий, — цикл на нього не чекає.
            cur = poller.take()
            if cur is not None:
                report_line(now - t0, prev, cur, prev_t, now, r)
                prev, prev_t = cur, now

            # ⚠️ Тиша при живому сокеті — окремий вид смерті: нас витіснив
            # інший клієнт, або кадрування WebSocket поїхало. Без цієї
            # перевірки інструмент гортав би в порожнечу всі десять хвилин і
            # надрукував гарну таблицю зі стовідсотковою втратою.
            if r.tiles and now - r.last_tile_at > args.starve:
                raise SocketDied(
                    f"плиток немає {args.starve:.0f} с, а гортання триває — "
                    f"або нас витіснив інший клієнт, або потік WebSocket поїхав")

            time.sleep(0.001)
    except KeyboardInterrupt:
        print("\n⚠️ зупинено рукою — числа нижче за коротший проміжок")
    except SocketDied as e:
        print(f"\n⚠️ {e}. Прогін обірвано: числа нижче неповні й порівнювати "
              f"їх із цілим прогоном не можна.", file=sys.stderr)
        broken = True

    # Дати хвосту доїхати, перш ніж знімати підсумок.
    try:
        drain_until = time.monotonic() + 1.0
        while time.monotonic() < drain_until:
            r.pump()
            time.sleep(0.005)
    except SocketDied:
        pass

    after = poller.blocking()
    poller.close()
    elapsed = time.monotonic() - t0
    summary(args, before, after, r, elapsed, steps)

    if args.out:
        Path(args.out).write_text(json.dumps(
            {"before": before, "after": after, "elapsed_s": elapsed,
             "enc_steps": steps, "client_tiles": r.tiles,
             "client_frames": r.frames, "client_rx_bytes": r.rx_bytes,
             "client_tile_px": r.tile_px, "broken": broken,
             "args": vars(args)}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nзнімки збережено: {args.out}")

    link.close()
    # ⚠️ Обірваний прогін мусить відрізнятись кодом виходу: інакше зовні він
    # не відрізняється від цілого, і числа підуть у задачу як заміряні.
    return 3 if broken else 0


def report_line(t, prev, cur, prev_t, now, r):
    if prev is None or cur is None:
        print(f"  {t:5.0f}с  (лічильників моста немає)")
        return
    dt = max(now - prev_t, 1e-6)

    def d(path):
        return dig(cur, path) - dig(prev, path)

    fps = d("uart.frames") / dt
    kbs = d("uart.bytes") / dt / 1024
    print(f"  {t:5.0f}с  кадрів/с {fps:4.1f}  дріт {kbs:6.1f} КБ/с  "
          f"черга+{d('ws.chunks_dropped'):3d}  send+{d('ws.send_dropped'):3d}  "
          f"crc+{d('uart.crc_errors'):2d}  uartdrop+{d('uart.dropped'):2d}  "
          f"купа {dig(cur, 'heap_free') // 1024:3d}К (мін {dig(cur, 'heap_min') // 1024}К)")


def summary(args, before, after, r, elapsed, steps):
    print("\n" + "=" * 72)
    print(f"ПІДСУМОК за {elapsed:.0f} с, клацань енкодера {steps} "
          f"({steps / elapsed:.1f}/с)")
    print("=" * 72)

    if r is not None:
        print(f"\nДо клієнта доїхало: плиток {r.tiles}, кадрів {r.frames}, "
              f"{r.rx_bytes / 1024:.0f} КБ  ({r.frames / elapsed:.1f} кадр/с)")
        print(f"Розбирач клієнта: помилок CRC {r.dec.crc_errors}, "
              f"брехливих довжин {r.dec.oversized}")
    else:
        print("\n⚠️ Режим спостереження: боку клієнта не видно. Наскрізна "
              "втрата не рахується — тільки те, що визнав сам міст.")

    if before is None or after is None:
        print("\n⚠️ Лічильників моста немає — причин втрат не розділити. "
              "Це половина заміру, і саме та, заради якої він робився.")
        return

    def d(path):
        # Лічильники моста 32-бітові й обертаються: `uart.bytes` при
        # 2 625 000 бод — приблизно за 4.5 год. Без маски довгий прогін дав би
        # від'ємний приріст, і це прочиталось би як вада заміру.
        return (dig(after, path) - dig(before, path)) & 0xFFFFFFFF

    baud = dig(after, "baud") or proto.DEFAULT_BAUD
    uart_bytes = d("uart.bytes")
    load = uart_bytes * 10 / baud / elapsed * 100

    print("\n--- дріт (пульт → міст) ---")
    print(f"  байтів {uart_bytes} = {uart_bytes / elapsed / 1024:.1f} КБ/с, "
          f"завантаження {load:.1f}% від {baud}")
    print(f"  плиток {d('uart.tiles')}, кадрів {d('uart.frames')} "
          f"({d('uart.frames') / elapsed:.1f}/с)")
    print(f"  ⚠️ мають лишатись нулями: crc_errors {d('uart.crc_errors')}, "
          f"dropped {d('uart.dropped')}, oversized {d('uart.oversized')}, "
          f"silence_resets {d('uart.silence_resets')}")

    sent = d("uart.tiles")
    print("\n--- наскрізна втрата плиток ---")
    if r is None:
        print("  (не міряється: клієнт не наш)")
    elif sent:
        lost_e2e = sent - r.tiles
        print(f"  пульт віддав {sent}, до клієнта доїхало {r.tiles} → "
              f"загублено {lost_e2e} ({lost_e2e / sent * 100:.2f}%)")
    else:
        print("  ⚠️ пульт не віддав жодної плитки — екран не мінявся, "
              "і замір нічого не міряв")

    print("\n--- ПРИЧИНИ (критерій 1.1) ---")
    q = d("ws.chunks_dropped")
    s = d("ws.send_dropped")
    nomem = d("send_err.nomem")
    again = d("send_err.again")
    conn = d("send_err.conn")
    other = d("send_err.other")
    q_tiles = d("ws.tiles_dropped")
    print(f"  1. повна черга до Wi-Fi ... {q:5d} пачок "
          f"({q_tiles} плиток, {d('ws.packets_dropped')} пакетів)")
    print(f"  2. невдале відправлення .. {s:5d} пачок, з них за причиною:")
    print(f"       брак пам'яті (nomem) .... {nomem}")
    print(f"       затор TCP (again) ....... {again}")
    print(f"       сокет мертвий (conn) .... {conn}")
    print(f"       інше .................... {other} "
          f"(останні коди: esp={dig(after, 'send_err.last')}, "
          f"errno={dig(after, 'send_err.errno_last')})")
    # ⚠️ Склад пачки, що не відправилась, уже невідомий: у черзі лежить
    # безіменний блоб. Тому плитки друкуються лише біля причини 1, і мовчання
    # біля причини 2 — це «не міряється», а не «нуль».
    print("     ⚠️ у плитках причина 2 не міряється: у черзі лежить блоб "
          "без розбору. Оцінка через середню пачку — нижче.")
    print(f"  3. обрізаний кадр WebSocket {d('send_err.truncated'):3d} "
          f"(дозаписів залишку {d('send_err.partial')})")
    print(f"     найдовше відправлення за прогін: {dig(after, 'send_err.ms_max')} мс")
    if dig(after, "send_err.ms_max") > 500:
        print("     ⚠️ понад пів секунди в `send()` — черга за цей час "
              "переповнюється гарантовано, тобто причина 1 тут наслідок, "
              "а не причина")
    print(f"  довідково, не втрата: телефона не було {d('ws.chunks_noclient')} пачок")

    print("\n--- пам'ять (критерій 1.2) ---")
    print(f"  вільно зараз {dig(after, 'heap_free')} Б, "
          f"МІНІМУМ за весь час {dig(after, 'heap_min')} Б "
          f"({dig(after, 'heap_min') / 1024:.1f} КБ)")
    print(f"  найбільший суцільний шматок {dig(after, 'heap_largest')} Б — "
          f"фрагментація видна саме тут, а не в сумі")
    print(f"  запас стека задачі httpd: {dig(after, 'httpd_stack_free')} Б "
          f"(там лежить буфер JSON; нуль — паніка моста)")

    print("\n--- розмір пачки (критерій 3) ---")
    built = d("ws.chunks_built")
    built_b = d("ws.chunk_bytes_built")
    chunk_max = dig(after, "ws.chunk_len_max")
    ring_min = dig(after, "ws.ring_free_min")
    if built:
        avg = built_b / built
        print(f"  зібрано {built} пачок, у середньому {avg:.0f} Б, "
              f"найбільша {chunk_max} Б "
              f"(константа BRIDGE_WS_CHUNK_BYTES = 8192)")
        if chunk_max < 8192 * 0.9:
            print("  ⚠️ до 8192 не доходить: справжню стелю пачки задає буфер "
                  "читання rx_task, а не константа з назвою «розмір пачки»")

        # Площа — з розібраних пакетів, а не з припущення про 32×32:
        # на 480×272 нижній ряд плиток має висоту 16.
        if r is not None and r.tiles:
            px_per_tile = r.tile_px / r.tiles
            print(f"  плитка в середньому {px_per_tile:.0f} пікселів "
                  f"(заміряно з самих пакетів, не припущено)")
            if q and q_tiles:
                px_lost = q_tiles / q * px_per_tile
                print(f"  ⇒ одна втрачена пачка коштувала {q_tiles / q:.1f} "
                      f"плиток = {px_lost:.0f} пікселів"
                      + (f" = {px_lost / SCREEN_PX * 100:.1f}% площі екрана"
                         if SCREEN_PX else ""))
            else:
                print("  (втрачених пачок не було — площу втрати міряти нема на чому)")
        print(f"  накладні витрати на обгортку при {avg:.0f} Б: "
              f"~{80 / avg * 100:.1f}% каналу "
              f"(≈80 Б на заголовки WebSocket+TCP+802.11)")
    print(f"  успішно відправлено {d('ws.chunks')} пачок, {d('ws.bytes')} Б")

    # ⚠️ Не «вільне місце»: для NOSPLIT це найбільша пачка, яку черга ще
    # прийняла б, зі стелею align(розмір/2)−8 (`ringbuf.c:233`). Тому
    # порівнюємо з найбільшою фактичною пачкою, а не з розміром черги.
    print(f"  найменша пачка, яку черга ще приймала: {ring_min} Б "
          f"(проти найбільшої фактичної {chunk_max} Б)")
    if q == 0 and ring_min > chunk_max:
        print("  ⇒ черга у стелю не впиралась жодного разу — втрати не в ній")
    elif q:
        print(f"  ⇒ черга впиралась {q} разів")

    # ⚠️ Тільки **недостача** в клієнта є ознакою обрізаних кадрів. Надлишок
    # означав би зсув самого заміру, а не ваду моста, і кричати про нього —
    # це доводити ваду, якої немає.
    ws_bytes = d("ws.bytes") if r is not None else 0
    if ws_bytes and ws_bytes - r.rx_bytes > chunk_max:
        print(f"\n⚠️ міст каже, що відправив {ws_bytes} Б, клієнт отримав "
              f"{r.rx_bytes} Б — недостача {ws_bytes - r.rx_bytes} Б, більша "
              f"за пачку. Найімовірніше обрізані кадри WebSocket.")
    elif ws_bytes and r.rx_bytes - ws_bytes > chunk_max:
        print(f"\n⚠️ клієнт отримав більше, ніж міст надіслав "
              f"({r.rx_bytes} проти {ws_bytes}) — це зсув заміру, не вада "
              f"моста: лічильники двох боків стартували в різні миті.")

    print("\n--- сеанс ---")
    print(f"  під'єднань {d('session.seen')}, розривів {d('session.lost')}, "
          f"відпускань вводу {d('session.releases')} "
          f"(з них за мовчанням {d('session.silence_timeouts')})")
    if d("session.lost"):
        print("  ⚠️ сеанс рвався — числа вище змішані з перепідключеннями")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_transport_args(ap)
    ap.add_argument("--set-baud", type=int, default=None,
                    help="попросити швидкість каналу перед прогоном, напр. 2625000")
    ap.add_argument("--stats", default=None,
                    help="URL лічильників моста; типово виводиться з --ws")
    ap.add_argument("--minutes", type=float, default=10.0, help="скільки гортати")
    ap.add_argument("--rate", type=float, default=20.0,
                    help="клацань енкодера на секунду (утримана ↓ дає ~25)")
    ap.add_argument("--sweep", type=float, default=3.0,
                    help="через скільки секунд міняти напрямок гортання")
    ap.add_argument("--every", type=float, default=15.0,
                    help="як часто друкувати проміжний рядок")
    ap.add_argument("--starve", type=float, default=3.0,
                    help="скільки секунд без плиток вважати смертю потоку")
    ap.add_argument("--wait", type=float, default=120.0,
                    help="скільки чекати на телефон у режимі спостереження")
    ap.add_argument("--observe", type=float, default=None, metavar="СЕКУНД",
                    help="не ставати клієнтом: гортає людина з телефона, "
                         "інструмент лише знімає /api/stats")
    ap.add_argument("--out", default=None, help="куди скласти знімки JSON")
    args = ap.parse_args(argv)
    return observe(args) if args.observe else run(args)


if __name__ == "__main__":
    sys.exit(main())
