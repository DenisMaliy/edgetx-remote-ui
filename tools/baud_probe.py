#!/usr/bin/env python3
"""Перевірка запобіжника повернення: чи вертається пульт сам (задача 0017).

⚠️ Цей інструмент перевіряє механізм **відмовою, а не успіхом**.

Успішне перемикання нічого не доводить: воно виглядало б однаково і з
запобіжником, і без нього. Доводить лише те, що пульт, лишившись на швидкості,
на яку його співрозмовник не перейшов, **сам повертається додому** — і робить
це без втручання людини, у якої немає екрана.

Тому доказом тут вважається **відновлений зв'язок**, а не рядок у журналі.
Журнал може написати що завгодно, якщо пульта на тому кінці вже немає, тож
інструмент чекає справжніх кадрів із правильним CRC і міряє час до них.

Досліди:

  --mode follow    звичайне перемикання: обидва боки йдуть на нову швидкість.
                   Контрольний, щоб решта дослідів не переплутала «зламано» з
                   «не працює взагалі».

  --mode betray    ⚠️ головний, критерій 2.1. Просимо швидкість і **самі на неї
                   не переходимо**. Пульт має перемкнутись, промовчати рівно
                   вікно повернення й повернутись до нас.

  --mode reboot    критерій 2.2: у мить перемикання інструмент закриває порт
                   зовсім — так виглядає міст, що перезавантажився. Порт
                   відкривається знову на домашній швидкості.

Приклади:

  ./baud_probe.py --device /dev/ttyUSB0 --mode betray --to 2000000
  ./baud_probe.py --device /dev/ttyUSB0 --mode follow --to 2000000
  ./baud_probe.py --device /dev/ttyUSB0 --mode reboot --to 2000000

⚠️ Стенд: інструмент говорить із пультом **напряму** через перетворювач
USB-UART, без моста. Так дослід не залежить від третього боку, який сам може
бути зламаний.
"""

import argparse
import sys
import time

import remote_ui_proto as proto

# Скільки чекати на відповідь пульта, перш ніж визнати дослід невдалим.
#
# Вікно повернення — 500 мс, найгірша заміряна затримка задачі — 132 мс, плюс
# період INPUT_STATE 250 мс. П'ять секунд — це десятикратний запас: якщо за
# такий час пульт не озвався, він і не озветься, і чекати довше означає лише
# пізніше про це дізнатись.
RECOVERY_LIMIT_S = 5.0

# Скільки тримати канал живим до й після досліду.
SETTLE_S = 1.5


