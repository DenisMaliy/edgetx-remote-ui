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

#include <errno.h>
#include <string.h>
#include <unistd.h>

#include "bridge_cfg.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "framing.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "lwip/sockets.h"
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

/** Ввід уже відпущено за мовчанням — вдруге не відпускаємо. Під `s_lock`. */
static bool s_input_released;

/** Скільки відправлень поспіль не вдалося. Пише лише `ws_tx_task`. */
static uint32_t s_send_fails;

/**
 * Скільки було `ws_errors` у мить під'єднання цього телефона.
 *
 * Потрібно рівно для одного висновку: сокет помер **мовчки**. Якщо `close_fn`
 * спрацював, а лічильник помилок за весь сеанс не зрушив, то міст не робив
 * нічого — сеанс помер піді мною. Саме цей випадок у задачі 0012 не мав
 * жодної ознаки, і причину шукали б у живленні.
 */
static uint32_t s_errors_at_attach;

/** Розбирач напрямку «телефон → пульт». Потрібен лише щоб бачити типи. */
static rui_scanner_t s_client_scan;

/** Наша передача з дозаписом залишку. Оголошена тут, бо ставиться при
 *  під'єднанні, а живе нижче, серед решти передачі. */
static int send_all(httpd_handle_t hd, int sockfd, const char *buf, size_t buf_len, int flags);

/**
 * `errno` тієї самої миті, коли `send()` відмовив. Пише `send_all`, читає
 * `note_send_error`. Обидва — в `ws_tx_task`.
 *
 * ⚠️ Живого `errno` там читати не можна, і це не педантизм. Між невдалим
 * `send()` і поверненням із `httpd_ws_send_frame_async` стоять чужі
 * `ESP_LOGW` (`httpd_ws.c:449,456`), а журнал у цьому проєкті — синхронний
 * запис у UART0 на 115200. Тобто до нашого читання `errno` устигає
 * побувати в чужих руках.
 */
static int s_last_send_errno;

/** Потік WebSocket зіпсовано обрізаним кадром — сокет далі не придатний. */
static bool s_stream_corrupt;

static inline void lock(void) { xSemaphoreTake(s_lock, portMAX_DELAY); }
static inline void unlock(void) { xSemaphoreGive(s_lock); }

/* ------------------------------------------------- биття задачі вводу -----*/

/** Коли задача сервера востаннє прокидалась на наше биття, мкс. */
static int64_t s_hb_last_us;

/**
 * @brief Виконується **в задачі сервера httpd** — саме в тій, що чує ввід.
 *
 * Нічого не робить, крім позначки часу. Уся цінність у тому, **коли** вона
 * виконалась: різниця між двома позначками — це затримка планування задачі,
 * яка розбирає пакети телефона.
 */
static void hb_work(void *arg)
{
    (void)arg;
    const int64_t now = esp_timer_get_time();

    if (s_hb_last_us != 0) {
        const uint32_t dt = (uint32_t)((now - s_hb_last_us) / 1000);
        if (dt > g_stats.beat_interval_max_ms) {
            g_stats.beat_interval_max_ms = dt;
        }
    }
    s_hb_last_us = now;
    g_stats.hb_beats++;
}

void ws_bridge_heartbeat(void)
{
    if (!s_server) {
        return;
    }

    /* ⚠️ Виклик **не безкоштовний і не миттєвий**, хоч і зветься чергою.
     * `httpd_queue_work()` шле дейтаграму на керівний сокет, а без
     * `CONFIG_LWIP_TCPIP_CORE_LOCKING` lwIP кладе її в чергу задачі tcpip і
     * чекає на семафорі, доки та виконає. Тобто `rx_task`, про яку сказано
     * «не має права чекати», десять разів на секунду таки чекає на
     * мережевому стеку.
     *
     * Зараз це поглинається: пріоритет tcpip (18) вищий за наш, а приймальне
     * кільце UART тримає ~178 мс потоку на 921600. ⚠️ **На 2 Мбод запас падає
     * удвічі**, а прилад планують лишити саме для того переходу — тоді це
     * треба переміряти, а не припустити.
     *
     * `CONFIG_HTTPD_QUEUE_WORK_BLOCKING` мусить лишатись вимкненим
     * (закріплено в `sdkconfig.defaults`): з ним семафор віддається лише
     * **після** виконання роботи в задачі httpd, тобто приймання UART
     * зупинялося б рівно на той час, який ми міряємо. */
    if (httpd_queue_work(s_server, hb_work, NULL) != ESP_OK) {
        g_stats.hb_queue_full++;
    }
}

