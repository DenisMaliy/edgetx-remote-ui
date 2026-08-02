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
set(WEBUI_GZ_DIR "${CMAKE_CURRENT_BINARY_DIR}/webui-gz")
set(WEBUI_FILES index.html proto.js wait.js panels.js app.js style.css)

file(MAKE_DIRECTORY "${WEBUI_GZ_DIR}")

foreach(asset IN LISTS WEBUI_FILES)
    set(src "${WEBUI_DIR}/${asset}")
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
        DEPENDS "${src}"
        COMMENT "стискаю ${asset} для вбудовування"
        VERBATIM)

    list(APPEND WEBUI_GZ_FILES "${gz}")
    # Символи виходять із **імені файлу**: `app.js.gz` -> `_binary_app_js_gz_start`.
    # ⚠️ Змінив ім'я тут — зміни оголошення у `ws_bridge.c`.
    target_add_binary_data(${COMPONENT_LIB} "${gz}" BINARY)
endforeach()

# Без цього стиснення й вбудовування не пов'язані порядком: `target_add_binary_data`
# читає файл, якого на той момент може ще не бути.
add_custom_target(webui_gz DEPENDS ${WEBUI_GZ_FILES})
add_dependencies(${COMPONENT_LIB} webui_gz)
