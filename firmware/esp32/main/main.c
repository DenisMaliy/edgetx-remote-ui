/**
 * @file main.c
 * @brief Міст TX16S Remote UI: пульт по дроту, телефон по Wi-Fi.
 *
 * ## Дві задачі, а не одна, і чому саме так
 *
 * Приймання з UART **не має права блокуватись**. На 921600 бод апаратна
 * черга наповнюється за 1.4 мс, і задача, що заснула в надрах Wi-Fi, з'їла б
 * кадри мовчки. Тому шлях розрізаний надвоє скінченною чергою:
 *
 * ```
 *   UART ──► rx_task ──► черга ──► ws_tx_task ──► Wi-Fi
 *            (не чекає)  (скінченна)  (має право чекати)
 * ```
 *
 * Черга скінченна навмисно, і її переповнення **і є** тим самим «захлинувся
 * WebSocket — плитки відкидаються», що ухвалено в ADR-0003. Місце, де ми
 * відкидаємо, тут рівно одне, і воно рахується.
 *
 * ⚠️ Це **не** накопичення кадру. У черзі лежить щонайбільше 24 КБ, тоді як
 * повний кадр екрана — 255 КБ і не влазить у жодну з плат.
 */

#include <string.h>

#include "bridge_cfg.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "framing.h"
#include "freertos/FreeRTOS.h"
#include "freertos/ringbuf.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "stats.h"
#include "uart_link.h"
#include "wifi_ap.h"
#include "ws_bridge.h"

static const char *TAG = "bridge";

/* Пачка мусить вміщати щонайменше один найбільший кадр цілком, інакше
 * `chunk_flush` нижче не врятує й станеться вихід за межі буфера. */
_Static_assert(BRIDGE_WS_CHUNK_BYTES >= RUI_FRAME_MAX,
               "пачка WebSocket менша за найбільший кадр протоколу");

static RingbufHandle_t s_ring;
static rui_scanner_t s_uart_scan;

/* Пачка, яку збирає rx_task. Тільки його, іншим тут робити нічого. */
static uint8_t s_chunk[BRIDGE_WS_CHUNK_BYTES];
static size_t s_chunk_len;
static uint32_t s_chunk_tiles;
static uint32_t s_chunk_packets;

/**
 * @brief Віддати зібрану пачку в чергу до Wi-Fi.
 *
 * Тут і тільки тут міст щось відкидає — і рахує, що саме.
 */
static void chunk_flush(void)
{
    if (s_chunk_len == 0) {
        return;
    }

    if (!ws_bridge_has_client()) {
        /* Телефона немає. Це не втрата: пульт говорить лише у відповідь на
         * `PING`, тож сюди потрапляє хіба хвіст кадру, початий за мить до
         * розриву. Рахуємо окремо, щоб не псувати мірку заторів. */
        g_stats.chunks_noclient++;
    } else if (xRingbufferSend(s_ring, s_chunk, s_chunk_len, 0) != pdTRUE) {
        /* Wi-Fi не встигає. Відкидаємо цілими пакетами — саме заради цього
         * міст і розбирає кадрування (framing.h). */
        g_stats.chunks_dropped++;
        g_stats.tiles_dropped += s_chunk_tiles;
        g_stats.packets_dropped += s_chunk_packets;
    }

    s_chunk_len = 0;
    s_chunk_tiles = 0;
    s_chunk_packets = 0;
}

static void on_uart_packet(void *ctx, uint8_t type, const uint8_t *frame, size_t len)
{
    (void)ctx;

    if (type == RUI_PKT_TILE) {
        g_stats.tiles_in++;
    } else if (type == RUI_PKT_FRAME_END) {
        g_stats.frames_in++;
    }

    if (s_chunk_len + len > sizeof(s_chunk)) {
        chunk_flush();
    }

    memcpy(s_chunk + s_chunk_len, frame, len);
    s_chunk_len += len;
    s_chunk_packets++;
    if (type == RUI_PKT_TILE) {
        s_chunk_tiles++;
    }

    /* `FRAME_END` віддаємо негайно: саме він дозволяє клієнту показати кадр,
     * і затримка тут була б затримкою рівно там, де її видно оком. */
    if (type == RUI_PKT_FRAME_END) {
        chunk_flush();
    }
}