/* ------------------------------------------------------ стан з'єднання ----*/

bool ws_bridge_has_client(void)
{
    lock();
    const bool has = (s_client_fd >= 0);
    unlock();
    return has;
}

int ws_bridge_client_fd(void)
{
    lock();
    const int fd = s_client_fd;
    unlock();
    return fd;
}

bool ws_bridge_release_if_silent(uint32_t silence_ms)
{
    /* Рішення й засувка — під одним замком: інакше між «побачив мовчання» і
     * «відпустив» устигає під'єднатися новий телефон, і ми відпустимо його
     * ввід. Та сама гонка, що й із сокетом, тільки тихіша. */
    lock();
    const bool due = (s_client_fd >= 0) && (s_last_input_us != 0) && !s_input_released &&
                     ((esp_timer_get_time() - s_last_input_us) / 1000 > (int64_t)silence_ms);
    if (due) {
        s_input_released = true;
    }
    unlock();

    if (!due) {
        return false;
    }

    /* Сокет лишається живим — телефон міг просто згасити екран. */
    uart_link_send_input_release(BRIDGE_LOST_SILENCE);
    return true;
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
static void client_drop(int expect_fd, bridge_lost_t reason, bool close_socket)
{
    lock();
    const int fd = s_client_fd;
    const bool mine = (fd >= 0) && (expect_fd < 0 || expect_fd == fd);
    if (mine) {
        s_client_fd = -1;
        s_last_input_us = 0;
        s_input_released = false;
    }
    unlock();

    if (!mine) {
        return; /* уже загубили, або це вже не наш сокет */
    }

    g_stats.clients_lost++;

    /* Називаємо причину. Сума цих шести дорівнює `clients_lost` — розбіжність
     * означала б шлях втрати, про який ми не знаємо. */
    switch (reason) {
    case BRIDGE_LOST_EVICTED:   g_stats.lost_evicted++; break;
    case BRIDGE_LOST_SEND_FAIL: g_stats.lost_send_fail++; break;
    case BRIDGE_LOST_OVERSIZED: g_stats.lost_oversized++; break;
    case BRIDGE_LOST_WS_CLOSE:  g_stats.lost_ws_close++; break;
    case BRIDGE_LOST_WIFI:
        g_stats.lost_wifi++;
        break;
    case BRIDGE_LOST_SOCKET:
        g_stats.lost_socket++;
        /* ⚠️ Той самий випадок, що в 0012 не мав жодної ознаки: сокет помер, а
         * міст за весь сеанс не мав **жодної** невдачі відправлення. Отже це
         * не затор і не наша дія — сеанс помер піді мною. */
        if (g_stats.ws_errors == s_errors_at_attach) {
            g_stats.lost_socket_silent++;
        }
        break;
    case BRIDGE_LOST_BOOT:
    case BRIDGE_LOST_SILENCE:
        /* Сюди не потрапляють: старт не має сеансу, а сторож сокет не рве. */
        break;
    }

    /* ⚠️ Найважливіші два рядки в усьому мості. Пульт відпускає ввід сам лише
     * через 1000 мс тиші; ми зобов'язані зробити це негайно. */
    uart_link_send_input_release(reason);

    if (close_socket && s_server) {
        httpd_sess_trigger_close(s_server, fd);
    }
}

void ws_bridge_client_lost(int expect_fd, bridge_lost_t reason)
{
    client_drop(expect_fd, reason, true);
}

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
    client_drop(sockfd, BRIDGE_LOST_SOCKET, false);
    close(sockfd);
}

