/**
 * @file ws_bridge.c
 * @brief Сервер моста: сторінка клієнта по HTTP, потік протоколу по WebSocket.
 *
 * ## Один телефон за раз
 *
 * Новий телефон витісняє попередній, а не стає поруч. Причина не в економії
 * пам'яті: клієнт **шле ввід**, і два одночасні джерела натискань на пульт із
 * розбитим екраном — це спосіб зробити щось несподіване, не побачивши цього.
 *
 * Витіснення, а не відмова, — бо Wi-Fi рветься мовчки: телефон, який вийшов
 * за межу зв'язку, лишає по собі відкритий з боку моста сокет, і людина,
 * повернувшись, мала б чекати, доки TCP це помітить.
 */

#include "ws_bridge.h"

#include <string.h>
#include <unistd.h>

#include "bridge_cfg.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "framing.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "stats.h"
#include "uart_link.h"
#include "wifi_ap.h"

static const char *TAG = "ws";

/* Клієнт вбудований у прошивку з webui/ — див. main/CMakeLists.txt. */
extern const uint8_t index_html_start[] asm("_binary_index_html_start");
extern const uint8_t index_html_end[] asm("_binary_index_html_end");
extern const uint8_t proto_js_start[] asm("_binary_proto_js_start");
extern const uint8_t proto_js_end[] asm("_binary_proto_js_end");
extern const uint8_t app_js_start[] asm("_binary_app_js_start");
extern const uint8_t app_js_end[] asm("_binary_app_js_end");
extern const uint8_t style_css_start[] asm("_binary_style_css_start");
extern const uint8_t style_css_end[] asm("_binary_style_css_end");

static httpd_handle_t s_server;
static SemaphoreHandle_t s_lock;

/** Сокет телефона, або -1. Під `s_lock`. */
static int s_client_fd = -1;

/** Коли востаннє прийшов пакет із вводом, мкс. 0 — не приходив. Під `s_lock`. */
static int64_t s_last_input_us;

/** Розбирач напрямку «телефон → пульт». Потрібен лише щоб бачити типи. */
static rui_scanner_t s_client_scan;

static inline void lock(void) { xSemaphoreTake(s_lock, portMAX_DELAY); }
static inline void unlock(void) { xSemaphoreGive(s_lock); }

/* ------------------------------------------------------ стан з'єднання ----*/

bool ws_bridge_has_client(void)
{
    lock();
    const bool has = (s_client_fd >= 0);
    unlock();
    return has;
}

uint32_t ws_bridge_input_silence_ms(void)
{
    lock();
    const int fd = s_client_fd;
    const int64_t last = s_last_input_us;
    unlock();

    if (fd < 0 || last == 0) {
        return UINT32_MAX;
    }
    return (uint32_t)((esp_timer_get_time() - last) / 1000);
}

/**
 * @brief Спільна частина втрати телефона.
 *
 * @param expect_fd  скинути, лише якщо поточний сокет саме цей; `-1` — будь-який.
 *
 * ⚠️ `expect_fd` не про акуратність, а про справжню гонку. Закриття сокета
 * і поява нового телефона — події з різних задач. Без перевірки виходило б
 * так: сокет A закривається, у цю мить під'єднується телефон B, і
 * запізнілий обробник закриття A скидав би **B** — новий телефон одразу
 * ставав би «загубленим», сторінка на ньому завмирала б, а причину шукали б
 * у Wi-Fi.
 */
static void client_drop(int expect_fd, bool silence, bool close_socket)
{
    lock();
    const int fd = s_client_fd;
    const bool mine = (fd >= 0) && (expect_fd < 0 || expect_fd == fd);
    if (mine) {
        s_client_fd = -1;
        s_last_input_us = 0;
    }
    unlock();

    if (!mine) {
        return; /* уже загубили, або це вже не наш сокет */
    }

    g_stats.clients_lost++;

    /* ⚠️ Найважливіші два рядки в усьому мості. Пульт відпускає ввід сам лише
     * через 1000 мс тиші; ми зобов'язані зробити це негайно. */
    uart_link_send_input_release(silence);

    if (close_socket && s_server) {
        httpd_sess_trigger_close(s_server, fd);
    }
}

void ws_bridge_client_lost(bool silence) { client_drop(-1, silence, true); }

/**
 * @brief Сокет закрився сам.
 *
 * ⚠️ Виставивши `close_fn`, ми забрали в httpd штатне закриття — тепер
 * закрити дескриптор зобов'язані ми, інакше вони закінчаться.
 */
