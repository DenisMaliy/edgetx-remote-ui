/**
 * @file ws_bridge.c
 * @brief Сервер моста: сторінка клієнта по HTTP, потік протоколу по WebSocket.
 *
 * ## Один телефон за раз, і витіснення тільки на вимогу
 *
 * Керує пультом рівно один телефон. Причина не в економії пам'яті: клієнт
 * **шле ввід**, і два одночасні джерела натискань на пульт із розбитим
 * екраном — це спосіб зробити щось несподіване, не побачивши цього.
 *
 * ⚠️ **Доти новий телефон витісняв попереднього мовчки, і це давало не
 * одного господаря, а гойдалку** (задача 0024): витіснений клієнт вважав це
 * обривом і повертався через секунду, витісняючи того, хто щойно витіснив
 * його. Заміряно в 0023: очікування `HELLO` свіжим вікном 25 с замість 0.3 с,
 * прогін приладу не доживав до половини, купа моста просідала до межі
 * тривоги.
 *
 * Тепер прибулець мусить **назватися**, і міст розрізняє три випадки:
 *
 * | що в рядку запиту `/ws` | коли так буває | що робить міст |
 * |---|---|---|
 * | нічого                  | звичайне відкриття сторінки | зайнято — «busy» і закрити сокет |
 * | `take=1`                | натиснуто «Перейняти керування» | витісняє, `lost_evicted` |
 * | `resume=1&id=…` свого   | сокет господаря помер, він вертається | тихо забирає своє, `lost_resumed` |
 *
 * ⚠️ Третій рядок діє лише **коли господаря не чути** довше за
 * `BRIDGE_CLIENT_SILENCE_MS`. Живого господаря `resume` не зсуває: збіг
 * імені при живому сокеті означає наш власний другий сокет, і «повернення
 * свого» стало б витісненням самого себе — гойдалкою з одного клієнта.
 *
 * Ім'я (`id`) клієнт вигадує собі сам на кожне завантаження сторінки; міст
 * його лише зберігає й порівнює. Без імені `resume` не діє: інакше будь-хто
 * забирав би керування, назвавшись «це знову я».
 *
 * ⚠️ Що втрачено разом із мовчазним витісненням: телефон, який вийшов за межу
 * зв'язку, лишає по собі відкритий з боку моста сокет, і повернувшись, він
 * більше не заходить сам собою. Дірку затуляє саме `resume`: сторінка на тому
 * телефоні жива, ім'я в неї те саме, і вона забирає своє без питань. А от
 * **інший** пристрій побачить «зайнято» і мусить натиснути кнопку — так і
 * задумано, це свідома дія.
 *
 * Той, кому сказали «зайнято», не тримає сокета: цей міст гасить одразу, а
 * `/api/status` віддає відповідь із `Connection: close`. Тобто між
 * опитуваннями раз на дві секунди в черзі не лишається **жодного** з'єднання —
 * інакше забута вкладка постійно займала б одне гніздо з чотирьох.
 */

#include "ws_bridge.h"

#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#include "bridge_cfg.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "framing.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "heap_watch.h"
#include "lwip/sockets.h"
#include "stats.h"
#include "uart_link.h"
#include "wifi_ap.h"

static const char *TAG = "ws";

/**
 * Клієнт вбудований у прошивку з webui/ — див. main/CMakeLists.txt.
 *
 * ⚠️ Вбудовується **стиснене**, і це не про місце у флеші. Заміри 0021:
 * віддача одного файлу на 67 КБ забирає з купи моста 61 КБ, бо стільки його
 * одночасно лежить у черзі TCP і в буферах Wi-Fi. Тобто ціна сторінки в
 * пам'яті — це її розмір **на дроті**, і єдиний спосіб її зменшити, не
 * ламаючи ні клієнта, ні протокол, — щоб на дроті було менше байтів.
 *
 * Розпаковує браузер, безкоштовно для нас: жодного рядка на розпакування в
 * мості немає, є лише заголовок `Content-Encoding`.
 */
