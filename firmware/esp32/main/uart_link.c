/**
 * @file uart_link.c
 * @brief Канал до пульта: UART на 921600.
 */

#include "uart_link.h"

#include <stdbool.h>
#include <stdint.h>

#include "bridge_cfg.h"
#include "driver/uart.h"
#include "esp_log.h"
#include "framing.h"
#include "stats.h"

static const char *TAG = "uart";

/**
 * ⚠️ Буфер приймання мусить пережити найгіршу заміряну затримку задачі на
 * найвищій швидкості, яку міст узагалі може погодитись прийняти.
 *
 * Це не запас «про всяк випадок». На 921600 старий буфер у 16 КіБ тримав
 * 178 мс проти заміряних 132 — і рівно тому все працювало. На 2 Мбод той
 * самий буфер дає 82 мс, на 2 625 000 — 62 мс, тобто **менше за затримку**, і
 * `uart_dropped` перестав би бути нулем. Причина була б не в дроті й не в
 * пульті, а тут.
 *
 * 10 біт на байт — старт і стоп на дроті, а не 8.
 */
_Static_assert((uint64_t)BRIDGE_UART_RX_BUF * 10 * 1000 >=
                   (uint64_t)BRIDGE_UART_MAX_BAUD * BRIDGE_UART_WORST_LATENCY_MS,
               "приймальний буфер менший за заміряну затримку задачі на "
               "найвищій швидкості — підніми буфер або опусти стелю");

/** Черга подій драйвера. Потрібна лише щоб побачити переповнення. */
static QueueHandle_t s_events;

esp_err_t uart_link_init(void)
{
    const uart_config_t cfg = {
        .baud_rate = BRIDGE_BAUDRATE,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };

    /* Читаємо в циклі, а не за подіями, але чергу подій усе одно заводимо:
     * інакше переповнення буфера ніяк не побачити, і втрачені на вході байти
     * виглядали б як помилки CRC.
     *
     * Розмір буфера приймання виводиться з найгіршої заміряної затримки
     * задачі — див. _Static_assert вище. */
    ESP_ERROR_CHECK(uart_driver_install(BRIDGE_UART_PORT, BRIDGE_UART_RX_BUF, 0, 16, &s_events, 0));
    ESP_ERROR_CHECK(uart_param_config(BRIDGE_UART_PORT, &cfg));
    ESP_ERROR_CHECK(uart_set_pin(BRIDGE_UART_PORT, BRIDGE_PIN_TX, BRIDGE_PIN_RX,
                                 UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));

    /* ⚠️ Поріг переривання нижчий за типовий (120 зі 128).
     *
     * На 921600 апаратна черга в 128 байтів наповнюється за 1.4 мс. При
     * порозі 120 в обробника лишається 87 мкс запасу — під Wi-Fi це мало.
     * Поріг 64 дає 694 мкс, тобто на порядок більше, ціною вдвічі частіших
     * переривань (1400 на секунду — дрібниця).
     *
     * ⚠️ На 2 625 000 той самий поріг дає **244 мкс**, бо черга наповнюється
     * утричі швидше. Це вже не «на порядок більше», а просто «достатньо», і
     * саме цей запас перевіряється дослідом: якщо `uart_dropped` росте при
     * достатньому буфері, наступний підозрюваний — цей поріг, а не розмір
     * буфера. Переривань при цьому 4100 на секунду. */
    ESP_ERROR_CHECK(uart_set_rx_full_threshold(BRIDGE_UART_PORT, 64));

    ESP_LOGI(TAG, "AUX1 ↔ UART%d: RX=GPIO%d, TX=GPIO%d, %d бод", BRIDGE_UART_PORT, BRIDGE_PIN_RX,
             BRIDGE_PIN_TX, BRIDGE_BAUDRATE);
    ESP_LOGI(TAG, "⚠️ TX моста йде до RX пульта — хрест обов'язковий");

    return ESP_OK;
}

int uart_link_read(uint8_t *buf, size_t cap, uint32_t timeout_ms)
{
    const int n = uart_read_bytes(BRIDGE_UART_PORT, buf, cap, pdMS_TO_TICKS(timeout_ms));
    return (n > 0) ? n : 0;
}

void uart_link_poll_events(void)
{
    uart_event_t ev;
    while (s_events && xQueueReceive(s_events, &ev, 0) == pdTRUE) {
        if (ev.type == UART_FIFO_OVF || ev.type == UART_BUFFER_FULL) {
            /* Байти вже втрачені. Чистити буфер не можна: у ньому лежать
             * цілі кадри, а розбирач і так ресинхронізується сам. */
            g_stats.uart_dropped++;
            ESP_LOGW(TAG, "приймання не встигло: %s",
                     ev.type == UART_FIFO_OVF ? "апаратна черга" : "буфер драйвера");
        }
    }
}

int uart_link_write(const uint8_t *data, size_t len)
{
    /* Драйвер UART бере власний м'ютекс на передачу, тому окремого замка тут
     * не треба: пишуть і задача httpd (ввід від клієнта), і сторож
     * (обнулений INPUT_STATE). */
    return uart_write_bytes(BRIDGE_UART_PORT, data, len);
}

void uart_link_set_baudrate(uint32_t baud)
{
    /* 1. Дочекатись, поки відправлене зійде з дроту. Це єдине місце в мості,
     *    де ми свідомо чекаємо: недописаний кадр, дожований на новій
     *    швидкості, став би сміттям на тому кінці саме тоді, коли пульт
     *    найуважніше слухає. Черги передачі в драйвера немає
     *    (`tx_buffer_size = 0`), тож чекати доводиться лише апаратну чергу —
     *    128 байтів, тобто одиниці мілісекунд навіть на найповільнішій
     *    швидкості переліку. */
    uart_wait_tx_done(BRIDGE_UART_PORT, pdMS_TO_TICKS(20));

    /* 2. Новий дільник. */
    ESP_ERROR_CHECK(uart_set_baudrate(BRIDGE_UART_PORT, baud));

    /* 3. І аж тепер викидаємо прийняте: байти, що ловилися в мить запису,
     *    спотворені за побудовою. Порядок 2 -> 3, не навпаки. */
    uart_flush_input(BRIDGE_UART_PORT);
}

void uart_link_send_ping(void)
{
    uint8_t frame[RUI_FRAME_OVERHEAD];
    const size_t n = rui_build(frame, RUI_PKT_PING, NULL, 0);
    uart_link_write(frame, n);
}

void uart_link_send_input_release(bridge_lost_t reason)
{
    uint8_t frame[RUI_INPUT_STATE_FRAME];
    const size_t n = rui_build_input_state_zero(frame);

    uart_link_write(frame, n);

    g_stats.input_releases++;
    if (reason == BRIDGE_LOST_SILENCE) {
        g_stats.silence_timeout++;
    }

    /* Три різні події — три різні рядки. Раніше всі троє друкувались як
     * «телефон зник», і чистий старт моста повідомляв про розрив, якого не
     * було: телефона не існувало жодного разу. */
    if (reason == BRIDGE_LOST_BOOT) {
        ESP_LOGI(TAG, "старт: відпускаю ввід на пульті — він міг лишитись натиснутим "
                      "від сеансу до перезавантаження моста");
    } else if (reason == BRIDGE_LOST_SILENCE) {
        ESP_LOGW(TAG, "ввід застарів (мовчання клієнта) — відпускаю на пульті, сокет лишаю");
    } else {
        ESP_LOGW(TAG, "телефон зник (%s) — відпускаю ввід на пульті",
                 bridge_lost_name(reason));
    }
}
