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

* смужка стану згорнута **за замовчуванням** і не займає власного ряду;
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
import re
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

# `--gap` зі `style.css` — те саме «мінімально» з критеріїв 7.4 і 8.4. ⚠️ Число
# тут не незалежне: воно повторює сталу оформлення, і при її зміні цей рядок
# треба міняти разом із нею. Заводити його змінною CSS і читати з живої
# сторінки означало б перевіряти сторінку нею ж самою.
GAP = 4
# Товщина бруска ручки згортання — одне число на обидві орієнтації (критерій
# 9.2, число назвав бос). В альбомній це ширина вертикального бруска, у
# книжковій — висота горизонтального ряду ручок.
RAIL = 26

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
  // «Однаковий вигляд» із критерію 8.2 — це перелік чисел, а не враження.
  const look = (el) => {
    if (!el) return null;
    const s = getComputedStyle(el);
    return {bg: s.backgroundColor, color: s.color, radius: s.borderRadius,
            font: s.fontSize + '/' + s.fontFamily};
  };
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
    padsCollapsed: {
      l: g('#pad-left').classList.contains('collapsed'),
      r: g('#pad-right').classList.contains('collapsed'),
    },
    padL: box(g('#pad-left')),
    padR: box(g('#pad-right')),
    rail: box(g('#pad-left .pad-rail')),
    bodyL: box(g('#pad-left-body')),
    padToggle: box(g('#pad-left-toggle')),
    barToggle: box(g('#bar-toggle-left')),
    // Розділ 8: ряд ручок згортання міряється цілком, а не з одного боку.
    padToggleR: box(g('#pad-right-toggle')),
    railR: box(g('#pad-right .pad-rail')),
    barToggleR: box(g('#bar-toggle-right')),
    look: {
      left: look(g('#pad-left-toggle')),
      mid: look(g('.bar-own-toggle')),
      right: look(g('#pad-right-toggle')),
    },
    canvas: box(g('#screen')),
    // Задача 0025: місце під зображення й вікно, яке в ньому стоїть, поки
    // кадру немає. `wrap` — сама зарезервована площа, `veil` — напис із
    // кнопками, тобто те, що бос бачить замість екрана.
    wrap: box(g('#screen-wrap')),
    veil: box(g('#veil')),
    keysL: list('#pad-left-body button.key'),
    keysR: list('#pad-right-body button.key'),
    stick: [...document.querySelectorAll('.stick-btn')].map(
        (el) => Object.assign({cls: el.className}, box(el))),
    color: {
      // Тло під полотном: поки кадру немає, полотно прозоре, і рівність тла
      // видно саме тут (задача 0025, критерій 1.4).
      wrap: css(g('#screen-wrap'), 'backgroundColor'),
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
    // Розділ 7. `barOwnVisible` — власна ручка смужки, яка є лише в книжковій.
    barOwn: box(g('.bar-own-toggle')),
    barRowVisible: (() => {
      const el = g('#bar-row');
      return !!(el && el.getClientRects().length);
    })(),
    barOwnVisible: (() => {
      const el = g('.bar-own-toggle');
      return !!(el && el.getClientRects().length);
    })(),
    railBarToggleVisible: (() => {
      const el = g('#bar-toggle-left');
      return !!(el && el.getClientRects().length);
    })(),
    focusRing: css(g('#screen-wrap'), 'boxShadow'),
    focusOutline: css(g('#screen-wrap'), 'outlineStyle'),
    focusChip: (g('#focus') || {}).textContent || '',
    focused: document.activeElement === g('#screen-wrap'),
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


def bar_toggle(page):
    """Ручка смужки, **видима зараз**.

    ⚠️ Їх три, і видимі різні: в альбомній — дві бічні (чверть від ручок
    панелей, критерій 2.6), у книжковій — власна ручка смужки (критерій 7.2).
    Клацати по схованій не можна: Playwright чекав би її появи до тайм-ауту.
    """
    for sel in ("#bar-toggle-mid", "#bar-toggle-left", "#bar-toggle-right"):
        el = page.query_selector(sel)
        if el and el.is_visible():
            return sel
    raise AssertionError("жодної видимої ручки смужки стану немає")


def set_bar(page, collapsed):
    """Згорнути або розгорнути смужку стану — рівно одним торканням ручки."""
    if measure(page)["barCollapsed"] != collapsed:
        page.click(bar_toggle(page))
        time.sleep(0.35)


def set_pad(page, side, collapsed):
    """Одна бічна панель у потрібний стан, не більш ніж одним клацом.

    ⚠️ Стан читається, а не припускається: клієнт пам'ятає його між
    завантаженнями (`localStorage`, задача 0022), тож «клацнути двічі, щоб
    напевно» дало б рівно протилежне тому, що потрібно.
    """
    if measure(page)["padsCollapsed"][side[0]] != collapsed:
        page.click(f"#pad-{side}-toggle")
        time.sleep(0.35)


def set_pads(page, collapsed):
    """Обидві бічні панелі в один і той самий стан."""
    for side in ("left", "right"):
        set_pad(page, side, collapsed)


def gaps(keys):
    """Проміжки між сусідніми кнопками стовпчика, згори вниз."""
    return [round(keys[i + 1]["t"] - keys[i]["b"], 1) for i in range(len(keys) - 1)]


# ⚠️ Перевірки нижче припускають, що перша кнопка лівої панелі — `SYS`, а
# остання правої — `MDL`. Це припущення **інструмента**, не клієнта: сам
# клієнт так само будує панелі з `HELLO` (`panels_test.js` про чужий пульт
# лишається зеленим). На пульті з іншим набором клавіш падати має саме ця
# перевірка, а не панелі, — і шукати треба тут, а не в `panels.js`.


# Помилки сторінки, спричинені **втратою пульта**, а не вадою клієнта.
#
# ⚠️ Міст обслуговує рівно одного клієнта, і той, у кого пульт забрали, бачить
# обрив сокета. Це його штатна поведінка, а не вада сторінки.
#
# ⚠️ Мовчки вони не зникають. `real_errors` вертає ще й скільки їх було, і це
# число друкується в діагностиці кожної такої перевірки: сховати обрив, який
# став масовим, цей фільтр не дає.
#
# ⚠️ **Після задачі 0024 витіснення без вимоги більше немає**, тож більшість
# цих обривів мала б зникнути. Фільтр лишається на два випадки, які нікуди не
# поділись: сокет, що його міст гасить услід за словом «зайнято», і той, у
# кого керування перейняли кнопкою. ⚠️ Ціна фільтра названа прямо: він глушить
# і `Failed to load resource`, тобто заразом сховав би невдале опитування
# `/api/status`. Помітно це стане в іншому місці: клієнт, який не бачить, що
# пульт звільнився, не пройде перевірок 2.3 і 2.4.
EVICTION_MARKS = (
    "Error during WebSocket handshake",
    "net::ERR_CONNECTION_RESET",
    "net::ERR_EMPTY_RESPONSE",
    "Failed to load resource",
)


def real_errors(errors):
    """(справжні помилки, скільки відкинуто як витіснення)."""
    ours = [e for e in errors if not any(m in e for m in EVICTION_MARKS)]
    return ours, len(errors) - len(ours)


def chk_no_page_errors(chk, name, errors):
    ours, evicted = real_errors(errors)
    chk(name, not ours,
        "; ".join(ours[:3]) + (f" (обривів від витіснення: {evicted})" if evicted else ""))


def bridge_stats(url):
    """Лічильники цілі — прямо з мережі, повз браузер.

    ⚠️ Повз браузер навмисно: питати їх зі сторінки, яка стоїть у черзі,
    означало б самому додати той трафік, відсутність якого міряємо.
    """
    import gzip
    import urllib.request

    req = urllib.request.Request(url.rstrip("/") + "/api/stats",
                                 headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=6) as r:
        body = r.read()
        if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
            body = gzip.decompress(body)
    return json.loads(body.decode("utf-8"))


def gate_shown(page):
    """Чи стоїть на сторінці вікно черги."""
    return page.evaluate("() => !document.getElementById('veil-buttons').hidden")


def screen_ready(page):
    return page.evaluate("() => document.getElementById('screen').width > 100")


def wait_ready(page, timeout=25000):
    """Дочекатись картинки, а на зайнятому пульті — свідомо перейняти керування.

    ⚠️ З'явилось у задачі 0024, і без нього прилад тепер не працює взагалі:
    доти новий клієнт витісняв попереднього мовчки, і прилад цим користувався,
    сам того не називаючи. Тепер зайнятий пульт відповідає «зайнято», і вікно
    приладу мусить натиснути ті самі дві кнопки, що й бос.

    ⚠️ Очікування спільне на два наслідки — картинка **або** вікно черги, — а
    не два очікування поспіль: інакше на вільному пульті кожне вікно платило б
    тайм-аутом за чергу, якої немає.

    :return: True, якщо керування довелось переймати.
    """
    page.wait_for_function(
        "() => document.getElementById('screen').width > 100"
        " || !document.getElementById('veil-buttons').hidden", timeout=timeout)
    if not gate_shown(page):
        return False

    page.click("#btn-take")
    page.wait_for_selector("#btn-take-yes:visible", timeout=5000)
    page.click("#btn-take-yes")
    page.wait_for_function(
        "() => document.getElementById('screen').width > 100", timeout=timeout)
    return True


def open_page(browser, url, viewport, tries=3):
    """Свіже вікно з ловцем помилок сторінки.

    ⚠️ Ловець тут не для повноти. Рецензія 0023 знайшла, що додаткові вікна
    (типовий стан смужки, браузер без повноекранного режиму) створювались без
    нього — тобто виняток саме на тих шляхах не побачив би ніхто.

    ⚠️ Очікування `HELLO` повторюється, і це не послаблення перевірки, а
    властивість живого моста: він обслуговує **рівно одного** клієнта, і слот
    попереднього вікна звільняється не в мить закриття контексту, а за тишею в
    сокеті. Проти моста прогін падав саме тут — на другому й третьому свіжому
    вікні, — тоді як одиничне вікно отримувало `HELLO` за 0.3 с. На стенді без
    заліза повтор не спрацьовує жодного разу: двійник пульта вітається одразу.

    ⚠️ **Після задачі 0024 повтор лишається, але на інший випадок.** Слот, який
    ще тримає мертве вікно, тепер не витісняється мовчки — на нього кажуть
    «зайнято», і `wait_ready` переймає керування кнопкою. Повтор потрібен там,
    де вікно черги не з'явилось і картинка теж (міст щойно піднявся).

    ⚠️ Якщо `HELLO` не приходить **зовсім**, повтор нічого не ховає: після
    останньої спроби виняток іде далі, і прогін падає, як падав.
    """
    ctx = browser.new_context(viewport=dict(viewport))
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(f"console.error: {m.text}")
            if m.type == "error" else None)
    page.goto(url, wait_until="load", timeout=25000)
    for attempt in range(tries):
        try:
            wait_ready(page)
            break
        except Exception:
            if attempt == tries - 1:
                raise
            print(f"   HELLO не прийшов за 25 с — перезавантажую вікно "
                  f"(спроба {attempt + 2} з {tries})")
            time.sleep(2.0)
            page.reload(wait_until="load", timeout=25000)
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
        chk_no_page_errors(chk, "сторінка без помилок JS (звуження вікна)", errors)
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
        page.click(bar_toggle(page))
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
        page.click(bar_toggle(page))
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
        chk_no_page_errors(chk, "сторінка без помилок JS (сторож пам'яті)", errors)
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
        if where == "альбомна":
            chk("1.1 згорнута смужка не має висоти (альбомна)",
                near(m["bar"]["h"], 0), f"висота {m['bar']['h']} px")
        else:
            # ⚠️ У книжковій згорнута смужка **не нульова**: у ній лишається
            # власна ручка (критерій 7.2). Але ряду вона не додає — стоїть у
            # тому самому, що й панелі, і нижча за них. Саме це й міряється:
            # «не займає окремого ряду», а не «дорівнює нулю».
            # ⚠️ Порівняння з висотою панелі — заслабке: запас там 287 px, і
            # мутація «показати згорнуту смужку цілком» лишалась зеленою.
            # Тому міряється те, що справді стверджується: у згорнутій видно
            # **саму ручку й нічого більше**.
            # ⚠️ Допуск тут — рівно два пікселі на округлення, і не більше.
            # Доти стояло `own_h + 2 * 4 + 2`, де вісімка була вертикальними
            # полями ручки; розділ 8 їх прибрав (`margin: 0 …`), і десять
            # пікселів мертвого запасу тихо лишились: смужка з `padding: 8px`
            # проходила б зеленою.
            own_h = m["barOwn"]["h"] if m["barOwn"] else 0
            chk("1.1 у згорнутій смужці видно саму ручку (книжкова)",
                not m["barRowVisible"] and own_h > 0
                and m["bar"]["h"] <= own_h + 2,
                f"смужка {round(m['bar']['h'])} px, ручка {round(own_h)} px, "
                f"чипи видно: {m['barRowVisible']}")

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
            page.click(bar_toggle(page))
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
        chk_no_page_errors(chk, f"сторінка без помилок JS (типовий стан, {where})", errors)
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
    # ⚠️ `+ GAP` тут з'явився разом із критерієм 8.4: між частинами бруска
    # тепер мінімальний відступ, інакше межу між ними видно лише в мить
    # натискання. Сума частин через це на чотири пікселі менша за брусок, і
    # перевірка про це знає — а не тримається на послабленому допуску.
    chk("2.6 обидві частини заповнюють ручку",
        near(m["padToggle"]["h"] + m["barToggle"]["h"] + GAP, m["rail"]["h"], 2),
        f"сума {round(m['padToggle']['h'] + m['barToggle']['h'], 1)} + проміжок {GAP}, "
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

    # 3.1 у редакції критерію 8.1.
    #
    # ⚠️ «Упритул» більше **не правило, а наслідок**: панель тіснить зображення
    # рівно тоді, коли їй бракує місця. Тому безумовне «упритул завжди» тут
    # стояти не може — і не тому, що ослабло, а тому, що воно неправда: варто
    # чужому пульту віддати в `HELLO` на одну клавішу менше, і панелі
    # перестануть діставати до центру. Стверджується сама диз'юнкція: або по
    # центру сцени, або низом упритул до панелі, третього стану немає.
    want = (m["stage"]["h"] - m["canvas"]["h"]) / 2
    top = m["canvas"]["t"] - m["stage"]["t"]
    centered = near(top, want, 2)
    squeezed = near(m["canvas"]["b"], m["padL"]["t"], 1.5) and top <= want + 2
    chk("3.1 зображення або по центру, або впритул до панелі — третього немає",
        centered or squeezed,
        f"згори {round(top, 1)} px при центрі {round(want, 1)}, "
        f"до панелі {round(m['padL']['t'] - m['canvas']['b'], 1)} px")
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


def check_portrait_centering(page, chk):
    """Критерій 8.1 — у книжковій зображення стоїть по центру вікна.

    ⚠️ Це не нова вимога, а **невиконаний критерій 3.1**: доказом його
    виконання були числа з альбомної орієнтації, де правило й так працювало.
    Тому вирішальний стан тут — **усі панелі згорнуті**. Доки вони розгорнуті,
    місця й справді бракує, зображення тісниться вгору — і «по центру» від
    «притиснуте до панелей» не відрізнити жодним числом.
    """
    set_bar(page, True)
    set_pads(page, True)
    time.sleep(0.4)
    m = measure(page)

    # ⚠️ Спершу — що сцена й вікно це одне й те саме. Без цього рядка відступ,
    # який з'явився б у `body` (а на iPhone `safe-area-inset-top` не нуль),
    # мовчки поглинувся б різницею між ними, і «по центру вікна» насправді
    # означало б «по центру сцени».
    chk("8.1 сцена займає все вікно",
        near(m["stage"]["t"], 0) and near(m["stage"]["h"], m["win"]["h"], 2),
        f"сцена {round(m['stage']['t'], 1)}…{round(m['stage']['b'], 1)} "
        f"при вікні {m['win']['h']}")

    top = round(m["canvas"]["t"], 1)
    bottom = round(m["win"]["h"] - m["canvas"]["b"], 1)
    chk("8.1 усі панелі згорнуті — відступи згори й знизу рівні",
        near(top, bottom, 2) and top > 1,
        f"згори {top} px, знизу {bottom} px")

    # ⚠️ Тут стояла перевірка «між зображенням і панеллю є проміжок» — доказ
    # того, що картинка відліпилась від панелей. **Критерій 9.1 її скасував**:
    # панелі знову прив'язані до нижнього краю зображення, тобто проміжок
    # дорівнює нулю за вимогою, а не за вадою. Твердження, заради якого вона
    # існувала (картинку вниз ніхто не тягне), перевіряє тепер
    # `check_pads_anchor` — інакше, через вільне місце **під** рядом панелей.
    #
    # ⚠️ Сама вада, від якої вона стерегла, лишається спійманою: картинка,
    # притиснута до панелей унизу вікна, дає різні відступи згори й знизу, і
    # це валить перевірку вище.

    # Друга половина правила: посунути зображення має право **лише** панель,
    # якій бракує місця.
    #
    # ⚠️ Вікно тут навмисно нижче за звичайне. При 860 місця бракує лише на
    # 29 пікселів — тобто перевірка міряла б не правило, а те, скільки клавіш
    # віддав `HELLO` двійника: чужий пульт із коротшою панеллю зробив би її
    # червоною на цілком справному коді. При 620 бракує на півтори сотні.
    page.set_viewport_size({"width": 420, "height": 620})
    set_pads(page, False)
    time.sleep(0.5)
    m = measure(page)
    mid = (m["canvas"]["t"] + m["canvas"]["b"]) / 2
    chk("8.1 розгорнута панель тіснить зображення вгору",
        mid < m["win"]["h"] / 2 - 2 and near(m["canvas"]["b"], m["padL"]["t"]),
        f"центр картинки {round(mid, 1)} проти {m['win']['h'] / 2}, "
        f"проміжок до панелі {round(m['padL']['t'] - m['canvas']['b'], 1)} px")
    page.set_viewport_size(dict(PORTRAIT))
    time.sleep(0.4)


def check_pads_anchor(page, chk):
    """Критерій 9.1 — панелі прив'язані до нижнього краю зображення.

    ⚠️ Це повернення поведінки, яку розділ 8 переписав, хоч ніхто не просив.
    Панелі розгорталися від нижнього краю **вікна**, і ручки згортання через
    це зависали на різній висоті: 816 пікселів при згорнутих панелях, 520 при
    розгорнутих, 588 при розгорнутій смужці. Між зображенням і ручками
    лишалась діра до 267 px.

    ⚠️ Двома різними твердженнями, і це не для повноти. Перше каже, що ручки
    приліплені **до картинки**; друге — що вони відліплені **від дна вікна**.
    Стара розкладка валила лише перше, а розкладка, де всю групу притиснуто
    донизу, валила б лише друге.
    """
    # ⚠️ Спершу — що розкладка взагалі **стоїть на місці**. Обчислення, у якому
    # доступна висота береться з ряду зображення, самозалежне: `fitCanvas`
    # читає власний попередній результат, і числа починають гуляти без упину.
    # Перша спроба зробити 9.1 була саме такою, і прогін падав не червоною
    # перевіркою, а тайм-аутом Playwright «element is not stable» — діагноз, за
    # яким причини не видно.
    set_pads(page, True)
    set_bar(page, True)
    time.sleep(0.5)
    first = measure(page)
    time.sleep(0.7)
    second = measure(page)
    moved = [round(abs(first[k]["t"] - second[k]["t"]), 1)
             for k in ("canvas", "padL", "padR", "bar")]
    chk("9.1 розкладка стоїть на місці, а не гуляє", max(moved) <= 1,
        f"зсув за 0.7 с: зображення {moved[0]}, панелі {moved[1]}/{moved[2]}, "
        f"смужка {moved[3]} px")

    states = (("усі згорнуті", True, True, True),
              ("усі розгорнуті", False, False, False),
              ("смужка розгорнута", True, True, False),
              ("одна згорнута", True, False, True))
    for name, left, right, bar in states:
        set_pad(page, "left", left)
        set_pad(page, "right", right)
        set_bar(page, bar)
        time.sleep(0.4)
        m = measure(page)
        tops = [round(t["t"], 1) for t in (m["padToggle"], m["barOwn"], m["padToggleR"])]
        bottom = round(m["canvas"]["b"], 1)
        chk(f"9.1 ручки згортання стоять одразу під зображенням ({name})",
            all(near(t, bottom, 1.5) for t in tops),
            f"низ зображення {bottom}, верх ручок {tops}")

        # ⚠️ Друге твердження має сенс лише там, де місця вдосталь: коли
        # панель розгорнута, вона законно впирається в дно вікна, і вимагати
        # від неї порожнього місця означало б вимагати протилежного 8.1.
        if left and right:
            room = round(m["win"]["h"] - max(m["padL"]["b"], m["padR"]["b"],
                                             m["bar"]["b"]), 1)
            chk(f"9.1 під панелями лишається вільне місце ({name})", room > 1,
                f"від низу панелей до низу вікна {room} px")

    # ⚠️ Остання перевірка тут — про **перерахунок**, а не про розкладку, і
    # без неї прив'язка трималася б на тому, що кожен шлях зміни висоти панелі
    # не забув покликати `fitCanvas` руками.
    #
    # Доти це гарантував спостерігач розміру: ряд зображення був `1fr`, тобто
    # ділив висоту з панелями, і будь-яка їхня зміна міняла його розмір.
    # Критерій 9.1 цей зв'язок розірвав навмисно — отже спостерігач має тепер
    # дивитись і на самі панелі. Тут це доводиться єдиним способом, який не
    # проходить повз нього: висота смужки міняється **без жодного клацання**,
    # прямо вмістом, як воно й буває, коли плашки переносяться на два рядки.
    # ⚠️ Дві спроби написати цю перевірку не працювали мовчки, і обидві спіймала
    # мутація, а не око — вона падала з нулем **у кожній** мутації, включно з
    # тими, до яких не мала стосунку:
    #
    #   1. ріс підпис пульта в смужці — плашки обрізаються
    #      (`text-overflow: ellipsis`), тож смужка від тексту не вищає;
    #   2. зонд ішов лише в **ліву** панель — а висоту ряду задає права, у ній
    #      джойстик (322 px проти 274). Ліва просто заповнювала вільне місце,
    #      і жодне число не рухалось.
    #
    # Тому зонд іде в обидві панелі: ряд росте, хоч би яка з них була вищою.
    set_pads(page, False)
    set_bar(page, True)
    time.sleep(0.4)
    before = measure(page)
    page.evaluate("""() => {
      for (const id of ['pad-left-body', 'pad-right-body']) {
        const b = document.createElement('button');
        b.className = 'key probe-key';
        b.textContent = 'ЗОНД';
        document.getElementById(id).appendChild(b);
      }
    }""")
    time.sleep(0.6)
    after = measure(page)
    tall = lambda m: max(m["padL"]["h"], m["padR"]["h"])
    grew = round(tall(after) - tall(before), 1)
    lifted = round(before["canvas"]["b"] - after["canvas"]["b"], 1)
    over = round(max(after["padL"]["b"], after["padR"]["b"]) - after["win"]["h"], 1)
    chk("9.1 панель виросла вмістом — розкладка перерахувалась сама",
        grew > 1 and lifted > 1 and over <= 1
        and near(after["canvas"]["b"], after["padToggle"]["t"], 1.5),
        f"панель виросла на {grew} px, зображення піднялось на {lifted} px, "
        f"низ панелі за вікном на {over} px, "
        f"низ зображення {round(after['canvas']['b'], 1)} проти верху ручки "
        f"{round(after['padToggle']['t'], 1)}")
    page.evaluate("() => document.querySelectorAll('.probe-key').forEach((e) => e.remove())")
    time.sleep(0.4)

    set_pads(page, False)
    set_bar(page, True)


def check_lift_leak(page, chk):
    """Критерій 9.1 — зсув книжкової не тече в альбомну.

    ⚠️ Ця перевірка з'явилась не з голови: мутація «зсув не знімається при
    поверненні в альбомну» пройшла **зеленою** через увесь прогін. Телефон
    повертають у руках, стилі лишаються від попередньої орієнтації, і зайві
    285 пікселів під панелями відірвали б смужку стану від низу вікна — а
    книжкові перевірки йдуть останніми й цього не бачать.
    """
    set_pads(page, True)
    set_bar(page, True)
    time.sleep(0.4)
    page.set_viewport_size(dict(LANDSCAPE))
    time.sleep(0.6)
    m = measure(page)
    below = round(m["win"]["h"] - max(m["padL"]["b"], m["padR"]["b"]), 1)
    chk("9.1 в альбомній під панелями порожнього місця немає", below <= 1.5,
        f"від низу панелей до низу вікна {below} px")
    page.set_viewport_size(dict(PORTRAIT))
    time.sleep(0.5)
    set_pads(page, False)


def check_rail_thickness(page, chk):
    """Критерій 9.2 — товщина бруска ручки одна на обидві орієнтації.

    ⚠️ Число знімається **з двох орієнтацій живої сторінки**, а не звіряється
    двічі в одній: в альбомній це ширина вертикального бруска, у книжковій —
    висота горизонтального ряду. Саме розходження цих двох чисел бос і назвав:
    висота в книжковій задавалась окремо й росла з кожним колом відгуку.

    ⚠️ Вікно повертається в книжкову тут само, а не покладається на те, що
    наступна перевірка його виставить.
    """
    page.set_viewport_size(dict(LANDSCAPE))
    time.sleep(0.5)
    land = measure(page)
    wide = [round(land["rail"]["w"], 1), round(land["railR"]["w"], 1)]

    page.set_viewport_size(dict(PORTRAIT))
    time.sleep(0.5)
    port = measure(page)
    tall = [round(t["h"], 1) for t in (port["padToggle"], port["barOwn"],
                                       port["padToggleR"])]

    chk("9.2 висота ручок у книжковій = ширина бруска в альбомній",
        all(near(h, wide[0], 1.5) for h in tall) and near(wide[0], wide[1], 1.5),
        f"книжкова {tall}, альбомна {wide}")
    # ⚠️ Абсолютне число окремо: рівність вище тримається на спільній змінній
    # `--rail`, тобто сама по собі переживе будь-яку її величину. Мутація
    # «--rail: 2px» валить рівно цю перевірку.
    chk("9.2 товщина бруска — те число, яке назвав бос",
        all(near(h, RAIL, 1.5) for h in tall) and near(wide[0], RAIL, 1.5),
        f"книжкова {tall}, альбомна {wide}, названо {RAIL}")


def check_toggle_row(page, chk):
    """Критерії 8.2–8.4 у книжковій — ряд із трьох ручок згортання.

    ⚠️ Кожне число знімається **тричі**: обидві згорнуті, обидві розгорнуті і
    **одна згорнута, друга ні**. Перші два стани — сама скарга боса: ручка
    згорнутої панелі всихала до ширини власної стрілки рівно тоді, коли по ній
    треба влучити, щоб панель повернути. Третій — найризикованіший для «в один
    ряд»: висоту ряду задає розгорнута сусідка, а брусок згорнутої мусить
    лишитись угорі, а не поїхати за нею.
    """
    widths = {}
    for state, left, right, bar in (("згорнуті", True, True, True),
                                    ("розгорнуті", False, False, False),
                                    ("одна згорнута", True, False, True)):
        set_bar(page, bar)
        set_pad(page, "left", left)
        set_pad(page, "right", right)
        time.sleep(0.4)
        m = measure(page)
        trio = [("ліва", m["padToggle"], m["padL"]),
                ("смужка", m["barOwn"], m["bar"]),
                ("права", m["padToggleR"], m["padR"])]

        # ⚠️ 8.2 виконується з точністю до видимої межі з 8.4, і це названо
        # вголос: панелі стоять упритул одна до одної (критерій 3.5), тож
        # проміжок між ручками може взятися лише з їхньої ж ширини. «Ширина
        # ручки = ширина панелі» і «між ручками є відступ» разом буквально
        # нездійсненні — вибрано видиму межу ціною чотирьох пікселів.
        bad = [f"{name}: ручка {round(t['w'], 1)}, панель {round(p['w'], 1)}"
               for name, t, p in trio if not near(t["w"] + GAP, p["w"], 1.5)]
        chk(f"8.2 кожна ручка завширшки зі свою панель ({state})", not bad,
            "; ".join(bad) if bad else
            f"ручки {[round(t['w']) for _, t, _ in trio]} при панелях "
            f"{[round(p['w']) for _, _, p in trio]}")

        tops = [round(t["t"], 1) for _, t, _ in trio]
        heights = [round(t["h"], 1) for _, t, _ in trio]
        chk(f"8.2 три ручки стоять в один ряд ({state})",
            max(tops) - min(tops) <= 1.5 and max(heights) - min(heights) <= 1.5,
            f"верх {tops}, висота {heights}")

        between = [round(m["barOwn"]["l"] - m["padToggle"]["r"], 1),
                   round(m["padToggleR"]["l"] - m["barOwn"]["r"], 1)]
        chk(f"8.4 між ручками мінімальний відступ ({state})",
            all(0 < g <= 8 for g in between), f"{between} px")

        widths[state] = [round(t["w"], 1) for _, t, _ in trio]

    chk("8.3 ширина ручок не залежить від того, згорнута панель",
        len({tuple(v) for v in widths.values()}) == 1,
        "; ".join(f"{k}: {v}" for k, v in widths.items()))

    look = measure(page)["look"]
    chk("8.2 три ручки мають однаковий вигляд",
        look["left"] == look["mid"] == look["right"],
        f"ліва {look['left']}, смужка {look['mid']}, права {look['right']}")

    # ⚠️ Стан повертаємо самі, а не покладаємось на те, що ця перевірка
    # остання: «останньою» вона лишається рівно до наступної правки прогону.
    set_pads(page, False)
    set_bar(page, True)


def check_toggle_row_landscape(page, chk):
    """Критерії 8.3 і 8.4 в альбомній.

    ⚠️ Ряду з трьох тут немає **за задумом**: власної ручки смужка в альбомній
    не має (критерій 2.6), а бічні поділені по висоті на панель і смужку. Тому
    міряється те, що в цій орієнтації взагалі має сенс, — видима межа між
    частинами бруска (8.4 прямо каже «в обох орієнтаціях») і незмінна ширина
    ручки при згортанні панелі (8.3).
    """
    widths = {}
    for state, collapsed in (("згорнуті", True), ("розгорнуті", False)):
        set_pads(page, collapsed)
        time.sleep(0.4)
        m = measure(page)
        between = [round(m["barToggle"]["t"] - m["padToggle"]["b"], 1),
                   round(m["barToggleR"]["t"] - m["padToggleR"]["b"], 1)]
        chk(f"8.4 між частинами ручки мінімальний відступ (альбомна, {state})",
            all(0 < g <= 8 for g in between), f"{between} px")
        # ⚠️ Тут ручка дорівнює бруску **побудовою flexbox** (розтяг у
        # колонковій смузі), тож сама по собі рівність нічого не доводить —
        # і обіцяти доказ було б неправдою. Перевірка ловить інше й одне:
        # витік бічних полів ручки з `@media (orientation: portrait)` в
        # альбомну, де сусідів по горизонталі немає й межу відбирати нема в
        # кого. Мутація «прибрати `@media` в правила полів» валить саме її.
        chk(f"8.2 ручка завширшки з брусок панелі (альбомна, {state})",
            near(m["padToggle"]["w"], m["rail"]["w"], 1.5)
            and near(m["padToggleR"]["w"], m["railR"]["w"], 1.5),
            f"ручки {round(m['padToggle']['w'], 1)}/{round(m['padToggleR']['w'], 1)}, "
            f"бруски {round(m['rail']['w'], 1)}/{round(m['railR']['w'], 1)}")
        widths[state] = (round(m["padToggle"]["w"], 1), round(m["padToggleR"]["w"], 1))

    chk("8.3 ширина ручок не залежить від стану панелі (альбомна)",
        widths["згорнуті"] == widths["розгорнуті"],
        f"згорнуті {widths['згорнуті']}, розгорнуті {widths['розгорнуті']}")
    set_pads(page, False)


def check_look_7(page, chk, where):
    """Розділ 7 — дописане за відгуком боса після проходу на стенді."""
    m = measure(page)

    # 7.4 — мінімальний відступ від країв і між кнопками.
    keys = m["keysL"]
    if not keys:
        # ⚠️ Мовчазний пропуск тут був би тим самим шаблоном, що й скрізь:
        # порожня панель — це вже вада, а не привід не перевіряти.
        chk(f"7.4 мінімальний відступ від країв і між кнопками ({where})",
            False, "жодної кнопки в лівій панелі")
    else:
        # ⚠️ Відступ міряється від **тіла** панелі, а не від її коробки: над
        # тілом у книжковій лежить брусок ручки, і відстань до нього — це не
        # відступ від краю, а сусідній елемент. Спершу міряв від коробки й
        # отримав 38 px замість 4.
        left = round(keys[0]["l"] - m["bodyL"]["l"], 1)
        top = round(keys[0]["t"] - m["bodyL"]["t"], 1)
        # Найменший проміжок, а не крайній: крайній міг би виявитись
        # подвійним на пульті з іншим порядком клавіш.
        between = min(gaps(keys)) if len(keys) > 1 else None
        # ⚠️ Не «дорівнює нулю» і не «на око малий»: відступ має бути **малим,
        # але не нульовим** — кнопка, приклеєна до краю вікна, ловить край
        # долоні, а приклеєна до сусідньої зливається з нею.
        ok = (0 < left <= 8 and 0 < top <= 8 and between is not None
              and 0 < between <= 8 and abs(left - between) <= 1)
        chk(f"7.4 мінімальний відступ від країв і між кнопками ({where})", ok,
            f"від краю панелі {left} / {top} px, між кнопками {between} px")

    if where == "книжкова":
        # 7.2 — власна ручка смужки, і вона видима **при згорнутій** смужці,
        # інакше розгортати не було б чим.
        set_bar(page, True)
        m = measure(page)
        chk("7.2 у книжковій смужка має власну ручку", m["barOwnVisible"],
            f"кнопка є: {m['barOwnVisible']}, згорнута: {m['barCollapsed']}")
        chk("7.2 бічні ручки смужки в книжковій прибрані",
            not m["railBarToggleVisible"])
        # ⚠️ Під охороною: якщо ручки не видно, клац дав би 30-секундний
        # тайм-аут Playwright зі стеком замість чесного «ПАД».
        if m["barOwnVisible"]:
            page.click("#bar-toggle-mid")
            time.sleep(0.4)
            chk("7.2 власна ручка справді розгортає смужку",
                not measure(page)["barCollapsed"])
        else:
            chk("7.2 власна ручка справді розгортає смужку", False,
                "ручки не видно — тиснути нічого")

        # 7.3 — ручка панелі більше не ділиться: вона займає весь бічний брусок.
        m = measure(page)
        # ⚠️ По ширині ручка менша за брусок рівно на видиму межу з критерію
        # 8.4 — і це записано числом, а не послабленим допуском. Сам критерій
        # 7.3 про **висоту**: «чверті в неї більше не забирають».
        chk("7.3 ручка бічної панелі ціла, чверті в неї не забирають",
            near(m["padToggle"]["w"] + GAP, m["rail"]["w"], 1.5)
            and near(m["padToggle"]["h"], m["rail"]["h"], 2),
            f"ручка {round(m['padToggle']['w'])}×{round(m['padToggle']['h'])}, "
            f"брусок {round(m['rail']['w'])}×{round(m['rail']['h'])}")
        # ⚠️ Абсолютне число, а не лише «ручка = брусок»: критерій каже
        # «нормального розміру», і без підлоги брусок на 8 px проходив би
        # зеленим.
        #
        # ⚠️ Межа тут була **44 px** — «ціль для пальця, як у `.key`». Критерій
        # 9.2 її прямо скасував: бос назвав інше число, і воно менше. Це та
        # сама суперечність, про яку каже правило «виправлення не переписує
        # погоджене», і розв'язана вона на користь нової вимоги вголос, а не
        # мовчки. Саме число стереже `check_rail_thickness` — тут лишається
        # підлога від безглуздого, щоб брусок на вісім пікселів не проходив.
        own_h = m["barOwn"]["h"] if m["barOwn"] else 0
        chk("7.3 обидві ручки лишаються відчутною ціллю",
            m["padToggle"]["h"] >= RAIL - 0.5 and own_h >= RAIL - 0.5,
            f"ручка панелі {round(m['padToggle']['h'])} px, "
            f"ручка смужки {round(own_h)} px, межа {RAIL}")
    else:
        # ⚠️ Смужку тут відчиняємо **навмисно**: у згорнутій власну ручку
        # ховає `#bar.collapsed { display: none }`, і твердження «в альбомній
        # її немає» справджувалось би саме тому, а не тому, що її не роблять.
        # Спіймано мутацією: без цього рядка зняття `display: none` з
        # `.bar-own-toggle` лишалось непоміченим.
        set_bar(page, False)
        m = measure(page)
        chk("7.2 в альбомній власної ручки немає — 2.6 лишається чинним",
            not m["barOwnVisible"] and m["railBarToggleVisible"],
            f"власна: {m['barOwnVisible']}, бічна: {m['railBarToggleVisible']}, "
            f"згорнута: {m['barCollapsed']}")


def check_focus_ring(page, chk):
    """Критерій 7.1 — рамки навколо зображення немає, а фокус усе одно видно.

    ⚠️ Фокус ставиться викликом, а не клацанням по канві: клацання — це
    **дотик до екрана пульта**, і проти живого моста воно тицяло б у меню.
    """
    page.evaluate("() => document.getElementById('screen-wrap').focus()")
    time.sleep(0.4)
    m = measure(page)
    chk("7.1 фокус справді на зображенні", m["focused"])
    chk("7.1 рамки навколо зображення немає",
        m["focusRing"] in ("none", "") and m["focusOutline"] in ("none", ""),
        f"тінь {m['focusRing']!r}, обведення {m['focusOutline']!r}")
    # ⚠️ Вимога 5.4 задачі 0022 не скасована — вона переїхала в смужку.
    chk("7.1 плашка каже, що клавіатура слухає", "✓" in m["focusChip"],
        m["focusChip"])

    # ⚠️ Фокус знімається `blur()`, а не переведенням на кнопку смужки: та
    # лежить у смужці, яка згорнута за замовчуванням, і `focus()` на схованому
    # елементі не робить нічого. Перевірка тоді залежала б від того, встиг
    # хтось раніше відчинити смужку чи ні, — і на живому мості вона впала
    # рівно через це.
    page.evaluate("() => document.getElementById('screen-wrap').blur()")
    time.sleep(0.4)
    m = measure(page)
    chk("7.1 фокус пішов — плашка це показує",
        not m["focused"] and "✓" not in m["focusChip"], m["focusChip"])
    page.evaluate("() => document.getElementById('screen-wrap').focus()")
    time.sleep(0.3)


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


"""Скільки секунд два вікна стоять поруч у короткій пробі гойдалки.

⚠️ Це **не** доказ критерію 1.1 — той просить шістдесят секунд і робиться
руками на живому мості. Тут коротка проба, яка ловить регресію: при старій
поведінці витіснення приходили пачками щосекунди, тож восьми вистачить, щоб
різниця лічильника перестала бути нулем.
"""
QUEUE_QUIET_S = 8


def check_queue(browser, url, chk):
    """Черга: господар один, решта чекають і нічого не споживають (0024).

    ⚠️ Два справжні вікна, а не підміна відповідей: уся вада була саме у
    взаємодії двох клієнтів із мостом, і жодна половина сама по собі її не
    показує.
    """
    a_ctx, a_page, a_err = open_page(browser, url, LANDSCAPE)
    b_ctx = browser.new_context(viewport=dict(LANDSCAPE))
    b_page = b_ctx.new_page()
    b_err = []
    b_page.on("pageerror", lambda e: b_err.append(str(e)))
    b_page.on("console", lambda m: b_err.append(f"console.error: {m.text}")
              if m.type == "error" else None)

    # ⚠️ Прилад під критерій 1.2 — самі кадри WebSocket, які отримало друге
    # вікно. Лічильники клієнта тут не годяться: у браузері вони не винесені
    # назовні, а лічильники моста не розділяють клієнтів.
    b_frames = []
    b_page.on("websocket",
              lambda w: w.on("framereceived", lambda payload: b_frames.append(payload)))

    try:
        before = bridge_stats(url)
        b_page.goto(url, wait_until="load", timeout=25000)

        # ⚠️ Через `try`, а не голим очікуванням: коли вікна черги немає
        # (тобто вада повернулась), прогін має сказати це **червоною
        # перевіркою**, а не тайм-аутом Playwright, за яким причини не видно.
        gated = True
        try:
            b_page.wait_for_selector("#btn-take:visible", timeout=20000)
        except Exception:
            gated = False

        chk("2.1 другий клієнт бачить «Є активне підключення» і кнопку",
            gated and "Є активне підключення" in b_page.text_content("#veil-text")
            and b_page.is_visible("#btn-take"),
            b_page.text_content("#veil-text"))
        chk("2.1 екрана пульта другому клієнтові не показують",
            not screen_ready(b_page))
        chk("2.1 перший клієнт нічого не помітив", screen_ready(a_page))

        time.sleep(QUEUE_QUIET_S)
        after = bridge_stats(url)
        evicted = (after["lost_by"]["evicted"] - before["lost_by"]["evicted"])
        refused = (after["queue"]["busy_refused"] - before["queue"]["busy_refused"])

        # ⚠️ Одного `lost_evicted` тут **замало**, і це знайдено мутацією, не
        # оком: при мовчазному витісненні витіснений вертається за секунду й
        # забирає своє назад, тож у мить заміру пульт знову в нього — картинка
        # ціла, знімок стану зелений. Гойдалку видно не станом, а рухом:
        # скільки **сеансів** міст завів і згубив за ці секунди. У черзі — нуль.
        seen = after["session"]["seen"] - before["session"]["seen"]
        lost = after["session"]["lost"] - before["session"]["lost"]

        chk(f"1.1 два клієнти поруч {QUEUE_QUIET_S} с — жодного витіснення",
            evicted == 0 and seen == 0 and lost == 0,
            f"нових сеансів {seen}, втрат {lost}, витіснень {evicted}, "
            f"відмов «зайнято» {refused}")
        chk("1.1 перший клієнт не втратив пульта", screen_ready(a_page)
            and not gate_shown(a_page))

        binary = [f for f in b_frames if not isinstance(f, str)]
        text = [f for f in b_frames if isinstance(f, str)]
        chk("1.2 клієнт у черзі не отримав жодного кадру пікселів",
            not binary, f"двійкових кадрів {len(binary)}, слів моста {len(text)}: {text[:2]}")

        # ⚠️ Окрема перевірка, і без неї попередня майже нічого не варта: клієнт,
        # який стукає в двері щосекунди, кадрів теж не отримує — а гойдалка при
        # цьому ціла, просто її тримає вже не міст. Тут її видно двома числами:
        # рівно один сокет за весь час очікування і рівно одна відмова.
        chk(f"1.2 клієнт у черзі не стукає повторно ({QUEUE_QUIET_S} с)",
            len(text) == 1 and refused <= 1,
            f"слів «зайнято» {len(text)}, відмов на мості {refused}")

        # ⚠️ Число має сенс лише проти **живого моста**: там воно з
        # `httpd_get_client_list`, тобто рахує всі сокети сервера. Двійник
        # рахує самі лише WebSocket, і різниця в нього завжди нуль — тобто на
        # стенді без заліза ця перевірка нічого не доводить, і в «Результаті»
        # число береться з мосту. Очікуємо **нуль**: `/api/status` віддається з
        # `Connection: close`, тож між опитуваннями черга не тримає нічого.
        # ⚠️ Мінімум із трьох знімків, а не один: опитування черги теж бере
        # сокет на кілька мілісекунд, і випадковий збіг дав би червону
        # перевірку на справному коді — рівно та крихкість, від якої лікує ця
        # задача.
        def fewest_sockets(tries=3):
            got = [bridge_stats(url)["session"].get("sockets") for _ in range(tries)]
            got = [g for g in got if g is not None]
            return min(got) if got else None

        sockets_now = fewest_sockets()
        chk("1.3 черга не лишає за собою жодного сокета",
            sockets_now is None or before["session"].get("sockets") is None
            or sockets_now - before["session"]["sockets"] <= 0,
            f"сокетів було {before['session'].get('sockets')}, стало {sockets_now}")

        if not gated:
            # Далі кожен крок починається з натискання кнопки, якої немає.
            for name in ("2.2 попередження називає наслідок і дає дві кнопки",
                         "2.2 «Скасувати» лишає клієнта в черзі, пульта не чіпає",
                         "2.3 після «Підключитись» другий клієнт отримує кадри",
                         "2.3 у першого клієнта — те саме вікно очікування з кнопкою",
                         "2.4 переймання працює в обидва боки без перезавантаження"):
                chk(name, False, "вікна черги не було — перевіряти нічим")
        else:
            # --- 2.2 переймання питає, а «Скасувати» лишає в черзі -----------
            b_page.click("#btn-take")
            b_page.wait_for_selector("#btn-take-yes:visible", timeout=5000)
            chk("2.2 попередження називає наслідок і дає дві кнопки",
                "буде відключений" in b_page.text_content("#veil-text")
                and b_page.is_visible("#btn-take-yes") and b_page.is_visible("#btn-take-no"),
                b_page.text_content("#veil-text"))
            b_page.click("#btn-take-no")
            time.sleep(1.0)
            chk("2.2 «Скасувати» лишає клієнта в черзі, пульта не чіпає",
                gate_shown(b_page) and not screen_ready(b_page) and screen_ready(a_page))

            # --- 2.3 і 2.4 переймання в обидва боки --------------------------
            b_page.click("#btn-take")
            b_page.wait_for_selector("#btn-take-yes:visible", timeout=5000)
            b_page.click("#btn-take-yes")
            b_page.wait_for_function(
                "() => document.getElementById('screen').width > 100", timeout=25000)
            chk("2.3 після «Підключитись» другий клієнт отримує кадри",
                screen_ready(b_page))

            back = True
            try:
                a_page.wait_for_selector("#btn-take:visible", timeout=20000)
            except Exception:
                back = False
            chk("2.3 у першого клієнта — те саме вікно очікування з кнопкою",
                back and gate_shown(a_page)
                and "Є активне підключення" in a_page.text_content("#veil-text"))

            # ⚠️ Назад — тими самими кнопками й **без перезавантаження
            # сторінки**: саме це критерій 2.4 і просить, бо перезавантаження
            # ховало б будь-яку ваду стану, що лишився від попереднього сеансу.
            if back:
                a_page.click("#btn-take")
                a_page.wait_for_selector("#btn-take-yes:visible", timeout=5000)
                a_page.click("#btn-take-yes")
                a_page.wait_for_function(
                    "() => document.getElementById('screen').width > 100", timeout=25000)
                b_page.wait_for_selector("#btn-take:visible", timeout=20000)
            chk("2.4 переймання працює в обидва боки без перезавантаження",
                back and screen_ready(a_page) and not gate_shown(a_page)
                and gate_shown(b_page))

            # --- 1.1 подвійне натискання не заводить гойдалки з одного клієнта -
            #
            # ⚠️ Цей випадок знайшла рецензія, не прилад, і жодна перевірка
            # вище його не бачила: два сокети з тим самим іменем витісняли одне
            # одного нескінченно, а лічильник казав `lost_resumed` — не
            # `lost_evicted`, за яким дивиться критерій 1.1. Тому тут
            # звіряються **всі** ознаки руху сеансів.
            if back and gate_shown(b_page):
                b_page.click("#btn-take")
                b_page.wait_for_selector("#btn-take-yes:visible", timeout=5000)
                two = bridge_stats(url)
                # ⚠️ Обидва натискання **в один такт**, через сам обробник, а
                # не мишею: після першого кнопка ховається (`gate(null)`), і
                # Playwright просто чекав би на видимість — тобто перевіряв би
                # не те. Тут відтворюється саме те, що робить палець, який
                # тицьнув двічі, поки сторінка ще не перемалювалась.
                b_page.evaluate("() => { const b = document.getElementById('btn-take-yes');"
                                " b.click(); b.click(); }")
                b_page.wait_for_function(
                    "() => document.getElementById('screen').width > 100", timeout=25000)
                time.sleep(QUEUE_QUIET_S)
                three = bridge_stats(url)
                moved = {
                    "сеансів": three["session"]["seen"] - two["session"]["seen"],
                    "втрат": three["session"]["lost"] - two["session"]["lost"],
                    "витіснень": three["lost_by"]["evicted"] - two["lost_by"]["evicted"],
                    "повернень": three["lost_by"]["resumed"] - two["lost_by"]["resumed"],
                }
                chk("1.1 подвійне «Підключитись» не заводить гойдалки",
                    moved["сеансів"] <= 2 and moved["повернень"] == 0
                    and screen_ready(b_page),
                    ", ".join(f"{k} {v}" for k, v in moved.items()))
            else:
                chk("1.1 подвійне «Підключитись» не заводить гойдалки", False,
                    "не було з чого починати — попередній крок не пройшов")

        chk_no_page_errors(chk, "сторінка без помилок JS (черга, перший клієнт)", a_err)
        chk_no_page_errors(chk, "сторінка без помилок JS (черга, другий клієнт)", b_err)
    finally:
        b_ctx.close()
        a_ctx.close()


# --------------------------------------- місце під екран до першого кадру ---
#
# ⚠️ Стан «кадру ще немає» відтворюється підміною самого `WebSocket` у вікні, а
# не глушінням цілі: міст має лишатись живим для інших перевірок, а сторінка
# має чесно пройти весь свій початок і зупинитись рівно там, де зупиняється в
# боса, — на очікуванні `HELLO`.
BLIND_WS = """
  window.WebSocket = function (url) {
    this.url = url;
    this.readyState = 0;
    this.send = () => {};
    this.close = () => {};
  };
  window.WebSocket.OPEN = 1;
"""

# Пам'ять про **інший** пульт: квадратний екран, якого в нас на столі немає.
# ⚠️ Числа описують удаваний чужий пульт, а не наш клієнт: перевіряється саме
# те, що справжній `HELLO` за них старший.
OTHER_RADIO = (200, 200)


def open_blind(ctx, url):
    """Вікно, яке до пульта не достукається: місце тримається, кадру немає."""
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(f"console.error: {m.text}")
            if m.type == "error" else None)
    page.add_init_script(BLIND_WS)
    page.goto(url, wait_until="load", timeout=25000)
    time.sleep(1.0)
    return page, errors


