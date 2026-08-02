# Клієнт вбудовується в прошивку з webui/ — одним примірником.
#
# Копії файлів у firmware/esp32/ немає навмисно: це та сама причина, з якої
# наш код в EdgeTX лежить через symlink, а не копіюванням (DECISIONS.md,
# рішення про механізм вбудовування). Другий примірник рано чи пізно правлять,
# а потім втрачають.

idf_component_register(
    SRCS
        "main.c"
        "baud_follow.c"
        "framing.c"
        "heap_watch.c"
        "stats.c"
        "uart_link.c"
        "ws_bridge.c"
        "wifi_ap.c"
    INCLUDE_DIRS "."
    REQUIRES
        driver
        esp_wifi
        esp_netif
        esp_event
        esp_http_server
        esp_app_format
        esp_hw_support
        esp_timer
        esp_system
        nvs_flash
)

# --- клієнт вбудовується стисненим ------------------------------------------
#
# ⚠️ Це не економія флешу, а лікування заміряної вади (задача 0021). Ціна
# сторінки в купі моста дорівнює її розміру **на дроті**: віддача файлу на
# 67 КБ забирає 61 КБ вільної пам'яті, бо стільки його одночасно лежить у
# черзі TCP і в буферах Wi-Fi. 122 КБ сторінки з'їдали майже всю купу
# (107 КБ), і мостові лишалось 2.5 КБ.
#
# Розпаковує браузер. У мості немає жодного рядка на розпакування — лише
# заголовок `Content-Encoding: gzip` у `send_blob()`.
#
# ⚠️ Стискається на **збірці**, а не руками: стиснений файл у git був би
# другим примірником клієнта, тобто рівно тим, від чого застерігає коментар
# вище. Джерело лишається одне — `webui/`.
set(WEBUI_DIR "${CMAKE_CURRENT_LIST_DIR}/../../../webui")
set(WEBUI_MIN_DIR "${CMAKE_CURRENT_BINARY_DIR}/webui-min")
set(WEBUI_GZ_DIR "${CMAKE_CURRENT_BINARY_DIR}/webui-gz")
set(WEBUI_FILES index.html proto.js wait.js panels.js app.js style.css)

# --- перед стисненням клієнт мініфікується ----------------------------------
#
# ⚠️ Теж лікування заміряної вади, а не косметика (задача 0022, критерій 8.3).
# Бокові панелі підняли сторінку 43.4 → 57.8 КБ gzip, і холодне завантаження
# опустило вільну купу моста до **20.5 КБ** при межі тривоги 32.8 КБ —
# заміряно тричі на стенді. Мініфікація дає 57.8 → 18.5 КБ, тобто сторінка
# стає легшою, ніж була до появи панелей.
#
# ⚠️ Українські коментарі з джерела **не ріжуться** — рішення людини від
# 2026-08-02. Вони лишаються у `webui/*.js`, де їх читають, і зникають із того
# примірника, що їде у флеш. Саме тому мініфікація живе на збірці: джерело
# лишається одне й читабельне.
#
# ⚠️ **Виняток — `index.html`: він лише копіюється**, тож його коментарі їдуть
# у флеш як є (пояснення — у `webui/minify.mjs`).
#
# Правила гри з інструментом описані у `webui/minify.mjs`; версія esbuild
# закріплена точним числом у `webui/package.json` і `pnpm-lock.yaml`.
set(WEBUI_ESBUILD_PKG "${WEBUI_DIR}/node_modules/esbuild/package.json")
set(WEBUI_PACKAGE_JSON "${WEBUI_DIR}/package.json")

# ⚠️ Усі три перевірки нижче закінчуються `FATAL_ERROR`, і це навмисно.
# Мовчазний відкат до немініфікованого клієнта був би найгіршим варіантом:
# прошивка зібралась би, поїхала б у міст і провалила б 8.3 так само тихо, як
# провалила його вперше. Краще не зібратися взагалі.

find_program(WEBUI_NODE node)
if(NOT WEBUI_NODE)
    message(FATAL_ERROR
        "Немає `node` — без нього клієнт не мініфікується.\n"
        "  Полікувати:  sudo pacman -S nodejs   (або інший спосіб для вашої ОС)")
endif()

if(NOT EXISTS "${WEBUI_ESBUILD_PKG}")
    message(FATAL_ERROR
        "Немає інструмента мініфікації клієнта.\n"
        "  Полікувати:  pnpm -C ${WEBUI_DIR} install\n"
        "  Навіщо:      без мініфікації сторінка важить 57.8 КБ замість 18.5,\n"
        "               і вільна купа моста падає до 20.5 КБ (межа 32.8).")
endif()

# ⚠️ Перевіряємо не наявність, а **версію**. Інакше застарілий `node_modules`
# від старішого lock-файлу зібрався б мовчки іншим esbuild — і твердження
# «версія закріплена точним числом» стало б неправдою рівно тоді, коли на
# нього спираються.
#
# ⚠️ Піноване число читається з `webui/package.json`, а не дублюється тут.
# Другий примірник числа — це те саме, від чого застерігає найперший коментар
# цього файлу: оновивши `package.json`, людина отримала б «версія розійшлася»
# і шукала б розбіжність не там, де вона є.
#
# ⚠️ Розбір регулярним виразом, а не `string(JSON ...)`: той з'явився в
# CMake 3.19, а проєкт оголошує мінімум 3.16. На тому мінімумі збірка впала б
# із `string does not recognize sub-command JSON` — тобто рівно тим стеком без
# діагнозу, якого уникають три `FATAL_ERROR` навколо.
file(READ "${WEBUI_PACKAGE_JSON}" WEBUI_PACKAGE_JSON_TEXT)
string(REGEX MATCH "\"esbuild\"[ \t]*:[ \t]*\"([^\"]+)\""
       _unused "${WEBUI_PACKAGE_JSON_TEXT}")
