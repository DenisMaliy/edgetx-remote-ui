#!/usr/bin/env python3
"""Водити браузерний клієнт із ПК: ходити по меню пульта й знімати докази.

Задача 0019. Екран пульта розбитий, а бокових панелей у клієнті ще немає
(етап 3), тож єдиний спосіб керувати пультом із ПК — **дотики по канві**, тобто
рівно те, що робить палець по телефону. Цей скрипт відкриває клієнт у Chrome
через Playwright і перекладає координати екрана пульта в координати сторінки.

Навіщо: без нього кожен замір коштує рук людини біля стенда. З ним — числа й
знімки знімаються сценарієм, відтворювано.

⚠️ Дві межі, обидві заміряні, а не припущені:

1. **Дотик коротший за ~100 мс пульт не помічає.** LVGL опитує сенсор раз на
   30 мс (`LV_INDEV_DEF_READ_PERIOD`), тож коротке натискання просто не
   потрапляє в опитування. Тут дотик тримається 150 мс.
2. **Клавіші `ENTER` у браузерному клієнті немає** — миша дає лише енкодер
   (колесо) і «назад» (права кнопка). Усе, що вимагає `ENTER` — зокрема
   `[ENTER] to reset` на сторінці `Debug`, — натискає людина на пульті.
   Та кнопка на екрані **не сенсорна**: це підпис до клавіші.

Потрібен Playwright і встановлений Chrome:

    python3 -m venv pw && ./pw/bin/pip install playwright

Запуск (вікно лишається відкритим, команди йдуть через FIFO):

    mkfifo /tmp/ui.fifo
    ./pw/bin/python tools/webui_drive.py /tmp/ui.fifo /tmp/shots &
    echo 'shot назва' > /tmp/ui.fifo
    echo 'quit'       > /tmp/ui.fifo

⚠️ Вікно навмисно **не закривається** між командами: кожне перепід'єднання
коштує `REFRESH` і повний кадр, а лічильники клієнта починаються з нуля — тобто
замір, розрізаний на перезапуски, не додається.

Команди:

    tap X Y          дотик у точку **екрана пульта** (не сторінки)
    back             клавіша «назад» (RTN) — права кнопка миші
    wheel N          N клацань енкодера, додатне — вниз
    scroll SEC HZ ІМ'Я   безперервне гортання **енкодером**; «-» без знімків
    swipe SEC ІМ'Я   протяг **пальцем** по списку; «-» без знімків
    dragrule on|off  правило «не чекати під протягом» (задача 0020)
    panel            текст панелі «Стан» — звідти числа затримки
    baud N           замовити швидкість каналу (задача 0017)
    click SELECTOR   кнопка самого клієнта: «Стан», «чек»
    shot ІМ'Я        знімок канви
    pageshot ІМ'Я    знімок сторінки цілком — з плашкою, режимом і швидкістю
    burst N MS ІМ'Я  N знімків сторінки підряд
    wait MS / info / say ТЕКСТ / quit

⚠️ `scroll` і `swipe` — **різні органи керування**, і плутати їх не можна:
перший крутить колесо миші, тобто емулює енкодер, другий веде пальцем по
сенсору. Уся задача 0020 саме про різницю між ними.

⚠️ Для доказів беріть `pageshot`, а не `shot`: рецензія 0019 вимагає, щоб
знімок сам казав, у якому режимі й на якій швидкості він знятий.
"""
import sys
import time
import pathlib
from playwright.sync_api import sync_playwright

FIFO = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/webui-drive.fifo")
SHOTS = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/webui-drive")
SHOTS.mkdir(parents=True, exist_ok=True)
LOG = SHOTS / "drive.log"
URL = sys.argv[3] if len(sys.argv) > 3 else "http://192.168.4.1/"
# Рівно 2× екран пульта: 480×272. Ціле збільшення, щоб піксель не мазався.
VIEW = {"width": 960, "height": 544}


