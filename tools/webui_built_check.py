#!/usr/bin/env python3
"""Перевірити **зібраного** клієнта — того, що справді їде у флеш моста.

## Навіщо окремий інструмент

У прошивку потрапляє не `webui/*.js`, а мініфікований примірник (задача 0022,
критерій 8.3: сторінка на дроті 57.8 → 18.5 КБ gzip). Отже всі наші зелені
тести — `panels_test.js`, `app_test.js`, `proto_test.js` — перевіряють **не
той файл**, який поїде в пульт: вони читають джерело.

⚠️ **Умова людини від 2026-08-02, коли вона дозволяла мініфікацію:** прогін
Playwright робити проти зібраної сторінки, а не проти джерела, «інакше
зламане мініфікатором пройде повз зелені тести». Цей файл — виконання тієї
умови в такому вигляді, щоб її не забули наступного разу.

Обґрунтування, чому мініфікація не має ламати наш код, лежить у
`webui/minify.mjs`. Тут воно свідомо **не переказується**: цей файл існує
рівно тому, що «безпечно за побудовою» не рахується за доказ. До того ж
перший варіант того обґрунтування виявився хибним (стверджував, що всі
модулі загорнуті в IIFE — `app.js` не загорнутий), і переказ хибного
твердження був би третім його примірником.

## Що саме перевіряється

⚠️ **Двома шарами, і другий з'явився в задачі 0023.** Спершу інструмент
перевіряв лише те, що ламає мініфікатор; тепер він ще й **міряє розкладку й
кольори живої сторінки**. Причина — спільне правило доказу 0023: числа
знімаються обчисленими розмірами й кольорами, а не оком по знімку. Знімок
каже «схоже на правду» — а критерій каже «дорівнює нулю» і «втричі більше».

**Шар перший — те, що ламає саме мініфікатор:**

* глобальні `RemoteUI`, `RemoteUIWait`, `RemoteUIPanels` не перейменовані;
* публічна поверхня (ключі об'єктів) ціла — `RemoteUIPanels.INTENT_UP`;
* панелі будуються з `HELLO`, а не з коду: склад і порядок кнопок;
* джойстик дає `enc`, клавіша дає пару «натиснуто / відпущено»;
* клавіатура: стрілка вниз → `enc`, `Esc` → `RTN`;
* вліво-вправо **не роблять нічого** (рішення людини від 03.08);
* жодної помилки JS за весь прогін.

**Шар другий — вигляд і розкладка (задача 0023), у двох орієнтаціях:**

* смужка стану згорнута **за замовчуванням** і не займає нічого;
* панелі однакові, стоять упритул до зображення, зображення по центру;
* подвійні відступи, ширина кнопок джойстика, чверть ручки під смужку;
* книжкова: одна колонка, три панелі в сумі дорівнюють ширині вікна;
* кольори тла, написів і двох порід кнопок;
* опис застосунку (`display: standalone`) і кнопка «на весь екран» — разом із
  тим, що там, де браузер повноекранного режиму не вміє, кнопки **немає**.

## Запуск

Типово інструмент піднімає власний стенд без заліза — двійник пульта
(`fake_radio.py`) плюс віддачу зібраної сторінки — і сам його гасить:

    python3 tools/webui_built_check.py

Проти живого моста (сторінка там уже зібрана прошивкою):

    python3 tools/webui_built_check.py --url http://192.168.4.1/

⚠️ **Мініфікація потрібна в обох режимах**, і в `--url` теж: там вона не для
того, щоб віддати, а щоб було **з чим звірити** те, що лежить у мості. Заразом
це ловить прошитий міст зі старою збіркою — корисно знати до заміру, а не
після.

⚠️ Потрібен Playwright — той самий, що для `webui_drive.py`:

    python3 -m venv pw && ./pw/bin/pip install playwright && ./pw/bin/playwright install chromium
    ./pw/bin/python tools/webui_built_check.py
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEBUI = os.path.join(ROOT, "webui")

# ⚠️ Має збігатися з `WEBUI_FILES` у firmware/esp32/main/CMakeLists.txt і з
# `MINIFY` + `COPY` у webui/minify.mjs. Новий файл клієнта додається в усіх
# трьох місцях плюс оголошення в `ws_bridge.c`.
ASSETS = ["index.html", "proto.js", "wait.js", "panels.js", "app.js", "style.css",
          "manifest.webmanifest", "icon.png"]


def die(msg, code=2):
    print(f"⛔ {msg}", file=sys.stderr)
    sys.exit(code)


class Checks:
    def __init__(self):
        self.failed = []
        self.total = 0

    def __call__(self, name, ok, detail=""):
        self.total += 1
        print(f"  [{'OK ' if ok else 'ПАД'}] {name}" + (f"  {detail}" if detail else ""))
        if not ok:
            self.failed.append(name)


def _json_field(path, *keys):
    """Дістати поле з JSON без залежностей. Вертає None, якщо шлях не знайдено."""
    try:
        with open(path, encoding="utf-8") as f:
            node = json.load(f)
    except (OSError, ValueError):
        return None
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node


def build(out_dir):
    """Мініфікувати клієнта тим самим скриптом, яким це робить збірка моста."""
    esbuild_pkg = os.path.join(WEBUI, "node_modules", "esbuild", "package.json")
    if not os.path.exists(esbuild_pkg):
        die("немає інструмента мініфікації — виконайте: pnpm -C webui install")

    # ⚠️ Звіряємо версію так само, як збірка моста. Інакше цей інструмент
    # мініфікував би одним esbuild, прошивка — іншим, і твердження «перевірено
    # те, що поїде у флеш» знову перестало б бути правдою.
    # ⚠️ Мовчазного пропуску тут бути не може. `_json_field` вертає `None` і на
    # відсутньому файлі, і на побитому JSON; при `if pinned and have` інструмент
    # у такому разі мініфікував би чим завгодно й друкував зелене — тоді як
    # CMake у тій самій ситуації дає `FATAL_ERROR`. Дві половини одного сторожа
    # розходились би рівно там, де інструмент обіцяє «те саме, що поїде у флеш».
    pinned = _json_field(os.path.join(WEBUI, "package.json"),
                         "devDependencies", "esbuild")
    have = _json_field(esbuild_pkg, "version")
    if pinned is None:
        die(f"не вдалось прочитати піновану версію esbuild з {WEBUI}/package.json")
    if have is None:
        die(f"не вдалось прочитати версію встановленого esbuild з {esbuild_pkg}")
    if pinned != have:
        die(f"версія esbuild розійшлася з пінованою: стоїть {have}, "
            f"очікується {pinned}\n"
            "   Полікувати:  pnpm -C webui install --frozen-lockfile")

    node = shutil.which("node")
    if not node:
        die("не знайдено node — потрібен для мініфікації клієнта")
    # ⚠️ Без `check=True`: інструмент навмисно вчився не показувати стеки —
    # `CalledProcessError` відсилав би читати трасування замість повідомлення.
    if subprocess.run([node, os.path.join(WEBUI, "minify.mjs"), out_dir]).returncode:
        die("мініфікація впала — дивіться повідомлення esbuild вище")


def assert_serving_built(url, built_dir):
    """⚠️ Довести, що на тому кінці — **наш зібраний** файл, а не якийсь інший.

    Головна пастка цього інструмента, знайдена рецензією: якщо порт уже
    зайнятий (стенд від попередньої сесії, віддача джерела), наші процеси
    мовчки помирають від `EADDRINUSE`, браузер іде на **чужий** сервер, і
    дев'ять перевірок із шістнадцяти зеленіють проти джерела — тобто проти
    рівно того файлу, задля відрізнення від якого інструмент і написаний.

    ⚠️ Друга рецензія показала, що першого виправлення було мало: гілка
    `--url` цієї перевірки не робила взагалі, і `--url` на стенд із джерелом
    так само бадьоро казав «зібрана сторінка ціла». Тому перевірка тепер
    спільна для обох гілок.

    Перевірка позитивна, а не «здається, працює»: беремо байти з мережі й
    звіряємо з байтами на диску. Це заразом ловить друкарську помилку в
    `--dir`, чужий сервер, що відповів першим, і **прошитий міст із застарілим
    клієнтом** — останнє теж корисно знати до того, як міряти.
    """
    import gzip
    import urllib.request

    # ⚠️ Звіряються **всі** файли, а не лише `app.js`. Спершу дивився один — і
    # це одразу пропустило змінений `index.html`: правки, що не чіпають
    # найбільший файл, лишались невидимими саме для того сторожа, який має їх
    # ловити.
    bad = []
    for name in ASSETS:
        try:
            with open(os.path.join(built_dir, name), "rb") as f:
                want = f.read()
        except OSError as e:
            die(f"немає зібраного {name}: {e}")
        req = urllib.request.Request(url.rstrip("/") + "/" + name,
                                     headers={"Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                got = r.read()
                # ⚠️ Справжній міст ставить `Content-Encoding: gzip`
                # **безумовно** (`ws_bridge.c`, `send_blob`), а `urllib` сам
                # не розпаковує.
                if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                    got = gzip.decompress(got)
        except Exception as e:
            die(f"не вдалось забрати /{name} з {url}: {e}")
        if got != want:
            bad.append(f"{name}: {len(got)} Б проти {len(want)} Б зібраних")

    if bad:
        die(f"на {url} віддається НЕ той клієнт, що зібраний зараз:\n"
            + "".join(f"     {b}\n" for b in bad)
            + "   Якщо це стенд на ПК — найімовірніше порт зайнятий іншим:\n"
              "     pkill -f webui_serve.py; pkill -f fake_radio.py\n"
              "   Якщо це живий міст — у ньому лежить інша збірка клієнта:\n"
              "     cd firmware/esp32 && idf.py -p /dev/ttyUSB0 flash")


# --------------------------------------------------------- вигляд і розкладка
#
# ⚠️ Спільне правило доказу задачі 0023: числа знімаються з **живої сторінки**
# обчисленими розмірами й кольорами, а не оком по знімку. Знімок каже «схоже на
# правду», а тут потрібне «дорівнює нулю» і «втричі більше».
#
# Обидві орієнтації міряються тим самим кодом: вікно 900×420 і 420×860.

LANDSCAPE = {"width": 900, "height": 420}
PORTRAIT = {"width": 420, "height": 860}

# Кольори з `webui/style.css`, записані так, як їх вертає `getComputedStyle`.
NEON = "rgb(255, 125, 26)"
WIDE_BG = "rgb(194, 98, 15)"
BLACK = "rgb(0, 0, 0)"

MEASURE_JS = r"""
() => {
  const g = (s) => document.querySelector(s);
  const box = (el) => {
    if (!el) return null;
    const b = el.getBoundingClientRect();
    return {w: b.width, h: b.height, l: b.left, r: b.right, t: b.top, b: b.bottom};
  };
  const css = (el, prop) => el ? getComputedStyle(el)[prop] : null;
  const list = (sel) => [...document.querySelectorAll(sel)].map((el) => Object.assign(
      {label: el.textContent.trim(), wide: el.classList.contains('key-wide')}, box(el)));
  const bar = g('#bar');
  const seen = (id) => {
    const el = document.getElementById(id);
    return !!(el && el.getClientRects().length);
  };
  return {
    win: {w: window.innerWidth, h: window.innerHeight},
    stage: box(g('#stage')),
    bar: box(bar),
    barCollapsed: bar.classList.contains('collapsed'),
    padL: box(g('#pad-left')),
    padR: box(g('#pad-right')),
    rail: box(g('#pad-left .pad-rail')),
    padToggle: box(g('#pad-left-toggle')),
    barToggle: box(g('#bar-toggle-left')),
    canvas: box(g('#screen')),
    keysL: list('#pad-left-body button.key'),
    keysR: list('#pad-right-body button.key'),
    stick: [...document.querySelectorAll('.stick-btn')].map(
        (el) => Object.assign({cls: el.className}, box(el))),
    color: {
      padL: css(g('#pad-left'), 'backgroundColor'),
      padR: css(g('#pad-right'), 'backgroundColor'),
      bar: css(bar, 'backgroundColor'),
      narrowText: css(g('button.key:not(.key-wide)'), 'color'),
      narrowBg: css(g('button.key:not(.key-wide)'), 'backgroundColor'),
      wideText: css(g('.key-wide'), 'color'),
      wideBg: css(g('.key-wide'), 'backgroundColor'),
      stickText: css(g('.stick-btn'), 'color'),
    },
    hasFullscreenButton: !!g('#btn-full'),
    chipsSeen: {
      heap: seen('heap'), fps: seen('fps'), focus: seen('focus'),
      baud: seen('baud-wrap'),
    },
  };
}
"""


def measure(page):
    return page.evaluate(MEASURE_JS)


def near(a, b, eps=1.5):
    return abs(a - b) <= eps


def set_bar(page, collapsed):
    """Згорнути або розгорнути смужку стану — рівно одним торканням ручки."""
    if measure(page)["barCollapsed"] != collapsed:
        page.click("#bar-toggle-left")
        time.sleep(0.35)


def gaps(keys):
    """Проміжки між сусідніми кнопками стовпчика, згори вниз."""
    return [round(keys[i + 1]["t"] - keys[i]["b"], 1) for i in range(len(keys) - 1)]


# ⚠️ Перевірки нижче припускають, що перша кнопка лівої панелі — `SYS`, а
# остання правої — `MDL`. Це припущення **інструмента**, не клієнта: сам
# клієнт так само будує панелі з `HELLO` (`panels_test.js` про чужий пульт
# лишається зеленим). На пульті з іншим набором клавіш падати має саме ця
# перевірка, а не панелі, — і шукати треба тут, а не в `panels.js`.


def open_page(browser, url, viewport):
    """Свіже вікно з ловцем помилок сторінки.

    ⚠️ Ловець тут не для повноти. Рецензія 0023 знайшла, що додаткові вікна
    (типовий стан смужки, браузер без повноекранного режиму) створювались без
    нього — тобто виняток саме на тих шляхах не побачив би ніхто.
    """
    ctx = browser.new_context(viewport=dict(viewport))
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(f"console.error: {m.text}")
            if m.type == "error" else None)
    page.goto(url, wait_until="load", timeout=25000)
    page.wait_for_function(
        "() => document.getElementById('screen').width > 100", timeout=25000)
    time.sleep(1.0)
    return ctx, page, errors


def check_resize_sweep(browser, url, chk):
    """⚠️ Звуження вікна — заміряна вада, а не гіпотеза.

    Рецензія 0023: при звуженні сітка стискала панелі під **ще не виправлену**
    ширину зображення, `fitCanvas` читав стиснуте число й лишав зображення
    завеликим. Заміряно на 900 → 760: клавіші по 14 пікселів замість 84, на
    640 — панелі по нулю й горизонтальна прокрутка.

    ⚠️ Прогін іде у **свіжому** вікні зі згорнутою смужкою, тобто в типовому
    стані. Перевірка 2.5 звужувала вікно й казала `[OK]` рівно тому, що йшла
    після інших: смужка вже була розгорнута, картинку обмежувала висота, і до
    стискання по ширині справа не доходила.
    """
    ctx, page, errors = open_page(browser, url, LANDSCAPE)
    try:
        base = measure(page)
        want_pad = round(base["padL"]["w"], 1)
        want_key = round(base["keysL"][0]["w"], 1) if base["keysL"] else 0
        bad = []
        for w in (900, 760, 640, 520, 700, 900):
            page.set_viewport_size({"width": w, "height": 420})
            time.sleep(0.35)
            m = measure(page)
            key = round(m["keysL"][0]["w"], 1) if m["keysL"] else 0
            over = page.evaluate("() => document.documentElement.scrollWidth >"
                                 " window.innerWidth")
            if (not near(m["padL"]["w"], want_pad) or not near(m["padR"]["w"], want_pad)
                    or not near(key, want_key) or over):
                bad.append(f"{w}: панелі {round(m['padL']['w'])}/{round(m['padR']['w'])}, "
                           f"клавіша {key}" + (", переповнення" if over else ""))
        chk("2.4 звуження вікна не стискає панелі й не дає прокрутки", not bad,
            "; ".join(bad) if bad else f"панель {want_pad}, клавіша {want_key} на всіх "
                                       "ширинах 900…520")
        # ⚠️ Одне абсолютне число на весь розділ розкладки. Решта перевірок
        # звіряє частини сторінки між собою (панель із панеллю, клавішу з
        # клавішею), тож `--key-w: 8px` лишив би їх усі зеленими — знайдено
        # рецензією. 44 px — та сама ціль для пальця, що вже стоїть у
        # `.key { min-height: 44px }`.
        chk("2.1 клавіша не вужча за ціль для пальця", want_key >= 44,
            f"{want_key} px при межі 44")
        # ⚠️ Межа, нижче якої панелі однак не поміщаються: 2 × 110 + мінімум
        # під картинку ≈ 268 px ширини вікна. Жоден телефон в альбомній не
        # буває вужчим за 480, тому прогін нижче 520 не спускається.
        chk("сторінка без помилок JS (звуження вікна)", not errors, "; ".join(errors[:3]))
    finally:
        ctx.close()


def check_heap_watchdog(browser, url, chk):
    """Сторож пам'яті моста з 0021 не сховався разом зі смужкою.

    ⚠️ Низька купа підставляється **підміною відповіді моста**, а не полем у
    двійнику: у двійника справжньої купи немає, і постійне «мало пам'яті»
    зробило б решту перевірок несхожими на роботу. Тут перевіряється рівно те,
    що робить клієнт, коли міст каже, що пам'ять скінчилась.
    """
    ctx, page, errors = open_page(browser, url, LANDSCAPE)
    try:
        chk("сторож: смужка починає згорнутою", measure(page)["barCollapsed"])

        def low(route):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"heap": {"ceiling": 262144, "warn_at": 32768,
                                                    "min_window": 12000}}))

        page.route("**/api/stats", low)

        # ⚠️ Чекати треба **викликом Playwright**, а не `time.sleep`. У
        # синхронному Playwright перехоплювач запиту виконується лише тоді,
        # коли головна нитка всередині його виклику; на звичайному сні запит
        # просто висить неперехоплений, клієнт низької купи не бачить — і
        # перевірка падає на справному коді. Спіймано на собі.
        opened = True
        try:
            page.wait_for_function(
                "() => !document.getElementById('bar').classList.contains('collapsed')",
                timeout=8000)
        except Exception:
            opened = False
        chk("сторож: пам'ять упала нижче межі — смужка відчинилась сама", opened)

        # ⚠️ Друга половина правила, і без неї перша шкідлива: смужку, яку
        # відчиняють щосекунди, закрити неможливо саме тоді, коли людина
        # дивиться на пульт.
        page.click("#bar-toggle-left")
        for _ in range(6):
            page.wait_for_timeout(500)
        chk("сторож: відчиняється раз на подію, а не щосекунди",
            measure(page)["barCollapsed"])

        # ⚠️ Блимання зв'язку — не нова тривога. Низька купа моста береться від
        # тиску трафіку, і **той самий тиск зриває опитування**: якби засувка
        # знімалась першою пропущеною відповіддю, смужка ставала б незакривною
        # саме під навантаженням. Заміряно рецензією на першому виправленні.
        page.unroute("**/api/stats")
        page.route("**/api/stats", lambda r: r.abort())
        page.wait_for_timeout(2000)
        page.unroute("**/api/stats")
        page.route("**/api/stats", low)
        for _ in range(6):
            page.wait_for_timeout(500)
        chk("сторож: блимання зв'язку не подає тривогу наново",
            measure(page)["barCollapsed"])

        # ⚠️ А довге мовчання — подає: це вже не блимання, а перезапуск моста
        # або відхід телефона з мережі. Без цієї другої половини перша
        # перетворилась би на «тривога подається один раз за життя вкладки».
        page.unroute("**/api/stats")
        page.route("**/api/stats", lambda r: r.abort())
        page.wait_for_timeout(7000)
        page.unroute("**/api/stats")
        page.route("**/api/stats", low)
        rearmed = True
        try:
            page.wait_for_function(
                "() => !document.getElementById('bar').classList.contains('collapsed')",
                timeout=8000)
        except Exception:
            rearmed = False
        chk("сторож: після довгого мовчання тривога подається наново", rearmed)

        # ⚠️ Те саме блимання ще раз, уже **після** довгого мовчання. Без цього
        # кроку не перевіреним лишається скидання лічильника при живому мості:
        # мутаційний прогін показав, що прибрати його можна, і всі перевірки
        # лишаються зеленими. У житті нескинутий лічильник повз би вгору за
        # години роботи на хиткому Wi-Fi і тихо повернув би ваду з блиманням.
        page.click("#bar-toggle-left")
        page.wait_for_timeout(500)
        page.unroute("**/api/stats")
        page.route("**/api/stats", lambda r: r.abort())
        page.wait_for_timeout(2000)
        page.unroute("**/api/stats")
        page.route("**/api/stats", low)
        for _ in range(6):
            page.wait_for_timeout(500)
        chk("сторож: лічильник мовчання скидається живим мостом",
            measure(page)["barCollapsed"])
        page.unroute("**/api/stats")

        # ⚠️ Обірвані запити ми влаштували самі, і браузер про кожен пише в
        # журнал. Це не помилка сторінки: `pollBridge` їх ловить і саме на них
        # і розрахований. Відсіюємо рівно цей рядок, решту — ні.
        ours = [e for e in errors if "Failed to load resource" not in e]
        chk("сторінка без помилок JS (сторож пам'яті)", not ours, "; ".join(ours[:3]))
    finally:
        ctx.close()


def check_icon_reproducible(chk, built_dir):
    """Значок у git має збігатися з тим, що дає його генератор.

    ⚠️ Це другий примірник даних у репозиторії — рівно те, від чого застерігає
    найперший коментар `firmware/esp32/main/CMakeLists.txt`. Двійник без
    сторожа розходиться з джерелом мовчки.
    """
    out = os.path.join(built_dir, "icon-check.png")
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "make_icon.py"), out],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if r.returncode:
        chk("1.4 значок відтворюється генератором", False,
            r.stderr.decode("utf-8", "replace")[:200])
        return
    with open(out, "rb") as f:
        made = f.read()
    with open(os.path.join(WEBUI, "icon.png"), "rb") as f:
        stored = f.read()
    chk("1.4 значок у git збігається з тим, що дає tools/make_icon.py",
        made == stored, f"{len(made)} Б проти {len(stored)} Б")


def check_default_bar(browser, url, chk, viewport, where):
    """Критерій 1.1 — смужка згорнута **за замовчуванням**.

    ⚠️ Вікно береться свіже, а не те, у якому йдуть решта перевірок, і це не
    зайва обережність: стан панелей клієнт пам'ятає між завантаженнями
    (`localStorage`, задача 0022). У вікні, де смужку вже хтось відчиняв,
    «згорнута за замовчуванням» довелося б спершу згорнути руками — тобто
    перевірка міряла б власний клац, а не типовий стан.
    """
    ctx, page, errors = open_page(browser, url, viewport)
    try:
        m = measure(page)
        chk(f"1.1 смужка згорнута за замовчуванням ({where})", m["barCollapsed"])
        chk(f"1.1 згорнута смужка не має висоти ({where})", near(m["bar"]["h"], 0),
            f"висота {m['bar']['h']} px")

        # ⚠️ «Не займає окремого ряду» доводиться в двох орієнтаціях **різними
        # числами**, бо ряду в них два різні.
        if where == "альбомна":
            # Тут смужка — справді власний ряд під панелями. Отже: у типовому
            # стані панель дістає до самого низу сцени, а варто смужку
            # відчинити — зображення на її висоту меншає. Друге число й робить
            # перевірку такою, що може провалитись: якби смужка займала ряд
            # завжди, різниці не було б.
            chk("1.1 згорнута смужка не займає окремого ряду (альбомна)",
                near(m["padL"]["b"], m["stage"]["b"]),
                f"низ панелі {round(m['padL']['b'], 1)}, "
                f"низ сцени {round(m['stage']['b'], 1)}")
            page.click("#bar-toggle-left")
            time.sleep(0.4)
            opened = measure(page)
            gain = m["canvas"]["h"] - opened["canvas"]["h"]
            chk("1.1 згорнута смужка віддає ту висоту зображенню (альбомна)",
                gain > 1 and opened["bar"]["h"] > 1,
                f"картинка {round(m['canvas']['h'])} проти "
                f"{round(opened['canvas']['h'])}, смужка {round(opened['bar']['h'])} px")
        else:
            # ⚠️ У книжковій різниці немає **за задумом**: смужка стоїть між
            # панелями (критерій 3.5), а не під ними, тобто бере місце, яке
            # інакше просто порожнє. Тому доказ інший: смужка нульова, і між
            # зображенням та панелями теж нічого.
            #
            # ⚠️ Межі цієї перевірки названі чесно, бо мутаційний прогін їх
            # показав: сам по собі **окремий ряд** під смужку вона не ловить
            # (ловлять 3.1 і 3.5) — вона падає, коли згорнута смужка лишає за
            # собою коробку. Разом із «смужка не має висоти» вище цього досить,
            # а обіцяти більше — те саме, що брехати в коментарі.
            chk("1.1 між зображенням і панелями нічого немає (книжкова)",
                near(m["canvas"]["b"], m["padL"]["t"]),
                f"проміжок {round(m['padL']['t'] - m['canvas']['b'], 1)} px")
        chk(f"сторінка без помилок JS (типовий стан, {where})", not errors,
            "; ".join(errors[:3]))
    finally:
        ctx.close()


def check_height_back(page, chk, where):
    """Критерій 1.3 — за одне торкання видно все, що показало свою вартість."""
    set_bar(page, False)
    m = measure(page)
    missing = [k for k, v in m["chipsSeen"].items() if not v]
    chk(f"1.3 за одне торкання видно пам'ять, швидкість, кадри, фокус ({where})",
        not missing, f"не видно: {missing}" if missing else "усі чотири")
    return m


def check_landscape(page, chk):
    """Розділ 2 (альбомна) плюс критерій 1.2."""
    m = check_height_back(page, chk, "альбомна")

    # 1.2 — смужка внизу, на всю ширину сцени.
    chk("1.2 смужка стану переїхала вниз",
        m["bar"]["t"] >= m["canvas"]["b"] - 1 and near(m["bar"]["b"], m["stage"]["b"]),
        f"верх смужки {m['bar']['t']}, низ картинки {m['canvas']['b']}")
    chk("1.2 смужка на всю ширину", near(m["bar"]["w"], m["stage"]["w"]),
        f"{m['bar']['w']} проти {m['stage']['w']}")

    # 2.1 — панелі однакової ширини.
    chk("2.1 ліва й права панелі однакової ширини",
        near(m["padL"]["w"], m["padR"]["w"]),
        f"{m['padL']['w']} проти {m['padR']['w']}")

    # 2.2 — подвійний відступ після SYS і після MDL.
    gl = gaps(m["keysL"])
    ok_l = len(gl) >= 2 and near(gl[0], 2 * gl[1], 1.0)
    chk("2.2 подвійний відступ після SYS", ok_l, f"проміжки лівої: {gl}")
    stick_top = min((s["t"] for s in m["stick"]), default=None)
    mdl = m["keysR"][-1] if m["keysR"] else None
    if mdl and stick_top is not None and gl:
        chk("2.2 подвійний відступ між MDL і джойстиком",
            near(stick_top - mdl["b"], 2 * gl[1], 1.5),
            f"{round(stick_top - mdl['b'], 1)} проти звичайного {gl[1]}")
    else:
        chk("2.2 подвійний відступ між MDL і джойстиком", False, "джойстика немає")

    # 2.3 — кнопка джойстика завширшки як MDL.
    if mdl and m["stick"]:
        widths = sorted({round(s["w"], 1) for s in m["stick"]})
        chk("2.3 кнопки джойстика завширшки як MDL",
            len(widths) == 1 and near(widths[0], mdl["w"]),
            f"джойстик {widths}, MDL {mdl['w']}")
        square = [s for s in m["stick"] if "st-mid" not in s["cls"]]
        chk("2.3 пропорції збережені: напрямки квадратні",
            all(near(s["w"], s["h"], 2) for s in square),
            f"{[(round(s['w']), round(s['h'])) for s in square]}")

    # 2.4 — панель упритул до зображення, без проміжку.
    chk("2.4 ліва панель упритул до зображення", near(m["padL"]["r"], m["canvas"]["l"]),
        f"проміжок {round(m['canvas']['l'] - m['padL']['r'], 1)} px")
    chk("2.4 права панель упритул до зображення", near(m["padR"]["l"], m["canvas"]["r"]),
        f"проміжок {round(m['padR']['l'] - m['canvas']['r'], 1)} px")

    # 2.6 — чверть ручки віддана смужці.
    ratio = m["padToggle"]["h"] / m["barToggle"]["h"] if m["barToggle"]["h"] else 0
    chk("2.6 ручка смужки — чверть висоти ручки панелі", near(ratio, 3, 0.15),
        f"панель {round(m['padToggle']['h'], 1)}, смужка {round(m['barToggle']['h'], 1)}")
    chk("2.6 обидві частини заповнюють ручку",
        near(m["padToggle"]["h"] + m["barToggle"]["h"], m["rail"]["h"], 2),
        f"сума {round(m['padToggle']['h'] + m['barToggle']['h'], 1)}, "
        f"ручка {round(m['rail']['h'], 1)}")


def check_centering(page, chk):
    """Критерій 2.5 — обидва випадки, на різних розмірах вікна."""
    # Місця вистачає: зображення стоїть по центру вікна.
    page.set_viewport_size({"width": 1200, "height": 420})
    time.sleep(0.4)
    m = measure(page)
    mid = (m["canvas"]["l"] + m["canvas"]["r"]) / 2
    chk("2.5 місця вистачає — зображення по центру", near(mid, m["win"]["w"] / 2, 2),
        f"центр картинки {round(mid, 1)}, центр вікна {m['win']['w'] / 2}")
    chk("2.5 і панелі при цьому впритул",
        near(m["padL"]["r"], m["canvas"]["l"]) and near(m["padR"]["l"], m["canvas"]["r"]))

    # Місця не вистачає, друга панель згорнута: панель тіснить зображення.
    #
    # ⚠️ Вікно тут вужче за звичайне (700, не 900) навмисно. При 900 місця
    # вистачає навіть зі згорнутою панеллю — картинку обмежує висота, а не
    # ширина, — і перевірка мовчки міряла б перший випадок удруге.
    page.set_viewport_size({"width": 700, "height": 420})
    page.click("#pad-right-toggle")
    time.sleep(0.4)
    m = measure(page)
    mid = (m["canvas"]["l"] + m["canvas"]["r"]) / 2
    chk("2.5 місця не вистачає — панель тіснить зображення",
        near(m["padL"]["l"], 0) and not near(mid, m["win"]["w"] / 2, 2),
        f"ліва панель від краю {round(m['padL']['l'], 1)}, "
        f"центр картинки {round(mid, 1)} проти {m['win']['w'] / 2}")
    chk("2.5 зображення не вилізло за краї",
        m["canvas"]["l"] >= m["padL"]["r"] - 1.5 and m["canvas"]["r"] <= m["padR"]["l"] + 1.5)
    page.click("#pad-right-toggle")
    page.set_viewport_size(dict(LANDSCAPE))
    time.sleep(0.4)


def check_portrait(page, chk):
    """Розділ 3 — книжкова орієнтація."""
    m = check_height_back(page, chk, "книжкова")

    # 3.1 — упритул до зображення, вільне місце над ним.
    chk("3.1 панель упритул до зображення знизу", near(m["padL"]["t"], m["canvas"]["b"]),
        f"проміжок {round(m['padL']['t'] - m['canvas']['b'], 1)} px")
    chk("3.1 над зображенням лишається вільне місце",
        m["canvas"]["t"] > m["stage"]["t"] + 1,
        f"{round(m['canvas']['t'] - m['stage']['t'], 1)} px")

    # 3.2 — усі кнопки в одну колонку.
    for side, keys in (("ліва", m["keysL"]), ("права", m["keysR"])):
        one_col = all(near(k["l"], keys[0]["l"]) for k in keys) and \
            all(keys[i + 1]["t"] >= keys[i]["b"] - 1 for i in range(len(keys) - 1))
        chk(f"3.2 {side} панель — одна колонка", bool(keys) and one_col,
            f"{[(k['label'], round(k['l']), round(k['t'])) for k in keys]}")

    # 3.3 — усі кнопки однакової ширини.
    widths = sorted({round(k["w"], 1) for k in m["keysL"] + m["keysR"]})
    chk("3.3 усі кнопки однакової ширини", len(widths) == 1, f"{widths}")

    # 3.4 — подвійні відступи від SYS і MDL.
    gl = gaps(m["keysL"])
    chk("3.4 подвійний відступ після SYS (книжкова)",
        len(gl) >= 2 and near(gl[0], 2 * gl[1], 1.0), f"{gl}")

    # 3.5 — три панелі в сумі дорівнюють ширині вікна.
    total = m["padL"]["w"] + m["bar"]["w"] + m["padR"]["w"]
    chk("3.5 ліва + смужка + права = ширина вікна", near(total, m["win"]["w"], 2),
        f"{round(m['padL']['w'], 1)} + {round(m['bar']['w'], 1)} + "
        f"{round(m['padR']['w'], 1)} = {round(total, 1)} при {m['win']['w']}")
    chk("3.5 смужка стоїть між панелями",
        near(m["bar"]["l"], m["padL"]["r"]) and near(m["bar"]["r"], m["padR"]["l"]))


def check_colors(page, chk, where):
    """Розділ 4 — кольори."""
    c = measure(page)["color"]
    chk(f"4.1 фон панелей цілком чорний ({where})",
        c["padL"] == BLACK and c["padR"] == BLACK and c["bar"] == BLACK,
        f"{c['padL']} / {c['padR']} / смужка {c['bar']}")
    chk(f"4.2 написи на кнопках — неоновий помаранчевий ({where})",
        c["narrowText"] == NEON and c["stickText"] == NEON,
        f"клавіші {c['narrowText']}, джойстик {c['stickText']}")
    chk(f"4.3 SYS і MDL — помаранчеві з чорним написом ({where})",
        c["wideBg"] == WIDE_BG and c["wideText"] == BLACK,
        f"тло {c['wideBg']}, напис {c['wideText']}")
    chk(f"4.4 різниця між групами лишилась помітною ({where})",
        c["wideBg"] != c["narrowBg"] and c["wideText"] != c["narrowText"],
        f"вузькі: {c['narrowBg']} / {c['narrowText']}; "
        f"широкі: {c['wideBg']} / {c['wideText']}")


def check_manifest(page, url, chk):
    """Критерій 1.4 — сторінка вміє запускатися без браузерної обгортки."""
    import gzip
    import urllib.parse
    import urllib.request

    def fetch(u):
        """⚠️ Розпакувати, якщо треба.

        Справжній міст ставить `Content-Encoding: gzip` **безумовно**
        (`ws_bridge.c`, `send_blob`), а `urllib` сам не розпаковує. Стенд без
        заліза віддає нестиснене — тому перевірка була зелена там і падала на
        живому мості з `'utf-8' codec can't decode byte 0x8b`. Той самий
        обхід уже стоїть в `assert_serving_built`; тут його бракувало.
        """
        req = urllib.request.Request(u, headers={"Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req, timeout=6) as r:
            body = r.read()
            enc = (r.headers.get("Content-Encoding") or "").lower()
            ctype = (r.headers.get("Content-Type") or "").split(";")[0]
        return (gzip.decompress(body) if enc == "gzip" else body), ctype

    href = page.evaluate("() => { const l = document.querySelector('link[rel=manifest]');"
                         " return l ? l.href : null; }")
    chk("1.4 сторінка оголошує опис застосунку", bool(href), f"{href}")
    if not href:
        return
    try:
        body, ctype = fetch(href)
        man = json.loads(body.decode("utf-8"))
    except Exception as e:
        chk("1.4 опис застосунку віддається й розбирається", False, str(e))
        return

    chk("1.4 опис застосунку віддається й розбирається", True, f"тип {ctype}")
    chk("1.4 display = standalone", man.get("display") == "standalone",
        f"{man.get('display')}")
    chk("1.4 тип опису — application/manifest+json",
        ctype == "application/manifest+json", ctype)

    icons = man.get("icons") or []
    ok = False
    if icons:
        icon_url = urllib.parse.urljoin(href, icons[0].get("src", ""))
        try:
            ok = fetch(icon_url)[0].startswith(b"\x89PNG")
        except Exception:
            ok = False
    # ⚠️ Значок не оздоблення: без нього Chrome на Android робить не застосунок,
    # а звичайну закладку — вона відкриється в тій самій обгортці з адресним
    # рядком, тобто рівно в тому, від чого критерій 1.4 і рятує.
    chk("1.4 значок віддається (без нього Android робить закладку, а не застосунок)", ok)


def check_fullscreen(page, chk):
    """Критерій 1.5, перша половина: кнопка є і працює там, де браузер уміє."""
    set_bar(page, False)   # кнопка живе у смужці, а та згорнута за замовчуванням
    supported = page.evaluate("() => !!(document.fullscreenEnabled ||"
                              " document.webkitFullscreenEnabled)")
    has = measure(page)["hasFullscreenButton"]
    chk("1.5 кнопка «на весь екран» є там, де браузер уміє", supported and has,
        f"браузер уміє: {supported}, кнопка: {has}")
    if not (supported and has):
        return
    page.click("#btn-full")
    time.sleep(0.6)
    on = page.evaluate("() => !!(document.fullscreenElement ||"
                       " document.webkitFullscreenElement)")
    chk("1.5 кнопка справді розгортає на весь екран", on)
    if on:
        page.evaluate("() => document.exitFullscreen && document.exitFullscreen()")
        time.sleep(0.4)


def check_no_fullscreen(browser, url, chk):
    """Критерій 1.5, друга половина: де браузер не вміє, кнопки немає.

    ⚠️ Перевіряється в **окремому вікні**, якому підміняється оголошення
    підтримки. Інакше цю половину не перевірити взагалі: Chromium уміє
    повноекранний режим завжди, а пристроїв Apple у нас на столі немає.
    """
    ctx = browser.new_context(viewport=dict(LANDSCAPE))
    ctx.add_init_script("""
      Object.defineProperty(document, 'fullscreenEnabled', {get: () => false});
      Object.defineProperty(document, 'webkitFullscreenEnabled', {get: () => false});
    """)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(f"console.error: {m.text}")
            if m.type == "error" else None)
    try:
        page.goto(url, wait_until="load", timeout=25000)
        page.wait_for_function(
            "() => document.getElementById('screen').width > 100", timeout=25000)
        time.sleep(1.0)
        chk("1.5 браузер не вміє — кнопки немає, а не мертва",
            page.evaluate("() => !document.getElementById('btn-full')"))
        chk("сторінка без помилок JS (браузер без повноекранного режиму)",
            not errors, "; ".join(errors[:3]))
    finally:
        ctx.close()


def events(log_path, kind=None):
    out = []
    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                d = json.loads(line)
                if kind is None or d.get("kind") == kind:
                    out.append(d)
    except FileNotFoundError:
        pass
    return out


def run_checks(url, log_path, chk):
    from playwright.sync_api import sync_playwright

    def acts():
        # ⚠️ `state` — періодичний повтор стану раз на 250 мс, він іде завжди.
        # Рахувати його як дію означало б, що перевірка «вліво нічого не
        # робить» падає від самого плину часу.
        return [d for d in events(log_path) if d.get("kind") in ("key", "enc", "touch")]

    with sync_playwright() as p:
        br = p.chromium.launch()
        try:
            page = br.new_context(viewport=dict(LANDSCAPE)).new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(f"console.error: {m.text}")
                    if m.type == "error" else None)

            page.goto(url, wait_until="load", timeout=25000)
            page.wait_for_function(
                "() => document.getElementById('screen').width > 100", timeout=25000)
            time.sleep(3)

            chk("сторінка без помилок JS", not errors, "; ".join(errors[:3]))

            for g in ("RemoteUI", "RemoteUIWait", "RemoteUIPanels"):
                chk(f"глобальний {g} не перейменований",
                    page.evaluate(f"typeof window.{g} !== 'undefined'"))
            chk("публічна поверхня ціла (RemoteUIPanels.INTENT_UP)",
                page.evaluate("RemoteUIPanels.INTENT_UP === 'up'"))
            chk("публічна поверхня ціла (RemoteUIWait.WAIT_ALWAYS)",
                page.evaluate("RemoteUIWait.WAIT_ALWAYS === 2"))

            btns = page.eval_on_selector_all(
                "button.key", "e => e.map(x => x.textContent.trim())")
            chk("панелі побудовані з HELLO", len(btns) >= 5, f"{btns}")
            wide = page.eval_on_selector_all(
                "button.key.key-wide", "e => e.map(x => x.textContent.trim())")
            chk("широкі кнопки виділені окремо", len(wide) >= 1, f"{wide}")

            stick = page.eval_on_selector_all(".stick-btn", "e => e.map(x => x.className)")
            chk("джойстик побудований", len(stick) >= 3, f"{len(stick)} кнопок")

            if log_path:
                n = len(events(log_path, "enc"))
                page.click(".stick-btn.st-up")
                time.sleep(1.0)
                chk("джойстик вгору → enc", len(events(log_path, "enc")) > n)

                # ⚠️ Якщо панель не збудувалась, `btns` порожній. Без цієї
                # перевірки далі був би стек `IndexError` замість чесного
                # «ПАД» — а стек відсилає шукати ваду в інструменті.
                n = len(events(log_path, "key"))
                cand = [b for b in page.query_selector_all("button.key")
                        if (b.text_content() or "").strip()
                        == (btns[1] if len(btns) > 1 else btns[0])] if btns else []
                if not cand:
                    chk("клавіша: рівно натиснуто + відпущено", False,
                        "жодної кнопки клавіші не знайдено")
                    chk("клавіша не лишилась натиснутою", False, "нічого натискати")
                else:
                    box = cand[0].bounding_box()
                    page.mouse.move(box["x"] + box["width"] / 2,
                                    box["y"] + box["height"] / 2)
                    page.mouse.down()
                    time.sleep(0.6)
                    page.mouse.up()
                    time.sleep(0.8)
                    new = events(log_path, "key")[n:]
                    chk("клавіша: рівно натиснуто + відпущено", len(new) == 2, f"{new}")
                    chk("клавіша не лишилась натиснутою",
                        len(new) == 2 and new[0]["pressed"] is True
                        and new[1]["pressed"] is False)

                page.click("canvas")
                n = len(events(log_path, "enc"))
                page.keyboard.press("ArrowDown")
                time.sleep(0.8)
                chk("клавіатура: стрілка вниз → enc", len(events(log_path, "enc")) > n)

                n = len(events(log_path, "key"))
                page.keyboard.press("Escape")
                time.sleep(0.8)
                chk("клавіатура: Esc → клавіша «назад»",
                    len(events(log_path, "key")) > n)

                n = len(acts())
                page.keyboard.press("ArrowLeft")
                page.keyboard.press("ArrowRight")
                time.sleep(1.0)
                chk("вліво/вправо не роблять нічого", len(acts()) == n,
                    f"дій додалось: {len(acts()) - n}")

            # --- вигляд і розкладка (задача 0023) --------------------------
            #
            # ⚠️ Ідуть після перевірок поведінки навмисно: ті клацають по
            # кнопках і покладаються на типовий стан панелей, а ці його
            # міняють — згортають смужку, складають праву панель, крутять
            # вікно. Зворотний порядок зробив би перевірки поведінки
            # залежними від того, чим скінчилась розкладка.
            check_manifest(page, url, chk)

            # ⚠️ Головну сторінку тут **відпускаємо**, і це не причісування.
            # Міст обслуговує рівно одного клієнта: новий WebSocket витісняє
            # попередній, той перепідключається й витісняє новий — і жоден не
            # доживає до `HELLO`. На стенді без заліза це не видно (двійник
            # відповідає миттєво), а проти живого моста прилад падав саме тут.
            # Тому свіжі вікна працюють, поки головне стоїть на `about:blank`.
            page.goto("about:blank", wait_until="load", timeout=10000)

            check_default_bar(br, url, chk, LANDSCAPE, "альбомна")
            check_default_bar(br, url, chk, PORTRAIT, "книжкова")
            check_resize_sweep(br, url, chk)
            check_heap_watchdog(br, url, chk)
            check_no_fullscreen(br, url, chk)

            page.set_viewport_size(dict(LANDSCAPE))
            page.goto(url, wait_until="load", timeout=25000)
            page.wait_for_function(
                "() => document.getElementById('screen').width > 100", timeout=25000)
            time.sleep(2)

            check_fullscreen(page, chk)
            check_landscape(page, chk)
            check_colors(page, chk, "альбомна")
            check_centering(page, chk)

            page.set_viewport_size(dict(PORTRAIT))
            time.sleep(0.5)
            check_portrait(page, chk)
            check_colors(page, chk, "книжкова")

            chk("сторінка без помилок JS (у кінці)", not errors, "; ".join(errors[:3]))
        finally:
            br.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", help="перевіряти вже запущену ціль (напр. живий міст); "
                                  "без цього піднімається власний стенд без заліза")
    ap.add_argument("--keep", action="store_true", help="не прибирати теку зі зібраним")
    ap.add_argument("--port", type=int, default=8080, help="порт стенда (типово 8080)")
    args = ap.parse_args()

    # ⚠️ При перенаправленні у файл `stdout` буферизується блоками, а `die()`
    # пише в `stderr` — і діагноз виходив **раніше** за рядок, який каже, на
    # чому саме впало. Порядок повідомлень про помилку не має залежати від
    # того, дивиться людина в екран чи в журнал.
    sys.stdout.reconfigure(line_buffering=True)

    chk = Checks()
    tmp = tempfile.mkdtemp(prefix="webui-built-")
    procs = []
    try:
        if args.url:
            # ⚠️ Збираємо й **тут теж** — не щоб віддати, а щоб було з чим
            # звірити. Без цього гілка `--url` називала зібраним що завгодно,
            # включно з джерелом: рівно та вада, заради якої інструмент є.
            built = os.path.join(tmp, "built")
            print("збираю клієнта для звірки…")
            build(built)
            check_icon_reproducible(chk, built)
            print(f"перевіряю живу ціль: {args.url}")
            assert_serving_built(args.url, built)
            print("на тому кінці справді наш зібраний клієнт\n")
            # Журналу вводу немає — двійника пульта в цьому режимі не існує,
            # тож перевірки вводу пропускаються.
            run_checks(args.url, None, chk)
        else:
            built = os.path.join(tmp, "built")
            print("збираю клієнта…")
            build(built)
            check_icon_reproducible(chk, built)
            log_path = os.path.join(tmp, "input.jsonl")
            print("піднімаю стенд без заліза (двійник пульта + віддача зібраного)…")

            def spawn(name, argv, wait_s):
                """⚠️ Помирати мовчки процесам не даємо.

                Порт міг бути зайнятий стендом від попередньої сесії; тоді
                `Popen` відпрацює, процес одразу впаде на `EADDRINUSE`, а
                перевірки підуть на **чужий** сервер. `stderr` іде у файл, і
                при падінні друкується — інакше діагноз загубиться.
                """
                err = os.path.join(tmp, f"{name}.err")
                # Через `with`, щоб дескриптор закривався тут, а не покладався
                # на лічильник посилань CPython.
                with open(err, "wb") as fh:
                    pr = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=fh)
                procs.append(pr)
                time.sleep(wait_s)
                if pr.poll() is not None:
                    tail = ""
                    try:
                        with open(err, encoding="utf-8", errors="replace") as f:
                            tail = "".join(f.readlines()[-8:])
                    except OSError:
                        pass
                    die(f"{name} не піднявся (код {pr.returncode}).\n{tail}\n"
                        f"   Найімовірніше порт {args.port} або 7616 уже зайнятий:\n"
                        "     pkill -f webui_serve.py; pkill -f fake_radio.py")

            spawn("fake_radio", [sys.executable,
                                 os.path.join(ROOT, "tools", "fake_radio.py"),
                                 "--log", log_path], 2)
            spawn("webui_serve", [sys.executable,
                                  os.path.join(ROOT, "tools", "webui_serve.py"),
                                  "--dir", built, "--port", str(args.port)], 3)

            url = f"http://127.0.0.1:{args.port}/"
            # ⚠️ І навіть після цього — не віримо, а звіряємо байти.
            assert_serving_built(url, built)
            print("на порті справді наш зібраний клієнт\n")
            run_checks(url, log_path, chk)
    finally:
        # ⚠️ Гасимо завжди — хоч по помилці, хоч по Ctrl-C. Пастка задачі 0020:
        # драйвер від попередньої сесії прожив 1 год 14 хв і мовчки ділив дріт
        # із наступним заміром.
        for pr in procs:
            pr.terminate()
        for pr in procs:
            try:
                pr.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pr.kill()
        if args.keep:
            print(f"\nзібране лишилось у {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    print()
    if chk.failed:
        print(f"⛔ ПРОВАЛЕНО {len(chk.failed)} із {chk.total}: {chk.failed}")
        return 1
    print(f"✅ зібрана сторінка ціла: перевірок {chk.total}, невдалих 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
