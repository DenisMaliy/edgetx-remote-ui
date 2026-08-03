/**
 * @file stats.c
 * @brief Лічильники моста і їх подання у JSON.
 */

#include "stats.h"

#include <stdio.h>

#include "bridge_cfg.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "heap_watch.h"
#include "ws_bridge.h"

static const char *TAG = "stats";

bridge_stats_t g_stats;

/* На xtensa `uint32_t` — це `unsigned long`, тому «%u» без приведення
 * не збирається. Приведення тут беззбиткове: усі лічильники 32-бітові. */
#define U(x) ((unsigned)(x))

const char *bridge_lost_name(bridge_lost_t reason)
{
    switch (reason) {
    case BRIDGE_LOST_BOOT:      return "старт моста";
    case BRIDGE_LOST_EVICTED:   return "керування перейняв інший телефон";
    case BRIDGE_LOST_RESUMED:   return "господар повернувся на свій слот";
    case BRIDGE_LOST_SEND_FAIL: return "відправлення не проходять";
    case BRIDGE_LOST_OVERSIZED: return "кадр понад стелю";
    case BRIDGE_LOST_WS_CLOSE:  return "штатне прощання";
    case BRIDGE_LOST_SOCKET:    return "сокет помер сам";
    case BRIDGE_LOST_WIFI:      return "телефон вийшов із мережі";
    case BRIDGE_LOST_SILENCE:   return "мовчання";
    }
    return "невідомо";
}

/**
 * @brief Найбільший суцільний вільний шматок купи, з кешем.
 *
 * ⚠️ Кеш тут не заради швидкості. `heap_caps_get_largest_free_block()`
 * зводиться до `tlsf_walk_pool` по **всіх** блоках кожної купи, і робить це
 * під замком розподільника — того самого, який потрібен lwIP на pbuf.
 * Клієнт смикає `/api/stats` раз на секунду (`webui/app.js`), тобто поле,
 * поставлене заради `ENOMEM`, дістало б право його породжувати.
 *
 * П'ять секунд — компроміс: фрагментація так швидко не міняється, а прогін
 * заміру однаково дивиться на мінімум за весь час, а не на мить.
 */
static uint32_t largest_free_cached(void)
{
    static int64_t taken_us;
    static uint32_t value;

    const int64_t now = esp_timer_get_time();
    if (value == 0 || now - taken_us > 5 * 1000 * 1000) {
        value = (uint32_t)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT);
        taken_us = now;
    }
    return value;
}