def log(msg):
    with LOG.open("a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    print(msg, flush=True)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=False, args=[
            "--window-size=980,700", "--window-position=0,0"])
        page = browser.new_page(viewport=VIEW)
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_function(
            "() => document.getElementById('screen').width > 100", timeout=20000)
        box = page.locator("#screen").bounding_box()
        w = page.evaluate("document.getElementById('screen').width")
        h = page.evaluate("document.getElementById('screen').height")
        log(f"вікно відкрите: канва {w}×{h} на {box['width']:.0f}×{box['height']:.0f}")

        def to_page(x, y):
            return (box["x"] + (x + 0.5) * box["width"] / w,
                    box["y"] + (y + 0.5) * box["height"] / h)

        def status():
            return page.evaluate(
                "() => ['link','fps','busy','radio']"
                ".map(id => (document.getElementById(id)||{}).textContent)"
                ".join('  |  ')")

        def shot(name):
            page.locator("#screen").screenshot(path=str(SHOTS / f"{name}.png"))

        def pageshot(name):
            # Сторінка цілком: канва + плашка з режимом очікування й лічильниками.
            # Рецензія 0019 вимагає, щоб знімок сам казав, у якому режимі знятий.
            page.screenshot(path=str(SHOTS / f"{name}.png"))

        def wait_mode():
            return page.evaluate(
                "() => (document.getElementById('btn-wait')||{}).textContent")

        def panel_text():
            """Панель «Стан» текстом — звідти беруться числа затримки.

            ⚠️ Панель наповнюється раз на секунду і **тільки коли відкрита**
            (`updateInfo` виходить одразу, якщо `hidden`). Тому спершу
            відкриваємо, потім чекаємо оновлення, і лише тоді читаємо: інакше
            вертався б текст, зібраний хтозна-коли.
            """
            was_hidden = page.evaluate(
                "() => document.getElementById('info').hidden")
            if was_hidden:
                page.click("#btn-info")
            time.sleep(1.2)
            text = page.evaluate(
                "() => document.getElementById('info').textContent"
                ".split('міст')[0].trim()")
            # ⚠️ Закриваємо назад: панель лежить поверх канви, і залишена
            # відкритою вона з'їдала б дотики наступного протягу — замір
            # виглядав би проведеним, а міряв би тишу.
            if was_hidden:
                page.click("#btn-info")
            return text

        def swipe(seconds, name, hold_ms=360, steps=12, gap_ms=250):
            """Протяг пальцем по списку — те, заради чого існує задача 0020.

            ⚠️ Не те саме, що `scroll`: там колесо миші, тобто **енкодер**.
            Тут — справжній жест сенсора: DOWN, низка MOVE, UP.

            Кроки розтягнуті в часі навмисно: LVGL опитує сенсор раз на 30 мс
            (`LV_INDEV_DEF_READ_PERIOD`), тож миттєвий перескок Playwright пульт
            побачив би як один стрибок, а не як протяг. `gap_ms` між жестами —
            запас на хвіст інерції, який після відпускання ще їде.
            """
            x = w // 2
            y_lo, y_hi = int(h * 0.80), int(h * 0.20)
            t0, n, shots = time.time(), 0, 0
            while time.time() - t0 < seconds:
                a, b = (y_lo, y_hi) if n % 2 == 0 else (y_hi, y_lo)
                px, py = to_page(x, a)
                page.mouse.move(px, py)
                page.mouse.down()
                for i in range(1, steps + 1):
                    yy = a + (b - a) * i // steps
                    qx, qy = to_page(x, yy)
                    page.mouse.move(qx, qy)
                    time.sleep(hold_ms / 1000.0 / steps)
                page.mouse.up()
                n += 1
                # Знімок у гущі руху — доказ для критерію 2.
                if name != "-" and shots < 12 and n % 2 == 1:
                    pageshot(f"{name}-{shots:02d}")
                    shots += 1
                time.sleep(gap_ms / 1000.0)
            return n, shots

        while True:
            with FIFO.open() as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    cmd, *rest = line.split()
                    try:
                        if cmd == "quit":
                            log("зачиняю вікно")
                            browser.close()
                            return
                        elif cmd == "tap":
                            px, py = to_page(int(rest[0]), int(rest[1]))
                            page.mouse.move(px, py)
                            time.sleep(0.05)
                            page.mouse.down()
                            time.sleep(0.15)   # LVGL опитує сенсор раз на 30 мс
                            page.mouse.up()
                            log(f"дотик {rest[0]},{rest[1]}")
                        elif cmd == "click":
                            # Кнопка самого клієнта (не пульта): «Стан», «чек».
                            page.click(rest[0])
                            log(f"натиснуто {rest[0]}")
                        elif cmd == "baud":
                            # Перемикач швидкості каналу (задача 0017): вибір у
                            # списку + подія change, як від руки людини.
                            page.select_option("#baud", rest[0])
                            time.sleep(2.5)
                            log(f"швидкість {rest[0]}: "
                                + page.evaluate(
                                    "() => document.getElementById('baud').value"))
                        elif cmd == "back":
                            # Права кнопка миші = клавіша «назад» (RTN).
                            px, py = to_page(w // 2, h // 2)
                            page.mouse.move(px, py)
                            page.mouse.down(button="right")
                            time.sleep(0.15)
                            page.mouse.up(button="right")
                            log("назад (RTN)")
                        elif cmd == "wheel":
                            n = int(rest[0])
                            px, py = to_page(w // 2, h // 2)
                            page.mouse.move(px, py)
                            for _ in range(abs(n)):
                                page.mouse.wheel(0, 100 if n > 0 else -100)
                                time.sleep(0.12)
                            log(f"енкодер {n}")
                        elif cmd == "scroll":
                            sec, hz, name = float(rest[0]), float(rest[1]), rest[2]
                            px, py = to_page(w // 2, h // 2)
                            page.mouse.move(px, py)
                            t0, n, back, shots = time.time(), 0, False, 0
                            while time.time() - t0 < sec:
                                # Довгий пробіг в один бік: інакше виділення
                                # тупцює між двома сусідніми пунктами, список
                                # не гортається і дріт лишається майже вільним.
                                if n % 45 == 44:
                                    back = not back
                                page.mouse.wheel(0, -100 if back else 100)
                                n += 1
                                time.sleep(1.0 / hz)
                                # Знімок у самій гущі гортання — доказ 3.2.
                                if name != "-" and n % 7 == 3 and shots < 12:
                                    pageshot(f"{name}-{shots:02d}")
                                    shots += 1
                            log(f"гортання {sec} с: {n} клацань, {shots} знімків, {status()}")
                        elif cmd == "shot":
                            shot(rest[0])
                            log(f"знімок {rest[0]}  |  {status()}")
                        elif cmd == "pageshot":
                            pageshot(rest[0])
                            log(f"знімок сторінки {rest[0]}  |  {wait_mode()}  |  {status()}")
                        elif cmd == "burst":
                            n, ms, name = int(rest[0]), int(rest[1]), rest[2]
                            for i in range(n):
                                pageshot(f"{name}-{i:02d}")
                                time.sleep(ms / 1000.0)
                            log(f"черга {name}: {n} знімків")
                        elif cmd == "swipe":
                            sec, name = float(rest[0]), rest[1]
                            n, shots = swipe(sec, name)
                            log(f"протяг {sec} с: {n} жестів, {shots} знімків, "
                                f"{status()}")
                        elif cmd == "dragrule":
                            # ⚠️ Вимикач правила протягу — прилад **сліпого**
                            # порівняння (критерій 2.3). Кнопки в панелі він не
                            # має навмисно: видима кнопка сказала б людині, який
                            # режим увімкнено, а саме цього знання вона й не
                            # повинна мати.
                            on = rest[0] in ("on", "1", "true")
                            got = page.evaluate(
                                f"() => window.remoteUiDragRule({str(on).lower()})")
                            log(f"правило протягу: {'увімкнене' if got else 'ВИМКНЕНЕ'}"
                                " (лічильники обнулені)")
                        elif cmd == "panel":
                            log("панель:\n" + panel_text())
                        elif cmd == "wait":
                            time.sleep(int(rest[0]) / 1000.0)
                        elif cmd == "info":
                            log("стан: " + status())
                        elif cmd == "say":
                            log("— " + " ".join(rest))
                        else:
                            log(f"невідома команда: {line}")
                    except Exception as e:  # вікно має пережити криву команду
                        log(f"помилка на «{line}»: {e}")


if __name__ == "__main__":
    main()