static void client_attach(int fd)
{
    lock();
    const int old = s_client_fd;
    s_client_fd = fd;
    s_last_input_us = 0;
    s_input_released = false;
    unlock();

    s_send_fails = 0;
    s_last_send_errno = 0;
    s_stream_corrupt = false;
    s_errors_at_attach = g_stats.ws_errors;
    rui_scanner_reset(&s_client_scan);
    g_stats.clients_seen++;

    /* ⚠️ Своя функція відправлення — на цей сокет і тільки на нього. Штатна
     * мовчки лишає кадр WebSocket недописаним (див. `send_all`), і саме це
     * найімовірніше вбивало сеанси «без жодної ознаки» в задачі 0012. */
    if (s_server) {
        const esp_err_t ov = httpd_sess_set_send_override(s_server, fd, send_all);
        if (ov != ESP_OK) {
            /* Не падаємо: міст без дозапису працює так само, як працював
             * досі. Але мовчати не можна — інакше замір списав би обрізані
             * кадри на щось інше. */
            ESP_LOGE(TAG, "не вдалося поставити свою передачу на сокет %d (%s) — "
                          "обрізані кадри WebSocket лишаються можливими",
                     fd, esp_err_to_name(ov));
        }
    }

    if (old >= 0 && old != fd) {
        /* Витісняємо попередній телефон. Ввід обнуляємо: старий міг щось
         * утримувати, а новий заявить власний стан не пізніше ніж за 250 мс
         * (docs/03-protocol.md, INPUT_STATE — рівень, а не перехід). */
        ESP_LOGW(TAG, "новий телефон витісняє попередній (сокет %d → %d)", old, fd);
        uart_link_send_input_release(BRIDGE_LOST_EVICTED);
        g_stats.clients_lost++;
        g_stats.lost_evicted++;
        if (s_server) {
            httpd_sess_trigger_close(s_server, old);
        }
    }

    ESP_LOGI(TAG, "телефон під'єднався, сокет %d", fd);
}

/* --------------------------------------------------------- передача -------*/

/**
 * @brief Відправити **все** або чесно сказати, що не змогло.
 *
 * ⚠️ Це не оптимізація, а виправлення мовчазного псування потоку.
 *
 * Штатний `httpd_default_send` віддає скільки записалось, а
 * `httpd_ws_send_frame_async` перевіряє лише `< 0` (`httpd_ws.c:448,455`) —
 * циклу дозапису в нього немає. При цьому lwIP із виставленим `SO_SNDTIMEO`
 * (у нас `cfg.send_wait_timeout = 2`) по спливанні тайм-ауту повертає
 * **часткове відправлення як успіх**: `api_msg.c:1745-1754`, гілка
 * «partial write → err = ERR_OK».
 *
 * Разом це означає, що міст здатен віддати **обрізаний кадр WebSocket** і
 * зарахувати його в `ws_chunks` як успішний. Кадрування WebSocket
 * самосинхронізації не має: клієнт дочитає хвіст наступного кадру як вантаж
 * попереднього, далі прочитає вантаж як заголовок — і потік поїде назавжди.
 * Наш протокол усередині цього ресинхронізується (маркер + CRC), а от сам
 * WebSocket — ні, і браузер закриє з'єднання за порушенням.
 *
 * ⚠️ Найімовірніше це і є «сокет помер мовчки, `ws_errors` нулі» із задачі
 * 0012: часткове відправлення успіхом **не рахується як помилка**, тож
 * лічильникам не було чого показати.
 *
 * Тому: дозаписуємо залишок, поки є поступ і поки не вичерпано бюджет часу.
 * Не встигли, а частина вже пішла — потік зіпсований, і єдине чесне рішення
 * гасити сокет. Мовчки лишати його живим означало б і далі годувати клієнта
 * сміттям.
 *
 * `send()` кличеться напряму, а не через `httpd_default_send`, рівно з двох
 * причин: той друкує в журнал на кожній відмові (`httpd_txrx.c:743`, тобто
 * мілісекунди блокування `ws_tx_task` у найгіршу мить) і затирає `errno`,
 * заради якого все й робиться.
 */
