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

Не вигляд, а те, що ламає **саме мініфікатор**:

* глобальні `RemoteUI`, `RemoteUIWait`, `RemoteUIPanels` не перейменовані;
* публічна поверхня (ключі об'єктів) ціла — `RemoteUIPanels.INTENT_UP`;
* панелі будуються з `HELLO`, а не з коду: склад і порядок кнопок;
* джойстик дає `enc`, клавіша дає пару «натиснуто / відпущено»;
* клавіатура: стрілка вниз → `enc`, `Esc` → `RTN`;
* вліво-вправо **не роблять нічого** (рішення людини від 03.08);
* жодної помилки JS за весь прогін.

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
ASSETS = ["index.html", "proto.js", "wait.js", "panels.js", "app.js", "style.css"]


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
            page = br.new_context(viewport={"width": 900, "height": 420}).new_page()
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