extern const uint8_t index_html_start[] asm("_binary_index_html_gz_start");
extern const uint8_t index_html_end[] asm("_binary_index_html_gz_end");
extern const uint8_t proto_js_start[] asm("_binary_proto_js_gz_start");
extern const uint8_t proto_js_end[] asm("_binary_proto_js_gz_end");
extern const uint8_t wait_js_start[] asm("_binary_wait_js_gz_start");
extern const uint8_t wait_js_end[] asm("_binary_wait_js_gz_end");
extern const uint8_t panels_js_start[] asm("_binary_panels_js_gz_start");
extern const uint8_t panels_js_end[] asm("_binary_panels_js_gz_end");
extern const uint8_t app_js_start[] asm("_binary_app_js_gz_start");
extern const uint8_t app_js_end[] asm("_binary_app_js_gz_end");
extern const uint8_t style_css_start[] asm("_binary_style_css_gz_start");
extern const uint8_t style_css_end[] asm("_binary_style_css_gz_end");
/* Опис застосунку й значок: із ними сторінку кладуть на робочий стіл телефона
 * і запускають без браузерної обгортки — задача 0023, критерій 1.4. */
extern const uint8_t manifest_start[] asm("_binary_manifest_webmanifest_gz_start");
extern const uint8_t manifest_end[] asm("_binary_manifest_webmanifest_gz_end");
extern const uint8_t icon_png_start[] asm("_binary_icon_png_gz_start");
extern const uint8_t icon_png_end[] asm("_binary_icon_png_gz_end");

static httpd_handle_t s_server;
static SemaphoreHandle_t s_lock;

/** Сокет телефона, або -1. Під `s_lock`. */
static int s_client_fd = -1;

/**
 * Скільки байтів імені клієнта тримаємо, разом із кінцевим нулем.
 *
 * Ім'я потрібне рівно для одного порівняння — «це той самий, хто щойно тут
 * був?». Довшого за це не буває: клієнт складає його з випадкових цифр
 * (`webui/app.js`, `MY_ID`), а надто довге ми просто не візьмемо.
 */
#define WS_CLIENT_ID_MAX 17

/** Ім'я поточного господаря, або порожньо. Під `s_lock`. */
static char s_client_id[WS_CLIENT_ID_MAX];

/**
 * Стеля одночасних сокетів сервера — те саме число, що в `cfg.max_open_sockets`.
 *
 * ⚠️ Число **не міняється** цією задачею: воно множник у ціні сторінки
 * (4 × `CONFIG_LWIP_TCP_SND_BUF_DEFAULT`), і чіпати його дозволено лише із
 * заміром пропускної здатності до і після (рішення 2026-08-02). Тут воно
 * стоїть іменем, бо під нього виділяється масив у `ws_bridge_open_sockets()`.
 */
#define WS_MAX_SOCKETS 4

/** Коли востаннє прийшов пакет із вводом, мкс. 0 — не приходив. Під `s_lock`. */
static int64_t s_last_input_us;

/**
 * Коли поточний господар під'єднався, мкс. Під `s_lock`.
 *
 * Потрібне рівно для одного рішення: чи господар ще живий, коли на його ім'я
 * приходить «це знову я». Самого `s_last_input_us` для цього не досить — у
 * щойно під'єднаного він нульовий, і свіжий сокет виглядав би мертвим.
 */
static int64_t s_client_since_us;

/** Ввід уже відпущено за мовчанням — вдруге не відпускаємо. Під `s_lock`. */
static bool s_input_released;

/**
 * Скільки відправлень поспіль не вдалося.
 *
 * ⚠️ Пише `ws_tx_task` — і, один раз на сеанс, задача httpd у `client_settle`.
 * Доти тут стояло «пише лише `ws_tx_task`», і це було неправдою вже тоді:
 * обнулення при під'єднанні завжди робила чужа задача. Небезпеки немає —
 * обнулення відбувається до того, як `ws_tx_task` дізнається про новий сокет,
 * — але твердження в коментарі має бути правдою, інакше на нього спираються.
 */
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