static int send_all(httpd_handle_t hd, int sockfd, const char *buf, size_t buf_len, int flags)
{
    (void)hd;

    if (!buf) {
        return HTTPD_SOCK_ERR_INVALID;
    }

    const int64_t deadline = esp_timer_get_time() + (int64_t)BRIDGE_WS_SEND_BUDGET_MS * 1000;
    size_t off = 0;

    while (off < buf_len) {
        const int n = send(sockfd, buf + off, buf_len - off, flags);

        if (n > 0) {
            off += (size_t)n;
            if (off < buf_len) {
                g_stats.send_partial++;
            }
            continue;
        }

        /* `n == 0` при ненульовій довжині TCP не повертає, але нескінченний
         * цикл коштував би моста — вважаємо це відмовою без `errno`. */
        s_last_send_errno = (n == 0) ? 0 : errno;

        if (off > 0) {
            /* Частина кадру вже на дроті. Обрізаний кадр WebSocket
             * невиправний — далі говорити в цей сокет нема сенсу. */
            g_stats.send_truncated++;
            s_stream_corrupt = true;
        }
        if (n == 0) {
            return HTTPD_SOCK_ERR_FAIL;
        }
        return (errno == EAGAIN || errno == EINTR) ? HTTPD_SOCK_ERR_TIMEOUT
                                                   : HTTPD_SOCK_ERR_FAIL;
    }

    if (esp_timer_get_time() > deadline && off < buf_len) {
        g_stats.send_truncated++;
        s_stream_corrupt = true;
        return HTTPD_SOCK_ERR_TIMEOUT;
    }

    return (int)buf_len;
}

/**
 * @brief Назвати причину невдалого відправлення.
 *
 * ⚠️ Задача 0018 вимагає розрізнити «затор у TCP» і «в мості скінчилась
 * пам'ять». Обидва приходять сюди однаково — `ESP_FAIL` від
 * `httpd_ws_send_frame_async`, — але означають протилежне: перше нормальна
 * робота під навантаженням, друге межа виживання моста.
 *
 * ⚠️ Коли `httpd` відмовив **до** самого `send()` — сокет не наш, це не
 * WebSocket, — збереженого `errno` про цю відмову немає, і причини він не
 * називає. Такі йдуть у `other`, а `send_err_last` показує, що це був не
 * `ESP_FAIL`.
 */