size_t bridge_stats_json(char *out, size_t cap)
{
    const bridge_stats_t *s = &g_stats;

    /* Виконується в задачі httpd, тож питає саме її стек — той, на якому
     * лежить `json[]` нижче. Найменше значення за весь час: `high water
     * mark` монотонно спадає, тому просто перезаписуємо. */
    g_stats.httpd_stack_free = (uint32_t)uxTaskGetStackHighWaterMark(NULL);

    /* Прилад купи складається окремо: він має власну довжину і власну
     * перевірку на замалий буфер.
     *
     * ⚠️ 640, а не «десь 400». Довжину задають **підписи контрольних точок**,
     * а вони кирилицею, тобто по два байти на літеру: шість наявних точок
     * дають 409 Б із 420 у першій же збірці — одинадцять байтів запасу.
     * Сьома точка мовчки перетворила б увесь прилад на `null`, і виглядало б
     * це як «прилад не працює», а не як «буфер замалий». Додаєш точку —
     * звіряйся з цим числом. */
    char heap[640];
    if (heap_watch_json(heap, sizeof(heap)) == 0) {
        /* Не мовчимо і не віддаємо порожнє поле: обрізаний вкладений об'єкт
         * зламав би розбір усього JSON, а причину шукали б у клієнті. */
        static bool heap_moaned;
        if (!heap_moaned) {
            heap_moaned = true;
            ESP_LOGE(TAG, "буфер приладу купи замалий (%u Б) — поле heap віддається "
                          "порожнім; додай місця в stats.c",
                     (unsigned)sizeof(heap));
        }
        snprintf(heap, sizeof(heap), "null");
    }

    const int n = snprintf(
        out, cap,
        "{"
        "\"uptime_s\":%llu,"
        "\"baud\":%d,"
        "\"heap_free\":%u,"
        "\"heap_min\":%u,"
        "\"heap_largest\":%u,"
        /* ⚠️ `heap_min` вище — монотонний від старту й ніколи не скидається,
         * тобто після одного глибокого просідання відповідає лише на питання
         * «чи бувало колись погано». На «чи погано зараз» і «через що саме»
         * відповідає цей об'єкт (heap_watch.h). */
        "\"heap\":%s,"
        "\"httpd_stack_free\":%u,"
        "\"client\":%s,"
        "\"uart\":{\"bytes\":%u,\"packets\":%u,\"tiles\":%u,\"frames\":%u,"
        "\"crc_errors\":%u,\"oversized\":%u,\"dropped\":%u,\"silence_resets\":%u},"
        "\"ws\":{\"bytes\":%u,\"chunks\":%u,\"errors\":%u,"
        "\"tiles_dropped\":%u,\"packets_dropped\":%u,\"chunks_dropped\":%u,"
        "\"chunks_noclient\":%u,\"send_dropped\":%u,"
        "\"chunks_built\":%u,\"chunk_bytes_built\":%u,\"chunk_len_max\":%u,"
        "\"ring_free_min\":%u},"
        "\"send_err\":{\"nomem\":%u,\"again\":%u,\"conn\":%u,\"other\":%u,"
        "\"last\":%d,\"errno_last\":%d,\"ms_max\":%u,"
        "\"partial\":%u,\"truncated\":%u},"
        "\"client_to_radio\":{\"bytes\":%u,\"packets\":%u,\"input\":%u},"
        "\"session\":{\"seen\":%u,\"lost\":%u,\"releases\":%u,\"silence_timeouts\":%u,"
        "\"sockets\":%d},"
        "\"queue\":{\"busy_refused\":%u,\"takeovers\":%u,\"resumes\":%u,"
        "\"status_polls\":%u},"
        "\"lost_by\":{\"evicted\":%u,\"resumed\":%u,\"send_fail\":%u,\"oversized\":%u,"
        "\"ws_close\":%u,\"socket\":%u,\"socket_silent\":%u,\"wifi\":%u},"
        "\"input_gap\":{\"max_ms\":%u,\"le300\":%u,\"le500\":%u,\"le750\":%u,"
        "\"over\":%u,\"after_release_ms\":%u},"
        "\"input_task\":{\"beat_interval_max_ms\":%u,\"beats\":%u,\"queue_full\":%u},"
        "\"baud_reverts\":{\"bridge\":%u,\"radio\":%u}"
        "}",
        (unsigned long long)(esp_timer_get_time() / 1000000), U(s->baud_current),
        U(esp_get_free_heap_size()), U(esp_get_minimum_free_heap_size()),
        U(largest_free_cached()), heap, U(s->httpd_stack_free),
        ws_bridge_has_client() ? "true" : "false", U(s->uart_bytes), U(s->packets_ok),
        U(s->tiles_in), U(s->frames_in), U(s->crc_errors), U(s->oversized), U(s->uart_dropped),
        U(s->silence_resets), U(s->ws_bytes), U(s->ws_chunks), U(s->ws_errors),
        U(s->tiles_dropped), U(s->packets_dropped), U(s->chunks_dropped), U(s->chunks_noclient),
        U(s->send_dropped), U(s->chunks_built), U(s->chunk_bytes_built), U(s->chunk_len_max),
        U(s->ring_free_min), U(s->send_err_nomem), U(s->send_err_again), U(s->send_err_conn),
        U(s->send_err_other), (int)s->send_err_last, (int)s->send_errno_last,
        U(s->send_ms_max), U(s->send_partial), U(s->send_truncated),
        U(s->client_bytes), U(s->client_packets), U(s->client_input),
        U(s->clients_seen), U(s->clients_lost), U(s->input_releases), U(s->silence_timeout),
        ws_bridge_open_sockets(),
        U(s->busy_refused), U(s->takeovers), U(s->resumes), U(s->status_polls),
        U(s->lost_evicted), U(s->lost_resumed), U(s->lost_send_fail), U(s->lost_oversized),
        U(s->lost_ws_close),
        U(s->lost_socket), U(s->lost_socket_silent), U(s->lost_wifi), U(s->gap_max_ms),
        U(s->gap_le_300),
        U(s->gap_le_500), U(s->gap_le_750), U(s->gap_over), U(s->gap_after_release_ms),
        U(s->beat_interval_max_ms), U(s->hb_beats), U(s->hb_queue_full),
        U(s->baud_bridge_reverts), U(s->baud_radio_reverts));

    if (n < 0) {
        return 0;
    }

    /* ⚠️ Раніше тут стояло обрізання по `cap`, і це була пастка: `snprintf`
     * обрізав би JSON посеред числа, інструмент заміру впав би на розборі, а
     * виглядало б це як його власна вада — не як замалий буфер **тут**.
     * Порожня відповідь плюс рядок у журналі називають причину прямо. */
    if ((size_t)n >= cap) {
        static bool moaned;
        if (moaned) {
            return 0;
        }
        moaned = true;
        ESP_LOGE(TAG, "буфер /api/stats замалий: треба %d Б, є %u", n, (unsigned)cap);
        return 0;
    }
    return (size_t)n;
}