int ws_bridge_open_sockets(void)
{
    if (!s_server) {
        return 0;
    }

    /* ⚠️ Число, а не відчуття. Ціна сторінки в купі моста множиться саме на
     * кількість одночасних сокетів (рішення 2026-08-02), і питання «скільки
     * їх тримає той, хто чекає» доти не мало приладу взагалі — на нього
     * відповідали міркуванням про браузер. */
    int fds[WS_MAX_SOCKETS];
    size_t n = WS_MAX_SOCKETS;
    if (httpd_get_client_list(s_server, &n, fds) != ESP_OK) {
        return -1;
    }
    return (int)n;
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
        /* ⚠️ Ім'я гасне разом із сокетом, і це не прибирання. Лишене, воно
         * дозволило б `resume` без живого господаря — тобто «тихо забрати
         * своє» у порожнього моста, куди й так пускають без питань, а гірше
         * того, у нового господаря, який зайшов **після** нас. */
        s_client_id[0] = '\0';
    }
    unlock();

    if (!mine) {
        return; /* уже загубили, або це вже не наш сокет */
    }

    g_stats.clients_lost++;

    /* Називаємо причину. Сума цих **семи** дорівнює `clients_lost` —
     * розбіжність означала б шлях втрати, про який ми не знаємо. Число
     * навмисно стоїть у тексті: коли причин побільшало, а рядок лишився
     * старим, звірка лічильників починає доводити не те. */
    switch (reason) {
    case BRIDGE_LOST_EVICTED:   g_stats.lost_evicted++; break;
    case BRIDGE_LOST_RESUMED:   g_stats.lost_resumed++; break;
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

/** Чим назвався прибулець у рядку запиту `/ws`. */
typedef struct {
    bool take;                    /**< `take=1` — свідоме переймання керування */
    bool resume;                  /**< `resume=1` — «я був тут, мій сокет помер» */
    char id[WS_CLIENT_ID_MAX];    /**< ім'я клієнта; порожнє — не назвався */
} ws_claim_t;

/**
 * @brief Прочитати заявку прибульця.
 *
 * ⚠️ Рядок запиту доступний і після рукостискання WebSocket: `httpd` шукає
 * обробник за **шляхом** (`httpd_uri.c:300`, розбір `UF_PATH`), а сам
 * `req->uri` лишається цілим разом із хвостом після `?`.
 */
static void read_claim(httpd_req_t *req, ws_claim_t *out)
{
    memset(out, 0, sizeof(*out));

    char query[96];
    if (httpd_req_get_url_query_str(req, query, sizeof(query)) != ESP_OK) {
        return; /* нічого не заявив — звичайне відкриття сторінки */
    }

    char one[8];
    if (httpd_query_key_value(query, "take", one, sizeof(one)) == ESP_OK) {
        out->take = (one[0] == '1');
    }
    if (httpd_query_key_value(query, "resume", one, sizeof(one)) == ESP_OK) {
        out->resume = (one[0] == '1');
    }
    /* Обрізане ім'я — те саме, що чуже: порівнювати його не можна, бо два
     * різні клієнти з довгими іменами злилися б в одного. */
    if (httpd_query_key_value(query, "id", out->id, sizeof(out->id)) != ESP_OK) {
        out->id[0] = '\0';
    }
}

/**
 * @brief Сказати клієнтові словами те, чого не скажеш розривом.
 *
 * ⚠️ Текстовий кадр, а не пакет нашого протоколу, і це межа за задумом: у
 * протоколі до пульта такої розмови немає й не має бути (обмеження задачі
 * 0024). Клієнт відрізняє їх у `onmessage` за типом даних — рядок проти
 * двійкового буфера.
 */
static void ws_say(int fd, const char *text)
{
    if (!s_server) {
        return;
    }

    httpd_ws_frame_t frame = {
        .final = true,
        .type = HTTPD_WS_TYPE_TEXT,
        .payload = (uint8_t *)text,
        .len = strlen(text),
    };
    /* ⚠️ Іде **штатною** передачею, а не нашою `send_all`: своя ставиться лише
     * на сокет господаря, а цей ним не став. Для десятка байтів це байдуже —
     * часткове відправлення такого кадру означало б, що в буфері немає й
     * десяти байтів, тобто мостові вже не до слів. */
    const esp_err_t err = httpd_ws_send_frame_async(s_server, fd, &frame);
    if (err != ESP_OK) {
        /* Не біда: клієнт однаково побачить закритий сокет і спитає
         * `/api/status`. Але мовчати не можна — це прямий шлях до «кнопка
         * не з'явилась, і незрозуміло чому». */
        ESP_LOGW(TAG, "не сказав сокету %d «%s» (%s)", fd, text, esp_err_to_name(err));
    }
}

static void client_settle(int fd)
{
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
}

/**
 * @brief Прибув телефон: пустити, віддати керування на вимогу або сказати
 *        «зайнято».
 *
 * ⚠️ Рішення цілком під одним замком, а дії — після нього. Інакше між
 * «побачив, що вільно» і «зайняв» устигає другий телефон, і обидва вважали б
 * себе господарем: той самий клас гонки, що й у `client_drop`, лише в
 * протилежний бік.
 */
static void client_arrived(int fd, const ws_claim_t *claim)
{
    lock();
    const int old = s_client_fd;
    const bool busy = (old >= 0 && old != fd);

    /* Скільки господар мовчить. Доки він не сказав нічого від самого
     * під'єднання, рахуємо від під'єднання: свіжий сокет ще не мав коли
     * заговорити, і вважати його мертвим не можна.
     *
     * ⚠️ Поріг тут **зв'язаний із клієнтом**, хоч і живе в іншій кодовій базі:
     * `RECONNECT_MS` у `webui/app.js` (1000 мс) мусить лишатись більшим за
     * `BRIDGE_CLIENT_SILENCE_MS` (750 мс). Запас — 250 мс, тобто один період
     * `INPUT_STATE`. Стане навпаки — і **кожне** звичайне перепідключення
     * після обриву впиратиметься у «Є активне підключення до пульта» з
     * кнопкою, тобто в ручну дію там, де її не має бути. */
    const int64_t now = esp_timer_get_time();
    const int64_t heard = (s_last_input_us != 0) ? s_last_input_us : s_client_since_us;
    const bool ghost = busy && ((now - heard) / 1000 > (int64_t)BRIDGE_CLIENT_SILENCE_MS);

    /* «Це знову я»: ім'я збігається з іменем господаря — **і того господаря
     * вже не чути**.
     *
     * ⚠️ Друга половина умови не педантизм, а блокер, знайдений рецензією.
     * Ім'я живе одне завантаження сторінки, тож збіг означає, що це наш
     * **власний** інший сокет. Якщо перший при цьому живий і шле ввід,
     * «повернення свого» стає витісненням самого себе: перший бачить розрив,
     * вертається через секунду з тим самим іменем, витісняє другий — і це
     * знову гойдалка, лише з одного клієнта. Вона навіть не потрапила б у
     * `lost_evicted`, тобто повз усі критерії задачі.
     *
     * Порожнє ім'я не збігається ні з чим — `strcmp` тут не досить. */
    const bool mine = busy && ghost && claim->resume && claim->id[0] != '\0' &&
                      strcmp(claim->id, s_client_id) == 0;
    const bool allow = !busy || claim->take || mine;

    if (allow) {
        s_client_fd = fd;
        s_client_since_us = now;
        s_last_input_us = 0;
        s_input_released = false;
        snprintf(s_client_id, sizeof(s_client_id), "%s", claim->id);
    }
    unlock();

    if (!allow) {
        /* ⚠️ Стану моста ця гілка не змінює **жодного біта**: господар лишився
         * той самий, ввід не відпускається, сеанс не рахується. Прибулець
         * отримує слово «зайнято» й закритий сокет — і чекає далі рідкісним
         * опитуванням `/api/status`, без сокета й без трафіку. */
        g_stats.busy_refused++;
        ESP_LOGI(TAG, "сокет %d прийшов на зайнятий пульт — кажу «зайнято»", fd);
        ws_say(fd, "{\"busy\":1}");
        if (s_server) {
            httpd_sess_trigger_close(s_server, fd);
        }
        return;
    }

    /* ⚠️ Рахуємо **заявку**, а не її наслідок, і тому окремо від
     * `lost_evicted`: кнопку могли натиснути в ту саму мить, коли інший
     * телефон уже пішов сам. Тоді витіснення не було, а свідома дія була. */
    if (claim->take) {
        g_stats.takeovers++;
    } else if (claim->resume) {
        g_stats.resumes++;
    }

    client_settle(fd);

    if (busy) {
        /* Ввід обнуляємо: старий міг щось утримувати, а новий заявить власний
         * стан не пізніше ніж за 250 мс (docs/03-protocol.md, INPUT_STATE —
         * рівень, а не перехід). */
        const bool taken = claim->take;
        if (taken) {
            ESP_LOGW(TAG, "керування перейнято (сокет %d → %d)", old, fd);
        } else {
            ESP_LOGW(TAG, "господар повернувся на свій слот (сокет %d → %d)", old, fd);
        }
        uart_link_send_input_release(taken ? BRIDGE_LOST_EVICTED : BRIDGE_LOST_RESUMED);
        g_stats.clients_lost++;
        if (taken) {
            g_stats.lost_evicted++;
        } else {
            g_stats.lost_resumed++;
        }
        if (s_server) {
            httpd_sess_trigger_close(s_server, old);
        }
    }

    /* ⚠️ Останнім рядком, а не першим: інакше журнал читається навиворіт —
     * «телефон під'єднався» стояло б перед «керування перейнято». */
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
        /* Рукостискання WebSocket завершено — лишилось вирішити, чи цей сокет
         * стає господарем. Заявку читаємо тут-таки: далі `req` не живе. */
        ws_claim_t claim;
        read_claim(req, &claim);
        client_arrived(httpd_req_to_sockfd(req), &claim);
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

    /* ⚠️ На дріт пускаємо **тільки господаря**, і цей рядок з'явився не з
     * обережності, а за знахідкою рецензії.
     *
     * Доти негосподарських сокетів не існувало взагалі: хто під'єднався, той і
     * говорив. Тепер їх два види — той, кому сказали «зайнято» (гасимо
     * відкладено), і той, у кого щойно перейняли керування. Обидва встигають
     * дописати в пульт: клієнт шле `PING` **негайно** з `onopen`, а витіснений
     * міг би повернути свій `INPUT_STATE` уже **після** обнулення, тобто
     * знову натиснути клавішу, яку ми щойно відпустили (критерій 2.5 задачі
     * 0024).
     *
     * Порядок подій `esp_http_server` це сьогодні затуляє — відкладене
     * закриття виконується раніше за читання даних, — але то властивість
     * чужої реалізації, а не наша гарантія. */
    if (fd != ws_bridge_client_fd()) {
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

/**
 * @brief Віддати вбудований файл сторінки.
 *
 * ⚠️ Позначка `heap_watch_page_enter/leave` тут не діагностична прикраса, а
 * єдине місце, де взагалі можна приписати просідання купи віддачі сторінки.
 * Задача 0020 записала «вільної пам'яті 1.9 КБ за сеанс із одним клієнтом» і
 * шукала винного в потоці пікселів — тоді як просідання належало цим
 * кільком рядкам і тривало десяті частки секунди.
 */
static esp_err_t send_blob(httpd_req_t *req, const char *ctype, const uint8_t *start,
                           const uint8_t *end)
{
    httpd_resp_set_type(req, ctype);
    /* Вміст стиснений при збірці — див. оголошення символів вище. */
    httpd_resp_set_hdr(req, "Content-Encoding", "gzip");
    /* Без кешу: сторінка живе в прошивці й міняється разом із нею. */
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    /* ⚠️ Сокет після файлу гасимо, і це заміряна потреба, а не охайність.
     *
     * Браузер тягне сторінку кількома з'єднаннями й лишає їх відкритими: у
     * `esp_http_server` тайм-ауту простою немає, тож вони живуть до кінця
     * світу. Заміряно на живому мості 2026-08-03: **одне** вікно клієнта
     * тримало всі **чотири** гнізда сервера (`session.sockets` = 4 при
     * `max_open_sockets` = 4). Другому вікну гнізда вже не лишалось: `httpd`
     * при `lru_purge_enable` гасив найдавніший сеанс — тобто **WebSocket
     * першого клієнта**, — і замість черги виходила та сама гойдалка, лише
     * влаштована іншим механізмом.
     *
     * Ці файли віддаються рівно раз на завантаження сторінки й лежать під
     * `no-store`, тож тримати під них з'єднання нема для чого. Після правки
     * одне вікно тримає два гнізда: WebSocket і опитування `/api/stats`. */
    httpd_resp_set_hdr(req, "Connection", "close");

    heap_watch_page_enter();
    const esp_err_t err = httpd_resp_send(req, (const char *)start, (size_t)(end - start));
    heap_watch_page_leave();

    /* Закриття виконається після цього обробника — `httpd` кладе його в чергу
     * власної задачі, тож відповідь устигає піти цілком. */
    if (s_server) {
        httpd_sess_trigger_close(s_server, httpd_req_to_sockfd(req));
    }
    return err;
}

static esp_err_t index_get(httpd_req_t *req)
{
    return send_blob(req, "text/html; charset=utf-8", index_html_start, index_html_end);
}

static esp_err_t protojs_get(httpd_req_t *req)
{
    return send_blob(req, "application/javascript; charset=utf-8", proto_js_start, proto_js_end);
}

/* Політика показу кадру — окремий модуль клієнта, без DOM (задача 0020). */
static esp_err_t waitjs_get(httpd_req_t *req)
{
    return send_blob(req, "application/javascript; charset=utf-8", wait_js_start, wait_js_end);
}

/* Намір, розкладка панелей і розгін енкодера — теж без DOM (задача 0022). */
static esp_err_t panelsjs_get(httpd_req_t *req)
{
    return send_blob(req, "application/javascript; charset=utf-8", panels_js_start, panels_js_end);
}

static esp_err_t appjs_get(httpd_req_t *req)
{
    return send_blob(req, "application/javascript; charset=utf-8", app_js_start, app_js_end);
}

static esp_err_t css_get(httpd_req_t *req)
{
    return send_blob(req, "text/css; charset=utf-8", style_css_start, style_css_end);
}

/* ⚠️ Тип обов'язковий: із чужим типом браузер опис застосунку мовчки
 * ігнорує, і сторінка з робочого столу відкривається знову в обгортці. */
static esp_err_t manifest_get(httpd_req_t *req)
{
    return send_blob(req, "application/manifest+json; charset=utf-8",
                     manifest_start, manifest_end);
}

/* ⚠️ Значок теж їде стисненим — `send_blob` ставить `Content-Encoding: gzip`
 * безумовно. Для PNG це не виграш (512 Б проти 513), але й не втрата, а
 * винятку в спільній дорозі не з'явилось. */
static esp_err_t icon_get(httpd_req_t *req)
{
    return send_blob(req, "image/png", icon_png_start, icon_png_end);
}

/**
 * @brief Почати нове вікно заміру купи.
 *
 * ⚠️ Існує рівно тому, що `heap_min` від ESP-IDF скинути неможливо: він
 * монотонний від старту. Без цієї ручки кожен замір пам'яті починався б із
 * перезавантаження моста — а перезавантаження рве Wi-Fi, змушує ПК
 * перепід'єднуватись і саме собою породжує сплеск, який ми ж і міряємо.
 */
static esp_err_t heap_reset_post(httpd_req_t *req)
{
    heap_watch_window_reset();
    httpd_resp_set_type(req, "application/json");
    /* ⚠️ Вертаємо вільне **зараз**, а не `heap_watch_window_min()`: скид
     * виконає найближчий знімок (до 10 мс), тож мінімум вікна в цю мить ще
     * старий, і надрукований тут він виглядав би як «скид не спрацював». */
    char json[80];
    const int n = snprintf(json, sizeof(json), "{\"ok\":true,\"heap_free\":%u}",
                           (unsigned)esp_get_free_heap_size());
    return httpd_resp_send(req, json, n);
}

/**
 * @brief Чи зайнятий пульт — найдешевша відповідь, яку вміє міст.
 *
 * ⚠️ Окремий шлях, а не `/api/stats`, і це не смак. Цим питанням живе клієнт,
 * який **стоїть у черзі**: він питає раз на дві секунди, і питати повний стан
 * (2.5 КБ JSON плюс обхід усіх лічильників) означало б, що черга коштує
 * дорожче за роботу. Тут — три десятки байтів.
 */
static esp_err_t status_get(httpd_req_t *req)
{
    g_stats.status_polls++;

    char json[64];
    const int n = snprintf(json, sizeof(json), "{\"busy\":%d,\"sockets\":%d}",
                           ws_bridge_has_client() ? 1 : 0, ws_bridge_open_sockets());
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    /* ⚠️ Сокет після відповіді гасимо, і це не причісування, а виконання
     * обіцянки «той, хто чекає, не тримає сокета». Інакше браузер лишає
     * з'єднання відкритим назавжди (тайм-ауту простою `esp_http_server` не
     * має), і забута вкладка постійно займає одне гніздо з чотирьох — те саме
     * число, навколо якого крутиться вся ціна сторінки в купі (0022).
     *
     * Заголовок і закриття — разом: без заголовка браузер спробує повторно
     * скористатися сокетом, який ми вже гасимо, і замість відповіді отримає
     * обрив. Саме закриття виконається після цього обробника: `httpd` кладе
     * його в чергу власної задачі. */
    httpd_resp_set_hdr(req, "Connection", "close");
    const esp_err_t err = httpd_resp_send(req, json, n);
    if (s_server) {
        httpd_sess_trigger_close(s_server, httpd_req_to_sockfd(req));
    }
    return err;
}

static esp_err_t stats_get(httpd_req_t *req)
{
    /* Росте разом із набором лічильників. Замалий буфер більше не обрізає
     * JSON мовчки — `bridge_stats_json()` скаржиться в журнал.
     *
     * ⚠️ Запас порахований 2026-08-03, після появи лічильників черги: сталого
     * тексту 1130 Б, полів 66 (найгірше по 10 знаків) і вкладений `heap` до
     * 639 Б — разом **2448 із 2560**. Тобто місця лишилось на два-три нові
     * числа, а не на десять.
     *
     * ⚠️ Лежить на стеку задачі httpd (`cfg.stack_size` нижче), і разом із
     * буфером приладу купи (640 Б у `stats.c`) з'їдає 3.2 КБ із 8. Заміряний
     * найменший запас після цього — **2564 Б** (`httpd_stack_free`). Запас є,
     * але він уже не «купа»: наступний, хто захоче тут ще кілобайт, мусить
     * підняти `cfg.stack_size`, а не сподіватись. Переповнення стека дало б
     * паніку моста, схожу на просадку живлення. */
    char json[2560];
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

    /* ⚠️ Перелік стоїть **до** налаштувань навмисно: стеля кількості шляхів
     * рахується з нього самого (`cfg.max_uri_handlers` нижче). Доти число
     * стояло окремо з приміткою «додаєш шлях — звір із цим числом», і
     * перебір упав би не при збірці, а на старті моста, у полі. */
    static const httpd_uri_t uris[] = {
        {.uri = "/", .method = HTTP_GET, .handler = index_get},
        {.uri = "/index.html", .method = HTTP_GET, .handler = index_get},
        {.uri = "/proto.js", .method = HTTP_GET, .handler = protojs_get},
        {.uri = "/wait.js", .method = HTTP_GET, .handler = waitjs_get},
        {.uri = "/panels.js", .method = HTTP_GET, .handler = panelsjs_get},
        {.uri = "/app.js", .method = HTTP_GET, .handler = appjs_get},
        {.uri = "/style.css", .method = HTTP_GET, .handler = css_get},
        {.uri = "/manifest.webmanifest", .method = HTTP_GET, .handler = manifest_get},
        {.uri = "/icon.png", .method = HTTP_GET, .handler = icon_get},
        {.uri = "/api/status", .method = HTTP_GET, .handler = status_get},
        {.uri = "/api/stats", .method = HTTP_GET, .handler = stats_get},
        {.uri = "/api/heap/reset", .method = HTTP_POST, .handler = heap_reset_post},
        {.uri = "/api/wifi/off", .method = HTTP_POST, .handler = wifi_off_post},
        {.uri = "/ws", .method = HTTP_GET, .handler = ws_handler, .is_websocket = true},
    };

    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.stack_size = 8192; /* у обробнику лежить буфер на BRIDGE_WS_RX_MAX */
    cfg.max_open_sockets = WS_MAX_SOCKETS;
    cfg.max_uri_handlers = sizeof(uris) / sizeof(uris[0]);
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

    for (size_t i = 0; i < sizeof(uris) / sizeof(uris[0]); ++i) {
        ESP_ERROR_CHECK(httpd_register_uri_handler(s_server, &uris[i]));
    }

    ESP_LOGI(TAG, "сервер піднято: сторінка на /, потік на /ws, лічильники на /api/stats");
    return ESP_OK;
}
