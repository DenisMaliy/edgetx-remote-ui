/**
 * @file stats.c
 * @brief Лічильники моста і їх подання у JSON.
 */

#include "stats.h"

#include <stdio.h>

#include "bridge_cfg.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "ws_bridge.h"

bridge_stats_t g_stats;

/* На xtensa `uint32_t` — це `unsigned long`, тому «%u» без приведення
 * не збирається. Приведення тут беззбиткове: усі лічильники 32-бітові. */
#define U(x) ((unsigned)(x))

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
        "\"session\":{\"seen\":%u,\"lost\":%u,\"releases\":%u,\"silence_timeouts\":%u}"
        "}",
        (unsigned long long)(esp_timer_get_time() / 1000000), BRIDGE_BAUDRATE,
        U(esp_get_free_heap_size()), U(esp_get_minimum_free_heap_size()),
        ws_bridge_has_client() ? "true" : "false", U(s->uart_bytes), U(s->packets_ok),
        U(s->tiles_in), U(s->frames_in), U(s->crc_errors), U(s->oversized), U(s->uart_dropped),
        U(s->silence_resets), U(s->ws_bytes), U(s->ws_chunks), U(s->ws_errors),
        U(s->tiles_dropped), U(s->packets_dropped), U(s->chunks_dropped), U(s->chunks_noclient),
        U(s->send_dropped), U(s->client_bytes), U(s->client_packets), U(s->client_input),
        U(s->clients_seen), U(s->clients_lost), U(s->input_releases), U(s->silence_timeout));

    if (n < 0) {
        return 0;
    }
    return ((size_t)n < cap) ? (size_t)n : (cap - 1);
}