def wait_ready_soft(page):
    """Дочекатись кадру — але не падати тайм-аутом Playwright.

    ⚠️ За тайм-аутом причини не видно взагалі: чи пульт зайнятий, чи клієнт
    зламаний. Мутація «пригадане старше за HELLO» провалилась саме так, доки
    цього не було, — і замість червоної перевірки прогін дав стек.
    """
    try:
        wait_ready(page)
        return True
    except Exception:
        return False


def edges(b):
    return (round(b["l"], 1), round(b["t"], 1), round(b["r"], 1), round(b["b"], 1))


def check_reserve(browser, url, chk, viewport, where):
    """Місце під екран тримається до першого кадру (задача 0025).

    ⚠️ Власний контекст, тобто **порожнє сховище**: перший у житті візит
    інакше не відтворити — усі попередні перевірки вже щось у ньому лишили.

    ⚠️ Обидві орієнтації, і це не повнота заради повноти: до задачі 0025 гілка
    `portrait()` у `fitCanvas` до `HELLO` не виконувалась **жодного разу**, а
    саме в ній рахуються `margin-top` полотна й `margin-bottom` ряду панелей —
    та сама арифметика, яка в 0023 коштувала окремого кола відгуку. Телефон
    боса за замовчуванням книжковий.
    """
    ctx = browser.new_context(viewport=dict(viewport))
    errors = []
    try:
        # --- 1.2 перший у житті візит ------------------------------------
        p0, e0 = open_blind(ctx, url)
        errors += e0
        first = measure(p0)
        p0.close()

        # --- ті самі числа, але з пультом --------------------------------
        p1 = ctx.new_page()
        p1.on("pageerror", lambda e: errors.append(str(e)))
        p1.goto(url, wait_until="load", timeout=25000)
        live_ok = wait_ready_soft(p1)
        time.sleep(1.0)
        live = measure(p1)
        p1.close()
        why = "" if live_ok else "; кадру в чистому вікні не дочекались"

        # ⚠️ Вісь залежить від орієнтації, і це не причісування: в альбомній
        # вільне місце — те, що лишили панелі **збоку**, у книжковій — те, що
        # вони лишили **знизу**. Порівнювати не ту вісь означало б перевіряти
        # ширину вікна саму по собі.
        if where == "альбомна":
            free = first["stage"]["w"] - first["padL"]["w"] - first["padR"]["w"]
            got = first["canvas"]["w"]
        else:
            rows = [first["padL"]["h"], first["padR"]["h"], first["bar"]["h"]]
            free = first["stage"]["h"] - max(rows)
            got = first["canvas"]["h"]

        chk(f"1.2 перший візит: панелі мають ту саму ширину, що й з пультом ({where})",
            live_ok and near(first["padL"]["w"], live["padL"]["w"])
            and near(first["padR"]["w"], live["padR"]["w"]),
            f'до {first["padL"]["w"]}/{first["padR"]["w"]}, '
            f'після {live["padL"]["w"]}/{live["padR"]["w"]}{why}')
        chk(f"1.2 перший візит: під зображення віддано все вільне місце ({where})",
            near(got, free) and free > 0,
            f"зарезервовано {got} з вільних {free}")
        # ⚠️ Порівнюються **краї**, а не центри. Центр обох областей — це
        # середина вікна за будь-якої розкладки колонки (панелі рівні за
        # побудовою), тобто перевірка центрів справджувалась би сама собою:
        # рецензія коду назвала це прямо.
        chk(f"1.2 перший візит: вікно очікування накриває зарезервоване місце ({where})",
            edges(first["veil"]) == edges(first["wrap"]),
            f'вікно {edges(first["veil"])}, місце {edges(first["wrap"])}')

        # --- 1.1 друге відкриття: розмір пригадується --------------------
        p2, e2 = open_blind(ctx, url)
        errors += e2
        again = measure(p2)
        # ⚠️ Однорідність береться з самого полотна, а не з вигляду: «рівне
        # тло» — це коли всі точки однакові, і жодна стара картинка крізь
        # нього не проступає (критерій 1.4).
        flat = p2.evaluate("""() => {
          const c = document.getElementById('screen');
          const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
          for (let i = 4; i < d.length; i += 4) {
            if (d[i] !== d[0] || d[i+1] !== d[1] || d[i+2] !== d[2]) return null;
          }
          return [d[0], d[1], d[2], d[3]];
        }""")
        p2.close()

        chk(f"1.1 друге відкриття: місце під екран те саме, що з пультом ({where})",
            live_ok and near(again["canvas"]["w"], live["canvas"]["w"])
            and near(again["canvas"]["h"], live["canvas"]["h"]),
            f'до з\'єднання {again["canvas"]["w"]}×{again["canvas"]["h"]}, '
            f'з пультом {live["canvas"]["w"]}×{live["canvas"]["h"]}{why}')
        # ⚠️ Місце — окремо від розміру, і межа тут **20 px**, а не «краї до
        # країв». Причина названа числом: пам'ять тримає розмір екрана, але не
        # вміст панелей — його приносить `HELLO`, і до нього панель порожня,
        # тобто нижча. У книжковій це зсуває зарезервоване місце на 11 px
        # угору. Вимагати збігу край у край означало б вимагати пам'яті про
        # перелік клавіш чужого пульта, а це вже вигадка про нього.
        #
        # ⚠️ Перевірка не справджується сама собою: з поверненим раннім
        # виходом із `fitCanvas` (тобто з тією самою вадою, заради якої задача
        # існує) центр стояв на 118 замість 450.
        seat = ((again["canvas"]["l"] + again["canvas"]["r"]) / 2,
                (again["canvas"]["t"] + again["canvas"]["b"]) / 2)
        seen = ((live["canvas"]["l"] + live["canvas"]["r"]) / 2,
                (live["canvas"]["t"] + live["canvas"]["b"]) / 2)
        chk(f"1.1 друге відкриття: місце стоїть там, де буде екран ({where})",
            live_ok and near(seat[0], seen[0], 20) and near(seat[1], seen[1], 20),
            f"вікно на {seat}, екран буде на {seen}{why}")
        chk(f"1.1 друге відкриття: вікно очікування накриває зарезервоване місце ({where})",
            edges(again["veil"]) == edges(again["wrap"]),
            f'вікно {edges(again["veil"])}, місце {edges(again["wrap"])}')
        # ⚠️ Однорідності полотна тут **замало**, і це знайшов сам прогін:
        # незаймане полотно віддає `[0, 0, 0, 0]`, тобто прозоре. Видно крізь
        # нього тло `#screen-wrap`, і саме воно й має бути рівним чорним —
        # інакше «рівне тло» трималося б на тому, чим браузер заповнює
        # прозорість.
        # ⚠️ Колір названий, а не «аби однорідний»: буквально виконана
        # пропозиція боса «заповнити буфер чорним», де переплутали колір, дала
        # б однорідний червоний прямокутник — і мовчазно пройшла б.
        chk(f"1.4 у зарезервованому місці — рівне тло, а не стара картинка ({where})",
            flat is not None and flat[:3] == [0, 0, 0]
            and again["color"]["wrap"] == BLACK,
            f'точки полотна: {flat}, тло під ним {again["color"]["wrap"]}')

        # --- 1.5 пульт із іншим екраном старший за пригадане --------------
        p3 = ctx.new_page()
        p3.on("pageerror", lambda e: errors.append(str(e)))
        p3.add_init_script(
            f"localStorage.setItem('remoteui.screen.w', '{OTHER_RADIO[0]}');"
            f"localStorage.setItem('remoteui.screen.h', '{OTHER_RADIO[1]}');")
        p3.goto(url, wait_until="load", timeout=25000)
        got_frame = wait_ready_soft(p3)
        time.sleep(1.0)
        other = measure(p3)
        kept = p3.evaluate("""() => {
          const c = document.getElementById('screen');
          return {w: +localStorage['remoteui.screen.w'],
                  h: +localStorage['remoteui.screen.h'],
                  cw: c.width, ch: c.height};
        }""")
        p3.close()

        chk(f"1.5 HELLO старший за пригадане: розкладка під справжній пульт ({where})",
            got_frame and live_ok and near(other["canvas"]["w"], live["canvas"]["w"])
            and near(other["canvas"]["h"], live["canvas"]["h"]),
            f'{other["canvas"]["w"]}×{other["canvas"]["h"]} проти '
            f'{live["canvas"]["w"]}×{live["canvas"]["h"]} у чистому вікні'
            + ("" if got_frame else "; кадру так і не дочекались") + why)
        chk(f"1.5 і пригадане оновилось під нього ({where})",
            got_frame and kept["w"] == kept["cw"] and kept["h"] == kept["ch"]
            and (kept["w"], kept["h"]) != OTHER_RADIO,
            f'у сховищі {kept["w"]}×{kept["h"]}, пульт віддає '
            f'{kept["cw"]}×{kept["ch"]}')

        chk_no_page_errors(
            chk, f"сторінка без помилок JS (місце під екран, {where})", errors)
    finally:
        ctx.close()