class Link:
    """Сеанс із пультом: тримає ввід живим і рахує, що прийшло.

    Дві труби, той самий протокол.

    **Дріт** (`--device`): інструмент сам грає роль моста й сам вирішує, чи
    переходити на нову швидкість. Дає всі три досліди.

    **Міст через WebSocket** (`--ws`): у ролі моста — справжній міст. Він про
    `BAUD_SET` ще не знає й пересилає потік клієнта не тлумачачи, тобто на нову
    швидкість не перейде **ніколи**.

    ⚠️ Це не обмеження, а найкращий доступний дослід `betray`: міст, який не
    перемикається, тут не імітований інструментом, а справжній. Заразом
    доказом стає те, що видно людині без приладів, — картинка в застосунку
    завмирає й оживає сама.
    """

    def __init__(self, device: str | None, ws: str | None, baud: int, verbose: bool):
        self.device = device
        self.ws = ws
        self.verbose = verbose
        self.dec = proto.Decoder()
        self.last_input = 0.0
        self.baud = baud
        self.tr = self._open(baud)

    def _open(self, baud: int):
        if self.ws:
            return proto.WsTransport(self.ws)
        return proto.SerialTransport(self.device, baud=baud)

    @property
    def over_bridge(self) -> bool:
        return self.ws is not None

    def close(self):
        self.tr.close()

    def reopen(self, baud: int):
        """Закрити й відкрити трубу наново — так виглядає перезавантажений міст."""
        self.tr.close()
        self.dec.reset()
        self.tr = self._open(baud)
        self.baud = baud

    def set_baudrate(self, baud: int):
        # Через міст своєї швидкості в нас немає: нею володіє міст, а він про
        # перемикання ще не знає. Мовчазне ігнорування тут було б брехнею —
        # дослід `follow` через міст неможливий, і про це сказано вголос.
        if self.over_bridge:
            raise RuntimeError(
                "через міст перемкнути свій бік неможливо: швидкістю дроту "
                "володіє міст. Дослід follow вимагає --device."
            )
        self.tr.set_baudrate(baud)
        self.dec.reset()
        self.baud = baud

    def pump(self, seconds: float, keep_alive: bool = True):
        """Крутить канал `seconds` секунд, віддаючи кожен розібраний пакет.

        Тримати ввід живим обов'язково: клієнт зобов'язаний слати INPUT_STATE
        раз на 250 мс, і саме цей потік закриває вікно повернення на боці
        пульта. Дослід, у якому ми замовкли самі, перевіряв би не запобіжник, а
        нашу здатність мовчати.
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            now = time.monotonic()
            if keep_alive and now - self.last_input >= proto.INPUT_STATE_PERIOD_S:
                self.tr.send(proto.encode_input_state(0, 0, False, 0, 0))
                self.last_input = now

            data = self.tr.recv()
            if data is None:
                raise RuntimeError("порт закрився з того боку")
            if data:
                for ptype, payload in self.dec.feed(data):
                    yield ptype, payload

    def wait_for_any_frame(self, limit: float, keep_alive: bool = True):
        """Чекає першого валідного кадру. Повертає (секунди, тип) або (None, None).

        ⚠️ Саме «будь-який валідний кадр», а не «саме той пакет, на який ми
        чекаємо». Прошивка закриває вікно повернення так само — будь-яким
        кадром із правильним CRC, — і інструмент має міряти те, що справді
        сталося, а не те, що нам хотілося б побачити.
        """
        started = time.monotonic()
        for ptype, payload in self.pump(limit, keep_alive):
            return time.monotonic() - started, ptype, payload
        return None, None, None


def describe_baud(value):
    return "не застосовне" if value is None else f"{value} бод"


def print_report(rep: dict):
    print("    вердикт:            ", rep["verdict_name"])
    print("    nonce:              ", rep["nonce"])
    print("    перемкнеться на:    ", rep["target"], "бод")
    print("    поточна:            ", describe_baud(rep["current"]))
    print("    switch_delay_ms:    ", rep["switch_delay_ms"])
    print("    revert_window_ms:   ", rep["revert_window_ms"])
    print("    відкотів у пульті:  ", rep["reverts"])


def request_switch(link: Link, target: int, nonce: int, verbose: bool):
    """Шле BAUD_SET і чекає на відповідь `BAUD` із **нашим** nonce.

    Чужий nonce ігнорується мовчки: це може бути підтвердження попередньої
    команди, що доїхало із запізненням, і прийняти його за свою відповідь
    означало б розійтися з пультом, не помітивши цього.
    """
    link.tr.send(proto.encode_baud_set(target, nonce))
    started = time.monotonic()

    for ptype, payload in link.pump(2.0):
        if ptype != proto.PKT_BAUD:
            continue
        rep = proto.parse_baud(payload)
        if rep is None:
            print("  ⚠️ BAUD з обрізаним вантажем — відкинуто")
            continue
        if rep["nonce"] != nonce:
            if verbose:
                print(f"  (BAUD із чужим nonce {rep['nonce']} — ігноруємо)")
            continue
        return rep, time.monotonic() - started

    return None, time.monotonic() - started


def run(args) -> int:
    home = args.home
    target = args.to

    where = f"міст {args.ws}" if args.ws else f"дріт {args.device}"
    print(f"Пульт через: {where}, домашня швидкість {home} бод")
    print(f"Дослід: {args.mode}, ціль {target} бод")
    if args.ws:
        print("⚠️ Через міст: на нову швидкість не переходить НІХТО, крім пульта.")
        print("   Саме тому це найчесніший betray — міст справжній, не вдаваний.")
    print()

    link = Link(args.device, args.ws, home, args.verbose)
    try:
        # --- Крок 0. Переконатись, що канал узагалі живий ---------------------
        #
        # Без цього невдалий дослід не відрізнити від мертвого стенда.
        print("[0] перевіряю, що зв'язок є до досліду…")
        link.tr.send(proto.encode_frame(proto.PKT_PING))
        elapsed, ptype, _ = link.wait_for_any_frame(RECOVERY_LIMIT_S)
        if elapsed is None:
            print("  ✗ пульт мовчить ДО досліду — стенд не готовий, дослід не почато")
            return 2
        print(f"  ✓ пульт відповідає, перший кадр за {elapsed * 1000:.0f} мс")

        for _ in link.pump(SETTLE_S):
            pass

        # --- Крок 1. Попросити перемикання ------------------------------------
        print(f"\n[1] прошу перемикання на {target} бод…")
        rep, ack_ms = request_switch(link, target, args.nonce, args.verbose)
        if rep is None:
            print("  ✗ підтвердження не прийшло за 2 с — пульт не зрозумів команди")
            return 2

        print(f"  підтвердження за {ack_ms * 1000:.0f} мс:")
        print_report(rep)

        if rep["verdict"] != proto.BAUD_ACCEPTED:
            # Це не обов'язково провал: --to з-поза переліку саме так і має
            # закінчитись. Але перемикання не буде, тож і міряти нема чого.
            print("\n  → пульт відмовився перемикатись; нічого не сталося.")
            return 0 if args.expect_refusal else 1

        switch_delay = rep["switch_delay_ms"] / 1000.0
        revert_window = rep["revert_window_ms"] / 1000.0

        # --- Крок 2. Дослід ----------------------------------------------------
        if args.mode == "follow":
            print(f"\n[2] чекаю {rep['switch_delay_ms']} мс і йду за пультом…")
            time.sleep(switch_delay)
            link.set_baudrate(target)
            print(f"  свій бік перемкнуто на {target} бод")

            elapsed, ptype, payload = link.wait_for_any_frame(RECOVERY_LIMIT_S)
            if elapsed is None:
                print(f"  ✗ пульт не озвався на {target} бод за {RECOVERY_LIMIT_S} с")
                print("    (це або дріт не тримає швидкості, або перемикання зламане)")
                return 1
            print(f"  ✓ пульт говорить на {target} бод, перший кадр за {elapsed * 1000:.0f} мс")

            # ⚠️ Повертаємось додому свідомо, а не лишаємо стенд на новій
            # швидкості: наступний інструмент відкриє порт на домашній і не
            # зрозуміє, чому пульт мовчить.
            print(f"\n[3] повертаю обидва боки додому на {home} бод…")
            link.tr.send(proto.encode_baud_set(home, args.nonce + 1))
            for _ in link.pump(0.3):
                pass
            time.sleep(switch_delay)
            link.set_baudrate(home)
            elapsed, _, _ = link.wait_for_any_frame(RECOVERY_LIMIT_S)
            print(
                "  ✓ вдома" if elapsed is not None else "  ⚠️ додому не повернулись автоматично"
            )
            return 0

        if args.mode == "betray":
            print(f"\n[2] ⚠️ НЕ перемикаюсь. Пульт зараз на {target} бод, я лишаюсь на {home}.")
            print(f"    Вікно повернення — {rep['revert_window_ms']} мс.")
            print("    Далі мовчу й слухаю: пульт має повернутись сам.")

        elif args.mode == "reboot":
            print(f"\n[2] ⚠️ закриваю порт зовсім — так виглядає перезавантажений міст.")
            link.reopen(home)
            print(f"    порт відкрито наново на {home} бод")

        # --- Крок 3. Головний вимір -------------------------------------------
        #
        # Від цієї миті рахуємо час до першого валідного кадру. Усе, що прийде
        # раніше за відкат, — сміття з чужої швидкості, і CRC його не пропустить.
        print(f"\n[3] стежу за потоком {RECOVERY_LIMIT_S} с і шукаю провал…")

        # ⚠️ Міряємо не «час до першого кадру», а **найдовший провал у потоці**.
        #
        # Наївний вимір «від підтвердження до наступного валідного кадру» тут
        # бреше, і особливо через міст: пульт перемикається не в мить
        # підтвердження, а на кілька мілісекунд пізніше, і всі ці мілісекунди
        # кадри ще законно йдуть. Інструмент побачив би «повернувся за 3 мс» і
        # оголосив, що перемикання не сталося.
        #
        # Провал у потоці — це і є сліпий проміжок, і він у тому самому місці,
        # де його міряє сам пульт: від запису дільника до першого почутого
        # кадру. Заразом він не залежить від того, скільки труб між нами.
        stamps = []
        revert_report = None
        started = time.monotonic()

        try:
            for ptype, payload in link.pump(RECOVERY_LIMIT_S):
                stamps.append((time.monotonic() - started, ptype))
                if ptype == proto.PKT_BAUD:
                    rep2 = proto.parse_baud(payload)
                    if rep2 and rep2["verdict"] == proto.BAUD_REVERTED:
                        revert_report = rep2
        except RuntimeError as exc:
            print(f"  ✗ {exc}")
            return 1

        if not stamps:
            print(f"  ✗ ПУЛЬТ НЕ ПОВЕРНУВСЯ: жодного валідного кадру за {RECOVERY_LIMIT_S} с.")
            print("    Це блокер: запобіжник не працює, і пульт лишився німим.")
            return 1

        # Найбільший проміжок між сусідніми кадрами, з нуля включно.
        gap_ms, gap_at = stamps[0][0] * 1000.0, 0.0
        for (t_prev, _), (t_now, _) in zip(stamps, stamps[1:]):
            if (t_now - t_prev) * 1000.0 > gap_ms:
                gap_ms, gap_at = (t_now - t_prev) * 1000.0, t_prev

        after = [t for t, _ in stamps if t > gap_at + gap_ms / 1000.0 - 1e-9]
        print(f"  кадрів за вікно: {len(stamps)}, після провалу: {len(after)}")
        print(f"  ✓ НАЙДОВШИЙ ПРОВАЛ: {gap_ms:.0f} мс, почався на {gap_at * 1000:.0f} мс")

        if len(after) < 2:
            print("  ✗ після провалу потік не відновився — пульт не повернувся.")
            return 1

        first_ms = gap_ms

        # --- Крок 4. Що з цього випливає --------------------------------------
        print("\n[4] підсумок:")
        print(f"    провал у потоці:        {first_ms:.0f} мс")
        print(f"    вікно повернення:       {rep['revert_window_ms']} мс")

        if revert_report is not None:
            print(f"    пульт назвав причину:   {revert_report['verdict_name']}")
            print(f"    відкотів у пульті:      {revert_report['reverts']}")
        else:
            # Не провал, але й не повний доказ: зв'язок є, а слова від того,
            # хто відкотився, ми не почули.
            print("    ⚠️ пакета REVERTED не бачив — зв'язок відновлено, але")
            print("       причину пульт не назвав. Перевір, чи не з'їв його")
            print("       розрив між кроками.")

        # Часова межа знизу — теж перевірка. Пульт, що «повернувся» раніше за
        # власне вікно, насправді не перемикався зовсім, і дослід нічого не
        # довів би.
        if first_ms < rep["revert_window_ms"] * 0.5:
            print("\n  ⚠️ повернувся підозріло швидко — менше за половину вікна.")
            print("     Схоже, перемикання не сталося взагалі. Перевір журнал.")
            return 1

        print("\n  ✓ ЗАПОБІЖНИК ПРАЦЮЄ: пульт повернувся сам, без втручання.")
        return 0

    finally:
        link.close()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Перевірка запобіжника повернення швидкості (задача 0017)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--device", help="послідовний порт до пульта (перетворювач USB-UART на AUX1)")
    src.add_argument("--ws", help="WebSocket до моста, напр. ws://192.168.4.1/ws")
    ap.add_argument(
        "--mode",
        choices=("follow", "betray", "reboot"),
        default="betray",
        help="дослід: follow — обидва перемикаються; betray — ми ні; reboot — ми вмираємо",
    )
    ap.add_argument("--to", type=int, default=2000000, help="цільова швидкість")
    ap.add_argument("--home", type=int, default=proto.DEFAULT_BAUD, help="домашня швидкість")
    ap.add_argument("--nonce", type=int, default=7, help="nonce команди, 1…255")
    ap.add_argument(
        "--expect-refusal",
        action="store_true",
        help="дослід вважається вдалим, якщо пульт відмовився (перевірка переліку)",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if not 1 <= args.nonce <= 255:
        print("nonce має бути в межах 1…255", file=sys.stderr)
        return 2

    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nперервано")
        return 130
    except RuntimeError as exc:
        print(f"помилка: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
