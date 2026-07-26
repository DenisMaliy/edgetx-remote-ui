# Клієнт вбудовується в прошивку з webui/ — одним примірником.
#
# Копії файлів у firmware/esp32/ немає навмисно: це та сама причина, з якої
# наш код в EdgeTX лежить через symlink, а не копіюванням (DECISIONS.md,
# рішення про механізм вбудовування). Другий примірник рано чи пізно правлять,
# а потім втрачають.

idf_component_register(
    SRCS
        "main.c"
        "framing.c"
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
    EMBED_FILES
        "../../../webui/index.html"
        "../../../webui/proto.js"
        "../../../webui/app.js"
        "../../../webui/style.css"
)