static void on_sock_close(httpd_handle_t hd, int sockfd)
{
    (void)hd;

    /* Сокет уже закривається сам — нам лишається скинути стан і відпустити
     * ввід, і то лише якщо телефоном був саме цей сокет. */
    client_drop(sockfd, false, false);
    close(sockfd);
}

static void client_attach(int fd)
{
    lock();
    const int old = s_client_fd;
    s_client_fd = fd;
    s_last_input_us = 0;
    unlock();

    rui_scanner_reset(&s_client_scan);
    g_stats.clients_seen++;

    if (old >= 0 && old != fd) {
        /* Витісняємо попередній телефон. Ввід обнуляємо: старий міг щось
         * утримувати, а новий заявить власний стан не пізніше ніж за 250 мс
         * (docs/03-protocol.md, INPUT_STATE — рівень, а не перехід). */
        ESP_LOGW(TAG, "новий телефон витісняє попередній (сокет %d → %d)", old, fd);
        uart_link_send_input_release(false);
        g_stats.clients_lost++;
        if (s_server) {
            httpd_sess_trigger_close(s_server, old);
        }
    }

    ESP_LOGI(TAG, "телефон під'єднався, сокет %d", fd);
}

/* --------------------------------------------------------- передача -------*/

esp_err_t ws_bridge_send(const uint8_t *data, size_t len)
{
    lock();
    const int fd = s_client_fd;
    unlock();

    if (fd < 0 || !s_server) {
        return ESP_ERR_INVALID_STATE;
    }

    httpd_ws_frame_t frame = {
        .final = true,
        .fragmented = false,
        .type = HTTPD_WS_TYPE_BINARY,
        .payload = (uint8_t *)data,
        .len = len,
    };

    const esp_err_t err = httpd_ws_send_frame_async(s_server, fd, &frame);
    if (err != ESP_OK) {
        g_stats.ws_errors++;
        ESP_LOGW(TAG, "відправлення не вдалося (%s) — вважаю телефон загубленим",
                 esp_err_to_name(err));
        /* Саме `fd`, а не «поточний»: доки ми відправляли, телефон міг уже
         * змінитися, і скидати треба той сокет, на якому справді впало. */
        client_drop(fd, false, true);
        return err;
    }

    g_stats.ws_bytes += len;
    g_stats.ws_chunks++;
    return ESP_OK;
}

/* --------------------------------------------------------- приймання ------*/

static void on_client_packet(void *ctx, uint8_t type, const uint8_t *frame, size_t len)
{
    (void)ctx;
    (void)frame;
    (void)len;

    g_stats.client_packets++;

    /* ⚠️ Живим клієнта роблять тільки пакети, що несуть ввід. `PING` і
     * `REFRESH` — ні, і це те саме правило, що діє в прошивці пульта. Інакше
     * достатньо було б, щоб телефон лишався «живим» на самих лише пінгах,
     * тримаючи натиснутою клавішу, яку вже ніхто не відпустить. */
    if (rui_carries_input(type)) {
        g_stats.client_input++;
        lock();
        s_last_input_us = esp_timer_get_time();
        unlock();
    }
}

static esp_err_t ws_handler(httpd_req_t *req)
{
    if (req->method == HTTP_GET) {
        /* Рукостискання WebSocket завершено — сокет наш. */
        client_attach(httpd_req_to_sockfd(req));
        return ESP_OK;
    }

    httpd_ws_frame_t frame = {0};
    esp_err_t err = httpd_ws_recv_frame(req, &frame, 0);
    if (err != ESP_OK) {
        return err;
    }

    if (frame.type == HTTPD_WS_TYPE_CLOSE) {
        ws_bridge_client_lost(false);
        return ESP_OK;
    }

    if (frame.len == 0) {
        return ESP_OK;
    }

    if (frame.len > BRIDGE_WS_RX_MAX) {
        /* У цьому напрямку найбільший пакет — `INPUT_STATE`, 20 Б. Кілобайт
         * означає, що на тому кінці не наш клієнт. */
        ESP_LOGW(TAG, "кадр від клієнта %u Б понад стелю — рву з'єднання",
                 (unsigned)frame.len);
        ws_bridge_client_lost(false);
        return ESP_FAIL;
    }

    uint8_t buf[BRIDGE_WS_RX_MAX];
    frame.payload = buf;
    err = httpd_ws_recv_frame(req, &frame, frame.len);
    if (err != ESP_OK) {
        return err;
    }

    /* 1. Пересилаємо як є. Міст не тлумачить вмісту: що клієнт надіслав, те
     *    й іде на дріт. Навіть якщо наш розбирач нижче чогось не зрозуміє. */
    uart_link_write(buf, frame.len);
    g_stats.client_bytes += frame.len;

    /* 2. Окремо дивимось на типи — виключно щоб знати, чи телефон живий. */
    rui_scanner_feed(&s_client_scan, buf, frame.len, on_client_packet, NULL);

    return ESP_OK;
}

