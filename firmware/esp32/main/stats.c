/**
 * @file stats.c
 * @brief Лічильники моста і їх подання у JSON.
 */

#include "stats.h"

#include <stdio.h>

#include "bridge_cfg.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
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
    case BRIDGE_LOST_EVICTED:   return "витіснений новим телефоном";
    case BRIDGE_LOST_SEND_FAIL: return "відправлення не проходять";
    case BRIDGE_LOST_OVERSIZED: return "кадр понад стелю";
    case BRIDGE_LOST_WS_CLOSE:  return "штатне прощання";
    case BRIDGE_LOST_SOCKET:    return "сокет помер сам";
    case BRIDGE_LOST_WIFI:      return "телефон вийшов із мережі";
    case BRIDGE_LOST_SILENCE:   return "мовчання";
    }
    return "невідомо";
}

size_t bridge_stats_json(char *out, size_t cap)
{
    const bridge_stats_t *s = &g_stats;

    const int n = snprintf(
        out, cap,
        "{"
        "\"uptime_s\":%llu,"
        "\"baud\":%d,"
        "\"heap_free\":%u,"
        "\"heap_min\":%u,"
        "\"client\":%s,"
        "\"uart\":{\"bytes\":%u,\"packets\":%u,\"tiles\":%u,\"frames\":%u,"
        "\"crc_errors\":%u,\"oversized\":%u,\"dropped\":%u,\"silence_resets\":%u},"
        "\"ws\":{\"bytes\":%u,\"chunks\":%u,\"errors\":%u,"
        "\"tiles_dropped\":%u,\"packets_dropped\":%u,\"chunks_dropped\":%u,"
        "\"chunks_noclient\":%u,\"send_dropped\":%u},"
        "\"client_to_radio\":{\"bytes\":%u,\"packets\":%u,\"input\":%u},"
        "\"session\":{\"seen\":%u,\"lost\":%u,\"releases\":%u,\"silence_timeouts\":%u},"
        "\"lost_by\":{\"evicted\":%u,\"send_fail\":%u,\"oversized\":%u,"
        "\"ws_close\":%u,\"socket\":%u,\"socket_silent\":%u,\"wifi\":%u},"
        "\"input_gap\":{\"max_ms\":%u,\"le300\":%u,\"le500\":%u,\"le750\":%u,"
        "\"over\":%u,\"after_release_ms\":%u},"
        "\"input_task\":{\"beat_interval_max_ms\":%u,\"beats\":%u,\"queue_full\":%u},"
        "\"baud_reverts\":{\"bridge\":%u,\"radio\":%u}"
        "}",
        (unsigned long long)(esp_timer_get_time() / 1000000), U(s->baud_current),
        U(esp_get_free_heap_size()), U(esp_get_minimum_free_heap_size()),
        ws_bridge_has_client() ? "true" : "false", U(s->uart_bytes), U(s->packets_ok),
        U(s->tiles_in), U(s->frames_in), U(s->crc_errors), U(s->oversized), U(s->uart_dropped),
        U(s->silence_resets), U(s->ws_bytes), U(s->ws_chunks), U(s->ws_errors),
        U(s->tiles_dropped), U(s->packets_dropped), U(s->chunks_dropped), U(s->chunks_noclient),
        U(s->send_dropped), U(s->client_bytes), U(s->client_packets), U(s->client_input),
        U(s->clients_seen), U(s->clients_lost), U(s->input_releases), U(s->silence_timeout),
        U(s->lost_evicted), U(s->lost_send_fail), U(s->lost_oversized), U(s->lost_ws_close),
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