def check_no_radio_numbers(chk, built_dir):
    """Критерій 1.3: у тому, що їде у флеш, немає розміру нашого пульта.

    ⚠️ Дивимось у **зібране**, а не в джерело. У `js` і `css` коментарі там уже
    зрізані, тож згадка «літерал 480 тут = помилка» за порушення не рахується,
    а справжнє число — рахується. `index.html` мініфікатор лише копіює
    (`webui/minify.mjs`), тобто його коментарі теж поїдуть у флеш і теж
    рахуються — і це правильно, місце вони займають так само.

    ⚠️ Кінець числа стережеться `(?!\\d)`, а не `(?![\\d.])`: у JS `480.0` — те
    саме число, що `480`, і мініфікатор його не чіпає. З суворішим хвостом
    головна пастка задачі проходила б повз перевірку, щойно її записали з
    крапкою. Знайдено рецензією коду, не прогоном.
    """
    bad = []
    for name in sorted(os.listdir(built_dir)):
        if not name.endswith((".js", ".css", ".html", ".webmanifest")):
            continue
        with open(os.path.join(built_dir, name), encoding="utf-8",
                  errors="replace") as f:
            text = f.read()
        # Шістнадцяткові кольори прибираються з тексту заздалегідь: `#a480ff`
        # містить «480», але про роздільність не каже нічого.
        text = re.sub(r"#[0-9a-fA-F]{3,8}\b", "", text)
        for num in ("480", "272"):
            if re.search(r"(?<![\d.])" + num + r"(?!\d)", text):
                bad.append(f"{name}: {num}")
    chk("1.3 у клієнті немає роздільності нашого пульта", not bad, "; ".join(bad))


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
        wait_ready(page)
        time.sleep(1.0)
        chk("1.5 браузер не вміє — кнопки немає, а не мертва",
            page.evaluate("() => !document.getElementById('btn-full')"))
        chk_no_page_errors(
            chk, "сторінка без помилок JS (браузер без повноекранного режиму)", errors)
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
            took = wait_ready(page)
            time.sleep(3)

            # ⚠️ Не прикраса: якщо на пульті хтось був, далі йдуть перевірки
            # вигляду, а не поведінки черги, і знати, з чого починався прогін,
            # треба саме тут.
            if took:
                print("   пульт був зайнятий — керування перейнято кнопкою")

            chk_no_page_errors(chk, "сторінка без помилок JS", errors)

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

            # ⚠️ Черга — перша серед вікон, які відкриваються після
            # відпускання головного: вона єдина міряє **два** клієнти поруч, і
            # чужі вікна, що лишились від попередніх перевірок, зробили б її
            # числа неправдою.
            check_queue(br, url, chk)
            # ⚠️ Одразу за чергою й теж у власному контексті: перевірка
            # починається з **порожнього** сховища, і чуже вікно, що лишилось
            # від сусідньої перевірки, забрало б у неї пульт посеред заміру.
            check_reserve(br, url, chk, LANDSCAPE, "альбомна")
            check_reserve(br, url, chk, PORTRAIT, "книжкова")

            check_default_bar(br, url, chk, LANDSCAPE, "альбомна")
            check_default_bar(br, url, chk, PORTRAIT, "книжкова")
            check_resize_sweep(br, url, chk)
            check_heap_watchdog(br, url, chk)
            check_no_fullscreen(br, url, chk)

            page.set_viewport_size(dict(LANDSCAPE))
            page.goto(url, wait_until="load", timeout=25000)
            wait_ready(page)
            time.sleep(2)

            check_fullscreen(page, chk)
            check_focus_ring(page, chk)
            check_landscape(page, chk)
            check_colors(page, chk, "альбомна")
            check_look_7(page, chk, "альбомна")
            check_centering(page, chk)
            check_toggle_row_landscape(page, chk)

            page.set_viewport_size(dict(PORTRAIT))
            time.sleep(0.5)
            check_portrait(page, chk)
            check_colors(page, chk, "книжкова")
            check_look_7(page, chk, "книжкова")
            # ⚠️ Розділ 8 згортає **всі** панелі й міняє висоту вікна, тобто
            # лишає по собі стан, якого решта перевірок не чекає. Тримається це
            # не порядком, а тим, що кожна його частина повертає стан сама
            # (`set_pads(page, False)`, повернення вікна в `PORTRAIT`) — саме
            # тому альбомна частина може стояти всередині прогону. Книжкова
            # йде останньою просто тому, що вона тут остання за орієнтацією.
            check_portrait_centering(page, chk)
            check_toggle_row(page, chk)
            check_pads_anchor(page, chk)
            check_lift_leak(page, chk)
            check_rail_thickness(page, chk)

            chk_no_page_errors(chk, "сторінка без помилок JS (у кінці)", errors)
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
            check_no_radio_numbers(chk, built)
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
            check_no_radio_numbers(chk, built)
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