/* ------------------------------------------------------------ сторінка ----*/

static esp_err_t send_blob(httpd_req_t *req, const char *ctype, const uint8_t *start,
                           const uint8_t *end)
{
    httpd_resp_set_type(req, ctype);
    /* Без кешу: сторінка живе в прошивці й міняється разом із нею. */
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    return httpd_resp_send(req, (const char *)start, (size_t)(end - start));
}

static esp_err_t index_get(httpd_req_t *req)
{
    return send_blob(req, "text/html; charset=utf-8", index_html_start, index_html_end);
}

static esp_err_t protojs_get(httpd_req_t *req)
{
    return send_blob(req, "application/javascript; charset=utf-8", proto_js_start, proto_js_end);
}

static esp_err_t appjs_get(httpd_req_t *req)
{
    return send_blob(req, "application/javascript; charset=utf-8", app_js_start, app_js_end);
}

static esp_err_t css_get(httpd_req_t *req)
{
    return send_blob(req, "text/css; charset=utf-8", style_css_start, style_css_end);
}

static esp_err_t stats_get(httpd_req_t *req)
{
    char json[768];
    const size_t n = bridge_stats_json(json, sizeof(json));
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    return httpd_resp_send(req, json, n);
}

/**
 * @brief Вимкнути Wi-Fi на мості.
 *
 * Навіщо: поруч працює радіомодуль пульта, і перед польотом зайвий передавач
 * у корпусі можна прибрати, не розбираючи нічого. Увімкнути назад —
 * перезавантаженням моста: команду вимкнення ми виконуємо, обірвавши той
 * самий канал, яким вона прийшла, тому іншого шляху тут і не буває.
 */
static esp_err_t wifi_off_post(httpd_req_t *req)
{
    httpd_resp_set_type(req, "application/json");
    httpd_resp_sendstr(req, "{\"ok\":true,\"note\":\"Wi-Fi вимикається; увімкнути назад — "
                            "перезавантаженням моста\"}");

    /* Вимикаємо із затримкою й з іншої задачі, інакше відповідь не доїде. */
    wifi_ap_stop_deferred(300);
    return ESP_OK;
}

/* ------------------------------------------------------------- запуск -----*/

esp_err_t ws_bridge_start(void)
{
    s_lock = xSemaphoreCreateMutex();
    if (!s_lock) {
        return ESP_ERR_NO_MEM;
    }
    rui_scanner_reset(&s_client_scan);

    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.stack_size = 8192; /* у обробнику лежить буфер на BRIDGE_WS_RX_MAX */
    cfg.max_open_sockets = 4;
    /* Типова стеля — 8, а ми реєструємо рівно 8. Наступний доданий шлях
     * упав би не при збірці, а на старті моста, у полі. */
    cfg.max_uri_handlers = 12;
    cfg.lru_purge_enable = true;
    cfg.close_fn = on_sock_close;
    cfg.send_wait_timeout = 2;
    cfg.recv_wait_timeout = 10;
#if CONFIG_IDF_TARGET_ESP32
    cfg.core_id = 0; /* разом із Wi-Fi; приймання UART живе на ядрі 1 */
#endif

    ESP_ERROR_CHECK(httpd_start(&s_server, &cfg));

    static const httpd_uri_t uris[] = {
        {.uri = "/", .method = HTTP_GET, .handler = index_get},
        {.uri = "/index.html", .method = HTTP_GET, .handler = index_get},
        {.uri = "/proto.js", .method = HTTP_GET, .handler = protojs_get},
        {.uri = "/app.js", .method = HTTP_GET, .handler = appjs_get},
        {.uri = "/style.css", .method = HTTP_GET, .handler = css_get},
        {.uri = "/api/stats", .method = HTTP_GET, .handler = stats_get},
        {.uri = "/api/wifi/off", .method = HTTP_POST, .handler = wifi_off_post},
        {.uri = "/ws", .method = HTTP_GET, .handler = ws_handler, .is_websocket = true},
    };

    for (size_t i = 0; i < sizeof(uris) / sizeof(uris[0]); ++i) {
        ESP_ERROR_CHECK(httpd_register_uri_handler(s_server, &uris[i]));
    }

    ESP_LOGI(TAG, "сервер піднято: сторінка на /, потік на /ws, лічильники на /api/stats");
    return ESP_OK;
}