static void note_send_error(esp_err_t err)
{
    const int e = s_last_send_errno;

    g_stats.send_err_last = (int32_t)err;
    g_stats.send_errno_last = (int32_t)e;

    if (err != ESP_FAIL) {
        g_stats.send_err_other++;
        return;
    }

    switch (e) {
    case ENOMEM:
    case ENOBUFS:
        g_stats.send_err_nomem++;
        break;
    case EAGAIN:
#if defined(EWOULDBLOCK) && (EWOULDBLOCK != EAGAIN)
    case EWOULDBLOCK:
#endif
        g_stats.send_err_again++;
        break;
    case EPIPE:
    case ECONNRESET:
    case ECONNABORTED:
    case ENOTCONN:
    case EBADF:
        g_stats.send_err_conn++;
        break;
    default:
        g_stats.send_err_other++;
        break;
    }
}

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

    /* ⚠️ Час самого відправлення — прилад, без якого розкладка причин
     * читається навиворіт. `cfg.send_wait_timeout = 2` означає, що
     * «вікно TCP зачинене» — це не мить, а **дві секунди сну** `ws_tx_task`.
     * За них при 260 КБ/с надходить понад 500 КБ, а черга — 24 КБ. Тобто
     * `chunks_dropped` виросте як **наслідок** заснулого відправника, і без
     * цього числа «винна повна черга» виглядало б доведеним. */
    const int64_t t_send = esp_timer_get_time();
    const esp_err_t err = httpd_ws_send_frame_async(s_server, fd, &frame);
    const uint32_t send_ms = (uint32_t)((esp_timer_get_time() - t_send) / 1000);
    if (send_ms > g_stats.send_ms_max) {
        g_stats.send_ms_max = send_ms;
    }

    if (s_stream_corrupt) {
        /* Кадр WebSocket пішов обрізаним — потік невиправний. Тримати такий
         * сокет означає годувати клієнта сміттям, тож гасимо його названою
         * причиною, а не лишаємо помирати мовчки. */
        ESP_LOGW(TAG, "кадр WebSocket пішов обрізаним — потік зіпсовано, рву сокет %d", fd);
        s_stream_corrupt = false;
        g_stats.ws_errors++;
        g_stats.send_dropped++;
        note_send_error(err == ESP_OK ? ESP_FAIL : err);
        client_drop(fd, BRIDGE_LOST_SEND_FAIL, true);
        return ESP_FAIL;
    }

    if (err != ESP_OK) {
        /* ⚠️ Невдале відправлення — це **викинута пачка**, а не «телефона
         * немає». Серед причин тут банальне переповнення вікна TCP, тобто
         * рівно той затор, на який ухвалена плавна деградація. Розрив
         * коштував би перепідключення й `REFRESH` на 20–40 КБ у той самий
         * затор — і знову розриву. Вирішувати, що телефона немає, має
         * сторож мовчання, а не черга передачі. */
        note_send_error(err);
        g_stats.ws_errors++;
        g_stats.send_dropped++;
        s_send_fails++;

        if (s_send_fails >= BRIDGE_WS_SEND_FAILS_MAX) {
            ESP_LOGW(TAG, "%u відправлень поспіль не вдалося (%s) — сокет мертвий",
                     (unsigned)s_send_fails, esp_err_to_name(err));
            s_send_fails = 0;
            /* Саме `fd`, а не «поточний»: доки ми відправляли, телефон міг
             * уже змінитися, і гасити треба той сокет, на якому впало. */
            client_drop(fd, BRIDGE_LOST_SEND_FAIL, true);
        }
        return err;
    }

    s_send_fails = 0;
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
    if (!rui_carries_input(type)) {
        return;
    }

    g_stats.client_input++;

    lock();
    const int64_t now = esp_timer_get_time();
    const int64_t prev = s_last_input_us;
    /* Чи це перший пакет після того, як сторож відпустив ввід. Знімаємо
     * засувку тут-таки: наступне мовчання буде новим епізодом. */
    const bool after_release = s_input_released;
    s_last_input_us = now;
    s_input_released = false;
    unlock();

    if (prev == 0) {
        return; /* перший пакет сеансу — паузи ще немає */
    }

    /* ⚠️ Це і є прилад для питання «чому сторож спрацював посеред живої
     * сесії». Пауза міряється тут, тобто в момент, коли пакет **розібрала
     * задача httpd**. Якщо клієнт стверджує, що слав рівно раз на 250 мс, а
     * тут стоїть 900 — час загубився між ними, і шукати треба в мережі або в
     * мості. Якщо обидва боки кажуть 900 — не слав і клієнт. */
    const uint32_t gap = (uint32_t)((now - prev) / 1000);

    if (gap > g_stats.gap_max_ms) {
        g_stats.gap_max_ms = gap;
    }
    if (gap <= 300) {
        g_stats.gap_le_300++;
    } else if (gap <= 500) {
        g_stats.gap_le_500++;
    } else if (gap <= BRIDGE_CLIENT_SILENCE_MS) {
        g_stats.gap_le_750++;
    } else {
        g_stats.gap_over++;
    }

    if (after_release) {
        /* Тільки тепер пауза, що спричинила відпускання, відома цілком:
         * сторож знав про неї лише «більше за поріг». */
        g_stats.gap_after_release_ms = gap;
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

    const int fd = httpd_req_to_sockfd(req);

    if (frame.type == HTTPD_WS_TYPE_CLOSE) {
        ws_bridge_client_lost(fd, BRIDGE_LOST_WS_CLOSE);
        return ESP_OK;
    }

    /* ⚠️ Тільки двійкові кадри. Наш клієнт шле виключно їх; текстовий кадр
     * означає, що на тому кінці не наш клієнт, і згодовувати його розбирачу
     * протоколу нема сенсу. */
    if (frame.type != HTTPD_WS_TYPE_BINARY) {
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
        ws_bridge_client_lost(fd, BRIDGE_LOST_OVERSIZED);
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
    /* Росте разом із набором лічильників. Замалий буфер більше не обрізає
     * JSON мовчки — `bridge_stats_json()` скаржиться в журнал. */
    char json[1792];
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

esp_err_t ws_bridge_init(void)
{
    /* ⚠️ Замок створюється тут, а не в `ws_bridge_start()`, і це не
     * причісування. `esp_wifi_start()` уже здатен покликати обробник події,
     * а той бере цей замок: у вікні між підняттям Wi-Fi і стартом сервера
     * `xSemaphoreTake(NULL)` дав би паніку. Виглядало б це як
     * перезавантаження моста на старті — тобто **точно як просадка живлення
     * від кидка струму**, і шукали б у конденсаторі. */
    s_lock = xSemaphoreCreateMutex();
    if (!s_lock) {
        return ESP_ERR_NO_MEM;
    }
    rui_scanner_reset(&s_client_scan);
    return ESP_OK;
}

esp_err_t ws_bridge_start(void)
{
    if (!s_lock) {
        ESP_LOGE(TAG, "ws_bridge_init() не викликано");
        return ESP_ERR_INVALID_STATE;
    }

    /* ⚠️ Чужі попередження в гарячому шляху — не шум, а затримка.
     *
     * `httpd_ws.c:449,456` друкує два рядки на **кожне** невдале
     * відправлення, а консоль — це UART0 на 115200 без драйвера, тобто
     * синхронне очікування FIFO в задачі, що друкує. Разом ≈90 Б ≈ 7.5 мс, і
     * блокує це `ws_tx_task` рівно тоді, коли черга й так переповнюється.
     * Замір показав би «винна повна черга», а винен був би журнал.
     *
     * `httpd_txrx` глушиться за компанію: нашого шляху він більше не
     * стосується (`send_all` кличе `send()` напряму), але тим самим рядком
     * закривається і решта його балакучості під навантаженням.
     */
    esp_log_level_set("httpd_ws", ESP_LOG_ERROR);
    esp_log_level_set("httpd_txrx", ESP_LOG_ERROR);

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

    /* Пріоритет задачі httpd — чуже число, а ввід від телефона розбирає саме
     * вона. Друкуємо його поруч із нашими, щоб на стенді не доводилось лізти
     * в джерела ESP-IDF: у задачі 0013 цей розклад був першим підозрюваним, і
     * ніде в коді його не було видно цілком. Чи він на щось впливає, показує
     * `input_task.beat_interval_max_ms` у `/api/stats`, а не міркування. */
    ESP_LOGI(TAG, "пріоритети: приймання %d, ввід (httpd) %d, сторож %d, пікселі %d",
             BRIDGE_PRIO_RX, cfg.task_priority, BRIDGE_PRIO_WATCHDOG, BRIDGE_PRIO_WS_TX);

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