set(WEBUI_ESBUILD_PINNED "${CMAKE_MATCH_1}")

file(READ "${WEBUI_ESBUILD_PKG}" WEBUI_ESBUILD_PKG_TEXT)
string(REGEX MATCH "\"version\"[ \t]*:[ \t]*\"([^\"]+)\""
       _unused "${WEBUI_ESBUILD_PKG_TEXT}")
set(WEBUI_ESBUILD_VERSION "${CMAKE_MATCH_1}")

if(NOT WEBUI_ESBUILD_PINNED)
    message(FATAL_ERROR "Не вдалось прочитати піновану версію esbuild "
                        "з ${WEBUI_PACKAGE_JSON}")
endif()
if(NOT WEBUI_ESBUILD_VERSION STREQUAL WEBUI_ESBUILD_PINNED)
    message(FATAL_ERROR
        "Версія esbuild розійшлася з пінованою.\n"
        "  Стоїть:      ${WEBUI_ESBUILD_VERSION}\n"
        "  Очікується:  ${WEBUI_ESBUILD_PINNED} (webui/package.json)\n"
        "  Полікувати:  pnpm -C ${WEBUI_DIR} install --frozen-lockfile")
endif()

# ⚠️ Без цього вердикт вище застигає: він виноситься на етапі конфігурації, і
# після `pnpm install` іншої версії збірка йшла б зі старою відповіддю, доки
# хтось не зачепить сам `CMakeLists.txt`.
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
             "${WEBUI_PACKAGE_JSON}" "${WEBUI_ESBUILD_PKG}"
             "${WEBUI_DIR}/pnpm-lock.yaml")

# Один виклик на всі файли, а не по одному на кожен: запуск node коштує
# ~50 мс, і шість запусків були б помітні в кожній збірці.
set(WEBUI_MIN_FILES)
set(WEBUI_SRC_FILES)
foreach(asset IN LISTS WEBUI_FILES)
    list(APPEND WEBUI_MIN_FILES "${WEBUI_MIN_DIR}/${asset}")
    list(APPEND WEBUI_SRC_FILES "${WEBUI_DIR}/${asset}")
endforeach()

add_custom_command(
    OUTPUT ${WEBUI_MIN_FILES}
    COMMAND "${WEBUI_NODE}" "${WEBUI_DIR}/minify.mjs" "${WEBUI_MIN_DIR}"
    DEPENDS ${WEBUI_SRC_FILES} "${WEBUI_DIR}/minify.mjs"
    COMMENT "мініфікую клієнта"
    VERBATIM)

file(MAKE_DIRECTORY "${WEBUI_GZ_DIR}")

# Обнуляється явно, як і два списки вище. Причина не в повторному запуску
# `cmake` (звичайні змінні між запусками не живуть), а в тому, що цей файл
# підключається через `add_subdirectory` і **успадковує змінні батьківської
# області**: однойменна змінна ззовні мовчки втекла б у список.
set(WEBUI_GZ_FILES)

foreach(asset IN LISTS WEBUI_FILES)
    # ⚠️ Джерелом для стиснення тепер є **мініфікований** файл, а не `webui/`.
    set(src "${WEBUI_MIN_DIR}/${asset}")
    set(gz "${WEBUI_GZ_DIR}/${asset}.gz")

    # `mtime=0` прибирає час із заголовка gzip: інакше кожна збірка давала б
    # інші байти при тому самому вмісті, і `idf.py build` перезбирав би
    # прошивку без причини. (Те саме робить `gzip -n`.)
    #
    # Стискає Python із самої ESP-IDF, а не зовнішній `gzip`: збірка й так без
    # нього не працює, тобто нової залежності не з'являється, і немає гри в
    # лапки з перенаправленням у `add_custom_command`.
    add_custom_command(
        OUTPUT "${gz}"
        COMMAND "${python}" -c
                "import gzip,sys; open(sys.argv[2],'wb').write(gzip.compress(open(sys.argv[1],'rb').read(),9,mtime=0))"
                "${src}" "${gz}"
        # Залежність рівно від свого мініфікованого файлу — решта списку тут
        # зайва.
        #
        # ⚠️ Дрібності це не дає, і обіцяти її не треба: мініфікація групова
        # (один виклик на всі шість, залежить від усіх шести джерел), а esbuild
        # і `copyFile` пишуть безумовно. Тобто правка одного `style.css` однак
        # оновлює час усіх шести `webui-min/*` і перестискає всі шість. Ціна —
        # п'ять крихітних зайвих кроків, і вона свідомо прийнята.
        DEPENDS "${src}"
        COMMENT "стискаю ${asset} для вбудовування"
        VERBATIM)

    list(APPEND WEBUI_GZ_FILES "${gz}")
    # Символи виходять із **імені файлу**: `app.js.gz` -> `_binary_app_js_gz_start`.
    # ⚠️ Змінив ім'я тут — зміни ще у трьох місцях: оголошення `extern` у
    # `ws_bridge.c`, перелік у `webui/minify.mjs`, `ASSETS` у
    # `tools/webui_built_check.py`. Повний перелік — у `minify.mjs`.
    target_add_binary_data(${COMPONENT_LIB} "${gz}" BINARY)
endforeach()

# Без цього стиснення й вбудовування не пов'язані порядком: `target_add_binary_data`
# читає файл, якого на той момент може ще не бути.
add_custom_target(webui_gz DEPENDS ${WEBUI_GZ_FILES})
add_dependencies(${COMPONENT_LIB} webui_gz)