static void rx_task(void *arg)
{
    (void)arg;
    static uint8_t rd[2048];

    for (;;) {
        const int n = uart_link_read(rd, sizeof(rd), 10);
        if (n > 0) {
            g_stats.uart_bytes += (uint32_t)n;
            rui_scanner_feed(&s_uart_scan, rd, (size_t)n, on_uart_packet, NULL);

            g_stats.packets_ok = s_uart_scan.packets;
            g_stats.crc_errors = s_uart_scan.crc_errors;
            g_stats.oversized = s_uart_scan.oversized;
        }

        uart_link_poll_events();

        /* Наприкінці кожного читання: дрібна зміна не має чекати, доки
         * набереться повна пачка. */
        chunk_flush();
    }
}

static void ws_tx_task(void *arg)
{
    (void)arg;

    for (;;) {
        size_t size = 0;
        uint8_t *item = (uint8_t *)xRingbufferReceive(s_ring, &size, pdMS_TO_TICKS(200));
        if (!item) {
            continue;
        }

        ws_bridge_send(item, size);
        vRingbufferReturnItem(s_ring, item);
    }
}

/**
 * @brief Сторож мовчання телефона.
 *
 * Третій і найважливіший спосіб помітити втрату. Закриття сокета ловить
 * штатний вихід, подія Wi-Fi ловить вимкнений телефон, а от телефон, який
 * просто винесли за межу зв'язку, не породжує **жодної** події: сокет із
 * боку моста лишається відкритим ще десятки секунд. Саме в цьому випадку
 * клавіша й лишилася б натиснутою.
 */
static void watchdog_task(void *arg)
{
    (void)arg;

    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(BRIDGE_WATCHDOG_PERIOD_MS));

        const uint32_t silence = ws_bridge_input_silence_ms();
        if (silence != UINT32_MAX && silence > BRIDGE_CLIENT_SILENCE_MS) {
            ESP_LOGW(TAG, "клієнт мовчить %u мс", (unsigned)silence);
            ws_bridge_client_lost(true);
        }
    }
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    const esp_app_desc_t *app = esp_app_get_description();
    ESP_LOGI(TAG, "TX16S Remote UI — міст, збірка %s %s", app->date, app->time);

    /* Канал до пульта піднімаємо першим: обнулений `INPUT_STATE` має бути
     * куди слати ще до того, як з'явиться перший телефон. */
    ESP_ERROR_CHECK(uart_link_init());

    rui_scanner_reset(&s_uart_scan);
    s_ring = xRingbufferCreate(BRIDGE_WS_RING_BYTES, RINGBUF_TYPE_NOSPLIT);
    if (!s_ring) {
        ESP_LOGE(TAG, "не вистачило пам'яті на чергу до Wi-Fi");
        abort();
    }

    ESP_ERROR_CHECK(wifi_ap_start());
    ESP_ERROR_CHECK(ws_bridge_start());

    /* Приймання — найвищий пріоритет із наших трьох і власне ядро там, де
     * ядер два. Задачі Wi-Fi і TCP/IP усе одно вищі за нас; це правильно —
     * ми не маємо права заважати їм, як і транспорт у пульті не має права
     * заважати мікшеру. */
    xTaskCreatePinnedToCore(rx_task, "rui_rx", 4096, NULL, 12, NULL, BRIDGE_RX_CORE);
    xTaskCreate(ws_tx_task, "rui_ws_tx", 4096, NULL, 6, NULL);
    xTaskCreate(watchdog_task, "rui_wd", 3072, NULL, 4, NULL);

    ESP_LOGI(TAG, "міст працює");
}
